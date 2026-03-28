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
    "mic_active": False,
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
ser_lock = threading.Lock()

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
    while True:
        try:
            with ser_lock:
                if ser and ser.is_open:
                    if ser.in_waiting > 0:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                        if line:
                            logger.info(f"[ESP32] {line}")
        except Exception as e:
            logger.error(f"Serial read error: {e}")
        time.sleep(config.SERIAL_READER_SLEEP)

def audio_processor():
    """Background thread to capture audio and send intensity to ESP32"""
    if not pyaudio:
        logger.error("pyaudio not installed. Audio reactivity disabled.")
        return

    CHUNK = config.AUDIO_CHUNK
    FORMAT = pyaudio.paInt16
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
        logger.info("Audio stream opened for I2S microphone")
    except Exception as e:
        logger.error(f"Failed to open audio stream: {e}")
        return

    last_send_time = 0
    last_log_time = 0
    while True:
        try:
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio_data = np.frombuffer(data, dtype=np.int16)
            
            # Simple RMS-based intensity
            # Use float32 to avoid overflow when squaring int16
            rms = np.sqrt(np.mean(audio_data.astype(np.float32)**2))
            
            # Diagnostic log every 10 seconds to reduce chattiness
            now = time.time()
            if now - last_log_time > 10.0:
                logger.info(f"Microphone RMS: {rms:.2f}")
                last_log_time = now

            # Floor subtraction and 8x boost for high reactivity
            # Silence is ~4.0 based on logs.
            adj_rms = max(0, rms - 4.0)
            intensity = int(min(100, adj_rms * 8.0)) 
            
            assistant_state["audio_intensity"] = intensity
            
            # Broadcast to ESP32 to prevent buffer bloat
            now = time.time()
            if now - last_send_time > (1.0 / config.AUDIO_UPDATE_HZ):
                # Delta threshold: only send if intensity changed significantly
                last_sent = assistant_state.get("last_intensity_sent", 0)
                diff = abs(intensity - last_sent)
                
                # Send if significant change OR if we need to return to 0
                if diff >= 3 or (intensity == 0 and last_sent > 0):
                    send_uart_command(f"A{intensity}")
                    assistant_state["last_intensity_sent"] = intensity
                
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
    # Start Serial Reader thread
    reader_thread = threading.Thread(target=serial_reader, daemon=True)
    reader_thread.start()
    logger.info("Serial reader thread started")

    # Start Audio Processor thread
    audio_thread = threading.Thread(target=audio_processor, daemon=True)
    audio_thread.start()
    logger.info("Audio processor thread started")

    # Start Flask API in a separate thread
    # This allows the rotary encoder to continue processing in the main thread
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT)
