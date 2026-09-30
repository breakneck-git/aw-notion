import fcntl
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from notion_client.errors import HTTPResponseError, RequestTimeoutError

from aw_notion import cli
from aw_notion.blocks import AFKEvent, AWEvent, FocusBlock
from aw_notion.cli import _acquire_lock, _filter_excluded, main, sync
from aw_notion.config import (
    ActivityWatchConfig,
    Config,
    NotionConfig,
    SyncConfig,
)
from aw_notion.notion import block_dedup_key


def test_acquire_lock_succeeds_when_free(tmp_path):
    lock = tmp_path / "sync.lock"
    with _acquire_lock(lock):
        assert lock.exists()
    with _acquire_lock(lock):
        pass


def test_acquire_lock_raises_when_held(tmp_path):
    lock = tmp_path / "sync.lock"
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(BlockingIOError):
            with _acquire_lock(lock):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    fake_cfg = Config(
        notion=NotionConfig(token="t", timelog_db="db"),
        activitywatch=ActivityWatchConfig(cluster_gap_sec=0),
        sync=SyncConfig(),
        timezone="UTC",
    )
    monkeypatch.setattr(cli, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(cli, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "sync.lock")

    captured = {"aw_start": None, "aw_end": None, "notion_calls": []}

    class FakeAW:
        def __init__(self, *a, **k):
            pass

        def is_running(self):
            return True

        def get_all_events(self, start, end, browser_apps=None):
            captured["aw_start"] = start
            captured["aw_end"] = end
            captured["browser_apps"] = browser_apps
            # Settled (ended well before `end`) and inside any commit range.
            captured["event_ts"] = end - timedelta(minutes=30)
            evt = AWEvent(
                timestamp=captured["event_ts"],
                duration=300.0,
                app="Code",
                title="test.py",
            )
            return [evt], []

    class FakeNotion:
        def __init__(self, *a, **k):
            pass

        def create_entry(self, block, tz):
            captured["notion_calls"].append(block)
            return "page-xyz"

        def fetch_existing_keys(self, start_utc):
            captured["existing_keys_queried_from"] = start_utc
            if captured.get("seed_existing"):
                return {block_dedup_key("Code", captured["event_ts"])}
            return set()

    monkeypatch.setattr(cli, "ActivityWatchClient", FakeAW)
    monkeypatch.setattr(cli, "NotionTimeLogClient", FakeNotion)
    captured["tmp_path"] = tmp_path
    captured["cfg"] = fake_cfg
    return captured


def test_sync_runs_health_check_before_fetching(sync_env, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "check_health", lambda aw: seen.append(aw) or True)
    sync()
    assert len(seen) == 1
    assert len(sync_env["notion_calls"]) == 1


def test_dry_run_health_check_does_not_send_alerts(sync_env, monkeypatch):
    """--dry-run is for looking, not acting: it must not fire real Telegram
    alerts (nor record them as sent)."""
    seen = {}

    def fake_check(aw, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(cli, "check_health", fake_check)
    sync(dry_run=True)
    assert "alert" in seen, "dry-run must override the alert channel"
    assert seen["alert"]("aw-server down") is False


def test_sync_skips_when_health_check_reports_server_down(sync_env, monkeypatch):
    monkeypatch.setattr(cli, "check_health", lambda aw: False)
    sync()
    assert sync_env["aw_start"] is None
    assert sync_env["notion_calls"] == []


def test_sync_dry_run_skips_notion(sync_env):
    sync(dry_run=True)
    assert sync_env["notion_calls"] == []


def test_sync_without_dry_run_calls_notion(sync_env):
    sync()
    assert len(sync_env["notion_calls"]) == 1


def test_sync_dry_run_does_not_write_state(sync_env):
    sync(dry_run=True)
    assert not (sync_env["tmp_path"] / "state.json").exists()


def test_sync_since_overrides_start(sync_env):
    sync(dry_run=True, since="2026-04-05T00:00:00")
    expected = datetime(2026, 4, 5, 0, 0, tzinfo=UTC)
    # Fetch reaches merge_gap(+margin) earlier so boundary blocks aren't clipped.
    assert sync_env["aw_start"] == expected - timedelta(seconds=180) - cli.FETCH_MARGIN


def test_sync_since_skips_entries_already_in_notion(sync_env):
    """H9 regression: a --since backfill must NOT recreate an entry that already
    exists in Notion (whose signature was pruned from local state). Seed
    Notion with the key of the one 'Code' block the fake AW emits."""
    sync_env["seed_existing"] = True
    sync(since="2026-04-05T00:00:00")
    assert sync_env["notion_calls"] == [], "existing Notion entry must be skipped, not duplicated"
    # The Notion-side dedup query was scoped to the --since window start.
    assert sync_env["existing_keys_queried_from"] == datetime(2026, 4, 5, 0, 0, tzinfo=UTC)


def test_sync_since_creates_when_absent(sync_env):
    """Counterpart: with no matching Notion entry, --since still creates."""
    sync(since="2026-04-05T00:00:00")
    assert len(sync_env["notion_calls"]) == 1


def test_main_parses_dry_run(monkeypatch):
    captured = {}

    def fake_sync(dry_run=False, since=None, debug=False):
        captured["dry_run"] = dry_run
        captured["since"] = since

    monkeypatch.setattr(cli, "sync", fake_sync)
    monkeypatch.setattr("sys.argv", ["aw-notion", "sync", "--dry-run"])
    main()
    assert captured == {"dry_run": True, "since": None}


def test_main_parses_since(monkeypatch):
    captured = {}

    def fake_sync(dry_run=False, since=None, debug=False):
        captured["since"] = since

    monkeypatch.setattr(cli, "sync", fake_sync)
    monkeypatch.setattr("sys.argv", ["aw-notion", "sync", "--since", "2026-04-05T00:00:00"])
    main()
    assert captured["since"] == "2026-04-05T00:00:00"


def test_main_no_command_exits_error(monkeypatch):
    monkeypatch.setattr("sys.argv", ["aw-notion"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_sync_returns_cleanly_when_lock_held(tmp_path, monkeypatch, caplog):
    lock = tmp_path / "sync.lock"
    monkeypatch.setattr("aw_notion.cli.LOCK_PATH", lock)
    caplog.set_level(logging.INFO, logger="aw_notion.cli")

    fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        sync()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert "another sync in progress" in caplog.text.lower()


def test_sync_runs_git_fallback_for_path_like_titles(tmp_path, monkeypatch):
    """
    Blocks whose title starts with ~/ or / and whose note is still None
    should get their note filled from find_git_branch.
    """
    fake_cfg = Config(
        notion=NotionConfig(token="t", timelog_db="db"),
        activitywatch=ActivityWatchConfig(cluster_gap_sec=0),
        sync=SyncConfig(),
        timezone="UTC",
    )
    monkeypatch.setattr(cli, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(cli, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "sync.lock")

    blocks: list = []
    git_calls: list[str] = []

    class FakeAW:
        def __init__(self, *a, **k):
            pass

        def is_running(self):
            return True

        def get_all_events(self, start, end, browser_apps=None):
            return (
                [
                    AWEvent(
                        timestamp=end - timedelta(minutes=30),
                        duration=300.0,
                        app="Ghostty",
                        title="~/code/aw-notion",
                    ),
                    AWEvent(
                        timestamp=end - timedelta(minutes=20),
                        duration=300.0,
                        app="Chrome",
                        title="GitHub — Chrome",
                    ),
                ],
                [],
            )

    class FakeNotion:
        def __init__(self, *a, **k):
            pass

        def create_entry(self, block, tz):
            blocks.append(block)
            return f"page-{len(blocks)}"

        def fetch_existing_keys(self, start_utc):
            return set()

    def fake_find_git_branch(path, block_end):
        git_calls.append(path)
        return "aw-notion @ main"

    monkeypatch.setattr(cli, "ActivityWatchClient", FakeAW)
    monkeypatch.setattr(cli, "NotionTimeLogClient", FakeNotion)
    monkeypatch.setattr(cli, "find_git_branch", fake_find_git_branch)

    sync()

    assert len(blocks) == 2
    path_block = next(b for b in blocks if b.title == "~/code/aw-notion")
    non_path_block = next(b for b in blocks if b.title == "GitHub — Chrome")
    assert path_block.note == "aw-notion @ main"
    assert non_path_block.note is None
    assert git_calls == ["~/code/aw-notion"]


def test_sync_git_fallback_does_not_override_existing_note(tmp_path, monkeypatch):
    """
    If ax-watcher already set a note on the block, the git fallback must
    not overwrite it (git reflog is a fallback, not a primary source).
    """
    fake_cfg = Config(
        notion=NotionConfig(token="t", timelog_db="db"),
        activitywatch=ActivityWatchConfig(cluster_gap_sec=0),
        sync=SyncConfig(),
        timezone="UTC",
    )
    monkeypatch.setattr(cli, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(cli, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "sync.lock")

    blocks: list = []
    git_called = False

    class FakeAW:
        def __init__(self, *a, **k):
            pass

        def is_running(self):
            return True

        def get_all_events(self, start, end, browser_apps=None):
            return (
                [
                    AWEvent(
                        timestamp=end - timedelta(minutes=30),
                        duration=300.0,
                        app="Ghostty",
                        title="~/code/aw-notion",
                        note="already set by ax",
                    )
                ],
                [],
            )

    class FakeNotion:
        def __init__(self, *a, **k):
            pass

        def create_entry(self, block, tz):
            blocks.append(block)
            return "page-xyz"

        def fetch_existing_keys(self, start_utc):
            return set()

    def fake_find_git_branch(path, block_end):
        nonlocal git_called
        git_called = True
        return "should-not-be-used"

    monkeypatch.setattr(cli, "ActivityWatchClient", FakeAW)
    monkeypatch.setattr(cli, "NotionTimeLogClient", FakeNotion)
    monkeypatch.setattr(cli, "find_git_branch", fake_find_git_branch)

    sync()

    assert git_called is False
    assert blocks[0].note == "already set by ax"


# ---------------------------------------------------------------------------
# Exclusion filter (cli._filter_excluded)
# ---------------------------------------------------------------------------
def _b(app: str, url: str | None = None, title: str = "x") -> FocusBlock:
    base = datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC)
    return FocusBlock(
        app=app,
        title=title,
        start_utc=base,
        end_utc=base + timedelta(seconds=300),
        active_seconds=300,
        url=url,
    )


def test_filter_excluded_by_title_substring():
    """Privacy: a sensitive page can carry a *clean* URL (wrong-tab enrichment),
    so title-based exclusion must catch it independent of the URL."""
    blocks = [
        _b("Comet", url="https://clean.example.com", title="Pornhub - Best Of - Comet"),
        _b("Comet", url="https://github.com/x/y", title="GitHub - Comet"),
    ]
    cfg = SyncConfig(exclude_title_substrings=["pornhub"])
    kept, n = _filter_excluded(blocks, cfg)
    assert n == 1
    assert len(kept) == 1
    assert kept[0].title == "GitHub - Comet"


def test_filter_excluded_title_substring_also_matches_note():
    """Privacy: the note carries the ax context (e.g. a Telegram chat name) —
    a title rule must keep it out of Notion just like a matching title."""
    b = _b("Telegram", title="Telegram")
    b.note = "Secret Project chat"
    kept, n = _filter_excluded([b], SyncConfig(exclude_title_substrings=["secret project"]))
    assert (kept, n) == ([], 1)


def test_filter_excluded_no_rules_returns_all():
    blocks = [_b("Code"), _b("Chrome", url="https://example.com")]
    kept, n = _filter_excluded(blocks, SyncConfig())
    assert n == 0
    assert kept == blocks


def test_filter_excluded_by_app_exact_match():
    blocks = [_b("Telegram"), _b("Code"), _b("loginwindow")]
    cfg = SyncConfig(exclude_apps=["Telegram", "loginwindow"])
    kept, n = _filter_excluded(blocks, cfg)
    assert n == 2
    assert [b.app for b in kept] == ["Code"]


def test_filter_excluded_app_match_is_case_insensitive():
    blocks = [_b("TELEGRAM"), _b("telegram"), _b("Code")]
    cfg = SyncConfig(exclude_apps=["Telegram"])
    kept, n = _filter_excluded(blocks, cfg)
    assert n == 2
    assert [b.app for b in kept] == ["Code"]


def test_filter_excluded_by_url_substring():
    blocks = [
        _b("Chrome", url="https://www.pornhub.com/view_video.php?id=abc"),
        _b("Chrome", url="https://github.com/x/y"),
        _b("Chrome", url="https://m.pornhub.com/other"),
    ]
    cfg = SyncConfig(exclude_url_substrings=["pornhub"])
    kept, n = _filter_excluded(blocks, cfg)
    assert n == 2
    assert kept[0].url == "https://github.com/x/y"


def test_filter_excluded_url_match_is_case_insensitive():
    blocks = [_b("Chrome", url="https://PornHub.com/x")]
    cfg = SyncConfig(exclude_url_substrings=["pornhub"])
    kept, n = _filter_excluded(blocks, cfg)
    assert n == 1
    assert kept == []


def test_filter_excluded_combined_app_and_url():
    blocks = [
        _b("Telegram"),
        _b("Chrome", url="https://pornhub.com/x"),
        _b("Code"),
    ]
    cfg = SyncConfig(
        exclude_apps=["Telegram"],
        exclude_url_substrings=["pornhub"],
    )
    kept, n = _filter_excluded(blocks, cfg)
    assert n == 2
    assert [b.app for b in kept] == ["Code"]


def test_filter_excluded_url_check_skipped_when_no_url():
    """Block with url=None must not crash the substring check."""
    blocks = [_b("Code", url=None)]
    cfg = SyncConfig(exclude_url_substrings=["anything"])
    kept, n = _filter_excluded(blocks, cfg)
    assert n == 0
    assert kept == blocks


class ClippingAW:
    """Fake aw-server that behaves like the real one on range queries: an event
    overlapping the query's start is returned clipped to it (verified live:
    01:43:24/1728s → 01:57:48/864s), and an event still being heartbeated is
    only as long as `now` lets it be."""

    def __init__(self, events, clock, afk=()):
        self._events = events  # [(app, title, start, end)] — true, unclipped
        self._clock = clock
        self._afk = afk  # [(start, end)]

    def is_running(self):
        return True

    def get_all_events(self, start, end, browser_apps=None):
        out = []
        for app, title, ev_start, ev_end in self._events:
            ev_end = min(ev_end, self._clock["now"])
            lo, hi = max(ev_start, start), min(ev_end, end)
            if hi > lo:
                out.append(
                    AWEvent(timestamp=lo, duration=(hi - lo).total_seconds(), app=app, title=title)
                )
        afk = [
            AFKEvent(timestamp=a_s, duration=(a_e - a_s).total_seconds(), status="afk")
            for a_s, a_e in self._afk
        ]
        return out, afk


@pytest.fixture
def clipping_env(tmp_path, monkeypatch):
    cfg = Config(
        notion=NotionConfig(token="t", timelog_db="db"),
        activitywatch=ActivityWatchConfig(),
        sync=SyncConfig(),
        timezone="UTC",
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "sync.lock")
    # fail: {app: exception} raised once for that app's next create_entry.
    # lost_response: apps whose POST lands in Notion but whose reply is lost.
    env = {
        "events": [],
        "afk": [],
        "clock": {},
        "created": [],
        "fail": {},
        "lost_response": set(),
        "attempts": [],
        "keys_queried": [],
        "cfg": cfg,
    }

    class FakeNotion:
        def __init__(self, *a, **k):
            pass

        def create_entry(self, block, tz):
            env["attempts"].append(block.app)
            exc = env["fail"].pop(block.app, None)
            if exc is not None:
                raise exc
            env["created"].append(block)
            if block.app in env["lost_response"]:
                env["lost_response"].discard(block.app)
                raise RequestTimeoutError()
            return f"page-{len(env['created'])}"

        def fetch_existing_keys(self, start_utc):
            env["keys_queried"].append(start_utc)
            return {
                block_dedup_key(b.app, b.start_utc)
                for b in env["created"]
                if b.start_utc >= start_utc
            }

    def make_aw(*a, **k):
        return ClippingAW(env["events"], env["clock"], env["afk"])

    monkeypatch.setattr(cli, "ActivityWatchClient", make_aw)
    monkeypatch.setattr(cli, "NotionTimeLogClient", FakeNotion)
    monkeypatch.setattr(cli, "_utcnow", lambda: env["clock"]["now"])
    return env


T0 = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


def _sync_at(env, now):
    env["clock"]["now"] = now
    sync()


def test_long_block_across_syncs_is_written_once_with_full_duration(clipping_env):
    """Review #1: a block that outlives the 30-min rewind used to come back
    clipped to the query start → new signature → a second, overlapping row
    every sync. It must land exactly once, whole, after it has ended."""
    env = clipping_env
    env["events"] += [
        ("Code", "a.py", T0, T0 + timedelta(minutes=60)),
        ("Slack", "general", T0 + timedelta(minutes=60), T0 + timedelta(minutes=65)),
    ]
    # First run backfills; then ordinary incremental syncs every 20 minutes.
    for minutes in (20, 40, 80, 100, 120):
        _sync_at(env, T0 + timedelta(minutes=minutes))

    code = [b for b in env["created"] if b.app == "Code"]
    assert len(code) == 1, [(b.start_utc, b.end_utc) for b in code]
    assert code[0].start_utc == T0
    assert code[0].active_minutes() == 60


def test_block_still_growing_at_sync_time_is_deferred(clipping_env):
    """A block whose end is within merge_gap of now may still be extended; it
    must not be written yet (else it's stored short and never corrected)."""
    env = clipping_env
    env["events"].append(("Code", "a.py", T0, T0 + timedelta(hours=5)))
    _sync_at(env, T0 + timedelta(minutes=20))
    assert env["created"] == []


def test_since_backfill_does_not_write_block_clipped_at_since(clipping_env):
    """Review #6: --since used to write the boundary block with start=since (the
    server's clip), which neither matches the row already in Notion nor any
    later sync's signature → duplicate. A block that truly starts before
    --since is out of range; one starting after it is written whole."""
    env = clipping_env
    env["events"] += [
        ("Code", "a.py", T0, T0 + timedelta(minutes=30)),
        ("Slack", "general", T0 + timedelta(minutes=30), T0 + timedelta(minutes=40)),
        ("Mail", "inbox", T0 + timedelta(minutes=40), T0 + timedelta(minutes=45)),
    ]
    env["clock"]["now"] = T0 + timedelta(hours=2)
    sync(since=(T0 + timedelta(minutes=10)).isoformat())
    assert [(b.app, b.start_utc) for b in env["created"]] == [
        ("Slack", T0 + timedelta(minutes=30)),
        ("Mail", T0 + timedelta(minutes=40)),
    ]


def test_window_fully_afk_so_far_still_holds_the_fetch_back(clipping_env):
    """An event entirely covered by AFK has no block yet, so no block marks it
    open — but it gains one when the user comes back to that same window. Its
    start must still hold pending_since back, or later syncs only ever see it
    clipped and it is never written."""
    env = clipping_env
    env["events"] += [
        ("Code", "a.py", T0, T0 + timedelta(minutes=130)),
        ("Slack", "general", T0 + timedelta(minutes=130), T0 + timedelta(minutes=135)),
    ]
    env["afk"].append((T0, T0 + timedelta(minutes=100)))
    for minutes in (40, 80, 120, 160):
        _sync_at(env, T0 + timedelta(minutes=minutes))

    code = [b for b in env["created"] if b.app == "Code"]
    assert [(b.start_utc, b.active_minutes()) for b in code] == [(T0, 30)]


def _http_error(status: int, code: str) -> HTTPResponseError:
    return HTTPResponseError(code, status, f"{status} {code}", httpx.Headers(), "")


def _two_blocks(env):
    # A clean sync first, so the ones under test are incremental, not first-run
    # backfills (which consult Notion regardless).
    _sync_at(env, T0 - timedelta(minutes=5))
    env["events"] += [
        ("Code", "a.py", T0, T0 + timedelta(minutes=10)),
        ("Slack", "general", T0 + timedelta(minutes=10), T0 + timedelta(minutes=20)),
    ]


def test_validation_error_skips_block_instead_of_jamming_sync(clipping_env):
    """Review #2: a 400 for one block is permanent — retrying it can't help.
    It used to exit before advancing last_sync, so every later sync hit the
    same wall and nothing after it was ever written."""
    env = clipping_env
    _two_blocks(env)
    env["fail"]["Code"] = _http_error(400, "validation_error")
    _sync_at(env, T0 + timedelta(minutes=55))
    assert [b.app for b in env["created"]] == ["Slack"]

    _sync_at(env, T0 + timedelta(minutes=75))
    assert env["attempts"] == ["Code", "Slack"], "the bad block is not retried forever"


@pytest.mark.parametrize(
    "exc",
    [_http_error(401, "unauthorized"), _http_error(503, "service_unavailable")],
    ids=["auth", "server"],
)
def test_non_block_error_stops_sync_and_retries_later(clipping_env, exc):
    """Auth/config/server errors would fail every block: skipping them would
    drop all data. Stop without advancing, retry next sync."""
    env = clipping_env
    _two_blocks(env)
    env["fail"]["Code"] = exc
    with pytest.raises(SystemExit):
        _sync_at(env, T0 + timedelta(minutes=55))
    _sync_at(env, T0 + timedelta(minutes=75))
    assert sorted(b.app for b in env["created"]) == ["Code", "Slack"]


def test_lost_response_after_successful_post_is_not_duplicated(clipping_env):
    """Review #5: a timeout after Notion already created the page left no
    trace in state, so the retry created it again. The next sync must check
    Notion for what the failed one may have written."""
    env = clipping_env
    _two_blocks(env)
    env["lost_response"].add("Code")
    with pytest.raises(SystemExit):
        _sync_at(env, T0 + timedelta(minutes=55))
    _sync_at(env, T0 + timedelta(minutes=75))
    assert sorted(b.app for b in env["created"]) == ["Code", "Slack"]
    assert env["keys_queried"], "retry consulted Notion"


def test_interrupted_activity_is_glued_before_min_duration(clipping_env):
    """Code 3 min → Slack 1 min → Code 3 min: each Code piece alone is under
    the 5-min minimum; glued they are one 6-min row."""
    env = clipping_env
    env["cfg"].activitywatch.min_block_duration_sec = 300
    env["events"] += [
        ("Code", "a.py", T0, T0 + timedelta(minutes=3)),
        ("Slack", "general", T0 + timedelta(minutes=3), T0 + timedelta(minutes=4)),
        ("Code", "a.py", T0 + timedelta(minutes=4), T0 + timedelta(minutes=7)),
        ("Mail", "inbox", T0 + timedelta(minutes=7), T0 + timedelta(minutes=8)),
    ]
    _sync_at(env, T0 + timedelta(minutes=60))
    assert [(b.app, b.start_utc, b.active_minutes()) for b in env["created"]] == [("Code", T0, 6)]


def test_cluster_is_written_once_only_after_its_gap_has_passed(clipping_env):
    """Until cluster_gap has passed since its last piece, a later piece could
    still join — writing early would store it short, then a second row."""
    env = clipping_env
    env["events"] += [
        ("Code", "a.py", T0, T0 + timedelta(minutes=10)),
        ("Slack", "general", T0 + timedelta(minutes=10), T0 + timedelta(minutes=11)),
        ("Code", "a.py", T0 + timedelta(minutes=30), T0 + timedelta(minutes=35)),
        ("Mail", "inbox", T0 + timedelta(minutes=35), T0 + timedelta(minutes=36)),
    ]
    _sync_at(env, T0 + timedelta(minutes=25))
    assert env["created"] == []
    for minutes in (45, 70, 100, 130):
        _sync_at(env, T0 + timedelta(minutes=minutes))
    code = [b for b in env["created"] if b.app == "Code"]
    assert [(b.start_utc, b.active_minutes()) for b in code] == [(T0, 15)]


def test_since_fetch_reaches_back_a_full_cluster_gap(sync_env):
    """A piece within cluster_gap before --since decides whether the next piece
    starts a new row or continues one, so it must be in view."""
    sync_env["cfg"].activitywatch.cluster_gap_sec = 1800
    sync(dry_run=True, since="2026-04-05T00:00:00")
    expected = datetime(2026, 4, 5, 0, 0, tzinfo=UTC)
    assert sync_env["aw_start"] == expected - timedelta(seconds=1800) - cli.FETCH_MARGIN
