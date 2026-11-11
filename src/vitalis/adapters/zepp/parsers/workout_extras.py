"""Bounded workout extras shared by dictionary and streaming parsers.

Protocol reference: zepp-mcp 904cc301d63a64da6f633dd8a75b2a612e67240a.
Reference units are labelled; opaque streams never become normalized facts.
"""
from collections import Counter
from datetime import UTC, datetime
import json
from math import isfinite

from vitalis.domain import WorkoutLap

ABSOLUTE_STREAM_SPECS = (
    ("speed", "speed", "m/s", 1.0, 0.0, 20.0),
    ("equivPace", "equivalent_pace", "s/km", 1.0, 60.0, 3600.0),
    ("currentDistance", "distance", "m", 0.01, 0.0, 1_000_000.0),
    ("time_delta_altitude", "altitude", "m", 0.1, -1000.0, 10_000.0),
    ("power_meter", "running_power", "W", 1.0, 0.0, 5000.0),
)
LAP_COLUMNS = {0: ("index", "index"), 1: ("duration_seconds", "s"), 2: ("distance_meters", "m"),
    4: ("avg_heart_rate_bpm", "bpm"), 5: ("elapsed_seconds_cumulative", "s"),
    9: ("pace_unverified", "vendor_raw"), 12: ("stroke_speed_unverified", "vendor_raw"),
    13: ("strokes", "strokes"), 14: ("swolf", "swolf")}


def _number(value, minimum=0, maximum=1e12):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if isfinite(value) and minimum <= value <= maximum else None


# metric -> vendor field, unit, minimum, maximum, scale. Raw value travels too.
SUMMARY_METRICS = {
    "swolf": ("swolf", "swolf", 1, 10000, 1),
    "strokes": ("total_strokes", "strokes", 1, 1e7, 1),
    "lengths": ("total_trips", "lengths", 1, 1e6, 1),
    "pool_length": ("swim_pool_length", "m", 1, 1000, 1),
    "distance_per_stroke": ("avg_distance_per_stroke", "m", .001, 100, 1),
    "avg_stroke_speed": ("avg_stroke_speed", "vendor_raw", 0, 1e6, 1),
    "max_stroke_speed": ("max_stroke_speed", "vendor_raw", 0, 1e6, 1),
    "swim_style": ("swim_style", "vendor_code", 0, 100, 1),
    "total_jumps": ("rope_skipping_count", "jumps", 1, 1e7, 1),
    "max_consecutive_jumps": ("numberOfConsecutive", "jumps", 1, 1e7, 1),
    "cadence": ("avg_frequency", "movements/min", 1, 1000, 1),
    "max_cadence": ("max_frequency", "movements/min", 1, 1000, 1),
    "stride_length": ("avg_stride_length", "cm", 1, 1000, 1),
    "ground_contact_time": ("averageGct", "ms", 1, 2000, 1),
    "min_ground_contact_time": ("minGct", "ms", 1, 2000, 1),
    "vertical_oscillation": ("averageVo", "mm", 1, 1000, 1),
    "max_vertical_oscillation": ("maxVo", "mm", 1, 1000, 1),
    "vertical_stride_ratio": ("avgVertStrideRatio", "%", 1, 1000, .1),
    "power": ("average_power", "W", 0, 10000, 1),
    "max_power": ("max_power", "W", 0, 10000, 1),
    "vo2_max": ("VO2_max", "ml/kg/min", 1, 150, 1),
    "lactate_threshold_hr": ("lactateThresholdHr", "bpm", 30, 250, 1),
    "lactate_threshold_pace": ("lactateThresholdPace", "s/km", 60, 3600, 1),
    "lactate_threshold_update": ("lactateThresholdUpdateFlag", "vendor_code", 0, 100, 1),
    "elevation_gain": ("elevationGain", "m", 0, 1e8, .01),
    "elevation_loss": ("elevationLoss", "m", 0, 1e8, .01),
    "equivalent_distance": ("equivDistance", "m", 0, 1e9, .01),
    "minimum_heart_rate": ("min_heart_rate", "bpm", 1, 300, 1),
    "spo2_min": ("spo2_min", "%", 1, 100, 1),
    "spo2_max": ("spo2_max", "%", 1, 100, 1),
    "total_steps": ("total_step", "steps", 1, 1e7, 1),
    "reported_groups": ("total_group", "groups", 1, 10000, 1),
    "rest_between_sets_seconds": ("restTimeBetweenGroupsWithMillis", "s", 0, 1e9, .001),
}
for _field in ("avg_temperature", "min_temperature", "max_temperature"):
    SUMMARY_METRICS[_field] = (_field, "C", -100, 100, 1)
