"""
Holy Stone HS720 Drone Control
Protocol decoded from Ophelia GO APK (com.opheliago)

Architecture: App → WiFi Module (GOL/TCP:18000) → UART → Flight Controller (0xAA)
GOL authentication required before sending 0xAA commands.
"""

import argparse
import json
import socket
import struct
import time
import threading
import sys
import os

from decode_key import LogCheck, load_key_table, log_decode_key, log_encode_key

DRONE_IP = "172.16.11.1"
GOL_PORT = 18000  # GOL protocol port (auth + command transport)
KEYTABLE_PATH = os.path.join(os.path.dirname(__file__), "keytable.bin")

# --- Protocol Constants ---

START_BYTE = 0xAA


class MsgId:
    GPSInFo         = 0x01
    SetFence        = 0x02
    SetRadius       = 0x03
    SetFollow       = 0x04
    ExitFollow      = 0x05
    SetCircle       = 0x06
    ExitCircle      = 0x07
    SetCruise       = 0x08
    ExitCruise      = 0x09
    UpCruise        = 0x10
    SetTurnBack     = 0x16
    ExitTurnBack    = 0x17
    AppPhoto        = 0x18
    AppRecord       = 0x19
    WiFiChannel     = 0x1A
    CtrlCmd         = 0x1C
    SetCmd          = 0x1D
    SetPtzCmd       = 0x1E
    PTZInFo         = 0x20
    ControlInfo     = 0x30
    SetFlyPtzCmd    = 0x50
    SignOutFlyCmd   = 0x51
    SetUpFlyPtzCmd  = 0x52
    SignOutUpFlyCmd = 0x53

# SetCmd sub-commands
CMD_HEADLESS     = 0x01
CMD_GYRO_CAL     = 0x02
CMD_GEO_CAL      = 0x04
CMD_TAKEOFF_LAND = 0x08
CMD_UNLOCK       = 0x04

# CtrlCmd flag bits
FLAG_LOCK       = 0x01
FLAG_HIGH_SPEED = 0x02
FLAG_STOP       = 0x04
FLAG_GPS_MODE   = 0x08
FLAG_LIGHT      = 0x10
FLAG_PTZ_UP     = 0x20
FLAG_PTZ_DOWN   = 0x40

FLIGHT_MODES = {
    0: "Locked (no GPS)", 1: "Altitude hold", 2: "Position hold",
    3: "Return to home", 4: "Follow me", 5: "Circle/orbit",
    6: "Waypoint path", 7: "Shutdown", 8: "Gyro calibration",
    9: "Compass H cal", 10: "Compass V cal", 11: "Landing",
    12: "Idle", 13: "Locked (GPS)", 14: "Fly forward",
    15: "Fly backward", 16: "Distance fly", 17: "Point circle",
}

# --- GOL Protocol ---

GOL_HEADER = b"\x01GOL"
GOL_FOOTER = b"\xffGOL"

# GOL command IDs (from lxCmdMaps table in liblgPro.so)
# Format: base ID + variant (0=request, 1=response/write, 2+=extended)
GOL_AUTH_CHALLENGE = 0x30000  # Auth challenge (drone→app)
GOL_AUTH_RESPONSE  = 0x30001  # Auth response (app→drone→app)
GOL_SRL_DATA       = 0x10001  # Serial data (0xAA commands via lgDataForward)
GOL_DEV_INFO       = 0x40000  # Device info/state
GOL_DEV_INFO_ACK   = 0x40001  # Device info ack


def gol_wrap(payload: bytes, msg_type: int, rwbit: int = 0) -> bytes:
    """Wrap payload in GOL framing.

    rwbit: Read/Write bit field (lower 2 bits of FBit from command map).
    This is NOT a sequence number - it indicates the message direction/type.
    Values: 0=read/query, 1=info, 2=data/forward, 3=ack/response
    """
    frame = bytearray()
    frame += GOL_HEADER
    frame += struct.pack("<I", msg_type)
    frame += struct.pack("<I", rwbit)
    frame += struct.pack("<I", len(payload))
    frame += payload
    frame += GOL_FOOTER
    return bytes(frame)


