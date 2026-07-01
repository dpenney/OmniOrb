import sys
from unittest.mock import MagicMock
import pytest
import os

# Ensure the pi directory is in the import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'pi')))

@pytest.fixture(scope="session", autouse=True)
def mock_hardware_modules():
    # Mock RPi.GPIO
    mock_gpio = MagicMock()
    sys.modules['RPi'] = MagicMock()
    sys.modules['RPi.GPIO'] = mock_gpio
    
    # Mock PyAudio
    mock_pyaudio = MagicMock()
    sys.modules['pyaudio'] = mock_pyaudio
    
    # Mock Serial
    mock_serial = MagicMock()
    sys.modules['serial'] = mock_serial

    # Mock openwakeword
    mock_oww = MagicMock()
    sys.modules['openwakeword'] = mock_oww
    sys.modules['openwakeword.model'] = mock_oww
    
    # Mock webrtcvad
    mock_vad = MagicMock()
    sys.modules['webrtcvad'] = mock_vad
    
    # Mock google.genai
    mock_genai = MagicMock()
    sys.modules['google.genai'] = mock_genai
    sys.modules['google.genai.types'] = mock_genai
    
    # Mock mem0
    mock_mem0 = MagicMock()
    sys.modules['mem0'] = mock_mem0
    
    yield
    
    # Optional cleanup
    pass
