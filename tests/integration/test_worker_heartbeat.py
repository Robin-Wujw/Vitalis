"""Worker activity is a separate signal from API/schema readiness."""

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.entrypoints.cli import _doctor
from vitalis.config import settings
from vitalis.scheduler.jobs import worker_heartbeat_job
from vitalis.adapters.persistence import database
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import WorkerHeartbeat


def test_doctor_reports_unseen_live_and_stale_worker(tmp_path, monkeypatch, capsys):
    path = tmp_path / "heartbeat.db"
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url)
    init_db(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    try:
        assert _doctor() == 0
        assert json.loads(capsys.readouterr().out)["worker"] == "not_seen"

        worker_heartbeat_job()
        assert _doctor() == 0
        ready = json.loads(capsys.readouterr().out)
        assert ready["worker"] == "alive"
        assert ready["worker_last_seen_at"].endswith("Z")

        with sessions.begin() as db:
            db.get(WorkerHeartbeat, "primary").last_seen_at = (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
            )
        assert _doctor() == 0
        assert json.loads(capsys.readouterr().out)["worker"] == "stale"
    finally:
        engine.dispose()
