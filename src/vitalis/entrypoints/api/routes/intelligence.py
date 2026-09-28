"""Versioned Vitalis Health Intelligence API."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from vitalis.entrypoints.api.deps import require_scope, require_user_id
from vitalis.intelligence.contracts import (
    AgentContext,
    DecisionExplanation,
    EventAcknowledgement,
    HealthEventResponse,
    HealthTimeline,
    LinkRecommendationInput,
    PersonalAssociationProfile,
    PersonalModel,
    RecommendationInstance,
    StrengthExerciseRecord,
    StrengthWorkoutConfirmationInput,
    TrendResponse,
    TrainingPreferenceInput,
    TrainingPreferencePatch,
    TrainingPreferences,
    UserProfile,
    UserProfilePatch,
    ProfileRevisionConflict,
    TrainingResponseProfile,
)
from vitalis.bootstrap import get_intelligence_action, get_intelligence_query
from vitalis.time import local_today


router = APIRouter(prefix="/intelligence", tags=["intelligence"])


def _snapshot_or_404(value):
    if value is None:
        raise HTTPException(status_code=404, detail="指定日期尚未生成分析快照")
    return value


@router.get("/profile", response_model=UserProfile, summary="Read the user-confirmed profile")
def profile(user_id: str = Depends(require_user_id)) -> UserProfile:
    return get_intelligence_query().profile(user_id)


@router.patch("/profile", response_model=UserProfile, summary="Patch the user-confirmed profile")
def patch_profile(
    payload: UserProfilePatch,
    user_id: str = Depends(require_scope("manage")),
) -> UserProfile:
    try:
        return get_intelligence_action().patch_profile(user_id, payload)
    except ProfileRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/trends", response_model=TrendResponse, summary="Get deterministic personal trends")
def trends(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> TrendResponse:
    return _snapshot_or_404(get_intelligence_query().trends(user_id, day))


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
        return get_intelligence_query().events(user_id, period_start, period_end, event_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/explain", response_model=DecisionExplanation, summary="Explain a training decision")
def explain(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> DecisionExplanation:
    return _snapshot_or_404(get_intelligence_query().explain(user_id, day))


@router.get("/context", response_model=AgentContext, summary="Get structured Hermes agent context")
def context(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> AgentContext:
    return _snapshot_or_404(get_intelligence_query().context(user_id, day))


@router.get(
    "/training-responses",
    response_model=TrainingResponseProfile,
    summary="Read deterministic post-workout responses",
)
def training_responses(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> TrainingResponseProfile:
    return _snapshot_or_404(get_intelligence_query().training_responses(user_id, day))


@router.get(
    "/personal-model",
    response_model=PersonalModel,
    summary="Read Personal Model v2",
)
def personal_model(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> PersonalModel:
    return _snapshot_or_404(get_intelligence_query().personal_model(user_id, day))


@router.get(
    "/personal-associations",
    response_model=PersonalAssociationProfile,
    summary="Read deterministic 60/90-day personal associations",
)
def personal_associations(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> PersonalAssociationProfile:
    return _snapshot_or_404(get_intelligence_query().personal_associations(user_id, day))


@router.get("/timeline", response_model=HealthTimeline, summary="Read the typed health timeline")
def timeline(
    start: date | None = None,
    end: date | None = None,
    limit: int = 100,
    user_id: str = Depends(require_user_id),
) -> HealthTimeline:
    period_end = end or local_today()
    period_start = start or period_end - timedelta(days=27)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit 必须在 1 到 100 之间")
    try:
        return get_intelligence_query().timeline(user_id, period_start, period_end, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/recommendations/{recommendation_id}", response_model=RecommendationInstance)
def recommendation(
    recommendation_id: str,
    user_id: str = Depends(require_user_id),
) -> RecommendationInstance:
    value = get_intelligence_query().recommendation(user_id, recommendation_id)
    if value is None:
        raise HTTPException(status_code=404, detail="训练建议不存在")
    return value


@router.post(
    "/recommendations/{recommendation_id}/complete",
    response_model=RecommendationInstance,
    summary="Explicitly link a recommendation to a completed workout",
)
def complete_recommendation(
    recommendation_id: str,
    payload: LinkRecommendationInput,
    user_id: str = Depends(require_scope("feedback")),
) -> RecommendationInstance:
    try:
        return get_intelligence_action().complete_recommendation(
            user_id,
            recommendation_id,
            payload.workout_id,
            workout_source=payload.workout_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/training-preferences",
    response_model=TrainingPreferences,
    summary="Read health-first concurrent training constraints",
)
def training_preferences(
    user_id: str = Depends(require_user_id),
) -> TrainingPreferences:
    return get_intelligence_query().training_preferences(user_id)


@router.put(
    "/training-preferences",
    response_model=TrainingPreferences,
    summary="Replace health-first concurrent training constraints",
)
def set_training_preferences(
    payload: TrainingPreferenceInput,
    user_id: str = Depends(require_scope("manage")),
) -> TrainingPreferences:
    return get_intelligence_action().set_training_preferences(user_id, payload)


@router.patch(
    "/training-preferences",
    response_model=TrainingPreferences,
    summary="Patch only provided training preference fields",
)
def patch_training_preferences(
    payload: TrainingPreferencePatch,
    user_id: str = Depends(require_scope("manage")),
) -> TrainingPreferences:
    try:
        return get_intelligence_action().patch_training_preferences(user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/workouts/{workout_id}/strength-exercises",
    response_model=list[StrengthExerciseRecord],
    status_code=201,
    summary="Replace user-confirmed exercises for one strength workout",
)
def confirm_strength_workout(
    workout_id: str,
    payload: StrengthWorkoutConfirmationInput,
    source: str = Query(..., min_length=1, max_length=32),
    user_id: str = Depends(require_scope("feedback")),
) -> list[StrengthExerciseRecord]:
    try:
        return get_intelligence_action().confirm_strength_workout(
            user_id, workout_id, payload, workout_source=source
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/events/{event_id}/acknowledge",
    response_model=EventAcknowledgement,
    summary="Acknowledge a user-scoped health event",
)
def acknowledge_event(
    event_id: str,
    user_id: str = Depends(require_scope("feedback")),
) -> EventAcknowledgement:
    event = get_intelligence_action().acknowledge_event(user_id, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="健康事件不存在")
    return EventAcknowledgement(event=event)
