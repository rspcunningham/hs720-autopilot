#!/bin/bash
# Deploy relay to Pi and start the service.
# Handles first-time setup automatically. Safe to re-run.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/opt/drone-relay"

usage() {
    cat <<EOF
Usage: ./relay/deploy.sh [--host user@host]

  --host    SSH target (default: \$RELAY_HOST or pi@pi1.local)
  --help    Show this help
EOF
    exit 0
}

# Parse args
PI_HOST="${RELAY_HOST:-pi@pi1.local}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) PI_HOST="$2"; shift 2 ;;
        --help|-h) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

echo "[*] Deploying to $PI_HOST:$REMOTE_DIR ..."

# Ensure remote directory exists
ssh "$PI_HOST" "sudo mkdir -p $REMOTE_DIR && sudo chown pi:pi $REMOTE_DIR"

# Sync files
rsync -av --delete \
    "$SCRIPT_DIR/relay.py" \
    "$SCRIPT_DIR/protocol.py" \
    "$SCRIPT_DIR/auth.py" \
    "$SCRIPT_DIR/keytable.bin" \
    "$SCRIPT_DIR/relay.env" \
    "$SCRIPT_DIR/drone-relay.service" \
    "$PI_HOST:$REMOTE_DIR/"

# Install/update systemd service
echo "[*] Installing service..."
ssh "$PI_HOST" "sudo cp $REMOTE_DIR/drone-relay.service /etc/systemd/system/ \
    && sudo systemctl daemon-reload \
    && sudo systemctl enable drone-relay.service"

# Clean up old NAT rules (from earlier experiments)
ssh "$PI_HOST" "sudo nft flush ruleset 2>/dev/null || true; \
    sudo sysctl -w net.ipv4.ip_forward=0 >/dev/null 2>&1 || true; \
    sudo sed -i 's/^net.ipv4.ip_forward=1/net.ipv4.ip_forward=0/' /etc/sysctl.conf 2>/dev/null || true"

# Restart
echo "[*] Restarting service..."
ssh "$PI_HOST" "sudo systemctl restart drone-relay.service"
sleep 1
ssh "$PI_HOST" "sudo systemctl status drone-relay.service --no-pager -l"

echo "[+] Deploy complete"
