"""Persistence/application contracts for explicit Phase 3 product inputs."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import (
    AnalysisJob,
    AnalysisRun,
    AnalysisSnapshot,
    HealthEventObservation,
    HealthEventRecord,
    NotificationDelivery,
    RecommendationInstance,
    User,
    Workout,
)
from vitalis.adapters.persistence.product_tracking import (
    ProductFeedbackEvent,
    ProductFeedbackRevision,
    ProductGoal,
    ProductGoalRevision,
    ProductInputEvent,
    ProductWriteAttempt,
    ProductWriteRequest,
    SqlProductTrackingStore,
    product_analysis_context,
)
from vitalis.application.product_tracking import (
    GoalInput,
    GoalPatch,
    ProductFeedbackInput,
    ProductIdempotencyConflict,
    ProductResourceNotFound,
    ProductRevisionConflict,
    ProductTrackingService,
)


DAY = date(2026, 9, 20)
T0 = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


@pytest.fixture
def product_database(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'product-tracking.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all([User(id="product-owner"), User(id="product-other")])
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def clock():
    value = {"now": T0}

    def now():
        return value["now"]

    def set_now(new_value):
        value["now"] = new_value

    return now, set_now


def _service(factory, clock, *, user="product-owner"):
    now, _ = clock
    return ProductTrackingService(
        SqlProductTrackingStore(factory, timezone_name="UTC"),
        today_factory=lambda: DAY,
        now_factory=now,
        timezone_name="UTC",
    )


def _goal(**overrides):
    payload = {
        "confirmed": True,
        "goal_type": "sleep_duration",
        "target_value": 450,
        "target_unit": "min",
        "target_date": DAY + timedelta(days=30),
        "window_days": 3,
    }
    payload.update(overrides)
    return GoalInput.model_validate(payload)


def _report_rows(factory, user_id="product-owner", *, run_id="run-1", target=DAY, created=None):
    created = created or datetime(2026, 9, 20, 1)
    with factory.begin() as db:
        db.add(AnalysisRun(
            id=run_id,
            user_id=user_id,
            target_date=target,
            status="SUCCEEDED",
            started_at=created,
            completed_at=created + timedelta(minutes=10),
            intelligence_version="test",
            decision_policy_version="test",
            evidence_version="test",
            input_revision_used=0,
            config_digest="test",
            input_manifest={},
            input_manifest_hash="test",
        ))
        db.add(AnalysisSnapshot(
            id=f"snapshot-{run_id}",
            analysis_run_id=run_id,
            user_id=user_id,
            profile_type="daily",
            period_start=target,
            period_end=target,
            schema_version="test",
            intelligence_version="test",
            decision_policy_version="test",
            evidence_version="test",
            payload={
                "data_quality": {"required_signals": ["sleep_duration"], "flags": []},
                "facts": {},
            },
            generated_at=created + timedelta(minutes=10),
        ))


def _recommendation(factory, *, rec_id="rec-1", run_id="run-1", user_id="product-owner", day=DAY, status="PLANNED"):
    decision = {
        "recommendation_id": rec_id,
        "action": "TRAIN_NORMAL",
        "confidence": "LOW",
        "action_plan": {
            "valid_for_date": day.isoformat(),
            "expires_at": datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat(),
            "safety_status": "CLEAR",
            "safety_status_label": "安全",
            "weekly_balance": {
                "running_completed_7d": 0, "running_target_7d": 1,
                "running_completed_28d": 0, "running_target_28d": 4,
                "strength_completed_7d": 0, "strength_target_7d": 1,
                "strength_completed_28d": 0, "strength_target_28d": 4,
                "running_due": False, "strength_due": False,
            },
            "session_relationship": "NONE",
            "session_relationship_label": "无",
        },
    }
    with factory.begin() as db:
        db.add(RecommendationInstance(
            id=rec_id,
            analysis_run_id=run_id,
            user_id=user_id,
            date=day,
            decision=decision,
            completion_status=status,
            created_at=datetime(2026, 9, 20, 2),
        ))


def test_goal_lifecycle_persists_target_and_preferences_and_queues_scoped_input(product_database, clock):
    service = _service(product_database, clock)
    before = service.goals("product-owner")["training_preferences"]["revision"]
    created = service.put_goal(
        "product-owner",
        _goal(
            goal_type="weekly_running_sessions",
            target_value=3,
            target_unit="sessions",
            target_date=DAY + timedelta(days=45),
            available_training_days=[7, 2, 4],
            pain_or_injury_status="PRESENT",
            pain_or_injury_notes="synthetic knee note",
            expected_training_preferences_revision=before,
        ),
        idempotency_key="product-goal-create-001",
    )
    assert created["goal"]["source"] == "user_confirmed"
    assert created["goal"]["accepted"] is True
    assert created["goal"]["target_date"] == (DAY + timedelta(days=45)).isoformat()
    assert created["goal"]["target_value"] == 3
    assert created["training_preferences"]["available_training_days"] == [2, 4, 7]
    assert created["training_preferences"]["pain_or_injury_status"] == "PRESENT"
    assert created["input_event"]["affected_dates"] == [DAY.isoformat()]
    assert set(created["input_event"]["affected_streams"]) == {
        "preferences", "product_goal", "recommendation",
    }

    with product_database() as db:
        assert db.scalar(select(func.count()).select_from(ProductGoal)) == 1
        assert db.scalar(select(func.count()).select_from(ProductGoalRevision)) == 1
        event_row = db.scalar(select(ProductInputEvent))
        assert event_row.payload_ref.startswith("goal:")
        jobs = db.scalars(select(AnalysisJob)).all()
        assert jobs and {job.target_date for job in jobs} == {DAY}
        assert all(job.user_id == "product-owner" for job in jobs)


def test_goal_idempotency_revision_conflict_and_user_isolation(product_database, clock):
    service = _service(product_database, clock)
    first = service.put_goal("product-owner", _goal(), idempotency_key="product-goal-replay-001")
    replay = service.put_goal("product-owner", _goal(), idempotency_key="product-goal-replay-001")
    assert replay == first
    with pytest.raises(ProductIdempotencyConflict):
        service.put_goal(
            "product-owner", _goal(target_value=451), idempotency_key="product-goal-replay-001",
        )

    goal_id = first["goal"]["id"]
    with pytest.raises(ProductRevisionConflict):
        service.patch_goal(
            "product-owner", goal_id,
            GoalPatch(confirmed=True, expected_revision=2, target_value=451),
            idempotency_key="product-goal-stale-001",
        )
    with pytest.raises(ProductResourceNotFound):
        service.patch_goal(
            "product-other", goal_id,
            GoalPatch(confirmed=True, expected_revision=1, target_value=451),
            idempotency_key="product-goal-other-001",
        )

    with product_database() as db:
        assert db.scalar(select(func.count()).select_from(ProductGoal)) == 1
        attempts = db.scalars(select(ProductWriteAttempt)).all()
        assert {row.outcome for row in attempts} >= {"accepted", "replay", "idempotency_conflict", "revision_conflict"}
        assert db.scalar(select(func.count()).select_from(ProductWriteRequest)) == 1


def test_preference_revision_is_checked_without_leaking_partial_goal_write(product_database, clock):
    service = _service(product_database, clock)
    current = service.goals("product-owner")["training_preferences"]["revision"]
    first = service.put_goal(
        "product-owner",
        _goal(available_training_days=[1], expected_training_preferences_revision=current),
        idempotency_key="product-preference-create-001",
    )
    goal_id = first["goal"]["id"]
    with pytest.raises(ProductRevisionConflict):
        service.patch_goal(
            "product-owner",
            goal_id,
            GoalPatch(
                confirmed=True,
                expected_revision=1,
                target_value=500,
                available_training_days=[2],
                expected_training_preferences_revision=current,
            ),
            idempotency_key="product-preference-stale-001",
        )
    after = service.goals("product-owner")
    assert after["goals"][0]["revision"] == 1
    assert after["goals"][0]["target_value"] == first["goal"]["target_value"]
    assert after["training_preferences"]["available_training_days"] == [1]


def test_each_feedback_kind_is_explicit_audited_and_scoped(product_database, clock):
    service = _service(product_database, clock)
    _report_rows(product_database, run_id="run-feedback")
    _recommendation(product_database, rec_id="rec-feedback", run_id="run-feedback")
    _recommendation(product_database, rec_id="rec-other", run_id="run-other", user_id="product-other")
    with product_database.begin() as db:
        db.add(Workout(
            user_id="product-owner", source="zepp", workout_id="workout-feedback",
            started_at=datetime(2026, 9, 20, 8), data={}, detail_synced=False,
        ))
        db.add(HealthEventRecord(
            id="event-feedback", user_id="product-owner", event_type="sleep_debt",
            metric="sleep_duration", start_date=DAY - timedelta(days=1), end_date=DAY,
            lifecycle="DETECTED", last_observed_date=DAY, last_evaluated_date=DAY,
            payload={},
        ))
        db.add(HealthEventObservation(
            id="observation-feedback", analysis_run_id="run-feedback", event_id="event-feedback",
            user_id="product-owner", date=DAY, detected=True, lifecycle="DETECTED",
            created_at=datetime(2026, 9, 20, 1),
        ))

    bodies = [
        ProductFeedbackInput(
            confirmed=True, kind="report_usefulness", report_run_id="run-feedback",
            usefulness="partly_useful", notes="synthetic usefulness note",
        ),
        ProductFeedbackInput(
            confirmed=True, kind="data_correction", correction_field="sleep_duration",
            corrected_value=420, correction_unit="min", correction_observed_on=DAY - timedelta(days=1),
        ),
        ProductFeedbackInput(
            confirmed=True, kind="recommendation_completion", report_run_id="run-feedback",
            recommendation_id="rec-feedback", workout_source="zepp", workout_id="workout-feedback",
            completed=True,
        ),
        ProductFeedbackInput(
            confirmed=True, kind="recommendation_outcome", report_run_id="run-feedback",
            recommendation_id="rec-feedback", outcome="beneficial", next_experiment="repeat synthetic dose",
        ),
        ProductFeedbackInput(
            confirmed=True, kind="event_assessment", report_run_id="run-feedback",
            health_event_id="event-feedback", false_positive=True,
        ),
    ]
    responses = [
        service.record_feedback("product-owner", body, idempotency_key=f"product-feedback-{index:03d}-001")
        for index, body in enumerate(bodies)
    ]
    assert [response["feedback"]["kind"] for response in responses] == [
        "report_usefulness", "data_correction", "recommendation_completion",
        "recommendation_outcome", "event_assessment",
    ]
    assert responses[1]["feedback"]["applied_to_source"] is False
    assert responses[2]["feedback"]["completed"] is True
    assert responses[3]["feedback"]["outcome"] == "beneficial"
    assert responses[4]["feedback"]["false_positive"] is True

    with product_database() as db:
        feedback_rows = db.scalars(select(ProductFeedbackEvent)).all()
        assert len(feedback_rows) == 5
        assert db.scalar(select(func.count()).select_from(ProductFeedbackRevision)) == 5
        product_events = db.scalars(select(ProductInputEvent)).all()
        assert len(product_events) == 5
        assert all(event.source == "user" for event in product_events)
        assert all(event.payload_ref.startswith(("feedback:", "goal:")) for event in product_events)
        assert all(event.user_id == "product-owner" for event in product_events)
        linked = db.get(RecommendationInstance, "rec-feedback")
        assert linked.completion_status == "COMPLETED"
        assert linked.linked_workout_id == "workout-feedback"

    context = service.analysis_context("product-owner", DAY)
    assert len(context["feedback"]) == 5
    assert context["next_experiment"]["accepted"] is False
    assert context["next_experiment"]["title"] == "repeat synthetic dose"
    assert all(item["user_id"] == "product-owner" for item in context["feedback"])


def test_feedback_reference_validation_rolls_back_key_and_replay_skips_revalidation(product_database, clock):
    service = _service(product_database, clock)
    invalid = ProductFeedbackInput(
        confirmed=True, kind="recommendation_completion", recommendation_id="missing-rec",
        completed=True,
    )
    with pytest.raises(ProductResourceNotFound):
        service.record_feedback("product-owner", invalid, idempotency_key="product-bad-reference-001")
    with product_database() as db:
        assert db.scalar(select(func.count()).select_from(ProductWriteRequest)) == 0

    _report_rows(product_database, run_id="run-replay")
    valid = ProductFeedbackInput(
        confirmed=True, kind="report_usefulness", report_run_id="run-replay", usefulness="useful",
    )
    first = service.record_feedback("product-owner", valid, idempotency_key="product-feedback-replay-001")
    with product_database.begin() as db:
        db.query(AnalysisSnapshot).filter(AnalysisSnapshot.analysis_run_id == "run-replay").delete()
    replay = service.record_feedback("product-owner", valid, idempotency_key="product-feedback-replay-001")
    assert replay == first
    with product_database() as db:
        assert db.scalar(select(func.count()).select_from(ProductFeedbackEvent)) == 1


def test_historical_context_uses_feedback_revision_visible_at_as_of(product_database, clock):
    now, set_now = clock
    service = _service(product_database, clock)
    _recommendation(product_database, rec_id="rec-history", run_id="run-history")
    set_now(datetime(2026, 9, 20, 10, tzinfo=timezone.utc))
    first = service.record_feedback(
        "product-owner",
        ProductFeedbackInput(
            confirmed=True, kind="recommendation_outcome", recommendation_id="rec-history",
            outcome="neutral", next_experiment="first experiment",
        ),
        idempotency_key="product-history-first-001",
    )
    set_now(datetime(2026, 9, 20, 16, tzinfo=timezone.utc))
    service.record_feedback(
        "product-owner",
        ProductFeedbackInput(
            confirmed=True, kind="recommendation_outcome", feedback_id=first["feedback"]["id"],
            expected_revision=1, recommendation_id="rec-history", outcome="beneficial",
            next_experiment="second experiment",
        ),
        idempotency_key="product-history-second-001",
    )
    historical = product_analysis_context(
        product_database(), "product-owner", DAY,
        as_of=datetime(2026, 9, 20, 12, tzinfo=timezone.utc), timezone_name="UTC",
    )
    assert historical["feedback"][0]["next_experiment"] == "first experiment"
    assert historical["next_experiment"]["title"] == "first experiment"
    assert historical["feedback"][0]["revision"] == 1
    assert now().hour == 16


def test_concurrent_same_key_commits_exactly_one_goal(product_database, clock):
    service = _service(product_database, clock)
    body = _goal()

    def write():
        return service.put_goal("product-owner", body, idempotency_key="product-concurrent-key-001")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in [pool.submit(write), pool.submit(write)]]
    assert results[0] == results[1]
    with product_database() as db:
        assert db.scalar(select(func.count()).select_from(ProductGoal)) == 1
        assert db.scalar(select(func.count()).select_from(ProductInputEvent)) == 1
        assert db.scalar(select(func.count()).select_from(ProductWriteRequest)) == 1


def _metric_job(*, job_id, user_id, target, created, started=None, affected=None, event_type="source_sync"):
    return AnalysisJob(
        id=job_id, user_id=user_id, target_date=target, status="succeeded",
        idempotency_key=f"metric-{job_id}", request_hash=job_id,
        event_type=event_type, source="zepp", reason="synthetic metric job",
        affected_dates=affected, affected_streams=["sleep"],
        affected_start=target if affected else None, affected_end=target if affected else None,
        created_at=created, started_at=started, finished_at=started,
        updated_at=created,
    )


def test_metrics_use_explicit_denominators_and_unknown_counts(product_database, clock):
    service = _service(product_database, clock)
    first_day = DAY - timedelta(days=2)
    report_one = datetime(2026, 9, 18, 1)
    report_two = datetime(2026, 9, 20, 1)
    with product_database.begin() as db:
        db.add_all([
            AnalysisRun(
                id="metric-run-1", user_id="product-owner", target_date=DAY - timedelta(days=1),
                status="SUCCEEDED", started_at=report_one, completed_at=report_one + timedelta(minutes=10),
                intelligence_version="test", decision_policy_version="test", evidence_version="test",
                input_revision_used=0, config_digest="test", input_manifest={}, input_manifest_hash="test",
            ),
            AnalysisRun(
                id="metric-run-2", user_id="product-owner", target_date=DAY,
                status="SUCCEEDED", started_at=report_two, completed_at=report_two + timedelta(minutes=20),
                intelligence_version="test", decision_policy_version="test", evidence_version="test",
                input_revision_used=0, config_digest="test", input_manifest={}, input_manifest_hash="test",
            ),
        ])
        db.add_all([
            AnalysisSnapshot(
                id="metric-snapshot-1", analysis_run_id="metric-run-1", user_id="product-owner",
                profile_type="daily", period_start=DAY - timedelta(days=1), period_end=DAY - timedelta(days=1),
                schema_version="test", intelligence_version="test", decision_policy_version="test",
                evidence_version="test",
                payload={
                    "data_quality": {"required_signals": ["sleep_duration"], "flags": []},
                    "facts": {"sleep": [{
                        "metric": "sleep_duration", "value": 440, "unit": "min", "observed_at": "2026-09-19",
                        "provenance": {"source": "zepp", "source_scope": "device", "device_id": "watch"},
                    }]},
                },
                generated_at=report_one + timedelta(minutes=10),
            ),
            AnalysisSnapshot(
                id="metric-snapshot-2", analysis_run_id="metric-run-2", user_id="product-owner",
                profile_type="daily", period_start=DAY, period_end=DAY,
                schema_version="test", intelligence_version="test", decision_policy_version="test",
                evidence_version="test",
                payload={
                    "data_quality": {"required_signals": ["sleep_duration"]},
                    "facts": {"sleep": [{
                        "metric": "sleep_duration", "value": 450, "unit": "min", "observed_at": "2026-09-20",
                        "provenance": {"source": "zepp", "source_scope": "device", "device_id": "watch"},
                    }]},
                },
                generated_at=report_two + timedelta(minutes=20),
            ),
            _metric_job(
                job_id="metric-job-early", user_id="product-owner", target=DAY - timedelta(days=1),
                created=datetime(2026, 9, 18, 0), started=datetime(2026, 9, 18, 0, 1),
                affected=[(DAY - timedelta(days=1)).isoformat()],
            ),
            _metric_job(
                job_id="metric-job-late", user_id="product-owner", target=DAY,
                created=datetime(2026, 9, 20, 2), started=datetime(2026, 9, 20, 2, 1),
                affected=[DAY.isoformat()],
            ),
            _metric_job(
                job_id="metric-job-unknown", user_id="product-owner", target=DAY,
                created=datetime(2026, 9, 20, 3), started=None, affected=None,
            ),
            NotificationDelivery(
                id="metric-delivery-done", user_id="product-owner", analysis_run_id="metric-run-1",
                period="morning", target_date=DAY - timedelta(days=1), status="delivered",
                created_at=datetime(2026, 9, 19, 1), updated_at=datetime(2026, 9, 19, 1, 2),
            ),
            NotificationDelivery(
                id="metric-delivery-unknown", user_id="product-owner", analysis_run_id="metric-run-2",
                period="morning", target_date=DAY, status="accepted",
                created_at=datetime(2026, 9, 20, 1), updated_at=datetime(2026, 9, 20, 1, 1),
            ),
            RecommendationInstance(
                id="metric-rec-1", analysis_run_id="metric-run-1", user_id="product-owner", date=DAY - timedelta(days=1),
                decision={"action": "TRAIN_NORMAL"}, completion_status="COMPLETED",
                created_at=datetime(2026, 9, 19, 1),
            ),
            RecommendationInstance(
                id="metric-rec-2", analysis_run_id="metric-run-2", user_id="product-owner", date=DAY,
                decision={"action": "TRAIN_LIGHT"}, completion_status="PLANNED",
                created_at=datetime(2026, 9, 20, 1),
            ),
            HealthEventRecord(
                id="metric-event-1", user_id="product-owner", event_type="sleep_debt", metric="sleep_duration",
                start_date=DAY - timedelta(days=1), end_date=DAY - timedelta(days=1), lifecycle="DETECTED",
                last_observed_date=DAY - timedelta(days=1), last_evaluated_date=DAY - timedelta(days=1), payload={},
            ),
            HealthEventRecord(
                id="metric-event-2", user_id="product-owner", event_type="stress", metric="stress",
                start_date=DAY, end_date=DAY, lifecycle="DETECTED", last_observed_date=DAY,
                last_evaluated_date=DAY, payload={},
            ),
            ProductFeedbackEvent(
                id="metric-feedback-1", user_id="product-owner", kind="report_usefulness",
                occurred_on=DAY, revision=1, payload={"usefulness": "useful"}, report_run_id="metric-run-1",
                created_at=datetime(2026, 9, 20, 4), updated_at=datetime(2026, 9, 20, 4),
            ),
            ProductFeedbackEvent(
                id="metric-feedback-2", user_id="product-owner", kind="event_assessment",
                occurred_on=DAY, revision=1, payload={"false_positive": True}, health_event_id="metric-event-1",
                created_at=datetime(2026, 9, 20, 4), updated_at=datetime(2026, 9, 20, 4),
            ),
        ])
        db.add(HealthEventObservation(
            id="metric-observation-1", analysis_run_id="metric-run-1", event_id="metric-event-1",
            user_id="product-owner", date=DAY - timedelta(days=1), detected=True, lifecycle="DETECTED",
            created_at=datetime(2026, 9, 19, 1),
        ))

    metrics = service.metrics("product-owner", first_day, DAY)
    assert metrics["coverage"]["numerator"] == 2
    assert metrics["coverage"]["denominator"] == 3
    assert metrics["coverage"]["unknown_count"] == 1
    assert metrics["coverage"]["rate"] == pytest.approx(2 / 3)
    assert metrics["feedback_rate"]["numerator"] == 1
    assert metrics["feedback_rate"]["denominator"] == 2
    assert metrics["feedback_rate"]["unknown_count"] == 1
    assert metrics["adoption"]["numerator"] == 1
    assert metrics["adoption"]["denominator"] == 2
    assert metrics["adoption"]["unknown_count"] == 1
    assert metrics["false_positive"]["numerator"] == 1
    assert metrics["false_positive"]["denominator"] == 1
    assert metrics["false_positive"]["unknown_count"] == 1
    assert metrics["late"]["numerator"] == 1
    assert metrics["late"]["denominator"] == 2
    assert metrics["late"]["unknown_count"] == 1
    assert metrics["report_queue_latency"]["unknown_count"] == 1
    assert metrics["report_delivery_latency"]["sample_count"] == 1
    assert metrics["report_delivery_latency"]["unknown_count"] == 1
    assert "silence" in " ".join(metrics["limitations"])
    assert "product-other" not in str(metrics)
