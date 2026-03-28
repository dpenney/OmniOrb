# 🥧 Raspberry Pi Assistant Setup Guide

This guide will walk you through setting up your Raspberry Pi 4 to act as the "Brains" for your ESP32 Radar.

---

## 🔌 1. Hardware Wiring

Connect your components to the Raspberry Pi 4 using the following pins. 

> [!TIP]
> Use the **Physical Pin Number** (1-40) to find the correct spot on your Pi's header.

### 🎙️ I2S Microphone (Audio)
| Mic Pin | Pi Pin Name | Physical Pin |
| :--- | :--- | :--- |
| **VDD** | 3.3V Power | **Pin 1 or 17** |
| **GND** | Ground | **Pin 6, 9, or 14** |
| **L/R** | Left/Right Select | **GND** (for Left channel) |
| **SCK** | BCLK (Clock) | **Pin 12** (GPIO 18) |
| **SD** | DIN (Data In) | **Pin 38** (GPIO 20) |
| **WS** | LRCLK (Select) | **Pin 35** (GPIO 19) |

### 🎡 Rotary Encoder (Zoom Control)
| Encoder Pin | Pi Pin Name | Physical Pin |
| :--- | :--- | :--- |
| **CLK** | GPIO 17 | **Pin 11** |
| **DT** | GPIO 22 | **Pin 15** |
| **GND** | GPIO 27 | **Pin 13** (Software Ground) |

---

## ⚙️ 2. Internal Settings

Run these commands on the Pi to enable the necessary hardware features:

1.  **Enable Serial & Audio**:
    -   Run `sudo raspi-config`.
    -   Go to **Interface Options** -> **Serial Port**.
    -   Select **NO** for login shell, **YES** for hardware serial.
2.  **Enable I2S**:
    -   `sudo nano /boot/config.txt`
    -   Add `dtparam=i2s=on` and `dtoverlay=googlevoicehat-soundcard` to the bottom.
3.  **Install Base Ingredients**:
    -   `sudo apt-get update`
    -   `sudo apt-get install -y python3-venv python3-pip portaudio19-dev python3-pyaudio python3-numpy rsync`
4.  **Reboot**:
    -   `sudo reboot`

---

## 🚀 3. One-Click Installation

Once connected to your network, run the following from your computer (using the provided `deploy.sh`):

1.  **Sync Files**:
    ```bash
    ./pi/deploy.sh
    ```
2.  **Run Installer**:
    Follow the prompt from the deployment script, or run:
    ```bash
    ssh pi@octopi.local 'cd ~/assistant && chmod +x install.sh && ./install.sh'
    ```

---

## 🔑 4. Adding Your Gemini API Key

To enable the LLM brain (Google Gemini), you need to add your API key:

1.  Open the secret config file:
    `nano ~/assistant/.env`
2.  Paste your key into the `GEMINI_API_KEY=` line.
3.  Restart the assistant:
    `sudo systemctl restart assistant.service`

---

## 🛠️ Troubleshooting
- **Logs**: Monitor everything in real-time: `tail -f ~/assistant/assistant.log`
- **Service Status**: Check if the assistant is happy: `sudo systemctl status assistant.service`
- **Pins**: Change pins anytime in `~/assistant/config.py`.
