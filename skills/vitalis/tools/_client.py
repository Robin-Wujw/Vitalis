"""Shared HTTP client for Vitalis Skill tools."""

import json
import os

import httpx


API = os.getenv("VITALIS_API", "http://localhost:8000").rstrip("/")


def configured_user() -> str | None:
    return os.getenv("VITALIS_USER")


def request(method: str, path: str, user: str, **kwargs) -> dict | list:
    fixed_user = configured_user()
    if fixed_user and user != fixed_user:
        raise ValueError("请求的用户与 VITALIS_USER 不一致")
    response = httpx.request(
        method,
        f"{API}/api/v1/intelligence/{path.lstrip('/')}",
        headers={"X-User-Id": user},
        timeout=60.0,
        **kwargs,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        if method.upper() == "GET" and response.status_code == 404:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            if detail == "指定日期尚未生成分析快照":
                return {
                    "status": "snapshot_missing",
                    "http_status": 404,
                    "date": (kwargs.get("params") or {}).get("day"),
                }
        raise
    return response.json()


def print_json(payload: dict | list) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
