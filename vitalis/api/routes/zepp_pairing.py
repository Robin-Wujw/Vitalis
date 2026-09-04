"""One-time browser-to-cloud pairing for Zepp credentials.

The cloud never receives the Zepp password. A user-controlled browser extension
reads the official login cookie and sends it through this short-lived channel.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from vitalis.api.deps import require_user_id
from vitalis.config import settings
from vitalis.connectors import get_connector
from vitalis.connectors.zepp import ZeppAuthError, ZeppConnector
from vitalis.connectors.zepp.auth_parser import extract_from_login_info
from vitalis.models import MetricSample, User
from vitalis.storage import HealthRepository, session_scope

router = APIRouter(prefix="/connect/zepp", tags=["connect"])


class PairingCredentials(BaseModel):
    cookie: str = Field(min_length=2, max_length=64 * 1024)


class DisconnectNotice(BaseModel):
    reason: str = Field(default="Zepp 网页登录已失效，请重新登录", max_length=512)


class DeviceHeartRateBatch(BaseModel):
    protocol_version: Literal[2]
    samples: list[object] = Field(min_length=1, max_length=1000)


_DEVICE_SAMPLE_ID = re.compile(
    r"^z2:(\d{13}):(\d{1,3}):([a-z0-9]{4,32}):(\d+)$"
)


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
        sync_status = None
        if row.sync_attempt_id:
            attempt = HealthRepository(db).sync_attempt(row.sync_attempt_id, user_id=user_id)
            sync_status = attempt.status if attempt else None
        return {
            "status": status,
            "message": message,
            "expires_at": row.expires_at.isoformat() + "Z",
            "sync_attempt_id": row.sync_attempt_id,
            "sync_status": sync_status,
            "attempt_status": sync_status,
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
        if not repo.claim_pairing_session(
            pairing_id, settings.pairing_processing_lease_seconds
        ):
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
        sync_attempt_id = None
        connector_for_sync = connector
        if hasattr(connector_for_sync, "create_attempt"):
            attempt = connector_for_sync.create_attempt(
                user_id, days=sync_days, trigger="pairing_initial",
                trigger_ref=f"{pairing_id}|{link_digest}",
            )
            sync_attempt_id = attempt.id
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.finish_pairing_session(pairing_id, "Zepp 已连接，云端正在同步", sync_attempt_id)
            repo.create_browser_link(link_digest, user_id, sync_attempt_id)
    except ZeppAuthError as exc:
        _pairing_failed(pairing_id, str(exc))
        status_code = 409 if exc.kind == "identity_conflict" else 400
        detail = str(exc) if exc.kind == "identity_conflict" else "Zepp 凭据验证失败，请重新登录后重试"
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except RuntimeError as exc:
        _pairing_failed(pairing_id, str(exc))
        raise HTTPException(status_code=400, detail="Zepp 凭据验证失败，请重新登录后重试") from exc

    background_tasks.add_task(
        _initial_pairing_sync, user_id, sync_days, pairing_id, link_digest, sync_attempt_id
    )
    return {
        "status": "connected",
        "message": "凭据已安全交给 Vitalis，云端同步已启动",
        "browser_link_token": browser_link_token,
        "sync_attempt_id": sync_attempt_id,
        "sync_status": "queued" if sync_attempt_id else None,
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
                    client = connector._client_for(HealthRepository(db), User(id=user_id))
                client.verify()
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_verified(
                link_digest,
                "登录凭据已更新" if changed else "登录状态有效",
            )
    except RuntimeError as exc:
        _raise_link_validation_error(link_digest, exc)

    sync_attempt_id = None
    if changed and hasattr(connector, "create_attempt"):
        attempt = connector.create_attempt(
            user_id, days=7, trigger="link_refresh", trigger_ref=link_digest
        )
        sync_attempt_id = attempt.id
        with session_scope() as db:
            link = HealthRepository(db).browser_link(link_digest)
            if link:
                link.sync_attempt_id = sync_attempt_id
    if changed:
        background_tasks.add_task(_linked_incremental_sync, user_id, link_digest, sync_attempt_id)
    return {
        "status": "connected",
        "credential_updated": changed,
        "message": "登录凭据已更新，正在同步数据" if changed else "登录状态有效",
        "sync_attempt_id": sync_attempt_id,
        "sync_status": "queued" if sync_attempt_id else None,
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


@router.post("/link/validate", summary="验证云端已保存的 Zepp 凭据")
def validate_linked_credentials(
    authorization: str = Header(default="", alias="Authorization"),
) -> dict:
    """Use Zepp's server response, not browser cookie visibility, as expiry evidence."""
    link_digest, user_id = _linked_user(authorization)
    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    try:
        with session_scope() as db:
            repo = HealthRepository(db)
            if repo.get_token(user_id, "zepp") is None:
                raise ZeppAuthError("Zepp 凭据不存在", kind="auth")
            client = connector._client_for(repo, User(id=user_id))
        client.verify()
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_verified(link_digest, "云端登录凭据仍然有效")
    except RuntimeError as exc:
        _raise_link_validation_error(link_digest, exc)
    return {"status": "connected", "message": "云端登录凭据仍然有效"}


