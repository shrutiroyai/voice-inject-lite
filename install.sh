#!/bin/bash
#
# Voice Inject Lite - One-Command Installer & Launcher
# Optimized for Command Mode (Auto-Paste) only.
#

# === COLOR CONSTANTS ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\133[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# === PATH RESOLUTION ===
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# === CONFIGURATION ===
HEALTH_TIMEOUT=15
CLIENT_CHECK_DELAY=3
CONFIG_PATH="$HOME/.voice-inject/config.yaml"

# === FUNCTIONS ===

cleanup() {
    echo -e "\n${YELLOW}Shutting down Voice Inject...${NC}"
    pkill -f "python3.*server.py" 2>/dev/null
    pkill -f "python3.*client.py" 2>/dev/null
    echo -e "${GREEN}All services stopped${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

detect_platform() {
    case "$(uname -s)" in
        Darwin) PLATFORM="macos" ;;
        MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
        *) PLATFORM="linux" ;;
    esac
    echo -e "${BLUE}Detected platform: ${PLATFORM}${NC}"
}

check_prerequisites() {
    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}❌ python3 not found. Please install it.${NC}"
        if [ "$PLATFORM" = "macos" ]; then
            echo -e "${RED}   Run: brew install python3${NC}"
        elif [ "$PLATFORM" = "linux" ]; then
            echo -e "${RED}   Run: sudo apt install python3 python3-venv${NC}"
        else
            echo -e "${RED}   Download from https://python.org${NC}"
        fi
        exit 1
    fi
    echo -e "${GREEN}✓ Prerequisites found${NC}"
}

bootstrap_config() {
    mkdir -p "$HOME/.voice-inject"
    
    # Check if config.yaml exists and has a token
    local has_token=false
    if [ -f "$CONFIG_PATH" ]; then
        if grep -q "huggingface_token" "$CONFIG_PATH"; then
            has_token=true
        fi
    fi

    if [ "$has_token" = false ]; then
        echo -e "${BLUE}=========================================="
        echo -e "🔐 Hugging Face Setup"
        echo -e "==========================================${NC}"
        echo -e "A token is required to download the Phi-3.5 model."
        echo -e "1. Visit: https://huggingface.co/settings/tokens"
        echo -e "2. Create a 'Read' token."
        echo ""
        echo -n "Enter your HF Token (or press Enter to skip and add via UI later): "
        read -r token
        if [ -n "$token" ]; then
            echo "huggingface_token: $token" >> "$CONFIG_PATH"
            echo "min_speech_energy: 180" >> "$CONFIG_PATH"
            echo "active_preset: office" >> "$CONFIG_PATH"
            echo -e "${GREEN}✓ Token saved to $CONFIG_PATH${NC}"
        fi
    fi
}

install_deps() {
    if [ ! -d ".venv" ]; then
        echo -e "${BLUE}Creating virtual environment...${NC}"
        python3 -m venv .venv
    fi
    source .venv/bin/activate
    echo -e "${BLUE}Installing dependencies (this may take a minute)...${NC}"
    pip install -r requirements.txt --quiet

    # Platform-specific extras
    if [ "$PLATFORM" = "linux" ]; then
        echo -e "${BLUE}Installing Linux extras (xclip, xdotool)...${NC}"
        if command -v apt &>/dev/null; then
            sudo apt install -y xclip xdotool 2>/dev/null || true
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y xclip xdotool 2>/dev/null || true
        fi
        pip install dbus-python PyGObject --quiet 2>/dev/null || true
    elif [ "$PLATFORM" = "windows" ]; then
        echo -e "${BLUE}Installing Windows extras...${NC}"
        pip install torch transformers --quiet 2>/dev/null || true
    fi

    echo -e "${GREEN}✓ Dependencies installed${NC}"
}

open_browser() {
    local url="http://localhost:3000"
    if command -v open &>/dev/null; then
        open "$url"
    elif command -v xdg-open &>/dev/null; then
        xdg-open "$url"
    fi
}

