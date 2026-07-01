import pytest
import numpy as np
import threading
from unittest.mock import patch, MagicMock
import sys
import os

# Ensure the pi directory is in the import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'pi')))

# Mock hardware before importing assistant_brains
sys.modules['RPi'] = MagicMock()
sys.modules['RPi.GPIO'] = MagicMock()
sys.modules['pyaudio'] = MagicMock()
sys.modules['openwakeword'] = MagicMock()
sys.modules['openwakeword.model'] = MagicMock()
sys.modules['webrtcvad'] = MagicMock()
sys.modules['google.genai'] = MagicMock()
sys.modules['google.genai.types'] = MagicMock()
sys.modules['mem0'] = MagicMock()

# Import the module to be tested.
import assistant_brains
from assistant_brains import (
    get_fft_bounds,
    calculate_spectrum_bins,
    is_exit_command,
    _tts_clean,
    handle_uart_message,
    assistant_state,
    state_lock
)

# ---------------------------------------------------------
# Phase 1: Helper Functions
# ---------------------------------------------------------

def test_get_fft_bounds():
    chunk_size = 1024
    sample_rate = 48000
    bounds = get_fft_bounds(chunk_size, sample_rate)
    
    assert len(bounds) == 17
    # Bounds should be integers and within [0, chunk_size/2]
    assert all(isinstance(b, np.integer) or isinstance(b, int) for b in bounds)
    assert all(0 <= b <= chunk_size // 2 for b in bounds)

def test_calculate_spectrum_bins():
    chunk_size = 1024
    sample_rate = 48000
    bounds = get_fft_bounds(chunk_size, sample_rate)
    
    # Create a dummy signal (e.g., sine wave)
    t = np.linspace(0, chunk_size / sample_rate, chunk_size, endpoint=False)
    # A 1 kHz sine wave
    norm_samples = 0.5 * np.sin(2 * np.pi * 1000 * t)
    
    bins = calculate_spectrum_bins(norm_samples, bounds, gain=1.0)
    
    assert len(bins) == 16
    assert all(0 <= b <= 100 for b in bins)

def test_is_exit_command():
    assert is_exit_command("goodbye") is True
    assert is_exit_command("thanks") is True
    assert is_exit_command("that's all") is True
    assert is_exit_command("thank you") is True
    assert is_exit_command("stop") is True
    assert is_exit_command("dismiss") is True
    
    assert is_exit_command("hello there") is False
    assert is_exit_command("what time is it?") is False
    assert is_exit_command("") is False

def test_tts_clean():
    # Test markdown removal
    text = "Here is some **bold** and *italic* text."
    cleaned = _tts_clean(text)
    assert cleaned == "Here is some bold and italic text."
    
    # Test emoji retention (emojis are not stripped currently)
    text = "Hello! \U0001f60a Let's go! \U0001f680"
    cleaned = _tts_clean(text)
    assert "Hello!" in cleaned
    assert "Let's go!" in cleaned
    assert "\U0001f60a" in cleaned
    
    # Test blockquotes
    text = "> This is a quote"
    cleaned = _tts_clean(text)
    assert ">" in cleaned

# ---------------------------------------------------------
# Phase 2: UART State Machine
# ---------------------------------------------------------

@patch('assistant_brains._save_device_settings')
def test_handle_uart_message_geo(mock_save_settings):
    msg = "GEO:37.7749,-122.4194,America/Los_Angeles"
    handle_uart_message(msg)
    mock_save_settings.assert_called_once_with(37.7749, -122.4194, 'America/Los_Angeles')

@patch('assistant_brains.logger')
def test_handle_uart_message_vol(mock_logger):
    # Reset volume to known state
    assistant_brains._volume = 50
    
    msg = "VOL:25"
    handle_uart_message(msg)
    
    assert assistant_brains._volume == 25
    
    # Test volume max clamping (default max is 50)
    msg = "VOL:75"
    handle_uart_message(msg)
    
    assert assistant_brains._volume == 50

@patch('assistant_brains.send_uart_command')
def test_handle_uart_message_app_switching(mock_send):
    msg = "APP:ASSISTANT"
    handle_uart_message(msg)
    
    with state_lock:
        assert assistant_state["current_app"] == "ASSISTANT"
        assert assistant_state["mic_active"] is True
        assert assistant_state["radar_active"] is False
        
    msg = "APP:RADAR"
    handle_uart_message(msg)
    
    with state_lock:
        assert assistant_state["current_app"] == "RADAR"
        assert assistant_state["mic_active"] is False
        assert assistant_state["radar_active"] is True

    # Globe should fetch POIs
    msg = "APP:GLOBE"
    handle_uart_message(msg)
    
    with state_lock:
        assert assistant_state["current_app"] == "GLOBE"
        assert assistant_state["mic_active"] is False
        assert assistant_state["radar_active"] is False
        
    # We should verify it calls send_uart_command via thread. 
    # Since it spawns a thread, we might need a small sleep to let it execute.
    import time
    time.sleep(0.1)
    # Check that send_uart_command was called with GLOBE:POIS:
    called = any("GLOBE:POIS:" in call.args[0] for call in mock_send.call_args_list)
    assert called is True
