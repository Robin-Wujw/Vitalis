"""Activity periods and independently reconciled minute steps from band data."""
import base64
import binascii

from .supplemental import number


def band_activity(row: dict, summary: dict) -> dict:
    steps = summary.get("stp")
    steps = steps if isinstance(steps, dict) else {}
    periods = []
    stages = steps.get("stage")
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict):
            continue
        start, stop = (number(stage.get(k), 0, 1439) for k in ("start", "stop"))
        if start is None or stop is None or stop < start or int(start) != start or int(stop) != stop:
            continue
        count = number(stage.get("step"), 0, 1e6)
        periods.append({"start_minute": int(start), "end_minute_inclusive": int(stop),
            "duration_minutes": stop - start + 1, "steps": count,
            "distance_meters": number(stage.get("dis"), 0, 1e7),
            "mode_code": number(stage.get("mode"), 0, 255),
            "steps_per_minute": count / (stop - start + 1) if count is not None else None,
            "qualification": "vendor_period_unnamed_mode"})
    result = {"activity_periods": periods, "step_goal": number(summary.get("goal"), 1, 1e6),
        "run_distance_raw": number(steps.get("runDist"), 0, 1e9),
        "run_distance_qualification": "unknown_semantics",
        "minute_steps": None, "hourly_steps": None, "qualification": "no_minute_data",
        "source_field": "band_data.data", "unit": "steps", "time_basis": "calendar_day",
        "timezone_offset_seconds": number(summary.get("tz"), -43200, 50400)}
    encoded = row.get("data")
    if not isinstance(encoded, str) or len(encoded) > 20000:
        return result
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        result["qualification"] = "unrecognized_encoding"
        return result
    # Upstream MCP documents only the three-byte layout. Eight-byte archive
    # records remain raw until their specific encoding is independently checked.
    if len(raw) != 1440 * 3:
        result["qualification"] = "unverified_layout"
        result["raw_byte_count"] = len(raw)
        return result
    minute_steps = list(raw[2::3])
    result["raw_minutes"] = {"kind_raw": list(raw[0::3]), "intensity_raw": list(raw[1::3]),
        "step_byte_raw": minute_steps, "qualification": "byte_values_unknown_semantics"}
    total = number(steps.get("ttl"), 0, 1e6)
    if total is None or sum(minute_steps) != total:
        result["qualification"] = "unverified_total_mismatch"
        return result
    if not periods or any(p["steps"] is None or sum(minute_steps[p["start_minute"]:p["end_minute_inclusive"] + 1]) != p["steps"] for p in periods):
        result["qualification"] = "unverified_period_mismatch"
        return result
    if result["timezone_offset_seconds"] is None:
        result["qualification"] = "unknown_timezone"
        return result
    result.update(minute_steps=minute_steps,
        hourly_steps=[sum(minute_steps[i:i + 60]) for i in range(0, 1440, 60)],
        qualification="reconciled_day_and_stages")
    return result
