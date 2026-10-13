from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import socket
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import HealthRepository, init_db
from vitalis.adapters.persistence.models import (
    AuthToken, SourceAccount, SyncAttempt, User, WorkerHeartbeat,
)
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
    monkeypatch.setattr(settings, "weekly_report_enabled", False)
    monkeypatch.setattr(settings, "monthly_report_enabled", False)

    scheduler = jobs.start_scheduler()

    assert scheduler.timezone == "UTC"
    assert scheduler.started
    assert {item[2]["id"] for item in captured} == {
        "nightly_sync", "morning_analysis", "evening_analysis", "sync_dispatcher",
        "worker_heartbeat", "report_catch_up", "morning_wake_poll",
    }
    catch_up = next(item for item in captured if item[2]["id"] == "report_catch_up")
    assert catch_up[0] is jobs.catch_up_scheduled_reports
    assert catch_up[1].interval.total_seconds() == 300
    assert abs((datetime.now(timezone.utc) - catch_up[2]["next_run_time"]).total_seconds()) < 5
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


def test_scheduler_registers_optional_calendar_report_jobs(monkeypatch):
    captured = []

    class FakeScheduler:
        def __init__(self, *, timezone):
            self.timezone = timezone

        def add_job(self, func, trigger, **kwargs):
            captured.append((func, trigger, kwargs))

        def start(self):
            return None

    monkeypatch.setattr(jobs, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(settings, "weekly_report_enabled", True)
    monkeypatch.setattr(settings, "weekly_report_hour", 8)
    monkeypatch.setattr(settings, "weekly_report_minute", 15)
    monkeypatch.setattr(settings, "monthly_report_enabled", True)
    monkeypatch.setattr(settings, "monthly_report_hour", 9)
    monkeypatch.setattr(settings, "monthly_report_minute", 45)

    jobs.start_scheduler()

    weekly = next(item for item in captured if item[2]["id"] == "weekly_analysis")
    monthly = next(item for item in captured if item[2]["id"] == "monthly_analysis")
    assert weekly[0] is jobs.weekly_analysis_job
    assert "day_of_week='mon'" in str(weekly[1])
    assert "hour='8'" in str(weekly[1])
    assert "minute='15'" in str(weekly[1])
    assert monthly[0] is jobs.monthly_analysis_job
    assert "day='1'" in str(monthly[1])
    assert "hour='9'" in str(monthly[1])
    assert "minute='45'" in str(monthly[1])


@pytest.mark.parametrize(("target_day", "timezone_name"), [
    (date(2026, 8, 31), "Asia/Shanghai"),
    (date(2026, 3, 9), "America/New_York"),
    (date(2026, 11, 2), "America/New_York"),
])
def test_weekly_sync_covers_full_report_period(monkeypatch, target_day, timezone_name):
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator
    from vitalis.intelligence.report_periods import delivery_period_dates

    queued = []
    coordinator = ZeppSyncCoordinator()

    def create_attempt(user_id, *, days, trigger, trigger_ref):
        queued.append((trigger, coordinator._window(None, days)))
        return SimpleNamespace(id="weekly-window-attempt")

    monkeypatch.setattr(settings, "timezone", timezone_name)
    monkeypatch.setattr("vitalis.time.local_today", lambda: target_day)
    monkeypatch.setattr(jobs, "_get_authorized_users", lambda: {"weekly-window-user"})
    monkeypatch.setattr(
        "vitalis.bootstrap.get_connector",
        lambda _source: SimpleNamespace(create_attempt=create_attempt),
    )
    monkeypatch.setattr(jobs, "dispatcher_job", lambda: 0)

    jobs.weekly_analysis_job()

    assert len(queued) == 1
    trigger, window = queued[0]
    period_start, _ = delivery_period_dates("weekly", target_day)
    assert trigger == "weekly"
    assert window.start_day(timezone_name) == period_start.isoformat()
    assert window.end_day(timezone_name) == target_day.isoformat()


@pytest.mark.parametrize("status,reason,offset,expected", [
    ("deferred", "snapshot_unavailable", 0, 1),
    ("deferred", "sleep_incomplete", 0, 1),
    ("deferred", "stored_data_incomplete", 0, 1),
    ("accepted", None, 0, 0),
    ("uncertain", "transport_ambiguous", 0, 0),
    ("running", None, 0, 0),
    ("deferred", "delivery_disabled", 0, 0),
    ("deferred", "stale_report_date", 0, 0),
    ("deferred", "snapshot_unavailable", -1, 0),
    ("deferred", "snapshot_unavailable", 1, 0),
])
def test_reconcile_deferred_daily_reports_queues_one_idempotent_reanalysis(
    tmp_path, monkeypatch, status, reason, offset, expected,
):
    from vitalis.adapters.persistence import database
    from vitalis.adapters.persistence.analysis_jobs import SqlAnalysisJobRepository
    from vitalis.adapters.persistence.models import AnalysisJob, AnalysisRun, NotificationDelivery, User
    from vitalis.application.jobs import configure_analysis_jobs
    from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
    from vitalis.intelligence import contracts

    day = date(2026, 8, 29)
    target_date = day + timedelta(days=offset)
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'reconcile.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        with factory.begin() as db:
            db.add(User(id="owner"))
            db.add(AnalysisRun(
                id="old-run", user_id="owner", target_date=target_date, status="SUCCEEDED",
                started_at=datetime(2026, 8, 29, 12),
                completed_at=datetime(2026, 8, 29, 12),
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                config_digest=_current_analysis_config_digest(),
            ))
            db.add(NotificationDelivery(
                id="deferred-daily", user_id="owner", analysis_run_id="old-run",
                period="morning", target_date=target_date, status=status,
                last_error=reason,
                lease_token="existing-lease" if status == "running" else None,
                lease_expires_at=(
                    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
                    if status == "running" else None
                ),
            ))
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(settings, "push_user", "owner")
        monkeypatch.setattr(settings, "pushplus_token", "offline-token")
        monkeypatch.setattr("vitalis.time.local_today", lambda: day)
        monkeypatch.setattr(
            jobs,
            "_utc_now",
            lambda: datetime(2026, 8, 29, 1, 45, tzinfo=timezone.utc),
        )
        configure_analysis_jobs(
            repository=SqlAnalysisJobRepository(factory),
            runner=lambda _claim: "unused",
        )

        assert jobs.reconcile_deferred_daily_reports() == expected
        assert jobs.reconcile_deferred_daily_reports() == 0
        with factory() as db:
            rows = db.query(AnalysisJob).all()
            expected_analysis_jobs = 0 if reason == "sleep_incomplete" else expected
            assert len(rows) == expected_analysis_jobs
            if rows:
                assert rows[0].target_date == day
                assert rows[0].delivery_period is None
                assert rows[0].idempotency_key.startswith("daily-recovery:")
    finally:
        engine.dispose()


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
    monkeypatch.setattr("vitalis.application.jobs.drain_analysis_jobs", lambda **_: 0)

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


def test_dispatcher_drains_notification_delivery_before_sync(monkeypatch):
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
    assert calls == ["notification", "sync"]


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
    monkeypatch.setattr("vitalis.application.jobs.drain_analysis_jobs", lambda **_: 0)

    assert jobs.dispatcher_job() == 3
    assert len(calls) == 3


def _shanghai(*parts):
    return datetime(*parts, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)


@pytest.fixture
def catch_up_database(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def isolated_session_scope():
        with factory.begin() as db:
            yield db

    queued = []

    def sync_user(user_id, days, label):
        queued.append((user_id, days, label))
        _add_attempt(factory, f"queued-{len(queued)}", label, jobs._utc_now())
        return f"queued-{len(queued)}"

    monkeypatch.setattr("vitalis.adapters.persistence.session_scope", isolated_session_scope)
    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    monkeypatch.setattr(settings, "push_user", "owner")
    monkeypatch.setattr(settings, "pushplus_token", "offline-token")
    monkeypatch.setattr(jobs, "_get_authorized_users", lambda: {"owner", "other"})
    monkeypatch.setattr(jobs, "_sync_user", sync_user)
    yield factory, queued
    engine.dispose()


def _add_attempt(factory, attempt_id, trigger, created_at, trigger_ref="owner"):
    with factory.begin() as db:
        db.add(SyncAttempt(
            id=attempt_id, user_id="owner", trigger=trigger, trigger_ref=trigger_ref,
            request_key=attempt_id, window_start=datetime(2026, 10, 11, 16),
            window_end=datetime(2026, 10, 12, 16), status="succeeded",
            created_at=created_at.replace(tzinfo=None),
        ))


@pytest.mark.parametrize("local_now, expected", [
    ((2026, 10, 12, 9, 31), []),
    ((2026, 10, 12, 10, 5), [("owner", 2, "morning")]),
    ((2026, 10, 12, 12, 31), []),
    ((2026, 10, 12, 21, 45), [("owner", 1, "evening")]),
    ((2026, 10, 13, 0, 10), []),
])
def test_missed_report_sync_is_caught_up_once_inside_its_window(
    catch_up_database, monkeypatch, local_now, expected,
):
    _factory, queued = catch_up_database
    monkeypatch.setattr(jobs, "_utc_now", lambda: _shanghai(*local_now))

    assert jobs.catch_up_scheduled_reports() == len(expected)
    assert jobs.catch_up_scheduled_reports() == 0
    assert queued == expected


def test_catch_up_counts_only_todays_scheduled_sync(catch_up_database, monkeypatch):
    factory, queued = catch_up_database
    _add_attempt(factory, "yesterday", "morning", _shanghai(2026, 10, 11, 9, 30))
    _add_attempt(
        factory, "sleep-retry", "morning", _shanghai(2026, 10, 12, 9, 40),
        trigger_ref="morning-sleep-retry:2026-10-12",
    )
    monkeypatch.setattr(jobs, "_utc_now", lambda: _shanghai(2026, 10, 12, 10, 5))
    assert jobs.catch_up_scheduled_reports() == 1

    _add_attempt(factory, "evening-cron", "evening", _shanghai(2026, 10, 12, 21, 30))
    monkeypatch.setattr(jobs, "_utc_now", lambda: _shanghai(2026, 10, 12, 21, 45))
    assert jobs.catch_up_scheduled_reports() == 0
    assert queued == [("owner", 2, "morning")]


def test_catch_up_needs_a_configured_delivery_owner(catch_up_database, monkeypatch):
    _factory, queued = catch_up_database
    monkeypatch.setattr(settings, "pushplus_token", "")
    monkeypatch.setattr(jobs, "_utc_now", lambda: _shanghai(2026, 10, 12, 10, 5))

    assert jobs.catch_up_scheduled_reports() == 0
    assert queued == []


def test_heartbeat_feeds_the_systemd_watchdog(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def isolated_session_scope():
        with factory.begin() as db:
            yield db

    notes = []
    monkeypatch.setattr("vitalis.adapters.persistence.session_scope", isolated_session_scope)
    monkeypatch.setattr(jobs, "_notify_systemd", notes.append)
    try:
        jobs.worker_heartbeat_job()
        with factory() as db:
            assert db.get(WorkerHeartbeat, "primary") is not None
    finally:
        engine.dispose()
    assert notes == ["WATCHDOG=1"]


def test_systemd_notify_is_a_noop_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert jobs._notify_systemd("WATCHDOG=1") is False


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
    reason="needs unix datagram sockets",
)
def test_systemd_notify_sends_one_datagram(monkeypatch, tmp_path):
    path = tmp_path / "notify.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        assert jobs._notify_systemd("WATCHDOG=1") is True
        assert server.recv(64) == b"WATCHDOG=1"


def test_memory_text_never_raises():
    text = jobs._memory_text()
    assert text == "memory=unavailable" or text.startswith("rss=")
