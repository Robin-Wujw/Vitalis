"""GET /api/v1/health/today —— 获取今日健康状态。"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from vitalis.api.deps import require_user_id
from vitalis.connectors import get_connector
from vitalis.connectors.zepp import ZeppAuthError, ZeppConnector
from vitalis.models import User
from vitalis.services import SummaryService
from vitalis.services.aggregation_service import AggregationService, Granularity
from vitalis.storage import HealthRepository, session_scope

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/today")
def health_today(day: date | None = None, user_id: str = Depends(require_user_id)) -> dict:
    """获取指定日（缺省今天）的健康状态摘要。

    对应架构文档的返回形态：
    {"score": 86, "sleep": "good", "training": "ready", "stress": "medium"}
    """
    payload = SummaryService().today(user_id, day=day)
    if not payload.get("found"):
        return {
            "user_id": user_id,
            "date": (day or date.today()).isoformat(),
            "score": None,
            "sleep": "no_data",
            "training": "no_data",
            "stress": "no_data",
            "detail": "该日暂无数据，请先 POST /connect/zepp 同步",
        }
    return {
        "user_id": payload["user_id"],
        "date": payload["date"],
        "score": payload["overall_score"],
        "sleep": str(payload.get("recovery_level") or "unknown"),
        "training": str(payload.get("training_readiness") or "unknown"),
        "stress": str(payload.get("stress_level") or "unknown"),
        "explanation": payload.get("explanation", ""),
        "matched_rules": payload.get("matched_rules", []),
    }


@router.post("/sync")
def health_sync(
    days: int = Query(7, ge=1, le=730, description="同步天数"),
    user_id: str = Depends(require_user_id),
) -> dict:
    """手动触发增量同步（不局限于凌晨自动调度）。"""
    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    try:
        with session_scope() as db:
            repo = HealthRepository(db)
            auth = connector.load_token(repo, user_id)
            if auth is None:
                return {
                    "user_id": user_id,
                    "status": "token_required",
                    "detail": "尚未导入 Zepp 凭据，请先访问 /connect/zepp/scan 导入",
                }
            report = connector.sync_with_report(
                User(id=user_id), days=days, repo=repo
            )
        return {
            "user_id": user_id,
            "status": "synced",
            "success": report.success,
            "streams": [
                {
                    "stream": s.stream,
                    "status": s.status,
                    "records_written": s.records_written,
                    "message": s.message,
                }
                for s in report.streams
            ],
            "records_written": report.records_written,
            "message": report.message,
        }
    except ZeppAuthError as exc:
        with session_scope() as db:
            HealthRepository(db).mark_user_browser_links_reauth(
                user_id, "Zepp 登录已失效，请重新登录"
            )
        return {
            "user_id": user_id,
            "status": "needs_reauth",
            "detail": str(exc),
            "hint": "Zepp 登录已失效，请在官方登录页重新登录；浏览器扩展会自动恢复连接",
        }
    except Exception as exc:
        return {"user_id": user_id, "status": "error", "detail": str(exc)}


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
    # 尝试验证 token 有效性
    try:
        connector.authenticate()
        with session_scope() as db:
            repo = HealthRepository(db)
            client = connector._client_for(repo, User(id=user_id))
            client.verify()
            link = repo.latest_browser_link(user_id)
            if link and link.status != "needs_login":
                repo.mark_browser_link_verified(link.token_digest)
        valid = True
        detail = "凭据有效"
    except Exception as exc:
        valid = False
        detail = str(exc)
        with session_scope() as db:
            HealthRepository(db).mark_user_browser_links_reauth(
                user_id, "Zepp 连接验证失败，请重新登录"
            )

    with session_scope() as db:
        link = HealthRepository(db).latest_browser_link(user_id)

    return {
        "user_id": user_id,
        "authorized": True,
        "valid": valid,
        "vendor_user_id": auth.source_user_id,
        "region_host": auth.region_host,
        "detail": detail,
        "connection_status": link.status if link else ("connected" if valid else "needs_login"),
        "needs_login": bool(link and link.status == "needs_login") or not valid,
        "connection_message": link.message if link else detail,
        "last_verified_at": (
            link.last_verified_at.isoformat() + "Z" if link and link.last_verified_at else None
        ),
        "last_sync_at": link.last_sync_at.isoformat() + "Z" if link and link.last_sync_at else None,
        "next_auto_sync": "每天凌晨 02:00",
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
                "health": {
                    "hrv_avg": b.hrv_avg,
                    "recovery_score_avg": b.recovery_score_avg,
                    "overall_score_avg": b.overall_score_avg,
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
        rows = HealthRepository(db).metric_samples(user_id, metric, start, end)

    if resolution == "raw":
        points = [
            {
                "timestamp": _iso_utc(row.timestamp),
                "value": row.value,
                "unit": row.unit,
                "source_scope": row.source_scope,
                "device_id": row.device_id,
            }
            for row in rows
        ]
    else:
        buckets: dict[str, list[float]] = defaultdict(list)
        units: dict[str, str] = {}
        for row in rows:
            if resolution == "1h":
                key = row.timestamp.replace(minute=0, second=0, microsecond=0).isoformat() + "Z"
            else:
                key = row.timestamp.date().isoformat()
            buckets[key].append(row.value)
            units[key] = row.unit
        points = [
            {
                "timestamp": key,
                "value": round(sum(values) / len(values), 2),
                "min": min(values),
                "max": max(values),
                "count": len(values),
                "unit": units[key],
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
                "source_scope": row.source_scope,
                "device_id": row.device_id,
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
    """Return indexed dense-file coverage while deliberately withholding file IDs."""
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
            {**row.data, "detail_available": row.detail_synced}
            for row in rows
        ],
    }


@router.get("/workouts/{workout_id}")
def health_workout_detail(workout_id: str, user_id: str = Depends(require_user_id)) -> dict:
    with session_scope() as db:
        repo = HealthRepository(db)
        row = repo.workout(user_id, workout_id)
        if row is None:
            raise HTTPException(status_code=404, detail="运动记录不存在")
        detail = dict(row.detail or {})
        samples = repo.workout_samples(user_id, workout_id)
        if samples:
            detail["samples"] = [
                {
                    "timestamp": _iso_utc(sample.timestamp),
                    "heart_rate": sample.heart_rate,
                    "source_scope": sample.source_scope,
                    "device_id": sample.device_id,
                }
                for sample in samples
            ]
        return {
            "user_id": user_id,
            "workout": row.data,
            "detail_available": row.detail_synced,
            "detail": detail or None,
        }


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
