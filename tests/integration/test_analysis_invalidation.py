"""Current intelligence must not be presented after its user inputs change."""

from datetime import date, datetime, timezone

import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.domain import MetricSample, NormalizedDaily, SleepRecord, Workout, WorkoutType
from vitalis.intelligence.contracts import (
    SubjectiveFeedbackInput,
    TrainingPreferencePatch,
)
from vitalis.application.intelligence_service import IntelligenceAction, IntelligenceCommand, IntelligenceQuery
from vitalis.bootstrap import get_intelligence_action, get_intelligence_command, get_intelligence_query


TARGET = date(2026, 8, 28)


def test_feedback_invalidates_saved_report_until_explicit_rerun():
    user_id = "feedback-invalidation-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    original = get_intelligence_command().analyze(user_id, TARGET)
    query = get_intelligence_query()
    assert query.weekly(user_id, TARGET).analysis_run_id == original.run.id

    get_intelligence_action().log_feedback(
        user_id,
        SubjectiveFeedbackInput(date=TARGET, physical_fatigue=4),
    )
    assert query.daily(user_id, TARGET) is None
    assert query.weekly(user_id, TARGET) is None
    assert query.monthly(user_id, TARGET) is None

    updated = get_intelligence_command().analyze(user_id, TARGET)
    assert query.weekly(user_id, TARGET).analysis_run_id == updated.run.id
    # The new input invalidates all current snapshots, but the target-day
    # feedback is outside the preceding complete calendar week.
    assert updated.weekly.facts.feedback.response_count == 0
    with session_scope() as db:
        assert len(HealthRepository(db).analysis_snapshots(user_id, "daily", TARGET, TARGET)) == 2


def test_preference_change_invalidates_report_but_noop_patch_does_not():
    user_id = "preference-invalidation-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    original = get_intelligence_command().analyze(user_id, TARGET)
    query = get_intelligence_query()
    current = query.training_preferences(user_id)
    get_intelligence_action().patch_training_preferences(
        user_id,
        TrainingPreferencePatch(weekly_running_target=current.weekly_running_target),
    )
    assert query.daily(user_id, TARGET).analysis_run_id == original.run.id

    get_intelligence_action().patch_training_preferences(
        user_id,
        TrainingPreferencePatch(weekly_running_target=current.weekly_running_target + 1),
    )
    assert query.daily(user_id, TARGET) is None
    assert query.personal_model(user_id, TARGET) is None
    rerun = get_intelligence_command().analyze(user_id, TARGET)
    assert rerun.run.input_revision_used > original.run.input_revision_used
    assert query.daily(user_id, TARGET) is not None


def test_new_daily_fact_invalidates_report_but_identical_replay_does_not():
    user_id = "daily-source-invalidation-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    query = get_intelligence_query()
    first = get_intelligence_command().analyze(user_id, TARGET)
    daily = NormalizedDaily(
        user_id=user_id,
        date=TARGET,
        sleep=SleepRecord(user_id=user_id, date=TARGET, sleep_duration=460),
    )
    with session_scope() as db:
        HealthRepository(db).save_daily(daily)
    assert query.daily(user_id, TARGET) is None

    second = get_intelligence_command().analyze(user_id, TARGET)
    assert second.run.input_revision_used > first.run.input_revision_used
    with session_scope() as db:
        HealthRepository(db).save_daily(daily)
    assert query.daily(user_id, TARGET).analysis_run_id == second.run.id


def test_same_sample_replay_does_not_invalidate_but_new_value_does():
    user_id = "sample-source-invalidation-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
    sample = MetricSample(
        user_id=user_id,
        source="zepp",
        metric="heart_rate",
        timestamp=datetime(2026, 8, 28, 4, tzinfo=timezone.utc),
        value=72,
        unit="bpm",
        source_scope="device",
        source_record_id="synthetic-sample",
    )
    with session_scope() as db:
        HealthRepository(db).save_metric_samples([sample])
    first = get_intelligence_command().analyze(user_id, TARGET)
    query = get_intelligence_query()
    with session_scope() as db:
        HealthRepository(db).save_metric_samples([sample])
    assert query.daily(user_id, TARGET).analysis_run_id == first.run.id
    with session_scope() as db:
        HealthRepository(db).save_metric_samples([sample.model_copy(update={"value": 75})])
    assert query.daily(user_id, TARGET) is None


