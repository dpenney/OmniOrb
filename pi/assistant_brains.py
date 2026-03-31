import requests
import serial
import logging
import os
import RPi.GPIO as GPIO
from flask import Flask, jsonify
from rotary_encoder import RotaryEncoder
import threading
from logging.handlers import RotatingFileHandler
import time
import numpy as np
import config
from dotenv import load_dotenv

# Load secret environment variables from .env
load_dotenv()
try:
    import pyaudio
except ImportError:
    pyaudio = None

app = Flask(__name__)

# State
assistant_state = {
    "zoom": 15,
    "last_event": None,
    "mic_active": True,  # Default to TRUE so it works even if sync fails
    "audio_intensity": 0
}

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(config.LOG_FILE, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Serial Port Configuration
# Try common Pi serial ports
serial_ports = config.SERIAL_PORTS
ser = None
ser_lock = threading.Lock()   # protects writes only
ser_read_lock = threading.Lock()  # protects reads only (TX and RX are independent)

for port in serial_ports:
    try:
        if os.path.exists(port):
            ser = serial.Serial(port, config.SERIAL_BAUD, timeout=1, write_timeout=1)
            logger.info(f"Connected to Serial Port: {port} (with timeouts)")
            break
    except Exception as e:
        logger.error(f"Failed to connect to {port}: {e}")

if not ser:
    logger.error("No valid serial port found!")

def serial_reader():
    """Background thread to read and log incoming serial data from ESP32"""
    global assistant_state
    while True:
        try:
            if ser and ser.is_open:
                # Read ALL available lines in a burst to prevent buffer lag
                # Uses ser_read_lock (separate from write lock — TX/RX are independent)
                while ser.in_waiting > 0:
                    with ser_read_lock:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()

                    if not line:
                        break

                    # Mode Synchronization
                    if "APP:" in line:
                        # Extract everything after "APP:" robustly
                        app_mode = line.split("APP:", 1)[1].split()[0]
                        if app_mode == "ASSISTANT":
                            assistant_state["mic_active"] = True
                            logger.info("Assistant mode confirmed: Processing audio")
                        else:
                            assistant_state["mic_active"] = False
                            logger.info(f"App mode switched to {app_mode}: Pausing audio")
                    else:
                        # Log anything else from the ESP32 if it's not a common debug line
                        if not any(x in line for x in ["Gesture", "Touch", "Remote"]):
                            logger.info(f"[ESP32] {line}")
                        else:
                            logger.debug(f"[ESP32 Debug] {line}")
        except Exception as e:
            logger.error(f"Serial read error: {e}")
            time.sleep(1) # Wait on error
            
        time.sleep(config.SERIAL_READER_SLEEP)

def audio_processor():
    """Background thread to capture audio and send intensity to ESP32"""
    if not pyaudio:
        logger.error("pyaudio not installed. Audio reactivity disabled.")
        return

    CHUNK = config.AUDIO_CHUNK
    FORMAT = pyaudio.paInt32  # I2S MEMS mics require 32-bit format
    CHANNELS = config.AUDIO_CHANNELS
    RATE = config.AUDIO_RATE
    DEVICE_INDEX = config.AUDIO_DEVICE_INDEX 

    p = pyaudio.PyAudio()
    
    try:
        stream = p.open(format=FORMAT,
                        channels=CHANNELS,
                        rate=RATE,
                        input=True,
                        input_device_index=DEVICE_INDEX,
                        frames_per_buffer=CHUNK)
        logger.info("Audio stream opened for I2S microphone (16-bit)")
    except Exception as e:
        logger.error(f"Failed to open audio stream: {e}")
        return

    last_send_time = 0
    last_log_time = time.time()
    while True:
        try:
            # Only process audio if the Assistant screen is active on the ESP32
            if not assistant_state.get("mic_active", False):
                time.sleep(0.1)
                continue

            data = stream.read(CHUNK, exception_on_overflow=False)
            # Diagnostic: Calculate RMS for both stereo slots to find where the mic is!
            raw_samples = np.frombuffer(data, dtype=np.int32)  # Match 32-bit format
            ch_left  = raw_samples[0::2]
            ch_right = raw_samples[1::2]
            
            # Normalize 32-bit samples to float64 ±1.0 range
            norm_samples = ch_left.astype(np.float64) / 2147483648.0

            # Simple RMS-based intensity (scaled 0-100)
            rms_left  = np.sqrt(np.mean(norm_samples**2)) * 100.0
            
            # Auto-gain moving average (EMA)
            avg_rms = assistant_state.get("avg_rms", 1.0)
            avg_rms = (avg_rms * 0.95) + (rms_left * 0.05)
            assistant_state["avg_rms"] = max(0.1, avg_rms) # Prevent div by 0

            # Diagnostic log every 10 seconds 
            now = time.time()
            if now - last_log_time > 10.0:
                logger.debug(f"Microphone RMS: {rms_left:.4f} (Avg: {avg_rms:.4f})")
                last_log_time = now

            # Floor subtraction (user calibrated to 0.03)
            adj_rms = max(0.0, rms_left - 0.03)
            
            # ─── Auto-Gain Control (AGC) for Central Orb ───
            if adj_rms > 0.02:
                # Faster Attack: 10% new data per frame (~1 second adapt)
                avg_rms = (avg_rms * 0.90) + (adj_rms * 0.10)
            else:
                # Fast Decay: Drop `avg_rms` down to 0.10 during silence 
                # This allows the multiplier to relax up to 400x for the next quiet sound
                avg_rms = (avg_rms * 0.95) + (0.10 * 0.05)
                
            assistant_state["avg_rms"] = max(0.1, avg_rms)
            
            dynamic_multiplier = 80.0 / assistant_state["avg_rms"]
            dynamic_multiplier = max(2.0, min(2000.0, dynamic_multiplier))
            
            intensity = int(min(100, adj_rms * dynamic_multiplier)) 
            assistant_state["audio_intensity"] = intensity
            
            # ─── FFT Spectrum Analysis ───
            fft_data = np.abs(np.fft.rfft(norm_samples)) 
            
            # Bucket into 16 bins for the ESP32
            num_bins = 16
            chunk_size = len(fft_data) // num_bins
            bins = []
            
            # Calculate a dynamic gain factor based on moving average volume
            # Amplifies quiet sounds, dampens very loud sounds
            gain = 1.0 / assistant_state["avg_rms"]
            gain = max(0.5, min(10.0, gain)) # Clamp the gain multiplier
            
            for i in range(num_bins):
                start = i * chunk_size
                end = (i + 1) * chunk_size
                mag = np.mean(fft_data[start:end])
                
                # Apply Dynamic Gain, log scale, and cap at 100
                scaled_mag = int(min(100, np.log1p(mag * 200.0 * gain) * 15.0))
                bins.append(scaled_mag)

            
            bin_string = ",".join(map(str, bins))

            # Broadcast to ESP32 to prevent buffer bloat
            now = time.time()
            if now - last_send_time > (1.0 / config.AUDIO_UPDATE_HZ):
                # Always send spectrum and intensity combined if the app is active
                send_uart_command(f"S{bin_string}|A{intensity}")
                
                last_send_time = now
                
        except Exception as e:
            logger.error(f"Audio processing error: {e}")
            # Try to recover stream if it's a known error
            time.sleep(0.5)

    stream.stop_stream()
    stream.close()
    p.terminate()

def send_uart_command(cmd):
    try:
        with ser_lock:
            if ser and ser.is_open:
                ser.write(f"{cmd}\n".encode())
                # Only log non-audio commands (like Zoom) to reduce chattiness
                if not cmd.startswith("A"):
                    logger.info(f"Sent UART Command: {cmd}")
    except Exception as e:
        logger.error(f"Error sending UART command: {e}")

def on_encoder_event(event, direction, value):
    global assistant_state
    assistant_state["last_event"] = f"{event}_{direction}"
    logger.info(f"Encoder Event: {event} | Direction: {direction}")
    
    if event == "rotate":
        if direction == "CW":
            assistant_state["zoom"] = max(5, assistant_state["zoom"] - 1)
            send_uart_command("Z+")
        else:
            assistant_state["zoom"] = min(250, assistant_state["zoom"] + 1)
            send_uart_command("Z-")
    
    logger.debug(f"Assistant State: {assistant_state}")

# Initialize Hardware
try:
    GPIO.setmode(GPIO.BCM)
    # Software GND
    if config.PIN_SFT_GND:
        GPIO.setup(config.PIN_SFT_GND, GPIO.OUT)
        GPIO.output(config.PIN_SFT_GND, GPIO.LOW)
        logger.info(f"Software GND enabled on GPIO {config.PIN_SFT_GND}")
    
    # Encoder pins from config
    encoder = RotaryEncoder(
        clk_pin=config.PIN_ROTARY_CLK, 
        dt_pin=config.PIN_ROTARY_DT, 
        sw_pin=config.PIN_ROTARY_SW, 
        callback=on_encoder_event
    )
    logger.info(f"Rotary Encoder initialized on GPIO {config.PIN_ROTARY_CLK} (CLK) and {config.PIN_ROTARY_DT} (DT)")
except Exception as e:
    logger.error(f"Hardware init failed: {e}")

@app.route('/status')
def get_status():
    return jsonify(assistant_state)

@app.route('/mic/start')
def mic_start():
    assistant_state["mic_active"] = True
    return jsonify({"status": "mic_active"})

@app.route('/mic/stop')
def mic_stop():
    assistant_state["mic_active"] = False
    return jsonify({"status": "mic_idle"})

if __name__ == "__main__":
    # Start Serial Reader thread before sending SYNC? so the response isn't missed
    reader_thread = threading.Thread(target=serial_reader, daemon=True)
    reader_thread.start()
    logger.info("Serial reader thread started")

    # Request current app mode from ESP32 in case it's already running
    time.sleep(0.5)
    send_uart_command("SYNC?")

    # Start Audio Processor thread
    audio_thread = threading.Thread(target=audio_processor, daemon=True)
    audio_thread.start()
    logger.info("Audio processor thread started")

    # Start Flask API in a separate thread
    # This allows the rotary encoder to continue processing in the main thread
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT)
