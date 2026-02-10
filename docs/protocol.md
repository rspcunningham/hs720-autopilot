# HS720 (HS710) WiFi Protocol Reference

## Hardware Overview

- **Drone model:** Holy Stone HS720 (internal ID: "HS710")
- **WiFi module:** Bilian Electronic, Fullhan FH8852 SoC (ARMv7), Linux 3.0.8
- **WiFi AP:** SSID `HolyStoneFPV-4bc278D` (open, no password), IP `172.16.11.1`
- **Flight controller UART:** 38400 baud, 8N1
- **Camera sensor:** GC4653, max 3840x2160, currently 2560x1440 H264
- **RF controller:** 2.4 GHz proprietary protocol (separate from WiFi, ~1km range)
- **Compatible apps:** Ophelia GO (`com.linghang.opheliagopro`), HolyStone-FPV — both use the same protocol

## Communication Architecture

The drone has **two independent control paths**:

| Path | Frequency | Purpose | Range |
|------|-----------|---------|-------|
| **RF Controller** | 2.4 GHz | Flight commands, **motor arm/disarm** | ~1 km |
| **WiFi FPV** | 5 GHz (802.11) | Video, telemetry, takeoff/land, GPS, settings | ~300 m |

**Motor arming (LOCKED → IDLE) can ONLY be done via the RF controller.** Once armed, WiFi can command takeoff, landing, and other functions. This has been confirmed by:
- Capturing both apps (Ophelia GO, HolyStone-FPV) — neither sends an arm command
- Brute-forcing all SetCmd values 0x00-0x80 via WiFi — none arm the motors
- Successfully taking off via WiFi after RF-arming (captured and confirmed)

## Network Architecture

| Channel | Protocol | Direction | Purpose | Status |
|---------|----------|-----------|---------|--------|
| **TCP 18000** | GOL framing | Bidirectional | Auth, commands, config, telemetry relay | **WORKING** |
| **UDP 17000** | GOL-wrapped 0xAA | Bidirectional | Serial data uplink/downlink to FC | **WORKING** |
| **UDP 16000** | `\x00GOL` framing | Drone → App | H.264 video stream (1280x720, ~2.5Mbps) | Receiving not yet implemented |
| TCP 8856 | Unknown | Unknown | Unknown (accepts connections, always silent) | Dead end |
| TCP 23 | Telnet | Interactive | Linux login (password unknown) | Dead end |

**Port mirroring:** UDP source and destination ports match (17000→17000, 16000→16000).

## Our Setup

```
Mac (scripts) → SSH → Pi (pi1.local) → Drone WiFi (wlan0) → Drone (172.16.11.1)
                         └─ Home network (eth0)
```

- SSH tunnel for TCP: `ssh -f -N -L 18000:172.16.11.1:18000 pi@pi1.local`
- **UDP cannot tunnel over SSH** — scripts using UDP 17000/16000 must run on Pi
- Reconnect Pi to drone WiFi: `ssh pi@pi1.local "sudo nmcli dev wifi connect 'HolyStoneFPV-4bc278D' ifname wlan0"`

## GOL Frame Format

All TCP 18000 and UDP 17000 communication uses GOL framing:

```
Header:  \x01GOL  (4 bytes)
CmdId:   uint32_LE
RWBit:   uint32_LE  (bit 31 = error flag)
PayLen:  uint32_LE
Payload: [PayLen bytes]
Footer:  \xffGOL  (4 bytes)
```

Total overhead: 20 bytes per frame. Video on UDP 16000 uses `\x00GOL` header.

## 0xAA Serial Packet Format

Flight controller commands/telemetry use this format inside GOL payloads:

```
Byte 0:  0xAA  (start marker)
Byte 1:  MsgId
Byte 2:  Total packet length (including header)
Byte 3:  Checksum = sum(all bytes except [0] and [3]) & 0xFF
Byte 4+: Payload data
```

## Complete Initialization Sequence

Decoded from Wireshark captures of both Ophelia GO and HolyStone-FPV apps. Both use the same sequence:

### Phase 1: Auth (TCP 18000)

