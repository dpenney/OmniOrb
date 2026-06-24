#!/bin/bash
# 🥧 ESP32 Assistant "One-Click" Installer
set -euo pipefail

# Get absolute path to this script's directory
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

# Colors for better feedback
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}------------------------------------------${NC}"
echo -e "🚀 Starting Assistant Installation..."
echo -e "${BLUE}------------------------------------------${NC}"

# 1. Hardware Detection
IS_RPI=0
IS_RPI5=0
if grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
    IS_RPI=1
    if grep -q "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null; then
        IS_RPI5=1
        echo -e "${GREEN}📍 Raspberry Pi 5 detected.${NC}"
    else
        echo -e "${GREEN}📍 Raspberry Pi 3/4 detected.${NC}"
    fi
else
    echo -e "${YELLOW}📍 Non-RPi hardware detected (e.g. Particle Tachyon). Skipping Pi-specific drivers.${NC}"
fi


# 2. System Dependencies
echo -e "📦 Checking system dependencies..."
# Core dependencies for all platforms
DEPENDENCIES="python3 python3-venv python3-dev python3-pyaudio portaudio19-dev swig libspeexdsp-dev build-essential curl wget unzip alsa-utils"

MISSING_DEPS=""
for dep in $DEPENDENCIES; do
    if ! dpkg -s "$dep" 2>/dev/null | grep -q "Status: install ok installed"; then
        MISSING_DEPS="$MISSING_DEPS $dep"
    fi
done

if [ -n "$MISSING_DEPS" ]; then
    echo -e "${YELLOW}Installing missing dependencies:${NC} $MISSING_DEPS"
    sudo apt-get update
    sudo apt-get install -y $MISSING_DEPS
else
    echo -e "${GREEN}✅ All core system dependencies already installed.${NC}"
fi

# 3. Create Virtual Environment
echo -e "🐍 Creating/Updating virtual environment (venv)..."
if [ -d "venv" ]; then
    echo -e "${YELLOW}🧹 Removing old/incompatible venv...${NC}"
    rm -rf venv 
fi
python3 -m venv --system-site-packages venv

# 4. Install Python Ingredients
echo -e "📥 Installing python dependencies into venv..."
./venv/bin/pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    ./venv/bin/pip install -r requirements.txt
else
    echo -e "${RED}❌ requirements.txt not found!${NC}"
    exit 1
fi

if [ "$IS_RPI" -eq 1 ]; then
    if [ "$IS_RPI5" -eq 1 ]; then
        echo -e "📦 Installing Raspberry Pi 5 GPIO support (rpi-lgpio)..."
        # rpi-lgpio is a drop-in RPi.GPIO replacement backed by lgpio (Pi 5 RP1 GPIO).
        # Standard RPi.GPIO imports on Pi 5 but its setmode() is absent — rpi-lgpio fixes this.
        sudo apt-get install -y python3-lgpio 2>/dev/null || true
        ./venv/bin/pip install rpi-lgpio
    else
        echo -e "📦 Installing Raspberry Pi 4 GPIO support (RPi.GPIO)..."
        ./venv/bin/pip install RPi.GPIO
    fi
fi

if [ "$IS_RPI5" -eq 1 ]; then
    echo -e "🔧 Applying speexdsp Python 3.13 patch (imp→importlib)..."
    # Python 3.12+ removed the 'imp' module. The piwheels cp313 speexdsp wheel's
    # SWIG-generated wrapper still uses it. Patch in-place after pip install.
    SPEEX_PY=$(./venv/bin/python3 -c \
        "import speexdsp, os; print(os.path.join(os.path.dirname(speexdsp.__file__), 'speexdsp.py'))" \
        2>/dev/null || true)
    if [ -n "$SPEEX_PY" ] && grep -q "import imp" "$SPEEX_PY" 2>/dev/null; then
        ./venv/bin/python3 - "$SPEEX_PY" <<'PATCH'
import sys, re
path = sys.argv[1]
src = open(path).read()
old = re.search(r'def swig_import_helper\(\):.*?^_speexdsp = swig_import_helper\(\)', src, re.DOTALL | re.MULTILINE)
if not old:
    print(f"  speexdsp.py: pattern not found, skipping patch")
    sys.exit(0)
new_helper = '''def swig_import_helper():
    import importlib.util, os
    pkg_dir = os.path.dirname(__file__)
    so = next((f for f in os.listdir(pkg_dir) if f.startswith('_speexdsp') and f.endswith('.so')), None)
    if so:
        spec = importlib.util.spec_from_file_location('_speexdsp', os.path.join(pkg_dir, so))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    import _speexdsp
    return _speexdsp
_speexdsp = swig_import_helper()'''
patched = src[:old.start()] + new_helper + src[old.end():]
open(path, 'w').write(patched)
print(f"  Patched: {path}")
PATCH
        echo -e "${GREEN}✅ speexdsp patched.${NC}"
    else
        echo -e "${GREEN}✅ speexdsp does not need patching.${NC}"
    fi
