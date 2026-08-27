"""One-time browser-to-cloud pairing for Zepp credentials.

The cloud never receives the Zepp password. A user-controlled browser extension
reads the official login cookie and sends it through this short-lived channel.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from vitalis.api.deps import require_user_id
from vitalis.config import settings
from vitalis.connectors import get_connector
from vitalis.connectors.zepp import ZeppAuthError, ZeppConnector
from vitalis.connectors.zepp.auth_parser import extract_from_login_info
from vitalis.models import User
from vitalis.storage import HealthRepository, session_scope

router = APIRouter(prefix="/connect/zepp", tags=["connect"])


class PairingCredentials(BaseModel):
    cookie: str = Field(min_length=2, max_length=64 * 1024)


class DisconnectNotice(BaseModel):
    reason: str = Field(default="Zepp 网页登录已失效，请重新登录", max_length=512)


def create_pairing(user_id: str, sync_days: int = 30) -> dict:
    """Create a pairing session for API callers and the server-rendered page."""
    pairing_id = secrets.token_urlsafe(24)
    expires_at = datetime.utcnow() + timedelta(minutes=max(1, settings.pairing_ttl_minutes))
    with session_scope() as db:
        HealthRepository(db).create_pairing_session(pairing_id, user_id, expires_at, sync_days)
    return {
        "status": "waiting",
        "pairing_code": pairing_id,
        "expires_at": expires_at.isoformat() + "Z",
        "submit_path": f"/api/v1/connect/zepp/pair/{pairing_id}/credentials",
    }


@router.post("/pair", summary="创建 Zepp 云端配对会话")
def create_zepp_pairing(
    sync_days: int = Query(30, ge=1, le=730),
    user_id: str = Depends(require_user_id),
) -> dict:
    return {"user_id": user_id, **create_pairing(user_id, sync_days)}


@router.get("/pair/{pairing_id}", summary="查询 Zepp 配对状态")
def zepp_pairing_status(pairing_id: str, user_id: str = Depends(require_user_id)) -> dict:
    with session_scope() as db:
        row = HealthRepository(db).pairing_session(pairing_id)
        if row is None or not secrets.compare_digest(row.user_id, user_id):
            raise HTTPException(status_code=404, detail="配对会话不存在")
        status = row.status
        message = row.message
        if status not in ("connected", "expired") and row.expires_at <= datetime.utcnow():
            row.status = status = "expired"
            row.message = message = "配对码已过期，请刷新页面重试"
        return {
            "status": status,
            "message": message,
            "expires_at": row.expires_at.isoformat() + "Z",
        }


@router.post("/pair/{pairing_id}/credentials", summary="浏览器扩展提交 Zepp 登录凭据")
def submit_zepp_pairing_credentials(
    pairing_id: str,
    body: PairingCredentials,
    background_tasks: BackgroundTasks,
) -> dict:
    """Accept a cookie through a one-time bearer pairing code.

    This endpoint deliberately does not accept a user id from the extension;
    the random pairing code is already bound to the user by the cloud page.
    """
    with session_scope() as db:
        repo = HealthRepository(db)
        row = repo.pairing_session(pairing_id)
        if row is None:
            raise HTTPException(status_code=404, detail="配对会话不存在")
        if row.status == "connected":
            raise HTTPException(status_code=409, detail="配对码已使用")
        if row.expires_at <= datetime.utcnow():
            row.status = "expired"
            raise HTTPException(status_code=410, detail="配对码已过期")
        user_id = row.user_id
        sync_days = row.sync_days
        if not repo.claim_pairing_session(pairing_id):
            raise HTTPException(status_code=409, detail="配对正在处理中，请稍候")

    extracted = extract_from_login_info(body.cookie)
    if extracted is None:
        _pairing_failed(pairing_id, "官方登录 Cookie 中没有可用的 userid/apptoken")
        raise HTTPException(status_code=400, detail="未读到有效 Zepp 登录，请在官方页面登录完成后重试")

    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    try:
        if connector.mock:
            with session_scope() as db:
                connector.exchange_and_save(HealthRepository(db), user_id, "paired-mock")
        else:
            # Kept in the same validated path as manual import.
            from .connect import _probe_region_hosts

            region = _probe_region_hosts(
                extracted.user_id,
                extracted.app_token,
                extracted.region_hint,
                saved_host=None,
            )
            with session_scope() as db:
                connector.import_token(
                    HealthRepository(db),
                    user_id,
                    extracted.app_token,
                    vendor_user_id=extracted.user_id,
                    region_host=region,
                )
        browser_link_token = secrets.token_urlsafe(32)
        link_digest = _token_digest(browser_link_token)
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.finish_pairing_session(pairing_id, "Zepp 已连接，云端正在同步")
            repo.create_browser_link(link_digest, user_id)
    except (ZeppAuthError, RuntimeError) as exc:
        _pairing_failed(pairing_id, str(exc))
        raise HTTPException(status_code=400, detail="Zepp 凭据验证失败，请重新登录后重试") from exc

    background_tasks.add_task(_initial_pairing_sync, user_id, sync_days, pairing_id, link_digest)
    return {
        "status": "connected",
        "message": "凭据已安全交给 Vitalis，云端同步已启动",
        "browser_link_token": browser_link_token,
    }


@router.post("/pair/{pairing_id}/credentials/raw", summary="一键书签提交 Zepp 登录凭据", include_in_schema=False)
async def submit_zepp_pairing_credentials_raw(
    pairing_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Simple text body keeps the bookmarklet request free of CORS preflight."""
    raw = await request.body()
    try:
        cookie = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Cookie 编码无效") from exc
    return submit_zepp_pairing_credentials(
        pairing_id,
        PairingCredentials(cookie=cookie),
        background_tasks,
    )