# RWBit values for each GOL command (FBit & 3 from lxCmdMaps)
GOL_RWBIT = {
    0x10000: 3,  # Config (FBit=7)
    0x10001: 2,  # Serial data (FBit=6)
    0x20000: 3,  # Config2 (FBit=7)
    0x20001: 2,  # (FBit=6)
    0x20002: 2,  # (FBit=6)
    0x30000: 0,  # Auth challenge (FBit=0)
    0x30001: 3,  # Auth response/ack (FBit=3)
    0x40000: 1,  # Device info (FBit=5)
    0x40001: 3,  # Device info ack (FBit=7)
    0x40002: 3,  # (FBit=7)
    0x50000: 3,  # (FBit=7)
    0x80000: 3,  # Extended config (FBit=7)
    0x80001: 1,  # (FBit=5)
    0x80002: 1,  # (FBit=5)
    0x80003: 1,  # (FBit=5)
    0x80004: 3,  # (FBit=7)
    0x80005: 3,  # (FBit=7)
    0x90000: 3,  # (FBit=7)
}


def gol_unwrap(data: bytes) -> list:
    """Parse GOL frames from raw TCP data. Returns [(type, rwbit, payload), ...]."""
    frames = []
    pos = 0
    while pos < len(data):
        hdr_idx = data.find(GOL_HEADER, pos)
        if hdr_idx == -1:
            break
        if hdr_idx + 16 > len(data):
            break
        msg_type = struct.unpack_from("<I", data, hdr_idx + 4)[0]
        rwbit = struct.unpack_from("<I", data, hdr_idx + 8)[0]
        payload_len = struct.unpack_from("<I", data, hdr_idx + 12)[0]
        payload_start = hdr_idx + 16
        payload_end = payload_start + payload_len
        if payload_end + 4 > len(data):
            break
        footer = data[payload_end:payload_end + 4]
        if footer == GOL_FOOTER:
            frames.append((msg_type, rwbit, data[payload_start:payload_end]))
        pos = payload_end + 4
    return frames


# --- 0xAA Packet Building ---

def calc_checksum(packet: bytearray) -> int:
    s = 0
    for i, b in enumerate(packet):
        if i != 0 and i != 3:
            s += b
    return s & 0xFF


def build_packet(msg_id: int, payload: bytes = b"") -> bytes:
    length = 4 + len(payload)
    packet = bytearray(length)
    packet[0] = START_BYTE
    packet[1] = msg_id
    packet[2] = length & 0xFF
    packet[3] = 0
    packet[4:] = payload
    packet[3] = calc_checksum(packet)
    return bytes(packet)


def build_ctrl_cmd(aileron=0.0, elevator=0.0, throttle=0.0, rudder=0.0, flags=0) -> bytes:
    def stick_val(f):
        v = round(256 * ((f + 1.0) / 2.0))
        return max(3, min(253, v))
    payload = bytes([
        stick_val(aileron), stick_val(elevator),
        stick_val(throttle), stick_val(rudder),
        flags & 0xFF, 0x00, 0x00,
    ])
    return build_packet(MsgId.CtrlCmd, payload)


def build_set_cmd(cmd: int) -> bytes:
    return build_packet(MsgId.SetCmd, bytes([cmd]))


def build_gps_info(lon: float, lat: float, flag: int = 0) -> bytes:
    payload = bytearray(12)
    struct.pack_into("<i", payload, 0, int(lon * 1e7))
    struct.pack_into("<i", payload, 4, int(lat * 1e7))
    payload[11] = flag & 0xFF
    return build_packet(MsgId.GPSInFo, bytes(payload))


def build_return_home(enter=True) -> bytes:
    mid = MsgId.SetTurnBack if enter else MsgId.ExitTurnBack
    return build_packet(mid, bytes([0x01]))


def build_photo() -> bytes:
    return build_packet(MsgId.AppPhoto, bytes([0x01]))


def build_record(start=True) -> bytes:
    return build_packet(MsgId.AppRecord, bytes([0x01 if start else 0x00]))


# --- Telemetry Parsing ---

def parse_gps_telemetry(data: bytes) -> dict:
    if len(data) < 4 or data[0] != START_BYTE or data[1] != MsgId.GPSInFo:
        return {}
    info = {}
    if len(data) >= 8:  info["lon"] = struct.unpack_from("<i", data, 4)[0] / 1e7
    if len(data) >= 12: info["lat"] = struct.unpack_from("<i", data, 8)[0] / 1e7
    if len(data) >= 14: info["altitude"] = struct.unpack_from("<h", data, 12)[0]
    if len(data) >= 16: info["distance"] = struct.unpack_from("<h", data, 14)[0]
    if len(data) >= 22:
        info["flight_mode"] = data[21]
        info["flight_mode_str"] = FLIGHT_MODES.get(data[21], f"Unknown({data[21]})")
    if len(data) >= 23: info["voltage"] = data[22] / 10.0
    if len(data) >= 24: info["satellites"] = data[23] & 0x1F
    if len(data) >= 27: info["speed"] = data[26] / 10.0
    return info


