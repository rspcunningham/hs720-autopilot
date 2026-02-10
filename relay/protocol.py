"""
GOL protocol framing for the relay. Stdlib only, no external deps.
"""

import struct

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

# Relay-specific command ID (never used by drone)
GOL_RELAY_STATUS   = 0xFF000

GOL_RWBIT = {
    0x10000: 3, 0x10001: 2, 0x20000: 3, 0x20001: 2, 0x20002: 2,
    0x30000: 0, 0x30001: 3, 0x40000: 1, 0x40001: 3, 0x40002: 3,
    0x50000: 3, 0x80000: 3, 0x80001: 1, 0x80002: 1, 0x80003: 1,
    0x80004: 3, 0x80005: 3, 0x90000: 3, 0xFF000: 0,
}


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
    """Parse GOL frames. Returns [(cmd_id, rwbit, payload), ...]."""
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
