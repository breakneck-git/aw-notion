import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "aw-notion" / "config.toml"

DEFAULT_BROWSER_APPS: tuple[str, ...] = (
    "Google Chrome",
    "Chromium",
    "Firefox",
    "Safari",
    "Brave Browser",
    "Opera",
    "Microsoft Edge",
    "Arc",
    "Vivaldi",
    "Comet",
    "Chrome Canary",
)


@dataclass
class NotionFieldsConfig:
    entry: str = "Entry"
    start: str = "Start"
    end: str = "End"
    duration_minutes: str = "Duration"
    app: str = "App"
    url: str = "URL"
    sorted: str = "Sorted"
    note: str | None = None


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
    browser_apps: list[str] = field(default_factory=lambda: list(DEFAULT_BROWSER_APPS))


@dataclass
class SyncConfig:
    initial_sync_days: int = 7
    # Block-level exclusion rules. Matched case-insensitively. Excluded blocks
    # never reach Notion AND never get a signature recorded in state — if you
    # later remove the entry from exclusion config, only NEW events of that
    # kind start syncing (already-excluded past events stay excluded unless
    # you force `--since`). A forced `--since` is now idempotent: it dedups
    # against entries already in Notion (NotionTimeLogClient.fetch_existing_keys),
    # so re-syncing a past window fills gaps without creating duplicates.
    exclude_apps: list[str] = field(default_factory=list)
    exclude_url_substrings: list[str] = field(default_factory=list)
    # Title-based exclusion. Privacy must not hinge on the URL alone: web-watcher
    # can attach the wrong tab's URL to a block (background-tab heartbeats), so a
    # sensitive page can slip past `exclude_url_substrings` on a clean URL. Match
    # the window title too. Case-insensitive substring match against block title.
    exclude_title_substrings: list[str] = field(default_factory=list)


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
