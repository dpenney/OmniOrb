# OmniOrb

A multi-functional, high-performance interactive hub for the [Waveshare ESP32-S3-Knob-Touch-LCD-1.28](https://www.waveshare.com/esp32-s3-knob-touch-lcd-1.28.htm). 

**OmniOrb** is the next iteration of the [waveshare_radar_blipper](https://github.com/dpenney/waveshare_radar_blipper) project, expanded into a comprehensive "desktop companion" that integrates real-time data, AI assistance, and high-end visuals.

## 🚀 Features

-   **📡 ADSB Radar:** Real-time PPI-style aircraft tracking with smooth sweep animations. Painted blips and touch-to-view details.
-   **🕒 Smart Clock:** A high-frequency, smooth-beat digital/analog hybrid clock with anti-aliasing.
-   **🤖 NEXUS Assistant:** An AI-powered voice assistant with a futuristic "iris" UI, powered by a Raspberry Pi backend using Google Gemini.
-   **⚙️ Twin-Core Architecture:** Seamless synchronization between the ESP32 (display/UI) and the Raspberry Pi (AI "brains" and audio processing).
-   **🕹️ Interactive Controls:** Full support for the mechanical rotary encoder and touch gestures.

## 📁 Project Structure

```bash
├── pi/                 # Raspberry Pi backend (Python, AI Brains, Audio)
│   ├── install.sh      # Robust "one-click" installer for the Pi
│   ├── assistant_brains.py
│   └── setup_adsb.sh   # 1090-fa setup script
├── src/                # ESP32 Firmware (C++, LVGL)
│   ├── AssistantView.cpp
│   ├── ClockView.cpp
│   └── main.cpp
├── include/            # Hardware & WiFi configuration
└── platformio.ini      # Build configuration
```

## 🛠️ Setup & Installation

Detailed instructions for both the ESP32 and the Raspberry Pi side can be found in the [SETUP_GUIDE.md](pi/SETUP_GUIDE.md).

1.  **Pi Backend:** Use the provided `pi/install.sh` for an automated setup of services and dependencies.
2.  **ESP32 Firmware:** Flash using PlatformIO. Ensure your `include/config.h` is set up with your WiFi and Pi IP.

## 📜 Credits & History

This project began as a dedicated ADS-B radar display. It has since evolved into a generalized multi-app hub for round displays. 

Original repository: [dpenney/waveshare_radar_blipper](https://github.com/dpenney/waveshare_radar_blipper)

## ⚖️ License

MIT
