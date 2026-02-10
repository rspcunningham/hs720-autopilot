#!/usr/bin/env python3
"""
Drone relay service for Raspberry Pi.

Maintains a single connection to the HS720 drone and multiplexes
it to any number of clients on the home network.

State machine: SCANNING → WIFI_CONNECTING → DRONE_CONNECTING → READY
On failure at any stage, returns to SCANNING.

Clients connect to TCP 4900 and receive:
  - A GOL status frame (cmd 0xFF000) immediately on connect
  - All GOL frames from the drone, verbatim
  - Updated status frames on state changes

Video: clients send any UDP packet to port 4901 to register.
       Relay forwards all drone video packets to registered clients.

Serial: clients send UDP to port 4902, relay forwards to drone:17000.
"""

import json
import logging
import os
import socket
import struct
import subprocess
import threading
import time

from auth import LogCheck, load_key_table, log_decode_key, log_encode_key
from protocol import (
    GOL_AUTH_CHALLENGE, GOL_AUTH_RESPONSE, GOL_DEV_CTL, GOL_DEV_INFO,
    GOL_EXT_CMD, GOL_RELAY_STATUS, GOL_SD_CFG, GOL_SP_CFG,
    GOL_ST_PLAY, GOL_ST_STOP,
    gol_unwrap, gol_wrap,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYTABLE_PATH = os.path.join(BASE_DIR, "keytable.bin")

DRONE_SSID = os.environ.get("DRONE_SSID", "HolyStoneFPV-4bc278D")
DRONE_IP = os.environ.get("DRONE_IP", "172.16.11.1")
DRONE_TCP_PORT = 18000   # protocol constant
DRONE_VIDEO_PORT = 16000  # protocol constant
DRONE_SERIAL_PORT = 17000  # protocol constant

RELAY_TCP_PORT = int(os.environ.get("RELAY_TCP_PORT", "4900"))
RELAY_VIDEO_PORT = int(os.environ.get("RELAY_VIDEO_PORT", "4901"))
RELAY_SERIAL_PORT = int(os.environ.get("RELAY_SERIAL_PORT", "4902"))

WIFI_IFACE = os.environ.get("WIFI_IFACE", "wlan0")
SCAN_INTERVAL = 5
VIDEO_CLIENT_TIMEOUT = 30

log = logging.getLogger("relay")


class RelayState:
    SCANNING = "scanning"
    WIFI_CONNECTING = "wifi_connecting"
    DRONE_CONNECTING = "drone_connecting"
    READY = "ready"


class DroneRelay:
    def __init__(self):
        self.state = RelayState.SCANNING
        self._key_table = load_key_table(KEYTABLE_PATH)

        # Drone connection
        self._drone_sock: socket.socket | None = None

        # Client management
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()

        # Video fan-out: {(ip, port): last_seen_time}
        self._video_clients: dict[tuple, float] = {}
        self._video_clients_lock = threading.Lock()

        # Threads
        self._running = True

    def run(self):
        """Main loop — state machine."""
        # Start client-facing servers in background
        threading.Thread(target=self._tcp_accept_loop, daemon=True).start()
        threading.Thread(target=self._video_relay_loop, daemon=True).start()
        threading.Thread(target=self._serial_relay_loop, daemon=True).start()

        log.info("Relay started — TCP=%d VIDEO=%d SERIAL=%d",
                 RELAY_TCP_PORT, RELAY_VIDEO_PORT, RELAY_SERIAL_PORT)

        while self._running:
            try:
                if self.state == RelayState.SCANNING:
                    self._do_scanning()
                elif self.state == RelayState.WIFI_CONNECTING:
                    self._do_wifi_connect()
                elif self.state == RelayState.DRONE_CONNECTING:
                    self._do_drone_connect()
                elif self.state == RelayState.READY:
                    self._do_ready()
            except Exception as e:
                log.error("State %s error: %s", self.state, e)
                self._set_state(RelayState.SCANNING)
                time.sleep(2)

    def _set_state(self, new_state: str):
        if new_state != self.state:
            log.info("State: %s → %s", self.state, new_state)
            self.state = new_state
            self._broadcast_status()

    # --- State handlers ---

    def _do_scanning(self):
        """Scan for drone WiFi."""
        # Check if already connected to drone SSID
        current = self._get_current_ssid()
        if current == DRONE_SSID:
            log.info("Already connected to %s", DRONE_SSID)
            self._set_state(RelayState.DRONE_CONNECTING)
            return

        # Scan for drone SSID
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list",
                 "ifname", WIFI_IFACE, "--rescan", "yes"],
                capture_output=True, text=True, timeout=15
            )
            ssids = [line.strip() for line in result.stdout.splitlines()]
            if DRONE_SSID in ssids:
                log.info("Found %s", DRONE_SSID)
                self._set_state(RelayState.WIFI_CONNECTING)
                return
            else:
                log.info("Scanning... (%d SSIDs visible, drone not found)", len(ssids))
        except Exception as e:
            log.warning("WiFi scan failed: %s", e)

        time.sleep(SCAN_INTERVAL)

    def _do_wifi_connect(self):
        """Connect to drone WiFi."""
        try:
            result = subprocess.run(
                ["nmcli", "dev", "wifi", "connect", DRONE_SSID,
                 "ifname", WIFI_IFACE],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode == 0:
                log.info("WiFi connected to %s", DRONE_SSID)
                time.sleep(1)  # Let the interface settle
                self._set_state(RelayState.DRONE_CONNECTING)
            else:
                log.warning("WiFi connect failed: %s", result.stderr.strip())
                self._set_state(RelayState.SCANNING)
        except Exception as e:
            log.warning("WiFi connect error: %s", e)
            self._set_state(RelayState.SCANNING)

    def _do_drone_connect(self):
        """TCP connect + auth + init with the drone."""
        try:
            self._drone_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._drone_sock.settimeout(10)
            self._drone_sock.connect((DRONE_IP, DRONE_TCP_PORT))
            log.info("TCP connected to drone")

            self._authenticate()
            self._init_sequence()

            self._set_state(RelayState.READY)
        except Exception as e:
            log.warning("Drone connect failed: %s", e)
            self._close_drone()
            self._set_state(RelayState.SCANNING)

    def _do_ready(self):
        """Relay data between drone and clients."""
        assert self._drone_sock is not None
        self._drone_sock.settimeout(2)
        buf = b""
        while self._running and self.state == RelayState.READY:
            try:
                data = self._drone_sock.recv(4096)
                if not data:
                    log.warning("Drone connection closed")
                    break
                buf += data

                # Forward complete GOL frames to all clients
                frames = gol_unwrap(buf)
                if frames:
                    # Find end of last parsed frame
                    last_footer = buf.rfind(b"\xffGOL")
                    raw_to_send = buf[:last_footer + 4] if last_footer >= 0 else buf
                    buf = buf[last_footer + 4:] if last_footer >= 0 else b""

                    self._broadcast_raw(raw_to_send)
                elif len(buf) > 16384:
                    buf = buf[-4096:]

            except socket.timeout:
                # Check WiFi still connected
                if self._get_current_ssid() != DRONE_SSID:
                    log.warning("WiFi disconnected")
                    break
                continue
            except Exception as e:
                log.warning("Drone recv error: %s", e)
                break

        self._close_drone()
        self._set_state(RelayState.SCANNING)

    # --- Auth ---

    def _authenticate(self):
        """3-message auth handshake with drone."""
        log.info("Authenticating...")
        data = self._recv_until_gol()
        frames = gol_unwrap(data)
        if not frames:
            raise ConnectionError("No GOL frame in challenge")

        cmd_id, rwbit, payload = frames[0]
        if cmd_id != GOL_AUTH_CHALLENGE or len(payload) < 24:
            raise ConnectionError(f"Bad challenge: cmd=0x{cmd_id:x} len={len(payload)}")

        ck = LogCheck(payload[:24])
        result, user_id = log_decode_key(0, 1, ck, self._key_table)
        if result != 0:
            raise ConnectionError(f"Auth decode failed: {result}")

        old_ck_app = ck.ck_app
        ck.ck_app = struct.unpack('<I', os.urandom(4))[0]
        while ck.ck_app == old_ck_app:
            ck.ck_app = struct.unpack('<I', os.urandom(4))[0]

        log_encode_key(0, 1, ck, user_id, self._key_table)
        frame = gol_wrap(ck.to_bytes(), GOL_AUTH_RESPONSE)
        assert self._drone_sock is not None
        self._drone_sock.sendall(frame)

        data = self._recv_until_gol()
        frames = gol_unwrap(data)
        if not frames:
            raise ConnectionError("No auth ack")
        if frames[0][1] & 0x80000000:
            raise ConnectionError("Auth rejected")

        log.info("Auth complete (UserId=%d)", user_id)

    def _init_sequence(self):
        """Post-auth init matching the app's startup sequence."""
        assert self._drone_sock is not None
        sock = self._drone_sock
        def send(payload, cmd_id):
            frame = gol_wrap(payload, cmd_id)
            sock.sendall(frame)
            time.sleep(0.05)

        send(b"\x00", GOL_DEV_INFO)
        send(json.dumps({"DevCtlPower": 1}).encode() + b"\x00", GOL_DEV_CTL)
        send(json.dumps({"DevCtlPower": 0}).encode() + b"\x00", GOL_DEV_CTL)
        send(json.dumps({"StRelIdx": -1, "VStOpen": 1}).encode() + b"\x00", GOL_ST_STOP)
        send(b"\x00", GOL_SP_CFG)
        send(b"\x00", GOL_ST_PLAY)
        send(b"\x00", GOL_SD_CFG)
        send(json.dumps({"cmd": 65536}).encode() + b"\x00", GOL_EXT_CMD)

        ts = time.strftime("%Y%m%d-%H%M%S") + f"{int(time.time()*100)%100:02d}"
        send(json.dumps({"cmd": 131072, "data": {"RealtimeSet": ts}}).encode() + b"\x00",
             GOL_EXT_CMD)

        log.info("Init sequence complete")

    def _recv_until_gol(self, timeout=10.0):
        assert self._drone_sock is not None
        self._drone_sock.settimeout(timeout)
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self._drone_sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during recv")
            buf += chunk
            if gol_unwrap(buf):
                return buf
        raise TimeoutError("No GOL frame received")

    # --- Client TCP server ---

    def _tcp_accept_loop(self):
        """Accept client TCP connections on RELAY_TCP_PORT."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", RELAY_TCP_PORT))
        srv.listen(5)
        srv.settimeout(2)
        log.info("TCP server listening on :%d", RELAY_TCP_PORT)

        while self._running:
            try:
                client_sock, addr = srv.accept()
                log.info("Client connected: %s:%d", *addr)
                with self._clients_lock:
                    self._clients.append(client_sock)

                # Send current status immediately
                self._send_status(client_sock)

                # Start a thread to read commands from this client
                threading.Thread(target=self._client_recv_loop,
                                 args=(client_sock, addr), daemon=True).start()
            except socket.timeout:
                continue

    def _client_recv_loop(self, client_sock: socket.socket, addr: tuple):
        """Read GOL frames from a client and forward to drone."""
        client_sock.settimeout(5)
        while self._running:
            try:
                data = client_sock.recv(4096)
                if not data:
                    break
                # Forward to drone if connected
                if self._drone_sock and self.state == RelayState.READY:
                    try:
                        self._drone_sock.sendall(data)
                    except Exception:
                        pass
            except socket.timeout:
                continue
            except Exception:
                break

        log.info("Client disconnected: %s:%d", *addr)
        with self._clients_lock:
            if client_sock in self._clients:
                self._clients.remove(client_sock)
        try:
            client_sock.close()
        except Exception:
            pass

    # --- Video relay ---

    def _video_relay_loop(self):
        """Receive video from drone (UDP 16000), fan out to clients."""
        # Socket to receive video from drone
        drone_video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        drone_video.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        drone_video.bind(("0.0.0.0", DRONE_VIDEO_PORT))
        drone_video.settimeout(2)

        # Socket to receive client registrations and send video to clients
        client_video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_video.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        client_video.bind(("0.0.0.0", RELAY_VIDEO_PORT))
        client_video.settimeout(0.5)

        # Thread to handle client video registrations
        def client_reg_loop():
            while self._running:
                try:
                    _, addr = client_video.recvfrom(1024)
                    with self._video_clients_lock:
                        self._video_clients[addr] = time.time()
                    log.info("Video client registered: %s:%d", *addr)
                except socket.timeout:
                    continue
                except Exception:
                    break

        threading.Thread(target=client_reg_loop, daemon=True).start()

        log.info("Video relay listening — drone=:%d clients=:%d",
                 DRONE_VIDEO_PORT, RELAY_VIDEO_PORT)

        while self._running:
            try:
                data, _ = drone_video.recvfrom(65536)
                # Fan out to all registered video clients
                now = time.time()
                with self._video_clients_lock:
                    # Clean expired clients
                    expired = [a for a, t in self._video_clients.items()
                               if now - t > VIDEO_CLIENT_TIMEOUT]
                    for a in expired:
                        del self._video_clients[a]
                        log.info("Video client expired: %s:%d", *a)
                    targets = list(self._video_clients.keys())

                for addr in targets:
                    try:
                        client_video.sendto(data, addr)
                    except Exception:
                        pass
            except socket.timeout:
                continue
            except Exception as e:
                log.warning("Video relay error: %s", e)
                time.sleep(1)

    # --- Serial relay ---

    def _serial_relay_loop(self):
        """Receive serial packets from clients (UDP), forward to drone."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", RELAY_SERIAL_PORT))
        sock.settimeout(2)
        log.info("Serial relay listening on :%d", RELAY_SERIAL_PORT)

        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                # Forward to drone
                sock.sendto(data, (DRONE_IP, DRONE_SERIAL_PORT))
            except socket.timeout:
                continue
            except Exception as e:
                log.warning("Serial relay error: %s", e)
                time.sleep(1)

    # --- Broadcast helpers ---

    def _broadcast_raw(self, data: bytes):
        """Send raw bytes to all connected TCP clients."""
        with self._clients_lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass

    def _broadcast_status(self):
        """Send current status to all connected clients."""
        with self._clients_lock:
            for c in list(self._clients):
                try:
                    self._send_status(c)
                except Exception:
                    pass

    def _send_status(self, sock: socket.socket):
        """Send a relay status GOL frame to a single client."""
        status = {"state": self.state}
        payload = json.dumps(status).encode() + b"\x00"
        frame = gol_wrap(payload, GOL_RELAY_STATUS)
        try:
            sock.sendall(frame)
        except Exception:
            pass

    # --- Helpers ---

    def _get_current_ssid(self) -> str:
        """Get the SSID wlan0 is currently connected to."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show", WIFI_IFACE],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if ":" in line:
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _close_drone(self):
        if self._drone_sock:
            try:
                self._drone_sock.close()
            except Exception:
                pass
            self._drone_sock = None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    relay = DroneRelay()
    try:
        relay.run()
    except KeyboardInterrupt:
        log.info("Shutting down")
        relay._running = False
