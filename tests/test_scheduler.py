from types import SimpleNamespace

from vitalis.config import settings
from vitalis.scheduler import jobs


def test_scheduler_uses_configured_timezone_cron_and_single_dispatcher(monkeypatch):
    captured = []

    class FakeScheduler:
        def __init__(self, *, timezone):
            self.timezone = timezone

        def add_job(self, func, trigger, **kwargs):
            captured.append((func, trigger, kwargs))

        def start(self):
            return None

    monkeypatch.setattr(jobs, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "sync_cron_hour", 4)
    monkeypatch.setattr(settings, "sync_cron_minute", 25)
    monkeypatch.setattr(settings, "sync_dispatcher_interval_seconds", 17)

    scheduler = jobs.start_scheduler()

    assert scheduler.timezone == "UTC"
    nightly = next(item for item in captured if item[2]["id"] == "nightly_sync")
    assert "hour='4'" in str(nightly[1])
    assert "minute='25'" in str(nightly[1])
    dispatcher = next(item for item in captured if item[2]["id"] == "sync_dispatcher")
    assert dispatcher[2]["max_instances"] == 1
    assert dispatcher[2]["coalesce"] is True
    assert dispatcher[1].interval.total_seconds() == 17


def test_scheduled_sync_only_enqueues_attempt(monkeypatch):
    attempt = SimpleNamespace(id="attempt-1")
    calls = []
    monkeypatch.setattr(jobs, "_create_attempt", lambda user, days, trigger: calls.append((user, days, trigger)) or attempt)

    assert jobs._sync_user("user-1", 7, "nightly") == "attempt-1"
    assert calls == [("user-1", 7, "nightly")]


def test_dispatcher_drains_until_no_due_attempt(monkeypatch):
    reports = [SimpleNamespace(), None]

    class FakeCoordinator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def drain_once(self):
            return reports.pop(0)

    monkeypatch.setattr(
        "vitalis.connectors.get_connector", lambda _source: object()
    )
    monkeypatch.setattr(
        "vitalis.services.zepp_sync_coordinator.ZeppSyncCoordinator",
        FakeCoordinator,
    )

    assert jobs.dispatcher_job() == 1


def test_dispatcher_limits_each_pass_to_configured_chunk_batch(monkeypatch):
    calls = []

    class FakeCoordinator:
        def __init__(self, **_kwargs):
            pass

        def drain_once(self):
            calls.append(1)
            return SimpleNamespace()

    monkeypatch.setattr(settings, "sync_dispatcher_batch_chunks", 3)
    monkeypatch.setattr("vitalis.connectors.get_connector", lambda _source: object())
    monkeypatch.setattr(
        "vitalis.services.zepp_sync_coordinator.ZeppSyncCoordinator",
        FakeCoordinator,
    )

    assert jobs.dispatcher_job() == 3
    assert len(calls) == 3
