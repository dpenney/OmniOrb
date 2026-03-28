import os
import json

# Serial Configuration
SERIAL_PORTS = ['/dev/serial0', '/dev/ttyS0', '/dev/ttyAMA0']
SERIAL_BAUD = 115200

# GPIO Pins (BCM numbering)
# Encoder CLK on GPIO 17, DT on GPIO 22
PIN_ROTARY_CLK = 17
PIN_ROTARY_DT  = 22
PIN_ROTARY_SW  = None # Not currently used
PIN_SFT_GND    = 27   # Software Ground for Encoder

# Audio Configuration
AUDIO_DEVICE_INDEX = 1
AUDIO_CHANNELS     = 2
AUDIO_RATE         = 48000
AUDIO_CHUNK        = 1024

# Logging
LOG_FILE = "/home/pi/assistant/assistant.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# UI Update Frequency
AUDIO_UPDATE_HZ = 10 
SERIAL_READER_SLEEP = 0.1
ENCODER_POLL_SLEEP = 0.001

# Flask Configuration
FLASK_HOST = '0.0.0.0'
FLASK_PORT = 5000
