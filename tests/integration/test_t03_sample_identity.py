"""Normalized sample keys preserve independently observed same-time facts."""

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import (
    DenseDataFile as StoredDenseDataFile,
    WorkoutMetricSample as StoredWorkoutMetricSample,
)
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.domain import DenseDataFile, Workout, WorkoutMetricSample


def test_workout_detail_distinguishes_same_time_streams_and_ordinals(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'workout-samples.db').as_posix()}")
    init_db(engine)
    timestamp = datetime(2026, 9, 26, 8, 7, 6, tzinfo=timezone.utc)
    samples = [
        WorkoutMetricSample(
            workout_id="workout-1", timestamp=timestamp,
            metric="heart_rate", value=value, unit="bpm",
            source_scope=scope, device_id=device_id, sample_ordinal=ordinal,
        )
        for value, scope, device_id, ordinal in (
            (72, "wrist", None, 0),
            (74, "wrist", None, 1),
            (76, "strap", None, 0),
            (78, "wrist", "device-2", 0),
        )
    ]
    try:
        with Session(engine) as db:
            repo = HealthRepository(db)
            repo.save_workout(Workout(user_id="owner", workout_id="workout-1", duration=20))
            assert repo.save_workout_detail("owner", "workout-1", {}, samples)
            assert repo.save_workout_detail("owner", "workout-1", {}, samples)
            db.commit()
        with Session(engine) as db:
            rows = db.scalars(select(StoredWorkoutMetricSample)).all()
            assert len(rows) == 4
            assert {row.value for row in rows} == {72, 74, 76, 78}
            assert {row.timestamp for row in rows} == {timestamp.replace(tzinfo=None)}
            assert {row.device_id for row in rows} == {"", "device-2"}
    finally:
        engine.dispose()


def test_dense_index_without_start_time_replays_by_file_and_date(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'dense-files.db').as_posix()}")
    init_db(engine)
    files = [
        DenseDataFile(
            user_id="owner", source="zepp", stream="second_heart_rate",
            file_id="same-vendor-file", date=day, start_utc=None,
            device_id=None, sample_count=3,
        )
        for day in (date(2026, 9, 25), date(2026, 9, 26))
    ]
    try:
        with Session(engine) as db:
            repo = HealthRepository(db)
            assert repo.save_dense_data_files(files) == 2
            assert repo.save_dense_data_files(files) == 2
            db.commit()
        with Session(engine) as db:
            rows = db.scalars(select(StoredDenseDataFile)).all()
            assert len(rows) == 2
            assert {row.date for row in rows} == {date(2026, 9, 25), date(2026, 9, 26)}
            assert {row.start_utc for row in rows} == {None}
            assert len({row.start_identity for row in rows}) == 2
    finally:
        engine.dispose()
