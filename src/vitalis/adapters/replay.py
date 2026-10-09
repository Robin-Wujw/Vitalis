"""Explicit offline replay of encrypted Zepp journal rows or synthetic fixtures.

The adapter only reuses local parsers and HealthRepository writes. It never
creates a connector, a sync attempt, a credential, or a delivery intent. Every
projection lives in an independent replay user whose origin binding is frozen.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import delete, select, update

from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.database import SessionLocal
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.persistence.source_journal import (
    CURRENT_PARSER_VERSION, DENSE_RAW_SCHEMA_VERSION, RAW_SCHEMA_VERSION,
    MAX_MANIFEST_DAYS, MAX_MANIFEST_RECORDS, ReplayDataset,
    SourceJournalRepository, SourceRecordInput, SourceReplayRun,
    payload_hash,
)
from vitalis.adapters.zepp.dense_hr import decode_sec_hr_archive
from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord
from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.adapters.zepp.sync_manager import StreamReport, SyncManager
from vitalis.application.replay import ReplayCommand, ReplayError, ReplayResult, ReplayService
from vitalis.config import settings
from vitalis.domain import DenseDataFile, User


FIXTURE_SCHEMA_VERSION = "vitalis.source-replay-fixture.v1"
MAX_REPLAY_BYTES = 128 * 1024 * 1024
_FACT_TABLES = (
    orm.WorkoutMetricSample, orm.Workout, orm.SleepRecord, orm.ActivityRecord,
    orm.TrainingRecord, orm.MetricSample, orm.DailyMetric, orm.DenseDataFile, orm.Device,
)


class SourceReplayAdapter:
    """One transaction per explicitly selected replay projection."""

    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or SessionLocal

    def replay(self, command: ReplayCommand) -> ReplayResult:
        with self.session_factory() as db:
            try:
                source_user = db.get(orm.User, command.source_user_id)
                if source_user is None or source_user.source_mode not in {"real", "mock"}:
                    raise ReplayError("replay source requires an explicitly bound live or synthetic dataset")
                store = SourceJournalRepository(db)
                manifest = store.input_manifest(
                    command.source_user_id, start_date=command.start_date, end_date=command.end_date,
                    as_of=command.as_of, journal_ids=command.journal_ids,
                    source="zepp", source_mode=source_user.source_mode, timezone_name=command.timezone_name,
                )
                dataset = _target_dataset(
                    db, command.target_user_id, origin_kind="journal", origin_user_id=command.source_user_id,
                    origin_source="zepp", origin_source_mode=source_user.source_mode,
                )
                cached = _cached_run(db, dataset, manifest, command.parser_version)
                if cached is not None:
                    db.commit()
                    return _result(cached, reused=True)
                if sum(record["payload_bytes"] for record in manifest["records"]) > MAX_REPLAY_BYTES:
                    raise ReplayError("source replay exceeds its bounded payload size")
                target_ids = []
                for metadata in manifest["records"]:
                    raw = store.read_payload(metadata["journal_id"], user_id=command.source_user_id)
                    # The first parser/fetch metadata remains the source's
                    # original metadata even when this run uses a new parser.
                    target = store.append(_metadata_input(
                        metadata, raw, user_id=command.target_user_id,
                        source_mode="replay", origin_journal_id=metadata["journal_id"],
                        origin_source_mode=source_user.source_mode,
                    ))
                    target_ids.append(target.id)
                run = _project(db, dataset, manifest, target_ids, command.parser_version)
                db.commit()
                return _result(run)
            except ReplayError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                # No parser exception text, payload, credentials, SQL values, or
                # partially written data leave the transaction boundary.
                raise ReplayError("source replay transaction failed") from None


def replay_source_records(
    source_user_id: str,
    target_user_id: str,
    *,
    start_date: date,
    end_date: date,
    as_of: datetime,
    journal_ids: Sequence[str] | None = None,
    parser_version: str = CURRENT_PARSER_VERSION,
    timezone_name: str = "UTC",
    session_factory=None,
) -> ReplayResult:
    """CLI composition function; caller supplies explicit independent users."""
    command = ReplayCommand(
        source_user_id=source_user_id, target_user_id=target_user_id,
        start_date=start_date, end_date=end_date, as_of=as_of,
        journal_ids=tuple(journal_ids) if journal_ids is not None else None,
        parser_version=parser_version, timezone_name=timezone_name,
    )
    return ReplayService(SourceReplayAdapter(session_factory=session_factory)).replay(command)


def replay_fixture(
    fixture: Mapping[str, Any],
    target_user_id: str,
    *,
    parser_version: str = CURRENT_PARSER_VERSION,
    session_factory=None,
) -> ReplayResult:
    """Import explicitly synthetic responses and replay their selected as_of.

    The caller reads its JSON file; this boundary does not inspect the filesystem
    or process credentials. Late fixture responses are retained in the journal
    but cannot enter an earlier as_of projection.
    """
    origin, start_date, end_date, as_of, timezone_name, inputs = _fixture_inputs(fixture, target_user_id)
    if not parser_version or len(parser_version) > 96:
        raise ReplayError("replay parser version is invalid")
    factory = session_factory or SessionLocal
    with factory() as db:
        try:
            dataset = _target_dataset(
                db, target_user_id, origin_kind="fixture", origin_user_id=origin,
                origin_source="zepp", origin_source_mode="mock",
            )
            store = SourceJournalRepository(db)
            for item in inputs:
                store.append(item)
            manifest = store.input_manifest(
                target_user_id, start_date=start_date, end_date=end_date,
                as_of=as_of, source="zepp", source_mode="replay", timezone_name=timezone_name,
            )
            cached = _cached_run(db, dataset, manifest, parser_version)
            if cached is not None:
                db.commit()
                return _result(cached, reused=True)
            run = _project(db, dataset, manifest, [row["journal_id"] for row in manifest["records"]], parser_version)
            db.commit()
            return _result(run)
        except ReplayError:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise ReplayError("source fixture replay transaction failed") from None


def _target_dataset(db, user_id, *, origin_kind, origin_user_id, origin_source, origin_source_mode):
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 64:
        raise ReplayError("replay target user is invalid")
    if settings.env not in {"dev", "test"}:
        raise ReplayError("offline replay requires a development or test dataset")
    repo = HealthRepository(db)
    user = db.get(orm.User, user_id)
    if user is None:
        repo.upsert_user(user_id)
        repo.bind_source_mode(user_id, "replay")
        dataset = ReplayDataset(
            user_id=user_id, origin_kind=origin_kind, origin_user_id=origin_user_id,
            origin_source=origin_source, origin_source_mode=origin_source_mode,
        )
        db.add(dataset)
        db.flush()
    else:
        # Serialize projection commits with analysis publication and competing
        # replays using the existing user-first write-lock convention.
        db.execute(update(orm.User).where(orm.User.id == user_id).values(
            analysis_input_revision=orm.User.analysis_input_revision,
        ))
        dataset = db.get(ReplayDataset, user_id)
        if user.source_mode != "replay" or dataset is None:
            raise ReplayError("replay target must be a new independent user or its bound replay dataset")
        if (dataset.origin_kind, dataset.origin_user_id, dataset.origin_source, dataset.origin_source_mode) != (
            origin_kind, origin_user_id, origin_source, origin_source_mode,
        ):
            raise ReplayError("replay target is bound to a different source dataset")
    if db.scalar(select(orm.SourceAccount.id).where(orm.SourceAccount.user_id == user_id).limit(1)) is not None:
        raise ReplayError("replay target cannot own a vendor source account")
    return dataset


def _run_id(dataset, manifest, parser_version):
    return payload_hash({
        "target_user_id": dataset.user_id, "manifest_hash": manifest["manifest_hash"],
        "parser_version": parser_version,
    })


def _cached_run(db, dataset, manifest, parser_version):
    run_id = _run_id(dataset, manifest, parser_version)
    if dataset.current_run_id != run_id:
        return None
    return db.get(SourceReplayRun, run_id)


def _project(db, dataset, manifest, target_ids, parser_version):
    if sum(record["payload_bytes"] for record in manifest["records"]) > MAX_REPLAY_BYTES:
        raise ReplayError("source replay exceeds its bounded payload size")
    repo = HealthRepository(db)
    store = SourceJournalRepository(db)
    previous = db.get(SourceReplayRun, dataset.current_run_id) if dataset.current_run_id else None
    # Parser corrections can remove a fact or a field. Rebuild only this
    # isolated dataset's normalized source projection, preserving all journals,
    # replay audits, analysis snapshots, and explicit user inputs.
    for model in _FACT_TABLES:
        db.execute(delete(model).where(model.user_id == dataset.user_id))
    db.info["source_journal_context"] = {"replay_prejournaled": True}
    manager = SyncManager(None, dense_archive_budget=0)
    outcomes = []
    partial = unrecognized = written = 0
    try:
        # Facts are applied in original acquisition order. Details and archives
        # follow summaries/indexes within a single acquisition timestamp.
        priority = {"devices": 0, "workouts": 1, "dense_files": 1, "workout_detail": 2, "dense_archive": 2}
        ordered = sorted(target_ids, key=lambda item: (
            store.get(item, user_id=dataset.user_id).fetched_at,
            priority.get(store.get(item, user_id=dataset.user_id).stream, 1), item,
        ))
        for journal_id in ordered:
            row = store.get(journal_id, user_id=dataset.user_id)
            raw = store.read_payload(journal_id, user_id=dataset.user_id)
            db.info["active_source_record_id"] = journal_id
            if row.stream == "dense_archive":
                if row.raw_schema_version != DENSE_RAW_SCHEMA_VERSION or not isinstance(raw, bytes):
                    raise ReplayError("dense replay raw schema is unsupported")
                report = _parse_dense_archive(repo, dataset.user_id, row, raw)
            else:
                if row.raw_schema_version != RAW_SCHEMA_VERSION or row.payload_format != "json":
                    raise ReplayError("source replay raw schema is unsupported")
                fetched = FetchedRecord(RawRecord(
                    row.stream, row.record_id, _timestamp(row.observed_start),
                    _timestamp(row.observed_end) if row.observed_end is not None else None,
                    raw, row.capability,
                ), incomplete=row.incomplete)
                if row.stream == "devices":
                    devices = ZeppParser().parse_devices(raw)
                    for device in devices:
                        device.user_id = dataset.user_id
                        repo.upsert_device(device)
                    report = StreamReport("devices", "success", records_written=len(devices),
                                          parse_status="success", write_status="success")
                elif row.stream in {"sleep", "heart_rate", "workouts", "workout_detail", "daily_summary", "hrv", "wellness", "dense_files"}:
                    report = manager._persist_record(
                        fetched, repo, User(id=dataset.user_id), fetched_at=_timestamp(row.fetched_at),
                    )
                else:
                    raise ReplayError("source replay stream is unsupported")
            if report.status == "failed":
                raise ReplayError("source replay parsing failed") from None
            # Restore the original acquisition time on workout detail facts. The
            # existing parser writer defaults to now for live synchronization.
            if row.stream == "workout_detail":
                workout_id = row.record_id.split(":", 2)[1]
                workout = repo.workout(dataset.user_id, workout_id, source="zepp")
                if workout is not None and workout.detail is not None:
                    detail = dict(workout.detail)
                    detail["fetched_at"] = _timestamp(row.fetched_at).isoformat().replace("+00:00", "Z")
                    workout.detail = detail
            written += report.records_written
            partial += int(row.incomplete)
            unrecognized += int(report.parse_status == "unrecognized")
            outcomes.append({
                "journal_id": journal_id, "stream": row.stream,
                "parse_status": report.parse_status, "write_status": report.write_status,
                "records_written": report.records_written, "incomplete": row.incomplete,
            })
    except ReplayError:
        raise
    except Exception:
        raise ReplayError("source replay parsing failed") from None
    finally:
        db.info.pop("source_journal_context", None)
        db.info.pop("active_source_record_id", None)
    run_id = _run_id(dataset, manifest, parser_version)
    run = db.get(SourceReplayRun, run_id)
    if run is None:
        run = SourceReplayRun(
            id=run_id, target_user_id=dataset.user_id, parser_version=parser_version,
            manifest_hash=manifest["manifest_hash"], input_manifest=manifest, outcomes=outcomes,
            records_replayed=len(target_ids), records_written=written,
            partial_records=partial, unrecognized_records=unrecognized,
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(run)
    dataset.current_run_id = run_id
    # Deletions as well as new values invalidate the previous input window.
    dates = set()
    for selection in [manifest["selection"], *([previous.input_manifest["selection"]] if previous else [])]:
        start = date.fromisoformat(selection["start_date"])
        end = date.fromisoformat(selection["end_date"])
        dates.update(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    repo.bump_analysis_input_revision(dataset.user_id)
    repo.enqueue_analysis_job(
        dataset.user_id, max(dates), event_type="source_sync", source="zepp",
        affected_dates=dates, affected_streams={"sleep", "activity", "training", "heart_rate", "hrv", "wellness", "daily_summary", "dense_files"},
        reason="offline replay projection changed", delivery_period=None,
    )
    db.flush()
    return run


def _parse_dense_archive(repo, user_id, journal_row, archive):
    parts = journal_row.record_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "dense_archive":
        raise ReplayError("dense replay record identity is invalid")
    indexed = repo.dense_data_file_group(user_id, "second_heart_rate", parts[2], source="zepp")
    files = [DenseDataFile(
        user_id=user_id, source="zepp", stream=row.stream, file_id=row.file_id,
        file_type=row.file_type, date=row.date,
        start_utc=_timestamp(row.start_utc) if row.start_utc else None,
        end_utc=_timestamp(row.end_utc) if row.end_utc else None,
        source_scope=row.source_scope, device_id=row.device_id,
        parse_status=row.parse_status, sample_count=row.sample_count,
    ) for row in indexed]
    decoded = decode_sec_hr_archive(archive, files)
    for sample in decoded.samples:
        sample.user_id = user_id
    written = repo.save_dense_data_files(decoded.files) + repo.save_metric_samples(decoded.samples)
    return StreamReport("dense_archive", "success", records_written=written,
                        parse_status="success", write_status="success")


def _result(run, *, reused=False):
    return ReplayResult(
        run_id=run.id, user_id=run.target_user_id, source_mode="replay",
        parser_version=run.parser_version, manifest_hash=run.manifest_hash,
        records_replayed=run.records_replayed, records_written=run.records_written,
        partial_records=run.partial_records, unrecognized_records=run.unrecognized_records,
        reused=reused,
    )


def _metadata_input(metadata, payload, *, user_id, source_mode, origin_journal_id, origin_source_mode):
    return SourceRecordInput(
        user_id=user_id, source=metadata["source"], source_mode=source_mode,
        stream=metadata["stream"], record_id=metadata["record_id"],
        observed_start=_parse_time(metadata["observed_start"]),
        observed_end=_parse_time(metadata["observed_end"]) if metadata["observed_end"] else None,
        fetched_at=_parse_time(metadata["fetched_at"]), payload=payload,
        raw_schema_version=metadata["raw_schema_version"], parser_version=metadata["parser_version"],
        source_account_id=metadata["source_account_id"], source_account_epoch=metadata["source_account_epoch"],
        source_scope=metadata["source_scope"], device_id=metadata["device_id"],
        capability=metadata["capability"], incomplete=metadata["incomplete"], time_basis=metadata["time_basis"],
        origin_journal_id=origin_journal_id, origin_source_mode=origin_source_mode,
    )


def _fixture_inputs(fixture, target_user_id):
    try:
        if (not isinstance(fixture, Mapping) or fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION
            or fixture.get("synthetic") is not True or fixture.get("source") != "zepp"):
            raise ReplayError("fixture requires an explicit supported synthetic source declaration")
        origin = fixture["dataset_id"]
        if not isinstance(origin, str) or not origin.strip() or len(origin) > 128:
            raise ReplayError("fixture dataset identity is invalid")
        start_date = date.fromisoformat(fixture["start_date"])
        end_date = date.fromisoformat(fixture["end_date"])
        if start_date > end_date or (end_date - start_date).days + 1 > MAX_MANIFEST_DAYS:
            raise ReplayError("fixture date window exceeds its bounded range")
        as_of = _parse_time(fixture["as_of"])
        timezone_name = fixture.get("timezone", "UTC")
        records = fixture["records"]
        if not isinstance(records, list) or len(records) > MAX_MANIFEST_RECORDS:
            raise ReplayError("fixture records exceed their bounded limit")
        inputs = []
        total_bytes = 0
        for record in records:
            if not isinstance(record, Mapping) or record.get("source", "zepp") != "zepp":
                raise ReplayError("fixture cannot mix vendor sources")
            item = SourceRecordInput(
                user_id=target_user_id, source="zepp", source_mode="replay",
                stream=record["stream"], record_id=record["record_id"],
                observed_start=_parse_time(record["observed_start"]),
                observed_end=_parse_time(record["observed_end"]) if record.get("observed_end") else None,
                fetched_at=_parse_time(record["fetched_at"]), payload=record["payload"],
                raw_schema_version=record.get("raw_schema_version", RAW_SCHEMA_VERSION),
                parser_version=record.get("parser_version", CURRENT_PARSER_VERSION),
                source_scope=record.get("source_scope"), device_id=record.get("device_id"),
                capability=record.get("capability", "verified"), incomplete=record.get("incomplete", False),
                time_basis=record.get("time_basis", "request_window"), origin_source_mode="mock",
            )
            # Validation/hashing accepts only strict JSON and never formats raw
            # values into an error. Per-record and total replay sizes are bounded.
            payload_hash(item.payload)
            import json
            total_bytes += len(json.dumps(item.payload, ensure_ascii=False).encode("utf-8"))
            if total_bytes > MAX_REPLAY_BYTES:
                raise ReplayError("fixture payloads exceed their bounded size")
            inputs.append(item)
        inputs.sort(key=lambda item: item.fetched_at)
        return origin, start_date, end_date, as_of, timezone_name, inputs
    except ReplayError:
        raise
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ReplayError("source fixture metadata is invalid") from None


def _timestamp(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayError("replay timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "FIXTURE_SCHEMA_VERSION", "SourceReplayAdapter", "replay_fixture", "replay_source_records",
]
