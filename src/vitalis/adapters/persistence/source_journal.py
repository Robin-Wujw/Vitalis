"""Encrypted immutable source responses and bounded metadata input manifests.

All writes use the caller's SQLAlchemy transaction. There is no commit, rollback,
network client, or payload logging in this repository. A response's first fetch
and parser metadata never change; a different response or source qualification
gets a different journal ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from cryptography.fernet import InvalidToken
from sqlalchemy import Boolean, CheckConstraint, DDL, DateTime, ForeignKey, Index, Integer, String, Text, event, or_, select
from sqlalchemy.orm import Mapped, Session, load_only, mapped_column
from sqlalchemy.types import JSON

from vitalis.adapters import credentials
from vitalis.application.replay import ReplayError, manifest_digest
from vitalis.config import settings

from .database import Base
from . import models as orm


RAW_SCHEMA_VERSION = "zepp-json-v1"
DENSE_RAW_SCHEMA_VERSION = "zepp-sec-hr-v1"
CURRENT_PARSER_VERSION = "zepp-parser-v1"
MANIFEST_VERSION = "source-input-manifest-v1"
MAX_MANIFEST_DAYS = 730
MAX_MANIFEST_RECORDS = 5_000
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
_MODES = {"real", "mock", "replay"}


class JournalError(ReplayError):
    """An invalid journal request or an unavailable encryption boundary."""


class SourceJournalRecord(Base):
    """An encrypted raw response; repeated identical responses reuse this row."""

    __tablename__ = "source_journal_records"
    __table_args__ = (
        CheckConstraint("source_mode IN ('real', 'mock', 'replay')", name="ck_source_journal_mode"),
        CheckConstraint("origin_source_mode IN ('real', 'mock')", name="ck_source_journal_origin_mode"),
        CheckConstraint("observed_end IS NULL OR observed_end >= observed_start", name="ck_source_journal_window"),
        CheckConstraint(
            "(source_account_id IS NULL AND source_account_epoch IS NULL) OR "
            "(source_account_id IS NOT NULL AND source_account_epoch >= 0)",
            name="ck_source_journal_account_epoch",
        ),
        CheckConstraint(
            "encrypted_payload LIKE 'fernet:%' OR "
            "(origin_source_mode = 'mock' AND encrypted_payload LIKE 'mock-fernet:%')",
            name="ck_source_journal_encryption",
        ),
        CheckConstraint("payload_format IN ('json', 'bytes')", name="ck_source_journal_payload_format"),
        Index("ix_source_journal_scope_time", "user_id", "source", "fetched_at", "observed_start"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    source_mode: Mapped[str] = mapped_column(String(16))
    stream: Mapped[str] = mapped_column(String(96))
    record_id: Mapped[str] = mapped_column(String(256))
    # RawRecord.start/end describe the requested observation window. The
    # qualification prevents consumers from treating it as measured coverage.
    time_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    observed_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_format: Mapped[str] = mapped_column(String(8), nullable=False)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_schema_version: Mapped[str] = mapped_column(String(48), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(96), nullable=False)
    # Snapshot strings intentionally have no FK to mutable/revocable accounts.
    source_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_account_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capability: Mapped[str] = mapped_column(String(24), nullable=False)
    incomplete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    origin_journal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_source_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)


class ReplayDataset(Base):
    """An independent replay user's immutable origin binding and current run."""

    __tablename__ = "replay_datasets"
    __table_args__ = (
        CheckConstraint("origin_kind IN ('journal', 'fixture')", name="ck_replay_dataset_origin"),
        CheckConstraint("origin_source_mode IN ('real', 'mock')", name="ck_replay_dataset_mode"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    origin_kind: Mapped[str] = mapped_column(String(16))
    origin_user_id: Mapped[str] = mapped_column(String(128))
    origin_source: Mapped[str] = mapped_column(String(32))
    origin_source_mode: Mapped[str] = mapped_column(String(16))
    current_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SourceReplayRun(Base):
    """Immutable replay audit, separate from the currently parsed projection."""

    __tablename__ = "source_replay_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parser_version: Mapped[str] = mapped_column(String(96))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    input_manifest: Mapped[dict] = mapped_column(JSON)
    outcomes: Mapped[list] = mapped_column(JSON)
    records_replayed: Mapped[int] = mapped_column(Integer)
    records_written: Mapped[int] = mapped_column(Integer)
    partial_records: Mapped[int] = mapped_column(Integer)
    unrecognized_records: Mapped[int] = mapped_column(Integer)
    completed_at: Mapped[datetime] = mapped_column(DateTime)


@event.listens_for(SourceJournalRecord, "before_update")
@event.listens_for(SourceReplayRun, "before_update")
def _reject_source_update(_mapper, _connection, _target) -> None:
    raise JournalError("source records and completed replay runs are immutable")


# SQL updates cannot bypass immutability. Deleting an explicitly deleted user
# remains possible through FK cascades; ordinary journal operations never delete.
event.listen(SourceJournalRecord.__table__, "after_create", DDL(
    "CREATE TRIGGER vitalis_source_journal_no_update BEFORE UPDATE ON source_journal_records "
    "BEGIN SELECT RAISE(ABORT, 'source journal records are immutable'); END"
).execute_if(dialect="sqlite"))
event.listen(SourceJournalRecord.__table__, "after_create", DDL(
    "CREATE OR REPLACE FUNCTION vitalis_source_journal_no_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'source journal records are immutable'; END; $$"
).execute_if(dialect="postgresql"))
event.listen(SourceJournalRecord.__table__, "after_create", DDL(
    "CREATE TRIGGER vitalis_source_journal_no_update BEFORE UPDATE ON source_journal_records "
    "FOR EACH ROW EXECUTE FUNCTION vitalis_source_journal_no_update()"
).execute_if(dialect="postgresql"))


@dataclass(frozen=True)
class SourceRecordInput:
    user_id: str
    source: str
    source_mode: str
    stream: str
    record_id: str
    observed_start: datetime
    observed_end: datetime | None
    fetched_at: datetime
    payload: Any = field(repr=False)
    raw_schema_version: str = RAW_SCHEMA_VERSION
    parser_version: str = CURRENT_PARSER_VERSION
    source_account_id: str | None = None
    source_account_epoch: int | None = None
    source_scope: str | None = None
    device_id: str | None = None
    capability: str = "verified"
    incomplete: bool = False
    time_basis: str = "request_window"
    origin_journal_id: str | None = None
    origin_source_mode: str | None = None


_METADATA_COLUMNS = tuple(
    column for column in SourceJournalRecord.__table__.columns
    if column.name != "encrypted_payload"
)


class SourceJournalRepository:
    """Journal operations in the caller's transaction, with no implicit commit."""

    def __init__(self, db: Session):
        self.db = db

    def append(self, item: SourceRecordInput) -> SourceJournalRecord:
        _validate_input(item)
        self.db.flush()
        owner = self.db.get(orm.User, item.user_id)
        if owner is None or owner.source_mode != item.source_mode:
            raise JournalError("source journal requires its owner's bound source mode")
        origin_mode = item.origin_source_mode or item.source_mode
        if item.source_mode == "replay":
            dataset = self.db.get(ReplayDataset, item.user_id)
            if dataset is None or dataset.origin_source != item.source or dataset.origin_source_mode != origin_mode:
                raise JournalError("source journal replay origin does not match its dataset")
            if dataset.origin_kind == "journal":
                origin = self.get(item.origin_journal_id or "", user_id=dataset.origin_user_id)
                if (origin.source != item.source or origin.source_mode != origin_mode
                    or origin.source_account_id != item.source_account_id
                    or origin.source_account_epoch != item.source_account_epoch):
                    raise JournalError("source journal replay provenance is invalid")
        elif item.source_account_id is not None:
            account = self.db.get(orm.SourceAccount, item.source_account_id)
            if (account is None or account.user_id != item.user_id or account.source != item.source
                or account.status != "active" or account.fence_epoch != item.source_account_epoch):
                raise JournalError("source journal account ownership or epoch is invalid")
        elif item.source_mode == "real":
            raise JournalError("real source journal requires an account epoch")

        encoded, payload_format = _payload_bytes(item.payload)
        digest = hashlib.sha256(encoded).hexdigest()
        values = {
            "user_id": item.user_id, "source": item.source, "source_mode": item.source_mode,
            "stream": item.stream, "record_id": item.record_id,
            "time_basis": item.time_basis,
            "observed_start": _iso(item.observed_start), "observed_end": _iso(item.observed_end),
            "payload_hash": digest, "payload_format": payload_format, "payload_bytes": len(encoded),
            "raw_schema_version": item.raw_schema_version,
            "source_account_id": item.source_account_id, "source_account_epoch": item.source_account_epoch,
            "source_scope": item.source_scope, "device_id": item.device_id,
            "capability": item.capability, "incomplete": item.incomplete,
            "origin_journal_id": item.origin_journal_id, "origin_source_mode": origin_mode,
        }
        journal_id = payload_hash(values)
        if origin_mode == "real":
            try:
                credentials._cipher()
            except (RuntimeError, ValueError, UnicodeError) as exc:
                raise JournalError("source journal encryption is unavailable") from exc
        existing = self.db.get(SourceJournalRecord, journal_id)
        if existing is not None:
            return existing
        values.update({
            "id": journal_id,
            "observed_start": _naive(item.observed_start),
            "observed_end": _naive(item.observed_end),
            "fetched_at": _naive(item.fetched_at),
            "parser_version": item.parser_version,
            "encrypted_payload": _encrypt(encoded, origin_mode),
        })
        dialect = self.db.get_bind().dialect.name
        if dialect in {"sqlite", "postgresql"}:
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            else:
                from sqlalchemy.dialects.postgresql import insert
            # A concurrent duplicate must not roll back the caller's facts or
            # fencing state. The losing append only reads the immutable winner.
            self.db.execute(insert(SourceJournalRecord).values(**values).on_conflict_do_nothing(index_elements=["id"]))
        else:
            self.db.add(SourceJournalRecord(**values))
        self.db.flush()
        return self.get(journal_id, user_id=item.user_id)

    def get(self, journal_id: str, *, user_id: str) -> SourceJournalRecord:
        row = self.db.execute(select(SourceJournalRecord).where(
            SourceJournalRecord.id == journal_id, SourceJournalRecord.user_id == user_id,
        )).scalar_one_or_none()
        if row is None:
            raise JournalError("source journal record is outside the requested user scope")
        return row

    def read_payload(self, journal_id: str, *, user_id: str) -> Any:
        row = self.get(journal_id, user_id=user_id)
        encoded = _decrypt(row.encrypted_payload, row.origin_source_mode)
        if hashlib.sha256(encoded).hexdigest() != row.payload_hash or len(encoded) != row.payload_bytes:
            raise JournalError("source journal payload integrity check failed")
        if row.payload_format == "bytes":
            return encoded
        try:
            return json.loads(encoded.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise JournalError("source journal payload cannot be decoded") from exc

    @staticmethod
    def metadata(row: SourceJournalRecord) -> dict[str, Any]:
        metadata = {column.name: getattr(row, column.name) for column in _METADATA_COLUMNS}
        metadata["journal_id"] = metadata.pop("id")
        for key in ("observed_start", "observed_end", "fetched_at"):
            metadata[key] = _iso(metadata[key])
        return metadata

    def input_manifest(
        self,
        user_id: str,
        *,
        start_date: date,
        end_date: date,
        as_of: datetime,
        journal_ids: Sequence[str] | None = None,
        source: str | None = None,
        source_mode: str | None = None,
        timezone_name: str = "UTC",
        limit: int = MAX_MANIFEST_RECORDS,
    ) -> dict[str, Any]:
        """Read at most 730 days/5000 records without loading/decrypting payloads.

        Dates are inclusive civil dates in ``timezone_name``; intervals are
        half-open and fetched_at is bounded by as_of. Explicit IDs must satisfy
        every predicate, including user/date/as_of, or the selection is rejected.
        """
        start, end, cutoff, ids = _selection(start_date, end_date, as_of, timezone_name, limit, journal_ids)
        if source_mode is not None and source_mode not in _MODES:
            raise JournalError("manifest source mode is invalid")
        statement = select(SourceJournalRecord).options(load_only(*[
            getattr(SourceJournalRecord, column.name) for column in _METADATA_COLUMNS
        ])).where(
            SourceJournalRecord.user_id == user_id,
            SourceJournalRecord.observed_start < end,
            or_(
                SourceJournalRecord.observed_end > start,
                (or_(SourceJournalRecord.observed_end.is_(None),
                     SourceJournalRecord.observed_end == SourceJournalRecord.observed_start)
                 & (SourceJournalRecord.observed_start >= start)),
            ),
            SourceJournalRecord.fetched_at <= cutoff,
        )
        if source is not None:
            statement = statement.where(SourceJournalRecord.source == source)
        if source_mode is not None:
            statement = statement.where(SourceJournalRecord.source_mode == source_mode)
        if ids is not None:
            statement = statement.where(SourceJournalRecord.id.in_(ids))
        rows = list(self.db.scalars(statement.order_by(
            SourceJournalRecord.fetched_at, SourceJournalRecord.id,
        ).limit(limit + 1)))
        if len(rows) > limit:
            raise JournalError("source input manifest exceeds its bounded limit")
        if ids is not None and {row.id for row in rows} != set(ids):
            raise JournalError("one or more journal IDs are outside the requested scope")
        manifest = {
            "schema_version": MANIFEST_VERSION,
            "selection": {
                "user_id": user_id, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                "as_of": _iso(as_of), "timezone": timezone_name,
                "source": source, "source_mode": source_mode,
                "journal_ids": list(ids) if ids is not None else None,
            },
            "records": [self.metadata(row) for row in rows],
        }
        manifest["manifest_hash"] = manifest_digest(manifest)
        return manifest

    def analysis_manifest(
        self,
        user_id: str,
        target_date: date,
        as_of: datetime,
        *,
        timezone_name: str,
        lookback_days: int = 179,
        source: str = "zepp",
        source_mode: str | None = None,
    ) -> dict[str, Any]:
        """Return the metadata for the projection actually feeding an analysis.

        A plain journal window is not sufficient for replay users: immutable
        rows from a later replay, or late rows excluded by an earlier replay,
        remain in the journal. A replay target therefore resolves only the
        dataset's current completed run and maps its origin IDs to the target's
        copied rows. Live/mock datasets use the latest fetched response for
        each stable source record identity. No payload or ciphertext is read.
        """
        if type(target_date) is not date:
            raise JournalError("analysis manifest requires one civil target date")
        if type(lookback_days) is not int or not 0 <= lookback_days <= MAX_MANIFEST_DAYS - 1:
            raise JournalError("analysis manifest lookback is outside its bounded range")
        start_date = target_date - timedelta(days=lookback_days)
        end_date = target_date
        start, end, cutoff, _ = _selection(
            start_date, end_date, as_of, timezone_name, MAX_MANIFEST_RECORDS, None,
        )
        owner = self.db.get(orm.User, user_id)
        if source_mode is None:
            source_mode = owner.source_mode if owner is not None else None
        if source_mode not in _MODES:
            raise JournalError("analysis manifest source mode is not explicitly bound")
        if owner is None or owner.source_mode != source_mode:
            raise JournalError("analysis manifest source mode is not bound to its owner")
        if source_mode == "replay":
            return self._replay_analysis_manifest(
                user_id, target_date, start_date, end_date, as_of,
                timezone_name=timezone_name, source=source,
                start=start, end=end, cutoff=cutoff,
            )

        statement = select(SourceJournalRecord).options(load_only(*[
            getattr(SourceJournalRecord, column.name) for column in _METADATA_COLUMNS
        ])).where(
            SourceJournalRecord.user_id == user_id,
            SourceJournalRecord.source == source,
            SourceJournalRecord.source_mode == source_mode,
            SourceJournalRecord.observed_start < end,
            or_(
                SourceJournalRecord.observed_end > start,
                (or_(SourceJournalRecord.observed_end.is_(None),
                     SourceJournalRecord.observed_end == SourceJournalRecord.observed_start)
                 & (SourceJournalRecord.observed_start >= start)),
            ),
            SourceJournalRecord.fetched_at <= cutoff,
        ).order_by(SourceJournalRecord.fetched_at, SourceJournalRecord.id).limit(
            MAX_MANIFEST_RECORDS + 1,
        )
        rows = list(self.db.scalars(statement))
        if len(rows) > MAX_MANIFEST_RECORDS:
            raise JournalError("analysis input manifest exceeds its bounded limit")
        effective: dict[tuple[Any, ...], SourceJournalRecord] = {}
        for row in rows:
            identity = (
                row.source, row.stream, row.record_id, row.observed_start,
                row.observed_end, row.source_account_id, row.source_account_epoch,
            )
            previous = effective.get(identity)
            if previous is None or (row.fetched_at, row.id) > (previous.fetched_at, previous.id):
                effective[identity] = row
        selected = sorted(effective.values(), key=lambda row: (row.fetched_at, row.id))
        manifest = self._manifest_for_rows(
            user_id, target_date, start_date, end_date, as_of, timezone_name,
            source, source_mode, selected,
            projection={
                "kind": "effective_journal",
                "selection_rule": "latest_fetched_at_per_source_stream_record_window_account_epoch",
            },
        )
        return manifest

    def _replay_analysis_manifest(
        self,
        user_id: str,
        target_date: date,
        start_date: date,
        end_date: date,
        as_of: datetime,
        *,
        timezone_name: str,
        source: str,
        start: datetime,
        end: datetime,
        cutoff: datetime,
    ) -> dict[str, Any]:
        dataset = self.db.get(ReplayDataset, user_id)
        run = self.db.get(SourceReplayRun, dataset.current_run_id) if dataset and dataset.current_run_id else None
        if run is None:
            return self._manifest_for_rows(
                user_id, target_date, start_date, end_date, as_of, timezone_name,
                source, "replay", [],
                projection={"kind": "replay_run", "state": "missing", "run_id": None},
            )
        selection = run.input_manifest.get("selection") if isinstance(run.input_manifest, dict) else None
        run_as_of = _manifest_time(selection.get("as_of")) if isinstance(selection, dict) else None
        if run_as_of is None:
            raise JournalError("current replay run has invalid input manifest metadata")
        if _aware(as_of) < run_as_of:
            raise JournalError("analysis as_of precedes the current replay projection")
        source_records = run.input_manifest.get("records") if isinstance(run.input_manifest, dict) else None
        if not isinstance(source_records, list) or len(source_records) > MAX_MANIFEST_RECORDS:
            raise JournalError("current replay input manifest is invalid")
        origin_ids = {
            item.get("journal_id") for item in source_records
            if isinstance(item, dict) and isinstance(item.get("journal_id"), str)
        }
        if dataset is None or dataset.origin_kind == "fixture":
            target_ids = origin_ids
            statement = select(SourceJournalRecord).options(load_only(*[
                getattr(SourceJournalRecord, column.name) for column in _METADATA_COLUMNS
            ])).where(
                SourceJournalRecord.user_id == user_id,
                SourceJournalRecord.source == source,
                SourceJournalRecord.source_mode == "replay",
                SourceJournalRecord.id.in_(target_ids) if target_ids else False,
            )
        else:
            statement = select(SourceJournalRecord).options(load_only(*[
                getattr(SourceJournalRecord, column.name) for column in _METADATA_COLUMNS
            ])).where(
                SourceJournalRecord.user_id == user_id,
                SourceJournalRecord.source == source,
                SourceJournalRecord.source_mode == "replay",
                SourceJournalRecord.origin_journal_id.in_(origin_ids) if origin_ids else False,
            )
        rows = [row for row in self.db.scalars(statement) if _row_overlaps(row, start, end)]
        rows.sort(key=lambda row: (row.fetched_at, row.id))
        actual_as_of = _manifest_time(selection.get("as_of"))
        manifest = self._manifest_for_rows(
            user_id, target_date, start_date, end_date, actual_as_of, timezone_name,
            source, "replay", rows,
            projection={
                "kind": "replay_run", "state": "current", "run_id": run.id,
                "parser_version": run.parser_version,
                "source_manifest_hash": run.manifest_hash,
            },
        )
        return manifest

    def _manifest_for_rows(
        self,
        user_id: str,
        target_date: date,
        start_date: date,
        end_date: date,
        as_of: datetime,
        timezone_name: str,
        source: str,
        source_mode: str,
        rows: Sequence[SourceJournalRecord],
        *,
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = {
            "schema_version": MANIFEST_VERSION,
            "selection": {
                "user_id": user_id,
                "target_date": target_date.isoformat(),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "as_of": _iso(as_of),
                "timezone": timezone_name,
                "source": source,
                "source_mode": source_mode,
                "journal_ids": [row.id for row in rows],
            },
            "records": [self.metadata(row) for row in rows],
            "projection": projection,
        }
        manifest["manifest_hash"] = manifest_digest(manifest)
        return manifest


def payload_hash(payload: Any) -> str:
    """SHA-256 over raw binary bytes or strict canonical JSON, preserving nulls."""
    encoded, _ = _payload_bytes(payload)
    return hashlib.sha256(encoded).hexdigest()


def _payload_bytes(value: Any) -> tuple[bytes, str]:
    if isinstance(value, bytes):
        encoded, encoding = value, "bytes"
    else:
        stack = [(value, 0)]
        nodes = 0
        while stack:
            current, depth = stack.pop()
            nodes += 1
            if depth > 100 or nodes > 1_000_000:
                raise JournalError("source JSON structure exceeds its bounded limit")
            if isinstance(current, dict):
                if any(not isinstance(key, str) for key in current):
                    raise JournalError("source JSON object keys must be strings")
                stack.extend((child, depth + 1) for child in current.values())
            elif isinstance(current, list):
                stack.extend((child, depth + 1) for child in current)
            elif current is not None and not isinstance(current, (str, int, float, bool)):
                raise JournalError("source payload is not strict JSON")
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError) as exc:
            raise JournalError("source payload is not strict JSON") from exc
        encoding = "json"
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise JournalError("source payload exceeds its bounded size limit")
    return encoded, encoding


def _validate_input(item: SourceRecordInput) -> None:
    for value, maximum in (
        (item.user_id, 64), (item.source, 32), (item.stream, 96), (item.record_id, 256),
        (item.raw_schema_version, 48), (item.parser_version, 96), (item.time_basis, 32),
        (item.capability, 24),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise JournalError("source journal identity or version is invalid")
    if item.source_mode not in _MODES:
        raise JournalError("source journal mode must be explicitly declared")
    origin_mode = item.origin_source_mode or item.source_mode
    if origin_mode not in {"real", "mock"}:
        raise JournalError("source journal origin mode must be explicitly declared")
    if item.source_mode != "replay" and origin_mode != item.source_mode:
        raise JournalError("source journal mode cannot differ from its origin")
    for value in (item.observed_start, item.fetched_at):
        _aware(value)
    if item.observed_end is not None and _aware(item.observed_end) < _aware(item.observed_start):
        raise JournalError("source observation end precedes its start")
    if not isinstance(item.incomplete, bool):
        raise JournalError("source incomplete qualification must be boolean")
    if (item.source_account_id is None) != (item.source_account_epoch is None):
        raise JournalError("source account ID and epoch must be supplied together")
    if item.source_account_epoch is not None and (type(item.source_account_epoch) is not int or item.source_account_epoch < 0):
        raise JournalError("source account epoch must be a nonnegative integer")


def _selection(start_date, end_date, as_of, timezone_name, limit, journal_ids):
    if type(start_date) is not date or type(end_date) is not date or start_date > end_date:
        raise JournalError("manifest requires an ordered civil-date window")
    if (end_date - start_date).days + 1 > MAX_MANIFEST_DAYS:
        raise JournalError("manifest date range exceeds its bounded limit")
    if type(limit) is not int or not 1 <= limit <= MAX_MANIFEST_RECORDS:
        raise JournalError("manifest limit is outside its bounded range")
    cutoff = _aware(as_of).replace(tzinfo=None)
    try:
        zone = ZoneInfo(timezone_name)
        start = _naive(datetime.combine(start_date, time.min, tzinfo=zone))
        end = _naive(datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone))
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise JournalError("manifest timezone or date is invalid") from exc
    ids = None
    if journal_ids is not None:
        if isinstance(journal_ids, (str, bytes)) or len(journal_ids) > MAX_MANIFEST_RECORDS:
            raise JournalError("journal IDs exceed their bounded limit")
        if any(not isinstance(value, str) or len(value) != 64 for value in journal_ids):
            raise JournalError("journal IDs contain an invalid ID")
        ids = tuple(sorted(set(journal_ids)))
    return start, end, cutoff, ids


def _encrypt(encoded: bytes, origin_mode: str) -> str:
    try:
        if origin_mode == "real" or settings.token_encryption_key:
            return "fernet:" + credentials._cipher().encrypt(encoded).decode("ascii")
        if origin_mode == "mock" and settings.env in {"dev", "test"}:
            return "mock-fernet:" + credentials._mock_cipher().encrypt(encoded).decode("ascii")
        raise JournalError("source journal encryption requires a private key")
    except (RuntimeError, ValueError, UnicodeError) as exc:
        raise JournalError("source journal encryption is unavailable") from exc


def _decrypt(value: str, origin_mode: str) -> bytes:
    try:
        if value.startswith("fernet:"):
            return credentials._cipher().decrypt(value[len("fernet:"):].encode("ascii"))
        if value.startswith("mock-fernet:") and origin_mode == "mock" and settings.env in {"dev", "test"}:
            return credentials._mock_cipher().decrypt(value[len("mock-fernet:"):].encode("ascii"))
    except (RuntimeError, ValueError, UnicodeError, InvalidToken) as exc:
        raise JournalError("source journal encryption cannot be decrypted") from exc
    raise JournalError("source journal encryption format is invalid")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise JournalError("source journal timestamps require an explicit timezone")
    return value.astimezone(timezone.utc)


def _naive(value: datetime | None) -> datetime | None:
    return _aware(value).replace(tzinfo=None) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # Only ORM metadata is naive, with an explicit storage contract of UTC.
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else _aware(value)
    return aware.isoformat().replace("+00:00", "Z")


def _manifest_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _aware(parsed)
    except (TypeError, ValueError, OverflowError):
        return None


def _row_overlaps(row: SourceJournalRecord, start: datetime, end: datetime) -> bool:
    if row.observed_start >= end:
        return False
    if row.observed_end is not None and row.observed_end > start:
        return True
    return (
        (row.observed_end is None or row.observed_end == row.observed_start)
        and row.observed_start >= start
    )


def stage_sync_record(repo, user, record, fetcher, *, fetched_at=None) -> SourceJournalRecord | None:
    """Stage a raw response at the existing SyncManager persistence entrance.

    Durable workers provide their frozen attempt via session.info. Standalone
    callers must carry an explicit connector mode; this boundary never infers a
    source from process configuration.
    """
    db = getattr(repo, "db", None)
    if not isinstance(db, Session):
        # In-memory repository ports used by parser unit tests do not persist.
        return None
    context = db.info.get("source_journal_context", {})
    if context.get("replay_prejournaled"):
        return None
    connector = getattr(fetcher, "connector", None)
    db.flush()
    mode = context.get("source_mode") or getattr(connector, "source_mode", None)
    owner_mode = repo.source_mode(user.id)
    if mode is None and owner_mode in {"real", "mock"}:
        mode = owner_mode
    if mode not in {"real", "mock"}:
        raise JournalError("sync journal requires an explicitly bound source mode")
    if owner_mode == "unknown":
        # A synthetic/live connector can establish an unbound pending user, but
        # only from its explicit mode; never consult ZEPP_MOCK or other config.
        repo.bind_source_mode(user.id, mode)
    elif owner_mode != mode:
        raise JournalError("sync journal source mode is not bound to its owner")
    account_id = context.get("source_account_id")
    account_epoch = context.get("source_account_epoch")
    if "source_account_id" not in context:
        account = repo.source_account(user.id, "zepp", active_only=True)
        if account is not None:
            account_id, account_epoch = account.id, account.fence_epoch
    raw = record.raw
    return SourceJournalRepository(db).append(SourceRecordInput(
        user_id=user.id, source="zepp", source_mode=mode,
        stream=raw.stream, record_id=raw.source_key,
        observed_start=raw.start_utc, observed_end=raw.end_utc,
        fetched_at=context.get("fetched_at") or fetched_at or datetime.now(timezone.utc),
        payload=raw.payload, source_account_id=account_id, source_account_epoch=account_epoch,
        capability=raw.capability, incomplete=record.incomplete,
    ))


def sync_journal_context(repo, attempt_id: str, *, fetched_at: datetime) -> dict[str, Any]:
    """Load the durable attempt's provenance within its existing transaction."""
    attempt = repo.sync_attempt(attempt_id)
    if attempt is None:
        raise JournalError("source journal sync owner is missing")
    return {
        "source_mode": (attempt.options or {}).get("source_mode"),
        "source_account_id": attempt.source_account_id,
        "source_account_epoch": attempt.source_account_epoch,
        "fetched_at": fetched_at,
    }


def stage_dense_archive(repo, attempt_id: str, chunk: dict[str, Any], archive: bytes | None,
                        *, fetched_at: datetime) -> SourceJournalRecord | None:
    """Stage the downloaded SEC_HR bytes inside the fenced decode transaction."""
    if archive is None:
        return None
    context = sync_journal_context(repo, attempt_id, fetched_at=fetched_at)
    attempt = repo.sync_attempt(attempt_id)
    params = (chunk.get("stages") or {}).get("params") or {}
    file_type, file_id = params.get("file_type"), params.get("file_id")
    if not isinstance(file_type, str) or not isinstance(file_id, str) or not file_type or not file_id:
        raise JournalError("dense journal requires its source file identity")
    return SourceJournalRepository(repo.db).append(SourceRecordInput(
        user_id=attempt.user_id, source=attempt.source, source_mode=context["source_mode"],
        stream="dense_archive", record_id=f"dense_archive:{file_type}:{file_id}",
        observed_start=chunk.get("window_start") or attempt.window_start.replace(tzinfo=timezone.utc),
        observed_end=chunk.get("window_end") or attempt.window_end.replace(tzinfo=timezone.utc),
        fetched_at=fetched_at, payload=archive, raw_schema_version=DENSE_RAW_SCHEMA_VERSION,
        source_account_id=context["source_account_id"], source_account_epoch=context["source_account_epoch"],
    ))


__all__ = [
    "CURRENT_PARSER_VERSION", "DENSE_RAW_SCHEMA_VERSION", "JournalError", "MANIFEST_VERSION",
    "MAX_MANIFEST_DAYS", "MAX_MANIFEST_RECORDS", "RAW_SCHEMA_VERSION", "ReplayDataset",
    "SourceJournalRecord", "SourceJournalRepository", "SourceRecordInput", "SourceReplayRun",
    "manifest_digest", "payload_hash", "stage_sync_record", "sync_journal_context",
]
