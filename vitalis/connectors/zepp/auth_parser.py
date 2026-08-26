"""Zepp 登录 cookie 解析器：翻译自 ZeppBridge login.rs。

支持三种输入格式：
  1. 完整的 hm-user-login-info JSON（URL 编码或纯 JSON）
  2. 独立的 userid + apptoken cookie
  3. HAR 文件导入（未来扩展）
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from vitalis.connectors.zepp.client import validate_region_host


@dataclass
class ExtractedAuth:
    """从 cookie 中提取的认证信息。"""

    user_id: str
    app_token: str
    region_hint: str | None = None


# 区域主机白名单（对齐 ZeppBridge）
REGION_HOST_ALLOWLIST = [
    "https://api-mifit-cn.huami.com",
    "https://api-mifit-cn2.huami.com",
    "https://api-mifit-cn.zepp.com",
    "https://api-mifit-cn2.zepp.com",
    "https://api-mifit-cn3.zepp.com",
    "https://api-mifit.huami.com",
    "https://api-mifit.zepp.com",
    "https://api-mifit-us.huami.com",
    "https://api-mifit-us2.huami.com",
    "https://api-mifit-us3.zepp.com",
    "https://api-mifit-de.huami.com",
    "https://api-mifit-de.zepp.com",
    "https://api-mifit-sg.huami.com",
    "https://api-mifit-in.huami.com",
    "https://api-mifit-ru.huami.com",
]


def percent_decode(value: str) -> str:
    """简单 percent-decode（处理 %2C %7B 等）。"""
    # 先处理常见编码
    replaced = value.replace("+", " ").replace("%2C", ",").replace("%2c", ",")
    try:
        return urllib.parse.unquote(replaced)
    except Exception:
        return replaced


def _decode_possibly_encoded(raw: str) -> str:
    """双层解码：先 percent-decode，如果还有 % 再 decode 一次。"""
    first = percent_decode(raw.strip().strip('"'))
    if "%" in first:
        return percent_decode(first)
    return first


def _json_string(value: dict, keys: list[str]) -> str | None:
    """从 dict 中按优先级找字符串值。"""
    for key in keys:
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return None


def _sanitize_user_id(value: str) -> str | None:
    v = value.strip()
    if not v or len(v) > 256 or not re.match(r"^[\w-]+$", v):
        return None
    return v


def _sanitize_app_token(value: str) -> str | None:
    v = value.strip()
    if not v or len(v) > 16 * 1024 or any(ord(c) < 32 for c in v):
        return None
    return v


def extract_from_login_info(raw: str) -> ExtractedAuth | None:
    """从 hm-user-login-info 的值中提取认证信息。

    支持 URL 编码和普通 JSON 两种格式。
    """
    decoded = _decode_possibly_encoded(raw)
    try:
        root: dict = json.loads(decoded)
    except json.JSONDecodeError:
        return None

    # token_info 可能是字符串（嵌套 JSON）或对象
    token_info = root.get("token_info")
    if isinstance(token_info, str):
        try:
            token_info = json.loads(_decode_possibly_encoded(token_info))
        except json.JSONDecodeError:
            return None
    elif not isinstance(token_info, dict):
        # 兜底：根对象本身可能就是 token_info 的结构
        token_info = root

    user_id = _json_string(token_info, ["user_id", "userid", "userId"])
    app_token = _json_string(token_info, ["app_token", "apptoken", "appToken", "app-token"])
    region_hint = (
        _json_string(token_info, ["region", "region_host", "host", "domain", "api_host"])
        or _json_string(root, ["region", "region_host", "host", "domain", "api_host"])
    )

    if not user_id or not app_token:
        return None

    user_id = _sanitize_user_id(user_id)
    app_token = _sanitize_app_token(app_token)
    if not user_id or not app_token:
        return None

    return ExtractedAuth(user_id=user_id, app_token=app_token, region_hint=region_hint)


def extract_from_cookie_pairs(pairs: dict[str, str]) -> ExtractedAuth | None:
    """从 cookie 名值对中提取认证信息。"""
    # 优先尝试 hm-user-login-info（包含最完整信息）
    for key in ("hm-user-login-info", "hm_user_login_info"):
        if key in pairs:
            if (extracted := extract_from_login_info(pairs[key])) is not None:
                return extracted

    # 兜底：独立的 userid + apptoken
    user_id = None
    for key in ("userid", "user_id", "userId"):
        if key in pairs:
            user_id = _sanitize_user_id(percent_decode(pairs[key]))
            if user_id:
                break

    app_token = None
    for key in ("apptoken", "app_token", "app-token", "appToken"):
        if key in pairs:
            app_token = _sanitize_app_token(percent_decode(pairs[key]))
            if app_token:
                break

    if user_id and app_token:
        return ExtractedAuth(user_id=user_id, app_token=app_token)
    return None


def hosts_from_region_hint(hint: str | None) -> list[str]:
    """从 region hint 映射到允许的区域主机列表。"""
    if not hint:
        return []
    trimmed = hint.strip().lower()
    if not trimmed:
        return []

    # 如果已经是完整 URL，直接验证
    try:
        host = validate_region_host(trimmed)
        return [host]
    except Exception:
        pass

    # 从 hint 中提取区域 token（cn/cn2/cn3/us/de/sg/in/ru）
    token = None
    for part in reversed(re.split(r"[/\.\-_]", trimmed)):
        if part in ("cn", "cn2", "cn3", "us", "us2", "us3", "de", "sg", "in", "ru"):
            token = part
            break

    if not token:
        return []

    return [h for h in REGION_HOST_ALLOWLIST if f"-{token}." in h]


def preferred_region_hosts(saved_host: str | None, region_hint: str | None) -> list[str]:
    """构建探测用区域主机列表（优先级排序）。"""
    hosts: list[str] = []

    def push(host: str) -> None:
        try:
            h = validate_region_host(host)
            if h not in hosts:
                hosts.append(h)
        except Exception:
            pass

    if saved_host:
        push(saved_host)
    for h in hosts_from_region_hint(region_hint):
        push(h)
    for h in REGION_HOST_ALLOWLIST:
        push(h)
    return hosts
