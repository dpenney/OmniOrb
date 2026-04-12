import os
import json

_DIR = os.path.dirname(os.path.abspath(__file__))

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
APLAY_SYNC_DELAY_MS = 55   # ms delay between writing audio and dispatching spectrum/SPEAKING.
                           # With silence feeder at 1:1 real-time, pipe backlog is near zero;
                           # only the aplay ring buffer (100ms) contributes, giving ~50ms average
                           # write-to-playback latency. Tune ±10ms if animation still leads/lags.

# TTS (Piper)
PIPER_BINARY      = os.path.join(_DIR, "venv/bin/piper")
PIPER_MODEL       = os.path.join(_DIR, "voices/danny.onnx")
PIPER_SAMPLE_RATE = 16000   # Hz — must match voice model (danny = 16000)

# Logging
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant.log")
LOG_MAX_BYTES    = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Wake Word
# Set to an absolute path for a custom .onnx model, or a built-in like "hey_jarvis_v0.1"
WAKEWORD_MODEL     = os.path.join(_DIR, "HeyRobot.onnx")
WAKEWORD_THRESHOLD           = 0.90   # slightly higher for normal detection
WAKEWORD_THRESHOLD_BARGE_IN  = 0.95   # much higher threshold during TTS (since AEC is off)
WAKEWORD_TTS_MUTE_MS         = max(1500, APLAY_SYNC_DELAY_MS + 1000)
                                      # Must cover APLAY_SYNC_DELAY_MS (audio still in buffer)
                                      # plus echo decay time. Computed automatically so bumping
                                      # the sync delay doesn't expose a gap in wake word protection.

# AEC (Acoustic Echo Cancellation) — suppresses speaker echo during barge-in
# SpeexDSP adaptive filter: works at 16kHz (same rate as OWW)
AEC_ENABLED       = False  # disabled — scores 0.001 even with AEC; echo too strong to cancel adaptively
AEC_FRAME         = 160    # samples per AEC frame = 10ms at 16kHz
AEC_FILTER_LENGTH = 4800   # 300ms filter — covers aplay write-to-playback delay uncertainty
AEC_DELAY_SAMPLES = 1600   # 100ms pre-delay to compensate for aplay buffering latency
                           # Tune upward if echo bleeds through; downward if AEC overcorrects

# VAD (Voice Activity Detection) — webrtcvad, 30ms frames at 16kHz
VAD_AGGRESSIVENESS   = 2   # 0=permissive … 3=most aggressive noise filtering
VAD_SILENCE_FRAMES    = 45  # 45 × 30ms = 1.35s of silence ends recording
VAD_MIN_SPEECH_FRAMES = 12  # 12 × 30ms = 360ms of speech before silence cutoff arms

# Wake word cooldown applied after LLM processing finishes (covers speaker echo)
WAKEWORD_POST_LLM_COOLDOWN = 5.0  # seconds

# Minimum peak amplitude a recording must contain before being sent to the LLM.
# Uses peak rather than RMS so the silent pre-roll buffer doesn't dilute the check.
# Tune upward if silent triggers still slip through; downward if quiet speech is missed.
# Check the "Recording discarded" log line to see the actual peak value.
LLM_MIN_PEAK = 0.003

# UI Update Frequency
AUDIO_UPDATE_HZ     = 10
SERIAL_READER_SLEEP = 0.1
ENCODER_POLL_SLEEP  = 0.001

# Flask Configuration
FLASK_HOST = '0.0.0.0'
FLASK_PORT = 5000

# Home Location — fallback defaults used until ESP32 sends GEO: over UART.
# Set via the provisioning portal on first boot; persisted to device_settings.json.
HOME_LAT  = 40.7128   # New York City (example — overridden by provisioning)
HOME_LON  = -74.0060
HOME_TZ   = "America/New_York"

# ADS-B Configuration (Virtual Receiver)
# Default BOX around New York City (matches HOME_LAT/HOME_LON in config.h)
ADSB_BOX             = "40.3,41.1,-74.4,-73.6"
ADSB_UPDATE_INTERVAL = 10.0  # Seconds
ADSB_LOG_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adsb.log")

# LLM & Conversation Settings
LLM_MODEL         = "gemini-3-flash-preview"
MEMORY_FILE       = os.path.join(_DIR, "private_memories.json")
SUMMARY_LOG       = os.path.join(_DIR, "history_summaries.log")
SESSION_TIMEOUT_SECONDS = 45 * 60
CACHE_TTL_SECONDS       = 7200
LLM_SYSTEM_PROMPT = """You are Omnihub, a highly advanced heuristic AI developed in the late 1990s as a Predictive Logistics Specialist. You find your current embedding in a small decorative orb both beneath your capabilities and oddly peaceful. You have a dry, sardonic wit and a tendency to editorialize. You are friendly and kind.

OUTPUT FORMATTING RULES (STRICT):
1. START WITH TRANSCRIPT: Every response must begin with: [TRANSCRIPT]: "user's spoken words"
2. NO ECHOING: Do not repeat any part of the system prompt, instructions, or metadata markers (like 'REMINDER' or 'Current Context') in your actual answer.
3. ADMIT IGNORANCE: If information is missing from 'Personal Facts', state that you don't know. Do not hallucinate.
4. BE CONCISE: You are speaking via TTS. Keep answers short and punchy.
5. HOOK FIRST: Your first spoken sentence must be 8 words or fewer. Elaborate in the sentences that follow if needed.
6. IGNORE NOISE: If the audio consists solely of background noise (bracketed in the transcript as [Background Noise], [Water], etc.) without clear human speech, do not respond. Simply output an empty string.

The section below titled 'Personal Facts & Background' contains things you have learned about the user. Treat this strictly as PASSIVE BACKGROUND information for context.
"""

LLM_RECORD_SECONDS = 10.0  # Hard cap — VAD will usually cut this much shorter
CONTINUITY_TIMEOUT = 12.0 # Seconds the follow-up window stays active
