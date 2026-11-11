"""Analysis reads compact band facts while the explicit API keeps minute arrays."""
from datetime import UTC, date, datetime

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker
import pytest

from vitalis.adapters.persistence.band_sources import BandSourceRecord
from vitalis.adapters.persistence.database import _create_engine, init_db
from vitalis.adapters.persistence.health_reader import SqlHealthReader
from vitalis.adapters.persistence.repositories import HealthRepository


def test_analysis_projects_activity_arrays_out_in_sql_but_api_preserves_them(tmp_path):
    engine = _create_engine(f"sqlite:///{(tmp_path / 'band-projection.db').as_posix()}")
    init_db(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    day, stamp = date(2026, 9, 1), datetime(2026, 9, 2, tzinfo=UTC)
    owner = "synthetic-band-projection"
    activity = {"minute_steps": [1] * 1440, "hourly_steps": [60] * 24,
                "qualification": "reconciled_day_and_stages"}
    try:
        with sessions.begin() as db:
            HealthRepository(db).upsert_user(owner)
            db.flush()
            db.add(BandSourceRecord(id="synthetic-band", user_id=owner, date=day, kind="activity",
                source="zepp", source_scope="device", device_id="SYNTHETICWATCH",
                fetched_at=stamp.replace(tzinfo=None), source_record_id=None,
                data={"steps": 1440, "source": "zepp", "band_activity": activity}))
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        with sessions() as db:
            rows = HealthRepository(db).band_source_records(owner, day, day, as_of=stamp)
        assert rows[0]["data"]["steps"] == 1440
        assert "band_activity" not in rows[0]["data"]
        assert rows[0]["data"]["fetched_at"] == stamp.isoformat()
        assert len(statements) == 1 and "json_remove(" in statements[0]
        event.remove(engine, "before_cursor_execute", capture)
        full = SqlHealthReader(sessions).activity_records(owner, day, day, as_of=stamp,
            include_minutes=True, include_hourly=True)
        assert full[0]["data"]["band_activity"] == activity
    finally:
        engine.dispose()


@pytest.mark.parametrize("kind,method", [("sleep", "sleep_records"), ("naps", "nap_records")])
def test_sleep_and_nap_reads_filter_kind_and_arrays_in_sql(tmp_path, kind, method):
    engine = _create_engine(f"sqlite:///{(tmp_path / 'band-kind.db').as_posix()}")
    init_db(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    day, stamp = date(2026, 9, 1), datetime(2026, 9, 2, tzinfo=UTC)
    try:
        with sessions.begin() as db:
            HealthRepository(db).upsert_user("synthetic-band-kind")
            db.flush()
            for record_kind in ("activity", "sleep", "naps"):
                db.add(BandSourceRecord(id=record_kind, user_id="synthetic-band-kind", date=day, kind=record_kind,
                    source="zepp", source_scope="device", device_id="SYNTHETICWATCH",
                    fetched_at=stamp.replace(tzinfo=None), source_record_id=None,
                    data={"band_activity": {"minute_steps": [1] * 1440}, "synthetic_kind": record_kind}))
        statements = []

        def capture(_connection, _cursor, statement, parameters, _context, _executemany):
            statements.append((statement, parameters))

        event.listen(engine, "before_cursor_execute", capture)
        rows = getattr(SqlHealthReader(sessions), method)("synthetic-band-kind", day, day, as_of=stamp)
        assert len(rows) == 1 and rows[0]["kind"] == kind
        assert "band_activity" not in rows[0]["data"]
        assert len(statements) == 1 and "json_remove(" in statements[0][0]
        assert "band_source_records.kind =" in statements[0][0] and kind in statements[0][1]
    finally:
        engine.dispose()
