"""SQLite current-schema migration tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from vitalis.storage.database import Base
from vitalis.storage.schema_migration import (
    UnsafeSchemaMigration,
    audit_sqlite_schema,
    migrate_sqlite_schema,
)
from vitalis.storage import models as orm


def _database():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_current_sqlite_schema_migration_is_noop():
    engine, _sessions = _database()

    report = migrate_sqlite_schema(engine)

    assert report.migrated_tables == ()
    assert report.row_counts == {}
    assert report.audit.clean


def test_known_legacy_tables_are_rebuilt_without_losing_rows():
    engine, _sessions = _database()
    with engine.begin() as db:
        db.exec_driver_sql("DROP TABLE workout_metric_samples")
        db.exec_driver_sql("""
            CREATE TABLE workout_metric_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(64) NOT NULL,
                workout_id VARCHAR(128) NOT NULL,
                timestamp DATETIME NOT NULL,
                metric VARCHAR(48) NOT NULL,
                value FLOAT NOT NULL,
                unit VARCHAR(24) NOT NULL,
                source_scope VARCHAR(32) NOT NULL,
                device_id VARCHAR(128),
                CONSTRAINT uq_workout_metric_sample
                    UNIQUE (user_id, workout_id, metric, timestamp)
            )
        """)
        db.execute(text("""
            INSERT INTO workout_metric_samples
                (user_id, workout_id, timestamp, metric, value, unit, source_scope, device_id)
            VALUES
                ('local-a', 'run-1', '2026-09-03 08:00:00', 'heart_rate', 145, 'bpm', 'workout_detail', NULL)
        """))

        db.exec_driver_sql("DROP TABLE daily_metrics")
        db.exec_driver_sql("""
            CREATE TABLE daily_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(64) NOT NULL,
                source VARCHAR(32) NOT NULL,
                date DATE NOT NULL,
                metric VARCHAR(64) NOT NULL,
                value FLOAT NOT NULL,
                unit VARCHAR(24) NOT NULL,
                source_scope VARCHAR(24) NOT NULL,
                device_id VARCHAR(128),
                CONSTRAINT uq_daily_metric
                    UNIQUE (user_id, source, date, metric)
            )
        """)
        db.execute(text("""
            INSERT INTO daily_metrics
                (user_id, source, date, metric, value, unit, source_scope, device_id)
            VALUES
                ('local-a', 'zepp', '2026-09-03', 'stress', 29, 'score', 'unknown', NULL)
        """))

        db.exec_driver_sql("DROP TABLE analysis_runs")
        db.exec_driver_sql("""
            CREATE TABLE analysis_runs (
                id VARCHAR(64) PRIMARY KEY,
                user_id VARCHAR(64) NOT NULL,
                target_date DATE NOT NULL,
                status VARCHAR(16) NOT NULL,
                started_at DATETIME NOT NULL,
                completed_at DATETIME,
                intelligence_version VARCHAR(32) NOT NULL,
                decision_policy_version VARCHAR(32) NOT NULL,
                evidence_version VARCHAR(32) NOT NULL,
                error VARCHAR(1024)
            )
        """)
        db.execute(text("""
            INSERT INTO analysis_runs
                (id, user_id, target_date, status, started_at,
                 intelligence_version, decision_policy_version, evidence_version)
            VALUES
                ('run-a', 'local-a', '2026-09-03', 'SUCCEEDED',
                 '2026-09-03 09:00:00', '10.0', '7.0', '2026-09a')
        """))

    audit = audit_sqlite_schema(engine)
    assert not audit.clean

    report = migrate_sqlite_schema(engine, legacy_source="zepp")

    assert report.audit.clean
    assert set(report.migrated_tables) == {
        "analysis_runs", "daily_metrics", "workout_metric_samples",
    }
    assert report.row_counts == {
        "analysis_runs": 1,
        "daily_metrics": 1,
        "workout_metric_samples": 1,
    }
    with engine.connect() as db:
        sample = db.execute(text("""
            SELECT source, user_id, workout_id, metric, value
            FROM workout_metric_samples
        """)).mappings().one()
        analysis = db.execute(text("""
            SELECT id, profile_revision_used FROM analysis_runs
        """)).mappings().one()
        daily = db.execute(text("""
            SELECT metric, value, device_id FROM daily_metrics
        """)).mappings().one()
        assert dict(sample) == {
            "source": "zepp",
            "user_id": "local-a",
            "workout_id": "run-1",
            "metric": "heart_rate",
            "value": 145.0,
        }
        assert dict(analysis) == {
            "id": "run-a",
            "profile_revision_used": 0,
        }
        assert dict(daily) == {
            "metric": "stress",
            "value": 29.0,
            "device_id": "",
        }
        assert db.execute(text("PRAGMA quick_check")).scalar_one() == "ok"


def test_audit_detects_same_name_index_with_wrong_definition():
    engine, _sessions = _database()
    with engine.begin() as db:
        db.exec_driver_sql("DROP INDEX ix_metric_samples_user_metric_timestamp")
        db.exec_driver_sql(
            "CREATE INDEX ix_metric_samples_user_metric_timestamp "
            "ON metric_samples (user_id)"
        )

    audit = audit_sqlite_schema(engine)
    drift = next(item for item in audit.tables if item.table == "metric_samples")
    assert drift.missing_indexes
    assert drift.extra_indexes

    assert migrate_sqlite_schema(engine).audit.clean


def test_missing_legacy_device_column_is_backfilled():
    engine, _sessions = _database()
    with engine.begin() as db:
        db.exec_driver_sql("DROP TABLE metric_samples")
        db.exec_driver_sql("""
            CREATE TABLE metric_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(64) NOT NULL,
                source VARCHAR(32) NOT NULL,
                metric VARCHAR(64) NOT NULL,
                timestamp DATETIME NOT NULL,
                value FLOAT NOT NULL,
                unit VARCHAR(24) NOT NULL,
                source_scope VARCHAR(24) NOT NULL
            )
        """)
        db.execute(text("""
            INSERT INTO metric_samples
                (user_id, source, metric, timestamp, value, unit, source_scope)
            VALUES
                ('local-a', 'zepp', 'stress', '2026-09-03 08:00:00', 29, 'score', 'unknown')
        """))

    assert migrate_sqlite_schema(engine).audit.clean
    with engine.connect() as db:
        assert db.execute(text(
            "SELECT device_id FROM metric_samples"
        )).scalar_one() == ""


def test_audit_detects_missing_foreign_key():
    engine, _sessions = _database()
    with engine.begin() as db:
        db.exec_driver_sql("DROP TABLE zepp_browser_links")
        db.exec_driver_sql("""
            CREATE TABLE zepp_browser_links (
                token_digest VARCHAR(64) PRIMARY KEY,
                user_id VARCHAR(64) NOT NULL,
                status VARCHAR(24) NOT NULL,
                message VARCHAR(512) NOT NULL,
                created_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL,
                last_verified_at DATETIME,
                last_sync_at DATETIME,
                revoked_at DATETIME,
                sync_attempt_id VARCHAR(64)
            )
        """)
        db.exec_driver_sql(
            "CREATE INDEX ix_zepp_browser_links_user_id ON zepp_browser_links (user_id)"
        )
        db.exec_driver_sql(
            "CREATE INDEX ix_zepp_browser_links_status ON zepp_browser_links (status)"
        )
        db.exec_driver_sql(
            "CREATE INDEX ix_zepp_browser_links_sync_attempt_id "
            "ON zepp_browser_links (sync_attempt_id)"
        )

    audit = audit_sqlite_schema(engine)
    drift = next(item for item in audit.tables if item.table == "zepp_browser_links")
    assert drift.missing_foreign_keys


def test_migration_refuses_unmanaged_table_trigger():
    engine, _sessions = _database()
    with engine.begin() as db:
        db.exec_driver_sql("""
            CREATE TRIGGER legacy_sleep_trigger
            AFTER INSERT ON sleep_records
            BEGIN
                SELECT 1;
            END
        """)

    audit = audit_sqlite_schema(engine)
    drift = next(item for item in audit.tables if item.table == "sleep_records")
    assert drift.unexpected_triggers == ("legacy_sleep_trigger",)
    with pytest.raises(UnsafeSchemaMigration, match="trigger"):
        migrate_sqlite_schema(engine)


def test_migration_refuses_unknown_extra_table():
    engine, _sessions = _database()
    with engine.begin() as db:
        db.exec_driver_sql("CREATE TABLE legacy_unknown (id INTEGER PRIMARY KEY)")

    audit = audit_sqlite_schema(engine)
    assert not audit.clean
    assert audit.extra_tables == ("legacy_unknown",)
    with pytest.raises(UnsafeSchemaMigration, match="未知表"):
        migrate_sqlite_schema(engine)


def test_migration_refuses_nonempty_drifted_sync_ledger():
    engine, sessions = _database()
    now = datetime(2026, 9, 4, 8, 0)
    with sessions.begin() as db:
        db.add(orm.SyncAttempt(
            id="legacy-attempt",
            user_id="local-a",
            source="zepp",
            trigger="manual",
            plan_version="zepp-sync-v3",
            request_key="legacy-request",
            window_start=now,
            window_end=now + timedelta(days=1),
            timezone="Asia/Shanghai",
            options={},
            status="queued",
        ))
    with engine.begin() as db:
        db.exec_driver_sql("DROP INDEX ix_sync_attempts_request_key")

    with pytest.raises(UnsafeSchemaMigration, match="sync_attempts"):
        migrate_sqlite_schema(engine)

    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM sync_attempts")).scalar_one() == 1
