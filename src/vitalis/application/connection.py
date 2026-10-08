"""Grouped source-connection use cases.

The service owns connection state transitions and transaction boundaries.  Vendor
providers perform parsing and network work; persistence adapters implement the
small repository operations exposed by ``application.ports``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any, Callable

from .ports import (
    BrowserLinkClaim,
    CredentialInput,
    CredentialProvider,
    PairingClaim,
    SourceAccountRepository,
    SourceClaim,
    SourceSyncCreator,
    UnitOfWorkPort,
)


class ConnectionOperationError(RuntimeError):
    """Safe application error with a stable vendor-independent classification."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        needs_reauth: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = "auth" if needs_reauth else kind
        self.needs_reauth = self.kind == "auth"
        self.retry_after = retry_after


class PairingRateLimited(ConnectionOperationError):
    """The one-time pairing code has exhausted its bounded attempt window."""


class PairingBusy(ConnectionOperationError):
    """Another request currently owns the pairing processing lease."""


class SourceConnectionConflict(ConnectionOperationError):
    """A user, account, credential generation, or one-time claim changed."""


@dataclass(frozen=True)
class PairingStatus:
    user_id: str
    status: str
    message: str
    expires_at: datetime
    sync_attempt_id: str | None
    sync_status: str | None


@dataclass(frozen=True)
class ConnectionStatus:
    user_id: str
    authorized: bool
    source_account_status: str | None
    source_user_id: str | None = None
    region_host: str | None = None
    scope: str | None = None
    expires_at: datetime | None = None
    expired: bool = False
    connection_status: str = "disconnected"
    needs_login: bool = True
    connection_message: str = ""
    last_verified_at: datetime | None = None
    last_sync_at: datetime | None = None
    sync_attempt_id: str | None = None
    sync_status: str | None = None

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "authorized": self.authorized,
            "connection_status": self.connection_status,
            "source_account_status": self.source_account_status,
            "needs_login": self.needs_login,
            **({
                "source_user_id": self.source_user_id,
                "region_host": self.region_host,
                "scope": self.scope,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "expired": self.expired,
                "connection_message": self.connection_message,
                "last_verified_at": (
                    self.last_verified_at.isoformat() + "Z"
                    if self.last_verified_at else None
                ),
                "last_sync_at": (
                    self.last_sync_at.isoformat() + "Z"
                    if self.last_sync_at else None
                ),
                "sync_attempt_id": self.sync_attempt_id,
                "sync_status": self.sync_status,
                "attempt_status": self.sync_status,
            } if self.authorized else {
                "detail": (
                    "Zepp 数据源账号已撤销，请重新配对"
                    if self.source_account_status == "revoked"
                    else "尚未连接 Zepp，请打开登录配对页"
                ),
            }),
        }


