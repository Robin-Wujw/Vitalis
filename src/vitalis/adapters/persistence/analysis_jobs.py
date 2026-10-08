"""SQL-backed durable analysis requests with atomic leases and fencing."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from vitalis.application.jobs import MAX_ANALYSIS_ATTEMPTS
from vitalis.application.ports import AnalysisJobState, IdempotencyConflict, JobClaim
from vitalis.adapters.persistence.models import AnalysisJob, AnalysisRun, User


_INPUT_CHANGED_ERROR = "analysis_input_changed"


def _now() -> datetime:
    # Storage DateTime columns, including AnalysisRun, use naive UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _state(row: AnalysisJob) -> AnalysisJobState:
    return AnalysisJobState(
        id=row.id,
        user_id=row.user_id,
        target_date=row.target_date,
        status=row.status,
        run_id=row.run_id,
        error=row.error,
        attempt_count=row.attempt_count,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        delivery_period=row.delivery_period,
    )


class SqlAnalysisJobRepository:
    """Database uniqueness and compare-and-swap updates arbitrate concurrent workers."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def enqueue(
        self, user_id: str, day: date, key: str, request_hash: str,
        delivery_period: str | None = None,
    ) -> str:
        if delivery_period not in (None, "morning", "evening", "weekly", "monthly"):
            raise ValueError("invalid delivery period")
        job_id = uuid4().hex
        try:
            with self._sessions.begin() as db:
                db.add(AnalysisJob(
                    id=job_id,
                    user_id=user_id,
                    target_date=day,
                    status="queued",
                    idempotency_key=key,
                    request_hash=request_hash,
                    delivery_period=delivery_period,
                ))
                db.flush()
                if db.get(User, user_id) is None:
                    raise ValueError("user not found")
            return job_id
        except IntegrityError:
            # The unique constraint arbitrates concurrent requests, not a prior read.
            with self._sessions() as db:
                existing = db.execute(select(AnalysisJob).where(
                    AnalysisJob.user_id == user_id,
                    AnalysisJob.idempotency_key == key,
                )).scalar_one_or_none()
                if existing is None:
                    raise
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict("idempotency key belongs to another request") from None
                if existing.delivery_period != delivery_period:
                    raise IdempotencyConflict("idempotency key belongs to another delivery period") from None
                return existing.id

    def get(self, user_id: str, job_id: str) -> AnalysisJobState | None:
        with self._sessions() as db:
            row = db.execute(select(AnalysisJob).where(
                AnalysisJob.id == job_id,
                AnalysisJob.user_id == user_id,
            )).scalar_one_or_none()
            return _state(row) if row is not None else None

    def claim(self, lease_seconds: int) -> JobClaim | None:
        now = _now()
        due = or_(
            AnalysisJob.status == "queued",
            (AnalysisJob.status == "running") & (AnalysisJob.lease_expires_at <= now),
        )
        claimable = due & (AnalysisJob.attempt_count < MAX_ANALYSIS_ATTEMPTS)
        with self._sessions() as db:
            exhausted_candidates = db.execute(
                select(AnalysisJob.id).where(
                    AnalysisJob.status == "running",
                    AnalysisJob.lease_expires_at <= now,
                    AnalysisJob.attempt_count >= MAX_ANALYSIS_ATTEMPTS,
                ).order_by(AnalysisJob.created_at, AnalysisJob.id).limit(16)
            ).scalars().all()
            candidates = db.execute(
                select(AnalysisJob.id).where(claimable)
                .order_by(AnalysisJob.created_at, AnalysisJob.id).limit(16)
            ).scalars().all()
        candidate_ids = tuple(dict.fromkeys((*exhausted_candidates, *candidates)))
        for job_id in candidate_ids:
            token = uuid4().hex
            with self._sessions.begin() as db:
                current = db.get(AnalysisJob, job_id)
                previous_run_id = current.run_id if current is not None else None
                if (
                    current is not None
                    and current.status == "running"
                    and current.attempt_count >= MAX_ANALYSIS_ATTEMPTS
                    and current.lease_expires_at is not None
                    and current.lease_expires_at <= now
                ):
                    # Do not let an expired third attempt become a fourth execution.
                    exhausted = db.execute(
                        update(AnalysisJob).where(
                            AnalysisJob.id == job_id,
                            AnalysisJob.user_id == current.user_id,
                            AnalysisJob.status == "running",
                            AnalysisJob.lease_token == current.lease_token,
                            AnalysisJob.lease_epoch == current.lease_epoch,
                            AnalysisJob.lease_expires_at == current.lease_expires_at,
                            AnalysisJob.lease_expires_at <= now,
                            AnalysisJob.attempt_count == current.attempt_count,
                            AnalysisJob.run_id == previous_run_id,
                        ).values(
                            status="failed",
                            run_id=None,
                            error="Analysis failed",
                            lease_token=None,
                            lease_expires_at=None,
                            finished_at=now,
                            updated_at=now,
                        )
                    ).rowcount
                    if exhausted and previous_run_id:
                        db.execute(
                            update(AnalysisRun).where(
                                AnalysisRun.id == previous_run_id,
                                AnalysisRun.status == "RUNNING",
                            ).values(
                                status="FAILED",
                                completed_at=now,
                                error="analysis_attempt_limit",
                            )
                        )
                    continue
                changed = db.execute(
                    update(AnalysisJob).where(
                        AnalysisJob.id == job_id, claimable,
                    ).values(
                        status="running",
                        lease_token=token,
                        lease_epoch=AnalysisJob.lease_epoch + 1,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        attempt_count=AnalysisJob.attempt_count + 1,
                        run_id=None,
                        started_at=now,
                        finished_at=None,
                        error=None,
                        updated_at=now,
                    )
                ).rowcount
                if changed:
                    # A worker can die after publishing a RUNNING run but before
                    # the final transaction. Reclaim only this job's prior run.
                    if previous_run_id:
                        db.execute(
                            update(AnalysisRun).where(
                                AnalysisRun.id == previous_run_id,
                                AnalysisRun.status == "RUNNING",
                            ).values(
                                status="FAILED",
                                completed_at=now,
                                error="analysis_lease_reclaimed",
                            )
                        )
                    row = db.get(AnalysisJob, job_id)
                    return JobClaim(
                        row.id, row.user_id, row.target_date, token, row.lease_epoch,
                        row.delivery_period,
                    )
        return None

    def renew(self, claim: JobClaim, lease_seconds: int) -> bool:
        now = _now()
        with self._sessions.begin() as db:
            return bool(db.execute(
                update(AnalysisJob).where(
                    AnalysisJob.id == claim.id,
                    AnalysisJob.status == "running",
                    AnalysisJob.lease_token == claim.token,
                    AnalysisJob.lease_epoch == claim.epoch,
                    AnalysisJob.lease_expires_at > now,
                ).values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
            ).rowcount)

    def attach_run(self, transaction: object, claim: JobClaim, run_id: str) -> bool:
        """Bind a RUNNING analysis run before work leaves the claim transaction."""
        if not isinstance(transaction, Session):
            raise TypeError("analysis transaction must be a SQLAlchemy Session")
        return bool(transaction.execute(
            update(AnalysisJob).where(
                AnalysisJob.id == claim.id,
                AnalysisJob.user_id == claim.user_id,
                AnalysisJob.target_date == claim.target_date,
                AnalysisJob.status == "running",
                AnalysisJob.lease_token == claim.token,
                AnalysisJob.lease_epoch == claim.epoch,
                AnalysisJob.lease_expires_at > _now(),
                AnalysisJob.run_id.is_(None),
            ).values(run_id=run_id)
        ).rowcount)

    def succeed(self, transaction: object, claim: JobClaim, run_id: str) -> bool:
        """Fence success inside the caller's analysis/snapshot transaction."""
        if not isinstance(transaction, Session):
            raise TypeError("analysis transaction must be a SQLAlchemy Session")
        db = transaction
        now = _now()
        return bool(db.execute(
            update(AnalysisJob).where(
                AnalysisJob.id == claim.id,
                AnalysisJob.user_id == claim.user_id,
                AnalysisJob.target_date == claim.target_date,
                AnalysisJob.status == "running",
                AnalysisJob.lease_token == claim.token,
                AnalysisJob.lease_epoch == claim.epoch,
                AnalysisJob.lease_expires_at > now,
                exists(select(User.id).where(User.id == claim.user_id)),
            ).values(
                status="succeeded",
                run_id=run_id,
                error=None,
                lease_token=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
        ).rowcount)

    def finish(self, claim: JobClaim, *, error: str) -> bool:
        """Fence a failure that occurred before the final analysis commit."""
        now = _now()
        with self._sessions.begin() as db:
            changed = db.execute(
                update(AnalysisJob).where(
                    AnalysisJob.id == claim.id,
                    AnalysisJob.status == "running",
                    AnalysisJob.lease_token == claim.token,
                    AnalysisJob.lease_epoch == claim.epoch,
                    AnalysisJob.lease_expires_at > now,
                ).values(
                    status="failed",
                    run_id=None,
                    error=error,
                    lease_token=None,
                    lease_expires_at=None,
                    finished_at=now,
                    updated_at=now,
                )
            ).rowcount
        return bool(changed)

    def requeue_input_changed(
        self, claim: JobClaim, *, max_attempts: int = MAX_ANALYSIS_ATTEMPTS,
    ) -> bool:
        """Requeue only a fenced job whose persisted run has the stable race error."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        now = _now()
        with self._sessions.begin() as db:
            changed = db.execute(
                update(AnalysisJob).where(
                    AnalysisJob.id == claim.id,
                    AnalysisJob.user_id == claim.user_id,
                    AnalysisJob.target_date == claim.target_date,
                    AnalysisJob.status == "running",
                    AnalysisJob.lease_token == claim.token,
                    AnalysisJob.lease_epoch == claim.epoch,
                    AnalysisJob.lease_expires_at > now,
                    AnalysisJob.attempt_count < max_attempts,
                    AnalysisJob.run_id.is_not(None),
                    exists(select(User.id).where(User.id == claim.user_id)),
                    exists(select(AnalysisRun.id).where(
                        AnalysisRun.id == AnalysisJob.run_id,
                        AnalysisRun.user_id == claim.user_id,
                        AnalysisRun.target_date == claim.target_date,
                        AnalysisRun.status == "FAILED",
                        AnalysisRun.error == _INPUT_CHANGED_ERROR,
                    )),
                ).values(
                    status="queued",
                    run_id=None,
                    error=None,
                    lease_token=None,
                    lease_expires_at=None,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            ).rowcount
        return bool(changed)
