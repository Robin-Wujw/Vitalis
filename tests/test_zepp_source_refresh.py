"""Synthetic safety contracts for the one-off offline source refresh."""
import base64
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.band_sources import BandSourceRecord
from vitalis.adapters.persistence.database import _create_engine, init_db
from vitalis.adapters.persistence.readiness_revisions import ReadinessRevision
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.persistence.sleep_supplements import SleepSupplement
from vitalis.adapters.persistence.source_journal import (
    SourceJournalRecord,
    SourceJournalRepository,
    SourceRecordInput,
)
from vitalis.domain import Device
from vitalis.zepp_source_refresh import refresh_window

DAY = date(2026, 11, 2)
START = datetime(2026, 11, 2, tzinfo=UTC)
FETCHED = START + timedelta(days=1)


@pytest.fixture
def sessions(monkeypatch):
    import httpx
    def forbidden(*args, **kwargs):
        pytest.fail("offline refresh attempted network")
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    engine = _create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as db, db.begin():
        repo = HealthRepository(db)
        repo.upsert_user("synthetic")
        repo.bind_source_mode("synthetic", "mock")
    yield factory
    engine.dispose()


def append(db, stream, payload, *, key=None, fetched=FETCHED, start=START, end=None):
    return SourceJournalRepository(db).append(SourceRecordInput(
        user_id="synthetic", source="zepp", source_mode="mock", stream=stream,
        record_id=key or stream, observed_start=start, observed_end=end or start + timedelta(days=1),
        fetched_at=fetched, payload=payload,
    ))


def stress(value=30, day=DAY):
    return {"items": [{"date": day.isoformat(), "deviceId": "SYNTHETICDEVICE", "avgStress": value,
        "data": [{"time": f"{day}T00:00:00Z", "value": value}]}]}


def refresh(db, **kwargs):
    return refresh_window(db, "synthetic", DAY, DAY, as_of=FETCHED + timedelta(hours=1),
                          timezone_name="UTC", allow_mock=True, **kwargs)


def test_dry_run_is_read_only_and_apply_preserves_unproved_sources(sessions):
    with sessions() as db, db.begin():
        journal = append(db, "wellness", stress(), key="wellness:all_day_stress:one")
        for device, field, reference in (("proved", "all_day_stress.data", journal.id), ("legacy", None, None)):
            db.add(orm.MetricSample(user_id="synthetic", source="zepp", metric="stress", timestamp=START.replace(tzinfo=None),
                value=30, unit="score", source_scope="device", device_id=device, source_field=field, journal_record_id=reference))
        db.add(orm.MetricSample(user_id="synthetic", source="other", metric="stress", timestamp=START.replace(tzinfo=None),
            value=30, unit="score", source_scope="device", device_id="other"))
        db.add(orm.Workout(user_id="synthetic", source="zepp", workout_id="keep", data={"explicit": "unchanged"}))
    with sessions() as db:
        before = db.get(orm.User, "synthetic").analysis_input_revision
        summary = refresh(db)
        assert summary["proven_legacy_removals"] == 1
        assert summary["retained_ambiguous_legacy"] == 1
        assert not db.new and not db.dirty and not db.deleted
        assert db.get(orm.User, "synthetic").analysis_input_revision == before
        assert db.scalar(select(func.count()).select_from(orm.AnalysisJob)) == 0
    with sessions() as db, db.begin():
        refresh(db, apply=True)
        rows = list(db.scalars(select(orm.MetricSample)))
        assert {(r.source, r.source_scope, r.device_id) for r in rows} == {
            ("zepp", "device", "legacy"), ("other", "device", "other"), ("zepp", "user_summary", "")}
        account = next(r for r in rows if r.source_scope == "user_summary")
        assert account.journal_record_id == journal.id
        assert account.fetched_at == FETCHED.replace(tzinfo=None)
        assert db.scalar(select(orm.Workout)).data == {"explicit": "unchanged"}
        jobs = list(db.scalars(select(orm.AnalysisJob)))
        assert jobs and all(j.execution_context["notification_policy"] == "suppress" for j in jobs)
        assert db.scalar(select(func.count()).select_from(orm.NotificationDelivery)) == 0
        assert db.scalar(select(func.count()).select_from(SourceJournalRecord)) == 1
        assert "execution_context" not in db.info


def test_band_retains_sec_hr_and_writes_sources_with_original_time(sessions):
    # Valid merged band minute HR. Legacy NULL attribution cannot prove whether
    # it was band or SEC_HR; never delete it just because the value is equal.
    payload = {"data": {"items": [{"date_time": DAY.isoformat(), "data_hr": base64.b64encode(bytes([72, 73])).decode(),
        "summary": base64.b64encode(json.dumps({"isMerged": True, "sn": "SYNTHETICDEVICE", "stp": {"ttl": 42}}).encode()).decode()}]}}
    with sessions() as db, db.begin():
        append(db, "sleep", payload)
        for field in (None, "ACTIVE_ORIGIN_DATA.content[3]"):
            db.add(orm.MetricSample(user_id="synthetic", source="zepp", metric="heart_rate", timestamp=START.replace(tzinfo=None),
                value=72, unit="bpm", source_scope="device", device_id=field or "legacy", source_field=field))
    with sessions() as db, db.begin():
        refresh(db, apply=True)
        assert db.scalar(select(func.count()).select_from(orm.MetricSample).where(orm.MetricSample.source_scope == "device")) == 2
        assert db.scalar(select(func.count()).select_from(orm.MetricSample).where(orm.MetricSample.source_scope == "user_fused")) == 2
        band = db.scalar(select(BandSourceRecord))
        assert band is not None and band.fetched_at == FETCHED.replace(tzinfo=None)


