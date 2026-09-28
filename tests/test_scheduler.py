from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import HealthRepository, init_db
from vitalis.adapters.persistence.models import AuthToken, SourceAccount, User
from vitalis.config import settings
from vitalis.scheduler import jobs


def test_scheduler_uses_configured_timezone_cron_and_single_dispatcher(monkeypatch):
    captured = []

    class FakeScheduler:
        def __init__(self, *, timezone):
            self.timezone = timezone
            self.started = False

        def add_job(self, func, trigger, **kwargs):
            captured.append((func, trigger, kwargs))

        def start(self):
            self.started = True

    monkeypatch.setattr(jobs, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "sync_cron_hour", 4)
    monkeypatch.setattr(settings, "sync_cron_minute", 25)
    monkeypatch.setattr(settings, "sync_dispatcher_interval_seconds", 17)

    scheduler = jobs.start_scheduler()

    assert scheduler.timezone == "UTC"
    assert scheduler.started
    assert {item[2]["id"] for item in captured} == {
        "nightly_sync", "morning_analysis", "evening_analysis", "sync_dispatcher",
        "worker_heartbeat",
    }
    nightly = next(item for item in captured if item[2]["id"] == "nightly_sync")
    assert nightly[0] is jobs.nightly_sync_job
    assert "hour='4'" in str(nightly[1])
    assert "minute='25'" in str(nightly[1])
    morning = next(item for item in captured if item[2]["id"] == "morning_analysis")
    assert morning[0] is jobs.morning_analysis_job
    assert "hour='9'" in str(morning[1])
    assert "minute='30'" in str(morning[1])
    evening = next(item for item in captured if item[2]["id"] == "evening_analysis")
    assert evening[0] is jobs.evening_analysis_job
    assert "hour='21'" in str(evening[1])
    assert "minute='30'" in str(evening[1])
    dispatcher = next(item for item in captured if item[2]["id"] == "sync_dispatcher")
    assert dispatcher[0] is jobs.dispatcher_job
    assert all(item[2]["max_instances"] == 1 and item[2]["coalesce"] for item in captured)
    assert dispatcher[1].interval.total_seconds() == 17
    heartbeat = next(item for item in captured if item[2]["id"] == "worker_heartbeat")
    assert heartbeat[0] is jobs.worker_heartbeat_job
    assert heartbeat[1].interval.total_seconds() == 30
    startup_delay = datetime.now(timezone.utc) - dispatcher[2]["next_run_time"]
    assert abs(startup_delay.total_seconds()) < 5


def test_scheduled_sync_only_enqueues_attempt(monkeypatch):
    attempt = SimpleNamespace(id="attempt-1")
    calls = []
    monkeypatch.setattr(jobs, "_create_attempt", lambda user, days, trigger: calls.append((user, days, trigger)) or attempt)

    assert jobs._sync_user("user-1", 7, "nightly") == "attempt-1"
    assert calls == [("user-1", 7, "nightly")]