def parse_control_info(data: bytes) -> dict:
    if len(data) < 4 or data[0] != START_BYTE or data[1] != MsgId.ControlInfo:
        return {}
    info = {}
    if len(data) >= 9:
        info["speed1"] = data[4]
        info["speed2"] = data[5]
        info["speed3"] = data[6]
        info["speed4"] = data[7]
        info["speed5"] = data[8]
    if len(data) >= 10:
        b = data[9]
        info["lock"] = (b >> 7) & 1
        info["unlock"] = (b >> 6) & 1
        info["up"] = (b >> 5) & 1
        info["back"] = (b >> 4) & 1
        info["photo"] = (b >> 3) & 1
        info["rec"] = (b >> 2) & 1
        info["state"] = b & 3
    return info


def validate_packet(data: bytes) -> bool:
    if not data or len(data) < 4:
        return False
    if data[0] != START_BYTE:
        return False
    if data[2] != len(data):
        return False
    expected = calc_checksum(bytearray(data))
    return data[3] == expected


# --- Drone Connection ---

class HS720:
    def __init__(self, ip: str = DRONE_IP, key_table_path: str = KEYTABLE_PATH):
        self.ip = ip
        self.sock = None
        self._recv_thread = None
        self._running = False
        self._telemetry = {}
        self._lock = threading.Lock()
        self._authenticated = False
        self._seq = 0
        self._user_id = 0
        self._key_table = load_key_table(key_table_path)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def connect_and_auth(self) -> bool:
        """Connect to drone GOL port and perform authentication handshake.

        Auth flow (from lxOnCheckRqtCbk + lgPackCmdSend in liblgPro.so):
        1. Drone sends GOL type 0x30000 (rwbit=0) with 24-byte encoded logCheck_t
        2. App decodes with logAppDeCodeKey → extracts UserId
        3. App replaces CkApp with random value (logGetRmad), keeps CkDev
        4. App re-encodes with logEnCodeKey(0, 1, ck, UserId)
        5. App sends response as GOL type 0x30001, rwbit=3
        6. Drone validates (CkDev matches, CkApp changed), transitions to state 5
        7. Drone sends 0x30001 back with device info → auth complete (no step 4!)
        """
        print(f"[*] Connecting to {self.ip}:{GOL_PORT}...")
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((self.ip, GOL_PORT))
            print(f"[+] TCP connected to {GOL_PORT}")
        except Exception as e:
            print(f"[-] Connection failed: {e}")
            return False

        # Step 1: Receive initial GOL message from drone (0x30000 challenge)
        print("[*] Waiting for drone challenge...")
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            print("[-] Timeout waiting for challenge")
            self.sock.close()
            return False

        if not data:
            print("[-] No data received")
            self.sock.close()
            return False

        frames = gol_unwrap(data)
        if not frames:
            print(f"[-] Could not parse GOL frame: {data.hex()}")
            self.sock.close()
            return False

        msg_type, rwbit, payload = frames[0]
        print(f"[*] Received GOL type=0x{msg_type:06x} rwbit={rwbit} len={len(payload)}")
        print(f"    Payload: {payload.hex()}")

        if len(payload) < 24:
            print(f"[-] Payload too short ({len(payload)}) for logCheck_t (24)")
            self.sock.close()
            return False

        # Step 2: Decode the challenge
        ck = LogCheck(payload[:24])
        print(f"[*] Encoded challenge: {ck}")

        result, user_id = log_decode_key(0, 1, ck, self._key_table)
        print(f"[*] Decode result={result}, UserId={user_id} (0x{user_id:x})")
        print(f"    Decoded: CkApp=0x{ck.ck_app:08x} CkDev=0x{ck.ck_dev:08x}")

        if result != 0:
            print(f"[-] Decode failed with code {result}")
            self.sock.close()
            return False

        self._user_id = user_id

        # Step 3: Replace CkApp with random value (logGetRmad), keep CkDev unchanged
        old_ck_app = ck.ck_app
        new_ck_app = struct.unpack('<I', os.urandom(4))[0]
        while new_ck_app == old_ck_app:
            new_ck_app = struct.unpack('<I', os.urandom(4))[0]
        ck.ck_app = new_ck_app
        print(f"[*] CkApp: 0x{old_ck_app:08x} → 0x{new_ck_app:08x}")

        # Step 4: Re-encode with logEnCodeKey(0, 1, ck, UserId)
        enc_result = log_encode_key(0, 1, ck, user_id, self._key_table)
        print(f"[*] Encode result={enc_result}")
        resp_payload = ck.to_bytes()
        print(f"    Response payload: {resp_payload.hex()}")

        # Step 5: Send as GOL type 0x30001 with rwbit=3 (FBit & 3 for auth response)
        resp_frame = gol_wrap(resp_payload, GOL_AUTH_RESPONSE, rwbit=GOL_RWBIT[GOL_AUTH_RESPONSE])
        print(f"[*] Sending GOL type=0x030001 rwbit=3 len={len(resp_payload)}")

        try:
            self.sock.sendall(resp_frame)
        except Exception as e:
            print(f"[-] Send error: {e}")
            self.sock.close()
            return False

        # Step 6: Wait for drone's 0x30001 response (ack + device info)
        try:
            self.sock.settimeout(5)
            resp_data = self.sock.recv(4096)
        except socket.timeout:
            print("[-] Timeout waiting for auth response")
            self.sock.close()
            return False

        if not resp_data:
            print("[-] No response")
            self.sock.close()
            return False

        resp_frames = gol_unwrap(resp_data)
        if not resp_frames:
            print(f"[-] Can't parse response: {resp_data.hex()}")
            self.sock.close()
            return False

        rt, rw, rp = resp_frames[0]
        print(f"[*] Auth ack: type=0x{rt:06x} rwbit={rw} len={len(rp)}")
        print(f"    Payload: {rp[:48].hex()}")
        if rw & 0x80000000:
            print(f"[-] Error in auth ack: rwbit=0x{rw:08x}")
            self.sock.close()
            return False

        if len(rp) >= 24:
            # Decode drone's response to verify auth succeeded
            ck2 = LogCheck(rp[:24])
            result2, user_id2 = log_decode_key(0, 1, ck2, self._key_table)
            print(f"[*] Auth ack decode: result={result2}, UserId={user_id2}")
            print(f"    CkApp=0x{ck2.ck_app:08x} CkDev=0x{ck2.ck_dev:08x}")
            if len(rp) > 24:
                print(f"    Device info ({len(rp)-24} bytes): {rp[24:].hex()}")

        # Auth complete - no step 4 needed!
        # The protocol is 3 messages: challenge → response → ack
        self._authenticated = True
        self.sock.settimeout(5)
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

        print(f"[+] AUTH COMPLETE")

        # Post-auth setup (from ActStPlay.onCntStateCbk when state==5):
        # 1. StPlay(-1): GOL 0x20001 opens the serial/stream data channel
        stplay_json = json.dumps({"StRelIdx": -1, "VStOpen": 1})
        stplay_payload = stplay_json.encode() + b"\x00"  # null-terminated
        self.send_gol(stplay_payload, 0x20001)
        print("[*] Sent StPlay(-1) → GOL 0x20001")
        time.sleep(0.15)

        # 2. SetCfg(RealtimeSet, timestamp): GOL 0x90000
        ts = time.strftime("%Y%m%d-%H%M%S") + f"{int(time.time()*100)%100:02d}"
        cfg_json = json.dumps({"cmd": 0x20000, "data": {"RealtimeSet": ts}})
        cfg_payload = cfg_json.encode() + b"\x00"
        self.send_gol(cfg_payload, 0x90000)
        print(f"[*] Sent SetCfg(RealtimeSet={ts}) → GOL 0x90000")
        time.sleep(0.15)

        # 3. Query device config: GOL 0x40000 (DevInfo)
        self.send_gol(b"\x00", GOL_DEV_INFO)
        print("[*] Sent DevInfo query → GOL 0x40000")
        time.sleep(0.3)

        return True

    def disconnect(self):
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self._authenticated = False
        print("[*] Disconnected")

    def _recv_loop(self):
        buf = b""
        while self._running and self.sock:
            try:
                data = self.sock.recv(4096)
                if not data:
                    print("[-] Connection closed by drone")
                    break
                buf += data
                self._process_buffer(buf)
                buf = b""
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[-] Recv error: {e}")
                break

    def _process_buffer(self, data: bytes):
        frames = gol_unwrap(data)
        if frames:
            for msg_type, rwbit, payload in frames:
                if msg_type in (GOL_SRL_DATA, 0x10000) and payload and payload[0] == START_BYTE:
                    self._parse_aa_packet(payload)
                elif msg_type in (GOL_AUTH_CHALLENGE, GOL_AUTH_RESPONSE):
                    print(f"  [Auth 0x{msg_type:05x}] rwbit={rwbit} len={len(payload)}")
                elif msg_type in (GOL_DEV_INFO, GOL_DEV_INFO_ACK):
                    print(f"  [DevInfo 0x{msg_type:05x}] rwbit={rwbit} len={len(payload)}")
                else:
                    print(f"  [GOL 0x{msg_type:06x}] rwbit={rwbit} "
                          f"len={len(payload)} {payload[:32].hex()}")
            return

        if data and data[0] == START_BYTE:
            self._parse_aa_packet(data)
            return

        print(f"  [RAW] ({len(data)}): {data[:64].hex()}")

    def _parse_aa_packet(self, data: bytes):
        if not data or data[0] != START_BYTE:
            return
        msg_id = data[1] if len(data) > 1 else 0
        valid = validate_packet(data)
        tag = "OK" if valid else "BAD_CHKSUM"

        if msg_id == MsgId.GPSInFo:
            info = parse_gps_telemetry(data)
            if info:
                with self._lock:
                    self._telemetry.update(info)
                print(f"  [GPS {tag}] mode={info.get('flight_mode_str', '?')} "
                      f"alt={info.get('altitude', '?')}m "
                      f"sat={info.get('satellites', '?')} "
                      f"bat={info.get('voltage', '?')}V")
        elif msg_id == MsgId.ControlInfo:
            info = parse_control_info(data)
            print(f"  [CTRL {tag}] {info}")
        elif msg_id == MsgId.PTZInFo:
            print(f"  [PTZ {tag}] {data.hex()}")
        else:
            print(f"  [MSG 0x{msg_id:02x} {tag}] {data.hex()}")

    def send_aa(self, packet: bytes):
        """Send a 0xAA command wrapped in GOL serial data (0x10001, rwbit=2)."""
        if not self.sock:
            print("[-] Not connected")
            return
        frame = gol_wrap(packet, GOL_SRL_DATA, rwbit=GOL_RWBIT[GOL_SRL_DATA])
        try:
            self.sock.sendall(frame)
            print(f"  [SENT] {packet.hex()}")
        except Exception as e:
            print(f"[-] Send error: {e}")

    def send_gol(self, payload: bytes, msg_type: int):
        """Send arbitrary GOL message with auto-detected RWBit."""
        if not self.sock:
            print("[-] Not connected")
            return
        rwbit = GOL_RWBIT.get(msg_type, 0)
        frame = gol_wrap(payload, msg_type, rwbit=rwbit)
        try:
            self.sock.sendall(frame)
            print(f"  [SENT GOL 0x{msg_type:06x} rwbit={rwbit}] {payload.hex()}")
        except Exception as e:
            print(f"[-] Send error: {e}")

    def send_raw(self, data: bytes):
        """Send raw bytes."""
        if not self.sock:
            print("[-] Not connected")
            return
        try:
            self.sock.sendall(data)
        except Exception as e:
            print(f"[-] Send error: {e}")

    # --- High-level commands ---

    def calibrate_gyro(self):
        print("[*] Gyroscope calibration...")
        self.send_aa(build_set_cmd(CMD_GYRO_CAL))

    def calibrate_compass(self):
        print("[*] Compass calibration...")
        self.send_aa(build_set_cmd(CMD_GEO_CAL))

    def unlock_motors(self):
        print("[*] Unlocking motors...")
        self.send_aa(build_set_cmd(CMD_UNLOCK))

    def takeoff_land(self):
        print("[*] Takeoff/land toggle...")
        self.send_aa(build_set_cmd(CMD_TAKEOFF_LAND))

    def emergency_stop(self):
        print("[!] EMERGENCY STOP")
        self.send_aa(build_ctrl_cmd(flags=FLAG_STOP))

    def hover(self):
        self.send_aa(build_ctrl_cmd())

    def move(self, aileron=0.0, elevator=0.0, throttle=0.0, rudder=0.0, flags=0):
        self.send_aa(build_ctrl_cmd(aileron, elevator, throttle, rudder, flags))

    def return_home(self):
        print("[*] Return to home...")
        self.send_aa(build_return_home(enter=True))

    def take_photo(self):
        print("[*] Taking photo...")
        self.send_aa(build_photo())

    def record(self, start=True):
        print(f"[*] {'Start' if start else 'Stop'} recording...")
        self.send_aa(build_record(start))

    def get_telemetry(self) -> dict:
        with self._lock:
            return dict(self._telemetry)


