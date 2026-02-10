"""Systematic test: auth + try various commands to map what works."""
import json
import socket
import struct
import time
import os

from decode_key import LogCheck, load_key_table, log_decode_key, log_encode_key

GOL_HEADER = b"\x01GOL"
GOL_FOOTER = b"\xffGOL"
KEYTABLE = load_key_table(os.path.join(os.path.dirname(__file__), "keytable.bin"))

GOL_RWBIT = {
    0x10000: 3, 0x10001: 2, 0x20000: 3, 0x20001: 2, 0x20002: 2,
    0x30000: 0, 0x30001: 3, 0x40000: 1, 0x40001: 3, 0x40002: 3,
    0x50000: 3, 0x80000: 3, 0x80001: 1, 0x80002: 1, 0x80003: 1,
    0x80004: 3, 0x80005: 3, 0x90000: 3,
}


def gol_wrap(payload, msg_type, rwbit=0):
    frame = bytearray()
    frame += GOL_HEADER
    frame += struct.pack("<I", msg_type)
    frame += struct.pack("<I", rwbit)
    frame += struct.pack("<I", len(payload))
    frame += payload
    frame += GOL_FOOTER
    return bytes(frame)


def gol_unwrap(data):
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


def do_auth(sock):
    data = sock.recv(4096)
    frames = gol_unwrap(data)
    if not frames:
        print(f"[-] No challenge: {data[:40].hex()}")
        return False
    _, _, payload = frames[0]
    ck = LogCheck(payload[:24])
    result, uid = log_decode_key(0, 1, ck, KEYTABLE)
    if result != 0:
        print(f"[-] Decode failed")
        return False
    old = ck.ck_app
    ck.ck_app = struct.unpack('<I', os.urandom(4))[0]
    while ck.ck_app == old:
        ck.ck_app = struct.unpack('<I', os.urandom(4))[0]
    log_encode_key(0, 1, ck, uid, KEYTABLE)
    sock.sendall(gol_wrap(ck.to_bytes(), 0x30001, rwbit=3))
    data = sock.recv(4096)
    frames = gol_unwrap(data)
    if frames and frames[0][1] & 0x80000000:
        print(f"[-] Auth error")
        return False
    print(f"[+] AUTH OK")
    return True


def send_and_check(sock, payload, cmd_id, label=""):
    rwbit = GOL_RWBIT.get(cmd_id, 0)
    frame = gol_wrap(payload, cmd_id, rwbit)
    sock.sendall(frame)
    time.sleep(0.15)
    sock.settimeout(0.5)
    try:
        data = sock.recv(4096)
    except socket.timeout:
        data = b""

    if data:
        frames = gol_unwrap(data)
        for mt, rw, pl in frames:
            err = "ERROR" if rw & 0x80000000 else "OK"
            errcode = f"0x{rw & 0xFFFF:04x}" if rw & 0x80000000 else ""
            txt = ""
            try:
                t = pl.rstrip(b'\x00').decode('utf-8', errors='replace')
                if '{' in t:
                    txt = f" JSON:{t[:100]}"
            except:
                pass
            print(f"  {label}0x{cmd_id:06x}→0x{mt:06x} rw=0x{rw:08x} [{err}{errcode}] "
                  f"len={len(pl)}{txt}")
            if pl and err == "OK":
                print(f"    DATA: {pl[:80].hex()}")
    else:
        print(f"  {label}0x{cmd_id:06x}→ (no response)")


def main():
    host = "localhost"
    port = 18000

    print(f"=== Systematic Command Test ===\n")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((host, port))

    if not do_auth(sock):
        return

    print(f"\n=== Testing ALL command IDs ===\n")

    # Test FBit=5 commands (DevInfo worked, test others)
    print("--- FBit=5 (rwbit=1) commands ---")
    send_and_check(sock, b"\x00", 0x40000, "DevInfo: ")
    send_and_check(sock, b"\x00", 0x80001, "DirInfo: ")
    send_and_check(sock, b"\x00", 0x80002, "FileDL:  ")
    send_and_check(sock, b"\x00", 0x80003, "VOD:     ")

    # Test FBit=7 (rwbit=3) commands
    print("\n--- FBit=7 (rwbit=3) commands ---")
    send_and_check(sock, b"\x00", 0x10000, "Config:   ")
    send_and_check(sock, b"\x00", 0x20000, "StreamInf:")
    send_and_check(sock, b"\x00", 0x40001, "DevSet:   ")
    send_and_check(sock, b"\x00", 0x50000, "WiFiInfo: ")
    send_and_check(sock, b"\x00", 0x80000, "SDCard:   ")

    # Test FBit=6 (rwbit=2) commands
    print("\n--- FBit=6 (rwbit=2) commands ---")
    stplay = json.dumps({"StRelIdx": -1, "VStOpen": 1}, separators=(',', ':')).encode() + b"\x00"
    send_and_check(sock, stplay, 0x20001, "StPlay:   ")
    send_and_check(sock, b"\x00", 0x20002, "IFrame:   ")

    # Serial data
    hover = bytearray([0xAA, 0x1C, 0x0B, 0x00, 128, 128, 128, 128, 0x00, 0x00, 0x00])
    s = sum(hover[i] for i in range(len(hover)) if i != 0 and i != 3) & 0xFF
    hover[3] = s
    send_and_check(sock, bytes(hover), 0x10001, "SrlData:  ")

    # Extended JSON config
    print("\n--- FBit=7 extended ---")
    ts = time.strftime("%Y%m%d-%H%M%S") + "00"
    cfg = json.dumps({"cmd": 0x20000, "data": {"RealtimeSet": ts}}, separators=(',', ':')).encode() + b"\x00"
    send_and_check(sock, cfg, 0x90000, "SetCfg:   ")

    # Try serial data with rwbit=0 (query mode)
    print("\n--- Serial data with different rwbits ---")
    for rw in [0, 1, 2, 3]:
        frame = gol_wrap(bytes(hover), 0x10001, rw)
        sock.sendall(frame)
        time.sleep(0.1)
        sock.settimeout(0.3)
        try:
            data = sock.recv(4096)
            frames = gol_unwrap(data)
            for mt, r, pl in frames:
                err = "ERR" if r & 0x80000000 else "OK"
                print(f"  rwbit={rw}: resp rw=0x{r:08x} [{err}]")
        except socket.timeout:
            print(f"  rwbit={rw}: (no response)")

    # Try sending just raw 0xAA data without GOL wrapping
    print("\n--- Raw 0xAA (no GOL wrap) ---")
    try:
        sock.sendall(bytes(hover))
        time.sleep(0.2)
        sock.settimeout(0.5)
        data = sock.recv(4096)
        if data:
            print(f"  Response: {data[:80].hex()}")
            frames = gol_unwrap(data)
            for mt, rw, pl in frames:
                print(f"  GOL 0x{mt:06x} rw=0x{rw:08x} len={len(pl)}")
        else:
            print("  (no response)")
    except socket.timeout:
        print("  (no response)")
    except Exception as e:
        print(f"  Error: {e}")

    sock.close()
    print("\n[*] Done")


if __name__ == "__main__":
    main()
