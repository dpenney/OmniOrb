import pyaudio
import numpy as np

p = pyaudio.PyAudio()
stream = p.open(format=pyaudio.paInt16, channels=2, rate=48000, input=True, input_device_index=1, frames_per_buffer=1024)

print("Capturing 1 second of audio (16-bit, 2 channels)...")
data = stream.read(48000, exception_on_overflow=False)
samples = np.frombuffer(data, dtype=np.int16)
left = samples[0::2]
right = samples[1::2]

print("Left RMS: ", np.sqrt(np.mean(left.astype(np.float64)**2)))
print("Right RMS:", np.sqrt(np.mean(right.astype(np.float64)**2)))

stream.close()
p.terminate()
