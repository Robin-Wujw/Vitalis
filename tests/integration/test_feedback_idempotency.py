"""Feedback keys claim the write before validation and survive a new DB connection."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event

import pytest
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from vitalis.domain import Workout, WorkoutType
from vitalis.intelligence.contracts import SubjectiveFeedbackInput
from vitalis.application.intelligence_service import IntelligenceAction
from vitalis.application.ports import FeedbackIdempotencyConflict
from vitalis.bootstrap import get_intelligence_action
from vitalis.adapters.persistence import database
from vitalis.adapters.persistence.database import check_schema, init_db
from vitalis.adapters.persistence.models import FeedbackRequest, SubjectiveFeedback, User, Workout as WorkoutRow
from vitalis.adapters.persistence.repositories import HealthRepository


KEY = "synthetic-feedback-request-key"
DAY = date(2026, 9, 20)


@pytest.fixture
def feedback_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'feedback.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False,
    ))
    yield engine
    engine.dispose()


def _headers(token, *, key=KEY):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _counts(engine, user_id):
    with Session(engine) as db:
        return (
            db.scalar(select(func.count()).select_from(FeedbackRequest).where(
                FeedbackRequest.user_id == user_id,
            )),
            db.scalar(select(func.count()).select_from(SubjectiveFeedback).where(
                SubjectiveFeedback.user_id == user_id,
            )),
        )


def test_feedback_key_replays_exact_result_after_new_connection_and_rejects_changes(
    feedback_database, client, issue_token,
):
    token = issue_token("feedback-key-owner", {"feedback"})
    headers = _headers(token)
    body = {"date": DAY.isoformat(), "notes": "synthetic private note", "mental_state": 3}
    first = client.post("/api/feedback", json=body, headers=headers)
    assert first.status_code == 201, first.text
    feedback_database.dispose()
    check_schema(feedback_database)

    repeated = client.post(
        "/api/feedback",
        json={"mental_state": 3, "notes": "synthetic private note", "date": DAY.isoformat()},
        headers=headers,
    )
    assert repeated.status_code == 201
    assert repeated.json() == first.json()
    for changed in (
        {**body, "date": "2026-09-21"},
        {**body, "notes": "a different private note"},
        {**body, "mental_state": 4},
    ):
        conflict = client.post("/api/feedback", json=changed, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "conflict"
        assert "private note" not in conflict.text
    assert _counts(feedback_database, "feedback-key-owner") == (1, 1)
    with Session(feedback_database) as db:
        assert db.get(User, "feedback-key-owner").analysis_input_revision == 1

    unkeyed = client.post("/api/feedback", json=body, headers={"Authorization": f"Bearer {token}"})
    assert unkeyed.status_code == 201, unkeyed.text
    assert unkeyed.json()["id"] != first.json()["id"]
    assert _counts(feedback_database, "feedback-key-owner") == (1, 2)
    with Session(feedback_database) as db:
        assert db.get(User, "feedback-key-owner").analysis_input_revision == 2


def test_omitted_day_replays_the_original_day(feedback_database, client, issue_token, monkeypatch):
    token = issue_token("feedback-default-day-owner", {"feedback"})
    monkeypatch.setattr("vitalis.bootstrap.local_today", lambda: DAY)
    first = client.post("/api/feedback", json={"notes": "synthetic note"}, headers=_headers(token))
    assert first.status_code == 201
    assert first.json()["date"] == DAY.isoformat()
    monkeypatch.setattr("vitalis.bootstrap.local_today", lambda: DAY + timedelta(days=1))
    repeated = client.post("/api/feedback", json={"notes": "synthetic note"}, headers=_headers(token))
    assert repeated.status_code == 201
    assert repeated.json() == first.json()
    assert _counts(feedback_database, "feedback-default-day-owner") == (1, 1)


def test_replay_skips_workout_validation_after_original_is_saved(
    feedback_database, client, issue_token,
):
    token = issue_token("feedback-workout-owner", {"feedback"})
    with Session(feedback_database) as db:
        with db.begin():
            HealthRepository(db).save_workout(Workout(
                user_id="feedback-workout-owner", workout_id="synthetic-workout",
                type=WorkoutType.RUNNING, duration=30,
            ))
    body = {
        "date": DAY.isoformat(), "workout_source": "zepp",
        "workout_id": "synthetic-workout", "session_rpe": 7,
    }
    first = client.post("/api/feedback", json=body, headers=_headers(token))
    assert first.status_code == 201, first.text
    with Session(feedback_database) as db:
        with db.begin():
            db.execute(delete(WorkoutRow).where(
                WorkoutRow.user_id == "feedback-workout-owner",
                WorkoutRow.workout_id == "synthetic-workout",
            ))
    replay = client.post("/api/feedback", json=body, headers=_headers(token))
    assert replay.status_code == 201
    assert replay.json() == first.json()
    conflict = client.post(
        "/api/feedback", json={**body, "session_rpe": 8}, headers=_headers(token),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "conflict"
    assert _counts(feedback_database, "feedback-workout-owner") == (1, 1)


def test_feedback_key_and_replayed_feedback_are_user_scoped(feedback_database, client, issue_token):
    owner = issue_token("feedback-scope-owner", {"feedback"})
    other = issue_token("feedback-scope-other", {"feedback"})
    first = client.post(
        "/api/feedback", json={"date": DAY.isoformat(), "notes": "owner note"},
        headers=_headers(owner),
    )
    second = client.post(
        "/api/feedback", json={"date": DAY.isoformat(), "notes": "other note"},
        headers=_headers(other),
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["user_id"] == "feedback-scope-owner"
    assert second.json()["user_id"] == "feedback-scope-other"
    conflict = client.post(
        "/api/feedback", json={"date": DAY.isoformat(), "notes": "owner note"},
        headers=_headers(other),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "conflict"
    assert _counts(feedback_database, "feedback-scope-owner") == (1, 1)
    assert _counts(feedback_database, "feedback-scope-other") == (1, 1)


def test_invalid_reference_and_failed_write_leave_no_key_or_feedback(
    feedback_database, client, issue_token, monkeypatch,
):
    token = issue_token("feedback-rollback-owner", {"feedback"})
    invalid = client.post(
        "/api/feedback",
        json={"date": DAY.isoformat(), "workout_source": "zepp", "workout_id": "absent", "session_rpe": 6},
        headers=_headers(token),
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "validation_error"
    assert _counts(feedback_database, "feedback-rollback-owner") == (0, 0)

    original = IntelligenceAction._save_feedback

    def fail_after_save(self, repository, user_id, feedback_input):
        original(self, repository, user_id, feedback_input)
        raise RuntimeError("simulated storage failure")

    body = {"date": DAY.isoformat(), "notes": "synthetic private note"}
    with monkeypatch.context() as patch:
        patch.setattr(IntelligenceAction, "_save_feedback", fail_after_save)
        failed = client.post("/api/feedback", json=body, headers=_headers(token))
        assert failed.status_code == 500
        assert failed.json()["code"] == "internal_error"
    assert _counts(feedback_database, "feedback-rollback-owner") == (0, 0)
    assert client.post("/api/feedback", json=body, headers=_headers(token)).status_code == 201
    assert _counts(feedback_database, "feedback-rollback-owner") == (1, 1)


def test_competing_requests_with_same_key_never_write_a_second_feedback(
    feedback_database, monkeypatch,
):
    with Session(feedback_database) as db:
        with db.begin():
            db.add(User(id="feedback-race-owner"))

    entered = Event()
    release = Event()
    original_save = IntelligenceAction._save_feedback

    def block_first_request(self, repository, user_id, feedback_input):
        if feedback_input.notes == "first private note":
            entered.set()
            assert release.wait(10)
        return original_save(self, repository, user_id, feedback_input)

    monkeypatch.setattr(IntelligenceAction, "_save_feedback", block_first_request)

    def write(notes):
        return get_intelligence_action().log_feedback(
            "feedback-race-owner",
            SubjectiveFeedbackInput(date=DAY, notes=notes),
            idempotency_key=KEY,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "first private note")
        try:
            assert entered.wait(10)
            second = pool.submit(write, "second private note")
        finally:
            release.set()
        original = first.result(timeout=15)
        with pytest.raises(FeedbackIdempotencyConflict):
            second.result(timeout=15)

    with Session(feedback_database) as db:
        rows = db.scalars(select(SubjectiveFeedback).where(
            SubjectiveFeedback.user_id == "feedback-race-owner"
        )).all()
        ledger = db.scalar(select(FeedbackRequest).where(
            FeedbackRequest.user_id == "feedback-race-owner",
        ))
        assert len(rows) == 1
        assert rows[0].id == ledger.feedback_id == original.id
        assert ledger.request_hash != "a" * 64
        assert "private note" not in ledger.request_hash
    with Session(feedback_database) as db:
        with db.begin():
            HealthRepository(db).delete_for_user("feedback-race-owner")
    assert _counts(feedback_database, "feedback-race-owner") == (0, 0)
