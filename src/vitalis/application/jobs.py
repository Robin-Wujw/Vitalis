"""Durable, user-scoped analysis requests executed by the worker."""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import logging
from threading import Event, Thread

from vitalis.application.ports import (
    AnalysisJobRepository, AnalysisJobState, AnalysisRunner, IdempotencyConflict, JobClaim,
)


log = logging.getLogger("vitalis.analysis_jobs")
DEFAULT_LEASE_SECONDS = 3600
MAX_JOBS_PER_PASS = 4
MAX_ANALYSIS_ATTEMPTS = 3
_SAFE_FAILURE = "Analysis failed"

_repository: AnalysisJobRepository | None = None
_runner: AnalysisRunner | None = None


def configure_analysis_jobs(repository: AnalysisJobRepository, runner: AnalysisRunner) -> None:
    """Bind the process's storage and analysis engine at the composition root."""
    global _repository, _runner
    _repository = repository
    _runner = runner


def _store() -> AnalysisJobRepository:
    if _repository is None:
        raise RuntimeError("analysis jobs have not been configured")
    return _repository


def create_analysis_job(
    user_id: str,
    day: date,
    idempotency_key: str,
    *,
    delivery_period: str | None = None,
) -> str:
    """Enqueue once per user/key; scheduled jobs may carry one delivery period."""
    if not isinstance(user_id, str) or not user_id or len(user_id) > 64:
        raise ValueError("invalid user id")
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("day must be a date")
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 128
    ):
        raise ValueError("invalid idempotency key")

    if delivery_period not in (None, "morning", "evening", "weekly", "monthly"):
        raise ValueError("delivery_period must be morning, evening, weekly, or monthly")
    request_hash = sha256(
        f"analyze:v2:{day.isoformat()}:{delivery_period or '-'}".encode("ascii")
    ).hexdigest()
    return _store().enqueue(
        user_id, day, idempotency_key, request_hash, delivery_period=delivery_period,
    )


def get_analysis_job(user_id: str, job_id: str) -> AnalysisJobState | None:
    """Return a detached job snapshot only when it belongs to the caller."""
    return _store().get(user_id, job_id)


def _keep_lease(
    repository: AnalysisJobRepository, claim: JobClaim, lease_seconds: int, stopped: Event,
) -> None:
    while not stopped.wait(lease_seconds / 3):
        try:
            if not repository.renew(claim, lease_seconds):
                return
        except Exception:
            # A missed renewal cannot authorize a stale completion. A later
            # renewal may still succeed before the lease expires.
            log.warning("analysis job lease renewal failed: job=%s", claim.id)


def _requeue_input_changed(repository: AnalysisJobRepository, claim: JobClaim) -> bool:
    """Ask persistence to retry only an explicitly classified input race."""
    try:
        return repository.requeue_input_changed(claim, max_attempts=MAX_ANALYSIS_ATTEMPTS)
    except Exception:
        # A retry decision must never turn an unrelated storage failure into a retry.
        log.warning("analysis job input-race retry failed: job=%s", claim.id)
        return False


def drain_analysis_jobs(*, max_jobs: int = 1, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> int:
    """Process a bounded batch of leased jobs.

    The runner commits job success with its analysis outputs; failures before
    that commit are finished separately, subject to the current lease.
    """
    if not isinstance(max_jobs, int) or max_jobs < 1:
        raise ValueError("max_jobs must be positive")
    if not isinstance(lease_seconds, int) or lease_seconds < 2:
        raise ValueError("lease_seconds must be at least 2")

    repository = _store()
    runner = _runner
    if runner is None:
        raise RuntimeError("analysis jobs have not been configured")
    drained = 0
    for _ in range(min(max_jobs, MAX_JOBS_PER_PASS)):
        claim = repository.claim(lease_seconds)
        if claim is None:
            break
        drained += 1
        stopped = Event()
        renewer = Thread(
            target=_keep_lease, args=(repository, claim, lease_seconds, stopped), daemon=True
        )
        renewer.start()
        error = None
        failed = False
        try:
            runner(claim)
        except Exception:
            failed = True
        finally:
            stopped.set()
            renewer.join()
        retry = failed and _requeue_input_changed(repository, claim)
        if failed and not retry:
            error = _SAFE_FAILURE
            log.warning("analysis job execution failed: job=%s", claim.id)
        if retry:
            continue
        if error is not None and not repository.finish(claim, error=error):
            log.warning("analysis job lease lost before failure: job=%s", claim.id)
    return drained
