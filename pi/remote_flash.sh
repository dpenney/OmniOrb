#!/bin/bash

# remote_flash.sh - Automatically finds the ESP32 and flashes it via OTA
# Usage: ./remote_flash.sh [firmware.bin]

FIRMWARE=${1:-firmware.bin}
LOG_FILE="/home/pi/assistant/pi/assistant.log"

if [ ! -f "$FIRMWARE" ]; then
    echo "Error: Firmware file '$FIRMWARE' not found."
    exit 1
fi

echo "Searching for ESP32 IP in logs..."
# Find the last 'IP:' entry in the assistant log
ESP_IP=$(grep -a "IP:" "$LOG_FILE" | tail -n 1 | sed 's/.*IP://')

if [ -z "$ESP_IP" ]; then
    echo "Error: Could not find ESP32 IP in $LOG_FILE."
    echo "Make sure the assistant service is running and the ESP32 is connected."
    exit 1
fi

echo "Found ESP32 at: $ESP_IP"

# Find which of our IPs is on the same subnet as the ESP32
SUBSET=$(echo $ESP_IP | cut -d. -f1-3)
PI_IP=$(ip -o -4 addr list | grep -F "$SUBSET" | awk '{print $4}' | cut -d/ -f1 | head -n 1)

if [ -n "$PI_IP" ]; then
    echo "Using Pi Host IP: $PI_IP for OTA handshake"
    export OTA_HOST_IP="$PI_IP"
fi

echo "Starting OTA Flash..."
/home/pi/assistant/pi/flash_ota.sh "$FIRMWARE" "$ESP_IP"
