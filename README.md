# HS720 Drone Control

Programmatic control of a Holy Stone HS720 drone from a Mac, via a Raspberry Pi relay. The protocol was reverse-engineered from the Ophelia GO Android app — see [`docs/reverse-engineering.md`](docs/reverse-engineering.md) for the full writeup.

**End goal:** neural network autonomous flight.

## Architecture

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  HS720 Drone │  WiFi   │ Raspberry Pi │   TCP/  │  Mac / Any   │
│   (WiFi AP)  │◄───────►│   (relay)    │◄──UDP──►│   Client     │
│  172.16.11.1 │         │  pi1.local   │         │              │
└──────────────┘         └──────────────┘         └──────────────┘
                          Ports:
                          4900 TCP — telemetry (broadcast to all clients)
                          4901 UDP — video (fan-out to all clients)
                          4902 UDP — serial uplink (command forwarding)
```

The Pi connects to the drone's WiFi, handles authentication and initialization, then relays telemetry + video to any number of clients on the local network. Clients never touch the drone directly.

## Quick Start

```bash
# 1. Deploy relay to Pi (one-time setup)
./relay/deploy.sh
ssh pi@pi1.local 'sudo bash /opt/drone-relay/setup.sh'

# 2. Power on drone, wait for relay to reach "ready"
ssh pi@pi1.local 'sudo journalctl -u drone-relay.service -f'

# 3. Launch dashboard on your Mac
uv run python main.py
# Open http://localhost:8080
```

## Project Structure

```
├── main.py              # Dashboard launcher
├── client/              # Client library (Python, zero dependencies)
│   ├── connection.py    #   TCP connection + telemetry parsing
│   ├── video.py         #   UDP video receiver
│   ├── auth.py          #   Keytable-based authentication
│   ├── protocol.py      #   GOL framing + 0xAA packet encoding
│   ├── telemetry.py     #   FlightState dataclass
│   ├── logger.py        #   JSONL flight logger
│   ├── dashboard.py     #   HTTP server (SSE telemetry + fMP4 video)
│   └── dashboard.html   #   Browser UI (dark theme, declarative JS)
├── relay/               # Raspberry Pi relay service
│   ├── relay.py         #   Main relay (auto WiFi, auth, broadcast)
│   ├── auth.py          #   Auth handshake
│   ├── protocol.py      #   GOL framing
│   ├── deploy.sh        #   rsync + systemctl restart
│   ├── setup.sh         #   First-time Pi setup (packages, service install)
│   └── drone-relay.service  # systemd unit file
├── research/            # Reverse engineering artifacts
│   ├── drone_control.py #   Early protocol exploration script
│   ├── decode_key.py    #   Keytable analysis
│   ├── quick_test.py    #   Quick connectivity test
│   ├── test_live.py     #   Live integration test
│   ├── captures/        #   Packet captures (.pcap, gitignored)
│   └── apk-decompiled/  #   Decompiled Ophelia GO app (gitignored)
├── docs/
│   ├── reverse-engineering.md  # Full RE writeup
│   ├── protocol.md             # Detailed protocol reference
│   └── network.md              # Network setup guide
└── logs/                # Flight logs (.jsonl, gitignored)
```

## Dashboard

The browser dashboard shows live telemetry and video at `http://localhost:8080`.

- **Video:** H.264 remuxed to fMP4 via ffmpeg (`-c:v copy`, no transcode), decoded by the browser's GPU via Media Source Extensions
- **Telemetry:** Server-Sent Events, ~1.5 updates/sec (GPS, altitude, battery, speed, satellites)
- **Relay status:** Live gateway state indicator (scanning / connecting / ready)
- Dashboard loads instantly — drone connection happens in the background

## Protocol Summary

| Layer | Transport | Content |
|-------|-----------|---------|
| GOL frames | TCP 8856 | Auth handshake, serial data, status |
| 0xAA packets | Inside GOL serial frames | Telemetry, joystick, commands |
| Video | UDP 16000 | `\x00GOL`-framed H.264, ~330 kbps |
| Serial uplink | UDP 17000 | 0xAA command packets to drone FC |

See [`docs/protocol.md`](docs/protocol.md) for the full packet format reference.

## Status

**Working:**
- Full relay pipeline (auto WiFi connect, auth, init, telemetry + video to clients)
- Browser dashboard with live telemetry SSE and video pipeline
- JSONL flight logging

**Next:**
- Send flight commands through relay (UDP 17000)
- Motor test (arm, takeoff, hover, land)
- Neural network flight controller
