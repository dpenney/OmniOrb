import pyaudio
import numpy as np

p = pyaudio.PyAudio()

for fmt in [pyaudio.paInt32, pyaudio.paInt16]:
    print(f"\n--- Testing Format: {fmt} ---")
    try:
        stream = p.open(format=fmt, channels=2, rate=48000, input=True, input_device_index=1, frames_per_buffer=1024)
        data = stream.read(48000, exception_on_overflow=False)
        dtype = np.int32 if fmt == pyaudio.paInt32 else np.int16
        samples = np.frombuffer(data, dtype=dtype)
        left = samples[0::2]
        right = samples[1::2]
        
        print("Left RMS:  ", np.sqrt(np.mean(left.astype(np.float64)**2)))
        print("Right RMS: ", np.sqrt(np.mean(right.astype(np.float64)**2)))
        print("Left sample slice: ", left[:10])
        print("Right sample slice:", right[:10])
        
        stream.close()
    except Exception as e:
        print("Failed:", e)

p.terminate()
