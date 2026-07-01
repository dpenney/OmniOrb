import logging
import threading
import time
from collections import deque
import numpy as np
import pyaudio
import openwakeword
from openwakeword.model import Model
import webrtcvad
import scipy.signal as _scipy_signal
import config

logger = logging.getLogger(__name__)

SCIPY_AVAILABLE = True
OWW_AVAILABLE = True
VAD_AVAILABLE = True

try:
    import speexdsp as _speexdsp
    _SPEEX_AVAILABLE = True
    class _SpeexEC:
        def __init__(self, st):
            self.st = st
        @classmethod
        def create(cls, frame_size, filter_length, sample_rate):
            st = _speexdsp.EchoCanceller.create(frame_size, filter_length, sample_rate)
            return cls(st)
        def capture(self, rec, ref):
            return self.st.capture(rec, ref)
except ImportError:
    _SPEEX_AVAILABLE = False

_FFT_WEIGHTS = np.ones(16)
_FFT_FLOOR   = np.array([0.673, 0.3535, 0.2799, 0.1647, 0.1024, 0.059, 0.0352, 0.0254,
                         0.0179, 0.0129, 0.0094, 0.0072, 0.0055, 0.0043, 0.0035, 0.0028])

class _InputFilters:
    """Stateful per-chunk input filtering (high-pass + optional notch).

    Operates on normalized float audio in [-1, 1]. Filter state (zi) is carried
    across chunks so there are no discontinuities at chunk boundaries. Disabled
    cheaply when the profile requests no filtering or scipy is unavailable."""

    def __init__(self, rate, highpass_hz=0, notch_hz=0):
        self.stages = []
        if not (highpass_hz or notch_hz):
            return
        if not SCIPY_AVAILABLE:
            logger.warning('scipy unavailable — input noise filtering disabled')
            return
        nyq = rate / 2.0
        if highpass_hz:
            b, a = _scipy_signal.butter(2, highpass_hz / nyq, btype='highpass')
            self.stages.append([b, a, _scipy_signal.lfilter_zi(b, a)])
        if notch_hz:
            b, a = _scipy_signal.iirnotch(notch_hz / nyq, 30.0)
            self.stages.append([b, a, _scipy_signal.lfilter_zi(b, a)])
        logger.info(f'Input filters: highpass={highpass_hz}Hz notch={notch_hz}Hz')

    @property
    def enabled(self):
        return bool(self.stages)

    def process(self, x):
        for st in self.stages:
            x, st[2] = _scipy_signal.lfilter(st[0], st[1], x, zi=st[2])
        return x

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
        end = max(start + 1, idx_bounds[i + 1])
        mag = max(0.0, np.mean(fft_data[start:end]) - _FFT_FLOOR[i])
        bins.append(int(min(100, np.log1p(mag * 180.0 * gain * _FFT_WEIGHTS[i]) * 17.0)))
    return bins

