import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import requests

from .blocks import AFKEvent, AWEvent
from .config import DEFAULT_BROWSER_APPS

log = logging.getLogger(__name__)

_PAGE_LIMIT = 10000

_ZERO_DURATION_EPSILON = timedelta(seconds=1)

# Minimum title similarity for a web event's title to override the plain
# temporal-first pick. Below this we fall back to first-overlap (old behavior).
_TITLE_MATCH_MIN = 0.34

# A containment match (one title is a substring of the other) only counts when
# the shorter string is at least this long. Without it, a 2-3 char fragment
# ("go" inside "Google") scores a false 0.9 and picks the wrong tab.
_MIN_CONTAINMENT_LEN = 4

# Generic / placeholder window titles that are NOT per-tab identifiers, so the
# (app, title) URL backfill must not key on them (they collide across unrelated
# tabs and smear one tab's URL onto others).
_GENERIC_TITLE_MARKERS = (
    "newtab",
    "new tab",
    "untitled",
    "картинка в картинке",
    "picture in picture",
    "picture-in-picture",
)


def _title_match_score(win_title: str | None, web_title: str | None) -> float:
    """Cheap similarity in [0, 1] between a window title and a web page title.
    Exact match = 1.0, containment either way = 0.9, else word-level Jaccard."""
    a = (win_title or "").casefold().strip()
    b = (web_title or "").casefold().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if (b in a and len(b) >= _MIN_CONTAINMENT_LEN) or (a in b and len(a) >= _MIN_CONTAINMENT_LEN):
        return 0.9
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _is_generic_title(title: str | None) -> bool:
    t = (title or "").strip().casefold()
    if not t:
        return True
    return any(m in t for m in _GENERIC_TITLE_MARKERS)


def _find_url_by_overlap(
    web_intervals: list[tuple[datetime, datetime, str, str]],
    win_start: datetime,
    win_end: datetime,
    win_title: str | None = None,
) -> str | None:
    """
    Return the URL of a web event whose interval overlaps [win_start, win_end],
    preferring the one whose page title best matches the window title. The
    web-watcher keeps emitting heartbeats for background tabs, so the *first*
    temporal overlap is frequently the wrong tab; a confident title match
    (>= _TITLE_MATCH_MIN) disambiguates. Falls back to the first overlap when no
    title matches well or `win_title` is empty (old behavior). Zero-duration web
    events get a tiny _ZERO_DURATION_EPSILON span so they still match.

    web_intervals must be sorted by start timestamp.
    """
    first_overlap_url: str | None = None
    best_url: str | None = None
    best_score = -1.0
    for ws, we, url, web_title in web_intervals:
        if ws >= win_end:
            break
        effective_we = we if we > ws else ws + _ZERO_DURATION_EPSILON
        if effective_we > win_start:
            if first_overlap_url is None:
                first_overlap_url = url
            score = _title_match_score(win_title, web_title)
            if score > best_score:
                best_score = score
                best_url = url
    if best_score >= _TITLE_MATCH_MIN:
        return best_url
    return first_overlap_url


def _find_note_by_overlap(
    ax_intervals: list[tuple[datetime, datetime, str, str]],
    win_start: datetime,
    win_end: datetime,
    app: str,
) -> str | None:
    """
    Return the context string of the first ax-watcher event whose interval
    overlaps [win_start, win_end] AND whose `app` field matches the window
    event's app. The app filter prevents an ax event emitted for Claude from
    leaking onto a window event for Telegram when their timestamps overlap.

    ax_intervals must be sorted by start timestamp.
    """
    for s, e, ax_app, ctx in ax_intervals:
        if s >= win_end:
            break
        effective_e = e if e > s else s + _ZERO_DURATION_EPSILON
        if effective_e > win_start and ax_app == app:
            return ctx
    return None


