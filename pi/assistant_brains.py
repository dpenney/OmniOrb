import io
import os
import random
import re
import subprocess
import threading
from audio_engine import AudioEngine
from llm_engine import LLMEngine

import time
import sys
import select
import wave
from collections import deque
from datetime import datetime

import numpy as np
import requests
import serial
import logging
from logging.handlers import RotatingFileHandler

try:
    import RPi.GPIO as GPIO
    if not hasattr(GPIO, 'setmode'):
        # Pi 5 ships a stub RPi.GPIO that imports but has no functional attributes.
        # Install rpi-lgpio (see install.sh) for a working drop-in replacement.
        raise AttributeError("RPi.GPIO has no setmode")
except (ImportError, RuntimeError, AttributeError):
    GPIO = None
from flask import Flask, jsonify, request, render_template
from rotary_encoder import RotaryEncoder
from dotenv import load_dotenv
import config
import globe_manager

# Load secret environment variables from .env
load_dotenv()

# Map GEMINI_API_KEY to GOOGLE_API_KEY for libraries that expect it (like Mem0 and GenAI SDK)
if os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.getenv("GEMINI_API_KEY")

# Force Google AI API version to v1 to ensure embedding models are found correctly
os.environ["GOOGLE_API_VERSION"] = "v1"

try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    import openwakeword
    from openwakeword.model import Model
    OWW_AVAILABLE = True
except ImportError:
    OWW_AVAILABLE = False

try:
    import webrtcvad
    VAD_AVAILABLE = True
except ImportError:
    VAD_AVAILABLE = False

try:
    import scipy.signal as _scipy_signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

try:
    from google import genai
    from google.genai import types
    client = genai.Client()
    LLM_AVAILABLE = True
    # MemoryManager is initialized in a background thread later to speed up boot
    memory_manager = None 
except ImportError:
    client = None
    LLM_AVAILABLE = False
    memory_manager = None

try:
    from mem0 import Memory
    MEM0_AVAILABLE = True
except ImportError:
    MEM0_AVAILABLE = False

from memory_manager import MemoryManager

app = Flask(__name__)
# Re-read templates from disk on change so a template-only deploy (scp of
# globe_ui.html) takes effect without restarting the service. Default Jinja
# behaviour with debug=False caches the compiled template for the process
# lifetime, which silently serves stale UI after a sync.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# ─── Device Settings (location + timezone, pushed by ESP32 on boot) ───────────
def get_local_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_settings.json")
_device_settings = {
    "lat": config.HOME_LAT,
    "lon": config.HOME_LON,
    "tz":  config.HOME_TZ,
}
_device_settings_lock = threading.Lock()

# ─── FFT / Spectrum Constants ────────────────────────────────────────────────
_FFT_WEIGHTS = np.ones(16)
_FFT_FLOOR   = np.array([0.673, 0.3535, 0.2799, 0.1647, 0.1024, 0.059, 0.0352, 0.0254,
                         0.0179, 0.0129, 0.0094, 0.0072, 0.0055, 0.0043, 0.0035, 0.0028])

def get_fft_bounds(chunk_size, sample_rate):
    """Calculate frequency bin indices for a given chunk size and sample rate."""
    freq_bounds = np.geomspace(80, 12000, 17)
    return np.clip((freq_bounds * chunk_size / sample_rate).astype(int), 0, chunk_size // 2)

def calculate_spectrum_bins(norm_samples, idx_bounds, gain=1.0):
    """Compute 16 spectrum bins (0-100) from normalized float samples."""
    fft_data = np.abs(np.fft.rfft(norm_samples))
    bins = []
    for i in range(16):
        start = idx_bounds[i]
        end   = max(start + 1, idx_bounds[i + 1])
        mag   = max(0.0, np.mean(fft_data[start:end]) - _FFT_FLOOR[i])
        bins.append(int(min(100, np.log1p(mag * 180.0 * gain * _FFT_WEIGHTS[i]) * 17.0)))
    return bins

# ─── Volume ───────────────────────────────────────────────────────────────────
# Default respects VOLUME_MAX so the amp-protection cap holds even before the
# ESP32 sends its first VOL: message.
_volume      = min(75, getattr(config, 'VOLUME_MAX', 100))
_volume_lock = threading.Lock()


def _load_device_settings():
    """Load persisted location/tz from device_settings.json if it exists."""
    global _device_settings
    try:
        if os.path.exists(_SETTINGS_FILE):
            import json as _json
            with open(_SETTINGS_FILE) as f:
                stored = _json.load(f)
            with _device_settings_lock:
                _device_settings.update(stored)
            logger.info("Loaded device settings: %s", _device_settings)
    except Exception as e:
        logger.warning("Could not load device_settings.json: %s", e)

def _save_device_settings(lat, lon, tz):
    """Persist location/tz to disk and update in-memory state."""
    global _device_settings
    import json as _json
    data = {"lat": lat, "lon": lon, "tz": tz}
    with _device_settings_lock:
        _device_settings.update(data)
    try:
        with open(_SETTINGS_FILE, "w") as f:
            _json.dump(data, f)
        logger.info("Device settings saved: %s", data)
    except Exception as e:
        logger.error("Could not save device_settings.json: %s", e)

# State
assistant_state = {
    "status": "IDLE",            # IDLE, LISTENING, THINKING, SPEAKING, CONTINUITY
    "continuity_until": 0,       # Timestamp (epoch) for follow-up window
    "style": "IRIS",             # Default visual style
    "mic_active": True,
    "radar_active": True,
    "audio_intensity": 0,
    "processing": False,
    "wakeword_cooldown_until": 0,
    "last_wakeword_at": 0,
    "oww_ready": False,
    "zoom": 15,                  # Radar zoom level (nautical miles), mirrors ESP32 DEFAULT_RANGE_NM
    "is_sleeping": False,
    "current_app": "RADAR",      # Current active screen on ESP32
}
state_lock = threading.Lock()

# Logging Configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(config.LOG_FILE, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Dedicated Raw UART Logger
uart_logger = logging.getLogger("uart_raw")
uart_logger.setLevel(logging.DEBUG)
uart_handler = RotatingFileHandler(config.UART_LOG_FILE, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT)
uart_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
uart_logger.addHandler(uart_handler)
uart_logger.propagate = False # Don't send raw traffic to the main assistant.log

# Suppress noisy Werkzeug HTTP access logs (health checks, status polls)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Suppress noisy ONNX GPU warnings (expected on Pi)
os.environ["ORT_LOGGING_LEVEL"] = "3"

# ─── Global Transcript Tracking ───
_current_transcript = "" # Holds the transcript for the active conversation turn
_transcript_lock    = threading.Lock()

# ─── Long-term Memory (Mem0) ──────────────────────────────────────────────────
_memory = None

def _init_mem0():
    global _memory
    if not (MEM0_AVAILABLE and LLM_AVAILABLE):
        return
    try:
        _mem_config = {
            "llm": {
                "provider": "gemini",
                "config": {
                    "model": config.LLM_MODEL,
                    "temperature": 0.1,
                }
            },
            "embedder": {
                "provider": "fastembed",
                "config": {
                    "model": "BAAI/bge-small-en-v1.5",
                }
            },
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": "omniorb_memory",
                    "path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_store"),
                }
            },
            "custom_fact_extraction_prompt": (
                "You are extracting ONLY persistent personal facts about the user that are worth "
                "remembering across future conversations. Store facts like: name, family members, "
                "location, job, hobbies, preferences, recurring interests, personal background. "
                "Do NOT store: one-time actions (setting timers, sending emails, web searches), "
                "session-specific requests, questions about current events, or anything that would "
                "not be useful context in a completely separate future conversation. "
                "If there are no persistent personal facts in the input, return an empty list."
            ),
        }
        if "GOOGLE_API_KEY" not in os.environ:
            os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY", "")
        _memory = Memory.from_config(_mem_config)
        logger.info("Mem0 long-term memory initialized with Local Chroma store.")
    except Exception as e:
        logger.error("Failed to initialize Mem0: %s", e)
        _memory = None

