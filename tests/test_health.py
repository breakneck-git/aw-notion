import logging
import subprocess
from datetime import UTC, datetime

import requests

from aw_notion import health


def _buckets(afk: str | None, window: str | None) -> dict:
    b: dict = {"aw-stopwatch": {"last_updated": None}}
    if afk is not None:
        b["aw-watcher-afk_host"] = {"last_updated": afk}
    if window is not None:
        b["aw-watcher-window_host"] = {"last_updated": window}
    return b


class FakeAW:
    """Returns successive (running, buckets) snapshots, one per check."""

    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self._current = None

    def is_running(self):
        self._current = self._snapshots.pop(0)
        return self._current[0]

    def buckets(self):
        if isinstance(self._current[1], Exception):
            raise self._current[1]
        return self._current[1]


FRESH = _buckets("2026-09-27T01:00:00+00:00", "2026-09-27T00:59:59+00:00")
STALE = _buckets("2026-09-27T01:00:00+00:00", "2026-09-25T02:50:49+00:00")


NOW = datetime(2026, 9, 27, 1, 0, 30, tzinfo=UTC)
# Both watchers stopped at the same moment an hour ago: zero lag between them.
BOTH_STALE = _buckets("2026-09-27T00:00:00+00:00", "2026-09-27T00:00:00+00:00")


def _run(snapshots, state_path, delivered=True):
    sleeps: list[float] = []
    alerts: list[str] = []

    def alert(message):
        alerts.append(message)
        return delivered

    ok = health.check_health(
        FakeAW(snapshots),
        sleep=sleeps.append,
        alert=alert,
        state_path=state_path,
        now=lambda: NOW,
    )
    return ok, sleeps, alerts


def test_window_lag_is_afk_minus_window():
    lag = health.window_lag_sec(STALE)
    assert lag is not None and lag > 40 * 3600


def test_window_lag_none_when_a_bucket_is_missing():
    assert health.window_lag_sec(_buckets("2026-09-27T01:00:00+00:00", None)) is None
    assert health.window_lag_sec(_buckets(None, "2026-09-27T01:00:00+00:00")) is None


def test_window_lag_takes_newest_bucket_per_prefix():
    b = STALE | {"aw-watcher-window_other": {"last_updated": "2026-09-27T01:00:00+00:00"}}
    assert health.window_lag_sec(b) == 0


def test_healthy_does_not_sleep_or_alert(tmp_path):
    ok, sleeps, alerts = _run([(True, FRESH)], tmp_path / "a")
    assert ok and sleeps == [] and alerts == []


def test_server_down_persistently_alerts_and_reports_not_ok(tmp_path):
    ok, sleeps, alerts = _run([(False, None), (False, None)], tmp_path / "a")
    assert not ok
    assert sleeps == [health.RECHECK_SEC]
    assert len(alerts) == 1 and "aw-server" in alerts[0]


def test_server_down_transiently_does_not_alert(tmp_path):
    # e.g. RunAtLoad at login fires before ActivityWatch has started
    ok, sleeps, alerts = _run([(False, None), (True, FRESH)], tmp_path / "a")
    assert ok and alerts == [] and sleeps == [health.RECHECK_SEC]


def test_window_watcher_dead_alerts_but_sync_proceeds(tmp_path):
    ok, sleeps, alerts = _run([(True, STALE), (True, STALE)], tmp_path / "a")
    assert ok
    assert len(alerts) == 1 and "aw-watcher-window" in alerts[0]


def test_window_lag_right_after_wake_does_not_alert(tmp_path):
    # Sync fires on wake before window-watcher's first post-sleep heartbeat.
    ok, sleeps, alerts = _run([(True, STALE), (True, FRESH)], tmp_path / "a")
    assert ok and alerts == [] and sleeps == [health.RECHECK_SEC]


def test_ongoing_problem_alerts_only_once(tmp_path):
    st = tmp_path / "a"
    _, _, first = _run([(True, STALE), (True, STALE)], st)
    _, _, second = _run([(True, STALE), (True, STALE)], st)
    assert len(first) == 1 and second == []


def test_recovery_sends_one_all_clear(tmp_path):
    st = tmp_path / "a"
    _run([(True, STALE), (True, STALE)], st)
    _, _, alerts = _run([(True, FRESH)], st)
    assert len(alerts) == 1 and "восстановлен" in alerts[0]
    _, _, again = _run([(True, FRESH)], st)
    assert again == []


def test_changed_problem_alerts_again(tmp_path):
    st = tmp_path / "a"
    _run([(True, STALE), (True, STALE)], st)
    _, _, alerts = _run([(False, None), (False, None)], st)
    assert len(alerts) == 1 and "aw-server" in alerts[0]


