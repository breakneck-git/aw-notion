from datetime import datetime, timezone
import requests

from .blocks import AWEvent, AFKEvent


class ActivityWatchClient:
    def __init__(self, base_url: str = "http://localhost:5600"):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def _get(self, path: str, **params) -> list | dict:
        resp = self._session.get(
            f"{self.base_url}/api/0{path}", params=params, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def is_running(self) -> bool:
        try:
            self._get("/info")
            return True
        except requests.RequestException:
            return False

    def _fetch_events(
        self, bucket_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        return self._get(
            f"/buckets/{bucket_id}/events",
            start=start.isoformat(),
            end=end.isoformat(),
            limit=10000,
        )

    def get_all_events(
        self, start: datetime, end: datetime
    ) -> tuple[list[AWEvent], list[AFKEvent]]:
        """
        Returns (window_events, afk_events).
        Web watcher events replace window watcher events for browser apps.
        """
        buckets: dict = self._get("/buckets")

        web_apps: set[str] = set()
        web_events: list[AWEvent] = []
        afk_events: list[AFKEvent] = []

        # First pass: web watcher (determines which apps it covers)
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-web"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(timezone.utc)
                app = e["data"].get("app", "Browser")
                web_apps.add(app)
                web_events.append(AWEvent(
                    timestamp=ts,
                    duration=float(e["duration"]),
                    app=app,
                    title=e["data"].get("title", ""),
                    url=e["data"].get("url"),
                ))

        # Second pass: window watcher (skip apps covered by web watcher)
        window_events: list[AWEvent] = []
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-window"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                app = e["data"].get("app", "Unknown")
                if app in web_apps:
                    continue
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(timezone.utc)
                window_events.append(AWEvent(
                    timestamp=ts,
                    duration=float(e["duration"]),
                    app=app,
                    title=e["data"].get("title", ""),
                ))

        # AFK watcher
        for bucket_id in buckets:
            if not bucket_id.startswith("aw-watcher-afk"):
                continue
            for e in self._fetch_events(bucket_id, start, end):
                ts = datetime.fromisoformat(e["timestamp"]).astimezone(timezone.utc)
                afk_events.append(AFKEvent(
                    timestamp=ts,
                    duration=float(e["duration"]),
                    status=e["data"].get("status", "afk"),
                ))

        return window_events + web_events, afk_events
