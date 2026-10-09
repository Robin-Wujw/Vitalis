"""Input changes and immutable invalidation chains share the writer transaction."""

from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence.analysis_jobs import SqlAnalysisJobRepository
from vitalis.adapters.persistence.database import UnitOfWork, init_db
from vitalis.adapters.persistence.input_events import (
    ACTIVE_SOURCE_RECORD_ID, AnalysisInvalidation, InputEvent,
    InputEventAuditError, InputEventAuditRepository, InputEventRequest,
)
from vitalis.adapters.persistence.models import (
    AnalysisJob, AnalysisRun, FeedbackRequest, SubjectiveFeedback, User,
)
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.application.intelligence_service import IntelligenceAction
from vitalis.application.ports import FeedbackIdempotencyConflict
from vitalis.domain import NormalizedDaily, SleepRecord
from vitalis.intelligence.contracts import SubjectiveFeedbackInput


DAY = date(2026, 9, 20)


@pytest.fixture
def audit_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'input-audit.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # The queue's eligible current target is deterministic, without changing its
    # expansion algorithm or creating a real clock/configured DB dependency.
    monkeypatch.setattr("vitalis.adapters.persistence.repositories.local_day", lambda *_: DAY)
    with factory.begin() as db:
        db.add_all([User(id="owner"), User(id="other")])
    try:
        yield factory
    finally:
        engine.dispose()


def _action(factory):
    return IntelligenceAction(lambda: UnitOfWork(factory), today_factory=lambda: DAY)


def _rows(factory, model, user_id="owner"):
    with factory() as db:
        return list(db.scalars(select(model).where(model.user_id == user_id)))


def _enqueue(repo, *, key=None, streams=None, target=DAY, **kwargs):
    return repo.enqueue_analysis_job(
        "owner", target, event_type="source_sync", source="zepp",
        affected_dates={DAY}, affected_streams=streams or {"sleep"},
        reason="synthetic source facts changed", idempotency_key=key, **kwargs,
    )


def _saved_target(db, user_id, day, suffix):
    db.add(AnalysisRun(
        id=f"synthetic-{user_id}-{suffix}", user_id=user_id, target_date=day,
        status="SUCCEEDED", started_at=datetime(2026, 9, 20),
        completed_at=datetime(2026, 9, 20), intelligence_version="test",
        decision_policy_version="test", evidence_version="test",
    ))


def test_feedback_replay_keeps_one_event_and_never_copies_notes(audit_database):
    action = _action(audit_database)
    feedback = SubjectiveFeedbackInput(date=DAY, physical_fatigue=4, notes="synthetic private note")
    first = action.log_feedback("owner", feedback, idempotency_key="synthetic-feedback-key")
    assert action.log_feedback("owner", feedback, idempotency_key="synthetic-feedback-key").id == first.id
    with pytest.raises(FeedbackIdempotencyConflict):
        action.log_feedback(
            "owner", feedback.model_copy(update={"physical_fatigue": 5}),
            idempotency_key="synthetic-feedback-key",
        )
    events = _rows(audit_database, InputEvent)
    links = _rows(audit_database, AnalysisInvalidation)
    assert len(events) == len(links) == 1
    assert len(_rows(audit_database, SubjectiveFeedback)) == 1
    assert len(_rows(audit_database, FeedbackRequest)) == 1
    assert len(_rows(audit_database, AnalysisJob)) == 1
    event_row = events[0]
    assert event_row.event_type == "feedback" and event_row.source == "user"
    assert event_row.input_revision == 1
    assert event_row.affected_dates == [DAY.isoformat()]
    assert event_row.affected_streams == ["feedback"]
    assert event_row.payload_ref == "input_revision:1"
    assert event_row.occurred_at is not None
    serialized = json.dumps({
        column.name: str(getattr(event_row, column.name)) for column in InputEvent.__table__.columns
    })
    assert "synthetic private note" not in serialized
    assert "synthetic-feedback-key" not in serialized
    assert "notes" not in InputEvent.__table__.columns
    assert "payload" not in InputEvent.__table__.columns
    assert links[0].event_id == event_row.id
    assert links[0].reason == "subjective feedback changed"


