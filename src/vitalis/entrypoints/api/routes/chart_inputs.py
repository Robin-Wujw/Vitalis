"""Bearer-scoped chart input commands and saved-data reads.

The composition root supplies the application service. No health calculations,
file access, account connection, or notification delivery live in these routes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Callable, Literal, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from vitalis.application.chart_inputs import (
    ActivityReviewInput, ActivityRuleInput, AnnotationInput, AnnotationPatch, ChartInputConflict,
    ChartInputLimitError, ChartInputNotFound, ChartInputService, ChartInputValidationError,
    DeleteAnnotationInput, HevyImportInput, HevyLinkInput, HevySetMappingInput,
)
from vitalis.entrypoints.api.deps import require_scope, require_user_id


router = APIRouter(prefix="/chart-inputs", tags=["chart-inputs"])
Key = Annotated[str, Header(min_length=16, max_length=128, alias="Idempotency-Key")]
T = TypeVar("T")


def chart_input_service() -> ChartInputService:
    """Bootstrap owns sessions, configured timezone, and clocks."""
    from vitalis.bootstrap import get_chart_input_service

    return get_chart_input_service()


Service = Annotated[ChartInputService, Depends(chart_input_service)]


def _call(action: Callable[[], T]) -> T:
    try:
        return action()
    except ChartInputConflict as exc:
        raise HTTPException(status_code=409, detail="chart input conflicts with current state") from exc
    except ChartInputNotFound as exc:
        raise HTTPException(status_code=404, detail="user-owned input not found") from exc
    except ChartInputLimitError as exc:
        raise HTTPException(status_code=413, detail="chart input budget exceeded") from exc
    except ChartInputValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid chart input") from exc


@router.get("/annotations", operation_id="get_chart_annotations")
def annotations(service: Service, start: date | None = None, end: date | None = None, limit: int = Query(100, ge=1, le=100), user_id: str = Depends(require_user_id)) -> dict:
    """Read user annotations overlapping an inclusive local calendar-day range."""
    return _call(lambda: service.annotations(user_id, start, end, limit=limit))


@router.post("/annotations", status_code=201, operation_id="create_chart_annotation")
def create_annotation(body: AnnotationInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Record explicitly requested timeline context; text is not a causal fact."""
    return _call(lambda: service.create_annotation(user_id, body, idempotency_key=idempotency_key))


@router.patch("/annotations/{annotation_id}", operation_id="patch_chart_annotation")
def patch_annotation(annotation_id: str, body: AnnotationPatch, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    return _call(lambda: service.patch_annotation(user_id, annotation_id, body, idempotency_key=idempotency_key))


@router.delete("/annotations/{annotation_id}", operation_id="delete_chart_annotation")
def delete_annotation(annotation_id: str, body: DeleteAnnotationInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Delete only on explicit user request; retain historical analysis revisions."""
    return _call(lambda: service.delete_annotation(user_id, annotation_id, body, idempotency_key=idempotency_key))


@router.get("/activity-reviews", operation_id="get_activity_review_queue")
def activity_reviews(service: Service, start: date | None = None, end: date | None = None, status: Literal["pending", "classified", "ignored", "all"] = "pending", limit: int = Query(100, ge=1, le=100), user_id: str = Depends(require_user_id)) -> dict:
    """Read auto-recognized or unclassified activities and their display overlays."""
    return _call(lambda: service.activity_reviews(user_id, start, end, status=status, limit=limit))


@router.post("/activity-reviews", operation_id="review_activity")
def review_activity(body: ActivityReviewInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Classify, ignore, or restore an overlay while retaining vendor records."""
    return _call(lambda: service.review_activity(user_id, body, idempotency_key=idempotency_key))


@router.get("/activity-rules", operation_id="get_activity_rules")
def activity_rules(service: Service, user_id: str = Depends(require_user_id)) -> dict:
    return _call(lambda: service.activity_rules(user_id))


@router.post("/activity-rules", operation_id="save_activity_rule")
def save_activity_rule(body: ActivityRuleInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Create/revise/disable a bounded suggest-only rule; never apply a review."""
    return _call(lambda: service.save_activity_rule(user_id, body, idempotency_key=idempotency_key))


@router.post("/hevy-imports", status_code=201, operation_id="import_hevy_csv")
def import_hevy(body: HevyImportInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Import user-uploaded CSV text (1 MiB, 2000 rows, 100 sessions); no credentials."""
    return _call(lambda: service.import_hevy(user_id, body, idempotency_key=idempotency_key))


@router.get("/external-workouts", operation_id="get_external_strength_workouts")
def external_workouts(service: Service, start: date | None = None, end: date | None = None, limit: int = Query(100, ge=1, le=100), user_id: str = Depends(require_user_id)) -> dict:
    return _call(lambda: service.external_workouts(user_id, start, end, limit=limit))


@router.get("/external-workouts/{external_id}", operation_id="get_external_strength_workout")
def external_workout(external_id: str, service: Service, user_id: str = Depends(require_user_id)) -> dict:
    """Read preserved uploaded sets and explicit link provenance."""
    return _call(lambda: service.external_workout(user_id, external_id))


@router.get("/external-workouts/{external_id}/candidates", operation_id="get_hevy_link_candidates")
def hevy_candidates(external_id: str, service: Service, user_id: str = Depends(require_user_id)) -> dict:
    """Return overlap/same-day candidates; multiple matches remain ambiguous."""
    return _call(lambda: service.hevy_candidates(user_id, external_id))


@router.post("/external-workouts/{external_id}/link", operation_id="link_hevy_workout")
def link_hevy(external_id: str, body: HevyLinkInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Explicitly identify one same-user Zepp workout; do not create another workout."""
    return _call(lambda: service.link_hevy(user_id, external_id, body, idempotency_key=idempotency_key))


@router.patch("/external-workouts/{external_id}/sets", operation_id="map_hevy_strength_sets")
def map_hevy_sets(external_id: str, body: HevySetMappingInput, idempotency_key: Key, service: Service, user_id: str = Depends(require_scope("feedback"))) -> dict:
    """Explicitly map set identity, muscle roles and side, preserving Hevy source."""
    return _call(lambda: service.map_hevy_sets(user_id, external_id, body, idempotency_key=idempotency_key))


@router.get("/snapshot", operation_id="get_chart_input_snapshot")
def snapshot(service: Service, start: date, end: date, as_of: datetime | None = None, user_id: str = Depends(require_user_id)) -> dict:
    """Read immutable input revisions visible at an explicit analysis cutoff."""
    return _call(lambda: service.snapshot(user_id, start, end, as_of=as_of))


__all__ = ["router", "chart_input_service"]
