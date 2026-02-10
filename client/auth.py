"""
Auth crypto for GOL protocol.
Copied from decode_key.py — logDeCodeKey / logEnCodeKey.
"""

import os
import struct
from pathlib import Path


class LogCheck:
    """logCheck_t: 24-byte challenge structure."""

    def __init__(self, data: bytes = None):
        if data and len(data) >= 24:
            self.key0, self.key1, self.ck_app, self.ck_dev = struct.unpack_from('<QQII', data)
        else:
            self.key0 = 0
            self.key1 = 0
            self.ck_app = 0
            self.ck_dev = 0

    def get_rdm(self, index: int) -> int:
        if index == 0: return self.key0 & 0xFFFFFFFF
        if index == 1: return (self.key0 >> 32) & 0xFFFFFFFF
        if index == 2: return self.key1 & 0xFFFFFFFF
        if index == 3: return (self.key1 >> 32) & 0xFFFFFFFF
        raise IndexError(index)

    def set_rdm(self, index: int, val: int):
        val &= 0xFFFFFFFF
        if index == 0:
            self.key0 = (self.key0 & 0xFFFFFFFF00000000) | val
        elif index == 1:
            self.key0 = (self.key0 & 0x00000000FFFFFFFF) | (val << 32)
        elif index == 2:
            self.key1 = (self.key1 & 0xFFFFFFFF00000000) | val
        elif index == 3:
            self.key1 = (self.key1 & 0x00000000FFFFFFFF) | (val << 32)

    def to_bytes(self) -> bytes:
        return struct.pack('<QQII',
                           self.key0 & 0xFFFFFFFFFFFFFFFF,
                           self.key1 & 0xFFFFFFFFFFFFFFFF,
                           self.ck_app & 0xFFFFFFFF,
                           self.ck_dev & 0xFFFFFFFF)

    def __repr__(self):
        return (f"LogCheck(Key0=0x{self.key0:016x}, Key1=0x{self.key1:016x}, "
                f"CkApp=0x{self.ck_app:08x}, CkDev=0x{self.ck_dev:08x})")


def load_key_table(filepath: str = None) -> list:
    if filepath is None:
        filepath = str(Path(__file__).parent / "keytable.bin")
    with open(filepath, 'rb') as f:
        data = f.read(2048)
    if len(data) != 2048:
        raise ValueError(f"Key table must be 2048 bytes, got {len(data)}")
    return list(struct.unpack('<512I', data))


def _get_rmad(prev: int) -> int:
    val = struct.unpack('<I', os.urandom(4))[0]
    while val == prev:
        val = struct.unpack('<I', os.urandom(4))[0]
    return val


def log_decode_key(ptl_ver: int, is_app: int, ck: LogCheck, key_table: list) -> tuple:
    key0 = ck.key0
    key1 = ck.key1
    ver = (key1 >> 43) & 0xFF
    b30 = (key0 >> 30) & 0xF
    b14 = (key0 >> 14) & 0xFFF
    new_ver = (ver ^ b30 ^ b14) & 0xFF
    key1 = (key1 & 0xFFF807FFFFFFFFFF) | (new_ver << 43)
    ck.key1 = key1

    if is_app == 0:
        ck.ck_dev ^= ck.get_rdm(3)
        ck.ck_app ^= ck.get_rdm(1)
        xv = ck.ck_app
    else:
        ck.ck_app ^= ck.get_rdm(3)
        ck.ck_dev ^= ck.get_rdm(2)
        xv = ck.ck_dev

    key0 = ck.key0
    ck.key0 = (key0 & 0xFFFFFFFFFFFFF000) | ((key0 ^ xv) & 0xFFF)

    key0 = ck.key0
    idx = ((key0 & 0xFFF) ^ ((key0 >> 14) & 0xFFF)) % 512

    if ((ck.key1 >> 43) & 0xFF) != 0:
        return (-2, 0)

    ktv = key_table[idx]

    sh1 = (ck.key0 >> 12) & 3
    old1 = (ck.key0 >> 47) & 0xFF
    new1 = ((ktv >> sh1) ^ old1) & 0xFF
    ck.key0 = (ck.key0 & 0xFF807FFFFFFFFFFF) | (new1 << 47)

    sh2 = (ck.key0 >> 26) & 3
    old2 = (ck.key1 >> 17) & 0xFF
    new2 = ((ktv >> sh2) ^ old2) & 0xFF
    ck.key1 = (ck.key1 & 0xFFFFFFFFFE01FFFF) | (new2 << 17)

    sh3 = (ck.key0 >> 28) & 3
    old3 = (ck.key1 >> 25) & 0xFF
    new3 = ((ktv >> sh3) ^ old3) & 0xFF
    ck.key1 = (ck.key1 & 0xFFFFFFFE01FFFFFF) | (new3 << 25)

    user_id = 0
    f1 = ((ck.key0 >> 34) & 0x3FF) ^ ktv ^ ((ck.key0 >> 47) & 0xFF)
    user_id |= (f1 & 0x3FF) << 20
    f2 = ((ck.key1 >> 5) & 0x3FF) ^ ktv ^ ((ck.key1 >> 17) & 0xFF)
    user_id |= (f2 & 0x3FF) << 10
    f3 = ((ck.key1 >> 33) & 0x3FF) ^ ktv ^ ((ck.key1 >> 25) & 0xFF)
    user_id |= f3 & 0x3FF

    return (0, user_id & 0x3FFFFFFF)


