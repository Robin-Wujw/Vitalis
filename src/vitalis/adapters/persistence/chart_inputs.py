"""Transactional persistence for chart annotations, review overlays and Hevy CSV.

Import/register the independent models before database init/check. Writes use
ordinary feedback invalidation with a suppressed-notification execution context;
other feedback endpoints retain their normal delivery policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import hashlib
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, MetaData, String, Text, UniqueConstraint, and_, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from vitalis.application.chart_inputs import (
    ActivityReviewInput, ActivityRuleInput, AnnotationInput, AnnotationPatch, ChartInputConflict,
    ChartInputLimitError, ChartInputNotFound, ChartInputValidationError,
    DeleteAnnotationInput, HevyLinkInput, HevySetMappingInput, ImportedStrengthSession,
    activity_suggestions,
)
from vitalis.time import local_day, local_day_utc_bounds

from .database import Base
from .input_events import InputEvent
from . import models as orm
from .repositories import HealthRepository


class ChartAnnotation(Base):
    __tablename__ = "chart_annotations"
    __table_args__ = (
        CheckConstraint("revision >= 1 AND ended_at >= started_at", name="ck_chart_annotation_values"),
        Index("ix_chart_annotation_user_interval", "user_id", "started_at", "ended_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime] = mapped_column(DateTime)
    text: Mapped[str] = mapped_column(Text)
    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ChartWorkoutReview(Base):
    __tablename__ = "chart_workout_reviews"
    __table_args__ = (
        UniqueConstraint("user_id", "workout_source", "workout_id", name="uq_chart_workout_review"),
        CheckConstraint("revision >= 1 AND decision IN ('pending','classified','ignored')", name="ck_chart_workout_review"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workout_source: Mapped[str] = mapped_column(String(32))
    workout_id: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime] = mapped_column(DateTime)
    decision: Mapped[str] = mapped_column(String(16))
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ChartActivityRule(Base):
    __tablename__ = "chart_activity_rules"
    __table_args__ = (CheckConstraint("revision >= 1", name="ck_chart_activity_rule_revision"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean)
    classification: Mapped[str] = mapped_column(String(32))
    conditions: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ChartExternalWorkout(Base):
    __tablename__ = "chart_external_workouts"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "external_identity", name="uq_chart_external_identity"),
        UniqueConstraint("user_id", "linked_workout_source", "linked_workout_id", name="uq_chart_external_link"),
        CheckConstraint("revision >= 1 AND ended_at >= started_at", name="ck_chart_external_values"),
        Index("ix_chart_external_user_interval", "user_id", "started_at", "ended_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    source_scope: Mapped[str] = mapped_column(String(32))
    source_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_identity: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime] = mapped_column(DateTime)
    timezone_name: Mapped[str] = mapped_column(String(64))
    linked_workout_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    linked_workout_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ChartExternalSet(Base):
    __tablename__ = "chart_external_sets"
    __table_args__ = (UniqueConstraint("external_workout_id", "order", name="uq_chart_external_set_order"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    external_workout_id: Mapped[str] = mapped_column(ForeignKey("chart_external_workouts.id", ondelete="CASCADE"), index=True)
    order: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)


class ChartInputRevision(Base):
    """Immutable input versions; the latest visible revision controls inclusion."""
    __tablename__ = "chart_input_revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "resource_id", "revision", name="uq_chart_input_revision"),
        Index("ix_chart_revision_user_cutoff", "user_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime] = mapped_column(DateTime)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ChartInputWriteRequest(Base):
    __tablename__ = "chart_input_write_requests"
    __table_args__ = (UniqueConstraint("user_id", "key_digest", name="uq_chart_input_request"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key_digest: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


CHART_INPUT_MODELS = (ChartAnnotation, ChartWorkoutReview, ChartActivityRule, ChartExternalWorkout, ChartExternalSet, ChartInputRevision, ChartInputWriteRequest)


def register_chart_input_models(target: MetaData | type[Base]) -> None:
    metadata = getattr(target, "metadata", target)
    if not isinstance(metadata, MetaData):
        raise TypeError("target must be declarative Base or MetaData")
    for model in CHART_INPUT_MODELS:
        if model.__tablename__ not in metadata.tables:
            model.__table__.to_metadata(metadata)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)


def _json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if isinstance(value, datetime):
        return _utc(value).replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _interval_dates(start: datetime, end: datetime, timezone_name: str) -> set[date]:
    first, last = local_day(start, timezone_name), local_day(end, timezone_name)
    if last < first or (last - first).days > 366:
        raise ChartInputValidationError("input interval exceeds invalidation budget")
    return {first + timedelta(days=index) for index in range((last - first).days + 1)}


def _eligible(data: dict) -> bool:
    return data.get("auto_recognized") is True or data.get("sport_mode") in (None, "", "unknown") or data.get("recognition_confidence") == "NONE"


def _workout_interval(row: orm.Workout) -> tuple[datetime, datetime]:
    start = _datetime(row.started_at) or _datetime((row.data or {}).get("started_at"))
    if start is None:
        raise ChartInputValidationError("workout has no observed timestamp")
    end = _datetime((row.data or {}).get("ended_at")) or start
    if end < start:
        raise ChartInputValidationError("workout interval is invalid")
    return start, end


class SqlChartInputStore:
    def __init__(self, sessions: sessionmaker[Session], *, timezone_name: str = "UTC") -> None:
        self._sessions = sessions
        self.timezone_name = timezone_name

    @staticmethod
    def _owner(db: Session, user_id: str) -> None:
        if db.get(orm.User, user_id) is None:
            raise ChartInputNotFound("user not found")

    @staticmethod
    def _owned(db: Session, model, user_id: str, resource_id: str):
        row = db.scalar(select(model).where(model.id == resource_id, model.user_id == user_id))
        if row is None:
            raise ChartInputNotFound("user-owned input not found")
        return row

    @staticmethod
    def _workout(db: Session, user_id: str, source: str, workout_id: str) -> orm.Workout:
        row = db.scalar(select(orm.Workout).where(orm.Workout.user_id == user_id, orm.Workout.source == source, orm.Workout.workout_id == workout_id))
        if row is None:
            raise ChartInputNotFound("user-owned workout not found")
        return row

    @staticmethod
    def _revision(row, expected: int) -> None:
        if row.revision != expected:
            raise ChartInputConflict("input revision changed; refresh before retry")

    def _write(self, user_id: str, operation: str, key: str, fingerprint: str, now: datetime, save) -> dict:
        with self._sessions() as db:
            # Serialize all this user's chart writes before claiming a key, so
            # concurrent imports/revisions/links cannot overwrite one another.
            changed = db.execute(update(orm.User).where(orm.User.id == user_id).values(analysis_input_revision=orm.User.analysis_input_revision)).rowcount
            if not changed:
                raise ChartInputNotFound("user not found")
            db.info["execution_context"] = {"origin": "chart_inputs", "notification_policy": "suppress"}
            dialect = db.get_bind().dialect.name
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            elif dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                raise NotImplementedError("chart input idempotency requires SQLite or PostgreSQL")
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            claimed = db.execute(insert(ChartInputWriteRequest).values(
                id=uuid4().hex, user_id=user_id, key_digest=digest, request_hash=fingerprint,
                operation=operation, created_at=_utc(now),
            ).on_conflict_do_nothing(index_elements=["user_id", "key_digest"])).rowcount
            request = db.scalar(select(ChartInputWriteRequest).where(ChartInputWriteRequest.user_id == user_id, ChartInputWriteRequest.key_digest == digest))
            if not claimed:
                if request.request_hash != fingerprint or request.operation != operation:
                    raise ChartInputConflict("Idempotency-Key belongs to another chart input request")
                if request.response is None:
                    raise RuntimeError("chart request has no committed result")
                result = dict(request.response)
            else:
                result = _json(save(db))
                request.response = result
            db.commit()
            return result

    def _input(self, db: Session, user_id: str, *, kind: str, dates: set[date], now: datetime, payload_ref: str, source: str = "user") -> dict:
        repo = HealthRepository(db)
        repo.bump_analysis_input_revision(user_id)
        event_id = uuid4().hex
        streams = {kind, "chart_inputs"}
        if kind in {"workout_reviews", "external_strength_sets"}:
            streams.add("workout")
        if kind == "external_strength_sets":
            streams.add("strength")
        jobs = repo.enqueue_input_change(
            user_id, event_id=event_id, event_type="feedback", source=source,
            affected_dates=dates, affected_streams=streams, target_dates=dates,
            reason=f"explicit {kind}", payload_ref=payload_ref, occurred_at=now,
        )
        if not jobs or any(job is None for job in jobs):
            raise RuntimeError("chart input did not enqueue analysis")
        audit = db.scalar(select(InputEvent).where(
            InputEvent.user_id == user_id, InputEvent.input_revision == repo.analysis_input_revision(user_id),
            InputEvent.payload_ref == payload_ref,
        ))
        if audit is None:
            raise RuntimeError("chart input audit event was not persisted")
        return {
            "id": audit.id, "event_type": "feedback", "input_kind": kind, "source": source,
            "affected_dates": sorted(dates), "affected_streams": sorted(streams),
            "input_revision": repo.analysis_input_revision(user_id),
            "analysis_job_ids": [job.id for job in jobs], "notification_policy": "suppress",
        }

    @staticmethod
    def _save_revision(db: Session, user_id: str, kind: str, row, payload: dict, now: datetime) -> None:
        db.add(ChartInputRevision(
            id=uuid4().hex, user_id=user_id, kind=kind, resource_id=row.id,
            revision=row.revision, started_at=row.started_at, ended_at=row.ended_at,
            payload=_json(payload), created_at=_utc(now),
        ))
        db.flush()

    @staticmethod
    def _annotation(row: ChartAnnotation) -> dict:
        return {
            "id": row.id, "user_id": row.user_id, "started_at": row.started_at, "ended_at": row.ended_at,
            "text": row.text, "tag": row.tag, "revision": row.revision, "deleted": row.deleted,
            "source": "user", "source_scope": "timeline_annotation", "qualification": "user_annotation",
            "time_basis": "calendar_day", "observed_at": row.started_at, "fetched_at": row.created_at,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "limitations": ["User context only; this annotation does not establish a physiological fact or causation."],
        }

    def create_annotation(self, user_id: str, body: AnnotationInput, *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            row = ChartAnnotation(id=uuid4().hex, user_id=user_id, started_at=_utc(body.started_at), ended_at=_utc(body.ended_at), text=body.text, tag=body.tag, revision=1, deleted=False, created_at=_utc(now), updated_at=_utc(now))
            db.add(row)
            db.flush()
            projection = self._annotation(row)
            self._save_revision(db, user_id, "annotation", row, projection, now)
            event = self._input(db, user_id, kind="chart_annotations", dates=_interval_dates(row.started_at, row.ended_at, self.timezone_name), now=now, payload_ref=f"chart_annotation:{row.id}:1")
            return {"annotation": projection, "input_event": event}
        return self._write(user_id, "annotation_create", key, fingerprint, now, save)

    def patch_annotation(self, user_id: str, annotation_id: str, body: AnnotationPatch, *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            row = self._owned(db, ChartAnnotation, user_id, annotation_id)
            self._revision(row, body.expected_revision)
            if row.deleted:
                raise ChartInputNotFound("annotation was deleted")
            dates = _interval_dates(row.started_at, row.ended_at, self.timezone_name)
            values = {"started_at": row.started_at.replace(tzinfo=timezone.utc), "ended_at": row.ended_at.replace(tzinfo=timezone.utc), "text": row.text, "tag": row.tag}
            values.update(body.model_dump(exclude_unset=True, exclude={"confirmed", "expected_revision"}))
            try:
                validated = AnnotationInput(confirmed=True, **values)
            except ValidationError as exc:
                raise ChartInputValidationError("annotation interval or content is invalid") from exc
            row.started_at, row.ended_at = _utc(validated.started_at), _utc(validated.ended_at)
            row.text, row.tag = validated.text, validated.tag
            row.revision += 1
            row.updated_at = _utc(now)
            dates.update(_interval_dates(row.started_at, row.ended_at, self.timezone_name))
            projection = self._annotation(row)
            self._save_revision(db, user_id, "annotation", row, projection, now)
            event = self._input(db, user_id, kind="chart_annotations", dates=dates, now=now, payload_ref=f"chart_annotation:{row.id}:{row.revision}")
            return {"annotation": projection, "input_event": event}
        return self._write(user_id, "annotation_patch", key, fingerprint, now, save)

    def delete_annotation(self, user_id: str, annotation_id: str, body: DeleteAnnotationInput, *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            row = self._owned(db, ChartAnnotation, user_id, annotation_id)
            self._revision(row, body.expected_revision)
            if row.deleted:
                raise ChartInputNotFound("annotation was deleted")
            row.deleted = True
            row.revision += 1
            row.updated_at = _utc(now)
            projection = self._annotation(row)
            self._save_revision(db, user_id, "annotation", row, projection, now)
            event = self._input(db, user_id, kind="chart_annotations", dates=_interval_dates(row.started_at, row.ended_at, self.timezone_name), now=now, payload_ref=f"chart_annotation:{row.id}:{row.revision}")
            return {"annotation": projection, "input_event": event}
        return self._write(user_id, "annotation_delete", key, fingerprint, now, save)

    @staticmethod
    def _review(row: ChartWorkoutReview) -> dict:
        return {
            "id": row.id, "user_id": row.user_id, "workout_source": row.workout_source, "workout_id": row.workout_id,
            "started_at": row.started_at, "ended_at": row.ended_at, "decision": row.decision,
            "classification": row.classification, "notes": row.notes, "original": dict(row.original),
            "revision": row.revision, "source": "user", "source_scope": "activity_review",
            "qualification": "explicit_user_decision", "applied_to_source": False, "time_basis": "activity_day",
            "observed_at": row.started_at, "fetched_at": row.created_at,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "limitations": ["A report/chart display overlay; original vendor records and canonical health facts are retained."],
        }

    def review_activity(self, user_id: str, body: ActivityReviewInput, *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            workout = self._workout(db, user_id, body.workout_source, body.workout_id)
            data = workout.data or {}
            row = db.scalar(select(ChartWorkoutReview).where(ChartWorkoutReview.user_id == user_id, ChartWorkoutReview.workout_source == body.workout_source, ChartWorkoutReview.workout_id == body.workout_id))
            start, end = _workout_interval(workout)
            if end > _utc(now):
                raise ChartInputValidationError("activity review requires a completed workout")
            if row is None:
                if not _eligible(data):
                    raise ChartInputValidationError("workout does not qualify for the review queue")
                if body.expected_revision != 0:
                    raise ChartInputConflict("review revision changed")
                row = ChartWorkoutReview(
                    id=uuid4().hex, user_id=user_id, workout_source=body.workout_source, workout_id=body.workout_id,
                    started_at=start, ended_at=end, revision=1,
                    original={name: data.get(name) for name in ("type", "sport_mode", "training_family", "auto_recognized", "recognition_confidence", "recognition_source", "vendor_type_id", "vendor_type_namespace")},
                    created_at=_utc(now), updated_at=_utc(now),
                )
                db.add(row)
            else:
                self._revision(row, body.expected_revision)
                row.revision += 1
                row.updated_at = _utc(now)
            row.decision = {"classify": "classified", "ignore": "ignored", "restore": "pending"}[body.action]
            row.classification, row.notes = body.classification, body.notes
            projection = self._review(row)
            self._save_revision(db, user_id, "workout_review", row, projection, now)
            event = self._input(db, user_id, kind="workout_reviews", dates=_interval_dates(start, end, self.timezone_name), now=now, payload_ref=f"chart_review:{row.id}:{row.revision}")
            return {"review": projection, "input_event": event}
        return self._write(user_id, "activity_review", key, fingerprint, now, save)

    @staticmethod
    def _rule(row: ChartActivityRule) -> dict:
        return {"id": row.id, "user_id": row.user_id, "name": row.name, "enabled": row.enabled,
                "classification": row.classification, "conditions": dict(row.conditions),
                "revision": row.revision, "mode": "suggest_only", "source": "user_rule",
                "created_at": row.created_at, "updated_at": row.updated_at}

    def activity_rules(self, user_id: str) -> dict:
        with self._sessions() as db:
            self._owner(db, user_id)
            rows = db.scalars(select(ChartActivityRule).where(ChartActivityRule.user_id == user_id).order_by(ChartActivityRule.created_at, ChartActivityRule.id)).all()
            return _json({"items": [self._rule(row) for row in rows], "mode": "suggest_only", "limit": 50})

    def save_activity_rule(self, user_id: str, body: ActivityRuleInput, *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            row = self._owned(db, ChartActivityRule, user_id, body.rule_id) if body.rule_id else None
            if row is None:
                count = db.scalar(select(func.count()).select_from(ChartActivityRule).where(ChartActivityRule.user_id == user_id))
                if count >= 50:
                    raise ChartInputLimitError("at most 50 activity rules per user")
                row = ChartActivityRule(id=uuid4().hex, user_id=user_id, revision=1, created_at=_utc(now), updated_at=_utc(now))
                db.add(row)
            else:
                self._revision(row, body.expected_revision)
                row.revision += 1
                row.updated_at = _utc(now)
            row.name, row.enabled = body.name, body.enabled
            row.classification, row.conditions = body.classification, body.conditions.model_dump(mode="json", exclude_none=True)
            projection = self._rule(row)
            db.add(ChartInputRevision(
                id=uuid4().hex, user_id=user_id, kind="activity_rule", resource_id=row.id,
                revision=row.revision, started_at=_utc(now), ended_at=_utc(now),
                payload=_json(projection), created_at=_utc(now),
            ))
            db.flush()
            # Rules affect suggestions, not canonical workouts. Revisit saved
            # chart windows within the supported history without applying them.
            day = local_day(now, self.timezone_name)
            low, high = self._bounds(day - timedelta(days=179), day)
            workouts = db.scalars(select(orm.Workout).where(
                orm.Workout.user_id == user_id, orm.Workout.source == "zepp",
                orm.Workout.started_at >= low, orm.Workout.started_at < high,
            ).limit(5001)).all()
            if len(workouts) > 5000:
                raise ChartInputLimitError("rule input exceeds 5000 candidate workouts")
            dates = {day} | {local_day(item.started_at, self.timezone_name) for item in workouts if _eligible(item.data or {})}
            event = self._input(db, user_id, kind="activity_rules", dates=dates, now=now, payload_ref=f"activity_rule:{row.id}:{row.revision}")
            return {"rule": projection, "input_event": event}
        return self._write(user_id, "activity_rule", key, fingerprint, now, save)

    @staticmethod
    def _external(db: Session, row: ChartExternalWorkout, *, include_sets: bool = True) -> dict:
        sets = db.scalars(select(ChartExternalSet).where(ChartExternalSet.user_id == row.user_id, ChartExternalSet.external_workout_id == row.id).order_by(ChartExternalSet.order)).all()
        linked = row.linked_workout_id is not None
        result = {
            "id": row.id, "user_id": row.user_id, "source": row.source, "source_scope": row.source_scope,
            "source_record_id": row.source_record_id, "external_identity": row.external_identity,
            "title": row.title, "started_at": row.started_at, "ended_at": row.ended_at,
            "timezone": row.timezone_name, "qualification": "external_user_upload", "time_basis": "activity_day",
            "observed_at": row.started_at, "fetched_at": row.created_at, "created_at": row.created_at,
            "updated_at": row.updated_at, "revision": row.revision,
            "linked_workout_source": row.linked_workout_source, "linked_workout_id": row.linked_workout_id,
            "linked_at": row.linked_at, "user_decision": "linked" if linked else "unlinked",
            "analysis_identity": {"source": row.linked_workout_source, "workout_id": row.linked_workout_id} if linked else {"source": row.source, "workout_id": row.id},
            "count_as_separate_workout": not linked, "set_count": len(sets),
            "unmapped_set_count": sum(not item.payload.get("primary_muscles") and not item.payload.get("secondary_muscles") for item in sets),
            "limitations": ["Uploaded external records; only explicit user mappings identify muscles. Linking does not overwrite vendor facts or confirm exercise identity."],
        }
        if include_sets:
            result["sets"] = [{"id": item.id, **dict(item.payload), "observed_at": row.started_at, "fetched_at": row.created_at} for item in sets]
        return result

    def import_hevy(self, user_id: str, sessions: tuple[ImportedStrengthSession, ...], *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            results, dates, created = [], set(), 0
            for session in sessions:
                row = db.scalar(select(ChartExternalWorkout).where(ChartExternalWorkout.user_id == user_id, ChartExternalWorkout.source == "hevy", ChartExternalWorkout.external_identity == session.identity))
                if row is not None:
                    if row.content_hash != session.content_hash:
                        raise ChartInputConflict("external session already exists with different sets")
                else:
                    row = ChartExternalWorkout(
                        id=uuid4().hex, user_id=user_id, source="hevy", source_scope="hevy_csv",
                        source_record_id=session.source_record_id, external_identity=session.identity,
                        content_hash=session.content_hash, title=session.title, started_at=_utc(session.started_at),
                        ended_at=_utc(session.ended_at), timezone_name=session.timezone_name,
                        revision=1, created_at=_utc(now), updated_at=_utc(now),
                    )
                    db.add(row)
                    db.flush()
                    db.add_all([ChartExternalSet(id=uuid4().hex, user_id=user_id, external_workout_id=row.id, order=item.order, payload=item.model_dump(mode="json")) for item in session.sets])
                    db.flush()
                    self._save_revision(db, user_id, "external_workout", row, self._external(db, row), now)
                    dates.update(_interval_dates(row.started_at, row.ended_at, self.timezone_name))
                    created += 1
                results.append(self._external(db, row))
            event = self._input(db, user_id, kind="external_strength_sets", dates=dates, now=now, payload_ref=f"hevy_import:{uuid4().hex}", source="hevy") if created else None
            return {"external_workouts": results, "created_count": created, "existing_count": len(results) - created, "input_event": event}
        return self._write(user_id, "hevy_import", key, fingerprint, now, save)

    def _candidate(self, workout: orm.Workout, external: ChartExternalWorkout) -> dict | None:
        start, end = _workout_interval(workout)
        # Explicit endpoint overlap is stronger evidence than sharing a local
        # date, but either is only a candidate, never an inferred identity.
        overlaps = start <= external.ended_at and end >= external.started_at
        same_day = local_day(start, self.timezone_name) == local_day(external.started_at, self.timezone_name)
        if not overlaps and not same_day:
            return None
        return {"source": workout.source, "workout_id": workout.workout_id, "started_at": start, "ended_at": end,
                "type": (workout.data or {}).get("type"), "match_basis": "time_overlap" if overlaps else "same_activity_day"}

    def link_hevy(self, user_id: str, external_id: str, body: HevyLinkInput, *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            row = self._owned(db, ChartExternalWorkout, user_id, external_id)
            self._revision(row, body.expected_revision)
            if row.linked_workout_id is not None:
                raise ChartInputConflict("external workout is already linked")
            workout = self._workout(db, user_id, body.workout_source, body.workout_id)
            if self._candidate(workout, row) is None:
                raise ChartInputValidationError("workout must overlap or share the external activity day")
            if _workout_interval(workout)[1] > _utc(now):
                raise ChartInputValidationError("link requires a completed workout")
            used = db.scalar(select(ChartExternalWorkout.id).where(ChartExternalWorkout.user_id == user_id, ChartExternalWorkout.linked_workout_source == body.workout_source, ChartExternalWorkout.linked_workout_id == body.workout_id))
            if used is not None:
                raise ChartInputConflict("Zepp workout is already linked to an external session")
            row.linked_workout_source, row.linked_workout_id = body.workout_source, body.workout_id
            row.linked_at = row.updated_at = _utc(now)
            row.revision += 1
            projection = self._external(db, row)
            self._save_revision(db, user_id, "external_workout", row, projection, now)
            start, end = _workout_interval(workout)
            dates = _interval_dates(row.started_at, row.ended_at, self.timezone_name) | _interval_dates(start, end, self.timezone_name)
            event = self._input(db, user_id, kind="external_strength_sets", dates=dates, now=now, payload_ref=f"hevy_link:{row.id}:{row.revision}", source="hevy")
            return {"external_workout": projection, "input_event": event}
        return self._write(user_id, "hevy_link", key, fingerprint, now, save)

    def map_hevy_sets(self, user_id: str, external_id: str, body: HevySetMappingInput, *, key: str, fingerprint: str, now: datetime) -> dict:
        def save(db):
            row = self._owned(db, ChartExternalWorkout, user_id, external_id)
            self._revision(row, body.expected_revision)
            sets = {item.id: item for item in db.scalars(select(ChartExternalSet).where(ChartExternalSet.user_id == user_id, ChartExternalSet.external_workout_id == external_id)).all()}
            if any(item.set_id not in sets for item in body.mappings):
                raise ChartInputNotFound("mapping set does not belong to this external workout")
            for mapping in body.mappings:
                item = sets[mapping.set_id]
                muscles = mapping.primary_muscles + mapping.secondary_muscles
                item.payload = {
                    **dict(item.payload), **mapping.model_dump(mode="json", exclude={"set_id"}),
                    "muscle_groups": muscles, "mapping_status": "user_mapped" if muscles else "unknown",
                    "mapping_source": "user_confirmed", "mapping_observed_at": _json(now),
                }
            row.revision += 1
            row.updated_at = _utc(now)
            db.flush()
            projection = self._external(db, row)
            self._save_revision(db, user_id, "external_workout", row, projection, now)
            dates = _interval_dates(row.started_at, row.ended_at, self.timezone_name)
            if row.linked_workout_id is not None:
                workout = self._workout(db, user_id, row.linked_workout_source, row.linked_workout_id)
                dates.update(_interval_dates(*_workout_interval(workout), self.timezone_name))
            event = self._input(db, user_id, kind="external_strength_sets", dates=dates, now=now, payload_ref=f"hevy_mapping:{row.id}:{row.revision}", source="hevy")
            return {"external_workout": projection, "input_event": event}
        return self._write(user_id, "hevy_set_mapping", key, fingerprint, now, save)

    def _bounds(self, start: date, end: date, timezone_name: str | None = None) -> tuple[datetime, datetime]:
        zone = timezone_name or self.timezone_name
        first, _ = local_day_utc_bounds(start, zone)
        _, last = local_day_utc_bounds(end, zone)
        return _utc(first), _utc(last)

    @staticmethod
    def _list(items: list[dict], limit: int, start: date, end: date, time_basis: str) -> dict:
        return _json({"items": items[:limit], "truncated": len(items) > limit, "limit": limit,
                      "period_start": start, "period_end": end, "time_basis": time_basis})

    def annotations(self, user_id: str, start: date, end: date, *, limit: int = 100) -> dict:
        low, high = self._bounds(start, end)
        with self._sessions() as db:
            self._owner(db, user_id)
            rows = db.scalars(select(ChartAnnotation).where(ChartAnnotation.user_id == user_id, ChartAnnotation.deleted.is_(False), ChartAnnotation.started_at < high, ChartAnnotation.ended_at >= low).order_by(ChartAnnotation.started_at, ChartAnnotation.id).limit(limit + 1)).all()
            return self._list([self._annotation(row) for row in rows], limit, start, end, "calendar_day")

    def activity_reviews(self, user_id: str, start: date, end: date, *, limit: int = 100, status: str = "pending") -> dict:
        low, high = self._bounds(start, end)
        with self._sessions() as db:
            self._owner(db, user_id)
            pairs = db.execute(select(orm.Workout, ChartWorkoutReview).outerjoin(ChartWorkoutReview, and_(ChartWorkoutReview.user_id == orm.Workout.user_id, ChartWorkoutReview.workout_source == orm.Workout.source, ChartWorkoutReview.workout_id == orm.Workout.workout_id)).where(orm.Workout.user_id == user_id, orm.Workout.source == "zepp", orm.Workout.started_at >= low, orm.Workout.started_at < high).order_by(orm.Workout.started_at.desc(), orm.Workout.workout_id).limit(5001)).all()
            if len(pairs) > 5000:
                raise ChartInputLimitError("workout queue scan exceeds 5000 records; narrow the period")
            rules = [self._rule(row) for row in db.scalars(select(ChartActivityRule).where(ChartActivityRule.user_id == user_id, ChartActivityRule.enabled.is_(True))).all()]
            external = db.scalars(select(ChartExternalWorkout).where(ChartExternalWorkout.user_id == user_id, ChartExternalWorkout.linked_workout_id.is_(None), ChartExternalWorkout.started_at < high, ChartExternalWorkout.ended_at >= low).limit(501)).all()
            if len(external) > 500:
                raise ChartInputLimitError("suggestion scan exceeds 500 external workouts")
            items = []
            for workout, review in pairs:
                data = workout.data or {}
                if review is None and not _eligible(data):
                    continue
                decision = review.decision if review is not None else "pending"
                if status != "all" and status != decision:
                    continue
                item = {"workout_source": workout.source, "workout_id": workout.workout_id, "started_at": workout.started_at,
                        "auto_recognized": data.get("auto_recognized"), "original_type": data.get("type"), "original_sport_mode": data.get("sport_mode"),
                        "decision": decision, "revision": review.revision if review is not None else 0,
                        "review": self._review(review) if review is not None else None,
                        "eligibility": [reason for reason, qualifies in (("auto_recognized", data.get("auto_recognized") is True), ("unknown_classification", data.get("sport_mode") in (None, "", "unknown") or data.get("recognition_confidence") == "NONE")) if qualifies]}
                item.update(activity_suggestions(
                    data, weekday=local_day(workout.started_at, self.timezone_name).isoweekday(), rules=rules,
                    external_ids=[candidate.id for candidate in external if self._candidate(workout, candidate) is not None],
                ))
                items.append(item)
                if len(items) > limit:
                    break
            return self._list(items, limit, start, end, "activity_day")

    def external_workouts(self, user_id: str, start: date, end: date, *, limit: int = 100) -> dict:
        low, high = self._bounds(start, end)
        with self._sessions() as db:
            self._owner(db, user_id)
            rows = db.scalars(select(ChartExternalWorkout).where(ChartExternalWorkout.user_id == user_id, ChartExternalWorkout.started_at >= low, ChartExternalWorkout.started_at < high).order_by(ChartExternalWorkout.started_at.desc(), ChartExternalWorkout.id).limit(limit + 1)).all()
            return self._list([self._external(db, row, include_sets=False) for row in rows], limit, start, end, "activity_day")

    def external_workout(self, user_id: str, external_id: str) -> dict:
        with self._sessions() as db:
            return _json(self._external(db, self._owned(db, ChartExternalWorkout, user_id, external_id)))

    def hevy_candidates(self, user_id: str, external_id: str) -> dict:
        with self._sessions() as db:
            row = self._owned(db, ChartExternalWorkout, user_id, external_id)
            low, high = self._bounds(local_day(row.started_at, self.timezone_name), local_day(row.ended_at, self.timezone_name))
            # Include prior-day activities whose explicit end overlaps the upload.
            candidates = db.scalars(select(orm.Workout).where(orm.Workout.user_id == user_id, orm.Workout.source == "zepp", orm.Workout.started_at >= low - timedelta(days=7), orm.Workout.started_at < high).order_by(orm.Workout.started_at, orm.Workout.workout_id).limit(501)).all()
            if len(candidates) > 500:
                raise ChartInputLimitError("candidate scan exceeds 500 records")
            matches = []
            for workout in candidates:
                candidate = self._candidate(workout, row)
                if candidate is not None:
                    used = db.scalar(select(ChartExternalWorkout.id).where(ChartExternalWorkout.user_id == user_id, ChartExternalWorkout.linked_workout_source == "zepp", ChartExternalWorkout.linked_workout_id == workout.workout_id))
                    candidate["already_linked"] = used is not None
                    matches.append(candidate)
            return _json({"external_workout_id": row.id, "state": "ambiguous" if len(matches) > 1 else "candidate" if matches else "no_candidate", "candidates": matches, "requires_explicit_link": True, "linked_workout_source": row.linked_workout_source, "linked_workout_id": row.linked_workout_id})

    def snapshot(self, user_id: str, start: date, end: date, *, as_of: datetime, timezone_name: str | None = None) -> dict:
        """Return detached latest visible versions at the supplied analysis cutoff.

        Callers merge a linked external workout into its analysis_identity; they
        must not count it again or use the display-only review as canonical data.
        """
        with self._sessions() as db:
            self._owner(db, user_id)
            return read_chart_input_snapshot(
                db, user_id, start, end, as_of=as_of,
                timezone_name=timezone_name or self.timezone_name,
            )


def read_chart_input_snapshot(
    db: Session, user_id: str, start: date, end: date, *,
    as_of: datetime, timezone_name: str = "UTC",
) -> dict:
    """Read detached input versions inside an existing analysis transaction.

    This function never commits, rolls back, or opens another session. Selecting
    the latest revision before applying the interval filter prevents old values
    resurfacing when an annotation was deleted or moved out of the range.
    """
    # Analysis may read a newly added, not-yet-flushed owner in this same
    # transaction. Every query remains scoped by user; no row means no inputs.
    ZoneInfo(timezone_name)
    low, _ = local_day_utc_bounds(start, timezone_name)
    _, high = local_day_utc_bounds(end, timezone_name)
    latest = select(
        ChartInputRevision.kind, ChartInputRevision.resource_id,
        func.max(ChartInputRevision.revision).label("revision"),
    ).where(
        ChartInputRevision.user_id == user_id, ChartInputRevision.created_at <= _utc(as_of),
    ).group_by(ChartInputRevision.kind, ChartInputRevision.resource_id).subquery()
    rows = db.scalars(select(ChartInputRevision).join(latest, and_(
        ChartInputRevision.kind == latest.c.kind,
        ChartInputRevision.resource_id == latest.c.resource_id,
        ChartInputRevision.revision == latest.c.revision,
    )).where(
        ChartInputRevision.user_id == user_id, ChartInputRevision.kind != "activity_rule",
        ChartInputRevision.started_at < _utc(high), ChartInputRevision.ended_at >= _utc(low),
    ).order_by(ChartInputRevision.started_at, ChartInputRevision.resource_id).limit(5001)).all()
    if len(rows) > 5000:
        raise ChartInputLimitError("snapshot exceeds 5000 inputs; narrow the period")
    result = {"schema_version": "1.0", "user_id": user_id, "period_start": start, "period_end": end, "as_of": as_of, "timezone": timezone_name, "annotations": [], "workout_reviews": [], "external_workouts": []}
    destinations = {"annotation": "annotations", "workout_review": "workout_reviews", "external_workout": "external_workouts"}
    set_count = 0
    for row in rows:
        payload = dict(row.payload)
        if row.kind == "annotation" and payload.get("deleted"):
            continue
        if row.kind == "external_workout":
            set_count += len(payload.get("sets") or [])
            if set_count > 20_000:
                raise ChartInputLimitError("snapshot exceeds 20000 external sets; narrow the period")
        result[destinations[row.kind]].append(payload)
    return _json(result)


__all__ = ["CHART_INPUT_MODELS", "ChartAnnotation", "ChartWorkoutReview", "ChartActivityRule", "ChartExternalWorkout", "ChartExternalSet", "ChartInputRevision", "ChartInputWriteRequest", "SqlChartInputStore", "read_chart_input_snapshot", "register_chart_input_models"]
