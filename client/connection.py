"""
Client connection to the drone relay (Pi).
No auth needed — the relay handles drone authentication.
"""

import json
import socket
import threading
import time

from .logger import FlightLogger
from .protocol import (
    GOL_RELAY_STATUS, GOL_SRL_DATA, GOL_SP_CFG, MsgId, START_BYTE,
    gol_unwrap, gol_wrap,
)
from .telemetry import FlightState, parse_telemetry


class Connection:
    def __init__(self, host: str = "pi1.local", port: int = 4900,
                 logger: FlightLogger | None = None):
        self.host = host
        self.port = port
        self.logger = logger
        self._sock: socket.socket | None = None
        self._running = False
        self._recv_thread: threading.Thread | None = None
        self._state: FlightState | None = None
        self._lock = threading.Lock()
        self.relay_state: str = "unknown"
        self.on_telemetry = None   # callback(FlightState)
        self.on_raw_gol = None     # callback(cmd_id, rwbit, payload)
        self.on_relay_status = None  # callback(dict)

    @property
    def state(self) -> FlightState | None:
        with self._lock:
            return self._state

    @property
    def connected(self) -> bool:
        return self._running and self._sock is not None

    def connect(self, timeout: float = 10.0):
        """Connect to the relay and start receiving."""
        print(f"[*] Connecting to relay {self.host}:{self.port}...")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((self.host, self.port))
        print(f"[+] Connected to relay")

        if self.logger:
            self.logger.log("relay_connect", host=self.host, port=self.port)

        # Read the initial status frame
        self._sock.settimeout(5)
        try:
            data = self._sock.recv(4096)
            frames = gol_unwrap(data)
            for cmd_id, rwbit, payload in frames:
                if cmd_id == GOL_RELAY_STATUS:
                    self._handle_relay_status(payload)
        except socket.timeout:
            print("[!] No status frame from relay")

        self._running = True
        self._sock.settimeout(2)
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        print(f"[+] Receive loop started (relay state: {self.relay_state})")

    def disconnect(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self.logger:
            self.logger.log("disconnect")
        print("[*] Disconnected from relay")

    def send_gol(self, payload: bytes, cmd_id: int, rwbit: int | None = None):
        """Send a GOL frame through the relay to the drone."""
        if not self._sock:
            return
        frame = gol_wrap(payload, cmd_id, rwbit)
        self._sock.sendall(frame)
        if self.logger:
            self.logger.log("gol_send", cmd=f"0x{cmd_id:06x}",
                            size=len(payload), payload_hex=payload.hex())

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until relay reports drone is ready, or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.relay_state == "ready":
                return True
            time.sleep(0.5)
        return False

    # --- Receive ---

    def _recv_loop(self):
        buf = b""
        while self._running and self._sock:
            try:
                data = self._sock.recv(4096)
                if not data:
                    print("[-] Relay connection closed")
                    break
                buf += data
                buf = self._process_buffer(buf)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[-] Recv error: {e}")
                break

    def _process_buffer(self, data: bytes) -> bytes:
        frames = gol_unwrap(data)
        if not frames:
            if len(data) > 16384:
                return data[-4096:]
            return data

        for cmd_id, rwbit, payload in frames:
            if cmd_id == GOL_RELAY_STATUS:
                self._handle_relay_status(payload)
                continue

            # Log every received GOL frame
            if self.logger:
                self.logger.log("gol_recv", cmd=f"0x{cmd_id:06x}", rw=rwbit,
                                size=len(payload), payload_hex=payload[:64].hex())

            if cmd_id in (GOL_SRL_DATA, GOL_SP_CFG) and payload and payload[0] == START_BYTE:
                self._handle_aa_packet(payload)
            elif self.on_raw_gol:
                self.on_raw_gol(cmd_id, rwbit, payload)

        last_footer = data.rfind(b"\xffGOL")
        if last_footer >= 0:
            return data[last_footer + 4:]
        return b""

    def _handle_relay_status(self, payload: bytes):
        try:
            text = payload.rstrip(b"\x00").decode()
            status = json.loads(text)
            self.relay_state = status.get("state", "unknown")
            if self.logger:
                self.logger.log("relay_status", **status)
            if self.on_relay_status:
                self.on_relay_status(status)
            print(f"[*] Relay state: {self.relay_state}")
        except Exception:
            pass

    def _handle_aa_packet(self, data: bytes):
        if not data or data[0] != START_BYTE:
            return
        msg_id = data[1] if len(data) > 1 else 0

        if msg_id == MsgId.GPSInFo:
            state = parse_telemetry(data)
            if state:
                with self._lock:
                    self._state = state
                if self.logger:
                    self.logger.log("telemetry",
                                    mode=state.mode, mode_name=state.mode_name,
                                    lat=state.lat, lon=state.lon,
                                    altitude=state.altitude, distance=state.distance,
                                    voltage=state.voltage, satellites=state.satellites,
                                    speed=state.speed, yaw=state.yaw)
                if self.on_telemetry:
                    self.on_telemetry(state)

        elif msg_id == MsgId.ControlInfo:
            if self.logger:
                self.logger.log("control_info", raw_hex=data.hex())

        else:
            if self.logger:
                self.logger.log("aa_packet", msg_id=f"0x{msg_id:02x}",
                                raw_hex=data.hex())
