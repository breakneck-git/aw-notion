import fcntl
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from aw_notion import cli
from aw_notion.blocks import AWEvent
from aw_notion.cli import _acquire_lock, main, sync
from aw_notion.config import (
    ActivityWatchConfig,
    Config,
    NotionConfig,
    SyncConfig,
)


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
        activitywatch=ActivityWatchConfig(),
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
            evt = AWEvent(
                timestamp=start + timedelta(seconds=1),
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

    monkeypatch.setattr(cli, "ActivityWatchClient", FakeAW)
    monkeypatch.setattr(cli, "NotionTimeLogClient", FakeNotion)
    captured["tmp_path"] = tmp_path
    return captured


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
    assert sync_env["aw_start"] == expected


def test_main_parses_dry_run(monkeypatch):
    captured = {}

    def fake_sync(dry_run=False, since=None):
        captured["dry_run"] = dry_run
        captured["since"] = since

    monkeypatch.setattr(cli, "sync", fake_sync)
    monkeypatch.setattr("sys.argv", ["aw-notion", "sync", "--dry-run"])
    main()
    assert captured == {"dry_run": True, "since": None}


def test_main_parses_since(monkeypatch):
    captured = {}

    def fake_sync(dry_run=False, since=None):
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
        activitywatch=ActivityWatchConfig(),
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
                        timestamp=start + timedelta(seconds=1),
                        duration=300.0,
                        app="Ghostty",
                        title="~/code/aw-notion",
                    ),
                    AWEvent(
                        timestamp=start + timedelta(seconds=1),
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
        activitywatch=ActivityWatchConfig(),
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
                        timestamp=start + timedelta(seconds=1),
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