def test_two_feedback_events_coalesce_without_merging_their_audit(audit_database):
    action = _action(audit_database)
    action.log_feedback("owner", SubjectiveFeedbackInput(date=DAY, physical_fatigue=3))
    first = _rows(audit_database, InputEvent)[0]
    original = {
        column.name: getattr(first, column.name) for column in InputEvent.__table__.columns
    }
    action.log_feedback("owner", SubjectiveFeedbackInput(date=DAY, muscle_soreness=5))
    jobs = _rows(audit_database, AnalysisJob)
    events = _rows(audit_database, InputEvent)
    links = _rows(audit_database, AnalysisInvalidation)
    assert len(jobs) == 1 and jobs[0].input_revision == 2
    assert {row.input_revision for row in events} == {1, 2}
    assert len(links) == 2
    assert {row.job_id for row in links} == {jobs[0].id}
    assert {row.event_id for row in links} == {row.id for row in events}
    stored_first = next(row for row in events if row.id == first.id)
    assert {
        column.name: getattr(stored_first, column.name) for column in InputEvent.__table__.columns
    } == original


def test_job_event_and_link_roll_back_with_the_failed_feedback(audit_database, monkeypatch):
    original = InputEventAuditRepository.link

    def fail_after_link(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("synthetic failure after all audit rows were written")

    monkeypatch.setattr(InputEventAuditRepository, "link", fail_after_link)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        _action(audit_database).log_feedback(
            "owner", SubjectiveFeedbackInput(date=DAY, mental_state=3),
            idempotency_key="synthetic-rollback-key",
        )
    for model in (InputEvent, InputEventRequest, AnalysisInvalidation, AnalysisJob, SubjectiveFeedback, FeedbackRequest):
        assert _rows(audit_database, model) == []
    with audit_database() as db:
        assert db.get(User, "owner").analysis_input_revision == 0


def test_same_key_and_revision_are_isolated_by_user(audit_database):
    action = _action(audit_database)
    for user_id in ("owner", "other"):
        action.log_feedback(
            user_id, SubjectiveFeedbackInput(date=DAY, physical_fatigue=4),
            idempotency_key="synthetic-shared-key",
        )
    owner_event = _rows(audit_database, InputEvent)[0]
    other_event = _rows(audit_database, InputEvent, "other")[0]
    owner_job = _rows(audit_database, AnalysisJob)[0]
    other_job = _rows(audit_database, AnalysisJob, "other")[0]
    assert owner_event.id != other_event.id and owner_job.id != other_job.id
    with audit_database.begin() as db:
        audit = InputEventAuditRepository(db)
        assert audit.get("other", owner_event.id) is None
        assert audit.linked_job("other", owner_event.id, DAY, None) is None
        with pytest.raises(InputEventAuditError, match="same user"):
            audit.link("other", owner_event.id, db.get(AnalysisJob, other_job.id), reason="synthetic")
        with pytest.raises(InputEventAuditError, match="same user"):
            audit.link("owner", owner_event.id, db.get(AnalysisJob, other_job.id), reason="synthetic")
    assert len(_rows(audit_database, AnalysisInvalidation)) == 1
    assert len(_rows(audit_database, AnalysisInvalidation, "other")) == 1


def test_revision_deduplication_retains_all_request_aliases(audit_database):
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision("owner")
        job_id = _enqueue(repo, key="synthetic-request-1").id
        assert _enqueue(repo, key="synthetic-request-2").id == job_id
        assert _enqueue(repo).id == job_id
    assert len(_rows(audit_database, InputEvent)) == 1
    assert len(_rows(audit_database, InputEventRequest)) == 2
    assert len(_rows(audit_database, AnalysisInvalidation)) == 1
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        db.get(AnalysisJob, job_id).status = "succeeded"
        db.flush()
        repo.bump_analysis_input_revision("owner")
        assert _enqueue(repo, key="synthetic-request-2").id == job_id
    assert len(_rows(audit_database, InputEvent)) == 1
    assert len(_rows(audit_database, AnalysisJob)) == 1
    with audit_database() as db:
        assert db.get(AnalysisJob, job_id).input_revision == 1


def test_replaying_a_coalesced_request_after_completion_keeps_both_events(audit_database):
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision("owner")
        first_id = _enqueue(repo, key="synthetic-first-input").id
        repo.bump_analysis_input_revision("owner")
        assert _enqueue(repo, key="synthetic-second-input", streams={"workouts"}).id == first_id
        db.get(AnalysisJob, first_id).status = "succeeded"
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        assert _enqueue(repo, key="synthetic-second-input", streams={"workouts"}).id == first_id
    events = _rows(audit_database, InputEvent)
    links = _rows(audit_database, AnalysisInvalidation)
    assert len(events) == len(links) == 2
    assert {tuple(row.affected_streams) for row in events} == {("sleep",), ("workouts",)}
    assert {tuple(row.affected_streams) for row in links} == {("sleep",), ("workouts",)}
    assert len(_rows(audit_database, AnalysisJob)) == 1
    with pytest.raises(InputEventAuditError, match="another request"):
        with audit_database.begin() as db:
            _enqueue(HealthRepository(db), key="synthetic-second-input", streams={"sleep"})
    assert len(_rows(audit_database, InputEvent)) == 2


def test_direct_enqueue_helper_also_records_audit(audit_database):
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision("owner")
        first = repo._enqueue_analysis_job(
            "owner", DAY, event_type="profile", source="user", affected_streams={"profile"},
            idempotency_key="synthetic-profile-request", payload_ref="profile_revision:1",
        )
        assert repo._enqueue_analysis_job(
            "owner", DAY, event_type="profile", source="user", affected_streams={"profile"},
            idempotency_key="synthetic-profile-request", payload_ref="profile_revision:1",
        ).id == first.id
    events = _rows(audit_database, InputEvent)
    assert len(events) == len(_rows(audit_database, AnalysisInvalidation)) == 1
    assert events[0].payload_ref == "profile_revision:1"


def test_source_noop_produces_no_new_event(audit_database):
    daily = NormalizedDaily(user_id="owner", date=DAY, sleep=SleepRecord(
        user_id="owner", date=DAY, sleep_duration=440,
    ))
    with audit_database.begin() as db:
        HealthRepository(db).save_daily(daily)
    first_event_id = _rows(audit_database, InputEvent)[0].id
    with audit_database.begin() as db:
        HealthRepository(db).save_daily(daily)
    assert [row.id for row in _rows(audit_database, InputEvent)] == [first_event_id]
    with audit_database.begin() as db:
        HealthRepository(db).save_daily(daily.model_copy(update={
            "sleep": daily.sleep.model_copy(update={"sleep_duration": 460}),
        }))
    assert len(_rows(audit_database, InputEvent)) == 2
    assert len(_rows(audit_database, AnalysisJob)) == 1


def test_each_real_target_has_its_own_window_and_unmerged_direct_scope(audit_database):
    dependent = DAY + timedelta(days=7)
    with audit_database.begin() as db:
        _saved_target(db, "owner", dependent, "dependent")
        _saved_target(db, "owner", DAY - timedelta(days=1), "older")
        _saved_target(db, "owner", DAY + timedelta(days=180), "outside")
        _saved_target(db, "other", DAY + timedelta(days=14), "other-owner")
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision("owner")
        _enqueue(repo)
    events = _rows(audit_database, InputEvent)
    links = _rows(audit_database, AnalysisInvalidation)
    jobs = _rows(audit_database, AnalysisJob)
    assert len(events) == 1
    assert {row.target_date for row in jobs} == {DAY, dependent}
    assert {row.target_date for row in links} == {DAY, dependent}
    for link in links:
        assert link.event_id == events[0].id
        assert link.job_id in {job.id for job in jobs}
        assert link.direct_dates == [DAY.isoformat()]
        assert link.affected_streams == ["sleep"]
        assert link.dependency_start == link.target_date - timedelta(days=179)
        assert link.dependency_end == link.target_date
        assert link.reason == "synthetic source facts changed"
    assert _rows(audit_database, AnalysisInvalidation, "other") == []


@pytest.mark.parametrize("model,field,value", [
    (InputEvent, "payload_ref", "input_revision:999"),
    (InputEvent, "input_revision", 999),
    (AnalysisInvalidation, "reason", "synthetic attempted replacement"),
    (InputEventRequest, "request_hash", "a" * 64),
])
def test_orm_cannot_update_audit_records(audit_database, model, field, value):
    _action(audit_database).log_feedback("owner", SubjectiveFeedbackInput(date=DAY, mental_state=3))
    with audit_database() as db:
        row = db.scalar(select(model).where(model.user_id == "owner"))
        previous = getattr(row, field)
        setattr(row, field, value)
        with pytest.raises(InputEventAuditError, match="immutable"):
            db.flush()
        db.rollback()
        assert getattr(row, field) == previous


def test_in_place_scope_edits_are_also_rejected(audit_database):
    _action(audit_database).log_feedback("owner", SubjectiveFeedbackInput(date=DAY, mental_state=3))
    with audit_database() as db:
        row = db.scalar(select(InputEvent).where(InputEvent.user_id == "owner"))
        row.affected_dates.append((DAY + timedelta(days=1)).isoformat())
        with pytest.raises(InputEventAuditError, match="immutable"):
            db.flush()
        db.rollback()
        assert row.affected_dates == [DAY.isoformat()]


@pytest.mark.parametrize("model,field,value", [
    (InputEvent, "payload_ref", "input_revision:999"),
    (AnalysisInvalidation, "reason", "synthetic SQL replacement"),
    (InputEventRequest, "request_hash", "a" * 64),
])
def test_database_rejects_bulk_sql_audit_updates(audit_database, model, field, value):
    _action(audit_database).log_feedback("owner", SubjectiveFeedbackInput(date=DAY, mental_state=3))
    with pytest.raises(IntegrityError, match="immutable"):
        with audit_database.begin() as db:
            db.execute(update(model).where(model.user_id == "owner").values({field: value}))


def test_payload_references_are_opaque_and_explicit_time_is_utc(audit_database):
    occurred_at = datetime(2026, 9, 20, 8, tzinfo=timezone(timedelta(hours=8)))
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        db.info[ACTIVE_SOURCE_RECORD_ID] = "synthetic-journal-record"
        repo.bump_analysis_input_revision("owner")
        _enqueue(repo, occurred_at=occurred_at)
        repo.bump_analysis_input_revision("owner")
        _enqueue(repo, payload_ref="profile_revision:2")
    rows = sorted(_rows(audit_database, InputEvent), key=lambda row: row.input_revision)
    assert rows[0].payload_ref == "source_record:synthetic-journal-record"
    assert rows[0].occurred_at == datetime(2026, 9, 20)
    assert rows[1].payload_ref == "profile_revision:2"
    with pytest.raises(InputEventAuditError, match="opaque audit reference"):
        with audit_database.begin() as db:
            repo = HealthRepository(db)
            repo.bump_analysis_input_revision("owner")
            _enqueue(repo, payload_ref='{"notes":"synthetic private note"}')
    assert len(_rows(audit_database, InputEvent)) == 2


def test_running_job_coalescing_keeps_lease_and_priority(audit_database):
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision("owner")
        job_id = _enqueue(repo).id
    claim = SqlAnalysisJobRepository(audit_database).claim(60)
    assert claim.id == job_id
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision("owner")
        job = repo.enqueue_analysis_job(
            "owner", DAY, event_type="sync_completion", source="zepp",
            affected_streams={"workouts"}, reason="synthetic sync completed",
        )
        assert job.id == job_id and job.status == "running"
        assert job.lease_token == claim.token and job.lease_epoch == claim.epoch
        assert job.attempt_count == 1 and job.priority == 5
        assert job.input_revision == 2
    assert len(_rows(audit_database, InputEvent)) == 2
    assert {row.job_id for row in _rows(audit_database, AnalysisInvalidation)} == {job_id}


def test_audit_does_not_turn_distant_input_revision_into_global_fencing(audit_database):
    distant = DAY + timedelta(days=180)
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision("owner")
        _enqueue(repo)
        revision = repo.analysis_input_revision("owner")
        repo.bump_analysis_input_revision("owner")
        repo.enqueue_analysis_job(
            "owner", distant, event_type="source_sync", source="zepp",
            affected_dates={distant}, affected_streams={"sleep"},
        )
        assert repo.lock_analysis_scope("owner", revision, DAY)
        assert not repo.lock_analysis_scope("owner", revision, distant)
    assert {row.target_date for row in _rows(audit_database, AnalysisInvalidation)} == {DAY, distant}


def test_existing_user_deletion_cascades_audit_without_update_or_delete_guards(audit_database):
    _action(audit_database).log_feedback("owner", SubjectiveFeedbackInput(date=DAY, mental_state=3))
    with audit_database.begin() as db:
        HealthRepository(db).delete_for_user("owner")
    for model in (InputEvent, InputEventRequest, AnalysisInvalidation, AnalysisJob, SubjectiveFeedback):
        assert _rows(audit_database, model) == []
    with audit_database() as db:
        assert db.get(User, "owner") is None
        assert db.get(User, "other") is not None


def test_missing_owner_never_creates_audit_or_queue(audit_database):
    with audit_database.begin() as db:
        repo = HealthRepository(db)
        assert repo.enqueue_analysis_job("missing", DAY, event_type="feedback", source="user") is None
        assert repo._enqueue_analysis_job("missing", DAY, event_type="feedback", source="user") is None
        assert repo.enqueue_input_change(
            "missing", event_id="synthetic-missing-feedback", event_type="feedback", source="user",
            affected_dates={DAY}, affected_streams={"feedback"}, target_dates={DAY}, reason="synthetic",
        ) == []
        assert db.get(User, "missing") is None
        for model in (InputEvent, InputEventRequest, AnalysisInvalidation, AnalysisJob):
            assert db.scalar(select(func.count()).select_from(model)) == 0
