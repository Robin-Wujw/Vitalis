from datetime import date, timedelta

import pytest

from vitalis.intelligence.contracts import (
    AnalysisRunStatus,
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
    SubjectiveFeedbackInput,
)
from vitalis.intelligence.service import IntelligenceAction, IntelligenceCommand, IntelligenceQuery
from vitalis.models import Workout, WorkoutType
from vitalis.storage import HealthRepository, session_scope
from vitalis.storage.models import AnalysisRun as OrmAnalysisRun


TARGET = date(2026, 8, 28)


def test_feedback_is_validated_stored_and_user_scoped():
    user_id = "feedback-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id="feedback-workout",
            type=WorkoutType.RUNNING,
            duration=30,
        ))

    action = IntelligenceAction()
    feedback = action.log_feedback(
        user_id,
        SubjectiveFeedbackInput(
            date=TARGET,
            workout_source="zepp",
            workout_id="feedback-workout",
            session_rpe=7,
            physical_fatigue=3,
            mental_state=4,
            muscle_soreness=2,
            notes="  正常完成  ",
        ),
    )

    assert feedback.notes == "正常完成"
    assert IntelligenceQuery().feedback(user_id, TARGET, TARGET)[0].session_rpe == 7
    assert IntelligenceQuery().feedback("another-user", TARGET, TARGET) == []


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
        IntelligenceAction().log_feedback(
            "feedback-other",
            SubjectiveFeedbackInput(
                date=TARGET,
                workout_source="zepp",
                workout_id="private-workout",
                session_rpe=6,
            ),
        )


def test_session_rpe_requires_completed_workout_identity():
    with pytest.raises(ValueError, match="RPE 必须关联"):
        SubjectiveFeedbackInput(date=TARGET, session_rpe=6)


def test_analysis_snapshots_are_immutable_per_run():
    user_id = "snapshot-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        for index, marker in enumerate(("first", "second"), start=1):
            repo.save_analysis_snapshot(
                f"run-{index}",
                user_id,
                "daily",
                TARGET,
                TARGET,
                "12.0",
                "11.0",
                "8.0",
                "2026-09a",
                {"generated_at": "2026-08-28T01:00:00Z", "marker": marker},
            )
        rows = repo.analysis_snapshots(
            user_id, "daily", TARGET - timedelta(days=1), TARGET
        )

    assert len(rows) == 2
    assert {row.payload["marker"] for row in rows} == {"first", "second"}
    assert rows[0].analysis_run_id != rows[1].analysis_run_id


def test_legacy_snapshot_is_filtered_before_model_validation():
    user_id = "legacy-snapshot-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_analysis_snapshot(
            "legacy-run", user_id, "daily", TARGET, TARGET,
            "9.0", "9.0", "7.0", "2026-08e",
            {"schema_version": "9.0"},
        )
        assert repo.latest_analysis_snapshot(user_id, "daily", TARGET) is None
        assert repo.latest_analysis_snapshot_on_or_before(user_id, "daily", TARGET) is None