| Step | Direction | CmdId | RWBit | Payload |
|------|-----------|-------|-------|---------|
| 1 | Drone→App | 0x30000 | 0 | 24-byte encoded logCheck_t challenge |
| 2 | App→Drone | 0x30001 | 3 | 24-byte re-encoded response (randomize CkApp, keep CkDev) |
| 3 | Drone→App | 0x30001 | 0 | 84 bytes (24-byte logCheck_t + 60-byte device info) |

Crypto uses `logEnCodeKey`/`logDeCodeKey` with a 2048-byte key table from `liblgPro.so`.

### Phase 2: Enable Telemetry (TCP 18000)

| Step | Direction | CmdId | RWBit | Payload | Notes |
|------|-----------|-------|-------|---------|-------|
| 4 | App→Drone | 0x40000 | 1 | (empty) | DevInfo query |
| 5 | App→Drone | 0x20001 | 2 | `{"StRelIdx": 1, "VStOpen": 1}\0` | StStop — prepare video |
| 6 | App→Drone | 0x40001 | 3 | `{"DevCtlPower": 1}\0` | **TELEMETRY TRIGGER** |

Telemetry starts flowing ~170ms after DevCtlPower=1. The drone pushes 0xAA frames via SerialData(0x10001, rw=0) on TCP 18000 at ~2Hz.

### Phase 3: Config Queries (TCP 18000)

| Step | Direction | CmdId | RWBit | Payload |
|------|-----------|-------|-------|---------|
| 7 | App→Drone | 0x20000 | 3 | (empty) — StPlay query |
| 8 | App→Drone | 0x80000 | 3 | (empty) — SdCfg query |
| 9 | App→Drone | 0x90000 | 3 | `{"cmd": 131072, "data": {"RealtimeSet": "YYYYMMDD-HHMMSSss"}}\0` |
| 10 | App→Drone | 0x90000 | 3 | `{"cmd": 65536}\0` — capabilities query |
| 11 | App→Drone | 0x90000 | 3 | `{"cmd": 131072, "data": {"sdRltSet": "2560x1440"}}\0` — set resolution |

### Phase 4: Serial Uplink (UDP 17000)

After TCP init completes, the app begins sending on UDP 17000:

| Order | 0xAA MsgId | Name | Raw Bytes | Purpose |
|-------|-----------|------|-----------|---------|
| 1 | 0x02 | SetFence | `aa 02 08 55 0f 1e 1e 00` | Set geofence (altitude=30, distance=30) |
| 2 | 0x03 | Status | `aa 03 06 0e 05 00` | FC status query ("hello") |
| 3 | 0x01 | GPSInFo | `aa 01 10 xx ...` | Phone GPS position (at ~2.8Hz) |

The app alternates SetFence and Status queries at ~2.3Hz each (5 of one, then 5 of the other), with GPS uplink interleaved at ~2.8Hz during steady state.

### Phase 5: Steady State

- **TCP 18000:** DevInfo(0x40000) queries at ~2Hz as keepalive; drone responds with DevInfo + piggybacked telemetry
- **UDP 17000:** GPS uplink at ~2.8Hz, alternating SetFence/Status queries
- **UDP 16000:** H.264 video stream drone→app (~220 packets/sec, 1400 bytes each)

## Confirmed Working Operations

### Takeoff (SetCmd 0x08) — CONFIRMED WORKING

**Prerequisite:** Drone must be in IDLE mode (12) — requires RF controller arm.

```
0xAA packet: aa 1d 05 2a 08
```

The app sends SetCmd(0x1D, 0x08) at ~2.8Hz for about 1.5 seconds. The drone transitions IDLE → ALT_HOLD and takes off to its default hover altitude.

**Observed flight from capture (hsfpv_capture.pcap):**
```
t=12.9s  RF arm:     LOCKED(noGPS) → IDLE        (no WiFi command)
t=19.9s  WiFi:       SetCmd(0x08) sent via UDP 17000
t=20.2s  Takeoff:    IDLE → ALT_HOLD              (drone flying!)
t=21.7s  Landing:    ALT_HOLD → LANDING
t=30.6s  Landed:     LANDING → LOCKED(noGPS)
```

