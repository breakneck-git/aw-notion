from datetime import UTC, datetime, timedelta

from aw_notion.blocks import AFKEvent, AWEvent, compute_focus_blocks


def dt(offset_sec: float) -> datetime:
    """Helper: UTC datetime at base + offset_sec."""
    base = datetime(2026, 4, 11, 10, 0, 0, tzinfo=UTC)
    return base + timedelta(seconds=offset_sec)


def win(
    offset_sec: float,
    duration: float,
    app: str,
    title: str,
    url=None,
    note=None,
) -> AWEvent:
    return AWEvent(
        timestamp=dt(offset_sec),
        duration=duration,
        app=app,
        title=title,
        url=url,
        note=note,
    )


def afk(offset_sec: float, duration: float) -> AFKEvent:
    return AFKEvent(timestamp=dt(offset_sec), duration=duration, status="afk")


def test_single_event_above_minimum():
    events = [win(0, 200, "Code", "file.py")]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 1
    assert blocks[0].app == "Code"
    assert blocks[0].title == "file.py"
    assert blocks[0].active_seconds == 200


def test_single_event_below_minimum_filtered():
    events = [win(0, 60, "Code", "file.py")]  # 60s < 120s min
    blocks = compute_focus_blocks(events, [])
    assert blocks == []


def test_same_app_title_gap_under_merge_threshold_merges():
    events = [
        win(0, 100, "Code", "file.py"),
        win(200, 100, "Code", "file.py"),  # gap = 100s < 180s → merge
    ]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 1
    assert blocks[0].active_seconds == 200


def test_same_app_title_gap_over_merge_threshold_splits():
    events = [
        win(0, 150, "Code", "file.py"),
        win(400, 150, "Code", "file.py"),  # gap = 250s > 180s → split
    ]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 2


def test_different_app_creates_new_block():
    events = [
        win(0, 200, "Code", "file.py"),
        win(200, 200, "Chrome", "GitHub"),
    ]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 2
    assert blocks[0].app == "Code"
    assert blocks[1].app == "Chrome"


def test_afk_event_filters_window_events():
    events = [
        win(0, 200, "Code", "file.py"),
        win(300, 200, "Code", "file.py"),  # during AFK
    ]
    afk_events = [afk(250, 400)]  # AFK from t=250 to t=650
    blocks = compute_focus_blocks(events, afk_events)
    assert len(blocks) == 1
    assert blocks[0].active_seconds == 200


def test_afk_hard_boundary_separates_blocks():
    events = [
        win(0, 150, "Code", "file.py"),
        win(800, 150, "Code", "file.py"),  # gap = 650s > 600s threshold
    ]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 2


def test_url_preserved_in_block():
    events = [win(0, 200, "Chrome", "GitHub", url="https://github.com/x/y")]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 1
    assert blocks[0].url == "https://github.com/x/y"


def test_url_backfilled_from_later_merged_event():
    """
    If the first event in a merged run has no URL but a subsequent merged
    event does, the block must keep that URL. Regression guard: when
    web-watcher arrived late into a sequence of same-(app,title) window
    events, earlier aw-notion versions dropped the URL.
    """
    events = [
        win(0, 100, "Comet", "GitHub - Comet", url=None),
        win(150, 100, "Comet", "GitHub - Comet", url="https://github.com/x/y"),
    ]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 1
    assert blocks[0].url == "https://github.com/x/y"


def test_empty_events_returns_empty():
    assert compute_focus_blocks([], []) == []


def test_block_signature_is_stable():
    events = [win(0, 200, "Code", "file.py")]
    b1 = compute_focus_blocks(events, [])[0]
    b2 = compute_focus_blocks(events, [])[0]
    assert b1.signature() == b2.signature()


def test_block_active_minutes_rounds():
    events = [win(0, 190, "Code", "file.py")]  # 190s = 3.16 min → rounds to 3
    blocks = compute_focus_blocks(events, [])
    assert blocks[0].active_minutes() == 3


def test_note_preserved_in_block():
    events = [win(0, 200, "Claude", "Claude", note="Conversation about cats")]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 1
    assert blocks[0].note == "Conversation about cats"


def test_note_backfilled_from_later_merged_event():
    events = [
        win(0, 100, "Claude", "Claude", note=None),
        win(150, 100, "Claude", "Claude", note="Conversation about cats"),
    ]
    blocks = compute_focus_blocks(events, [])
    assert len(blocks) == 1
    assert blocks[0].note == "Conversation about cats"


def test_note_not_in_signature():
    """Signature must be stable regardless of note — idempotency key is
    (app, title, start_utc) only."""
    e1 = [win(0, 200, "Claude", "Claude", note="First context")]
    e2 = [win(0, 200, "Claude", "Claude", note="Second context")]
    assert (
        compute_focus_blocks(e1, [])[0].signature() == compute_focus_blocks(e2, [])[0].signature()
    )
