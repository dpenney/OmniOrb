import json
import os
import logging

logger = logging.getLogger(__name__)

_POI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "globe_pois.json")

def hex_to_rgb565(hex_str):
    """Convert #RRGGBB to RGB565 (uint16_t)."""
    try:
        hex_str = hex_str.lstrip('#')
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        # RGB565: 5 bits Red, 6 bits Green, 5 bits Blue
        return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    except Exception:
        return 0xFFFF  # Fallback to white

def load_pois():
    """Load POIs from JSON file."""
    if not os.path.exists(_POI_FILE):
        # Default POIs (Barksdale and Bruno's Lair)
        defaults = [
            {"name": "Barksdale AFB", "lat": 32.4960, "lon": -93.6728, "color": "#0000FF"},
            {"name": "Bruno's Lair", "lat": 37.8499, "lon": -122.1157, "color": "#FF0000"}
        ]
        save_pois(defaults)
        return defaults
    try:
        with open(_POI_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading POIs: {e}")
        return []

def save_pois(pois):
    """Save POIs to JSON file."""
    try:
        with open(_POI_FILE, 'w') as f:
            json.dump(pois, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving POIs: {e}")

def get_pois_serial():
    """Return a compact string for ESP32 UART: lat,lon,color|lat,lon,color"""
    pois = load_pois()
    parts = []
    for p in pois:
        color_565 = hex_to_rgb565(p.get("color", "#FFFFFF"))
        parts.append(f"{p['lat']:.4f},{p['lon']:.4f},{color_565}")
    return "|".join(parts)