def _pairing_failed(pairing_id: str, message: str) -> None:
    with session_scope() as db:
        HealthRepository(db).fail_pairing_session(pairing_id, message)


@router.post("/link/credentials", summary="更新 Zepp 浏览器登录凭据")
def update_linked_credentials(
    body: PairingCredentials,
    background_tasks: BackgroundTasks,
    authorization: str = Header(default="", alias="Authorization"),
) -> dict:
    """Verify a browser session update through a revocable bearer link."""
    link_digest, user_id = _linked_user(authorization)
    extracted = extract_from_login_info(body.cookie)
    if extracted is None:
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_reauth(
                link_digest, "未读到有效 Zepp 登录，请重新登录"
            )
        raise HTTPException(status_code=400, detail="未读到有效 Zepp 登录，请重新登录")

    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    changed = True
    try:
        if connector.mock:
            with session_scope() as db:
                repo = HealthRepository(db)
                current = repo.get_token(user_id, "zepp")
                changed = current is None
                if changed:
                    connector.exchange_and_save(repo, user_id, "linked-mock")
        else:
            with session_scope() as db:
                current = HealthRepository(db).get_token(user_id, "zepp")
            changed = (
                current is None
                or current.source_user_id != extracted.user_id
                or not secrets.compare_digest(current.access_token, extracted.app_token)
            )
            if changed:
                from .connect import _probe_region_hosts

                region = _probe_region_hosts(
                    extracted.user_id,
                    extracted.app_token,
                    extracted.region_hint,
                    saved_host=current.region_host if current else None,
                )
                with session_scope() as db:
                    connector.import_token(
                        HealthRepository(db),
                        user_id,
                        extracted.app_token,
                        vendor_user_id=extracted.user_id,
                        region_host=region,
                    )
            else:
                with session_scope() as db:
                    connector._client_for(HealthRepository(db), User(id=user_id)).verify()
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_verified(
                link_digest,
                "登录凭据已更新" if changed else "登录状态有效",
            )
    except (ZeppAuthError, RuntimeError) as exc:
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_reauth(link_digest, str(exc))
        raise HTTPException(status_code=400, detail="Zepp 登录已失效，请重新登录") from exc

    if changed:
        background_tasks.add_task(_linked_incremental_sync, user_id, link_digest)
    return {
        "status": "connected",
        "credential_updated": changed,
        "message": "登录凭据已更新，正在同步数据" if changed else "登录状态有效",
    }


@router.post("/link/disconnected", summary="报告 Zepp 浏览器登录断开")
def report_link_disconnected(
    body: DisconnectNotice,
    authorization: str = Header(default="", alias="Authorization"),
) -> dict:
    link_digest, _user_id = _linked_user(authorization)
    with session_scope() as db:
        HealthRepository(db).mark_browser_link_reauth(link_digest, body.reason)
    return {"status": "needs_login", "message": body.reason}


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _linked_user(authorization: str) -> tuple[str, str]:
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or len(token) < 32:
        raise HTTPException(status_code=401, detail="浏览器链接令牌无效")
    digest = _token_digest(token)
    with session_scope() as db:
        row = HealthRepository(db).browser_link(digest)
        if row is None or row.revoked_at is not None:
            raise HTTPException(status_code=401, detail="浏览器链接令牌无效或已撤销")
        return digest, row.user_id


def _initial_pairing_sync(
    user_id: str, sync_days: int, pairing_id: str, link_digest: str
) -> None:
    try:
        connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
        with session_scope() as db:
            report = connector.sync_with_report(
                User(id=user_id), days=sync_days, repo=HealthRepository(db)
            )
        message = f"Zepp 已连接，首次同步写入 {report.records_written} 条记录"
    except Exception:
        message = "Zepp 已连接，但首次同步失败，将稍后重试"
    with session_scope() as db:
        repo = HealthRepository(db)
        row = repo.pairing_session(pairing_id)
        if row and row.status == "connected":
            row.message = message[:512]
        repo.mark_browser_link_synced(link_digest, message)


def _linked_incremental_sync(user_id: str, link_digest: str) -> None:
    try:
        connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
        with session_scope() as db:
            report = connector.sync_with_report(
                User(id=user_id), days=7, repo=HealthRepository(db)
            )
        message = f"登录凭据已更新，同步写入 {report.records_written} 条记录"
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_synced(link_digest, message)
    except ZeppAuthError as exc:
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_reauth(
                link_digest, f"同步验证失败，请重新登录：{exc}"
            )
    except Exception:
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_sync_failed(
                link_digest, "登录状态有效，但数据同步失败，将稍后重试"
            )
