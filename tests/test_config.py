from pathlib import Path
import pytest
from timetrack.config import load_config, Config, NotionConfig

def test_load_config(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
timezone = "Europe/Moscow"

[notion]
token = "secret_abc123"
timelog_db = "35b4cfe8-1f3a-457a-80a8-fe61aa465a18"

[activitywatch]
afk_threshold_min = 15
""")
    cfg = load_config(cfg_file)
    assert cfg.notion.token == "secret_abc123"
    assert cfg.notion.timelog_db == "35b4cfe8-1f3a-457a-80a8-fe61aa465a18"
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
