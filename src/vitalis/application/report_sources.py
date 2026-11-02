"""Pure report-only source projection; never change retained query/chart facts.

An account scope is a vendor result, not proof of an undisclosed fusion formula.
Zepp's account workout list is a separate record family, not a physiological
sensor stream. Its arithmetic summaries keep that distinction in provenance.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime
from math import isfinite
from statistics import median

from vitalis.intelligence.activity import workout_calories_kcal
from vitalis.intelligence.contracts import (
    Coverage,
    DataQuality,
    EnergyObservation,
    Provenance,
    QualityFlag,
    QualityStatus,
)
from vitalis.intelligence.facts import facts_for_day
from vitalis.intelligence.localization import QUALITY_LABELS
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.time import local_day, local_sleep_window

ACCOUNT_SCOPES = frozenset({"user_summary", "user_fused"})
SOURCE_POLICY_VERSION = "zepp-account-report-1"
_RECORD_FIELDS = {
    "sleep": (("sleep_duration", "sleep_duration", "min"), ("deep_sleep", "deep_sleep", "min"),
        ("rem_sleep", "rem_sleep", "min"), ("light_sleep", "light_sleep", "min"),
        ("awake", "awake", "min"), ("sleep_score", "sleep_score", "score"), ("sleep_wake_count", "wake_count", "count")),
    "activity": (("steps", "steps", "steps"), ("distance_km", "distance_km", "km"),
        ("active_minutes", "active_minutes", "min"), ("calories", "calories", "kcal"), ("resting_hr", "resting_hr", "bpm")),
    "training": (("training_load", "total_load", "load"), ("training_duration", "total_duration", "min")),
}


def is_report_account_source(source: str, source_scope: str) -> bool:
    """Contributing device metadata never changes an explicit account scope."""
    return source == "zepp" and source_scope in ACCOUNT_SCOPES


def report_source_view(raw: RawDailyProfile) -> RawDailyProfile:
    """Return detached analysis containers with no device/unknown fallback.

    All ``chart_*`` fields remain untouched. Callers must use the original raw
    profile for complete chart construction (charts also read ``series`` and
    ``workouts``), and this view for features, decisions, trends and signals.
    No repository, settings, clock, network, or mutation of ``raw`` is involved.
    """
    sleep = _band_records(raw, "sleep", raw.sleep_by_day)
    activity = _band_records(raw, "activity", raw.activity_by_day)
    workouts = [dict(item) for item in raw.workouts if item.get("source") == "zepp"
        and _workout_visible(raw, item)]
    training = _training_records(raw, workouts)
    series = {
        metric: [point for point in points
            if is_report_account_source(point.source, point.source_scope)
            and _point_visible(raw, point)
            and not (point.source_field or "").startswith(("sleep.", "activity.", "training."))]
        for metric, points in raw.series.items()
    }
    for kind, records in (("sleep", sleep), ("activity", activity), ("training", training)):
        _record_series(series, records, kind, raw.timezone_name)
    series = {metric: points for metric, points in series.items() if points}
    view = replace(raw, series=series, sleep_by_day=sleep, activity_by_day=activity,
        training_by_day=training, workouts=workouts,
        heart_rate_samples=[point for point in raw.heart_rate_samples
            if is_report_account_source(point.source, point.source_scope) and _point_visible(raw, point)],
        sample_window_summaries={metric: item for metric, item in raw.sample_window_summaries.items()
            if is_report_account_source(item.provenance.source, item.provenance.source_scope)
            and _time_visible(raw, item.last_observed_at)},
        report_context={**raw.report_context, "source_policy": {
            "version": SOURCE_POLICY_VERSION, "device_fallback": False,
            "account_scopes": sorted(ACCOUNT_SCOPES),
            "workout_basis": "zepp_account_workout_records",
        }},
    )
    view.energy_observations = _energy_observations(view, raw.energy_observations)
    view.facts = facts_for_day(view)
    view.data_quality = _quality(view)
    view.report_context["latest_observations"] = {
        metric: max(points, key=lambda point: _observed_key(point.observed_at)).observed_at.isoformat()
        for metric, points in series.items()
    }
    return view


def _explicit_merged(record: dict) -> bool:
    # No string truthiness, device-name inference, or missing-ID promotion.
    return any(value is True or (type(value) is int and value == 1)
        for value in (record.get("is_merged"), record.get("isMerged")))


def _band_record(record: dict) -> dict | None:
    if record.get("source") != "zepp":
        return None
    if is_report_account_source(record["source"], record.get("source_scope", "unknown")):
        return dict(record)
    if _explicit_merged(record):
        return {**record, "source_scope": "user_fused"}
    return None


def _band_records(raw: RawDailyProfile, kind: str, legacy: dict) -> dict[date, dict]:
    candidates = defaultdict(dict)
    for day, record in legacy.items():
        if (eligible := _band_record(record)) is not None:
            candidates[day][_identity(eligible)] = eligible
    # Per-source saved records supersede the lossy day-keyed projection only
    # within the very same identity. Competing account identities abstain.
    for row in raw.chart_band_records:
        if row.get("kind") != kind:
            continue
        fetched = row.get("fetched_at")
        if isinstance(fetched, datetime) and _observed_key(fetched) > _observed_key(raw.as_of):
            continue
        day = _date(row.get("date"))
        record = row.get("data")
        if day is not None and isinstance(record, dict) and (eligible := _band_record(record)) is not None:
            candidates[day][_identity(eligible)] = {**eligible, "date": day}
    return {day: next(iter(records.values())) for day, records in candidates.items()
        if len(records) == 1 and _record_visible(raw, day, next(iter(records.values())), kind)}


def _training_records(raw: RawDailyProfile, workouts: list[dict]) -> dict[date, dict]:
    output = {}
    for day, record in raw.training_by_day.items():
        # TrainingRecord has no sensor-scope field. A Zepp record is the
        # account's workout summary; arbitrary canonical_workouts rows may
        # include other vendors and cannot be trusted without their members.
        if (record.get("source") == "zepp" and record.get("source_scope") != "device"
                and _record_visible(raw, day, record, "training")):
            output[day] = {**record, "date": day, "source_scope": record.get("source_scope") or "workout_summary"}
    grouped = defaultdict(list)
    for item in workouts:
        day = _date(item.get("local_day"))
        if day is None and isinstance(item.get("started_at"), datetime):
            day = local_day(item["started_at"], raw.timezone_name)
        if day is not None and day <= raw.day:
            grouped[day].append(item.get("data") or {})
    for day, rows in grouped.items():
        if day in output:
            continue
        output[day] = {"date": day, "source": "canonical_workouts", "source_scope": "workout_summary",
            "qualification": "derived_zepp_workout_summary", "workout_count": len(rows),
            "total_duration": _sum_observed(rows, "duration"), "total_load": _sum_observed(rows, "load")}
    return output


def _sum_observed(rows: list[dict], field: str) -> float | None:
    values = [row.get(field) for row in rows]
    return sum(values) if all(_number(value) is not None and value >= 0 for value in values) else None


def _record_series(series: dict, records: dict, kind: str, zone: str | None) -> None:
    for day, record in records.items():
        for metric, field, unit in _RECORD_FIELDS[kind]:
            value = _number(record.get(field))
            if value is None or value < 0:
                continue
            guarded_zero = kind == "activity" or (kind == "sleep" and field in {"deep_sleep", "light_sleep", "awake"})
            if guarded_zero and value == 0 and field not in (record.get("observed_fields") or []):
                continue
            if kind == "activity" and field == "resting_hr" and value == 0:
                continue
            _append(series, record, day, metric, value, unit, f"{kind}.{field}")
        if kind == "sleep":
            window = local_sleep_window(day, record.get("bedtime"), record.get("wake_time"), zone)
            if window is not None:
                bed, wake = window
                _append(series, record, day, "sleep_bedtime_offset_minutes", (bed.hour * 60 + bed.minute + 720) % 1440 - 720,
                    "min_from_midnight", "sleep.bedtime")
                _append(series, record, day, "sleep_wake_time_minutes", wake.hour * 60 + wake.minute,
                    "min_from_midnight", "sleep.wake_time")


def _append(series: dict, record: dict, day: date, metric: str, value: float, unit: str, field: str) -> None:
    series.setdefault(metric, []).append(SeriesPoint(metric, value, unit, day, day,
        record["source"], record["source_scope"], record.get("device_id"), source_field=field,
        qualification=record.get("qualification")))


def _energy_observations(raw: RawDailyProfile, observations: list) -> list[EnergyObservation]:
    output = [item for item in observations if item.role != "workout"
        and is_report_account_source(item.provenance.source, item.provenance.source_scope)
        and _time_visible(raw, item.observed_at)]
    represented = {(local_day(item.observed_at, raw.timezone_name) if isinstance(item.observed_at, datetime) else item.observed_at,
        item.provenance.source, item.provenance.source_scope, item.provenance.device_id, item.unit) for item in output}
    grouped = defaultdict(list)
    for point in raw.series.get("calories", []):
        key = (point.day, point.source, point.source_scope, point.device_id, point.unit)
        if key not in represented:
            grouped[key].append(point)
    for (_, source, scope, device, unit), points in grouped.items():
        latest = max(points, key=lambda point: _observed_key(point.observed_at))
        output.append(EnergyObservation(metric="calories", value=float(median(point.value for point in points)),
            unit=unit, observed_at=latest.observed_at, provenance=Provenance(source=source, source_scope=scope, device_id=device),
            role="unspecified", source_field=latest.source_field, limitations=["energy_role_unverified"]))
    for item in raw.workouts:
        value = workout_calories_kcal(item.get("data") or {})
        observed = item.get("started_at") or _date(item.get("local_day"))
        if value is not None and isinstance(observed, date):
            output.append(EnergyObservation(metric="calories", value=value, unit="kcal", observed_at=observed,
                provenance=Provenance(source="zepp", source_scope="workout_summary"), role="workout",
                source_field="workout.calories", limitations=["vendor_energy_estimate"]))
    return output


def _quality(raw: RawDailyProfile) -> DataQuality:
    missing = [] if any(point.day == raw.day for point in raw.series.get("sleep_duration", [])) else ["sleep_duration"]
    previous = raw.data_quality
    flags = [item for item in (previous.flags if previous else [])
        if item.code not in {"MISSING_REQUIRED_SIGNAL", "UNKNOWN_DEVICE_ATTRIBUTION"}]
    if missing:
        flags.append(QualityFlag(code="MISSING_REQUIRED_SIGNAL", severity="error", detail="缺少当天合格的 Zepp 账户睡眠结果；不使用设备记录替代。"))
    coverage = []
    for metric, points in sorted(raw.series.items()):
        observed = sorted((point.observed_at for point in points), key=_observed_key)
        coverage.append(Coverage(metric=metric, sample_count=sum(point.sample_count for point in points),
            distinct_days=len({point.day for point in points}), first_observed_at=observed[0], last_observed_at=observed[-1],
            device_ids=sorted({point.device_id for point in points if point.device_id})))
    return DataQuality(status=QualityStatus.INSUFFICIENT if missing else QualityStatus.SUFFICIENT,
        status_label=QUALITY_LABELS["INSUFFICIENT" if missing else "SUFFICIENT"], required_signals=["sleep_duration"], required_signal_labels=["睡眠时长"],
        missing_required_signals=missing, missing_required_signal_labels=["睡眠时长"] if missing else [], coverage=coverage, flags=flags)


def _time_visible(raw: RawDailyProfile, value: datetime | date | None) -> bool:
    if value is None:
        return True  # Unknown fetching time remains unknown.
    if isinstance(value, datetime):
        return _observed_key(value) <= _observed_key(raw.as_of)
    return value <= min(raw.day, local_day(raw.as_of, raw.timezone_name))


def _point_visible(raw: RawDailyProfile, point: SeriesPoint) -> bool:
    return (point.day <= raw.day and _time_visible(raw, point.day)
        and _time_visible(raw, point.observed_at) and _time_visible(raw, point.fetched_at))


def _record_visible(raw: RawDailyProfile, day: date, record: dict, kind: str) -> bool:
    if not _time_visible(raw, day) or not _time_visible(raw, record.get("fetched_at")):
        return False
    if kind == "sleep":
        window = local_sleep_window(day, record.get("bedtime"), record.get("wake_time"), raw.timezone_name)
        return window is None or _time_visible(raw, window[1])
    return True


def _workout_visible(raw: RawDailyProfile, item: dict) -> bool:
    return (_time_visible(raw, _date(item.get("local_day")))
        and _time_visible(raw, item.get("started_at")) and _time_visible(raw, item.get("fetched_at")))


def _identity(record: dict) -> tuple:
    return record.get("source"), record.get("source_scope"), record.get("device_id")


def _date(value: object) -> date | None:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _observed_key(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC))
    return datetime.combine(value, datetime.min.time(), UTC)
