"""Source-qualified arithmetic over reported activity periods, never mode names."""
from collections import defaultdict
from math import isfinite

from .observation_statistics import ObservationBudgetExceeded


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0 else None


def activity_mode_totals(days, *, start, end):
    groups = defaultdict(list)
    for day in days:
        for period in day.get("band_activity", {}).get("activity_periods", []):
            if not isinstance(period, dict):
                continue
            key = (day.get("source"), day.get("source_scope"), day.get("device_id"),
                   period.get("qualification"), period.get("mode_code"))
            if key not in groups and len(groups) >= 1000:
                raise ObservationBudgetExceeded("activity mode groups exceed budget")
            groups[key].append({**period, "date": day["date"], "fetched_at": day.get("fetched_at")})
    result = []
    for key, periods in sorted(groups.items(), key=lambda pair: str(pair[0])):
        spans = defaultdict(list)
        invalid = False
        for period in periods:
            lower, upper = (_number(period.get(field)) for field in ("start_minute", "end_minute_inclusive"))
            if lower is None or upper is None or upper < lower or upper >= 1440:
                invalid = True
            else:
                spans[period["date"]].append((lower, upper))
        overlap = False
        for values in spans.values():
            high = -1
            for lower, upper in sorted(values):
                overlap |= lower <= high
                high = max(high, upper)
        metrics = {}
        for name, field, unit in (("minutes", "duration_minutes", "min"), ("steps", "steps", "steps"),
                                  ("distance_meters", "distance_meters", "m")):
            values = [value for period in periods if (value := _number(period.get(field))) is not None]
            metrics[name] = {"total": sum(values) if values and not overlap and not invalid else None,
                "unit": unit, "observed_periods": len(values), "missing_periods": len(periods) - len(values),
                "total_qualification": "overlapping_periods" if overlap else "invalid_period_bounds" if invalid else
                    "observed_periods_only" if values else "no_observed_values"}
        matching = [(period.get("steps"), period.get("duration_minutes")) for period in periods
                    if _number(period.get("steps")) is not None and (_number(period.get("duration_minutes")) or 0) > 0]
        rate = sum(value for value, _ in matching) / sum(minutes for _, minutes in matching) if matching and not overlap and not invalid else None
        result.append({"source": key[0], "source_scope": key[1], "device_id": key[2], "qualification": key[3],
            "mode_code": key[4], "source_field": "band_data.stp.stage", "time_basis": "calendar_day",
            "period_count": len(periods), **metrics,
            "steps_per_minute": {"value": rate, "unit": "steps/min", "observed_periods": len(matching),
                "method": "total_steps_over_duration_of_complete_period_pairs"},
            "coverage": {"days_with_data": len({period["date"] for period in periods}),
                "days_requested": (end - start).days + 1, "scope": "reported_activity_periods_only"},
            "from": min(period["date"] for period in periods), "to": max(period["date"] for period in periods),
            "fetched_at": max((period["fetched_at"] for period in periods if period.get("fetched_at")), default=None)})
    return result