def test_authorized_users_require_active_source_and_usable_credential(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def isolated_session_scope():
        with factory.begin() as db:
            yield db

    monkeypatch.setattr("vitalis.adapters.persistence.session_scope", isolated_session_scope)
    try:
        with factory.begin() as db:
            db.add_all([
                User(id="scheduled-user"),
                User(id="revoked-user"),
                User(id="empty-credential-user"),
            ])
            db.add_all([
                SourceAccount(
                    id="scheduled-account", user_id="scheduled-user", source="zepp",
                    vendor_id="vendor-scheduled", status="active",
                ),
                SourceAccount(
                    id="revoked-account", user_id="revoked-user", source="zepp",
                    vendor_id="vendor-revoked", status="revoked",
                ),
                SourceAccount(
                    id="empty-account", user_id="empty-credential-user", source="zepp",
                    vendor_id="vendor-empty", status="active",
                ),
            ])
            db.add_all([
                AuthToken(
                    source_account_id="scheduled-account", access_token="fernet:synthetic",
                ),
                AuthToken(
                    source_account_id="revoked-account", access_token="fernet:synthetic",
                ),
                AuthToken(source_account_id="empty-account", access_token=""),
            ])

        assert jobs._get_authorized_users() == {"scheduled-user"}

        with factory.begin() as db:
            assert HealthRepository(db).revoke_source_account("scheduled-user", "zepp")

        assert jobs._get_authorized_users() == set()
    finally:
        engine.dispose()


def test_dispatcher_drains_until_no_due_attempt(monkeypatch):
    reports = [SimpleNamespace(), None]

    class FakeCoordinator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def drain_once(self):
            return reports.pop(0)

    monkeypatch.setattr(
        "vitalis.bootstrap.get_connector", lambda _source: object()
    )
    monkeypatch.setattr(
        "vitalis.adapters.zepp.sync_coordinator.ZeppSyncCoordinator",
        FakeCoordinator,
    )
    monkeypatch.setattr(jobs, "drain_notification_deliveries", lambda **_: 0)

    assert jobs.dispatcher_job() == 1


def test_worker_dispatcher_drains_one_analysis_job_before_sync(monkeypatch):
    calls = []

    class FakeCoordinator:
        def __init__(self, **_kwargs):
            pass

        def drain_once(self):
            calls.append("sync")
            return None

    def drain_analysis(*, max_jobs):
        calls.append(("analysis", max_jobs))
        return 1

    monkeypatch.setattr("vitalis.bootstrap.get_connector", lambda _source: object())
    monkeypatch.setattr(
        "vitalis.adapters.zepp.sync_coordinator.ZeppSyncCoordinator",
        FakeCoordinator,
    )
    monkeypatch.setattr("vitalis.application.jobs.drain_analysis_jobs", drain_analysis)

    assert jobs.dispatcher_job() == 1
    assert calls == [("analysis", 1), "sync"]


def test_analysis_dispatch_is_not_starved_by_sync_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "vitalis.application.jobs.drain_analysis_jobs",
        lambda *, max_jobs: calls.append(max_jobs) or 1,
    )
    def unavailable(_source):
        raise RuntimeError("sync unavailable")

    monkeypatch.setattr("vitalis.bootstrap.get_connector", unavailable)

    with pytest.raises(RuntimeError, match="sync unavailable"):
        jobs.dispatcher_job()
    assert calls == [1]


def test_dispatcher_drains_notification_delivery_after_sync(monkeypatch):
    calls = []

    class FakeCoordinator:
        def __init__(self, **_kwargs):
            pass

        def drain_once(self):
            calls.append("sync")
            return None

    monkeypatch.setattr("vitalis.bootstrap.get_connector", lambda _source: object())
    monkeypatch.setattr(
        "vitalis.adapters.zepp.sync_coordinator.ZeppSyncCoordinator",
        FakeCoordinator,
    )
    monkeypatch.setattr(
        "vitalis.application.jobs.drain_analysis_jobs",
        lambda *, max_jobs: 0,
    )
    monkeypatch.setattr(
        jobs, "drain_notification_deliveries", lambda **_: calls.append("notification") or 1
    )

    assert jobs.dispatcher_job() == 1
    assert calls == ["sync", "notification"]


def test_dispatcher_limits_each_pass_to_configured_chunk_batch(monkeypatch):
    calls = []

    class FakeCoordinator:
        def __init__(self, **_kwargs):
            pass

        def drain_once(self):
            calls.append(1)
            return SimpleNamespace()

    monkeypatch.setattr(settings, "sync_dispatcher_batch_chunks", 3)
    monkeypatch.setattr("vitalis.bootstrap.get_connector", lambda _source: object())
    monkeypatch.setattr(
        "vitalis.adapters.zepp.sync_coordinator.ZeppSyncCoordinator",
        FakeCoordinator,
    )
    monkeypatch.setattr(jobs, "drain_notification_deliveries", lambda **_: 0)

    assert jobs.dispatcher_job() == 3
    assert len(calls) == 3
