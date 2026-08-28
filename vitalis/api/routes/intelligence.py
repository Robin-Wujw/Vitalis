"""Versioned Vitalis Health Intelligence API."""

from datetime import date

from fastapi import APIRouter, Depends

from vitalis.api.deps import require_user_id
from vitalis.intelligence.contracts import DailyProfile
from vitalis.intelligence.service import IntelligenceService


router = APIRouter(prefix="/intelligence", tags=["intelligence"])


@router.get(
    "/daily-profile",
    response_model=DailyProfile,
    summary="Build a deterministic daily health intelligence profile",
)
def daily_profile(
    day: date | None = None,
    user_id: str = Depends(require_user_id),
) -> DailyProfile:
    return IntelligenceService().daily_profile(user_id, day)