fi

# 5. Piper TTS Binary
PIPER_VERSION="v1.2.0"
PIPER_ARCH="arm64"
PIPER_TARBALL="piper_${PIPER_ARCH}.tar.gz"
PIPER_URL="https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/${PIPER_TARBALL}"

if [ ! -f "venv/bin/piper" ]; then
    echo -e "🔊 Downloading Piper TTS binary..."
    curl -L "$PIPER_URL" -o /tmp/piper.tar.gz
    tar -xzf /tmp/piper.tar.gz -C /tmp/
    cp /tmp/piper/piper venv/bin/piper
    chmod +x venv/bin/piper
    cp /tmp/piper/*.so* venv/bin/ 2>/dev/null || true
    rm -rf /tmp/piper /tmp/piper.tar.gz
    echo -e "${GREEN}✅ Piper installed.${NC}"
else
    echo -e "${GREEN}✅ Piper already installed.${NC}"
fi

# Voice model directory
mkdir -p voices
if [ ! -f "voices/alan.onnx" ]; then
    echo -e "${YELLOW}⚠️  voices/alan.onnx not found.${NC}"
    echo -e "   Place the alan voice model files at:"
    echo -e "   ${BLUE}$SCRIPT_DIR/voices/alan.onnx${NC}"
    echo -e "   ${BLUE}$SCRIPT_DIR/voices/alan.onnx.json${NC}"
fi

# 6. Configuration Setup
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo -e "📝 Creating .env from template..."
        cp .env.example .env
        echo -e "${YELLOW}⚠️  Please edit .env with your Google API Key later!${NC}"
    else
         echo -e "${RED}⚠️  Warning: .env.example not found. Skipping .env creation.${NC}"
    fi
else
    echo -e "${GREEN}✅ .env file exists.${NC}"
fi

# Set AUDIO_SOURCE based on detected hardware (overrides template default).
if [ -f ".env" ] && [ "$IS_RPI" -eq 1 ]; then
    if [ "$IS_RPI5" -eq 1 ]; then
        TARGET_AUDIO="inmp441_pi5"
    else
        TARGET_AUDIO="inmp441"
    fi
    CURRENT_AUDIO=$(grep -E '^AUDIO_SOURCE=' .env | cut -d= -f2 || true)
    if [ "$CURRENT_AUDIO" != "$TARGET_AUDIO" ]; then
        sed -i "s/^AUDIO_SOURCE=.*/AUDIO_SOURCE=$TARGET_AUDIO/" .env
        echo -e "${GREEN}✅ AUDIO_SOURCE set to $TARGET_AUDIO${NC}"
    fi
fi

# 7. Boot Configuration (/boot/firmware/config.txt)
echo -e "🔧 Checking Boot Configuration..."
BOOT_CONFIG="/boot/firmware/config.txt"
BOOT_CHANGED=0

if [ -f "$BOOT_CONFIG" ]; then
    append_if_missing() {
        local line="$1"
        if ! grep -qF "$line" "$BOOT_CONFIG" 2>/dev/null; then
            echo "$line" | sudo tee -a "$BOOT_CONFIG" > /dev/null
            echo -e "${YELLOW}  Added: $line${NC}"
            BOOT_CHANGED=1
        fi
    }

    if [ "$IS_RPI" -eq 1 ] && [ "$IS_RPI5" -eq 0 ]; then
        append_if_missing "dtoverlay=googlevoicehat-soundcard"
        append_if_missing "enable_uart=1"
        append_if_missing "dtoverlay=disable-bt"
        append_if_missing "gpio=13=op,dl"
    elif [ "$IS_RPI5" -eq 1 ]; then
        # Pi 5 I2S mic + UART
        append_if_missing "dtparam=i2s=on"
        append_if_missing "enable_uart=1"

        # Compile and install custom i2s-mems-mic overlay if it doesn't exist
        if [ ! -f "/boot/firmware/overlays/i2s-mems-mic.dtbo" ]; then
            echo -e "🛠️  Compiling and installing i2s-mems-mic device tree overlay..."
            if ! command -v dtc >/dev/null 2>&1; then
                sudo apt-get install -y device-tree-compiler
            fi
            dtc -@ -I dts -O dtb -o /tmp/i2s-mems-mic.dtbo "$SCRIPT_DIR/i2s-mems-mic.dts"
            sudo cp /tmp/i2s-mems-mic.dtbo /boot/firmware/overlays/i2s-mems-mic.dtbo
            rm -f /tmp/i2s-mems-mic.dtbo
            echo -e "${GREEN}✅ i2s-mems-mic overlay installed.${NC}"
        fi
        append_if_missing "dtoverlay=i2s-mems-mic"

        # Pi 5: ttyAMA0 (RP1 UART0 on GPIO 14/15) is root:root 600 by default.
        # A udev rule lets dialout-group users open it; add the service user to dialout.
        UDEV_RULE='/etc/udev/rules.d/50-ttyama0.rules'
        if [ ! -f "$UDEV_RULE" ]; then
            echo -e "🔧 Adding udev rule for /dev/ttyAMA0 permissions..."
            echo 'KERNEL=="ttyAMA0", GROUP="dialout", MODE="0660"' | sudo tee "$UDEV_RULE" > /dev/null
            sudo udevadm control --reload-rules
            sudo udevadm trigger
            echo -e "${GREEN}✅ udev rule added.${NC}"
        fi
        if ! groups "$USER" | grep -q "dialout"; then
            echo -e "🔧 Adding $USER to dialout group..."
            sudo usermod -aG dialout "$USER"
            echo -e "${YELLOW}⚠️  Group change takes effect after next login.${NC}"
        fi

        # Pi 5: remove the kernel serial console so it doesn't corrupt ESP32 UART traffic.
        CMDLINE="/boot/firmware/cmdline.txt"
        if [ -f "$CMDLINE" ] && grep -q "console=serial0" "$CMDLINE"; then
            echo -e "🔧 Removing serial console from $CMDLINE..."
            sudo sed -i 's/console=serial0,[0-9]* //g' "$CMDLINE"
            BOOT_CHANGED=1
            echo -e "${GREEN}✅ Serial console removed from cmdline.txt${NC}"
        fi
    fi

    if [ "$BOOT_CHANGED" -eq 1 ]; then
        echo -e "${YELLOW}⚠️  $BOOT_CONFIG was updated — a reboot is required!${NC}"
    else
        echo -e "${GREEN}✅ Boot config already correct.${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  Warning: $BOOT_CONFIG not found. Skipping boot configuration.${NC}"
