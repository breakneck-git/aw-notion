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
trips it. That relative check is blind when both watchers die together (lag
stays ~0), so the newest heartbeat of *either* is also held against the wall
clock — window-watcher heartbeats continuously while alive, so this only trips
when both are silent (afk alone can't be used: it stalls 11–34 min by itself).

Every failed check is re-run once after RECHECK_SEC, and only a problem seen
by both runs alerts. That absorbs the benign races: the RunAtLoad sync at
login firing before ActivityWatch has started (and, once it has, before the
watchers' first heartbeat), and the sync launchd fires on wake running before
the watchers' first post-sleep heartbeat.
"""

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

RECHECK_SEC = 60
WINDOW_LAG_ALERT_SEC = 10 * 60
WATCHERS_SILENT_ALERT_SEC = 10 * 60
# Last alerted problem ("" = healthy). Alerts go out only on a state change —
# one on breakage, one on recovery — not on every 15-minute sync.
ALERT_STATE_PATH = Path.home() / ".config" / "aw-notion" / "alert.state"

_PROJECT = "aw-notion"
_SERVER_DOWN = "aw-server недоступен — время не пишется. Запусти ActivityWatch."
_WINDOW_DEAD = (
    "aw-watcher-window не пишет события — записей в Notion не будет. "
    "Проверь: launchctl print gui/$(id -u)/com.aw-watcher-window.keepalive"
)
_BUCKETS_FAILED = "aw-server не отдаёт список buckets — синхронизация не идёт."
_WATCHERS_SILENT = (
    "aw-watcher-window и aw-watcher-afk молчат больше 10 минут — записей в Notion не будет. "
    "Проверь, запущен ли ActivityWatch и его watcher'ы."
)
_RECOVERED = "Источники восстановлены, записи снова пишутся. Было: {}"


def _parse_stamp(raw) -> datetime | None:
    """aw-server's last_updated; naive is taken as UTC, garbage as absent."""
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _newest(buckets: dict, prefix: str) -> datetime | None:
    stamps = [
        _parse_stamp(meta.get("last_updated"))
        for bucket_id, meta in buckets.items()
        if bucket_id.startswith(prefix) and isinstance(meta, dict)
    ]
    return max((s for s in stamps if s is not None), default=None)


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


def _desktop(message: str) -> bool:
    osascript = _which("osascript")
    if osascript is None:
        return False
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
        res = subprocess.run(args + [_PROJECT, message], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("desktop notification failed: %s", exc)
        return False
    return res.returncode == 0


def send_alert(message: str) -> bool:
    """Telegram (Mon&Mesh) first; macOS notification if that's unavailable.
    True if either channel took it. Never raises — alerting must not break
    the sync."""
    return _telegram(message) or _desktop(message)


def _problem(aw, now: datetime) -> str | None:
    if not aw.is_running():
        return _SERVER_DOWN
    try:
        buckets = aw.buckets()
    except Exception as exc:  # any failure here would otherwise kill the sync silently
        log.warning("GET /buckets failed: %r", exc)
        return _BUCKETS_FAILED
    lag = window_lag_sec(buckets)
    if lag is not None and lag > WINDOW_LAG_ALERT_SEC:
        return _WINDOW_DEAD
    # Only with a window bucket present: afk alone stalls for minutes by itself.
    window = _newest(buckets, "aw-watcher-window")
    if window is not None:
        afk = _newest(buckets, "aw-watcher-afk")
        newest = max(window, afk) if afk is not None else window
        if (now - newest).total_seconds() > WATCHERS_SILENT_ALERT_SEC:
            return _WATCHERS_SILENT
    return None


def _read_state(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):  # ValueError: UnicodeDecodeError on a corrupt file
        return ""


def _write_state(path: Path, value: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        log.warning("could not persist alert state: %s", exc)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def check_health(
    aw,
    *,
    sleep: Callable[[float], None] = time.sleep,
    alert: Callable[[str], bool] = send_alert,
    state_path: Path | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> bool:
    """Alert on a dead data source. Returns False iff the sync can't run
    (aw-server down or its bucket list failing); dead watchers alert but let
    the sync proceed."""
    if state_path is None:
        state_path = ALERT_STATE_PATH
    first = _problem(aw, now())
    problem = first
    if first is not None:
        sleep(RECHECK_SEC)
        problem = _problem(aw, now())
    can_sync = problem not in (_SERVER_DOWN, _BUCKETS_FAILED)
    if problem is not None and problem != first:
        # Two different problems in a row: things are still moving (typically
        # login). Neither is confirmed — no alert, no state change.
        log.warning("unconfirmed data-source problem: %s", problem)
        return can_sync
    if problem is not None:
        log.error(problem)
    previous = _read_state(state_path)
    current = problem or ""
    if current != previous:
        # Persist only once delivered, so an undelivered alert is retried.
        if alert(problem if problem is not None else _RECOVERED.format(previous)):
            _write_state(state_path, current)
    return can_sync
