from datetime import date, timedelta

import pytest

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
    SubjectiveFeedbackInput,
)
from vitalis.intelligence.service import IntelligencePipeline
from vitalis.models import Workout, WorkoutType
from vitalis.storage import HealthRepository, session_scope


TARGET = date(2026, 8, 28)


def test_feedback_is_validated_stored_and_user_scoped():
    user_id = "feedback-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    pipeline = IntelligencePipeline()
    feedback = pipeline.log_feedback(
        user_id,
        SubjectiveFeedbackInput(
            date=TARGET,
            session_rpe=7,
            physical_fatigue=3,
            mental_state=4,
            muscle_soreness=2,
            notes="  正常完成  ",
        ),
    )

    assert feedback.notes == "正常完成"
    assert pipeline.feedback(user_id, TARGET, TARGET)[0].session_rpe == 7
    assert pipeline.feedback("another-user", TARGET, TARGET) == []


def test_feedback_rejects_foreign_workout_reference():
    with session_scope() as db:
        repo = HealthRepository(db)
        for user_id in ("feedback-owner", "feedback-other"):
            repo.delete_for_user(user_id)
            repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id="feedback-owner",
            workout_id="private-workout",
            type=WorkoutType.RUNNING,
            duration=30,
        ))

    with pytest.raises(ValueError, match="不属于当前用户"):
        IntelligencePipeline().log_feedback(
            "feedback-other",
            SubjectiveFeedbackInput(
                date=TARGET,
                workout_id="private-workout",
                session_rpe=6,
            ),
        )


def test_analysis_snapshot_upsert_is_idempotent():
    user_id = "snapshot-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        for marker in ("first", "second"):
            repo.save_analysis_snapshot(
                user_id,
                "daily",
                TARGET,
                TARGET,
                "1.0",
                "model-1",
                {"generated_at": "2026-08-28T01:00:00Z", "marker": marker},
            )
        rows = repo.analysis_snapshots(
            user_id, "daily", TARGET - timedelta(days=1), TARGET
        )

    assert len(rows) == 1
    assert rows[0].payload["marker"] == "second"


def test_pipeline_persists_daily_weekly_snapshots_and_uses_feedback():
    user_id = "pipeline-snapshot-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    pipeline = IntelligencePipeline()
    pipeline.log_feedback(
        user_id,
        SubjectiveFeedbackInput(
            date=TARGET,
            session_rpe=8,
            physical_fatigue=4,
            mental_state=3,
        ),
    )
    stored_event = HealthEvent(
        id="weekly-stored-event",
        type="TRAINING_GAP",
        type_label="训练连续中断",
        severity=EventSeverity.INFO,
        severity_label="提示",
        start_date=TARGET - timedelta(days=6),
        end_date=TARGET - timedelta(days=1),
        duration_days=6,
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
        summary="已持久化的本周事件",
    )
    with session_scope() as db:
        HealthRepository(db).save_health_events(user_id, [stored_event])
    daily = pipeline.build_daily_profile(user_id, TARGET)
    weekly = pipeline.build_weekly_profile(user_id, TARGET)

    assert weekly.facts.feedback.response_count == 1
    assert weekly.facts.feedback.average_session_rpe == 8
    assert weekly.facts.feedback.average_physical_fatigue == 4
    assert "weekly-stored-event" in {event.id for event in weekly.inferences.events}
    with session_scope() as db:
        repo = HealthRepository(db)
        daily_rows = repo.analysis_snapshots(user_id, "daily", TARGET, TARGET)
        weekly_rows = repo.analysis_snapshots(
            user_id, "weekly", TARGET - timedelta(days=6), TARGET
        )
    assert len(daily_rows) == 1
    assert daily_rows[0].payload["date"] == daily.date.isoformat()
    assert len(weekly_rows) == 1
    assert weekly_rows[0].payload["period_end"] == TARGET.isoformat()