def test_workout_change_invalidates_report_without_noop_resync():
    user_id = "workout-source-invalidation-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
    workout = Workout(
        user_id=user_id, workout_id="synthetic-workout", type=WorkoutType.RUNNING,
        started_at=datetime(2026, 8, 28, 4, tzinfo=timezone.utc), duration=30,
    )
    with session_scope() as db:
        HealthRepository(db).save_workout(workout)
    first = get_intelligence_command().analyze(user_id, TARGET)
    query = get_intelligence_query()
    with session_scope() as db:
        HealthRepository(db).save_workout(workout)
    assert query.daily(user_id, TARGET).analysis_run_id == first.run.id
    with session_scope() as db:
        HealthRepository(db).save_workout(workout.model_copy(update={"duration": 45}))
    assert query.daily(user_id, TARGET) is None


def test_input_change_during_analysis_cannot_publish_old_result(monkeypatch):
    user_id = "analysis-race-invalidation-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    original_build = IntelligenceCommand._build_daily_from_raw

    def build_then_change(*args):
        daily = original_build(*args)
        get_intelligence_action().log_feedback(
            user_id,
            SubjectiveFeedbackInput(date=TARGET, physical_fatigue=5),
        )
        return daily

    monkeypatch.setattr(IntelligenceCommand, "_build_daily_from_raw", staticmethod(build_then_change))
    with pytest.raises(RuntimeError, match="输入在计算期间发生变化"):
        get_intelligence_command().analyze(user_id, TARGET)

    assert get_intelligence_query().daily(user_id, TARGET) is None
    with session_scope() as db:
        runs = HealthRepository(db).analysis_runs(user_id, TARGET, TARGET)
        assert len(runs) == 1
        assert runs[0].status == "FAILED"


def test_source_identity_creation_and_reactivation_invalidate_saved_reports():
    user_id = "source-account-invalidation-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    command = get_intelligence_command()
    query = get_intelligence_query()
    first = command.analyze(user_id, TARGET)
    assert first.daily.metadata["identity"]["source_account_status"] is None

    with session_scope() as db:
        HealthRepository(db).ensure_source_account(user_id, "zepp", "source-account-invalidation-vendor")
    assert query.daily(user_id, TARGET) is None
    second = command.analyze(user_id, TARGET)
    assert second.daily.metadata["identity"]["source_account_status"] == "active"

    with session_scope() as db:
        HealthRepository(db).revoke_source_account(user_id, "zepp")
    revoked = command.analyze(user_id, TARGET)
    assert revoked.daily.metadata["identity"]["source_account_status"] == "revoked"

    with session_scope() as db:
        HealthRepository(db).ensure_source_account(user_id, "zepp", "source-account-invalidation-vendor")
    assert query.daily(user_id, TARGET) is None
    assert command.analyze(user_id, TARGET).daily.metadata["identity"]["source_account_status"] == "active"


def test_source_identity_created_during_analysis_cannot_publish_old_result(monkeypatch):
    user_id = "source-account-analysis-race"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    original_build = IntelligenceCommand._build_daily_from_raw

    def build_then_bind(*args):
        daily = original_build(*args)
        with session_scope() as db:
            HealthRepository(db).ensure_source_account(user_id, "zepp", "race-vendor")
        return daily

    monkeypatch.setattr(IntelligenceCommand, "_build_daily_from_raw", staticmethod(build_then_bind))
    with pytest.raises(RuntimeError, match="输入在计算期间发生变化"):
        get_intelligence_command().analyze(user_id, TARGET)
    assert get_intelligence_query().daily(user_id, TARGET) is None