### Landing (SetCmd 0x08) — Same Command as Takeoff

SetCmd(0x08) is a toggle — send while in IDLE/LOCKED to take off, send while flying to land.

After landing commands, the app sends SetCmd(0x00) to clear the command state.

### Telemetry Reception — CONFIRMED WORKING

After DevCtlPower=1, the drone pushes telemetry at ~2Hz:

| 0xAA MsgId | Name | Rate | Content |
|------------|------|------|---------|
| 0x01 | GPSInFo | ~2 Hz | Flight mode, altitude, voltage, GPS, speed, yaw, satellites |
| 0x95 | DeviceID | ~0.3 Hz | "HS710" + version data |
| 0x03 | Status | On request | FC status response (always `aa 03 05 09 01`) |
| 0x02 | SetFence | On request | Fence ack (always `aa 02 05 08 01`) |

### GPSInFo Telemetry Fields (0xAA MsgId 0x01, 31 bytes)

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 4-7 | int32 LE | Longitude | Divide by 10,000,000 for degrees |
| 8-11 | int32 LE | Latitude | Divide by 10,000,000 for degrees |
| 12-13 | int16 LE | Altitude | Meters |
| 14-15 | int16 LE | Distance | Meters from home |
| 21 | uint8 | Flight mode | See flight mode table |
| 22 | uint8 | Voltage | Divide by 10 for volts |
| 23 | uint8 | Satellites | Mask with 0x1F |
| 26 | uint8 | Speed | Divide by 10 |
| 28-29 | int16 LE | Yaw | Heading in degrees |

### IFrame Request (0x20002) — Used During Flight

The HolyStone-FPV app sends `Cmd_0x20002` (IFrame request) on TCP 18000 during flight at variable rate. This requests a video keyframe for better video recovery.

### SetFence (0x02) — Geofence Parameters

```
0xAA packet: aa 02 08 55 0f 1e 1e 00
```
Bytes: msgid=0x02, len=8, checksum=0x55, data=`0f 1e 1e 00` (altitude limit=30m, distance limit=30m).

### Config Responses

**SpCfg (0x10000):** `{"SpBaudrate": 38400, "SpDataBit": 8, "SpStop": 1, "SpParity": 0}`

**StPlay (0x20000):** `{"StContrasts": 55, "StSaturation": 80, "StBrightness": 155, "StSharpness": 27, "StMirrorImg": 0, "StMajorBps": 20480, "StMinorBps": 3072, "StMajorGop": 25, "StMinorGop": 50, "StMajorFps": 20, "StMinorFps": 20, "StCurRltIdx": 1}`

**SdCfg (0x80000):** `{"SdRecTime": 300, "SdCover": 0, "SdImgFmt": 0, "SdRecFmt": 1}`

**ExtCmd capabilities (0x90000, cmd=65536):**
```json
{
  "cmd": 65536,
  "data": {
    "sdRltSup": ["3840x2160", "2560x1440", "1920x1080", "1280x720"],
    "encSup": ["H265", "H264"],
    "ContrlerSup": ["yes", "no"],
    "ActuatorSup": ["yes", "no"],
    "fpvSup": ["BD", "HD", "Fluent"],
    "SpecialeffectsSup": ["BlackWhite", "Overcast", "Sunny", "Highcontrast", "Nostalgia", "Nomal"],
    "curContrler": "no",
    "curActuator": "no",
    "curSdRlt": "2560x1440",
    "curEnc": "H264",
    "curfpv": "HD",
    "curSpecialeffects": "Nomal"
  },
  "result": 0
}
```

`curContrler: "no"` is a firmware-level setting (same response from both apps). This is why WiFi joystick control doesn't work for arming.

## Flight Mode States

