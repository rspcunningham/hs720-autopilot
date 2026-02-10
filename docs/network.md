
# Network Setup: Mac → Pi → Drone

## Overview

The Pi (pi1.local) acts as a router between the home network and the drone's WiFi.
No SSH tunnels needed — Mac talks directly to the drone IP (172.16.11.1) for all protocols.

```
Mac (192.168.2.x)  ──eth0──  Pi (192.168.2.129)  ──wlan0──  Drone (172.16.11.1)
                   home LAN                       drone WiFi
```

## Network Details

| Device | Interface | IP | Subnet |
|--------|-----------|-----|--------|
| Mac | en0 | DHCP (192.168.2.x) | 192.168.2.0/24 |
| Pi | eth0 | 192.168.2.129 | 192.168.2.0/24 |
| Pi | wlan0 | DHCP (172.16.11.x) | 172.16.11.0/24 |
| Drone | AP | 172.16.11.1 | 172.16.11.0/24 |

## Usage

Each time the drone is powered on, run:

```bash
./drone_connect.sh
```

This script:
1. Connects Pi wlan0 to drone WiFi (or confirms already connected)
2. Verifies IP forwarding + NAT rules on Pi (persistent across reboots)
3. Adds Mac route if missing (persists until Mac reboot)
4. Pings drone to verify end-to-end connectivity

## Manual Steps (if needed)

### Connect Pi to Drone WiFi
```bash
ssh pi@pi1.local "sudo nmcli dev wifi connect 'HolyStoneFPV-4bc278D' ifname wlan0"
```

### Add Mac Route
```bash
sudo route add -net 172.16.11.0/24 192.168.2.129
```

### Verify
```bash
ping -c 3 172.16.11.1
```

## What's Persistent (survives reboots)

**Pi** — configured once, saved to disk:
- IP forwarding: `/etc/sysctl.d/99-drone.conf`
- NAT rules: `/etc/nftables.conf` (loaded by `nftables.service` on boot)

**What needs re-running per session:**
- `drone_connect.sh` — connects Pi to drone WiFi (only step that resets on drone power cycle)

## Teardown

### Remove Mac route
```bash
sudo route delete -net 172.16.11.0/24
```

### Remove Pi NAT rules
```bash
ssh pi@pi1.local "sudo /usr/sbin/nft flush ruleset; sudo /usr/sbin/sysctl -w net.ipv4.ip_forward=0"
```

### Disconnect Pi from drone WiFi
```bash
ssh pi@pi1.local "sudo nmcli dev disconnect wlan0"
```

## Persistence

This setup is intentionally ephemeral — it resets on reboot. To make it persist:

**Pi (survives reboot):**
```bash
# /etc/sysctl.d/99-drone.conf
net.ipv4.ip_forward = 1

# Save nftables rules:
sudo /usr/sbin/nft list ruleset | sudo tee /etc/nftables.conf
sudo systemctl enable nftables
```

**Mac:** Add the route to a script or launch daemon (not done yet — manual for now).

## Notes

- The drone WiFi is open (no password). SSID: `HolyStoneFPV-4bc278D`
- NAT means the drone sees all traffic from Pi's wlan0 IP, not Mac's IP
- This works for TCP (18000), UDP (17000, 16000), and any other port
- The drone only needs to be powered on — no app connection required for WiFi AP
