#!/bin/bash
# 🥧 ESP32 Assistant "One-Click" Installer
set -euo pipefail

# Get absolute path to this script's directory
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

# Colors for better feedback
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}------------------------------------------${NC}"
echo -e "🚀 Starting Assistant Installation..."
echo -e "${BLUE}------------------------------------------${NC}"

# 1. User Validation
if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}❌ Error: Please DO NOT run this script with 'sudo'. Run it as the 'pi' user.${NC}"
    echo "The script will use 'sudo' internally when it needs to."
    exit 1
fi

# 2. Install System Dependencies
echo -e "📦 Checking system dependencies..."
DEPENDENCIES="python3-venv python3-pip portaudio19-dev python3-pyaudio python3-numpy rsync"
MISSING_DEPS=""

for dep in $DEPENDENCIES; do
    if ! dpkg -l "$dep" >/dev/null 2>&1; then
        MISSING_DEPS="$MISSING_DEPS $dep"
    fi
done

if [ -n "$MISSING_DEPS" ]; then
    echo -e "${YELLOW}Installing missing dependencies:${NC} $MISSING_DEPS"
    sudo apt-get update
    sudo apt-get install -y $MISSING_DEPS
else
    echo -e "${GREEN}✅ All system dependencies already installed.${NC}"
fi

# 3. Create Virtual Environment
# We ensure system-site-packages is enabled so we can use system numpy/pyaudio
VENV_CFG="venv/pyvenv.cfg"
if [ ! -f "venv/bin/pip" ] || [ ! -f "$VENV_CFG" ] || ! grep -q "include-system-site-packages = true" "$VENV_CFG"; then
    echo -e "🐍 Creating/Updating virtual environment (venv)..."
    if [ -d "venv" ]; then
        echo -e "${YELLOW}🧹 Removing old/incompatible venv...${NC}"
        rm -rf venv 
    fi
    python3 -m venv --system-site-packages venv
else
    echo -e "${GREEN}✅ Venv already exists and is correctly configured.${NC}"
fi

# 4. Install Python Ingredients
echo -e "📥 Installing python dependencies into venv..."
./venv/bin/pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    ./venv/bin/pip install -r requirements.txt
else
    echo -e "${RED}❌ requirements.txt not found!${NC}"
    exit 1
fi

# 5. Configuration Setup
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo -e "📝 Creating .env from template..."
        cp .env.example .env
        echo -e "${YELLOW}⚠️  Please edit .env with your Google API Key later!${NC}"
    else
         echo -e "${RED}⚠️  Warning: .env.example not found. Skipping .env creation.${NC}"
    fi
else
    echo -e "${GREEN}✅ .env file exists.${NC}"
fi

# 6. Systemd Service Integration
echo -e "⚙️  Installing systemd service..."

# Create a temporary service file with correct paths
SERVICE_FILE="assistant.service"
if [ -f "$SERVICE_FILE" ]; then
    TEMP_SERVICE="/tmp/assistant.service"
    sed "s|{{APP_PATH}}|$SCRIPT_DIR|g" "$SERVICE_FILE" > "$TEMP_SERVICE"
    sudo cp "$TEMP_SERVICE" /etc/systemd/system/assistant.service
    rm "$TEMP_SERVICE"
    
    sudo systemctl daemon-reload
    sudo systemctl enable assistant.service
    sudo systemctl restart assistant.service
    echo -e "${GREEN}✅ Service installed and restarted.${NC}"
else
    echo -e "${RED}❌ $SERVICE_FILE not found!${NC}"
fi

echo -e "${BLUE}------------------------------------------${NC}"
echo -e "🎉 ${GREEN}Installation Complete!${NC}"
echo -e "${BLUE}------------------------------------------${NC}"
echo -e "Monitor logs: ${BLUE}tail -f $SCRIPT_DIR/assistant.log${NC}"
echo -e "Edit config:  ${BLUE}nano $SCRIPT_DIR/config.py${NC}"
echo -e "Edit secrets: ${BLUE}nano $SCRIPT_DIR/.env${NC}"
echo -e "${BLUE}------------------------------------------${NC}"