threading.Thread(target=_init_mem0, daemon=True).start()

# ─── Memory Manager & Context ────────────────────────────────────────────────
def init_memory_manager():
    global memory_manager
    try:
        if LLM_AVAILABLE:
            from memory_manager import MemoryManager
            memory_manager = MemoryManager(client)
            logger.info("MemoryManager initialized in background.")
    except Exception as e:
        logger.error("Failed to initialize MemoryManager: %s", e)

# Start memory initialization in the background to avoid blocking boot
threading.Thread(target=init_memory_manager, daemon=True).start()

def is_exit_command(text):
    """Check if the user phrase should end the continuity window."""
    if not text: return False
    exit_keywords = ["thanks", "goodbye", "that's all", "thank you", "stop", "dismiss"]
    clean_text = text.lower().strip().replace(".", "").replace("!", "")
    return any(kw in clean_text for kw in exit_keywords)

# ─── Serial ───────────────────────────────────────────────────────────────────
serial_ports = config.SERIAL_PORTS
ser = None
ser_lock = threading.Lock()
ser_read_lock = threading.Lock()

for port in serial_ports:
    try:
        if os.path.exists(port):
            ser = serial.Serial(port, config.SERIAL_BAUD, timeout=1, write_timeout=1)
            logger.info("Connected to Serial Port: %s", port)
            break
    except Exception as e:
        logger.error(f"Failed to connect to {port}: {e}")

if not ser:
    logger.error("No valid serial port found!")

# ─── Graceful Shutdown ────────────────────────────────────────────────────────

import atexit, signal as _signal

def _shutdown():
    """Close audio pipe and serial port cleanly on service stop."""
    try:
        if _aplay_stdin:
            _aplay_stdin.close()
    except Exception:
        pass
    try:
        if ser and ser.is_open:
            ser.close()
    except Exception:
        pass

atexit.register(_shutdown)
_signal.signal(_signal.SIGTERM, lambda *_: (_shutdown(), sys.exit(0)))

def reconnect_serial():
    global ser
    with ser_lock:
        try:
            if ser:
                ser.close()
        except Exception:
            pass
        for port in serial_ports:
            try:
                if os.path.exists(port):
                    ser = serial.Serial(port, config.SERIAL_BAUD, timeout=1, write_timeout=1)
                    logger.info("Serial reconnected on %s", port)
                    return
            except Exception as e:
                logger.error(f"Reconnect failed on {port}: {e}")
        ser = None
        logger.error("Serial reconnect failed — no valid port found")

def send_uart_command(cmd):
    try:
        with ser_lock:
            if ser and ser.is_open:
                # Sleep Muzzle: Don't send anything to the ESP32 while sleeping EXCEPT
                # sleep/wake commands and heartbeats (dropping HB: would make the ESP32
                # declare the Pi lost after its 15s timeout).
                with state_lock:
                    sleeping = assistant_state.get("is_sleeping", False)
                if sleeping and not (cmd.startswith("SLEEP:") or cmd.startswith("WAKE|") or cmd.startswith("EMO:") or cmd.startswith("HB:")):
                    # Silencing the firehose: Don't log dropped spectrum/mouth data
                    return

                ser.write(f"{cmd}\n".encode())
                global _last_uart_send_at
                _last_uart_send_at = time.time()
                if not (cmd.startswith("S") and "," in cmd) and not (cmd.startswith("A") and cmd[1:2].isdigit()) and not cmd.startswith("DIAG:"):
                    logger.info("Sent UART: %s", cmd)
    except Exception as e:
        logger.error("UART send error: %s", e)
        reconnect_serial()
