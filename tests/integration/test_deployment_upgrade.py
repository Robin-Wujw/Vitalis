"""Explicit deployment upgrade preserves synthetic data and delivery history."""
from datetime import date, datetime
import sqlite3

import pytest
from sqlalchemy import Column, ForeignKey, MetaData, Table, UniqueConstraint, create_engine

from tools.upgrade_deployment_db import (
    AFFECTED, SOURCE_REVISION, upgrade,
)
from vitalis.adapters.persistence.database import Base, SCHEMA_REVISION, check_schema, init_db
from vitalis.adapters.persistence import models


DAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 10)


def _old_database(path):
    metadata = MetaData()
    for name, table in Base.metadata.tables.items():
        if name not in AFFECTED:
            table.to_metadata(metadata)
    for name, old_columns in AFFECTED.items():
        current = Base.metadata.tables[name]
        columns = []
        for column in current.columns:
            if column.name not in old_columns:
                continue
            foreign_keys = [ForeignKey(key.target_fullname) for key in column.foreign_keys]
            columns.append(Column(
                column.name, column.type, *foreign_keys,
                primary_key=column.primary_key,
                nullable=False if name == "strength_exercises" and column.name == "created_at" else column.nullable,
            ))
        unique = [UniqueConstraint(*[column.name for column in constraint.columns]) for constraint in current.constraints if isinstance(constraint, UniqueConstraint)]
        Table(name, metadata, *columns, *unique)
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(metadata.tables["vitalis_schema"].insert().values(id=1, revision=SOURCE_REVISION))
        db.execute(models.User.__table__.insert().values(id="synthetic-upgrade", name="Synthetic upgrade"))
        db.execute(models.AnalysisRun.__table__.insert().values(
            id="synthetic-run", user_id="synthetic-upgrade", target_date=DAY,
            status="SUCCEEDED", started_at=NOW, completed_at=NOW,
            intelligence_version="14.0", decision_policy_version="9.0", evidence_version="2026-09a",
        ))
        db.execute(models.AnalysisSnapshot.__table__.insert().values(
            id="synthetic-snapshot", analysis_run_id="synthetic-run", user_id="synthetic-upgrade",
            profile_type="daily", period_start=DAY, period_end=DAY,
            schema_version="14.0", intelligence_version="14.0", decision_policy_version="9.0",
            evidence_version="2026-09a", payload={"synthetic": True, "sleep_minutes": 420},
        ))
        db.execute(metadata.tables["strength_exercises"].insert().values(
            id="synthetic-exercise", user_id="synthetic-upgrade", workout_source="zepp",
            workout_id="synthetic-workout", order=1, exercise_name="Synthetic curl",
            movement_pattern="isolation", movement_pattern_label="Synthetic pattern",
            muscle_groups=["biceps"], muscle_group_labels=["Synthetic muscle"],
            sets=3, repetitions=12, weight_kg=10, source="user_confirmed",
            confidence="HIGH", confidence_label="Synthetic confidence", created_at=NOW,
        ))
        for index, status in enumerate(("succeeded", "running", "deferred")):
            db.execute(metadata.tables["notification_deliveries"].insert().values(
                id=f"synthetic-delivery-{index}", user_id="synthetic-upgrade", analysis_run_id="synthetic-run",
                target_date=DAY, period=("morning", "evening", "weekly")[index], status=status,
                attempt_count=1, lease_token="synthetic-lease" if status == "running" else None,
                lease_expires_at=NOW if status == "running" else None,
                created_at=NOW, updated_at=NOW,
            ))
    engine.dispose()


def test_known_upgrade_is_atomic_preserves_records_and_does_not_resend(tmp_path):
    database, backup = tmp_path / "deployment.sqlite", tmp_path / "backup.sqlite"
    _old_database(database)
    before = database.read_bytes()
    assert upgrade(database, backup)["status"] == "ready"
    assert database.read_bytes() == before and not backup.exists()
    result = upgrade(database, backup, apply=True)
    assert result["status"] == "upgraded" and result["records_preserved"]
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        check_schema(engine)
    finally:
        engine.dispose()
    with sqlite3.connect(database) as db:
        assert dict(db.execute("SELECT id,status FROM notification_deliveries")) == {
            "synthetic-delivery-0": "accepted", "synthetic-delivery-1": "uncertain", "synthetic-delivery-2": "deferred",
        }
        assert db.execute("SELECT weight_kg,weight_unit,weight_basis FROM strength_exercises").fetchone() == (10.0, "kg", None)
        assert db.execute("SELECT COUNT(*) FROM analysis_snapshots").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
        assert db.execute("SELECT status,provider_id FROM notification_deliveries WHERE id='synthetic-delivery-0'").fetchone() == ("accepted", None)
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT revision FROM vitalis_schema").fetchone()[0] == SOURCE_REVISION
        assert db.execute("SELECT status FROM notification_deliveries WHERE id='synthetic-delivery-0'").fetchone()[0] == "succeeded"
    assert upgrade(database, tmp_path / "unused.sqlite", apply=True)["status"] == "already_current"


def test_existing_backup_is_never_overwritten(tmp_path):
    database, backup = tmp_path / "deployment.sqlite", tmp_path / "backup.sqlite"
    _old_database(database)
    backup.write_bytes(b"existing protected backup")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="backup must be a new path"):
        upgrade(database, backup, apply=True)
    assert database.read_bytes() == before and backup.read_bytes() == b"existing protected backup"


def test_unknown_revision_is_refused_without_changing_source(tmp_path):
    database, backup = tmp_path / "deployment.sqlite", tmp_path / "backup.sqlite"
    _old_database(database)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE vitalis_schema SET revision='unknown-source'")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="verified calendar-report"):
        upgrade(database, backup, apply=True)
    assert database.read_bytes() == before and not backup.exists()


def test_unexpected_table_shape_is_refused(tmp_path):
    database, backup = tmp_path / "deployment.sqlite", tmp_path / "backup.sqlite"
    _old_database(database)
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE strength_exercises ADD COLUMN unexpected TEXT")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="known deployment schema"):
        upgrade(database, backup, apply=True)
    assert database.read_bytes() == before and not backup.exists()
