"""Fresh-database checks never operate on the user's configured database."""

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text

from vitalis.adapters.persistence.database import (
    SCHEMA_REVISION,
    SchemaMismatch,
    _index_predicate,
    check_schema,
    init_db,
)


def test_startup_schema_check_does_not_create_a_database_file(tmp_path):
    path = tmp_path / "must-be-initialized.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with pytest.raises(SchemaMismatch, match="vitalis db init"):
            check_schema(engine)
        assert not path.exists()
    finally:
        engine.dispose()


def test_fresh_database_initializes_once_and_keeps_revision(tmp_path):
    path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        init_db(engine)
        init_db(engine)
        with engine.connect() as db:
            assert db.execute(text("SELECT revision FROM vitalis_schema WHERE id=1")).scalar_one() == SCHEMA_REVISION
        assert "workouts" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_previous_delivery_schema_is_refused_without_modifying_data(tmp_path):
    path = tmp_path / "previous-delivery.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        init_db(engine)
        with engine.begin() as db:
            db.execute(text(
                "UPDATE vitalis_schema SET revision='2026-10-cloud-zepp-only'"
            ))
        original = path.read_bytes()
        with pytest.raises(SchemaMismatch, match="版本不匹配"):
            check_schema(engine)
        with pytest.raises(SchemaMismatch, match="版本不匹配"):
            init_db(engine)
        assert path.read_bytes() == original
    finally:
        engine.dispose()


def test_existing_unversioned_database_is_not_modified(tmp_path):
    path = tmp_path / "previous.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO users VALUES ('owner', 'existing data')")
    original = path.read_bytes()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with pytest.raises(SchemaMismatch, match="当前 Vitalis schema"):
            init_db(engine)
        with engine.connect() as db:
            assert db.execute(text("SELECT name FROM users WHERE id='owner'")).scalar_one() == "existing data"
            assert inspect(engine).get_table_names() == ["users"]
    finally:
        engine.dispose()
    assert path.read_bytes() == original


def test_unknown_revision_and_column_drift_refuse_writes(tmp_path):
    path = tmp_path / "changed.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        init_db(engine)
        with engine.begin() as db:
            db.execute(text("UPDATE vitalis_schema SET revision='unknown'"))
        with pytest.raises(SchemaMismatch, match="版本不匹配"):
            init_db(engine)
        with engine.begin() as db:
            db.execute(text("UPDATE vitalis_schema SET revision=:revision"), {"revision": SCHEMA_REVISION})
            db.execute(text("ALTER TABLE workouts ADD COLUMN surprise TEXT"))
        with pytest.raises(SchemaMismatch, match="workouts"):
            init_db(engine)
    finally:
        engine.dispose()


def test_unknown_extra_table_is_never_silently_accepted(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'extra.db').as_posix()}")
    try:
        init_db(engine)
        with engine.begin() as db:
            db.execute(text("CREATE TABLE unrelated_history (id INTEGER PRIMARY KEY)"))
            db.execute(text("INSERT INTO unrelated_history VALUES (42)"))
        with pytest.raises(SchemaMismatch, match="当前 Vitalis schema"):
            init_db(engine)
        with engine.connect() as db:
            assert db.execute(text("SELECT id FROM unrelated_history")).scalar_one() == 42
    finally:
        engine.dispose()


def test_missing_foreign_key_is_detected_without_migration(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'missing-fk.db').as_posix()}")
    try:
        init_db(engine)
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
            for name, column in (
                ("user_id", "user_id"), ("status", "status"),
                ("sync_attempt_id", "sync_attempt_id"),
            ):
                db.exec_driver_sql(
                    f"CREATE INDEX ix_zepp_browser_links_{name} ON zepp_browser_links ({column})"
                )
        assert not inspect(engine).get_foreign_keys("zepp_browser_links")
        with pytest.raises(SchemaMismatch, match="zepp_browser_links"):
            init_db(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("index_name", "replacement"),
    [
        (
            "uq_sync_attempt_running",
            (
                "CREATE INDEX uq_sync_attempt_running ON sync_attempts (user_id, source) "
                "WHERE status = 'running'"
            ),
        ),
        (
            "uq_sync_attempt_running",
            (
                "CREATE UNIQUE INDEX uq_sync_attempt_running ON sync_attempts (user_id, source) "
                "WHERE status = 'queued'"
            ),
        ),
        (
            "uq_sync_attempt_running",
            "CREATE UNIQUE INDEX uq_sync_attempt_running ON sync_attempts (user_id, source)",
        ),
        (
            "ix_sync_attempts_lease",
            "CREATE UNIQUE INDEX ix_sync_attempts_lease ON sync_attempts (status, lease_expires_at)",
        ),
    ],
    ids=["nonunique-partial", "wrong-predicate", "missing-predicate", "unexpected-unique"],
)
def test_index_drift_refuses_writes(tmp_path, index_name, replacement):
    path = tmp_path / "index-drift.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        init_db(engine)
        with engine.begin() as db:
            db.exec_driver_sql(f"DROP INDEX {index_name}")
            db.exec_driver_sql(replacement)
        before = path.read_bytes()

        with pytest.raises(SchemaMismatch, match="sync_attempts"):
            check_schema(engine)
        with pytest.raises(SchemaMismatch, match="sync_attempts"):
            init_db(engine)
        assert path.read_bytes() == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("declared", "reflected", "matches"),
    [
        ("status = 'running'", "((status)::text = 'running'::text)", True),
        (
            "status IN ('queued', 'running', 'retry_wait')",
            (
                "((status)::text = ANY ((ARRAY['queued'::character varying, "
                "'running'::character varying, 'retry_wait'::character varying])::text[]))"
            ),
            True,
        ),
        (
            "trigger = 'manual' AND trigger_ref LIKE 'api:%'",
            (
                "(((trigger)::text = 'manual'::text) AND "
                "((trigger_ref)::text ~~ 'api:%'::text))"
            ),
            True,
        ),
        ("status = 'running'", "((status)::text = 'queued'::text)", False),
        (
            "status = 'running' AND (source = 'zepp' OR source = 'other')",
            "status = 'running' AND source = 'zepp' OR source = 'other'",
            False,
        ),
    ],
)
def test_postgresql_reflected_index_predicates(declared, reflected, matches):
    assert (
        _index_predicate(declared, "postgresql") == _index_predicate(reflected, "postgresql")
    ) is matches
