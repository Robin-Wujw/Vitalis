"""Immutable input-change audit, separate from mutable analysis execution jobs.

Import this module before ``Base.metadata.create_all`` or schema validation.
``ACTIVE_SOURCE_RECORD_ID`` is a transaction-local journal ID supplied through
``Session.info``; only its opaque reference, never a source payload, is retained.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint, DDL, Date, DateTime, ForeignKey, Index, Integer, String,
    UniqueConstraint, event, select,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import JSON

from .database import Base
from .models import AnalysisJob, User


ACTIVE_SOURCE_RECORD_ID = "active_source_record_id"
ANALYSIS_LOOKBACK_DAYS = 179
_REFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.:-]+\Z")


class InputEventAuditError(ValueError):
    """An input audit reference conflicts with its immutable owner or identity."""


class InputEvent(Base):
    """One immutable input change; the user revision is an audit cursor only."""

    __tablename__ = "input_events"
    __table_args__ = (
        UniqueConstraint("user_id", "input_revision", "scope_hash", name="uq_input_event_revision_scope"),
        CheckConstraint("input_revision >= 0", name="ck_input_event_revision"),
        Index("ix_input_events_user_occurred", "user_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    affected_dates: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON))
    affected_streams: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON))
    payload_ref: Mapped[str] = mapped_column(String(256))
    input_revision: Mapped[int] = mapped_column(Integer)
    scope_hash: Mapped[str] = mapped_column(String(64))


class InputEventRequest(Base):
    """Immutable request alias; several requests may describe the same revision.

    Keeping aliases outside the event avoids updating it when an unchanged
    revision is requested with another key.  Raw request keys are never stored.
    """

    __tablename__ = "input_event_requests"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(ForeignKey("input_events.id", ondelete="CASCADE"), index=True)


class AnalysisInvalidation(Base):
    """One event's original scope linked to an actual analysis execution target."""

    __tablename__ = "analysis_invalidations"
    __table_args__ = (
        UniqueConstraint("event_id", "job_id", "target_date", name="uq_analysis_invalidation_event_job_target"),
        CheckConstraint("dependency_start <= dependency_end", name="ck_analysis_invalidation_window"),
        Index("ix_analysis_invalidations_user_target", "user_id", "target_date"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("input_events.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("analysis_jobs.id", ondelete="CASCADE"), index=True)
    target_date: Mapped[date] = mapped_column(Date)
    direct_dates: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON))
    affected_streams: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON))
    dependency_start: Mapped[date] = mapped_column(Date)
    dependency_end: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(128))
    # Freeze the requested period even if the job is later promoted for delivery.
    delivery_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


def _reject_update(_mapper, _connection, _target) -> None:
    raise InputEventAuditError("input audit records are immutable")


