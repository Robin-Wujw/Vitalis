"""Read-only connection milestones and operational timing from durable evidence.

Coverage counts observed or ledger-verified dates; a requested window or a
successful empty fetch never becomes an observed measurement. This module only
reads repository queries and never starts synchronization or analysis.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo


INITIAL_SYNC_DAYS = 180
MAX_SYNC_DAYS = 730
CONNECTION_STATES = (
    "connected", "credential_verified", "backfill_requested", "backfill_progress",
    "bootstrap_analysis_queued", "first_report_ready", "daily_cadence",
)
_INITIAL_TRIGGERS = {"oauth_callback", "pairing_initial", "token_import"}
_HISTORY_LIMIT = 2048
_SAMPLE_LIMIT = 20_000


class ConnectionProgressRepository(Protocol):
    """Existing read queries used by the connection projection."""

    def source_account_status(self, user_id: str, source: str) -> dict | None: ...
    def sync_attempts(self, user_id: str, source: str, limit: int) -> list[Any]: ...
    def sync_chunks(self, attempt_id: str, user_id: str) -> list[Any]: ...
    def analysis_jobs_for_target(self, user_id: str, target: date) -> list[Any]: ...
    def latest_good_analysis_snapshot_for_target(self, user_id: str, profile_type: str, target_date: date) -> Any: ...
    def sleep_range(self, user_id: str, start: date, end: date) -> list[dict]: ...
    def activity_range(self, user_id: str, start: date, end: date) -> list[dict]: ...
    def metric_samples(self, user_id: str, metric: str, start: datetime, end: datetime, limit: int) -> list[Any]: ...
    def training_history_coverage(self, user_id: str, start: date, end: date, as_of: datetime, *, timezone_name: str) -> dict: ...
    def notification_deliveries(self, user_id: str, *, limit: int) -> list[Any]: ...


def utc(value: datetime) -> datetime:
    """Persisted naive timestamps are UTC, never the process local zone."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso_utc(value: datetime | None) -> str | None:
    return utc(value).isoformat().replace("+00:00", "Z") if value else None


