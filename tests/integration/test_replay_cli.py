"""The replay command is an explicit, local-only operation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vitalis.adapters.persistence.database import _create_engine, init_db
from vitalis.adapters.persistence.models import NotificationDelivery, SyncAttempt, User
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.persistence.source_journal import SourceJournalRepository, SourceRecordInput


ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "replay" / "phase3.json"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 2, tzinfo=timezone.utc)
FETCHED = datetime(2026, 9, 2, 1, tzinfo=timezone.utc)


def _cli(*args, database_url: str, env: str = "test"):
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "VITALIS_ENV": env,
        "ZEPP_MOCK": "true" if env in {"dev", "test"} else "false",
    }
    return subprocess.run(
        [sys.executable, "-m", "vitalis", *map(str, args)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_fixture_replay_cli_writes_only_replay_metadata(tmp_path):
    target = tmp_path / "fixture-replay.db"
    engine = _create_engine(f"sqlite:///{target.as_posix()}")
    init_db(engine)
    engine.dispose()

    result = _cli(
        "replay", "fixture", "--input", FIXTURE, "--target-user", "cli-fixture-target",
        database_url=f"sqlite:///{target.as_posix()}",
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["user_id"] == "cli-fixture-target"
    assert output["source_mode"] == "replay"
    assert output["records_replayed"] == 9
    assert "synthetic-balance-hr" not in result.stdout
    assert "synthetic-balance-hr" not in result.stderr

    with Session(create_engine(f"sqlite:///{target.as_posix()}")) as db:
        assert db.get(User, "cli-fixture-target").source_mode == "replay"
        assert db.scalars(select(SyncAttempt)).all() == []
        assert db.scalars(select(NotificationDelivery)).all() == []


def test_source_journal_replay_cli_uses_an_independent_target(tmp_path):
    target = tmp_path / "journal-replay.db"
    engine = _create_engine(f"sqlite:///{target.as_posix()}")
    init_db(engine)
    with Session(engine) as db, db.begin():
        repo = HealthRepository(db)
        repo.upsert_user("cli-source")
        repo.bind_source_mode("cli-source", "mock")
        SourceJournalRepository(db).append(SourceRecordInput(
            user_id="cli-source", source="zepp", source_mode="mock", stream="heart_rate",
            record_id="heart_rate:cli", observed_start=START, observed_end=END,
            fetched_at=FETCHED, payload={"items": [{
                "sample_id": "cli-sample", "timestamp": START.isoformat(),
                "value": 72, "deviceId": "SYNTHBALANCE2",
            }]},
        ))
    engine.dispose()

    result = _cli(
        "replay", "source-journal", "--source-user", "cli-source",
        "--target-user", "cli-journal-target", "--start-date", "2026-09-01",
        "--end-date", "2026-09-01", "--as-of", "2026-09-03T00:00:00Z",
        database_url=f"sqlite:///{target.as_posix()}",
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["user_id"] == "cli-journal-target"
    assert output["records_replayed"] == 1
    assert "cli-sample" not in result.stdout
    assert "cli-sample" not in result.stderr


def test_replay_cli_rejects_production_before_reading_fixture(tmp_path):
    target = tmp_path / "production-replay.db"
    fixture = tmp_path / "contains-secret.json"
    fixture.write_text('{"payload":"synthetic-private-marker"}', encoding="utf-8")
    result = _cli(
        "replay", "fixture", "--input", fixture, "--target-user", "blocked",
        database_url=f"sqlite:///{target.as_posix()}", env="prod",
    )
    assert result.returncode == 1
    assert "offline replay" in result.stderr
    assert "synthetic-private-marker" not in result.stdout
    assert "synthetic-private-marker" not in result.stderr
    assert not target.exists()
