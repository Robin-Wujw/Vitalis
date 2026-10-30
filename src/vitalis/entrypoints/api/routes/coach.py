"""Narrow server-policy-authorized conversation operations; no manage permission."""
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from vitalis.bootstrap import get_health_query
from vitalis.time import local_today
from starlette.responses import JSONResponse

from vitalis.application.coach_operations import CoachError, CoachOperation
from vitalis.entrypoints.api.deps import require_scope, require_user_id

router = APIRouter(tags=["coach"])


def get_coach_operation_service():
    from vitalis.bootstrap import get_coach_operation_service as build
    return build()


@router.get("/coach/strength-review", operation_id="get_strength_review",
            summary="Calculate recorded strength workload and comparable previous sessions without writes")
def strength_review(
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=100, description="Maximum workouts read; result reports truncation; at most 2000 expanded sets, reduce range or limit on 422"),
    user_id: str = Depends(require_user_id),
) -> dict:
    if (from_date is None) != (to_date is None):
        raise HTTPException(status_code=422, detail="from and to must be paired")
    end = to_date or local_today()
    start = from_date or end - timedelta(days=27)
    if start > end or (end - start).days >= 90:
        raise HTTPException(status_code=422, detail="window must be 1..90 days")
    try:
        return get_health_query().strength_review(user_id, start, end, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Reduce the date range or workout limit") from exc


@router.get("/coach/race-equivalence", operation_id="get_race_equivalence",
            summary="Compute a conditional distance equivalence from one saved run; not a personal forecast")
def race_equivalence(
    workout_id: str = Query(..., min_length=1, max_length=128),
    source: str = Query(..., min_length=1, max_length=32),
    target_km: float = Query(21.0975, ge=3, le=42.195, allow_inf_nan=False),
    effort_basis: Literal["scenario", "user_reported_maximal"] = Query(
        "scenario", description="Scenario assumes maximal source effort; user_reported_maximal requires the user's explicit statement and is not saved"),
    user_id: str = Depends(require_user_id),
) -> dict:
    try:
        result = get_health_query().race_equivalence(user_id, workout_id, source,
                                                   target_km=target_km, effort_basis=effort_basis)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Recorded run cannot support this calculation") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    return result


@router.get("/coach/policy", operation_id="get_coach_policy")
def get_coach_policy(user_id: str = Depends(require_user_id)) -> dict:
    from vitalis.config import settings
    return {**get_coach_operation_service().policy(user_id), "timezone": settings.timezone}


@router.post("/coach/operations", status_code=202, operation_id="create_coach_operation")
def create_coach_operation(
    body: CoachOperation,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
    user_id: str = Depends(require_scope("coach")),
):
    try:
        return get_coach_operation_service().create(user_id, idempotency_key, body)
    except CoachError as exc:
        if not exc.failure_code:
            raise HTTPException(status_code=exc.status_code, detail="Coach operation rejected") from exc
        payload = {
            "state": "failed", "failure_code": exc.failure_code,
            "message": str(exc), "retryable": exc.retryable,
            "next_action": exc.next_action, "request_id": request.state.request_id,
        }
        request.state.coach_error = payload
        return JSONResponse(status_code=exc.status_code, content=payload)