def _elapsed(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    seconds = (utc(end) - utc(start)).total_seconds()
    return round(seconds, 6) if seconds >= 0 else None


def latency_metadata(
    *, created_at: datetime | None = None, started_at: datetime | None = None,
    finished_at: datetime | None = None, as_of: datetime,
) -> dict:
    """Safe timings without errors, input payloads, credentials, or inferred zeros.

    Compute latency measures elapsed execution (including any persisted retry
    wait), not CPU time. Missing timestamps and invalid clock ordering stay null.
    """
    return {
        "created_at": iso_utc(created_at), "started_at": iso_utc(started_at),
        "finished_at": iso_utc(finished_at),
        "queue_latency_seconds": _elapsed(created_at, started_at),
        "compute_latency_seconds": _elapsed(started_at, finished_at),
        "queue_age_seconds": _elapsed(created_at, as_of) if started_at is None and finished_at is None else None,
        "running_age_seconds": _elapsed(started_at, as_of) if finished_at is None else None,
        "total_latency_seconds": _elapsed(created_at, finished_at),
    }


def _date_coverage(days: set[date], start: date, end: date, *, verified: bool = False) -> dict:
    available = sorted(day for day in days if start <= day <= end)
    expected = (end - start).days + 1
    return {
        "period_start": start.isoformat(), "period_end": end.isoformat(),
        "calendar_semantics": "calendar_day", "expected_days": expected,
        "verified_days" if verified else "available_days": len(available),
        "dates": [day.isoformat() for day in available],
        "coverage": len(available) / expected,
        "status": "AVAILABLE" if len(available) == expected else "PARTIAL" if available else "UNKNOWN",
    }


def _observed_dates(rows: list[dict], fields: tuple[str, ...]) -> set[date]:
    output = set()
    for row in rows:
        if not any(row.get(key) is not None for key in fields):
            continue
        value = row.get("date")
        output.add(value if isinstance(value, date) else date.fromisoformat(str(value)))
    return output


def _window(attempt: Any, zone: ZoneInfo) -> tuple[date, date]:
    return (
        utc(attempt.window_start).astimezone(zone).date(),
        (utc(attempt.window_end) - timedelta(microseconds=1)).astimezone(zone).date(),
    )


def _backfill(repository: ConnectionProgressRepository, user_id: str, attempt: Any, as_of: datetime, zone: ZoneInfo) -> dict:
    start, end = _window(attempt, zone)
    chunks = repository.sync_chunks(attempt.id, user_id=user_id)
    states = Counter(chunk.status for chunk in chunks)
    streams = defaultdict(list)
    for chunk in chunks:
        streams[chunk.health_stream or chunk.stream].append(chunk)
    progress = []
    for stream, rows in sorted(streams.items()):
        counts = Counter(row.status for row in rows)
        progress.append({
            "stream": stream, "total_chunks": len(rows),
            "succeeded_chunks": counts["succeeded"], "unavailable_chunks": counts["unavailable"],
            "failed_chunks": counts["failed"], "cancelled_chunks": counts["cancelled"],
            "pending_chunks": sum(counts[state] for state in ("queued", "running", "retry_wait")),
            "records_written": sum(row.records_written or 0 for row in rows),
        })
    return {
        "attempt_id": attempt.id, "status": attempt.status,
        "requested_days": (end - start).days + 1, "target_days": INITIAL_SYNC_DAYS,
        "period_start": start.isoformat(), "period_end": end.isoformat(),
        "timezone": str(zone), "total_chunks": len(chunks),
        "completed_chunks": sum(states[state] for state in ("succeeded", "unavailable", "failed", "cancelled")),
        "succeeded_chunks": states["succeeded"],
        "streams": progress,
        "latency": latency_metadata(
            created_at=attempt.created_at, started_at=attempt.started_at,
            finished_at=attempt.finished_at, as_of=as_of,
        ),
    }


def connection_progress(
    repository: ConnectionProgressRepository, user_id: str, *, source: str,
    as_of: datetime, authorized: bool, needs_login: bool = False,
    verified_at: datetime | None = None,
) -> dict:
    """Project actual milestones using the first connection attempt and its day.

    A scheduled attempt after the first saved report is evidence of daily
    cadence; merely reading this projection cannot advance a milestone.
    """
    now = utc(as_of)
    account = repository.source_account_status(user_id, source)
    attempts = repository.sync_attempts(user_id, source=source, limit=_HISTORY_LIMIT)
    attempts = [row for row in attempts if account is None or row.source_account_id == account["id"]]
    initial = min(
        (row for row in attempts if row.trigger in _INITIAL_TRIGGERS),
        key=lambda row: (utc(row.created_at), row.id), default=None,
    )
    zone = ZoneInfo(initial.timezone if initial else "UTC")
    today = now.astimezone(zone).date()
    start, end = _window(initial, zone) if initial else (today - timedelta(days=INITIAL_SYNC_DAYS - 1), today)
    snapshot = repository.latest_good_analysis_snapshot_for_target(user_id, "daily", end) if initial else None
    if snapshot is not None and not (utc(initial.created_at) <= utc(snapshot.generated_at) <= now):
        snapshot = None
    jobs = repository.analysis_jobs_for_target(user_id, end) if initial else []
    jobs = [row for row in jobs if row.delivery_period is None and utc(row.updated_at) >= utc(initial.created_at)]
    matching = [row for row in jobs if initial.id in row.idempotency_key]
    bootstrap = max(matching or jobs, key=lambda row: (utc(row.updated_at), row.id), default=None)
    cadence = snapshot is not None and any(
        row.trigger in {"morning", "evening", "nightly"}
        and utc(row.created_at) >= utc(snapshot.generated_at) for row in attempts
    )
    reached = set()
    if authorized:
        reached.update(("connected", "credential_verified"))
    if initial is not None:
        reached.add("backfill_requested")
        if initial.started_at is not None or initial.status != "queued":
            reached.add("backfill_progress")
    if bootstrap is not None:
        reached.add("bootstrap_analysis_queued")
    if snapshot is not None:
        reached.add("first_report_ready")
    if cadence:
        reached.add("daily_cadence")
    state = next((item for item in reversed(CONNECTION_STATES) if item in reached), "disconnected")
    failure_code, retryable, next_action = None, False, "wait_for_worker"
    if not authorized or needs_login:
        state = "needs_login" if needs_login else "disconnected"
        failure_code = "credential_required"
        next_action = "reconnect_source"
    elif initial is not None and initial.status in {"failed", "cancelled", "needs_reauth"}:
        failure_code = f"backfill_{initial.status}"
        next_action = "reconnect_source" if initial.status == "needs_reauth" else "request_sync"
    elif bootstrap is not None and bootstrap.status == "failed" and snapshot is None:
        failure_code, next_action = "bootstrap_analysis_failed", "request_analysis"
    elif initial is not None and initial.status == "retry_wait" and snapshot is None:
        retryable = True
    elif snapshot is not None:
        next_action = "read_first_report"
    baseline_start, baseline_end = today - timedelta(days=28), today - timedelta(days=1)
    read_start = min(start, baseline_start)
    sleeps = _observed_dates(repository.sleep_range(user_id, read_start, max(end, today)), ("sleep_duration",))
    activities = _observed_dates(repository.activity_range(user_id, read_start, max(end, today)), ("steps", "distance_km", "calories", "active_minutes", "resting_hr"))
    history = repository.training_history_coverage(user_id, start, end, now, timezone_name=str(zone))
    verified_days = {date.fromisoformat(value) for value in history.get("verified_days", [])}
    coverage = {
        "sleep": {**_date_coverage(sleeps, start, end), "calendar_semantics": "sleep_day", "source": source},
        "activity": {**_date_coverage(activities, start, end), "calendar_semantics": "activity_day", "source": source},
        "training_history": {**_date_coverage(verified_days, start, end, verified=True),
                             "status": history.get("status", "UNKNOWN"),
                             "truncated": bool(history.get("truncated")), "source": source},
    }
    warmup = {}
    for key, days in (("sleep_28d", sleeps), ("activity_28d", activities)):
        values = _date_coverage(days, baseline_start, baseline_end)
        warmup[key] = {**values, "ready": values["available_days"] == 28, "qualification": "observed_days_only"}
    load_start = today - timedelta(days=42)
    load = repository.training_history_coverage(user_id, load_start, baseline_end, now, timezone_name=str(zone))
    load_days = {date.fromisoformat(value) for value in load.get("verified_days", [])}
    load_coverage = _date_coverage(load_days, load_start, baseline_end, verified=True)
    warmup["training_history_42d"] = {
        **load_coverage, "ready": load_coverage["verified_days"] == 42 and not load.get("truncated"),
        "qualification": "verified_history_only", "truncated": bool(load.get("truncated")),
    }
    sample_start = datetime.combine(baseline_start, datetime.min.time(), tzinfo=zone).astimezone(UTC)
    sample_end = datetime.combine(today, datetime.min.time(), tzinfo=zone).astimezone(UTC) - timedelta(microseconds=1)
    samples = repository.metric_samples(user_id, "hrv_rmssd", sample_start, sample_end, limit=_SAMPLE_LIMIT + 1)
    sample_days = {utc(row.timestamp).astimezone(zone).date() for row in samples[:_SAMPLE_LIMIT] if row.value > 0}
    hrv_coverage = _date_coverage(sample_days, baseline_start, baseline_end)
    warmup["hrv_rmssd_28d"] = {
        **hrv_coverage, "ready": hrv_coverage["available_days"] == 28 and len(samples) <= _SAMPLE_LIMIT,
        "qualification": "observed_days_only", "truncated": len(samples) > _SAMPLE_LIMIT,
    }
    deliveries = repository.notification_deliveries(user_id, limit=20)
    return {
        "user_id": user_id, "source": source, "state": state, "as_of": iso_utc(now),
        "failure_code": failure_code, "retryable": retryable, "next_action": next_action,
        "history_truncated": len(attempts) >= _HISTORY_LIMIT,
        "milestones": [{"state": item, "reached": item in reached} for item in CONNECTION_STATES],
        "credential_verified_at": iso_utc(verified_at or (initial.created_at if initial else None)),
        "backfill": _backfill(repository, user_id, initial, now, zone) if initial else None,
        "coverage": coverage, "warmup": warmup,
        "bootstrap_analysis": {
            "job_id": bootstrap.id, "status": bootstrap.status,
            "target_date": bootstrap.target_date.isoformat(),
            "latency": latency_metadata(created_at=bootstrap.created_at, started_at=bootstrap.started_at,
                                        finished_at=bootstrap.finished_at, as_of=now),
        } if bootstrap else None,
        "first_report": {
            "ready": snapshot is not None,
            "analysis_run_id": snapshot.analysis_run_id if snapshot else None,
            "date": end.isoformat() if initial else None,
            "generated_at": iso_utc(snapshot.generated_at) if snapshot else None,
            "as_of": (snapshot.payload.get("report_context") or {}).get("as_of") if snapshot else None,
            "report_url": f"/api/reports/daily?day={end.isoformat()}" if snapshot else None,
        },
        "daily_cadence": {"active": cadence, "timezone": str(zone)},
        "delivery_latency": [{
            "delivery_id": row.id, "period": row.period, "date": row.target_date.isoformat(),
            "status": row.status,
            "delivery_latency_seconds": _elapsed(row.created_at, row.updated_at) if row.status in {"accepted", "delivered"} else None,
            "measurement": "intent_to_current_provider_state",
            "as_of": iso_utc(row.updated_at),
        } for row in deliveries],
    }
