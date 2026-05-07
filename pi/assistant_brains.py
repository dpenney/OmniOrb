import io
import os
import random
import re
import subprocess
import threading
import time
import sys
import select
import wave
from collections import deque

import numpy as np
import requests
import serial
import logging
from logging.handlers import RotatingFileHandler

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):
    GPIO = None
from flask import Flask, jsonify
from rotary_encoder import RotaryEncoder
from dotenv import load_dotenv
import config

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

# ─── Device Settings (location + timezone, pushed by ESP32 on boot) ───────────
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
_volume      = 75          # 0-100; set by VOL: messages from ESP32
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
            logger.info(f"Loaded device settings: {_device_settings}")
    except Exception as e:
        logger.warning(f"Could not load device_settings.json: {e}")

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
        logger.info(f"Device settings saved: {data}")
    except Exception as e:
        logger.error(f"Could not save device_settings.json: {e}")

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
            }
        }
        if "GOOGLE_API_KEY" not in os.environ:
            os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY", "")
        _memory = Memory.from_config(_mem_config)
        logger.info("Mem0 long-term memory initialized with Local Chroma store.")
    except Exception as e:
        logger.error(f"Failed to initialize Mem0: {e}")
        _memory = None

threading.Thread(target=_init_mem0, daemon=True).start()

def _seed_private_memories():
    """Load facts from private_memories.json and push them into Mem0 if not already present."""
    if not _memory:
        return
    
    private_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "private_memories.json")
    if not os.path.exists(private_file):
        logger.info("No private_memories.json found. Skipping memory seeding.")
        return

    try:
        import json as _json
        with open(private_file) as f:
            memories = _json.load(f)
        
        # Search for any items to prevent duplicates
        existing_raw = _memory.get_all(user_id="primary_user")
        # Handle dict wrapper from some versions of mem0
        existing = existing_raw.get("results", []) if isinstance(existing_raw, dict) else existing_raw
        
        # Handle cases where memory items might be objects or dicts
        def get_text(m):
            if isinstance(m, dict): return m.get("text", m.get("memory", ""))
            return getattr(m, "text", getattr(m, "memory", ""))
        
        for mem in memories:
            fact = mem.get("content", "")
            if fact and not any(get_text(m) == fact for m in existing):
                _memory.add(fact, user_id="primary_user")
                logger.info(f"Seeded private memory: {fact[:50]}...")
    except Exception as e:
        logger.error(f"Error seeding private memories: {e}")

# Deprecated: Seeding slowed down boot; now handled by context caching in MemoryManager
# _seed_private_memories()

# ─── Memory Manager & Context ────────────────────────────────────────────────
def init_memory_manager():
    global memory_manager
    try:
        if LLM_AVAILABLE:
            from memory_manager import MemoryManager
            memory_manager = MemoryManager(client)
            logger.info("MemoryManager initialized in background.")
    except Exception as e:
        logger.error(f"Failed to initialize MemoryManager: {e}")

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
            logger.info(f"Connected to Serial Port: {port}")
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
                    logger.info(f"Serial reconnected on {port}")
                    return
            except Exception as e:
                logger.error(f"Reconnect failed on {port}: {e}")
        ser = None
        logger.error("Serial reconnect failed — no valid port found")

def send_uart_command(cmd):
    try:
        with ser_lock:
            if ser and ser.is_open:
                # Sleep Muzzle: Don't send anything to the ESP32 while sleeping EXCEPT the sleep/wake commands.
                with state_lock:
                    sleeping = assistant_state.get("is_sleeping", False)
                if sleeping and not (cmd.startswith("SLEEP:") or cmd.startswith("WAKE|") or cmd.startswith("EMO:")):
                    # Silencing the firehose: Don't log dropped spectrum/mouth data
                    return

                ser.write(f"{cmd}\n".encode())
                global _last_uart_send_at
                _last_uart_send_at = time.time()
                if not (cmd.startswith("S") and "," in cmd) and not (cmd.startswith("A") and cmd[1:2].isdigit()) and not cmd.startswith("DIAG:"):
                    logger.info(f"Sent UART: {cmd}")
    except Exception as e:
        logger.error(f"UART send error: {e}")
        reconnect_serial()
