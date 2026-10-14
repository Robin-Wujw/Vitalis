"""Zepp official-web password login adapter.

The public ``user.zepp.com`` bundle currently performs two form POSTs:

1. ``api-user.huami.com/registrations/{account}/tokens`` exchanges the account
   and password for a short-lived ``access`` value.
2. ``account.huami.com/v2/client/login`` exchanges that value for the
   ``app_token``/``login_token``/``user_id`` tuple used by the Zepp web client.

``refresh`` renews the ``app_token`` with the saved ``login_token`` through the
app-token endpoint the Zepp apps use.  That endpoint is undocumented; any
failure falls back to a new phone login, never to a guessed credential.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Self
from urllib.parse import quote

import httpx

from vitalis.domain import AuthToken

API_USER_BASE = "https://api-user.huami.com"
ACCOUNT_BASE = "https://account.huami.com"
APP_NAME = "com.huami.webapp"
CLIENT_ID = "HuaMi"
DEVICE_ID = "02:00:00:00:00:00"
DEVICE_MODEL = "web"
APP_VERSION = "4.3.0"
DEFAULT_COUNTRY_CODE = "CN"
DEFAULT_DN = (
    "account.huami.com,api-user.huami.com,auth.huami.com,"
    "api-mifit.huami.com,api-open.huami.com"
)
# Zepp's open platform routes to the official login for the account's
# region (with its "forgot password"); user.zepp.com does not open from
# every network (2026-10-14).
OFFICIAL_RESET_URL = "https://console.zepp.com/"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}(?:-[A-Za-z0-9_-]{1,16})?$")
_REGION_RE = re.compile(r"^api-mifit[^.]*\.(?:zepp\.com|huami\.com)$", re.IGNORECASE)
MAX_RESPONSE_BYTES = 64 * 1024


class ZeppLoginError(RuntimeError):
    """Safe, machine-readable error from the official login exchange."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "vendor_response",
        failure_code: str = "vendor_response",
        needs_reauth: bool = False,
        retry_after: int | None = None,
        reset_url: str | None = None,
    ) -> None:
        # ``message`` is always supplied by this module as a fixed, secret-free
        # string.  Never include an upstream response body in it.
        super().__init__(message)
        self.kind = "auth" if needs_reauth else kind
        self.failure_code = failure_code
        self.needs_reauth = self.kind == "auth"
        self.retry_after = retry_after
        self.reset_url = reset_url

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(kind={self.kind!r}, "
            f"failure_code={self.failure_code!r}, "
            f"retry_after={self.retry_after!r})"
        )


@dataclass(frozen=True, kw_only=True)
class ZeppLoginResult:
    """Transient login result; secret fields are intentionally hidden in repr."""

    source_user_id: str
    region_host: str
    expires_at: datetime | None = None
    scope: str = "apptoken login_token"
    app_token: str = field(repr=False)
    login_token: str = field(repr=False)

    def to_auth_token(self, user_id: str) -> AuthToken:
        """Build the domain credential only at the persistence boundary."""
        return AuthToken(
            user_id=user_id,
            source="zepp",
            access_token=self.app_token,
            refresh_token=self.login_token,
            expires_at=self.expires_at,
            scope=self.scope,
            region_host=self.region_host,
            source_user_id=self.source_user_id,
        )


