"""
GOL protocol framing and 0xAA packet building.
Pure functions — no classes, no state, no sockets.
"""

import struct

# --- Protocol Constants ---

START_BYTE = 0xAA

GOL_HEADER = b"\x01GOL"
GOL_FOOTER = b"\xffGOL"

# GOL command IDs
GOL_AUTH_CHALLENGE = 0x30000
GOL_AUTH_RESPONSE  = 0x30001
GOL_SRL_DATA       = 0x10001
GOL_DEV_INFO       = 0x40000
GOL_DEV_CTL        = 0x40001
GOL_ST_STOP        = 0x20001
GOL_SP_CFG         = 0x10000
GOL_ST_PLAY        = 0x20000
GOL_SD_CFG         = 0x80000
GOL_EXT_CMD        = 0x90000

# Relay-specific
GOL_RELAY_STATUS   = 0xFF000

GOL_RWBIT = {
    0x10000: 3, 0x10001: 2, 0x20000: 3, 0x20001: 2, 0x20002: 2,
    0x30000: 0, 0x30001: 3, 0x40000: 1, 0x40001: 3, 0x40002: 3,
    0x50000: 3, 0x80000: 3, 0x80001: 1, 0x80002: 1, 0x80003: 1,
    0x80004: 3, 0x80005: 3, 0x90000: 3, 0xFF000: 0,
}


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


# --- GOL Framing ---

def gol_wrap(payload: bytes, cmd_id: int, rwbit: int = None) -> bytes:
    if rwbit is None:
        rwbit = GOL_RWBIT.get(cmd_id, 0)
    frame = bytearray()
    frame += GOL_HEADER
    frame += struct.pack("<I", cmd_id)
    frame += struct.pack("<I", rwbit)
    frame += struct.pack("<I", len(payload))
    frame += payload
    frame += GOL_FOOTER
    return bytes(frame)


def gol_unwrap(data: bytes) -> list:
    """Parse GOL frames from raw TCP data. Returns [(cmd_id, rwbit, payload), ...]."""
    frames = []
    pos = 0
    while pos < len(data):
        hdr_idx = data.find(GOL_HEADER, pos)
        if hdr_idx == -1:
            break
        if hdr_idx + 16 > len(data):
            break
        cmd_id = struct.unpack_from("<I", data, hdr_idx + 4)[0]
        rwbit = struct.unpack_from("<I", data, hdr_idx + 8)[0]
        payload_len = struct.unpack_from("<I", data, hdr_idx + 12)[0]
        payload_start = hdr_idx + 16
        payload_end = payload_start + payload_len
        if payload_end + 4 > len(data):
            break
        footer = data[payload_end:payload_end + 4]
        if footer == GOL_FOOTER:
            frames.append((cmd_id, rwbit, data[payload_start:payload_end]))
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


def validate_packet(data: bytes) -> bool:
    if not data or len(data) < 4:
        return False
    if data[0] != START_BYTE:
        return False
    if data[2] != len(data):
        return False
    expected = calc_checksum(bytearray(data))
    return data[3] == expected


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


def build_status_query() -> bytes:
    return build_packet(0x03)


def build_set_fence() -> bytes:
    return build_packet(MsgId.SetFence, bytes([0x01]))