class AudioEngine:
    def __init__(self, callbacks):
        self.callbacks = callbacks
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._audio_loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _audio_loop(self):
            """Background thread: capture mic audio, drive wake word + VAD + spectrum."""
            if not pyaudio:
            logger.error('pyaudio not installed. Audio disabled.')
            return
            CHUNK = config.AUDIO_CHUNK
            CHANNELS = config.AUDIO_CHANNELS
            RATE = config.AUDIO_RATE
            if config.AUDIO_SAMPLE_FORMAT == 'int16':
            FORMAT = pyaudio.paInt16
            SAMPLE_DTYPE = np.int16
            FULL_SCALE = 32768.0
            else:
            FORMAT = pyaudio.paInt32
            SAMPLE_DTYPE = np.int32
            FULL_SCALE = 2147483648.0
            logger.info(f"Audio source '{config.AUDIO_SOURCE}': dev={config.AUDIO_DEVICE_INDEX} fmt={config.AUDIO_SAMPLE_FORMAT} rate={RATE} gain={config.AUDIO_GAIN}")
            self.callbacks.get('apply_audio_routing', lambda: None)()
            input_filters = _InputFilters(RATE, config.AUDIO_HIGHPASS_HZ, config.AUDIO_NOTCH_HZ)
            p = pyaudio.PyAudio()
            oww_model = None
            if OWW_AVAILABLE and hasattr(config, 'WAKEWORD_MODEL'):
            try:
                model_target = config.WAKEWORD_MODEL
                wakeword_models = []
                if not model_target.endswith('.onnx'):
                    for path in openwakeword.get_pretrained_model_paths():
                        if model_target in path:
                            wakeword_models.append(path)
                            break
                else:
                    wakeword_models = [model_target]
                if not wakeword_models:
                    logger.error('OWW model not found: %s', model_target)
                else:
                    oww_model = Model(wakeword_model_paths=wakeword_models)
                    with self.callbacks.get('get_state_lock', threading.Lock)():
                        self.callbacks.get('get_state_dict', lambda: {})()['oww_ready'] = True
                    logger.info('OWW model loaded: %s', wakeword_models[0])
                    for _ in range(_MAX_WARM_PIPERS):
                        threading.Thread(target=self.callbacks.get('warmup_piper', lambda: None), daemon=True).start()
            except Exception as e:
                logger.error('OWW load failed: %s', e)
            vad = None
            if VAD_AVAILABLE:
            try:
                vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
                logger.info('VAD ready (aggressiveness=%s)', config.VAD_AGGRESSIVENESS)
            except Exception as e:
                logger.error('VAD init failed: %s', e)
            stream = None
            for attempt in range(1, 6):
            try:
                stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, input_device_index=config.AUDIO_DEVICE_INDEX, frames_per_buffer=CHUNK)
                logger.info(f'Audio stream opened ({config.AUDIO_SAMPLE_FORMAT}, {CHANNELS}ch @ {RATE}Hz, attempt {attempt})')
                break
            except Exception as e:
                logger.warning(f'Audio stream open attempt {attempt}/5 failed: {e}')
                if 'Invalid number of channels' in str(e) and CHANNELS > 1:
                    CHANNELS = 1
                    logger.info('Falling back to mono capture (1 channel)')
                time.sleep(1.0)
            if stream is None:
            logger.error('Failed to open audio stream after 5 attempts — audio disabled')
            return
            self.callbacks.get('amp_enable', lambda: None)()
            self.callbacks.get('start_persistent_output', lambda: None)()
            logger.info('=' * 60)
            logger.info('  ASSISTANT READY — listening for wake word')
            logger.info('=' * 60)
            global _aec_ref_buf
            VAD_FRAME_SAMPLES = 480
            mic_idx_bounds = get_fft_bounds(CHUNK, RATE)
            recording = False
            recording_end_time = 0
            from_continuity = False
            query_buffer = []
            vad_speech_frames = 0
            vad_silence_frames = 0
            vad_frame_buf = np.array([], dtype=np.int16)
            aec = None
            if config.AEC_ENABLED and _SPEEX_AVAILABLE:
            try:
                aec = _SpeexEC.create(config.AEC_FRAME, config.AEC_FILTER_LENGTH, 16000)
                _aec_ref_buf = deque([0] * config.AEC_DELAY_SAMPLES, maxlen=32000)
                logger.info(f'AEC ready (SpeexDSP, frame={config.AEC_FRAME}, filter={config.AEC_FILTER_LENGTH}, delay={config.AEC_DELAY_SAMPLES})')
            except Exception as e:
                logger.warning('AEC init failed: %s', e)
            elif not _SPEEX_AVAILABLE:
            logger.info('AEC disabled — speexdsp not installed (pip install speexdsp)')
            consecutive_errors = 0
            last_send_time = 0
            oww_buffer = np.array([], dtype=np.int16)
            last_log_time = time.time()
            pre_record_buffer = deque()
            bg_rms = 0.5
            pre_roll_len = 0
            continuity_speech_frames = 0
            continuity_silence_frames = 0
            while self.running:
            try:
                with self.callbacks.get('get_state_lock', threading.Lock)():
                    mic_active = self.callbacks.get('get_state', lambda k, d: d)(('mic_active', False)
                    status = self.callbacks.get('get_state', lambda k, d: d)(('status', 'IDLE')
                data = stream.read(CHUNK, exception_on_overflow=False)
                consecutive_errors = 0
                raw_samples = np.frombuffer(data, dtype=SAMPLE_DTYPE)
                if CHANNELS == 1:
                    active_ch = raw_samples
                else:
                    ch_left = raw_samples[0::2]
                    ch_right = raw_samples[1::2]
                    _ch_sel = config.AUDIO_CHANNEL_SELECT
                    if _ch_sel == 'left':
                        active_ch = ch_left
                    elif _ch_sel == 'right':
                        active_ch = ch_right
                    else:
                        rms_left = np.mean(np.abs(ch_left))
                        rms_right = np.mean(np.abs(ch_right))
                        active_ch = ch_left if rms_left > rms_right else ch_right
                norm_full = active_ch.astype(np.float64) / FULL_SCALE
                if input_filters.enabled:
                    norm_full = input_filters.process(norm_full)
                norm_samples = np.clip(norm_full * config.AUDIO_GAIN, -1.0, 1.0)
                mic_16k = (np.clip(norm_full[::3] * config.AUDIO_OWW_GAIN, -1.0, 1.0) * 32767).astype(np.int16)
                if not recording:
                    pre_record_buffer.append(norm_samples)
                    pre_roll_len += len(norm_samples)
                    while pre_roll_len > 24000:
                        pre_roll_len -= len(pre_record_buffer.popleft())
                if recording:
                    query_buffer.extend(norm_samples)
                    if vad:
                        vad_frame_buf = np.concatenate([vad_frame_buf, mic_16k])
                        while len(vad_frame_buf) >= VAD_FRAME_SAMPLES:
                            frame = vad_frame_buf[:VAD_FRAME_SAMPLES]
                            vad_frame_buf = vad_frame_buf[VAD_FRAME_SAMPLES:]
                            try:
                                is_speech = vad.is_speech(frame.tobytes(), 16000)
                            except Exception:
                                is_speech = True
                            if is_speech:
                                vad_speech_frames += 1
                                vad_silence_frames = 0
                            elif vad_speech_frames >= config.VAD_MIN_SPEECH_FRAMES:
                                vad_silence_frames += 1
                    vad_cutoff = vad is not None and vad_speech_frames >= config.VAD_MIN_SPEECH_FRAMES and (vad_silence_frames >= config.VAD_SILENCE_FRAMES)
                    if vad_cutoff or time.time() > recording_end_time:
                        reason = 'VAD silence' if vad_cutoff else 'timeout'
                        logger.info(f'Recording ended ({reason}): {len(query_buffer)} samples, speech={vad_speech_frames} silence={vad_silence_frames}')
                        recording = False
                        audio_array = np.array(query_buffer, dtype=np.float64)
                        query_buffer = []
                        vad_speech_frames = 0
                        vad_silence_frames = 0
                        vad_frame_buf = np.array([], dtype=np.int16)
                        threading.Thread(target=process_llm, args=(audio_array, from_continuity), daemon=True).start()
                        from_continuity = False
                else:
                    with self.callbacks.get('get_state_lock', threading.Lock)():
                        is_processing = self.callbacks.get('get_state', lambda k, d: d)(('processing', False)
                        cooldown_until = self.callbacks.get('get_state', lambda k, d: d)(('wakeword_cooldown_until', 0)
                        continuity_until = self.callbacks.get('get_state', lambda k, d: d)(('continuity_until', 0)
                    if status == 'CONTINUITY' and time.time() > continuity_until:
                        with self.callbacks.get('get_state_lock', threading.Lock)():
                            self.callbacks.get('get_state_dict', lambda: {})()['status'] = 'IDLE'
                        self.callbacks.get('send_uart_command', lambda x: None)('APP:ASSISTANT')
                        logger.info('Continuity window expired. Returning to IDLE.')
                        status = 'IDLE'
                    is_speaking = _tts_active.is_set()
                    is_thinking = is_processing and (not is_speaking)
                    in_cooldown = is_thinking or is_speaking or (not is_processing and time.time() - self.callbacks.get('get_state', lambda k, d: d)(('last_wakeword_at', 0) <= 2.0) or (not is_processing and status != 'CONTINUITY' and (time.time() < cooldown_until))
                    if in_cooldown:
                        oww_buffer = np.array([], dtype=np.int16)
                        time.sleep(0.01)
                        continue
                    audio_16k = mic_16k
                    if status == 'CONTINUITY' and vad:
                        try:
                            vad.set_mode(2)
                        except:
                            pass
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
                            silence_threshold_frames = int(config.CONTINUITY_SILENCE_TIMEOUT * 33.3)
                            if continuity_silence_frames >= silence_threshold_frames:
                                logger.info('CONTINUITY: Silence threshold (%ss) reached. Exiting early.', config.CONTINUITY_SILENCE_TIMEOUT)
                                with self.callbacks.get('get_state_lock', threading.Lock)():
                                    self.callbacks.get('get_state_dict', lambda: {})()['status'] = 'IDLE'
                                self.callbacks.get('send_uart_command', lambda x: None)('APP:ASSISTANT')
                                continuity_silence_frames = 0
                                continue
                            if continuity_speech_frames >= 5:
                                tts_mute_s = config.WAKEWORD_TTS_MUTE_MS / 1000.0
                                if time.time() - _tts_finished_at < tts_mute_s:
                                    logger.debug('CONTINUITY: VAD fired within TTS mute window (%.1fs). Ignoring echo.', tts_mute_s)
                                    continuity_speech_frames = 0
                                else:
                                    logger.info('CONTINUITY: Speech detected via VAD (%s frames). Bypassing wake word.', continuity_speech_frames)
                                    with self.callbacks.get('get_state_lock', threading.Lock)():
                                        self.callbacks.get('get_state_dict', lambda: {})()['status'] = 'LISTENING'
                                    recording = True
                                    from_continuity = True
                                    recording_end_time = time.time() + config.LLM_RECORD_SECONDS
                                    query_buffer = list(np.concatenate(list(pre_record_buffer)) if pre_record_buffer else [])
                                    vad_speech_frames = continuity_speech_frames
                                    vad_silence_frames = 0
                                    continuity_silence_frames = 0
                                    vad_frame_buf = np.array([], dtype=np.int16)
                                    continuity_speech_frames = 0
                                    continue
                    if oww_model and status == 'IDLE':
                        if vad:
                            try:
                                vad.set_mode(config.VAD_AGGRESSIVENESS)
                            except:
                                pass
                        oww_buffer = np.concatenate((oww_buffer, audio_16k))
                        if len(oww_buffer) >= 1280:
                            chunk_oww = oww_buffer[:1280]
                            oww_buffer = oww_buffer[1280:]
                            if aec and is_speaking and (_aec_ref_buf is not None):
                                cleaned = []
                                for i in range(0, 1280, config.AEC_FRAME):
                                    mic_f = chunk_oww[i:i + config.AEC_FRAME].tobytes()
                                    with _aec_ref_lock:
                                        n = min(config.AEC_FRAME, len(_aec_ref_buf))
                                        if n == config.AEC_FRAME:
                                            ref_arr = np.array([_aec_ref_buf.popleft() for _ in range(config.AEC_FRAME)], dtype=np.int16)
                                        else:
                                            ref_arr = np.zeros(config.AEC_FRAME, dtype=np.int16)
                                    cleaned.extend(np.frombuffer(aec.process(mic_f, ref_arr.tobytes()), dtype=np.int16))
                                chunk_oww = np.array(cleaned, dtype=np.int16)
                            prediction = oww_model.predict(chunk_oww)
                            ww_threshold = config.WAKEWORD_THRESHOLD_BARGE_IN if is_speaking else config.WAKEWORD_THRESHOLD
                            for mdl, score in prediction.items():
                                if score > 0.01:
                                    logger.debug(f'OWW: {mdl} score={score:.3f} (threshold={ww_threshold:.2f})')
                                if score >= ww_threshold:
                                    logger.info(f'WAKE WORD: {mdl} ({score:.3f})')
                                    if is_speaking:
                                        interrupt_tts()
                                    self.callbacks.get('send_uart_command', lambda x: None)(f'WAKE|{mdl}')
                                    self.callbacks.get('send_uart_command', lambda x: None)('EMO:ALERT')
                                    with self.callbacks.get('get_state_lock', threading.Lock)():
                                        self.callbacks.get('get_state_dict', lambda: {})()['status'] = 'LISTENING'
                                    with self.callbacks.get('get_state_lock', threading.Lock)():
                                        self.callbacks.get('get_state_dict', lambda: {})()['last_wakeword_at'] = time.time()
                                    recording = True
                                    from_continuity = False
                                    recording_end_time = time.time() + config.LLM_RECORD_SECONDS
                                    query_buffer = list(np.concatenate(list(pre_record_buffer)) if pre_record_buffer else [])
                                    vad_speech_frames = 0
                                    vad_silence_frames = 0
                                    vad_frame_buf = np.array([], dtype=np.int16)
                                    oww_buffer = np.array([], dtype=np.int16)
                                    if hasattr(oww_model, 'reset'):
                                        oww_model.reset()
                                    logger.info(f'Recording started (via WAKE WORD)')
                                    break
                rms_now = np.sqrt(np.mean(norm_samples ** 2)) * 100.0
                with self.callbacks.get('get_state_lock', threading.Lock)():
                    avg_rms = self.callbacks.get('get_state', lambda k, d: d)(('avg_rms', 1.0)
                if rms_now <= bg_rms * 2.0:
                    bg_rms = bg_rms * 0.997 + rms_now * 0.003
                adj_rms = max(0.0, rms_now - max(0.15, bg_rms * 1.5))
                if adj_rms > 0.02:
                    avg_rms = avg_rms * 0.9 + adj_rms * 0.1
                else:
                    avg_rms = avg_rms * 0.95 + 0.1 * 0.05
                avg_rms = max(0.1, avg_rms)
                dynamic_multiplier = max(2.0, min(2000.0, 80.0 / avg_rms))
                intensity = int(min(100, adj_rms * dynamic_multiplier))
                with self.callbacks.get('get_state_lock', threading.Lock)():
                    self.callbacks.get('get_state_dict', lambda: {})()['avg_rms'] = avg_rms
                    self.callbacks.get('get_state_dict', lambda: {})()['audio_intensity'] = intensity
                now = time.time()
                is_speaking = _tts_active.is_set()
                should_send = mic_active or status in ('LISTENING', 'THINKING', 'SPEAKING', 'CONTINUITY')
                if should_send and (not is_speaking) and (now - last_send_time > 1.0 / config.AUDIO_UPDATE_HZ):
                    gain = max(0.5, min(8.0, 1.0 / avg_rms))
                    bins = calculate_spectrum_bins(norm_samples, mic_idx_bounds, gain=gain)
                    self.callbacks.get('send_uart_command', lambda x: None)(f"S{','.join(map(str, bins))}|A{intensity}")
                    last_send_time = now
                now = time.time()
                if now - last_log_time > 10.0:
                    bg_thresh = max(0.15, bg_rms * 1.5)
                    logger.debug(f'Mic RMS: {rms_now:.4f} (bg: {bg_rms:.4f}, thresh: {bg_thresh:.4f}, adj: {adj_rms:.4f}, intensity: {intensity})')
                    last_log_time = now
            except Exception as e:
                logger.error('Audio processing error: %s', e)
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    logger.error('Audio input failing repeatedly — reopening stream')
                    try:
                        stream.close()
                    except Exception:
                        pass
                    try:
                        stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, input_device_index=config.AUDIO_DEVICE_INDEX, frames_per_buffer=CHUNK)
                        consecutive_errors = 0
                        logger.info('Audio input stream reopened')
                    except Exception as reopen_err:
                        logger.error('Stream reopen failed: %s', reopen_err)
                time.sleep(0.5)