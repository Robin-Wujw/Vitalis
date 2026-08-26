"""API 依赖。"""

from collections.abc import Iterator

from fastapi import Header, HTTPException

from vitalis.storage import get_session


def get_db() -> Iterator:
    yield from get_session()


def require_user_id(x_user_id: str = Header(default="001", alias="X-User-Id")) -> str:
    """多用户支持：请求头 X-User-Id 标识用户；缺省用 001（演示）。"""
    if not x_user_id.strip():
        raise HTTPException(status_code=400, detail="X-User-Id 不能为空")
    return x_user_id.strip()