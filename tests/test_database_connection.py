import pytest
from sqlalchemy.exc import IntegrityError

from vitalis.adapters.persistence import database


@pytest.fixture(params=["file", "memory"])
def sqlite_engine(request, tmp_path, monkeypatch):
    url = (
        f"sqlite:///{(tmp_path / 'connection.db').as_posix()}"
        if request.param == "file" else "sqlite:///:memory:"
    )
    engine = database._create_engine(url)
    monkeypatch.setattr(database, "_engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_sqlite_connections_wait_for_bounded_writer_lock(sqlite_engine):
    engine = database.get_engine()
    assert engine.dialect.name == "sqlite"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30_000
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        if engine.url.database != ":memory:":
            assert journal_mode.lower() == "wal"


def test_sqlite_wal_allows_writer_while_reader_transaction_is_open(tmp_path):
    engine = database._create_engine(f"sqlite:///{(tmp_path / 'wal.db').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE lock_probe (value INTEGER NOT NULL)")
            connection.exec_driver_sql("INSERT INTO lock_probe (value) VALUES (1)")
        reader = engine.connect()
        reader.exec_driver_sql("BEGIN")
        assert reader.exec_driver_sql("SELECT value FROM lock_probe").scalar_one() == 1
        try:
            with engine.begin() as writer:
                writer.exec_driver_sql("INSERT INTO lock_probe (value) VALUES (2)")
        finally:
            reader.rollback()
            reader.close()
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM lock_probe"
            ).scalar_one() == 2
    finally:
        engine.dispose()


def test_sqlite_get_engine_enforces_foreign_keys(sqlite_engine):
    engine = database.get_engine()
    database.init_db(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO devices (id, user_id, source, model, device_id, connected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("orphan", "missing-owner", "test", "", "", "2026-09-27 00:00:00"),
        )
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM devices").scalar_one() == 0
