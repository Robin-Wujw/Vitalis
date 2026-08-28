"""API 路由。"""
from .connect import router as connect_router
from .health import router as health_router
from .intelligence import router as intelligence_router
from .zepp_pairing import router as zepp_pairing_router

__all__ = ["connect_router", "health_router", "intelligence_router", "zepp_pairing_router"]
