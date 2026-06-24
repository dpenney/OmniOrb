# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**OmniOrb** is a multi-functional interactive hub for the Waveshare ESP32-S3 touch LCD displays. It integrates three main applications: an ADS-B radar display, an AI voice assistant (NEXUS), and a smart clock. The system has a twin-core architecture where the ESP32 handles display/UI and the Raspberry Pi backend provides AI capabilities and audio processing.

### Hardware Targets
- Primary: Waveshare ESP32-S3-Knob-Touch-LCD-1.28 (round display)
- Alternative: ESP32-S3-Touch-LCD-2.8C (square 480×480 display)

## Build Commands

### ESP32 Firmware
```bash
# Build the firmware
pio run

# Upload to device via USB
pio run --target upload

# Upload via OTA (device must be on network)
# Edit platformio.ini upload_port first, then:
pio run --target upload

# Monitor serial output
pio run --target monitor
```

### Raspberry Pi Backend
```bash
# Install dependencies (run from pi/ directory)
./install.sh

# Manual service control
sudo systemctl start assistant.service
sudo systemctl stop assistant.service
sudo systemctl restart assistant.service

# View logs
tail -f ~/assistant/assistant.log
```

## Architecture

### ESP32 Firmware (C++/Arduino)
The ESP32 firmware runs a dual-core FreeRTOS application:

**Core 0 (Background):**
- HTTP fetch task for ADS-B aircraft data
- Runs independently without blocking UI

**Core 1 (Main Loop):**
- Radar sweep animation and blip painting
- Touch gesture processing
- LVGL rendering for clock view
- UART communication with Pi

**Key Design Patterns:**
- **Mutex-protected shared data**: `ac_mutex` protects the `aircraft[]` array accessed by both cores
- **Paint-on-sweep**: Aircraft blips are painted only when the radar sweep arm crosses their bearing angle, creating authentic PPI radar behavior
- **VSYNC synchronization**: LVGL buffer flushes are synchronized with the display's VSYNC signal to prevent tearing
- **View switching**: Horizontal swipes navigate between Radar → Assistant → Clock apps

### View System
Three main views that can be switched via horizontal swipes:

1. **Radar View (APP_RADAR)**: PPI-style ADS-B aircraft tracking with rotating sweep
2. **Assistant View (APP_ASSISTANT)**: AI assistant "iris" visualization with audio reactivity
3. **Clock View (APP_CLOCK)**: LVGL-based analog/digital hybrid clock

Each view has its own `.cpp/.h` files with static methods for `init()`, `show()`, `hide()`, and `update()`.

### Raspberry Pi Backend (Python)
The Pi runs a Flask server (`assistant_brains.py`) that:
- Captures audio from I2S microphone and sends intensity levels to ESP32 via UART
- Processes rotary encoder input (zoom control)
- Provides REST API for assistant state
- Runs three daemon threads: serial reader, audio processor, and Flask server

**Communication Protocol:**
- `Z+` / `Z-`: Zoom in/out commands
- `A{0-100}`: Audio intensity level for assistant iris animation

### Provisioning System
On first boot or touch-during-startup, the ESP32 enters AP mode and serves a captive portal where users can configure:
- WiFi credentials
- Home location (lat/lon) for radar centering
- Timezone / GMT offset for clock

(The ADS-B server host/port and default radar range are compile-time constants
in `include/config.h`, not portal fields.)

Settings are stored in LittleFS (`/config.json`) via the `Settings` class.

## Critical Implementation Details

### Display Hardware Access
The codebase uses a header hack to access private members of `Arduino_RGB_Display`:
```cpp
#define private public
#define protected public
#include <Arduino_GFX_Library.h>
#undef private
#undef protected
```
This is required to register the VSYNC callback on `gfx->_rgbpanel->_panel_handle`. Do not remove this pattern.

### LVGL Integration
- LVGL is only active when `lvgl_active = true` (clock view)
- When switching away from clock, LVGL is disabled to prevent it from flushing over the radar/assistant graphics
- Two full-screen buffers in PSRAM enable double buffering for tear-free rendering
- `disp_drv.full_refresh = 1` is required for ESP32 DMA compatibility

### Touch Gesture System
Touch gestures are detected in `process_swipe()`:
- **Tap**: Select aircraft or dismiss detail box
- **Vertical swipe**: Zoom in/out
- **Horizontal swipe**: Switch between apps
- Thresholds: movement under `TAP_THRESHOLD` (20 px) is a tap; swipes register past `GESTURE_THRESHOLD` (10 px)

### Aircraft Aging System
Aircraft blips use a two-stage aging system:
1. **Fresh data**: Painted in bright green (`C_BLIP`)
2. **Stale data** (no update since last sweep): Dimmed to dark green (`C_DIM_BLIP`)
3. **Expired** (no update for 2 sweeps OR `AIRCRAFT_MAX_AGE_S`): Removed