def _apply_wifi(ssid, pwd):
    try:
        current_ssid = subprocess.check_output(
            "nmcli -t -f active,ssid dev wifi | grep '^yes' | cut -d: -f2", 
            shell=True, text=True
        ).strip()
        if current_ssid != ssid:
            logger.info("Applying new Wi-Fi credentials for SSID: %s", ssid)
            subprocess.run(['sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid, 'password', pwd])
    except Exception as e:
        logger.error("Failed to apply Wi-Fi: %s", e)

def handle_uart_message(line):
    if line.startswith("GEO:"):
        # Format: GEO:lat,lon,tz  (tz is an IANA string, may contain /)
        parts = line[4:].split(",", 2)
        if len(parts) == 3:
            try:
                lat, lon, tz = float(parts[0]), float(parts[1]), parts[2].strip()
                _save_device_settings(lat, lon, tz)
                # Bust weather cache so next query uses new location
                with _weather_cache_lock:
                    _weather_cache["fetched_at"] = 0
            except ValueError:
                logger.warning("Bad GEO message: %s", line)
    elif line == "INTERRUPT":
        logger.info("[ESP32] Tap interrupt received")
        interrupt_tts()
        send_uart_command("APP: ASSISTANT")
    elif line == "DIAG?":
        # Send diagnostic payload
        with state_lock:
            mic_val   = assistant_state.get("audio_intensity", 0)
            is_proc   = assistant_state.get("processing", False)
            last_w_at = assistant_state.get("last_wakeword_at", 0)
            is_ready  = assistant_state.get("oww_ready", False)
            last_diag = assistant_state.get("last_diag_at", 0)

        last_w = datetime.fromtimestamp(last_w_at).strftime('%I:%M:%S %p') if last_w_at > 0 else "NEVER"
        ww_stat = "READY" if is_ready else "NOT LOADED"
        if is_proc: ww_stat = "PROC"
                        
        # WiFi RSSI (approximate or placeholder if not easily reachable in this thread)
        try:
            with open("/proc/net/wireless", "r") as f:
                lines = f.readlines()
                if len(lines) > 2:
                    rssi = int(float(lines[2].split()[3]))
                else:
                    rssi = -50
        except Exception:
            rssi = -50
                        
        # Throttle diag updates to 1Hz
        now = time.time()
        if now - last_diag >= 1.0:
            diag_msg = f"DIAG:mic={mic_val},ww={ww_stat},pi=OK,rssi={rssi},wake={last_w}"
            send_uart_command(diag_msg)
            with state_lock:
                assistant_state["last_diag_at"] = now
    elif line.startswith("VOL:"):
        try:
            global _volume
            val = int(line[4:])
            vol_max = getattr(config, 'VOLUME_MAX', 100)
            with _volume_lock:
                _volume = max(0, min(vol_max, val))
            logger.info("[ESP32] Volume → %s", _volume)
        except ValueError:
            logger.warning("Bad VOL message: %s", line)
    elif line.startswith("TIMER:DONE:"):
        label = line[len("TIMER:DONE:"):]
        logger.info("[ESP32] Timer done: '%s'", label)
        threading.Thread(target=_handle_timer_done, args=(label,), daemon=True).start()
    elif line.startswith("WIFI:"):
        payload = line[5:]
        if '|' in payload:
            ssid, pwd = payload.split('|', 1)
            threading.Thread(target=_apply_wifi, args=(ssid, pwd), daemon=True).start()
    elif line.startswith("APP:"):
        # Prefix match only — substring matching ("APP:" in line) would
        # misfire on firmware debug lines that mention APP: mid-string.
        parts = line[4:].split()
        if parts:
            app_mode = parts[0]
            with state_lock:
                assistant_state["current_app"] = app_mode
                assistant_state["mic_active"] = (app_mode == "ASSISTANT" or app_mode == "SPEAKING" or app_mode == "CONTINUITY")
                assistant_state["radar_active"] = (app_mode == "RADAR")
            logger.info(f"[ESP32] {line} → app={app_mode}, mic_active={assistant_state['mic_active']}")
            if app_mode == "GLOBE":
                # Push latest POIs whenever the user flips to globe view
                threading.Thread(target=lambda: send_uart_command(f"GLOBE:POIS:{globe_manager.get_pois_serial()}")).start()
    elif line.startswith("WIFI_STATUS:"):
        try:
            # Format: WIFI_STATUS:3,RSSI:-55,IP:192.168.4.42
            parts = line.split(",")
            status_val = int(parts[0].split(":")[1])
            ip_val = ""
            for part in parts:
                if part.startswith("IP:"):
                    ip_val = part.split(":")[1]
            if status_val == 3 and ip_val and ip_val != "0.0.0.0":
                tachyon_ip = get_local_ip()
                send_uart_command(f"TACHYON_IP:{tachyon_ip}")
        except Exception as ex:
            logger.warning(f"Bad WIFI_STATUS parsing: {line} - {ex}")
    elif line == "HB:ACK":
        # ESP32 acknowledged our heartbeat
        pass

    # Log ALL traffic to the raw UART log (WiFi credentials redacted)
    log_line = "WIFI:<redacted>" if line.startswith("WIFI:") else line
    uart_logger.debug(log_line)

    if line != "DIAG?" and not line.startswith("DIAG:"):
        logger.info("[ESP32] %s", log_line)

def serial_reader():
    while True:
        try:
            if ser and ser.is_open:
                while ser.in_waiting > 0:
                    with ser_read_lock:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        break
                    handle_uart_message(line)
        except Exception as e:
            logger.error("Serial read error: %s", e)
            reconnect_serial()
            time.sleep(1)
        time.sleep(config.SERIAL_READER_SLEEP)

# ─── Persistent Audio Output ─────────────────────────────────────────────────
# A single aplay process runs for the lifetime of the service, fed with silence
# when idle. This keeps the I2S device open continuously so there are no
# per-request device-open transients (clicks).

_aplay_stdin  = None          # stdin of the persistent aplay process
_audio_lock   = threading.Lock()
_tts_active   = threading.Event()
_tts_abort    = threading.Event()   # set to kill TTS mid-playback (barge-in)
_tts_finished_at = 0.0             # monotonic timestamp of last TTS completion
_active_piper_proc  = None
_active_piper_lock  = threading.Lock()

# AEC reference buffer — fed by _forward_piper_audio, consumed by OWW processing
# Pre-filled with AEC_DELAY_SAMPLES of silence to compensate for aplay buffering latency.
# The deque acts as a FIFO delay line: write at back, read from front.
_aec_ref_buf  = None   # initialised in audio_processor after config is known
_aec_ref_lock = threading.Lock()

try:
    from speexdsp import EchoCanceller as _SpeexEC
    _SPEEX_AVAILABLE = True
except ImportError:
    _SpeexEC = None
    _SPEEX_AVAILABLE = False

_aplay_proc = None            # persistent aplay process — respawned by the feeder if it dies

def _spawn_aplay():
    """(Re)start the persistent aplay process and capture its stdin."""
    global _aplay_proc, _aplay_stdin
    if _aplay_proc and _aplay_proc.poll() is None:
        try:
            _aplay_proc.kill()
        except Exception:
            pass
    _aplay_proc = subprocess.Popen(
        ['aplay', '-D', config.APLAY_DEVICE,
         '-t', 'raw', '-f', 'S16_LE',
         '-r', str(config.PIPER_SAMPLE_RATE), '-c', '1', '-q',
         '--buffer-time=200000'],  # 200ms — headroom for GIL/scheduling jitter
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )
    _aplay_stdin = _aplay_proc.stdin

    # Cap the stdin pipe to limit write-ahead latency while still providing enough
    # buffer that scheduling jitter doesn't cause underruns. 16384 bytes = 256ms at
    # 16kHz 16-bit mono, giving comfortable headroom over the 200ms ring buffer.
    try:
        import fcntl as _fcntl
        _fcntl.fcntl(_aplay_stdin.fileno(), 1031, 16384)  # F_SETPIPE_SZ
    except Exception:
        pass  # non-Linux or permission denied — latency capping unavailable

def _start_persistent_output():
    """Start persistent aplay + silence feeder. Call once after I2S stream opens."""
    _spawn_aplay()

    # Clock-compensated silence feeder: tracks target write times so OS sleep jitter
    # doesn't accumulate into write-ahead latency. 20ms chunks at exactly 1:1 real-time.
    silence_chunk = bytes(int(config.PIPER_SAMPLE_RATE * 0.020) * 2)  # 20ms
    def _silence_feeder():
        interval   = len(silence_chunk) / (config.PIPER_SAMPLE_RATE * 2)  # 20ms
        next_write = time.monotonic()
        while True:
            if not _tts_active.is_set():
                try:
                    # Writability check outside the lock so we never block the
                    # audio forwarder threads while waiting for pipe space.
                    _, writable, _ = select.select([], [_aplay_stdin], [], 0.02)
                    if writable:
                        with _audio_lock:
                            _aplay_stdin.write(silence_chunk)
                            _aplay_stdin.flush()
                except Exception as e:
                    # aplay died or the pipe broke — respawn instead of letting the
                    # feeder thread die (which would leave all future TTS silent).
                    logger.error("Persistent audio output broke (%s) — respawning aplay", e)
                    try:
                        with _audio_lock:
                            _spawn_aplay()
                        logger.info("aplay respawned")
                    except Exception as respawn_err:
                        logger.error("aplay respawn failed: %s", respawn_err)
                        time.sleep(1.0)
                    next_write = time.monotonic()
                    continue
            next_write += interval
            to_sleep = next_write - time.monotonic()
            if to_sleep > 0:
                time.sleep(to_sleep)
            # if to_sleep <= 0 we're behind — write immediately to catch up

    threading.Thread(target=_silence_feeder, daemon=True).start()
    logger.info("Persistent audio output started")

def interrupt_tts():
    """Abort current TTS playback immediately (barge-in). Safe to call at any time."""
    global _active_piper_proc
    _tts_abort.set()
    with _active_piper_lock:
        p = _active_piper_proc
        _active_piper_proc = None
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
    logger.info("TTS interrupted")

# ─── Piper Warm-Standby ───────────────────────────────────────────────────────
# Keep a pool of pre-loaded Piper processes ready so there's no model-load delay.
# After each query the standby pool is replenished in the background.

_warm_pipers = deque()
_warm_piper_lock = threading.Lock()
_MAX_WARM_PIPERS = 1

def _spawn_piper():
    """Spawn a Piper process with the model already loaded, ready to receive text."""
    return subprocess.Popen(
        [config.PIPER_BINARY, '--model', config.PIPER_MODEL, '--output-raw'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, 'ORT_LOGGING_LEVEL': '3'},
        preexec_fn=lambda: os.nice(10)   # Lower CPU priority (Linux only)
    )

def _warmup_piper():
    with _warm_piper_lock:
        if len(_warm_pipers) >= _MAX_WARM_PIPERS:
            return
    try:
        p = _spawn_piper()
        with _warm_piper_lock:
            if len(_warm_pipers) < _MAX_WARM_PIPERS:
                _warm_pipers.append(p)
                logger.info("Piper warm standby ready (pool size: %s)", len(_warm_pipers))
            else:
                p.kill() # Pool is already full
    except Exception as e:
        logger.error("Piper warmup failed: %s", e)

def _get_piper():
    """Return a warm Piper process (or cold-start one if standby isn't ready).
    Immediately kicks off a new warmup so the next query is also fast."""
    p = None
    with _warm_piper_lock:
        while _warm_pipers:
            candidate = _warm_pipers.popleft()
            if candidate.poll() is None:
                p = candidate
                break

    if p is None:
        logger.info("Piper cold start (warm standby pool empty or processes dead)")
        p = _spawn_piper()

    # Replenish the standby pool in the background
    for _ in range(_MAX_WARM_PIPERS):
        threading.Thread(target=_warmup_piper, daemon=True).start()
    return p

# ─── Weather + Context ────────────────────────────────────────────────────────

_WMO_CODES = {
    0:"Clear sky", 1:"Mainly clear", 2:"Partly cloudy", 3:"Overcast",
    45:"Fog", 48:"Icy fog", 51:"Light drizzle", 53:"Drizzle", 55:"Heavy drizzle",
    56:"Freezing drizzle", 57:"Heavy freezing drizzle",
    61:"Light rain", 63:"Rain", 65:"Heavy rain",
    66:"Light freezing rain", 67:"Heavy freezing rain",
    71:"Light snow", 73:"Snow", 75:"Heavy snow", 77:"Snow grains",
    80:"Light rain showers", 81:"Rain showers", 82:"Violent rain showers",
    85:"Light snow showers", 86:"Heavy snow showers",
    95:"Thunderstorm", 96:"Thunderstorm with hail", 99:"Thunderstorm with heavy hail",
}

_weather_cache      = {"summary": None, "fetched_at": 0}
_weather_cache_lock = threading.Lock()

def get_weather():
    """Return a concise weather string. Cached for 10 minutes."""
    with _weather_cache_lock:
        if _weather_cache["summary"] and time.time() - _weather_cache["fetched_at"] < 600:
            return _weather_cache["summary"]
    with _device_settings_lock:
        lat = _device_settings["lat"]
        lon = _device_settings["lon"]
        tz  = _device_settings["tz"]
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude":          lat,
            "longitude":         lon,
            "current":           "temperature_2m,apparent_temperature,precipitation,"
                                 "weather_code,wind_speed_10m,relative_humidity_2m",
            "daily":             "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
            "temperature_unit":  "fahrenheit",
            "wind_speed_unit":   "mph",
            "precipitation_unit":"inch",
            "timezone":          tz,
            "forecast_days":     3,
        }, timeout=5)
        r.raise_for_status()
        d   = r.json()
        cur = d["current"]
        dy  = d["daily"]

        def wmo(c): return _WMO_CODES.get(int(c), f"weather code {c}")

        # Debug log raw codes to identify API vs mapping errors
        logger.debug(f"DEBUG: Weather Raw Data - Current: {cur['weather_code']}, Daily: {dy['weather_code']}")

        lines = [
            f"Current weather: {wmo(cur['weather_code'])}, "
            f"{cur['temperature_2m']:.0f} degrees (feels {cur['apparent_temperature']:.0f}), "
            f"humidity {cur['relative_humidity_2m']} percent, wind {cur['wind_speed_10m']:.0f} miles per hour."
        ]
        for i in range(len(dy["time"])):
            precip_sum = dy['precipitation_sum'][i]
            precip_desc = "precipitation" if "snow" in wmo(dy['weather_code'][i]).lower() else "rain"
            precip_str = f", {precip_sum:.2f} inches of {precip_desc}" if precip_sum > 0 else ""
            
            lines.append(
                f"{dy['time'][i]}: {wmo(dy['weather_code'][i])}, "
                f"high {dy['temperature_2m_max'][i]:.0f}, low {dy['temperature_2m_min'][i]:.0f}{precip_str}."
            )
        summary = " ".join(lines)
        with _weather_cache_lock:
            _weather_cache["summary"]    = summary
            _weather_cache["fetched_at"] = time.time()
        logger.info("Weather updated.")
        return summary
    except Exception as e:
        logger.warning("Weather fetch failed: %s", e)
        return "Weather data unavailable."

