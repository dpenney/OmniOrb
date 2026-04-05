import pyaudio, numpy as np, time

CHUNK = 1024; RATE = 48000; BINS = 16

p = pyaudio.PyAudio()
stream = p.open(format=pyaudio.paInt32, channels=2, rate=RATE, input=True,
                input_device_index=1, frames_per_buffer=CHUNK)

freq_bounds = np.geomspace(80, 12000, 17)
idx_bounds  = np.clip((freq_bounds * CHUNK / RATE).astype(int), 0, CHUNK // 2)

def capture_bins(seconds):
    all_bins = []
    for _ in range(int(RATE / CHUNK * seconds)):
        data = stream.read(CHUNK, exception_on_overflow=False)
        s = np.frombuffer(data, dtype=np.int32)
        r = s[1::2].astype(np.float64) / 2147483648.0
        fft = np.abs(np.fft.rfft(r))
        row = []
        for i in range(BINS):
            st = idx_bounds[i]
            en = max(st + 1, idx_bounds[i + 1])
            row.append(float(np.mean(fft[st:en])))
        all_bins.append(row)
    return np.array(all_bins)

print("SILENCE (10s)...", flush=True)
silence = capture_bins(10)
s_p95 = np.percentile(silence, 95, axis=0)
print("Silence p95:", np.round(s_p95, 4).tolist(), flush=True)

print("Start music now - capturing in 3s...", flush=True)
time.sleep(3)
print("MUSIC (10s)...", flush=True)
music = capture_bins(10)
m_p95 = np.percentile(music, 95, axis=0)
print("Music p95:  ", np.round(m_p95, 4).tolist(), flush=True)

floors = np.round(s_p95 * 1.3, 4)
print("\nRecommended _fft_floor:", floors.tolist(), flush=True)
print("Dynamic range (music/silence p95):", np.round(m_p95 / np.maximum(s_p95, 1e-9), 1).tolist(), flush=True)

stream.close()
p.terminate()