This provides visual feedback about data freshness without hard cutoffs.

## File Structure

### ESP32 Source
- `src/main.cpp`: Core application loop, radar logic, view switching
- `src/ClockView.cpp/h`: LVGL clock implementation
- `src/AssistantView.cpp/h`: AI assistant iris visualization
- `src/Provisioning.cpp/h`: Captive portal for WiFi setup
- `src/Settings.cpp/h`: NVS storage for configuration
- `src/waveshare_init.cpp/h`: Hardware initialization for display panels
- `src/TCA9554PWR.cpp/h`: I2C IO expander driver
- `src/Touch_GT911.cpp/h`: Capacitive touch controller driver

### Configuration
- `include/config.h`: WiFi, ADS-B server, home location, defaults (user-editable)
- `include/pins.h`: GPIO pin assignments for hardware (hardware-specific)

### Raspberry Pi
- `pi/assistant_brains.py`: Main Flask server with audio processing and encoder handling
- `pi/config.py`: Pin assignments, audio settings, serial configuration
- `pi/install.sh`: One-click installer for Pi backend
- `pi/requirements.txt`: Python dependencies

## Common Modifications

### Adding a New View
1. Create `NewView.cpp/h` with static methods: `init()`, `show()`, `hide()`, `update()`
2. Add `APP_NEWVIEW` to `AppState` enum in `main.cpp`
3. Add swipe logic in `process_swipe()` to handle navigation to/from new view
4. In `loop()`, add case for new view to call `NewView::update()`

### Changing Radar Colors
All radar colors are RGB565 constants at the top of `main.cpp`:
- `C_BG`: Background
- `C_RING`: Range ring lines
- `C_GRID`: Crosshair grid
- `C_SWEEP`: Rotating sweep arm
- `C_BLIP`: Fresh aircraft blips
- `C_DIM_BLIP`: Stale aircraft blips

### Porting to Different Hardware
1. Update `include/pins.h` with new GPIO assignments
2. Modify `create_waveshare_28C_rgb_panel()` in `waveshare_init.cpp` for display initialization
3. Update `SCREEN_WIDTH`/`SCREEN_HEIGHT`/`CX`/`CY` constants in `main.cpp`

## Known Issues & Workarounds

### Serial Monitor on Windows/MSYS
The git status shows this is a Windows environment with MINGW. When monitoring serial output via PlatformIO on Windows, use `pio device monitor` instead of `pio run -t monitor` if you encounter port access issues.

### OTA Upload
OTA is configured for `esp32-radar.local` via mDNS. If OTA fails:
1. Check device is on same network
2. Try direct IP instead of `.local` hostname in `platformio.ini`
3. Ensure firewall allows port 3232

### PSRAM Buffer Allocation
If LVGL buffer allocation fails, the code falls back to SRAM with a smaller 40-line buffer. This is normal on devices without PSRAM or when PSRAM is exhausted.

### Raspberry Pi Audio Issues (INMP441 I2S Microphone)
The Pi backend uses an INMP441 I2S MEMS microphone. Key configuration points:

**Audio Format:** INMP441 requires **32-bit samples (S32_LE)**, not 16-bit. PyAudio must use `pyaudio.paInt32` and numpy arrays must use `dtype=np.int32`.

**Channel Selection:** INMP441 L/R (SELECT) pin determines output channel:
- L/R tied to GND = Left channel only
- L/R tied to 3.3V = Right channel only
- Must be solidly connected, not floating

**Sample Rate:** Hardware locked to 48kHz. Other rates will be resampled by ALSA.

**Device Tree:** Currently using `googlevoicehat-soundcard` overlay which provides I2S interface but expects Google Voice HAT hardware. Device appears as `hw:0,0`.

**Troubleshooting silent microphone:**
```bash
# Test recording directly from hardware
sudo systemctl stop assistant.service
arecord -D hw:0,0 -f S32_LE -r 48000 -c 2 -d 3 test.wav

# Analyze the recording
python3 << EOF
import wave, numpy as np
w = wave.open("test.wav", "rb")
data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int32)
left = data[0::2]
right = data[1::2]
print(f'L max: {np.max(np.abs(left))}, R max: {np.max(np.abs(right))}')
EOF
```

If max values are 0, check:
1. Wiring connections (VDD=3.3V, GND, SD=GPIO20, WS=GPIO19, SCK=GPIO18, L/R=GND)
2. Power supply voltage at INMP441 (should be stable 3.3V)
3. L/R pin is solidly connected to GND or 3.3V
4. Try different INMP441 module (ESD damage is common)