# Mapper guards cover ORM attribute/list edits; database triggers also cover bulk
# and direct SQL UPDATEs.  Owner deletion keeps the existing cascade semantics.
for _model in (InputEvent, InputEventRequest, AnalysisInvalidation):
    event.listen(_model, "before_update", _reject_update)
    _table = _model.__table__
    event.listen(_table, "after_create", DDL(
        f"CREATE TRIGGER {_table.name}_no_update BEFORE UPDATE ON {_table.name} "
        "BEGIN SELECT RAISE(ABORT, 'input audit records are immutable'); END"
    ).execute_if(dialect="sqlite"))
    _function = f"{_table.name}_reject_update"
    event.listen(_table, "after_create", DDL(
        f"CREATE FUNCTION {_function}() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'input audit records are immutable'; END; "
        "$$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"))
    event.listen(_table, "after_create", DDL(
        f"CREATE TRIGGER {_table.name}_no_update BEFORE UPDATE ON {_table.name} "
        f"FOR EACH ROW EXECUTE FUNCTION {_function}()"
    ).execute_if(dialect="postgresql"))
    event.listen(_table, "after_drop", DDL(
        f"DROP FUNCTION IF EXISTS {_function}()"
    ).execute_if(dialect="postgresql"))


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def _now(value: datetime | None = None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    return timestamp.astimezone(timezone.utc).replace(tzinfo=None) if timestamp.tzinfo else timestamp


def _dates(values: Iterable[date | str]) -> list[str]:
    result = set()
    for value in values:
        if isinstance(value, datetime):
            raise InputEventAuditError("input audit dates must be calendar dates")
        day = value if isinstance(value, date) else date.fromisoformat(value)
        result.add(day.isoformat())
    return sorted(result)


class InputEventAuditRepository:
    """Append and link audit records inside the input writer's existing session."""

    def __init__(self, db: Session):
        self.db = db

    def _insert(self, model, values: dict, conflict_columns: list[str]) -> bool:
        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise NotImplementedError("input audit requires SQLite or PostgreSQL")
        return bool(self.db.execute(insert(model).values(**values).on_conflict_do_nothing(
            index_elements=conflict_columns,
        )).rowcount)

    def get(self, user_id: str, event_id: str) -> InputEvent | None:
        return self.db.scalar(select(InputEvent).where(
            InputEvent.user_id == user_id, InputEvent.id == event_id,
        ))

    def append(
        self, user_id: str, *, event_type: str, source: str,
        affected_dates: Iterable[date | str], affected_streams: Iterable[str],
        input_revision: int, payload_ref: str | None = None,
        occurred_at: datetime | None = None, request_key: str | None = None,
        request_context: dict | None = None,
    ) -> InputEvent | None:
        if self.db.get(User, user_id) is None:
            return None
        dates = _dates(affected_dates)
        streams = sorted({str(value) for value in affected_streams if str(value)})
        scope_hash = _digest({
            "event_type": event_type, "source": source,
            "affected_dates": dates, "affected_streams": streams,
        })
        request_hash = _digest({"scope": scope_hash, "context": request_context})
        key_hash = _digest(request_key) if request_key else None
        if key_hash:
            previous = self.db.get(InputEventRequest, (user_id, key_hash))
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise InputEventAuditError("input event request key belongs to another request")
                return self.get(user_id, previous.event_id)
        reference = payload_ref
        if reference is None:
            source_record_id = self.db.info.get(ACTIVE_SOURCE_RECORD_ID)
            reference = (
                f"source_record:{source_record_id}" if source_record_id is not None
                else f"input_revision:{input_revision}"
            )
        if not isinstance(reference, str) or len(reference) > 256 or not _REFERENCE.fullmatch(reference):
            raise InputEventAuditError("payload_ref must be an opaque audit reference")
        values = dict(
            id=uuid4().hex, user_id=user_id, event_type=event_type[:48], source=source[:32],
            occurred_at=_now(occurred_at), affected_dates=dates, affected_streams=streams,
            payload_ref=reference, input_revision=input_revision, scope_hash=scope_hash,
        )
        self._insert(InputEvent, values, ["user_id", "input_revision", "scope_hash"])
        row = self.db.scalar(select(InputEvent).where(
            InputEvent.user_id == user_id, InputEvent.input_revision == input_revision,
            InputEvent.scope_hash == scope_hash,
        ))
        if key_hash:
            claimed = self._insert(InputEventRequest, dict(
                user_id=user_id, key_hash=key_hash, request_hash=request_hash, event_id=row.id,
            ), ["user_id", "key_hash"])
            if not claimed:
                previous = self.db.get(InputEventRequest, (user_id, key_hash), populate_existing=True)
                if previous.request_hash != request_hash or previous.event_id != row.id:
                    raise InputEventAuditError("input event request was claimed by another revision")
        return row

    def linked_job(
        self, user_id: str, event_id: str, target_date: date,
        delivery_period: str | None,
    ) -> AnalysisJob | None:
        return self.db.scalar(select(AnalysisJob).join(
            AnalysisInvalidation, AnalysisInvalidation.job_id == AnalysisJob.id,
        ).where(
            AnalysisInvalidation.user_id == user_id,
            AnalysisInvalidation.event_id == event_id,
            AnalysisInvalidation.target_date == target_date,
            AnalysisInvalidation.delivery_period == delivery_period,
            AnalysisJob.user_id == user_id,
        ).order_by(AnalysisInvalidation.created_at, AnalysisInvalidation.id).limit(1))

    def link(
        self, user_id: str, event_id: str, job: AnalysisJob, *, reason: str,
        delivery_period: str | None = None,
    ) -> AnalysisInvalidation:
        input_event = self.get(user_id, event_id)
        owned_job = self.db.scalar(select(AnalysisJob.id).where(
            AnalysisJob.id == job.id, AnalysisJob.user_id == user_id,
            AnalysisJob.target_date == job.target_date,
        ))
        if input_event is None or job.user_id != user_id or owned_job is None:
            raise InputEventAuditError("input event and analysis job must belong to the same user")
        self._insert(AnalysisInvalidation, dict(
            id=uuid4().hex, user_id=user_id, event_id=event_id, job_id=job.id,
            target_date=job.target_date, direct_dates=list(input_event.affected_dates),
            affected_streams=list(input_event.affected_streams),
            dependency_start=job.target_date - timedelta(days=ANALYSIS_LOOKBACK_DAYS),
            dependency_end=job.target_date, reason=reason[:128],
            delivery_period=delivery_period, created_at=_now(),
        ), ["event_id", "job_id", "target_date"])
        return self.db.scalar(select(AnalysisInvalidation).where(
            AnalysisInvalidation.user_id == user_id, AnalysisInvalidation.event_id == event_id,
            AnalysisInvalidation.job_id == job.id,
            AnalysisInvalidation.target_date == job.target_date,
        ))
