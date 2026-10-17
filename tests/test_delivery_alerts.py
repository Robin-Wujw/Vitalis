from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import (
    AnalysisRun, DeliveryAlertEvent, NotificationDelivery, WorkerHeartbeat,
)
from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
from vitalis.application.delivery_alerts import (
    DeliveryObservation,
    evaluate_alert_candidates,
    evaluate_delivery_target,
    evaluate_stale_heartbeat,
)
from vitalis.config import settings
from vitalis.domain import MetricSample
from vitalis.intelligence import contracts
from vitalis.scheduler.alerts import check_delivery_health, delivery_targets

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_targets_follow_the_morning_window_and_the_evening_schedule(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    targets = delivery_targets(date(2026, 10, 13))
    assert targets["morning"].astimezone(SHANGHAI).strftime("%H:%M") == "13:30"
    assert targets["evening"].astimezone(SHANGHAI).strftime("%H:%M") == "22:30"


def test_target_miss_is_one_event_and_a_rejection_is_reported_once():
    day = date(2026, 10, 13)
    target = datetime(2026, 10, 13, 3, 30, tzinfo=timezone.utc)
    assert evaluate_delivery_target(
        "alert-target-user", period="morning", target_date=day, target_at=target,
        now=target - timedelta(minutes=1),
    ) is None
    missed = evaluate_delivery_target(
        "alert-target-user", period="morning", target_date=day, target_at=target,
        now=target, missing_cause="worker_not_running",
    )
    assert missed.event_key == "delivery:morning:2026-10-13:target-missed"
    assert missed.payload["cause"] == "worker_not_running"
    assert missed.payload["delivery_present"] is False

    rejected = evaluate_alert_candidates(
        "alert-target-user", now=target, local_day=day, targets={"morning": target},
        deliveries=(DeliveryObservation(
            period="morning", target_date=day, status="failed",
            last_error="transport_rejected", delivery_id="delivery-1",
        ),),
    )
    assert rejected == ()


def test_stale_heartbeat_is_strictly_older_than_thirty_minutes():
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    assert evaluate_stale_heartbeat(
        "heartbeat-user",
        last_seen_at=now - timedelta(minutes=30),
        now=now,
    ) is None
    candidate = evaluate_stale_heartbeat(
        "heartbeat-user",
        last_seen_at=now - timedelta(minutes=31),
        now=now,
    )
    assert candidate is not None
    assert candidate.reason == "worker_heartbeat_stale"
    assert candidate.payload["age_seconds"] == 1860.0


def _local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI).astimezone(timezone.utc)


def _run(db, run_id: str, user_id: str, day: date, status: str, *, at: datetime) -> None:
    db.add(AnalysisRun(
        id=run_id, user_id=user_id, target_date=day, status=status,
        started_at=at.replace(tzinfo=None), completed_at=at.replace(tzinfo=None),
        error="analysis_failed" if status == "FAILED" else None,
        intelligence_version=contracts.INTELLIGENCE_VERSION,
        decision_policy_version=contracts.DECISION_POLICY_VERSION,
        evidence_version=contracts.EVIDENCE_VERSION,
        config_digest=_current_analysis_config_digest(),
    ))


@pytest.fixture
def alert_owner(monkeypatch):
    with session_scope() as db:
        row = db.get(WorkerHeartbeat, "primary")
        previous = row.last_seen_at if row is not None else None

    def setup(user_id: str, day: date, *, heartbeat: datetime):
        monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
        monkeypatch.setattr(settings, "push_user", user_id)
        monkeypatch.setattr(settings, "pushplus_token", "offline-token")
        with session_scope() as db:
            HealthRepository(db).upsert_user(user_id)
            row = db.get(WorkerHeartbeat, "primary")
            if row is None:
                db.add(WorkerHeartbeat(name="primary", last_seen_at=heartbeat.replace(tzinfo=None)))
            else:
                row.last_seen_at = heartbeat.replace(tzinfo=None)

    yield setup
    # The shared test database keeps one worker heartbeat for every test.
    with session_scope() as db:
        row = db.get(WorkerHeartbeat, "primary")
        if previous is None and row is not None:
            db.delete(row)
        elif row is not None:
            row.last_seen_at = previous


def _alerts(user_id: str) -> list[DeliveryAlertEvent]:
    with session_scope() as db:
        return HealthRepository(db).delivery_alerts(user_id)


def test_stopped_worker_leaves_one_missed_record_and_one_alert_each(alert_owner, client):
    user_id, day = "alert-worker-stopped", date(2026, 10, 20)
    alert_owner(user_id, day, heartbeat=_local(day, 2, 10))

    first = check_delivery_health(now=_local(day, 13, 35))
    again = check_delivery_health(now=_local(day, 13, 45))
    assert first["new_alerts"] == 2 and again["new_alerts"] == 0
    assert {row.reason for row in _alerts(user_id)} == {"delivery_target_missed", "worker_heartbeat_stale"}

    deliveries = client.get("/api/deliveries", headers={"X-User-Id": user_id}).json()
    assert [(item["period"], item["status"], item["failure_code"]) for item in deliveries] == [
        ("morning", "missed", "worker_not_running"),
    ]

    # Recovery: the heartbeat moves and the evening report is accepted on time.
    with session_scope() as db:
        db.get(WorkerHeartbeat, "primary").last_seen_at = _local(day, 22, 39).replace(tzinfo=None)
        _run(db, "alert-worker-evening", user_id, day, "SUCCEEDED", at=_local(day, 21, 35))
        db.flush()
        db.add(NotificationDelivery(
            id="alert-worker-evening-delivery", user_id=user_id,
            analysis_run_id="alert-worker-evening", period="evening",
            target_date=day, status="accepted",
        ))
    assert check_delivery_health(now=_local(day, 22, 40))["new_alerts"] == 0


def test_failed_analysis_leaves_one_missed_record_and_alerts_once(alert_owner, client):
    user_id, day = "alert-analysis-failed", date(2026, 10, 21)
    alert_owner(user_id, day, heartbeat=_local(day, 13, 34))
    with session_scope() as db:
        _run(db, "alert-failed-run", user_id, day, "FAILED", at=_local(day, 9, 5))
        _run(
            db, "alert-old-failed-run", user_id, day - timedelta(days=3), "FAILED",
            at=_local(day - timedelta(days=3), 9, 5),
        )

    assert check_delivery_health(now=_local(day, 13, 35))["new_alerts"] == 2
    with session_scope() as db:
        db.get(WorkerHeartbeat, "primary").last_seen_at = _local(day, 13, 49).replace(tzinfo=None)
    assert check_delivery_health(now=_local(day, 13, 50))["new_alerts"] == 0
    reasons = sorted(row.reason for row in _alerts(user_id))
    assert reasons == ["analysis_failed", "delivery_target_missed"]
    deliveries = client.get("/api/deliveries", headers={"X-User-Id": user_id}).json()
    assert [(item["status"], item["failure_code"]) for item in deliveries] == [
        ("missed", "snapshot_unavailable"),
    ]


def test_provider_rejection_alerts_once_and_keeps_its_own_delivery_record(alert_owner, client):
    user_id, day = "alert-provider-rejected", date(2026, 10, 22)
    alert_owner(user_id, day, heartbeat=_local(day, 22, 39))
    with session_scope() as db:
        _run(db, "alert-rejected-run", user_id, day, "SUCCEEDED", at=_local(day, 21, 33))
        _run(db, "alert-morning-run", user_id, day, "SUCCEEDED", at=_local(day, 9, 3))
        db.flush()
        db.add(NotificationDelivery(
            id="alert-rejected-delivery", user_id=user_id, analysis_run_id="alert-rejected-run",
            period="evening", target_date=day, status="failed", last_error="transport_rejected",
            provider_status="rejected", attempt_count=3, send_attempt_count=3,
            updated_at=_local(day, 21, 36).replace(tzinfo=None),
        ))
        db.add(NotificationDelivery(
            id="alert-morning-delivery", user_id=user_id, analysis_run_id="alert-morning-run",
            period="morning", target_date=day, status="accepted",
        ))

    assert check_delivery_health(now=_local(day, 22, 40))["new_alerts"] == 1
    with session_scope() as db:
        db.get(WorkerHeartbeat, "primary").last_seen_at = _local(day, 22, 59).replace(tzinfo=None)
    assert check_delivery_health(now=_local(day, 23, 0))["new_alerts"] == 0
    assert [row.reason for row in _alerts(user_id)] == ["provider_rejected"]
    deliveries = client.get("/api/deliveries", headers={"X-User-Id": user_id}).json()
    assert {(item["period"], item["status"]) for item in deliveries} == {
        ("evening", "failed"), ("morning", "accepted"),
    }


