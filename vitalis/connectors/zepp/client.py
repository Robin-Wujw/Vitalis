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
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import httpx

from .sport_types import ZEPP_SPORT_MODES

# ---- 真实 Zepp 区域云端点（实测有效路径） ----
API_DEVICES = "/users/{user_id}/devices"
API_HEART_RATE = "/users/{user_id}/heartRate"
API_BAND_DATA = "/v1/data/band_data.json"          # 手环原始数据（睡眠/步数等）
API_SPORT_HISTORY = "/v1/sport/{sport}/history.json"  # 运动摘要列表
API_SPORT_DETAIL = "/v1/sport/run/detail.json"     # 单次运动明细
API_WATCH_STATS = "/v2/watch/users/{user_id}/WatchSportStatistics/SPORT_LOAD"  # 训练负荷
API_EVENTS = "/v2/users/me/events"                 # HRV / 每日健康摘要
API_USER_EVENTS = "/users/{user_id}/events"        # SpO2 / PAI / all-day stress
API_USER_EVENTS_DATE = "/users/{user_id}/events/dateString"  # ODI / OSA nightly events
API_FILE_INFO_EVENTS = "/users/me/fileInfo/events"  # Dense measurement file index

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
    """校验并规范化区域主机名（防 SSRF）。"""
    if not host:
        raise ZeppAuthError("region_host 不能为空")
    host = host.strip().lower()
    if host.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        parsed = urlparse(host)
        host = parsed.netloc or host.split("//")[-1].split("/")[0]
    # 拒绝端口、路径、凭据
    if ":" in host:
        raise ZeppAuthError("region_host 不允许指定端口")
    if "/" in host or "?" in host or "#" in host:
        raise ZeppAuthError("region_host 不允许路径或查询参数")
    if "@" in host:
        raise ZeppAuthError("region_host 不允许凭据")
    if not ZEPP_HOST_PATTERN.match(host):
        raise ZeppAuthError(f"region_host 不合法，仅允许 api-mifit*.zepp.com 或 api-mifit*.huami.com")
    return host

# Full public Zepp OS workout code -> canonical mode name.
SPORT_TYPE_MAP = {code: mode.code for code, mode in ZEPP_SPORT_MODES.items()}

# 全运动查询列表（对齐 ZeppBridge）
SPORTS = [
    "run", "walking", "ride", "swimming", "indoor_run", "treadmill",
    "trail", "hiking", "strength", "elliptical", "rowing", "yoga", "climb",
]


