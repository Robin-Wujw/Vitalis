"""Conditional running-distance equivalence over canonical saved workout facts.

``workout`` is the saved ``Workout.data`` dictionary, not a vendor record or ORM
row. Its ``distance_km`` is kilometres and ``duration`` is integer minutes.
Naive saved datetimes are interpreted as UTC, following the persistence contract;
explicit offsets are converted to UTC before subtraction. Timestamp resolution
means the precision of the supplied representation, not verified clock accuracy.

No database, clock, effort classifier, samples or personal performance model is
used. Invalid required facts raise ValueError with a stable, non-sensitive code.
"""

# ruff: noqa: TRY004 -- All invalid request facts use stable ValueError codes.
from __future__ import annotations

import re
from datetime import UTC, datetime
from math import floor, isfinite

_POINT_EXPONENT = 1.06
_SENSITIVITY_EXPONENTS = (1.04, 1.10)
_MIN_DISTANCE_KM = 3.0
_MAX_TARGET_KM = 42.195
_MAX_DISTANCE_RATIO = 4.5
# Deliberately broad input sanity, not an ability or effort threshold.
_MAX_SOURCE_SPEED_KMH = 60.0
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?"
)


def _positive_number(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(code)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(code) from None
    if not isfinite(number) or number <= 0:
        raise ValueError(code)
    return number


def _timestamp(value: object, field: str) -> tuple[datetime | None, str | None, float | None, str | None]:
    if value is None:
        return None, None, None, None
    if isinstance(value, str):
        match = _TIMESTAMP.fullmatch(value)
        if match is None:
            raise ValueError(f"invalid_{field}")
        fraction = match.group("fraction")
        resolution = 10 ** -len(fraction) if fraction else 1
        text = value
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"invalid_{field}") from None
    elif isinstance(value, datetime):
        moment = value
        text = value.isoformat()
        resolution = 0.000001 if value.microsecond else 1
    else:
        raise ValueError(f"invalid_{field}")
    try:
        if moment.utcoffset() is None:
            return moment.replace(tzinfo=UTC), text, resolution, "utc_naive_contract"
        return moment.astimezone(UTC), text, resolution, "explicit_offset"
    except (ValueError, OverflowError, TypeError):
        raise ValueError(f"invalid_{field}") from None


