import argparse
import fcntl
import logging
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .activitywatch import ActivityWatchClient
from .blocks import compute_focus_blocks
from .config import load_config
from .notion import NotionTimeLogClient
from .state import STATE_PATH, State

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

LOCK_PATH = Path.home() / ".config" / "aw-notion" / "sync.lock"


@contextmanager
def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def sync(dry_run: bool = False, since: str | None = None) -> None:
    try:
        with _acquire_lock(LOCK_PATH):
            _run_sync(dry_run=dry_run, since=since)
    except BlockingIOError:
        log.info("another sync in progress, skipping")


def _parse_since(since: str) -> datetime:
    dt = datetime.fromisoformat(since)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _run_sync(dry_run: bool, since: str | None) -> None:
    cfg = load_config()
    state = State.load(STATE_PATH)

    aw = ActivityWatchClient(cfg.activitywatch.base_url)
    if not aw.is_running():
        log.warning("ActivityWatch is not running, skipping sync")
        return

    now = datetime.now(tz=UTC)

    if since is not None:
        start = _parse_since(since)
        log.info("Override start to %s", start.isoformat())
    elif state.last_sync is None:
        start = now - timedelta(days=cfg.sync.initial_sync_days)
        log.info("First run: syncing last %d days", cfg.sync.initial_sync_days)
    else:
        start = state.last_sync - timedelta(minutes=30)
        log.info("Incremental sync from %s", start.isoformat())

    window_events, afk_events = aw.get_all_events(
        start, now, browser_apps=cfg.activitywatch.browser_apps
    )
    blocks = compute_focus_blocks(
        window_events,
        afk_events,
        afk_threshold_sec=cfg.activitywatch.afk_threshold_min * 60,
        merge_gap_sec=cfg.activitywatch.merge_gap_sec,
        min_duration_sec=cfg.activitywatch.min_block_duration_sec,
    )
    log.info("Found %d focus blocks in range", len(blocks))

    notion = (
        NotionTimeLogClient(cfg.notion.token, cfg.notion.timelog_db, fields=cfg.notion.fields)
        if not dry_run
        else None
    )
    tz = ZoneInfo(cfg.timezone)
    new_count = 0

    for block in blocks:
        sig = block.signature()
        if sig in state.notion_entries:
            continue

        if dry_run:
            log.info(
                "DRY RUN would create: sig=%s app=%s title=%r minutes=%d",
                sig[:8],
                block.app,
                block.title,
                block.active_minutes(),
            )
            new_count += 1
            continue

        try:
            assert notion is not None
            page_id = notion.create_entry(block, tz)
            state.notion_entries[sig] = {
                "page_id": page_id,
                "created_at": datetime.now(tz=UTC).isoformat(),
            }
            new_count += 1
        except Exception as exc:
            log.error("Failed to create Notion entry for '%s': %s", block.title, exc)
            state.save(STATE_PATH)
            sys.exit(1)

    if not dry_run:
        state.last_sync = now
        state.save(STATE_PATH)
    log.info("Synced %d new entries%s", new_count, " (dry-run)" if dry_run else "")


def main() -> None:
    parser = argparse.ArgumentParser(prog="aw-notion")
    subs = parser.add_subparsers(dest="cmd", required=True)
    sync_p = subs.add_parser("sync", help="sync ActivityWatch -> Notion")
    sync_p.add_argument(
        "--dry-run",
        action="store_true",
        help="compute blocks and log, skip Notion writes and state save",
    )
    sync_p.add_argument(
        "--since",
        type=str,
        metavar="ISO8601",
        help="override start time (overrides incremental/initial-sync logic)",
    )
    args = parser.parse_args()
    if args.cmd == "sync":
        sync(dry_run=args.dry_run, since=args.since)
