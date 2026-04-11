import io
import os
import re
import subprocess
import threading
import time
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
except ImportError:
    client = None
    LLM_AVAILABLE = False

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
    "zoom": 15,
    "last_event": None,
    "mic_active": True,   # Default TRUE so it works even if sync fails
    "radar_active": True, # Default TRUE so it works even if sync fails
    "audio_intensity": 0,
    "processing": False,
    "wakeword_cooldown_until": 0  # epoch time — OWW blocked until this passes
}
state_lock = threading.Lock()

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(config.LOG_FILE, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suppress noisy Werkzeug HTTP access logs (health checks, status polls)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

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
                ser.write(f"{cmd}\n".encode())
                if not cmd.startswith("S") and not cmd.startswith("A"):
                    logger.info(f"Sent UART: {cmd}")
    except Exception as e:
        logger.error(f"UART send error: {e}")
        reconnect_serial()

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
                        last_w = datetime.fromtimestamp(last_wakeword_time).strftime('%I:%M:%S %p') if last_wakeword_time > 0 else "NEVER"
                        
                        # Check OWW status
                        ww_stat = "READY" if oww_model else "NOT LOADED"
                        if is_proc: ww_stat = "PROC"
                        
                        # WiFi RSSI (approximate or placeholder if not easily reachable in this thread)
                        # We can send it from the Pi's perspective or just a placeholder
                        rssi = -50 
                        
                        diag_msg = f"DIAG:mic={mic_val},ww={ww_stat},pi=OK,rssi={rssi},wake={last_w}"
                        send_uart_command(diag_msg)
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
                    elif "APP:" in line:
                        app_mode = line.split("APP:", 1)[1].split()[0]
                        with state_lock:
                            assistant_state["mic_active"] = (app_mode == "ASSISTANT" or app_mode == "SPEAKING")
                            assistant_state["radar_active"] = (app_mode == "RADAR")
                        logger.info(f"[ESP32] {line} → mic_active={assistant_state['mic_active']}, radar_active={app_mode == 'RADAR'}")
                    elif line not in ("HB:OK",):   # suppress noisy heartbeat
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

    silence_chunk = bytes(int(config.PIPER_SAMPLE_RATE * 0.05) * 2)  # 50ms silence
    def _silence_feeder():
        interval = len(silence_chunk) / (config.PIPER_SAMPLE_RATE * 2) * 0.85
        while True:
            if not _tts_active.is_set():
                try:
                    with _audio_lock:
                        _aplay_stdin.write(silence_chunk)
                        _aplay_stdin.flush()
                except Exception:
                    break
            time.sleep(interval)

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
# Keep a pre-loaded Piper process ready so there's no model-load delay on first
# use. After each query the standby is replaced in the background.

_warm_piper = None
_warm_piper_lock = threading.Lock()

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
    global _warm_piper
    try:
        p = _spawn_piper()
        with _warm_piper_lock:
            _warm_piper = p
        logger.info("Piper warm standby ready")
    except Exception as e:
        logger.error(f"Piper warmup failed: {e}")

def _get_piper():
    """Return a warm Piper process (or cold-start one if standby isn't ready).
    Immediately kicks off a new warmup so the next query is also fast."""
    global _warm_piper
    with _warm_piper_lock:
        p = _warm_piper
        _warm_piper = None

    if p is None or p.poll() is not None:
        logger.info("Piper cold start (warm standby not ready yet)")
        p = _spawn_piper()

    # Replenish the standby in the background
    threading.Thread(target=_warmup_piper, daemon=True).start()
    return p

# ─── Weather + Context ────────────────────────────────────────────────────────

_WMO_CODES = {
    0:"Clear sky", 1:"Mainly clear", 2:"Partly cloudy", 3:"Overcast",
    45:"Fog", 48:"Icy fog", 51:"Light drizzle", 53:"Drizzle", 55:"Heavy drizzle",
    61:"Light rain", 63:"Rain", 65:"Heavy rain", 71:"Light snow", 73:"Snow",
    75:"Heavy snow", 80:"Rain showers", 81:"Rain showers", 82:"Heavy showers",
    95:"Thunderstorm", 96:"Thunderstorm with hail", 99:"Thunderstorm with hail",
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

        lines = [
            f"Current weather: {wmo(cur['weather_code'])}, "
            f"{cur['temperature_2m']:.0f} degrees (feels {cur['apparent_temperature']:.0f}), "
            f"humidity {cur['relative_humidity_2m']} percent, wind {cur['wind_speed_10m']:.0f} miles per hour."
        ]
        for i in range(len(dy["time"])):
            rain = f", {dy['precipitation_sum'][i]:.2f} inches of rain" if dy["precipitation_sum"][i] > 0 else ""
            lines.append(
                f"{dy['time'][i]}: {wmo(dy['weather_code'][i])}, "
                f"high {dy['temperature_2m_max'][i]:.0f}, low {dy['temperature_2m_min'][i]:.0f}{rain}."
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
    """One-liner injected into every LLM prompt: date/time + weather."""
    from datetime import datetime
    now = datetime.now()
    return (
        f"Today is {now.strftime('%A, %B %d %Y')}. "
        f"The time is {now.strftime('%I:%M %p')}. "
        + get_weather()
    )

# ─── Timer Management ─────────────────────────────────────────────────────────

_TIMER_TAG = re.compile(r'\[TIMER:(\d+)(?::([^\]]*))?\]')

_active_timers      = {}
_active_timers_lock = threading.Lock()

def speak_text(text):
    """Speak arbitrary text through the Piper/aplay pipeline."""
    try:
        piper = _get_piper()
        piper.stdin.write(text.encode() + b'\n')
        piper.stdin.close()
        _tts_active.set()
        try:
            while True:
                chunk = piper.stdout.read(4096)
                if not chunk:
                    break
                with _audio_lock:
                    _aplay_stdin.write(chunk)
                    _aplay_stdin.flush()
        finally:
            _tts_active.clear()
    except Exception as e:
        logger.error(f"speak_text error: {e}")

def _handle_timer_done(label):
    """Called when ESP32 sends TIMER:DONE — announce completion."""
    with _active_timers_lock:
        _active_timers.pop(label, None)
    logger.info(f"Timer fired (ESP32): '{label}'")
    send_uart_command("APP: SPEAKING")
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
    import select as _select
    try:
        first = True
        while True:
            ready, _, _ = _select.select([piper.stdout], [], [], 0.010)  # 10ms timeout
            if ready:
                if _tts_abort.is_set():
                    break   # interrupt — discard buffered audio immediately
                chunk = piper.stdout.read(4096)
                if not chunk:
                    break   # piper stdout closed — done
                if first:
                    _tts_active.set()
                    send_uart_command("APP: SPEAKING")
                    first = False
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
                with _audio_lock:
                    _aplay_stdin.write(chunk_out)
                    _aplay_stdin.flush()
                # Push reference audio for AEC (use scaled output for correctness)
                ref_samples = np.frombuffer(chunk_out, dtype=np.int16)
                with _aec_ref_lock:
                    if _aec_ref_buf is not None:
                        _aec_ref_buf.extend(ref_samples)

                # ── Digital Mouth Sync ──
                # Calculate intensity + spectrum for the generated speech chunk
                # Alsa buffer is 100ms. We add a small delay to compensate for the hardware latency.
                samples_speech = np.frombuffer(chunk_out, dtype=np.int16)
                if len(samples_speech) > 256:
                    norm_speech = samples_speech.astype(np.float64) / 32768.0
                    intensity = int(min(100, np.sqrt(np.mean(norm_speech**2)) * 450.0))
                    
                    chunk_sz = len(samples_speech)
                    idx_bounds_speech = get_fft_bounds(chunk_sz, config.PIPER_SAMPLE_RATE)
                    bins = calculate_spectrum_bins(norm_speech, idx_bounds_speech, gain=2.5)
                    
                    # Wait a tiny bit so the audio actually exits the speakers before the mouth moves
                    # (Hardware/Alsa buffer compensation)
                    time.sleep(0.06) 
                    send_uart_command(f"S{','.join(map(str, bins))}|A{intensity}")

            elif not first:
                # Inter-sentence gap — keep aplay buffer fed to prevent underrun clicks
                with _audio_lock:
                    _aplay_stdin.write(_GAP_SILENCE)
                    _aplay_stdin.flush()
    except Exception:
        pass
    finally:
        _tts_active.clear()

_MD_STRIP = re.compile(r'(\*{1,3}|_{1,3}|`+)')

def _tts_clean(text: str) -> str:
    """Strip Markdown emphasis markers so Piper doesn't read them aloud."""
    return _MD_STRIP.sub('', text)

def _speak_text_iter(text_iter):
    """
    Consume text chunks from text_iter, feeding complete sentences to Piper
    as they arrive so TTS starts on the first sentence while the LLM is still
    generating the rest. Returns (piper_proc, full_text).
    Respects _tts_abort: stops feeding sentences and returns early if set.
    """
    global _active_piper_proc
    _tts_abort.clear()   # clear any abort flag left over from a previous interrupt
    piper = None
    fwd   = None
    buf   = ""
    parts = []

    def _ensure_piper():
        nonlocal piper, fwd
        if piper is None:
            piper = _get_piper()
            with _active_piper_lock:
                _active_piper_proc = piper
            fwd = threading.Thread(target=_forward_piper_audio, args=(piper,), daemon=True)
            fwd.start()

    _TRANS_PATTERN = re.compile(r'\[TRANSCRIPT\]:\s*"(.*?)"', re.DOTALL)
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
            if "]" in buf:
                match = _TRANS_PATTERN.search(buf)
                if match:
                    transcript = match.group(1)
                    logger.info(f"[USER TRANSCRIPT]: {transcript}")
                    # Remove the transcript block from the buffer
                    buf = buf[match.end():].lstrip()
                    logged_transcript = True
                elif len(buf) > 500: # Safety fallback if model fails to follow format
                    logged_transcript = True

        while logged_transcript: # Only start speaking once transcript is handled
            m = _SENTENCE_END.search(buf)
            if not m:
                break
            sent = buf[:m.start() + 1].strip()
            buf  = buf[m.end():]
            if sent:
                _ensure_piper()
                if not _tts_abort.is_set():
                    piper.stdin.write(_tts_clean(sent).encode() + b'\n')

    if buf.strip() and not _tts_abort.is_set():
        # Final safety check: if transcript was never logged, try one last time
        if not logged_transcript:
            match = _TRANS_PATTERN.search(buf)
            if match:
                logger.info(f"[USER TRANSCRIPT]: {match.group(1)}")
                buf = buf[match.end():].lstrip()
        
        if buf.strip():
            _ensure_piper()
            piper.stdin.write(_tts_clean(buf.strip()).encode() + b'\n')

    if piper:
        try:
            piper.stdin.close()   # always close — kernel cleans up even if piper was killed
        except Exception:
            pass
        fwd.join()   # forward thread exits quickly once piper stdout closes/dies
        try:
            piper.wait(timeout=2)   # reap zombie process
        except Exception:
            try:
                piper.kill()
            except Exception:
                pass
        if not _tts_abort.is_set():
            logger.info("Playback finished.")
        with _active_piper_lock:
            if _active_piper_proc is piper:
                _active_piper_proc = None

    return piper, ''.join(parts).strip()

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
    )
]) if LLM_AVAILABLE else None


