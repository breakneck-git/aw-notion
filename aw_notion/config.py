import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "aw-notion" / "config.toml"


@dataclass
class NotionFieldsConfig:
    entry: str = "Entry"
    start: str = "Start"
    end: str = "End"
    duration_minutes: str = "Duration"
    app: str = "App"
    url: str = "URL"
    sorted: str = "Sorted"


@dataclass
class NotionConfig:
    token: str
    timelog_db: str
    fields: NotionFieldsConfig = field(default_factory=NotionFieldsConfig)


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
            "Copy config.toml.example to that path and fill in your values."
        )
    with open(path, "rb") as f:
        data = tomllib.load(f)

    n = data["notion"]
    aw = data.get("activitywatch", {})
    s = data.get("sync", {})
    fields = n.get("fields", {})

    return Config(
        notion=NotionConfig(
            token=n["token"],
            timelog_db=n["timelog_db"],
            fields=NotionFieldsConfig(**fields),
        ),
        activitywatch=ActivityWatchConfig(**aw),
        sync=SyncConfig(**s),
        timezone=data.get("timezone", "UTC"),
    )