def test_command_persists_one_run_and_all_intelligence_snapshots():
    user_id = "pipeline-snapshot-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id="pipeline-feedback-workout",
            type=WorkoutType.RUNNING,
            duration=30,
        ))

    IntelligenceAction().log_feedback(
        user_id,
        SubjectiveFeedbackInput(
            date=TARGET,
            workout_source="zepp",
            workout_id="pipeline-feedback-workout",
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
        HealthRepository(db).save_health_event(user_id, stored_event)
    result = IntelligenceCommand().analyze(user_id, TARGET)
    daily = result.daily
    weekly = result.weekly
    monthly = result.monthly

    assert result.run.status == AnalysisRunStatus.SUCCEEDED
    assert result.run.completed_at is not None
    assert daily.analysis_run_id == result.run.id == weekly.analysis_run_id
    assert monthly.analysis_run_id == result.run.id
    assert result.personal_associations.analysis_run_id == result.run.id
    assert result.recommendation.id == daily.decision.recommendation_id
    assert result.recommendation.analysis_run_id == result.run.id
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
        response_rows = repo.analysis_snapshots(
            user_id, "training_responses", TARGET - timedelta(days=89), TARGET
        )
        personal_rows = repo.analysis_snapshots(
            user_id, "personal_model", TARGET, TARGET
        )
        monthly_rows = repo.analysis_snapshots(
            user_id, "monthly", TARGET - timedelta(days=27), TARGET
        )
        association_rows = repo.analysis_snapshots(
            user_id, "personal_associations", TARGET - timedelta(days=89), TARGET
        )
    assert len(daily_rows) == 1
    assert daily_rows[0].payload["date"] == daily.date.isoformat()
    assert len(weekly_rows) == 1
    assert weekly_rows[0].payload["period_end"] == TARGET.isoformat()
    assert len(response_rows) == 1
    assert len(personal_rows) == 1
    assert len(monthly_rows) == 1
    assert len(association_rows) == 1
    assert personal_rows[0].payload["analysis_run_id"] == result.run.id


def test_queries_are_read_only_and_return_latest_immutable_run():
    user_id = "query-only-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    query = IntelligenceQuery()
    assert query.daily(user_id, TARGET) is None
    assert query.weekly(user_id, TARGET) is None
    assert query.monthly(user_id, TARGET) is None
    assert query.personal_associations(user_id, TARGET) is None
    with session_scope() as db:
        assert db.query(OrmAnalysisRun).filter_by(user_id=user_id).count() == 0

    first = IntelligenceCommand().analyze(user_id, TARGET)
    second = IntelligenceCommand().analyze(user_id, TARGET)

    assert first.run.id != second.run.id
    assert query.daily(user_id, TARGET).analysis_run_id == second.run.id
    with session_scope() as db:
        repo = HealthRepository(db)
        assert len(repo.analysis_snapshots(user_id, "daily", TARGET, TARGET)) == 2
        assert len(repo.analysis_snapshots(user_id, "weekly", TARGET, TARGET)) == 2
        assert len(repo.analysis_snapshots(user_id, "monthly", TARGET, TARGET)) == 2
        assert len(repo.analysis_snapshots(user_id, "personal_associations", TARGET, TARGET)) == 2


def test_recommendation_completion_and_feedback_form_an_explicit_identity_chain():
    user_id = "recommendation-chain-user"
    workout_id = "completed-workout"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id=workout_id,
            type=WorkoutType.STRENGTH,
            duration=45,
        ))

    result = IntelligenceCommand().analyze(user_id, TARGET)
    linked = IntelligenceAction().complete_recommendation(
        user_id, result.recommendation.id, workout_id
    )
    feedback = IntelligenceAction().log_feedback(
        user_id,
        SubjectiveFeedbackInput(
            date=TARGET,
            workout_source="zepp",
            workout_id=workout_id,
            recommendation_id=linked.id,
            session_rpe=7,
        ),
    )

    assert linked.linked_workout_id == workout_id
    assert linked.completion_status.value == "COMPLETED"
    assert linked.completed_at is not None
    assert feedback.recommendation_id == linked.id


def test_recommendation_cannot_link_foreign_or_already_claimed_workout():
    owner = "recommendation-owner"
    other = "recommendation-other"
    workout_id = "owned-workout"
    with session_scope() as db:
        repo = HealthRepository(db)
        for user_id in (owner, other):
            repo.delete_for_user(user_id)
            repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=owner,
            workout_id=workout_id,
            type=WorkoutType.RUNNING,
            duration=30,
        ))
    first = IntelligenceCommand().analyze(owner, TARGET)
    second = IntelligenceCommand().analyze(owner, TARGET)

    with pytest.raises(ValueError, match="训练建议不存在"):
        IntelligenceAction().complete_recommendation(
            other, first.recommendation.id, workout_id
        )
    IntelligenceAction().complete_recommendation(owner, first.recommendation.id, workout_id)
    with pytest.raises(ValueError, match="已关联其他训练建议"):
        IntelligenceAction().complete_recommendation(
            owner, second.recommendation.id, workout_id
        )
