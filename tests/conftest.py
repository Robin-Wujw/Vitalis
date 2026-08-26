"""测试配置：强制使用 SQLite + mock Zepp，保证测试离线可跑。"""

import asyncio
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ZEPP_MOCK"] = "true"
os.environ["VITALIS_NO_SCHEDULER"] = "1"

import fastapi.routing  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import starlette.background  # noqa: E402
import anyio.to_thread  # noqa: E402

from vitalis.storage import init_db  # noqa: E402


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

    def request(self, method: str, url: str, **kwargs):
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
def client():
    from vitalis.api.app import app

    yield ASGITestClient(app)
