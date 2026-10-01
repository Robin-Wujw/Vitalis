"""Source sample identity keeps concurrent measurements without shifting time."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vitalis.domain import DenseDataFile, MetricSample, Workout, WorkoutMetricSample
from vitalis.adapters.persistence import HealthRepository
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import (
    DenseDataFile as StoredDenseDataFile,
    MetricSample as StoredMetricSample,
    WorkoutMetricSample as StoredWorkoutMetricSample,
)


def test_same_timestamp_with_distinct_source_ids_is_stored_and_replay_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'samples.db').as_posix()}")
    init_db(engine)
    observed_at = datetime(2026, 9, 26, 8, 7, 6, 123000, tzinfo=timezone.utc)
    samples = [
        MetricSample(
            user_id="owner", source="zepp", metric="heart_rate",
            timestamp=observed_at, value=rate, unit="bpm",
            source_scope="device", device_id=None,
            source_record_id=f"cloud-heart-rate-session-{index}",
            sample_ordinal=0,
        )
        for index, rate in enumerate((72.0, 74.0))
    ]
    try:
        with Session(engine) as db:
            repo = HealthRepository(db)
            repo.save_metric_samples(samples)
            repo.save_metric_samples(samples)
            db.commit()
        with Session(engine) as db:
            rows = db.execute(select(StoredMetricSample)).scalars().all()
            assert len(rows) == 2
            assert {row.source_record_id for row in rows} == {
                item.source_record_id for item in samples
            }
            assert {row.timestamp for row in rows} == {observed_at.replace(tzinfo=None)}
            assert {row.device_id for row in rows} == {""}
        with Session(engine) as db:
            duplicate = StoredMetricSample(
                user_id="owner", source="zepp", metric="heart_rate",
                timestamp=observed_at.replace(tzinfo=None), value=99, unit="bpm",
                source_scope="device", device_id="",
                source_record_id=samples[0].source_record_id,
            )
            db.add(duplicate)
            with pytest.raises(IntegrityError):
                db.flush()
    finally:
        engine.dispose()


def test_same_timestamp_with_distinct_ordinals_survives_replay(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'ordinal-samples.db').as_posix()}")
    init_db(engine)
    observed_at = datetime(2026, 9, 26, 8, 7, 6, 123000, tzinfo=timezone.utc)
    samples = [
        MetricSample(
            user_id="owner", source="zepp", metric="heart_rate",
            timestamp=observed_at, value=rate, unit="bpm",
            source_scope="device", device_id=None,
            sample_ordinal=ordinal,
        )
        for ordinal, rate in enumerate((72.0, 74.0))
    ]
    try:
        with Session(engine) as db:
            repo = HealthRepository(db)
            assert repo.save_metric_samples(samples) == 2
            assert repo.save_metric_samples(samples) == 2
            db.commit()
        with Session(engine) as db:
            rows = db.scalars(select(StoredMetricSample)).all()
            assert len(rows) == 2
            assert {row.sample_ordinal for row in rows} == {0, 1}
            assert {row.timestamp for row in rows} == {observed_at.replace(tzinfo=None)}
            assert {row.value for row in rows} == {72.0, 74.0}
    finally:
        engine.dispose()
