from datetime import UTC, datetime

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
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
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
            json=[
                {
                    "id": i,
                    "timestamp": f"2026-04-{10 + i}T10:00:00.000000+00:00",
                    "duration": 200.0,
                    "data": {"app": "Code", "title": f"day {i}"},
                }
            ],
        )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 10, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 13, 0, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 3
    titles = sorted(e.title for e in window)
    assert titles == ["day 0", "day 1", "day 2"]
    events_calls = [c for c in responses.calls if "/events" in c.request.url]
    assert len(events_calls) == 3


@responses.activate
def test_web_events_enrich_overlapping_window_events_with_url():
    """
    Web-watcher events enrich window-watcher events with URL, they do NOT
    replace them. The window event keeps its app/title/duration; the URL is
    looked up by timestamp overlap with any web-watcher bucket.

    This works for any chromium-based browser (Comet, Arc, Brave, etc.)
    without requiring a browser-name mapping.
    """
    buckets_with_web = {
        **BUCKETS,
        "aw-watcher-web-comet_testhost": {"type": "web.tab.current"},
    }
    # Sequential, non-overlapping window events (as window-watcher really produces).
    window_events_with_browser = [
        {
            "id": 1,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 300.0,
            "data": {"app": "Code", "title": "file.py — VS Code"},
        },
        {
            "id": 2,
            "timestamp": "2026-04-11T10:05:00.000000+00:00",
            "duration": 120.0,
            "data": {"app": "Code", "title": "test.py — VS Code"},
        },
        {
            "id": 5,
            "timestamp": "2026-04-11T10:07:00.000000+00:00",
            "duration": 120.0,
            "data": {"app": "Comet", "title": "GitHub - Comet"},
        },
    ]
    # Web event is fully inside the Comet window event's span.
    web_events = [
        {
            "id": 10,
            "timestamp": "2026-04-11T10:07:30.000000+00:00",
            "duration": 45.0,
            "data": {"title": "GitHub", "url": "https://github.com/x/y"},
        }
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_web)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events_with_browser,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-web-comet_testhost/events",
        json=web_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)

    # App name comes from window-watcher (Comet), not from bucket id.
    comet_events = [e for e in window if e.app == "Comet"]
    assert len(comet_events) == 1
    assert comet_events[0].title == "GitHub - Comet"
    assert comet_events[0].duration == 120.0
    assert comet_events[0].url == "https://github.com/x/y"

    # Non-browser window events are untouched.
    code_events = [e for e in window if e.app == "Code"]
    assert len(code_events) == 2
    assert all(e.url is None for e in code_events)


@responses.activate
def test_web_events_with_zero_duration_still_match_window_event():
    """Web-watcher sometimes emits zero-duration events on navigation."""
    buckets_with_web = {
        **BUCKETS,
        "aw-watcher-web-comet_testhost": {"type": "web.tab.current"},
    }
    window_events = [
        {
            "id": 5,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 60.0,
            "data": {"app": "Comet", "title": "Perplexity - Comet"},
        },
    ]
    web_events = [
        {
            "id": 10,
            "timestamp": "2026-04-11T10:00:15.000000+00:00",
            "duration": 0.0,
            "data": {"title": "Perplexity", "url": "https://perplexity.ai/"},
        },
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_web)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-web-comet_testhost/events",
        json=web_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 1
    assert window[0].url == "https://perplexity.ai/"


@responses.activate
def test_non_browser_window_events_never_get_url_from_background_web_heartbeats():
    """
    Web-watcher keeps emitting heartbeat events for the currently-selected
    tab even when the browser is NOT in the foreground. A terminal window
    event whose span happens to cover one of those heartbeats must NOT get
    that URL — app name filtering is what prevents this leak.
    """
    buckets_with_web = {
        **BUCKETS,
        "aw-watcher-web-comet_testhost": {"type": "web.tab.current"},
    }
    window_events = [
        {
            "id": 1,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 600.0,
            "data": {"app": "Ghostty", "title": "~/code/aw-notion"},
        },
    ]
    web_events = [
        {
            "id": 10,
            "timestamp": "2026-04-11T10:05:00.000000+00:00",
            "duration": 0.0,
            "data": {"title": "GitHub", "url": "https://github.com/x/y"},
        },
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_web)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-web-comet_testhost/events",
        json=web_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 1
    assert window[0].app == "Ghostty"
    assert window[0].url is None