# --- Interactive CLI ---

def print_help():
    print("""
Commands:
  connect       - Connect and authenticate with drone
  disconnect    - Disconnect
  gyro          - Calibrate gyroscope (drone on flat surface!)
  compass       - Calibrate compass
  unlock        - Unlock/arm motors
  takeoff       - Takeoff (or land if flying)
  land          - Same as takeoff (toggle)
  hover         - Send neutral sticks
  stop          - EMERGENCY STOP
  rth           - Return to home
  photo         - Take photo
  rec           - Start recording
  stoprec       - Stop recording
  telem         - Show last telemetry
  aa <hex>      - Send raw 0xAA packet via GOL SrlData
  gol <type> <hex> - Send GOL message (type as hex, e.g. gol e0000 aa1d0506)
  raw <hex>     - Send raw bytes (no GOL wrapping)
  quit          - Disconnect and exit
""")


def main():
    parser = argparse.ArgumentParser(description="Holy Stone HS720 Drone Control")
    parser.add_argument("--host", default="localhost",
                        help="Drone host (default: localhost via SSH tunnel)")
    parser.add_argument("--direct", action="store_true",
                        help="Connect directly to drone IP (172.16.11.1)")
    parser.add_argument("--port", type=int, default=GOL_PORT)
    parser.add_argument("--keytable", default=KEYTABLE_PATH)
    args = parser.parse_args()

    host = DRONE_IP if args.direct else args.host
    drone = HS720(ip=host, key_table_path=args.keytable)

    print("=" * 50)
    print("  Holy Stone HS720 Control")
    print("  Protocol: GOL auth + 0xAA commands")
    print(f"  Target: {host}:{GOL_PORT}")
    if not args.direct:
        print("  Mode: SSH tunnel (run tunnel first!)")
        print("  ssh -f -N -L 18000:172.16.11.1:18000 pi@pi1.local")
    print("=" * 50)
    print_help()

    while True:
        try:
            cmd = input("\nhs720> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not cmd:
            continue

        parts = cmd.split()
        action = parts[0].lower()

        if action in ("quit", "q"):
            break
        elif action in ("help", "h"):
            print_help()
        elif action == "connect":
            drone.connect_and_auth()
        elif action == "disconnect":
            drone.disconnect()
        elif action == "gyro":
            drone.calibrate_gyro()
        elif action == "compass":
            drone.calibrate_compass()
        elif action == "unlock":
            drone.unlock_motors()
        elif action in ("takeoff", "land"):
            drone.takeoff_land()
        elif action == "hover":
            drone.hover()
        elif action == "stop":
            drone.emergency_stop()
        elif action == "rth":
            drone.return_home()
        elif action == "photo":
            drone.take_photo()
        elif action == "rec":
            drone.record(True)
        elif action == "stoprec":
            drone.record(False)
        elif action == "telem":
            t = drone.get_telemetry()
            if t:
                for k, v in t.items():
                    print(f"  {k}: {v}")
            else:
                print("  No telemetry received yet")
        elif action == "aa" and len(parts) > 1:
            try:
                data = bytes.fromhex(parts[1])
                drone.send_aa(data)
            except ValueError:
                print("  Invalid hex")
        elif action == "gol" and len(parts) > 2:
            try:
                gol_type = int(parts[1], 16)
                data = bytes.fromhex(parts[2])
                drone.send_gol(data, gol_type)
            except ValueError:
                print("  Usage: gol <type_hex> <payload_hex>")
        elif action == "raw" and len(parts) > 1:
            try:
                data = bytes.fromhex(parts[1])
                drone.send_raw(data)
                print(f"  [SENT RAW] {data.hex()}")
            except ValueError:
                print("  Invalid hex")
        else:
            print(f"  Unknown command: {action}")

    drone.disconnect()


if __name__ == "__main__":
    main()
