"""Offline A27 notification outbox invariants."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.application.jobs import create_analysis_job, drain_analysis_jobs
from vitalis.bootstrap import configure_analysis_jobs
from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import AnalysisRun, NotificationDelivery, User
from vitalis.intelligence import contracts


DAY = date(2026, 8, 29)
NOW = datetime(2026, 8, 29, 12, 0, 0)


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'outbox.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id="owner"))
        db.add(User(id="other"))
        db.add(AnalysisRun(
            id="run-owner",
            user_id="owner",
            target_date=DAY,
            status="SUCCEEDED",
            started_at=NOW,
            completed_at=NOW,
            intelligence_version=contracts.INTELLIGENCE_VERSION,
            decision_policy_version=contracts.DECISION_POLICY_VERSION,
            evidence_version=contracts.EVIDENCE_VERSION,
            config_digest=_current_analysis_config_digest(),
        ))
        db.flush()
        HealthRepository(db).enqueue_notification_delivery(
            "owner", "run-owner", "morning", DAY
        )
    return engine, factory


def test_concurrent_claim_has_one_owner_and_restart_is_uncertain(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        def claim(_):
            with factory.begin() as db:
                row = HealthRepository(db).claim_notification_delivery(
                    now=NOW, lease_seconds=10
                )
                return row.lease_token if row is not None else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(claim, range(2)))
        owned = [token for token in tokens if token is not None]
        assert len(owned) == 1

        with factory.begin() as db:
            row = HealthRepository(db).claim_notification_delivery(
                now=NOW + timedelta(seconds=11), lease_seconds=10
            )
            assert row is None
        with factory() as db:
            delivery = db.query(NotificationDelivery).one()
            assert delivery.status == "uncertain"
            assert delivery.last_error == "lease_expired_uncertain"
    finally:
        engine.dispose()


def test_delivery_is_user_scoped_and_error_text_is_redacted(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            repo = HealthRepository(db)
            assert repo.notification_delivery("missing", "other") is None
            row = repo.claim_notification_delivery(now=datetime.utcnow())
            assert row is not None
            token = row.lease_token
            delivery_id = row.id
        with factory.begin() as db:
            repo = HealthRepository(db)
            assert not repo.complete_notification_delivery(
                delivery_id, "wrong-user-token", "failed", error="secret-token"
            )
            assert repo.complete_notification_delivery(
                delivery_id, token, "failed", error="secret-token"
            )
        with factory() as db:
            row = db.get(NotificationDelivery, delivery_id)
            assert row.status == "failed"
            assert row.last_error == "delivery_failed"
            assert "secret" not in (row.last_error or "")
    finally:
        engine.dispose()


def test_same_day_period_reuses_intent_without_rearming_uncertain(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            row = db.query(NotificationDelivery).one()
            row.status = "uncertain"
        with factory.begin() as db:
            db.add(AnalysisRun(
                id="run-owner-2",
                user_id="owner",
                target_date=DAY,
                status="SUCCEEDED",
                started_at=NOW + timedelta(minutes=1),
                completed_at=NOW + timedelta(minutes=1),
                intelligence_version="test",
                decision_policy_version="test",
                evidence_version="test",
            ))
        with factory.begin() as db:
            row = HealthRepository(db).enqueue_notification_delivery(
                "owner", "run-owner-2", "morning", DAY
            )
            assert row.analysis_run_id == "run-owner"
            assert row.status == "uncertain"
    finally:
        engine.dispose()


def test_scheduler_delivers_exact_saved_run_without_filesystem_marker(
    tmp_path, monkeypatch,
):
    from vitalis.scheduler import jobs
    from vitalis.adapters.persistence import database
    from vitalis.config import settings
    from vitalis.intelligence import contracts
    from vitalis.adapters import daily_push

    engine, factory = _database(tmp_path)
    sent = []
    payload = {
        "date": DAY.isoformat(),
        "data_quality": {"status": "SUFFICIENT"},
        "report_context": {"training_history": {
            "status": "COMPLETE", "prior_7d_verified": True,
        }},
        "features": {"sleep": {"status": "AVAILABLE", "wake_time": "08:00:00"}},
    }
    try:
        with factory.begin() as db:
            db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                id="snapshot-owner",
                analysis_run_id="run-owner",
                user_id="owner",
                profile_type="daily",
                period_start=DAY,
                period_end=DAY,
                schema_version=contracts.DAILY_SCHEMA_VERSION,
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                payload=payload,
            ))
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(settings, "push_user", "owner")
        monkeypatch.setattr(settings, "pushplus_token", "offline-token")
        monkeypatch.setattr(daily_push, "local_today", lambda: DAY)
        monkeypatch.setattr(
            "vitalis.adapters.daily_push.local_day_utc_bounds",
            lambda _day: (datetime(2026, 8, 29), datetime(2100, 1, 1)),
        )

        class OfflinePush:
            def __init__(self, pushplus_token):
                assert pushplus_token == "offline-token"

            def push_daily_profile(self, user_id, profile, period):
                sent.append((user_id, profile, period))
                return {"_pushplus_handler": "ok"}

        monkeypatch.setattr(daily_push, "PushService", OfflinePush)
        assert jobs.drain_notification_deliveries() == 1
        assert sent == [("owner", payload, "morning")]
        with factory() as db:
            row = db.query(NotificationDelivery).one()
            assert row.status == "accepted"
        assert not list(tmp_path.glob("*.sent"))
    finally:
        engine.dispose()


def test_dispatcher_delivers_before_sync_invalidates_saved_snapshot(tmp_path, monkeypatch):
    from vitalis.adapters import daily_push
    from vitalis.adapters.persistence import database
    from vitalis.config import settings
    from vitalis.scheduler import jobs

    engine, factory = _database(tmp_path)
    phases = []
    sent = []
    payload = {
        "date": DAY.isoformat(),
        "data_quality": {"status": "SUFFICIENT"},
        "report_context": {"training_history": {
            "status": "COMPLETE", "prior_7d_verified": True,
        }},
        "features": {"sleep": {"status": "AVAILABLE", "wake_time": "08:00:00"}},
    }
    try:
        with factory.begin() as db:
            db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                id="dispatch-snapshot", analysis_run_id="run-owner", user_id="owner",
                profile_type="daily", period_start=DAY, period_end=DAY,
                schema_version=contracts.DAILY_SCHEMA_VERSION,
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION, payload=payload,
            ))
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(settings, "push_user", "owner")
        monkeypatch.setattr(settings, "pushplus_token", "offline-token")
        monkeypatch.setattr(daily_push, "local_today", lambda: DAY)
        monkeypatch.setattr(
            "vitalis.adapters.daily_push.local_day_utc_bounds",
            lambda _day: (datetime(2026, 8, 29), datetime(2100, 1, 1)),
        )

        class OfflinePush:
            def __init__(self, pushplus_token):
                assert pushplus_token == "offline-token"

            def push_daily_profile(self, user_id, profile, period):
                phases.append("delivery")
                sent.append((user_id, profile, period))
                return {"_pushplus_handler": "ok"}

        class SyncThatChangesInput:
            def __init__(self, **_kwargs):
                pass

            def drain_once(self):
                phases.append("sync")
                with factory.begin() as db:
                    db.get(User, "owner").analysis_input_revision += 1
                return None

        def drain_analysis(*, max_jobs):
            phases.append("analysis")
            return 0

        monkeypatch.setattr(daily_push, "PushService", OfflinePush)
        monkeypatch.setattr("vitalis.bootstrap.get_connector", lambda _source: object())
        monkeypatch.setattr(
            "vitalis.adapters.zepp.sync_coordinator.ZeppSyncCoordinator",
            SyncThatChangesInput,
        )
        monkeypatch.setattr("vitalis.application.jobs.drain_analysis_jobs", drain_analysis)

        assert jobs.dispatcher_job() == 1
        assert sent == [("owner", payload, "morning")]
        assert phases == ["analysis", "delivery", "sync"]
        with factory() as db:
            assert db.query(NotificationDelivery).one().status == "accepted"
            assert db.get(User, "owner").analysis_input_revision == 1
            assert HealthRepository(db).latest_analysis_snapshot("owner", "daily", DAY) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("period", ["weekly", "monthly"])
@pytest.mark.parametrize("enabled", [True, False])
def test_calendar_delivery_uses_period_snapshot_and_deduplicates(
    tmp_path, monkeypatch, period, enabled,
):
    from vitalis.adapters import daily_push
    from vitalis.adapters.persistence import database
    from vitalis.config import settings
    from vitalis.intelligence.report_periods import delivery_period_dates
    from tests.test_report_content import synthetic_period_fixture

    engine, factory = _database(tmp_path)
    run_id = f"run-{period}"
    start, target = delivery_period_dates(period, DAY)
    payload = synthetic_period_fixture(period)
    payload.update({
        "analysis_run_id": run_id,
        "user_id": "owner",
        "period_start": start.isoformat(),
        "period_end": target.isoformat(),
        "report_context": {
            **payload.get("report_context", {}),
            "period_mode": "calendar",
        },
    })
    sent = []

    class OfflinePush:
        def __init__(self, pushplus_token):
            assert pushplus_token == "offline-token"

        def push_weekly_profile(self, user_id, profile):
            sent.append((user_id, "weekly", profile["period_end"]))
            return {"_pushplus_handler": "ok"}

        def push_monthly_profile(self, user_id, profile):
            sent.append((user_id, "monthly", profile["period_end"]))
            return {"_pushplus_handler": "ok"}

    try:
        with factory.begin() as db:
            db.query(NotificationDelivery).delete()
            db.add(AnalysisRun(
                id=run_id,
                user_id="owner",
                target_date=DAY,
                status="SUCCEEDED",
                started_at=NOW,
                completed_at=NOW,
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                config_digest=_current_analysis_config_digest(),
            ))
            db.flush()
            db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                id=f"snapshot-{period}",
                analysis_run_id=run_id,
                user_id="owner",
                profile_type=period,
                period_start=start,
                period_end=target,
                schema_version=(
                    contracts.WEEKLY_SCHEMA_VERSION
                    if period == "weekly" else contracts.MONTHLY_SCHEMA_VERSION
                ),
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                payload=payload,
            ))
            repo = HealthRepository(db)
            first = repo.enqueue_notification_delivery("owner", run_id, period, target)
            second = repo.enqueue_notification_delivery("owner", run_id, period, target)
            assert first.id == second.id
            assert first.target_date == target
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(settings, "push_user", "owner")
        monkeypatch.setattr(settings, "pushplus_token", "offline-token")
        monkeypatch.setattr(settings, f"{period}_report_enabled", enabled)
        monkeypatch.setattr(daily_push, "PushService", OfflinePush)
        from vitalis.scheduler import jobs
        assert jobs.drain_notification_deliveries() == 1
        assert sent == ([("owner", period, target.isoformat())] if enabled else [])
        assert jobs.drain_notification_deliveries() == 0
        with factory() as db:
            row = db.query(NotificationDelivery).filter_by(period=period).one()
            assert row.status == ("accepted" if enabled else "deferred")
            assert row.last_error == (None if enabled else "delivery_disabled")
    finally:
        engine.dispose()


def test_late_calendar_analysis_repoints_existing_unsent_intent(tmp_path):
    from vitalis.intelligence.report_periods import delivery_period_dates

    engine, factory = _database(tmp_path)
    _, period_end = delivery_period_dates("weekly", DAY)
    try:
        with factory.begin() as db:
            db.add_all([
                AnalysisRun(
                    id="run-calendar-old",
                    user_id="owner",
                    target_date=DAY,
                    status="SUCCEEDED",
                    started_at=NOW,
                    completed_at=NOW,
                    intelligence_version=contracts.INTELLIGENCE_VERSION,
                    decision_policy_version=contracts.DECISION_POLICY_VERSION,
                    evidence_version=contracts.EVIDENCE_VERSION,
                    config_digest=_current_analysis_config_digest(),
                ),
                AnalysisRun(
                    id="run-calendar-new",
                    user_id="owner",
                    target_date=DAY,
                    status="SUCCEEDED",
                    started_at=NOW + timedelta(minutes=1),
                    completed_at=NOW + timedelta(minutes=1),
                    intelligence_version=contracts.INTELLIGENCE_VERSION,
                    decision_policy_version=contracts.DECISION_POLICY_VERSION,
                    evidence_version=contracts.EVIDENCE_VERSION,
                    config_digest=_current_analysis_config_digest(),
                ),
            ])
            db.flush()
            repo = HealthRepository(db)
            row = repo.enqueue_notification_delivery(
                "owner", "run-calendar-old", "weekly", period_end
            )
            assert row.status == "pending"
            assert repo.refresh_existing_calendar_notification_deliveries(
                "owner", "run-calendar-new", DAY
            ) == 1
            db.refresh(row)
            assert row.analysis_run_id == "run-calendar-new"
            assert row.target_date == period_end
            assert row.status == "pending"
    finally:
        engine.dispose()


@pytest.mark.parametrize("enqueue_new_run", [False, True])
def test_claimed_delivery_uses_latest_eligible_run_before_sending(
    tmp_path, monkeypatch, enqueue_new_run,
):
    from vitalis.adapters import daily_push, persistence
    from vitalis.adapters.persistence import database
    from vitalis.config import settings
    from vitalis.scheduler import jobs

    engine, factory = _database(tmp_path)
    base_payload = {
        "date": DAY.isoformat(),
        "data_quality": {"status": "SUFFICIENT"},
        "report_context": {"training_history": {
            "status": "COMPLETE", "prior_7d_verified": True,
        }},
        "features": {"sleep": {"status": "AVAILABLE", "wake_time": "08:00:00"}},
    }
    old_payload = {**base_payload, "report_marker": "old"}
    new_payload = {**base_payload, "report_marker": "new"}
    sent = []
    try:
        with factory.begin() as db:
            db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                id="snapshot-old", analysis_run_id="run-owner", user_id="owner",
                profile_type="daily", period_start=DAY, period_end=DAY,
                schema_version=contracts.DAILY_SCHEMA_VERSION,
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION, payload=old_payload,
            ))
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(settings, "push_user", "owner")
        monkeypatch.setattr(settings, "pushplus_token", "offline-token")
        monkeypatch.setattr(daily_push, "local_today", lambda: DAY)
        monkeypatch.setattr(
            "vitalis.adapters.daily_push.local_day_utc_bounds",
            lambda _day: (datetime(2026, 8, 29), datetime(2100, 1, 1)),
        )
        original_scope = persistence.session_scope
        scope_count = 0

        @contextmanager
        def interleaved_scope():
            nonlocal scope_count
            scope_count += 1
            if scope_count == 2:
                with factory.begin() as db:
                    db.add(AnalysisRun(
                        id="run-new", user_id="owner", target_date=DAY,
                        status="SUCCEEDED", started_at=NOW + timedelta(minutes=1),
                        completed_at=NOW + timedelta(minutes=1),
                        intelligence_version=contracts.INTELLIGENCE_VERSION,
                        decision_policy_version=contracts.DECISION_POLICY_VERSION,
                        evidence_version=contracts.EVIDENCE_VERSION,
                        config_digest=_current_analysis_config_digest(),
                    ))
                    db.flush()
                    db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                        id="snapshot-new", analysis_run_id="run-new", user_id="owner",
                        profile_type="daily", period_start=DAY, period_end=DAY,
                        schema_version=contracts.DAILY_SCHEMA_VERSION,
                        intelligence_version=contracts.INTELLIGENCE_VERSION,
                        decision_policy_version=contracts.DECISION_POLICY_VERSION,
                        evidence_version=contracts.EVIDENCE_VERSION, payload=new_payload,
                    ))
                    if enqueue_new_run:
                        HealthRepository(db).enqueue_notification_delivery(
                            "owner", "run-new", "morning", DAY
                        )
            with original_scope() as db:
                yield db

        monkeypatch.setattr(persistence, "session_scope", interleaved_scope)

        class OfflinePush:
            def __init__(self, pushplus_token):
                assert pushplus_token == "offline-token"

            def push_daily_profile(self, user_id, profile, period):
                sent.append((user_id, profile["report_marker"], period))
                return {"_pushplus_handler": "ok"}

        monkeypatch.setattr(daily_push, "PushService", OfflinePush)
        assert jobs.drain_notification_deliveries() == 1
        assert sent == [("owner", "new", "morning")]
        with factory() as db:
            row = db.query(NotificationDelivery).one()
            assert row.status == "accepted"
            assert row.analysis_run_id == "run-new"
    finally:
        engine.dispose()


def test_snapshot_preparation_requires_current_lease(tmp_path):
    from vitalis.adapters.persistence import database

    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                id="lease-snapshot", analysis_run_id="run-owner", user_id="owner",
                profile_type="daily", period_start=DAY, period_end=DAY,
                schema_version=contracts.DAILY_SCHEMA_VERSION,
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION, payload={"date": DAY.isoformat()},
            ))
            row = HealthRepository(db).claim_notification_delivery(now=datetime.utcnow())
            delivery_id, token = row.id, row.lease_token
        with factory.begin() as db:
            repo = HealthRepository(db)
            assert repo.notification_delivery_snapshot(
                delivery_id, "owner", "run-owner", "wrong-token"
            ) is None
            assert repo.notification_delivery_snapshot(
                delivery_id, "other", "run-owner", token
            ) is None
            assert repo.notification_delivery_snapshot(
                delivery_id, "owner", "run-owner", token
            ) is not None
        with factory.begin() as db:
            db.get(NotificationDelivery, delivery_id).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        with factory.begin() as db:
            assert HealthRepository(db).notification_delivery_snapshot(
                delivery_id, "owner", "run-owner", token
            ) is None
    finally:
        engine.dispose()


def test_snapshot_unavailable_rearms_if_new_run_commits_before_completion(tmp_path):
    from vitalis.adapters.persistence import database

    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            repo = HealthRepository(db)
            row = repo.claim_notification_delivery(now=datetime.utcnow())
            delivery_id, token = row.id, row.lease_token
        with factory.begin() as db:
            repo = HealthRepository(db)
            assert repo.notification_delivery_snapshot(
                delivery_id, "owner", "run-owner", token
            ) is None
        with factory.begin() as db:
            db.add(AnalysisRun(
                id="run-after-lookup", user_id="owner", target_date=DAY,
                status="SUCCEEDED", started_at=NOW + timedelta(minutes=1),
                completed_at=NOW + timedelta(minutes=1),
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                config_digest=_current_analysis_config_digest(),
            ))
            db.flush()
            db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                id="snapshot-after-lookup", analysis_run_id="run-after-lookup",
                user_id="owner", profile_type="daily", period_start=DAY,
                period_end=DAY, schema_version=contracts.DAILY_SCHEMA_VERSION,
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                payload={"date": DAY.isoformat()},
            ))
        with factory.begin() as db:
            assert HealthRepository(db).complete_notification_delivery(
                delivery_id, token, "deferred", error="snapshot_unavailable"
            )
        with factory() as db:
            row = db.get(NotificationDelivery, delivery_id)
            assert row.status == "pending"
            assert row.analysis_run_id == "run-after-lookup"
            assert row.lease_token is None
    finally:
        engine.dispose()


def test_unavailable_completion_locks_owner_before_delivery_update(tmp_path):
    engine, factory = _database(tmp_path)
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    try:
        with factory.begin() as db:
            row = HealthRepository(db).claim_notification_delivery(now=datetime.utcnow())
            delivery_id, token = row.id, row.lease_token
        sa_event.listen(engine, "before_cursor_execute", record_statement)
        try:
            with factory.begin() as db:
                assert HealthRepository(db).complete_notification_delivery(
                    delivery_id, token, "deferred", error="snapshot_unavailable"
                )
        finally:
            sa_event.remove(engine, "before_cursor_execute", record_statement)
        owner_lock = next(
            index for index, statement in enumerate(statements)
            if "update users" in statement
        )
        delivery_update = next(
            index for index, statement in enumerate(statements)
            if "update notification_deliveries" in statement
        )
        assert owner_lock < delivery_update
    finally:
        engine.dispose()


@pytest.mark.parametrize("reason", [
    "snapshot_unavailable", "sleep_incomplete", "stored_data_incomplete",
])
def test_manual_analysis_rearms_only_existing_unavailable_delivery(tmp_path, monkeypatch, reason):
    from vitalis.adapters.persistence import database
    from vitalis.bootstrap import get_intelligence_command

    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            repo = HealthRepository(db)
            row = repo.claim_notification_delivery(now=datetime.utcnow())
            assert row is not None
            assert repo.complete_notification_delivery(
                row.id, row.lease_token, "deferred", error=reason
            )
        monkeypatch.setattr(database, "SessionLocal", factory)
        result = get_intelligence_command().analyze("owner", DAY)
        with factory() as db:
            rows = db.query(NotificationDelivery).all()
            assert len(rows) == 1
            assert rows[0].status == "pending"
            assert rows[0].analysis_run_id == result.run.id
        with factory.begin() as db:
            claimed = HealthRepository(db).claim_notification_delivery(now=datetime.utcnow())
            assert claimed is not None and claimed.analysis_run_id == result.run.id
    finally:
        engine.dispose()


def test_http_5xx_marks_outbox_delivery_uncertain(tmp_path, monkeypatch):
    from vitalis.adapters import notifications
    from vitalis.adapters.persistence import database
    from vitalis.config import settings
    from vitalis.scheduler import jobs
    from vitalis.adapters import daily_push

    engine, factory = _database(tmp_path)
    payload = {
        "date": DAY.isoformat(),
        "data_quality": {"status": "SUFFICIENT"},
        "report_context": {"training_history": {
            "status": "COMPLETE", "prior_7d_verified": True,
        }},
        "features": {"sleep": {"status": "AVAILABLE", "wake_time": "08:00:00"}},
    }
    try:
        with factory.begin() as db:
            db.execute(database.Base.metadata.tables["analysis_snapshots"].insert().values(
                id="snapshot-owner-5xx",
                analysis_run_id="run-owner",
                user_id="owner",
                profile_type="daily",
                period_start=DAY,
                period_end=DAY,
                schema_version=contracts.DAILY_SCHEMA_VERSION,
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                payload=payload,
            ))
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(settings, "push_user", "owner")
        monkeypatch.setattr(settings, "pushplus_token", "offline-token")
        monkeypatch.setattr(daily_push, "local_today", lambda: DAY)
        monkeypatch.setattr(
            "vitalis.adapters.daily_push.local_day_utc_bounds",
            lambda _day: (datetime(2026, 8, 29), datetime(2100, 1, 1)),
        )

        class Client:
            def __init__(self, **kwargs):
                assert kwargs == {"timeout": 10.0, "trust_env": False}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, url, **kwargs):
                return httpx.Response(
                    500,
                    request=httpx.Request("POST", url),
                )

        monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)

        class TransportOnlyPush:
            def __init__(self, pushplus_token):
                self._service = notifications.PushService(pushplus_token=pushplus_token)

            def push_daily_profile(self, user_id, profile, period):
                return self._service.push(
                    notifications.PushMessage(
                        title="synthetic report", body="synthetic body", user_id=user_id,
                    )
                )

        monkeypatch.setattr(daily_push, "PushService", TransportOnlyPush)
        assert jobs.drain_notification_deliveries() == 1
        assert jobs.drain_notification_deliveries() == 0
        with factory() as db:
            row = db.query(NotificationDelivery).one()
            assert row.status == "uncertain"
            assert row.last_error == "transport_ambiguous"
        assert not list(tmp_path.glob("*.sent"))
    finally:
        engine.dispose()


def test_disabled_delivery_is_deferred_without_transport(tmp_path, monkeypatch):
    from vitalis.scheduler import jobs
    from vitalis.adapters.persistence import database
    from vitalis.config import settings

    engine, factory = _database(tmp_path)
    try:
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(settings, "push_user", "owner")
        monkeypatch.setattr(settings, "pushplus_token", "")
        assert jobs.drain_notification_deliveries() == 1
        with factory() as db:
            row = db.query(NotificationDelivery).one()
            assert row.status == "deferred"
            assert row.last_error == "delivery_disabled"
    finally:
        engine.dispose()


def test_intent_insert_rolls_back_with_analysis_transaction(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        try:
            with factory.begin() as db:
                HealthRepository(db).enqueue_notification_delivery(
                    "owner", "run-owner", "evening", DAY
                )
                raise RuntimeError("synthetic rollback")
        except RuntimeError:
            pass
        with factory() as db:
            assert db.query(NotificationDelivery).count() == 1
            assert db.query(NotificationDelivery).one().period == "morning"
    finally:
        engine.dispose()


def test_ambiguous_outcome_is_not_auto_retried(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            row = HealthRepository(db).claim_notification_delivery(now=datetime.utcnow())
            assert row is not None
            assert HealthRepository(db).complete_notification_delivery(
                row.id, row.lease_token, "uncertain", error="transport_ambiguous"
            )
        with factory.begin() as db:
            assert HealthRepository(db).claim_notification_delivery(
                now=datetime.utcnow() + timedelta(hours=1)
            ) is None
        with factory() as db:
            assert db.query(NotificationDelivery).one().status == "uncertain"
    finally:
        engine.dispose()


def test_scheduled_analysis_records_disabled_delivery_intent(monkeypatch):
    from vitalis.config import settings
    from vitalis.scheduler.jobs import drain_notification_deliveries

    user_id = "scheduled-disabled-intent-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
    monkeypatch.setattr(settings, "push_user", "")
    monkeypatch.setattr(settings, "pushplus_token", "")
    configure_analysis_jobs()
    job_id = create_analysis_job(
        user_id, DAY, "scheduled-disabled-intent-key", delivery_period="morning"
    )
    assert drain_analysis_jobs(max_jobs=1) == 1
    with session_scope() as db:
        rows = HealthRepository(db).notification_deliveries(user_id)
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].analysis_run_id
    assert drain_notification_deliveries(max_deliveries=1) == 1
    with session_scope() as db:
        row = HealthRepository(db).notification_deliveries(user_id)[0]
        assert row.status == "deferred"
    assert job_id


def test_sqlite_backup_preserves_pending_delivery(tmp_path):
    import sqlite3

    engine, factory = _database(tmp_path)
    source = tmp_path / "outbox.db"
    backup = tmp_path / "outbox-backup.db"
    try:
        engine.dispose()
        with sqlite3.connect(source) as source_db, sqlite3.connect(backup) as backup_db:
            source_db.backup(backup_db)
        with sqlite3.connect(backup) as db:
            status, period = db.execute(
                "SELECT status, period FROM notification_deliveries"
            ).fetchone()
            assert (status, period) == ("pending", "morning")
    finally:
        engine.dispose()


def test_accepted_delivery_polls_without_a_second_send(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            repo = HealthRepository(db)
            send = repo.claim_notification_delivery(now=NOW)
            assert send is not None
            assert repo.complete_notification_delivery(
                send.id, send.lease_token, "accepted",
                provider_id="short-code", provider_status="accepted",
                next_poll_at=NOW, now=NOW,
            )
        with factory.begin() as db:
            repo = HealthRepository(db)
            poll = repo.claim_notification_poll(now=NOW, max_attempts=3)
            assert poll is not None
            assert poll.lease_kind == "poll"
            assert poll.send_attempt_count == 1
            assert poll.poll_attempt_count == 1
            assert repo.complete_notification_delivery(
                poll.id, poll.lease_token, "delivered",
                provider_id="short-code", provider_status="2",
                poll_attempt_id=poll.poll_attempt_id,
                expected_provider_id="short-code", now=NOW,
            )
        with factory() as db:
            row = db.query(NotificationDelivery).one()
            assert row.status == "delivered"
            assert row.send_attempt_count == 1
            assert row.poll_attempt_count == 1
            assert row.provider_id == "short-code"
    finally:
        engine.dispose()


def test_stale_poll_attempt_cannot_complete_current_lease(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        with factory.begin() as db:
            repo = HealthRepository(db)
            send = repo.claim_notification_delivery(now=NOW)
            repo.complete_notification_delivery(
                send.id, send.lease_token, "accepted", provider_id="old-code",
                provider_status="accepted", next_poll_at=NOW, now=NOW,
            )
        with factory.begin() as db:
            repo = HealthRepository(db)
            poll = repo.claim_notification_poll(now=NOW)
            assert poll is not None
            assert not repo.complete_notification_delivery(
                poll.id, poll.lease_token, "delivered", provider_id="new-code",
                provider_status="2", poll_attempt_id="late-poll",
                expected_provider_id="old-code",
            )
            current = db.get(NotificationDelivery, poll.id)
            assert current.status == "running"
            assert current.provider_id == "old-code"
    finally:
        engine.dispose()


def test_explicit_failure_retries_with_backoff_until_max_attempts(tmp_path):
    engine, factory = _database(tmp_path)
    try:
        now = NOW
        for _attempt in range(3):
            with factory.begin() as db:
                repo = HealthRepository(db)
                row = repo.claim_notification_delivery(now=now, max_attempts=3)
                assert row is not None
                assert repo.complete_notification_delivery(
                    row.id, row.lease_token, "failed", error="transport_rejected",
                    next_attempt_at=now, now=now,
                )
            now += timedelta(seconds=1)
        with factory.begin() as db:
            assert HealthRepository(db).claim_notification_delivery(
                now=now, max_attempts=3
            ) is None
        with factory() as db:
            row = db.query(NotificationDelivery).one()
            assert row.attempt_count == 3
            assert row.status == "failed"
    finally:
        engine.dispose()
