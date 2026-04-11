#!/bin/bash
set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$INSTALL_DIR/.venv"
LOG_DIR="$HOME/Library/Logs/aw-notion"
CONFIG_DIR="$HOME/.config/aw-notion"
CONFIG_FILE="$CONFIG_DIR/config.toml"
CONFIG_TEMPLATE="$INSTALL_DIR/config.toml.example"
PLIST_SRC="$INSTALL_DIR/com.aw-notion.sync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.aw-notion.sync.plist"

echo "Creating venv and installing aw-notion..."
python3.11 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q -e "$INSTALL_DIR"

echo "Creating log directory..."
mkdir -p "$LOG_DIR"

echo "Creating config directory..."
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    cp "$CONFIG_TEMPLATE" "$CONFIG_FILE"
    echo "⚠️  Config created at $CONFIG_FILE"
    echo "   Edit it and fill in:"
    echo "     • notion.token       (https://www.notion.so/my-integrations)"
    echo "     • notion.timelog_db  (UUID from your Notion database URL)"
    echo "     • timezone           (your IANA zone)"
    echo "   Then re-run: launchctl kickstart -k gui/\$(id -u)/com.aw-notion.sync"
fi

echo "Installing launchd service..."
sed -e "s|BIN_PATH|$VENV_DIR/bin/aw-notion|g" \
    -e "s|LOG_DIR|$LOG_DIR|g" \
    "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "✓ aw-notion installed. Syncs every 15 min."
echo "  Logs: $LOG_DIR/sync.log"
