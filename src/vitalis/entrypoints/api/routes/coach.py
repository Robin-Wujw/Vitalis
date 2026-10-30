"""Narrow server-policy-authorized conversation operations; no manage permission."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from starlette.responses import JSONResponse

from vitalis.application.coach_operations import CoachError, CoachOperation
from vitalis.entrypoints.api.deps import require_scope, require_user_id

router = APIRouter(tags=["coach"])


def get_coach_operation_service():
    from vitalis.bootstrap import get_coach_operation_service as build
    return build()


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
