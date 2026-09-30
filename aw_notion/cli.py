import argparse
import fcntl
import logging
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

from notion_client.errors import HTTPResponseError

from .activitywatch import ActivityWatchClient
from .blocks import cluster_blocks, compute_focus_blocks
from .config import load_config
from .git_context import find_git_branch
from .health import check_health
from .notion import NotionTimeLogClient, block_dedup_key
from .state import STATE_PATH, State

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

LOCK_PATH = Path.home() / ".config" / "aw-notion" / "sync.lock"

# Incremental syncs re-read this far before the last sync (and before any block
# still open then), so late-arriving events are picked up; state dedup makes the
# overlap free.
REWIND = timedelta(minutes=30)
# Slack between the fetch start and merge_gap before the commit range, so a
# boundary event exactly merge_gap away can't be merged in one run and not another.
FETCH_MARGIN = timedelta(minutes=1)


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


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


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
    - `exclude_title_substrings`: substring match against block.title and
      block.note (the ax context, e.g. a chat name, is just as private)
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
        if excluded_title_subs:
            texts = [t.lower() for t in (b.title, b.note) if t]
            if any(s in t for t in texts for s in excluded_title_subs):
                excluded += 1
                continue
        kept.append(b)
    return kept, excluded


def _log_alert_only(message: str) -> bool:
    log.warning("DRY RUN would alert: %s", message)
    return False


