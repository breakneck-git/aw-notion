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
from .git_context import find_git_branch
from .notion import NotionTimeLogClient, block_dedup_key
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


def sync(dry_run: bool = False, since: str | None = None, debug: bool = False) -> None:
    try:
        with _acquire_lock(LOCK_PATH):
            _run_sync(dry_run=dry_run, since=since, debug=debug)
    except BlockingIOError:
        log.info("another sync in progress, skipping")


def _log_blocks_debug(blocks, ax_intervals, state_sigs) -> None:
    log.info("=== DEBUG: %d focus blocks ===", len(blocks))
    for b in blocks:
        sig = b.signature()
        marker = "SKIP" if sig in state_sigs else "NEW "
        log.info(
            "[%s] %s %s->%s dur=%ds note=%r title=%r sig=%s",
            marker,
            b.app,
            b.start_utc.strftime("%m-%d %H:%M:%S"),
            b.end_utc.strftime("%H:%M:%S"),
            int(b.active_seconds),
            b.note,
            b.title,
            sig[:8],
        )
        if b.app == "Claude":
            overlapping = [
                (s, e, ap, ctx)
                for s, e, ap, ctx in ax_intervals
                if e >= b.start_utc and s <= b.end_utc
            ]
            if overlapping:
                log.info("       ax overlap candidates:")
                for s, e, ap, ctx in overlapping:
                    app_match = "app_match" if ap == b.app else f"app={ap!r}"
                    log.info(
                        "         %s->%s %s ctx=%r",
                        s.strftime("%H:%M:%S"),
                        e.strftime("%H:%M:%S"),
                        app_match,
                        ctx,
                    )
            else:
                log.info("       no ax events overlap this block's time range")
    log.info("=== END DEBUG ===")


def _parse_since(since: str) -> datetime:
    dt = datetime.fromisoformat(since)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _looks_like_path(title: str) -> bool:
    return bool(title) and (title.startswith("~/") or title.startswith("/"))


def _filter_excluded(blocks, sync_cfg):
    """Drop blocks matching user-configured exclusion rules.

    Returns (kept_blocks, excluded_count). Matching is case-insensitive:
    - `exclude_apps`: exact match against block.app
    - `exclude_url_substrings`: substring match against block.url
    - `exclude_title_substrings`: substring match against block.title
    """
    if not (
        sync_cfg.exclude_apps
        or sync_cfg.exclude_url_substrings
        or sync_cfg.exclude_title_substrings
    ):
        return list(blocks), 0

    excluded_apps = {a.lower() for a in sync_cfg.exclude_apps}
    excluded_url_subs = [s.lower() for s in sync_cfg.exclude_url_substrings]
    excluded_title_subs = [s.lower() for s in sync_cfg.exclude_title_substrings]

    kept = []
    excluded = 0
    for b in blocks:
        if b.app and b.app.lower() in excluded_apps:
            excluded += 1
            continue
        if b.url and excluded_url_subs:
            u = b.url.lower()
            if any(s in u for s in excluded_url_subs):
                excluded += 1
                continue
        if b.title and excluded_title_subs:
            t = b.title.lower()
            if any(s in t for s in excluded_title_subs):
                excluded += 1
                continue
        kept.append(b)
    return kept, excluded


def _run_sync(dry_run: bool, since: str | None, debug: bool = False) -> None:
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
        backfill = True
    elif state.last_sync is None:
        start = now - timedelta(days=cfg.sync.initial_sync_days)
        log.info("First run: syncing last %d days", cfg.sync.initial_sync_days)
        backfill = True
    else:
        start = state.last_sync - timedelta(minutes=30)
        log.info("Incremental sync from %s", start.isoformat())
        backfill = False

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

    blocks, excluded_count = _filter_excluded(blocks, cfg.sync)
    if excluded_count:
        log.info(
            "Excluded %d block(s) per config (apps=%s, url_substrings=%s, title_substrings=%s)",
            excluded_count,
            cfg.sync.exclude_apps,
            cfg.sync.exclude_url_substrings,
            cfg.sync.exclude_title_substrings,
        )

    for block in blocks:
        if block.note is None and _looks_like_path(block.title):
            block.note = find_git_branch(block.title, block.end_utc)

    if debug:
        ax_intervals = aw.fetch_ax_intervals(start, now)
        _log_blocks_debug(blocks, ax_intervals, state.notion_entries)

    notion = (
        NotionTimeLogClient(cfg.notion.token, cfg.notion.timelog_db, fields=cfg.notion.fields)
        if not dry_run
        else None
    )

    # Backfill (--since / first run) reaches past the state prune window, so
    # signature-based dedup can't see entries already in Notion and would
    # recreate them as duplicates. Pull existing keys straight from Notion and
    # gate creation on them too. Read-only, so we run it even in dry-run (for an
    # honest "would create" count). Skipped on plain incremental syncs — state
    # dedup covers the 30-min rewind and we avoid a query every 15 minutes.
    existing_keys: set[tuple[str, str]] = set()
    if backfill:
        reader = notion or NotionTimeLogClient(
            cfg.notion.token, cfg.notion.timelog_db, fields=cfg.notion.fields
        )
        existing_keys = reader.fetch_existing_keys(start)
        log.info("Backfill dedup: %d existing Notion entries in window", len(existing_keys))

    tz = ZoneInfo(cfg.timezone)
    new_count = 0

    for block in blocks:
        sig = block.signature()
        already_synced = sig in state.notion_entries
        already_in_notion = block_dedup_key(block.app, block.start_utc) in existing_keys
        if already_synced or already_in_notion:
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
    sync_p.add_argument(
        "--debug",
        action="store_true",
        help="dump each computed focus block with overlap diagnostics for Claude",
    )
    args = parser.parse_args()
    if args.cmd == "sync":
        sync(dry_run=args.dry_run, since=args.since, debug=args.debug)
