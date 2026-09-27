import pytest

from aw_notion import cli


@pytest.fixture(autouse=True)
def _no_real_health_check(monkeypatch):
    # The real check may sleep RECHECK_SEC and post a desktop notification;
    # CLI tests use FakeAW stubs, so reduce it to the plain liveness probe.
    # Tests exercising the check itself live in test_health.py.
    monkeypatch.setattr(cli, "check_health", lambda aw, **_: aw.is_running())
