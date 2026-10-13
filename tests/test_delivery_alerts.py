from datetime import date, datetime, timedelta, timezone

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import DeliveryAlertEvent
from vitalis.application.delivery_alerts import (
    DeliveryObservation,
    evaluate_alert_candidates,
    evaluate_delivery_target,
    evaluate_stale_heartbeat,
)
from vitalis.domain import MetricSample


def test_delivery_targets_are_1030_and_2230_and_rejection_is_one_event():
    day = date(2026, 10, 13)
    before_morning = datetime(2026, 10, 13, 2, 29, tzinfo=timezone.utc)
    at_morning = datetime(2026, 10, 13, 2, 30, tzinfo=timezone.utc)

    assert evaluate_delivery_target(
        "alert-target-user",
        period="morning",
        target_date=day,
        status="pending",
        now=before_morning,
        timezone_name="Asia/Shanghai",
    ) is None
    missed = evaluate_delivery_target(
        "alert-target-user",
        period="morning",
        target_date=day,
        status="pending",
        now=at_morning,
        timezone_name="Asia/Shanghai",
    )
    assert missed is not None
    assert missed.reason == "delivery_target_missed"
    assert missed.event_key == "delivery:morning:2026-10-13:target-missed"

    rejected = evaluate_alert_candidates(
        "alert-target-user",
        now=at_morning,
        timezone_name="Asia/Shanghai",
        deliveries=(DeliveryObservation(
            period="morning",
            target_date=day,
            status="failed",
            last_error="transport_rejected",
            delivery_id="delivery-1",
        ),),
    )
    assert len(rejected) == 1
    assert rejected[0].reason == "provider_rejected"
    assert rejected[0].category == "provider"


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
