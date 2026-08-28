"""Shared HTTP client for Vitalis Skill tools."""

import json
import os

import httpx


API = os.getenv("VITALIS_API", "http://localhost:8000").rstrip("/")


def configured_user() -> str | None:
    return os.getenv("VITALIS_USER")


def request(method: str, path: str, user: str, **kwargs) -> dict | list:
    response = httpx.request(
        method,
        f"{API}/api/v1/intelligence/{path.lstrip('/')}",
        headers={"X-User-Id": user},
        timeout=60.0,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def print_json(payload: dict | list) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
