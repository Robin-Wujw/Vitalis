"""Zepp 区域云客户端（真实）+ Mock 客户端。

真实模式（ZEPP_MOCK=false）：
  Zepp（华米）不使用 OAuth2 扫码，而是「网页登录会话 -> apptoken」机制：
    1. 用户在官方网页 watchface.zepp.com / user.huami.com 用账号密码登录
    2. 登录后 cookie `hm-user-login-info` 含 { userid, apptoken, ... }
    3. 把 user_id + apptoken 导入 Vitalis（POST /connect/zepp/token）
    4. 之后请求区域云 API：apptoken 放请求头，配合官方客户端同款标识头
  实现对齐第三方项目 ZeppBridge（已实测有效的端点/字段/请求头）。

区域主机：https://api-mifit*.zepp.com 或 https://api-mifit*.huami.com
  （中国区通常 api-mifitcn.zepp.com，其它区按账号所在区域）

Mock 模式（ZEPP_MOCK=true）：模拟同构数据，离线可端到端。
"""
from __future__ import annotations

import random
import re
import secrets
import time as time_mod
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from .sport_types import ZEPP_SPORT_MODES

try:
    import truststore
except ImportError:
    truststore = None
else:
    truststore.inject_into_ssl()

# ---- 真实 Zepp 区域云端点（实测有效路径） ----
API_DEVICES = "/users/{user_id}/devices"
API_HEART_RATE = "/users/{user_id}/heartRate"
API_BAND_DATA = "/v1/data/band_data.json"          # 手环原始数据（睡眠/步数等）
API_SPORT_HISTORY = "/v1/sport/{sport}/history.json"  # 运动摘要列表
API_SPORT_DETAIL = "/v1/sport/run/detail.json"     # 单次运动明细
API_WATCH_STATS = "/v2/watch/users/{user_id}/WatchSportStatistics/{statistic}"
API_EVENTS = "/v2/users/me/events"                 # HRV / 每日健康摘要
API_USER_EVENTS = "/users/{user_id}/events"        # SpO2 / PAI / all-day stress
API_USER_EVENTS_DATE = "/users/{user_id}/events/dateString"  # ODI / OSA nightly events
API_FILE_INFO_EVENTS = "/users/me/fileInfo/events"  # Dense measurement file index
API_FILE_DOWNLOAD_URLS = "/files/{file_type}/users/{user_id}/queryDownUrlList"
MAX_DENSE_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKOUT_DETAIL_BYTES = 4 * 1024 * 1024
MAX_WORKOUT_DETAIL_JSON_OBJECTS = 20_000
MAX_WORKOUT_DETAIL_JSON_SEPARATORS = 80_000

# 官方客户端请求头（ZeppBridge 实测有效）
APP_HEADERS = {
    "appname": "com.huami.midong",
    "appplatform": "ios_phone",
    "v": "2.0",
    "vn": "10.2.5",
    "cv": "1722_10.2.5",
    "vb": "202604132257",
    "lang": "en",
    "country": "",
    "timezone": "UTC",
    "accept": "*/*",
}

ZEPP_HOST_PATTERN = re.compile(r"^api-mifit[^.]*\.(zepp\.com|huami\.com)$", re.IGNORECASE)


def validate_region_host(host: str) -> str:
    """Validate and normalize an allowlisted Zepp HTTPS origin."""
    if not host:
        raise ZeppAuthError("region_host 不能为空", kind="invalid_request")
    raw = host.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme.lower() != "https":
            raise ZeppAuthError("region_host 仅允许 HTTPS", kind="invalid_request")
        if parsed.username or parsed.password:
            raise ZeppAuthError("region_host 不允许凭据", kind="invalid_request")
        try:
            has_port = parsed.port is not None
        except ValueError as exc:
            raise ZeppAuthError(
                "region_host 端口格式无效", kind="invalid_request"
            ) from exc
        if has_port:
            raise ZeppAuthError("region_host 不允许指定端口", kind="invalid_request")
        if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
            raise ZeppAuthError(
                "region_host 不允许路径、查询参数或片段", kind="invalid_request"
            )
        normalized = parsed.hostname or ""
    else:
        if any(char in raw for char in (":", "/", "?", "#", "@")):
            raise ZeppAuthError(
                "region_host 只允许裸主机名", kind="invalid_request"
            )
        normalized = raw
    normalized = normalized.lower()
    if not ZEPP_HOST_PATTERN.fullmatch(normalized):
        raise ZeppAuthError(
            "region_host 不合法，仅允许 api-mifit*.zepp.com 或 api-mifit*.huami.com",
            kind="invalid_request",
        )
    return normalized

