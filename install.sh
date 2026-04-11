#!/bin/bash
set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$INSTALL_DIR/.venv"
LOG_DIR="$HOME/Library/Logs/timetrack"
CONFIG_DIR="$HOME/.config/timetrack"
PLIST_SRC="$INSTALL_DIR/com.timetrack.sync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.timetrack.sync.plist"

echo "Creating venv and installing timetrack..."
python3.11 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q -e "$INSTALL_DIR"

echo "Creating log directory..."
mkdir -p "$LOG_DIR"

echo "Creating config directory..."
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    cat > "$CONFIG_DIR/config.toml" << 'EOF'
timezone = "Europe/Moscow"  # Change to your IANA timezone

[notion]
token = "YOUR_NOTION_INTEGRATION_TOKEN"
timelog_db = "35b4cfe8-1f3a-457a-80a8-fe61aa465a18"

[activitywatch]
base_url = "http://localhost:5600"
afk_threshold_min = 10
min_block_duration_sec = 120
merge_gap_sec = 180

[sync]
initial_sync_days = 7
EOF
    echo "⚠️  Config created at $CONFIG_DIR/config.toml"
    echo "   Fill in your Notion integration token before first sync."
fi

echo "Installing launchd service..."
sed -e "s|TIMETRACK_BIN|$VENV_DIR/bin/timetrack|g" \
    -e "s|LOG_DIR|$LOG_DIR|g" \
    "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "✓ timetrack installed. Syncs every 15 min."
echo "  Logs: $LOG_DIR/sync.log"
