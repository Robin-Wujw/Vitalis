"""Deterministic review of saved work, separate from qualified capacity.

Nominal recorded-load is the sum of each known recorded weight times its
recorded repetitions. It is not muscle load, effective body mass, a capacity
limit, or proof of strength gain. No source data or analysis snapshot is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import fsum

from vitalis.time import local_day

from .running import _parse_datetime
from .strength import StrengthAnalyzer, _capacity_number, reported_set_count


@dataclass
class _Session:
    started_at: datetime | None
    payload: dict
    groups: dict[tuple, dict]


def review_strength_workouts(
    workouts: list[dict], *, start: date, end: date, timezone_name: str,
    truncated: bool = False,
) -> dict:
    """Review only supplied saved rows; never infer complete upstream coverage."""
    if start > end:
        raise ValueError("start must not be after end")
    window = {"start": start.isoformat(), "end": end.isoformat(),
              "days": (end - start).days + 1, "timezone": timezone_name}
    sessions = []
    expanded_sets = 0
    for workout in workouts:
        confirmed = workout.get("confirmed_exercises") or []
        records = confirmed or (workout.get("detail") or {}).get("strength_sets") or []
        for row in records:
            expanded_sets += (_positive_int(row.get("sets")) or 1) if confirmed and isinstance(row, dict) else 1
            if expanded_sets > 2000:
                raise ValueError("strength_review_set_budget_exceeded")
    for workout in workouts:
        data, detail = workout.get("data") or {}, workout.get("detail") or {}
        confirmed = workout.get("confirmed_exercises") or []
        if not (data.get("training_family") == "strength" or data.get("type") == "strength"
                or detail.get("strength_sets") or confirmed):
            continue
        session = _session(workout, timezone_name)
        day = local_day(session.started_at, timezone_name) if session.started_at else None
        if day is None or start <= day <= end:
            sessions.append(session)
    sessions.sort(key=_session_sort_key)
    for session in sessions:
        for key, exercise in session.groups.items():
            references = [
                (other, other.groups[key]) for other in sessions
                if session.started_at is not None and other.started_at is not None
                and other.started_at < session.started_at and key in other.groups
                and exercise["identity"] is not None
            ]
            exercise["previous"] = (
                _comparison(session, exercise, references, window, truncated)
                if references else None
            )
    totals: dict[tuple, dict] = {}
    for session in sessions:
        for key, exercise in session.groups.items():
            total = totals.setdefault(key, {
                **_group_metadata(exercise), "sets": [], "session_count": 0, "dates": [],
            })
            total["sets"].extend(exercise["sets"])
            total["session_count"] += 1
            total["dates"].append(session.payload["date"])
    exercise_totals = []
    for total in totals.values():
        _summarize_group(total)
        total.pop("sets")
        total["scope"] = "returned_records_only"
        exercise_totals.append(total)
    payloads = [session.payload for session in sessions]
    return {
        "schema_version": "1.0", "time_basis": "activity_day", "window": window,
        "truncated": truncated,
        "coverage": {
            "workouts_read": len(workouts), "strength_sessions": len(sessions),
            "sessions_with_sets": sum(bool(s.groups) for s in sessions),
            "sessions_missing_sets": sum(not s.groups for s in sessions),
            "recorded_sets": sum(s["coverage"]["recorded_sets"] for s in payloads),
            "unknown_date_sessions": sum(s.started_at is None for s in sessions),
            "upstream_coverage_verified": False,
        },
        "sessions": payloads, "exercise_totals": exercise_totals,
        "limitations": [
            "recorded_work_not_muscle_load", "upstream_coverage_unknown",
            "totals_cover_returned_compatible_groups_only",
            *(["workout_limit_truncated"] if truncated else []),
        ],
    }


def _session(workout: dict, timezone_name: str) -> _Session:
    data, detail = workout.get("data") or {}, workout.get("detail") or {}
    confirmed = workout.get("confirmed_exercises") or []
    records = confirmed or detail.get("strength_sets") or []
    started = _utc(workout.get("started_at") or data.get("started_at"))
    detail_fetched = _iso(detail.get("fetched_at") or data.get("fetched_at"))
    confirmed_times = [_utc(row.get("created_at")) for row in confirmed if isinstance(row, dict)]
    fetched = (
        _iso(max((value for value in confirmed_times if value is not None), default=None))
        if confirmed else detail_fetched
    )
    source = _text(workout.get("source"))
    workout_id = _text(workout.get("workout_id"))
    device = _text(workout.get("device_id") or detail.get("device_id") or data.get("device_id"))
    machine = _text(workout.get("machine_id") or detail.get("machine_id") or data.get("machine_id"))
    groups: dict[tuple, dict] = {}
    invalid_records = 0
    ordered = [(index, row) for index, row in enumerate(records) if isinstance(row, dict)]
    invalid_records += len(records) - len(ordered)
    ordered.sort(key=lambda pair: (_positive_int(pair[1].get("order")) or pair[0] + 1, pair[0]))
    for index, row in ordered:
        identity = StrengthAnalyzer._exercise_identity(row.get("exercise_id"), row.get("exercise_name"))
        if identity is None and type(row.get("vendor_exercise_code")) is int:
            identity = ("vendor_code", str(row["vendor_exercise_code"]))
        weight, unit = _weight(row)
        basis = _text(row.get("weight_basis"))
        scope = "user_confirmed" if confirmed else _text(row.get("source"))
        own_device = _text(row.get("device_id")) or device
        own_machine = _text(row.get("machine_id")) or machine
        side = _text(row.get("side")) or "unknown"
        key = (identity or ("unidentified", source, workout_id, index),
               source, scope, unit, basis, own_device, own_machine, side)
        group = groups.setdefault(key, {
            "identity": {"kind": identity[0], "value": identity[1]} if identity else None,
            "exercise_id": _text(row.get("exercise_id")),
            "exercise_name": _text(row.get("exercise_name")),
            "source": source, "source_scope": scope, "weight_unit": unit,
            "weight_basis": basis, "device_id": own_device, "machine_id": own_machine,
            "side": side, "qualification": "recorded_work_not_muscle_load", "sets": [],
        })
        count = _positive_int(row.get("sets")) if confirmed else 1
        repetitions = _positive_int(row.get("repetitions"))
        volume = None
        limitations = [item for item in row.get("limitations") or [] if isinstance(item, str)]
        if weight is not None and repetitions is not None and unit in {"kg", "lb"} and count is not None:
            try:
                volume = _capacity_number(weight * repetitions)
            except OverflowError:
                volume = None
            if volume is None:
                limitations.append("arithmetic_out_of_range")
        if row.get("weight_value") is not None and _nonnegative(row.get("weight_value")) is None:
            limitations.append("invalid_recorded_weight")
        for within in range(count if count is not None else 1):
            group["sets"].append({
                "order": _positive_int(row.get("order")),
                "within_record_order": within + 1 if count is not None else None,
                "set_count_known": count is not None,
                "repetitions": repetitions, "weight_value": weight, "weight_unit": unit,
                "weight_basis": basis, "source_scope": scope,
                "set_kind": _text(row.get("set_kind")) or "unknown", "side": side,
                "started_at": _iso(row.get("started_at")),
                "recorded_at": _iso(row.get("created_at")) if confirmed else None,
                "fetched_at": _iso(row.get("created_at")) if confirmed else _iso(row.get("fetched_at")) or fetched,
                "nominal_volume": volume if basis not in {"bodyweight", "assistance"} else None,
                "assistance_volume": volume if basis == "assistance" else None,
                "limitations": limitations.copy(),
            })
    for group in groups.values():
        _summarize_group(group)
    known_sets = sum(g["coverage"]["recorded_sets"] for g in groups.values())
    unknown_counts = sum(g["coverage"]["records_with_unknown_set_count"] for g in groups.values())
    summary_sets = reported_set_count(data)
    payload = {
        "source": source, "workout_id": workout_id,
        "date": local_day(started, timezone_name).isoformat() if started else None,
        "started_at": _iso(started), "fetched_at": fetched, "device_id": device,
        "detail_fetched_at": detail_fetched,
        "detail_available": bool(workout.get("detail_available")),
        "record_source": "user_confirmed" if confirmed else "saved_workout_detail",
        "coverage": {
            "recorded_sets": known_sets, "records_with_unknown_set_count": unknown_counts,
            "summary_sets": summary_sets,
            "detail_complete": known_sets == summary_sets if summary_sets is not None and not unknown_counts else None,
            "missing_summary_sets": max(summary_sets - known_sets, 0) if summary_sets is not None and not unknown_counts else None,
            "invalid_records": invalid_records,
        },
        "exercises": list(groups.values()),
    }
    return _Session(started, payload, groups)


def _group_metadata(group: dict) -> dict:
    return {key: group[key] for key in (
        "identity", "exercise_id", "exercise_name", "source", "source_scope", "weight_unit",
        "weight_basis", "device_id", "machine_id", "side", "qualification",
    )}


def _summarize_group(group: dict) -> None:
    rows = group["sets"]
    known = [row for row in rows if row["set_count_known"]]
    unknown_counts = len(rows) - len(known)
    reps = [row["repetitions"] for row in known if row["repetitions"] is not None]
    group.update({
        "set_count": len(known) if not unknown_counts else None,
        "known_repetitions": sum(reps) if reps else None,
        "coverage": {
            "recorded_sets": len(known), "records_with_unknown_set_count": unknown_counts,
            "repetitions_known_sets": len(reps), "repetitions_missing_sets": len(known) - len(reps),
            "weight_missing_sets": sum(row["weight_value"] is None for row in known),
            "unit_unknown_sets": sum(row["weight_unit"] not in {"kg", "lb"} for row in known),
            "order_unknown_sets": sum(row["order"] is None for row in known),
        },
        "nominal_volume": _volume(rows, group["weight_unit"], "nominal_volume",
                                  group["weight_basis"] not in {"bodyweight", "assistance"}),
        "assistance_volume": _volume(rows, group["weight_unit"], "assistance_volume",
                                     group["weight_basis"] == "assistance"),
    })
    limitations = [item for row in rows for item in row["limitations"]]
    if group["weight_basis"] is None:
        limitations.append("weight_basis_unknown")
    if group["device_id"] is None:
        limitations.append("device_unknown")
    if group["weight_basis"] == "machine" and group["machine_id"] is None:
        limitations.append("machine_identity_unknown")
    if group["weight_basis"] == "bodyweight":
        limitations.append("effective_body_mass_unknown")
    if unknown_counts:
        limitations.append("set_count_unknown")
    if any(group[field]["known_sets"] and group[field]["value"] is None for field in ("nominal_volume", "assistance_volume")):
        limitations.append("arithmetic_out_of_range")
    group["limitations"] = sorted(set(limitations))


def _volume(rows: list[dict], unit: str | None, field: str, applicable: bool) -> dict:
    known = [row[field] for row in rows if row[field] is not None]
    missing = sum(row["set_count_known"] and row[field] is None for row in rows) if applicable else 0
    unknown = sum(not row["set_count_known"] for row in rows)
    try:
        total = _capacity_number(fsum(known)) if known else None
    except OverflowError:
        total = None
    return {
        "metric": "nominal_recorded_load" if field == "nominal_volume" else "nominal_recorded_assistance",
        "value": round(total, 6) if total is not None else None,
        "unit": f"{unit}*reps" if unit in {"kg", "lb"} else None,
        "known_sets": len(known), "missing_sets": missing,
        "unknown_set_count_records": unknown,
        "state": "not_applicable" if not applicable else "unavailable" if total is None else "partial" if missing or unknown else "complete",
    }


def _comparison(current: _Session, exercise: dict, references: list, window: dict, truncated: bool) -> dict:
    previous, reference = references[0]  # Sessions already have a stable newest-first order.
    limitations = list(set(exercise["limitations"] + reference["limitations"]))
    if truncated:
        limitations.append("history_truncated")
    if exercise["coverage"]["order_unknown_sets"] or reference["coverage"]["order_unknown_sets"]:
        limitations.append("set_order_unknown")
    complete_reps = all(g["set_count"] is not None and g["coverage"]["repetitions_missing_sets"] == 0 for g in (exercise, reference))
    complete_volume = all(g["nominal_volume"]["state"] == "complete" for g in (exercise, reference))
    nominal_delta = _delta(exercise["nominal_volume"]["value"], reference["nominal_volume"]["value"])
    if not complete_reps or any(g["nominal_volume"]["state"] in {"partial", "unavailable"} for g in (exercise, reference)):
        limitations.append("partial_recorded_dose")
    if exercise["set_count"] != reference["set_count"]:
        limitations.append("set_count_differs")
    conditional = any(reason in limitations for reason in (
        "weight_basis_unknown", "device_unknown", "machine_identity_unknown", "set_count_unknown", "set_order_unknown",
        "partial_recorded_dose", "set_count_differs",
    )) or exercise["weight_unit"] not in {"kg", "lb"}
    return {
        "source": previous.payload["source"], "workout_id": previous.payload["workout_id"],
        "date": previous.payload["date"], "started_at": previous.payload["started_at"],
        "fetched_at": previous.payload["fetched_at"],
        "comparison_basis": "same_recorded_group_not_proven_same_equipment_or_effort",
        "comparability": "conditional" if conditional else "recorded_only",
        "proves_strength_gain": False,
        "current": _raw_dose(exercise), "reference": _raw_dose(reference),
        "delta_total_repetitions": _delta(exercise["known_repetitions"], reference["known_repetitions"]) if complete_reps else None,
        "delta_nominal_volume": nominal_delta if complete_volume else None,
        "delta_known_nominal_volume": nominal_delta,
        "matched_sets": [
            {"position": index, "current_order": own["order"], "reference_order": old["order"],
             "delta_weight": _delta(own["weight_value"], old["weight_value"]) if exercise["weight_unit"] in {"kg", "lb"} else None,
             "delta_repetitions": _delta(own["repetitions"], old["repetitions"])}
            for index, (own, old) in enumerate(zip(exercise["sets"], reference["sets"]), 1)
            if own["set_count_known"] and old["set_count_known"]
        ],
        "set_matching_basis": "recorded_position_not_equivalent_effort",
        "coverage": {
            "window": window.copy(), "history_truncated": truncated,
            "compatible_previous_sessions": len(references), "sample_count": 2,
            "current": exercise["coverage"].copy(), "reference": reference["coverage"].copy(),
            "current_session": current.payload["coverage"].copy(),
            "reference_session": previous.payload["coverage"].copy(),
            "upstream_coverage_verified": False,
        },
        "limitations": sorted(set(limitations)),
    }


def _raw_dose(group: dict) -> dict:
    return {
        "set_count": group["set_count"],
        "repetitions": [row["repetitions"] for row in group["sets"]],
        "weights": [row["weight_value"] for row in group["sets"]],
        "weight_unit": group["weight_unit"], "weight_basis": group["weight_basis"],
        "known_repetitions": group["known_repetitions"],
        "nominal_volume": group["nominal_volume"].copy(),
        "assistance_volume": group["assistance_volume"].copy(),
    }


def _weight(row: dict) -> tuple[float | None, str | None]:
    unit = StrengthAnalyzer._known_weight_unit(row.get("weight_unit")) or _text(row.get("weight_unit"))
    # Prefer the recorded native unit; a saved kg conversion must not merge lb groups.
    if row.get("weight_value") is not None:
        return _nonnegative(row["weight_value"]), unit
    kg = _nonnegative(row.get("weight_kg"))
    if kg is not None:
        return kg, "kg"
    return None, unit


def _positive_int(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def _nonnegative(value) -> float | None:
    try:
        number = _capacity_number(value)
    except OverflowError:
        return None
    return number if number is not None and number >= 0 else None


def _text(value) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _utc(value) -> datetime | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _delta(current, previous):
    return round(current - previous, 6) if current is not None and previous is not None else None


def _session_sort_key(session: _Session) -> tuple:
    seconds = (session.started_at - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() if session.started_at else 0
    return (session.started_at is None, -seconds, session.payload["source"] or "", session.payload["workout_id"] or "")
