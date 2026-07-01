import pytest
import os
import json
from unittest.mock import patch, MagicMock, mock_open
import sys

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
sys.modules['rotary_encoder'] = MagicMock()

import assistant_brains
from assistant_brains import (
    get_local_ip,
    send_uart_command,
    _save_device_settings,
    _load_device_settings,
    ser
)

@patch('socket.socket')
def test_get_local_ip(mock_socket_class):
    mock_socket = MagicMock()
    mock_socket.getsockname.return_value = ('192.168.1.100', 80)
    mock_socket_class.return_value = mock_socket
    
    ip = get_local_ip()
    assert ip == '192.168.1.100'

@patch('socket.socket')
def test_get_local_ip_fallback(mock_socket_class):
    mock_socket = MagicMock()
    mock_socket.connect.side_effect = Exception("Network unreachable")
    mock_socket_class.return_value = mock_socket
    
    ip = get_local_ip()
    assert ip == '127.0.0.1'

def test_send_uart_command():
    assistant_brains.ser = MagicMock()
    assistant_brains.ser.is_open = True
    
    send_uart_command("TEST:HELLO")
    
    assistant_brains.ser.write.assert_called_once_with(b"TEST:HELLO\n")

@patch('builtins.open', new_callable=mock_open)
def test_save_device_settings(mock_file):
    _save_device_settings(37.7749, -122.4194, 'America/Los_Angeles')
    
    mock_file.assert_called_once_with(assistant_brains._SETTINGS_FILE, 'w')
    written_data = "".join(call.args[0] for call in mock_file().write.call_args_list)
    data = json.loads(written_data)
    
    assert data['lat'] == 37.7749
    assert data['lon'] == -122.4194
    assert data['tz'] == 'America/Los_Angeles'

@patch('os.path.exists')
@patch('builtins.open', new_callable=mock_open, read_data='{"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"}')
def test_load_device_settings(mock_file, mock_exists):
    mock_exists.return_value = True
    
    # Overwrite the in-memory globals first
    assistant_brains._device_settings['lat'] = 0
    assistant_brains._device_settings['lon'] = 0
    assistant_brains._device_settings['tz'] = ""
    
    _load_device_settings()
    
    assert assistant_brains._device_settings['lat'] == 40.7128
    assert assistant_brains._device_settings['lon'] == -74.0060
    assert assistant_brains._device_settings['tz'] == 'America/New_York'