def _duration(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid_duration")
    return _positive_number(value * 60, "invalid_duration")


def _pause_info(detail: dict | None, *, exact_elapsed: bool) -> dict:
    raw = detail.get("pauses") if detail is not None else None
    records = None
    if raw is not None:
        if not isinstance(raw, list):
            raise ValueError("invalid_pauses")
        records = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("invalid_pauses")
            duration = item.get("duration_seconds")
            if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
                raise ValueError("invalid_pauses")
            try:
                if not isfinite(duration):
                    raise ValueError("invalid_pauses")
                _, started, _, _ = _timestamp(item.get("started_at"), "pause_timestamp")
            except (ValueError, OverflowError):
                raise ValueError("invalid_pauses") from None
            if started is None:
                raise ValueError("invalid_pauses")
            records.append({"started_at": started, "duration_seconds": duration})
    return {
        "state": "unknown" if records is None else "recorded" if records else "none_recorded",
        "count": len(records) if records is not None else None,
        "records": records,
        "subtracted": False,
        "time_handling": (
            "elapsed_includes_any_pauses_between_endpoints"
            if exact_elapsed else "duration_pause_inclusion_unknown"
        ),
    }


def _display(seconds: float, *, rounded_minutes: bool) -> str:
    if rounded_minutes:
        return f"{floor(seconds / 60 + 0.5)} min"
    hours, remainder = divmod(floor(seconds + 0.5), 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds_part:02d}"


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def race_equivalence(
    workout: dict,
    detail: dict | None,
    target_km: float = 21.0975,
    effort_basis: str = "scenario",
) -> dict:
    """Return a Riegel equivalence and exponent sensitivity, never a fitness claim.

    ``scenario`` assumes maximal source performance only for a what-if.
    ``user_reported_maximal`` is the caller's explicit transient declaration of a
    race/test/all-out effort; it is neither independently verified nor persisted.
    Neither mode requires RPE, heart rate or moving pace.

    Valid elapsed endpoints take precedence over rounded minutes. Conflicting
    minute duration is retained and flagged, never used to select moving time.
    Missing endpoints permit minute fallback; explicitly invalid facts do not.
    Pauses are reported without subtraction, deduplication or inferred overlap.
    """
    if not isinstance(workout, dict):
        raise ValueError("invalid_workout")
    if detail is not None and not isinstance(detail, dict):
        raise ValueError("invalid_detail")
    if not isinstance(effort_basis, str) or effort_basis not in ("scenario", "user_reported_maximal"):
        raise ValueError("invalid_effort_basis")
    if workout.get("type") != "running":
        raise ValueError("unsupported_workout_type")
    for key, allowed in (("distance_unit", ("km",)), ("duration_unit", ("min", "minutes"))):
        unit = workout.get(key)
        if unit is not None and (not isinstance(unit, str) or unit not in allowed):
            raise ValueError("invalid_units")

    raw_distance = workout.get("distance_km")
    if raw_distance is None:
        raise ValueError("missing_distance")
    distance = _positive_number(raw_distance, "invalid_distance")
    target = _positive_number(target_km, "invalid_target_distance")
    if distance < _MIN_DISTANCE_KM:
        raise ValueError("source_distance_out_of_range")
    if not _MIN_DISTANCE_KM <= target <= _MAX_TARGET_KM:
        raise ValueError("target_distance_out_of_range")
    ratio = target / distance
    if ratio > _MAX_DISTANCE_RATIO:
        raise ValueError("distance_ratio_out_of_range")

    minute_seconds = _duration(workout.get("duration"))
    start, start_text, start_resolution, start_zone = _timestamp(workout.get("started_at"), "started_at")
    end, end_text, end_resolution, end_zone = _timestamp(workout.get("ended_at"), "ended_at")
    exact_elapsed = start is not None and end is not None
    limitations = ["not_validated_personal_forecast", "exponent_sensitivity_not_prediction_interval"]
    comparison = {"state": "unavailable", "elapsed_minus_duration_seconds": None}
    if exact_elapsed:
        seconds = (end - start).total_seconds()
        if not isfinite(seconds) or seconds <= 0:
            raise ValueError("invalid_elapsed_time")
        time_basis = "elapsed_timestamps"
        resolution = max(start_resolution, end_resolution)
        fields_used = ["type", "distance_km", "started_at", "ended_at"]
        if minute_seconds is not None:
            difference = seconds - minute_seconds
            comparison = {
                "state": "within_minute_resolution" if abs(difference) < 60 else "differs_from_elapsed",
                "elapsed_minus_duration_seconds": difference,
            }
            if comparison["state"] == "differs_from_elapsed":
                limitations.append("reported_duration_differs_from_elapsed")
    else:
        if minute_seconds is None:
            raise ValueError("missing_time")
        seconds = minute_seconds
        time_basis = "rounded_minutes"
        resolution = 60
        fields_used = ["type", "distance_km", "duration"]
        comparison["state"] = "elapsed_unavailable"
        limitations.append("rounded_minute_source_time")

    if distance / seconds * 3600 > _MAX_SOURCE_SPEED_KMH:
        raise ValueError("implausible_distance_time")
    pauses = _pause_info(detail, exact_elapsed=exact_elapsed)
    if pauses["state"] == "recorded":
        limitations.append("recorded_pauses_not_subtracted")
    if not exact_elapsed:
        limitations.append("duration_pause_inclusion_unknown")

    point = seconds * ratio ** _POINT_EXPONENT
    sensitivity = sorted((seconds * ratio ** exponent, exponent) for exponent in _SENSITIVITY_EXPONENTS)
    if any(not isfinite(value) or value <= 0 for value in (point, *(item[0] for item in sensitivity))):
        raise ValueError("invalid_equivalence")
    scenario = effort_basis == "scenario"
    fetched = workout.get("fetched_at")
    return {
        "kind": "race_equivalence",
        "target_km": target,
        "effort_basis": effort_basis,
        "equivalence": {
            "seconds": point,
            "display": _display(point, rounded_minutes=not exact_elapsed),
            "display_resolution_seconds": 1 if exact_elapsed else 60,
        },
        "sensitivity_range": {
            "seconds": [item[0] for item in sensitivity],
            "display": [_display(item[0], rounded_minutes=not exact_elapsed) for item in sensitivity],
            "exponents_at_bounds": [item[1] for item in sensitivity],
            "interpretation": "exponent_sensitivity_not_prediction_interval",
        },
        "method": {
            "name": "Riegel",
            "version": "1.0",
            "formula": "T2 = T1 * (D2 / D1) ** exponent",
            "point_exponent": _POINT_EXPONENT,
            "sensitivity_exponents": list(_SENSITIVITY_EXPONENTS),
            "domain": {
                "min_source_km": _MIN_DISTANCE_KM,
                "target_km_range": [_MIN_DISTANCE_KM, _MAX_TARGET_KM],
                "max_target_source_ratio": _MAX_DISTANCE_RATIO,
                "max_source_speed_kmh": _MAX_SOURCE_SPEED_KMH,
                "speed_limit_basis": "input_sanity_not_effort_or_fitness_classification",
            },
        },
        "qualification": {
            "status": "conditional_equivalence",
            "validation_state": "not_validated_personal_forecast",
            "actual_race_fitness_prediction": False,
            "maximal_effort_verified": False,
        },
        "basis": {
            "kind": "scenario_assumption" if scenario else "transient_user_report",
            "maximal_effort": "assumed" if scenario else "user_reported",
            "persisted": False,
            "independently_verified": False,
            "description": (
                "What-if: assume this run represents maximal performance at its recorded distance; "
                "ordinary training does not establish actual maximal performance."
                if scenario else
                "The user explicitly reports this run was a maximal race, test or all-out effort; "
                "the declaration applies only to this request and is not independently verified."
            ),
        },
        "assumptions": [
            "Source time represents maximal performance at the source distance, by assumption or user declaration.",
            "Sufficient endurance supports the target distance; comparable terrain, weather and conditions are assumed.",
            "The fixed exponents are not individually calibrated; their range measures parameter sensitivity only.",
            "Saved distance_km is kilometres and duration is integer minutes; raw vendor values are not interpreted.",
            "Saved naive timestamps are UTC; explicit offsets are normalized to UTC for elapsed subtraction.",
            "Time resolution describes input representation, not verified measurement accuracy or forecast precision.",
            "Pauses are never subtracted; minute fallback has unknown pause inclusion.",
        ],
        "source_facts": {
            "workout_id": _optional_text(workout.get("workout_id")),
            "source": _optional_text(workout.get("source")),
            "name": _optional_text(workout.get("name")),
            "type": "running",
            "sport_mode_label": _optional_text(workout.get("sport_mode_label")),
            "distance_km": distance,
            "distance_unit": "km",
            "duration_minutes": workout.get("duration"),
            "started_at": start_text,
            "ended_at": end_text,
            "fetched_at": fetched.isoformat() if isinstance(fetched, datetime) else _optional_text(fetched),
            "time_seconds": seconds,
            "time_basis": time_basis,
            "time_resolution_seconds": resolution,
            "timestamp_timezone_basis": {"started_at": start_zone, "ended_at": end_zone},
            "duration_comparison": comparison,
            "pauses": pauses,
            "fields_used": fields_used,
        },
        "limitations": limitations,
    }
