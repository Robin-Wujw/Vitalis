"""Raw health queries and synchronization endpoints."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from vitalis.api.deps import require_user_id
from vitalis.connectors import get_connector
from vitalis.connectors.zepp import ZeppAuthError, ZeppConnector
from vitalis.config import settings
from vitalis.models import User
from vitalis.services.aggregation_service import AggregationService, Granularity
from vitalis.storage import HealthRepository, session_scope
from vitalis.time import local_day

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/data-health")
def health_data_health(user_id: str = Depends(require_user_id)) -> dict:
    """Explain the latest Zepp fetch and durable attempt progress."""
    with session_scope() as db:
        repo = HealthRepository(db)
        rows = repo.sync_stream_states(user_id)
        attempts = repo.sync_attempts(user_id, source="zepp", limit=1)
        latest = attempts[0] if attempts else None
        chunk_rows = repo.sync_chunks(latest.id, user_id=user_id) if latest else []
    chunk_summary: dict[str, dict] = {}
    for row in chunk_rows:
        stream = row.health_stream or row.stream
        summary = chunk_summary.setdefault(stream, {"total": 0, "succeeded": 0, "unavailable": 0, "failed": 0, "retry_wait": 0, "running": 0, "queued": 0, "records_written": 0, "raw_records": 0})
        summary["total"] += 1
        if row.status in summary:
            summary[row.status] += 1
        summary["records_written"] += row.records_written or 0
        summary["raw_records"] += row.raw_records or 0
    latest_payload = None
    if latest is not None:
        counts = {key: 0 for key in ("succeeded", "unavailable", "failed", "retry_wait", "running", "queued", "cancelled")}
        for row in chunk_rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        latest_payload = {
            "attempt_id": latest.id,
            "status": latest.status,
            "trigger": latest.trigger,
            "window": {"start": _iso_utc(latest.window_start), "end": _iso_utc(latest.window_end)},
            "window_start": _iso_utc(latest.window_start),
            "window_end": _iso_utc(latest.window_end),
            "deadline": _iso_utc(latest.deadline_at) if latest.deadline_at else None,
            "retry": {"count": latest.retry_count, "next_at": _iso_utc(latest.next_retry_at) if latest.next_retry_at else None},
            "next_retry": _iso_utc(latest.next_retry_at) if latest.next_retry_at else None,
            "progress": {"total_chunks": len(chunk_rows), **counts, "completed_count": counts["succeeded"] + counts["unavailable"]},
            "chunks_by_stream": chunk_summary,
        }
    return {
        "user_id": user_id,
        "latest_attempt": latest_payload,
        "streams": [
            {
                "stream": row.stream,
                "fetch": {"status": row.fetch_status, "at": _iso_utc(row.fetched_at) if row.fetched_at else None},
                "parse": {"status": row.parse_status, "at": _iso_utc(row.parsed_at) if row.parsed_at else None},
                "write": {"status": row.write_status, "at": _iso_utc(row.written_at) if row.written_at else None},
                "last_sample_at": _iso_utc(row.last_sample_at) if row.last_sample_at else None,
                "raw_records": row.raw_records,
                "records_written": row.records_written,
                "error_kind": row.error_kind,
            }
            for row in rows
        ],
    }


@router.post("/sync")
def health_sync(
    days: int = Query(7, ge=1, le=730, description="同步天数"),
    decode_dense_files: bool = Query(False, description="显式解码最多一个秒级心率归档"),
    user_id: str = Depends(require_user_id),
) -> dict:
    """Check the token in a short session, then run the coordinator outside it."""
    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    try:
        with session_scope() as db:
            auth = connector.load_token(HealthRepository(db), user_id)
        if auth is None and not getattr(connector, "mock", False):
            return {"user_id": user_id, "status": "token_required", "detail": "尚未导入 Zepp 凭据，请先访问 /connect/zepp/scan 导入"}
        if getattr(connector, "mock", False):
            with session_scope() as db:
                report = connector.sync_with_report(
                    User(id=user_id), days=days, repo=HealthRepository(db),
                    decode_dense_files=decode_dense_files,
                    max_chunks=max(1, settings.sync_dispatcher_batch_chunks),
                    trigger="manual",
                )
        else:
            report = connector.sync_with_report(
                User(id=user_id), days=days,
                decode_dense_files=decode_dense_files,
                max_chunks=max(1, settings.sync_dispatcher_batch_chunks),
                trigger="manual",
            )
        progress = report.progress or {}
        attempt_status = progress.get("status")
        status_map = {
            "succeeded": "synced", "retry_wait": "transient_error",
            "needs_reauth": "needs_reauth", "partial": "incomplete",
            "failed": "failed", "cancelled": "cancelled", "queued": "queued",
        }
        response_status = status_map.get(attempt_status, "synced" if report.success else "incomplete")
        if report.needs_reauth:
            response_status = "needs_reauth"
        return {
            "user_id": user_id,
            "status": response_status,
            "success": report.success,
            "attempt_id": progress.get("attempt_id"),
            "attempt_status": attempt_status,
            "progress": progress,
            "next_retry": progress.get("next_retry_at"),
            "retryable": response_status == "transient_error",
            "streams": [
                {"stream": s.stream, "status": s.status, "records_written": s.records_written,
                 "fetch_status": s.fetch_status, "parse_status": s.parse_status,
                 "write_status": s.write_status, "needs_reauth": s.needs_reauth,
                 "error_kind": s.error_kind, "message": s.message}
                for s in report.streams
            ],
            "records_written": report.records_written,
            "message": report.message,
        }
    except ZeppAuthError as exc:
        with session_scope() as db:
            HealthRepository(db).save_sync_stream_state(
                user_id, "sync", fetch_status="failed", parse_status="not_run", write_status="not_run",
                fetched_at=datetime.now(timezone.utc), parsed_at=None, written_at=None,
                raw_records=0, records_written=0, error_kind=exc.kind, message=str(exc),
            )
        if exc.needs_reauth:
            with session_scope() as db:
                HealthRepository(db).mark_user_browser_links_reauth(user_id, "Zepp 登录已失效，请重新登录")
            return {"user_id": user_id, "status": "needs_reauth", "retryable": False, "error_kind": "auth", "detail": str(exc)}
        if exc.kind in {"network", "service", "timeout"}:
            return {"user_id": user_id, "status": "transient_error", "retryable": True, "error_kind": exc.kind, "detail": str(exc)}
        return {"user_id": user_id, "status": "failed", "retryable": False, "error_kind": exc.kind, "detail": str(exc)}
    except Exception as exc:
        return {"user_id": user_id, "status": "failed", "retryable": False, "detail": str(exc)}


@router.get("/sync/{attempt_id}")
def health_sync_status(attempt_id: str, user_id: str = Depends(require_user_id)) -> dict:
    from vitalis.services.zepp_sync_coordinator import ZeppSyncCoordinator
    connector = get_connector("zepp")
    state = ZeppSyncCoordinator(connector=connector).public_status(attempt_id, user_id=user_id)
    if state is None:
        raise HTTPException(status_code=404, detail="同步尝试不存在")
    state["user_id"] = user_id
    return state


@router.post("/sync/{attempt_id}/cancel")
def health_sync_cancel(attempt_id: str, user_id: str = Depends(require_user_id)) -> dict:
    from vitalis.services.zepp_sync_coordinator import ZeppSyncCoordinator
    coordinator = ZeppSyncCoordinator(connector=get_connector("zepp"))
    state = coordinator.public_status(attempt_id, user_id=user_id)
    if state is None:
        raise HTTPException(status_code=404, detail="同步尝试不存在")
    changed = coordinator.request_cancel(attempt_id)
    return {"user_id": user_id, "attempt_id": attempt_id, "cancel_requested": changed, "attempt_status": "cancel_requested" if changed else state["attempt"]["status"]}


@router.get("/token-status")
def health_token_status(user_id: str = Depends(require_user_id)) -> dict:
    """查询当前用户的 Zepp 凭据状态（是否有效、过期提醒）。"""
    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    with session_scope() as db:
        repo = HealthRepository(db)
        auth = connector.load_token(repo, user_id)
    if auth is None:
        return {
            "user_id": user_id,
            "authorized": False,
            "detail": "未导入凭据",
            "import_page": "/api/v1/connect/zepp/scan",
        }
    # 尝试验证 token 有效性；只有明确认证失败才使浏览器链接失效。
    retryable = False
    error_kind: str | None = None
    try:
        connector.authenticate()
        if hasattr(connector, "_client_for"):
            with session_scope() as db:
                client = connector._client_for(HealthRepository(db), User(id=user_id))
        else:
            from vitalis.connectors.zepp.client import ZeppAPIClient
            client = ZeppAPIClient(
                app_token=getattr(auth, "access_token", ""),
                user_id=getattr(auth, "source_user_id", None) or "me",
                region_host=getattr(auth, "region_host", "") or "api-mifitcn.zepp.com",
            )
        client.verify()
        with session_scope() as db:
            link = HealthRepository(db).latest_browser_link(user_id)
            if link and link.status != "needs_login":
                HealthRepository(db).mark_browser_link_verified(link.token_digest)
        valid = True
        detail = "凭据有效"
    except ZeppAuthError as exc:
        valid = False
        detail = str(exc)
        error_kind = exc.kind
        retryable = exc.kind in {"network", "service", "timeout"}
        if exc.needs_reauth:
            with session_scope() as db:
                HealthRepository(db).mark_user_browser_links_reauth(
                    user_id, "Zepp 连接验证失败，请重新登录"
                )
    except Exception as exc:
        valid = False
        detail = str(exc)
        error_kind = "unknown"

    with session_scope() as db:
        link = HealthRepository(db).latest_browser_link(user_id)
    needs_login = bool(link and link.status == "needs_login") or error_kind == "auth"

    return {
        "user_id": user_id,
        "authorized": True,
        "valid": valid,
        "vendor_user_id": auth.source_user_id,
        "region_host": auth.region_host,
        "detail": detail,
        "error_kind": error_kind,
        "retryable": retryable,
        "connection_status": link.status if link else ("needs_login" if needs_login else "connected"),
        "needs_login": needs_login,
        "connection_message": link.message if link else detail,
        "last_verified_at": (
            link.last_verified_at.isoformat() + "Z" if link and link.last_verified_at else None
        ),
        "last_sync_at": link.last_sync_at.isoformat() + "Z" if link and link.last_sync_at else None,
        "next_auto_sync": _next_auto_sync(),
        "manual_sync": f"POST /api/v1/health/sync?days=7 (X-User-Id: {user_id})",
    }


@router.get("/range")
def health_range(
    user_id: str = Depends(require_user_id),
    from_date: date = Query(..., alias="from", description="起始日期"),
    to_date: date = Query(..., alias="to", description="结束日期"),
    granularity: Granularity = Query("1d", description="聚合粒度: 180d / 90d / 30d / 7d / 1d"),
) -> dict:
    """获取指定时间范围的多级聚合健康数据。

    支持从半年(180d)到单日(1d)的下钻粒度：
    - 180d: 半年维度（适合看 2 年长期趋势）
    - 90d:  季度维度
    - 30d:  月度维度
    - 7d:   周维度
    - 1d:   日维度（默认）
    """
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    max_span = 730  # 2 年上限
    if (to_date - from_date).days > max_span:
        return {
            "user_id": user_id,
            "error": f"查询跨度不能超过 {max_span} 天（2年）",
        }
    service = AggregationService()
    blocks = service.range_summary(user_id, from_date, to_date, granularity)
    return {
        "user_id": user_id,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "granularity": granularity,
        "blocks": [
            {
                "start": b.start.isoformat(),
                "end": b.end.isoformat(),
                "days_with_data": b.days_with_data,
                "days_total": b.days_total,
                "sleep": {
                    "duration_avg": b.sleep_duration_avg,
                    "deep_avg": b.deep_sleep_avg,
                    "rem_avg": b.rem_sleep_avg,
                    "light_avg": b.light_sleep_avg,
                    "awake_avg": b.awake_avg,
                    "score_avg": b.sleep_score_avg,
                },
                "activity": {
                    "steps_avg": b.steps_avg,
                    "calories_total": b.calories_total,
                    "distance_km_total": b.distance_km_total,
                    "resting_hr_avg": b.resting_hr_avg,
                },
                "training": {
                    "workout_count": b.workout_count_total,
                    "duration_total": b.training_duration_total,
                    "load_total": b.training_load_total,
                },
            }
            for b in blocks
        ],
    }


@router.get("/metrics/{metric}")
def health_metric_series(
    metric: str,
    from_time: datetime | None = Query(None, alias="from"),
    to_time: datetime | None = Query(None, alias="to"),
    resolution: Literal["raw", "1h", "1d"] = Query("1h"),
    user_id: str = Depends(require_user_id),
) -> dict:
    """Query timestamped measurements with optional hourly/daily aggregation."""
    end = to_time or datetime.now(timezone.utc)
    start = from_time or end - timedelta(days=7)
    if start > end:
        start, end = end, start
    if end - start > timedelta(days=730):
        raise HTTPException(status_code=400, detail="查询跨度不能超过 730 天")
    with session_scope() as db:
        repo = HealthRepository(db)
        if resolution == "raw":
            rows = repo.metric_samples(user_id, metric, start, end)
            points = [
                {
                    "timestamp": _iso_utc(row.timestamp),
                    "value": row.value,
                    "unit": row.unit,
                    "source": row.source,
                    "source_scope": row.source_scope,
                    "device_id": row.device_id or None,
                }
                for row in rows
            ]
        else:
            buckets: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
            for row in repo.metric_sample_rows(user_id, metric, start, end):
                if resolution == "1h":
                    timestamp = (
                        row.timestamp.replace(
                            minute=0, second=0, microsecond=0
                        ).isoformat() + "Z"
                    )
                else:
                    timestamp = local_day(row.timestamp).isoformat()
                key = (
                    timestamp,
                    row.source,
                    row.source_scope,
                    row.device_id or "",
                    row.unit,
                )
                buckets[key].append(row.value)
            points = [
                {
                    "timestamp": key[0],
                    "value": round(sum(values) / len(values), 2),
                    "min": min(values),
                    "max": max(values),
                    "count": len(values),
                    "unit": key[4],
                    "source": key[1],
                    "source_scope": key[2],
                    "device_id": key[3] or None,
                }
                for key, values in sorted(buckets.items())
            ]
    return {
        "user_id": user_id,
        "metric": metric,
        "resolution": resolution,
        "from": _iso_utc(start),
        "to": _iso_utc(end),
        "points": points,
    }


@router.get("/daily-metrics")
def health_daily_metrics(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    metric: str | None = Query(None),
    user_id: str = Depends(require_user_id),
) -> dict:
    """Query sparse vendor daily metrics without mixing them with computed scores."""
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    if (to_date - from_date).days > 730:
        raise HTTPException(status_code=400, detail="查询跨度不能超过 730 天")
    with session_scope() as db:
        rows = HealthRepository(db).daily_metrics(user_id, from_date, to_date, metric)
    return {
        "user_id": user_id,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "metrics": [
            {
                "date": row.date.isoformat(),
                "metric": row.metric,
                "value": row.value,
                "unit": row.unit,
                "source": row.source,
                "source_scope": row.source_scope,
                "device_id": row.device_id or None,
            }
            for row in rows
        ],
    }


@router.get("/dense-files/{stream}")
def health_dense_file_coverage(
    stream: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    limit: int = Query(5000, ge=1, le=20_000),
    user_id: str = Depends(require_user_id),
) -> dict:
    """Return dense-file coverage and decode status without exposing file IDs."""
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    if (to_date - from_date).days > 730:
        raise HTTPException(status_code=400, detail="查询跨度不能超过 730 天")
    with session_scope() as db:
        rows = HealthRepository(db).dense_data_files(
            user_id, stream, from_date, to_date, limit
        )
    return {
        "user_id": user_id,
        "stream": stream,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "files": [
            {
                "file_type": row.file_type,
                "date": row.date.isoformat() if row.date else None,
                "start": _iso_utc(row.start_utc) if row.start_utc else None,
                "end": _iso_utc(row.end_utc) if row.end_utc else None,
                "source_scope": row.source_scope,
                "device_id": row.device_id or None,
                "parse_status": row.parse_status,
                "sample_count": row.sample_count,
            }
            for row in rows
        ],
        "payload_decoded": any(row.parse_status == "decoded" for row in rows),
    }


@router.get("/workouts")
def health_workouts(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(require_user_id),
) -> dict:
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    with session_scope() as db:
        rows = HealthRepository(db).workouts(user_id, from_date, to_date, limit)
    return {
        "user_id": user_id,
        "workouts": [
            {
                **row.data,
                "source": row.source,
                "workout_id": row.workout_id,
                "detail_available": row.detail_synced,
            }
            for row in rows
        ],
    }


@router.get("/workouts/{workout_id}")
def health_workout_detail(
    workout_id: str,
    source: str = Query(..., min_length=1, max_length=32),
    user_id: str = Depends(require_user_id),
) -> dict:
    with session_scope() as db:
        repo = HealthRepository(db)
        row = repo.workout(user_id, workout_id, source=source)
        if row is None:
            raise HTTPException(status_code=404, detail="运动记录不存在")
        detail = dict(row.detail or {})
        samples = repo.workout_metric_samples(
            user_id, workout_id, source=source
        )
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
            "workout": {**row.data, "source": row.source, "workout_id": row.workout_id},
            "detail_available": row.detail_synced,
            "detail": detail or None,
        }


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_auto_sync() -> str:
    from vitalis.config import settings
    now = datetime.now(ZoneInfo(settings.timezone))
    candidate = now.replace(
        hour=settings.sync_cron_hour, minute=settings.sync_cron_minute,
        second=0, microsecond=0,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat()