class ZeppLoginAdapter:
    """Perform the two requests made by the official Zepp web login page.

    ``client``/``transport`` are injectable so tests never contact Zepp.  The
    default client disables environment proxies because scheduled Vitalis jobs
    must not inherit interactive shell proxy settings.
    """

    source = "zepp"

    def __init__(
        self,
        *,
        client: Any | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        api_user_base: str = API_USER_BASE,
        account_base: str = ACCOUNT_BASE,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            trust_env=False,
            transport=transport,
            follow_redirects=False,
        )
        self._timeout = timeout
        self._api_user_base = api_user_base.rstrip("/")
        self._account_base = account_base.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def login(
        self,
        account: str,
        password: str,
        *,
        country_code: str = DEFAULT_COUNTRY_CODE,
    ) -> ZeppLoginResult:
        """Exchange one in-memory account/password pair for Zepp tokens."""
        account = self._validate_account(account)
        self._validate_password(password)
        country = self._validate_country(country_code)
        account_type = "email" if _EMAIL_RE.fullmatch(account) else "phone"

        first = self._post(
            self._registration_tokens_url(account),
            data={
                "country_code": country,
                "password": password,
                "name": account,
                "client_id": CLIENT_ID,
                "redirect_uri": "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html",
                "state": "REDIRECTION",
                "token": "access",
                "json_response": "true",
            },
            headers={
                "app_name": APP_NAME,
                "Accept-Language": "zh",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        first_payload = self._json_object(first)
        self._raise_for_response(first, first_payload, stage="password")
        access = _text_value(first_payload.get("access"), max_length=8192)
        if not access:
            # The official page treats this as a failed login.  Do not guess at
            # another field or try to turn an arbitrary response into a token.
            if _contains_captcha_marker(first_payload):
                raise ZeppLoginError(
                    "Zepp 要求图形验证，请先在官方页面重置密码后重试",
                    kind="auth",
                    failure_code="captcha_required",
                    needs_reauth=True,
                    reset_url=OFFICIAL_RESET_URL,
                )
            raise ZeppLoginError(
                "Zepp 登录响应缺少授权码",
                kind="vendor_response",
                failure_code="vendor_response",
            )

        second_country = _text_value(first_payload.get("country_code"), max_length=32) or country
        second = self._post(
            f"{self._account_base}/v2/client/login",
            data={
                "country_code": second_country.split("-", 1)[0],
                "app_name": APP_NAME,
                "third_name": (
                    "huami" if account_type == "email" else "huami_phone"
                ),
                "grant_type": "access_token",
                "code": access,
                "device_id": DEVICE_ID,
                "device_model": DEVICE_MODEL,
                "app_version": APP_VERSION,
                "allow_registration": "false",
                "dn": DEFAULT_DN,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        second_payload = self._json_object(second)
        self._raise_for_response(second, second_payload, stage="token")
        if second_payload.get("result") != "ok":
            if _contains_captcha_marker(second_payload):
                raise ZeppLoginError(
                    "Zepp 要求图形验证，请先在官方页面重置密码后重试",
                    kind="auth",
                    failure_code="captcha_required",
                    needs_reauth=True,
                    reset_url=OFFICIAL_RESET_URL,
                )
            raise ZeppLoginError(
                "Zepp 登录响应未确认成功",
                kind="vendor_response",
                failure_code="vendor_response",
            )

        token_info = second_payload.get("token_info")
        if not isinstance(token_info, dict):
            raise ZeppLoginError(
                "Zepp 登录响应缺少令牌信息",
                kind="vendor_response",
                failure_code="vendor_response",
            )
        app_token = _text_value(token_info.get("app_token"), max_length=16 * 1024)
        login_token = _text_value(token_info.get("login_token"), max_length=16 * 1024)
        source_user_id = _text_value(token_info.get("user_id"), max_length=256)
        if not app_token or not login_token or not source_user_id:
            raise ZeppLoginError(
                "Zepp 登录响应缺少必要令牌",
                kind="vendor_response",
                failure_code="vendor_response",
            )

        region_host = _region_from_response(second_payload)
        if not region_host:
            raise ZeppLoginError(
                "Zepp 登录响应缺少可验证区域主机",
                kind="vendor_response",
                failure_code="vendor_response",
            )
        # The public bundle exposes app_ttl but its units/expiry semantics are
        # not documented, so do not invent a persisted expiry from that field.
        return ZeppLoginResult(
            source_user_id=source_user_id,
            region_host=region_host,
            app_token=app_token,
            login_token=login_token,
        )

    def refresh(self, token: AuthToken) -> ZeppLoginResult:
        """Exchange the saved ``login_token`` for a new ``app_token``.

        Uses ``GET account.huami.com/v1/client/app_tokens``, the call the Zepp
        apps make with a stored login token.  It is not in Zepp's public
        documentation; any unexpected answer is treated as "log in again".
        """
        login_token = token.refresh_token
        if not login_token or not token.source_user_id:
            raise ZeppLoginError(
                "没有可续期的 Zepp 登录令牌，请重新登录",
                kind="auth", failure_code="reauth_required", needs_reauth=True,
                reset_url=OFFICIAL_RESET_URL,
            )
        try:
            response = self._client.get(
                f"{self._account_base}/v1/client/app_tokens",
                params={"app_name": APP_NAME, "dn": DEFAULT_DN, "login_token": login_token},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ZeppLoginError("Zepp 续期请求超时", kind="timeout", failure_code="timeout") from None
        except httpx.HTTPError:
            raise ZeppLoginError("无法连接 Zepp 续期服务", kind="network", failure_code="network_error") from None
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ZeppLoginError("Zepp 续期响应超过安全上限", kind="vendor_response", failure_code="response_too_large")
        payload = self._json_object(response)
        self._raise_for_response(response, payload, stage="token")
        info = payload.get("token_info") if isinstance(payload.get("token_info"), dict) else {}
        app_token = _text_value(info.get("app_token"), max_length=16 * 1024)
        user_id = _text_value(info.get("user_id"), max_length=256)
        if not app_token or (user_id and user_id != token.source_user_id):
            raise ZeppLoginError(
                "Zepp 续期响应无效，请重新登录",
                kind="auth", failure_code="reauth_required", needs_reauth=True,
                reset_url=OFFICIAL_RESET_URL,
            )
        return ZeppLoginResult(
            source_user_id=token.source_user_id,
            region_host=token.region_host or "",
            app_token=app_token,
            login_token=_text_value(info.get("login_token"), max_length=16 * 1024) or login_token,
        )

    def _registration_tokens_url(self, account: str) -> str:
        # Encode the complete account path segment, including email addresses;
        # no password ever enters this URL.
        segment = quote(account, safe="")
        return f"{self._api_user_base}/registrations/{segment}/tokens"

    def _post(self, url: str, *, data: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        try:
            response = self._client.post(
                url, data=data, headers=headers, timeout=self._timeout
            )
        except httpx.TimeoutException:
            raise ZeppLoginError(
                "Zepp 登录请求超时，请稍后重试",
                kind="timeout",
                failure_code="timeout",
            ) from None
        except httpx.HTTPError:
            raise ZeppLoginError(
                "无法连接 Zepp 登录服务，请稍后重试",
                kind="network",
                failure_code="network_error",
            ) from None
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ZeppLoginError(
                "Zepp 登录响应超过安全上限",
                kind="vendor_response",
                failure_code="response_too_large",
            )
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _raise_for_response(
        response: httpx.Response,
        payload: dict[str, Any],
        *,
        stage: str,
    ) -> None:
        if response.status_code == 429:
            raise ZeppLoginError(
                "Zepp 登录尝试过于频繁，请稍后重试",
                kind="rate_limited",
                failure_code="rate_limited",
                retry_after=_retry_after(response),
            )
        if _contains_captcha_marker(payload):
            raise ZeppLoginError(
                "Zepp 要求图形验证，请先在官方页面重置密码后重试",
                kind="auth",
                failure_code="captcha_required",
                needs_reauth=True,
                reset_url=OFFICIAL_RESET_URL,
            )
        error_value = payload.get("error")
        if response.status_code in {401, 403} or str(error_value) in {"401", "403"}:
            raise ZeppLoginError(
                "Zepp 账号或密码错误",
                kind="auth",
                failure_code="invalid_credentials",
                needs_reauth=True,
            )
        if response.status_code != 200:
            raise ZeppLoginError(
                "Zepp 登录服务返回了未确认的响应",
                kind="vendor_response",
                failure_code=f"vendor_{stage}_response",
            )

    @staticmethod
    def _validate_account(value: str) -> str:
        if not isinstance(value, str):
            raise ZeppLoginError(
                "账号格式无效",
                kind="invalid_request",
                failure_code="invalid_account",
            )
        value = value.strip()
        if not value or len(value) > 320 or any(ord(char) < 32 for char in value):
            raise ZeppLoginError(
                "账号格式无效",
                kind="invalid_request",
                failure_code="invalid_account",
            )
        if "/" in value or "?" in value or "#" in value:
            raise ZeppLoginError(
                "账号格式无效",
                kind="invalid_request",
                failure_code="invalid_account",
            )
        return value

    @staticmethod
    def _validate_password(value: str) -> None:
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ZeppLoginError(
                "密码格式无效",
                kind="invalid_request",
                failure_code="invalid_password",
            )

    @staticmethod
    def _validate_country(value: str) -> str:
        if not isinstance(value, str):
            raise ZeppLoginError(
                "国家代码格式无效",
                kind="invalid_request",
                failure_code="invalid_country_code",
            )
        value = value.strip().upper()
        if not _COUNTRY_RE.fullmatch(value):
            raise ZeppLoginError(
                "国家代码格式无效",
                kind="invalid_request",
                failure_code="invalid_country_code",
            )
        return value


def _text_value(value: object, *, max_length: int) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        if 0 < len(text) <= max_length and all(ord(char) >= 32 for char in text):
            return text
    return None


def _contains_captcha_marker(payload: dict[str, Any]) -> bool:
    for key in ("code", "error", "error_code", "message", "reason"):
        value = payload.get(key)
        if key == "code" and value == 4:
            # The official page treats registration response code 4 as an
            # image-captcha refusal and sends the user through reset.
            return True
        if isinstance(value, (str, int)) and "captcha" in str(value).lower():
            return True
    return False


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(parsed, 86_400)) if parsed >= 0 else None


def _region_from_response(payload: dict[str, Any]) -> str | None:
    domains = payload.get("domains")
    if not isinstance(domains, list):
        return None
    for domain in domains:
        if not isinstance(domain, dict):
            continue
        names = domain.get("cnames")
        if not isinstance(names, list):
            continue
        for name in names:
            if not isinstance(name, str):
                continue
            candidate = name.strip().lower()
            if candidate.startswith("https://"):
                candidate = candidate[8:].split("/", 1)[0]
            if _REGION_RE.fullmatch(candidate):
                return candidate
    return None


__all__ = [
    "ACCOUNT_BASE",
    "API_USER_BASE",
    "OFFICIAL_RESET_URL",
    "ZeppLoginAdapter",
    "ZeppLoginError",
    "ZeppLoginResult",
]
