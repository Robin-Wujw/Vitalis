"""Workout-detail coverage must stay accurate without scanning dense history."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import Session

from vitalis.adapters.persistence import HealthRepository
from vitalis.adapters.persistence import models as orm
from vitalis.time import local_day_utc_bounds


@pytest.fixture
def coverage_db():
    engine = create_engine("sqlite://")
    orm.Workout.__table__.create(engine)
    orm.WorkoutMetricSample.__table__.create(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


def sample(timestamp, *, user="coverage-owner", source="zepp", workout="orphan", ordinal=0):
    return {
        "user_id": user, "source": source, "workout_id": workout,
        "timestamp": timestamp, "metric": "heart_rate", "value": 123.0,
        "unit": "bpm", "source_scope": "workout", "device_id": "synthetic-watch",
        "sample_ordinal": ordinal,
    }


@pytest.mark.parametrize("day,zone", [
    (date(2026, 10, 28), "Asia/Singapore"),
    (date(2026, 11, 1), "America/New_York"),  # 25-hour local day.
    (date(2026, 3, 8), "America/New_York"),  # 23-hour local day.
])
@pytest.mark.parametrize("generic_statement", [False, True], ids=["sqlite-index", "generic-sql"])
def test_workout_detail_days_preserve_boundaries_source_and_orphan_samples(coverage_db, day, zone, generic_statement):
    start, end = local_day_utc_bounds(day, zone)
    start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    next_start, next_end = local_day_utc_bounds(day + timedelta(days=1), zone)
    next_start, next_end = next_start.replace(tzinfo=None), next_end.replace(tzinfo=None)
    coverage_db.execute(insert(orm.Workout), [{
        "user_id": "coverage-owner", "source": "zepp", "workout_id": "cross-midnight",
        "started_at": start - timedelta(hours=1), "data": {},
    }])
    coverage_db.execute(insert(orm.WorkoutMetricSample), [
        sample(start - timedelta(microseconds=1)),
        sample(start, workout="cross-midnight"),
        sample(start + timedelta(hours=1)),
        sample(end - timedelta(microseconds=1)),
        sample(next_start, user="other-owner"),
        sample(next_start + timedelta(seconds=1), source="other-source"),
        sample(next_end),
    ])
    coverage_db.commit()
    # Execute the standard SQL branch on this isolated database too; the hint
    # must stay SQLite-only while both paths retain identical observations.
    reader = coverage_db if not generic_statement else SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        scalar=coverage_db.scalar,
    )
    repo = HealthRepository(reader)
    assert repo.stream_observation_dates(
        "coverage-owner", "workout_detail", source="zepp",
        window_start=day, window_end=day + timedelta(days=1), timezone_name=zone,
    ) == {day}
    assert repo.stream_observation_dates(
        "coverage-owner", "workout_detail", source="zepp",
        window_start=day + timedelta(days=2), window_end=day, timezone_name=zone,
    ) == set()


@pytest.mark.parametrize("day,zone,timestamp,present", [
    (date(2026, 10, 28), "Asia/Singapore", datetime(2026, 10, 27, 15, 59, 59, 999999), False),
    (date(2026, 10, 28), "Asia/Singapore", datetime(2026, 10, 27, 16), True),
    (date(2026, 10, 28), "Asia/Singapore", datetime(2026, 10, 28, 15, 59, 59, 999999), True),
    (date(2026, 10, 28), "Asia/Singapore", datetime(2026, 10, 28, 16), False),
    (date(2026, 11, 1), "America/New_York", datetime(2026, 11, 1, 4), True),
    (date(2026, 11, 1), "America/New_York", datetime(2026, 11, 2, 4, 59, 59, 999999), True),
    (date(2026, 11, 1), "America/New_York", datetime(2026, 11, 2, 5), False),
    (date(2026, 3, 8), "America/New_York", datetime(2026, 3, 8, 5), True),
    (date(2026, 3, 8), "America/New_York", datetime(2026, 3, 9, 3, 59, 59, 999999), True),
    (date(2026, 3, 8), "America/New_York", datetime(2026, 3, 9, 4), False),
])
def test_single_workout_sample_uses_exact_local_day_utc_bounds(coverage_db, day, zone, timestamp, present):
    coverage_db.execute(insert(orm.WorkoutMetricSample), [sample(timestamp)])
    coverage_db.commit()
    assert HealthRepository(coverage_db).stream_observation_dates(
        "coverage-owner", "workout_detail", window_start=day, window_end=day, timezone_name=zone,
    ) == ({day} if present else set())


def test_workout_detail_coverage_has_a_history_independent_sqlite_instruction_budget(coverage_db):
    history_start = datetime(2025, 1, 1)
    coverage_db.execute(insert(orm.WorkoutMetricSample), [
        sample(history_start + timedelta(seconds=index), workout="historical", ordinal=index)
        for index in range(30_000)
    ])
    observed_day = date(2026, 10, 28)
    coverage_db.execute(insert(orm.WorkoutMetricSample), [sample(datetime(2026, 10, 28, 12))])
    coverage_db.commit()
    connection = coverage_db.connection()
    raw = connection.connection.driver_connection
    statements = []
    steps = 0

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    def progress():
        nonlocal steps
        steps += 100
        return int(steps > 10_000)

    event.listen(connection, "before_cursor_execute", capture)
    raw.set_progress_handler(progress, 100)
    try:
        result = HealthRepository(coverage_db).stream_observation_dates(
            "coverage-owner", "workout_detail", source="zepp",
            window_start=observed_day - timedelta(days=6), window_end=observed_day,
            timezone_name="UTC",
        )
    finally:
        raw.set_progress_handler(None, 0)
        event.remove(connection, "before_cursor_execute", capture)
    assert result == {observed_day}
    assert steps <= 10_000
    assert 1 <= len(statements) <= 7
    plans = {
        row[3]
        for statement, parameters in statements
        for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
    }
    assert all("ix_workout_metric_samples_timestamp" in plan for plan in plans)
    assert all("timestamp>?" in plan and "timestamp<?" in plan for plan in plans)
    print(f"workout_detail: {steps} SQLite VM instructions; plans={sorted(plans)}")
