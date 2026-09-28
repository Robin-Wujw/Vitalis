from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import SyncAttempt
from vitalis.adapters.persistence.repositories import HealthRepository, SyncIdempotencyConflict


def test_api_sync_key_survives_terminal_status_and_rejects_changed_request(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'sync-key.db').as_posix()}")
    init_db(engine)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    kwargs = {
        "user_id": "owner", "source": "zepp", "trigger": "manual",
        "trigger_ref": "api:synthetic-request-key", "plan_version": "current",
        "window_start": start, "window_end": start + timedelta(days=1),
        "timezone_name": "UTC", "options": {}, "manifest": [],
    }
    try:
        with Session(engine) as db:
            first = HealthRepository(db).create_or_reuse_sync_attempt(**kwargs)
            job_id = first.id
            db.commit()
        with Session(engine) as db:
            row = db.get(SyncAttempt, job_id)
            row.status = "succeeded"
            db.commit()
        with Session(engine) as db:
            assert HealthRepository(db).create_or_reuse_sync_attempt(**kwargs).id == job_id
            with pytest.raises(SyncIdempotencyConflict):
                HealthRepository(db).create_or_reuse_sync_attempt(
                    **{**kwargs, "window_end": start + timedelta(days=2)}
                )
            assert db.execute(select(SyncAttempt).where(SyncAttempt.user_id == "owner")).scalars().all()
    finally:
        engine.dispose()
