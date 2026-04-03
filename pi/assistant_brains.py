import io
import os
import re
import subprocess
import threading
import time
import wave

import numpy as np
import requests
import serial
import logging
from logging.handlers import RotatingFileHandler

import RPi.GPIO as GPIO
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

# State
assistant_state = {
    "zoom": 15,
    "last_event": None,
    "mic_active": True,  # Default TRUE so it works even if sync fails
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
                    if "APP:" in line:
                        app_mode = line.split("APP:", 1)[1].split()[0]
                        with state_lock:
                            assistant_state["mic_active"] = (app_mode == "ASSISTANT")
                        logger.info(f"[ESP32] {line} → mic_active={app_mode == 'ASSISTANT'}")
                    else:
                        logger.debug(f"[ESP32 unexpected] {line}")
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

def _start_persistent_output():
    """Start persistent aplay + silence feeder. Call once after I2S stream opens."""
    global _aplay_stdin
    aplay = subprocess.Popen(
        ['aplay', '-D', config.APLAY_DEVICE,
         '-t', 'raw', '-f', 'S16_LE',
         '-r', str(config.PIPER_SAMPLE_RATE), '-c', '1', '-q',
         '--buffer-time=500000'],
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

# ─── LLM + TTS Pipeline ───────────────────────────────────────────────────────

_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

def _iter_sentences(stream):
    """Yield complete sentences as they arrive from a streaming LLM response.
    Yields (sentence, is_last_fragment, full_text_so_far) tuples."""
    buf = ""
    full_parts = []
    for chunk in stream:
        text = getattr(chunk, 'text', None) or ''
        if not text:
            continue
        buf += text
        full_parts.append(text)
        while True:
            m = _SENTENCE_END.search(buf)
            if not m:
                break
            sentence = buf[:m.start() + 1].strip()
            buf = buf[m.end():]
            if sentence:
                yield sentence, False, ''.join(full_parts)
    # Yield whatever remains as the final fragment
    remaining = buf.strip()
    if remaining:
        yield remaining, True, ''.join(full_parts)
    elif full_parts:
        yield '', True, ''.join(full_parts)

def process_llm(audio_array):
    """
    Full optimized pipeline:
      1. Normalize + encode audio in-memory (no temp file)
      2. Send inline audio to Gemini (no file-upload round-trip)
      3. Stream response → buffer to sentence boundaries
      4. Feed sentences to warm Piper subprocess
      5. Forward Piper raw PCM → aplay stdin (audio starts before LLM finishes)
    """
    if not LLM_AVAILABLE:
        logger.error("LLM libraries not installed!")
        send_uart_command("TXT|AI missing")
        send_uart_command("APP: ASSISTANT")
        return

    with state_lock:
        assistant_state["processing"] = True

    piper_proc = None

    try:
        # Gate: discard silent recordings before touching the LLM.
        # Uses peak amplitude (not RMS) so the silent pre-roll buffer doesn't dilute
        # the check — one moment of speech will always produce a clear peak.
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

        # Encode to 16-bit PCM WAV in memory — no disk I/O, no temp file
        audio_int16 = (audio_array * 32767).astype(np.int16)
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(config.AUDIO_RATE)
            wf.writeframes(audio_int16.tobytes())
        wav_bytes = wav_buf.getvalue()

        # Stream from Gemini — inline audio skips the file-upload round-trip
        response_stream = client.models.generate_content_stream(
            model=config.LLM_MODEL,
            contents=[
                types.Part.from_bytes(data=wav_bytes, mime_type='audio/wav'),
                "Listen to the audio and fulfill the spoken request. "
                "Ignore background noises and clicks. "
                + config.LLM_SYSTEM_PROMPT
            ]
        )

        # Grab the pre-warmed Piper (model already loaded → no startup delay)
        piper_proc = _get_piper()

        # Forward thread: piper stdout → persistent aplay stdin
        # Pauses the silence feeder while TTS audio is writing so they don't interleave.
        def _forward_audio():
            first = True
            try:
                while True:
                    chunk = piper_proc.stdout.read(4096)
                    if not chunk:
                        break
                    if first:
                        _tts_active.set()   # Pause silence feeder
                        send_uart_command("APP: SPEAKING")
                        first = False
                    with _audio_lock:
                        _aplay_stdin.write(chunk)
                        _aplay_stdin.flush()
            except Exception:
                pass
            finally:
                _tts_active.clear()  # Resume silence feeder

        forward_thread = threading.Thread(target=_forward_audio, daemon=True)
        forward_thread.start()

        # Stream LLM → sentence buffer → Piper stdin
        full_text = ""
        for sentence, is_last, accumulated in _iter_sentences(response_stream):
            if sentence:
                logger.debug(f"TTS: {sentence!r}")
                piper_proc.stdin.write(sentence.encode() + b'\n')
                piper_proc.stdin.flush()
            if is_last:
                full_text = accumulated

        # Signal Piper that input is done → it flushes remaining audio and exits
        piper_proc.stdin.close()

        # Send transcription to display (non-blocking — happens while audio plays)
        if full_text:
            display_text = full_text.strip().replace('\n', ' ')
            logger.info(f"LLM answer: {display_text}")
            send_uart_command(f"TXT|{display_text}")

        # Wait for all audio to be written to the persistent aplay buffer
        forward_thread.join()
        logger.info("Playback finished.")

    except Exception as e:
        logger.error(f"LLM pipeline error: {e}")
        send_uart_command("TXT|Sorry, I had an error.")
    finally:
        _tts_active.clear()  # Ensure silence feeder resumes on any exit path
        for proc in (piper_proc,):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        with state_lock:
            assistant_state["processing"] = False
            # Post-LLM cooldown so OWW doesn't re-trigger on speaker echo
            assistant_state["wakeword_cooldown_until"] = time.time() + config.WAKEWORD_POST_LLM_COOLDOWN
        send_uart_command("APP: ASSISTANT")

# ─── Speaker Init ─────────────────────────────────────────────────────────────

def _amp_enable():
    """Enable the amplifier (SD pin HIGH)."""
    if config.PIN_AMP_MUTE is not None:
        GPIO.output(config.PIN_AMP_MUTE, GPIO.HIGH)

def _amp_mute():
    """Shut down the amplifier (SD pin LOW)."""
    if config.PIN_AMP_MUTE is not None:
        GPIO.output(config.PIN_AMP_MUTE, GPIO.LOW)

def _unmute_and_prime_speaker():
    """No-op when hardware SD pin is configured — amp is enabled only after
    audio data is already flowing (see _forward_audio). Kept for setups without
    a mute pin where ALSA buffer pre-init is still useful."""
    if config.PIN_AMP_MUTE is not None:
        logger.info("SD pin active — skipping software prime, amp stays muted")
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

    # VAD frame: 30ms at 16kHz = 480 samples
    VAD_FRAME_SAMPLES = 480

    # ── Recording state ──
    recording            = False
    recording_end_time   = 0
    query_buffer         = []
    vad_speech_frames    = 0
    vad_silence_frames   = 0
    vad_frame_buf        = np.array([], dtype=np.int16)

    # ── Other state ──
    last_send_time     = 0
    oww_buffer         = np.array([], dtype=np.int16)
    last_log_time      = time.time()
    last_wakeword_time = 0
    pre_record_buffer  = []

    while True:
        try:
            with state_lock:
                mic_active = assistant_state.get("mic_active", False)
            if not mic_active:
                time.sleep(0.1)
                continue

            data = stream.read(CHUNK, exception_on_overflow=False)
            raw_samples = np.frombuffer(data, dtype=np.int32)
            ch_left  = raw_samples[0::2]   # INMP441 on left channel
            ch_right = raw_samples[1::2]   # noqa: F841

            # Normalize 32-bit → float64 ±1.0
            norm_samples = ch_left.astype(np.float64) / 2147483648.0

            # Maintain ~1.5s pre-roll buffer for natural-sounding recordings
            if not recording:
                pre_record_buffer.extend(norm_samples)
                if len(pre_record_buffer) > 71680:
                    pre_record_buffer = pre_record_buffer[-71680:]

            # ── Recording + VAD ──
            if recording:
                query_buffer.extend(norm_samples)

                if vad:
                    # Accumulate downsampled int16 for VAD (48k→16k, 32bit→16bit)
                    chunk_16k = np.right_shift(ch_left[::3], 16).astype(np.int16)
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

                in_cooldown = (
                    is_processing
                    or time.time() - last_wakeword_time <= 3.0
                    or time.time() < cooldown_until
                )
                if in_cooldown:
                    # Drain OWW buffer so stale audio can't re-trigger after cooldown
                    oww_buffer = np.array([], dtype=np.int16)
                    if is_processing:
                        time.sleep(0.01)
                    continue

                # Downsample 48kHz → 16kHz, 32-bit → 16-bit for OWW
                ch_left_16k = ch_left[::3]
                oww_audio   = np.right_shift(ch_left_16k, 16).astype(np.int16)
                oww_buffer  = np.concatenate((oww_buffer, oww_audio))

                if len(oww_buffer) >= 1280:
                    prediction = oww_model.predict(oww_buffer[:1280])
                    oww_buffer = oww_buffer[1280:]

                    for mdl, score in prediction.items():
                        if score >= config.WAKEWORD_THRESHOLD:
                            logger.info(f"WAKE WORD: {mdl} ({score:.3f})")
                            send_uart_command(f"WAKE|{mdl}")
                            last_wakeword_time = time.time()

                            recording          = True
                            recording_end_time = time.time() + config.LLM_RECORD_SECONDS
                            query_buffer       = list(pre_record_buffer)
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
            adj_rms = max(0.0, rms_left - 0.03)

            if adj_rms > 0.02:
                avg_rms = (avg_rms * 0.90) + (adj_rms * 0.10)
            else:
                avg_rms = (avg_rms * 0.95) + (0.10 * 0.05)
            assistant_state["avg_rms"] = max(0.1, avg_rms)

            dynamic_multiplier = max(2.0, min(2000.0, 80.0 / assistant_state["avg_rms"]))
            intensity = int(min(100, adj_rms * dynamic_multiplier))
            with state_lock:
                assistant_state["audio_intensity"] = intensity

            fft_data    = np.abs(np.fft.rfft(norm_samples))
            num_bins    = 16
            freq_bounds = np.geomspace(80, 12000, num_bins + 1)
            idx_bounds  = np.clip((freq_bounds * CHUNK / RATE).astype(int), 0, len(fft_data) - 1)
            gain        = max(0.5, min(8.0, 1.0 / assistant_state["avg_rms"]))
            weights     = np.linspace(1.0, 2.0, num_bins)

            bins = []
            for i in range(num_bins):
                start, end = idx_bounds[i], max(idx_bounds[i] + 1, idx_bounds[i + 1])
                mag = max(0.0, np.mean(fft_data[start:end]) - 0.001)
                bins.append(int(min(100, np.log1p(mag * 180.0 * gain * weights[i]) * 17.0)))

            now = time.time()
            if now - last_send_time > (1.0 / config.AUDIO_UPDATE_HZ):
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

# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
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