class ZeppAuthError(RuntimeError):
    """Zepp request failure with an explicit credential-rejection signal."""

    def __init__(self, message: str, *, needs_reauth: bool = False):
        super().__init__(message)
        self.needs_reauth = needs_reauth


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
        self.region_host = region_host or "api-mifitcn.zepp.com"  # 缺省中国区
        if self.region_host.startswith(("http://", "https://")):
            # 规范化：只保留主机名
            from urllib.parse import urlparse

            self.region_host = urlparse(self.region_host).netloc or self.region_host.split("//")[-1]
        self.base_url = f"https://{self.region_host}"
        self._client = httpx.Client(timeout=30.0)
        # 官方客户端同款请求头 + 动态 apptoken
        self._headers = {"apptoken": self.app_token, **APP_HEADERS}
        self._request_seq = 0

    # ---- 核心 GET ----
    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "r": self._request_id()}
        for attempt in range(3):
            try:
                resp = self._client.get(
                    self.base_url + path,
                    params=params,
                    headers=self._headers,
                )
            except httpx.HTTPError as exc:
                if attempt < 2:
                    time_mod.sleep(0.05 * (attempt + 1))
                    continue
                raise ZeppAuthError(f"网络错误: {exc}")

            if resp.status_code in (401, 403):
                raise ZeppAuthError(
                    f"token 失效（HTTP {resp.status_code}），请重新导入",
                    needs_reauth=True,
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < 2:
                    time_mod.sleep(0.1)
                    continue
                raise ZeppAuthError(f"Zepp 服务暂时不可用（HTTP {resp.status_code}）")
            if resp.status_code != 200:
                raise ZeppAuthError(f"Zepp 接口错误（HTTP {resp.status_code}）: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError:
                # band_data 等可能返回非 JSON，按文本返回
                return {"_raw_text": resp.text}
        raise ZeppAuthError("Zepp 请求重试耗尽")

    def _request_id(self) -> str:
        self._request_seq += 1
        return f"ZEPBRIDGE-{self._request_seq:016X}"

    # ---- 数据接口 ----
    def fetch_devices(self) -> dict:
        return self._get(
            API_DEVICES.format(user_id=self.user_id),
            {"enableMultiDevice": "true", "device_type": "android_phone"},
        )

    def fetch_heart_rate(self, start_ms: int, end_ms: int, limit: int = 1000, hr_type: int = 2) -> dict:
        return self._get(
            API_HEART_RATE.format(user_id=self.user_id),
            {"startTime": str(start_ms), "endTime": str(end_ms), "limit": str(limit), "type": str(hr_type)},
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
        return self._get(API_SPORT_DETAIL, {"trackid": track_id, "source": source})

    def fetch_watch_statistics(self, statistic: str = "SPORT_LOAD", start_day: str = "",
                               end_day: str = "", limit: int = 30, reverse: bool = True) -> dict:
        """训练负荷 / VO2。"""
        return self._get(
            API_WATCH_STATS.format(user_id=self.user_id),
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
        from_iso: str,
        to_iso: str,
        time_zone: str = "Asia/Shanghai",
        limit: int = 1000,
    ) -> dict:
        return self._get(
            API_USER_EVENTS_DATE.format(user_id=self.user_id),
            {
                "eventType": event_type,
                "subType": sub_type,
                "from": from_iso,
                "to": to_iso,
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

    def fetch_hrv(self, start_date: str, end_date: str) -> dict:
        """HRV 数据：日期格式 YYYY-MM-DD，内部转为毫秒时间戳。"""
        from datetime import datetime
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.replace(hour=23, minute=59, second=59).timestamp() * 1000)
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

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)
        self.authorize_url_base = "http://mock-zepp.local/authorize"

    # OAuth 演示兼容接口（mock 扫码页仍可用）
    def get_authorize_url(self, state: str) -> str:
        return f"{self.authorize_url_base}?state={state}&fake=1"

    def exchange_code(self, code: str) -> "MockOAuthToken":
        return MockOAuthToken(access_token=f"mock-access-{code[-8:]}")

    def refresh_token(self, refresh_token: str) -> "MockOAuthToken":
        return MockOAuthToken(access_token="mock-access-refreshed")

    def verify(self) -> dict:
        return {"data": {"devices": [{"name": "Mock Amazfit"}]}}

    def get_user_info(self, token) -> dict:
        return {"user_id": "mock-user-001", "nickname": "Mock User"}

    # ---- 同构模拟数据 ----
    def fetch_devices(self) -> dict:
        return {"data": {"items": [{"model": "Amazfit GTR 4", "serial_number": "MOCK-DEV-1"}]}}

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
        day = date.today()
        weekday = day.weekday()
        items = []
        if weekday in (0, 2, 5):  # 模拟训练日
            items.append({
                "trackid": f"mock-{sport}-{stop_track_id}",
                "type": 1 if sport == "run" else 6,
                "start_time": f"{day.isoformat()}T07:30:00",
                "end_time": f"{day.isoformat()}T08:20:00",
                "distance": 7000, "calories": 420,
                "avg_hr": 138, "max_hr": 168,
                "training_load": 42, "source": "mock",
            })
        return {"code": 0, "data": {"items": items, "next": -1}}

    def fetch_watch_statistics(self, statistic: str = "SPORT_LOAD", start_day: str = "",
                               end_day: str = "", limit: int = 30, reverse: bool = True) -> dict:
        return {"code": 0, "data": {"items": [
            {"day": (date.today() - timedelta(days=i)).isoformat(), "load": self._rng.randint(20, 80)}
            for i in range(min(limit, 30))
        ]}}

    def fetch_events(self, event_type: str, sub_type: str, from_ms: int, to_ms: int,
                     limit: int = 2000, reverse: bool = True) -> dict:
        items = []
        for i in range(min(limit, 14)):
            ts = date.today() - timedelta(days=i)
            items.append({"ts": ts.isoformat(), "value": 40 + self._rng.randint(0, 30)})
        return {"code": 0, "data": {"items": items}}

    def fetch_hrv(self, start_date: str, end_date: str) -> dict:
        return self.fetch_events("hrv_sdnn", "real_data", 0, 9999999999999, 2000, True)
