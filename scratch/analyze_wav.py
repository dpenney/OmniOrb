import wave
import numpy as np

try:
    with wave.open('/tmp/test_adapter.wav', 'rb') as w:
        params = w.getparams()
        fs = params.framerate
        data = w.readframes(w.getnframes())
        samples = np.frombuffer(data, dtype=np.int16)
        
        # If stereo, split and use left channel
        if params.nchannels == 2:
            samples = samples[::2]
            
        n = len(samples)
        print(f"Num mono samples: {n} (at {fs} Hz)")
        
        if n > 1000:
            # Perform FFT
            fft_data = np.fft.rfft(samples)
            fft_freq = np.fft.rfftfreq(n, d=1.0/fs)
            fft_magnitude = np.abs(fft_data)
            
            # Filter out frequencies < 20 Hz
            audible_mask = fft_freq >= 20.0
            audible_freq = fft_freq[audible_mask]
            audible_mag = fft_magnitude[audible_mask]
            
            # Find peak frequency in audible range
            peak_idx = np.argmax(audible_mag)
            peak_freq = audible_freq[peak_idx]
            peak_mag = audible_mag[peak_idx]
            
            print(f"Peak audible frequency: {peak_freq:.2f} Hz (magnitude: {peak_mag:.2f})")
            
            # Print top 5 audible frequencies
            sorted_indices = np.argsort(audible_mag)[::-1]
            print("Top 5 audible frequencies:")
            for i in range(5):
                idx = sorted_indices[i]
                print(f"  {audible_freq[idx]:.2f} Hz (magnitude: {audible_mag[idx]:.2f})")
        else:
            print("Not enough samples for FFT.")
except Exception as e:
    print(f"Error: {e}")