def test_alert_repository_deduplicates_by_user_event_key_and_api_is_sanitized(client):
    user_id = "delivery-alert-api-user"
    observed = datetime(2026, 10, 13, 12, 0)
    with session_scope() as db:
        repository = HealthRepository(db)
        repository.upsert_user(user_id)
        first = repository.create_delivery_alert(
            user_id,
            event_key="delivery:morning:2026-10-13:target-missed",
            category="delivery",
            reason="delivery_target_missed",
            observed_at=observed,
            period="morning",
            target_date=date(2026, 10, 13),
            payload={"delivery_status": "pending", "secret": "must-not-leak"},
        )
        second = repository.create_delivery_alert(
            user_id,
            event_key="delivery:morning:2026-10-13:target-missed",
            category="delivery",
            reason="delivery_target_missed",
            observed_at=observed + timedelta(minutes=1),
            period="morning",
            target_date=date(2026, 10, 13),
        )
        assert first.id == second.id
        assert len(repository.delivery_alerts(user_id)) == 1
        assert repository.acknowledge_delivery_alert(user_id, first.id).status == "acknowledged"

    response = client.get("/api/delivery-alerts", headers={"X-User-Id": user_id})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["status"] == "acknowledged"
    assert body[0]["reason"] == "delivery_target_missed"
    assert body[0]["external_channel"] == "unconfigured"
    assert "payload" not in body[0]
    assert "must-not-leak" not in response.text


def test_data_status_reports_unknown_without_observations_and_actual_7d_coverage(client):
    empty_user = "freshness-empty-user"
    with session_scope() as db:
        repository = HealthRepository(db)
        repository.upsert_user(empty_user)
        repository.save_sync_stream_state(
            empty_user,
            "wellness/hrv_rmssd",
            fetch_status="success",
            parse_status="success",
            write_status="success",
            fetched_at=datetime.now(timezone.utc),
            parsed_at=datetime.now(timezone.utc),
            written_at=datetime.now(timezone.utc),
            raw_records=1,
            records_written=0,
        )

    empty = client.get("/api/data-status", headers={"X-User-Id": empty_user})
    assert empty.status_code == 200
    empty_stream = empty.json()["streams"][0]
    assert empty_stream["freshness"] == "unknown"
    assert empty_stream["age_seconds"] is None
    assert empty_stream["coverage_7d"]["observed_days"] is None
    assert empty_stream["coverage_7d"]["coverage"] is None

    user_id = "freshness-observed-user"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with session_scope() as db:
        repository = HealthRepository(db)
        repository.upsert_user(user_id)
        repository.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=now - timedelta(days=1),
                value=50,
                unit="ms",
                source_record_id="fresh-1",
            ),
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=now,
                value=52,
                unit="ms",
                source_record_id="fresh-2",
            ),
        ])
        repository.save_sync_stream_state(
            user_id,
            "wellness/hrv_rmssd",
            fetch_status="success",
            parse_status="success",
            write_status="success",
            fetched_at=now,
            parsed_at=now,
            written_at=now,
            raw_records=2,
            records_written=2,
        )

    observed = client.get("/api/data-status", headers={"X-User-Id": user_id})
    assert observed.status_code == 200
    stream = observed.json()["streams"][0]
    assert stream["freshness"] == "fresh"
    assert stream["expected_interval_seconds"] == 86400
    assert stream["coverage_7d"]["observed_days"] == 2
    assert stream["coverage_7d"]["coverage"] == 2 / 7


def test_requeued_analysis_after_input_change_is_not_an_alert():
    from datetime import date, datetime, timezone

    from vitalis.application.delivery_alerts import AnalysisFailureObservation, evaluate_analysis_failure

    now = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
    changed = AnalysisFailureObservation(run_id="r1", target_date=date(2026, 10, 14), failed_at=now, error="analysis_input_changed")
    limit = AnalysisFailureObservation(run_id="r2", target_date=date(2026, 10, 14), failed_at=now, error="analysis_attempt_limit")
    assert evaluate_analysis_failure("u", changed, now=now) is None
    assert evaluate_analysis_failure("u", limit, now=now).reason == "analysis_failed"
