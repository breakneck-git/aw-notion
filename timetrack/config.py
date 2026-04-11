import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "timetrack" / "config.toml"


@dataclass
class NotionConfig:
    token: str
    timelog_db: str


@dataclass
class ActivityWatchConfig:
    base_url: str = "http://localhost:5600"
    afk_threshold_min: int = 10
    min_block_duration_sec: int = 120
    merge_gap_sec: int = 180


@dataclass
class SyncConfig:
    initial_sync_days: int = 7


@dataclass
class Config:
    notion: NotionConfig
    activitywatch: ActivityWatchConfig = field(default_factory=ActivityWatchConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    timezone: str = "UTC"


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}\n"
            "Create it — see config template in install.sh."
        )
    with open(path, "rb") as f:
        data = tomllib.load(f)

    n = data["notion"]
    aw = data.get("activitywatch", {})
    s = data.get("sync", {})

    return Config(
        notion=NotionConfig(
            token=n["token"],
            timelog_db=n["timelog_db"],
        ),
        activitywatch=ActivityWatchConfig(
            base_url=aw.get("base_url", "http://localhost:5600"),
            afk_threshold_min=aw.get("afk_threshold_min", 10),
            min_block_duration_sec=aw.get("min_block_duration_sec", 120),
            merge_gap_sec=aw.get("merge_gap_sec", 180),
        ),
        sync=SyncConfig(
            initial_sync_days=s.get("initial_sync_days", 7),
        ),
        timezone=data.get("timezone", "UTC"),
    )
