#!/bin/bash
set -e

# Target Raspberry Pi address
PI_HOST="octopi.local"
PI_USER="pi"
PI_DIR="/home/pi/assistant"

# Ensure the target directory exists
ssh "$PI_USER@$PI_HOST" "mkdir -p $PI_DIR"

# Sync the contents of the pi/ directory to the target
# Use the script's directory as the source
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rsync -avz --exclude '__pycache__' --exclude 'venv' --exclude '*.pyc' \
    "$SCRIPT_DIR/" "$PI_USER@$PI_HOST:$PI_DIR/"

echo "Deployment to $PI_HOST complete."
echo "------------------------------------------"
echo "To finish setup, run the installer on the Pi:"
echo "ssh $PI_USER@$PI_HOST 'cd $PI_DIR && chmod +x install.sh && ./install.sh'"
echo "------------------------------------------"