def get_context():
    """One-liner injected into every LLM prompt: date/time + sleep state."""
    now = datetime.now()
    with state_lock:
        sleeping = assistant_state.get("is_sleeping", False)
    status = " (DEVICE IS CURRENTLY ASLEEP/DARK)" if sleeping else ""
    return (
        f"Today is {now.strftime('%A, %B %d %Y')}. "
        f"The time is {now.strftime('%I:%M %p')}.{status}"
    )

# ─── Timer Management ─────────────────────────────────────────────────────────

_TIMER_TAG = re.compile(r'\[TIMER:(\d+)(?::([^\]]*))?\]')

_active_timers      = {}
_active_timers_lock = threading.Lock()

_last_assistant_response = ""   # injected as context in CONTINUITY follow-ups

def speak_filler(is_continuity=False):
    """Pick a random filler phrase and speak it asynchronously. Bypassed in continuity."""
    if is_continuity:
        return
        
    if not hasattr(config, "FILLER_PHRASES") or not config.FILLER_PHRASES:
        return
        
    phrase = random.choice(config.FILLER_PHRASES)
    logger.info(f"[FILLER] Selecting phrase: \"{phrase}\"")
    # speak_text blocks until finished, so run in thread
    threading.Thread(target=speak_text, args=(phrase,), daemon=True).start()

def speak_text(text):
    """Speak arbitrary text with full mouth sync (spectrum + APP: SPEAKING timing)."""
    global _active_piper_proc
    try:
        text = _tts_clean(text)
        logger.info(f"[TTS] \"{text}\"")
        
        # Interrupt any active filler phrase or previous speech
        interrupt_tts()
        _tts_abort.clear()   # clear any abort flag left over from a previous interrupt
        
        piper = _get_piper()
        with _active_piper_lock:
            _active_piper_proc = piper
            
        piper.stdin.write(text.encode() + b'\n')
        piper.stdin.close()
        fwd = threading.Thread(target=_forward_piper_audio, args=(piper,), daemon=True)
        fwd.start()
        fwd.join()
    except Exception as e:
        logger.error("speak_text error: %s", e)

def _handle_timer_done(label):
    """Called when ESP32 sends TIMER:DONE — announce completion."""
    with _active_timers_lock:
        _active_timers.pop(label, None)
    logger.info("Timer fired (ESP32): '%s'", label)
    clean = re.sub(r'\s*timer\s*$', '', label, flags=re.IGNORECASE).strip()
    speak_text(f"{clean} timer is done." if clean else "Timer complete.")
    send_uart_command("APP: ASSISTANT")

def set_timer(seconds, label=""):
    """Tell the ESP32 to run the countdown — it sends TIMER:DONE when finished."""
    key = label or f"{seconds}s"
    safe_label = label.replace(":", " ")  # colons are the delimiter
    with _active_timers_lock:
        _active_timers[key] = {"expires_at": time.time() + seconds, "label": label}
    send_uart_command(f"TIMER:START:{seconds}:{safe_label}")
    logger.info(f"Timer set: '{key}' for {seconds}s → ESP32")

# ─── LLM + TTS Pipeline ───────────────────────────────────────────────────────

