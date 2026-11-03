"""Bounded offline repair of Zepp report-source attribution, not a replay apply.

The caller owns the transaction and must stop competing input writers. Existing
unattributed legacy rows are retained: a NULL provenance field cannot distinguish
band heart rate from SEC_HR. Only exact observations with positive provenance are
removed when their source identity changes. No workout or explicit-input table is
written. Each CLI batch commits independently; dry-run never mutates the database.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import defer

from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.persistence.source_journal import (
    RAW_SCHEMA_VERSION,
    SourceJournalRecord,
    SourceJournalRepository,
)
from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord
from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.adapters.zepp.sync_manager import SyncManager
from vitalis.domain import User
from vitalis.domain.features import DAILY_FEATURE_METRICS
from vitalis.time import local_day, local_day_utc_bounds

MAX_BATCH_DAYS = 14
MAX_DAYS = 180
MAX_RECORDS = 5000
MAX_FACTS = 150_000
MAX_BYTES = 128 * 1024 * 1024


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _identity(item, sample=False):
    base = (item.source, item.metric, _naive(item.timestamp) if sample else item.date,
            item.source_scope, item.device_id or "")
    return (*base, item.source_record_id or "", item.sample_ordinal or 0) if sample else base


@dataclass
class _Prepared:
    start: date
    end: date
    zone: str
    journal: Any = None
    facts: dict[str, dict] = field(default_factory=lambda: {
        name: {} for name in ("daily", "samples", "metrics", "band", "naps", "readiness", "devices")
    })

    def _day(self, day):
        return self.start <= day <= self.end

    def _put(self, kind, key, value):
        self.facts[kind][key] = (value, self.journal)
        if sum(map(len, self.facts.values())) > MAX_FACTS:
            raise ValueError("source refresh exceeds normalized fact budget")

    def save_daily(self, daily):
        if self._day(daily.date):
            self._put("daily", (daily.date, "sleep" if daily.sleep else "activity"), daily)

    def save_band_source(self, user_id, day, kind, payload):
        if self._day(day):
            self._put("band", (day, kind, payload.get("source_scope"), payload.get("device_id")),
                      (user_id, day, kind, payload))
        return 1

    def save_naps(self, user_id, day, naps):
        if self._day(day):
            self._put("naps", day, (user_id, day, naps))
        return 1

    def save_metric_samples(self, samples):
        for item in samples:
            if self._day(local_day(item.timestamp, self.zone)):
                self._put("samples", _identity(item, True), item)
        return len(samples)

    def save_daily_metrics(self, metrics):
        for item in metrics:
            if self._day(item.date):
                self._put("metrics", _identity(item), item)
        return len(metrics)

    def replace_readiness(self, user_id, revisions, metrics, samples, *, timezone_name):
        for revision in revisions:
            day = revision["day"]
            if not self._day(day):
                continue
            key = (day, revision["source_scope"], revision.get("device_id") or "")
            previous = self.facts["readiness"].get(key)
            if previous:
                old = previous[0][1]["revision_at"]
                new = revision["revision_at"]
                if new is None or (old is not None and _utc(new) <= _utc(old)):
                    continue
            chosen_metrics = [m for m in metrics if (m.date, m.source_scope, m.device_id or "") == key]
            chosen_samples = [s for s in samples if (local_day(s.timestamp, self.zone), s.source_scope, s.device_id or "") == key]
            self._put("readiness", key, (user_id, revision, chosen_metrics, chosen_samples))
        return len(metrics) + len(samples)


def _selected(db, user_id, mode, start, end, as_of, zone):
    begin, _ = local_day_utc_bounds(start, zone)
    _, finish = local_day_utc_bounds(end, zone)
    rows = db.scalars(select(SourceJournalRecord).options(defer(SourceJournalRecord.encrypted_payload)).where(
        SourceJournalRecord.user_id == user_id, SourceJournalRecord.source == "zepp",
        SourceJournalRecord.source_mode == mode,
        or_(SourceJournalRecord.stream.in_(("sleep", "daily_summary", "devices")),
            (SourceJournalRecord.stream == "wellness") & SourceJournalRecord.record_id.startswith("wellness:all_day_stress:")),
        SourceJournalRecord.observed_start < _naive(finish),
        or_(SourceJournalRecord.observed_end > _naive(begin),
            (or_(SourceJournalRecord.observed_end.is_(None), SourceJournalRecord.observed_end == SourceJournalRecord.observed_start))
            & (SourceJournalRecord.observed_start >= _naive(begin))),
        SourceJournalRecord.fetched_at <= _naive(as_of),
    ).order_by(SourceJournalRecord.fetched_at, SourceJournalRecord.id).limit(MAX_RECORDS + 1)).all()
    if len(rows) > MAX_RECORDS:
        raise ValueError("source refresh exceeds journal record budget; narrow the batch")
    selected = {}
    for row in rows:
        key = (row.source, row.stream, row.record_id, row.observed_start, row.observed_end,
               row.source_account_id, row.source_account_epoch)
        selected[key] = row
    output = sorted(selected.values(), key=lambda row: (row.fetched_at, row.id))
    if sum(row.payload_bytes for row in output) > MAX_BYTES:
        raise ValueError("source refresh exceeds payload budget; narrow the batch")
    return output


def _prepare(db, user_id, mode, start, end, as_of, zone):
    prepared = _Prepared(start, end, zone)
    manager = SyncManager(SimpleNamespace(timezone_name=zone), dense_archive_budget=0)
    store = SourceJournalRepository(db)
    rows = _selected(db, user_id, mode, start, end, as_of, zone)
    skipped = 0
    for row in rows:
        if row.incomplete:
            skipped += 1
            continue
        if row.raw_schema_version != RAW_SCHEMA_VERSION or row.payload_format != "json":
            raise ValueError("source refresh raw schema is unsupported")
        raw = store.read_payload(row.id, user_id=user_id)
        db.expire(row, ["encrypted_payload"])
        prepared.journal = row
        if row.stream == "devices":
            for device in ZeppParser().parse_devices(raw):
                device.user_id = user_id
                prepared._put("devices", (device.source, device.device_id), device)
        else:
            report = manager._persist_record(FetchedRecord(RawRecord(
                row.stream, row.record_id, _utc(row.observed_start),
                _utc(row.observed_end) if row.observed_end else None, raw, row.capability,
            ), incomplete=False), prepared, User(id=user_id), fetched_at=_utc(row.fetched_at))
            if report.status == "failed":
                raise ValueError("source refresh parser failed")
            skipped += int(report.parse_status == "unrecognized")
    prepared.journal = None
    return prepared, rows, skipped


def _legacy_matches(db, user_id, prepared, selected):
    """Positive proof plus exact value/time, never NULL-as-proof for band/SEC_HR."""
    journal_streams = {row.id: row.stream for row in selected}
    removals = {orm.MetricSample: set(), orm.DailyMetric: set()}
    ambiguous = set()
    observations = [(True, value, journal) for value, journal in prepared.facts["samples"].values()]
    observations += [(False, value, journal) for value, journal in prepared.facts["metrics"].values()]
    for (owner, revision, metrics, samples), journal in prepared.facts["readiness"].values():
        observations.extend((True, value, journal) for value in samples)
        observations.extend((False, value, journal) for value in metrics)
    # One bounded read per table rather than one query for each sample.
    begin, _ = local_day_utc_bounds(prepared.start, prepared.zone)
    _, finish = local_day_utc_bounds(prepared.end, prepared.zone)
    for sample, model in ((True, orm.MetricSample), (False, orm.DailyMetric)):
        incoming = {}
        for is_sample, item, journal in observations:
            if is_sample == sample:
                key = (item.metric, _naive(item.timestamp) if sample else item.date, item.value, item.unit)
                incoming.setdefault(key, []).append((item, journal))
        if not incoming:
            continue
        scope = [model.timestamp >= _naive(begin), model.timestamp < _naive(finish)] if sample else [model.date.between(prepared.start, prepared.end)]
        query = select(model).where(model.user_id == user_id, model.source == "zepp", *scope,
                                    model.metric.in_({key[0] for key in incoming}))
        for old in db.scalars(query.execution_options(yield_per=5000)):
            key = (old.metric, old.timestamp if sample else old.date, old.value, old.unit)
            for item, journal in incoming.get(key, []):
                if (old.source_scope, old.device_id or "") == (item.source_scope, item.device_id or ""):
                    continue
                # Only move legacy non-account identities into a proven account
                # result. Independent device/unknown streams never migrate.
                if item.source_scope not in {"user_summary", "user_fused"}:
                    continue
                if old.source_scope in {"user_summary", "user_fused"}:
                    ambiguous.add((model.__tablename__, old.id))
                    continue
                if old.fetched_at and old.fetched_at > journal.fetched_at:
                    continue
                if old.revision_at and (item.revision_at is None or old.revision_at > _naive(item.revision_at)):
                    continue
                original = old.journal_record_id if sample else old.source_record_id
                contributor = getattr(item, "contributing_device_id", None)
                field_proof = bool(item.source_field and old.source_field == item.source_field
                                   and contributor and old.device_id == contributor)
                journal_proof = bool(original and original == journal.id)
                # SEC_HR and ACTIVE_ORIGIN_DATA must survive even equal values.
                if sample and item.metric == "heart_rate":
                    field_proof = field_proof and old.source_field == "band_data.data_hr"
                    journal_proof = journal_proof and journal_streams.get(original) == "sleep"
                if field_proof or journal_proof:
                    removals[model].add(old.id)
                else:
                    ambiguous.add((model.__tablename__, old.id))
    return removals, len(ambiguous)


def _write_observations(repo, prepared):
    # Group by original journal, retaining that acquisition time and source ID.
    # Sample writers refresh one day's features once per group, not once per point.
    for kind in ("metrics", "samples"):
        groups = {}
        for item, journal in prepared.facts[kind].values():
            groups.setdefault(journal.id, []).append(item)
        for journal_id, items in groups.items():
            repo.db.info["active_source_record_id"] = journal_id
            if kind == "metrics":
                repo.save_daily_metrics(items)
            else:
                repo.save_metric_samples(items)


def refresh_window(db, user_id: str, start: date, end: date, *, as_of: datetime,
                   timezone_name: str, apply: bool = False, allow_mock: bool = False) -> dict:
    """Prepare then optionally write one <=14-day batch in the caller transaction.

    No commits, clients or notification sends occur here. The operator must stop
    competing writers, use one frozen cutoff for all batches, and commit/rollback.
    No raw health values, device IDs or journal IDs are returned.
    """
    if type(start) is not date or type(end) is not date or not 1 <= (end - start).days + 1 <= MAX_BATCH_DAYS:
        raise ValueError("source refresh batch must cover 1–14 local days")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("source refresh cutoff requires an explicit timezone")
    owner = db.get(orm.User, user_id)
    if owner is None or owner.source_mode not in ({"real", "mock"} if allow_mock else {"real"}):
        raise ValueError("source refresh requires an explicitly bound live user")
    prepared, selected, skipped = _prepare(db, user_id, owner.source_mode, start, end, as_of, timezone_name)
    removals, ambiguous = _legacy_matches(db, user_id, prepared, selected)
    counts = {name: len(items) for name, items in prepared.facts.items()}
    result = {"state": "prepared", "start": start.isoformat(), "end": end.isoformat(),
              "journals": len(selected), "skipped": skipped, "prepared": counts,
              "proven_legacy_removals": sum(map(len, removals.values())),
              "retained_ambiguous_legacy": ambiguous, "notification_policy": "suppress"}
    if not apply or not any(counts.values()):
        return result
    db.execute(update(orm.User).where(orm.User.id == user_id).values(analysis_input_revision=orm.User.analysis_input_revision))
    repo = HealthRepository(db)
    keys = ("execution_context", "active_source_record_id", "source_journal_context")
    old_info = {key: db.info[key] for key in keys if key in db.info}
    db.info["execution_context"] = {"notification_policy": "suppress", "origin": "zepp_source_refresh"}
    db.info["source_journal_context"] = {"replay_prejournaled": True}
    try:
        for model, ids in removals.items():
            values = sorted(ids)
            for offset in range(0, len(values), 500):
                db.execute(delete(model).where(model.user_id == user_id, model.source == "zepp", model.id.in_(values[offset:offset + 500])))
        _write_observations(repo, prepared)
        for kind in ("devices", "daily", "band", "naps", "readiness"):
            for value, journal in prepared.facts[kind].values():
                db.info["active_source_record_id"] = journal.id
                if kind == "devices":
                    existing = next((d for d in repo.devices(user_id) if d.source == value.source and d.device_id == value.device_id), None)
                    if existing:
                        value.aliases = sorted(set(value.aliases or []) | set(existing.aliases or []))
                    repo.upsert_device(value, target_date=end)
                elif kind == "daily":
                    model = orm.SleepRecord if value.sleep else orm.ActivityRecord
                    current = db.scalar(select(model).where(model.user_id == user_id, model.date == value.date))
                    if current is not None and (current.data or {}).get("source") not in (None, "zepp"):
                        raise ValueError("source refresh refuses another source's daily projection")
                    repo.save_daily(value)
                elif kind == "band":
                    repo.save_band_source(*value, fetched_at=_utc(journal.fetched_at))
                elif kind == "naps":
                    repo.save_naps(*value, fetched_at=_utc(journal.fetched_at))
                else:
                    uid, revision, metrics, samples = value
                    repo.replace_readiness(uid, [revision], metrics, samples, timezone_name=timezone_name)
        dates = set()
        for kind in ("daily", "band", "naps", "metrics", "samples", "readiness"):
            for key in prepared.facts[kind]:
                if kind == "samples":
                    dates.add(local_day(key[2], timezone_name))
                elif kind == "metrics":
                    dates.add(key[2])
                else:
                    dates.add(key if kind == "naps" else key[0])
        # Include deletions and recompute historical dense features, not just the
        # two days for which analysis reads detailed samples.
        for day in sorted(dates):
            for metric in DAILY_FEATURE_METRICS:
                repo.refresh_daily_features(user_id, metric, day, timezone_name=timezone_name)
        repo.bump_analysis_input_revision(user_id)
        repo.enqueue_analysis_job(user_id, end, event_type="source_sync", source="zepp",
            affected_dates=dates or {end}, affected_streams={"sleep", "activity", "daily_metrics", "band_source_records", "readiness", "heart_rate", "stress"},
            reason="offline source attribution refreshed", delivery_period=None)
        db.flush()
    finally:
        for key in keys:
            db.info.pop(key, None)
        db.info.update(old_info)
    return {**result, "state": "applied"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--batch-days", type=int, default=14)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= (args.end - args.start).days + 1 <= MAX_DAYS or not 1 <= args.batch_days <= MAX_BATCH_DAYS:
        parser.error("range must cover 1–180 days and batches 1–14 days")
    from vitalis.adapters.persistence.database import SessionLocal, check_schema
    try:
        cutoff = datetime.fromisoformat(args.as_of)
        check_schema()
        cursor = args.start
        while cursor <= args.end:
            end = min(args.end, cursor + timedelta(days=args.batch_days - 1))
            with SessionLocal() as db, db.begin():
                summary = refresh_window(db, args.user, cursor, end, as_of=cutoff,
                    timezone_name=args.timezone, apply=args.apply)
            print(json.dumps(summary, sort_keys=True))
            cursor = end + timedelta(days=1)
        return 0
    except Exception:  # noqa: BLE001 -- privacy boundary: never print source-bearing SQL/parse exceptions
        # SQL/parser exceptions can contain source values. Earlier batches may
        # already be committed; the failing batch has rolled back completely.
        print(json.dumps({"state": "failed", "failure_code": "source_refresh_failed", "next_action": "inspect_batch_locally"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
