"""Zepp metadata and durable read-only upstream queries."""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from vitalis.application.ports import SyncJobCommand
from vitalis.application.sync_jobs import SyncJobAuthRequired, SyncJobConflict, SyncJobInvalid
from vitalis.application.vendor_request import validate_raw_request
from vitalis.bootstrap import get_health_analytics, get_sync_job_service, get_vendor_query
from vitalis.entrypoints.api.deps import require_scope, require_user_id
from .health_analytics import period

router = APIRouter(prefix="/sources/zepp", tags=["sources"])


@router.get("/schema", operation_id="get_zepp_schema")
def schema(user_id: str = Depends(require_user_id)):
    from vitalis.bootstrap import get_zepp_schema
    return get_zepp_schema()


class RawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=2, max_length=512)
    params: dict = Field(default_factory=dict)


@router.get("/profile", operation_id="get_zepp_profile")
def profile(include_devices: bool = True, user_id: str = Depends(require_user_id)):
    return get_health_analytics().profile(user_id, include_devices=include_devices)


@router.post("/raw-requests", status_code=202, operation_id="create_zepp_raw_request")
def create_raw_request(body: RawRequest,
    idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    user_id: str = Depends(require_scope("source_raw"))):
    try:
        request = validate_raw_request(body.model_dump())
        job = get_sync_job_service().create(SyncJobCommand(user_id=user_id, source="zepp",
            idempotency_key=idempotency_key, days=1, decode_dense_files=False, raw_request=request,
            execution_context={"notification_policy": "suppress", "origin": "explicit_raw_api"}))
    except (SyncJobAuthRequired, SyncJobConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (SyncJobInvalid, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**job.as_dict(), "result_url": f"/api/sources/zepp/raw-requests/{job.job_id}",
            "method": "GET", "qualification": "raw_unverified"}


@router.get("/raw-requests/{job_id}", operation_id="get_zepp_raw_result")
def raw_result(job_id: str, user_id: str = Depends(require_scope("source_raw"))):
    try:
        result = get_vendor_query().job_result(user_id, job_id)
    except ValueError as exc:
        raise HTTPException(413, "raw response exceeds public read budget") from exc
    if result is None:
        raise HTTPException(404, "raw request not found")
    if result["result"] is None:
        if result["status"] in {"failed", "cancelled", "unavailable", "partial"}:
            raise HTTPException(502, "raw fetch failed; inspect job status")
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content={**result, "state": "pending"})
    return {**result, "state": "ready"}


@router.get("/raw-records", operation_id="list_zepp_raw_records")
def raw_records(stream: str = Query(..., min_length=1, max_length=96), limit: int = Query(50, ge=1, le=100),
    window=Depends(period), user_id: str = Depends(require_scope("source_raw"))):
    return get_vendor_query().records(user_id, stream, *window, limit=limit)


@router.get("/raw-records/{record_id}", operation_id="get_zepp_raw_record")
def raw_record(record_id: str, user_id: str = Depends(require_scope("source_raw"))):
    try:
        result = get_vendor_query().record(user_id, record_id)
    except ValueError as exc:
        raise HTTPException(413, "raw response exceeds public read budget") from exc
    if result is None:
        raise HTTPException(404, "raw record not found")
    return {"state": "ready", **result}