# Full public Zepp OS workout code -> canonical mode name.
SPORT_TYPE_MAP = {code: mode.code for code, mode in ZEPP_SPORT_MODES.items()}

# Earlier plans queried these paths independently. The account-wide feed
# actually lives under /run/history.json; the other paths normally return 404.
LEGACY_SPORTS = (
    "run", "walking", "ride", "swimming", "indoor_run", "treadmill",
    "trail", "hiking", "strength", "elliptical", "rowing", "yoga", "climb",
)
SPORTS = ["run"]
WORKOUT_AGGREGATE_PLAN_VERSION = "zepp-sync-v6"


ZeppErrorKind = Literal[
    "auth",
    "not_available",
    "network",
    "service",
    "timeout",
    "cancelled",
    "invalid_request",
    "identity_conflict",
    "partial_coverage",
    "vendor_response",
    "unknown",
]


class ZeppAuthError(RuntimeError):
    """Zepp failure with a stable machine-readable classification."""

    def __init__(
        self,
        message: str,
        *,
        kind: ZeppErrorKind = "unknown",
        needs_reauth: bool = False,
    ):
        super().__init__(message)
        self.kind: ZeppErrorKind = "auth" if needs_reauth else kind
        self.needs_reauth = self.kind == "auth"


class MockOAuthToken:
    """mock 扫码演示用的轻量 token 对象（对齐真实 ZeppToken 字段名）。"""

    def __init__(self, access_token: str, refresh_token: str = "mock-refresh",
                 expires_in: int = 86400, scope: str = "user.sleep user.activity user.training user.hr",
                 source_user_id: str | None = "mock-user-001"):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in
        self.scope = scope
        self.source_user_id = source_user_id

    @property
    def expires_at(self):
        return datetime.now(timezone.utc) + timedelta(seconds=self.expires_in)


class ZeppAPIClient:
    """真实 Zepp 区域云客户端（apptoken 请求头模式）。"""

    def __init__(self, app_token: str, user_id: str, region_host: str = ""):
        self.app_token = app_token
        self.user_id = user_id
        self.region_host = validate_region_host(
            region_host or "api-mifitcn.zepp.com"
        )
        self.base_url = f"https://{self.region_host}"
        # Scheduled Hermes runs must not depend on an interactive shell proxy.
        self._default_timeout: float | None = 30.0
        self._request_timeout: float | None = self._default_timeout
        self._request_remaining: Callable[[], float | None] | None = None
        self._request_cancel_check: Callable[[], bool] | None = None
        self._request_check: Callable[[], None] | None = None
        self._client = httpx.Client(timeout=self._default_timeout, trust_env=False)
        # 官方客户端同款请求头 + 动态 apptoken
        self._headers = {"apptoken": self.app_token, **APP_HEADERS}
        self._request_seq = 0

    # ---- 核心 GET ----
    def _get(
        self, path: str, params: dict, *, max_response_bytes: int | None = None,
    ) -> dict:
        params = {**params, "r": self._request_id()}
        for attempt in range(3):
            self._check_request_budget()
            timeout = self._effective_request_timeout()
            try:
                if max_response_bytes is None:
                    request_kwargs = {
                        "params": params,
                        "headers": self._headers,
                    }
                    if timeout is not None:
                        request_kwargs["timeout"] = timeout
                    resp = self._client.get(self.base_url + path, **request_kwargs)
                else:
                    resp = self._bounded_get(
                        path, params, max_response_bytes, timeout=timeout
                    )
            except httpx.TimeoutException as exc:
                if attempt < 2:
                    self._sleep_before_retry(0.05 * (attempt + 1))
                    continue
                self._check_request_budget()
                raise ZeppAuthError(f"请求超时: {exc}", kind="timeout") from exc
            except httpx.HTTPError as exc:
                if attempt < 2:
                    self._sleep_before_retry(0.05 * (attempt + 1))
                    continue
                self._check_request_budget()
                raise ZeppAuthError(f"网络错误: {exc}", kind="network") from exc

            if resp.status_code in (401, 403):
                raise ZeppAuthError(
                    f"token 失效（HTTP {resp.status_code}），请重新导入",
                    kind="auth",
                )
            if resp.status_code == 404:
                raise ZeppAuthError(
                    "Zepp 接口不可用（HTTP 404）",
                    kind="not_available",
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < 2:
                    self._sleep_before_retry(0.1)
                    continue
                raise ZeppAuthError(
                    f"Zepp 服务暂时不可用（HTTP {resp.status_code}）",
                    kind="service",
                )
            if resp.status_code != 200:
                raise ZeppAuthError(
                    f"Zepp 接口错误（HTTP {resp.status_code}）: {resp.text[:200]}",
                    kind="vendor_response",
                )
            try:
                if max_response_bytes is not None and (
                    resp.content.count(b"{") > MAX_WORKOUT_DETAIL_JSON_OBJECTS
                    or resp.content.count(b",") > MAX_WORKOUT_DETAIL_JSON_SEPARATORS
                ):
                    raise ZeppAuthError("运动明细 JSON 结构超过安全上限", kind="vendor_response")
                return resp.json()
            except ValueError:
                # band_data 等可能返回非 JSON，按文本返回
                return {"_raw_text": resp.text}
        raise ZeppAuthError("Zepp 请求重试耗尽", kind="service")

    def _bounded_get(
        self,
        path: str,
        params: dict,
        max_bytes: int,
        *,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Read a detail response with raw-wire and expanded-body limits."""
        stream_kwargs = {
            "params": params,
            "headers": {**self._headers, "accept-encoding": "gzip, deflate, identity"},
        }
        if timeout is not None:
            stream_kwargs["timeout"] = timeout
        with self._client.stream("GET", self.base_url + path, **stream_kwargs) as response:
            if response.status_code != 200:
                return httpx.Response(response.status_code, content=b"", request=response.request)
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        raise ZeppAuthError("运动明细响应超过大小上限", kind="vendor_response")
                except ValueError:
                    pass
            encoding = response.headers.get("content-encoding", "").strip().lower()
            if encoding in {"", "identity"}:
                decoder = None
            elif encoding == "gzip":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif encoding == "deflate":
                decoder = zlib.decompressobj(zlib.MAX_WBITS)
            else:
                raise ZeppAuthError("运动明细响应压缩格式不受支持", kind="vendor_response")

            body = bytearray()
            wire_bytes = 0
            for part in response.iter_raw(chunk_size=64 * 1024):
                self._check_request_budget()
                wire_bytes += len(part)
                if wire_bytes > max_bytes:
                    raise ZeppAuthError("运动明细响应超过大小上限", kind="vendor_response")
                if decoder is None:
                    expanded = part
                else:
                    try:
                        expanded = decoder.decompress(part, max_bytes + 1 - len(body))
                    except zlib.error as exc:
                        raise ZeppAuthError("运动明细响应压缩内容无效", kind="vendor_response") from exc
                    if decoder.unconsumed_tail or decoder.unused_data:
                        raise ZeppAuthError("运动明细响应超过大小上限", kind="vendor_response")
                if len(body) + len(expanded) > max_bytes:
                    raise ZeppAuthError("运动明细响应超过大小上限", kind="vendor_response")
                body.extend(expanded)
            if decoder is not None and not decoder.eof:
                raise ZeppAuthError("运动明细响应压缩内容无效", kind="vendor_response")
            headers = {
                key: value for key, value in response.headers.items()
                if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            }
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
            )

    def _request_id(self) -> str:
        self._request_seq += 1
        return f"ZEPBRIDGE-{self._request_seq:016X}"

    def set_timeout(self, timeout: float | None) -> None:
        """Set the default timeout used when no request budget is active."""
        self._default_timeout = timeout
        self._request_timeout = timeout
        self._client.timeout = timeout

    def set_request_budget(
        self,
        timeout: float | None = None,
        *,
        remaining_seconds: Callable[[], float | None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        check: Callable[[], None] | None = None,
    ) -> None:
        """Bound retries to a caller-owned deadline and cancellation signal.

        ``remaining_seconds`` is evaluated before every attempt and backoff.
        ``check`` may raise a domain-specific cancellation/deadline exception;
        ``cancel_check`` is the simpler boolean form for standalone callers.
        """
        self._request_timeout = timeout if timeout is not None else self._default_timeout
        self._request_remaining = remaining_seconds
        self._request_cancel_check = cancel_check
        self._request_check = check
        self._client.timeout = self._request_timeout

    def clear_request_budget(self) -> None:
        """Restore the default request policy after a bounded operation."""
        self._request_timeout = self._default_timeout
        self._request_remaining = None
        self._request_cancel_check = None
        self._request_check = None
        self._client.timeout = self._default_timeout

    def _check_request_budget(self) -> None:
        if self._request_cancel_check is not None and self._request_cancel_check():
            raise ZeppAuthError("请求已取消", kind="cancelled")
        if self._request_check is not None:
            checked = self._request_check()
            if checked:
                raise ZeppAuthError("请求已取消", kind="cancelled")
        if self._request_remaining is not None:
            remaining = self._request_remaining()
            if remaining is not None and remaining <= 0:
                raise ZeppAuthError("请求达到时间预算", kind="timeout")

    def _effective_request_timeout(self) -> float | None:
        timeout = self._request_timeout
        if self._request_remaining is not None:
            remaining = self._request_remaining()
            if remaining is not None:
                if remaining <= 0:
                    self._check_request_budget()
                    raise ZeppAuthError("请求达到时间预算", kind="timeout")
                timeout = remaining if timeout is None else min(timeout, remaining)
        if timeout is None:
            return None
        return float(timeout)

    def _sleep_before_retry(self, delay: float) -> None:
        self._check_request_budget()
        if self._request_remaining is not None:
            remaining = self._request_remaining()
            if remaining is not None:
                if remaining <= 0:
                    self._check_request_budget()
                    raise ZeppAuthError("请求达到时间预算", kind="timeout")
                delay = min(delay, remaining)
        time_mod.sleep(max(0.0, delay))
        self._check_request_budget()

    def close(self) -> None:
        """Close the underlying HTTP client explicitly."""
        self._client.close()

    # ---- 数据接口 ----
    def fetch_devices(self) -> dict:
        return self._get(
            API_DEVICES.format(user_id=self.user_id),
            {"enableMultiDevice": "true", "device_type": "android_phone"},
        )

    def fetch_heart_rate(
        self,
        start_time: int,
        end_time: int,
        limit: int = 1000,
        hr_type: int = 2,
    ) -> dict:
        """Fetch heart-rate rows; this Zepp endpoint uses Unix seconds."""
        return self._get(
            API_HEART_RATE.format(user_id=self.user_id),
            {
                "startTime": str(start_time),
                "endTime": str(end_time),
                "limit": str(limit),
                "type": str(hr_type),
            },
        )

    def fetch_band_data(self, from_date: str, to_date: str, query_type: str = "detail",
                        byte_length: int = 8, device_type: int = 0) -> dict:
        """手环原始数据：睡眠/步数等（summary 为 base64 JSON）。"""
        return self._get(
            API_BAND_DATA,
            {
                "userid": self.user_id,
                "from_date": from_date,
                "to_date": to_date,
                "query_type": query_type,
                "byteLength": str(byte_length),
                "device_type": str(device_type),
            },
        )

    def fetch_sport_history(self, sport: str, start_track_id: int, stop_track_id: int,
                            need_sub_data: int = 1) -> dict:
        """运动历史：start/stopTrackId 是游标，响应 data.next 为下一页游标。"""
        return self._get(
            API_SPORT_HISTORY.format(sport=sport),
            {
                "userid": self.user_id,
                "startTrackId": str(start_track_id),
                "stopTrackId": str(stop_track_id),
                "need_sub_data": str(need_sub_data),
                "type": "",
            },
        )

    def fetch_sport_detail(self, track_id: str, source: str) -> dict:
        return self._get(
            API_SPORT_DETAIL,
            {"trackid": track_id, "source": source},
            max_response_bytes=MAX_WORKOUT_DETAIL_BYTES,
        )

    def fetch_watch_statistics(self, statistic: str = "SPORT_LOAD", start_day: str = "",
                               end_day: str = "", limit: int = 30, reverse: bool = True) -> dict:
        """训练负荷 / VO2。"""
        if statistic not in {"SPORT_LOAD", "VO2_MAX"}:
            raise ValueError(f"unsupported watch statistic: {statistic}")
        return self._get(
            API_WATCH_STATS.format(user_id=self.user_id, statistic=statistic),
            {
                "startDay": start_day, "endDay": end_day,
                "limit": str(limit), "isReverse": "true" if reverse else "false",
            },
        )

    def fetch_events(self, event_type: str, sub_type: str, from_ms: int, to_ms: int,
                     limit: int = 2000, reverse: bool = True) -> dict:
        """事件流：HRV(hrv_sdnn/real_data)、每日健康摘要(DailyHealth/summary)。"""
        return self._get(
            API_EVENTS,
            {
                "eventType": event_type, "subType": sub_type,
                "from": str(from_ms), "to": str(to_ms),
                "limit": str(limit), "reverse": "1" if reverse else "0",
            },
        )

    def fetch_user_events(self, event_type: str, sub_type: str | None, from_ms: int, to_ms: int,
                          limit: int = 1000, reverse: bool = True) -> dict:
        """User-scoped timeline used by SpO2, PAI and all-day stress."""
        params = {
            "eventType": event_type,
            "from": str(from_ms),
            "to": str(to_ms),
            "limit": str(limit),
            "reverse": "1" if reverse else "0",
            "userId": self.user_id,
        }
        if sub_type:
            params["subType"] = sub_type
        return self._get(API_USER_EVENTS.format(user_id=self.user_id), params)

    def fetch_user_events_date_string(
        self,
        event_type: str,
        sub_type: str,
        from_iso: str | date,
        to_iso: str | date,
        time_zone: str | None = None,
        limit: int = 1000,
    ) -> dict:
        """Fetch ODI/OSA using inclusive local calendar dates.

        Older callers may still pass ISO timestamps; reducing them to their
        date component keeps serialized attempts runnable while ensuring the
        wire contract is the device-local civil-date form.
        """
        if time_zone is None:
            from vitalis.config import settings

            time_zone = settings.timezone

        def calendar_date(value: str | date) -> str:
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, date):
                return value.isoformat()
            else:
                text = str(value).strip()
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    return text[:10] if len(text) >= 10 else text
            if parsed.tzinfo is None:
                return parsed.date().isoformat()
            return parsed.astimezone(ZoneInfo(time_zone)).date().isoformat()
        return self._get(
            API_USER_EVENTS_DATE.format(user_id=self.user_id),
            {
                "eventType": event_type,
                "subType": sub_type,
                "from": calendar_date(from_iso),
                "to": calendar_date(to_iso),
                "timeZone": time_zone,
                "limit": str(limit),
                "reverse": "0",
                "userId": self.user_id,
            },
        )

    def fetch_file_info_events(
        self, event_type: str, sub_type: str, from_ms: int, to_ms: int, limit: int = 2000
    ) -> dict:
        """Fetch file index metadata; this does not download measurement payloads."""
        return self._get(
            API_FILE_INFO_EVENTS,
            {
                "eventType": event_type,
                "subType": sub_type,
                "from": str(from_ms),
                "to": str(to_ms),
                "limit": str(limit),
            },
        )

    def fetch_file_download_urls(self, file_type: str, file_ids: list[str]) -> dict[str, str]:
        """Resolve indexed file IDs through Zepp's official download contract."""
        if not file_ids:
            return {}
        payload = self._get(
            API_FILE_DOWNLOAD_URLS.format(file_type=file_type, user_id=self.user_id),
            {"fileIds": ",".join(file_ids)},
        )
        urls = {
            str(file_id): value
            for file_id, value in payload.items()
            if str(file_id) in file_ids and isinstance(value, str)
        }
        if set(urls) != set(file_ids):
            raise ZeppAuthError(
                "Zepp 未返回全部高频心率文件下载地址",
                kind="vendor_response",
            )
        for value in urls.values():
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ZeppAuthError(
                    "Zepp 返回了不安全的文件下载地址",
                    kind="vendor_response",
                )
        return urls

    def download_dense_file(self, file_type: str, file_id: str) -> bytes:
        """Download one signed SEC_HR archive without forwarding Zepp credentials."""
        target = self.fetch_file_download_urls(file_type, [file_id])[file_id]
        for attempt in range(3):
            self._check_request_budget()
            timeout = self._effective_request_timeout()
            try:
                stream_kwargs = {"follow_redirects": False}
                if timeout is not None:
                    stream_kwargs["timeout"] = timeout
                with self._client.stream("GET", target, **stream_kwargs) as response:
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        self._sleep_before_retry(0.1 * (attempt + 1))
                        continue
                    if response.status_code != 200:
                        raise ZeppAuthError(
                            f"Zepp 高频心率文件下载失败（HTTP {response.status_code}）",
                            kind=(
                                "not_available"
                                if response.status_code == 404
                                else "service"
                                if response.status_code in (429, 500, 502, 503, 504)
                                else "vendor_response"
                            ),
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        self._check_request_budget()
                        content.extend(chunk)
                        if len(content) > MAX_DENSE_FILE_BYTES:
                            raise ZeppAuthError(
                                "Zepp 高频心率文件超过大小限制",
                                kind="vendor_response",
                            )
                    return bytes(content)
            except httpx.TimeoutException as exc:
                if attempt < 2:
                    self._sleep_before_retry(0.1 * (attempt + 1))
                    continue
                self._check_request_budget()
                raise ZeppAuthError(
                    f"高频心率文件下载超时: {exc}",
                    kind="timeout",
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < 2:
                    self._sleep_before_retry(0.1 * (attempt + 1))
                    continue
                self._check_request_budget()
                raise ZeppAuthError(
                    f"高频心率文件网络错误: {exc}",
                    kind="network",
                ) from exc
        raise ZeppAuthError("Zepp 高频心率文件下载重试耗尽", kind="service")

    def fetch_hrv(
        self,
        start_date: str,
        end_date: str,
        timezone_name: str | None = None,
    ) -> dict:
        """HRV data for inclusive local dates, encoded as UTC millis.

        The timezone is part of a persisted sync attempt.  It must not be
        re-resolved from the process configuration when a worker resumes.
        """
        from vitalis.time import local_day_utc_bounds

        start_day = date.fromisoformat(start_date)
        end_day = date.fromisoformat(end_date)
        start, _ = local_day_utc_bounds(start_day, timezone_name)
        _, end = local_day_utc_bounds(end_day, timezone_name)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000) - 1
        return self.fetch_events("hrv_sdnn", "real_data", start_ms, end_ms, 2000, True)

    # ---- 验证 ----
    def verify(self) -> dict:
        """验证 token 有效性：拉取设备列表（失败抛 ZeppAuthError）。"""
        devices = self.fetch_devices()
        return devices


def generate_state() -> str:
    """生成一次性 state（保留扫码演示流程用）。"""
    return secrets.token_urlsafe(24)


class MockZeppClient:
    """确定性 Mock：模拟 apptoken 模式与同构数据，离线可端到端。"""

    def __init__(self, seed: int = 42, timezone_name: str = "Asia/Shanghai"):
        self._rng = random.Random(seed)
        self._timezone_name = timezone_name
        self.authorize_url_base = "http://mock-zepp.local/authorize"

    # OAuth 演示兼容接口（mock 扫码页仍可用）
    def get_authorize_url(self, state: str) -> str:
        return f"{self.authorize_url_base}?state={state}&fake=1"

    def exchange_code(self, code: str) -> "MockOAuthToken":
        return MockOAuthToken(access_token=f"mock-access-{code[-8:]}")

    def refresh_token(self, refresh_token: str) -> "MockOAuthToken":
        return MockOAuthToken(access_token="mock-access-refreshed")

    def set_timeout(self, timeout: float | None) -> None:
        """Match the real client timeout control used by the coordinator."""
        return None

    def close(self) -> None:
        """Mock clients do not own network resources."""
        return None

    def verify(self) -> dict:
        return {"data": {"devices": [{"name": "Mock Amazfit"}]}}

    def get_user_info(self, token) -> dict:
        return {"user_id": "mock-user-001", "nickname": "Mock User"}

    # ---- 同构模拟数据 ----
    def fetch_heart_rate(self, start_time: int, end_time: int, limit: int = 1000, hr_type: int = 2) -> dict:
        return {"items": [{"timestamp": start_time, "value": 62}]}

    def fetch_file_info_events(self, event_type: str, sub_type: str, from_ms: int, to_ms: int, limit: int = 2000) -> dict:
        return {"items": []}

    def fetch_user_events(self, event_type: str, sub_type: str | None, from_ms: int, to_ms: int, limit: int = 1000, reverse: bool = True) -> dict:
        return {"items": []}

    def fetch_user_events_date_string(self, event_type: str, sub_type: str, from_iso: str | date, to_iso: str | date, time_zone: str | None = None, limit: int = 1000) -> dict:
        return {"items": []}

    def fetch_sport_detail(self, track_id: str, source: str) -> dict:
        return {"data": {"trackid": track_id, "source": source}}

    def download_dense_file(self, file_type: str, file_id: str) -> bytes:
        raise ZeppAuthError("mock 没有高频心率归档", kind="not_available")

    def fetch_devices(self) -> dict:
        return {"items": [{"displayName": "Mock wearable", "macAddress": "02:00:00:00:00:01"}]}

    def fetch_band_data(self, from_date: str, to_date: str, query_type: str = "detail",
                        byte_length: int = 8, device_type: int = 0) -> dict:
        """模拟手环原始数据：item.summary = base64(slp/stp/tz)。"""
        import base64
        import json as _json

        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
        items = []
        day = start
        while day <= end:
            weekday = day.weekday()
            weekend = weekday >= 5
            base = 480 if weekend else 420 + (self._rng.randint(-20, 60))
            score = min(95, 40 + base // 10 + self._rng.randint(0, 8))
            deep = int(base * 0.20)
            rem = int(base * 0.22)
            light = base - deep - rem
            steps = self._rng.randint(6000, 14000) + (2500 if weekend else 0)
            summary = {
                "tz": 28800,  # UTC+8
                "slp": {
                    "ss": score, "st": "23:10", "ed": "07:00",
                    "dp": deep, "lt": light, "rm": rem, "wk": self._rng.randint(10, 30),
                    "rhr": self._rng.randint(52, 66),
                },
                "stp": {"ttl": steps, "cal": self._rng.randint(300, 700), "dis": steps * 0.7},
            }
            items.append({
                "date_time": day.isoformat(),
                "summary": base64.b64encode(_json.dumps(summary).encode()).decode(),
            })
            day += timedelta(days=1)
        return {"code": 0, "data": {"items": items}}

    def fetch_sport_history(self, sport: str, start_track_id: int, stop_track_id: int,
                            need_sub_data: int = 1) -> dict:
        if stop_track_id <= start_track_id:
            return {"code": 0, "data": {"items": [], "next": -1}}
        zone = ZoneInfo(self._timezone_name)
        first = datetime.fromtimestamp(start_track_id, timezone.utc).astimezone(zone).date()
        last = datetime.fromtimestamp(stop_track_id - 1, timezone.utc).astimezone(zone).date()
        items = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            if day.weekday() not in (0, 2, 5):
                continue
            started = datetime(day.year, day.month, day.day, 7, 30, tzinfo=zone)
            track_id = int(started.timestamp())
            if not start_track_id <= track_id < stop_track_id:
                continue
            items.append({
                "trackid": str(track_id),
                "type": 1 if sport == "run" else 6,
                "start_time": started.isoformat(),
                "end_time": (started + timedelta(minutes=50)).isoformat(),
                "distance": 7000, "calories": 420,
                "avg_hr": 138, "max_hr": 168,
                "training_load": 42, "source": "mock",
            })
        items.sort(key=lambda item: int(item["trackid"]), reverse=True)
        page = items[:100]
        return {"code": 0, "data": {
            "items": page,
            "next": int(page[-1]["trackid"]) if len(items) > len(page) else -1,
        }}

    def fetch_watch_statistics(self, statistic: str = "SPORT_LOAD", start_day: str = "",
                               end_day: str = "", limit: int = 30, reverse: bool = True) -> dict:
        metric = "load" if statistic == "SPORT_LOAD" else "vo2max"
        first = date.fromisoformat(start_day) if start_day else date.today() - timedelta(days=29)
        last = date.fromisoformat(end_day) if end_day else date.today()
        days = [first + timedelta(days=offset) for offset in range(max(0, (last - first).days + 1))]
        if reverse:
            days.reverse()
        return {"code": 0, "data": {"items": [
            {"day": day.isoformat(), metric: (
                20 + day.toordinal() % 61 if statistic == "SPORT_LOAD"
                else 40 + day.toordinal() % 16
            )}
            for day in days[:max(0, min(limit, 900))]
        ]}}

    def fetch_events(self, event_type: str, sub_type: str, from_ms: int, to_ms: int,
                     limit: int = 2000, reverse: bool = True) -> dict:
        import base64
        from vitalis.time import local_day_utc_bounds

        if (event_type, sub_type) == ("hrv_sdnn", "real_data"):
            # The older mock facade still consumes the daily-value representation.
            items = [
                {"ts": (date.today() - timedelta(days=i)).isoformat(),
                 "value": 40 + (date.today() - timedelta(days=i)).toordinal() % 31}
                for i in range(max(0, min(limit, 14)))
            ]
            return {"code": 0, "data": {"items": items}}

        supported = {
            ("DailyHealth", "summary"), ("Charge", "real_data"),
            ("readiness", "watch_score"), ("RespiratoryRate", "real_data"),
            ("HRVRMSSD", "real_data"), ("LactateThreshold", "summary"),
        }
        if (event_type, sub_type) not in supported or to_ms <= from_ms or limit <= 0:
            return {"code": 0, "data": {"items": []}}

        zone = ZoneInfo(self._timezone_name)
        window_start = datetime.fromtimestamp(from_ms / 1000, timezone.utc)
        window_end = datetime.fromtimestamp(to_ms / 1000, timezone.utc)
        first = window_start.astimezone(zone).date()
        last = (window_end - timedelta(milliseconds=1)).astimezone(zone).date()
        items = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            day_start, day_end = local_day_utc_bounds(day, self._timezone_name)
            start, end = max(window_start, day_start), min(window_end, day_end)
            if start >= end:
                continue
            timestamp_ms = int((start + (end - start) / 2).timestamp() * 1000)
            ordinal = day.toordinal()
            if event_type == "DailyHealth":
                steps = 6000 + ordinal % 5000
                item = {"date": day.isoformat(), "steps": steps,
                        "calories": 300 + ordinal % 200,
                        "totalDistance": steps * 0.7}
            elif event_type == "Charge":
                item = {"eventType": "Charge", "date": day.isoformat(),
                        "value": {"startTime": timestamp_ms, "samples": [
                            {"s": 0, "total": 60 + ordinal % 20,
                             "physical": 65, "mental": 55}
                        ]}}
            elif event_type == "readiness":
                item = {"eventType": "readiness", "date": day.isoformat(),
                        "value": {"timestamp": timestamp_ms,
                                  "rdnsScore": 70 + ordinal % 20, "phyScore": 75}}
            elif event_type == "RespiratoryRate":
                item = {"date": day.isoformat(), "value": {
                    "measurements": base64.b64encode(bytes([14 + ordinal % 5, 16])).decode("ascii")
                }}
            elif event_type == "HRVRMSSD":
                item = {"value": {"startTime": timestamp_ms,
                                  "samples": [{"s": 0, "hrv": 40 + ordinal % 30}]}}
            else:
                item = {"value": {"samples": [{
                    "dateString": day.isoformat(),
                    "lactateThresholdHr": 150 + ordinal % 20,
                    "lactateThresholdPace": 300 + ordinal % 60,
                }]}}
            items.append(item)
        if reverse:
            items.reverse()
        return {"code": 0, "data": {"items": items[:limit]}}

    def fetch_hrv(
        self,
        start_date: str,
        end_date: str,
        timezone_name: str | None = None,
    ) -> dict:
        from vitalis.time import local_day_utc_bounds

        items = []
        day = date.fromisoformat(start_date)
        last = date.fromisoformat(end_date)
        while day <= last:
            start, end = local_day_utc_bounds(day, timezone_name)
            if start < end:
                midpoint_ms = int((start + (end - start) / 2).timestamp() * 1000)
                items.append({"value": {
                    "startTime": midpoint_ms,
                    "samples": [{"s": 0, "sdnn": 40 + day.toordinal() % 31}],
                }})
            day += timedelta(days=1)
        return {"code": 0, "data": {"items": items}}
