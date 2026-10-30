"""Durable root-request budgets and crash-safe conversation dispatch."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from vitalis.application.coach_operations import CoachError, CoachOperation, CoachPolicy
from vitalis.application.ports import SyncJobCommand
from .database import Base


class CoachRequest(Base):
    __tablename__ = "coach_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_coach_request_key"),
        Index("ix_coach_requests_user_created", "user_id", "created_at"),
        Index("ix_coach_requests_user_state", "user_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(32))
    day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    state: Mapped[str] = mapped_column(String(24), default="queued")
    reserved_units: Mapped[int] = mapped_column(Integer, default=1)
    policy_revision: Mapped[str] = mapped_column(String(128))
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(String(64), nullable=True)


def _projection(row: CoachRequest) -> dict:
    return {"request_id": row.id, "user_id": row.user_id, "operation": row.operation,
            "day": row.day.isoformat(), "state": row.state, "job_id": row.job_id,
            "policy_revision": row.policy_revision, "error": row.error,
            "status_url": f"/api/jobs/{row.job_id}" if row.job_id else None}


class CoachRequestRepository:
    """session_factory MUST return a commit-on-success context (session_scope).

    A bare Session/sessionmaker context does not commit and is not supported.
    Budgets cover roots only, not descendant work or CPU usage.
    """

    def __init__(self, session_factory, sync_service):
        self.session_factory = session_factory
        self.sync_service = sync_service

    def lookup(self, user_id: str, key: str, request_hash: str) -> dict | None:
        """Recover accepted linkage, never call an adapter or enqueue work."""
        with self.session_factory() as db:
            # Serialize linkage updates with analysis dispatch, including SQLite.
            db.execute(update(CoachRequest).where(
                CoachRequest.user_id == user_id, CoachRequest.idempotency_key == key
            ).values(reserved_units=CoachRequest.reserved_units))
            row = db.scalar(select(CoachRequest).where(
                CoachRequest.user_id == user_id, CoachRequest.idempotency_key == key))
            if row is None:
                return None
            if row.request_hash != request_hash:
                raise CoachError("Idempotency-Key conflicts with previous request", 409)
            self._reconcile(db, row)
            result = _projection(row)
        # Commit recovered linkage even when the accepted job has failed.
        self._raise_failed(result)
        return result

    def reserve(self, user_id: str, key: str, request: CoachOperation, request_hash: str,
                policy: CoachPolicy, now: datetime) -> dict:
        from . import models as orm
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
        with self.session_factory() as db:
            # An actual no-op UPDATE serializes SQLite as well as PostgreSQL.
            locked = db.execute(update(orm.User).where(orm.User.id == user_id).values(
                analysis_input_revision=orm.User.analysis_input_revision))
            if not locked.rowcount:
                raise CoachError("Unknown user", 404)
            existing = db.execute(select(CoachRequest).where(
                CoachRequest.user_id == user_id, CoachRequest.idempotency_key == key)).scalar_one_or_none()
            if existing:
                if existing.request_hash != request_hash:
                    raise CoachError("Idempotency-Key conflicts with previous request", 409)
                self._reconcile(db, existing)
                return _projection(existing)
            rows = list(db.execute(select(CoachRequest).where(
                CoachRequest.user_id == user_id,
                CoachRequest.state.in_(["queued", "running"]))).scalars())
            for row in rows:
                self._reconcile(db, row)
            recent = db.scalar(select(func.count()).select_from(CoachRequest).where(
                CoachRequest.user_id == user_id,
                CoachRequest.created_at > now - timedelta(hours=1)))
            if recent >= policy.max_requests_per_hour:
                raise CoachError("Coach hourly root-request budget exhausted", 429)
            if sum(row.state in {"queued", "running"} for row in rows) >= policy.max_inflight:
                raise CoachError("Coach inflight root-request budget exhausted", 429)
            row = CoachRequest(id=uuid4().hex, user_id=user_id, idempotency_key=key,
                request_hash=request_hash, operation=request.operation, day=request.day,
                created_at=now, state="queued", reserved_units=1, policy_revision=policy.revision)
            db.add(row)
            db.flush()
            return _projection(row)

    @staticmethod
    def _reconcile(db, row):
        from . import models as orm
        if row.state not in {"queued", "running"}:
            return
        model = orm.SyncAttempt if row.operation == "refresh_workouts" else orm.AnalysisJob
        if row.job_id:
            root = db.get(model, row.job_id)
        else:
            key = "coach:" + row.id
            if row.operation == "refresh_workouts":
                root = db.scalar(select(model).where(
                    model.user_id == row.user_id, model.source == "zepp",
                    model.trigger == "manual",
                    model.trigger_ref == "api:" + hashlib.sha256(key.encode()).hexdigest()))
            else:
                root = db.scalar(select(model).where(
                    model.user_id == row.user_id, model.idempotency_key == key))
            if root is not None:
                row.job_id, row.error = root.id, None
        if root is None:
            return  # Unknown dispatch remains reserved, never silently refunded.
        if root.user_id != row.user_id:
            return
        if root.status in {"queued", "running", "retry_wait"}:
            row.state = "running"
        elif root.status in {"succeeded", "partial", "failed", "needs_reauth", "cancelled", "superseded"}:
            row.state = "succeeded" if root.status == "succeeded" else "failed"
        # Unknown root state also retains the reservation.

    @staticmethod
    def _raise_failed(reservation: dict) -> None:
        if reservation["error"] and reservation["state"] == "failed":
            status = {"source_auth_required": 409, "dispatch_conflict": 409,
                      "dispatch_invalid": 422}.get(reservation["error"], 503)
            raise CoachError("Coach dispatch rejected", status,
                             failure_code=reservation["error"])
        if reservation["state"] == "failed":
            raise CoachError("Coach root operation failed; inspect its job status", 409,
                             failure_code="operation_failed", next_action="check_resource")

    def dispatch(self, reservation: dict) -> dict:
        from vitalis.application.sync_jobs import SyncJobAuthRequired, SyncJobConflict, SyncJobInvalid

        self._raise_failed(reservation)
        if reservation["job_id"] or reservation["state"] not in {"queued", "running"}:
            return reservation
        context = {"origin": "conversation", "root_id": reservation["request_id"],
                   "notification_policy": "suppress", "policy_revision": reservation["policy_revision"]}
        key = "coach:" + reservation["request_id"]
        user_id = reservation["user_id"]
        day = date.fromisoformat(reservation["day"])
        try:
            if reservation["operation"] == "refresh_workouts":
                job_id = self.sync_service.create(SyncJobCommand(user_id=user_id, source="zepp",
                    idempotency_key=key, days=1, from_date=day, to_date=day,
                    workout_only=True, execution_context=context)).job_id
            else:
                from .repositories import HealthRepository
                with self.session_factory() as db:
                    # Serialize same-key analysis dispatch across processes, including SQLite.
                    db.execute(update(CoachRequest).where(CoachRequest.id == reservation["request_id"]).values(
                        reserved_units=CoachRequest.reserved_units))
                    linked = db.get(CoachRequest, reservation["request_id"])
                    if linked.job_id:
                        return _projection(linked)
                    # Same transaction stores queue acceptance and reservation linkage.
                    db.info["execution_context"] = context
                    job = HealthRepository(db).enqueue_analysis_job(user_id, day,
                        event_type="conversation_analysis", source="coach", idempotency_key=key)
                    if job is None:
                        raise CoachError("Unknown user", 404)
                    row = db.get(CoachRequest, reservation["request_id"])
                    row.job_id, row.error = job.id, None
                    db.flush()
                    return _projection(row)
            with self.session_factory() as db:
                row = db.get(CoachRequest, reservation["request_id"])
                row.job_id, row.error, row.state = job_id, None, "queued"
                db.flush()
                return _projection(row)
        except (SyncJobAuthRequired, SyncJobConflict, SyncJobInvalid, CoachError) as exc:
            code, status = (
                ("source_auth_required", 409) if isinstance(exc, SyncJobAuthRequired) else
                ("dispatch_conflict", 409) if isinstance(exc, SyncJobConflict) else
                ("dispatch_invalid", 422) if isinstance(exc, SyncJobInvalid) else
                ("dispatch_rejected", exc.status_code)
            )
            with self.session_factory() as db:
                row = db.get(CoachRequest, reservation["request_id"])
                # Another same-key caller may already have linked acceptance.
                if not row.job_id:
                    row.state, row.error = "failed", code
            raise CoachError("Coach dispatch rejected", status, failure_code=code) from exc
        except Exception as exc:
            # Unknown acceptance remains reserved; only the SAME stable key may retry.
            with self.session_factory() as db:
                row = db.get(CoachRequest, reservation["request_id"])
                if not row.job_id:
                    row.error = "dispatch_pending"
            raise CoachError("Dispatch uncertain; retry only the same Idempotency-Key", 503,
                             failure_code="dispatch_pending", retryable=True,
                             next_action="refresh_and_retry") from exc
