import os
import time
import json
import struct
import logging
import threading
import requests
from flask import Flask, jsonify, request

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("adsb_proxy_sidecar")

try:
    import zstandard as zstd
    ZSTD_AVAILABLE = True
except ImportError:
    ZSTD_AVAILABLE = False
    logger.error("zstandard library not found. ADS-B decoding will fail.")

app = Flask(__name__)

# ── Settings ──────────────────────────────────────────────────────────────────
STATUS_URL    = "http://localhost:5000/status"
POLL_INTERVAL = 10.0
PORT          = 5050

# Default bounding box — New York City (matches ESP32 HOME_LAT/HOME_LON)
# This is only used until the ESP32 sends its actual coordinates via ?lat=&lon=
DEFAULT_BOX   = "40.3,41.1,-74.4,-73.6"

# ── Shared State ──────────────────────────────────────────────────────────────
latest_flights = {"now": 0, "aircraft": []}
radar_active   = True   # assume radar on boot; ESP32 always starts on radar screen
current_box    = DEFAULT_BOX
box_lock       = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_box(lat, lon, radius_deg_lat=0.3, radius_deg_lon=0.4):
    """Return a minLat,maxLat,minLon,maxLon string centered on (lat, lon)."""
    return (f"{lat - radius_deg_lat:.4f},"
            f"{lat + radius_deg_lat:.4f},"
            f"{lon - radius_deg_lon:.4f},"
            f"{lon + radius_deg_lon:.4f}")

def update_location(lat, lon):
    global current_box
    new_box = make_box(lat, lon)
    with box_lock:
        if new_box != current_box:
            current_box = new_box
            logger.info(f"Bounding box updated for ({lat:.4f}, {lon:.4f}) → {new_box}")

# ── Background ADS-B Fetcher ──────────────────────────────────────────────────
#
# binCraft struct layout (stride = 112 bytes, all little-endian, __packed__):
#   0   uint32  addr         ICAO = bits 0-23
#   4   int32   seen         ms since last message / 100
#   8   int32   lon          degrees * 1e6
#  12   int32   lat          degrees * 1e6
#  16   int16   baro_rate    ft/min / 8   (NOT altitude)
#  20   int16   baro_alt     feet / 25
#  34   int16   gs           knots * 10
#  40   int16   track        degrees * 90
#  68   uint8   airground nibble (& 0x0F): 0=unknown/airborne, 1=ground, 2=airborne
#  73   uint8   validity bits: bit3=callsign, bit4=baro_alt, bit6=position, bit7=gs
#  74   uint8   validity bits: bit3=track
#  78   8 bytes callsign[8]  null-padded ASCII
#  86   2 bytes dbFlags      (skip)
#  88   4 bytes typeCode[4]  null-padded ASCII

def fetch_adsb():
    global latest_flights, radar_active

    session = requests.Session()
    session.headers.update({
        "Referer":           "https://globe.adsbexchange.com/",
        "X-Requested-With":  "XMLHttpRequest",
        "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    while True:
        try:
            # 1. Poll main assistant service for radar_active state
            try:
                resp = requests.get(STATUS_URL, timeout=2)
                if resp.status_code == 200:
                    radar_active = resp.json().get("radar_active", radar_active)
            except Exception:
                pass  # keep current radar_active value — don't flip to False on transient errors

            if not radar_active:
                time.sleep(5)
                continue

            # 2. Fetch from ADSBExchange using the current (possibly location-updated) box
            with box_lock:
                fetch_box = current_box

            url = f"https://globe.adsbexchange.com/re-api/?binCraft&zstd&box={fetch_box}"
            r = session.get(url, timeout=10)

            if r.status_code == 200 and ZSTD_AVAILABLE:
                dctx   = zstd.ZstdDecompressor()
                data   = dctx.decompress(r.content)
                stride = struct.unpack_from('<I', data, 8)[0]  # elementSize at header offset 8

                aircraft_list = []
                offset = stride  # header occupies one stride; first record starts here
                while offset + stride <= len(data):
                    rec = data[offset : offset + stride]
                    offset += stride

                    # ICAO address (lower 24 bits of uint32 at offset 0)
                    hex_raw = struct.unpack_from('<I', rec, 0)[0]
                    if hex_raw == 0:
                        continue  # unused slot
                    icao = f"{(hex_raw & 0xFFFFFF):06x}".upper()

                    # On-ground detection: byte 68 lower nibble, value 1 = on ground
                    on_ground = (rec[68] & 0x0F) == 1
                    if on_ground:
                        continue

                    # Validity bits
                    byte73 = rec[73]
                    position_valid = bool(byte73 & 0x40)  # bit 6
                    baro_alt_valid = bool(byte73 & 0x10)  # bit 4
                    gs_valid       = bool(byte73 & 0x80)  # bit 7
                    byte74 = rec[74]
                    track_valid    = bool(byte74 & 0x08)  # bit 3

                    if not position_valid:
                        continue

                    lon = struct.unpack_from('<i', rec,  8)[0] / 1e6
                    lat = struct.unpack_from('<i', rec, 12)[0] / 1e6

                    # Altitude: offset 20 (int16), stored as feet / 25
                    baro_alt_raw = struct.unpack_from('<h', rec, 20)[0]
                    alt_baro = baro_alt_raw * 25 if baro_alt_valid else None

                    # Ground speed: offset 34 (int16), stored as knots * 10
                    gs_raw = struct.unpack_from('<h', rec, 34)[0]
                    gs = round(gs_raw / 10.0, 1) if gs_valid else None

                    # Track: offset 40 (int16), stored as degrees * 90
                    trk_raw = struct.unpack_from('<h', rec, 40)[0]
                    track = round((trk_raw / 90.0) % 360.0, 1) if track_valid else None

                    # Callsign: 8 bytes at offset 78 (NOT 10 — would bleed into dbFlags)
                    flight = rec[78:86].decode('ascii', errors='ignore').strip('\x00').strip()

                    # Aircraft type: 4 bytes at offset 88
                    ac_type = rec[88:92].decode('ascii', errors='ignore').strip('\x00').strip()

                    aircraft_list.append({
                        "hex":      icao,
                        "flight":   flight,
                        "lat":      lat,
                        "lon":      lon,
                        "alt_baro": alt_baro,
                        "gs":       gs,
                        "track":    track,
                        "type":     ac_type,
                    })

                latest_flights = {"now": time.time(), "aircraft": aircraft_list}
                logger.info(f"Updated {len(aircraft_list)} aircraft (box={fetch_box})")

        except Exception as e:
            logger.error(f"Fetcher error: {e}")

        time.sleep(POLL_INTERVAL)

# ── Flask Routes ──────────────────────────────────────────────────────────────

@app.route('/data/aircraft.json')
def get_aircraft():
    """Serve aircraft data. Accepts optional ?lat=&lon= from the ESP32 to center the search area."""
    try:
        lat = request.args.get('lat')
        lon = request.args.get('lon')
        if lat and lon:
            update_location(float(lat), float(lon))
    except Exception as e:
        logger.warning(f"Bad location params: {e}")

    return jsonify(latest_flights)

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"Starting private ADS-B sidecar on port {PORT}...")
    threading.Thread(target=fetch_adsb, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
