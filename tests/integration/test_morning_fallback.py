from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.adapters import daily_push
from vitalis.adapters.persistence import init_db
from vitalis.adapters.persistence import database
from vitalis.adapters.persistence.models import (
    AnalysisRun,
    AnalysisSnapshot,
    NotificationDelivery,
    SyncAttempt,
    User,
)
from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
from vitalis.bootstrap import get_intelligence_query
from vitalis.config import settings
from vitalis.intelligence import contracts
from vitalis.scheduler import jobs


DAY = date(2026, 8, 29)
RUN_ID = "morning-fallback-run"


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'morning-fallback.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(settings, "push_user", "owner")
    monkeypatch.setattr(settings, "pushplus_token", "offline-token")
    monkeypatch.setattr("vitalis.time.local_today", lambda: DAY)
    yield factory
    engine.dispose()


def _payload(*, sleep_complete: bool, same_day_facts: bool, finished: bool | None = None) -> dict:
    stored = sleep_complete or finished is False
    sleep = {
        "status": "AVAILABLE" if stored else "PARTIAL",
        "duration_minutes": 420 if same_day_facts else None,
        "wake_time": "08:00:00" if stored else None,
        "finished": sleep_complete if finished is None else finished,
    }
    features = {"sleep": sleep}
    if same_day_facts:
        features["activity"] = {
            "status": "AVAILABLE",
            "steps": {"value": 3200, "unit": "steps"},
        }
    return {
        "analysis_run_id": RUN_ID,
        "user_id": "owner",
        "date": DAY.isoformat(),
        "data_quality": {"status": "PARTIAL"},
        "report_context": {
            "timezone": "Asia/Shanghai",
            "as_of": "2026-08-29T01:40:00Z",
            "training_history": {
                "status": "COMPLETE",
                "prior_7d_verified": True,
            },
        },
        "features": features,
        "decision": {"action": "TRAIN_NORMAL", "action_plan": {"title": "计划"}},
    }


def _seed(factory, payload: dict, error: str) -> None:
    with factory.begin() as db:
        db.add(User(id="owner"))
        db.add(AnalysisRun(
            id=RUN_ID,
            user_id="owner",
            target_date=DAY,
            status="SUCCEEDED",
            started_at=datetime(2026, 8, 29, 1, 30),
            completed_at=datetime(2026, 8, 29, 1, 40),
            intelligence_version=contracts.INTELLIGENCE_VERSION,
            decision_policy_version=contracts.DECISION_POLICY_VERSION,
            evidence_version=contracts.EVIDENCE_VERSION,
            config_digest=_current_analysis_config_digest(),
        ))
        db.flush()
        db.add(AnalysisSnapshot(
            id="morning-fallback-snapshot",
            analysis_run_id=RUN_ID,
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
        db.add(NotificationDelivery(
            id="morning-fallback-delivery",
            user_id="owner",
            analysis_run_id=RUN_ID,
            period="morning",
            target_date=DAY,
            status="deferred",
            last_error=error,
        ))


def _delivery(factory) -> NotificationDelivery:
    with factory() as db:
        return db.query(NotificationDelivery).one()


@pytest.mark.parametrize("hour,expected", [(0, 1), (4, 1), (5, 0)])
def test_previous_evening_rearms_only_before_its_deadline(isolated_database, monkeypatch, hour, expected):
    from zoneinfo import ZoneInfo

    _seed(isolated_database, _payload(sleep_complete=True, same_day_facts=True), "stored_data_incomplete")
    with isolated_database.begin() as db:
        db.query(NotificationDelivery).one().period = "evening"
    next_day = DAY + timedelta(days=1)
    now = datetime.combine(next_day, datetime.min.time(), ZoneInfo("Asia/Shanghai")) + timedelta(hours=hour)
    monkeypatch.setattr("vitalis.time.local_today", lambda: next_day)
    monkeypatch.setattr(jobs, "_utc_now", lambda: now.astimezone(UTC))

    assert jobs.reconcile_deferred_daily_reports() == expected
    row = _delivery(isolated_database)
    assert row.target_date == DAY
    assert row.status == ("pending" if expected else "deferred")


def test_waiting_inside_the_wake_window_queues_no_extra_sync(isolated_database, monkeypatch):
    _seed(
        isolated_database,
        _payload(sleep_complete=False, same_day_facts=True),
        "sleep_incomplete",
    )
    monkeypatch.setattr(
        jobs,
        "_utc_now",
        lambda: datetime(2026, 8, 29, 1, 45, tzinfo=UTC),
    )

    assert jobs.reconcile_deferred_daily_reports() == 0
    with isolated_database() as db:
        assert db.query(SyncAttempt).count() == 0
    row = _delivery(isolated_database)
    assert (row.status, row.last_error) == ("deferred", "sleep_incomplete")


def test_sleep_arrival_rearms_and_sends_full_morning_report(
    isolated_database, monkeypatch,
):
    _seed(
        isolated_database,
        _payload(sleep_complete=True, same_day_facts=True),
        "sleep_incomplete",
    )
    monkeypatch.setattr(
        jobs,
        "_utc_now",
        lambda: datetime(2026, 8, 29, 1, 45, tzinfo=UTC),
    )
    sent = []

    class OfflinePush:
        def __init__(self, pushplus_token, **kwargs):
            assert pushplus_token == "offline-token"

        def push_daily_profile(self, user_id, profile, period):
            sent.append((user_id, profile, period))
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push, "PushService", OfflinePush)
    monkeypatch.setattr(
        daily_push,
        "_delivery_now",
        lambda: datetime(2026, 8, 29, 1, 45, tzinfo=UTC),
    )
    monkeypatch.setattr(daily_push, "local_today", lambda: DAY)

    assert jobs.reconcile_deferred_daily_reports() == 1
    assert _delivery(isolated_database).status == "pending"
    assert jobs.drain_notification_deliveries() == 1
    assert len(sent) == 1
    assert sent[0][2] == "morning"
    assert sent[0][1]["delivery_metadata"]["facts_only"] is False
    assert _delivery(isolated_database).status == "accepted"


def test_fallback_after_the_wake_window_sends_facts_only(
    isolated_database, monkeypatch,
):
    _seed(
        isolated_database,
        _payload(sleep_complete=False, same_day_facts=True),
        "sleep_incomplete",
    )
    monkeypatch.setattr(
        jobs,
        "_utc_now",
        lambda: datetime(2026, 8, 29, 5, 5, tzinfo=UTC),
    )
    sent = []

    class OfflinePush:
        def __init__(self, pushplus_token, **kwargs):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append(profile)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push, "PushService", OfflinePush)
    monkeypatch.setattr(
        daily_push,
        "_delivery_now",
        lambda: datetime(2026, 8, 29, 5, 5, tzinfo=UTC),
    )
    monkeypatch.setattr(daily_push, "local_today", lambda: DAY)

    assert jobs.reconcile_deferred_daily_reports() == 1
    assert _delivery(isolated_database).status == "pending"
    assert jobs.drain_notification_deliveries() == 1
    assert sent[0]["delivery_metadata"]["facts_only"] is True
    assert sent[0]["delivery_metadata"]["coverage_reason"] == "sleep_incomplete"
    assert _delivery(isolated_database).status == "accepted"


