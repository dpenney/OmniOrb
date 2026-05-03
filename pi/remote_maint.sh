#!/bin/bash
# remote_maint.sh - Remote maintenance script for OmniOrb Pi
# This script pulls the latest code from Git, updates dependencies, and restarts the service.

set -e

# Configuration
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="assistant.service"

echo "--- OmniOrb Remote Maintenance ---"
echo "Time: $(date)"
echo "Directory: $APP_DIR"

# 1. Pull latest code
echo "Checking for updates..."
cd "$APP_DIR"
git pull

# 2. Update dependencies if requirements.txt changed
if [ -f "requirements.txt" ]; then
    echo "Updating Python dependencies..."
    # Assumes venv is in $APP_DIR/venv as per assistant.service
    if [ -d "venv" ]; then
        ./venv/bin/pip install -r requirements.txt
    else
        echo "Warning: venv not found. Skipping dependency update."
    fi
fi

# 3. Restart the service
echo "Restarting $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME"

echo "Maintenance complete!"
echo "Current status:"
sudo systemctl status "$SERVICE_NAME" --no-pager
