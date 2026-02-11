
# Network Setup: Mac → Pi Relay → Drone

## Overview

The Pi runs a relay service that connects to the drone's WiFi, handles authentication and initialization, then multiplexes telemetry, video, and serial commands to any number of clients on the home network.

```
Mac (192.168.2.x)  ──eth0──  Pi (pi1.local)  ──wlan0──  Drone (172.16.11.1)
                   home LAN    relay service    drone WiFi
```

Clients never connect to the drone directly — the relay handles everything.

## Relay Ports

| Port | Protocol | Direction | Content |
|------|----------|-----------|---------|
| 4900 | TCP | relay → client | GOL frames (telemetry, status, commands) |
| 4901 | UDP | relay → client | Video (send any UDP packet to register) |
| 4902 | UDP | client → relay | Serial uplink (forwarded to drone:17000) |

## Deployment

```bash
# Deploy and start (handles first-time setup automatically)
./relay/deploy.sh                        # default: pi@pi1.local
./relay/deploy.sh --host pi@mypi.local   # custom host

# Check relay logs
ssh pi@pi1.local 'sudo journalctl -u drone-relay.service -f'
```

## Configuration

Edit `relay/relay.env` before deploying if defaults don't match your setup:

```env
DRONE_SSID=HolyStoneFPV-4bc278D
DRONE_IP=172.16.11.1
RELAY_TCP_PORT=4900
RELAY_VIDEO_PORT=4901
RELAY_SERIAL_PORT=4902
WIFI_IFACE=wlan0
```

## Relay State Machine

```
SCANNING → WIFI_CONNECTING → DRONE_CONNECTING → READY
    ↑              ↑                 ↑              │
    └──────────────┴─────────────────┴── on failure ┘
```

- **scanning**: Looking for drone SSID via `nmcli` WiFi scan
- **wifi_connecting**: Connecting to drone WiFi
- **drone_connecting**: TCP connect + auth handshake + init sequence
- **ready**: Relaying data between drone and clients

Clients receive a status GOL frame (cmd `0xFF000`) on connect and on every state change.

## Notes

- The drone WiFi is open (no password). SSID: `HolyStoneFPV-4bc278D`
- The relay runs as a systemd service (`drone-relay.service`), auto-restarts on failure
- No NAT, IP forwarding, or routing needed — the relay proxies all traffic
- The drone only needs to be powered on — the relay handles connection automatically
