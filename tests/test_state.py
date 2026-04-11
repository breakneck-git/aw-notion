from datetime import datetime, timedelta, timezone

import pytest

from aw_notion.state import State


def test_first_run_is_empty(tmp_path):
    state = State.load(tmp_path / "state.json")
    assert state.last_sync is None
    assert state.notion_entries == {}


def test_save_and_reload(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.last_sync = datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc)
    state.notion_entries["abc123"] = {
        "page_id": "notion-page-id-xyz",
        "created_at": datetime(2026, 4, 11, 21, 30, tzinfo=timezone.utc).isoformat(),
    }
    state.save(path)

    reloaded = State.load(path)
    assert reloaded.last_sync == datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc)
    assert reloaded.notion_entries["abc123"]["page_id"] == "notion-page-id-xyz"


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    state = State.load(path)
    state.save(path)
    assert path.exists()


def test_save_atomic_keeps_original_on_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.last_sync = datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc)
    state.save(path)
    original = path.read_text()

    import aw_notion.state as state_module

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(state_module.json, "dump", boom)

    state.last_sync = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(OSError):
        state.save(path)

    assert path.read_text() == original


def test_save_cleans_up_tmp_file_on_success(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.save(path)
    assert not (tmp_path / "state.json.tmp").exists()


def test_load_migrates_old_format(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"last_sync": null, "notion_entries": {"oldsig": "old-page-id"}}'
    )
    state = State.load(path)
    assert state.notion_entries["oldsig"]["page_id"] == "old-page-id"
    assert state.notion_entries["oldsig"]["created_at"] is None


def test_save_prunes_entries_older_than_one_day(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.last_sync = datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc)
    state.notion_entries["old"] = {
        "page_id": "page-old",
        "created_at": (state.last_sync - timedelta(days=2)).isoformat(),
    }
    state.notion_entries["recent"] = {
        "page_id": "page-recent",
        "created_at": (state.last_sync - timedelta(hours=1)).isoformat(),
    }
    state.save(path)
    reloaded = State.load(path)
    assert "old" not in reloaded.notion_entries
    assert "recent" in reloaded.notion_entries


def test_save_prunes_legacy_entries_with_null_created_at(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.last_sync = datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc)
    state.notion_entries["legacy"] = {"page_id": "page-x", "created_at": None}
    state.save(path)
    reloaded = State.load(path)
    assert "legacy" not in reloaded.notion_entries


def test_save_without_last_sync_skips_prune(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.notion_entries["x"] = {"page_id": "p", "created_at": None}
    state.save(path)
    reloaded = State.load(path)
    assert "x" in reloaded.notion_entries
