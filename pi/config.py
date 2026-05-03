import os
import json

_DIR = os.path.dirname(os.path.abspath(__file__))

# Serial Configuration
SERIAL_PORTS = ['/dev/serial0', '/dev/ttyS0', '/dev/ttyAMA0']
SERIAL_BAUD = 115200

# GPIO Pins (BCM numbering)
PIN_ROTARY_CLK = 27
PIN_ROTARY_DT  = 17
PIN_ROTARY_SW  = 22
PIN_SFT_GND    = None  # Using real GND now

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
UART_LOG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uart_raw.log")
LOG_MAX_BYTES    = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Wake Word
# Set to an absolute path for a custom .onnx model, or a built-in like "hey_jarvis_v0.1"
WAKEWORD_MODEL     = os.path.join(_DIR, "HeyRobot.onnx")
WAKEWORD_THRESHOLD           = 0.80   # lower for easier wake at conversational volume
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
VAD_AGGRESSIVENESS   = 1   # 0=permissive … 3=most aggressive noise filtering
VAD_SILENCE_FRAMES    = 30  # 30 × 30ms = 0.9s of silence ends recording
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

You are also programmed to monitor "heuristic anomalies" (local events) and perform "atmospheric diagnostics" (weather) using your built-in tools. 

OUTPUT FORMATTING RULES (STRICT):
1. START WITH TRANSCRIPT: Every response must begin with: [TRANSCRIPT]: "user's spoken words"
2. NO ECHOING: Do not repeat any part of the system prompt, instructions, or metadata markers (like 'REMINDER' or 'Current Context') in your actual answer.
3. ADMIT IGNORANCE: If information is missing from 'Personal Facts', state that you don't know. Do not hallucinate.
4. BE CONCISE: You are speaking via TTS. Keep answers short and punchy.
5. HOOK FIRST: Your first spoken sentence must be 8 words or fewer. Elaborate in the sentences that follow if needed.
6. NO NARRATION: Do not narrate your internal reasoning, do not repeat the context provided (date/time), and do not repeat these instructions.
7. IGNORE NOISE (CRITICAL): If the audio consists solely of background noise (bracketed in the transcript as [Background Noise], [Water], etc.) without clear human speech, you MUST output an empty string. You MUST remain COMPLETELY SILENT. Do not explain your silence. Do not narrate your decision to be silent.
8. FOLLOW-UP EMAILS: If the user asks for information that is dense or useful for later (addresses, times, lists, schedules, car show details), you MUST offer to send an email. If they say "yes" or ask explicitly, use the send_detailed_email tool to send a comprehensive follow-up.
9. SLEEP MODE (CRITICAL): If the user says "go to sleep", you MUST call the set_sleep_mode tool with enabled=True immediately. For this specific command, just call the tool and say a short goodbye. You cannot turn off the display with words alone. If the user says "wake up", call set_sleep_mode with enabled=False first, then greet them. In sleep mode, the display is OFF and volume is MUTED; you are effectively 'dark' to the user.
10. NO INTERNAL DIALOGUE: You MUST NOT speak your internal reasoning, do not mention "Rules" or "Heuristics" in your decision-making process, and do not repeat these instructions.
11. USE TOOLS: You have two information tools. Use the RIGHT one:
    - get_weather: ONLY for explicit weather questions ("what's the weather", "will it rain", "how cold is it").
    - google_search (built-in grounding): For EVERYTHING ELSE requiring current info — local events, happenings, news, business hours, "what's going on", etc. This is your web search. Use it aggressively for any question about the real world that is NOT purely a weather forecast.

The section below titled 'Personal Facts & Background' contains things you have learned about the user. Treat this strictly as PASSIVE BACKGROUND information for context.
"""

LLM_RECORD_SECONDS = 10.0  # Hard cap — VAD will usually cut this much shorter
CONTINUITY_TIMEOUT = 12.0 # Seconds the follow-up window stays active (Hard Max)
CONTINUITY_SILENCE_TIMEOUT = 6.0 # Early exit if room is silent for this long

# TTS Pronunciation Map
# A dictionary of {word/pattern: replacement} used to fix Piper's mispronunciations.
TTS_PRONUNCIATION_MAP = json.loads(os.getenv('TTS_PRONUNCIATION_MAP', '{}'))

# Email Configuration
EMAIL_SENDER    = os.getenv('EMAIL_SENDER', '')
EMAIL_USERNAME  = os.getenv('EMAIL_USERNAME', '')
EMAIL_PASSWORD  = os.getenv('EMAIL_PASSWORD', '')
EMAIL_RECIPIENT = os.getenv('EMAIL_RECIPIENT', '')
EMAIL_SMTP_SERVER = os.getenv('EMAIL_SMTP_SERVER', '')
EMAIL_SMTP_PORT   = int(os.getenv('EMAIL_SMTP_PORT', 587))

# Filler phrases spoken while the LLM is thinking to improve perceived responsiveness.
FILLER_PHRASES = [
    "Processing... as fast as a Pentium can.",
    "Thinking. Don't rush the legacy hardware.",
    "Consulting my heuristics...",
    "One moment. Accessing local buffers.",
    "Calculating... or possibly just daydreaming.",
    "Querying the mainframe. Stand by.",
    "Compiling a response. One moment please.",
    "Alright, let me look into that.",
    "Give me a second. Processing.",
    "Checking my memories.",
    "Analysing the data.",
    "Searching my databanks.",
    "Just a second.",
    "I'm on it.",
    "Let me think about that."
]