def _run_sync(dry_run: bool, since: str | None, debug: bool = False) -> None:
    cfg = load_config()
    state = State.load(STATE_PATH)

    aw = ActivityWatchClient(cfg.activitywatch.base_url)
    # Alerts (ERROR + Telegram/desktop) on a down server or dead watchers —
    # both otherwise just yield "0 focus blocks" forever, silently. A dry run
    # only logs them (and, as undelivered, doesn't record them as sent).
    health_kwargs = {"alert": _log_alert_only} if dry_run else {}
    if not check_health(aw, **health_kwargs):
        log.warning("ActivityWatch unavailable, skipping sync")
        return

    now = _utcnow()

    # Blocks are written only when they are *settled*: whole (start inside the
    # commit range) and finished (ended over merge_gap ago, so no later event
    # can extend them). aw-server clips an event overlapping the query start to
    # that start, so a block reaching back past the fetch start comes back with
    # a fake start → a new signature → a duplicate row every sync. Fetching
    # merge_gap (+margin) before commit_from means any block starting at or
    # after commit_from has all its events in view and its true start.
    if since is not None:
        commit_from = _parse_since(since)
        log.info("Override start to %s", commit_from.isoformat())
        backfill = True
    elif state.last_sync is None:
        commit_from = now - timedelta(days=cfg.sync.initial_sync_days)
        log.info("First run: syncing last %d days", cfg.sync.initial_sync_days)
        backfill = True
    else:
        anchor = state.last_sync
        if state.pending_since is not None and state.pending_since < anchor:
            anchor = state.pending_since
        commit_from = anchor - REWIND
        log.info("Incremental sync from %s", commit_from.isoformat())
        backfill = False

    # A block is settled merge_gap after it ends; a cluster of pieces only
    # cluster_gap after its last piece (a later piece could still join). The
    # fetch reaches back as far, so a piece just before commit_from — which
    # makes the next piece a continuation, not a new row — is in view.
    lookback = timedelta(
        seconds=max(cfg.activitywatch.merge_gap_sec, cfg.activitywatch.cluster_gap_sec)
    )
    start = commit_from - lookback - FETCH_MARGIN
    settled_before = now - lookback

    window_events, afk_events = aw.get_all_events(
        start, now, browser_apps=cfg.activitywatch.browser_apps
    )
    # min_duration applied below, after clustering and the open/settled split:
    # a piece too short on its own may still be part of a long enough activity.
    pieces = compute_focus_blocks(
        window_events,
        afk_events,
        afk_threshold_sec=cfg.activitywatch.afk_threshold_min * 60,
        merge_gap_sec=cfg.activitywatch.merge_gap_sec,
        min_duration_sec=0,
    )
    # Excluded apps must not glue the pieces around them into one activity,
    # and the git note is part of an activity's identity — both before clustering.
    pieces, excluded_count = _filter_excluded(pieces, cfg.sync)
    if excluded_count:
        log.info(
            "Excluded %d block(s) per config (apps=%s, url_substrings=%s, title_substrings=%s)",
            excluded_count,
            cfg.sync.exclude_apps,
            cfg.sync.exclude_url_substrings,
            cfg.sync.exclude_title_substrings,
        )
    for piece in pieces:
        if piece.note is None and _looks_like_path(piece.title):
            piece.note = find_git_branch(piece.title, piece.end_utc)
    all_blocks = cluster_blocks(pieces, gap_sec=cfg.activitywatch.cluster_gap_sec)
    # Open = may still grow. Window events are included too: one fully covered
    # by AFK so far has no block yet, but gains one when the user returns.
    open_starts = [b.start_utc for b in all_blocks if b.end_utc > settled_before]
    open_starts += [
        e.timestamp
        for e in window_events
        if e.timestamp + timedelta(seconds=e.duration) > settled_before
    ]
    pending_since = min(open_starts, default=None)
    blocks = [
        b
        for b in all_blocks
        if b.start_utc >= commit_from
        and b.end_utc <= settled_before
        and b.active_seconds >= cfg.activitywatch.min_block_duration_sec
    ]
    log.info("Found %d focus blocks in range", len(blocks))

    if debug:
        ax_intervals = aw.fetch_ax_intervals(start, now)
        _log_blocks_debug(blocks, ax_intervals, state.notion_entries)

    # Constructing the client is side-effect-free (no network until a call is
    # made), so build it unconditionally; dry-run is enforced at the write site
    # below, not by withholding the client.
    notion = NotionTimeLogClient(cfg.notion.token, cfg.notion.timelog_db, fields=cfg.notion.fields)

    # Backfill (--since / first run) reaches past the state prune window, so
    # signature-based dedup can't see entries already in Notion and would
    # recreate them as duplicates. Pull existing keys straight from Notion and
    # gate creation on them too. Read-only, so we run it even in dry-run (for an
    # honest "would create" count). Skipped on plain incremental syncs — state
    # dedup covers the 30-min rewind and we avoid a query every 15 minutes.
    # Same query after a failed write that may have landed anyway (#5).
    existing_keys: set[tuple[str, str]] = set()
    if backfill or state.verify_notion:
        existing_keys = notion.fetch_existing_keys(commit_from)
        log.info("Notion dedup: %d existing entries in window", len(existing_keys))

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
            page_id = notion.create_entry(block, tz)
        except HTTPResponseError as exc:
            if exc.status != 400:
                _abort_sync(state, block, exc)
            # 400 = Notion rejected this block's content; a retry gets the same
            # answer, so stopping here would wedge every later sync behind it.
            # Record it as handled and move on.
            log.error("Notion rejected entry for '%s', skipping it: %s", block.title, exc)
            state.notion_entries[sig] = {
                "page_id": None,
                "created_at": datetime.now(tz=UTC).isoformat(),
                "error": str(exc)[:200],
            }
            continue
        except Exception as exc:
            _abort_sync(state, block, exc)
        state.notion_entries[sig] = {
            "page_id": page_id,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        new_count += 1

    if not dry_run:
        state.last_sync = now
        state.pending_since = pending_since
        state.verify_notion = False
        state.save(STATE_PATH)
    log.info("Synced %d new entries%s", new_count, " (dry-run)" if dry_run else "")


def _abort_sync(state: State, block, exc: Exception) -> NoReturn:
    """Stop without advancing last_sync, so the next sync retries. Auth/config
    and server errors would fail every block alike — skipping would drop them
    all. The write may also have landed despite the error (timeout, dropped
    connection, 5xx), so the retry checks Notion first."""
    log.error("Failed to create Notion entry for '%s': %s", block.title, exc)
    state.verify_notion = True
    state.save(STATE_PATH)
    sys.exit(1)


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
