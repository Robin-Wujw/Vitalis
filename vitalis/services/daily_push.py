"""Run one explicit-user morning pipeline through the public Vitalis API."""

from __future__ import annotations

import httpx

from vitalis.services.push_service import PushService


def run_morning_push(
    user_id: str,
    pushplus_token: str,
    *,
    api: str = "http://localhost:8000",
    sync_days: int = 2,
) -> dict:
    if not user_id:
        raise ValueError("VITALIS_USER is required")
    if not pushplus_token:
        raise ValueError("PUSHPLUS_TOKEN is required")

    headers = {"X-User-Id": user_id}
    with httpx.Client(base_url=api.rstrip("/"), headers=headers, timeout=180.0) as client:
        sync_response = client.post("/api/v1/health/sync", params={"days": sync_days})
        sync_response.raise_for_status()
        sync = sync_response.json()
        if sync.get("status") != "synced" or sync.get("success") is not True:
            raise RuntimeError(
                f"Vitalis sync did not complete: {sync.get('status', 'unknown')}"
            )

        analysis_response = client.post("/api/v1/intelligence/analyze")
        analysis_response.raise_for_status()
        daily = analysis_response.json()["daily"]

    results = PushService(pushplus_token=pushplus_token).push_daily_profile(
        user_id, daily, period="morning"
    )
    if results.get("_pushplus_handler") != "ok":
        raise RuntimeError("PushPlus delivery failed")
    return {
        "status": "sent",
        "date": daily["date"],
        "quality": daily["data_quality"]["status"],
    }
