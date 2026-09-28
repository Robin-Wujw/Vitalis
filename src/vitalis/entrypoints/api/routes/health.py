"""Raw health queries and synchronization endpoints."""

from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from vitalis.entrypoints.api.deps import require_user_id
from vitalis.application.health_query import MetricQueryBudgetExceeded
from vitalis.bootstrap import get_health_query, get_range_summary
from vitalis.domain.aggregation import Granularity

router = APIRouter(prefix="/health", tags=["health"])


def health_data_health(user_id: str = Depends(require_user_id)) -> dict:
    """Explain the latest Zepp fetch and durable attempt progress."""
    return get_health_query().data_status(user_id)


@router.get("/token-status")
def health_token_status(user_id: str = Depends(require_user_id)) -> dict:
    """Return stored Zepp credential metadata without online verification."""
    return get_health_query().token_status(user_id)


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
        raise HTTPException(status_code=400, detail="查询时间范围超过允许上限")
    blocks = get_range_summary().range_summary(
        user_id, from_date, to_date, granularity
    )
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
    end = _utc_datetime(to_time or datetime.now(timezone.utc))
    start = _utc_datetime(from_time or end - timedelta(days=7))
    if start > end:
        start, end = end, start
    if end - start > timedelta(days=730):
        raise HTTPException(status_code=400, detail="查询跨度不能超过 730 天")
    try:
        result = get_health_query().metric_series(
            user_id, metric, start, end, resolution
        )
    except MetricQueryBudgetExceeded as exc:
        raise HTTPException(status_code=413, detail="聚合结果超过预算，请缩小查询窗口") from exc
    return {
        "user_id": user_id,
        "metric": metric,
        "resolution": resolution,
        "from": _iso_utc(start),
        "to": _iso_utc(end),
        **result,
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
    return {
        "user_id": user_id,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "metrics": get_health_query().daily_metrics(
            user_id, from_date, to_date, metric
        ),
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
    return {
        "user_id": user_id,
        "stream": stream,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        **get_health_query().dense_files(
            user_id, stream, from_date, to_date, limit
        ),
    }


def health_workouts(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(require_user_id),
) -> dict:
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    return get_health_query().list_workouts(user_id, from_date, to_date, limit)


def health_workout_detail(
    workout_id: str,
    source: str = Query(..., min_length=1, max_length=32),
    user_id: str = Depends(require_user_id),
) -> dict:
    result = get_health_query().workout_detail(user_id, workout_id, source)
    if result is None:
        raise HTTPException(status_code=404, detail="运动记录不存在")
    return result


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


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