def process_llm(audio_array):
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

    with state_lock:
        assistant_state["processing"] = True

    try:
        peak_amplitude = float(np.max(np.abs(audio_array)))
        if peak_amplitude < config.LLM_MIN_PEAK:
            logger.info(f"Recording discarded — silence (peak={peak_amplitude:.5f} < {config.LLM_MIN_PEAK})")
            return

        send_uart_command("APP: THINKING")
        logger.info(f"LLM query: {len(audio_array)} samples, peak={peak_amplitude:.5f}")

        # Normalize audio
        peak = np.max(np.abs(audio_array))
        if peak > 0.001:
            audio_array = (audio_array / peak) * 0.95

        # Encode to 16-bit PCM WAV in memory
        audio_int16 = (audio_array * 32767).astype(np.int16)
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(config.AUDIO_RATE)
            wf.writeframes(audio_int16.tobytes())
        wav_bytes = wav_buf.getvalue()

        context  = get_context()
        user_msg = (
            "Listen to the audio and answer the spoken question or fulfill the spoken request. "
            "Ignore background noises and clicks. "
            "IMPORTANT: Always start your response with a clear transcription of what you heard in the audio in this exact format: [TRANSCRIPT]: \"user's spoken words\". "
            "Then provide your actual answer on a new line. "
            "You can answer any question directly using your knowledge and the context below. "
            "Only call set_timer if the user explicitly asks to set or start a timer. "
            f"Context: {context}"
        )
        # Log exactly what is being sent to the AI
        logger.info(f"LLM SYSTEM PROMPT: {config.LLM_SYSTEM_PROMPT}")
        logger.info(f"LLM USER MESSAGE: {user_msg}")

        audio_part = types.Part.from_bytes(data=wav_bytes, mime_type='audio/wav')
        gen_cfg    = types.GenerateContentConfig(
            system_instruction=config.LLM_SYSTEM_PROMPT,
            tools=[_TIMER_TOOL],
        )

        # ── First streaming call: speak response, collect any function calls ───
        pending_timers = []
        model_parts    = []

        stream1 = client.models.generate_content_stream(
            model=config.LLM_MODEL,
            contents=[audio_part, user_msg],
            config=gen_cfg,
        )

        def _first_iter():
            for chunk in stream1:
                if not chunk.candidates:
                    continue
                for part in chunk.candidates[0].content.parts:
                    model_parts.append(part)
                    if hasattr(part, 'function_call') and part.function_call:
                        fc = part.function_call
                        if fc.name == "set_timer":
                            pending_timers.append((int(fc.args["seconds"]), str(fc.args.get("label", ""))))
                    if getattr(part, 'text', None):
                        yield part.text

        _, first_text = _speak_text_iter(_first_iter())

        if _tts_abort.is_set():
            return  # user interrupted — skip follow-up call and timer setup

        # ── If timer requested: follow-up streaming call for confirmation ─────
        # set_timer() is called after TTS finishes so the ring starts post-confirmation
        if pending_timers:
            fn_responses = [
                types.Part.from_function_response(name="set_timer", response={"status": "started"})
                for _ in pending_timers
            ]
            stream2 = client.models.generate_content_stream(
                model=config.LLM_MODEL,
                contents=[
                    audio_part, user_msg,
                    types.Content(role="model", parts=model_parts),
                    types.Content(role="user", parts=fn_responses),
                ],
                config=types.GenerateContentConfig(system_instruction=config.LLM_SYSTEM_PROMPT),
            )

            def _follow_iter():
                for chunk in stream2:
                    if not chunk.candidates:
                        continue
                    for part in chunk.candidates[0].content.parts:
                        if getattr(part, 'text', None):
                            yield part.text

            _, full_text = _speak_text_iter(_follow_iter())
        else:
            full_text = first_text

        if full_text:
            logger.info(f"LLM answer: {full_text}")
            send_uart_command(f"TXT|{full_text.replace(chr(10), ' ')}")

        # Start timers after confirmation has been spoken
        for secs, label in pending_timers:
            set_timer(secs, label)

    except Exception as e:
        logger.error(f"LLM pipeline error: {e}")
        send_uart_command("TXT|Sorry, I had an error.")
    finally:
        _tts_active.clear()  # Ensure silence feeder resumes on any exit path
        with state_lock:
            assistant_state["processing"] = False
            # Post-LLM cooldown so OWW doesn't re-trigger on speaker echo
            assistant_state["wakeword_cooldown_until"] = time.time() + config.WAKEWORD_POST_LLM_COOLDOWN
        send_uart_command("APP: ASSISTANT")

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
                logger.info(f"OWW model loaded: {wakeword_models[0]}")
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
    last_wakeword_time = 0
    pre_record_buffer  = deque()  # deque of numpy chunks, total ≤ 71680 samples
    pre_roll_len       = 0

    while True:
        try:
            with state_lock:
                mic_active = assistant_state.get("mic_active", False)

            data = stream.read(CHUNK, exception_on_overflow=False)
            raw_samples = np.frombuffer(data, dtype=np.int32)
            ch_left  = raw_samples[0::2]   # noqa: F841
            ch_right = raw_samples[1::2]   # INMP441 on right channel (L/R=3.3V)

            # Normalize 32-bit → float64 ±1.0
            norm_samples = ch_right.astype(np.float64) / 2147483648.0

            # Maintain ~1.5s pre-roll buffer — store numpy chunks to avoid O(n) list copies
            if not recording:
                pre_record_buffer.append(norm_samples)
                pre_roll_len += len(norm_samples)
                while pre_roll_len > 71680:
                    pre_roll_len -= len(pre_record_buffer.popleft())

            # ── Recording + VAD ──
            if recording:
                query_buffer.extend(norm_samples)

                if vad:
                    # Accumulate downsampled int16 for VAD (48k→16k, 32bit→16bit)
                    chunk_16k = np.right_shift(ch_right[::3], 16).astype(np.int16)
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
                    threading.Thread(target=process_llm, args=(audio_array,), daemon=True).start()
                    last_wakeword_time = time.time()

            # ── Wake Word Detection ──
            elif oww_model:
                with state_lock:
                    is_processing      = assistant_state.get("processing", False)
                    cooldown_until     = assistant_state.get("wakeword_cooldown_until", 0)

                is_speaking    = _tts_active.is_set()
                is_thinking    = is_processing and not is_speaking  # LLM still generating

                in_cooldown = (
                    is_thinking   # can't interrupt while thinking, only while speaking
                    or (not is_processing and time.time() - last_wakeword_time <= 3.0)
                    or (not is_processing and time.time() < cooldown_until)
                )
                if in_cooldown:
                    # Drain OWW buffer so stale audio can't re-trigger after cooldown
                    oww_buffer = np.array([], dtype=np.int16)
                    if is_thinking:
                        time.sleep(0.01)
                    continue

                # Downsample 48kHz → 16kHz, 32-bit → 16-bit for OWW
                ch_right_16k = ch_right[::3]
                oww_audio    = np.right_shift(ch_right_16k, 16).astype(np.int16)
                oww_buffer  = np.concatenate((oww_buffer, oww_audio))

                if len(oww_buffer) >= 1280:
                    chunk_oww = oww_buffer[:1280]
                    oww_buffer = oww_buffer[1280:]

                    # AEC: cancel speaker echo when TTS is playing
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
                            logger.info(f"WAKE WORD: {mdl} ({score:.3f}, threshold={ww_threshold})")
                            if is_speaking:
                                interrupt_tts()
                            send_uart_command(f"WAKE|{mdl}")
                            send_uart_command("EMO:ALERT")
                            last_wakeword_time = time.time()

                            recording          = True
                            recording_end_time = time.time() + config.LLM_RECORD_SECONDS
                            query_buffer       = list(np.concatenate(list(pre_record_buffer)) if pre_record_buffer else [])
                            vad_speech_frames  = 0
                            vad_silence_frames = 0
                            vad_frame_buf      = np.array([], dtype=np.int16)
                            oww_buffer         = np.array([], dtype=np.int16)
                            if hasattr(oww_model, 'reset'):
                                oww_model.reset()
                            logger.info(f"Recording started (max {config.LLM_RECORD_SECONDS}s, VAD={'on' if vad else 'off'})")
                            break

            # ── Spectrum + Intensity for Orb ──
            rms_left = np.sqrt(np.mean(norm_samples ** 2)) * 100.0

            avg_rms = assistant_state.get("avg_rms", 1.0)
            adj_rms = max(0.0, rms_left - 2.5)

            if adj_rms > 0.02:
                avg_rms = (avg_rms * 0.90) + (adj_rms * 0.10)
            else:
                avg_rms = (avg_rms * 0.95) + (0.10 * 0.05)
            assistant_state["avg_rms"] = max(0.1, avg_rms)

            dynamic_multiplier = max(2.0, min(2000.0, 80.0 / assistant_state["avg_rms"]))
            intensity = int(min(100, adj_rms * dynamic_multiplier))
            with state_lock:
                assistant_state["audio_intensity"] = intensity

            now = time.time()
            is_speaking = _tts_active.is_set()
            if mic_active and not is_speaking and now - last_send_time > (1.0 / config.AUDIO_UPDATE_HZ):
                # FFT only at send rate (10Hz) if not speaking (speech synthesis sends its own data)
                gain = max(0.5, min(8.0, 1.0 / assistant_state["avg_rms"]))
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
    assistant_state["last_event"] = f"{event}_{direction}"
    logger.info(f"Encoder: {event} {direction}")
    if event == "rotate":
        if direction == "CW":
            assistant_state["zoom"] = max(5, assistant_state["zoom"] - 1)
            send_uart_command("Z+")
        else:
            assistant_state["zoom"] = min(250, assistant_state["zoom"] + 1)
            send_uart_command("Z-")

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
    return jsonify(assistant_state)

@app.route('/mic/start')
def mic_start():
    assistant_state["mic_active"] = True
    return jsonify({"status": "mic_active"})

@app.route('/mic/stop')
def mic_stop():
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

    # The ADS-B proxy is now managed as a standalone systemd service (adsb_sidecar)
    # so we no longer need to launch it as a subprocess here.

    reader_thread = threading.Thread(target=serial_reader, daemon=True)
    reader_thread.start()
    logger.info("Serial reader thread started")

    time.sleep(0.5)
    send_uart_command("SYNC?")

    # Pre-warm Piper so the first query has no model-load delay
    threading.Thread(target=_warmup_piper, daemon=True).start()

    audio_thread = threading.Thread(target=audio_processor, daemon=True)
    audio_thread.start()
    logger.info("Audio processor thread started")

    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT)
