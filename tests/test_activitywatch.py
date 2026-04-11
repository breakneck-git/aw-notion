from datetime import datetime, timezone
import responses as resp_mock
import responses
from aw_notion.activitywatch import ActivityWatchClient

BASE = "http://localhost:5600/api/0"

BUCKETS = {
    "aw-watcher-window_testhost": {"type": "currentwindow"},
    "aw-watcher-afk_testhost": {"type": "afkstatus"},
}

WINDOW_EVENTS = [
    {
        "id": 1,
        "timestamp": "2026-04-11T10:00:00.000000+00:00",
        "duration": 300.0,
        "data": {"app": "Code", "title": "file.py — VS Code"},
    },
    {
        "id": 2,
        "timestamp": "2026-04-11T10:05:00.000000+00:00",
        "duration": 200.0,
        "data": {"app": "Code", "title": "test.py — VS Code"},
    },
]

AFK_EVENTS = [
    {
        "id": 3,
        "timestamp": "2026-04-11T10:10:00.000000+00:00",
        "duration": 600.0,
        "data": {"status": "afk"},
    }
]

@responses.activate
def test_is_running_true():
    responses.add(responses.GET, f"{BASE}/info", json={"version": "0.12"})
    client = ActivityWatchClient()
    assert client.is_running() is True

@responses.activate
def test_is_running_false_when_connection_error():
    # No mock registered → connection error
    client = ActivityWatchClient()
    assert client.is_running() is False

@responses.activate
def test_get_all_events_returns_window_and_afk():
    responses.add(responses.GET, f"{BASE}/buckets", json=BUCKETS)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=WINDOW_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=timezone.utc)
    window_events, afk_events = client.get_all_events(start, end)
    assert len(window_events) == 2
    assert window_events[0].app == "Code"
    assert window_events[0].duration == 300.0
    assert len(afk_events) == 1
    assert afk_events[0].status == "afk"

@responses.activate
def test_fetch_events_paginates_by_day():
    buckets = {"aw-watcher-window_host": {"type": "currentwindow"}}
    url = f"{BASE}/buckets/aw-watcher-window_host/events"
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets)
    for i in range(3):
        responses.add(
            responses.GET,
            url,
            json=[{
                "id": i,
                "timestamp": f"2026-04-{10+i}T10:00:00.000000+00:00",
                "duration": 200.0,
                "data": {"app": "Code", "title": f"day {i}"},
            }],
        )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 3
    titles = sorted(e.title for e in window)
    assert titles == ["day 0", "day 1", "day 2"]
    events_calls = [c for c in responses.calls if "/events" in c.request.url]
    assert len(events_calls) == 3


@responses.activate
def test_web_watcher_events_replace_browser_window_events():
    buckets_with_web = {
        **BUCKETS,
        "aw-watcher-web-chrome": {"type": "web.tab.current"},
    }
    web_events = [
        {
            "id": 10,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 120.0,
            "data": {
                "title": "GitHub",
                "url": "https://github.com/x/y",
            },
        }
    ]
    window_events_with_chrome = [
        *WINDOW_EVENTS,
        {
            "id": 5,
            "timestamp": "2026-04-11T10:07:00.000000+00:00",
            "duration": 120.0,
            "data": {"app": "Google Chrome", "title": "GitHub"},
        },
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_web)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events_with_chrome,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-web-chrome/events",
        json=web_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=timezone.utc)
    window, _ = client.get_all_events(start, end)
    # Chrome window event should be replaced by web event (with URL)
    chrome_events = [e for e in window if e.app == "Google Chrome"]
    assert len(chrome_events) == 1
    assert chrome_events[0].url == "https://github.com/x/y"
