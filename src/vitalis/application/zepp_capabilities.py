"""Public mapping of the pinned MCP query contract to Vitalis HTTP resources."""
MCP_REFERENCE = "https://github.com/DhavalBhimani44/zepp-mcp/tree/904cc301d63a64da6f633dd8a75b2a612e67240a"
TOOLS = {
    "zepp_daily_summary": {"endpoints": ["GET /api/health/daily-summary"], "options": ["from", "to", "include_hourly", "include_minutes"]},
    "zepp_heart_rate": {"endpoints": ["GET /api/health/heart-rate"], "options": ["day", "include_series"]},
    "zepp_sleep": {"endpoints": ["GET /api/health/sleep"], "options": ["from", "to"]},
    "zepp_list_workouts": {"endpoints": ["GET /api/workouts"], "options": ["from", "to", "sport", "family", "type", "source", "limit", "offset"]},
    "zepp_workout_detail": {"endpoints": ["GET /api/workouts/{workout_id}/sections"], "options": ["source", "include"]},
    "zepp_body_composition": {"endpoints": ["GET /api/health/body-composition"], "options": ["from", "to", "limit", "before", "cursor"]},
    "zepp_training_thresholds": {"endpoints": ["GET /api/health/training-thresholds"], "options": ["from", "to"]},
    "zepp_health_metrics": {"endpoints": ["GET /api/health/summary/{category}"], "options": ["from", "to", "include_samples"]},
    "zepp_workout_stats": {"endpoints": ["GET /api/workouts/statistics"], "options": ["from", "to", "sport", "group_by", "metric", "family", "source"]},
    "zepp_period_summary": {"endpoints": ["GET /api/health/period-summary"], "options": ["from", "to"]},
    "zepp_profile": {"endpoints": ["GET /api/sources/zepp/profile"], "options": ["include_devices"]},
    "zepp_describe_schema": {"endpoints": ["GET /api/sources/zepp/schema"], "options": []},
    "zepp_raw_request": {"endpoints": ["POST /api/sources/zepp/raw-requests", "GET /api/sources/zepp/raw-requests/{job_id}",
        "GET /api/sources/zepp/raw-records", "GET /api/sources/zepp/raw-records/{record_id}"],
        "options": ["path", "params"], "scope": "source_raw", "execution": "durable_authenticated_GET"},
    "zepp_auth_status": {"endpoints": ["GET /api/health/token-status"], "options": []},
}


def schema(*, sports, wellness, workout):
    from .health_analytics import BODY_METRICS, HEALTH_CATEGORIES
    return {"reference": MCP_REFERENCE, "tools": TOOLS, "sports": sports,
        "health_categories": HEALTH_CATEGORIES, "body_metrics": BODY_METRICS,
        "wellness": wellness, "workouts": workout,
        "availability": "capability_is_independent_of_account_data; inspect /api/data-status",
        "qualifications": {"vendor_recorded": "reported by the vendor; not independently measured",
            "vendor_reference": "vendor estimate; not Vitalis decision permission",
            "community_schema": "field and unit from community schema; not verified on this account",
            "inconsistent_record": "vendor BMI disagrees with the same record's weight and height",
            "raw_unverified": "readable original representation; semantics or encoding unresolved",
            "reconciled_day_and_stages": "three-byte minute steps match both the daily total and stage totals"},
        "activity_modes": {"representation": "vendor numeric mode; no inferred activity label"},
        "known_gaps": ["8-byte activity payloads do not become verified minute steps",
            "muscleRate and oneFootMeasureTime units are unknown",
            "GPS streams retain their vendor encoding; no guessed coordinates",
            "Charge stress_data/insight_data and readiness watch_score_data remain raw",
            "Auth country_code is not saved as public metadata; do not infer it from region_host",
            "A successful empty vendor response does not prove the user performed no activity"],
        "read_contract": {"network": "only explicit sync/raw jobs contact Zepp", "empty": "no_data",
            "missing": "null_or_absent_never_zero", "windows": "local inclusive dates, UTC sample intervals half-open"}}
