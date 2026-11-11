"""Deterministic statistics over saved, source-qualified workout observations.

No vendor calls, persistence access, inferred missing values, or health scoring.
"""
from collections import defaultdict
from datetime import UTC, date, datetime
from math import isfinite
from statistics import fmean, linear_regression
from zoneinfo import ZoneInfo


METRICS = {
    "distance": ("m", "sum"), "moving_time": ("s", "sum"),
    "pace": ("s/km", "distance_weighted"), "swim_pace": ("s/100m", "distance_weighted"),
    "avg_heart_rate": ("bpm", "moving_time_weighted"), "max_heart_rate": ("bpm", "max"),
    "cadence": ("movements/min", "mean"), "exercise_load": ("vendor_load", "sum"),
    "aerobic_training_effect": ("score_0_5", "mean"), "vo2_max": ("ml/kg/min", "mean"),
    "calories": ("kcal", "sum"), "swolf": ("swolf", "mean"), "strokes": ("strokes", "sum"),
    "distance_per_stroke": ("m", "mean"), "stride_length": ("cm", "mean"),
    "ground_contact_time": ("ms", "mean"), "vertical_oscillation": ("mm", "mean"),
    "power": ("W", "mean"), "elevation_gain": ("m", "sum"), "total_reps": ("reps", "sum"),
}
GROUPS = {"week", "month", "year", "sport", "family", "sport_family", "none"}
_LOWER = {"pace", "swim_pace", "swolf", "ground_contact_time", "vertical_oscillation"}
_DISTANCE_TYPES = {"running", "walking", "cycling", "swimming"}
_SOURCE_FIELDS = {
    "moving_time": "moving_time_seconds", "avg_heart_rate": "heart_rate_avg",
    "max_heart_rate": "heart_rate_max", "exercise_load": "load",
    "aerobic_training_effect": "training_effect", "calories": "calories",
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if isfinite(result) and result >= 0 else None


def sport_family(row):
    category = row.get("type")
    if category and category != "other":
        return category
    mode = row.get("sport_mode") or "unknown"
    if mode in {"badminton", "tennis", "table_tennis", "squash", "padel", "pickleball"}:
        return "racket_sport"
    if mode in {"football", "soccer", "basketball", "volleyball", "baseball", "rugby"}:
        return "team_sport"
    return "unknown"


def sport_identity(row):
    mode = row.get("sport_mode")
    if mode and mode != "unknown":
        return str(mode)
    if row.get("type") and row["type"] != "other":
        return str(row["type"])
    return f"unknown:{row.get('vendor_type_namespace') or ''}:{row.get('vendor_type_id') or ''}"


def matches(row, *, sport=None, family=None, workout_type=None, source=None):
    if source and row.get("source") != source:
        return False
    if workout_type and row.get("type") != workout_type:
        return False
    if family and family not in {sport_family(row), row.get("training_family")}:
        return False
    if sport:
        query = sport.casefold()
        names = (str(row.get("sport_mode") or ""), str(row.get("sport_mode_label") or ""),
                 str(row.get("type") or ""), sport_family(row), str(row.get("training_family") or ""))
        if query.isdecimal():
            return str(row.get("vendor_type_id")) == query
        return any(query in name.casefold() for name in names)
    return True


def _started(row):
    raw = row.get("started_at")
    try:
        stamp = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
        if not isinstance(stamp, datetime):
            return None
        return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
    except ValueError:
        return None


def _value(row, metric):
    if metric in _SOURCE_FIELDS:
        return _number(row.get(_SOURCE_FIELDS[metric]))
    distance = _number(row.get("distance_km"))
    if metric == "distance":
        return None if distance is None else distance * 1000
    if metric in {"pace", "swim_pace"}:
        moving = _number(row.get("moving_time_seconds"))
        kind = row.get("type")
        wanted = kind == "swimming" if metric == "swim_pace" else kind in _DISTANCE_TYPES - {"swimming"}
        if not wanted or not distance or not moving:
            return None
        return moving / distance / (10 if metric == "swim_pace" else 1)
    if metric == "total_reps":
        if row.get("type") != "strength":
            return None
        sets = row.get("strength_sets") or []
        values = [_number(item.get("repetitions")) for item in sets if isinstance(item, dict)]
        if not values:
            values = [_number(item.get("vendor_fields", {}).get("count")) for item in row.get("vendor_sets", [])]
        present = [value for value in values if value is not None]
        return sum(present) if present else None
    if metric in {"swolf", "strokes", "distance_per_stroke"} and row.get("type") != "swimming":
        return None
    if metric in {"stride_length", "ground_contact_time", "vertical_oscillation"} and row.get("type") not in {"running", "walking"}:
        return None
    evidence = row.get("vendor_summary_metrics", {}).get(metric, {})
    expected = METRICS[metric][0]
    if evidence.get("qualification") != "vendor_reported" or evidence.get("unit") != expected:
        return None
    return _number(evidence.get("value"))


def _totals(rows):
    metrics = {}
    sources = {row.get("source") for row in rows}
    sports = {sport_identity(row) for row in rows}
    for metric, (unit, method) in METRICS.items():
        observed = [(row, value) for row in rows if (value := _value(row, metric)) is not None]
        value = None
        used = len(observed)
        limitation = None
        if len(sources) > 1:
            limitation = "multiple_sources_use_partitions"
            used = 0
        elif metric in {"pace", "swim_pace"} and len({sport_family(row) for row, _ in observed}) > 1:
            limitation = "multiple_families_use_partitions"
            used = 0
        elif metric in {"cadence", "swolf", "distance_per_stroke", "stride_length", "ground_contact_time", "vertical_oscillation"} and len(sports) > 1:
            limitation = "multiple_sports_use_partitions"
            used = 0
        elif observed:
            values = [v for _, v in observed]
            if method == "sum":
                value = sum(values)
            elif method == "max":
                value = max(values)
            elif method == "mean":
                value = fmean(values)
            elif method == "distance_weighted":
                distance = sum(row["distance_km"] for row, _ in observed)
                value = sum(row["moving_time_seconds"] for row, _ in observed) / distance / (10 if metric == "swim_pace" else 1)
            else:
                weighted = [(v, _number(row.get("moving_time_seconds"))) for row, v in observed]
                weighted = [(v, weight) for v, weight in weighted if weight and weight > 0]
                used = len(weighted)
                if weighted:
                    value = sum(v * weight for v, weight in weighted) / sum(weight for _, weight in weighted)
                else:
                    limitation = "moving_time_unknown"
        metrics[metric] = {"value": round(value, 6) if value is not None else None,
            "unit": unit, "method": method, "observations": len(observed), "used_observations": used,
            "workouts": len(rows), "coverage": len(observed) / len(rows) if rows else None,
            "qualification": "derived_from_saved_observations", "limitation": limitation}
    return {"workouts": len(rows), "metrics": metrics}


def _point(row, metric):
    return {"workout_id": row.get("workout_id"), "source": row.get("source"),
            "sport": sport_identity(row), "sport_family": sport_family(row),
            "activity_day": row["activity_day"], "value": _value(row, metric),
            "observed_at": row.get("started_at"), "fetched_at": row.get("fetched_at"),
            "source_record_id": row.get("source_record_id")}


def _series_partitions(rows, metric, *, by_family=False):
    partitions = defaultdict(list)
    denominators = defaultdict(int)
    for row in rows:
        family = sport_family(row)
        identity = (("family", family) if family != "unknown" else ("unclassified", sport_identity(row))) if by_family else sport_identity(row)
        key = (row.get("source"), identity)
        denominators[key] += 1
        if _value(row, metric) is not None:
            partitions[key].append(_point(row, metric))
    results = []
    for (source, sport), points in sorted(partitions.items(), key=lambda pair: str(pair[0])):
        values = [point["value"] for point in points]
        result = {"source": source, "sport": sport, "values": points, "observations": len(points),
                  "mean": fmean(values), "minimum": min(values), "maximum": max(values), "trend": None}
        if by_family:
            result.pop("sport")
            result.update(sport_family=points[0]["sport_family"], sports=sorted({point["sport"] for point in points}))
        best = (min if metric in _LOWER else max)(points, key=lambda point: point["value"])
        result["best"] = {**best, "unit": METRICS[metric][0], "scope": "queried_range",
                          "selection": "minimum" if metric in _LOWER else "maximum"}
        days = [date.fromisoformat(point["activity_day"]) for point in points]
        if len(points) >= 3 and len(set(days)) > 1:
            fit = linear_regression([(day - days[0]).days for day in days], values)
            result["trend"] = {"change_per_30_days": round(fit.slope * 30, 6),
                "method": "least_squares_over_activity_day", "observations": len(points),
                "from": days[0].isoformat(), "to": days[-1].isoformat()}
        denominator = denominators[(source, sport)]
        result["workouts"] = denominator
        result["coverage"] = len(points) / denominator
        results.append(result)
    return results


def _series(rows, metric):
    return {"metric": metric, "unit": METRICS[metric][0], "partitions": _series_partitions(rows, metric),
            "family_partitions": _series_partitions(rows, metric, by_family=True),
            "qualification": "descriptive_no_health_direction_inferred"}


def _records(rows):
    partitions = defaultdict(list)
    for row in rows:
        partitions[(row.get("source"), sport_identity(row))].append(row)
    results = []
    for (source, sport), subset in sorted(partitions.items(), key=lambda pair: str(pair[0])):
        records = {}
        for metric in ("distance", "moving_time", "exercise_load", "avg_heart_rate", "calories", "swolf", "total_reps", "pace", "swim_pace"):
            eligible = [row for row in subset if _value(row, metric) is not None]
            if metric in {"pace", "swim_pace"}:
                eligible = [row for row in eligible if (_value(row, "distance") or 0) >= (100 if metric == "swim_pace" else 1000)]
            if eligible:
                found = (min if metric in _LOWER else max)(eligible, key=lambda row: _value(row, metric))
                records[metric] = {**_point(found, metric), "unit": METRICS[metric][0],
                                   "selection": "minimum" if metric in _LOWER else "maximum"}
        jumps = [(row, _number(row.get("vendor_summary_metrics", {}).get("total_jumps", {}).get("value"))) for row in subset]
        jumps = [(row, value) for row, value in jumps if value is not None and "rope" in str(row.get("sport_mode"))]
        if jumps:
            row, value = max(jumps, key=lambda pair: pair[1])
            records["total_jumps"] = {"workout_id": row.get("workout_id"), "value": value, "unit": "jumps"}
        flagged = [{"workout_id": row.get("workout_id"), "activity_day": row["activity_day"],
                    **row["vendor_personal_bests"]} for row in subset if row.get("vendor_personal_bests")]
        results.append({"source": source, "sport": sport, "records": records, "vendor_flagged_personal_bests": flagged})
    return {"scope": "queried_range", "partitions": results,
            "qualification": "observed_range_extrema_not_all_time_records"}


def workout_statistics(workouts, *, start, end, timezone_name, group_by="month", metric=None, truncated=False):
    if start > end or group_by not in GROUPS or metric is not None and metric not in METRICS:
        raise ValueError("invalid workout statistics query")
    zone = ZoneInfo(timezone_name)
    rows = []
    for row in workouts:
        timestamp = _started(row)
        if timestamp is None:
            continue
        day = timestamp.astimezone(zone).date()
        if start <= day <= end:
            rows.append({**row, "activity_day": day.isoformat()})
    rows.sort(key=lambda row: (_started(row), str(row.get("source")), str(row.get("workout_id"))))
    sources, sports, families, periods = (defaultdict(list) for _ in range(4))
    for row in rows:
        source, sport = row.get("source"), sport_identity(row)
        sources[source].append(row)
        sports[(source, sport)].append(row)
        families[(source, sport_family(row))].append(row)
        day = date.fromisoformat(row["activity_day"])
        year, week, _ = day.isocalendar()
        key = {"week": f"{year}-W{week:02d}", "month": day.strftime("%Y-%m"), "year": str(day.year)}.get(group_by)
        if key is not None:
            periods[(source, key)].append(row)
    result = {
        "state": "partial" if truncated else "ready" if rows else "no_data",
        "range": {"from": start.isoformat(), "to": end.isoformat(), "timezone": timezone_name, "day_semantics": "activity_day"},
        "coverage": {"workouts": len(rows), "activity_days_observed": len({row["activity_day"] for row in rows}),
                     "calendar_days": (end - start).days + 1, "truncated": truncated,
                     "scope": "saved_records_only", "vendor_history_complete": None},
        "metric_catalog": {name: {"unit": unit, "aggregation": method} for name, (unit, method) in METRICS.items()},
        "sports_seen": [{"source": key[0], "sport": key[1], "sport_family": sport_family(values[0]),
                         "workouts": len(values), "recognition_confidences": sorted({str(row.get("recognition_confidence") or "unknown") for row in values})}
                        for key, values in sorted(sports.items(), key=str)],
        "overall": _totals(rows),
        "by_source": [{"source": source, **_totals(values)} for source, values in sorted(sources.items(), key=str)],
        "by_sport": [{"source": key[0], "sport": key[1], **_totals(values)} for key, values in sorted(sports.items(), key=str)],
        "by_sport_family": [{"source": key[0], "sport_family": key[1], **_totals(values)} for key, values in sorted(families.items(), key=str)],
        "records": _records(rows),
    }
    if group_by in {"week", "month", "year"}:
        result["by_period"] = {"group_by": group_by, "periods": [{"source": key[0], "period": key[1], **_totals(values)} for key, values in sorted(periods.items(), key=str)]}
    if metric:
        result["series"] = _series(rows, metric)
    return result
