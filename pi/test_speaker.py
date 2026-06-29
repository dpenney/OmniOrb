#!/usr/bin/env python3
import time
import sys
import os
import subprocess
import wave
import numpy as np

# Use virtualenv's python packages if running on Pi
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

# Configure paths
_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_DIR)
import config

def play_tone(frequency=440, duration=3.0, sample_rate=16000):
    print(f"Generating {frequency}Hz sine wave tone for {duration} seconds...")
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    # Generate sine wave in range [-32768, 32767]
    tone = np.sin(frequency * t * 2 * np.pi) * 16384  # 50% max volume
    audio_data = tone.astype(np.int16).tobytes()

    wav_path = "/tmp/speaker_test.wav"
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)

    print(f"Playing via aplay on device: {config.APLAY_DEVICE}...")
    try:
        subprocess.run([
            "aplay", "-D", config.APLAY_DEVICE,
            "-f", "S16_LE", "-r", str(sample_rate), "-c", "1",
            wav_path
        ], check=True)
    except Exception as e:
        print(f"Failed to play audio with aplay: {e}")
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

def main():
    if GPIO is None:
        print("Error: RPi.GPIO not available.")
        sys.exit(1)

    # Set up GPIO 26 (PIN_AMP_MUTE)
    amp_pin = 26
    print(f"Using BCM {amp_pin} based on PCB layout.")

    print(f"Initializing GPIO pin {amp_pin}...")
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(amp_pin, GPIO.OUT)

    print(f"Enabling Amplifier (Setting GPIO {amp_pin} HIGH)...")
    GPIO.output(amp_pin, GPIO.HIGH)

    # Allow amp to power up
    time.sleep(0.1)

    try:
        # Play test tone
        play_tone(frequency=440, duration=2.0)
        time.sleep(0.5)
        play_tone(frequency=880, duration=1.0)
    finally:
        print(f"Disabling Amplifier (Setting GPIO {amp_pin} LOW)...")
        GPIO.output(amp_pin, GPIO.LOW)
        GPIO.cleanup()
        print("Cleanup completed.")

if __name__ == "__main__":
    main()
