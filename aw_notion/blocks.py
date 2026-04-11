import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass
class AWEvent:
    timestamp: datetime  # UTC-aware
    duration: float  # seconds
    app: str
    title: str
    url: str | None = None


@dataclass
class AFKEvent:
    timestamp: datetime  # UTC-aware
    duration: float  # seconds
    status: str  # "afk" | "not-afk"


@dataclass
class FocusBlock:
    app: str
    title: str
    start_utc: datetime
    end_utc: datetime
    active_seconds: float
    url: str | None = None

    def start_local(self, tz: ZoneInfo) -> datetime:
        return self.start_utc.astimezone(tz)

    def end_local(self, tz: ZoneInfo) -> datetime:
        return self.end_utc.astimezone(tz)

    def active_minutes(self) -> int:
        return round(self.active_seconds / 60)

    def signature(self) -> str:
        raw = f"{self.app}|{self.title}|{self.start_utc.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compute_focus_blocks(
    window_events: list[AWEvent],
    afk_events: list[AFKEvent],
    *,
    afk_threshold_sec: int = 600,
    merge_gap_sec: int = 180,
    min_duration_sec: int = 120,
) -> list[FocusBlock]:
    """Group window events into focus blocks, skipping AFK periods."""
    afk_intervals = [
        (e.timestamp, e.timestamp + timedelta(seconds=e.duration))
        for e in afk_events
        if e.status == "afk"
    ]

    def is_afk(ts: datetime) -> bool:
        return any(s <= ts < e for s, e in afk_intervals)

    active = sorted(
        [e for e in window_events if not is_afk(e.timestamp)],
        key=lambda e: e.timestamp,
    )

    if not active:
        return []

    blocks: list[FocusBlock] = []
    first = active[0]
    cur = FocusBlock(
        app=first.app,
        title=first.title,
        start_utc=first.timestamp,
        end_utc=first.timestamp + timedelta(seconds=first.duration),
        active_seconds=first.duration,
        url=first.url,
    )

    for event in active[1:]:
        event_end = event.timestamp + timedelta(seconds=event.duration)
        gap_sec = (event.timestamp - cur.end_utc).total_seconds()
        same = event.app == cur.app and event.title == cur.title

        if gap_sec > afk_threshold_sec:
            if cur.active_seconds >= min_duration_sec:
                blocks.append(cur)
            cur = FocusBlock(
                app=event.app,
                title=event.title,
                start_utc=event.timestamp,
                end_utc=event_end,
                active_seconds=event.duration,
                url=event.url,
            )
        elif same and gap_sec <= merge_gap_sec:
            cur.end_utc = event_end
            cur.active_seconds += event.duration
            if cur.url is None and event.url is not None:
                cur.url = event.url
        else:
            if cur.active_seconds >= min_duration_sec:
                blocks.append(cur)
            cur = FocusBlock(
                app=event.app,
                title=event.title,
                start_utc=event.timestamp,
                end_utc=event_end,
                active_seconds=event.duration,
                url=event.url,
            )

    if cur.active_seconds >= min_duration_sec:
        blocks.append(cur)

    return blocks
