# Reverse Engineering the Holy Stone HS720

How we went from zero documentation to full programmatic control.

## Drone Overview

The HS720 (internally "HS710") is a mid-tier GPS drone — not a toy quad.

- **Weight:** 245g (sub-250g, no FAA registration required)
- **Positioning:** GPS + GLONASS + Optical Flow
- **Motors:** Brushless
- **Camera:** 4K UHD, 120 FOV, 90 adjustable pitch
- **FPV:** 5 GHz Wi-Fi (5.745 GHz), range ~300m
- **Controller RF:** 2.4 GHz proprietary, range ~1km
- **Flight time:** ~23 min per battery
- **Manufacturer:** Xiamen Huoshiquan Import & Export CO., LTD (Holy Stone)

## Communication Architecture

The drone has two independent communication channels:

| Channel | Frequency | Purpose | Range |
|---------|-----------|---------|-------|
| RF Controller | 2.4 GHz proprietary | Flight commands (sticks, RTH, modes) | ~1 km |
| Wi-Fi FPV | 5 GHz 802.11 | Video stream + app-based flight control | ~300 m |

**Key insight:** The physical RC controller does NOT use WiFi. It uses proprietary 2.4 GHz RF. The drone's WiFi AP exists for the **Ophelia GO** smartphone app (FPV camera, GPS features, flight control). The app can fly the drone independently of the RC controller.

We chose the Wi-Fi path — faster to implement, no extra hardware, and the app protocol supports full flight control.

## Phase 1: Reconnaissance

### Network Discovery

Power on drone, connect to its WiFi AP:

- **SSID:** `HolyStoneFPV-4bc278D` (open, no password)
- **Drone IP:** `172.16.11.1`
- **Client IP:** assigned via DHCP

Port scan revealed:
- TCP 8856 — control/telemetry (GOL protocol)
- TCP 18000 — video stream (GOL-framed H.264)
- TCP 23 — telnet (password-protected)
- TCP 21 — FTP (configured but ftpd binary missing)

### App Identification

The companion app is **Ophelia GO** (`com.opheliago`). Compatible drones include HS720, HS720E, HS710, HS700D, HS700E, HS550, HS510.

## Phase 2: APK Decompilation

Decompiled `com.opheliago.apk` with JADX. Key findings:

### Native Libraries
- `liblgPro.so` — core protocol, connection management, command encoding
- `liblgAVPlayer.so` — video decoding (H.264/JPEG, YUV420)
- `liblxffmpeg.so` — FFmpeg for media processing

### Protocol Discovery (from Java layer)

The Java source revealed the complete command table, packet format, and telemetry structure. The protocol uses `0xAA`-prefixed binary packets over a GOL transport layer.

## Phase 3: Protocol Decoding

### GOL Transport Layer (Port 8856 / 18000)

All data is wrapped in GOL frames:
```
Bytes 0-3:   \x01GOL or \x00GOL  (header)
Bytes 4-7:   type/version
Bytes 8-11:  payload length (uint32 LE)
Bytes 12-15: padding
Bytes 16-N:  payload
Last 4:      \xFFGOL (footer)
```

GOL command types:
- `0x30000` — auth request
- `0x30001` — auth response
- `0x10001` — serial data (contains 0xAA command packets)
- `0xFF000` — status frame (JSON payload with `state` field)
- `0x00GOL` — video data (H.264)

### 0xAA Packet Format

Inside GOL serial data frames:
```
Byte 0:  0xAA        (start marker)
Byte 1:  MsgId       (command type)
Byte 2:  Length       (total packet length)
Byte 3:  Checksum    (sum of all bytes except byte 0 and byte 3)
Byte 4+: Payload
```

### Authentication

3-message handshake using `keytable.bin` (2048 bytes, shipped with the app):
1. Client sends auth request (GOL cmd `0x30000`)
2. Drone sends challenge with random index into keytable
3. Client responds with keytable lookup (GOL cmd `0x30001`)

### Initialization Sequence

After auth, a 10-step init sequence enables telemetry:
1. Auth handshake (3 messages)
2. DevCtlPower=1 — this triggers the drone to start sending telemetry
3. Various configuration commands

### Command Table

