#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.teleclaude.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOG_DIR="$SCRIPT_DIR/logs"

echo "=== Teleclaude Setup ==="
echo ""

# 1. Install Python dependencies
echo "[1/5] Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt"
echo ""

# 2. Set up .env
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "[2/5] Creating .env file..."
    read -p "Enter your Telegram Bot Token: " BOT_TOKEN
    echo ""
    read -p "Enter your Telegram Chat ID (get it from @userinfobot): " OWNER_ID
    {
        echo "TELEGRAM_BOT_TOKEN=$BOT_TOKEN"
        echo "OWNER_CHAT_ID=$OWNER_ID"
    } > "$SCRIPT_DIR/.env"
    echo ".env created."
else
    echo "[2/5] .env already exists, skipping."
    # Check if OWNER_CHAT_ID is set
    if ! grep -q "OWNER_CHAT_ID" "$SCRIPT_DIR/.env" || grep -q "OWNER_CHAT_ID=$" "$SCRIPT_DIR/.env"; then
        echo "  Warning: OWNER_CHAT_ID is not set in .env"
        read -p "  Enter your Telegram Chat ID (get it from @userinfobot): " OWNER_ID
        echo "OWNER_CHAT_ID=$OWNER_ID" >> "$SCRIPT_DIR/.env"
        echo "  OWNER_CHAT_ID added."
    fi
fi
echo ""

# 3. Create log directory
echo "[3/5] Creating log directory..."
mkdir -p "$LOG_DIR"
echo ""

# 4. Register initial base directory (optional)
echo "[4/5] Register a base directory (optional)..."
read -p "Enter a base directory path (or press Enter to skip): " BASE_DIR
if [ -n "$BASE_DIR" ]; then
    RESOLVED=$(cd "$BASE_DIR" 2>/dev/null && pwd || echo "")
    if [ -n "$RESOLVED" ]; then
        # Update config.json with the base directory
        python3 -c "
import json
cfg = json.load(open('$SCRIPT_DIR/config.json'))
if '$RESOLVED' not in cfg['base_directories']:
    cfg['base_directories'].append('$RESOLVED')
    json.dump(cfg, open('$SCRIPT_DIR/config.json', 'w'), indent=2)
    print('  Registered: $RESOLVED')
else:
    print('  Already registered: $RESOLVED')
"
    else
        echo "  Directory not found: $BASE_DIR (skipping)"
    fi
fi
echo ""

# 5. Install launchd service
echo "[5/5] Setting up launchd auto-start..."

# Replace INSTALL_DIR placeholder with actual path
sed "s|INSTALL_DIR|$SCRIPT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

# Unload if already loaded
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Load the service
launchctl load "$PLIST_DST"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "The bot is now running and will auto-start on login."
echo ""
echo "Useful commands:"
echo "  View logs:     tail -f $LOG_DIR/bot.log"
echo "  Stop service:  launchctl unload $PLIST_DST"
echo "  Start service: launchctl load $PLIST_DST"
echo "  Run manually:  python3 $SCRIPT_DIR/bot.py"
echo ""
echo "Bot commands (send via Telegram):"
echo "  /start                       - Register and show help"
echo "  /register <path>             - Add a base directory"
echo "  /unregister <path|number>    - Remove a base directory"
echo "  /dirs                        - Browse directories (inline keyboard)"
echo "  /launch <path>               - Launch Claude Code"
echo "  /status                      - Show running sessions"
echo "  /terminal <auto|iterm|terminal> - Set preferred terminal"
echo "  /dotdirs <on|off>            - Toggle hidden directory visibility"
