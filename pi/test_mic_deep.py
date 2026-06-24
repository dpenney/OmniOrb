import pyaudio
import numpy as np

p = pyaudio.PyAudio()
idx = 44 # default device

try:
    info = p.get_device_info_by_index(idx)
    print(f"Recording from Device {idx}: {info.get('name')}")
    stream = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, input_device_index=idx, frames_per_buffer=1024)
    
    # record 2 seconds
    frames = []
    print("Recording 2 seconds of audio...")
    for _ in range(int(16000 / 1024 * 2)):
        data = stream.read(1024, exception_on_overflow=False)
        frames.append(data)
        
    stream.close()
    
    audio_data = b"".join(frames)
    samples = np.frombuffer(audio_data, dtype=np.int16)
    
    print(f"Recorded {len(samples)} samples.")
    print(f"Min value: {np.min(samples)}")
    print(f"Max value: {np.max(samples)}")
    print(f"Mean value: {np.mean(samples)}")
    print(f"Standard deviation: {np.std(samples)}")
    print("First 30 samples:", samples[:30].tolist())
except Exception as e:
    print(f"Error: {e}")

p.terminate()