for _field in ("avg_altitude", "min_altitude", "max_altitude", "altitude_ascend",
               "altitude_descend", "distance_ascend", "climb_dis_descend"):
    SUMMARY_METRICS[_field] = (_field, "m", -1000 if "_altitude" in _field else 0, 1e6, 1)
for _field in ("freestyle_length", "breast_stroke_length", "butterfly_length",
               "medley_length", "back_stroke_length", "other_stroke_length",
               "avg_pace", "max_pace", "min_pace", "run_time", "landing_time",
               "flight_ratio", "forefoot_ratio", "marathon", "avg_slope", "max_slope",
               "climb_dis_ascend_time", "climb_dis_descend_time", "work_value",
               "totalMuscularExertion", "totalCardiacExertion", "totalScore",
               "averageRTPC", "bestRTPC", "worstRTPC"):
    SUMMARY_METRICS[_field] = (_field, "vendor_raw", 0, 1e12, 1)


def _object(value):
    if isinstance(value, str) and len(value) <= 128000:
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def summary_extras(row, *, training_family=None):
    observed = None
    epoch = _number(row.get("trackid"), 1, 1e11)
    if epoch is not None:
        try:
            observed = datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            pass
    metrics = {}
    for name, (field, unit, minimum, maximum, scale) in SUMMARY_METRICS.items():
        if name == "power" and _number(row.get(field), minimum, maximum) is None:
            field = "avg_power"
        raw = _number(row.get(field), minimum, maximum)
        if raw is None or (field.endswith("RTPC") and raw == 21):
            continue
        metrics[name] = {
            "value": round(raw * scale, 6), "raw_value": raw, "unit": unit,
            "source": "zepp", "source_scope": "workout_summary", "source_field": field,
            "observed_at": observed, "time_basis": "activity_day",
            "qualification": "vendor_unit_unverified" if unit == "vendor_raw" else "vendor_reported",
            "unit_evidence": "reference_protocol", "fetched_at": None,
        }
    result = {"vendor_summary_metrics": metrics}
    strengths = _object(row.get("strengthScores"))
    if isinstance(strengths, (dict, list)):
        metrics["strength_scores"] = {"value": strengths, "unit": "vendor_raw",
            "source": "zepp", "source_scope": "workout_summary", "source_field": "strengthScores",
            "qualification": "vendor_structure_unverified", "observed_at": observed,
            "time_basis": "activity_day", "fetched_at": None}
    added = _object(row.get("add_info"))
    dem = added.get("adjust_dem_altitude") if isinstance(added, dict) else None
    if isinstance(dem, dict):
        for field in ("elevationGain", "elevationLoss", "avgAltitude", "maxAltitude", "minAltitude"):
            value = _number(dem.get(field), 0 if field.startswith("elevation") else -999, 1e6)
            if value is not None:
                metrics["dem_" + field] = {"value": value, "unit": "m", "source": "zepp",
                    "source_scope": "workout_summary_dem", "source_field": "add_info.adjust_dem_altitude." + field,
                    "qualification": "vendor_reported", "unit_evidence": "reference_protocol",
                    "observed_at": observed, "time_basis": "activity_day", "fetched_at": None}
    program = _number(row.get("runningProgram"), 1)
    if program is not None:
        plan = {"source": "zepp", "qualification": "vendor_reported",
                "running_program_id": int(program)}
        kind = _number(row.get("runningType"))
        if kind is not None:
            plan["running_type_code"] = int(kind)
        if type(row.get("dailyPlanFinished")) is bool:
            plan["plan_session_finished"] = row["dailyPlanFinished"]
        score = _number(row.get("dailyScore"), 0, 100)
        if score is not None:
            plan["plan_session_score_percent"] = score
        result["vendor_training_plan"] = plan
    pb = _object(row.get("pb"))
    if isinstance(pb, dict):
        values = {str(k): v for k, v in pb.items() if len(str(k)) <= 128 and _number(v) is not None}
        if values:
            result["vendor_personal_bests"] = {"source": "zepp", "qualification": "vendor_flagged_unit_unverified", "values": values}
    parent = row.get("parent_trackid")
    children = _object(row.get("child_list"))
    if _number(parent, 1) is not None or isinstance(children, list) and children:
        result["vendor_multisport"] = {"parent_track_id": str(parent) if parent is not None else None,
            "child_track_ids": [str(v) for v in children if isinstance(v, (str, int))][:1000] if isinstance(children, list) else [],
            "qualification": "vendor_reported"}
    # Never borrow stale strength rows from running/swimming summaries.
    groups = _object(row.get("strength_training_group"))
    if (training_family == "strength" or str(row.get("type")) in {"21", "52"}) and _number(row.get("total_group"), 1) and isinstance(groups, list):
        sets = []
        for index, item in enumerate(groups[:2000]):
            if not isinstance(item, dict):
                continue
            numeric = {str(k): float(v) for k, v in item.items() if _number(v) is not None}
            if numeric:
                sets.append({"index": index, "vendor_fields": numeric, "qualification": "vendor_reported_codes_unverified"})
        result["vendor_sets"] = sets
    for name in ("vendor_training_plan", "vendor_personal_bests", "vendor_multisport"):
        if name in result:
            result[name].update(source="zepp", observed_at=observed, source_scope="workout_summary", time_basis="activity_day")
    return result


