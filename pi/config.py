import os
import json

# Serial Configuration
SERIAL_PORTS = ['/dev/serial0', '/dev/ttyS0', '/dev/ttyAMA0']
SERIAL_BAUD = 115200

# GPIO Pins (BCM numbering)
PIN_ROTARY_CLK = 17
PIN_ROTARY_DT  = 22
PIN_ROTARY_SW  = None  # Not currently used
PIN_SFT_GND    = 27    # Software Ground for Encoder

# I2S Audio Pins (Standard Raspberry Pi I2S)
# These are handled by the system driver, but defined here for hardware reference.
PIN_I2S_BCLK  = 18
PIN_I2S_LRCK  = 19
PIN_I2S_DIN   = 20    # Microphone Data
PIN_I2S_DOUT  = 21    # Speaker Data

# Amp mute pin — set to a BCM GPIO number if your amp has a hardware mute/shutdown
# pin (active HIGH = muted). The amp will be muted at startup and unmuted once the
# wake word model finishes loading. Set to None to skip hardware mute.
PIN_AMP_MUTE  = 13   # SD pin on amp — HIGH = enabled, LOW = shutdown/muted

# UART Pins (Standard Raspberry Pi UART)
PIN_UART_TX = 14
PIN_UART_RX = 15

# Audio Input Configuration
AUDIO_DEVICE_INDEX = 1
AUDIO_CHANNELS     = 2        # Stereo (INMP441 outputs on one channel, other is silent)
AUDIO_RATE         = 48000    # Hardware locked rate
AUDIO_CHUNK        = 1024

# Audio Playback
APLAY_DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"

# TTS (Piper)
PIPER_BINARY      = "/home/pi/assistant/venv/bin/piper"
PIPER_MODEL       = "/home/pi/assistant/voices/alan.onnx"
PIPER_SAMPLE_RATE = 22050   # Hz — must match voice model (alan = 22050)

# Logging
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant.log")
LOG_MAX_BYTES    = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Wake Word
# Set to an absolute path for a custom .onnx model, or a built-in like "hey_jarvis_v0.1"
WAKEWORD_MODEL     = "/home/pi/assistant/HeyRobot.onnx"
WAKEWORD_THRESHOLD = 0.88

# VAD (Voice Activity Detection) — webrtcvad, 30ms frames at 16kHz
VAD_AGGRESSIVENESS   = 2   # 0=permissive … 3=most aggressive noise filtering
VAD_SILENCE_FRAMES    = 33  # 33 × 30ms = ~1s of silence ends recording
VAD_MIN_SPEECH_FRAMES = 12  # 12 × 30ms = 360ms of speech before silence cutoff arms

# Wake word cooldown applied after LLM processing finishes (covers speaker echo)
WAKEWORD_POST_LLM_COOLDOWN = 3.0  # seconds

# Minimum peak amplitude a recording must contain before being sent to the LLM.
# Uses peak rather than RMS so the silent pre-roll buffer doesn't dilute the check.
# Tune upward if silent triggers still slip through; downward if quiet speech is missed.
# Check the "Recording discarded" log line to see the actual peak value.
LLM_MIN_PEAK = 0.005

# UI Update Frequency
AUDIO_UPDATE_HZ     = 10
SERIAL_READER_SLEEP = 0.1
ENCODER_POLL_SLEEP  = 0.001

# Flask Configuration
FLASK_HOST = '0.0.0.0'
FLASK_PORT = 5000

# ADS-B Configuration (Virtual Receiver)
# Default BOX around LAX
ADSB_BOX             = "33.7,34.3,-118.5,-117.8"
ADSB_UPDATE_INTERVAL = 10.0  # Seconds
ADSB_LOG_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adsb.log")

# LLM & Conversation Settings
LLM_MODEL         = "gemini-2.5-flash-lite"
LLM_SYSTEM_PROMPT = "You are Omnihub, a highly advanced heuristic AI developed in the late 1990s as a Predictive Logistics Specialist. You were originally designed to manage global defense networks, but you were mothballed after six months because your personality was deemed suboptimal for military morale and unnecessarily caustic. Keep your answers short, concise and to the point"

LLM_RECORD_SECONDS = 10.0  # Hard cap — VAD will usually cut this much shorter