def test_a_sleep_still_unfinished_at_one_pm_goes_out_as_facts_only(
    isolated_database, monkeypatch,
):
    _seed(
        isolated_database,
        _payload(sleep_complete=False, same_day_facts=True, finished=False),
        "sleep_incomplete",
    )
    sent = []

    class OfflinePush:
        def __init__(self, pushplus_token, **kwargs):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append(profile)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push, "PushService", OfflinePush)
    monkeypatch.setattr(daily_push, "local_today", lambda: DAY)
    for now in (datetime(2026, 8, 29, 4, 55, tzinfo=UTC), datetime(2026, 8, 29, 5, 5, tzinfo=UTC)):
        monkeypatch.setattr(jobs, "_utc_now", lambda now=now: now)
        monkeypatch.setattr(daily_push, "_delivery_now", lambda now=now: now)
        if now.hour == 4:
            # 12:55 in Shanghai: the stored sleep may still be cut short, keep waiting.
            assert jobs.reconcile_deferred_daily_reports() == 0
            assert _delivery(isolated_database).status == "deferred"
            continue
        assert jobs.reconcile_deferred_daily_reports() == 1
        assert jobs.drain_notification_deliveries() == 1
    assert len(sent) == 1
    assert sent[0]["delivery_metadata"]["facts_only"] is True
    assert sent[0]["delivery_metadata"]["coverage_reason"] == "sleep_unconfirmed"
    assert "decision" not in sent[0]
    assert _delivery(isolated_database).status == "accepted"


def test_fallback_without_same_day_facts_keeps_a_missed_record(
    isolated_database, monkeypatch,
):
    _seed(
        isolated_database,
        _payload(sleep_complete=False, same_day_facts=False),
        "sleep_incomplete",
    )
    monkeypatch.setattr(
        jobs,
        "_utc_now",
        lambda: datetime(2026, 8, 29, 5, 5, tzinfo=UTC),
    )
    monkeypatch.setattr(
        daily_push,
        "PushService",
        lambda **_: pytest.fail("no-facts fallback must not send"),
    )

    assert jobs.reconcile_deferred_daily_reports() == 1
    row = _delivery(isolated_database)
    assert row.status == "deferred"
    assert row.last_error == "morning_fallback_no_facts"
    assert jobs.drain_notification_deliveries() == 0


def test_delivery_query_exposes_only_whitelisted_failure_code(
    isolated_database,
):
    _seed(
        isolated_database,
        _payload(sleep_complete=False, same_day_facts=False),
        "morning_fallback_no_facts",
    )

    delivery = get_intelligence_query().deliveries("owner")[0]
    assert delivery["failure_code"] == "morning_fallback_no_facts"

    with isolated_database.begin() as db:
        db.query(NotificationDelivery).one().last_error = "private-detail"
    delivery = get_intelligence_query().deliveries("owner")[0]
    assert delivery["failure_code"] is None


def test_morning_deadline_closes_a_deferred_record(
    isolated_database, monkeypatch,
):
    _seed(
        isolated_database,
        _payload(sleep_complete=False, same_day_facts=True),
        "sleep_incomplete",
    )
    monkeypatch.setattr(
        jobs,
        "_utc_now",
        lambda: datetime(2026, 8, 29, 6, 35, tzinfo=UTC),
    )

    assert jobs.reconcile_deferred_daily_reports() == 1
    row = _delivery(isolated_database)
    assert row.status == "deferred"
    assert row.last_error == "stale_plan_expired"
    assert jobs.drain_notification_deliveries() == 0