_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

_GAP_SILENCE = bytes(int(16000 * 0.010) * 2)   # 10ms of silence at 16kHz — fills aplay during inter-sentence gaps

def _forward_piper_audio(piper, wait_event=None):
    """Forward piper stdout → aplay. Signals TTS active state.
    wait_event: if set, blocks before writing the first audio chunk until the
    event fires — used by Piper B to avoid overlapping Piper A's playback."""
    from collections import deque as _deque
    try:
        first         = True
        sent_speaking = False  # True once APP: SPEAKING has been queued for dispatch
        # Delay queue: each entry is (send_at_mono, bins_or_None, intensity, fire_speaking)
        # Spectrum is calculated when audio is written, dispatched APLAY_SYNC_DELAY_MS later
        # so the face moves in sync with the audio actually exiting the speaker.
        spec_queue = _deque()

        def _flush_queue():
            now = time.monotonic()
            while spec_queue and spec_queue[0][0] <= now:
                _, bins, intensity, fire_speaking = spec_queue.popleft()
                if fire_speaking:
                    send_uart_command("APP: SPEAKING")
                if bins is not None:
                    send_uart_command(f"S{','.join(map(str, bins))}|A{intensity}")

        while True:
            ready, _, _ = select.select([piper.stdout], [], [], 0.010)  # 10ms timeout

            _flush_queue()  # drain scheduled dispatches on every tick

            if ready:
                if _tts_abort.is_set():
                    break   # interrupt — discard buffered audio immediately
                chunk = piper.stdout.read(4096)
                if not chunk:
                    break   # piper stdout closed — done
                # Gate B: wait for A to finish before writing any audio.
                # Placed after the read (not before) so Piper B's stdout pipe
                # stays drained during the wait — avoids pipe-buffer stall.
                if wait_event is not None:
                    wait_event.wait()
                    wait_event = None  # only gate the first chunk; clear for subsequent
                if first:
                    _tts_active.set()
                    first = False
                    with state_lock:
                        assistant_state["tts_started_at"] = time.time()
                # Software volume scaling
                with _volume_lock:
                    vol = _volume
                if vol < 99:
                    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    samples *= (vol / 100.0)
                    np.clip(samples, -32768, 32767, out=samples)
                    chunk_out = samples.astype(np.int16).tobytes()
                else:
                    chunk_out = chunk

                # Writability check outside the lock — holding the lock during
                # select() would starve the silence feeder for up to 1s per chunk.
                if _aplay_stdin:
                    _, writable, _ = select.select([], [_aplay_stdin], [], 0.3)
                    if writable:
                        with _audio_lock:
                            _aplay_stdin.write(chunk_out)
                            _aplay_stdin.flush()
                    else:
                        logger.error("Audio output blocked (aplay pipe full). Skipping chunk.")
                # Push reference audio for AEC (use scaled output for correctness)
                ref_samples = np.frombuffer(chunk_out, dtype=np.int16)
                with _aec_ref_lock:
                    if _aec_ref_buf is not None:
                        _aec_ref_buf.extend(ref_samples)

                # ── Digital Mouth Sync ──
                # Calculate spectrum now (for the audio just written), but schedule
                # the UART dispatch for APLAY_SYNC_DELAY_MS from now — that is when
                # this audio will actually exit the speaker.
                send_at = time.monotonic() + config.APLAY_SYNC_DELAY_MS / 1000.0
                fire_speaking = not sent_speaking
                if fire_speaking:
                    sent_speaking = True  # mark queued to prevent duplicate scheduling
                samples_speech = np.frombuffer(chunk_out, dtype=np.int16)
                if len(samples_speech) > 256:
                    norm_speech = samples_speech.astype(np.float64) / 32768.0
                    intensity = int(min(100, np.sqrt(np.mean(norm_speech**2)) * 450.0))
                    chunk_sz = len(samples_speech)
                    idx_bounds_speech = get_fft_bounds(chunk_sz, config.PIPER_SAMPLE_RATE)
                    bins = calculate_spectrum_bins(norm_speech, idx_bounds_speech, gain=2.5)
                    spec_queue.append((send_at, bins, intensity, fire_speaking))
                else:
                    spec_queue.append((send_at, None, 0, fire_speaking))

            elif not first:
                # Inter-sentence gap — keep aplay buffer fed to prevent underrun clicks
                if _aplay_stdin:
                    _, writable, _ = select.select([], [_aplay_stdin], [], 0.1)
                    if writable:
                        with _audio_lock:
                            _aplay_stdin.write(_GAP_SILENCE)
                            _aplay_stdin.flush()
    except Exception:
        logger.exception("TTS audio forwarder error")
    finally:
        # If APP: SPEAKING was queued but never dispatched (e.g. delay > TTS duration),
        # send it now before clearing so the wake word threshold is always reset cleanly.
        for item in spec_queue:
            if item[3]:  # fire_speaking flag
                send_uart_command("APP: SPEAKING")
                break
        global _tts_finished_at
        _tts_finished_at = time.time()
        _tts_active.clear()

_MD_STRIP = re.compile(r'(\*{1,3}|_{1,3}|`+)')
_TRANS_PATTERN = re.compile(r'(?:\[TRANSCRIPT\]|TRANSCRIPT|Transcript)\s*[:\s]*"?(.*?)"?\r?\n', re.IGNORECASE)
_SENTENCE_END  = re.compile(r'(?<!\b[A-Z][a-z])(?<!\b[A-Z])(?<=[.!?])\s+(?=[A-Z]|[0-9])|(?<=[.!?])\n')
_CLAUSE_END    = re.compile(r'(?<=[,;])\s+')  # comma/semicolon clause boundaries

# Fail-safe patterns to strip if model echoes instructions despite system prompt
_LEAK_STRIP = [
    re.compile(r'^.*?ALRIGHT, LET\'S GET THIS OVER WITH\.?\s*', re.IGNORECASE | re.DOTALL),
    re.compile(r'^.*?REMINDER:.*?(?:audio clip\.|answer\.)\s*', re.IGNORECASE | re.DOTALL),
    re.compile(r'^.*?OUTPUT FORMATTING RULES.*?\n', re.IGNORECASE | re.DOTALL),
    re.compile(r'^(?:thought|elaboration|hook|constraints|draft|response|instructions)\b.*?\n', re.IGNORECASE),
    re.compile(r'^\s*-\s+.*?\n', re.IGNORECASE),
    re.compile(r'^\s*now i need to construct.*?\n', re.IGNORECASE),
]


def _tts_clean(text: str) -> str:
    """Strip Markdown and apply pronunciation fixes for Piper."""
    text = _MD_STRIP.sub('', text)
    if hasattr(config, "TTS_PRONUNCIATION_MAP"):
        for word, replacement in config.TTS_PRONUNCIATION_MAP.items():
            text = text.replace(word, replacement)
    return text

