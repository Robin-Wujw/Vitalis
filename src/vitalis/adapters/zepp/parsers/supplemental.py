"""Pure, evidence-bounded decoding of Zepp supplemental observations.

Protocol reference: zepp-mcp 904cc301d63a64da6f633dd8a75b2a612e67240a.
Independently implemented; no account identifiers or captured values belong here.
"""

from datetime import datetime, time, timedelta, timezone
from math import isfinite

from vitalis.domain import DailyMetric, MetricSample
from vitalis.time import local_day


def number(value: object, minimum: float, maximum: float) -> float | None:
    """Accept finite vendor numbers, never booleans or arbitrary coercions."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) and minimum <= parsed <= maximum else None


def sleep_timing(sleep: dict, zone, asleep_minutes: int) -> dict:
    """Require both local-minute identities before calling a span time in bed."""
    latency = number(sleep.get("is"), 0, 1440)
    reported = {"sleep_latency_minutes": latency} if latency is not None else {}
    values = [number(sleep.get(key), 1, 1e11) for key in ("st", "ed")]
    if any(value is None for value in values) or values[1] <= values[0]:
        return reported
    try:
        start, end = (datetime.fromtimestamp(value, timezone.utc) for value in values)
        midnight = datetime.combine(end.astimezone(zone).date(), time.min)
    except (OverflowError, OSError, ValueError):
        return reported
    delay = number(sleep.get("lb"), 0, 1440)
    into = number(sleep.get("obt"), -1440, 1440)
    out = number(sleep.get("ebt"), -1440, 2880)
    if any(value is None for value in (latency, delay, into, out)):
        return reported
    bed = start - timedelta(minutes=latency)
    leave = end + timedelta(minutes=delay)
    offsets = [(value.astimezone(zone).replace(tzinfo=None) - midnight).total_seconds() / 60
        for value in (bed, leave)]
    if abs(offsets[0] - into) > 1 or abs(offsets[1] - out) > 1:
        return reported
    minutes = (leave - bed).total_seconds() / 60
    if not 0 < asleep_minutes <= minutes <= 1440:
        return reported
    return {
        "in_bed_at": bed, "out_of_bed_at": leave,
        "sleep_latency_minutes": latency, "out_of_bed_delay_minutes": delay,
        "time_in_bed_minutes": minutes,
        "sleep_efficiency_percent": asleep_minutes / minutes * 100,
    }


def workout_supplements(row: dict, *, training_family: str | None = None) -> dict:
    """Preserve each time denominator and keep inconsistent components unknown."""
    from .workout_extras import summary_extras

    fields = summary_extras(row, training_family=training_family)
    for normalized, vendor in (("moving_time_seconds", "exerciseTimeWithMillis"),
        ("paused_time_seconds", "pauseTimeWithMillis"),
        ("elapsed_time_seconds", "totalTimeWithMillis")):
        raw = number(row.get(vendor), 0, 7 * 86400 * 1000)
        if raw is not None:
            fields[normalized] = raw / 1000
    if all(key in fields for key in ("elapsed_time_seconds", "moving_time_seconds", "paused_time_seconds")) and abs(fields["elapsed_time_seconds"] - fields["moving_time_seconds"] - fields["paused_time_seconds"]) > 0.01:
        fields.pop("moving_time_seconds")
        fields.pop("paused_time_seconds")
    for normalized, vendor, lower, upper, scale in (
        ("anaerobic_training_effect", "anaerobic_te", 0, 50, 0.1),
        ("equivalent_pace_seconds_per_km", "avgEquivPace", 1, 36000, 1),
        ("best_equivalent_pace_seconds_per_km", "bestEquivPace", 1, 36000, 1),
    ):
        raw = number(row.get(vendor), lower, upper)
        if raw is not None:
            fields[normalized] = round(raw * scale, 6)
    return fields


def weight_samples(items: list[dict]) -> list[MetricSample]:
    """Only explicitly identified holder records; BMI requires its own check."""
    output = []
    for item in items:
        if type(item.get("memberId")) not in (int, str) or str(item["memberId"]) != "-1":
            continue
        epoch = number(item.get("generatedTime"), 1, 1e11)
        summary = item.get("summary")
        if epoch is None or not isinstance(summary, dict):
            continue
        try:
            timestamp = datetime.fromtimestamp(epoch, timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        weight = number(summary.get("weight"), 1, 1000)
        height = number(summary.get("height"), 30, 300)
        vendor_bmi = bmi = number(summary.get("bmi"), 1, 200)
        inconsistent = (weight is not None and height is not None and bmi is not None
                        and abs(bmi - weight / (height / 100) ** 2) > 0.15)
        if weight is None or height is None or inconsistent:
            bmi = None
        for metric, value, unit in (("weight", weight, "kg"), ("height", height, "cm"), ("bmi", bmi, "kg/m2")):
            if value is not None:
                output.append(MetricSample(metric=metric, timestamp=timestamp,
                    value=value, unit=unit, source_scope="user_summary",
                    source_field=f"summary.{metric}", qualification="vendor_recorded"))
        if vendor_bmi is not None and bmi is None:
            output.append(MetricSample(metric="vendor_bmi", timestamp=timestamp,
                value=vendor_bmi, unit="kg/m2", source_scope="user_summary",
                source_field="summary.bmi", qualification="inconsistent_record" if inconsistent else "unchecked_record"))
        for field, metric, unit in BODY_FIELDS:
            value = number(summary.get(field), 0.000001, 1e6)
            if value is not None:
                output.append(MetricSample(metric=metric, timestamp=timestamp, value=value,
                    unit=unit, source_scope="user_summary", source_field=f"summary.{field}",
                    qualification="community_schema_inconsistent_record" if inconsistent else "community_schema"))
        for field in ("impedance", "encryptImpedance"):
            value = number(summary.get(field), 0.000001, 1e6)
            if value is not None:
                output.append(MetricSample(metric="impedance", timestamp=timestamp, value=value,
                    unit="ohm", source_scope="user_summary", source_field=f"summary.{field}",
                    qualification="community_schema_inconsistent_record" if inconsistent else "community_schema"))
                break
    return output


# Units are community schema claims, not independent device verification.
# muscleRate and oneFootMeasureTime deliberately retain unknown units.
BODY_FIELDS = (
    ("fatRate", "body_fat_percent", "%"), ("bodyWaterRate", "body_water_percent", "%"),
    ("boneMass", "bone_mass", "kg"), ("metabolism", "basal_metabolism", "kcal/day"),
    ("muscleRate", "muscle_rate", "unknown"), ("muscleAge", "muscle_age", "year"),
    ("proteinRatio", "protein_percent", "%"), ("standBodyWeight", "ideal_weight", "kg"),
    ("visceralFat", "visceral_fat_rating", "vendor_rating"), ("bodyScore", "body_score", "score"),
    ("bodyStyle", "body_style_code", "vendor_code"), ("bodyBalanceScore", "body_balance_score", "score"),
    ("oneFootMeasureTime", "one_foot_measure_time", "unknown"), ("age", "body_age", "year"),
)


def exertion_metrics(items: list[dict], timezone_name: str | None) -> list[DailyMetric]:
    """Keep vendor load estimates separate from Vitalis training-load metrics."""
    output = []
    for item in items:
        value = item.get("value")
        epoch = number(item.get("timestamp"), 1, 1e14)
        if epoch is None or not isinstance(value, dict):
            continue
        try:
            observed = datetime.fromtimestamp(epoch / 1000, timezone.utc)
            updated = number(value.get("lastUpdateTime"), 1, 1e14)
            revision = datetime.fromtimestamp(updated / 1000, timezone.utc) if updated else None
        except (OverflowError, OSError, ValueError):
            continue
        for key in ("atl", "ctl", "tsb", "atlTotal", "ctlTotal", "tsbTotal"):
            reading = number(value.get(key), -1e6 if key.startswith("tsb") else 0, 1e6)
            if reading is None:
                continue
            metric = "vendor_" + key.replace("Total", "_total")
            output.append(DailyMetric(date=local_day(observed, timezone_name),
                metric=metric, value=reading, unit="vendor_load", source_scope="user_summary",
                source_field=key, observed_at=observed, revision_at=revision,
                qualification="vendor_estimate_unvalidated"))
    return output
