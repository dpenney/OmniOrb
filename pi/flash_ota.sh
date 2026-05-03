#!/bin/bash
# flash_ota.sh - Wireless update for ESP32 from the Pi
# Usage: ./flash_ota.sh firmware.bin [IP_ADDRESS]

FW_FILE=$1
ESP_IP=${2:-"esp32-radar.local"}

if [ -z "$FW_FILE" ]; then
    echo "Usage: $0 firmware.bin [IP_ADDRESS]"
    exit 1
fi

if [ ! -f "$FW_FILE" ]; then
    echo "Error: Firmware file $FW_FILE not found."
    exit 1
fi

# Ensure espota.py is available
if [ ! -f "espota.py" ]; then
    echo "espota.py not found. Downloading from Espressif..."
    wget https://raw.githubusercontent.com/espressif/arduino-esp32/master/tools/espota.py -O espota.py
fi

echo "Flashing $FW_FILE to $ESP_IP via OTA..."
python3 espota.py -i "$ESP_IP" -p 3232 -f "$FW_FILE"

if [ $? -eq 0 ]; then
    echo "OTA Flash Successful!"
else
    echo "OTA Flash Failed."
    exit 1
fi
