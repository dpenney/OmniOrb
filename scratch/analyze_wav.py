import numpy as np
import wave
import sys

try:
    w = wave.open('test.wav', 'rb')
    data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int32)
    left = data[0::2]
    right = data[1::2]
    print(f"L Mean Abs: {np.mean(np.abs(left))}")
    print(f"R Mean Abs: {np.mean(np.abs(right))}")
    print(f"L Max: {np.max(np.abs(left))}")
    print(f"R Max: {np.max(np.abs(right))}")
except Exception as e:
    print(f"Error: {e}")
