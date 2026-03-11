#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.claude-launcher.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOG_DIR="$SCRIPT_DIR/logs"

echo "=== Claude Code Telegram Launcher Setup ==="
echo ""

# 1. Install Python dependencies
echo "[1/4] Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt"
echo ""

# 2. Set up .env
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "[2/4] Creating .env file..."
    read -p "Enter your Telegram Bot Token: " BOT_TOKEN
    echo "TELEGRAM_BOT_TOKEN=$BOT_TOKEN" > "$SCRIPT_DIR/.env"
    echo ".env created."
else
    echo "[2/4] .env already exists, skipping."
fi
echo ""

# 3. Create log directory
echo "[3/4] Creating log directory..."
mkdir -p "$LOG_DIR"
echo ""

# 4. Install launchd service
echo "[4/4] Setting up launchd auto-start..."

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
echo "  View errors:   tail -f $LOG_DIR/bot.err"
echo "  Stop service:  launchctl unload $PLIST_DST"
echo "  Start service: launchctl load $PLIST_DST"
echo "  Run manually:  python3 $SCRIPT_DIR/bot.py"
echo ""
echo "Next steps:"
echo "  1. Open Telegram and message your bot"
echo "  2. Send /start to register yourself"
echo "  3. Send /register ~/Projects to add a base directory"
echo "  4. Send /dirs to browse and launch Claude Code!"
