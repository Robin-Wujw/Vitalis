"""测试配置：强制使用 SQLite + mock Zepp，保证测试离线可跑。"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ZEPP_MOCK"] = "true"
os.environ["VITALIS_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

import fastapi.routing  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import starlette.background  # noqa: E402
import anyio.to_thread  # noqa: E402

from vitalis.adapters.persistence import HealthRepository, init_db, session_scope  # noqa: E402
from vitalis.adapters.persistence.access_tokens import (  # noqa: E402
    ACCESS_SCOPES, provision_access_token,
)


async def _run_sync_route_directly(func, *args, **kwargs):
    """Work around the AnyIO worker-thread deadlock on the Python 3.14 test image."""
    return func(*args, **kwargs)


async def _run_anyio_sync_directly(func, *args, **_kwargs):
    return func(*args)


fastapi.routing.run_in_threadpool = _run_sync_route_directly
starlette.background.run_in_threadpool = _run_sync_route_directly
anyio.to_thread.run_sync = _run_anyio_sync_directly


class ASGITestClient:
    def __init__(self, app):
        self.app = app
        self._tokens: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs):
        authenticate = kwargs.pop("authenticate", True)
        headers = dict(kwargs.get("headers") or {})
        by_name = {key.lower(): value for key, value in headers.items()}
        user_id = by_name.get("x-user-id")
        if authenticate and user_id and "authorization" not in by_name:
            if user_id not in self._tokens:
                with session_scope() as db:
                    HealthRepository(db).upsert_user(user_id)
                    db.flush()
                    self._tokens[user_id] = provision_access_token(
                        db, user_id, ACCESS_SCOPES,
                        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                    )
            headers["Authorization"] = f"Bearer {self._tokens[user_id]}"
            kwargs["headers"] = headers

        async def send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=True,
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def _db():
    # sqlite memory 单连接：所有测试共享一份初始化
    init_db()
    yield


@pytest.fixture()
def issue_token():
    def issue(user_id: str, scopes=ACCESS_SCOPES, *, expires_at=None) -> str:
        expiry = expires_at or datetime.now(timezone.utc) + timedelta(days=1)
        with session_scope() as db:
            HealthRepository(db).upsert_user(user_id)
            db.flush()
            return provision_access_token(db, user_id, scopes, expires_at=expiry)

    return issue


@pytest.fixture()
def client():
    from vitalis.entrypoints.api.app import app

    yield ASGITestClient(app)
