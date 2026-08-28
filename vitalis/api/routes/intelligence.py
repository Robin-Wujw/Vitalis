"""Versioned Vitalis Health Intelligence API."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException

from vitalis.api.deps import require_user_id
from vitalis.intelligence.contracts import (
    AgentContext,
    DailyProfile,
    DecisionExplanation,
    EventAcknowledgement,
    HealthEventResponse,
    SubjectiveFeedback,
    SubjectiveFeedbackInput,
    TrendResponse,
    WeeklyProfile,
)
from vitalis.intelligence.service import IntelligencePipeline
from vitalis.time import local_today


router = APIRouter(prefix="/intelligence", tags=["intelligence"])


@router.get(
    "/daily",
    response_model=DailyProfile,
    summary="Build a deterministic daily health intelligence profile",
)
def daily_profile(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> DailyProfile:
    return IntelligencePipeline().build_daily_profile(user_id, day)


@router.get("/weekly", response_model=WeeklyProfile, summary="Build a deterministic weekly profile")
def weekly_profile(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> WeeklyProfile:
    return IntelligencePipeline().build_weekly_profile(user_id, day)


@router.get("/trends", response_model=TrendResponse, summary="Get deterministic personal trends")
def trends(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> TrendResponse:
    return IntelligencePipeline().trends(user_id, day)


@router.get("/events", response_model=HealthEventResponse, summary="Get persistent health events")
def events(
    start: date | None = None,
    end: date | None = None,
    event_type: str | None = None,
    user_id: str = Depends(require_user_id),
) -> HealthEventResponse:
    period_end = end or local_today()
    period_start = start or period_end - timedelta(days=27)
    try:
        return IntelligencePipeline().events(user_id, period_start, period_end, event_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/explain", response_model=DecisionExplanation, summary="Explain a training decision")
def explain(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> DecisionExplanation:
    return IntelligencePipeline().explain(user_id, day)


@router.get("/context", response_model=AgentContext, summary="Get structured Hermes agent context")
def context(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> AgentContext:
    return IntelligencePipeline().context(user_id, day)


@router.post("/feedback", response_model=SubjectiveFeedback, status_code=201)
def log_feedback(
    payload: SubjectiveFeedbackInput,
    user_id: str = Depends(require_user_id),
) -> SubjectiveFeedback:
    try:
        return IntelligencePipeline().log_feedback(user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/feedback", response_model=list[SubjectiveFeedback])
def feedback(
    start: date | None = None,
    end: date | None = None,
    user_id: str = Depends(require_user_id),
) -> list[SubjectiveFeedback]:
    period_end = end or local_today()
    period_start = start or period_end - timedelta(days=6)
    if period_start > period_end:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    return IntelligencePipeline().feedback(user_id, period_start, period_end)


@router.post(
    "/events/{event_id}/acknowledge",
    response_model=EventAcknowledgement,
    summary="Acknowledge a user-scoped health event",
)
def acknowledge_event(
    event_id: str,
    user_id: str = Depends(require_user_id),
) -> EventAcknowledgement:
    event = IntelligencePipeline().acknowledge_event(user_id, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="健康事件不存在")
    return EventAcknowledgement(event=event)
