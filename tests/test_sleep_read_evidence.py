"""Only successful, complete sleep reads can confirm a stored wake time."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from vitalis.adapters.persistence import HealthRepository, init_db
from vitalis.adapters.persistence.models import SyncAttempt, SyncChunk, User


START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=1)


@pytest.fixture
def repository():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as db:
        db.add_all([User(id="sleep-reader"), User(id="another-reader")])
        db.flush()
        yield HealthRepository(db)
    engine.dispose()


def add_read(repo, key, *, minute=60, elapsed=0, user_id="sleep-reader", **changes):
    started = (START + timedelta(minutes=minute)).replace(tzinfo=None)
    repo.db.add(SyncAttempt(
        id=key, user_id=user_id, trigger="morning_probe", request_key=key,
        window_start=START.replace(tzinfo=None), window_end=END.replace(tzinfo=None),
        status="succeeded", created_at=started, timezone="UTC",
    ))
    repo.db.flush()
    values = dict(
        attempt_id=key, stable_key="sleep", stream="sleep", health_stream="sleep",
        status="succeeded", fetch_status="success", parse_status="success",
        write_status="success", records_written=1,
        stages={"sleep_dates": [START.date().isoformat()], "heart_rate_seen_until": {
            START.date().isoformat(): (START + timedelta(minutes=minute - 1)).isoformat(),
        }},
        window_start=START.replace(tzinfo=None), window_end=END.replace(tzinfo=None),
        started_at=started, finished_at=started + timedelta(minutes=elapsed),
    )
    values.update(changes)
    repo.db.add(SyncChunk(**values))
    repo.db.flush()


@pytest.mark.parametrize("changes", [
    {"parse_status": "empty", "write_status": "not_run", "records_written": 0},
    {"parse_status": "unrecognized", "write_status": "not_run", "records_written": 0},
    {"fetch_status": "partial", "stages": {}},
    {"fetch_status": "failed", "stages": {}},
    {"write_status": "failed", "stages": {}},
    {"records_written": 0, "stages": {}},
    {"status": "failed", "stages": {}},
    {"started_at": None, "stages": {}},
    {"stages": {}},
    {"stages": {"sleep_dates": []}},
    {"stages": {"sleep_dates": [END.date().isoformat()]}},
])
def test_empty_or_unsuccessful_reads_do_not_confirm_an_existing_sleep(repository, changes):
    add_read(repository, "older-valid", minute=10)
    if changes.get("records_written") == 0:
        changes = {**changes, "stages": {}}
    add_read(repository, "newer-unusable", **changes)

    assert repository.sleep_confirmation("sleep-reader", START, END, END) == (
        START + timedelta(minutes=10), START + timedelta(minutes=9),
    )


def test_delayed_completion_cannot_make_an_early_read_confirm_the_wake(repository):
    add_read(repository, "slow-write", minute=59, elapsed=4)

    assert repository.sleep_confirmation("sleep-reader", START, END, END) == (
        START + timedelta(minutes=59), START + timedelta(minutes=58),
    )


def test_only_finished_reads_for_the_user_and_local_day_count(repository):
    add_read(repository, "valid", minute=45)
    add_read(repository, "other-user", minute=55, user_id="another-reader")
    add_read(repository, "not-yet-finished", minute=59, elapsed=4)
    add_read(repository, "next-day", minute=60,
             window_start=END.replace(tzinfo=None),
             window_end=(END + timedelta(days=1)).replace(tzinfo=None))

    assert repository.sleep_confirmation(
        "sleep-reader", START, END, START + timedelta(minutes=60),
    ) == (START + timedelta(minutes=45), START + timedelta(minutes=44))


def test_without_a_usable_read_the_confirmation_remains_unknown(repository):
    add_read(repository, "empty", parse_status="empty", write_status="not_run", records_written=0)

    assert repository.sleep_confirmation("sleep-reader", START, END, END) == (None, None)


@pytest.mark.parametrize("changes", [
    {"fetch_status": "partial"},
    {"parse_status": "failed", "write_status": "not_run", "records_written": 0},
    {"status": "failed"},
    {"started_at": None},
])
def test_newer_sleep_write_cannot_borrow_an_older_complete_responses_evidence(repository, changes):
    add_read(repository, "older-valid", minute=10)
    add_read(repository, "new-sleep", **changes)

    assert repository.sleep_confirmation("sleep-reader", START, END, END) == (None, None)


def test_last_published_sleep_owns_the_evidence_even_when_it_started_earlier(repository):
    add_read(repository, "fast-response", minute=60)
    add_read(repository, "slow-response-published-last", minute=55, elapsed=10)

    assert repository.sleep_confirmation("sleep-reader", START, END, END) == (
        START + timedelta(minutes=55), START + timedelta(minutes=54),
    )


@pytest.mark.parametrize("watermark", [None, "invalid", "2026-10-01T00:59:00", END.isoformat()])
def test_missing_or_outside_day_watermarks_remain_unknown(repository, watermark):
    add_read(repository, "no-heart-rate", stages={
        "sleep_dates": [START.date().isoformat()],
        "heart_rate_seen_until": {START.date().isoformat(): watermark},
    })

    assert repository.sleep_confirmation("sleep-reader", START, END, END) == (
        START + timedelta(minutes=60), None,
    )


def test_new_heart_rate_does_not_replace_the_sleep_responses_watermark(repository):
    from vitalis.adapters.persistence.models import MetricSample

    add_read(repository, "morning", minute=60)
    repository.db.add(MetricSample(
        user_id="sleep-reader", metric="heart_rate", value=60,
        timestamp=(START + timedelta(hours=18)).replace(tzinfo=None),
    ))
    repository.db.flush()

    assert repository.sleep_confirmation("sleep-reader", START, END, END) == (
        START + timedelta(minutes=60), START + timedelta(minutes=59),
    )


@pytest.mark.parametrize(("with_sleep", "fail_after_sleep", "persistence_seconds"), [
    (True, False, 0), (False, False, 0), (True, True, 0), (True, False, 5),
])
def test_sleep_probe_persists_only_its_own_sleep_and_heart_rate_evidence(
    tmp_path, monkeypatch, with_sleep, fail_after_sleep, persistence_seconds,
):
    import base64
    import json

    from sqlalchemy.orm import sessionmaker

    from vitalis.adapters.zepp.fetcher import FetchWindow
    from vitalis.adapters.zepp.parser import ZeppParser
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator
    from vitalis.adapters.zepp.sync_manager import SyncManager
    from vitalis.config import settings

    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    day = START.date()
    window = FetchWindow.local_dates(day, day, "Asia/Shanghai")
    now = window.start + timedelta(hours=10, minutes=5)
    clock = [now]
    if persistence_seconds:
        persist = SyncManager._persist_record

        def slow_persist(*args, **kwargs):
            result = persist(*args, **kwargs)
            clock[0] += timedelta(seconds=persistence_seconds)
            return result

        monkeypatch.setattr(SyncManager, "_persist_record", slow_persist)
    summary = {"tz": 28800, "stp": {"ttl": 500}}
    if with_sleep:
        summary["slp"] = {"st": "00:30", "ed": "08:00", "wk": 0}
    if fail_after_sleep:
        def fail(*_args, **_kwargs):
            raise ValueError("synthetic heart-rate parse failure after the sleep write")

        monkeypatch.setattr(ZeppParser, "parse_band_heart_rate", fail)

    class BandConnector:
        source_mode = "mock"

        def fetch_band_data(self, *args):
            return {"data": {"items": [{
                "date": day.isoformat(),
                "summary": base64.b64encode(json.dumps(summary).encode()).decode(),
                "data_hr": base64.b64encode(bytes([0] * 540 + [60] * 65)).decode(),
            }]}}

    engine = create_engine(f"sqlite:///{(tmp_path / 'sleep-evidence.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    coordinator = ZeppSyncCoordinator(
        connector=BandConnector(), session_factory=factory, wall_clock=lambda: clock[0],
    )
    attempt = coordinator.create_attempt(
        "probe-reader", window=window, trigger="morning_probe", options={"sleep_only": True},
    )
    coordinator.run_attempt(attempt.id)
    with factory() as db:
        repo = HealthRepository(db)
        chunk = repo.sync_chunks(attempt.id)[0]
        assert chunk.status == ("failed" if fail_after_sleep else "succeeded")
        assert chunk.finished_at == clock[0].replace(tzinfo=None)
        assert chunk.stages["sleep_dates"] == ([day.isoformat()] if with_sleep else [])
        assert chunk.stages["heart_rate_seen_until"] == ({} if fail_after_sleep else {
            day.isoformat(): (now - timedelta(minutes=1)).isoformat(),
        })
        assert repo.sleep_confirmation("probe-reader", window.start, window.end, clock[0]) == (
            (now, now - timedelta(minutes=1)) if with_sleep and not fail_after_sleep else (None, None)
        )
    engine.dispose()


@pytest.mark.parametrize(("status", "reason", "valid_read", "expected"), [
    ("deferred", "sleep_incomplete", True, 1),
    (None, None, True, 0),
    ("accepted", None, True, 0),
    ("deferred", "stored_data_incomplete", True, 0),
    ("deferred", "sleep_incomplete", False, 0),
])
def test_only_a_waiting_morning_reanalyzes_a_new_valid_probe(
    repository, status, reason, valid_read, expected,
):
    from vitalis.adapters.persistence.models import AnalysisJob, AnalysisRun, NotificationDelivery
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

    if status is not None:
        repository.db.add(AnalysisRun(
            id="waiting-run", user_id="sleep-reader", target_date=START.date(),
            status="SUCCEEDED", started_at=START.replace(tzinfo=None),
            intelligence_version="test", decision_policy_version="test", evidence_version="test",
        ))
        repository.db.flush()
        repository.db.add(NotificationDelivery(
            id="waiting-morning", user_id="sleep-reader", analysis_run_id="waiting-run",
            period="morning", target_date=START.date(), status=status, last_error=reason,
        ))
    # An older valid response must not qualify an empty new probe.
    add_read(repository, "older-valid", minute=10)
    add_read(repository, "new-probe", **({} if valid_read else {
        "stages": {}, "parse_status": "empty", "write_status": "not_run", "records_written": 0,
    }))
    attempt = {
        "id": "new-probe", "user_id": "sleep-reader", "trigger": "morning_probe",
        "window_start": START, "window_end": END, "timezone": "UTC",
    }
    coordinator = ZeppSyncCoordinator(wall_clock=lambda: END)
    coordinator._enqueue_scheduled_analysis_job(repository.db, attempt, "succeeded")
    coordinator._enqueue_scheduled_analysis_job(repository.db, attempt, "succeeded")
    repository.db.flush()

    jobs = repository.db.query(AnalysisJob).filter_by(user_id="sleep-reader").all()
    assert len(jobs) == expected
    if jobs:
        assert jobs[0].target_date == START.date()
        assert jobs[0].delivery_period is None
        assert jobs[0].status == "queued"
        assert jobs[0].affected_streams == ["sleep"]