| Name | MsgId | Direction | Description |
|------|-------|-----------|-------------|
| GPSInFo | 0x01 | Both | GPS telemetry |
| CtrlCmd | 0x1C | App->Drone | Joystick control (primary flight) |
| SetCmd | 0x1D | App->Drone | Discrete commands (takeoff, land, cal) |
| SetFollow | 0x04 | App->Drone | Follow-me mode |
| SetCircle | 0x06 | App->Drone | Circle/orbit mode |
| SetCruise | 0x08 | App->Drone | Waypoint cruise |
| SetTurnBack | 0x16 | App->Drone | Return to home |
| AppPhoto | 0x18 | App->Drone | Take photo |
| AppRecord | 0x19 | App->Drone | Start/stop recording |
| SetPtzCmd | 0x1E | App->Drone | Gimbal control |

### Joystick Control (CtrlCmd 0x1C) — 11 bytes

```
Byte 4: Aileron/Roll   (0-255, center ~128)
Byte 5: Elevator/Pitch (0-255, center ~128)
Byte 6: Throttle       (0-255, center ~128)
Byte 7: Rudder/Yaw     (0-255, center ~128)
Byte 8: Flags          (bitfield)
```

Flag bits: `0x01` lock, `0x02` high speed, `0x04` emergency, `0x08` GPS mode, `0x10` light, `0x20`/`0x40` PTZ tilt.

### GPS Telemetry (GPSInFo 0x01)

```
Bytes 4-7:   Longitude (int32 LE / 10,000,000 = degrees)
Bytes 8-11:  Latitude (int32 LE / 10,000,000 = degrees)
Bytes 12-13: Altitude (int16 LE, meters)
Bytes 14-15: Distance from home (int16 LE, meters)
Byte 21:     Flight mode (0=locked..17=point circle)
Byte 22:     Voltage (/ 10 = volts)
Byte 23:     Satellites (bits 0-4: count)
Byte 26:     Speed (/ 10 = m/s)
Bytes 28-29: Yaw (int16 LE, degrees)
```

Flight modes: 0=Locked(no GPS), 1=AltHold, 2=PosHold, 3=RTH, 4=Follow, 5=Circle, 6=Waypoint, 7=Shutdown, 8=GyroCal, 11=Landing, 12=Idle, 13=Locked(GPS).

### Video Stream

- UDP port 16000, `\x00GOL`-framed H.264
- ~330 kbps, most packets 1404 bytes (1384 payload)
- GOL header: 16 bytes, payload length at offset 8 (uint32 LE), H.264 data at offset 16
- FC UART baudrate: 38400

## Prior Art & References

### Direct HS720 Work
- [benjamind2/HS720](https://github.com/benjamind2/HS720) — hardware teardown, no protocol RE
- [Blog](https://puppicornprojects.blogspot.com/) — board photos, UART logs, firmware dumps
- Found: Linux SoC, U-Boot, MX25L645D SPI flash, multi-board UART topology

### Holy Stone Protocol Work (other models)
- [HS200 Gobot Driver](https://github.com/hybridgroup/gobot/tree/master/platforms/holystone/hs200) — TCP 8888 control, different protocol family
- [HS110W Node.js](https://github.com/lancecaraccioli/holystone-hs110w) — web interface

### Similar Drone RE
- [E58 ESP8266 Control](https://github.com/martin-ger/ESP_E58-Drone) — Lewei LW9809 camera module, 8-byte UDP packets
- [E58 Video RE](https://github.com/guillesanbri/e58-drone-reversing) — MJPEG video protocol
- [DJI Tello Python SDK](https://github.com/damiafuentes/DJITelloPy) — reference for clean drone API design
- [Parrot AR.Drone Hacking](https://github.com/markszabo/drone-hacking) — AT commands, no auth

### Methodology Resources
- [Wireshark Drone RE (HackerNoon)](https://hackernoon.com/how-to-reverse-engineer-a-drone-with-wireshark-using-packet-dissection)
- [ProMark VR UDP RE (Hackaday)](https://hackaday.io/project/19356-reverse-engineering-a-promark-vr-toy-drone)
- [Hacking WiFi Drones (thesis)](https://www.diva-portal.org/smash/get/diva2:1586253/FULLTEXT01.pdf)

## RF Controller Path (Future Work)

The RF controller at 2.4 GHz has better latency and range but requires hardware RE:
1. Open controller, identify transceiver chip (likely XN297, BK2423, or Telink TLSR)
2. Tap SPI bus with logic analyzer to capture protocol
3. Replicate on Pi with matching transceiver module

This path is documented but not yet pursued — Wi-Fi control was sufficient for the initial goal.