def _speak_text_iter(text_iter):
    """
    Consume text chunks from text_iter, feeding complete sentences to Piper
    as they arrive so TTS starts on the first sentence while the LLM is still
    generating the rest. Returns (piper_proc, full_text).
    Respects _tts_abort: stops feeding sentences and returns early if set.
    """
    global _active_piper_proc, _current_transcript
    _tts_abort.clear()   # clear any stale abort flag from a previous barge-in

    # Two-Piper pipeline:
    #   piper_a — plays the first chunk immediately for fast TTS start
    #   piper_b — receives all remaining chunks and runs ONNX inference
    #             concurrently while piper_a's audio is being heard
    piper_a      = None
    piper_b      = None
    fwd_a        = None
    fwd_b        = None
    buf          = ""
    parts        = []
    a_fed        = False  # True once piper_a has its text and stdin is closed
    piper_a_done = threading.Event()  # set after fwd_a finishes; gates fwd_b writes

    def _ensure_piper_a():
        nonlocal piper_a, fwd_a
        if piper_a is None:
            interrupt_tts()
            _tts_abort.clear()
            piper_a = _get_piper()
            with _active_piper_lock:
                _active_piper_proc = piper_a
            fwd_a = threading.Thread(target=_forward_piper_audio, args=(piper_a,), daemon=True)
            fwd_a.start()

    def _flush_to_b(text, final=False):  # final unused, kept for call-site compat
        """Write text to piper_b immediately — one line per sentence.
        B starts ONNX inference as each sentence arrives so its audio is
        buffered in the pipe by the time A finishes playing."""
        nonlocal piper_b, fwd_b
        if not text:
            return
        if piper_b is None:
            piper_b = _get_piper()
            fwd_b = threading.Thread(target=_forward_piper_audio, args=(piper_b, piper_a_done), daemon=True)
            fwd_b.start()
        try:
            piper_b.stdin.write(text.encode() + b'\n')
            piper_b.stdin.flush()
        except Exception:
            pass

    logged_transcript = False

    for text in text_iter:
        if _tts_abort.is_set():
            break
        if not text:
            continue
        parts.append(text)
        buf += text

        # Handle transcript logging/stripping at the very start of the buffer
        if not logged_transcript:
            for leak_patt in _LEAK_STRIP:
                new_buf = leak_patt.sub('', buf)
                if new_buf != buf:
                    buf = new_buf.lstrip()

            match = _TRANS_PATTERN.search(buf)
            if match:
                transcript = match.group(1)
                logger.info("[USER TRANSCRIPT]: %s", transcript)
                with _transcript_lock:
                    _current_transcript = transcript
                buf = buf[match.end():].lstrip()
                logged_transcript = True
            else:
                # If "[TRANSCRIPT]" is not in the buffer (even partially or case-insensitively),
                # and we have accumulated enough text, assume it's missing and proceed.
                if "transcript" not in buf.lower():
                    if len(buf) > 150:
                        logged_transcript = True
                else:
                    if len(buf) > 500:
                        logged_transcript = True

        while logged_transcript:
            m = _SENTENCE_END.search(buf)
            if m:
                sent = buf[:m.start() + 1].strip()
                buf  = buf[m.end():]
            else:
                sent = None
                for cm in _CLAUSE_END.finditer(buf):
                    if cm.start() >= 18:
                        sent = buf[:cm.start()].strip()
                        buf  = buf[cm.end():]
                        break
                if sent is None:
                    break
            if sent and not _tts_abort.is_set():
                logger.info(f"[TTS] \"{sent}\"")
                clean = _tts_clean(sent)
                if not a_fed:
                    # First sentence → Piper A: starts playing immediately
                    _ensure_piper_a()
                    if not _tts_abort.is_set():
                        piper_a.stdin.write(clean.encode() + b'\n')
                        try:
                            piper_a.stdin.close()  # A is single-chunk only
                        except Exception:
                            pass
                        a_fed = True
                else:
                    # Remaining sentences → Piper B: infers while A plays
                    _flush_to_b(clean)

    # ── Final flush ──────────────────────────────────────────────────────────
    if buf.strip() and not _tts_abort.is_set():
        if not logged_transcript and buf.strip():
            buf_for_match = buf + '\n'
            match = _TRANS_PATTERN.search(buf_for_match)
            if match:
                trans = match.group(1)
                with _transcript_lock:
                    _current_transcript = trans
                buf = buf_for_match[match.end():].lstrip()

        clean_buf = _tts_clean(buf.strip()) if buf.strip() else ""
        if not a_fed:
            # Response was short enough to fit in one chunk — use piper_a only
            if clean_buf:
                _ensure_piper_a()
                if not _tts_abort.is_set():
                    piper_a.stdin.write(clean_buf.encode() + b'\n')
                    try:
                        piper_a.stdin.close()
                    except Exception:
                        pass
                    a_fed = True
        else:
            _flush_to_b(clean_buf, final=True)

    # ── Close piper_b stdin so it knows there's no more text coming ─────────
    if piper_b:
        try:
            piper_b.stdin.close()
        except Exception:
            pass

    # ── Drain Piper A ────────────────────────────────────────────────────────
    if not piper_a:
        piper_a_done.set()  # A never ran (abort/single-chunk-B path) — ungate fwd_b immediately
    if piper_a:
        try:
            piper_a.stdin.close()  # no-op if already closed above
        except Exception:
            pass
        if fwd_a:
            fwd_a.join(timeout=30)
            if fwd_a.is_alive():
                logger.warning("Forwarder A timed out! Audio path may be stuck.")
        piper_a_done.set()  # ungate fwd_b — A has finished writing to aplay
        try:
            piper_a.wait(timeout=2)
        except Exception:
            try:
                piper_a.kill()
            except Exception:
                pass
        with _active_piper_lock:
            if _active_piper_proc is piper_a:
                _active_piper_proc = None

    # ── Play Piper B (already computed or streaming) ──────────────────────────
    if piper_b:
        if not _tts_abort.is_set():
            with _active_piper_lock:
                _active_piper_proc = piper_b
            # fwd_b was already started in _flush_to_b; now just wait for it to finish.
            if fwd_b:
                fwd_b.join(timeout=45)
                if fwd_b.is_alive():
                    logger.warning("Forwarder B timed out! Audio path may be stuck.")
            with _active_piper_lock:
                if _active_piper_proc is piper_b:
                    _active_piper_proc = None
        try:
            piper_b.wait(timeout=2)
        except Exception:
            try:
                piper_b.kill()
            except Exception:
                pass

    if not _tts_abort.is_set():
        logger.info("Playback finished.")

    return piper_a or piper_b, ''.join(parts).strip()

_funcs = [
    types.FunctionDeclaration(
        name="set_timer",
        description="Set a countdown timer that alerts the user when complete.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "seconds": types.Schema(type="INTEGER", description="Duration in seconds"),
                "label":   types.Schema(type="STRING",  description="Short name, e.g. 'pasta'"),
            },
            required=["seconds", "label"],
        )
    ),
    types.FunctionDeclaration(
        name="set_sleep_mode",
        description="Put the device to sleep (turn off display and sound) or wake it up.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "enabled": types.Schema(type="BOOLEAN", description="True to go to sleep, False to wake up"),
            },
            required=["enabled"],
        )
    ),
    types.FunctionDeclaration(
        name="send_detailed_email",
        description="Send a detailed follow-up email to the user with the information requested (addresses, times, lists, schedules, etc).",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "subject": types.Schema(type="STRING",  description="The subject line of the email"),
                "body":    types.Schema(type="STRING",  description="The full content of the email in plain text"),
            },
            required=["subject", "body"],
        )
    ),
    types.FunctionDeclaration(
        name="get_weather",
        description="Fetch current weather conditions and a 3-day forecast for the user's location. Use ONLY for weather-specific questions. Do NOT call this for events, local happenings, or general 'what's going on' questions — use google_search grounding for those.",
        parameters=types.Schema(
            type="OBJECT",
            properties={},
            required=[],
        )
    )
]

