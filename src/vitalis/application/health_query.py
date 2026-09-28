"""Application use case for user-scoped raw health reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal

from vitalis.time import local_day

from .ports import HealthReader


MetricResolution = Literal["raw", "1h", "1d"]


class MetricQueryBudgetExceeded(ValueError):
    """The requested aggregate has more output groups than the read budget."""


@dataclass
class _MetricAggregate:
    total: float
    count: int
    minimum: float
    maximum: float

    def add(self, value: float) -> None:
        self.total += value
        self.count += 1
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)


class HealthQuery:
    """Compose detached persistence values into stable raw-health projections."""

    RAW_METRIC_LIMIT = 50_000
    MAX_AGGREGATE_GROUPS = 50_000

    def __init__(
        self,
        reader: HealthReader,
        *,
        timezone_name: str = "UTC",
        now_factory: Callable[[], datetime] | None = None,
        next_auto_sync_factory: Callable[[], str] | None = None,
    ) -> None:
        self._reader = reader
        self._timezone_name = timezone_name
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._next_auto_sync_factory = next_auto_sync_factory

    def data_status(self, user_id: str) -> dict:
        """Return source coverage and the latest bounded synchronization result."""
        rows = self._reader.sync_stream_states(user_id)
        latest = self._reader.latest_sync_attempt(user_id, source="zepp")
        chunks = (
            self._reader.sync_chunks(latest.attempt_id, user_id)
            if latest is not None
            else ()
        )

        chunk_summary: dict[str, dict[str, int]] = {}
        for row in chunks:
            stream = row.health_stream or row.stream
            summary = chunk_summary.setdefault(
                stream,
                {
                    "total": 0,
                    "succeeded": 0,
                    "unavailable": 0,
                    "failed": 0,
                    "retry_wait": 0,
                    "running": 0,
                    "queued": 0,
                    "records_written": 0,
                    "raw_records": 0,
                },
            )
            summary["total"] += 1
            if row.status in summary:
                summary[row.status] += 1
            summary["records_written"] += row.records_written
            summary["raw_records"] += row.raw_records

        latest_payload = None
        if latest is not None:
            counts = {
                key: 0
                for key in (
                    "succeeded",
                    "unavailable",
                    "failed",
                    "retry_wait",
                    "running",
                    "queued",
                    "cancelled",
                )
            }
            for row in chunks:
                counts[row.status] = counts.get(row.status, 0) + 1
            latest_payload = {
                "attempt_id": latest.attempt_id,
                "status": latest.status,
                "trigger": latest.trigger,
                "window": {
                    "start": _iso_utc(latest.window_start),
                    "end": _iso_utc(latest.window_end),
                },
                "window_start": _iso_utc(latest.window_start),
                "window_end": _iso_utc(latest.window_end),
                "deadline": _iso_utc(latest.deadline_at) if latest.deadline_at else None,
                "retry": {
                    "count": latest.retry_count,
                    "next_at": (
                        _iso_utc(latest.next_retry_at)
                        if latest.next_retry_at
                        else None
                    ),
                },
                "next_retry": (
                    _iso_utc(latest.next_retry_at)
                    if latest.next_retry_at
                    else None
                ),
                "progress": {
                    "total_chunks": len(chunks),
                    **counts,
                    "completed_count": counts["succeeded"] + counts["unavailable"],
                },
                "chunks_by_stream": chunk_summary,
            }

        return {
            "user_id": user_id,
            "latest_attempt": latest_payload,
            "streams": [
                {
                    "stream": row.stream,
                    "fetch": {
                        "status": row.fetch_status,
                        "at": _iso_utc(row.fetched_at) if row.fetched_at else None,
                    },
                    "parse": {
                        "status": row.parse_status,
                        "at": _iso_utc(row.parsed_at) if row.parsed_at else None,
                    },
                    "write": {
                        "status": row.write_status,
                        "at": _iso_utc(row.written_at) if row.written_at else None,
                    },
                    "last_sample_at": (
                        _iso_utc(row.last_sample_at) if row.last_sample_at else None
                    ),
                    "raw_records": row.raw_records,
                    "records_written": row.records_written,
                    "error_kind": row.error_kind,
                }
                for row in rows
            ],
        }

    def metric_series(
        self,
        user_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        resolution: MetricResolution = "1h",
    ) -> dict:
        """Return source-qualified points with an explicit completeness flag."""
        start = _as_utc(start)
        end = _as_utc(end)
        if resolution == "raw":
            rows = self._reader.metric_samples(
                user_id, metric, start, end, limit=self.RAW_METRIC_LIMIT + 1
            )
            return {
                "points": [
                    {
                        "timestamp": _iso_utc(row.timestamp),
                        "value": row.value,
                        "unit": row.unit,
                        "source": row.source,
                        "source_scope": row.source_scope,
                        "device_id": row.device_id,
                        **(
                            {
                                "sample_id": row.source_record_id,
                                "sample_ordinal": row.sample_ordinal,
                            }
                            if row.source_record_id
                            else {}
                        ),
                    }
                    for row in rows[:self.RAW_METRIC_LIMIT]
                ],
                "truncated": len(rows) > self.RAW_METRIC_LIMIT,
            }

        buckets: dict[tuple[str, str, str, str, str], _MetricAggregate] = {}
        with self._reader.metric_sample_rows(user_id, metric, start, end) as rows:
            for row in rows:
                if resolution == "1h":
                    timestamp = _iso_utc(
                        _as_utc(row.timestamp).replace(minute=0, second=0, microsecond=0)
                    )
                elif resolution == "1d":
                    timestamp = local_day(row.timestamp, self._timezone_name).isoformat()
                else:
                    raise ValueError(f"unsupported metric resolution: {resolution}")
                key = (
                    timestamp,
                    row.source,
                    row.source_scope,
                    row.device_id or "",
                    row.unit,
                )
                aggregate = buckets.get(key)
                if aggregate is None:
                    if len(buckets) >= self.MAX_AGGREGATE_GROUPS:
                        raise MetricQueryBudgetExceeded("聚合结果超过预算，请缩小查询窗口")
                    buckets[key] = _MetricAggregate(row.value, 1, row.value, row.value)
                else:
                    aggregate.add(row.value)

        return {
            "points": [
                {
                    "timestamp": key[0],
                    "value": round(aggregate.total / aggregate.count, 2),
                    "min": aggregate.minimum,
                    "max": aggregate.maximum,
                    "count": aggregate.count,
                    "unit": key[4],
                    "source": key[1],
                    "source_scope": key[2],
                    "device_id": key[3] or None,
                }
                for key, aggregate in sorted(buckets.items())
            ],
            "truncated": False,
        }

    def daily_metrics(
        self,
        user_id: str,
        start: date,
        end: date,
        metric: str | None = None,
    ) -> list[dict]:
        return [
            {
                "date": row.date.isoformat(),
                "metric": row.metric,
                "value": row.value,
                "unit": row.unit,
                "source": row.source,
                "source_scope": row.source_scope,
                "device_id": row.device_id,
            }
            for row in self._reader.daily_metrics(user_id, start, end, metric)
        ]

    def dense_files(
        self,
        user_id: str,
        stream: str,
        start: date,
        end: date,
        limit: int,
    ) -> dict:
        rows = self._reader.dense_files(user_id, stream, start, end, limit + 1)
        visible_rows = rows[:limit]
        return {
            "truncated": len(rows) > limit,
            "files": [
                {
                    "file_type": row.file_type,
                    "date": row.date.isoformat() if row.date else None,
                    "start": _iso_utc(row.start_utc) if row.start_utc else None,
                    "end": _iso_utc(row.end_utc) if row.end_utc else None,
                    "source_scope": row.source_scope,
                    "device_id": row.device_id,
                    "parse_status": row.parse_status,
                    "sample_count": row.sample_count,
                }
                for row in visible_rows
            ],
            "payload_decoded": any(row.parse_status == "decoded" for row in visible_rows),
        }

    def list_workouts(
        self, user_id: str, start: date, end: date, limit: int
    ) -> dict:
        return {
            "user_id": user_id,
            "workouts": [
                {
                    **row.data,
                    "source": row.source,
                    "workout_id": row.workout_id,
                    "detail_available": row.detail_synced,
                }
                for row in self._reader.workouts(user_id, start, end, limit)
            ],
        }

    def workout_detail(
        self, user_id: str, workout_id: str, source: str
    ) -> dict | None:
        result = self._reader.workout_detail(user_id, workout_id, source)
        if result is None:
            return None
        row, samples = result
        detail = dict(row.detail or {})
        if samples:
            detail["samples"] = [
                {
                    "timestamp": _iso_utc(sample.timestamp),
                    "metric": sample.metric,
                    "value": sample.value,
                    "unit": sample.unit,
                    "source_scope": sample.source_scope,
                    "device_id": sample.device_id,
                }
                for sample in samples
            ]
        return {
            "user_id": user_id,
            "source": row.source,
            "workout": {
                **row.data,
                "source": row.source,
                "workout_id": row.workout_id,
            },
            "detail_available": row.detail_synced,
            "detail": detail or None,
        }

    def token_status(self, user_id: str, source: str = "zepp") -> dict:
        metadata = self._reader.token_metadata(user_id, source)
        if metadata is None:
            return {
                "user_id": user_id,
                "authorized": False,
                "detail": "未导入凭据",
                "import_page": "/api/connect/zepp/scan",
            }

        now = _as_utc(self._now_factory())
        valid = (
            metadata.expires_at is None
            or _as_utc(metadata.expires_at) > now + timedelta(seconds=60)
        )
        detail = "凭据已保存，尚未执行在线验证" if valid else "凭据已过期，请重新连接"
        link = self._reader.latest_browser_link(user_id)
        needs_login = bool(link and link.status == "needs_login") or not valid
        return {
            "user_id": user_id,
            "authorized": True,
            "valid": valid,
            "vendor_user_id": metadata.vendor_user_id,
            "region_host": metadata.region_host,
            "detail": detail,
            "error_kind": None,
            "retryable": False,
            "connection_status": (
                link.status if link else ("needs_login" if needs_login else "connected")
            ),
            "needs_login": needs_login,
            "connection_message": link.message if link else detail,
            "last_verified_at": (
                _iso_utc(link.last_verified_at)
                if link and link.last_verified_at
                else None
            ),
            "last_sync_at": (
                _iso_utc(link.last_sync_at) if link and link.last_sync_at else None
            ),
            "next_auto_sync": (
                self._next_auto_sync_factory()
                if self._next_auto_sync_factory is not None
                else None
            ),
            "manual_sync": "POST /api/sync-jobs (JSON: {\"days\": 7}; Idempotency-Key required)",
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
