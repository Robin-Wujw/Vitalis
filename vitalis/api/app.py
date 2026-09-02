"""FastAPI 应用。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import os

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vitalis import __version__
from vitalis.connectors import ConnectorRegistry  # noqa: F401 确保 zepp 连接器注册
from vitalis.storage import init_db

from .routes import connect_router, health_router, intelligence_router, zepp_pairing_router

api = APIRouter(prefix="/api/v1")
api.include_router(connect_router)
api.include_router(zepp_pairing_router)
api.include_router(health_router)
api.include_router(intelligence_router)

log = logging.getLogger("vitalis.api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Prepare storage and recovery dispatch for every supported ASGI launch."""
    init_db()
    scheduler = None
    if os.getenv("VITALIS_NO_SCHEDULER", "0") != "1":
        try:
            from vitalis.scheduler import start_scheduler

            scheduler = start_scheduler()
        except Exception as exc:
            log.warning("scheduler start skipped: %s", exc)
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vitalis Health Agent",
        version=__version__,
        description="个人健康数据平台：标准化数据 + 个人基线 + 确定性决策 + Hermes 渲染",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        from vitalis.connectors import ConnectorRegistry

        return {
            "service": "Vitalis Health Agent",
            "version": __version__,
            "docs": "/docs",
            "available_sources": ConnectorRegistry.available(),
        }

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