def parts(value):
    if isinstance(value, str):
        return iter(value.split(";"))
    iterator = getattr(value, "iter_parts", None)
    return iterator() if callable(iterator) else iter(())


def lap_rows(encoded):
    output = []
    columns = {index: name for index, (name, _) in LAP_COLUMNS.items() if index > 2}
    for part in parts(encoded):
        cells = part.split(",")
        if len(cells) < 3:
            continue
        first = [_number(v) for v in cells[:3]]
        if any(value is None for value in first):
            continue
        metrics = {name: value for i, name in columns.items() if i < len(cells)
                   and (value := _number(cells[i])) is not None}
        numeric = {str(i): value for i, cell in enumerate(cells) if len(cell) <= 64
                   and (value := _number(cell, -1e12)) is not None}
        output.append(WorkoutLap(index=int(first[0]), duration_seconds=int(first[1]), distance_meters=first[2],
                                 vendor_metrics=metrics, vendor_numeric_columns=numeric,
                                 qualification="vendor_columns_reference_only" if metrics else "vendor_reported"))
    return output


GPS_FIELDS = {"longitude_latitude", "altitude", "accuracy", "DEMAltitude"}
NORMALIZED_FIELDS = {"trackid", "time", "heart_rate", "speed", "equivPace", "currentDistance",
    "time_delta_altitude", "power_meter", "gait", "runPosture", "lap", "pause", "strengthSets",
    "source", "provider", "memo"}


