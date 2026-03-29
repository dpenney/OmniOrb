import pyaudio
import numpy as np
import time

p = pyaudio.PyAudio()

print("\n--- Audio Device Probe ---")
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    name = info.get('name')
    inputs = info.get('maxInputChannels')
    print(f"Index {i}: {name} (Inputs: {inputs})")

    if inputs > 0:
        for rate in [16000, 44100, 48000]:
            for fmt in [pyaudio.paInt16, pyaudio.paInt32]:
                fmt_name = "Int16" if fmt == pyaudio.paInt16 else "Int32"
                dtype = np.int16 if fmt == pyaudio.paInt16 else np.int32
                
                try:
                    stream = p.open(format=fmt, channels=1, rate=rate, input=True, input_device_index=i, frames_per_buffer=1024)
                    print(f"  Testing {rate}Hz {fmt_name}...")
                    
                    # Read 10 chunks to check for data
                    has_data = False
                    for _ in range(10):
                        data = stream.read(1024, exception_on_overflow=False)
                        samples = np.frombuffer(data, dtype=dtype)
                        rms = np.sqrt(np.mean(samples.astype(np.float32)**2))
                        if rms > 0.001:
                            has_data = True
                            print(f"    SUCCESS! RMS: {rms:.2f}")
                            break
                    
                    stream.stop_stream()
                    stream.close()
                    
                    if has_data:
                        print(f"    ==> WORKING CONFIG: Index {i}, Rate {rate}, Format {fmt_name}")
                except Exception as e:
                    pass

p.terminate()
print("\n--- Probe Complete ---")