@responses.activate
def test_url_backfills_across_same_app_title_window_events():
    """
    If only one Comet window event for a given (app, title) had a temporal
    overlap with a web event, the URL is propagated to other Comet window
    events of the same (app, title) — even if they had no overlap of their
    own. This matches reality: title ≈ per-tab, so the URL is effectively
    a property of the tab, not of a specific moment.
    """
    buckets_with_web = {
        **BUCKETS,
        "aw-watcher-web-comet_testhost": {"type": "web.tab.current"},
    }
    # Two Comet events for the same tab: the first is long (no web overlap),
    # the second is short (web event overlaps it).
    window_events = [
        {
            "id": 1,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 300.0,
            "data": {"app": "Comet", "title": "GitHub - Comet"},
        },
        {
            "id": 2,
            "timestamp": "2026-04-11T10:10:00.000000+00:00",
            "duration": 60.0,
            "data": {"app": "Comet", "title": "GitHub - Comet"},
        },
    ]
    web_events = [
        {
            "id": 10,
            "timestamp": "2026-04-11T10:10:30.000000+00:00",
            "duration": 10.0,
            "data": {"title": "GitHub", "url": "https://github.com/x/y"},
        },
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_web)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-web-comet_testhost/events",
        json=web_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 2
    # Both events get the URL, even though only the second had a direct overlap.
    assert all(e.url == "https://github.com/x/y" for e in window)


@responses.activate
def test_window_events_without_overlap_have_no_url():
    """Window events that don't overlap any web event keep url=None."""
    buckets_with_web = {
        **BUCKETS,
        "aw-watcher-web-comet_testhost": {"type": "web.tab.current"},
    }
    window_events = [
        {
            "id": 5,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 60.0,
            "data": {"app": "Comet", "title": "Perplexity - Comet"},
        },
    ]
    # Web event is 10 minutes later — no overlap.
    web_events = [
        {
            "id": 10,
            "timestamp": "2026-04-11T10:10:00.000000+00:00",
            "duration": 30.0,
            "data": {"title": "Other", "url": "https://other.example/"},
        },
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_web)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-web-comet_testhost/events",
        json=web_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 1
    assert window[0].url is None


@responses.activate
def test_ax_bucket_enriches_window_events_with_note():
    """
    aw-watcher-ax heartbeat events set `note` on overlapping window events
    whose app matches. Same timestamp-overlap pattern as web enrichment.
    """
    buckets_with_ax = {
        **BUCKETS,
        "aw-watcher-ax_testhost": {"type": "currentwindow"},
    }
    window_events = [
        {
            "id": 1,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 300.0,
            "data": {"app": "Claude", "title": "Claude"},
        },
    ]
    ax_events = [
        {
            "id": 20,
            "timestamp": "2026-04-11T10:01:00.000000+00:00",
            "duration": 120.0,
            "data": {"app": "Claude", "context": "Conversation about cats"},
        },
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_ax)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-ax_testhost/events",
        json=ax_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 1
    assert window[0].note == "Conversation about cats"


@responses.activate
def test_ax_bucket_missing_is_fine():
    """No ax-watcher bucket present → window events have note=None, no error."""
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
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 2
    assert all(e.note is None for e in window)


@responses.activate
def test_ax_note_respects_app_filter():
    """
    ax events are filtered by app name to prevent a Claude context string
    from leaking onto a temporally overlapping Telegram window event.
    """
    buckets_with_ax = {
        **BUCKETS,
        "aw-watcher-ax_testhost": {"type": "currentwindow"},
    }
    window_events = [
        {
            "id": 1,
            "timestamp": "2026-04-11T10:00:00.000000+00:00",
            "duration": 300.0,
            "data": {"app": "Telegram", "title": "Telegram"},
        },
    ]
    # ax event is for Claude but temporally overlaps the Telegram window event.
    ax_events = [
        {
            "id": 20,
            "timestamp": "2026-04-11T10:01:00.000000+00:00",
            "duration": 120.0,
            "data": {"app": "Claude", "context": "Conversation about cats"},
        },
    ]
    responses.add(responses.GET, f"{BASE}/buckets", json=buckets_with_ax)
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-window_testhost/events",
        json=window_events,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-afk_testhost/events",
        json=AFK_EVENTS,
    )
    responses.add(
        responses.GET,
        f"{BASE}/buckets/aw-watcher-ax_testhost/events",
        json=ax_events,
    )
    client = ActivityWatchClient()
    start = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 11, 11, 0, tzinfo=UTC)
    window, _ = client.get_all_events(start, end)
    assert len(window) == 1
    assert window[0].note is None