| Value | Name | Meaning |
|-------|------|---------|
| 0 | LOCKED(noGPS) | Motors locked, no GPS fix |
| 1 | ALT_HOLD | Flying, altitude hold |
| 2 | POS_HOLD | Flying, position hold (GPS) |
| 3 | RTH | Return to home |
| 4 | FOLLOW | Follow-me mode |
| 5 | CIRCLE | Point of interest circle |
| 6 | WAYPOINT | Waypoint mission |
| 7 | SHUTDOWN | Shutting down |
| 8 | GYRO_CAL | Gyroscope calibrating |
| 9 | COMPASS_H_CAL | Compass horizontal calibration |
| 10 | COMPASS_V_CAL | Compass vertical calibration |
| 11 | LANDING | Landing in progress |
| **12** | **IDLE** | **Armed, ready to fly** |
| 13 | LOCKED(GPS) | Motors locked, GPS acquired |

## 0xAA Command Reference

### Commands (Phone → Drone, via UDP 17000)

| MsgId | Name | Len | Example Raw | Purpose |
|-------|------|-----|-------------|---------|
| 0x01 | GPSInFo | 16 | `aa 01 10 xx [12B GPS]` | Phone GPS position for RTH/follow |
| 0x02 | SetFence | 8 | `aa 02 08 55 0f 1e 1e 00` | Set geofence limits |
| 0x03 | Status | 6 | `aa 03 06 0e 05 00` | FC status query |
| 0x04 | SetFollow | - | | Enable follow-me |
| 0x05 | ExitFollow | - | | Exit follow-me |
| 0x16 | SetTurnBack | - | | Enable RTH |
| 0x17 | ExitTurnBack | - | | Exit RTH |
| 0x18 | AppPhoto | - | | Take photo |
| 0x19 | AppRecord | - | | Start/stop recording |
| 0x1C | CtrlCmd | 11 | `aa 1c 0b xx RR PP TT YY FF 00 00` | Joystick (NOT working on this model) |
| 0x1D | SetCmd | 5 | `aa 1d 05 xx CC` | Discrete command |

### SetCmd (0x1D) Values

| Value | Name | Observed Effect |
|-------|------|-----------------|
| 0x00 | Clear | Clears previous SetCmd state |
| 0x01 | Headless | Headless mode toggle |
| 0x02 | GyroCal | Gyro recalibration |
| 0x04 | CompassCal | **Compass calibration** (misleadingly named "eCmdUnLock" in APK) |
| 0x08 | TakeoffLand | **Takeoff/land toggle — CONFIRMED WORKING when in IDLE mode** |

### CtrlCmd (0x1C) Flags

| Bit | Name | Purpose |
|-----|------|---------|
| 0 (0x01) | isLock | Arm/disarm toggle |
| 1 (0x02) | isHSpd | High speed mode |
| 2 (0x04) | isEStop | Emergency stop |
| 3 (0x08) | isGps | GPS mode toggle |
| 4 (0x10) | isLight | LED toggle |
| 5 (0x20) | isPtzUp | Gimbal tilt up |
| 6 (0x40) | isPtzDn | Gimbal tilt down |

**Note:** CtrlCmd joystick values have no effect on this model (`curContrler: "no"`). The stick values (roll/pitch/throttle/yaw, 0-255, 128=center) are accepted but not forwarded to the FC.

### Telemetry (Drone → Phone, via TCP 18000 SerialData relay)

| MsgId | Name | Len | Rate | Content |
|-------|------|-----|------|---------|
| 0x01 | GPSInFo | 31 | ~2 Hz | Full flight state (see field table above) |
| 0x02 | SetFence ack | 5 | On request | Always `aa 02 05 08 01` |
| 0x03 | Status ack | 5 | On request | Always `aa 03 05 09 01` |
| 0x95 | DeviceID | 16 | ~0.3 Hz | `aa 95 10 d9 00 01 00 00 48 53 37 31 30 00 00 00` ("HS710") |

## GOL Command Reference (TCP 18000)

