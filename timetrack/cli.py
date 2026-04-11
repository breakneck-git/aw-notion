import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .activitywatch import ActivityWatchClient
from .blocks import compute_focus_blocks
from .config import load_config
from .notion import NotionTimeLogClient
from .state import State

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def sync() -> None:
    cfg = load_config()
    state = State.load()

    aw = ActivityWatchClient(cfg.activitywatch.base_url)
    if not aw.is_running():
        log.warning("ActivityWatch is not running, skipping sync")
        sys.exit(0)

    now = datetime.now(tz=timezone.utc)

    if state.last_sync is None:
        start = now - timedelta(days=cfg.sync.initial_sync_days)
        log.info("First run: syncing last %d days", cfg.sync.initial_sync_days)
    else:
        start = state.last_sync - timedelta(minutes=30)
        log.info("Incremental sync from %s", start.isoformat())

    window_events, afk_events = aw.get_all_events(start, now)
    blocks = compute_focus_blocks(
        window_events,
        afk_events,
        afk_threshold_sec=cfg.activitywatch.afk_threshold_min * 60,
        merge_gap_sec=cfg.activitywatch.merge_gap_sec,
        min_duration_sec=cfg.activitywatch.min_block_duration_sec,
    )
    log.info("Found %d focus blocks in range", len(blocks))

    notion = NotionTimeLogClient(cfg.notion.token, cfg.notion.timelog_db)
    tz = ZoneInfo(cfg.timezone)
    new_count = 0

    for block in blocks:
        sig = block.signature()
        if sig in state.notion_entries:
            continue

        try:
            page_id = notion.create_entry(block, tz)
            state.notion_entries[sig] = page_id
            new_count += 1
        except Exception as exc:
            log.error("Failed to create Notion entry for '%s': %s", block.title, exc)
            state.save()
            sys.exit(1)

    state.last_sync = now
    state.save()
    log.info("Synced %d new entries", new_count)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "sync":
        print("Usage: timetrack sync")
        sys.exit(1)
    sync()