fi

# 8. Systemd Service Integration
echo -e "⚙️  Installing systemd service..."

# Create a temporary service file with correct paths
SERVICE_FILE="assistant.service"
if [ -f "$SERVICE_FILE" ]; then
    TEMP_SERVICE="/tmp/assistant.service"
    sed -e "s|{{APP_PATH}}|$SCRIPT_DIR|g" \
        -e "s|{{SERVICE_USER}}|$USER|g" \
        "$SERVICE_FILE" > "$TEMP_SERVICE"
    sudo cp "$TEMP_SERVICE" /etc/systemd/system/assistant.service
    rm "$TEMP_SERVICE"
    
    # Tachyon codec capture needs the user's PulseAudio session. A bare system
    # service has none, so the codec open fails with PortAudio -9998. Enable
    # lingering (so /run/user/$UID + PulseAudio exist at boot without a login)
    # and point the service at that session via a drop-in.
    AUDIO_SOURCE_VAL=""
    if grep -q '^AUDIO_SOURCE=' "$SCRIPT_DIR/.env" 2>/dev/null; then
        AUDIO_SOURCE_VAL="$(grep -E '^AUDIO_SOURCE=' "$SCRIPT_DIR/.env" | cut -d= -f2)"
    fi
    
    if [ "$AUDIO_SOURCE_VAL" = "tachyon_codec" ]; then
        echo -e "🔊 Configuring Tachyon codec audio session..."
        sudo loginctl enable-linger "$USER"
        UID_NUM="$(id -u "$USER")"
        sudo mkdir -p /etc/systemd/system/assistant.service.d
        sudo tee /etc/systemd/system/assistant.service.d/audio-env.conf >/dev/null <<CONF
[Service]
Environment=XDG_RUNTIME_DIR=/run/user/${UID_NUM}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${UID_NUM}/bus
CONF
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable assistant.service
    sudo systemctl restart assistant.service
    echo -e "${GREEN}✅ Service installed and restarted.${NC}"
else
    echo -e "${RED}❌ $SERVICE_FILE not found!${NC}"
fi

echo -e "${BLUE}------------------------------------------${NC}"
echo -e "🎉 ${GREEN}Installation Complete!${NC}"
echo -e "${BLUE}------------------------------------------${NC}"
echo -e "Monitor logs: ${BLUE}tail -f $SCRIPT_DIR/assistant.log${NC}"
echo -e "Edit config:  ${BLUE}nano $SCRIPT_DIR/config.py${NC}"
echo -e "Edit secrets: ${BLUE}nano $SCRIPT_DIR/.env${NC}"
echo -e "${BLUE}------------------------------------------${NC}"
echo -e "${YELLOW}⚠️  If this is a new device, verify audio device indices:${NC}"
echo -e "   ${BLUE}aplay -l${NC}  (output devices)"
echo -e "   ${BLUE}python3 $SCRIPT_DIR/find_mic.py${NC}  (input device index)"
echo -e "   Update AUDIO_DEVICE_INDEX and APLAY_DEVICE in config.py if needed."
echo -e "${BLUE}------------------------------------------${NC}"
