"""Source-isolated context from saved observations; no decision thresholds."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import mean

from .contracts import Provenance, RecordedContext, RecordedGoal, RecordedMetric
from .profile import RawDailyProfile, SeriesPoint
from vitalis.time import local_day


SLEEP_METRICS = {
    "sleep_latency_minutes": "min", "out_of_bed_delay_minutes": "min",
    "time_in_bed_minutes": "min", "sleep_efficiency_percent": "%",
}
BODY_METRICS = {"weight": "kg", "height": "cm", "bmi": "kg/m2"}
VENDOR_METRICS = {f"vendor_{key}": "vendor_load"
                  for key in ("atl", "ctl", "tsb", "atl_total", "ctl_total", "tsb_total")}
GOALS = {"step_goal": ("steps", "steps"), "calorie_goal": (None, "kcal"),
         "active_minutes_goal": ("active_minutes", "min")}


def _instant(value: datetime | date) -> datetime:
    if not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time(), timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _streams(raw: RawDailyProfile, metric: str, unit: str, start: date, end: date):
    streams = defaultdict(list)
    for point in raw.series.get(metric, []):
        if (not start <= point.day <= end or point.unit != unit
                or not _visible(raw, point.day)
                or point.source != "zepp" or point.source_scope not in {"user_fused", "user_summary"}
                or not isinstance(point.value, (int, float)) or isinstance(point.value, bool) or not isfinite(point.value)
                or point.qualification in {"retained_raw", "raw_not_sleep_stage"}
                or not _visible(raw, point.observed_at)
                or (point.fetched_at is not None and (not isinstance(point.fetched_at, datetime)
                    or _instant(point.fetched_at) > _instant(raw.as_of)))):
            continue
        if point.value < 0 and metric not in {"vendor_tsb", "vendor_tsb_total"}:
            continue
        if metric in {*BODY_METRICS, *GOALS, "time_in_bed_minutes"} and point.value <= 0:
            continue
        if metric == "sleep_efficiency_percent" and point.value > 100:
            continue
        streams[(point.source, point.source_scope, point.device_id)].append(point)
    return streams


def _visible(raw: RawDailyProfile, value: date | datetime) -> bool:
    if isinstance(value, datetime):
        return _instant(value) <= _instant(raw.as_of)
    return isinstance(value, date) and value <= min(raw.day, local_day(raw.as_of, raw.timezone_name))


def _daily(points: list[SeriesPoint]) -> dict[date, SeriesPoint]:
    """Use the latest saved observation each day, not a sum of revisions."""
    grouped = defaultdict(list)
    for point in points:
        grouped[point.day].append(point)
    output = {}
    for day, rows in grouped.items():
        key = lambda item: (_instant(item.observed_at), _instant(item.fetched_at or item.observed_at))
        latest = max(key(point) for point in rows)
        candidates = [point for point in rows if key(point) == latest]
        if len({point.value for point in candidates}) == 1:
            # Equal values are duplicates; preserve a stable qualifier rather
            # than changing the chosen provenance when input order changes.
            output[day] = min(candidates, key=lambda item: (item.source_field or "", item.qualification or ""))
    return output


def _actual_by_day(raw: RawDailyProfile, metric: str | None, unit: str, start: date, end: date) -> dict[date, SeriesPoint]:
    candidates = defaultdict(list)
    for points in _streams(raw, metric, unit, start, end).values() if metric else []:
        daily = _daily(points)
        for day in {point.day for point in points}:
            candidates[day].append(daily.get(day))
    # Goal settings and actual account observations are separate record families.
    # A single qualified account stream may be compared; competing streams abstain.
    return {day: points[0] for day, points in candidates.items() if len(points) == 1 and points[0] is not None}


def _provenance(point: SeriesPoint) -> Provenance:
    return Provenance(source=point.source, source_scope=point.source_scope, device_id=point.device_id)


def build_recorded_context(raw: RawDailyProfile, start: date) -> RecordedContext:
    end = raw.day
    days = (end-start).days+1
    if days < 1:
        raise ValueError("recorded context requires an ordered period")
    output = RecordedContext()
    for metric, unit in (SLEEP_METRICS | BODY_METRICS | VENDOR_METRICS).items():
        # Sparse body records retain their own dates, even outside a report day.
        first = end-timedelta(days=89) if metric in BODY_METRICS else start
        for identity, points in sorted(_streams(raw, metric, unit, first, end).items(), key=lambda item: str(item[0])):
            daily = _daily(points)
            if not daily:
                continue
            latest = daily[max(daily)]
            item = RecordedMetric(metric=metric, unit=unit,
                provenance=Provenance(source=identity[0], source_scope=identity[1], device_id=identity[2]),
                qualification=latest.qualification or "unspecified",
                date_basis="sleep_day" if metric in SLEEP_METRICS else "calendar_day" if metric in BODY_METRICS else "activity_day",
                period_start=first, period_end=end, available_days=len(daily), sample_count=len(points),
                coverage_ratio=len(daily)/((end-first).days+1), latest_value=latest.value, latest_day=latest.day,
                latest_observed_at=latest.observed_at, latest_fetched_at=latest.fetched_at, source_field=latest.source_field)
            if metric in SLEEP_METRICS:
                item.average = round(mean(point.value for point in daily.values()), 2)
                if days > 1:
                    reference_start, reference_end = start-timedelta(days=days), start-timedelta(days=1)
                    previous = _streams(raw, metric, unit, reference_start, reference_end).get(identity, [])
                    previous_daily = _daily(previous)
                    item.reference_start, item.reference_end = reference_start, reference_end
                    item.previous_available_days = len(previous_daily)
                    item.previous_sample_count = len(previous)
                    item.previous_coverage_ratio = len(previous_daily)/days
                    if len(daily) >= 5 and len(previous_daily) >= 5:
                        item.previous_average = round(mean(point.value for point in previous_daily.values()), 2)
                        item.change = round(item.average-item.previous_average, 2)
            output.metrics.append(item)
    for metric, (actual_metric, unit) in GOALS.items():
        for identity, points in sorted(_streams(raw, metric, unit, start, end).items(), key=lambda item: str(item[0])):
            daily = _daily(points)
            if not daily:
                continue
            latest_day = max(daily)
            latest = daily[latest_day]
            actual = _actual_by_day(raw, actual_metric, unit, start, end)
            measured = actual.get(latest_day)
            matched = [day for day in daily if day in actual and
                (day < end or raw.report_context.get("target_day_complete") is True)]
            # Zepp calorie-goal energy role has not been verified. Never compare
            # it with daily-total, active, or workout calories by unit alone.
            output.goals.append(RecordedGoal(metric=metric, unit=unit,
                provenance=Provenance(source=identity[0], source_scope=identity[1], device_id=identity[2]),
                qualification=latest.qualification or "unspecified", period_start=start, period_end=end,
                available_days=len(daily), sample_count=len(points), coverage_ratio=len(daily)/days,
                latest_value=latest.value, latest_day=latest.day, latest_observed_at=latest.observed_at,
                latest_fetched_at=latest.fetched_at, source_field=latest.source_field,
                actual_value=measured.value if measured else None,
                actual_observed_at=measured.observed_at if measured else None,
                actual_fetched_at=measured.fetched_at if measured else None,
                actual_provenance=_provenance(measured) if measured else None,
                actual_qualification=(measured.qualification or "unspecified") if measured else None,
                actual_source_field=measured.source_field if measured else None,
                evaluated_days=len(matched),
                evaluation_coverage_ratio=len(matched)/days,
                met_days=sum(actual[day].value >= daily[day].value for day in matched) if matched else None,
                daily_progress=[{
                    "day": day, "target_value": daily[day].value, "actual_value": actual[day].value,
                    "target_qualification": daily[day].qualification or "unspecified",
                    "target_observed_at": daily[day].observed_at, "target_fetched_at": daily[day].fetched_at,
                    "target_source_field": daily[day].source_field,
                    "actual_provenance": _provenance(actual[day]).model_dump(mode="json"),
                    "actual_qualification": actual[day].qualification or "unspecified",
                    "actual_source_field": actual[day].source_field,
                    "actual_observed_at": actual[day].observed_at, "actual_fetched_at": actual[day].fetched_at,
                    "met": actual[day].value >= daily[day].value,
                } for day in sorted(matched)]))
    return output
