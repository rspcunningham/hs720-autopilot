# HS720 Drone Control

Programmatic control of a Holy Stone HS720 drone from a Mac, via a Raspberry Pi relay. The protocol was reverse-engineered from the Ophelia GO Android app — see [`docs/reverse-engineering.md`](docs/reverse-engineering.md) for the full writeup.

## Architecture

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  HS720 Drone │  WiFi   │ Raspberry Pi │   TCP/  │  Mac / Any   │
│   (WiFi AP)  │◄───────►│   (relay)    │◄──UDP──►│   Client     │
│  172.16.11.1 │         │  pi1.local   │         │              │
└──────────────┘         └──────────────┘         └──────────────┘
                          Ports:
                          4900 TCP — telemetry (broadcast to all clients)
                          4901 UDP — video (fan-out to registered clients)
                          4902 UDP — serial uplink (command forwarding)
```

The Pi connects to the drone's WiFi, handles authentication and initialization, then relays telemetry + video to any number of clients on the local network. Clients never touch the drone directly.

## Quick Start

```bash
# 1. Deploy relay to Pi (handles first-time setup automatically)
./relay/deploy.sh                        # default: pi@pi1.local
./relay/deploy.sh --host pi@mypi.local   # custom host

# 2. Power on drone, launch dashboard
uv run python main.py              # default: pi1.local
uv run python main.py mypi.local   # custom relay host
# Open http://localhost:8080
```

Edit `relay/relay.env` before deploying if your drone SSID, IP, or WiFi interface differ from defaults.

## Client Ports

| Port | Transport | Direction | Content |
|------|-----------|-----------|---------|
| 4900 | TCP | relay → client | GOL frames (telemetry, status) |
| 4901 | UDP | relay → client | H.264 video (send any packet to register) |
| 4902 | UDP | client → relay | Serial commands (forwarded to drone FC) |

See [`docs/protocol.md`](docs/protocol.md) for the full packet format reference.
