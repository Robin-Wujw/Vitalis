"""Bearer-scoped product-validation routes; bootstrap injects every use case."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Callable, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from vitalis.application.product_tracking import (
    GoalInput, GoalPatch, ProductFeedbackInput, ProductResourceNotFound,
    ProductTrackingConflict, ProductTrackingService, ProductTrackingValidationError,
)
from vitalis.entrypoints.api.deps import require_scope, require_user_id


router = APIRouter(prefix="/product", tags=["product"])
T = TypeVar("T")
Key = Annotated[str, Header(min_length=16, max_length=128, alias="Idempotency-Key")]


def product_tracking_service() -> ProductTrackingService:
    """The composition root owns the SQL adapter and clock/timezone choices."""
    from vitalis.bootstrap import get_product_tracking_service

    return get_product_tracking_service()


Service = Annotated[ProductTrackingService, Depends(product_tracking_service)]


def _call(action: Callable[[], T]) -> T:
    try:
        return action()
    except ProductTrackingConflict as exc:
        raise HTTPException(status_code=409, detail="product request conflicts with current state") from exc
    except ProductResourceNotFound as exc:
        raise HTTPException(status_code=404, detail="user-owned resource not found") from exc
    except ProductTrackingValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid product request") from exc


@router.get("/goals", operation_id="get_product_goals")
def goals(service: Service, user_id: str = Depends(require_user_id)) -> dict:
    """Read explicit goals and the existing training preference revision."""
    return _call(lambda: service.goals(user_id))


@router.post("/goals", status_code=201, operation_id="create_product_goal")
def create_goal(body: GoalInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("manage"))) -> dict:
    return _call(lambda: service.put_goal(user_id, body, idempotency_key=idempotency_key))


@router.put("/goals/{goal_id}", operation_id="replace_product_goal")
def replace_goal(goal_id: str, body: GoalInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("manage"))) -> dict:
    return _call(lambda: service.put_goal(user_id, body, goal_id=goal_id, idempotency_key=idempotency_key))


@router.patch("/goals/{goal_id}", operation_id="patch_product_goal")
def patch_goal(goal_id: str, body: GoalPatch, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("manage"))) -> dict:
    return _call(lambda: service.patch_goal(user_id, goal_id, body, idempotency_key=idempotency_key))


@router.get("/feedback", operation_id="get_product_feedback")
def feedback(service: Service, start: date | None = None, end: date | None = None, limit: int = Query(100, ge=1, le=100), user_id: str = Depends(require_user_id)) -> list[dict]:
    return _call(lambda: service.feedback(user_id, start, end, limit=limit))


@router.post("/feedback", status_code=201, operation_id="record_product_feedback")
def record_feedback(body: ProductFeedbackInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Record or revise one confirmed feedback kind without accepting a goal."""
    return _call(lambda: service.record_feedback(user_id, body, idempotency_key=idempotency_key))


@router.get("/input-events", operation_id="get_product_input_events")
def input_events(service: Service, limit: int = Query(100, ge=1, le=100), user_id: str = Depends(require_user_id)) -> list[dict]:
    """Read non-sensitive input refs and the atomically queued analysis IDs."""
    return _call(lambda: service.input_events(user_id, limit=limit))


@router.get("/summary", operation_id="get_product_summary")
def summary(service: Service, start: date | None = None, end: date | None = None, user_id: str = Depends(require_user_id)) -> dict:
    return _call(lambda: service.summary(user_id, start, end))


@router.get("/metrics", operation_id="get_product_metrics")
def metrics(service: Service, start: date | None = None, end: date | None = None, user_id: str = Depends(require_user_id)) -> dict:
    return _call(lambda: service.metrics(user_id, start, end))


@router.get("/context", operation_id="get_product_context")
def context(service: Service, day: date | None = None, user_id: str = Depends(require_user_id)) -> dict:
    return _call(lambda: service.analysis_context(user_id, day))


__all__ = ["router", "product_tracking_service"]
