#!/bin/bash
# Private installer for the ADS-B sidecar service.
# Run this on the Pi: bash install_adsb_sidecar.sh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VENV="$SCRIPT_DIR/venv/bin/pip"
PYTHON="$SCRIPT_DIR/venv/bin/python"
SERVICE_NAME="adsb_sidecar"

echo "--- ADS-B Sidecar Installer ---"

# 1. Install dependency into existing venv
echo "Installing zstandard..."
$VENV install --quiet zstandard

# 2. Write systemd service file
echo "Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Private ADS-B Sidecar Proxy
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON $SCRIPT_DIR/adsb_proxy_pi.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 3. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "Done! Sidecar running on port 5050."
echo "Logs: journalctl -u $SERVICE_NAME -f"
