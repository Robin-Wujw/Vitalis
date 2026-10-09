"""Synthetic source responses can be re-parsed without network or delivery."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence.database import _create_engine, init_db
from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.persistence.source_journal import (
    CURRENT_PARSER_VERSION,
    DENSE_RAW_SCHEMA_VERSION,
    JournalError,
    ReplayDataset,
    SourceJournalRecord,
    SourceJournalRepository,
    SourceRecordInput,
    SourceReplayRun,
    payload_hash,
)
from vitalis.adapters.replay import replay_fixture, replay_source_records
from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord
from vitalis.adapters.zepp.sync_coordinator import (
    StaleSyncLease, ZeppSyncCoordinator, _ClaimedChunk, _ChunkResult,
)
from vitalis.adapters.zepp.sync_manager import SyncManager
from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.application.replay import ReplayError, manifest_digest
from vitalis.application.sync_types import SyncLease
from vitalis.config import settings
from vitalis.domain import AuthToken, User


DAY = date(2026, 9, 1)
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = START + timedelta(days=1)
FETCHED = END + timedelta(hours=1)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "replay" / "phase3.json"


@pytest.fixture
def sessions(tmp_path):
    engine = _create_engine(f"sqlite:///{(tmp_path / 'source-replay.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("source replay requested network or notification delivery")

    import httpx
    from vitalis.adapters.notifications import PushService

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    for name in ("send", "send_report", "send_daily_report"):
        if hasattr(PushService, name):
            monkeypatch.setattr(PushService, name, forbidden)


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _owner(db, user_id="source-owner", mode="mock"):
    repo = HealthRepository(db)
    repo.upsert_user(user_id)
    repo.bind_source_mode(user_id, mode)
    if mode == "real":
        repo.save_token(AuthToken(
            user_id=user_id, source="zepp", source_user_id=f"synthetic-{user_id}",
            access_token="synthetic-token",
        ))
    return repo


def _input(user_id="source-owner", *, mode="mock", fetched_at=FETCHED,
           payload=None, record_id="heart_rate:synthetic", **kwargs):
    return SourceRecordInput(
        user_id=user_id, source="zepp", source_mode=mode,
        stream="heart_rate", record_id=record_id,
        observed_start=START, observed_end=END, fetched_at=fetched_at,
        payload=payload or {"items": [{
            "sample_id": "synthetic-a", "timestamp": START.isoformat(),
            "value": 72, "deviceId": "SYNTHBALANCE2",
        }]},
        **kwargs,
    )


def _append(db, item):
    return SourceJournalRepository(db).append(item)


def _replay(sessions, target="replay-owner", *, as_of=FETCHED, **kwargs):
    return replay_source_records(
        "source-owner", target, start_date=DAY, end_date=DAY,
        as_of=as_of, session_factory=sessions, **kwargs,
    )


def test_hash_is_canonical_and_rejects_non_json_values():
    assert payload_hash({"a": 1, "b": [None, 0]}) == payload_hash({"b": [None, 0], "a": 1})
    assert payload_hash({"a": None}) != payload_hash({"a": 0})
    with pytest.raises(JournalError):
        payload_hash({"synthetic-private-marker": float("nan")})


def test_journal_encryption_immutability_duplicate_and_late_revision(sessions):
    with sessions.begin() as db:
        _owner(db)
        store = SourceJournalRepository(db)
        first = store.append(_input())
        duplicate = store.append(_input(fetched_at=FETCHED + timedelta(minutes=1)))
        corrected = store.append(_input(
            fetched_at=FETCHED + timedelta(days=2),
            payload={"items": [{"timestamp": START.isoformat(), "value": 75}]},
        ))
        assert first.id == duplicate.id
        assert first.id != corrected.id
        assert first.fetched_at == FETCHED.replace(tzinfo=None)
        assert first.encrypted_payload.startswith("fernet:")
        assert "synthetic-a" not in first.encrypted_payload
        assert store.read_payload(first.id, user_id="source-owner")["items"][0]["value"] == 72
        ids = first.id, corrected.id

    with pytest.raises(JournalError), sessions.begin() as db:
        db.get(SourceJournalRecord, ids[0]).parser_version = "overwrite-is-forbidden"
    with sessions() as db:
        assert db.get(SourceJournalRecord, ids[0]).parser_version == CURRENT_PARSER_VERSION
        assert len(db.scalars(select(SourceJournalRecord)).all()) == 2


def test_real_raw_requires_private_key_even_when_runtime_is_mock(sessions, monkeypatch):
    with sessions.begin() as db:
        repo = _owner(db, mode="real")
        account = repo.source_account("source-owner")
        account_id, epoch = account.id, account.fence_epoch
    monkeypatch.setattr(settings, "token_encryption_key", "")
    monkeypatch.setattr(settings, "zepp_mock", True)
    with pytest.raises(JournalError, match="encryption"), sessions.begin() as db:
        _append(db, _input(
            mode="real", source_account_id=account_id, source_account_epoch=epoch,
        ))
    with sessions() as db:
        assert db.scalars(select(SourceJournalRecord)).all() == []


def test_manifest_is_bounded_scoped_and_does_not_decrypt(sessions, monkeypatch):
    with sessions.begin() as db:
        _owner(db)
        _owner(db, "other-owner")
        first = _append(db, _input())
        late = _append(db, _input(
            record_id="heart_rate:late", fetched_at=FETCHED + timedelta(days=2),
        ))
        other = _append(db, _input("other-owner"))
        first_id, late_id, other_id = first.id, late.id, other.id
    monkeypatch.setattr(settings, "token_encryption_key", "")
    with sessions() as db:
        store = SourceJournalRepository(db)
        args = dict(start_date=DAY, end_date=DAY, as_of=FETCHED)
        manifest = store.input_manifest("source-owner", **args)
        assert [row["journal_id"] for row in manifest["records"]] == [first_id]
        assert '"payload":' not in json.dumps(manifest)
        assert "encrypted_payload" not in json.dumps(manifest)
        assert "synthetic-a" not in json.dumps(manifest)
        assert manifest == store.input_manifest("source-owner", **args)
        for journal_id in (late_id, other_id):
            with pytest.raises(JournalError):
                store.input_manifest("source-owner", journal_ids=[journal_id], **args)
        with pytest.raises(JournalError):
            store.input_manifest("source-owner", start_date=DAY,
                                 end_date=DAY + timedelta(days=730), as_of=FETCHED)
        with pytest.raises(JournalError):
            store.input_manifest("source-owner", start_date=DAY, end_date=DAY,
                                 as_of=FETCHED + timedelta(days=3), limit=1)


def test_phase3_fixture_preserves_devices_missing_fields_partial_and_unknown_days(sessions, offline):
    result = replay_fixture(_fixture(), "fixture-target", session_factory=sessions)
    assert result.source_mode == "replay"
    assert result.partial_records == 1
    assert not result.reused
    with sessions() as db:
        repo = HealthRepository(db)
        assert repo.source_mode("fixture-target") == "replay"
        samples = db.scalars(select(orm.MetricSample).where(
            orm.MetricSample.user_id == "fixture-target", orm.MetricSample.metric == "heart_rate",
        )).all()
        assert {(s.source_scope, s.device_id, s.value, s.unit) for s in samples} == {
            ("device", "SYNTHBALANCE2", 72.0, "bpm"),
            ("device", "SYNTHHELIO157", 76.0, "bpm"),
            ("user_fused", "", 74.0, "bpm"),
        }
        assert {s.timestamp for s in samples} == {(START + timedelta(hours=8)).replace(tzinfo=None)}
        hrv = db.scalars(select(orm.MetricSample).where(
            orm.MetricSample.user_id == "fixture-target", orm.MetricSample.metric == "hrv_sdnn",
        )).all()
        assert {(s.device_id, s.unit) for s in hrv} == {
            ("SYNTHBALANCE2", "ms"), ("SYNTHHELIO157", "ms"),
        }
        sleep = repo.get_sleep("fixture-target", DAY)
        assert sleep["sleep_duration"] == 480
        assert sleep.get("deep_sleep") is None and sleep.get("rem_sleep") is None
        activity = repo.activity_range("fixture-target", DAY, DAY)[0]
        assert activity["steps"] == 4200 and activity["distance_km"] == 3.2
        assert activity.get("calories") is None
        assert repo.training_range("fixture-target", DAY + timedelta(days=1), DAY + timedelta(days=1)) == []
        training = repo.training_range("fixture-target", DAY, DAY)[0]
        assert training.get("total_load") is None
        assert db.scalars(select(orm.SyncAttempt)).all() == []
        assert db.scalars(select(orm.AuthToken)).all() == []
        assert db.scalars(select(orm.NotificationDelivery)).all() == []
        assert all(job.delivery_period is None for job in db.scalars(select(orm.AnalysisJob)))


def test_fixture_repeat_and_parser_revision_reuse_raw_journal(sessions, offline):
    fixture = _fixture()
    first = replay_fixture(fixture, "fixture-rerun", session_factory=sessions)
    repeated = replay_fixture(deepcopy(fixture), "fixture-rerun", session_factory=sessions)
    revised = replay_fixture(fixture, "fixture-rerun", parser_version="zepp-parser-revision-2",
                             session_factory=sessions)
    assert repeated.reused and repeated.run_id == first.run_id
    assert revised.run_id != first.run_id and not revised.reused
    with sessions() as db:
        rows = db.scalars(select(SourceJournalRecord)).all()
        assert len(rows) == len(fixture["records"]) - 1
        assert {row.parser_version for row in rows} == {CURRENT_PARSER_VERSION}
        runs = db.scalars(select(SourceReplayRun)).all()
        assert {run.parser_version for run in runs} == {CURRENT_PARSER_VERSION, "zepp-parser-revision-2"}
        assert {row.payload_hash for row in rows} == {payload_hash(rec["payload"]) for rec in fixture["records"]}
        assert db.scalars(select(orm.NotificationDelivery)).all() == []


def test_late_data_is_excluded_by_fetch_as_of_and_replay_can_restore_old_projection(sessions, offline):
    fixture = _fixture()
    early = deepcopy(fixture)
    early["as_of"] = "2026-09-02T03:00:00Z"
    first = replay_fixture(early, "fixture-as-of", session_factory=sessions)
    with sessions() as db:
        rows = HealthRepository(db).daily_metrics("fixture-as-of", DAY, DAY)
        assert next(row.value for row in rows if row.metric == "readiness") == 45
    latest = replay_fixture(fixture, "fixture-as-of", session_factory=sessions)
    with sessions() as db:
        rows = HealthRepository(db).daily_metrics("fixture-as-of", DAY, DAY)
        assert next(row.value for row in rows if row.metric == "readiness") == 68
    restored = replay_fixture(early, "fixture-as-of", session_factory=sessions)
    assert latest.run_id != first.run_id and restored.run_id == first.run_id
    with sessions() as db:
        rows = HealthRepository(db).daily_metrics("fixture-as-of", DAY, DAY)
        assert next(row.value for row in rows if row.metric == "readiness") == 45
        assert db.scalars(select(SourceJournalRecord).where(
            SourceJournalRecord.fetched_at > datetime(2026, 9, 2, 3),
        )).all()


def test_live_journal_replays_to_new_user_and_retains_real_cipher(sessions, offline, monkeypatch):
    with sessions.begin() as db:
        repo = _owner(db, mode="real")
        account = repo.source_account("source-owner")
        raw = _append(db, _input(
            mode="real", source_account_id=account.id, source_account_epoch=account.fence_epoch,
        ))
        journal_id, ciphertext = raw.id, raw.encrypted_payload
    result = _replay(sessions)
    with sessions() as db:
        source = db.get(SourceJournalRecord, journal_id)
        replay = db.scalars(select(SourceJournalRecord).where(
            SourceJournalRecord.user_id == "replay-owner",
        )).one()
        assert source.encrypted_payload == ciphertext
        assert replay.origin_journal_id == journal_id
        assert replay.origin_source_mode == "real"
        assert replay.encrypted_payload.startswith("fernet:")
        assert replay.source_account_id == source.source_account_id
        assert replay.source_account_epoch == source.source_account_epoch
        assert replay.fetched_at == source.fetched_at
        assert replay.source_mode == result.source_mode == "replay"
        assert db.scalars(select(orm.MetricSample).where(orm.MetricSample.user_id == "source-owner")).all() == []
    monkeypatch.setattr(settings, "token_encryption_key", "")
    with pytest.raises(ReplayError, match="encryption"):
        _replay(sessions, target="no-key-replay")
    with sessions() as db:
        assert db.get(orm.User, "no-key-replay") is None


def test_replay_refuses_shared_live_target_or_different_origin(sessions, offline):
    with sessions.begin() as db:
        _owner(db)
        _owner(db, "live-target", "real")
        _owner(db, "other-source")
        _append(db, _input())
        _append(db, _input("other-source"))
    with pytest.raises(ReplayError):
        _replay(sessions, target="source-owner")
    with pytest.raises(ReplayError):
        _replay(sessions, target="live-target")
    _replay(sessions)
    with pytest.raises(ReplayError):
        replay_source_records("other-source", "replay-owner", start_date=DAY,
                              end_date=DAY, as_of=FETCHED, session_factory=sessions)
    with sessions() as db:
        assert db.get(ReplayDataset, "replay-owner").origin_user_id == "source-owner"
        assert db.get(orm.User, "live-target").source_mode == "real"


def test_replay_parser_failure_is_safe_and_rolls_back_everything(sessions, offline, monkeypatch):
    with sessions.begin() as db:
        _owner(db)
        _append(db, _input())
    marker = "synthetic-private-marker"

    def fail(*_args, **_kwargs):
        raise ValueError(marker)

    monkeypatch.setattr(SyncManager, "_write_stream_result", fail)
    with pytest.raises(ReplayError) as caught:
        _replay(sessions)
    assert marker not in str(caught.value)
    with sessions() as db:
        assert db.get(orm.User, "replay-owner") is None
        assert db.scalars(select(SourceReplayRun)).all() == []
        assert len(db.scalars(select(SourceJournalRecord)).all()) == 1


def _claimed_chunk(sessions, *, lease_seconds=60, stream="heart_rate"):
    with sessions.begin() as db:
        repo = _owner(db)
        attempt = repo.create_or_reuse_sync_attempt(
            "source-owner", source="zepp", window_start=START, window_end=END,
            options={"source_mode": "mock"},
            manifest=[{"stable_key": "journal-chunk", "stream": stream,
                       "window_start": START, "window_end": END,
                       "stages": {"operation": "synthetic"}}],
        )
        chunk = repo.sync_chunks(attempt.id)[0]
        assert repo.claim_attempt(attempt.id, "parent", now=FETCHED, lease_seconds=lease_seconds)
        assert repo.claim_chunk(chunk.id, "child", now=FETCHED, lease_seconds=60,
                               attempt_lease_token="parent", attempt_lease_epoch=1)
        return (
            {"id": attempt.id, "user_id": "source-owner", "options": {"source_mode": "mock"},
             "timezone": "UTC"},
            _ClaimedChunk(SyncLease(chunk.id, "child", 1, FETCHED + timedelta(seconds=60)),
                          {"stream": stream, "stages": {}, "health_stream": stream,
                           "allow_unavailable": False, "window_start": START, "window_end": END}),
        )


@pytest.mark.parametrize("stale", [False, True])
def test_worker_journal_and_parsed_facts_share_lease_fenced_commit(sessions, stale):
    attempt, claim = _claimed_chunk(sessions, lease_seconds=1 if stale else 60)
    now = FETCHED + timedelta(seconds=2)
    coordinator = ZeppSyncCoordinator(session_factory=sessions, wall_clock=lambda: now)
    raw = _input().payload
    result = _ChunkResult(FetchedRecord(RawRecord(
        "heart_rate", "heart_rate:synthetic", START, END, raw,
    )), raw_records=1)
    if stale:
        with pytest.raises(StaleSyncLease):
            coordinator._finalize_success(attempt, claim, result, SimpleNamespace(source_mode="mock"))
    else:
        assert coordinator._finalize_success(attempt, claim, result, SimpleNamespace(source_mode="mock"))
    with sessions() as db:
        assert len(db.scalars(select(SourceJournalRecord)).all()) == (0 if stale else 1)
        assert len(db.scalars(select(orm.MetricSample)).all()) == (0 if stale else 1)


def test_unrecognized_raw_is_retained_for_a_parser_fix(sessions):
    with sessions.begin() as db:
        repo = _owner(db)
        record = FetchedRecord(RawRecord(
            "heart_rate", "heart_rate:unrecognized", START, END,
            {"items": [{"unrecognized": "synthetic-shape"}]},
        ))
        report = SyncManager(SimpleNamespace(connector=SimpleNamespace(source_mode="mock")))._persist_record(
            record, repo, User(id="source-owner"),
        )
        assert report.parse_status == "unrecognized"
    with sessions() as db:
        row = db.scalars(select(SourceJournalRecord)).one()
        assert SourceJournalRepository(db).read_payload(row.id, user_id="source-owner") == record.raw.payload
        assert db.scalars(select(orm.MetricSample)).all() == []


def test_bulk_sql_cannot_mutate_raw_and_authorized_user_deletion_cascades(sessions):
    with sessions.begin() as db:
        _owner(db)
        journal_id = _append(db, _input()).id
    with pytest.raises(IntegrityError, match="immutable"), sessions.begin() as db:
        db.execute(update(SourceJournalRecord).where(SourceJournalRecord.id == journal_id).values(
            parser_version="forbidden-update",
        ))
    with sessions.begin() as db:
        HealthRepository(db).delete_for_user("source-owner")
    with sessions() as db:
        assert db.get(SourceJournalRecord, journal_id) is None


def test_analysis_manifest_selects_effective_journal_as_of_without_ciphertext(sessions):
    with sessions.begin() as db:
        _owner(db)
        first = _append(db, _input(record_id="daily_summary:readiness", fetched_at=FETCHED,
                                   payload={"items": [{"date": DAY.isoformat(), "readiness": 45}]}))
        late = _append(db, _input(record_id="daily_summary:readiness", fetched_at=FETCHED + timedelta(days=2),
                                  payload={"items": [{"date": DAY.isoformat(), "readiness": 68}]}))
    with sessions() as db:
        store = SourceJournalRepository(db)
        early = store.analysis_manifest(
            "source-owner", DAY, FETCHED, timezone_name="UTC",
        )
        latest = store.analysis_manifest(
            "source-owner", DAY, FETCHED + timedelta(days=2), timezone_name="UTC",
        )
    assert [item["journal_id"] for item in early["records"]] == [first.id]
    assert [item["journal_id"] for item in latest["records"]] == [late.id]
    assert early["projection"]["kind"] == "effective_journal"
    assert "encrypted_payload" not in json.dumps(latest)


def test_analysis_manifest_follows_current_replay_projection_not_retained_late_rows(
    sessions, offline,
):
    fixture = _fixture()
    early = deepcopy(fixture)
    early["as_of"] = "2026-09-02T03:00:00Z"
    replay_fixture(early, "manifest-replay", session_factory=sessions)
    with sessions() as db:
        early_manifest = SourceJournalRepository(db).analysis_manifest(
            "manifest-replay", DAY, datetime(2026, 9, 5, tzinfo=timezone.utc),
            timezone_name="UTC",
        )
    assert early_manifest["projection"]["kind"] == "replay_run"
    assert early_manifest["projection"]["state"] == "current"
    assert early_manifest["selection"]["as_of"] == early["as_of"]
    assert all(item["fetched_at"] <= early["as_of"] for item in early_manifest["records"])

    replay_fixture(fixture, "manifest-replay", session_factory=sessions)
    with sessions() as db:
        latest_manifest = SourceJournalRepository(db).analysis_manifest(
            "manifest-replay", DAY, datetime(2026, 9, 5, tzinfo=timezone.utc),
            timezone_name="UTC",
        )
    assert latest_manifest["selection"]["as_of"] == fixture["as_of"]
    assert any(item["fetched_at"] > early["as_of"] for item in latest_manifest["records"])


def test_manifest_digest_is_reproducible_and_query_never_loads_ciphertext(sessions):
    statements = []
    with sessions.begin() as db:
        _owner(db)
        _append(db, _input())
    engine = sessions.kw["bind"]

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with sessions() as db:
            manifest = SourceJournalRepository(db).input_manifest(
                "source-owner", start_date=DAY, end_date=DAY, as_of=FETCHED,
            )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert manifest_digest(manifest) == manifest["manifest_hash"]
    assert statements and all("encrypted_payload" not in statement for statement in statements)
    changed = deepcopy(manifest)
    changed["selection"]["as_of"] = "2026-09-02T02:00:00Z"
    assert manifest_digest(changed) != manifest["manifest_hash"]


def test_source_account_epoch_is_part_of_raw_identity(sessions):
    with sessions.begin() as db:
        repo = _owner(db, mode="real")
        account = repo.source_account("source-owner")
        first = _append(db, _input(mode="real", source_account_id=account.id,
                                  source_account_epoch=account.fence_epoch))
        repo.save_token(AuthToken(
            user_id="source-owner", source="zepp", source_user_id="synthetic-source-owner",
            access_token="synthetic-refreshed-token",
        ))
        account = repo.source_account("source-owner")
        second = _append(db, _input(mode="real", source_account_id=account.id,
                                   source_account_epoch=account.fence_epoch))
        assert first.id != second.id and first.payload_hash == second.payload_hash
        assert first.source_account_epoch == 0 and second.source_account_epoch == 1


def test_unknown_mode_cannot_be_inferred_from_process_mock_setting(sessions):
    with sessions.begin() as db:
        repo = HealthRepository(db)
        repo.upsert_user("unknown-owner")
        record = FetchedRecord(RawRecord("heart_rate", "synthetic-unmarked", START, END, _input().payload))
        with pytest.raises(JournalError, match="explicitly bound"):
            SyncManager(SimpleNamespace())._persist_record(record, repo, User(id="unknown-owner"))
    with sessions() as db:
        assert db.get(orm.User, "unknown-owner").source_mode is None
        assert db.scalars(select(SourceJournalRecord)).all() == []
        assert db.scalars(select(orm.MetricSample)).all() == []


def test_parser_revision_removes_obsolete_facts_without_removing_raw(sessions, offline, monkeypatch):
    fixture = _fixture()
    first = replay_fixture(fixture, "parser-removal", session_factory=sessions)
    monkeypatch.setattr(ZeppParser, "parse_heart_rate_samples", staticmethod(lambda _raw: []))
    changed = replay_fixture(fixture, "parser-removal", parser_version="drop-obsolete-heart-rate",
                             session_factory=sessions)
    assert changed.run_id != first.run_id
    assert changed.unrecognized_records == first.unrecognized_records + 1
    with sessions() as db:
        assert db.scalars(select(orm.MetricSample).where(
            orm.MetricSample.user_id == "parser-removal", orm.MetricSample.metric == "heart_rate",
        )).all() == []
        assert len(db.scalars(select(SourceJournalRecord)).all()) == len(fixture["records"]) - 1
        assert db.get(SourceReplayRun, first.run_id) is not None


def test_replay_preserves_unverified_training_days_for_profile_loader(sessions, offline):
    from vitalis.intelligence.profile import ProfileLoader

    replay_fixture(_fixture(), "unknown-training-days", session_factory=sessions)
    with sessions() as db:
        raw = ProfileLoader(HealthRepository(db)).load(
            "unknown-training-days", DAY + timedelta(days=1),
            as_of=datetime(2026, 9, 5, tzinfo=timezone.utc),
        )
        assert raw.open_health_load_queried_days == []
        assert raw.open_health_load_upstream_coverage_verified is False
        assert HealthRepository(db).training_range(
            "unknown-training-days", DAY + timedelta(days=1), DAY + timedelta(days=1),
        ) == []


def test_worker_preserves_actual_fetch_time_and_input_event_source_ref(sessions):
    from vitalis.adapters.persistence.input_events import InputEvent

    attempt, claim = _claimed_chunk(sessions)
    now = FETCHED + timedelta(seconds=2)
    result = _ChunkResult(FetchedRecord(RawRecord(
        "heart_rate", "heart_rate:synthetic", START, END, _input().payload,
    )), raw_records=1, fetched_at=FETCHED)
    coordinator = ZeppSyncCoordinator(session_factory=sessions, wall_clock=lambda: now)
    assert coordinator._finalize_success(attempt, claim, result, SimpleNamespace(source_mode="mock"))
    with sessions() as db:
        row = db.scalars(select(SourceJournalRecord)).one()
        assert row.fetched_at == FETCHED.replace(tzinfo=None)
        source_events = db.scalars(select(InputEvent).where(InputEvent.event_type == "source_sync")).all()
        assert source_events and {item.payload_ref for item in source_events} == {f"source_record:{row.id}"}


def test_manager_restores_outer_source_record_context_even_on_unrecognized_raw(sessions):
    with sessions.begin() as db:
        repo = _owner(db)
        db.info["active_source_record_id"] = "outer-context"
        record = FetchedRecord(RawRecord(
            "heart_rate", "synthetic-unrecognized", START, END,
            {"items": [{"unknown": "synthetic-shape"}]},
        ))
        SyncManager(SimpleNamespace())._persist_record(record, repo, User(id="source-owner"))
        assert db.info["active_source_record_id"] == "outer-context"


def _varint(value):
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _synthetic_archive():
    from zipfile import ZipInfo

    inner = b"\x08" + _varint(int(START.timestamp()))
    inner += b"".join(b"\x10" + _varint(value) for value in [70, 255, 72])
    protobuf = b"\x0a" + _varint(len(inner)) + inner
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        info = ZipInfo("SEC_HR.pb", date_time=(2026, 9, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        archive.writestr(info, protobuf)
    return output.getvalue()


def test_binary_dense_raw_replays_locally_with_device_and_missing_sample_preserved(sessions, offline):
    archive = _synthetic_archive()
    assert payload_hash(archive) == payload_hash(_synthetic_archive())
    index = {"items": [{"value": {
        "startTime": START.isoformat(), "deviceId": "SYNTHBALANCE2",
        "samples": [{"s": 0, "e": 3000, "fileType": "sec_hr", "fileId": "synthetic-file"}],
    }}]}
    with sessions.begin() as db:
        _owner(db)
        store = SourceJournalRepository(db)
        store.append(SourceRecordInput(
            user_id="source-owner", source="zepp", source_mode="mock", stream="dense_files",
            record_id="file_info:second_heart_rate:synthetic", observed_start=START,
            observed_end=END, fetched_at=FETCHED, payload=index,
        ))
        binary = store.append(SourceRecordInput(
            user_id="source-owner", source="zepp", source_mode="mock", stream="dense_archive",
            record_id="dense_archive:sec_hr:synthetic-file", observed_start=START,
            observed_end=END, fetched_at=FETCHED, payload=archive,
            raw_schema_version=DENSE_RAW_SCHEMA_VERSION,
        ))
        assert store.read_payload(binary.id, user_id="source-owner") == archive
    result = _replay(sessions, target="dense-replay")
    assert result.records_replayed == 2
    with sessions() as db:
        samples = db.scalars(select(orm.MetricSample).where(
            orm.MetricSample.user_id == "dense-replay",
        ).order_by(orm.MetricSample.timestamp)).all()
        assert [(sample.value, sample.unit, sample.source_scope, sample.device_id) for sample in samples] == [
            (70, "bpm", "device", "SYNTHBALANCE2"), (72, "bpm", "device", "SYNTHBALANCE2"),
        ]
        assert [sample.timestamp for sample in samples] == [START.replace(tzinfo=None), (START + timedelta(seconds=2)).replace(tzinfo=None)]
        assert db.scalars(select(orm.NotificationDelivery)).all() == []


def test_worker_device_inventory_is_journaled_in_the_same_fenced_transaction(sessions):
    attempt, claim = _claimed_chunk(sessions, stream="devices")
    payload = {"items": [{"deviceId": "SYNTHBALANCE2", "additionalInfo": {"productId": "146"}}]}
    result = _ChunkResult(FetchedRecord(RawRecord("devices", "device_inventory", START, END, payload)),
                          raw_records=1, fetched_at=FETCHED)
    coordinator = ZeppSyncCoordinator(session_factory=sessions, wall_clock=lambda: FETCHED)
    assert coordinator._finalize_success(attempt, claim, result, SimpleNamespace(source_mode="mock"))
    with sessions() as db:
        journal = db.scalars(select(SourceJournalRecord)).one()
        assert journal.stream == "devices"
        assert SourceJournalRepository(db).read_payload(journal.id, user_id="source-owner") == payload
        assert len(db.scalars(select(orm.Device)).all()) == 1
