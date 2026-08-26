"""FastAPI 应用。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vitalis import __version__
from vitalis.connectors import ConnectorRegistry  # noqa: F401 确保 zepp 连接器注册
from vitalis.storage import init_db

from .routes import analyze_router, connect_router, health_router, zepp_pairing_router

api = APIRouter(prefix="/api/v1")
api.include_router(connect_router)
api.include_router(zepp_pairing_router)
api.include_router(health_router)
api.include_router(analyze_router)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Prepare persistent storage for every supported ASGI launch command."""
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vitalis Health Agent",
        version=__version__,
        description="个人健康数据平台：数据源插件化 + 统一 Schema + 三层分析引擎 + Hermes Skill",
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
