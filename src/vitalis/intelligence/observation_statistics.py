"""Descriptive statistics over qualified observations, never missing-day estimates."""
from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import fmean, linear_regression
from zoneinfo import ZoneInfo

from vitalis.time import local_day

IDENTITY = ("source", "source_scope", "device_id", "unit", "source_field", "qualification")


class ObservationBudgetExceeded(ValueError):
    """The source identities or observed day cells exceed the query budget."""


def iso(value):
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()
    return value.isoformat() if isinstance(value, date) else value


def describe(values):
    return {"sample_count": len(values), "mean": fmean(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def trend(days, field="mean"):
    points = [(date.fromisoformat(d["date"]), d[field]) for d in days if d[field] is not None]
    if len(points) < 3 or len({day for day, _ in points}) < 2:
        return None
    origin = min(day for day, _ in points)
    fit = linear_regression([(day - origin).days for day, _ in points], [value for _, value in points])
    return {"method": "least_squares_on_observed_daily_means" if field == "mean" else "least_squares_on_observed_daily_maxima",
            "change_per_30_days": fit.slope * 30,
            "sample_count": len(points), "from": iso(origin), "to": iso(max(day for day, _ in points))}


def grouped_observations(rows, *, start, end, timezone_name, include_samples=False, sample_limit=2000,
                         include_daily_totals=False, day_limit=50_000, include_daily_endpoints=False):
    """Stream all input, bound optional samples, partition every incompatible identity."""
    partitions = {}
    returned = 0
    observed_days = 0
    for row in rows:
        day = iso(row.get("date") or local_day(row["timestamp"], timezone_name))
        if not iso(start) <= day <= iso(end):
            continue
        key = tuple(row.get(field) for field in IDENTITY)
        if key not in partitions:
            if len(partitions) >= 1000:
                raise ObservationBudgetExceeded("observation source groups exceed budget")
            partitions[key] = {"days": {}, "samples": [], "count": 0, "first": None, "last": None}
        group = partitions[key]
        if day not in group["days"]:
            observed_days += 1
            if observed_days > day_limit:
                raise ObservationBudgetExceeded("observed source days exceed budget")
        values = group["days"].setdefault(day, {"count": 0, "total": 0, "min": row["value"], "max": row["value"]})
        values["count"] += 1
        values["total"] += row["value"]
        values["min"] = min(values["min"], row["value"])
        values["max"] = max(values["max"], row["value"])
        group["count"] += 1
        observed = iso(row.get("timestamp") or row.get("observed_at") or row.get("date"))
        if include_daily_endpoints and observed:
            for label, better in (("first", lambda a, b: a < b), ("last", lambda a, b: a > b)):
                at = label + "_observed_at"
                if at not in values or better(observed, values[at]):
                    values.update({at: observed, label: row["value"], label + "_ambiguous": False})
                elif observed == values[at] and row["value"] != values[label]:
                    values[label] = None
                    values[label + "_ambiguous"] = True
        if observed and (group["first"] is None or observed < group["first"]):
            group["first"] = observed
        if observed and (group["last"] is None or observed > group["last"]):
            group["last"] = observed
        if include_samples and returned < sample_limit:
            group["samples"].append({name: iso(value) for name, value in row.items()})
            returned += 1
    result = []
    for key, group in sorted(partitions.items(), key=lambda pair: str(pair[0])):
        days = [{"date": day, "sample_count": values["count"], "mean": values["total"] / values["count"],
                 "min": values["min"], "max": values["max"],
                 **({key: values.get(key) for key in ("first", "last", "first_observed_at", "last_observed_at",
                     "first_ambiguous", "last_ambiguous")} if include_daily_endpoints else {})}
                for day, values in sorted(group["days"].items())]
        count = group["count"]
        daily_totals = {}
        if include_daily_totals:
            ambiguous = sum(values["count"] != 1 for values in group["days"].values())
            daily_totals = {
                "total": None if ambiguous else sum(values["total"] for values in group["days"].values()),
                "total_qualification": "ambiguous_daily_records" if ambiguous else "observed_days_only",
                "ambiguous_days": ambiguous,
            }
        result.append({**dict(zip(IDENTITY, key)), "days": days,
            "summary": {"sample_count": count, "mean": sum(v["total"] for v in group["days"].values()) / count,
                "mean_of_observed_days": fmean(d["mean"] for d in days),
                "min": min(d["min"] for d in days), "max": max(d["max"] for d in days), **daily_totals},
            "trend": trend(days), "first_observed_at": group["first"], "last_observed_at": group["last"],
            **({"daily_max_summary": describe([d["max"] for d in days]),
                "daily_max_trend": trend(days, "max")} if include_daily_endpoints else {}),
            "coverage": {"days_with_data": len(days), "days_requested": (end - start).days + 1,
                "day_fraction": len(days) / ((end - start).days + 1), "within_day_coverage": None,
                "scope": "saved_observations"},
            **({"samples": group["samples"], "samples_truncated": len(group["samples"]) < count} if include_samples else {})})
    return result


def categorical_observations(rows, *, start, end, timezone_name, day_limit=50_000, include_samples=False):
    """Preserve vendor enums verbatim; codes have no numerical mean or trend."""
    groups = {}
    cells = 0
    for row in rows:
        day = iso(row.get("date") or local_day(row["timestamp"], timezone_name))
        if not iso(start) <= day <= iso(end):
            continue
        key = tuple(row.get(field) for field in IDENTITY)
        if key not in groups:
            if len(groups) >= 1000:
                raise ObservationBudgetExceeded("observation source groups exceed budget")
            groups[key] = {}
        if day not in groups[key]:
            cells += 1
            if cells > day_limit:
                raise ObservationBudgetExceeded("observed source days exceed budget")
            groups[key][day] = []
        groups[key][day].append({name: iso(value) for name, value in row.items()})
    days_requested = (end - start).days + 1
    return [{**dict(zip(IDENTITY, key)),
        "days": [{"date": day, "sample_count": len(values), "observations": values} for day, values in sorted(days.items())],
        "summary": {"sample_count": sum(map(len, days.values()))},
        "coverage": {"days_with_data": len(days), "days_requested": days_requested,
            "day_fraction": len(days) / days_requested, "scope": "saved_observations"}}
        for key, days in sorted(groups.items(), key=lambda pair: str(pair[0]))]


def endpoint_changes(rows, *, start, end, timezone_name):
    """Compare unique first/last observed days within each exact identity."""
    groups = defaultdict(lambda: defaultdict(list))
    for row in rows:
        day = iso(row.get("date") or local_day(row["timestamp"], timezone_name))
        if iso(start) <= day <= iso(end):
            groups[tuple(row.get(field) for field in IDENTITY)][day].append(row)
    result = []
    for key, days in sorted(groups.items(), key=lambda pair: str(pair[0])):
        first, last = min(days), max(days)
        ambiguous = len(days[first]) != 1 or len(days[last]) != 1
        qualified = not ambiguous and first != last
        result.append({**dict(zip(IDENTITY, key)), "from": first, "to": last,
            "first_value": days[first][0]["value"] if len(days[first]) == 1 else None,
            "last_value": days[last][0]["value"] if len(days[last]) == 1 else None,
            "change": days[last][0]["value"] - days[first][0]["value"] if qualified else None,
            "comparison_qualification": "ambiguous_endpoint_records" if ambiguous else
                "observed_endpoint_difference" if qualified else "insufficient_observed_days",
            "sample_count": sum(map(len, days.values())),
            "coverage": {"days_with_data": len(days), "days_requested": (end - start).days + 1,
                "scope": "saved_observations"},
            "first_observations": [{name: iso(value) for name, value in row.items()} for row in days[first]],
            "last_observations": [{name: iso(value) for name, value in row.items()} for row in days[last]]})
    return result


def goal_comparisons(rows, *, start, end):
    """Only unique source/qualification candidates can form a daily goal pair."""
    groups = defaultdict(lambda: defaultdict(list))
    for row in rows:
        key = (iso(row["date"]), row["source"], row["source_scope"], row["device_id"])
        if not iso(start) <= key[0] <= iso(end):
            continue
        candidates = groups[key][row["metric"]]
        if row not in candidates:
            candidates.append(row)
    result, summary = [], {}
    for key, metrics in sorted(groups.items(), key=lambda pair: str(pair[0])):
        for observed, goal, unit, meaning in (("steps", "step_goal", "steps", "steps"),
            ("calories", "calorie_goal", "kcal", "calories"),
            ("vendor_burning_minutes", "active_minutes_goal", "min", "zepp_heart_rate_zone_minutes")):
            values, targets = metrics.get(observed, []), metrics.get(goal, [])
            if not values and not targets:
                continue
            identity = (*key[1:], observed)
            count = summary.setdefault(identity, {"source": key[1], "source_scope": key[2], "device_id": key[3],
                "metric": observed, "goal_metric": goal, "compared_days": 0, "achieved_days": 0,
                "ambiguous_days": 0, "days_with_inputs": 0, "days_requested": (end - start).days + 1})
            count["days_with_inputs"] += 1
            if len(values) > 1 or len(targets) > 1:
                count["ambiguous_days"] += 1
                continue
            if not values or not targets:
                continue
            value, target = values[0], targets[0]
            if value["unit"] != unit or target["unit"] != unit or target["value"] <= 0:
                continue
            achieved = value["value"] >= target["value"]
            count["compared_days"] += 1
            count["achieved_days"] += achieved
            result.append({"date": key[0], "source": key[1], "source_scope": key[2], "device_id": key[3],
                "metric": observed, "goal_metric": goal, "meaning": meaning, "value": value["value"],
                "target": target["value"], "unit": unit, "achieved": achieved,
                "ratio": value["value"] / target["value"], "observation": value, "goal": target})
    for count in summary.values():
        count["missing_days"] = count["days_requested"] - count["compared_days"] - count["ambiguous_days"]
        count["coverage"] = count["compared_days"] / count["days_requested"]
        count["state"] = "partial" if count["coverage"] < 1 else "ready"
    return {"state": "partial" if any(c["state"] == "partial" for c in summary.values()) else "ready" if result else "no_data",
            "comparisons": result, "summary": list(summary.values()), "time_basis": "calendar_day",
            "qualification": "missing_or_ambiguous_is_unknown_not_not_achieved"}


def heart_rate_minutes(rows, *, timezone_name, include_series=False):
    """Rebuild only the documented band minute grid; preserve gaps and conflicts."""
    groups = {}
    zone = ZoneInfo(timezone_name)
    for row in rows:
        if row.get("source_field") != "band_data.data_hr" or row.get("unit") != "bpm":
            continue
        stamp = row["timestamp"]
        local = (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(zone)
        key = (local.date().isoformat(), *(row.get(field) for field in IDENTITY))
        bucket = groups.setdefault(key, {"minutes": [None] * 1440, "conflicts": set()})
        minute = local.hour * 60 + local.minute
        previous = bucket["minutes"][minute]
        if minute in bucket["conflicts"]:
            continue
        if previous is not None and previous != row["value"]:
            bucket["conflicts"].add(minute)
            bucket["minutes"][minute] = None
        else:
            bucket["minutes"][minute] = row["value"]
    result = []
    for key, bucket in sorted(groups.items(), key=lambda pair: str(pair[0])):
        present = [(index, value) for index, value in enumerate(bucket["minutes"]) if value is not None]
        result.append({"date": key[0], **dict(zip(IDENTITY, key[1:])),
            **describe([value for _, value in present]), "minutes_covered": len(present),
            "minute_conflicts": len(bucket["conflicts"]),
            "first_reading_minute": present[0][0] if present else None,
            "last_reading_minute": present[-1][0] if present else None,
            **({"per_minute": bucket["minutes"]} if include_series else {})})
    return result