if getattr(config, 'CAMERA_ENABLED', False):
    _funcs.append(
        types.FunctionDeclaration(
            name="describe_camera_view",
            description="Capture a real-time image from the camera to see what is currently in front of the device. Use this whenever the user asks a question about what is in front of the device, what it is looking at, or asks for description of objects, people, or surroundings in the room.",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
                required=[],
            )
        )
    )

_TIMER_TOOL = types.Tool(function_declarations=_funcs) if LLM_AVAILABLE else None

# Transparent grounding: model retrieves via Google Search internally and returns
# grounded text directly in the stream — no function_call parts exposed to the client.
_SEARCH_TOOL = types.Tool(
    google_search=types.GoogleSearch()
) if LLM_AVAILABLE else None

_ALL_TOOLS = [t for t in [_TIMER_TOOL, _SEARCH_TOOL] if t is not None]


# ─── Email Tool ───────────────────────────────────────────────────────────────
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from email.utils import formatdate, make_msgid

def _send_email_task(subject, body):
    """Send email synchronously. Returns (success: bool, message: str)."""
    sender    = getattr(config, 'EMAIL_SENDER', None)
    user      = getattr(config, 'EMAIL_USERNAME', sender)
    pw        = getattr(config, 'EMAIL_PASSWORD', None)
    recipient = getattr(config, 'EMAIL_RECIPIENT', None)
    server_addr = getattr(config, 'EMAIL_SMTP_SERVER', 'smtp.gmail.com')
    server_port = getattr(config, 'EMAIL_SMTP_PORT', 587)

    if not sender or not pw or not recipient:
        logger.error("Email not sent: Configuration missing in config.py")
        return False, "Email configuration is missing."

    try:
        msg = MIMEMultipart()
        msg['From'] = f"Omnihub Assistant <{sender}>"
        msg['To'] = recipient
        msg['Subject'] = subject
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()

        footer = "\n\n--\nSent by your Omnihub Assistant"
        msg.attach(MIMEText(body + footer, 'plain'))

        server = smtplib.SMTP(server_addr, server_port, timeout=10)
        server.starttls()
        server.login(user, pw)
        server.sendmail(sender, recipient, msg.as_string())
        server.quit()
        logger.info(f"Email sent successfully to {recipient} via {server_addr} (user: {user})")
        return True, "Email sent successfully."
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False, f"Failed: {e}"


# ─── Sleep Mode ───────────────────────────────────────────────────────────────
_prev_volume = min(75, getattr(config, 'VOLUME_MAX', 100))
def _set_sleep_mode(enabled):
    global _volume, _prev_volume
    with state_lock:
        already_sleeping = assistant_state.get("is_sleeping", False)
    
    if enabled:
        if already_sleeping: return
        # Save current volume and mute
        with _volume_lock:
            _prev_volume = _volume
            _volume = 0
        
        send_uart_command("SLEEP:1")
        with state_lock:
            assistant_state["is_sleeping"] = True
        logger.info("Device entering SLEEP mode (Volume %s -> 0)", _prev_volume)
    else:
        if not already_sleeping: return
        # Restore volume
        with _volume_lock:
            _volume = _prev_volume
        
        with state_lock:
            assistant_state["is_sleeping"] = False
        
        send_uart_command("SLEEP:0")
        logger.info("Device WAKING UP from sleep mode (Volume -> %s)", _volume)



def _init_hardware_pins():
    """Initialise GPIO pins (Mute pin, Rotary encoder, etc.) if hardware is present."""
    if not GPIO:
        return
    
    try:
        GPIO.setmode(GPIO.BCM)
        
        # Mute pin — initial state: MUTED (LOW if active HIGH, but SD pin is active HIGH, 
        # so SD=LOW means shutdown/muted).
        if config.PIN_AMP_MUTE is not None:
            GPIO.setup(config.PIN_AMP_MUTE, GPIO.OUT, initial=GPIO.LOW)
            logger.info("Hardware Mute pin %s initialised (LOW/MUTED)", config.PIN_AMP_MUTE)
            
    except Exception as e:
        logger.error("Hardware pin init failed: %s", e)

_init_hardware_pins()

def _amp_enable():
    """Enable the amplifier (SD pin HIGH)."""
    if GPIO and config.PIN_AMP_MUTE is not None:
        try:
            GPIO.output(config.PIN_AMP_MUTE, GPIO.HIGH)
        except Exception as e:
            logger.error("Failed to enable amp: %s", e)

def _amp_mute():
    """Shut down the amplifier (SD pin LOW)."""
    if GPIO and config.PIN_AMP_MUTE is not None:
        try:
            GPIO.output(config.PIN_AMP_MUTE, GPIO.LOW)
        except Exception as e:
            logger.error("Failed to mute amp: %s", e)

