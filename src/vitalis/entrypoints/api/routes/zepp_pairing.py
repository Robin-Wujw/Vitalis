"""One-time browser-to-cloud pairing for Zepp credentials.

The cloud never receives the Zepp password. A user-controlled browser extension
reads the official login cookie and sends it through this short-lived channel.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from vitalis.entrypoints.api.deps import authenticated_user, require_scope
from vitalis.config import settings
from vitalis.bootstrap import get_connector, get_connection_service
from vitalis.application.connection import ConnectionOperationError

router = APIRouter(prefix="/connect/zepp", tags=["connect"])


class PairingCredentials(BaseModel):
    cookie: str = Field(min_length=2, max_length=64 * 1024)


class DisconnectNotice(BaseModel):
    reason: str = Field(default="Zepp 网页登录已失效，请重新登录", max_length=512)


def _require_pairing_origin(request: Request) -> None:
    browser_origin = request.headers.get("Origin")
    if browser_origin is not None and browser_origin not in settings.pairing_allowed_origins:
        raise HTTPException(status_code=403, detail="不允许此浏览器来源提交凭据")


def create_pairing(user_id: str, sync_days: int = 30) -> dict:
    """Create a pairing session for API callers and the server-rendered page."""
    return get_connection_service().create_pairing(user_id, sync_days)


@router.post("/pair", summary="创建 Zepp 云端配对会话")
def create_zepp_pairing(
    sync_days: int = Query(30, ge=1, le=730),
    user_id: str = Depends(require_scope("manage")),
) -> dict:
    return {"user_id": user_id, **create_pairing(user_id, sync_days)}


@router.get("/pair/{pairing_id}", summary="查询 Zepp 配对状态")
def zepp_pairing_status(
    pairing_id: str,
    authorization: str = Header(default="", alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> dict:
    # The short-lived random pairing code is a purpose-bound status credential.
    # A supplied API identity must still be authenticated and match its owner.
    user_id = (
        authenticated_user(authorization, x_user_id, "read")
        if authorization or x_user_id is not None else None
    )
    try:
        status = get_connection_service().pairing_status(pairing_id, user_id)
    except ConnectionOperationError as exc:
        raise HTTPException(status_code=410 if exc.kind == "expired" else 404, detail=str(exc)) from exc
    return {
        "status": status.status,
        "message": status.message,
        "expires_at": status.expires_at.isoformat() + "Z",
        "sync_attempt_id": status.sync_attempt_id,
        "sync_status": status.sync_status,
        "attempt_status": status.sync_status,
    }


@router.post("/pair/{pairing_id}/credentials", summary="浏览器扩展提交 Zepp 登录凭据")
def submit_zepp_pairing_credentials(
    pairing_id: str,
    body: PairingCredentials,
    request: Request,
) -> dict:
    """Accept a cookie through a one-time bearer pairing code.

    This endpoint deliberately does not accept a user id from the extension;
    the random pairing code is already bound to the user by the cloud page.
    """
    _require_pairing_origin(request)
    try:
        return get_connection_service(provider=get_connector("zepp")).submit_pairing(pairing_id, body.cookie)
    except ConnectionOperationError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        status = 429 if exc.retry_after else (
            409 if exc.kind in {"conflict", "identity_conflict", "busy"}
            else 504 if exc.kind == "timeout"
            else 503 if exc.kind in {"network", "service"}
            else 400
        )
        raise HTTPException(status_code=status, detail=str(exc), headers=headers) from exc


@router.post("/pair/{pairing_id}/credentials/raw", summary="一键书签提交 Zepp 登录凭据", include_in_schema=False)
async def submit_zepp_pairing_credentials_raw(
    pairing_id: str,
    request: Request,
) -> dict:
    """Simple text body keeps the bookmarklet request free of CORS preflight."""
    _require_pairing_origin(request)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 64 * 1024:
            raise HTTPException(status_code=413, detail="配对凭据过长")
    try:
        cookie = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Cookie 编码无效") from exc
    return submit_zepp_pairing_credentials(
        pairing_id,
        PairingCredentials(cookie=cookie),
        request,
    )


@router.post("/link/credentials", summary="更新 Zepp 浏览器登录凭据")
def update_linked_credentials(
    body: PairingCredentials,
    request: Request,
    authorization: str = Header(default="", alias="Authorization"),
) -> dict:
    """Verify a browser session update through a revocable bearer link."""
    _require_pairing_origin(request)
    try:
        return get_connection_service(provider=get_connector("zepp")).update_link(authorization, body.cookie)
    except ConnectionOperationError as exc:
        status = (
            401 if exc.kind == "revoked" else
            409 if exc.kind in {"conflict", "identity_conflict"} else
            504 if exc.kind == "timeout" else
            503 if exc.kind in {"network", "service"} else 400
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/link/disconnected", summary="报告 Zepp 浏览器登录断开")
def report_link_disconnected(
    body: DisconnectNotice,
    authorization: str = Header(default="", alias="Authorization"),
) -> dict:
    try:
        return get_connection_service(provider=get_connector("zepp")).disconnect_link(authorization, body.reason)
    except ConnectionOperationError as exc:
        raise HTTPException(
            status_code=401 if exc.kind == "revoked" else 409,
            detail=str(exc),
        ) from exc


@router.post("/link/validate", summary="验证云端已保存的 Zepp 凭据")
def validate_linked_credentials(
    authorization: str = Header(default="", alias="Authorization"),
) -> dict:
    """Use Zepp's server response, not browser cookie visibility, as expiry evidence."""
    try:
        return get_connection_service(provider=get_connector("zepp")).validate_link(authorization)
    except ConnectionOperationError as exc:
        status = (
            401 if exc.kind == "revoked" else
            400 if exc.needs_reauth else
            504 if exc.kind == "timeout" else
            503 if exc.kind in {"network", "service"} else 409
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc
