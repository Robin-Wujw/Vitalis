"""Descriptive health endpoints; all facts and arithmetic live below HTTP."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from vitalis.bootstrap import get_health_analytics
from vitalis.application.health_query_budget import HealthQueryBudgetExceeded
from vitalis.entrypoints.api.deps import require_user_id
from vitalis.entrypoints.api.date_windows import date_window
from vitalis.time import local_today

router = APIRouter(prefix="/health", tags=["health"])


def period(from_date: date | None = Query(None, alias="from"), to_date: date | None = Query(None, alias="to")):
    return date_window(from_date, to_date, default_days=30)


def _query(call):
    try:
        return call()
    except HealthQueryBudgetExceeded as exc:
        raise HTTPException(413, "health query data exceeds resource budget") from exc
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "invalid health query") from exc


@router.get("/daily-summary", operation_id="get_daily_activity_summary")
def daily_summary(window=Depends(period), include_hourly: bool = False, include_minutes: bool = False,
                  user_id: str = Depends(require_user_id)):
    if include_minutes and (window[1] - window[0]).days >= 31:
        raise HTTPException(422, "minute activity queries require at most 31 days")
    return _query(lambda: get_health_analytics().daily_summary(user_id, *window,
        include_hourly=include_hourly, include_minutes=include_minutes))


@router.get("/heart-rate", operation_id="get_heart_rate_summary")
def heart_rate(day: date | None = None, include_series: bool = False,
               user_id: str = Depends(require_user_id)):
    selected = day or local_today()
    return _query(lambda: get_health_analytics().heart_rate(user_id, selected, selected, include_series=include_series))


@router.get("/sleep", operation_id="get_sleep_summary")
def sleep(window=Depends(period), user_id: str = Depends(require_user_id)):
    return _query(lambda: get_health_analytics().sleep(user_id, *window))


@router.get("/body-composition", operation_id="get_body_composition")
def body_composition(from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"), before: date | None = None,
    limit: int = Query(20, ge=1, le=200), cursor: str | None = Query(None, min_length=1, max_length=4096),
    user_id: str = Depends(require_user_id)):
    return _query(lambda: get_health_analytics().body_composition(user_id, from_date, to_date,
        limit=limit, before=before, cursor=cursor))


@router.get("/training-thresholds", operation_id="get_training_thresholds")
def thresholds(window=Depends(period), user_id: str = Depends(require_user_id)):
    return _query(lambda: get_health_analytics().thresholds(user_id, *window))


@router.get("/summary/{category}", operation_id="get_health_metric_summary")
def metric_summary(category: Literal["readiness", "hrv", "resting_heart_rate", "respiratory_rate", "stress",
    "spo2", "pai", "body_charge", "daily_goals"], include_samples: bool = False,
    window=Depends(period), user_id: str = Depends(require_user_id)):
    return _query(lambda: get_health_analytics().metrics(user_id, category, *window, include_samples=include_samples))


@router.get("/period-summary", operation_id="get_period_summary")
def period_summary(window=Depends(period), user_id: str = Depends(require_user_id)):
    result = _query(lambda: get_health_analytics().period_summary(user_id, *window))
    if result["state"] == "failed":
        raise HTTPException(503, "health analytics temporarily unavailable")
    return result
