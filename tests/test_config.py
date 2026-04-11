from pathlib import Path
import pytest
from aw_notion.config import load_config, Config, NotionConfig, NotionFieldsConfig

def test_load_config(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
timezone = "Europe/Moscow"

[notion]
token = "secret_abc123"
timelog_db = "00000000-0000-0000-0000-000000000000"

[activitywatch]
afk_threshold_min = 15
""")
    cfg = load_config(cfg_file)
    assert cfg.notion.token == "secret_abc123"
    assert cfg.notion.timelog_db == "00000000-0000-0000-0000-000000000000"
    assert cfg.timezone == "Europe/Moscow"
    assert cfg.activitywatch.afk_threshold_min == 15
    assert cfg.activitywatch.min_block_duration_sec == 120  # default
    assert cfg.sync.initial_sync_days == 7  # default

def test_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nonexistent.toml")

def test_defaults(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[notion]
token = "secret_x"
timelog_db = "db-id"
""")
    cfg = load_config(cfg_file)
    assert cfg.activitywatch.base_url == "http://localhost:5600"
    assert cfg.activitywatch.merge_gap_sec == 180
    assert cfg.timezone == "UTC"


def test_notion_fields_default_to_english(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[notion]
token = "secret_x"
timelog_db = "db-id"
""")
    cfg = load_config(cfg_file)
    f = cfg.notion.fields
    assert f.entry == "Entry"
    assert f.start == "Start"
    assert f.end == "End"
    assert f.duration_minutes == "Duration"
    assert f.app == "App"
    assert f.url == "URL"
    assert f.sorted == "Sorted"


def test_notion_fields_override(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[notion]
token = "secret_x"
timelog_db = "db-id"

[notion.fields]
duration_minutes = "Время"
entry = "Запись"
""")
    cfg = load_config(cfg_file)
    f = cfg.notion.fields
    assert f.duration_minutes == "Время"
    assert f.entry == "Запись"
    assert f.start == "Start"  # untouched default
    assert f.app == "App"