@router.post("/device-link", summary="创建 Balance 2 Zepp OS 上传链接")
def create_zepp_device_link(user_id: str = Depends(require_user_id)) -> dict:
    token = secrets.token_urlsafe(32)
    digest = _token_digest(token)
    with session_scope() as db:
        HealthRepository(db).create_device_link(digest, user_id)
    return {
        "status": "created",
        "device": "Balance 2",
        "upload_path": "/api/v1/connect/zepp/device-link/heart-rate",
        "device_link_token": token,
        "message": "上传令牌只显示一次，请配置到 Zepp App 中的 Vitalis Bridge 设置",
    }


@router.post("/device-link/heart-rate", summary="逐条结算 Balance 2 心率回调批次")
def upload_zepp_device_heart_rate(
    body: DeviceHeartRateBatch,
    authorization: str = Header(default="", alias="Authorization"),
) -> dict:
    digest, user_id, device_label = _device_linked_user(authorization)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    minimum_ms = now_ms - 31 * 24 * 60 * 60 * 1000
    maximum_ms = now_ms + 5 * 60 * 1000

    sample_id_counts: dict[str, int] = {}
    for raw in body.samples:
        if not isinstance(raw, dict):
            continue
        sample_id = raw.get("sample_id")
        if isinstance(sample_id, str) and len(sample_id) <= 128 and _DEVICE_SAMPLE_ID.fullmatch(sample_id):
            sample_id_counts[sample_id] = sample_id_counts.get(sample_id, 0) + 1

    rejected: list[dict] = []
    accepted_entries: list[tuple[str, int, int, int]] = []
    seen_sample_keys: set[tuple[int, int]] = set()
    reported_duplicate_ids: set[str] = set()
    for index, raw in enumerate(body.samples):
        if not isinstance(raw, dict):
            rejected.append(_device_sample_rejection(index, None, "invalid_sample"))
            continue
        raw_sample_id = raw.get("sample_id")
        sample_id = (
            raw_sample_id
            if (
                isinstance(raw_sample_id, str)
                and len(raw_sample_id) <= 128
                and _DEVICE_SAMPLE_ID.fullmatch(raw_sample_id)
            )
            else None
        )
        if sample_id is None:
            rejected.append(_device_sample_rejection(index, None, "invalid_sample_id"))
            continue
        if sample_id_counts[sample_id] > 1:
            if sample_id not in reported_duplicate_ids:
                rejected.append(
                    _device_sample_rejection(index, sample_id, "duplicate_sample_id")
                )
                reported_duplicate_ids.add(sample_id)
            continue

        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            rejected.append(_device_sample_rejection(index, sample_id, "invalid_timestamp"))
            continue
        sample_ordinal = raw.get("sample_ordinal")
        if (
            not isinstance(sample_ordinal, int)
            or isinstance(sample_ordinal, bool)
            or not 0 <= sample_ordinal < 1000
        ):
            rejected.append(
                _device_sample_rejection(index, sample_id, "invalid_sample_ordinal")
            )
            continue
        identity = _DEVICE_SAMPLE_ID.fullmatch(sample_id)
        if (
            identity is None
            or int(identity.group(1)) != timestamp
            or int(identity.group(2)) != sample_ordinal
        ):
            rejected.append(
                _device_sample_rejection(index, sample_id, "sample_identity_mismatch")
            )
            continue
        heart_rate = raw.get("heart_rate")
        if not isinstance(heart_rate, int) or isinstance(heart_rate, bool):
            rejected.append(_device_sample_rejection(index, sample_id, "invalid_heart_rate"))
            continue
        if timestamp < minimum_ms:
            rejected.append(_device_sample_rejection(index, sample_id, "timestamp_too_old"))
            continue
        if timestamp > maximum_ms:
            rejected.append(_device_sample_rejection(index, sample_id, "timestamp_too_future"))
            continue
        if not 20 <= heart_rate <= 240:
            rejected.append(_device_sample_rejection(index, sample_id, "heart_rate_out_of_range"))
            continue
        sample_key = (timestamp, sample_ordinal)
        if sample_key in seen_sample_keys:
            rejected.append(_device_sample_rejection(index, sample_id, "duplicate_sample_key"))
            continue
        seen_sample_keys.add(sample_key)
        accepted_entries.append((sample_id, timestamp, sample_ordinal, heart_rate))

    normalized = [
        MetricSample(
            user_id=user_id,
            source="zepp_os",
            metric="heart_rate",
            timestamp=(
                datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                + timedelta(microseconds=sample_ordinal)
            ),
            value=heart_rate,
            unit="bpm",
            source_scope="device_callback",
            device_id=device_label,
        )
        for _, timestamp, sample_ordinal, heart_rate in accepted_entries
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        if normalized:
            repo.save_metric_samples(normalized)
        repo.mark_device_link_seen(digest)

    acknowledged = [
        {
            "sample_id": sample_id,
            "timestamp": timestamp,
            "sample_ordinal": sample_ordinal,
        }
        for sample_id, timestamp, sample_ordinal, _ in accepted_entries
    ]
    return {
        "protocol_version": 2,
        "status": "processed",
        "received_count": len(body.samples),
        "acknowledged_count": len(acknowledged),
        "rejected_count": len(rejected),
        "acknowledged": acknowledged,
        "rejected": rejected,
    }


def _device_sample_rejection(index: int, sample_id: str | None, code: str) -> dict:
    return {
        "index": index,
        "sample_id": sample_id,
        "code": code,
        "retryable": False,
    }


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _raise_link_validation_error(link_digest: str, error: RuntimeError) -> None:
    if isinstance(error, ZeppAuthError) and error.needs_reauth:
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_reauth(
                link_digest, "Zepp 云端凭据已失效，请重新登录"
            )
        raise HTTPException(status_code=400, detail="Zepp 登录已失效，请重新登录") from error
    if isinstance(error, ZeppAuthError) and error.kind == "identity_conflict":
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise HTTPException(
        status_code=503,
        detail="Zepp 服务暂时不可用，已保留当前连接，请稍后重试",
    ) from error


def _authorization_digest(authorization: str, detail: str) -> str:
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or len(token) < 32:
        raise HTTPException(status_code=401, detail=detail)
    return _token_digest(token)


def _linked_user(authorization: str) -> tuple[str, str]:
    digest = _authorization_digest(authorization, "浏览器链接令牌无效")
    with session_scope() as db:
        row = HealthRepository(db).browser_link(digest)
        if row is None or row.revoked_at is not None:
            raise HTTPException(status_code=401, detail="浏览器链接令牌无效或已撤销")
        return digest, row.user_id


def _device_linked_user(authorization: str) -> tuple[str, str, str]:
    digest = _authorization_digest(authorization, "设备上传令牌无效")
    with session_scope() as db:
        row = HealthRepository(db).device_link(digest)
        if row is None or row.revoked_at is not None:
            raise HTTPException(status_code=401, detail="设备上传令牌无效或已撤销")
        return digest, row.user_id, row.device_label


def _initial_pairing_sync(
    user_id: str, sync_days: int, pairing_id: str, link_digest: str,
    sync_attempt_id: str | None = None,
) -> None:
    sync_succeeded = False
    needs_reauth = False
    try:
        connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
        if sync_attempt_id and not getattr(connector, "mock", False):
            from vitalis.services.zepp_sync_coordinator import ZeppSyncCoordinator

            ZeppSyncCoordinator(connector=connector).run_attempt(
                sync_attempt_id, max_chunks=1
            )
            return
        else:
            with session_scope() as db:
                report = connector.sync_with_report(
                    User(id=user_id), days=sync_days, repo=HealthRepository(db),
                    attempt_id=sync_attempt_id,
                )
        sync_succeeded = report.success
        needs_reauth = report.needs_reauth
        message = (
            f"Zepp 已连接，首次同步写入 {report.records_written} 条记录"
            if sync_succeeded
            else (
                "Zepp 登录已失效，请重新登录"
                if needs_reauth
                else "Zepp 已连接，但首次同步不完整，将稍后重试"
            )
        )
    except ZeppAuthError as exc:
        needs_reauth = exc.needs_reauth
        message = (
            f"Zepp 登录已失效，请重新登录：{exc}"
            if needs_reauth
            else "Zepp 已连接，但首次同步失败，将稍后重试"
        )
    except Exception:
        message = "Zepp 已连接，但首次同步失败，将稍后重试"
    with session_scope() as db:
        repo = HealthRepository(db)
        row = repo.pairing_session(pairing_id)
        if row and row.status == "connected":
            row.message = message[:512]
        if sync_succeeded:
            repo.mark_browser_link_synced(link_digest, message)
        elif needs_reauth:
            repo.mark_browser_link_reauth(link_digest, message)
        else:
            repo.mark_browser_link_sync_failed(link_digest, message)


def _linked_incremental_sync(
    user_id: str, link_digest: str, sync_attempt_id: str | None = None
) -> None:
    try:
        connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
        if sync_attempt_id and not getattr(connector, "mock", False):
            from vitalis.services.zepp_sync_coordinator import ZeppSyncCoordinator

            ZeppSyncCoordinator(connector=connector).run_attempt(
                sync_attempt_id, max_chunks=1
            )
            return
        else:
            with session_scope() as db:
                report = connector.sync_with_report(
                    User(id=user_id), days=7, repo=HealthRepository(db),
                    attempt_id=sync_attempt_id,
                )
        message = (
            f"登录凭据已更新，同步写入 {report.records_written} 条记录"
            if report.success
            else "登录凭据有效，但数据同步不完整，将稍后重试"
        )
        with session_scope() as db:
            repo = HealthRepository(db)
            if report.success:
                repo.mark_browser_link_synced(link_digest, message)
            elif report.needs_reauth:
                repo.mark_browser_link_reauth(
                    link_digest, "同步验证失败，请重新登录"
                )
            else:
                repo.mark_browser_link_sync_failed(link_digest, message)
    except ZeppAuthError as exc:
        with session_scope() as db:
            repo = HealthRepository(db)
            if exc.needs_reauth:
                repo.mark_browser_link_reauth(
                    link_digest, f"同步验证失败，请重新登录：{exc}"
                )
            else:
                repo.mark_browser_link_sync_failed(
                    link_digest, "登录状态有效，但数据同步失败，将稍后重试"
                )
    except Exception:
        with session_scope() as db:
            HealthRepository(db).mark_browser_link_sync_failed(
                link_digest, "登录状态有效，但数据同步失败，将稍后重试"
            )
