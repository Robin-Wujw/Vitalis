"""Durable analysis requests use fresh schema and never touch a configured DB."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Event
from time import sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence.analysis_jobs import SqlAnalysisJobRepository
from vitalis.application import jobs
from vitalis import bootstrap
from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence import database
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import AnalysisJob, AnalysisRun, User


DAY = date(2026, 8, 29)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def job_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'analysis-jobs.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(jobs, "_repository", SqlAnalysisJobRepository(factory))
    monkeypatch.setattr(jobs, "_runner", lambda *_: pytest.fail("unexpected analysis run"))
    with factory.begin() as db:
        db.add_all([User(id="owner"), User(id="other")])
    try:
        yield factory
    finally:
        engine.dispose()


def _record_run(factory, user_id="owner", day=DAY):
    run_id = uuid4().hex
    with factory.begin() as db:
        db.add(AnalysisRun(
            id=run_id, user_id=user_id, target_date=day,
            status="succeeded", started_at=_utc_now(),
            completed_at=_utc_now(), intelligence_version="test",
            decision_policy_version="test", evidence_version="test",
        ))
    return run_id


def _succeed_stub(factory, claim, run_id):
    with factory.begin() as db:
        assert SqlAnalysisJobRepository(factory).succeed(db, claim, run_id)
    return run_id


def test_idempotent_enqueue_is_user_scoped_and_collisions_rejected(job_database):
    first = jobs.create_analysis_job("owner", DAY, "request-1")
    assert jobs.create_analysis_job("owner", DAY, "request-1") == first
    assert jobs.create_analysis_job("other", DAY, "request-1") != first
    assert jobs.get_analysis_job("other", first) is None
    state = jobs.get_analysis_job("owner", first)
    assert state.id == first and state.target_date == DAY and state.status == "queued"
    assert state.run_id is None and state.attempt_count == 0
    with pytest.raises(jobs.IdempotencyConflict):
        jobs.create_analysis_job("owner", DAY + timedelta(days=1), "request-1")
    with job_database() as db:
        assert db.query(AnalysisJob).filter_by(user_id="owner").count() == 1


def test_enqueue_requires_existing_user_and_valid_input(job_database):
    with pytest.raises(ValueError, match="user not found"):
        jobs.create_analysis_job("missing", DAY, "request")
    with pytest.raises(ValueError, match="day must be a date"):
        jobs.create_analysis_job("owner", datetime(2026, 8, 29), "request")
    with pytest.raises(ValueError, match="invalid idempotency key"):
        jobs.create_analysis_job("owner", DAY, " ")
    with job_database() as db:
        assert db.query(AnalysisJob).count() == 0


def test_parallel_enqueues_share_one_persistent_request(job_database):
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(
            lambda _: jobs.create_analysis_job("owner", DAY, "parallel-key"),
            range(6),
        ))
    assert len(set(ids)) == 1
    with job_database() as db:
        assert db.query(AnalysisJob).filter_by(user_id="owner").count() == 1


def test_parallel_claims_have_one_owner(job_database):
    job_id = jobs.create_analysis_job("owner", DAY, "parallel-claim")
    repository = SqlAnalysisJobRepository(job_database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.claim(10), range(2)))
    claims = [claim for claim in results if claim is not None]
    assert len(claims) == 1 and claims[0].id == job_id
    assert jobs.get_analysis_job("owner", job_id).attempt_count == 1


def test_drain_limits_one_pass_to_four_jobs(job_database, monkeypatch):
    job_ids = [jobs.create_analysis_job("owner", DAY, f"batch-{i}") for i in range(5)]
    run_id = _record_run(job_database)
    monkeypatch.setattr(jobs, "_runner", lambda claim: _succeed_stub(job_database, claim, run_id))

    assert jobs.drain_analysis_jobs(max_jobs=50) == 4
    states = [jobs.get_analysis_job("owner", job_id) for job_id in job_ids]
    assert [state.status for state in states].count("succeeded") == 4
    assert [state.status for state in states].count("queued") == 1
    assert jobs.drain_analysis_jobs(max_jobs=50) == 1


def test_analysis_runs_once_and_persists_result_id(job_database, monkeypatch):
    job_id = jobs.create_analysis_job("owner", DAY, "request-success")
    run_id = _record_run(job_database)
    calls = []

    def run(claim):
        calls.append((claim.user_id, claim.target_date))
        return _succeed_stub(job_database, claim, run_id)

    monkeypatch.setattr(jobs, "_runner", run)
    assert jobs.drain_analysis_jobs(max_jobs=1) == 1
    assert jobs.drain_analysis_jobs(max_jobs=1) == 0
    state = jobs.get_analysis_job("owner", job_id)
    assert calls == [("owner", DAY)]
    assert state.status == "succeeded" and state.run_id == run_id
    assert state.attempt_count == 1 and state.finished_at is not None
    assert jobs.create_analysis_job("owner", DAY, "request-success") == job_id


def test_failure_is_persisted_without_leaking_exception(job_database, monkeypatch):
    job_id = jobs.create_analysis_job("owner", DAY, "request-failure")

    def run(_claim):
        raise RuntimeError("private medical value or credential")

    monkeypatch.setattr(jobs, "_runner", run)
    assert jobs.drain_analysis_jobs() == 1
    state = jobs.get_analysis_job("owner", job_id)
    assert state.status == "failed" and state.run_id is None
    assert state.error == "Analysis failed" and state.finished_at is not None
    assert jobs.drain_analysis_jobs() == 0


def test_competing_workers_do_not_execute_same_live_claim(job_database, monkeypatch):
    job_id = jobs.create_analysis_job("owner", DAY, "request-race")
    run_id = _record_run(job_database)
    entered, release = Event(), Event()
    calls = []

    def run(claim):
        calls.append((claim.user_id, claim.target_date))
        entered.set()
        assert release.wait(5)
        return _succeed_stub(job_database, claim, run_id)

    monkeypatch.setattr(jobs, "_runner", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(jobs.drain_analysis_jobs)
        try:
            assert entered.wait(5)
            second = pool.submit(jobs.drain_analysis_jobs)
            assert second.result(timeout=5) == 0
        finally:
            release.set()
        assert first.result(timeout=5) == 1
    assert calls == [("owner", DAY)]
    assert jobs.get_analysis_job("owner", job_id).status == "succeeded"


def test_long_analysis_renews_lease_while_work_is_outside_job_transaction(job_database, monkeypatch):
    job_id = jobs.create_analysis_job("owner", DAY, "request-renewal")
    run_id = _record_run(job_database)
    entered, release = Event(), Event()

    def run(claim):
        entered.set()
        assert release.wait(8)
        return _succeed_stub(job_database, claim, run_id)

    monkeypatch.setattr(jobs, "_runner", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(jobs.drain_analysis_jobs, lease_seconds=2)
        try:
            assert entered.wait(5)
            sleep(2.3)
            assert jobs.drain_analysis_jobs(lease_seconds=2) == 0
        finally:
            release.set()
        assert first.result(timeout=5) == 1
    state = jobs.get_analysis_job("owner", job_id)
    assert state.status == "succeeded" and state.attempt_count == 1


def test_expired_lease_reclaims_with_new_epoch_and_fences_old_owner(job_database):
    job_id = jobs.create_analysis_job("owner", DAY, "request-expired")
    repository = SqlAnalysisJobRepository(job_database)
    old_claim = repository.claim(2)
    assert old_claim.id == job_id
    with job_database.begin() as db:
        row = db.get(AnalysisJob, job_id)
        row.lease_expires_at = _utc_now() - timedelta(seconds=1)

    new_claim = repository.claim(2)
    assert new_claim.id == job_id
    assert new_claim.token != old_claim.token
    assert new_claim.epoch == old_claim.epoch + 1
    assert not repository.finish(old_claim, error="Analysis failed")
    assert jobs.get_analysis_job("owner", job_id).status == "running"
    assert repository.finish(new_claim, error="Analysis failed")
    state = jobs.get_analysis_job("owner", job_id)
    assert state.status == "failed" and state.attempt_count == 2


def test_expired_token_cannot_finalize_without_reclaim(job_database):
    job_id = jobs.create_analysis_job("owner", DAY, "request-expired-unclaimed")
    repository = SqlAnalysisJobRepository(job_database)
    claim = repository.claim(2)
    with job_database.begin() as db:
        db.get(AnalysisJob, job_id).lease_expires_at = _utc_now() - timedelta(seconds=1)
    assert not repository.finish(claim, error="Analysis failed")
    assert jobs.get_analysis_job("owner", job_id).status == "running"


def test_reclaim_closes_only_the_expired_claims_running_run(job_database):
    job_id = jobs.create_analysis_job("owner", DAY, "request-reclaim-run")
    repository = SqlAnalysisJobRepository(job_database)
    old_claim = repository.claim(2)
    run_id = uuid4().hex
    with job_database.begin() as db:
        db.add(AnalysisRun(
            id=run_id, user_id="owner", target_date=DAY,
            status="RUNNING", started_at=_utc_now(),
            intelligence_version="test", decision_policy_version="test",
            evidence_version="test",
        ))
        assert repository.attach_run(db, old_claim, run_id)
        db.get(AnalysisJob, job_id).lease_expires_at = _utc_now() - timedelta(seconds=1)

    new_claim = repository.claim(2)
    assert new_claim.epoch == old_claim.epoch + 1
    with job_database() as db:
        run = db.get(AnalysisRun, run_id)
        job = db.get(AnalysisJob, job_id)
        assert run.status == "FAILED"
        assert run.error == "analysis_lease_reclaimed"
        assert job.run_id is None
        assert job.status == "running"


def test_real_analysis_command_creates_run_and_snapshots(job_database, monkeypatch):
    # The real command's session_scope must use the same isolated database as the job.
    monkeypatch.setattr(database, "SessionLocal", job_database)
    bootstrap.configure_analysis_jobs()
    user_id = f"job-real-{uuid4().hex}"
    with session_scope() as db:
        HealthRepository(db).upsert_user(user_id)
    key = uuid4().hex
    job_id = jobs.create_analysis_job(user_id, DAY, key)

    assert jobs.drain_analysis_jobs(max_jobs=1) == 1
    state = jobs.get_analysis_job(user_id, job_id)
    assert state.status == "succeeded" and state.run_id is not None
    with session_scope() as db:
        run = db.get(AnalysisRun, state.run_id)
        assert run.user_id == user_id and run.target_date == DAY
        assert run.status == "SUCCEEDED" or run.status == "succeeded"
        from vitalis.adapters.persistence.models import AnalysisSnapshot
        assert db.query(AnalysisSnapshot).filter_by(analysis_run_id=state.run_id).count() >= 3
    assert jobs.create_analysis_job(user_id, DAY, key) == job_id
    assert jobs.drain_analysis_jobs(max_jobs=1) == 0
    with session_scope() as db:
        assert db.query(AnalysisRun).filter_by(user_id=user_id).count() == 1
