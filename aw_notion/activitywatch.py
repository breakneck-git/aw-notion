import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import requests

from .blocks import AFKEvent, AWEvent
from .config import DEFAULT_BROWSER_APPS

log = logging.getLogger(__name__)

_PAGE_LIMIT = 10000

_ZERO_DURATION_EPSILON = timedelta(seconds=1)


def _find_url_by_overlap(
    web_intervals: list[tuple[datetime, datetime, str]],
    win_start: datetime,
    win_end: datetime,
) -> str | None:
    """
    Return the URL of the first web event whose interval overlaps
    [win_start, win_end]. Zero-duration web events are treated as having
    a tiny _ZERO_DURATION_EPSILON span so they can still match.

    web_intervals must be sorted by start timestamp.
    """
    for ws, we, url in web_intervals:
        if ws >= win_end:
            break
        effective_we = we if we > ws else ws + _ZERO_DURATION_EPSILON
        if effective_we > win_start:
            return url
    return None


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

        web_intervals: list[tuple[datetime, datetime, str]] = []
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-web"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                url = e["data"].get("url")
                if not url:
                    continue
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(UTC)
                we = ts + timedelta(seconds=float(e["duration"]))
                web_intervals.append((ts, we, url))
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
                url = None
                if app.casefold() in browser_set:
                    url = _find_url_by_overlap(web_intervals, ts, win_end)
                note = _find_note_by_overlap(ax_intervals, ts, win_end, app)
                window_events.append(
                    AWEvent(
                        timestamp=ts,
                        duration=duration,
                        app=app,
                        title=e["data"].get("title", ""),
                        url=url,
                        note=note,
                    )
                )

        # Backfill URLs within the same (app, title): web-watcher often emits
        # URL events only when the user returns briefly to a tab, while the
        # long window-watcher events for the same tab sit earlier without a
        # temporal overlap. Since title is effectively per-tab, it's safe to
        # propagate the URL to other window events of the same (app, title).
        url_by_key: dict[tuple[str, str], str] = {}
        for e in window_events:
            if e.url:
                url_by_key.setdefault((e.app, e.title), e.url)
        for e in window_events:
            if e.url is None:
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