def workout_detail_catalog():
    units = {field: {"metric": metric, "unit": unit, "scale": scale,
        "encoding": "delta_time_absolute_value", "qualification": "reference_protocol"}
        for field, metric, unit, scale, _, _ in ABSOLUTE_STREAM_SPECS}
    units.update({field: {"unit": "vendor_raw", "qualification": "vendor_encoded_unverified"}
                  for field in sorted(GPS_FIELDS)})
    units.update({"heart_rate": {"unit": "bpm", "encoding": "delta_time_delta_value",
                                  "qualification": "reference_protocol"},
                  "gait": {"metrics": {"cadence": "spm", "stride_length": "cm"},
                           "encoding": "delta_time_columns", "qualification": "reference_protocol"},
                  "runPosture": {"metrics": {"ground_contact_time": "ms", "vertical_oscillation": "mm",
                                              "vertical_stride_ratio": "%"},
                                 "encoding": "delta_time_columns", "qualification": "reference_protocol"}})
    return {"stream_units": units, "lap_columns": {str(index): {"name": name, "unit": unit,
        "qualification": "vendor_unit_unverified" if unit == "vendor_raw" else "reference_protocol"}
        for index, (name, unit) in LAP_COLUMNS.items()}, "unknown_streams": "vendor_encoded_unverified"}


def detail_extras(data, laps):
    splits, gps, streams = {}, {}, {}
    fields = []
    total_bytes = 0
    for field, value in data.items():
        if field in NORMALIZED_FIELDS:
            if value is not None:
                fields.append(field)
            continue
        if not isinstance(value, str) and not callable(getattr(value, "iter_parts", None)):
            continue
        encoded = []
        for part in parts(value):
            total_bytes += len(part.encode("utf-8"))
            if total_bytes > 8 * 1024 * 1024:
                from ..parser import WorkoutDetailLimitError
                raise WorkoutDetailLimitError("运动扩展明细超过大小上限")
            if part:
                encoded.append(part)
        if not encoded:
            continue
        fields.append(field)
        if field in {"kilo_pace", "mile_pace"}:
            rows = []
            for part in encoded:
                cells = part.split(",")
                if len(cells) < 2:
                    continue
                index, seconds = _number(cells[0]), _number(cells[1])
                if index is None or seconds is None:
                    continue
                additional = {name: val for i, name in ((4, "avg_heart_rate_bpm"), (5, "elapsed_seconds_cumulative"),
                    (9, "avg_stride_length_cm"), (13, "avg_cadence_spm")) if i < len(cells) and (val := _number(cells[i])) is not None}
                rows.append({"index": int(index), "duration_seconds": seconds, "vendor_metrics": additional,
                             "qualification": "duration_reference_protocol_other_columns_unverified"})
            splits["km" if field == "kilo_pace" else "mile"] = rows
        else:
            target = gps if field in GPS_FIELDS else streams
            target[field] = {"source": "zepp", "source_field": field, "unit": "vendor_raw",
                             "encoding": "vendor_delimited", "qualification": "vendor_encoded_unverified",
                             "rows": encoded}
    swimming = {}
    candidates = [lap for lap in laps if "swolf" in lap.vendor_metrics and "strokes" in lap.vendor_metrics]
    if candidates:
        distances = Counter(lap.distance_meters for lap in candidates if lap.distance_meters > 0)
        unit = distances.most_common(1)[0][0] if distances else None
        lengths = [lap for lap in candidates if unit is not None and lap.distance_meters == unit]
        for lap in candidates:
            lap.kind = "length_candidate" if lap in lengths else "set_summary_candidate"
        swimming = {"unit_distance_meters": unit, "length_count": len(lengths),
                    "set_count": len(candidates) - len(lengths), "qualification": "distance_classification_candidate",
                    "swolf_check": {"laps_checked": len(lengths),
                        "identity_holds": sum(abs(lap.vendor_metrics["swolf"] - lap.duration_seconds - lap.vendor_metrics["strokes"]) <= 1 for lap in lengths),
                        "tolerance": 1}}
    return {"splits": splits, "gps": gps, "vendor_streams": streams, "swimming": swimming,
            "available_fields": sorted(set(fields))}
