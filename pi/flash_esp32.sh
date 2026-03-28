#!/bin/bash
set -e

# Port definition
PORT="/dev/ttyS0"
BAUD="460800"

# Navigate to the firmware directory relative to the script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FW_DIR="$SCRIPT_DIR/firmware"

if [ ! -d "$FW_DIR" ]; then
    echo "Error: Firmware directory not found at $FW_DIR"
    exit 1
fi

cd "$FW_DIR"

echo "Attempting to flash ESP32-S3 on $PORT..."
echo "IMPORTANT: Ensure ESP32 is in BOOTLOADER MODE (Hold BOOT, tap RESET)."
echo "Available files: $(ls *.bin)"

# Flash the binaries
# ESP32-S3 default offsets: bootloader@0x0000, partitions@0x8000, firmware@0x10000
python3 -m esptool --chip esp32s3 --port "$PORT" --baud "$BAUD" \
    --before default_reset --after hard_reset write_flash -z \
    --flash_mode dio --flash_freq 80m --flash_size detect \
    0x0000 bootloader.bin \
    0x8000 partitions.bin \
    0x10000 firmware.bin

echo "Flashing complete! Reset your ESP32."
