#!/bin/bash
# 📡 ADSB Receiver (dump1090-fa) Setup
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
echo -e "📡 Setting up ADSB Receiver..."
echo -e "${BLUE}------------------------------------------${NC}"

# 1. Update package list
echo -e "🔄 Updating apt package list..."
sudo apt-get update

# 2. Add FlightAware repository
# We detect the OS codename to pick the right repository package
OS_CODENAME=$(lsb_release -sc)
REPOSITORY_URL="https://www.flightaware.com/adsb/piaware/files/packages/pool/main/p/piaware-repository/piaware-repository_8.2_all.deb"

if [ "$OS_CODENAME" == "bullseye" ]; then
    REPOSITORY_URL="https://www.flightaware.com/adsb/piaware/files/packages/pool/main/p/piaware-repository/piaware-repository_8.2_all.deb"
elif [ "$OS_CODENAME" == "bookworm" ]; then
    # Assume 8.2 also works for Bookworm, but FA usually provides specific versions.
    # If a specific bookworm version is known, it should be updated here.
    REPOSITORY_URL="https://www.flightaware.com/adsb/piaware/files/packages/pool/main/p/piaware-repository/piaware-repository_8.2_all.deb"
fi

if ! dpkg -l "piaware-repository" >/dev/null 2>&1; then
    echo -e "📦 Adding FlightAware repository..."
    TEMP_DEB="/tmp/piaware-repository.deb"
    wget -O "$TEMP_DEB" "$REPOSITORY_URL"
    sudo dpkg -i "$TEMP_DEB"
    rm "$TEMP_DEB"
    sudo apt-get update
else
    echo -e "${GREEN}✅ FlightAware repository already present.${NC}"
fi

# 3. Install dump1090-fa
if ! dpkg -l "dump1090-fa" >/dev/null 2>&1; then
    echo -e "📥 Installing dump1090-fa..."
    sudo apt-get install -y dump1090-fa
else
    echo -e "${GREEN}✅ dump1090-fa is already installed.${NC}"
fi

# 4. Service Management
echo -e "⚙️  Configuring services..."
sudo systemctl enable dump1090-fa
sudo systemctl restart dump1090-fa

# 5. Verification
echo -e "${BLUE}------------------------------------------${NC}"
echo -e "🎉 ${GREEN}ADSB Setup Complete!${NC}"
echo -e "${BLUE}------------------------------------------${NC}"
echo -e "Verify local JSON: ${BLUE}http://localhost:8080/data/aircraft.json${NC}"
echo -e "View Map:         ${BLUE}http://$(hostname).local/skyaware/${NC}"
echo -e "${BLUE}------------------------------------------${NC}"
