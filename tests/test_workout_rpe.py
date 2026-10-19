"""Workout-summary RPE stays a sourced subjective observation through sync."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.source_journal import (
    SourceJournalRepository,
    SourceRecordInput,
)
from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.domain.models import WorkoutRpe
from vitalis.time import local_day


START = datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)


def _payload(rpe=7, *, other=0):
    return {"data": {"summary": [{
        "trackid": int(START.timestamp()),
        "end_time": int((START + timedelta(minutes=30)).timestamp()),
        "type": 52,
        "rpe": rpe,
        "unrelated_summary_value": other,
    }]}}


def _workout(user, payload):
    workout, = ZeppParser().parse_sport_history(payload)
    return workout.model_copy(update={"user_id": user})


@pytest.mark.parametrize("value", [1, 5, 10, 7.5])
def test_summary_rpe_preserves_scale_source_and_activity_time(value):
    workout = _workout("rpe-parser", _payload(value))
    rpe = workout.session_rpe
    assert rpe is not None
    assert rpe.value == value
    assert rpe.unit == "score_1_10"
    assert rpe.source == "zepp"
    assert rpe.source_scope == "workout_summary"
    assert rpe.source_field == "rpe"
    assert rpe.qualification == "user_reported"
    assert rpe.observed_at == workout.started_at
    assert rpe.time_basis == "activity_day"
    assert rpe.fetched_at is None
    assert rpe.source_record_id is None
    assert "session_rpe" in workout.observed_fields


@pytest.mark.parametrize("value", [
    None, "", "7", "NaN", False, True, 0, -1, 255, 0.5, 10.1,
    float("nan"), float("inf"), -float("inf"), 10 ** 1000, {}, [],
])
def test_missing_or_invalid_summary_rpe_does_not_become_an_observation(value):
    workout = _workout("rpe-parser-invalid", _payload(value))
    assert workout.session_rpe is None
    assert "session_rpe" not in workout.observed_fields


def test_only_verified_summary_field_is_read_and_missing_time_stays_missing():
    workout, = ZeppParser().parse_sport_history({"data": {"summary": [{
        "type": 52, "session_rpe": 8, "manualRPE": 9,
    }]}})
    assert workout.session_rpe is None
    workout, = ZeppParser().parse_sport_history({"data": {"summary": [{
        "type": 52, "rpe": 8,
    }]}})
    assert workout.session_rpe.observed_at is None


@pytest.mark.parametrize("value", [True, False, "7", 0, -1, 11, float("nan"), float("inf")])
def test_rpe_domain_model_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        WorkoutRpe(
            value=value, source="zepp", source_scope="workout_summary", source_field="rpe",
        )


def _journal(db, user, payload, *, hours=1, source="zepp", stream="workouts"):
    HealthRepository(db).bind_source_mode(user, "mock")
    return SourceJournalRepository(db).append(SourceRecordInput(
        user_id=user, source=source, source_mode="mock", stream=stream,
        record_id="synthetic-rpe-summary", observed_start=START,
        observed_end=START + timedelta(hours=1), fetched_at=START + timedelta(hours=hours),
        payload=payload,
    ))


def _saved_rpe(repo, user, workout):
    saved = repo.workout(user, workout.workout_id, source="zepp")
    assert saved is not None
    return WorkoutRpe.model_validate(saved.data["session_rpe"])


def test_summary_rpe_journal_provenance_is_idempotent_but_value_changes_invalidate():
    user = "rpe-source-provenance"
    payload = _payload(7)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        first = _journal(db, user, payload)
        db.info["active_source_record_id"] = first.id
        workout = _workout(user, payload)
        assert repo.save_workout(workout) == {local_day(START)}
        original = _saved_rpe(repo, user, workout)
        assert original.fetched_at == START + timedelta(hours=1)
        assert original.source_record_id == first.id
        revision = repo.analysis_input_revision(user)
        job_ids = {job.id for job in repo.analysis_jobs(user)}
        repo.save_workout(_workout(user, payload))
        assert repo.analysis_input_revision(user) == revision
        assert {job.id for job in repo.analysis_jobs(user)} == job_ids

        # A different response containing the same RPE does not move its first
        # observation provenance or enqueue a new analysis.
        same_rpe = _payload(7, other=1)
        later = _journal(db, user, same_rpe, hours=2)
        assert later.id != first.id
        db.info["active_source_record_id"] = later.id
        repo.save_workout(_workout(user, same_rpe))
        assert repo.analysis_input_revision(user) == revision
        assert {job.id for job in repo.analysis_jobs(user)} == job_ids
        assert _saved_rpe(repo, user, workout) == original

        changed_payload = _payload(9)
        changed = _journal(db, user, changed_payload, hours=3)
        db.info["active_source_record_id"] = changed.id
        repo.save_workout(_workout(user, changed_payload))
        assert repo.analysis_input_revision(user) == revision + 1
        saved = _saved_rpe(repo, user, workout)
        assert saved.value == 9
        assert saved.fetched_at == START + timedelta(hours=3)
        assert saved.source_record_id == changed.id
        jobs = repo.analysis_jobs(user)
        assert any(
            job.status == "queued" and job.input_revision == revision + 1
            and local_day(START).isoformat() in job.affected_dates
            and "workouts" in job.affected_streams for job in jobs
        )
        assert db.scalar(select(func.count()).select_from(orm.SubjectiveFeedback).where(
            orm.SubjectiveFeedback.user_id == user,
        )) == 0
        missing_payload = _payload(0)
        missing = _journal(db, user, missing_payload, hours=4)
        db.info["active_source_record_id"] = missing.id
        repo.save_workout(_workout(user, missing_payload))
        assert "session_rpe" not in repo.workout(user, workout.workout_id, source="zepp").data
        assert repo.analysis_input_revision(user) == revision + 2
        db.info.pop("active_source_record_id", None)


@pytest.mark.parametrize("journal_kind", [
    "none", "unverified_model_metadata", "other_user", "other_source", "other_stream",
])
def test_summary_rpe_does_not_invent_or_borrow_fetch_provenance(journal_kind):
    user = f"rpe-provenance-{journal_kind}"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        payload = _payload()
        if journal_kind not in {"none", "unverified_model_metadata"}:
            owner = user + "-other" if journal_kind == "other_user" else user
            if owner != user:
                repo.upsert_user(owner)
            journal = _journal(
                db, owner, payload,
                source="other" if journal_kind == "other_source" else "zepp",
                stream="workout_detail" if journal_kind == "other_stream" else "workouts",
            )
            db.info["active_source_record_id"] = journal.id
        workout = _workout(user, payload)
        if journal_kind == "unverified_model_metadata":
            workout.session_rpe = workout.session_rpe.model_copy(update={
                "fetched_at": START, "source_record_id": "unverified-record",
            })
        repo.save_workout(workout)
        saved = _saved_rpe(repo, user, workout)
        assert saved.fetched_at is None
        assert saved.source_record_id is None
        db.info.pop("active_source_record_id", None)