def test_alert_is_logged_as_error(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        _run([(True, STALE), (True, STALE)], tmp_path / "a")
    assert any("aw-watcher-window" in r.message for r in caplog.records)


def test_send_alert_uses_owner_alert_with_text_on_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(health.shutil, "which", lambda name, path=None: f"/bin/{name}")

    def fake_run(args, **k):
        calls.append((args, k.get("input")))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(health.subprocess, "run", fake_run)
    health.send_alert('bad "quote"')
    assert len(calls) == 1
    args, stdin = calls[0]
    assert args[0] == "/bin/owner-alert" and args[1:3] == ["aw-notion", "-"]
    assert stdin == 'bad "quote"'


def test_send_alert_falls_back_to_desktop_when_owner_alert_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(health.shutil, "which", lambda name, path=None: f"/bin/{name}")

    def fake_run(args, **k):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if "owner-alert" in args[0] else 0)

    monkeypatch.setattr(health.subprocess, "run", fake_run)
    health.send_alert("m")
    assert [c[0] for c in calls] == ["/bin/owner-alert", "/bin/osascript"]
    assert calls[1][-1] == "m"  # text passed as argv, not spliced into the script


def test_send_alert_falls_back_when_owner_alert_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        health.shutil,
        "which",
        lambda name, path=None: None if name == "owner-alert" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0),
    )
    health.send_alert("m")
    assert [c[0] for c in calls] == ["/bin/osascript"]


def test_send_alert_never_raises(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name, path=None: f"/bin/{name}")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 10)

    monkeypatch.setattr(health.subprocess, "run", boom)
    health.send_alert("m")


def test_buckets_request_failing_alerts_instead_of_crashing(tmp_path):
    """Review #3: /info answers but /buckets raises — used to propagate out of
    check_health and kill the sync with no alert at all."""
    boom = requests.HTTPError("500 Server Error")
    ok, _, alerts = _run([(True, boom), (True, boom)], tmp_path / "a")
    assert not ok, "sync can't run without the bucket list"
    assert len(alerts) == 1 and "buckets" in alerts[0]


def test_both_watchers_dead_alerts_even_with_zero_lag(tmp_path):
    """Review #4: when afk and window die together their lag is ~0, so the
    relative check stays silent. Newest heartbeat of either vs the wall clock
    catches it (afk alone can't be used: it stalls 11–34 min by itself)."""
    ok, _, alerts = _run([(True, BOTH_STALE), (True, BOTH_STALE)], tmp_path / "a")
    assert ok, "stale watchers alert but don't block the sync"
    assert len(alerts) == 1 and "watcher" in alerts[0]


def test_fresh_window_with_stalled_afk_is_healthy(tmp_path):
    b = _buckets("2026-09-27T00:30:00+00:00", "2026-09-27T01:00:29+00:00")
    ok, sleeps, alerts = _run([(True, b)], tmp_path / "a")
    assert ok and sleeps == [] and alerts == []


def test_recheck_finding_a_different_problem_does_not_alert(tmp_path):
    """Review #7: at login the first check sees the server down, the recheck a
    window bucket still stale from before logout — neither is confirmed, so
    no alert (and no state change) this round."""
    st = tmp_path / "a"
    ok, _, alerts = _run([(False, None), (True, STALE)], st)
    assert ok and alerts == []
    assert not st.exists()


def test_undelivered_alert_is_retried_next_sync(tmp_path):
    """Review #8: an alert that reached no channel must not be recorded as
    sent, or the problem is never reported."""
    st = tmp_path / "a"
    _, _, first = _run([(True, STALE), (True, STALE)], st, delivered=False)
    _, _, second = _run([(True, STALE), (True, STALE)], st)
    assert len(first) == 1 and len(second) == 1


def test_corrupt_alert_state_is_treated_as_healthy(tmp_path):
    st = tmp_path / "a"
    st.write_bytes(b"\xff\xfe\x00garbage")
    ok, _, alerts = _run([(True, STALE), (True, STALE)], st)
    assert ok and len(alerts) == 1


def test_alert_state_write_is_atomic(tmp_path, monkeypatch):
    st = tmp_path / "a"
    _run([(True, STALE), (True, STALE)], st)
    before = st.read_text(encoding="utf-8")

    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(health.os, "replace", fail)
    _run([(True, FRESH)], st)  # recovery tries to overwrite the state
    assert st.read_text(encoding="utf-8") == before
    assert list(tmp_path.iterdir()) == [st], "no temp file left behind"


def test_malformed_or_naive_bucket_timestamps_do_not_crash(tmp_path):
    b = {
        "aw-watcher-afk_h": {"last_updated": "2026-09-27T01:00:00"},  # naive → UTC
        "aw-watcher-window_h": {"last_updated": "not a date"},
        "aw-watcher-window_h2": {"last_updated": "2026-09-27T01:00:00+00:00"},
    }
    ok, sleeps, alerts = _run([(True, b)], tmp_path / "a")
    assert ok and sleeps == [] and alerts == []


def test_send_alert_reports_delivery(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(
        health.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 1)
    )
    assert health.send_alert("m") is False
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda args, **k: subprocess.CompletedProcess(args, 1 if "owner-alert" in args[0] else 0),
    )
    assert health.send_alert("m") is True


def test_stalled_afk_without_window_bucket_is_not_an_alert(tmp_path):
    b = _buckets("2026-09-27T00:30:00+00:00", None)
    ok, sleeps, alerts = _run([(True, b)], tmp_path / "a")
    assert ok and sleeps == [] and alerts == []
