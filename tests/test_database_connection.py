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
