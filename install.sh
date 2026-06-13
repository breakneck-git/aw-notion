#!/bin/bash
set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$INSTALL_DIR/.venv"
CONFIG_DIR="$HOME/.config/aw-notion"
CONFIG_FILE="$CONFIG_DIR/config.toml"
CONFIG_TEMPLATE="$INSTALL_DIR/config.toml.example"

OS="$(uname -s)"

PYTHON_BIN=""
for candidate in python3.11 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "Error: Python 3.11+ not found in PATH."
    echo "  macOS: brew install python@3.11"
    echo "  Linux: apt install python3.11  |  dnf install python3.11  |  pacman -S python"
    exit 1
fi

echo "Creating venv and installing aw-notion (using $PYTHON_BIN)..."
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q -e "$INSTALL_DIR"

echo "Creating config directory..."
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    cp "$CONFIG_TEMPLATE" "$CONFIG_FILE"
    # Token goes here — restrict to owner only so other local users / Spotlight /
    # backup tools don't read it. Default umask leaves 0644 (world-readable),
    # which is unacceptable for an API credential.
    chmod 600 "$CONFIG_FILE"
    echo "⚠️  Config created at $CONFIG_FILE (mode 600)"
    echo "   Edit it and fill in:"
    echo "     • notion.token       (https://www.notion.so/my-integrations)"
    echo "     • notion.timelog_db  (UUID from your Notion database URL)"
    echo "     • timezone           (your IANA zone)"
else
    # Existing install: enforce 600 in case it was created before this fix.
    chmod 600 "$CONFIG_FILE" 2>/dev/null || true
fi

if [ "$OS" = "Darwin" ]; then
    LOG_DIR="$HOME/Library/Logs/aw-notion"
    PLIST_SRC="$INSTALL_DIR/com.aw-notion.sync.plist"
    PLIST_DST="$HOME/Library/LaunchAgents/com.aw-notion.sync.plist"

    echo "Installing launchd service (macOS)..."
    mkdir -p "$LOG_DIR"
    sed -e "s|BIN_PATH|$VENV_DIR/bin/aw-notion|g" \
        -e "s|LOG_DIR|$LOG_DIR|g" \
        "$PLIST_SRC" > "$PLIST_DST"

    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"

    # --- ActivityWatch window-watcher reliability (macOS) ---
    # aw-qt starts aw-watcher-window ONCE and does not restart it if it dies
    # (sleep/wake/crash). When it dies, ActivityWatch silently stops recording
    # window events -> aw-notion finds 0 focus blocks -> nothing reaches Notion.
    # Fix: supervise the watcher with a launchd KeepAlive agent (auto-restarts on
    # death, RunAtLoad at login) and drop it from aw-qt's autostart so there is
    # never a second instance writing to the same bucket.
    AW_WIN_BIN="/Applications/ActivityWatch.app/Contents/MacOS/aw-watcher-window"
    if [ -x "$AW_WIN_BIN" ]; then
        KA_DST="$HOME/Library/LaunchAgents/com.aw-watcher-window.keepalive.plist"
        echo "Supervising aw-watcher-window with launchd KeepAlive..."
        sed -e "s|BIN_PATH|$AW_WIN_BIN|g" \
            -e "s|LOG_DIR|$LOG_DIR|g" \
            "$INSTALL_DIR/com.aw-watcher-window.keepalive.plist" > "$KA_DST"

        AW_QT_TOML="$HOME/Library/Application Support/activitywatch/aw-qt/aw-qt.toml"
        if [ -f "$AW_QT_TOML" ]; then
            cp "$AW_QT_TOML" "$AW_QT_TOML.aw-notion.bak"
            awk '
              /^\[aw-qt\][[:space:]]*$/ { print; print "autostart_modules = [\"aw-server\", \"aw-watcher-afk\"]"; inq=1; next }
              /^\[/ { inq=0 }
              inq && /^[[:space:]]*#?[[:space:]]*autostart_modules/ { next }
              { print }
            ' "$AW_QT_TOML.aw-notion.bak" > "$AW_QT_TOML"
        fi

        # Restart ActivityWatch so aw-qt re-reads config (no longer launches
        # window), then let launchd own the watcher. KeepAlive + ThrottleInterval
        # tolerate aw-server not being up yet at boot (it retries until ready).
        osascript -e 'quit app "ActivityWatch"' 2>/dev/null || true
        sleep 3
        pkill -f "MacOS/aw-watcher-window" 2>/dev/null || true
        open -a ActivityWatch 2>/dev/null || true
        sleep 5
        launchctl unload "$KA_DST" 2>/dev/null || true
        launchctl load "$KA_DST"
        echo "✓ aw-watcher-window now auto-restarts on death/sleep (launchd KeepAlive)."
    else
        echo "⚠️  ActivityWatch not found at $AW_WIN_BIN — window events won't be recorded."
        echo "   Install ActivityWatch, then re-run ./install.sh."
    fi

    echo "✓ aw-notion installed. Syncs every 15 min."
    echo "  Logs:         $LOG_DIR/sync.log"
    echo "  Force resync: launchctl kickstart -k gui/\$(id -u)/com.aw-notion.sync"
elif [ "$OS" = "Linux" ]; then
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    SERVICE_SRC="$INSTALL_DIR/aw-notion.service"
    TIMER_SRC="$INSTALL_DIR/aw-notion.timer"

    echo "Installing systemd user service (Linux)..."
    mkdir -p "$SYSTEMD_DIR"
    sed -e "s|BIN_PATH|$VENV_DIR/bin/aw-notion|g" \
        "$SERVICE_SRC" > "$SYSTEMD_DIR/aw-notion.service"
    cp "$TIMER_SRC" "$SYSTEMD_DIR/aw-notion.timer"

    systemctl --user daemon-reload
    systemctl --user enable --now aw-notion.timer

    echo "✓ aw-notion installed. Syncs every 15 min."
    echo "  Logs:         journalctl --user -u aw-notion.service -f"
    echo "  Force resync: systemctl --user start aw-notion.service"
else
    echo "Error: unsupported OS: $OS (only Darwin and Linux are supported)"
    exit 1
fi
