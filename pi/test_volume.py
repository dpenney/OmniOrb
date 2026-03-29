#!/usr/bin/env python3
import pyaudio
import numpy as np
import time
import sys

# --- CALIBRATION SETTINGS ---
# Tweak these values and run the script until you get a good response!
FLOOR = 0.03          # Subtract this from the raw RMS (aim for slightly above silence RMS)
TARGET_INTENSITY = 80.0 # The Auto-Gain Control will try to keep average volume around this level

AUDIO_DEVICE_INDEX = 0
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
CHUNK = 4096

print("Starting Volume Calibration Tool...")
print("Speak into the microphone to see the real-time values.")
print("Press Ctrl+C to stop.\n")

p = pyaudio.PyAudio()

try:
    stream = p.open(format=pyaudio.paInt32,
                    channels=AUDIO_CHANNELS,
                    rate=AUDIO_RATE,
                    input=True,
                    input_device_index=AUDIO_DEVICE_INDEX,
                    frames_per_buffer=CHUNK)
except Exception as e:
    print(f"Failed to open audio stream: {e}")
    sys.exit(1)

try:
    avg_rms = 1.0
    while True:
        data = stream.read(CHUNK, exception_on_overflow=False)
        raw_samples = np.frombuffer(data, dtype=np.int32)
        
        # Audio is 2 channels, INMP441 uses LEFT channel
        ch_left = raw_samples[0::2]
        
        # Normalize 32-bit samples to float64 ±1.0 range
        norm_samples = ch_left.astype(np.float64) / 2147483648.0
        
        # Calculate raw 0-100 RMS
        rms_left = np.sqrt(np.mean(norm_samples**2)) * 100.0
        
        # Apply strict noise floor
        adj_rms = max(0.0, rms_left - FLOOR)

        # ─── Auto-Gain Control (AGC) ───
        # Only update the moving average if we are actually hearing something
        if adj_rms > 0.02:
            # Faster Attack: 10% new data per frame (~1 second adapt)
            avg_rms = (avg_rms * 0.90) + (adj_rms * 0.10)
        else:
            # Fast Decay: Drop `avg_rms` down to 0.10 during silence 
            # This allows the multiplier to relax up to 400x for the next quiet sound
            avg_rms = (avg_rms * 0.95) + (0.10 * 0.05)

        # Prevent divide-by-zero
        avg_rms = max(0.1, avg_rms)
        
        # Target an average intensity (default ~80), meaning continuous music rides high
        # Determine the dynamic multiplier
        dynamic_multiplier = TARGET_INTENSITY / avg_rms
        
        # Clamp multiplier to prevent insane boosting of static
        # Raised max to 2000 to easily capture whisper-quiet peaks
        dynamic_multiplier = max(2.0, min(2000.0, dynamic_multiplier))

        # Final scaled intensity
        intensity = int(min(100, adj_rms * dynamic_multiplier))
        
        # Draw a retro ASCII bar graph
        bar_len = intensity // 2
        bar = "#" * bar_len + "-" * (50 - bar_len)
        
        # Print the values clearly 
        sys.stdout.write(f"\rRaw: {rms_left:06.4f} | Avg: {avg_rms:06.4f} | Mult: {dynamic_multiplier:05.1f} | A: {intensity:03d} [{bar}]")
        sys.stdout.flush()
        
        time.sleep(0.05)

except KeyboardInterrupt:
    print("\n\nCalibration stopped.")

finally:
    stream.stop_stream()
    stream.close()
    p.terminate()