def log_encode_key(ptl_ver: int, is_app: int, ck: LogCheck, user_id: int,
                   key_table: list) -> int:
    ck.set_rdm(0, _get_rmad(0))
    ck.set_rdm(1, _get_rmad(ck.get_rdm(0)))
    ck.set_rdm(2, _get_rmad(ck.get_rdm(1)))
    ck.set_rdm(3, _get_rmad(ck.get_rdm(2)))

    key0 = ck.key0
    ck.key1 = (ck.key1 & 0xFFF807FFFFFFFFFF) | ((ptl_ver & 0xFF) << 43)
    ck.key0 = key0

    key0 = ck.key0
    idx = ((key0 & 0xFFF) ^ ((key0 >> 14) & 0xFFF)) % 512

    if ptl_ver != 0:
        return -2

    ktv = key_table[idx]

    uid = user_id & 0x3FFFFFFF
    f1 = ((uid >> 20) & 0x3FF) ^ ((ck.key0 >> 47) & 0xFF) ^ ktv
    ck.key0 = (ck.key0 & 0xFFFFF003FFFFFFFF) | ((f1 & 0x3FF) << 34)
    f2 = ((uid >> 10) & 0x3FF) ^ ((ck.key1 >> 17) & 0xFF) ^ ktv
    ck.key1 = (ck.key1 & 0xFFFFFFFFFFFF801F) | ((f2 & 0x3FF) << 5)
    f3 = (uid & 0x3FF) ^ ((ck.key1 >> 25) & 0xFF) ^ ktv
    ck.key1 = (ck.key1 & 0xFFFFF801FFFFFFFF) | ((f3 & 0x3FF) << 33)

    sh1 = (ck.key0 >> 12) & 3
    old1 = (ck.key0 >> 47) & 0xFF
    new1 = ((ktv >> sh1) ^ old1) & 0xFF
    ck.key0 = (ck.key0 & 0xFF807FFFFFFFFFFF) | (new1 << 47)

    sh2 = (ck.key0 >> 26) & 3
    old2 = (ck.key1 >> 17) & 0xFF
    new2 = ((ktv >> sh2) ^ old2) & 0xFF
    ck.key1 = (ck.key1 & 0xFFFFFFFFFE01FFFF) | (new2 << 17)

    sh3 = (ck.key0 >> 28) & 3
    old3 = (ck.key1 >> 25) & 0xFF
    new3 = ((ktv >> sh3) ^ old3) & 0xFF
    ck.key1 = (ck.key1 & 0xFFFFFFFE01FFFFFF) | (new3 << 25)

    if is_app == 0:
        xv = ck.ck_dev
        ck.key0 = (ck.key0 & 0xFFFFFFFFFFFFF000) | ((ck.key0 ^ xv) & 0xFFF)
        ck.ck_dev ^= ck.get_rdm(2)
        ck.ck_app ^= ck.get_rdm(3)
    else:
        xv = ck.ck_app
        ck.key0 = (ck.key0 & 0xFFFFFFFFFFFFF000) | ((ck.key0 ^ xv) & 0xFFF)
        ck.ck_app ^= ck.get_rdm(1)
        ck.ck_dev ^= ck.get_rdm(3)

    key0 = ck.key0
    chk = ((key0 >> 14) & 0xFFF) ^ ((key0 >> 30) & 0xF)
    ck.key1 = (ck.key1 & 0xFFF807FFFFFFFFFF) | ((chk & 0xFF) << 43)
    ck.key0 = key0

    return 0
