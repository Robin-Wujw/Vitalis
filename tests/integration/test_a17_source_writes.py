"""A17 source writes invalidate only on substantive changes."""

from datetime import date, datetime, timedelta, timezone

import pytest
from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.repositories import SourceIdentityConflict
from vitalis.domain import (
    DailyMetric,
    DenseDataFile,
    Device,
    NormalizedDaily,
    SleepRecord,
    Workout,
    WorkoutType,
)


UTC = timezone.utc
START = datetime(2026, 8, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def _fresh_user(user_id: str) -> None:
    with session_scope() as db:
        HealthRepository(db).upsert_user(user_id)


def test_source_upserts_bump_once_for_substantive_changes():
    user_id = "a17-source-upserts"
    _fresh_user(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        metric = DailyMetric(
            user_id=user_id,
            date=date(2026, 8, 1),
            metric="steps",
            value=10,
        )
        assert repo.save_daily_metrics([metric]) == 1
        assert repo.analysis_input_revision(user_id) == 1
        assert repo.save_daily_metrics([metric]) == 1
        assert repo.analysis_input_revision(user_id) == 1
        assert repo.save_daily_metrics([metric.model_copy(update={"value": 11})]) == 1
        assert repo.analysis_input_revision(user_id) == 2

        dense = DenseDataFile(
            user_id=user_id,
            stream="second_heart_rate",
            file_id="a17-file",
            start_utc=START,
            parse_status="indexed",
        )
        assert repo.save_dense_data_files([dense]) == 1
        assert repo.analysis_input_revision(user_id) == 3
        assert repo.save_dense_data_files([dense]) == 1
        assert repo.analysis_input_revision(user_id) == 3
        assert repo.save_dense_data_files([
            dense.model_copy(update={"parse_status": "decoded"})
        ]) == 1
        assert repo.analysis_input_revision(user_id) == 4

        device = Device(
            user_id=user_id,
            source="zepp",
            device_id="a17-device",
            model="Model A",
        )
        repo.upsert_device(device)
        assert repo.analysis_input_revision(user_id) == 5
        repo.upsert_device(device)
        assert repo.analysis_input_revision(user_id) == 5
        repo.upsert_device(device.model_copy(update={"model": "Model B"}))
        assert repo.analysis_input_revision(user_id) == 6


def test_workout_detail_fetched_at_only_refresh_does_not_invalidate():
    user_id = "a17-detail-refresh"
    _fresh_user(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        workout = Workout(
            user_id=user_id,
            workout_id="a17-workout",
            type=WorkoutType.RUNNING,
            started_at=START,
            duration=30,
        )
        repo.save_workout(workout)
        before_detail = repo.analysis_input_revision(user_id)
        assert repo.save_workout_detail(
            user_id,
            workout.workout_id,
            {"schema_version": "5.1", "metrics_present": []},
            fetched_at=START,
        )
        assert repo.analysis_input_revision(user_id) == before_detail + 1
        changed = repo.analysis_input_revision(user_id)
        assert repo.save_workout_detail(
            user_id,
            workout.workout_id,
            {"schema_version": "5.1", "metrics_present": []},
            fetched_at=START + timedelta(days=1),
        )
        assert repo.analysis_input_revision(user_id) == changed


def test_reordered_daily_input_fields_remain_an_idempotent_replay():
    user_id = "a17-daily-field-order"
    _fresh_user(user_id)
    fields = {
        "user_id": user_id, "date": START.date(),
        "source": "zepp", "sleep_duration": 430,
    }
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.save_daily(NormalizedDaily(
            user_id=user_id, date=START.date(), sleep=SleepRecord.model_validate(fields)
        ))
        revision = repo.analysis_input_revision(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id, date=START.date(),
            sleep=SleepRecord.model_validate(dict(reversed(list(fields.items())))),
        ))
        assert repo.analysis_input_revision(user_id) == revision


def test_workout_detail_reordered_json_keys_are_idempotent():
    user_id = "a17-detail-key-order"
    _fresh_user(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        workout = Workout(
            user_id=user_id, workout_id="reordered-detail",
            type=WorkoutType.RUNNING, started_at=START, duration=30,
        )
        repo.save_workout(workout)
        assert repo.save_workout_detail(
            user_id, workout.workout_id,
            {"schema_version": "5.1", "metrics_present": [], "nested": {"a": 1, "b": 2}},
            fetched_at=START,
        )
        revision = repo.analysis_input_revision(user_id)
        assert repo.save_workout_detail(
            user_id, workout.workout_id,
            {"nested": {"b": 2, "a": 1}, "metrics_present": [], "schema_version": "5.1"},
            fetched_at=START + timedelta(days=1),
        )
        assert repo.analysis_input_revision(user_id) == revision


def test_terminal_empty_workout_coverage_bumps_and_stale_lease_does_not():
    user_id = "a17-coverage"
    _fresh_user(user_id)
    manifest = [{
        "stable_key": "a17-empty-running",
        "stream": "workouts",
        "partition": "running",
        "ordinal": 0,
        "window_start": START,
        "window_end": END,
    }]
    with session_scope() as db:
        repo = HealthRepository(db)
        attempt = repo.create_or_reuse_sync_attempt(
            user_id,
            window_start=START,
            window_end=END,
            manifest=manifest,
        )
        assert not repo.finalize_sync_attempt_failure(
            attempt.id, "wrong-token", 1, now=START + timedelta(hours=1)
        )
        assert repo.analysis_input_revision(user_id) == 0
        assert repo.claim_sync_attempt(attempt.id, "attempt-token")
        chunk = repo.sync_chunks(attempt.id)[0]
        assert repo.claim_sync_chunk(
            chunk.id,
            "chunk-token",
            attempt_lease_token="attempt-token",
            attempt_lease_epoch=1,
        )
        assert repo.finalize_sync_chunk_success(
            chunk.id,
            "chunk-token",
            1,
            now=START + timedelta(hours=2),
            stages={
                "fetch_status": "success",
                "parse_status": "empty",
                "write_status": "not_run",
            },
        )
        assert repo.finalize_sync_attempt_success(
            attempt.id,
            "attempt-token",
            1,
            now=START + timedelta(hours=3),
        )
        assert repo.analysis_input_revision(user_id) == 1
        jobs = repo.analysis_jobs(user_id)
        assert jobs
        assert all("training_coverage" in job.affected_streams for job in jobs)
        assert all(job.input_revision == 1 for job in jobs)


def test_source_account_identity_changes_bump_revision_once():
    user_id = "a17-source-identity"
    _fresh_user(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        assert repo.analysis_input_revision(user_id) == 0
        repo.ensure_source_account(user_id, "zepp", "synthetic-vendor")
        assert repo.analysis_input_revision(user_id) == 1
        repo.ensure_source_account(user_id, "zepp", "synthetic-vendor")
        assert repo.analysis_input_revision(user_id) == 1
        assert repo.revoke_source_account(user_id, "zepp")
        assert repo.analysis_input_revision(user_id) == 2
        with pytest.raises(SourceIdentityConflict):
            repo.ensure_source_account(
                user_id, "zepp", "synthetic-vendor", allow_reactivate=False
            )
        assert repo.analysis_input_revision(user_id) == 2
        repo.ensure_source_account(user_id, "zepp", "synthetic-vendor")
        assert repo.analysis_input_revision(user_id) == 3
        repo.ensure_source_account(user_id, "zepp", "synthetic-vendor")
        assert repo.analysis_input_revision(user_id) == 3
