from vitalis.storage.database import get_engine


def test_sqlite_connections_wait_for_bounded_writer_lock():
    engine = get_engine()
    assert engine.dialect.name == "sqlite"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30_000