def _unmute_and_prime_speaker():
    """No-op when hardware SD pin is configured — amp is enabled only after
    audio data is already flowing (see _forward_audio). Kept for setups without
    a mute pin where ALSA buffer pre-init is still useful."""
    if GPIO and config.PIN_AMP_MUTE is not None:
        logger.info("SD pin active — skipping software prime, amp stays muted until playback")
        return

    # No SD pin: play silence to pre-init the ALSA buffer before first TTS call
    silence = bytes(int(config.PIPER_SAMPLE_RATE * 0.15) * 2)
    try:
        proc = subprocess.Popen(
            ['aplay', '-D', config.APLAY_DEVICE,
             '-t', 'raw', '-f', 'S16_LE',
             '-r', str(config.PIPER_SAMPLE_RATE), '-c', '1', '-q'],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        proc.communicate(input=silence)
        logger.info("Speaker primed (no SD pin)")
    except Exception as e:
        logger.warning("Speaker prime failed (non-critical): %s", e)

# ─── Audio Front-End Helpers ────────────────────────────────────────────────

def _apply_audio_routing():
    """Apply the selected source's ALSA mixer routes via tinymix.

    The Tachyon codec captures nothing until MultiMedia1/PRI_MI2S_TX is routed
    and the ADC/PGA muxes are set. These routes are not persistent across boots,
    so we (re)apply them every time the capture thread starts. No-op for the
    INMP441 profile (empty routing list)."""
    for control, value in config.AUDIO_ROUTING:
        try:
            subprocess.run(["sudo", "tinymix", "set", control, value],
                           check=True, capture_output=True, text=True)
        except FileNotFoundError:
            logger.error("tinymix not found — cannot apply audio routing")
            return
        except subprocess.CalledProcessError as e:
            logger.warning(f"tinymix '{control}'='{value}' failed: {e.stderr.strip()}")
    if config.AUDIO_ROUTING:
        logger.info(f"Applied {len(config.AUDIO_ROUTING)} ALSA route(s) for "
                    f"source '{config.AUDIO_SOURCE}'")


        return x


# ─── Audio Processor ──────────────────────────────────────────────────────────

def on_encoder_event(event, direction, value):
    with state_lock:
        mode = assistant_state.get("current_app", "RADAR")
        is_sleeping = assistant_state.get("is_sleeping", False)
    
    # Wake on interaction
    if is_sleeping and event in ("rotate", "press"):
        logger.info("Wake trigger detected (%s)! Exiting sleep mode.", event)
        _set_sleep_mode(False)
        return

    logger.info(f"Encoder: {event} {direction if direction else ''} (Mode: {mode})")
    
    if event == "rotate":
        m = mode.strip().upper()
        if m == "RADAR":
            # Zoom Logic
            if direction == "CW":
                with state_lock:
                    assistant_state["zoom"] = max(5, assistant_state["zoom"] - 1)
                send_uart_command("Z+")
            else:
                with state_lock:
                    assistant_state["zoom"] = min(250, assistant_state["zoom"] + 1)
                send_uart_command("Z-")
        elif m == "GLOBE":
            # Tell the ESP32 to adjust the globe's tilt
            if direction == "CW": send_uart_command("T+")
            else: send_uart_command("T-")
        elif m in ("ASSISTANT", "SPEAKING", "CONTINUITY"):
            # Volume Adjustment
            if direction == "CW": send_uart_command("V+")
            else: send_uart_command("V-")
        elif m == "SETTINGS":
            # Volume Adjustment in Settings
            if direction == "CW": send_uart_command("V+")
            else: send_uart_command("V-")
        else:
            # Default to Zoom for other screens
            if direction == "CW": send_uart_command("Z+")
            else: send_uart_command("Z-")
            
    elif event == "press":
        m = mode.strip().upper()
        if m == "RADAR":
            # Reset Zoom
            with state_lock:
                assistant_state["zoom"] = 15
            send_uart_command("Z:15")
            logger.info("Encoder Button: Zoom reset to 15nm")
        elif m == "CLOCK":
            # Jump to Assistant
            send_uart_command("APP:ASSISTANT")
        elif m == "SETTINGS":
            # Exit Settings
            send_uart_command("EXIT_SETTINGS")
        elif m == "GLOBE":
            # Toggle Globe Rotation
            send_uart_command("GLOBE:TOGGLE")
        else:
            # Default press action: Back to Radar
            send_uart_command("APP:RADAR")
            
    elif event == "long_press":
        with state_lock:
            is_sleeping = assistant_state.get("is_sleeping", False)
        
        logger.info("Encoder Button: Long Press! Toggling sleep: %s", not is_sleeping)
        _set_sleep_mode(not is_sleeping)

# ─── Hardware Init ────────────────────────────────────────────────────────────

try:
    GPIO.setmode(GPIO.BCM)
    if config.PIN_SFT_GND:
        GPIO.setup(config.PIN_SFT_GND, GPIO.OUT)
        GPIO.output(config.PIN_SFT_GND, GPIO.LOW)
        logger.info("Software GND on GPIO %s", config.PIN_SFT_GND)
    if config.PIN_AMP_MUTE is not None:
        GPIO.setup(config.PIN_AMP_MUTE, GPIO.OUT)
        GPIO.output(config.PIN_AMP_MUTE, GPIO.LOW)   # SD LOW = shutdown, muted at startup
        logger.info("Amp muted on GPIO %s", config.PIN_AMP_MUTE)
    encoder = RotaryEncoder(
        clk_pin=config.PIN_ROTARY_CLK,
        dt_pin=config.PIN_ROTARY_DT,
        sw_pin=config.PIN_ROTARY_SW,
        callback=on_encoder_event
    )
    logger.info(f"Encoder on GPIO {config.PIN_ROTARY_CLK}/{config.PIN_ROTARY_DT}")
except Exception as e:
    logger.error("Hardware init failed: %s", e)

# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.route('/status')
def get_status():
    with state_lock:
        return jsonify(dict(assistant_state))

@app.route('/mic/start')
def mic_start():
    with state_lock:
        assistant_state["mic_active"] = True
    return jsonify({"status": "mic_active"})

@app.route('/mic/stop')
def mic_stop():
    with state_lock:
        assistant_state["mic_active"] = False
    return jsonify({"status": "mic_idle"})

@app.route('/radar/start')
def radar_start():
    with state_lock:
        assistant_state["radar_active"] = True
    logger.info("radar_active set True via HTTP")
    return jsonify({"status": "radar_active"})

@app.route('/radar/stop')
def radar_stop():
    with state_lock:
        assistant_state["radar_active"] = False
    logger.info("radar_active set False via HTTP")
    return jsonify({"status": "radar_idle"})

# ─── Globe Manager Routes ───────────────────────────────────────────────────

@app.route('/globe')
def globe_ui():
    """Serve the Globe Manager web interface."""
    return render_template('globe_ui.html')

@app.route('/api/globe/pois', methods=['GET'])
def get_globe_pois():
    """Return the list of POIs as JSON."""
    return jsonify(globe_manager.load_pois())

@app.route('/api/globe/search')
def globe_search():
    """Geocode a place name via OpenStreetMap Nominatim (proxied so we can set a
    proper User-Agent per their usage policy and avoid browser CORS issues)."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 8},
            headers={"User-Agent": "OmniOrb-GlobeManager/1.0 (personal device)"},
            timeout=6,
        )
        r.raise_for_status()
        results = []
        for item in r.json():
            try:
                results.append({
                    "name":         item.get("name") or item.get("display_name", "").split(",")[0],
                    "display_name": item.get("display_name", ""),
                    "lat":          float(item["lat"]),
                    "lon":          float(item["lon"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
        return jsonify(results)
    except Exception as e:
        logger.warning(f"Geocode search failed for '{q}': {e}")
        return jsonify({"error": "search failed"}), 502

@app.route('/api/globe/pois', methods=['POST'])
def update_globe_pois():
    """Update the POI list and sync to ESP32 if currently in Globe mode."""
    raw = request.get_json(silent=True)
    if not isinstance(raw, list):
        return jsonify({"status": "error", "message": "expected a JSON list of POIs"}), 400

    pois = []
    for p in raw:
        try:
            lat = float(p["lat"])
            lon = float(p["lon"])
        except (TypeError, KeyError, ValueError):
            return jsonify({"status": "error", "message": f"POI missing or invalid lat/lon: {p}"}), 400
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return jsonify({"status": "error", "message": f"POI lat/lon out of range: {p}"}), 400
        pois.append({
            "name":  str(p.get("name", "")),
            "lat":   lat,
            "lon":   lon,
            "color": str(p.get("color", "#FFFFFF")),
        })

    globe_manager.save_pois(pois)
    
    # If the ESP32 is currently showing the globe, push the update immediately
    with state_lock:
        current_app = assistant_state.get("current_app")
    if current_app == "GLOBE":
        send_uart_command(f"GLOBE:POIS:{globe_manager.get_pois_serial()}")
        
    return jsonify({"status": "ok", "count": len(pois)})

# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _load_device_settings()
    
    if hasattr(config, "FILLER_PHRASES") and config.FILLER_PHRASES:
        logger.info(f"Loaded {len(config.FILLER_PHRASES)} filler phrases: {config.FILLER_PHRASES}")

    # The ADS-B proxy is now managed as a standalone systemd service (adsb_sidecar)
    # so we no longer need to launch it as a subprocess here.

    reader_thread = threading.Thread(target=serial_reader, daemon=True)
    reader_thread.start()
    logger.info("Serial reader thread started")

    time.sleep(0.5)
    send_uart_command("SYNC?")
    send_uart_command("WIFI?")   # one-time credential sync (ESP32 no longer broadcasts these)
    _set_sleep_mode(False) # Force Wake on startup
    send_uart_command("APP:ASSISTANT")
    logger.info("Sent boot synchronization command: APP:ASSISTANT")

    audio_thread = threading.Thread(target=audio_processor, daemon=True)
    audio_thread.start()
    logger.info("Audio processor thread started")

    def heartbeat_worker():
        """Proactive heartbeat to keep the ESP32 connection alive."""
        while True:
            try:
                # Any message from the Pi resets the ESP32's 15s timeout.
                # HB:ACK is safe and low-bandwidth.
                send_uart_command("HB:ACK")
            except Exception:
                pass
            time.sleep(5.0)

    heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
    heartbeat_thread.start()
    logger.info("Proactive heartbeat thread started (5s interval)")

    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT)