def _apply_wifi(ssid, pwd):
    try:
        current_ssid = subprocess.check_output(
            "nmcli -t -f active,ssid dev wifi | grep '^yes' | cut -d: -f2", 
            shell=True, text=True
        ).strip()
        if current_ssid != ssid:
            logger.info(f"Applying new Wi-Fi credentials for SSID: {ssid}")
            subprocess.run(['sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid, 'password', pwd])
    except Exception as e:
        logger.error(f"Failed to apply Wi-Fi: {e}")

def serial_reader():
    while True:
        try:
            if ser and ser.is_open:
                while ser.in_waiting > 0:
                    with ser_read_lock:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        break
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
                                logger.warning(f"Bad GEO message: {line}")
                    elif line == "INTERRUPT":
                        logger.info("[ESP32] Tap interrupt received")
                        interrupt_tts()
                        send_uart_command("APP: ASSISTANT")
                    elif line == "DIAG?":
                        # Send diagnostic payload
                        with state_lock:
                            mic_val = assistant_state.get("audio_intensity", 0)
                            is_proc = assistant_state.get("processing", False)
                        
                        # Get last wake time string
                        from datetime import datetime
                        with state_lock:
                            last_w_at = assistant_state.get("last_wakeword_at", 0)
                        last_w = datetime.fromtimestamp(last_w_at).strftime('%I:%M:%S %p') if last_w_at > 0 else "NEVER"
                        
                        # Check OWW status
                        with state_lock:
                            is_ready = assistant_state.get("oww_ready", False)
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
                        if now - assistant_state.get("last_diag_at", 0) >= 1.0:
                            diag_msg = f"DIAG:mic={mic_val},ww={ww_stat},pi=OK,rssi={rssi},wake={last_w}"
                            send_uart_command(diag_msg)
                            assistant_state["last_diag_at"] = now
                    elif line.startswith("VOL:"):
                        try:
                            global _volume
                            val = int(line[4:])
                            with _volume_lock:
                                _volume = max(0, min(100, val))
                            logger.info(f"[ESP32] Volume → {_volume}")
                        except ValueError:
                            logger.warning(f"Bad VOL message: {line}")
                    elif line.startswith("TIMER:DONE:"):
                        label = line[len("TIMER:DONE:"):]
                        logger.info(f"[ESP32] Timer done: '{label}'")
                        threading.Thread(target=_handle_timer_done, args=(label,), daemon=True).start()
                    elif line.startswith("WIFI:"):
                        payload = line[5:]
                        if '|' in payload:
                            ssid, pwd = payload.split('|', 1)
                            threading.Thread(target=_apply_wifi, args=(ssid, pwd), daemon=True).start()
                    elif "APP:" in line:
                        app_mode = line.split("APP:", 1)[1].split()[0]
                        with state_lock:
                            assistant_state["current_app"] = app_mode
                            assistant_state["mic_active"] = (app_mode == "ASSISTANT" or app_mode == "SPEAKING" or app_mode == "CONTINUITY")
                            assistant_state["radar_active"] = (app_mode == "RADAR")
                        logger.info(f"[ESP32] {line} → app={app_mode}, mic_active={assistant_state['mic_active']}")
                    elif line == "HB:OK":
                        send_uart_command("HB:ACK")
                    elif line == "HB:ACK":
                        # ESP32 acknowledged our heartbeat
                        pass
                    
                    # Log ALL traffic to the raw UART log
                    uart_logger.debug(line)

                    if line not in ("HB:OK", "DIAG?") and not line.startswith("DIAG:"):
                        logger.info(f"[ESP32] {line}")
        except Exception as e:
            logger.error(f"Serial read error: {e}")
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

def _start_persistent_output():
    """Start persistent aplay + silence feeder. Call once after I2S stream opens."""
    global _aplay_stdin
    aplay = subprocess.Popen(
        ['aplay', '-D', config.APLAY_DEVICE,
         '-t', 'raw', '-f', 'S16_LE',
         '-r', str(config.PIPER_SAMPLE_RATE), '-c', '1', '-q',
         '--buffer-time=100000'],  # 100ms — tight sync between digital mouth and audio
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )
    _aplay_stdin = aplay.stdin

    # Cap the stdin pipe at the Linux minimum (4096 bytes = 128ms at 16kHz 16-bit).
    # Without this, even a slightly-fast feeder re-fills the default 64KB pipe
    # within seconds, giving ~1s of write-ahead latency before TTS audio is heard.
    try:
        import fcntl as _fcntl
        _fcntl.fcntl(_aplay_stdin.fileno(), 1031, 4096)  # F_SETPIPE_SZ
    except Exception:
        pass  # non-Linux or permission denied — latency capping unavailable

    # Clock-compensated silence feeder: tracks target write times so OS sleep jitter
    # doesn't accumulate into write-ahead latency. 20ms chunks at exactly 1:1 real-time.
    silence_chunk = bytes(int(config.PIPER_SAMPLE_RATE * 0.020) * 2)  # 20ms
    def _silence_feeder():
        interval   = len(silence_chunk) / (config.PIPER_SAMPLE_RATE * 2)  # 20ms
        next_write = time.monotonic()
        while True:
            if not _tts_active.is_set():
                try:
                    with _audio_lock:
                        _aplay_stdin.write(silence_chunk)
                        _aplay_stdin.flush()
                except Exception:
                    break
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
_MAX_WARM_PIPERS = 3

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
                logger.info(f"Piper warm standby ready (pool size: {len(_warm_pipers)})")
            else:
                p.kill() # Pool is already full
    except Exception as e:
        logger.error(f"Piper warmup failed: {e}")

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
        logger.warning(f"Weather fetch failed: {e}")
        return "Weather data unavailable."

def get_context():
    """One-liner injected into every LLM prompt: date/time + sleep state."""
    from datetime import datetime
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
        logger.error(f"speak_text error: {e}")

def _handle_timer_done(label):
    """Called when ESP32 sends TIMER:DONE — announce completion."""
    with _active_timers_lock:
        _active_timers.pop(label, None)
    logger.info(f"Timer fired (ESP32): '{label}'")
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

def _forward_piper_audio(piper):
    """Forward piper stdout → aplay. Signals TTS active state.
    Uses select() so inter-sentence gaps are filled with silence rather than
    letting the aplay buffer drain (which causes audible clicks)."""
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

                # Write audio immediately — no sleep, never block the audio path.
                # Write audio — use select to avoid blocking indefinitely if aplay hangs
                with _audio_lock:
                    if _aplay_stdin:
                        _, writable, _ = select.select([], [_aplay_stdin], [], 1.0)
                        if writable:
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
                # Inter-sentence gap — keep aplay buffer fed to prevent underrun clicks
                with _audio_lock:
                    if _aplay_stdin:
                        _, writable, _ = select.select([], [_aplay_stdin], [], 0.1)
                        if writable:
                            _aplay_stdin.write(_GAP_SILENCE)
                            _aplay_stdin.flush()
    except Exception:
        pass
    finally:
        # If APP: SPEAKING was queued but never dispatched (e.g. delay > TTS duration),
        # send it now before clearing so the wake word threshold is always reset cleanly.
        for item in spec_queue:
            if item[3]:  # fire_speaking flag
                send_uart_command("APP: SPEAKING")
                break
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
    piper_a   = None
    piper_b   = None
    fwd_a     = None
    fwd_b     = None
    buf       = ""
    parts     = []
    a_fed     = False  # True once piper_a has its text and stdin is closed

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
            # Start forwarding audio from B immediately so it can buffer/play 
            # while we are still feeding it sentences via stdin.
            fwd_b = threading.Thread(target=_forward_piper_audio, args=(piper_b,), daemon=True)
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
                logger.info(f"[USER TRANSCRIPT]: {transcript}")
                with _transcript_lock:
                    _current_transcript = transcript
                buf = buf[match.end():].lstrip()
                logged_transcript = True
            elif "]" in buf or ":" in buf or len(buf) > 300:
                if not re.search(r'^\s*\[TRANSCRIPT\]', buf, re.IGNORECASE):
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
    if piper_a:
        try:
            piper_a.stdin.close()  # no-op if already closed above
        except Exception:
            pass
        if fwd_a:
            fwd_a.join(timeout=30)
            if fwd_a.is_alive():
                logger.warning("Forwarder A timed out! Audio path may be stuck.")
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

_TIMER_TOOL = types.Tool(function_declarations=[
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
]) if LLM_AVAILABLE else None

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

def _send_email_async(subject, body):
    threading.Thread(target=_send_email_task, args=(subject, body), daemon=True).start()

from email.utils import formatdate, make_msgid

def _send_email_task(subject, body):
    sender    = getattr(config, 'EMAIL_SENDER', None)
    user      = getattr(config, 'EMAIL_USERNAME', sender)
    pw        = getattr(config, 'EMAIL_PASSWORD', None)
    recipient = getattr(config, 'EMAIL_RECIPIENT', None)
    server_addr = getattr(config, 'EMAIL_SMTP_SERVER', 'smtp.gmail.com')
    server_port = getattr(config, 'EMAIL_SMTP_PORT', 587)
    
    if not sender or not pw or not recipient:
        logger.error("Email not sent: Configuration missing in config.py")
        return

    try:
        msg = MIMEMultipart()
        msg['From'] = f"Omnihub Assistant <{sender}>"
        msg['To'] = recipient
        msg['Subject'] = subject
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()
        
        footer = "\n\n--\nSent by your Omnihub Assistant"
        msg.attach(MIMEText(body + footer, 'plain'))

        server = smtplib.SMTP(server_addr, server_port)
        server.starttls()
        server.login(user, pw)
        server.sendmail(sender, recipient, msg.as_string())
        server.quit()
        logger.info(f"Email sent successfully to {recipient} via {server_addr} (user: {user})")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        speak_text("Sorry, I was unable to send that email.")


# ─── Sleep Mode ───────────────────────────────────────────────────────────────
_prev_volume = 75
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
        logger.info(f"Device entering SLEEP mode (Volume {_prev_volume} -> 0)")
    else:
        if not already_sleeping: return
        # Restore volume
        with _volume_lock:
            _volume = _prev_volume
        
        with state_lock:
            assistant_state["is_sleeping"] = False
        
        send_uart_command("SLEEP:0")
        logger.info(f"Device WAKING UP from sleep mode (Volume -> {_volume})")



def process_llm(audio_array):
    global _current_transcript, _last_assistant_response
    """
    LLM pipeline with streaming and Gemini function calling for timers:
      1. Normalize + encode audio
      2. Streaming first call — text fed sentence-by-sentence to Piper (TTS starts
         on first sentence while LLM is still generating), function calls collected
         as a side effect
      3. If timer tool called: streaming follow-up call for spoken confirmation
      4. set_timer() sent to ESP32 only after confirmation TTS finishes
    """
    if not LLM_AVAILABLE:
        logger.error("LLM libraries not installed!")
        send_uart_command("TXT|AI missing")
        send_uart_command("APP: ASSISTANT")
        return

    try:
        peak_amplitude = float(np.max(np.abs(audio_array)))
        if peak_amplitude < config.LLM_MIN_PEAK:
            logger.info(f"Recording discarded — silence (peak={peak_amplitude:.5f} < {config.LLM_MIN_PEAK})")
            # Return the ESP32 to IDLE/Radar screen and reset state
            with state_lock:
                assistant_state["status"] = "IDLE"
            send_uart_command("APP:ASSISTANT")
            return

        # Valid audio detected — now start the "thinking" UX
        with state_lock:
            prev_status = assistant_state.get("status")
            assistant_state["processing"] = True
            assistant_state["status"]     = "THINKING"
        
        send_uart_command("APP:THINKING")
        speak_filler(is_continuity=(prev_status == "CONTINUITY"))

        logger.info(f"LLM query: {len(audio_array)} samples, peak={peak_amplitude:.5f}")

        # Normalize audio
        peak = np.max(np.abs(audio_array))
        if peak > 0.001:
            audio_array = (audio_array / peak) * 0.95

        # Downsample 48kHz → 16kHz (take every 3rd sample) before encoding.
        # Speech is bandlimited to ~8kHz so no perceptible loss; payload is 3× smaller
        # which meaningfully reduces Gemini processing time.
        audio_ds = audio_array[::3]
        wav_rate = config.AUDIO_RATE // 3   # 16000

        # Encode to 16-bit PCM WAV in memory
        audio_int16 = (audio_ds * 32767).astype(np.int16)
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(wav_rate)
            wf.writeframes(audio_int16.tobytes())
        wav_bytes = wav_buf.getvalue()

        with _transcript_lock:
            _current_transcript = "" # Clear for new turn
        interaction_transcript = ""
        
        user_msg = f"[Current Context: {get_context()}]"
        if prev_status == "CONTINUITY" and _last_assistant_response:
            user_msg += f"\n[Your previous response was: \"{_last_assistant_response}\". The user is now following up.]"


        # Log exactly what is being sent to the AI
        logger.info(f"LLM SYSTEM PROMPT: {config.LLM_SYSTEM_PROMPT}")
        logger.info(f"LLM USER MESSAGE: {user_msg}")

        audio_part = types.Part.from_bytes(data=wav_bytes, mime_type='audio/wav')
        
        # Use Cached Content (Tier 1) if available, else use full instruction (Threshold-Aware)
        cache_id = memory_manager.cache_id if memory_manager else None
        full_instr = memory_manager.full_system_instruction if memory_manager else config.LLM_SYSTEM_PROMPT
        
        _tool_cfg = types.ToolConfig(include_server_side_tool_invocations=True)
        if cache_id:
            logger.info(f"Using Context Cache: {cache_id}")
            gen_cfg = types.GenerateContentConfig(
                cached_content=cache_id,
                tools=_ALL_TOOLS,
                tool_config=_tool_cfg,
            )
        else:
            logger.info("Using Full System Instruction (No Cache)")
            gen_cfg = types.GenerateContentConfig(
                system_instruction=full_instr,
                tools=_ALL_TOOLS,
                tool_config=_tool_cfg,
            )

        # ── First streaming call: speak response, collect any function calls ───
        pending_timers    = []
        fn_responses      = []
        model_parts       = []
        has_server_call   = [False]  # set True when google_search fires in stream1

        stream1 = client.models.generate_content_stream(
            model=config.LLM_MODEL,
            contents=[audio_part, user_msg],
            config=gen_cfg,
        )

        def _first_iter():
            for chunk in stream1:
                if not chunk.candidates:
                    continue
                candidate = chunk.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    continue
                for part in candidate.content.parts:
                    model_parts.append(part)
                    if hasattr(part, 'function_call') and part.function_call:
                        fc = part.function_call
                        if fc.name == "set_sleep_mode":
                            enabled = fc.args["enabled"]
                            logger.info(f"Executing Tool: set_sleep_mode - {enabled}")
                            _set_sleep_mode(enabled)
                            fn_responses.append(types.Part(function_response=types.FunctionResponse(
                                name="set_sleep_mode",
                                response={"status": "success", "is_sleeping": enabled},
                                id=fc.id
                            )))
                        elif fc.name == "set_timer":
                            secs = int(fc.args["seconds"])
                            label = str(fc.args.get("label", ""))
                            pending_timers.append((secs, label))
                            fn_responses.append(types.Part(function_response=types.FunctionResponse(
                                name="set_timer", 
                                response={"status": "success", "message": f"Timer for {secs}s started."},
                                id=fc.id
                            )))
                        elif fc.name == "send_detailed_email":
                            subject = fc.args["subject"]
                            body = fc.args["body"]
                            logger.info(f"Executing Tool: send_detailed_email - {subject}")
                            _send_email_async(subject, body)
                            fn_responses.append(types.Part(function_response=types.FunctionResponse(
                                name="send_detailed_email",
                                response={"status": "success", "message": "Email sent."},
                                id=fc.id
                            )))
                        elif fc.name == "get_weather":
                            logger.info("Executing Tool: get_weather")
                            w_data = get_weather()
                            logger.info(f"Tool Result: {w_data}")
                            fn_responses.append(types.Part(function_response=types.FunctionResponse(
                                name="get_weather",
                                response={"status": "success", "data": w_data},
                                id=fc.id
                            )))
                        else:
                            # Built-in server-side tool (google_search).
                            # The API executes it internally; we get the grounded answer
                            # by replaying the conversation with model_parts in stream2.
                            logger.info(f"Server-side tool: {fc.name}")
                            has_server_call[0] = True

                    if getattr(part, 'text', None):
                        yield part.text

        with state_lock:
            assistant_state["status"] = "SPEAKING"
        send_uart_command("APP:SPEAKING")
        _, first_text = _speak_text_iter(_first_iter())
        
        # Capture the transcript gathered by _speak_text_iter during the first stream
        with _transcript_lock:
            interaction_transcript = _current_transcript

        if _tts_abort.is_set():
            return  # user interrupted — skip follow-up call and timer setup

        # ── If tool calls happened: follow-up streaming call for confirmation/reporting ──
        if fn_responses:
            logger.info(f"Triggering tool follow-up (Pass 2) with {len(fn_responses)} response(s).")
            stream2 = client.models.generate_content_stream(
                model=config.LLM_MODEL,
                contents=[
                    audio_part, user_msg,
                    types.Content(role="model", parts=model_parts),
                    types.Content(role="user", parts=fn_responses),
                ],
                config=types.GenerateContentConfig(
                    cached_content=cache_id
                ) if cache_id else types.GenerateContentConfig(
                    system_instruction=full_instr
                ),
            )

            def _follow_iter():
                for chunk in stream2:
                    if not chunk.candidates:
                        continue
                    candidate = chunk.candidates[0]
                    if not candidate.content or not candidate.content.parts:
                        continue
                    for part in candidate.content.parts:
                        if getattr(part, 'text', None):
                            yield part.text

            _, full_text = _speak_text_iter(_follow_iter())

        elif has_server_call[0]:
            # google_search fired — replay conversation so Gemini generates the grounded answer.
            logger.info("google_search fired — follow-up stream for grounded response.")
            stream2 = client.models.generate_content_stream(
                model=config.LLM_MODEL,
                contents=[
                    audio_part, user_msg,
                    types.Content(role="model", parts=model_parts),
                ],
                config=gen_cfg,
            )

            def _search_follow_iter():
                for chunk in stream2:
                    if not chunk.candidates:
                        continue
                    candidate = chunk.candidates[0]
                    if not candidate.content or not candidate.content.parts:
                        continue
                    for part in candidate.content.parts:
                        if getattr(part, 'text', None):
                            yield part.text

            _, full_text = _speak_text_iter(_search_follow_iter())
            if not full_text:
                logger.warning("google_search follow-up returned no text.")
                full_text = first_text

        else:
            full_text = first_text

        # Start timers after confirmation has been spoken, before slow background tasks
        for secs, label in pending_timers:
            set_timer(secs, label)

        display_text = ""
        if full_text:
            # Strip the [TRANSCRIPT] tag and any internal conversation headers from the UI text
            display_text = _TRANS_PATTERN.sub('', full_text).strip()
            _last_assistant_response = display_text

            logger.info(f"LLM answer: {display_text}")
            send_uart_command(f"TXT|{display_text.replace(chr(10), ' ')}")
            
            # ── 3. Storage: Commit to Memory (Background) ──────────────────────────
            if _memory:
                with _transcript_lock:
                    final_trans = _current_transcript
                
                if final_trans:
                    def _store_mem_bg(txt):
                        try:
                            _memory.add(txt, user_id="primary_user")
                            logger.info("Turn committed to long-term memory (background).")
                        except Exception as e:
                            logger.warning(f"Memory storage failed: {e}")
                    
                    threading.Thread(target=_store_mem_bg, args=(final_trans,), daemon=True).start()

        # ── 5. Continuity: Start follow-up window ────────────────────────
        # Transition to CONTINUITY or IDLE regardless of whether text was spoken
        with state_lock:
            is_slp = assistant_state.get("is_sleeping", False)
            
        if is_slp:
            # Sleep Mode: Force IDLE and mute volume now that speech is done
            with state_lock:
                assistant_state["status"] = "IDLE"
            # Mute volume now
            global _volume
            with _volume_lock:
                _volume = 0
            logger.info("Sleep Mode active: Muting volume and bypassing continuity.")
            # No UART command here; send_uart_command will handle Sleep Muzzle
        elif not is_exit_command(interaction_transcript) and not is_exit_command(display_text):
            with state_lock:
                assistant_state["status"] = "CONTINUITY"
                assistant_state["continuity_until"] = time.time() + config.CONTINUITY_TIMEOUT
            send_uart_command("APP:CONTINUITY")
            logger.info(f"Transitioned to CONTINUITY state ({config.CONTINUITY_TIMEOUT}s window).")
        else:
            with state_lock:
                assistant_state["status"] = "IDLE"
            send_uart_command("APP:ASSISTANT")
            logger.info("Exit command detected or no follow-up needed. Returning to IDLE.")


    except Exception as e:
        logger.error(f"LLM pipeline error: {e}")
        send_uart_command("TXT|Sorry, I had an error.")
        speak_text("Sorry, I had an error.")
        with state_lock:
            assistant_state["status"] = "IDLE"
        send_uart_command("APP: ASSISTANT")
    finally:
        _tts_active.clear()  # Ensure silence feeder resumes on any exit path
        with state_lock:
            assistant_state["processing"] = False
            # Post-LLM cooldown so OWW doesn't re-trigger on speaker echo
            assistant_state["wakeword_cooldown_until"] = time.time() + config.WAKEWORD_POST_LLM_COOLDOWN

# ─── Speaker Init ─────────────────────────────────────────────────────────────

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
            logger.info(f"Hardware Mute pin {config.PIN_AMP_MUTE} initialised (LOW/MUTED)")
            
    except Exception as e:
        logger.error(f"Hardware pin init failed: {e}")

_init_hardware_pins()

def _amp_enable():
    """Enable the amplifier (SD pin HIGH)."""
    if GPIO and config.PIN_AMP_MUTE is not None:
        try:
            GPIO.output(config.PIN_AMP_MUTE, GPIO.HIGH)
        except Exception as e:
            logger.error(f"Failed to enable amp: {e}")

def _amp_mute():
    """Shut down the amplifier (SD pin LOW)."""
    if GPIO and config.PIN_AMP_MUTE is not None:
        try:
            GPIO.output(config.PIN_AMP_MUTE, GPIO.LOW)
        except Exception as e:
            logger.error(f"Failed to mute amp: {e}")

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
        logger.warning(f"Speaker prime failed (non-critical): {e}")

# ─── Audio Processor ──────────────────────────────────────────────────────────

def audio_processor():
    """Background thread: capture mic audio, drive wake word + VAD + spectrum."""
    if not pyaudio:
        logger.error("pyaudio not installed. Audio disabled.")
        return

    CHUNK    = config.AUDIO_CHUNK
    FORMAT   = pyaudio.paInt32
    CHANNELS = config.AUDIO_CHANNELS
    RATE     = config.AUDIO_RATE

    p = pyaudio.PyAudio()

    # ── Load OpenWakeWord (before opening I2S) ──
    # Opening the I2S stream starts the DMA engine. If the output buffer runs dry
    # while the CPU is hammered loading the ONNX model, you get audible clicks even
    # with the amp muted. Load all models first, then open the stream.
    oww_model = None
    if OWW_AVAILABLE and hasattr(config, "WAKEWORD_MODEL"):
        try:
            model_target = config.WAKEWORD_MODEL
            wakeword_models = []
            if not model_target.endswith(".onnx"):
                for path in openwakeword.get_pretrained_model_paths():
                    if model_target in path:
                        wakeword_models.append(path)
                        break
            else:
                wakeword_models = [model_target]
            if not wakeword_models:
                logger.error(f"OWW model not found: {model_target}")
            else:
                oww_model = Model(wakeword_model_paths=wakeword_models)
                with state_lock:
                    assistant_state["oww_ready"] = True
                logger.info(f"OWW model loaded: {wakeword_models[0]}")
                # Piper warmup deferred until here so OWW had full CPU during load
                for _ in range(_MAX_WARM_PIPERS):
                    threading.Thread(target=_warmup_piper, daemon=True).start()
        except Exception as e:
            logger.error(f"OWW load failed: {e}")

    # ── Load VAD ──
    vad = None
    if VAD_AVAILABLE:
        try:
            vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
            logger.info(f"VAD ready (aggressiveness={config.VAD_AGGRESSIVENESS})")
        except Exception as e:
            logger.error(f"VAD init failed: {e}")

    # ── Open I2S stream now that all CPU-intensive loading is done ──
    try:
        stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                        input=True, input_device_index=config.AUDIO_DEVICE_INDEX,
                        frames_per_buffer=CHUNK)
        logger.info("Audio stream opened (32-bit I2S)")
    except Exception as e:
        logger.error(f"Failed to open audio stream: {e}")
        return

    _amp_enable()
    _start_persistent_output()
    logger.info("=" * 60)
    logger.info("  ASSISTANT READY — listening for wake word")
    logger.info("=" * 60)

    # ── AEC init ──────────────────────────────────────────────────────────────
    global _aec_ref_buf
    
    # VAD frame: 30ms at 16kHz = 480 samples
    VAD_FRAME_SAMPLES = 480

    # ── Pre-compute Mic FFT bounds ──
    mic_idx_bounds = get_fft_bounds(CHUNK, RATE)

    # ── Recording state ──
    recording            = False
    recording_end_time   = 0
    query_buffer         = []
    vad_speech_frames    = 0
    vad_silence_frames   = 0
    vad_frame_buf        = np.array([], dtype=np.int16)

    aec = None
    if config.AEC_ENABLED and _SPEEX_AVAILABLE:
        try:
            aec = _SpeexEC.create(config.AEC_FRAME, config.AEC_FILTER_LENGTH, 16000)
            # Pre-fill with silence to represent aplay's write-to-playback latency
            _aec_ref_buf = deque([0] * config.AEC_DELAY_SAMPLES, maxlen=32000)
            logger.info(
                f"AEC ready (SpeexDSP, frame={config.AEC_FRAME}, "
                f"filter={config.AEC_FILTER_LENGTH}, delay={config.AEC_DELAY_SAMPLES})"
            )
        except Exception as e:
            logger.warning(f"AEC init failed: {e}")
    else:
        if not _SPEEX_AVAILABLE:
            logger.info("AEC disabled — speexdsp not installed (pip install speexdsp)")

    # VAD frame: 30ms at 16kHz = 480 samples
    VAD_FRAME_SAMPLES = 480

    # ── Recording state ──
    recording            = False
    recording_end_time   = 0
    query_buffer         = []
    vad_speech_frames    = 0
    vad_silence_frames   = 0
    vad_frame_buf        = np.array([], dtype=np.int16)

    # ── Pre-compute FFT constants (invariant for this sample rate / chunk size) ──
    _fft_freq_bounds = np.geomspace(80, 12000, 17)
    _fft_idx_bounds  = np.clip((_fft_freq_bounds * CHUNK / RATE).astype(int), 0, CHUNK // 2)
    _fft_weights     = np.ones(16)
    # Per-bin noise floor calibrated from measured silence (silence p95 * 1.3)
    _fft_floor       = np.array([0.673, 0.3535, 0.2799, 0.1647, 0.1024, 0.059, 0.0352, 0.0254,
                                  0.0179, 0.0129, 0.0094, 0.0072, 0.0055, 0.0043, 0.0035, 0.0028])

    # ── Other state ──
    last_send_time     = 0
    oww_buffer         = np.array([], dtype=np.int16)
    last_log_time      = time.time()
    pre_record_buffer  = deque()  # deque of numpy chunks, total ≤ 71680 samples
    pre_roll_len       = 0
    continuity_speech_frames = 0
    continuity_silence_frames = 0

    while True:
        try:
            with state_lock:
                mic_active = assistant_state.get("mic_active", False)

            data = stream.read(CHUNK, exception_on_overflow=False)
            raw_samples = np.frombuffer(data, dtype=np.int32)
            ch_left  = raw_samples[0::2]
            ch_right = raw_samples[1::2]

            # Auto-detect which channel the INMP441 is on (L/R pin to GND = Left, to 3.3V = Right)
            rms_left = np.mean(np.abs(ch_left))
            rms_right = np.mean(np.abs(ch_right))
            active_ch = ch_left if rms_left > rms_right else ch_right

            # Restore 2x digital boost for optimal pickup
            norm_samples = (active_ch.astype(np.float64) / 2147483648.0) * 2.0
            norm_samples = np.clip(norm_samples, -1.0, 1.0)

            # Maintain ~0.5s pre-roll buffer — captures tail of wake word and any
            # speech that overlaps OWW detection latency. 1.5s was wasteful since
            # "Hey Robot" is ~0.5s; the excess was just room noise sent to Gemini.
            if not recording:
                pre_record_buffer.append(norm_samples)
                pre_roll_len += len(norm_samples)
                while pre_roll_len > 24000:
                    pre_roll_len -= len(pre_record_buffer.popleft())

            # ── Recording + VAD ──
            if recording:
                query_buffer.extend(norm_samples)

                if vad:
                    # Accumulate downsampled int16 for VAD (48k→16k, 32bit→16bit)
                    chunk_16k = np.right_shift(active_ch[::3], 16).astype(np.int16)
                    vad_frame_buf = np.concatenate([vad_frame_buf, chunk_16k])

                    while len(vad_frame_buf) >= VAD_FRAME_SAMPLES:
                        frame = vad_frame_buf[:VAD_FRAME_SAMPLES]
                        vad_frame_buf = vad_frame_buf[VAD_FRAME_SAMPLES:]
                        try:
                            is_speech = vad.is_speech(frame.tobytes(), 16000)
                        except Exception:
                            is_speech = True  # treat error as speech to avoid false cutoff
                        if is_speech:
                            vad_speech_frames += 1
                            vad_silence_frames = 0
                        elif vad_speech_frames >= config.VAD_MIN_SPEECH_FRAMES:
                            vad_silence_frames += 1

                vad_cutoff = (
                    vad is not None
                    and vad_speech_frames >= config.VAD_MIN_SPEECH_FRAMES
                    and vad_silence_frames >= config.VAD_SILENCE_FRAMES
                )

                if vad_cutoff or time.time() > recording_end_time:
                    reason = "VAD silence" if vad_cutoff else "timeout"
                    logger.info(
                        f"Recording ended ({reason}): {len(query_buffer)} samples, "
                        f"speech={vad_speech_frames} silence={vad_silence_frames}"
                    )
                    recording = False
                    audio_array = np.array(query_buffer, dtype=np.float64)
                    query_buffer       = []
                    vad_speech_frames  = 0
                    vad_silence_frames = 0
                    vad_frame_buf      = np.array([], dtype=np.int16)
                    
                    # Record interaction in MemoryManager (Tier 3)
                    if memory_manager:
                        # We don't have the text yet, process_llm will call add_interaction later
                        pass

                    threading.Thread(target=process_llm, args=(audio_array,), daemon=True).start()
                    last_wakeword_time = time.time()

            # ── Wake Word Detection / Continuity Bypass ──
            else:
                with state_lock:
                    status            = assistant_state.get("status", "IDLE")
                    is_processing      = assistant_state.get("processing", False)
                    cooldown_until     = assistant_state.get("wakeword_cooldown_until", 0)
                    tts_started_at     = assistant_state.get("tts_started_at", 0)
                    continuity_until   = assistant_state.get("continuity_until", 0)

                # Check Continuity Timeout
                if status == "CONTINUITY" and time.time() > continuity_until:
                    with state_lock:
                        assistant_state["status"] = "IDLE"
                    send_uart_command("APP:ASSISTANT")
                    logger.info("Continuity window expired. Returning to IDLE.")
                    status = "IDLE"

                is_speaking      = _tts_active.is_set()
                is_thinking      = is_processing and not is_speaking
                
                in_cooldown = (
                    is_thinking
                    or is_speaking
                    or (not is_processing and time.time() - assistant_state.get("last_wakeword_at", 0) <= 2.0)
                    or (not is_processing and status != "CONTINUITY" and time.time() < cooldown_until)
                )

                if in_cooldown:
                    oww_buffer = np.array([], dtype=np.int16)
                    time.sleep(0.01)
                    continue

                # Prepare audio for VAD/OWW
                ch_right_16k = ch_right[::3]
                audio_16k    = np.right_shift(ch_right_16k, 16).astype(np.int16)

                # ── CONTINUITY MODE: Bypass Wake Word ──
                if status == "CONTINUITY" and vad:
                    try:
                        # Revert to balanced mode (2) for continuity to reduce false triggers
                        vad.set_mode(2) 
                    except: pass
                    
                    vad_frame_buf = np.concatenate([vad_frame_buf, audio_16k])
                    if len(vad_frame_buf) >= VAD_FRAME_SAMPLES:
                        frame = vad_frame_buf[:VAD_FRAME_SAMPLES]
                        vad_frame_buf = vad_frame_buf[VAD_FRAME_SAMPLES:]
                        if vad.is_speech(frame.tobytes(), 16000):
                            continuity_speech_frames += 1
                            continuity_silence_frames = 0
                        else:
                            continuity_speech_frames = 0
                            continuity_silence_frames += 1
                            
                        # Early exit if room is silent for too long
                        # 30ms frames -> 33.3 frames per second.
                        silence_threshold_frames = int(config.CONTINUITY_SILENCE_TIMEOUT * 33.3)
                        if continuity_silence_frames >= silence_threshold_frames:
                            logger.info(f"CONTINUITY: Silence threshold ({config.CONTINUITY_SILENCE_TIMEOUT}s) reached. Exiting early.")
                            with state_lock:
                                assistant_state["status"] = "IDLE"
                            send_uart_command("APP:ASSISTANT")
                            continuity_silence_frames = 0
                            continue

                        if continuity_speech_frames >= 5: # ~150ms of solid speech
                            logger.info(f"CONTINUITY: Speech detected via VAD ({continuity_speech_frames} frames). Bypassing wake word.")
                            with state_lock:
                                assistant_state["status"] = "LISTENING"
                            recording          = True
                            recording_end_time = time.time() + config.LLM_RECORD_SECONDS
                            query_buffer       = list(np.concatenate(list(pre_record_buffer)) if pre_record_buffer else [])
                            vad_speech_frames  = continuity_speech_frames
                            vad_silence_frames = 0
                            continuity_silence_frames = 0
                            vad_frame_buf      = np.array([], dtype=np.int16)
                            continuity_speech_frames = 0
                            continue

                # ── IDLE MODE: Standard Wake Word Detection ──
                if oww_model and status == "IDLE":
                    if vad:
                        try:
                            vad.set_mode(config.VAD_AGGRESSIVENESS) # Restore Level 3
                        except: pass
                    
                    oww_buffer  = np.concatenate((oww_buffer, audio_16k))
                    if len(oww_buffer) >= 1280:
                        chunk_oww = oww_buffer[:1280]
                        oww_buffer = oww_buffer[1280:]
                        
                        if aec and is_speaking and _aec_ref_buf is not None:
                            cleaned = []
                            for i in range(0, 1280, config.AEC_FRAME):
                                mic_f = chunk_oww[i:i + config.AEC_FRAME].tobytes()
                                with _aec_ref_lock:
                                    n = min(config.AEC_FRAME, len(_aec_ref_buf))
                                    if n == config.AEC_FRAME:
                                        ref_arr = np.array(
                                            [_aec_ref_buf.popleft() for _ in range(config.AEC_FRAME)],
                                            dtype=np.int16)
                                    else:
                                        ref_arr = np.zeros(config.AEC_FRAME, dtype=np.int16)
                                cleaned.extend(np.frombuffer(aec.process(mic_f, ref_arr.tobytes()), dtype=np.int16))
                            chunk_oww = np.array(cleaned, dtype=np.int16)
                        
                        prediction = oww_model.predict(chunk_oww)
                        ww_threshold = config.WAKEWORD_THRESHOLD_BARGE_IN if is_speaking else config.WAKEWORD_THRESHOLD
                        for mdl, score in prediction.items():
                            if score >= ww_threshold:
                                logger.info(f"WAKE WORD: {mdl} ({score:.3f})")
                                if is_speaking: interrupt_tts()
                                send_uart_command(f"WAKE|{mdl}")
                                send_uart_command("EMO:ALERT")
                                with state_lock:
                                    assistant_state["status"] = "LISTENING"
                                
                                with state_lock:
                                    assistant_state["last_wakeword_at"] = time.time()
                                recording          = True
                                recording_end_time = time.time() + config.LLM_RECORD_SECONDS
                                query_buffer       = list(np.concatenate(list(pre_record_buffer)) if pre_record_buffer else [])
                                vad_speech_frames  = 0
                                vad_silence_frames = 0
                                vad_frame_buf      = np.array([], dtype=np.int16)
                                oww_buffer         = np.array([], dtype=np.int16)
                                if hasattr(oww_model, 'reset'): oww_model.reset()
                                logger.info(f"Recording started (via WAKE WORD)")
                                break

            # ── Spectrum + Intensity for Orb ──
            rms_left = np.sqrt(np.mean(norm_samples ** 2)) * 100.0

            with state_lock:
                avg_rms = assistant_state.get("avg_rms", 1.0)
            adj_rms = max(0.0, rms_left - 2.5)

            if adj_rms > 0.02:
                avg_rms = (avg_rms * 0.90) + (adj_rms * 0.10)
            else:
                avg_rms = (avg_rms * 0.95) + (0.10 * 0.05)
            avg_rms = max(0.1, avg_rms)

            dynamic_multiplier = max(2.0, min(2000.0, 80.0 / avg_rms))
            intensity = int(min(100, adj_rms * dynamic_multiplier))
            with state_lock:
                assistant_state["avg_rms"] = avg_rms
                assistant_state["audio_intensity"] = intensity

            now = time.time()
            is_speaking = _tts_active.is_set()
            # Send spectrum if mic is active (ESP32 on Assistant screen) OR if Pi is in a mode that needs the face active
            should_send = mic_active or status in ("LISTENING", "THINKING", "SPEAKING", "CONTINUITY")
            
            if should_send and not is_speaking and now - last_send_time > (1.0 / config.AUDIO_UPDATE_HZ):
                # FFT only at send rate (10Hz) if not speaking (speech synthesis sends its own data)
                gain = max(0.5, min(8.0, 1.0 / avg_rms))
                bins = calculate_spectrum_bins(norm_samples, mic_idx_bounds, gain=gain)
                send_uart_command(f"S{','.join(map(str, bins))}|A{intensity}")
                last_send_time = now

            now = time.time()
            if now - last_log_time > 10.0:
                logger.debug(f"Mic RMS: {rms_left:.4f} (avg: {avg_rms:.4f})")
                last_log_time = now

        except Exception as e:
            logger.error(f"Audio processing error: {e}")
            time.sleep(0.5)

# ─── Encoder ──────────────────────────────────────────────────────────────────

def on_encoder_event(event, direction, value):
    with state_lock:
        mode = assistant_state.get("current_app", "RADAR")
        is_sleeping = assistant_state.get("is_sleeping", False)
    
    # Wake on interaction
    if is_sleeping and event in ("rotate", "press"):
        logger.info(f"Wake trigger detected ({event})! Exiting sleep mode.")
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
            if direction == "CW": send_uart_command("Z+")
            else: send_uart_command("Z-")
        elif m in ("ASSISTANT", "SPEAKING", "CONTINUITY"):
            # Style Toggle Logic
            send_uart_command("STYLE:TOGGLE")
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
        
        logger.info(f"Encoder Button: Long Press! Toggling sleep: {not is_sleeping}")
        _set_sleep_mode(not is_sleeping)

# ─── Hardware Init ────────────────────────────────────────────────────────────

try:
    GPIO.setmode(GPIO.BCM)
    if config.PIN_SFT_GND:
        GPIO.setup(config.PIN_SFT_GND, GPIO.OUT)
        GPIO.output(config.PIN_SFT_GND, GPIO.LOW)
        logger.info(f"Software GND on GPIO {config.PIN_SFT_GND}")
    if config.PIN_AMP_MUTE is not None:
        GPIO.setup(config.PIN_AMP_MUTE, GPIO.OUT)
        GPIO.output(config.PIN_AMP_MUTE, GPIO.LOW)   # SD LOW = shutdown, muted at startup
        logger.info(f"Amp muted on GPIO {config.PIN_AMP_MUTE}")
    encoder = RotaryEncoder(
        clk_pin=config.PIN_ROTARY_CLK,
        dt_pin=config.PIN_ROTARY_DT,
        sw_pin=config.PIN_ROTARY_SW,
        callback=on_encoder_event
    )
    logger.info(f"Encoder on GPIO {config.PIN_ROTARY_CLK}/{config.PIN_ROTARY_DT}")
except Exception as e:
    logger.error(f"Hardware init failed: {e}")

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
