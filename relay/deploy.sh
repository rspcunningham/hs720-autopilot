#!/bin/bash
# Deploy relay code to Pi and restart the service.
# Run from Mac: ./relay/deploy.sh
set -e

PI_HOST="${1:-pi@pi1.local}"
REMOTE_DIR="/opt/drone-relay"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[*] Deploying to $PI_HOST:$REMOTE_DIR ..."

# Ensure remote directory exists
ssh "$PI_HOST" "sudo mkdir -p $REMOTE_DIR && sudo chown pi:pi $REMOTE_DIR"

# Sync files
rsync -av --delete \
    "$SCRIPT_DIR/relay.py" \
    "$SCRIPT_DIR/protocol.py" \
    "$SCRIPT_DIR/auth.py" \
    "$SCRIPT_DIR/keytable.bin" \
    "$SCRIPT_DIR/drone-relay.service" \
    "$SCRIPT_DIR/setup.sh" \
    "$PI_HOST:$REMOTE_DIR/"

# Restart service (if installed)
if ssh "$PI_HOST" "systemctl is-enabled drone-relay.service 2>/dev/null"; then
    echo "[*] Restarting service..."
    ssh "$PI_HOST" "sudo systemctl restart drone-relay.service"
    sleep 1
    ssh "$PI_HOST" "sudo systemctl status drone-relay.service --no-pager -l"
else
    echo "[*] Service not installed yet. Run:"
    echo "    ssh $PI_HOST 'sudo bash $REMOTE_DIR/setup.sh'"
fi

echo "[+] Deploy complete"
