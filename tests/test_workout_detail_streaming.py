"""Bounded workout-detail streaming and failure-atomicity tests."""

import json
import tracemalloc
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.workout_detail_writer import (
    write_streamed_workout_detail,
)
from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord
from vitalis.adapters.zepp.parsers.workout_detail import (
    ParseContext,
    parse_workout_detail,
)
from vitalis.adapters.zepp.sync_manager import SyncManager
from vitalis.adapters.zepp.workout_streaming import spool_bytes
from vitalis.domain import User, Workout, WorkoutMetricSample, WorkoutType

START = datetime(2026, 8, 28, 6, tzinfo=UTC)


def _detail_payload(*, rows: int = 2, filler: str = "") -> bytes:
    lap = ";".join(f"{index},30,100,{filler}" for index in range(rows))
    return json.dumps(
        {
            "data": {
                "trackid": int(START.timestamp()),
                "time": ";".join("1" for _ in range(rows)),
                "heart_rate": ";".join("1,80" for _ in range(rows)),
                "lap": lap,
            }
        }
    ).encode()


def _record(workout_id: str, payload) -> FetchedRecord:
    return FetchedRecord(
        RawRecord(
            "workout_detail",
            f"workout_detail:{workout_id}:cloud",
            START,
            START + timedelta(minutes=30),
            payload,
        )
    )


def _setup_workout(user_id: str, workout_id: str) -> None:
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.bind_source_mode(user_id, "mock")
        repo.save_workout(
            Workout(
                user_id=user_id,
                workout_id=workout_id,
                started_at=START,
                duration=30,
                type=WorkoutType.RUNNING,
                vendor_source="cloud",
            )
        )


def test_twenty_mb_detail_is_parsed_with_bounded_heap():
    payload = _detail_payload(rows=2_000, filler="x" * 11_000)
    assert 20 * 1024 * 1024 < len(payload) < 32 * 1024 * 1024
    spool = spool_bytes(payload, max_bytes=32 * 1024 * 1024)
    try:
        tracemalloc.start()
        result = parse_workout_detail(spool, ParseContext())
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result.status == "recognized"
        assert result.detail is not None
        assert len(result.detail.laps) == 2_000
        assert peak < 12 * 1024 * 1024
    finally:
        spool.cleanup()


def test_malformed_stream_never_marks_detail_synced_or_publishes_samples():
    user_id, workout_id = "stream-malformed", "malformed-detail"
    _setup_workout(user_id, workout_id)
    spool = spool_bytes(
        b'{"data":{"trackid":1787919600,"heart_rate":"1,80"',
        max_bytes=32 * 1024 * 1024,
    )
    with session_scope() as db:
        report = SyncManager(
            SimpleNamespace(connector=SimpleNamespace(source_mode="mock"))
        )._persist_record(
            _record(workout_id, spool), HealthRepository(db), User(id=user_id)
        )
        assert report.status == "unverified"
        assert report.write_status == "not_run"
        assert report.records_written == 0
    with session_scope() as db:
        workout = HealthRepository(db).workout(user_id, workout_id)
        assert workout is not None
        assert workout.detail_synced is False
        assert workout.detail is None
        assert HealthRepository(db).workout_metric_samples(user_id, workout_id) == []


def test_repeated_streamed_detail_is_idempotent():
    user_id, workout_id = "stream-idempotent", "idempotent-detail"
    _setup_workout(user_id, workout_id)
    manager = SyncManager(
        SimpleNamespace(connector=SimpleNamespace(source_mode="mock"))
    )
    payload = _detail_payload(rows=4)
    with session_scope() as db:
        repo = HealthRepository(db)
        first = manager._persist_record(
            _record(workout_id, spool_bytes(payload, max_bytes=32 * 1024 * 1024)),
            repo,
            User(id=user_id),
        )
        revision = repo.analysis_input_revision(user_id)
        second = manager._persist_record(
            _record(workout_id, spool_bytes(payload, max_bytes=32 * 1024 * 1024)),
            repo,
            User(id=user_id),
        )
        assert first.status == second.status == "success"
        assert repo.analysis_input_revision(user_id) == revision
        assert len(repo.workout_metric_samples(user_id, workout_id)) == 4


class _FailingSampleSource:
    def __init__(self, samples):
        self.samples = samples
        self.calls = 0

    def iter_samples(self, _workout_id):
        self.calls += 1
        if self.calls == 1:
            yield from self.samples
            return
        for index, sample in enumerate(self.samples):
            if index == 1:
                raise RuntimeError("synthetic sample stream ended")
            yield sample


def test_mid_batch_writer_failure_rolls_back_detail_and_samples():
    user_id, workout_id = "stream-rollback", "rollback-detail"
    _setup_workout(user_id, workout_id)
    samples = [
        WorkoutMetricSample(
            workout_id=workout_id,
            timestamp=START + timedelta(seconds=index),
            metric="heart_rate",
            value=80,
            unit="bpm",
        )
        for index in range(300)
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        with pytest.raises(RuntimeError, match="ended"):
            write_streamed_workout_detail(
                repo,
                user_id,
                workout_id,
                {"schema_version": "5.1", "metrics_present": ["heart_rate"]},
                _FailingSampleSource(samples),
            )
    with session_scope() as db:
        repo = HealthRepository(db)
        workout = repo.workout(user_id, workout_id)
        assert workout is not None and workout.detail_synced is False
        assert repo.workout_metric_samples(user_id, workout_id) == []


def test_cancellation_after_a_written_batch_rolls_back_everything():
    user_id, workout_id = "stream-cancel", "cancel-detail"
    _setup_workout(user_id, workout_id)
    samples = [
        WorkoutMetricSample(
            workout_id=workout_id,
            timestamp=START + timedelta(seconds=index),
            metric="heart_rate",
            value=80,
            unit="bpm",
        )
        for index in range(300)
    ]

    class Source:
        def iter_samples(self, _workout_id):
            yield from samples

    checks = {"count": 0}

    def check():
        checks["count"] += 1
        if checks["count"] > 1:
            raise RuntimeError("cancelled")

    with session_scope() as db, pytest.raises(RuntimeError, match="cancelled"):
        write_streamed_workout_detail(
            HealthRepository(db),
            user_id,
            workout_id,
            {"schema_version": "5.1", "metrics_present": ["heart_rate"]},
            Source(),
            check=check,
        )
    with session_scope() as db:
        repo = HealthRepository(db)
        workout = repo.workout(user_id, workout_id)
        assert workout is not None and workout.detail_synced is False
        assert repo.workout_metric_samples(user_id, workout_id) == []
