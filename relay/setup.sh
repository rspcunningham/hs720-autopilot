#!/bin/bash
# First-time setup on the Pi. Run via:
#   ssh pi@pi1.local 'sudo bash /opt/drone-relay/setup.sh'
set -e

echo "=== Drone Relay Setup ==="

# Install systemd service
cp /opt/drone-relay/drone-relay.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable drone-relay.service

# Remove old NAT/forwarding rules
echo "Cleaning up old NAT rules..."
nft flush ruleset 2>/dev/null || true
sysctl -w net.ipv4.ip_forward=0 >/dev/null 2>&1 || true
# Remove persistent forwarding if set
sed -i 's/^net.ipv4.ip_forward=1/net.ipv4.ip_forward=0/' /etc/sysctl.conf 2>/dev/null || true

# Start the service
systemctl restart drone-relay.service
echo "=== Done. Service status: ==="
systemctl status drone-relay.service --no-pager -l