| CmdId | Name | RWBit | Purpose | Status |
|-------|------|-------|---------|--------|
| 0x30000 | AuthChallenge | 0 | Drone sends auth challenge on connect | Working |
| 0x30001 | AuthResponse | 3 | Auth response/ack | Working |
| 0x40000 | DevInfo | 1 | Device info query (works without auth) | Working |
| 0x40001 | DevCtl | 3 | DevCtlPower on/off | Working |
| 0x10000 | SpCfg | 3 | Serial port config query | Working |
| 0x10001 | SerialData | 0 (drone→app) | 0xAA telemetry relay from FC | Working |
| 0x20000 | StPlay | 3 | Stream parameters query | Working |
| 0x20001 | StStop | 2 | Stream stop + prepare video | Working |
| 0x20002 | IFrame | 2 | Request video keyframe | Working (HolyStone-FPV uses this) |
| 0x80000 | SdCfg | 3 | SD card config | Working |
| 0x90000 | ExtCmd | 3 | Extended commands (capabilities, time, resolution) | Working |

## Motor Arming — WiFi Cannot Do This

Arming (LOCKED → IDLE) **requires the RF controller**. This is confirmed by:

1. **Both apps** (Ophelia GO, HolyStone-FPV) never send an arm command via WiFi
2. **Brute-force** of all SetCmd values (0x00-0x80) — none trigger arming
3. **CtrlCmd isLock flag** — no effect (5s sustained, 500ms pulse, combined with sticks)
4. **Stick arm gestures** (throttle+yaw min/max) — no effect
5. **Successful flight test** — RF arm → WiFi takeoff works; WiFi-only does not
6. **`curContrler: "no"`** — firmware explicitly reports WiFi joystick control is disabled

### Path Forward for Arming

The RF controller uses a 2.4 GHz proprietary protocol (separate from WiFi). Options:

1. **RF reverse engineering** — open controller, identify transceiver chip, sniff SPI bus, replicate protocol on Pi with nRF24L01+ or similar module
2. **Direct FC UART** — bypass WiFi module, wire Pi UART directly to FC at 38400 baud
3. **Firmware modification** — dump SPI flash, patch `curContrler` to "yes", reflash
4. **Hybrid approach** — use RF for arm only, WiFi for everything else (current working method)

## 0x68 Protocol (Observed in HolyStone-FPV First Capture)

The HolyStone-FPV app sent three non-0xAA packets on UDP 17000 during its first connection:

```
68 0a 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 05    (19 bytes)
68 0b 08 00 00 00 00 00 00 00 00 03                          (12 bytes)
68 01 0d 80 80 80 80 20 18 00 00 00 00 00 00 00 34          (17 bytes)
```

The third packet contains `80 80 80 80` (centered joystick values) suggesting this is a control frame. These were only seen in the first connection attempt and NOT in the successful flight capture. Purpose unclear — may be an alternative FC protocol or initialization handshake.

## Capture Files

| File | Content | Packets | Key Findings |
|------|---------|---------|--------------|
| `startup_capture.pcap` | Ophelia GO full startup | 2496 | Complete init sequence, DevCtlPower=1 discovery |
| `capture.pcap` | Ophelia GO steady-state | 5000 | Three-channel architecture discovery |
| `takeoff_capture.pcap` | HolyStone-FPV idle session | 5000 | 0x68 protocol packets, same auth |
| `hsfpv_capture.pcap` | HolyStone-FPV RF arm + WiFi takeoff | 5000 | **SetCmd(0x08) takeoff confirmed** |

## Key Files

| File | Location | Purpose |
|------|----------|---------|
| `decode_key.py` | project dir + `/tmp/` (Pi) | Auth crypto implementation |
| `keytable.bin` | project dir + `/tmp/` (Pi) | 2048-byte key table for auth |
| `full_init.py` | `/tmp/` (Pi) | Working init sequence — telemetry confirmed |
| `flight_test2.py` | `/tmp/` (Pi) | Flight test with CtrlCmd arm attempt |
| `arm_test.py` | `/tmp/` (Pi) | Brute force arm methods (all failed) |
| `decode_hsfpv2.py` | `/tmp/` (Mac) | Decoder for hsfpv_capture.pcap |

## iPhone Traffic Capture Setup

```bash
# Create virtual interface
rvictl -s 00008130-001E04143E10001C    # → creates rvi0

# Capture
sudo tcpdump -i rvi0 -nn -w output.pcap -c 5000 'host 172.16.11.1'

# Cleanup
rvictl -x 00008130-001E04143E10001C
```