class ActivityWatchClient:
    def __init__(self, base_url: str = "http://localhost:5600"):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def _get(self, path: str, **params) -> list | dict:
        resp = self._session.get(f"{self.base_url}/api/0{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def is_running(self) -> bool:
        try:
            self._get("/info")
            return True
        except requests.RequestException:
            return False

    def fetch_ax_intervals(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, str, str]]:
        buckets = self._get("/buckets")
        out: list[tuple[datetime, datetime, str, str]] = []
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-ax"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                ctx = e["data"].get("context")
                ax_app = e["data"].get("app")
                if not ctx or not ax_app:
                    continue
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(UTC)
                ae = ts + timedelta(seconds=float(e["duration"]))
                out.append((ts, ae, ax_app, ctx))
        out.sort(key=lambda x: x[0])
        return out

    def _fetch_events(self, bucket_id: str, start: datetime, end: datetime) -> list[dict]:
        events: list[dict] = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=1), end)
            chunk = self._get(
                f"/buckets/{bucket_id}/events",
                start=cursor.isoformat(),
                end=chunk_end.isoformat(),
                limit=_PAGE_LIMIT,
            )
            events.extend(chunk)
            if len(chunk) >= _PAGE_LIMIT:
                log.warning(
                    "Bucket %s hit page limit %d for %s..%s — data may be truncated",
                    bucket_id,
                    _PAGE_LIMIT,
                    cursor.isoformat(),
                    chunk_end.isoformat(),
                )
            cursor = chunk_end
        return events

    def get_all_events(
        self,
        start: datetime,
        end: datetime,
        browser_apps: Iterable[str] = DEFAULT_BROWSER_APPS,
    ) -> tuple[list[AWEvent], list[AFKEvent]]:
        """
        Returns (window_events, afk_events).

        Window-watcher is the source of truth for app, title, and duration.
        Web-watcher events only enrich window events with a URL, by matching
        on timestamp overlap — but only for window events whose app is in
        `browser_apps`. This is required because web-watcher keeps emitting
        heartbeat events for the currently-selected tab even when the browser
        is not in the foreground, which would otherwise leak browser URLs
        onto unrelated window events (terminal, editor, chat app, ...).
        """
        browser_set = {a.casefold() for a in browser_apps}
        buckets = self._get("/buckets")

        web_intervals: list[tuple[datetime, datetime, str, str]] = []
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-web"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                url = e["data"].get("url")
                if not url:
                    continue
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(UTC)
                we = ts + timedelta(seconds=float(e["duration"]))
                web_intervals.append((ts, we, url, e["data"].get("title", "")))
        web_intervals.sort(key=lambda x: x[0])

        ax_intervals: list[tuple[datetime, datetime, str, str]] = []
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-ax"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                ctx = e["data"].get("context")
                ax_app = e["data"].get("app")
                if not ctx or not ax_app:
                    continue
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(UTC)
                ae = ts + timedelta(seconds=float(e["duration"]))
                ax_intervals.append((ts, ae, ax_app, ctx))
        ax_intervals.sort(key=lambda x: x[0])

        window_events: list[AWEvent] = []
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-window"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(UTC)
                duration = float(e["duration"])
                win_end = ts + timedelta(seconds=duration)
                app = e["data"].get("app", "Unknown")
                title = e["data"].get("title", "")
                url = None
                if app.casefold() in browser_set:
                    url = _find_url_by_overlap(web_intervals, ts, win_end, title)
                note = _find_note_by_overlap(ax_intervals, ts, win_end, app)
                window_events.append(
                    AWEvent(
                        timestamp=ts,
                        duration=duration,
                        app=app,
                        title=title,
                        url=url,
                        note=note,
                    )
                )

        # Backfill URLs within the same (app, title): web-watcher often emits
        # URL events only when the user returns briefly to a tab, while the
        # long window-watcher events for the same tab sit earlier without a
        # temporal overlap. Since title is effectively per-tab, it's safe to
        # propagate the URL to other window events of the same (app, title).
        # Generic/empty titles (newtab, picture-in-picture, …) are NOT per-tab
        # identifiers — they collide across unrelated tabs and would smear one
        # tab's URL onto others — so they are excluded from this backfill.
        url_by_key: dict[tuple[str, str], str] = {}
        for e in window_events:
            if e.url and not _is_generic_title(e.title):
                url_by_key.setdefault((e.app, e.title), e.url)
        for e in window_events:
            if e.url is None and not _is_generic_title(e.title):
                e.url = url_by_key.get((e.app, e.title))

        afk_events: list[AFKEvent] = []
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-afk"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(UTC)
                afk_events.append(
                    AFKEvent(
                        timestamp=ts,
                        duration=float(e["duration"]),
                        status=e["data"].get("status", "afk"),
                    )
                )

        return window_events, afk_events