class ConnectionService:
    """Coordinate one vendor connection without importing concrete adapters."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWorkPort],
        provider: CredentialProvider,
        *,
        sync_creator: SourceSyncCreator | None = None,
        pairing_ttl_minutes: int = 10,
        pairing_processing_lease_seconds: int = 120,
        pairing_rate_limit_attempts: int = 12,
        pairing_rate_limit_window_seconds: int = 60,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self.provider = provider
        self.sync_creator = sync_creator
        self.pairing_ttl_minutes = pairing_ttl_minutes
        self.pairing_processing_lease_seconds = pairing_processing_lease_seconds
        self.pairing_rate_limit_attempts = pairing_rate_limit_attempts
        self.pairing_rate_limit_window_seconds = pairing_rate_limit_window_seconds
        self._now = now_factory or (lambda: datetime.now(timezone.utc))

    @property
    def source(self) -> str:
        return getattr(self.provider, "source", "zepp")

    def _claim_source(self, user_id: str) -> SourceClaim:
        with self._unit_of_work_factory() as unit_of_work:
            claim = unit_of_work.repository.claim_source_account(user_id, self.source)
            unit_of_work.commit()
        return claim

    def _ensure_vendor_identity_available(
        self, user_id: str, credentials: CredentialInput
    ) -> None:
        with self._unit_of_work_factory() as unit_of_work:
            if unit_of_work.repository.source_identity_owned_by_other(
                user_id, self.source, credentials.vendor_user_id
            ):
                raise SourceConnectionConflict(
                    "Zepp 账号已被其他用户绑定", kind="identity_conflict"
                )
            unit_of_work.commit()

    def _attempt(
        self,
        repository: SourceAccountRepository,
        user_id: str,
        *,
        days: int,
        trigger: str,
        trigger_ref: str | None = None,
    ) -> Any | None:
        if self.sync_creator is None:
            return None
        return self.sync_creator(
            user_id,
            days=days,
            trigger=trigger,
            trigger_ref=trigger_ref,
            repository=repository,
        )

    @staticmethod
    def _attempt_id(attempt: Any) -> str | None:
        if attempt is None:
            return None
        return attempt.get("id") if isinstance(attempt, dict) else getattr(attempt, "id", None)

    @staticmethod
    def _attempt_status(attempt: Any) -> str | None:
        if attempt is None:
            return None
        return attempt.get("status") if isinstance(attempt, dict) else getattr(attempt, "status", None)

    @staticmethod
    def _provider_error(error: Exception, *, fallback: str) -> ConnectionOperationError:
        kind = getattr(error, "kind", "unknown")
        if kind == "unknown" and error.__class__.__name__ == "SourceIdentityConflict":
            kind = "identity_conflict"
        needs_reauth = bool(getattr(error, "needs_reauth", False))
        return ConnectionOperationError(
            fallback,
            kind=kind,
            needs_reauth=needs_reauth,
        )

    @staticmethod
    def _identity_conflict(error: Exception) -> SourceConnectionConflict | None:
        if (
            getattr(error, "kind", None) == "identity_conflict"
            or error.__class__.__name__ == "SourceIdentityConflict"
        ):
            return SourceConnectionConflict(
                "Zepp 账号已被其他用户绑定",
                kind="identity_conflict",
            )
        return None

    def authorize(self, user_id: str) -> dict:
        try:
            url, state = self.provider.authorize_url()
        except Exception as exc:
            raise self._provider_error(exc, fallback="无法发起 Zepp 授权") from exc
        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.repository.save_oauth_state(state, user_id, self.source)
            unit_of_work.commit()
        return {
            "status": "scan_required",
            "user_id": user_id,
            "authorize_url": url,
            "state": state,
            "hint": "用 Zepp App 扫描二维码授权；或在浏览器打开该地址登录授权",
        }

    def authorize_url_for(self, state: str) -> str:
        try:
            return self.provider.authorize_url_for(state)
        except Exception as exc:
            raise self._provider_error(exc, fallback="授权状态无效") from exc

    def oauth_state_exists(self, state: str) -> bool:
        with self._unit_of_work_factory() as unit_of_work:
            return bool(unit_of_work.repository.oauth_state_exists(state))

    def complete_oauth(self, code: str, state: str) -> tuple[str, Any, Any | None]:
        """Consume state, exchange outside SQL, then commit guarded credentials."""
        with self._unit_of_work_factory() as unit_of_work:
            user_id = unit_of_work.repository.consume_oauth_state(state)
            if user_id is None:
                raise ConnectionOperationError(
                    "state 无效或已被使用，请重新发起扫码", kind="invalid_request"
                )
            try:
                claim = unit_of_work.repository.claim_source_account(user_id, self.source)
            except Exception as exc:
                conflict = self._identity_conflict(exc)
                if conflict is None:
                    raise
                raise conflict from exc
            unit_of_work.commit()

        try:
            token = self.provider.exchange_code(user_id, code, state)
        except Exception as exc:
            raise self._provider_error(exc, fallback="Zepp 授权交换失败") from exc

        try:
            with self._unit_of_work_factory() as unit_of_work:
                repository = unit_of_work.repository
                if not repository.source_claim_current(claim):
                    raise SourceConnectionConflict("数据源账号在授权期间发生变化", kind="conflict")
                repository.save_token(
                    token,
                    allow_create_user=False,
                    allow_reactivate=False,
                    expected_account_id=claim.account_id,
                    expected_fence_epoch=claim.fence_epoch,
                )
                attempt = self._attempt(
                    repository, user_id, days=7, trigger="oauth_callback", trigger_ref=state
                )
                unit_of_work.commit()
        except SourceConnectionConflict:
            raise
        except Exception as exc:
            conflict = self._identity_conflict(exc)
            if conflict is not None:
                raise conflict from exc
            raise
        return user_id, token, attempt

    def create_pairing(self, user_id: str, sync_days: int = 30) -> dict:
        pairing_id = secrets.token_urlsafe(24)
        expires_at = self._now() + timedelta(minutes=max(1, self.pairing_ttl_minutes))
        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.repository.create_pairing_session(
                pairing_id, user_id, expires_at, sync_days
            )
            unit_of_work.commit()
        return {
            "status": "waiting",
            "pairing_code": pairing_id,
            "expires_at": expires_at.isoformat() + "Z",
            "scan_url": f"/api/connect/zepp/scan?code={pairing_id}",
            "submit_path": f"/api/connect/zepp/pair/{pairing_id}/credentials",
        }

    def pairing_status(self, pairing_id: str, user_id: str | None = None) -> PairingStatus:
        with self._unit_of_work_factory() as unit_of_work:
            row = unit_of_work.repository.pairing_session(pairing_id)
            if row is None or (user_id is not None and row.user_id != user_id):
                raise ConnectionOperationError("配对会话不存在", kind="not_found")
            now = self._now().replace(tzinfo=None)
            status = row.status
            message = row.message
            if user_id is None and row.expires_at <= now:
                raise ConnectionOperationError("配对码已过期", kind="expired")
            if status in {"waiting", "failed", "processing"} and row.expires_at <= now:
                status = "expired"
                message = "配对码已过期，请刷新页面重试"
            attempt_status = None
            if row.sync_attempt_id:
                attempt = unit_of_work.repository.sync_attempt(
                    row.sync_attempt_id, user_id=user_id
                )
                attempt_status = getattr(attempt, "status", None) if attempt else None
            return PairingStatus(
                user_id=row.user_id,
                status=status,
                message=message,
                expires_at=row.expires_at,
                sync_attempt_id=row.sync_attempt_id,
                sync_status=attempt_status,
            )

    def claim_pairing(self, pairing_id: str) -> PairingClaim:
        with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.repository
            token = repository.claim_pairing_session(
                pairing_id,
                self.pairing_processing_lease_seconds,
                rate_limit_attempts=self.pairing_rate_limit_attempts,
                rate_window_seconds=self.pairing_rate_limit_window_seconds,
            )
            if token is None:
                retry_after = repository.pairing_retry_after(
                    pairing_id,
                    self.pairing_rate_limit_attempts,
                    self.pairing_rate_limit_window_seconds,
                )
                if retry_after is not None:
                    raise PairingRateLimited(
                        "配对尝试过于频繁，请稍后重试",
                        kind="rate_limited",
                        retry_after=retry_after,
                    )
                raise PairingBusy("配对正在处理中，请稍候", kind="busy")
            claim = repository.pairing_claim(pairing_id, token)
            if claim is None:
                raise PairingBusy("配对声明已失效，请重试", kind="busy")
            unit_of_work.commit()
            return claim

    def _fail_pairing(self, claim: PairingClaim, message: str) -> None:
        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.repository.fail_pairing_session(
                claim.pairing_id, claim.processing_token, message
            )
            unit_of_work.commit()

    def submit_pairing(self, pairing_id: str, raw_cookie: str) -> dict:
        claim = self.claim_pairing(pairing_id)
        credentials = self.provider.parse_cookie(raw_cookie)
        if credentials is None:
            self._fail_pairing(claim, "官方登录 Cookie 中没有可用的 userid/apptoken")
            raise ConnectionOperationError(
                "未读到有效 Zepp 登录，请在官方页面登录完成后重试",
                kind="invalid_request",
            )
        try:
            self._ensure_vendor_identity_available(claim.user_id, credentials)
            token = self.provider.verify_credentials(
                claim.user_id,
                credentials,
                saved_host=None,
            )
        except Exception as exc:
            error = self._provider_error(exc, fallback="Zepp 凭据验证失败，请重新登录后重试")
            self._fail_pairing(claim, "Zepp 账号已被其他用户绑定" if error.kind == "identity_conflict" else str(error))
            raise error from exc

        browser_link_token = secrets.token_urlsafe(32)
        link_digest = _token_digest(browser_link_token)
        try:
            with self._unit_of_work_factory() as unit_of_work:
                repository = unit_of_work.repository
                if not repository.lock_pairing_claim(
                    claim.pairing_id,
                    claim.user_id,
                    claim.processing_token,
                    processing_epoch=claim.processing_epoch,
                ):
                    raise SourceConnectionConflict("配对声明已被接管、删除或过期", kind="conflict")
                repository.save_token(
                    token,
                    allow_create_user=False,
                    allow_reactivate=False,
                    expected_account_id=claim.source_claim.account_id,
                    expected_fence_epoch=claim.source_claim.fence_epoch,
                )
                attempt = self._attempt(
                    repository,
                    claim.user_id,
                    days=claim.sync_days,
                    trigger="pairing_initial",
                    trigger_ref=f"{claim.pairing_id}|{link_digest}",
                )
                attempt_id = self._attempt_id(attempt)
                if not repository.finish_pairing_session(
                    claim.pairing_id,
                    claim.processing_token,
                    "Zepp 已连接，云端正在同步",
                    attempt_id,
                ):
                    raise SourceConnectionConflict("配对声明已被接管或过期", kind="conflict")
                repository.create_browser_link(link_digest, claim.user_id, attempt_id)
                unit_of_work.commit()
        except SourceConnectionConflict:
            self._fail_pairing(claim, "Zepp 凭据验证失败，请重新登录后重试")
            raise
        except Exception as exc:
            self._fail_pairing(claim, "Zepp 凭据验证失败，请重新登录后重试")
            conflict = self._identity_conflict(exc)
            if conflict is not None:
                raise conflict from exc
            raise
        return {
            "status": "connected",
            "message": "凭据已安全交给 Vitalis，云端同步已启动",
            "browser_link_token": browser_link_token,
            "sync_attempt_id": attempt_id,
            "sync_status": "queued" if attempt_id else None,
        }

    def import_token(
        self,
        user_id: str,
        *,
        cookie: str = "",
        vendor_user_id: str = "",
        app_token: str = "",
        region_hint: str | None = None,
        saved_host: str | None = None,
        sync_history: bool = True,
        sync_days: int = 14,
    ) -> tuple[Any, Any | None]:
        claim = self._claim_source(user_id)
        credentials = self.provider.parse_cookie(cookie) if cookie.strip() else None
        if credentials is None:
            credentials = CredentialInput(
                vendor_user_id=vendor_user_id.strip(),
                app_token=app_token.strip(),
                region_hint=region_hint,
            )
        if not credentials.vendor_user_id or not credentials.app_token:
            raise ConnectionOperationError(
                "缺少 user_id 和 app_token；请粘贴 cookie 值或分别填写",
                kind="invalid_request",
            )
        try:
            self._ensure_vendor_identity_available(user_id, credentials)
            token = self.provider.verify_credentials(
                user_id, credentials, saved_host=saved_host
            )
        except Exception as exc:
            raise self._provider_error(
                exc, fallback="Zepp token 验证失败，请确认凭据与区域正确"
            ) from exc
        try:
            with self._unit_of_work_factory() as unit_of_work:
                repository = unit_of_work.repository
                if not repository.source_claim_current(claim):
                    raise SourceConnectionConflict("数据源账号在验证期间发生变化", kind="conflict")
                repository.save_token(
                    token,
                    allow_create_user=False,
                    allow_reactivate=False,
                    expected_account_id=claim.account_id,
                    expected_fence_epoch=claim.fence_epoch,
                )
                attempt = (
                    self._attempt(repository, user_id, days=sync_days, trigger="token_import")
                    if sync_history else None
                )
                unit_of_work.commit()
        except SourceConnectionConflict:
            raise
        except Exception as exc:
            conflict = self._identity_conflict(exc)
            if conflict is not None:
                raise conflict from exc
            raise
        return token, attempt

    @staticmethod
    def _normalize_link_token(raw_token: str) -> str:
        scheme, separator, token = raw_token.partition(" ")
        if separator and scheme.lower() == "bearer":
            raw_token = token
        if len(raw_token) < 32 or raw_token.strip() != raw_token or " " in raw_token:
            raise ConnectionOperationError("浏览器链接令牌无效", kind="revoked")
        return raw_token

    def resolve_link(self, raw_token: str) -> tuple[str, str]:
        raw_token = self._normalize_link_token(raw_token)
        digest = _token_digest(raw_token)
        with self._unit_of_work_factory() as unit_of_work:
            row = unit_of_work.repository.browser_link(digest)
            if row is None or row.revoked_at is not None:
                raise ConnectionOperationError("浏览器链接令牌无效或已撤销", kind="revoked")
            return digest, row.user_id

    def _claim_link(self, raw_token: str) -> tuple[str, BrowserLinkClaim]:
        raw_token = self._normalize_link_token(raw_token)
        digest = _token_digest(raw_token)
        with self._unit_of_work_factory() as unit_of_work:
            claim = unit_of_work.repository.claim_browser_link(digest)
            if claim is None:
                raise ConnectionOperationError("浏览器链接令牌无效或已撤销", kind="revoked")
            unit_of_work.commit()
        return digest, claim

    def update_link(self, raw_token: str, raw_cookie: str) -> dict:
        digest, claim = self._claim_link(raw_token)
        credentials = self.provider.parse_cookie(raw_cookie)
        if credentials is None:
            with self._unit_of_work_factory() as unit_of_work:
                if unit_of_work.repository.lock_browser_link(claim):
                    unit_of_work.repository.mark_browser_link_reauth(
                        digest, "未读到有效 Zepp 登录，请重新登录"
                    )
                    unit_of_work.commit()
            raise ConnectionOperationError(
                "未读到有效 Zepp 登录，请重新登录", kind="auth", needs_reauth=True
            )
        try:
            self._ensure_vendor_identity_available(claim.user_id, credentials)
            token = self.provider.verify_credentials(
                claim.user_id,
                credentials,
                saved_host=None,
            )
        except Exception as exc:
            error = self._provider_error(exc, fallback="Zepp 登录已失效，请重新登录")
            if error.needs_reauth:
                with self._unit_of_work_factory() as unit_of_work:
                    if unit_of_work.repository.lock_browser_link(claim):
                        unit_of_work.repository.mark_browser_link_reauth(
                            digest, "Zepp 登录已失效，请重新登录"
                        )
                        unit_of_work.commit()
            raise error from exc

        try:
            with self._unit_of_work_factory() as unit_of_work:
                repository = unit_of_work.repository
                if not repository.lock_browser_link(claim):
                    raise ConnectionOperationError("浏览器链接已撤销，请重新配对", kind="revoked")
                changed = repository.save_token(
                    token,
                    allow_create_user=False,
                    allow_reactivate=False,
                    expected_account_id=claim.source_claim.account_id,
                    expected_fence_epoch=claim.source_claim.fence_epoch,
                )
                attempt = (
                    self._attempt(repository, claim.user_id, days=7, trigger="link_refresh", trigger_ref=digest)
                    if changed else None
                )
                if attempt is not None:
                    repository.attach_browser_link_attempt(digest, self._attempt_id(attempt))
                repository.mark_browser_link_verified(
                    digest, "登录凭据已更新" if changed else "登录状态有效"
                )
                unit_of_work.commit()
        except ConnectionOperationError:
            raise
        except Exception as exc:
            conflict = self._identity_conflict(exc)
            if conflict is not None:
                raise conflict from exc
            raise
        return {
            "status": "connected",
            "credential_updated": changed,
            "message": "登录凭据已更新，正在同步数据" if changed else "登录状态有效",
            "sync_attempt_id": self._attempt_id(attempt),
            "sync_status": "queued" if attempt is not None else None,
        }

    def disconnect_link(self, raw_token: str, reason: str) -> dict:
        digest, claim = self._claim_link(raw_token)
        with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.repository
            if not repository.lock_browser_link(claim):
                raise ConnectionOperationError("浏览器链接令牌无效或已撤销", kind="revoked")
            repository.mark_browser_link_reauth(digest, reason)
            unit_of_work.commit()
        return {"status": "needs_login", "message": reason}

    def _verify_saved(self, token: Any) -> None:
        self.provider.verify_saved(token)

    def validate_link(self, raw_token: str) -> dict:
        digest, claim = self._claim_link(raw_token)
        with self._unit_of_work_factory() as unit_of_work:
            token = unit_of_work.repository.get_token(claim.user_id, self.source)
            unit_of_work.commit()
        if token is None:
            raise ConnectionOperationError("Zepp 凭据不存在", kind="auth", needs_reauth=True)
        try:
            self._verify_saved(token)
        except Exception as exc:
            error = self._provider_error(exc, fallback="Zepp 服务暂时不可用，已保留当前连接，请稍后重试")
            if error.needs_reauth:
                with self._unit_of_work_factory() as unit_of_work:
                    if unit_of_work.repository.lock_browser_link(claim):
                        unit_of_work.repository.mark_browser_link_reauth(
                            digest, "Zepp 云端凭据已失效，请重新登录"
                        )
                        unit_of_work.commit()
            raise error from exc
        with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.repository
            if not repository.lock_browser_link(claim):
                raise ConnectionOperationError("浏览器链接令牌无效或已撤销", kind="revoked")
            repository.mark_browser_link_verified(digest, "云端登录凭据仍然有效")
            unit_of_work.commit()
        return {"status": "connected", "message": "云端登录凭据仍然有效"}

    def token_status(self, user_id: str) -> ConnectionStatus:
        with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.repository
            token = repository.get_token(user_id, self.source)
            account = repository.source_account_status(user_id, self.source)
            link = repository.latest_browser_link(user_id)
            attempt = (
                repository.sync_attempt(link.sync_attempt_id, user_id=user_id)
                if link and link.sync_attempt_id else None
            )
            status = getattr(attempt, "status", None) if attempt else None
            if token is None:
                return ConnectionStatus(
                    user_id=user_id,
                    authorized=False,
                    source_account_status=account["status"] if account else None,
                    connection_status=account["status"] if account else "disconnected",
                )
            expired = bool(getattr(token, "expired", False))
            connection_status = (
                link.status if link else ("expired" if expired else "connected")
            )
            return ConnectionStatus(
                user_id=user_id,
                authorized=True,
                source_account_status=account["status"] if account else "active",
                source_user_id=token.source_user_id,
                region_host=token.region_host,
                scope=token.scope,
                expires_at=token.expires_at,
                expired=expired,
                connection_status=connection_status,
                needs_login=bool(link and link.status == "needs_login") or expired,
                connection_message=link.message if link else "凭据已保存",
                last_verified_at=link.last_verified_at if link else None,
                last_sync_at=link.last_sync_at if link else None,
                sync_attempt_id=link.sync_attempt_id if link else None,
                sync_status=status,
            )


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