def test_overlapping_windows_crop_and_newest_readiness_fence(sessions):
    def readiness(revision, value):
        return {"items": [{"eventType": "readiness", "subType": "watch_score", "timestamp": revision,
            "value": {"timestamp": START.isoformat(), "deviceId": "app", "deviceSource": 2, "rdnsScore": value}}]}
    with sessions() as db, db.begin():
        append(db, "wellness", stress(20), key="wellness:all_day_stress:one", fetched=FETCHED - timedelta(minutes=1))
        append(db, "wellness", stress(30), key="wellness:all_day_stress:one")
        append(db, "wellness", stress(90, DAY - timedelta(days=1)), key="wellness:all_day_stress:overlap", start=START - timedelta(days=1), end=START + timedelta(days=1))
        append(db, "daily_summary", readiness("2026-11-03T00:00:00Z", 80), key="events:readiness:watch_score:new")
        append(db, "daily_summary", readiness("2026-11-02T23:00:00Z", 40), key="events:readiness:watch_score:old", fetched=FETCHED + timedelta(minutes=1))
    with sessions() as db, db.begin():
        summary = refresh(db, apply=True)
        assert summary["journals"] == 4
        metrics = list(db.scalars(select(orm.DailyMetric)))
        assert all(row.date == DAY for row in metrics)
        assert next(m.value for m in metrics if m.metric == "stress") == 30
        assert next(m.value for m in metrics if m.metric == "readiness") == 80
        assert db.scalar(select(ReadinessRevision)).revision_at == FETCHED.replace(tzinfo=None)


def test_write_failure_rolls_back_and_restores_execution_context(sessions, monkeypatch):
    with sessions() as db, db.begin():
        append(db, "wellness", stress(), key="wellness:all_day_stress:one")
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic write failure")
    monkeypatch.setattr(HealthRepository, "refresh_daily_features", fail)
    with sessions() as db:
        db.info["execution_context"] = {"outer": True}
        with pytest.raises(RuntimeError), db.begin():
            refresh(db, apply=True)
        assert db.info["execution_context"] == {"outer": True}
        assert db.scalar(select(func.count()).select_from(orm.DailyMetric)) == 0
        assert db.scalar(select(func.count()).select_from(orm.AnalysisJob)) == 0


def test_missing_journal_does_not_clear_facts_and_rejects_mock_by_default(sessions):
    with sessions() as db, db.begin():
        db.add(orm.DailyMetric(user_id="synthetic", date=DAY, metric="stress", source="zepp", value=50, unit="score"))
    with sessions() as db, db.begin():
        result = refresh(db, apply=True)
        assert result["state"] == "prepared"
        assert db.scalar(select(orm.DailyMetric)).value == 50
        with pytest.raises(ValueError, match="live user"):
            refresh_window(db, "synthetic", DAY, DAY, as_of=FETCHED, timezone_name="UTC")
        with pytest.raises(ValueError, match="1–14"):
            refresh_window(db, "synthetic", DAY - timedelta(days=14), DAY, as_of=FETCHED, timezone_name="UTC")


def test_nap_timestamp_alias_preservation_and_readiness_withdrawal(sessions):
    def readiness(revision, value):
        return {"items": [{"eventType": "readiness", "subType": "watch_score", "timestamp": revision,
            "value": {"timestamp": START.isoformat(), "deviceId": "app", "deviceSource": 2, "rdnsScore": value}}]}
    summary = {"isMerged": True, "sn": "SYNTHETICDEVICE", "slp": {"isMerged": True, "odd_stage": []}}
    with sessions() as db, db.begin():
        HealthRepository(db).upsert_device(Device(user_id="synthetic", source="zepp", device_id="AABBCCDDEEFF", aliases=["OLDALIAS1"]))
        append(db, "devices", {"items": [{"macAddress": "AA:BB:CC:DD:EE:FF", "additionalInfo": {"sn": "NEWALIAS1"}}]})
        append(db, "sleep", {"data": {"items": [{"date_time": DAY.isoformat(), "summary": base64.b64encode(json.dumps(summary).encode()).decode()}]}})
        append(db, "daily_summary", readiness("2026-11-03T00:00:00Z", 80), key="events:readiness:watch_score:one")
    with sessions() as db, db.begin():
        refresh(db, apply=True)
        naps = db.scalar(select(SleepSupplement))
        assert naps is not None and naps.naps == []
        assert naps.fetched_at == FETCHED.replace(tzinfo=None)
        assert {"OLDALIAS1", "NEWALIAS1"} <= set(db.scalar(select(orm.Device)).aliases)
        assert db.scalar(select(orm.DailyMetric).where(orm.DailyMetric.metric == "readiness")).value == 80
    with sessions() as db, db.begin():
        append(db, "daily_summary", readiness("2026-11-03T00:10:00Z", 255),
               key="events:readiness:watch_score:one", fetched=FETCHED + timedelta(minutes=10))
    with sessions() as db, db.begin():
        refresh(db, apply=True)
        assert db.scalar(select(orm.DailyMetric).where(orm.DailyMetric.metric == "readiness")) is None
        assert db.scalar(select(ReadinessRevision)).revision_at == (FETCHED + timedelta(minutes=10)).replace(tzinfo=None)
        assert db.scalar(select(func.count()).select_from(SleepSupplement)) == 1
