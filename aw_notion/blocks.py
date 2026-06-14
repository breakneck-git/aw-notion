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
    note: str | None = None


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
    note: str | None = None

    def start_local(self, tz: ZoneInfo) -> datetime:
        return self.start_utc.astimezone(tz)

    def end_local(self, tz: ZoneInfo) -> datetime:
        return self.end_utc.astimezone(tz)

    def active_minutes(self) -> int:
        return round(self.active_seconds / 60)

    def signature(self) -> str:
        raw = f"{self.app}|{self.title}|{self.start_utc.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _afk_overlap_seconds(
    start: datetime, end: datetime, afk_intervals: list[tuple[datetime, datetime]]
) -> float:
    """Seconds of [start, end) covered by any AFK interval. ActivityWatch emits
    alternating afk/not-afk events, so AFK intervals are non-overlapping and the
    per-interval overlaps simply sum."""
    total = 0.0
    for a_s, a_e in afk_intervals:
        lo = start if start > a_s else a_s
        hi = end if end < a_e else a_e
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def compute_focus_blocks(
    window_events: list[AWEvent],
    afk_events: list[AFKEvent],
    *,
    afk_threshold_sec: int = 600,
    merge_gap_sec: int = 180,
    min_duration_sec: int = 120,
) -> list[FocusBlock]:
    """Group window events into focus blocks, skipping AFK periods.

    A window event's contribution to active time is its duration MINUS any AFK
    overlap. The window watcher keeps a window "focused" while the user is away
    (it has no notion of AFK), so a single heartbeat-merged event can span long
    idle gaps; counting its raw duration inflated Duration. We clip each event
    to its non-AFK portion and drop events with no active time left. `start_utc`
    stays at the event timestamp so `signature()`/dedup are unaffected
    (invariant #9) — only the active-seconds total changes."""
    afk_intervals = [
        (e.timestamp, e.timestamp + timedelta(seconds=e.duration))
        for e in afk_events
        if e.status == "afk"
    ]

    def active_sec(e: AWEvent) -> float:
        e_end = e.timestamp + timedelta(seconds=e.duration)
        return max(0.0, e.duration - _afk_overlap_seconds(e.timestamp, e_end, afk_intervals))

    active = sorted(
        ((e, active_sec(e)) for e in window_events),
        key=lambda pair: pair[0].timestamp,
    )
    active = [(e, a) for (e, a) in active if a > 0]

    if not active:
        return []

    blocks: list[FocusBlock] = []

    def new_block(e: AWEvent, ev_active: float) -> FocusBlock:
        return FocusBlock(
            app=e.app,
            title=e.title,
            start_utc=e.timestamp,
            end_utc=e.timestamp + timedelta(seconds=e.duration),
            active_seconds=ev_active,
            url=e.url,
            note=e.note,
        )

    def flush(b: FocusBlock) -> None:
        # Blocks shorter than min_duration (active time, not wall) drop on close.
        if b.active_seconds >= min_duration_sec:
            blocks.append(b)

    first, first_active = active[0]
    cur = new_block(first, first_active)

    for event, ev_active in active[1:]:
        gap_sec = (event.timestamp - cur.end_utc).total_seconds()
        same = event.app == cur.app and event.title == cur.title

        if gap_sec > afk_threshold_sec:  # (a) hard AFK boundary → close + start
            flush(cur)
            cur = new_block(event, ev_active)
        elif same and gap_sec <= merge_gap_sec:  # (b) same activity → extend
            cur.end_utc = event.timestamp + timedelta(seconds=event.duration)
            cur.active_seconds += ev_active
            if cur.url is None and event.url is not None:
                cur.url = event.url
            if cur.note is None and event.note is not None:
                cur.note = event.note
        else:  # (c) different activity → close + start
            flush(cur)
            cur = new_block(event, ev_active)

    flush(cur)
    return blocks