register_alias() {
    local SHELL_CONFIG=""
    local ALIAS_NAME="voice"
    local ALIAS_LINE="alias $ALIAS_NAME=\"cd '$SCRIPT_DIR' && git pull --quiet && ./install.sh\""
    
    if echo "$SHELL" | grep -q "zsh"; then
        SHELL_CONFIG="$HOME/.zshrc"
    elif echo "$SHELL" | grep -q "bash"; then
        SHELL_CONFIG="$HOME/.bashrc"
    fi

    if [ -n "$SHELL_CONFIG" ]; then
        # Remove any existing alias with the same name to allow override
        if grep -q "alias $ALIAS_NAME=" "$SHELL_CONFIG" 2>/dev/null; then
            if [[ "$OSTYPE" == "darwin"* ]]; then
                sed -i '' "/alias $ALIAS_NAME=/d" "$SHELL_CONFIG"
            else
                sed -i "/alias $ALIAS_NAME=/d" "$SHELL_CONFIG"
            fi
        fi
        
        echo "$ALIAS_LINE" >> "$SHELL_CONFIG"
        echo -e "${GREEN}✓ Registered/Updated '$ALIAS_NAME' command in $SHELL_CONFIG${NC}"
        echo -e "${BLUE}The 'voice' command will now automatically pull latest changes from GitHub.${NC}"
    fi
}

start_services() {
    echo -e "${BLUE}Cleaning up existing processes...${NC}"
    pkill -f "client.py" 2>/dev/null
    pkill -f "server.py" 2>/dev/null
    if [ "$PLATFORM" = "macos" ] || [ "$PLATFORM" = "linux" ]; then
        lsof -ti :3000 | xargs kill -9 2>/dev/null
    fi
    sleep 1
    pkill -9 -f "voice-inject-lite.*client.py" 2>/dev/null
    pkill -9 -f "voice-inject-lite.*server.py" 2>/dev/null

    echo -e "${BLUE}Starting Server...${NC}"
    ./.venv/bin/python3 server.py > /tmp/voice-lite-server.log 2>&1 &

    local elapsed=0
    while ! curl -s http://localhost:3000/health &>/dev/null; do
        sleep 1
        elapsed=$((elapsed+1))
        if [ $elapsed -gt $HEALTH_TIMEOUT ]; then
            echo -e "${RED}❌ Server failed to start. Check /tmp/voice-lite-server.log${NC}"
            exit 1
        fi
    done

    echo -e "${BLUE}Starting Client...${NC}"
    ./.venv/bin/python3 client.py > /tmp/voice-lite-client.log 2>&1 &
    echo -e "${GREEN}✓ Services running${NC}"
    open_browser
}

# === MAIN ===

echo -e "${BLUE}🎙️  Voice Inject Lite${NC}"
detect_platform
check_prerequisites
bootstrap_config
install_deps
register_alias
start_services

if [ "$PLATFORM" = "macos" ]; then
    HOTKEY_DESC="Double-tap Left Option (⌥)"
    PERMS_NOTE="⚠️  Make sure to grant Microphone & Accessibility permissions to your Terminal in System Settings."
elif [ "$PLATFORM" = "windows" ]; then
    HOTKEY_DESC="Double-tap Left Alt"
    PERMS_NOTE="⚠️  Run as Administrator if paste simulation doesn't work."
else
    HOTKEY_DESC="Double-tap Left Alt"
    PERMS_NOTE="⚠️  Ensure xclip, xdotool are installed. Add user to 'input' group for keyboard access."
fi

echo -e "${GREEN}=========================================="
echo -e "✅ Voice Inject Lite is READY!"
echo -e "   UI:      http://localhost:3000"
echo -e "   Hotkey:  ${HOTKEY_DESC}"
echo -e "   Press Ctrl+C to stop"
echo -e "==========================================${NC}"
echo ""
echo -e "${YELLOW}${PERMS_NOTE}${NC}"

wait
