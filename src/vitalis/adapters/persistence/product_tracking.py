"""SQL adapter for explicit goals, independently audited feedback, and metrics.

Models are defined here and share the current Base.  All user-owned rows cascade
on user deletion.  Bootstrap supplies sessions; HTTP routes never import SQL.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from statistics import mean, median
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, MetaData, String, UniqueConstraint, or_, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from vitalis.application.product_tracking import (
    GoalInput, GoalPatch, ProductFeedbackInput, ProductIdempotencyConflict,
    ProductResourceNotFound, ProductRevisionConflict, ProductTrackingConflict,
    ProductTrackingValidationError, calculate_progress,
)
from vitalis.intelligence.contracts import TrainingPreferencePatch
from vitalis.time import local_day, local_day_utc_bounds

from .database import Base
from . import models as orm
from .repositories import HealthRepository


class ProductGoal(Base):
    __tablename__ = "product_goals"
    __table_args__ = (
        CheckConstraint("revision >= 1 AND window_days BETWEEN 1 AND 90 AND target_value >= 0", name="ck_product_goal_values"),
        CheckConstraint("comparison IN ('at_least', 'at_most', 'equal')", name="ck_product_goal_comparison"),
        CheckConstraint("aggregation IN ('mean', 'sum', 'latest')", name="ck_product_goal_aggregation"),
        Index("ix_product_goals_user_date", "user_id", "target_date"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_type: Mapped[str] = mapped_column(String(64))
    metric_key: Mapped[str] = mapped_column(String(64))
    target_value: Mapped[float] = mapped_column(Float)
    target_unit: Mapped[str] = mapped_column(String(24))
    target_date: Mapped[date] = mapped_column(Date)
    comparison: Mapped[str] = mapped_column(String(16))
    aggregation: Mapped[str] = mapped_column(String(16))
    window_days: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ProductGoalRevision(Base):
    __tablename__ = "product_goal_revisions"
    __table_args__ = (UniqueConstraint("goal_id", "revision", name="uq_product_goal_revision"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("product_goals.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    input_event_ref: Mapped[str] = mapped_column(String(64))
    changed_preference_fields: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ProductFeedbackEvent(Base):
    __tablename__ = "product_feedback_events"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_product_feedback_revision"),
        CheckConstraint("kind IN ('report_usefulness', 'data_correction', 'recommendation_completion', 'recommendation_outcome', 'event_assessment')", name="ck_product_feedback_kind"),
        Index("ix_product_feedback_user_kind_date", "user_id", "kind", "occurred_on"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    occurred_on: Mapped[date] = mapped_column(Date)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    report_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    workout_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workout_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    health_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ProductFeedbackRevision(Base):
    __tablename__ = "product_feedback_revisions"
    __table_args__ = (UniqueConstraint("feedback_id", "revision", name="uq_product_feedback_revision"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    feedback_id: Mapped[str] = mapped_column(ForeignKey("product_feedback_events.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    input_event_ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ProductInputEvent(Base):
    """Non-sensitive input metadata; private content is behind an opaque ref."""
    __tablename__ = "product_input_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(24))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    affected_dates: Mapped[list] = mapped_column(JSON)
    affected_streams: Mapped[list] = mapped_column(JSON)
    payload_ref: Mapped[str] = mapped_column(String(128))
    client_event_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_revision: Mapped[int] = mapped_column(Integer)
    analysis_job_ids: Mapped[list] = mapped_column(JSON)


class ProductWriteRequest(Base):
    __tablename__ = "product_write_requests"
    __table_args__ = (UniqueConstraint("user_id", "key_digest", name="uq_product_write_request"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key_digest: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ProductWriteAttempt(Base):
    """Only requests reaching the adapter form this explicit conflict denominator."""
    __tablename__ = "product_write_attempts"
    __table_args__ = (
        CheckConstraint("outcome IN ('accepted', 'replay', 'revision_conflict', 'idempotency_conflict')", name="ck_product_write_attempt"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


PRODUCT_TRACKING_MODELS = (
    ProductGoal, ProductGoalRevision, ProductFeedbackEvent, ProductFeedbackRevision,
    ProductInputEvent, ProductWriteRequest, ProductWriteAttempt,
)


def register_product_tracking_models(target: MetaData | type[Base]) -> None:
    """Register these models before init/check; no schema DDL or migration."""
    metadata = getattr(target, "metadata", target)
    if not isinstance(metadata, MetaData):
        raise TypeError("target must be declarative Base or MetaData")
    for model in PRODUCT_TRACKING_MODELS:
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
        return (_utc(value).replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _preference_revision(preferences) -> str:
    encoded = json.dumps(preferences.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _preferences_projection(repository: HealthRepository, user_id: str) -> dict[str, Any]:
    preferences = repository.training_preferences(user_id)
    return {
        "revision": _preference_revision(preferences), "source": "training_preferences",
        "available_training_days": list(preferences.available_weekdays),
        "pain_or_injury_status": preferences.pain_or_injury_status,
        "pain_or_injury_notes": preferences.pain_or_injury_notes,
        "updated_at": preferences.updated_at,
    }


_GOAL_FIELDS = ("goal_type", "metric_key", "target_value", "target_unit", "target_date", "comparison", "aggregation", "window_days")
_PREFERENCE_FIELDS = {"available_training_days", "pain_or_injury_status", "pain_or_injury_notes"}
_LINK_FIELDS = ("report_run_id", "workout_source", "workout_id", "recommendation_id", "health_event_id")
_CONTENT_FIELDS = ("usefulness", "correction_field", "corrected_value", "correction_unit", "correction_observed_on", "completed", "outcome", "false_positive", "next_experiment", "notes")


class SqlProductTrackingStore:
    def __init__(self, sessions: sessionmaker[Session], *, timezone_name: str = "UTC") -> None:
        self._sessions = sessions
        self.timezone_name = timezone_name

    @staticmethod
    def _owner(db: Session, user_id: str) -> orm.User:
        row = db.get(orm.User, user_id)
        if row is None:
            raise ProductResourceNotFound("user not found")
        return row

    @staticmethod
    def _lock_owner(db: Session, user_id: str) -> None:
        changed = db.execute(update(orm.User).where(orm.User.id == user_id).values(
            analysis_input_revision=orm.User.analysis_input_revision,
        )).rowcount
        if not changed:
            raise ProductResourceNotFound("user not found")

    @staticmethod
    def _claim(db: Session, user_id: str, operation: str, key: str, fingerprint: str, now: datetime) -> tuple[ProductWriteRequest, bool]:
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise NotImplementedError("product idempotency requires SQLite or PostgreSQL")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        claimed = db.execute(insert(ProductWriteRequest).values(
            id=uuid4().hex, user_id=user_id, key_digest=digest, request_hash=fingerprint,
            operation=operation, created_at=now,
        ).on_conflict_do_nothing(index_elements=["user_id", "key_digest"])).rowcount
        row = db.execute(select(ProductWriteRequest).where(
            ProductWriteRequest.user_id == user_id, ProductWriteRequest.key_digest == digest,
        )).scalar_one()
        if not claimed and (row.request_hash != fingerprint or row.operation != operation):
            raise ProductIdempotencyConflict("Idempotency-Key belongs to another product request")
        if not claimed and row.response is None:
            raise RuntimeError("product request has no committed result")
        return row, bool(claimed)

    def _write(self, user_id: str, operation: str, key: str, fingerprint: str, now: datetime, create) -> dict[str, Any]:
        now = _utc(now)
        try:
            with self._sessions() as db:
                self._lock_owner(db, user_id)
                request, claimed = self._claim(db, user_id, operation, key, fingerprint, now)
                if claimed:
                    result = _json(create(db))
                    request.response = result
                else:
                    result = dict(request.response)
                db.add(ProductWriteAttempt(
                    id=uuid4().hex, user_id=user_id, operation=operation,
                    outcome="accepted" if claimed else "replay", created_at=now,
                ))
                db.commit()
                return result
        except ProductTrackingConflict as exc:
            # Input facts and jobs roll back first. A conflict attempt has no
            # health payload and is deliberately recorded in its own transaction.
            with self._sessions() as db:
                if db.get(orm.User, user_id) is not None:
                    db.add(ProductWriteAttempt(
                        id=uuid4().hex, user_id=user_id, operation=operation,
                        outcome=exc.kind, created_at=now,
                    ))
                    db.commit()
            raise

    @staticmethod
    def _goal(row: ProductGoal) -> dict[str, Any]:
        return {
            "id": row.id, "user_id": row.user_id,
            **{name: getattr(row, name) for name in _GOAL_FIELDS},
            "revision": row.revision, "source": "user_confirmed", "accepted": True,
            "created_at": row.created_at, "updated_at": row.updated_at,
        }

    @staticmethod
    def _feedback(row: ProductFeedbackEvent) -> dict[str, Any]:
        return {
            "id": row.id, "user_id": row.user_id, "kind": row.kind,
            "occurred_on": row.occurred_on, "revision": row.revision,
            **{name: getattr(row, name) for name in _LINK_FIELDS},
            **dict(row.payload), "source": "explicit_user_feedback",
            "applied_to_source": False if row.kind == "data_correction" else None,
            "created_at": row.created_at, "updated_at": row.updated_at,
        }

    @staticmethod
    def _preferences(repository: HealthRepository, user_id: str) -> dict[str, Any]:
        return _preferences_projection(repository, user_id)

    @staticmethod
    def _apply_preferences(repository: HealthRepository, user_id: str, body: GoalInput | GoalPatch) -> list[str]:
        changed_fields = sorted(body.model_fields_set & _PREFERENCE_FIELDS)
        if not changed_fields:
            return []
        current = repository.training_preferences(user_id)
        if body.expected_training_preferences_revision != _preference_revision(current):
            raise ProductRevisionConflict("training preferences changed; read their revision again")
        patch = {}
        for field in changed_fields:
            patch["available_weekdays" if field == "available_training_days" else field] = getattr(body, field)
        try:
            repository.patch_training_preferences(user_id, TrainingPreferencePatch.model_validate(patch))
        except ValidationError as exc:
            raise ProductTrackingValidationError("training preference change is invalid") from exc
        return changed_fields

    def _input(
        self, db: Session, user_id: str, *, before_revision: int, now: datetime,
        event_type: str, queue_type: str, dates: set[date], streams: set[str],
        payload_ref: str, client_ref: str | None, target_dates: set[date],
    ) -> dict[str, Any]:
        repository = HealthRepository(db)
        if repository.analysis_input_revision(user_id) == before_revision:
            repository.bump_analysis_input_revision(user_id)
        event_id = uuid4().hex
        jobs = repository.enqueue_input_change(
            user_id, event_id=event_id, event_type=queue_type, source="user",
            affected_dates=dates, affected_streams=streams, target_dates=target_dates,
            reason=f"explicit {event_type}",
        )
        if not jobs:
            raise RuntimeError("product input did not enqueue analysis")
        row = ProductInputEvent(
            id=event_id, user_id=user_id, event_type=event_type, source="user", occurred_at=now,
            affected_dates=sorted(day.isoformat() for day in dates), affected_streams=sorted(streams),
            payload_ref=payload_ref, client_event_ref=client_ref,
            input_revision=repository.analysis_input_revision(user_id), analysis_job_ids=[job.id for job in jobs],
        )
        db.add(row)
        db.flush()
        return self._input_projection(row)

    @staticmethod
    def _input_projection(row: ProductInputEvent) -> dict[str, Any]:
        return {
            "id": row.id, "input_event_ref": f"product-input:{row.id}", "user_id": row.user_id,
            "event_type": row.event_type, "source": row.source, "occurred_at": row.occurred_at,
            "affected_dates": list(row.affected_dates), "affected_streams": list(row.affected_streams),
            "payload_ref": row.payload_ref, "client_event_ref": row.client_event_ref,
            "input_revision": row.input_revision, "analysis_job_ids": list(row.analysis_job_ids),
        }

    def _save_goal(self, db: Session, user_id: str, body: GoalInput, *, goal_id: str | None, today: date, now: datetime) -> dict[str, Any]:
        repository = HealthRepository(db)
        before = repository.analysis_input_revision(user_id)
        row = db.get(ProductGoal, goal_id) if goal_id else None
        if goal_id and (row is None or row.user_id != user_id):
            raise ProductResourceNotFound("goal not found")
        actual = row.revision if row is not None else 0
        if body.expected_revision != actual:
            raise ProductRevisionConflict("goal revision changed")
        values = {name: getattr(body, name) for name in _GOAL_FIELDS}
        values["metric_key"] = body.metric_key or body.goal_type
        if row is None:
            row = ProductGoal(
                id=uuid4().hex, user_id=user_id, **values, revision=1,
                created_at=now, updated_at=now,
            )
            db.add(row)
        else:
            changed = db.execute(update(ProductGoal).where(
                ProductGoal.id == goal_id, ProductGoal.user_id == user_id,
                ProductGoal.revision == body.expected_revision,
            ).values(**values, revision=actual + 1, updated_at=now)).rowcount
            if not changed:
                raise ProductRevisionConflict("goal revision changed")
        db.flush()
        changed_preferences = self._apply_preferences(repository, user_id, body)
        input_event = self._input(
            db, user_id, before_revision=before, now=now, event_type="product_goal", queue_type="preferences",
            dates={today}, streams={"product_goal", "preferences", "recommendation"},
            payload_ref=f"goal:{row.id}:r{row.revision}", client_ref=body.input_event_ref, target_dates={today},
        )
        goal = self._goal(row)
        db.add(ProductGoalRevision(
            id=uuid4().hex, user_id=user_id, goal_id=row.id, revision=row.revision, payload=_json(goal),
            input_event_ref=input_event["input_event_ref"], changed_preference_fields=changed_preferences, created_at=now,
        ))
        db.flush()
        return {"goal": goal, "training_preferences": self._preferences(repository, user_id), "input_event": input_event}

    def write_goal(self, user_id: str, body: GoalInput, *, goal_id: str | None, key: str, fingerprint: str, today: date, now: datetime) -> dict[str, Any]:
        return self._write(user_id, "goal", key, fingerprint, now, lambda db: self._save_goal(
            db, user_id, body, goal_id=goal_id, today=today, now=_utc(now),
        ))

    def patch_goal(self, user_id: str, goal_id: str, body: GoalPatch, *, key: str, fingerprint: str, today: date, now: datetime) -> dict[str, Any]:
        def save(db: Session):
            row = db.get(ProductGoal, goal_id)
            if row is None or row.user_id != user_id:
                raise ProductResourceNotFound("goal not found")
            if row.revision != body.expected_revision:
                raise ProductRevisionConflict("goal revision changed")
            values = {name: getattr(row, name) for name in _GOAL_FIELDS}
            values.update(body.model_dump(exclude_unset=True))
            values["confirmed"] = True
            return self._save_goal(db, user_id, GoalInput.model_validate(values), goal_id=goal_id, today=today, now=_utc(now))
        return self._write(user_id, "goal_patch", key, fingerprint, now, save)

    def _references(self, db: Session, user_id: str, body: ProductFeedbackInput, occurred_on: date) -> tuple[set[date], orm.RecommendationInstance | None]:
        dates = {occurred_on}
        if body.correction_observed_on is not None:
            dates.add(body.correction_observed_on)
        run = db.get(orm.AnalysisRun, body.report_run_id) if body.report_run_id else None
        if body.report_run_id:
            snapshot = db.execute(select(orm.AnalysisSnapshot.id).where(
                orm.AnalysisSnapshot.user_id == user_id, orm.AnalysisSnapshot.analysis_run_id == body.report_run_id,
            ).limit(1)).scalar_one_or_none()
            if run is None or run.user_id != user_id or run.status != "SUCCEEDED" or snapshot is None:
                raise ProductResourceNotFound("saved report run not found")
            dates.add(run.target_date)
        workout = None
        if body.workout_id:
            workout = HealthRepository(db).workout(user_id, body.workout_id, source=body.workout_source)
            if workout is None:
                raise ProductResourceNotFound("workout not found")
            if workout.started_at is not None:
                dates.add(local_day(workout.started_at, self.timezone_name))
        recommendation = db.get(orm.RecommendationInstance, body.recommendation_id) if body.recommendation_id else None
        if body.recommendation_id:
            if recommendation is None or recommendation.user_id != user_id:
                raise ProductResourceNotFound("recommendation not found")
            if run is not None and recommendation.analysis_run_id != run.id:
                raise ProductTrackingValidationError("report run and recommendation refer to different analyses")
            if workout is not None and recommendation.linked_workout_id is not None and (
                recommendation.linked_workout_source != body.workout_source or recommendation.linked_workout_id != body.workout_id
            ):
                raise ProductTrackingValidationError("workout differs from explicitly completed recommendation")
            dates.add(recommendation.date)
        if body.health_event_id:
            event = db.get(orm.HealthEventRecord, body.health_event_id)
            if event is None or event.user_id != user_id:
                raise ProductResourceNotFound("health event not found")
            dates.update({event.start_date, event.end_date})
            if run is not None:
                referenced = db.execute(select(orm.HealthEventObservation.id).where(
                    orm.HealthEventObservation.user_id == user_id, orm.HealthEventObservation.event_id == event.id,
                    orm.HealthEventObservation.analysis_run_id == run.id,
                ).limit(1)).scalar_one_or_none()
                if referenced is None:
                    raise ProductTrackingValidationError("health event was not observed in the supplied report run")
        return dates, recommendation

    def record_feedback(self, user_id: str, body: ProductFeedbackInput, *, key: str, fingerprint: str, today: date, now: datetime) -> dict[str, Any]:
        def save(db: Session):
            now_utc = _utc(now)
            occurred_on = body.occurred_on or today
            if occurred_on > today or body.correction_observed_on is not None and body.correction_observed_on > today:
                raise ProductTrackingValidationError("future user observations cannot be recorded")
            repository = HealthRepository(db)
            before = repository.analysis_input_revision(user_id)
            row = db.get(ProductFeedbackEvent, body.feedback_id) if body.feedback_id else None
            if body.feedback_id and (row is None or row.user_id != user_id):
                raise ProductResourceNotFound("feedback not found")
            if row is not None:
                if row.revision != body.expected_revision:
                    raise ProductRevisionConflict("feedback revision changed")
                if row.kind != body.kind or any(getattr(row, name) != getattr(body, name) for name in _LINK_FIELDS):
                    raise ProductTrackingValidationError("feedback kind and owned links are immutable")
                if body.occurred_on is None:
                    occurred_on = row.occurred_on
            dates, recommendation = self._references(db, user_id, body, occurred_on)
            if row is not None:
                dates.add(row.occurred_on)
                previous_date = row.payload.get("correction_observed_on")
                if previous_date:
                    dates.add(date.fromisoformat(previous_date))
            if body.kind == "recommendation_completion" and recommendation is not None:
                if body.completed is False and recommendation.completion_status == "COMPLETED":
                    raise ProductTrackingConflict("recommendation already has an explicit completed workout")
                if body.completed is True and body.workout_id:
                    try:
                        repository.link_recommendation(user_id, recommendation.id, body.workout_id, body.workout_source)
                    except ValueError as exc:
                        raise ProductTrackingValidationError("recommendation completion link is invalid") from exc
            payload = _json({name: getattr(body, name) for name in _CONTENT_FIELDS})
            if row is None:
                row = ProductFeedbackEvent(
                    id=uuid4().hex, user_id=user_id, kind=body.kind, occurred_on=occurred_on,
                    revision=1, payload=payload, **{name: getattr(body, name) for name in _LINK_FIELDS},
                    created_at=now_utc, updated_at=now_utc,
                )
                db.add(row)
            else:
                changed = db.execute(update(ProductFeedbackEvent).where(
                    ProductFeedbackEvent.id == row.id, ProductFeedbackEvent.user_id == user_id,
                    ProductFeedbackEvent.revision == body.expected_revision,
                ).values(revision=body.expected_revision + 1, payload=payload, occurred_on=occurred_on, updated_at=now_utc)).rowcount
                if not changed:
                    raise ProductRevisionConflict("feedback revision changed")
            db.flush()
            streams = {"product_feedback", body.kind}
            if body.kind in {"recommendation_completion", "recommendation_outcome"}:
                streams.update({"recommendation", "training_response"})
            if body.kind == "event_assessment":
                streams.add("events")
            input_event = self._input(
                db, user_id, before_revision=before, now=now_utc, event_type="product_feedback", queue_type="feedback",
                dates=dates, streams=streams, payload_ref=f"feedback:{row.id}:r{row.revision}",
                client_ref=body.input_event_ref, target_dates={max(dates)},
            )
            feedback = self._feedback(row)
            db.add(ProductFeedbackRevision(
                id=uuid4().hex, user_id=user_id, feedback_id=row.id, revision=row.revision,
                payload=_json(feedback), input_event_ref=input_event["input_event_ref"], created_at=now_utc,
            ))
            db.flush()
            return {"feedback": feedback, "input_event": input_event}
        return self._write(user_id, "feedback", key, fingerprint, now, save)

    def goals(self, user_id: str) -> dict[str, Any]:
        with self._sessions() as db:
            self._owner(db, user_id)
            rows = db.scalars(select(ProductGoal).where(ProductGoal.user_id == user_id).order_by(ProductGoal.created_at, ProductGoal.id)).all()
            return _json({"goals": [self._goal(row) for row in rows], "training_preferences": self._preferences(HealthRepository(db), user_id)})

    def feedback(self, user_id: str, start: date, end: date, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._sessions() as db:
            self._owner(db, user_id)
            rows = db.scalars(select(ProductFeedbackEvent).where(
                ProductFeedbackEvent.user_id == user_id, ProductFeedbackEvent.occurred_on.between(start, end),
            ).order_by(ProductFeedbackEvent.updated_at.desc(), ProductFeedbackEvent.id).limit(limit)).all()
            return _json([self._feedback(row) for row in rows])

    def input_events(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._sessions() as db:
            self._owner(db, user_id)
            rows = db.scalars(select(ProductInputEvent).where(ProductInputEvent.user_id == user_id).order_by(ProductInputEvent.occurred_at.desc(), ProductInputEvent.id).limit(limit)).all()
            return _json([self._input_projection(row) for row in rows])

    def _context(self, db: Session, user_id: str, day: date, *, as_of: datetime | None = None) -> dict[str, Any]:
        return product_analysis_context(
            db, user_id, day, as_of=as_of or datetime.now(timezone.utc),
            timezone_name=self.timezone_name,
        )

    def analysis_context(
        self, user_id: str, day: date, *, as_of: datetime | None = None,
    ) -> dict[str, Any]:
        with self._sessions() as db:
            self._owner(db, user_id)
            return self._context(db, user_id, day, as_of=as_of)

    @staticmethod
    def _saved_reports(db: Session, user_id: str, start: date, end: date, cutoff: datetime):
        pairs = db.execute(select(orm.AnalysisSnapshot, orm.AnalysisRun).join(
            orm.AnalysisRun, orm.AnalysisRun.id == orm.AnalysisSnapshot.analysis_run_id,
        ).where(
            orm.AnalysisSnapshot.user_id == user_id, orm.AnalysisRun.user_id == user_id,
            orm.AnalysisRun.status == "SUCCEEDED", orm.AnalysisRun.completed_at <= cutoff,
            orm.AnalysisRun.target_date.between(start, end),
            orm.AnalysisSnapshot.profile_type.in_(("daily", "weekly", "monthly")),
        ).order_by(orm.AnalysisRun.completed_at, orm.AnalysisSnapshot.id)).all()
        daily = {}
        for snapshot, run in pairs:
            if snapshot.profile_type == "daily":
                daily[run.target_date] = (snapshot, run)
        return pairs, daily

    @staticmethod
    def _snapshot_series(daily) -> dict[str, list[dict[str, Any]]]:
        series = defaultdict(list)
        for day, (snapshot, _run) in daily.items():
            payload = snapshot.payload or {}
            facts = payload.get("facts")
            if not isinstance(facts, Mapping):
                continue
            for values in facts.values():
                if not isinstance(values, list):
                    continue
                for fact in values:
                    if not isinstance(fact, Mapping) or not isinstance(fact.get("provenance"), Mapping):
                        continue
                    observed = fact.get("observed_at")
                    if not observed or str(observed)[:10] != day.isoformat():
                        continue
                    provenance = fact["provenance"]
                    series[str(fact.get("metric") or "")].append({
                        "day": day, "value": fact.get("value"), "unit": fact.get("unit"), "observed_at": observed,
                        "source": provenance.get("source"), "source_scope": provenance.get("source_scope"), "device_id": provenance.get("device_id"),
                    })
        return dict(series)

    def summary(self, user_id: str, start: date, end: date, *, as_of: datetime) -> dict[str, Any]:
        with self._sessions() as db:
            context = self._context(db, user_id, end, as_of=as_of)
            context["observed_as_of"] = as_of
            _, daily = self._saved_reports(db, user_id, end - timedelta(days=89), end, _utc(as_of))
            summary = calculate_progress(context, self._snapshot_series(daily)).as_dict()
            summary["metrics"] = self._metrics(db, user_id, start, end, as_of)
            summary["period"] = {"start": start, "end": end}
            return _json(summary)

    @staticmethod
    def _ratio(numerator: int, denominator: int, *, unknown: int = 0, basis: str) -> dict[str, Any]:
        return {"numerator": numerator, "denominator": denominator, "rate": numerator / denominator if denominator else None,
                "unknown_count": unknown, "status": "AVAILABLE" if denominator else "UNKNOWN", "basis": basis}

    @staticmethod
    def _latency(pairs: Sequence[tuple[datetime | None, datetime | None]], *, basis: str) -> dict[str, Any]:
        values, invalid, missing = [], 0, 0
        for begin, finish in pairs:
            if begin is None or finish is None:
                missing += 1
                continue
            seconds = (_utc(finish) - _utc(begin)).total_seconds()
            if seconds < 0:
                invalid += 1
            else:
                values.append(seconds)
        return {"sample_count": len(values), "denominator": len(pairs), "unknown_count": missing, "invalid_count": invalid,
                "average_seconds": mean(values) if values else None, "median_seconds": median(values) if values else None,
                "max_seconds": max(values) if values else None, "unit": "s", "basis": basis,
                "status": "AVAILABLE" if values else "UNKNOWN"}

    def _metrics(self, db: Session, user_id: str, start: date, end: date, as_of: datetime) -> dict[str, Any]:
        cutoff = _utc(as_of)
        pairs, daily = self._saved_reports(db, user_id, start, end, cutoff)
        run_ids = {run.id for _snapshot, run in pairs}
        runs = db.scalars(select(orm.AnalysisRun).where(
            orm.AnalysisRun.user_id == user_id, orm.AnalysisRun.target_date.between(start, end), orm.AnalysisRun.started_at <= cutoff,
        )).all()
        jobs = db.scalars(select(orm.AnalysisJob).where(
            orm.AnalysisJob.user_id == user_id, orm.AnalysisJob.target_date.between(start, end), orm.AnalysisJob.created_at <= cutoff,
        )).all()
        deliveries = db.scalars(select(orm.NotificationDelivery).where(
            orm.NotificationDelivery.user_id == user_id, orm.NotificationDelivery.target_date.between(start, end), orm.NotificationDelivery.created_at <= cutoff,
        )).all()
        recommendations = db.scalars(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id, orm.RecommendationInstance.date.between(start, end),
            orm.RecommendationInstance.created_at <= cutoff, orm.RecommendationInstance.analysis_run_id.in_(run_ids),
        )).all() if run_ids else []
        # Include late feedback about this report cohort even when it was entered
        # on another date. Date-only subjective feedback is not a run-level answer.
        feedback = db.scalars(select(ProductFeedbackEvent).where(
            ProductFeedbackEvent.user_id == user_id, ProductFeedbackEvent.updated_at <= cutoff,
        ).order_by(ProductFeedbackEvent.updated_at, ProductFeedbackEvent.id)).all()
        health_events = db.scalars(select(orm.HealthEventRecord).where(
            orm.HealthEventRecord.user_id == user_id, orm.HealthEventRecord.start_date <= end, orm.HealthEventRecord.end_date >= start,
        )).all()
        event_ids = {event.id for event in health_events}
        marked = {}
        completion = {}
        feedback_runs = set()
        for event in feedback:
            if event.report_run_id in run_ids:
                feedback_runs.add(event.report_run_id)
            if event.kind == "event_assessment" and event.health_event_id in event_ids:
                marked[event.health_event_id] = event.payload["false_positive"]
            if event.kind == "recommendation_completion":
                completion[event.recommendation_id] = event.payload["completed"]
        for subjective in db.scalars(select(orm.SubjectiveFeedback).where(
            orm.SubjectiveFeedback.user_id == user_id, orm.SubjectiveFeedback.created_at <= cutoff,
        )).all():
            matching = next((item for item in recommendations if item.id == subjective.recommendation_id), None)
            if matching is not None:
                feedback_runs.add(matching.analysis_run_id)
        adopted = 0
        unknown_adoption = 0
        for item in recommendations:
            explicit = completion.get(item.id)
            if explicit is True or item.completion_status == "COMPLETED":
                adopted += 1
            elif explicit is None:
                unknown_adoption += 1
        first_cutoffs = {}
        for _snapshot, run in pairs:
            first_cutoffs[run.target_date] = min(first_cutoffs.get(run.target_date, run.started_at), run.started_at)
        source_jobs = db.scalars(select(orm.AnalysisJob).where(
            orm.AnalysisJob.user_id == user_id,
            orm.AnalysisJob.event_type.in_(("source_sync", "sync_completion", "training_coverage")),
            orm.AnalysisJob.created_at <= cutoff,
            or_(
                (orm.AnalysisJob.affected_start <= end) & (orm.AnalysisJob.affected_end >= start),
                orm.AnalysisJob.affected_start.is_(None),
                orm.AnalysisJob.affected_end.is_(None),
            ),
        )).all()
        known_source_jobs, late_jobs, unknown_source_jobs = 0, 0, 0
        for job in source_jobs:
            affected = {date.fromisoformat(value) for value in job.affected_dates or [] if start <= date.fromisoformat(value) <= end}
            if not affected:
                unknown_source_jobs += 1
                continue
            known_source_jobs += 1
            if any(day in first_cutoffs and job.created_at > first_cutoffs[day] for day in affected):
                late_jobs += 1
        flags_known = 0
        flags_conflict = 0
        required = set()
        for snapshot, _run in daily.values():
            quality = (snapshot.payload or {}).get("data_quality")
            if isinstance(quality, Mapping):
                required.update(quality.get("required_signals") or [])
                if isinstance(quality.get("flags"), list):
                    flags_known += 1
                    if any("CONFLICT" in str(flag.get("code", "")).upper() for flag in quality["flags"] if isinstance(flag, Mapping)):
                        flags_conflict += 1
        series = self._snapshot_series(daily)
        signals = required | set(series)
        signal_coverage = {}
        expected_days = (end - start).days + 1
        for metric in sorted(signals):
            observed_days = {point["day"] for point in series.get(metric, ()) if point["value"] is not None and point["unit"] and point["source"]}
            signal_coverage[metric] = self._ratio(len(observed_days), expected_days, unknown=expected_days - len(observed_days), basis="target-day facts in latest saved daily reports / requested calendar days")
        begin, _ = local_day_utc_bounds(start, self.timezone_name)
        _, finish = local_day_utc_bounds(end, self.timezone_name)
        attempts = db.scalars(select(ProductWriteAttempt).where(
            ProductWriteAttempt.user_id == user_id, ProductWriteAttempt.created_at >= _utc(begin),
            ProductWriteAttempt.created_at < min(_utc(finish), cutoff + timedelta(microseconds=1)),
        )).all()
        result = {
            "schema_version": "1.0", "user_id": user_id, "period": {"start": start, "end": end}, "as_of": as_of,
            "coverage": self._ratio(len(daily), expected_days, unknown=expected_days - len(daily), basis="dates with a saved successful daily report / requested calendar dates"),
            "signal_coverage": signal_coverage,
            "late": self._ratio(late_jobs, known_source_jobs, unknown=unknown_source_jobs, basis="source invalidation jobs recorded after an affected successful report's input cutoff / recorded source invalidation jobs"),
            "conflict": self._ratio(flags_conflict, flags_known, unknown=len(daily) - flags_known, basis="latest daily reports with explicit CONFLICT quality flags / latest daily reports with an evaluated flags list"),
            "write_conflict_rate": self._ratio(sum(item.outcome in {"revision_conflict", "idempotency_conflict"} for item in attempts), len(attempts), basis="conflicts / accepted, replayed, and conflicting product requests reaching persistence"),
            "feedback_rate": self._ratio(len(feedback_runs), len(run_ids), unknown=len(run_ids) - len(feedback_runs), basis="saved successful report runs with explicitly linked feedback / saved successful report runs"),
            "adoption": self._ratio(adopted, len(recommendations), unknown=unknown_adoption, basis="explicitly completed recommendations / emitted recommendations with saved successful report runs"),
            "false_positive": self._ratio(sum(value is True for value in marked.values()), len(marked), unknown=len(event_ids) - len(marked), basis="events explicitly marked false positive / explicitly assessed user-owned health events"),
            "report_queue_latency": self._latency([(job.created_at, job.started_at if job.started_at is not None and job.started_at <= cutoff else None) for job in jobs], basis="durable job started_at - created_at; latest recorded job attempt"),
            "report_compute_latency": self._latency([(run.started_at, run.completed_at if run.completed_at is not None and run.completed_at <= cutoff else None) for run in runs], basis="analysis run completed_at - started_at, including observed failed runs"),
            "report_delivery_latency": self._latency([(delivery.created_at, delivery.updated_at if delivery.status == "delivered" and delivery.updated_at <= cutoff else None) for delivery in deliveries], basis="confirmed delivered intent updated_at - created_at; accepted/uncertain delivery remains unknown"),
        }
        result["limitations"] = [
            "Coverage describes saved report/fact availability; it does not prove source collection completeness.",
            "Late counts persisted source invalidation jobs; coalesced jobs are not a count of every late source record.",
            "Delivery timing uses the persisted delivered-state timestamp; delivery/open/silence never supplies feedback.",
            "Data corrections are explicit user assertions; source facts are not overwritten by this endpoint.",
        ]
        return result

    def metrics(self, user_id: str, start: date, end: date, *, as_of: datetime) -> dict[str, Any]:
        with self._sessions() as db:
            self._owner(db, user_id)
            return _json(self._metrics(db, user_id, start, end, as_of))


def _feedback_history(
    db: Session, user_id: str, *, day: date, cutoff: datetime,
) -> list[dict[str, Any]]:
    """Project the latest revision visible at ``cutoff`` without leaking future edits."""
    selected: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    revision_rows = db.scalars(select(ProductFeedbackRevision).where(
        ProductFeedbackRevision.user_id == user_id,
        ProductFeedbackRevision.created_at <= cutoff,
    ).order_by(
        ProductFeedbackRevision.feedback_id,
        ProductFeedbackRevision.revision.desc(),
        ProductFeedbackRevision.created_at.desc(),
        ProductFeedbackRevision.id.desc(),
    )).all()
    for revision in revision_rows:
        if revision.feedback_id in seen_ids:
            continue
        seen_ids.add(revision.feedback_id)
        payload = dict(revision.payload or {})
        payload.setdefault("id", revision.feedback_id)
        payload.setdefault("user_id", user_id)
        payload.setdefault("revision", revision.revision)
        payload.setdefault("source", "explicit_user_feedback")
        payload.setdefault(
            "applied_to_source", False if payload.get("kind") == "data_correction" else None,
        )
        occurred_raw = payload.get("occurred_on")
        try:
            occurred_on = (
                occurred_raw if isinstance(occurred_raw, date)
                else date.fromisoformat(str(occurred_raw)[:10])
            )
        except (TypeError, ValueError):
            occurred_on = None
        # A later revision can move an aggregate to a future day.  Keep its ID
        # claimed without falling back to an older revision in this context.
        if occurred_on is not None and occurred_on <= day:
            selected[revision.feedback_id] = payload

    current_rows = db.scalars(select(ProductFeedbackEvent).where(
        ProductFeedbackEvent.user_id == user_id,
        ProductFeedbackEvent.created_at <= cutoff,
        ProductFeedbackEvent.occurred_on <= day,
    ).order_by(ProductFeedbackEvent.created_at.desc(), ProductFeedbackEvent.id.desc())).all()
    for row in current_rows:
        if row.id in seen_ids:
            continue
        selected[row.id] = SqlProductTrackingStore._feedback(row)

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("id") or ""))

    return sorted(selected.values(), key=sort_key, reverse=True)


def product_analysis_context(
    db: Session,
    user_id: str,
    day: date,
    *,
    as_of: datetime,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    """Read product inputs at one transaction cutoff for deterministic analysis.

    Goal and feedback revisions are immutable, so a later edit cannot erase what
    an older analysis run could have observed.  The query is always user-scoped
    and clamps the cutoff to the requested local calendar day.
    """
    owner = db.get(orm.User, user_id)
    if owner is None:
        raise ProductResourceNotFound("user not found")
    _, day_end = local_day_utc_bounds(day, timezone_name)
    cutoff = min(_utc(as_of), _utc(day_end))
    rows = db.scalars(select(ProductGoalRevision).where(
        ProductGoalRevision.user_id == user_id,
        ProductGoalRevision.created_at <= cutoff,
    ).order_by(ProductGoalRevision.goal_id, ProductGoalRevision.revision.desc())).all()
    goals: dict[str, dict[str, Any]] = {}
    for row in rows:
        goals.setdefault(row.goal_id, dict(row.payload))

    feedback = _feedback_history(db, user_id, day=day, cutoff=cutoff)
    proposal = None
    for item in feedback:
        if item.get("kind") == "recommendation_outcome" and item.get("next_experiment"):
            proposal = {
                "status": "PROPOSED", "title": item["next_experiment"], "accepted": False,
                "source": "explicit_user_feedback", "feedback_id": item.get("id"),
                "recommendation_id": item.get("recommendation_id"),
            }
            break

    preferences = _preferences_projection(HealthRepository(db), user_id)
    if preferences["updated_at"] is not None and _utc(preferences["updated_at"]) >= cutoff:
        preferences = {
            "source": "training_preferences", "available_training_days": [],
            "pain_or_injury_status": "UNKNOWN", "pain_or_injury_notes": None,
            "revision": None, "updated_at": None,
        }
    return _json({
        "schema_version": "1.0", "user_id": user_id, "day": day,
        "as_of": as_of, "goals": list(goals.values()),
        "training_preferences": preferences, "next_experiment": proposal,
        "feedback": feedback,
    })


__all__ = [
    "PRODUCT_TRACKING_MODELS", "ProductGoal", "ProductGoalRevision", "ProductFeedbackEvent",
    "ProductFeedbackRevision", "ProductInputEvent", "ProductWriteRequest", "ProductWriteAttempt",
    "SqlProductTrackingStore", "product_analysis_context", "register_product_tracking_models",
]
