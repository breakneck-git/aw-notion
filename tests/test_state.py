from datetime import datetime, timezone
from timetrack.state import State


def test_first_run_is_empty(tmp_path):
    state = State.load(tmp_path / "state.json")
    assert state.last_sync is None
    assert state.notion_entries == {}


def test_save_and_reload(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.last_sync = datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc)
    state.notion_entries["abc123"] = "notion-page-id-xyz"
    state.save(path)

    reloaded = State.load(path)
    assert reloaded.last_sync == datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc)
    assert reloaded.notion_entries["abc123"] == "notion-page-id-xyz"


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    state = State.load(path)
    state.save(path)
    assert path.exists()
