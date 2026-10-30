"""API 路由。"""
from .coach import router as coach_router
from .connect import router as connect_router
from .current import router as current_router
from .feedback import router as feedback_router
from .health import router as health_router
from .intelligence import router as intelligence_router
from .zepp_pairing import router as zepp_pairing_router
from .product import router as product_router

__all__ = [
    "coach_router", "connect_router", "current_router", "feedback_router", "health_router",
    "intelligence_router", "zepp_pairing_router", "product_router",
]
