"""FastAPI 应用。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from vitalis import __version__
from vitalis.bootstrap import available_sources
from vitalis.config import settings
from vitalis.adapters.persistence.database import SchemaMismatch, check_schema

from .errors import ERROR_RESPONSES, error_response
from .routes import (
    connect_router,
    current_router,
    health_router,
    intelligence_router,
    zepp_pairing_router,
)

log = logging.getLogger("vitalis.entrypoints.api")

api = APIRouter(prefix="/api", responses=ERROR_RESPONSES)
api.include_router(current_router)
api.include_router(connect_router)
api.include_router(zepp_pairing_router)
api.include_router(health_router)
api.include_router(intelligence_router)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Refuse uninitialized or mismatched storage before accepting traffic."""
    check_schema()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vitalis Health Agent",
        version=__version__,
        description="个人健康数据平台：标准化数据 + 个人基线 + 确定性决策 + Hermes 渲染",
        lifespan=lifespan,
    )
    @app.middleware("http")
    async def public_errors(request: Request, call_next) -> Response:
        request_id = uuid4().hex
        request.state.request_id = request_id
        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        try:
            response = await call_next(request)
        except Exception as exc:
            if not is_api:
                raise
            log.warning("API request failed: request_id=%s type=%s", request_id, type(exc).__name__)
            response = error_response(500, request_id)
        else:
            if is_api and response.status_code >= 400:
                retry_after = response.headers.get("Retry-After")
                response = error_response(
                    response.status_code, request_id,
                    www_authenticate=response.headers.get("WWW-Authenticate") == "Bearer",
                )
                if retry_after is not None:
                    response.headers["Retry-After"] = retry_after
        response.headers["X-Request-ID"] = request_id
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.pairing_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-User-Id"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.include_router(api)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": "Vitalis Health Agent",
            "version": __version__,
            "docs": "/docs",
            "available_sources": available_sources(),
        }

    @app.get("/live", include_in_schema=False)
    def live() -> dict:
        return {"status": "live"}

    @app.get("/ready", include_in_schema=False)
    def ready() -> dict:
        try:
            check_schema()
        except SchemaMismatch as exc:
            raise HTTPException(status_code=503, detail="数据库 schema 未就绪") from exc
        return {"status": "ready"}

    return app


app = create_app()
