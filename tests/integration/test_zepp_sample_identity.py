"""Zepp rows at one observed time need a stable identity beyond that time."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import MetricSample as StoredMetricSample
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.zepp.parser import ZeppParser


def test_same_millisecond_zepp_hr_rows_survive_replay_and_page_reordering(tmp_path):
    timestamp = 1_700_000_000_123
    payload = [
        {"timestamp": timestamp, "value": 72},
        {"timestamp": timestamp, "value": 74},
        {"timestamp": timestamp, "value": 72},
    ]
    first = ZeppParser.parse_heart_rate_samples({"items": payload})
    reordered = ZeppParser.parse_heart_rate_samples({"items": list(reversed(payload))})
    assert len(first) == len(reordered) == 3
    assert len({sample.timestamp for sample in first + reordered}) == 1
    assert len({sample.source_record_id for sample in first}) == 3
    assert {sample.source_record_id for sample in first} == {
        sample.source_record_id for sample in reordered
    }
    assert sorted(sample.sample_ordinal for sample in first if sample.value == 72) == [0, 1]
    assert all(sample.device_id is None for sample in first)

    engine = create_engine(f"sqlite:///{(tmp_path / 'zepp-samples.db').as_posix()}")
    init_db(engine)
    try:
        for group in (first, reordered, first):
            with Session(engine) as db:
                for sample in group:
                    sample.user_id = "synthetic-owner"
                HealthRepository(db).save_metric_samples(group)
                db.commit()
        with Session(engine) as db:
            saved = db.execute(select(StoredMetricSample)).scalars().all()
            assert len(saved) == 3
            assert len({row.source_record_id for row in saved}) == 3
            assert len({row.timestamp for row in saved}) == 1
            assert {row.device_id for row in saved} == {""}
    finally:
        engine.dispose()
