"""API 端到端测试：mock Zepp 下完整走通 连接 -> 同步 -> 查询 -> 分析。"""

import concurrent.futures
import hashlib
from datetime import date, datetime, timedelta, timezone
import importlib

import pytest
from sqlalchemy import delete, update
from starlette.requests import Request

from vitalis.adapters.persistence.models import ZeppDeviceLink

from vitalis.application.jobs import drain_analysis_jobs
from vitalis.config import settings
from vitalis.intelligence.contracts import ConfidenceBand, EventSeverity, HealthEvent
from vitalis.domain import (
    ActivityRecord,
    AuthToken,
    MetricSample,
    NormalizedDaily,
    SleepRecord,
    TrainingRecord,
    Workout,
    WorkoutType,
)
from vitalis.adapters.persistence import HealthRepository, session_scope


def _device_sample_id(
    timestamp: int, ordinal: int = 0, nonce: str = "test", sequence: int = 0
) -> str:
    return f"z2:{timestamp}:{ordinal}:{nonce}:{sequence}"


def _drain_sync_worker():
    """Drive durable sync work explicitly; HTTP only enqueues the job."""
    from vitalis.scheduler.jobs import dispatcher_job

    for _ in range(256):
        if not dispatcher_job():
            break


def _queue_sync_job(
    client,
    user_id,
    *,
    days=14,
    from_date=None,
    to_date=None,
    key=None,
    dispatch=True,
):
    body = {"days": days}
    if from_date is not None:
        body["from"] = from_date.isoformat()
    if to_date is not None:
        body["to"] = to_date.isoformat()
    headers = {
        "X-User-Id": user_id,
        "Idempotency-Key": key or f"api-sync-request-{user_id}-{days}",
    }
    response = client.post("/api/sync-jobs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert response.json()["status_url"] == f"/api/jobs/{job_id}"
    if dispatch:
        _drain_sync_worker()
    return response


def _run_analysis_job(client, user_id, day=None, *, key="first"):
    headers = {"X-User-Id": user_id, "Idempotency-Key": f"api-analysis-{user_id}-{key}"}
    request = {"day": day.isoformat()} if day is not None else {}
    queued = client.post("/api/analysis-runs", json=request, headers=headers)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    assert queued.json()["status_url"] == f"/api/jobs/{job_id}"
    assert client.get(f"/api/jobs/{job_id}", headers=headers).json()["status"] == "queued"
    assert drain_analysis_jobs(max_jobs=1) == 1
    job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert job["status"] == "succeeded", job
    report = client.get(
        "/api/reports/daily", params={"day": day.isoformat()} if day else {}, headers=headers,
    )
    assert report.status_code == 200, report.text
    assert report.json()["analysis_run_id"] == job["analysis_run_id"]
    return job, report.json()


def test_connect_and_sync(client):
    resp = _queue_sync_job(client, "001", dispatch=False)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert client.get(
        f"/api/jobs/{body['job_id']}", headers={"X-User-Id": "001"}
    ).json()["kind"] == "sync"


def test_daily_profile_after_sync(client):
    queued = _queue_sync_job(client, "001", dispatch=False)
    job_id = queued.json()["job_id"]
    assert client.get("/api/reports/daily", headers={"X-User-Id": "001"}).status_code == 404
    _drain_sync_worker()
    assert client.get(
        f"/api/jobs/{job_id}", headers={"X-User-Id": "001"}
    ).status_code == 200
    _run_analysis_job(client, "001")
    resp = client.get("/api/reports/daily", headers={"X-User-Id": "001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == "14.0"
    assert body["intelligence_version"] == "14.0"
    assert body["decision_policy_version"] == "9.0"
    assert body["evidence_version"] == "2026-09a"
    assert body["features"]["overnight_vitals"]["status"] == "INSUFFICIENT_DATA"
    assert body["analysis_run_id"]
    assert body["decision"]["action"] in {
        "TRAIN_HARD", "TRAIN_NORMAL", "TRAIN_LIGHT", "RECOVERY", "REST", "INSUFFICIENT_DATA"
    }
    assert body["decision"]["action_label"]
    assert body["decision"]["confidence_label"]
    assert body["decision"]["action_plan"]["goal"] == "HEALTH_FIRST_CONCURRENT"
    assert "score" not in body["decision"]


def test_morning_briefing_projects_persisted_daily_snapshot(client):
    user_id = "morning-briefing-user"
    headers = {"X-User-Id": user_id}
    analyzed = _queue_sync_job(client, user_id)
    assert analyzed.status_code == 202
    job, daily = _run_analysis_job(client, user_id)
    response = client.get("/api/reports/morning", headers=headers)

    assert response.status_code == 200
    briefing = response.json()
    assert briefing["analysis_run_id"] == daily["analysis_run_id"]
    assert briefing["date"] == daily["date"]
    assert briefing["decision_action"] == daily["decision"]["action"]
    assert briefing["action_plan"] == daily["decision"]["action_plan"]
    assert briefing["evidence"] == daily["decision"]["evidence"]
    assert briefing["analysis_run_id"] == job["analysis_run_id"]
    assert briefing == client.get("/api/reports/morning", headers=headers).json()
    assert briefing["schema_version"] == "4.0"
    assert len(briefing["sections"]) >= 3
    assert "feedback_prompt" not in briefing
    assert client.get(
        "/api/reports/morning",
        headers={"X-User-Id": "other-morning-briefing-user"},
    ).status_code == 404


def test_decision_explanation_projects_persisted_snapshot(client):
    user_id = "explanation-projection-user"
    headers = {"X-User-Id": user_id}
    _queue_sync_job(client, user_id)
    _run_analysis_job(client, user_id)
    daily = client.get("/api/reports/daily", headers=headers).json()
    response = client.get("/api/intelligence/explain", headers=headers)

    assert response.status_code == 200
    explanation = response.json()
    assert explanation["schema_version"] == "1.0"
    assert explanation["user_id"] == user_id
    assert explanation["date"] == daily["date"]
    assert explanation["snapshot"] == {
        "analysis_run_id": daily["analysis_run_id"],
        "generated_at": daily["generated_at"],
        "schema_version": daily["schema_version"],
        "intelligence_version": daily["intelligence_version"],
        "decision_policy_version": daily["decision_policy_version"],
        "evidence_version": daily["evidence_version"],
        "data_quality": daily["data_quality"],
    }
    assert explanation["action"] == daily["decision"]
    assert explanation["facts"] == daily["decision"]["evidence"]["facts"]
    assert explanation["gates"] == daily["decision"]["evidence"]["gates"]
    assert explanation["evidence_refs"] == daily["evidence_refs"]
    assert "metadata" not in explanation
    assert client.get(
        "/api/intelligence/explain", headers={"X-User-Id": "other-explanation-user"}
    ).status_code == 404


def test_decision_explanation_requires_rerun_after_preferences_change(client):
    user_id = "explanation-immutable-user"
    headers = {"X-User-Id": user_id}
    _queue_sync_job(client, user_id)
    _run_analysis_job(client, user_id)
    before = client.get("/api/intelligence/explain", headers=headers).json()

    changed = client.request(
        "PATCH",
        "/api/intelligence/training-preferences",
        headers=headers,
        json={
            "pain_or_injury_status": "PRESENT",
            "pain_or_injury_notes": "分析后记录的疼痛",
        },
    )
    stale = client.get("/api/intelligence/explain", headers=headers)

    assert changed.status_code == 200
    assert stale.status_code == 404
    _run_analysis_job(client, user_id, key="after-preference")
    refreshed = client.get("/api/intelligence/explain", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["snapshot"]["analysis_run_id"] != before["snapshot"]["analysis_run_id"]


def test_daily_profile_second_user_abstains(client):
    """A user without data gets an explicit abstention, not a fallback score."""
    _run_analysis_job(client, "999")
    resp = client.get("/api/reports/daily", headers={"X-User-Id": "999"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_quality"]["status"] == "INSUFFICIENT"
    assert body["data_quality"]["status_label"] == "数据不足"
    assert body["data_quality"]["missing_required_signal_labels"] == ["睡眠时长", "心率变异性"]
    assert body["decision"]["action"] == "INSUFFICIENT_DATA"
    assert body["decision"]["confidence"] == "NONE"
    assert body["decision"]["action_label"] == "数据不足，暂不建议"


def test_obsolete_analysis_routes_are_removed(client):
    assert client.get("/api/health/today").status_code == 404
    assert client.post("/api/analyze", json={}).status_code == 404
    assert client.post("/api/health/sync", headers={"X-User-Id": "removed-sync"}).status_code == 404
    assert client.post("/api/intelligence/analyze", headers={"X-User-Id": "removed-analysis"}).status_code == 404


def test_get_intelligence_without_snapshot_is_read_only(client):
    headers = {"X-User-Id": "no-analysis-snapshot"}
    assert client.get("/api/reports/daily", headers=headers).status_code == 404
    assert client.get("/api/reports/weekly", headers=headers).status_code == 404
    assert client.get("/api/reports/monthly", headers=headers).status_code == 404
    assert client.get("/api/intelligence/explain", headers=headers).status_code == 404
    assert client.get("/api/intelligence/training-responses", headers=headers).status_code == 404
    assert client.get("/api/intelligence/personal-model", headers=headers).status_code == 404
    assert client.get("/api/intelligence/personal-associations", headers=headers).status_code == 404
    with session_scope() as db:
        from vitalis.adapters.persistence.models import AnalysisRun

        assert db.query(AnalysisRun).filter_by(user_id="no-analysis-snapshot").count() == 0


def test_daily_profile_api_runs_device_baseline_to_decision(client):
    user_id = "intelligence-api"
    target = date(2026, 8, 27)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        for offset in range(21, -1, -1):
            day = target - timedelta(days=offset)
            current = offset == 0
            repo.save_daily(NormalizedDaily(
                user_id=user_id,
                date=day,
                sleep=SleepRecord(
                    user_id=user_id,
                    date=day,
                    sleep_duration=360 if current else 450 + offset % 3,
                ),
                activity=ActivityRecord(
                    user_id=user_id,
                    date=day,
                    resting_hr=64 if current else 56 + offset % 2,
                ),
                training=TrainingRecord(
                    user_id=user_id,
                    date=day,
                    workout_count=1,
                    total_duration=30,
                    total_load=30 + offset % 3,
                ),
            ))
            repo.save_metric_samples([MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
                value=40 if current else 50 + offset % 3,
                unit="ms",
                source_scope="device",
                device_id="helio-test",
            )])

    job, _ = _run_analysis_job(client, user_id, target)
    response = client.get(
        "/api/reports/daily", params={"day": target.isoformat()},
        headers={"X-User-Id": user_id},
    )
    assert response.json()["analysis_run_id"] == job["analysis_run_id"]
    assert response.status_code == 200
    payload = response.json()
    assert payload["data_quality"]["status"] == "SUFFICIENT"
    assert payload["features"]["hrv"]["preferred_device_id"] == "helio-test"
    assert payload["features"]["hrv"]["deviation"]["direction"] == "below"
    assert payload["decision"]["action"] == "REST"
    assert payload["decision"]["action_label"] == "休息"
    assert payload["decision"]["confidence_label"] in {"中等", "较高"}
    assert payload["decision"]["action_plan"]["primary_session"]["title"] == "完全休息"
    assert payload["decision"]["rule_ids"] == ["DECISION.MULTISIGNAL_SUPPRESSION_REST"]


def test_unknown_source(client):
    resp = client.post(
        "/api/connect/nonexistent",
        json={"sync_history": False},
        headers={"X-User-Id": "001"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_root_lists_sources(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "zepp" in resp.json()["available_sources"]


def test_user_scoped_endpoint_requires_bearer_identity(client):
    response = client.get("/api/reports/daily")
    assert response.status_code == 401


def test_intelligence_v3_routes_and_fact_inference_action_contract(client):
    user_id = "intelligence-v2"
    _queue_sync_job(client, user_id)
    headers = {"X-User-Id": user_id}
    _run_analysis_job(client, user_id)

    weekly = client.get("/api/reports/weekly", headers=headers)
    assert weekly.status_code == 200
    assert set(weekly.json()) >= {"facts", "inferences", "actions"}

    monthly = client.get("/api/reports/monthly", headers=headers)
    assert monthly.status_code == 200
    assert set(monthly.json()) >= {"facts", "inferences", "actions"}
    assert monthly.json()["period_start"] != weekly.json()["period_start"]

    trends = client.get("/api/intelligence/trends", headers=headers)
    assert trends.status_code == 200
    assert set(trends.json()) >= {"user_id", "date", "trends"}

    events = client.get("/api/intelligence/events", headers=headers)
    assert events.status_code == 200
    assert set(events.json()) >= {"period_start", "period_end", "events"}

    explanation = client.get("/api/intelligence/explain", headers=headers)
    assert explanation.status_code == 200
    assert set(explanation.json()) >= {"facts", "inferences", "action"}

    context = client.get("/api/intelligence/context", headers=headers)
    assert context.status_code == 200
    assert set(context.json()) >= {"current", "recent", "trend", "personal"}
    assert "daily" not in context.json()
    assert "weekly" not in context.json()

    responses = client.get("/api/intelligence/training-responses", headers=headers)
    assert responses.status_code == 200
    assert set(responses.json()) >= {"analysis_run_id", "responses"}

    personal = client.get("/api/intelligence/personal-model", headers=headers)
    assert personal.status_code == 200
    assert set(personal.json()) >= {
        "baselines", "long_term_trends", "training_response_patterns", "personal_associations"
    }

    associations = client.get("/api/intelligence/personal-associations", headers=headers)
    assert associations.status_code == 200
    assert set(associations.json()) >= {"analysis_run_id", "associations", "limitations"}

    timeline = client.get("/api/intelligence/timeline", headers=headers)
    assert timeline.status_code == 200
    assert set(timeline.json()) == {"user_id", "period_start", "period_end", "items"}


def test_feedback_api_is_scoped_and_validated(client):
    headers = {"X-User-Id": "feedback-api"}
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("feedback-api")
        from vitalis.domain import Workout, WorkoutType

        repo.save_workout(Workout(
            user_id="feedback-api",
            workout_id="feedback-api-workout",
            type=WorkoutType.RUNNING,
            duration=30,
        ))
    response = client.post(
        "/api/feedback",
        headers=headers,
        json={
            "date": "2026-08-28",
            "workout_source": "zepp",
            "workout_id": "feedback-api-workout",
            "session_rpe": 7,
            "physical_fatigue": 3,
            "notes": "训练按计划完成",
        },
    )
    assert response.status_code == 201
    assert response.json()["session_rpe"] == 7

    listing = client.get(
        "/api/feedback?start=2026-08-28&end=2026-08-28",
        headers=headers,
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    assert client.get(
        "/api/feedback?start=2026-08-28&end=2026-08-28",
        headers={"X-User-Id": "other-feedback-api"},
    ).json() == []

    invalid = client.post(
        "/api/feedback", headers=headers, json={"notes": "   "}
    )
    assert invalid.status_code == 422


def test_recommendation_completion_api_is_explicit_and_user_scoped(client):
    user_id = "recommendation-api"
    headers = {"X-User-Id": user_id}
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        from vitalis.domain import Workout, WorkoutType

        repo.save_workout(Workout(
            user_id=user_id,
            workout_id="recommendation-api-workout",
            type=WorkoutType.STRENGTH,
            duration=40,
        ))
    _, daily = _run_analysis_job(client, user_id, date(2026, 8, 28))
    recommendation_id = daily["decision"]["recommendation_id"]
    assert recommendation_id

    linked = client.post(
        f"/api/intelligence/recommendations/{recommendation_id}/complete",
        headers=headers,
        json={
            "workout_source": "zepp",
            "workout_id": "recommendation-api-workout",
        },
    )
    assert linked.status_code == 200
    assert linked.json()["completion_status"] == "COMPLETED"
    assert client.get(
        f"/api/intelligence/recommendations/{recommendation_id}",
        headers={"X-User-Id": "recommendation-api-other"},
    ).status_code == 404


def test_removed_daily_profile_route_is_not_kept_as_compatibility_alias(client):
    assert client.get(
        "/api/intelligence/daily-profile", headers={"X-User-Id": "001"}
    ).status_code == 404


def test_strength_exercise_confirmation_is_user_scoped(client):
    user_id = "strength-api-owner"
    workout_id = "strength-api-workout"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id=workout_id,
            type=WorkoutType.STRENGTH,
            training_family="strength",
            duration=45,
        ))

    response = client.post(
        f"/api/intelligence/workouts/{workout_id}/strength-exercises?source=zepp",
        headers={"X-User-Id": user_id},
        json={
            "session_focus": "PUSH",
            "exercises": [{
                "exercise_name": "卧推",
                "sets": 4,
                "repetitions": 8,
                "weight_kg": 60,
            }],
        },
    )

    assert response.status_code == 201
    exercise = response.json()[0]
    assert exercise["movement_pattern"] == "horizontal_push"
    assert exercise["muscle_group_labels"] == ["胸部", "肱三头肌", "肩部"]
    assert exercise["source"] == "user_confirmed"
    assert exercise["repetitions"] == 8
    for invalid in ("8", "8 次", "6–8 次"):
        assert client.post(
            f"/api/intelligence/workouts/{workout_id}/strength-exercises?source=zepp",
            headers={"X-User-Id": user_id},
            json={"exercises": [{"exercise_name": "卧推", "repetitions": invalid}]},
        ).status_code == 422
    assert client.post(
        f"/api/intelligence/workouts/{workout_id}/strength-exercises?source=zepp",
        headers={"X-User-Id": "strength-api-other"},
        json={
            "session_focus": "PUSH",
            "exercises": [{"exercise_name": "卧推"}],
        },
    ).status_code == 422


def test_training_preferences_are_health_first_and_user_scoped(client):
    response = client.request(
        "PUT",
        "/api/intelligence/training-preferences",
        headers={"X-User-Id": "training-preferences-owner"},
        json={
            "weekly_running_target": 3,
            "weekly_strength_target": 3,
            "rotation_policy": "ALTERNATE",
            "treadmill_available": False,
            "bad_weather_running_policy": "STRENGTH",
            "available_weekdays": [1, 2, 4, 5, 6],
            "max_session_minutes": 75,
            "running_experience": "INTERMEDIATE",
            "strength_experience": "INTERMEDIATE",
            "equipment": ["杠铃", "哑铃"],
            "pain_or_injury_status": "NONE",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["primary_goal"] == "HEALTH"
    assert payload["running_required"] is True
    assert payload["strength_required"] is True
    assert payload["rotation_policy"] == "ALTERNATE"
    assert payload["treadmill_available"] is False
    assert payload["bad_weather_running_policy"] == "STRENGTH"
    assert payload["available_weekdays"] == [1, 2, 4, 5, 6]
    assert client.get(
        "/api/intelligence/training-preferences",
        headers={"X-User-Id": "training-preferences-owner"},
    ).json()["equipment"] == ["杠铃", "哑铃"]
    other = client.get(
        "/api/intelligence/training-preferences",
        headers={"X-User-Id": "training-preferences-other"},
    ).json()
    assert other["equipment"] == []
    assert other["pain_or_injury_status"] == "UNKNOWN"
    assert other["rotation_policy"] == "BALANCE"
    assert other["bad_weather_running_policy"] == "DEFER"


def test_training_preference_patch_validates_merged_state_and_nulls(client):
    headers = {"X-User-Id": "training-patch-user"}
    created = client.request(
        "PUT",
        "/api/intelligence/training-preferences",
        headers=headers,
        json={
            "weekly_running_target": 3,
            "weekly_strength_target": 3,
            "rotation_policy": "BALANCE",
            "treadmill_available": False,
            "bad_weather_running_policy": "DEFER",
            "available_weekdays": [],
            "max_session_minutes": 60,
            "equipment": [],
            "pain_or_injury_status": "PRESENT",
            "pain_or_injury_notes": "膝部疼痛",
        },
    )
    assert created.status_code == 200

    idempotent = client.request(
        "PATCH",
        "/api/intelligence/training-preferences",
        headers=headers,
        json={"pain_or_injury_status": "PRESENT"},
    )
    assert idempotent.status_code == 200
    assert idempotent.json()["pain_or_injury_notes"] == "膝部疼痛"

    invalid_clear = client.request(
        "PATCH",
        "/api/intelligence/training-preferences",
        headers=headers,
        json={"pain_or_injury_notes": None},
    )
    assert invalid_clear.status_code == 422

    invalid_required_null = client.request(
        "PATCH",
        "/api/intelligence/training-preferences",
        headers=headers,
        json={"weekly_running_target": None},
    )
    assert invalid_required_null.status_code == 422

    nullable_clear = client.request(
        "PATCH",
        "/api/intelligence/training-preferences",
        headers=headers,
        json={"max_session_minutes": None},
    )
    assert nullable_clear.status_code == 200
    assert nullable_clear.json()["max_session_minutes"] is None


def test_health_event_acknowledgement_api_is_user_scoped(client):
    event = HealthEvent(
        id="api-event-ack",
        type="TRAINING_GAP",
        type_label="训练连续中断",
        severity=EventSeverity.INFO,
        severity_label="提示",
        start_date=date(2026, 8, 22),
        end_date=date(2026, 8, 28),
        duration_days=7,
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
        summary="测试事件",
    )
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("event-api-owner")
        repo.save_health_event("event-api-owner", event)

    denied = client.post(
        "/api/intelligence/events/api-event-ack/acknowledge",
        headers={"X-User-Id": "event-api-other"},
    )
    assert denied.status_code == 404
    accepted = client.post(
        "/api/intelligence/events/api-event-ack/acknowledge",
        headers={"X-User-Id": "event-api-owner"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["event"]["acknowledged"] is True


def test_removed_browser_get_connect_zepp_alias(client):
    response = client.get(
        "/api/connect/zepp?user=browser-user",
        headers={"X-User-Id": "browser-user"},
    )
    assert response.status_code == 404


async def test_app_lifespan_checks_database_without_starting_scheduler(monkeypatch):
    app_module = importlib.import_module("vitalis.entrypoints.api.app")
    scheduler_module = importlib.import_module("vitalis.scheduler")
    calls = []
    monkeypatch.setattr(app_module, "check_schema", lambda: calls.append("checked"))
    monkeypatch.setattr(
        scheduler_module, "start_scheduler",
        lambda: calls.append("scheduler started"),
    )

    candidate = app_module.create_app()
    async with candidate.router.lifespan_context(candidate):
        assert calls == ["checked"]
    assert calls == ["checked"]


def test_public_base_url_prefers_https_configuration(monkeypatch):
    from vitalis.entrypoints.api.routes.connect import _public_base_url

    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "method": "GET",
            "path": "/api/connect/zepp/scan",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8000")],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 12345),
        }
    )
    monkeypatch.setattr(settings, "public_url", "https://health.example.com")

    assert _public_base_url(request) == "https://health.example.com"


def test_region_probe_timeout_is_structured(monkeypatch):
    from vitalis.adapters.zepp import ZeppAuthError, ZeppConnector, auth_parser
    from vitalis.adapters.zepp import client as zepp_client

    class Client:
        def __init__(self, *_args, region_host, **_kwargs):
            self.region_host = region_host.replace("https://", "").rstrip("/")

        @staticmethod
        def verify():
            raise ZeppAuthError("offline", kind="network")

    monkeypatch.setattr(
        auth_parser,
        "preferred_region_hosts",
        lambda *_args: ["https://api-mifitcn.zepp.com"] * 7,
    )
    monkeypatch.setattr(zepp_client, "ZeppAPIClient", Client)
    monkeypatch.setattr(
        concurrent.futures,
        "as_completed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            concurrent.futures.TimeoutError()
        ),
    )

    with pytest.raises(ZeppAuthError) as raised:
        ZeppConnector(mock=False).probe_region_hosts(
            "vendor-user", "token", None, None
        )

    assert raised.value.kind == "timeout"


def test_region_probe_closes_every_temporary_client(monkeypatch):
    from vitalis.adapters import zepp as zepp_module
    from vitalis.adapters.zepp import ZeppAuthError, ZeppConnector, auth_parser

    created = []
    closed = []

    class Client:
        def __init__(self, *_args, region_host, **_kwargs):
            self.region_host = region_host.replace("https://", "").rstrip("/")
            created.append(self)

        @staticmethod
        def verify():
            raise ZeppAuthError("offline", kind="network")

        def close(self):
            closed.append(self)

    hosts = [
        "https://api-mifitcn.zepp.com",
        "https://api-mifit.zepp.com",
        "https://api-mifitde.zepp.com",
    ]
    monkeypatch.setattr(auth_parser, "preferred_region_hosts", lambda *_args: hosts)
    monkeypatch.setattr(zepp_module, "ZeppAPIClient", Client)

    with pytest.raises(ZeppAuthError) as raised:
        ZeppConnector(mock=False).probe_region_hosts(
            "vendor-user", "token", None, None
        )

    assert raised.value.kind == "vendor_response"
    assert created
    assert set(closed) == set(created)


# ---- 新 API：健康查询 / 同步 / 聚合 ----

def _queue_sync_attempt(client, user_id, *, days=2):
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        db.flush()
        repo.save_token(AuthToken(
            user_id=user_id, source="zepp", access_token="test-token",
            source_user_id=f"vendor-{user_id}",
        ))
        repo.create_browser_link(hashlib.sha256(user_id.encode()).hexdigest(), user_id)
    headers = {"X-User-Id": user_id, "Idempotency-Key": f"manual-sync-{user_id}"}
    response = client.post("/api/sync-jobs", json={"days": days}, headers=headers)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert response.json()["status_url"] == f"/api/jobs/{job_id}"
    assert client.get(f"/api/jobs/{job_id}", headers=headers).json()["attempt"]["status"] == "queued"
    return job_id


def _dispatch_single_heart_chunk(job_id, *, error=None):
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

    # Manifest planning has separate coverage; leave one real chunk for worker execution.
    with session_scope() as db:
        for chunk in HealthRepository(db).sync_chunks(job_id)[1:]:
            chunk.status = "succeeded"

    class HeartClient:
        def fetch_heart_rate(self, *_args):
            if error is not None:
                raise error
            return {"data": {"items": []}}

    return ZeppSyncCoordinator(connector=HeartClient()).drain_once(attempt_id=job_id)


def test_health_sync_job_dispatches_outside_http(client):
    user_id = "sync-user"
    job_id = _queue_sync_attempt(client, user_id, days=3)
    before = client.get("/api/data-status", headers={"X-User-Id": user_id}).json()
    assert before["latest_attempt"]["status"] == "queued"
    assert before["streams"] == []
    report = _dispatch_single_heart_chunk(job_id)
    assert report.success is True
    status = client.get(f"/api/jobs/{job_id}", headers={"X-User-Id": user_id}).json()
    assert status["kind"] == "sync"
    assert status["attempt"]["status"] == "succeeded"
    assert status["progress"]["completed_count"] == status["attempt"]["chunk_count"]
    diagnostic = client.get("/api/data-status", headers={"X-User-Id": user_id}).json()
    assert diagnostic["streams"][0]["stream"] == "heart_rate/minute_endpoint"
    assert diagnostic["streams"][0]["fetch"]["status"] == "success"


def test_health_sync_transient_zepp_failure_keeps_browser_link_connected(client):
    from vitalis.adapters.zepp import ZeppAuthError

    user_id = "sync-transient-user"
    job_id = _queue_sync_attempt(client, user_id)
    report = _dispatch_single_heart_chunk(job_id, error=ZeppAuthError("offline", kind="timeout"))
    assert report.progress["status"] == "retry_wait"
    job = client.get(f"/api/jobs/{job_id}", headers={"X-User-Id": user_id}).json()
    assert job["attempt"]["status"] == "retry_wait"
    assert job["attempt"]["next_retry_at"] is not None
    assert "offline" not in str(job)
    diagnostic = client.get("/api/data-status", headers={"X-User-Id": user_id}).json()
    heart = next(item for item in diagnostic["streams"] if item["stream"] == "heart_rate/minute_endpoint")
    assert heart["fetch"]["status"] == "failed"
    assert heart["error_kind"] == "timeout"
    link = client.get("/api/connect/zepp/token", headers={"X-User-Id": user_id}).json()
    assert link["connection_status"] == "connected"
    assert link["needs_login"] is False


def test_health_sync_real_auth_failure_marks_browser_link_for_login(client):
    from vitalis.adapters.zepp import ZeppAuthError

    user_id = "sync-reauth-user"
    job_id = _queue_sync_attempt(client, user_id)
    report = _dispatch_single_heart_chunk(job_id, error=ZeppAuthError("expired", needs_reauth=True))
    assert report.progress["status"] == "needs_reauth"
    status = client.get(f"/api/jobs/{job_id}", headers={"X-User-Id": user_id}).json()
    assert status["attempt"]["status"] == "needs_reauth"
    diagnostic = client.get("/api/data-status", headers={"X-User-Id": user_id}).json()
    heart = next(item for item in diagnostic["streams"] if item["stream"] == "heart_rate/minute_endpoint")
    assert heart["error_kind"] == "auth"
    link = client.get("/api/connect/zepp/token", headers={"X-User-Id": user_id}).json()
    assert link["connection_status"] == "needs_login"
    assert link["needs_login"] is True


def test_health_token_status_authorized(client):
    """GET /health/token-status 已授权用户。"""
    # 先扫码授权保存 mock token
    au = client.post("/api/connect/zepp/authorize", headers={"X-User-Id": "tok-user"}).json()
    client.get("/api/connect/zepp/callback", params={"code": "mock-tok", "state": au["state"]})
    resp = client.get("/api/health/token-status", headers={"X-User-Id": "tok-user"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized"] is True
    assert body["valid"] is True  # mock 模式下始终有效


def test_health_token_status_is_stored_only_and_does_not_decrypt_vendor(client, monkeypatch):
    from vitalis.adapters import credentials

    user_id = "token-status-transient"
    code = client.post(
        "/api/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": user_id},
    ).json()["pairing_code"]
    client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-status","apptoken":"saved-token"}'},
    )

    def fail_decrypt(_value):
        raise AssertionError("token-status must not decrypt vendor credentials")

    monkeypatch.setattr(credentials, "decrypt_token", fail_decrypt)
    response = client.get(
        "/api/health/token-status", headers={"X-User-Id": user_id}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["retryable"] is False
    assert body["error_kind"] is None
    assert body["vendor_user_id"] == "vendor-status"
    assert "尚未执行在线验证" in body["detail"]
    assert body["connection_status"] == "connected"
    assert body["needs_login"] is False


def test_health_token_status_unauthorized(client):
    """GET /health/token-status 未授权用户。"""
    resp = client.get("/api/health/token-status", headers={"X-User-Id": "nope"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized"] is False


def test_health_range(client):
    """GET /health/range 多级聚合查询。"""
    _queue_sync_job(client, "001")
    resp = client.get(
        "/api/health/range?from=2024-01-01&to=2024-01-07&granularity=1d",
        headers={"X-User-Id": "001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "1d"
    assert isinstance(body["blocks"], list)


def test_health_range_rejects_over_2years(client):
    """GET /health/range 跨度超过 730 天应拒绝。"""
    resp = client.get(
        "/api/health/range?from=2020-01-01&to=2024-01-01&granularity=30d",
        headers={"X-User-Id": "001"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "bad_request"
    assert resp.json()["request_id"] == resp.headers["x-request-id"]


# ---- 扫码授权流程（mock 模式） ----

def test_authorize_returns_scan_url(client):
    resp = client.post("/api/connect/zepp/authorize", headers={"X-User-Id": "001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scan_required"
    assert body["authorize_url"].startswith("http")
    assert body["state"]  # 一次性 state


def test_callback_saves_token_and_syncs(client):
    # 1. 发起扫码拿到 state
    au = client.post("/api/connect/zepp/authorize", headers={"X-User-Id": "001"}).json()
    state = au["state"]
    # 2. 模拟 Zepp 授权回调（mock 模式下任意 code 换 token）
    cb = client.get(
        "/api/connect/zepp/callback",
        params={"code": "mock-code-abc12345", "state": state},
        headers={"X-User-Id": "001"},
    )
    assert cb.status_code == 200
    body = cb.json()
    assert body["status"] == "authorized"
    assert body["token_saved"] is True
    assert body["source_user_id"] == "mock-user-001"
    assert body["sync"]["attempt_id"]
    assert body["sync"]["attempt_status"] == "queued"

    # 3. token 状态已持久化
    tok = client.get("/api/connect/zepp/token", headers={"X-User-Id": "001"})
    assert tok.json()["authorized"] is True
    assert tok.json()["source_user_id"] == "mock-user-001"


def test_oauth_success_html_does_not_link_to_removed_scan_alias(client):
    state = client.post(
        "/api/connect/zepp/authorize", headers={"X-User-Id": "oauth-html-user"},
    ).json()["state"]
    response = client.get(
        "/api/connect/zepp/callback",
        params={"code": "mock-html", "state": state},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    assert "同步任务已入队" in response.text
    assert "scan?user=" not in response.text


def test_callback_rejects_reused_state(client):
    au = client.post("/api/connect/zepp/authorize", headers={"X-User-Id": "002"}).json()
    state = au["state"]
    ok = client.get("/api/connect/zepp/callback", params={"code": "c1", "state": state}).json()
    assert ok["status"] == "authorized"
    # 同一 state 再次使用应失败
    again = client.get("/api/connect/zepp/callback", params={"code": "c2", "state": state})
    assert again.status_code == 400


def test_mock_callback_succeeds_without_operator_key_and_keeps_token_opaque(
    client, monkeypatch
):
    from vitalis.adapters.persistence.models import AuthToken as StoredAuthToken

    user_id = "mock-blank-key-callback"
    monkeypatch.setattr(settings, "zepp_mock", True)
    monkeypatch.setattr(settings, "token_encryption_key", "")
    with session_scope() as db:
        HealthRepository(db).delete_for_user(user_id)

    authorized = client.post(
        "/api/connect/zepp/authorize", headers={"X-User-Id": user_id}
    ).json()
    callback = client.get(
        "/api/connect/zepp/callback",
        params={"code": "mock-blank-key", "state": authorized["state"]},
    )

    assert callback.status_code == 200, callback.text
    assert callback.json()["token_saved"] is True
    with session_scope() as db:
        row = db.query(StoredAuthToken).filter_by(user_id=user_id).one()
        assert row.access_token.startswith("mock-fernet:")
        assert "mock-access-" not in row.access_token


def test_callback_maps_identity_conflict_to_http_409(client):
    from vitalis.entrypoints.api.routes import connect
    from vitalis.adapters.zepp import ZeppAuthError

    state = "identity-conflict-callback-state"
    html_state = "identity-conflict-callback-html-state"
    with session_scope() as db:
        HealthRepository(db).save_oauth_state(
            state, "identity-conflict-callback-user"
        )
        HealthRepository(db).save_oauth_state(
            html_state, "identity-conflict-callback-user"
        )

    class ConflictingConnector:
        source = "zepp"
        mock = True

        @staticmethod
        def exchange_and_save(*_args, **_kwargs):
            raise ZeppAuthError(
                "private-vendor-token: 该 Zepp 账号已绑定到其他本地用户",
                kind="identity_conflict",
            )

    from vitalis.entrypoints.api.app import app

    app.dependency_overrides[connect._connector] = lambda: ConflictingConnector()
    try:
        response = client.get(
            "/api/connect/zepp/callback",
            params={"code": "mock-code", "state": state},
            headers={"Accept": "application/json"},
        )
        html_response = client.get(
            "/api/connect/zepp/callback",
            params={"code": "mock-code", "state": html_state},
            headers={"Accept": "text/html"},
        )
    finally:
        app.dependency_overrides.pop(connect._connector, None)

    assert response.status_code == 409
    assert response.json()["message"] == "Request conflicts with existing state"
    assert html_response.status_code == 409
    assert html_response.headers["content-type"].startswith("application/json")
    assert html_response.json()["message"] == response.json()["message"]
    assert "private-vendor-token" not in response.text + html_response.text


def test_zepp_import_page_renders_executable_javascript(client):
    response = client.get("/api/connect/zepp/import")
    assert response.status_code == 200
    assert "async function doImport(){" in response.text
    assert "doImport(){{" not in response.text
    assert "body{font-family" in response.text
    assert "d.message||'请稍后重试'" in response.text


def test_token_status_when_not_authorized(client):
    resp = client.get("/api/connect/zepp/token", headers={"X-User-Id": "099"})
    assert resp.json()["authorized"] is False


# ---- 网页扫码页 + 二维码 ----

def test_scan_page_renders_html(client):
    resp = client.post(
        "/api/connect/zepp/scan", headers={"X-User-Id": "007"}
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "连接 Zepp 健康数据" in body
    assert "/api/connect/zepp/qrcode.png?state=" in body
    assert "mockBtn" in body  # mock 模式显示模拟授权按钮


def test_qrcode_png_generated(client):
    # 先创建 state（用 scan 页拿到 state，从 img src 提取）
    page = client.post(
        "/api/connect/zepp/scan", headers={"X-User-Id": "007"}
    ).text
    import re

    state = re.search(r"qrcode\.png\?state=([a-zA-Z0-9_-]+)", page).group(1)
    resp = client.get(f"/api/connect/zepp/qrcode.png?state={state}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG 魔数


def test_qrcode_png_rejects_unknown_state(client):
    resp = client.get("/api/connect/zepp/qrcode.png?state=not-exist-state")
    assert resp.status_code == 404


def test_scan_page_full_flow(client):
    """网页扫码完整链路：页面 state -> 回调授权 -> token 生效 -> 同步完成。"""
    import re

    page = client.post(
        "/api/connect/zepp/scan", headers={"X-User-Id": "008"}
    ).text
    state = re.search(r"qrcode\.png\?state=([a-zA-Z0-9_-]+)", page).group(1)
    qr = client.get(f"/api/connect/zepp/qrcode.png?state={state}")
    assert qr.status_code == 200

    cb = client.get(
        "/api/connect/zepp/callback",
        params={"code": "mock-scan-001", "state": state},
        headers={"X-User-Id": "008"},
    )
    assert cb.status_code == 200
    assert cb.json()["status"] == "authorized"

    tok = client.get("/api/connect/zepp/token", headers={"X-User-Id": "008"})
    assert tok.json()["authorized"] is True
    assert tok.json()["expired"] is False


# ---- 云端配对（浏览器书签 / 扩展共用） ----


def test_manual_token_import_rejects_cross_user_zepp_identity(
    client, monkeypatch
):
    from vitalis.entrypoints.api.routes import connect
    from vitalis.adapters.zepp import ZeppConnector
    from vitalis.adapters.zepp.client import ZeppAPIClient

    connector = ZeppConnector(mock=False)
    monkeypatch.setattr(connector, "authenticate", lambda: None)
    monkeypatch.setattr(connect, "_connector", lambda: connector)
    monkeypatch.setattr(ZeppAPIClient, "verify", lambda _self: None)
    for user_id in ("identity-owner", "identity-contender"):
        with session_scope() as db:
            HealthRepository(db).delete_for_user(user_id)

    payload = {
        "user_id": "vendor-api-shared",
        "app_token": "owner-token",
        "sync_history": False,
    }
    owner = client.post(
        "/api/connect/zepp/token",
        headers={"X-User-Id": "identity-owner"},
        json=payload,
    )
    contender = client.post(
        "/api/connect/zepp/token",
        headers={"X-User-Id": "identity-contender"},
        json={**payload, "app_token": "contender-token"},
    )

    assert owner.status_code == 200
    assert contender.status_code == 409
    assert contender.json()["message"] == "Request conflicts with existing state"


def test_initial_pairing_reports_identity_conflict_without_creating_link(
    client, monkeypatch
):
    from vitalis.entrypoints.api.routes import zepp_pairing
    from vitalis.adapters.zepp import ZeppConnector
    from vitalis.domain import AuthToken

    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("pairing-identity-owner")
        repo.delete_for_user("pairing-identity-contender")
        repo.save_token(
            AuthToken(
                user_id="pairing-identity-owner",
                source="zepp",
                access_token="owner-token",
                source_user_id="vendor-pairing-shared",
            )
        )

    connector = ZeppConnector(mock=False)
    monkeypatch.setattr(zepp_pairing, "get_connector", lambda _source: connector)
    code = client.post(
        "/api/connect/zepp/pair",
        headers={"X-User-Id": "pairing-identity-contender"},
    ).json()["pairing_code"]

    response = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={
            "cookie": '{"userid":"vendor-pairing-shared","apptoken":"other-token"}'
        },
    )

    assert response.status_code == 409
    assert response.json()["message"] == "Request conflicts with existing state"
    with session_scope() as db:
        repo = HealthRepository(db)
        assert repo.latest_browser_link("pairing-identity-contender") is None
        assert repo.pairing_session(code).status == "failed"


def test_link_refresh_identity_conflict_keeps_current_binding(
    client, monkeypatch
):
    from vitalis.entrypoints.api.routes import zepp_pairing
    from vitalis.adapters.zepp import ZeppConnector
    from vitalis.domain import AuthToken

    link_token = "l" * 48
    link_digest = hashlib.sha256(link_token.encode()).hexdigest()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("refresh-owner")
        repo.delete_for_user("refresh-contender")
        repo.save_token(
            AuthToken(
                user_id="refresh-owner",
                source="zepp",
                access_token="owner-token",
                source_user_id="vendor-refresh-owner",
            )
        )
        repo.save_token(
            AuthToken(
                user_id="refresh-contender",
                source="zepp",
                access_token="current-token",
                source_user_id="vendor-refresh-current",
            )
        )
        repo.create_browser_link(link_digest, "refresh-contender")

    connector = ZeppConnector(mock=False)
    monkeypatch.setattr(zepp_pairing, "get_connector", lambda _source: connector)
    response = client.post(
        "/api/connect/zepp/link/credentials",
        headers={"Authorization": f"Bearer {link_token}"},
        json={
            "cookie": '{"userid":"vendor-refresh-owner","apptoken":"new-token"}'
        },
    )

    assert response.status_code == 409
    with session_scope() as db:
        repo = HealthRepository(db)
        current = repo.get_token("refresh-contender", "zepp")
        link = repo.browser_link(link_digest)
        assert current is not None
        assert current.source_user_id == "vendor-refresh-current"
        assert current.access_token == "current-token"
        assert link is not None and link.status == "connected"


def test_pairing_credentials_reject_untrusted_browser_origins(client):
    user_id = "pair-origin-user"
    code = client.post(
        "/api/connect/zepp/pair",
        headers={"X-User-Id": user_id},
    ).json()["pairing_code"]
    path = f"/api/connect/zepp/pair/{code}/credentials"
    payload = {"cookie": '{"userid":"vendor-origin","apptoken":"synthetic-token"}'}

    rejected = client.post(path, json=payload, headers={"Origin": "https://evil.example"})
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "forbidden"
    assert client.get(f"/api/connect/zepp/pair/{code}", headers={"X-User-Id": user_id}).json()["status"] == "waiting"

    allowed = client.post(path, json=payload, headers={"Origin": "https://watchface.zepp.com"})
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "connected"


def test_raw_pairing_credentials_reject_wrong_origin_before_cookie_read(client):
    code = client.post(
        "/api/connect/zepp/pair",
        headers={"X-User-Id": "pair-raw-origin-user"},
    ).json()["pairing_code"]
    response = client.post(
        f"/api/connect/zepp/pair/{code}/credentials/raw",
        content="synthetic cookie",
        headers={"Origin": "null", "Content-Type": "text/plain"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_raw_pairing_body_limit_preserves_unused_code(client):
    user_id = "pair-raw-limit-user"
    code = client.post(
        "/api/connect/zepp/pair", headers={"X-User-Id": user_id},
    ).json()["pairing_code"]
    response = client.post(
        f"/api/connect/zepp/pair/{code}/credentials/raw",
        content="x" * (64 * 1024 + 1),
        headers={"Origin": "https://watchface.zepp.com", "Content-Type": "text/plain"},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert client.get(f"/api/connect/zepp/pair/{code}", headers={"X-User-Id": user_id}).json()["status"] == "waiting"


def test_pairing_rate_limit_survives_failure_and_resets(client, monkeypatch):
    monkeypatch.setattr(settings, "pairing_rate_limit_attempts", 2)
    monkeypatch.setattr(settings, "pairing_rate_limit_window_seconds", 60)
    user_id = "pair-rate-limit-user"
    code = client.post(
        "/api/connect/zepp/pair", headers={"X-User-Id": user_id},
    ).json()["pairing_code"]
    path = f"/api/connect/zepp/pair/{code}/credentials"
    for _ in range(2):
        assert client.post(path, json={"cookie": "invalid cookie"}).status_code == 400
    blocked = client.post(
        path, json={"cookie": '{"userid":"vendor-rate","apptoken":"token"}'},
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) > 0
    with session_scope() as db:
        row = HealthRepository(db).pairing_session(code)
        assert row.rate_window_attempts == 2
        row.rate_window_started_at = datetime.utcnow() - timedelta(seconds=61)
    retry = client.post(
        path, json={"cookie": '{"userid":"vendor-rate","apptoken":"token"}'},
    )
    assert retry.status_code == 200


def test_pairing_deletion_during_vendor_verify_cannot_recreate_owner(client, monkeypatch):
    from vitalis.adapters.zepp import ZeppConnector
    from vitalis.domain import AuthToken
    from vitalis.entrypoints.api.routes import zepp_pairing
    from vitalis.adapters.persistence.models import User as UserRow

    user_id = "pair-probe-deleted-user"
    code = client.post(
        "/api/connect/zepp/pair", headers={"X-User-Id": user_id},
    ).json()["pairing_code"]
    connector = ZeppConnector(mock=False)
    monkeypatch.setattr(zepp_pairing, "get_connector", lambda _source: connector)

    def delete_during_verify(local_user, _credentials, **_kwargs):
        with session_scope() as db:
            HealthRepository(db).delete_for_user(local_user)
        return AuthToken(
            user_id=local_user,
            source="zepp",
            access_token="synthetic-token",
            source_user_id="vendor-probe-deleted",
            region_host="api-mifitcn.zepp.com",
        )

    monkeypatch.setattr(connector, "verify_credentials", delete_during_verify)
    result = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-probe-deleted","apptoken":"synthetic-token"}'},
    )
    assert result.status_code == 409
    with session_scope() as db:
        repo = HealthRepository(db)
        assert db.get(UserRow, user_id) is None
        assert repo.latest_browser_link(user_id) is None
        assert repo.pairing_session(code) is None


def test_manual_token_import_deletion_during_vendor_verify_cannot_recreate_owner(
    client, monkeypatch
):
    from vitalis.entrypoints.api.routes import connect
    from vitalis.adapters.zepp import ZeppConnector
    from vitalis.domain import AuthToken
    from vitalis.adapters.persistence.models import User as UserRow

    user_id = "manual-import-deleted-owner"
    with session_scope() as db:
        HealthRepository(db).delete_for_user(user_id)

    connector = ZeppConnector(mock=False)
    monkeypatch.setattr(connect, "_connector", lambda: connector)

    def verify_and_delete(local_user, _credentials, **_kwargs):
        with session_scope() as db:
            HealthRepository(db).delete_for_user(local_user)
        return AuthToken(
            user_id=local_user,
            source="zepp",
            access_token="synthetic-manual-token",
            source_user_id="vendor-manual-deleted",
            region_host="api-mifitcn.zepp.com",
        )

    monkeypatch.setattr(connector, "verify_credentials", verify_and_delete)
    response = client.post(
        "/api/connect/zepp/token",
        headers={"X-User-Id": user_id},
        json={
            "user_id": "vendor-manual-deleted",
            "app_token": "synthetic-manual-token",
            "sync_history": False,
        },
    )

    assert response.status_code == 409
    with session_scope() as db:
        repo = HealthRepository(db)
        assert db.get(UserRow, user_id) is None
        assert repo.get_token(user_id, "zepp") is None


def test_manual_token_import_revoke_during_vendor_verify_cannot_reactivate_source(
    client, monkeypatch
):
    from vitalis.entrypoints.api.routes import connect
    from vitalis.adapters.zepp import ZeppConnector
    from vitalis.domain import AuthToken

    user_id = "manual-import-revoked-source"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.save_token(AuthToken(
            user_id=user_id,
            source="zepp",
            access_token="old-token",
            source_user_id="vendor-manual-revoked",
        ))

    connector = ZeppConnector(mock=False)
    monkeypatch.setattr(connect, "_connector", lambda: connector)

    def verify_and_revoke(local_user, _credentials, **_kwargs):
        with session_scope() as db:
            assert HealthRepository(db).revoke_source_account(
                local_user, "zepp", reason="synthetic revoke"
            )
        return AuthToken(
            user_id=local_user,
            source="zepp",
            access_token="new-token-after-revoke",
            source_user_id="vendor-manual-revoked",
            region_host="api-mifitcn.zepp.com",
        )

    monkeypatch.setattr(connector, "verify_credentials", verify_and_revoke)
    response = client.post(
        "/api/connect/zepp/token",
        headers={"X-User-Id": user_id},
        json={
            "user_id": "vendor-manual-revoked",
            "app_token": "new-token-after-revoke",
            "sync_history": False,
        },
    )

    assert response.status_code == 409
    with session_scope() as db:
        repo = HealthRepository(db)
        account = repo.source_account_status(user_id, "zepp")
        assert account is not None and account["status"] == "revoked"
        assert repo.get_token(user_id, "zepp") is None


def test_cloud_pairing_one_time_flow(client):
    created = client.post(
        "/api/connect/zepp/pair?sync_days=3",
        headers={"X-User-Id": "pair-user"},
    )
    assert created.status_code == 200
    code = created.json()["pairing_code"]
    cookie = '{"token_info":{"userid":"vendor-42","apptoken":"secret-token"}}'

    submitted = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": cookie},
    )
    assert submitted.status_code == 200
    submitted_body = submitted.json()
    assert submitted_body["status"] == "connected"
    link_token = submitted_body["browser_link_token"]
    assert len(link_token) >= 32
    with session_scope() as db:
        link = HealthRepository(db).latest_browser_link("pair-user")
        assert link is not None
        assert link.token_digest == hashlib.sha256(link_token.encode()).hexdigest()
        assert link.token_digest != link_token

    status = client.get(
        f"/api/connect/zepp/pair/{code}",
        headers={"X-User-Id": "pair-user"},
    ).json()
    assert status["status"] == "connected"
    assert "同步" in status["message"]

    reused = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": cookie},
    )
    assert reused.status_code == 409


def test_revoke_source_account_revokes_pairings_and_blocks_submission(client):
    user_id = "revoke-pairing-owner"
    first = client.post(
        "/api/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": user_id},
    )
    first_code = first.json()["pairing_code"]
    connected = client.post(
        f"/api/connect/zepp/pair/{first_code}/credentials",
        json={"cookie": '{"userid":"vendor-revoke","apptoken":"token-a"}'},
    )
    assert connected.status_code == 200

    pending = client.post(
        "/api/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": user_id},
    )
    pending_code = pending.json()["pairing_code"]
    revoked = client.post(
        "/api/sources/zepp/revoke",
        headers={"X-User-Id": user_id},
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    for code in (first_code, pending_code):
        status = client.get(
            f"/api/connect/zepp/pair/{code}",
            headers={"X-User-Id": user_id},
        )
        assert status.status_code == 200
        assert status.json()["status"] == "revoked"
    rejected = client.post(
        f"/api/connect/zepp/pair/{pending_code}/credentials",
        json={"cookie": '{"userid":"vendor-revoke","apptoken":"token-b"}'},
    )
    assert rejected.status_code == 409


def test_browser_link_renews_and_reports_disconnect(client):
    code = client.post(
        "/api/connect/zepp/pair?sync_days=3",
        headers={"X-User-Id": "renew-user"},
    ).json()["pairing_code"]
    paired = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-renew","apptoken":"first-token"}'},
    ).json()
    auth = {"Authorization": f"Bearer {paired['browser_link_token']}"}

    renewed = client.post(
        "/api/connect/zepp/link/credentials",
        headers=auth,
        json={"cookie": '{"userid":"vendor-renew","apptoken":"second-token"}'},
    )
    assert renewed.status_code == 200
    assert renewed.json()["status"] == "connected"

    disconnected = client.post(
        "/api/connect/zepp/link/disconnected",
        headers=auth,
        json={"reason": "browser session ended"},
    )
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "needs_login"

    status = client.get(
        "/api/connect/zepp/token",
        headers={"X-User-Id": "renew-user"},
    ).json()
    assert status["authorized"] is True
    assert status["connection_status"] == "needs_login"
    assert status["needs_login"] is True
    assert status["connection_message"] == "browser session ended"


def test_browser_link_server_validation_recovers_false_disconnect(client):
    code = client.post(
        "/api/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": "validate-user"},
    ).json()["pairing_code"]
    paired = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-validate","apptoken":"saved-token"}'},
    ).json()
    auth = {"Authorization": f"Bearer {paired['browser_link_token']}"}
    client.post(
        "/api/connect/zepp/link/disconnected",
        headers=auth,
        json={"reason": "cookie temporarily invisible"},
    )

    validated = client.post("/api/connect/zepp/link/validate", headers=auth)
    assert validated.status_code == 200
    assert validated.json()["status"] == "connected"
    status = client.get(
        "/api/connect/zepp/token", headers={"X-User-Id": "validate-user"}
    ).json()
    assert status["connection_status"] == "connected"


def test_browser_link_validation_network_failure_keeps_connection(client, monkeypatch):
    from vitalis.entrypoints.api.routes import zepp_pairing
    from vitalis.adapters.zepp import ZeppAuthError

    code = client.post(
        "/api/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": "validate-network-user"},
    ).json()["pairing_code"]
    paired = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-network","apptoken":"saved-token"}'},
    ).json()
    auth = {"Authorization": f"Bearer {paired['browser_link_token']}"}

    class UnavailableClient:
        def verify(self):
            raise ZeppAuthError("网络错误: temporary failure", kind="network")

    class UnavailableConnector:
        def _client_for(self, *_args, **_kwargs):
            return UnavailableClient()

    monkeypatch.setattr(
        zepp_pairing, "get_connector", lambda _source: UnavailableConnector()
    )
    response = client.post("/api/connect/zepp/link/validate", headers=auth)

    assert response.status_code == 503
    assert response.json() == {
        "code": "service_unavailable", "message": "Service temporarily unavailable",
        "retryable": True, "request_id": response.headers["x-request-id"],
    }
    status = client.get(
        "/api/connect/zepp/token",
        headers={"X-User-Id": "validate-network-user"},
    ).json()
    assert status["connection_status"] == "connected"
    assert status["needs_login"] is False


def test_browser_link_rejects_invalid_token(client):
    response = client.post(
        "/api/connect/zepp/link/credentials",
        headers={"Authorization": f"Bearer {'x' * 48}"},
        json={"cookie": '{"userid":"vendor","apptoken":"token"}'},
    )
    assert response.status_code == 401
    assert "browser_link_token" not in response.text


def test_balance2_device_link_ingests_idempotent_callback_samples(client):
    created = client.post(
        "/api/connect/zepp/device-link",
        headers={"X-User-Id": "balance2-device-user"},
    )
    assert created.status_code == 200
    token = created.json()["device_link_token"]
    assert len(token) >= 32
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    auth = {"Authorization": f"Bearer {token}"}
    batch = {"protocol_version": 2, "samples": [
        {"sample_id": _device_sample_id(now_ms - 2000), "timestamp": now_ms - 2000, "sample_ordinal": 0, "heart_rate": 72},
        {"sample_id": _device_sample_id(now_ms - 2000, 1), "timestamp": now_ms - 2000, "sample_ordinal": 1, "heart_rate": 73},
    ]}

    first = client.post(
        "/api/bridge/batches", headers=auth, json=batch
    )
    second = client.post(
        "/api/bridge/batches", headers=auth, json=batch
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["acknowledged_count"] == 2
    assert second.json()["acknowledged"] == first.json()["acknowledged"]
    with session_scope() as db:
        repo = HealthRepository(db)
        rows = repo.metric_samples(
            "balance2-device-user",
            "heart_rate",
            datetime.fromtimestamp((now_ms - 3000) / 1000, tz=timezone.utc),
            datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc),
        )
        link = repo.device_link(hashlib.sha256(token.encode()).hexdigest())
    assert len(rows) == 2
    assert {row.source for row in rows} == {"zepp_os"}
    assert {row.source_scope for row in rows} == {"device_callback"}
    assert {row.device_id for row in rows} == {"balance2_zepp_os"}
    assert link is not None and link.last_seen_at is not None


def test_bridge_preserves_same_millisecond_across_distinct_service_sessions(client):
    created = client.post(
        "/api/connect/zepp/device-link",
        headers={"X-User-Id": "same-ms-bridge-user"},
    )
    token = created.json()["device_link_token"]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 1000
    ids = [_device_sample_id(now_ms, 0, nonce) for nonce in ("sessiona", "sessionb")]
    batch = {"protocol_version": 2, "samples": [
        {"sample_id": sample_id, "timestamp": now_ms, "sample_ordinal": 0, "heart_rate": rate}
        for sample_id, rate in zip(ids, (72, 74))
    ]}
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(2):
        response = client.post("/api/bridge/batches", headers=headers, json=batch)
        assert response.status_code == 200
        assert {item["sample_id"] for item in response.json()["acknowledged"]} == set(ids)
    with session_scope() as db:
        rows = HealthRepository(db).metric_samples(
            "same-ms-bridge-user", "heart_rate",
            datetime.fromtimestamp((now_ms - 1) / 1000, tz=timezone.utc),
            datetime.fromtimestamp((now_ms + 1) / 1000, tz=timezone.utc),
        )
    assert len(rows) == 2
    assert {row.source_record_id for row in rows} == set(ids)
    assert {row.value for row in rows} == {72.0, 74.0}
    assert {row.timestamp for row in rows} == {
        datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    }
    public = client.get(
        "/api/health/metrics/heart_rate?resolution=raw",
        headers={"X-User-Id": "same-ms-bridge-user"},
    )
    assert public.status_code == 200
    points = public.json()["points"]
    assert {point["sample_id"] for point in points} == set(ids)
    assert len({point["timestamp"] for point in points}) == 1


def test_balance2_device_link_v2_settles_each_sample_without_partial_loss(client):
    created = client.post(
        "/api/connect/zepp/device-link",
        headers={"X-User-Id": "balance2-v2-user"},
    )
    token = created.json()["device_link_token"]
    auth = {"Authorization": f"Bearer {token}"}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    old_ms = now_ms - 32 * 24 * 60 * 60 * 1000
    future_ms = now_ms + 10 * 60 * 1000
    batch = {
        "protocol_version": 2,
        "samples": [
            {"sample_id": _device_sample_id(now_ms - 1000), "timestamp": now_ms - 1000, "sample_ordinal": 0, "heart_rate": 72},
            {"sample_id": _device_sample_id(old_ms), "timestamp": old_ms, "sample_ordinal": 0, "heart_rate": 70},
            {"sample_id": _device_sample_id(future_ms), "timestamp": future_ms, "sample_ordinal": 0, "heart_rate": 71},
            {"sample_id": _device_sample_id(now_ms - 2000), "timestamp": now_ms - 2000, "sample_ordinal": 0, "heart_rate": 10},
            "not-an-object",
        ],
    }

    response = client.post(
        "/api/bridge/batches", headers=auth, json=batch
    )

    assert response.status_code == 200
    result = response.json()
    assert result["protocol_version"] == 2
    assert result["status"] == "processed"
    assert result["received_count"] == 5
    assert result["acknowledged"] == [
        {
            "sample_id": _device_sample_id(now_ms - 1000),
            "timestamp": now_ms - 1000,
            "sample_ordinal": 0,
        }
    ]
    assert {item["code"] for item in result["rejected"]} == {
        "timestamp_too_old",
        "timestamp_too_future",
        "heart_rate_out_of_range",
        "invalid_sample",
    }
    assert all(item["retryable"] is False for item in result["rejected"])
    with session_scope() as db:
        rows = HealthRepository(db).metric_samples(
            "balance2-v2-user",
            "heart_rate",
            datetime.fromtimestamp((now_ms - 3000) / 1000, tz=timezone.utc),
            datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc),
        )
    assert [(row.timestamp, row.value) for row in rows] == [
        (datetime.fromtimestamp((now_ms - 1000) / 1000, tz=timezone.utc).replace(tzinfo=None), 72.0)
    ]


def test_balance2_device_link_v2_replay_and_all_rejected_are_processed(client):
    created = client.post(
        "/api/connect/zepp/device-link",
        headers={"X-User-Id": "balance2-v2-replay-user"},
    )
    token = created.json()["device_link_token"]
    auth = {"Authorization": f"Bearer {token}"}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    valid = {
        "protocol_version": 2,
        "samples": [{
            "sample_id": _device_sample_id(now_ms), "timestamp": now_ms,
            "sample_ordinal": 0, "heart_rate": 75,
        }],
    }

    first = client.post(
        "/api/bridge/batches", headers=auth, json=valid
    )
    replay = client.post(
        "/api/bridge/batches", headers=auth, json=valid
    )
    rejected = client.post(
        "/api/bridge/batches",
        headers=auth,
        json={
            "protocol_version": 2,
            "samples": [
                {"sample_id": _device_sample_id(now_ms), "timestamp": now_ms, "sample_ordinal": 0, "heart_rate": 76},
                {"sample_id": _device_sample_id(now_ms), "timestamp": now_ms + 1, "sample_ordinal": 0, "heart_rate": 77},
            ],
        },
    )
    mismatch = client.post(
        "/api/bridge/batches",
        headers=auth,
        json={
            "protocol_version": 2,
            "samples": [{
                "sample_id": _device_sample_id(now_ms, nonce="other"),
                "timestamp": now_ms + 1,
                "sample_ordinal": 0,
                "heart_rate": 78,
            }],
        },
    )

    assert first.status_code == replay.status_code == rejected.status_code == 200
    assert mismatch.status_code == 200
    assert first.json()["acknowledged"] == replay.json()["acknowledged"]
    assert replay.json()["acknowledged_count"] == 1
    assert rejected.json()["acknowledged"] == []
    assert rejected.json()["rejected_count"] == 1
    assert {item["code"] for item in rejected.json()["rejected"]} == {
        "duplicate_sample_id"
    }
    assert mismatch.json()["rejected"][0]["code"] == "sample_identity_mismatch"


def test_balance2_device_upload_rejects_invalid_link(client):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    response = client.post(
        "/api/bridge/batches",
        headers={"Authorization": f"Bearer {'x' * 48}"},
        json={
            "protocol_version": 2,
            "samples": [{
                "sample_id": _device_sample_id(now_ms), "timestamp": now_ms,
                "sample_ordinal": 0, "heart_rate": 72,
            }],
        },
    )
    assert response.status_code == 401


@pytest.mark.parametrize("interleaving", ["revoke", "delete_link", "delete_user"])
def test_device_upload_rejects_link_changed_before_write(client, monkeypatch, interleaving):
    from vitalis.entrypoints.api.routes import zepp_pairing

    user_id = f"device-race-{interleaving}"
    created = client.post("/api/connect/zepp/device-link", headers={"X-User-Id": user_id})
    assert created.status_code == 200
    token = created.json()["device_link_token"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    timestamp = int(datetime.now(timezone.utc).timestamp() * 1000) - 1000
    sample_id = _device_sample_id(timestamp)
    raced = []

    class InterleavedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if not raced:
                raced.append(True)
                with session_scope() as db:
                    if interleaving == "revoke":
                        db.execute(
                            update(ZeppDeviceLink)
                            .where(ZeppDeviceLink.token_digest == digest)
                            .values(revoked_at=datetime.now(timezone.utc).replace(tzinfo=None))
                        )
                    elif interleaving == "delete_link":
                        db.execute(
                            delete(ZeppDeviceLink).where(ZeppDeviceLink.token_digest == digest)
                        )
                    else:
                        HealthRepository(db).delete_for_user(user_id)
            return datetime.now(tz)

    # The old code checked the link in a separate session before reading the clock.
    monkeypatch.setattr(zepp_pairing, "datetime", InterleavedDatetime)
    response = client.post(
        "/api/bridge/batches",
        headers={"Authorization": f"Bearer {token}"},
        json={"protocol_version": 2, "samples": [{
            "sample_id": sample_id, "timestamp": timestamp,
            "sample_ordinal": 0, "heart_rate": 72,
        }]},
    )

    assert raced
    assert response.status_code == 401, response.text
    assert token not in response.text
    assert sample_id not in response.text
    with session_scope() as db:
        repo = HealthRepository(db)
        rows = repo.metric_samples(
            user_id, "heart_rate",
            datetime.fromtimestamp((timestamp - 1) / 1000, tz=timezone.utc),
            datetime.fromtimestamp((timestamp + 1) / 1000, tz=timezone.utc),
        )
        link = repo.device_link(digest)
    assert rows == []
    if interleaving == "revoke":
        assert link is not None and link.revoked_at is not None
        assert link.last_seen_at is None
    else:
        assert link is None


def test_link_refresh_queues_durable_attempt_for_coordinator(client):
    code = client.post(
        "/api/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": "coordinator-link-user"},
    ).json()["pairing_code"]
    paired = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-coordinator-link","apptoken":"first-token"}'},
    ).json()
    response = client.post(
        "/api/connect/zepp/link/credentials",
        headers={"Authorization": f"Bearer {paired['browser_link_token']}"},
        json={"cookie": '{"userid":"vendor-coordinator-link","apptoken":"second-token"}'},
    )

    assert response.status_code == 200
    assert response.json()["sync_status"] == "queued"
    attempt_id = response.json()["sync_attempt_id"]
    with session_scope() as db:
        attempt = HealthRepository(db).sync_attempt(attempt_id, user_id="coordinator-link-user")
        assert attempt is not None and attempt.status == "queued"


def test_initial_pairing_queues_durable_attempt_for_coordinator(client):
    user_id = "coordinator-initial-user"
    code = client.post(
        "/api/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": user_id},
    ).json()["pairing_code"]
    submitted = client.post(
        f"/api/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-coordinator-initial","apptoken":"token"}'},
    )

    assert submitted.status_code == 200
    attempt_id = submitted.json()["sync_attempt_id"]
    assert submitted.json()["sync_status"] == "queued"
    status = client.get(
        f"/api/connect/zepp/pair/{code}", headers={"X-User-Id": user_id}
    ).json()
    assert status["sync_attempt_id"] == attempt_id
    assert status["sync_status"] == "queued"


def test_browser_link_cannot_be_rebound_by_user_header(client):
    def pair(user_id: str) -> str:
        code = client.post(
            "/api/connect/zepp/pair",
            headers={"X-User-Id": user_id},
        ).json()["pairing_code"]
        return client.post(
            f"/api/connect/zepp/pair/{code}/credentials",
            json={"cookie": f'{{"userid":"{user_id}","apptoken":"token-{user_id}"}}'},
        ).json()["browser_link_token"]

    first_token = pair("link-owner")
    pair("other-user")
    response = client.post(
        "/api/connect/zepp/link/disconnected",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-User-Id": "other-user",
        },
        json={"reason": "owner browser signed out"},
    )
    assert response.status_code == 200

    owner = client.get(
        "/api/connect/zepp/token", headers={"X-User-Id": "link-owner"}
    ).json()
    other = client.get(
        "/api/connect/zepp/token", headers={"X-User-Id": "other-user"}
    ).json()
    assert owner["connection_status"] == "needs_login"
    assert other["connection_status"] == "connected"


def test_cloud_pairing_raw_bookmarklet_flow(client):
    code = client.post(
        "/api/connect/zepp/pair",
        headers={"X-User-Id": "bookmark-user"},
    ).json()["pairing_code"]
    cookie = '{"userid":"vendor-43","apptoken":"bookmark-token"}'
    submitted = client.post(
        f"/api/connect/zepp/pair/{code}/credentials/raw",
        content=cookie,
        headers={"Content-Type": "text/plain"},
    )
    assert submitted.status_code == 200


def test_extension_zip_download(client):
    response = client.get("/api/connect/zepp/extension.zip")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
