"""The single current HTTP contract for product clients."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from vitalis.entrypoints.api.deps import require_scope, require_user_id
from vitalis.bootstrap import (
    get_health_query,
    get_source_account_service,
    get_sync_job_service,
)
from vitalis.intelligence.contracts import (
    DailyProfile, MonthlyProfile, MorningBriefing, ReportBriefing,
    SubjectiveFeedback, SubjectiveFeedbackInput, WeeklyProfile,
)
from vitalis.intelligence.public_reports import PublicReportView, to_public_report_view
from vitalis.bootstrap import get_intelligence_action, get_intelligence_query
from vitalis.application.ports import FeedbackIdempotencyConflict, SyncJobCommand
from vitalis.application.sync_jobs import (
    SyncJobAuthRequired,
    SyncJobConflict,
    SyncJobInvalid,
)
from vitalis.time import local_today


router = APIRouter(tags=["current"])
Report = DailyProfile | WeeklyProfile | MonthlyProfile | MorningBriefing | ReportBriefing
ReportKind = Literal["daily", "morning", "evening", "weekly", "monthly", "weekly-briefing", "monthly-briefing"]


class SyncJobRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=730)
    source: Literal["zepp"] = "zepp"
    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")
    decode_dense_files: bool = False
    detail_backfill: bool = False
    workout_only: bool = False
    detail_only: bool = False
    detail_limit: int | None = Field(default=None, ge=1, le=4)
    detail_refresh_before: str | None = None


class AnalysisJobRequest(BaseModel):
    day: date | None = None


@router.get("/data-status", operation_id="get_data_status")
def get_data_status(user_id: str = Depends(require_user_id)) -> dict:
    return get_health_query().data_status(user_id)


@router.get("/deliveries", operation_id="list_notification_deliveries")
def list_notification_deliveries(
    limit: int = Query(100, ge=1, le=100),
    user_id: str = Depends(require_user_id),
) -> list[dict]:
    """Expose sanitized, user-scoped notification delivery state."""
    return get_intelligence_query().deliveries(user_id, limit=limit)


@router.get("/reports/{kind}", response_model=Report, operation_id="get_report")
def get_report(
    kind: ReportKind,
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> Report:
    query = get_intelligence_query()
    selection = {
        "daily": query.daily,
        "morning": query.morning_briefing,
        "evening": query.evening_briefing,
        "weekly": query.weekly,
        "monthly": query.monthly,
        "weekly-briefing": query.weekly_briefing,
        "monthly-briefing": query.monthly_briefing,
    }
    result = selection[kind](user_id, day)
    if result is None:
        raise HTTPException(status_code=404, detail="指定日期尚未生成分析快照")
    return result


@router.get("/reports/{kind}/view", response_model=PublicReportView, operation_id="get_public_report_view")
def get_public_report_view(
    kind: ReportKind,
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> PublicReportView:
    query = get_intelligence_query()
    selection = {
        "daily": query.daily, "morning": query.morning_briefing,
        "evening": query.evening_briefing, "weekly": query.weekly,
        "monthly": query.monthly, "weekly-briefing": query.weekly_briefing,
        "monthly-briefing": query.monthly_briefing,
    }
    result = selection[kind](user_id, day)
    if result is None:
        raise HTTPException(status_code=404, detail="指定日期尚未生成分析快照")
    normalized = kind.removesuffix("-briefing")
    return to_public_report_view(result, normalized)


@router.get("/reports/{kind}/state", operation_id="get_report_state")
def get_report_state(
    kind: ReportKind,
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> dict:
    """Return report freshness without replacing the existing report payload."""
    profile_type = {
        "daily": "daily",
        "morning": "daily",
        "evening": "daily",
        "weekly": "weekly",
        "weekly-briefing": "weekly",
        "monthly": "monthly",
        "monthly-briefing": "monthly",
    }[kind]
    return get_intelligence_query().report_state(user_id, profile_type, day)


@router.post("/analysis-runs", status_code=202, operation_id="create_analysis_run")
def create_analysis_run(
    body: AnalysisJobRequest,
    idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    user_id: str = Depends(require_scope("analyze")),
) -> dict:
    from vitalis.application.jobs import create_analysis_job
    from vitalis.bootstrap import configure_analysis_jobs

    configure_analysis_jobs()
    try:
        job_id = create_analysis_job(user_id, body.day or local_today(), idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job_id, "status": "queued", "status_url": f"/api/jobs/{job_id}"}


@router.get("/jobs/{job_id}", operation_id="get_job")
def get_job(job_id: str, user_id: str = Depends(require_user_id)) -> dict:
    from vitalis.application.jobs import get_analysis_job
    from vitalis.bootstrap import configure_analysis_jobs

    configure_analysis_jobs()
    analysis = get_analysis_job(user_id, job_id)
    if analysis is not None:
        return {
            "job_id": analysis.id,
            "kind": "analysis",
            "status": analysis.status,
            "target_date": analysis.target_date,
            "analysis_run_id": analysis.run_id,
            "error": analysis.error,
        }
    state = get_sync_job_service().status(user_id, job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job_id": job_id, "kind": "sync", "user_id": user_id, **state}


@router.post("/jobs/{job_id}/cancel", operation_id="cancel_sync_job")
def cancel_sync_job(job_id: str, user_id: str = Depends(require_scope("sync"))) -> dict:
    changed = get_sync_job_service().cancel(user_id, job_id)
    if changed is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job_id": job_id, "cancel_requested": changed}


@router.post("/sources/{source}/revoke", operation_id="revoke_source_account")
def revoke_source_account(
    source: Literal["zepp"],
    user_id: str = Depends(require_scope("manage")),
) -> dict:
    """Revoke one source account while retaining local facts and the user."""
    return get_source_account_service().revoke(
        user_id, source, reason=f"{source} 数据源账号已撤销"
    ).as_dict()


@router.post("/sync-jobs", status_code=202, operation_id="create_sync_job")
def create_sync_job(
    body: SyncJobRequest,
    idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    user_id: str = Depends(require_scope("sync")),
) -> dict:
    command = SyncJobCommand(
        user_id=user_id,
        source=body.source,
        idempotency_key=idempotency_key,
        days=body.days,
        from_date=body.from_date,
        to_date=body.to_date,
        decode_dense_files=body.decode_dense_files,
        detail_backfill=body.detail_backfill,
        workout_only=body.workout_only,
        detail_only=body.detail_only,
        detail_limit=body.detail_limit,
        detail_refresh_before=body.detail_refresh_before,
    )
    try:
        return get_sync_job_service().create(command).as_dict()
    except SyncJobAuthRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SyncJobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SyncJobInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workouts", operation_id="list_workouts")
def list_workouts(
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(require_user_id),
) -> dict:
    end = to_date or local_today()
    start = from_date or (end - timedelta(days=27))
    if start > end:
        start, end = end, start
    return get_health_query().list_workouts(user_id, start, end, limit)


@router.get("/workouts/{workout_id}", operation_id="get_workout")
def get_workout(
    workout_id: str,
    source: str = Query(..., min_length=1, max_length=32),
    user_id: str = Depends(require_user_id),
) -> dict:
    result = get_health_query().workout_detail(user_id, workout_id, source)
    if result is None:
        raise HTTPException(status_code=404, detail="运动记录不存在")
    return result


@router.post("/feedback", status_code=201, response_model=SubjectiveFeedback, operation_id="create_feedback")
def create_feedback(
    body: SubjectiveFeedbackInput,
    user_id: str = Depends(require_scope("feedback")),
    idempotency_key: Annotated[
        str | None, Header(min_length=16, max_length=128, alias="Idempotency-Key")
    ] = None,
) -> SubjectiveFeedback:
    try:
        return get_intelligence_action().log_feedback(
            user_id,
            body,
            idempotency_key=idempotency_key,
        )
    except FeedbackIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/feedback", response_model=list[SubjectiveFeedback], operation_id="list_feedback")
def list_feedback(
    start: date | None = None,
    end: date | None = None,
    user_id: str = Depends(require_user_id),
) -> list[SubjectiveFeedback]:
    period_end = end or local_today()
    period_start = start or period_end - timedelta(days=6)
    if period_start > period_end:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    return get_intelligence_query().feedback(user_id, period_start, period_end)
