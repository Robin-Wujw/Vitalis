"""Deleting a local owner must respect enforced job/run foreign keys."""

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import AnalysisJob, AnalysisRun, User
from vitalis.adapters.persistence.repositories import HealthRepository


def test_delete_owner_removes_job_before_its_analysis_run_with_fk_enabled(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'owner.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    try:
        with Session(engine) as db:
            with db.begin():
                db.add(User(id="synthetic-owner"))
                db.add(AnalysisRun(
                    id="run-1", user_id="synthetic-owner", target_date=date(2026, 9, 26),
                    status="SUCCEEDED", started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    intelligence_version="current", decision_policy_version="current",
                    evidence_version="current",
                ))
                db.flush()
                db.add(AnalysisJob(
                    id="job-1", user_id="synthetic-owner", target_date=date(2026, 9, 26),
                    status="succeeded", idempotency_key="synthetic-key",
                    request_hash="a" * 64, run_id="run-1",
                ))
        with Session(engine) as db:
            with db.begin():
                HealthRepository(db).delete_for_user("synthetic-owner")
        with Session(engine) as db:
            assert db.get(User, "synthetic-owner") is None
            assert db.execute(select(AnalysisJob)).first() is None
            assert db.execute(select(AnalysisRun)).first() is None
    finally:
        engine.dispose()
