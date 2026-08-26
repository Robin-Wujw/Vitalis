"""API 路由。"""
from .analyze import router as analyze_router
from .connect import router as connect_router
from .health import router as health_router
from .zepp_pairing import router as zepp_pairing_router

__all__ = ["analyze_router", "connect_router", "health_router", "zepp_pairing_router"]
