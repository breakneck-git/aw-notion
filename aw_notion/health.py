"""Data-source liveness checks, run before each sync.

A dead aw-watcher-window or a down aw-server doesn't fail the sync — it just
yields `Found 0 focus blocks` every 15 minutes, so the time log silently stops
filling (this went unnoticed for ~2 days after a macOS upgrade). These checks
turn that silence into a logged ERROR plus a Telegram alert (Mon&Mesh bot via
the shared `owner-alert` script), falling back to a macOS notification.

Window-watcher staleness is judged *relative to afk-watcher*, not against the
wall clock: afk-watcher heartbeats whenever the machine is awake, regardless of
whether the user is present, while all watchers (and this sync) are frozen
during sleep. So "afk is fresh but window lags far behind" means the window
watcher is dead — and neither being away from the desk nor overnight sleep
trips it. Every failed check is re-run once after RECHECK_SEC before alerting,
to absorb the two benign races: the RunAtLoad sync at login firing before
ActivityWatch has started, and the sync launchd fires on wake running before
the watchers' first post-sleep heartbeat.
"""

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

RECHECK_SEC = 60
WINDOW_LAG_ALERT_SEC = 10 * 60
# Last alerted problem ("" = healthy). Alerts go out only on a state change —
# one on breakage, one on recovery — not on every 15-minute sync.
ALERT_STATE_PATH = Path.home() / ".config" / "aw-notion" / "alert.state"

_PROJECT = "aw-notion"
_SERVER_DOWN = "aw-server недоступен — время не пишется. Запусти ActivityWatch."
_WINDOW_DEAD = (
    "aw-watcher-window не пишет события — записей в Notion не будет. "
    "Проверь: launchctl print gui/$(id -u)/com.aw-watcher-window.keepalive"
)
_RECOVERED = "Источники восстановлены, записи снова пишутся. Было: {}"


def _newest(buckets: dict, prefix: str) -> datetime | None:
    stamps = [
        datetime.fromisoformat(meta["last_updated"])
        for bucket_id, meta in buckets.items()
        if bucket_id.startswith(prefix) and meta.get("last_updated")
    ]
    return max(stamps, default=None)


def window_lag_sec(buckets: dict) -> float | None:
    """Seconds window-watcher's last heartbeat trails afk-watcher's, or None
    if either bucket is absent (nothing to compare against)."""
    afk = _newest(buckets, "aw-watcher-afk")
    window = _newest(buckets, "aw-watcher-window")
    if afk is None or window is None:
        return None
    return (afk - window).total_seconds()


def _which(name: str) -> str | None:
    # launchd's PATH lacks ~/.local/bin, where the shared owner-* scripts live.
    path = os.pathsep.join([os.environ.get("PATH", ""), str(Path.home() / ".local" / "bin")])
    return shutil.which(name, path=path)


def _telegram(message: str) -> bool:
    """Alert via the shared Mon&Mesh `owner-alert` script. True if delivered."""
    script = _which("owner-alert")
    if script is None:
        return False
    try:
        res = subprocess.run(
            [script, _PROJECT, "-", "Алерт"],
            input=message,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("owner-alert failed: %s", exc)
        return False
    if res.returncode != 0:
        log.warning("owner-alert exited %d: %s", res.returncode, (res.stderr or "").strip())
        return False
    return True


def _desktop(message: str) -> None:
    osascript = _which("osascript")
    if osascript is None:
        return
    # Text goes in argv, not interpolated into the script, so quotes in the
    # message can't break (or inject into) the AppleScript.
    args = [osascript]
    for line in (
        "on run argv",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "end run",
    ):
        args += ["-e", line]
    try:
        subprocess.run(args + [_PROJECT, message], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("desktop notification failed: %s", exc)


def send_alert(message: str) -> None:
    """Telegram (Mon&Mesh) first; macOS notification if that's unavailable.
    Never raises — alerting must not break the sync."""
    if not _telegram(message):
        _desktop(message)


def _problem(aw) -> str | None:
    if not aw.is_running():
        return _SERVER_DOWN
    lag = window_lag_sec(aw.buckets())
    if lag is not None and lag > WINDOW_LAG_ALERT_SEC:
        return _WINDOW_DEAD
    return None


def _read_state(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_state(path: Path, value: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist alert state: %s", exc)


def check_health(
    aw,
    *,
    sleep: Callable[[float], None] = time.sleep,
    alert: Callable[[str], None] = send_alert,
    state_path: Path | None = None,
) -> bool:
    """Alert on a dead data source. Returns False iff aw-server is down (the
    sync can't run); a dead window-watcher alerts but lets the sync proceed."""
    if state_path is None:
        state_path = ALERT_STATE_PATH
    problem = _problem(aw)
    if problem is not None:
        sleep(RECHECK_SEC)
        problem = _problem(aw)
    if problem is not None:
        log.error(problem)
    previous = _read_state(state_path)
    current = problem or ""
    if current != previous:
        alert(problem if problem is not None else _RECOVERED.format(previous))
        _write_state(state_path, current)
    return problem != _SERVER_DOWN
