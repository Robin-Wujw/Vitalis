"""Training dose and T+0/T+1/T+2/T+3 observations, without causal claims."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import median

from vitalis.time import local_day, local_day_utc_bounds

from .baseline import BaselineEngine
from .contracts import (
    Availability,
    BaselineStats,
    ConfidenceBand,
    RecoveryOutcome,
    ResponseMetricObservation,
    SubjectiveFeedback,
    TrainingDoseMetric,
    TrainingDoseQuality,
    TrainingResponse,
    TrainingResponseDay,
    WorkoutExposure,
)
from .localization import CONFIDENCE_LABELS
from .profile import RawDailyProfile


RESPONSE_METRICS = {
    "hrv_rmssd": "ms", "hrv_sdnn": "ms", "sleep_hrv": "ms",
    "resting_hr": "bpm", "sleep_rhr": "bpm", "sleep_duration": "min",
}
HRV_METRICS = {"hrv_rmssd", "hrv_sdnn", "sleep_hrv"}
RHR_METRICS = {"resting_hr", "sleep_rhr"}
ACTIVITY_METRICS = {"steps": "steps", "active_minutes": "min"}
WINDOW_STATUS_LABELS = {
    "not_due": "观察窗口尚未到达", "missing": "窗口已到但数据缺失",
    "partial": "窗口部分可用", "available": "窗口数据可用", "confounded": "窗口存在混杂",
}
RECOVERY_LABELS = {
    RecoveryOutcome.RETURNED_TO_BASELINE: "已初次回到个人基线",
    RecoveryOutcome.NOT_RETURNED: "三天内尚未回到个人基线",
    RecoveryOutcome.CONFOUNDED: "恢复窗口存在训练或活动混杂",
    RecoveryOutcome.INSUFFICIENT_DATA: "恢复数据不足",
}


class TrainingResponseEngine:
    def build(
        self,
        analysis_run_id: str,
        raw: RawDailyProfile,
        feedback: list[SubjectiveFeedback],
        recommendation_by_workout: dict[tuple[str, str] | str, str],
        history_days: int = 90,
    ) -> list[TrainingResponse]:
        if history_days < 1:
            raise ValueError("history_days must be positive")
        cutoff = _utc(raw.as_of)
        zone = raw.timezone_name or "UTC"
        start = raw.day - timedelta(days=history_days - 1)
        known: dict[tuple[str, str], dict] = {}
        for workout in raw.workouts:
            day = workout.get("local_day")
            if not workout.get("workout_id") or type(day) is not date or day > raw.day:
                continue
            if not _known_workout(workout, cutoff, zone):
                continue
            known.setdefault(_workout_key(workout), workout)
        workouts = sorted(
            (item for item in known.values() if start <= item["local_day"] <= raw.day),
            key=lambda item: (item["local_day"], *_workout_key(item)),
        )
        series = _qualified_series(raw, cutoff, zone)
        feedback_by_workout: dict[tuple[str, str], list[SubjectiveFeedback]] = defaultdict(list)
        seen_feedback: set[str] = set()
        for item in sorted(feedback, key=lambda item: (_utc(item.created_at), item.id)):
            key = (item.workout_source, item.workout_id)
            workout = known.get(key)
            if (item.id in seen_feedback or item.user_id != raw.user_id or workout is None
                    or not workout["local_day"] <= item.date <= raw.day
                    or _utc(item.created_at) > cutoff):
                continue
            seen_feedback.add(item.id)
            feedback_by_workout[key].append(item)
        id_counts = Counter(key[1] for key in known)
        responses = []
        for workout in workouts:
            key = _workout_key(workout)
            recommendation_id = recommendation_by_workout.get(key)
            if recommendation_id is None and id_counts[key[1]] == 1:
                recommendation_id = recommendation_by_workout.get(key[1])
            responses.append(self._one(
                analysis_run_id, raw, workout, list(known.values()), series,
                feedback_by_workout.get(key, []), recommendation_id, cutoff, zone,
            ))
        return responses

    def _one(
        self, analysis_run_id, raw, workout, known_workouts, series,
        feedback, recommendation_id, cutoff, zone,
    ) -> TrainingResponse:
        workout_day = workout["local_day"]
        relevant = {
            metric: [point for point in points
                     if workout_day - timedelta(days=28) <= point.day <= workout_day + timedelta(days=3)]
            for metric, points in series.items()
        }
        baseline_map = {
            _baseline_key(item): item
            for items in BaselineEngine().build(relevant, workout_day).values()
            for item in items if item.window_days == 28
        }
        stream_keys = sorted(
            (key for key in baseline_map if key[0] in RESPONSE_METRICS), key=_sort_key,
        )
        for metrics, fallback, unit in (
            (HRV_METRICS, "hrv_rmssd", "ms"), (RHR_METRICS, "resting_hr", "bpm"),
            ({"sleep_duration"}, "sleep_duration", "min"),
        ):
            if not any(key[0] in metrics for key in stream_keys):
                stream_keys.append((fallback, "unknown", "unknown", None, unit))
        exposure = _exposure(workout, cutoff, zone)
        response_days = []
        end = exposure.ended_at
        for offset in (0, 1, 2, 3):
            day = workout_day + timedelta(days=offset)
            start_at, end_at = local_day_utc_bounds(day, zone)
            due = day <= raw.day and cutoff >= end_at
            overlaps = sorted({
                f"{_workout_key(item)[0]}:{_workout_key(item)[1]}"
                for item in known_workouts
                if _workout_key(item) != _workout_key(workout)
                and workout_day <= item["local_day"] <= day and item["local_day"] <= raw.day
            })
            reasons = [f"overlapping_training:{key}" for key in overlaps]
            for activity_day in (workout_day + timedelta(days=index) for index in range(offset + 1)):
                if activity_day <= raw.day:
                    reasons.extend(_activity_confounders(activity_day, relevant, baseline_map))
            reasons = sorted(set(reasons))
            observations = [
                _observation(
                    key, baseline_map.get(key), relevant.get(key[0], []), day,
                    cutoff, due, end if offset == 0 else None,
                    offset == 0, bool(reasons),
                )
                for key in sorted(stream_keys, key=_sort_key)
            ]
            expected = len(observations) if due else 0
            observed = sum(item.observed for item in observations)
            comparable = sum(item.status == Availability.AVAILABLE for item in observations)
            status = _window_status(due, observed, comparable, expected, reasons)
            limitations = []
            if offset == 0:
                limitations.append("T+0 仅使用有精确时刻且晚于训练结束的生理观测；日期级睡眠不表示训练后反应。")
                if end is None:
                    limitations.append("训练结束时刻未知，不能定位当天训练后的生理观测。")
            history_verified = _history_verified(raw, workout_day, day)
            if history_verified is not True:
                limitations.append("训练历史覆盖未完整验证，未记录训练不等于已确认休息。")
            if not due:
                limitations.append("窗口要到本地日终才成熟；当前值不计入已到窗口分母。")
            window_confidence = _confidence(_ratio(comparable, expected), bool(reasons))
            available_confidences = [item.confidence for item in observations
                                     if item.status == Availability.AVAILABLE]
            if available_confidences:
                window_confidence = _cap_confidence(window_confidence, min(
                    available_confidences, key=_confidence_rank,
                ))
            response_days.append(TrainingResponseDay(
                day_offset=offset, date=day, status=status,
                status_label=WINDOW_STATUS_LABELS[status], as_of=cutoff,
                window_start=max(start_at, end) if offset == 0 and end is not None else start_at,
                window_end=end_at, observations=observations,
                observed=observed, expected=expected, comparable_count=comparable,
                coverage=_ratio(observed, expected), feedback=[item for item in feedback if item.date == day],
                overlapping_workout_ids=overlaps, confounding_reasons=reasons,
                training_history_verified=history_verified, confidence=window_confidence,
                dose_quality=exposure.dose_quality if offset == 0 else None, limitations=limitations,
            ))
        post_days = response_days[1:]
        observed = sum(item.observed for item in post_days)
        expected = sum(item.expected for item in post_days)
        comparable = sum(item.comparable_count for item in post_days)
        confounding_reasons = sorted({reason for item in response_days for reason in item.confounding_reasons})
        recovery = _recovery(post_days)
        confidence = _confidence(_ratio(comparable, expected), bool(confounding_reasons))
        due_confidences = [item.confidence for item in post_days if item.comparable_count]
        if due_confidences:
            confidence = _cap_confidence(confidence, min(due_confidences, key=_confidence_rank))
        if exposure.dose_quality.status != "available":
            confidence = _cap_confidence(confidence, ConfidenceBand.LOW)
        limitations = list(exposure.dose_quality.limitations)
        if any(item.status == "not_due" for item in post_days):
            limitations.append("部分观察窗口尚未成熟，未计入缺失或覆盖率分母。")
        if any(item.status in {"missing", "partial"} for item in post_days):
            limitations.append("部分训练后生理观测或同源 28 天基线不足。")
        if confounding_reasons:
            limitations.append("训练或活动混杂与观察同时存在，不能归因于单次训练。")
        limitations.append("恢复小时表示本地日偏移乘 24，不是精确的训练后经过时间。")
        return TrainingResponse(
            analysis_run_id=analysis_run_id, user_id=raw.user_id, exposure=exposure,
            recommendation_id=recommendation_id, feedback=feedback, response_days=response_days,
            missing_windows=[f"T+{item.day_offset} {item.status_label}" for item in response_days
                             if item.status in {"missing", "partial"}],
            overlapping_workout_ids=sorted({key for item in response_days for key in item.overlapping_workout_ids}),
            confounding_reasons=confounding_reasons, as_of=cutoff,
            recovery_status=recovery["legacy_status"],
            recovery_status_label=RECOVERY_LABELS[recovery["legacy_status"]],
            recovery_hours=_offset_hours(recovery["initial"]),
            initial_recovery_offset=recovery["initial"],
            initial_recovery_hours=_offset_hours(recovery["initial"]),
            sustained_recovery_status=recovery["status"],
            sustained_recovery_offset=recovery["sustained"],
            sustained_recovery_hours=_offset_hours(recovery["sustained"]),
            sustained_through_offset=recovery["through"],
            recovery_evaluable_windows=recovery["evaluable"],
            recovery_expected_windows=sum(item.status != "not_due" for item in post_days),
            confidence=confidence, confidence_label=CONFIDENCE_LABELS[confidence.value],
            coverage=_ratio(observed, expected), observed=observed, expected=expected,
            limitations=list(dict.fromkeys(limitations)),
        )


def _observation(key, baseline, points, day, cutoff, due, workout_end, same_day, confounded):
    metric, source, scope, device, unit = key
    matches = [point for point in points if point.day == day and _point_key(point) == key]
    limitations = []
    if same_day:
        matches = [point for point in matches
                   if workout_end is not None and isinstance(point.observed_at, datetime)
                   and _utc(point.observed_at) >= workout_end]
    if not due:
        matches = []
    value = float(median(point.value for point in matches)) if matches else None
    baseline_available = baseline is not None and baseline.status == Availability.AVAILABLE
    deviation = BaselineEngine.deviation(value, baseline) if value is not None and baseline_available else None
    available = deviation is not None and deviation.direction != "unknown"
    if not baseline_available:
        limitations.append("same_stream_28d_baseline_insufficient")
    if not matches and due:
        limitations.append("window_observation_missing")
    observed_at = max((point.observed_at for point in matches), key=_observation_time_key, default=None)
    observed = int(value is not None)
    expected = int(due)
    confidence = _baseline_confidence(baseline) if available else ConfidenceBand.NONE
    if confounded:
        confidence = _cap_confidence(confidence, ConfidenceBand.LOW)
    fetched_times = [_parse_datetime(getattr(point, "fetched_at", None)) for point in matches]
    fetched_at = max((item for item in fetched_times if item is not None), default=None)
    return ResponseMetricObservation(
        metric=metric, source=source, source_scope=scope, device_id=device, unit=unit,
        calendar_semantics="sleep_day" if metric.startswith("sleep_") else "calendar_day",
        as_of=cutoff, observed_at=observed_at, fetched_at=fetched_at,
        source_fields=sorted({point.source_field for point in matches if point.source_field}),
        status=Availability.AVAILABLE if available else Availability.INSUFFICIENT_DATA,
        value=round(value, 3) if value is not None else None,
        baseline=baseline.reference_value if baseline is not None else None,
        baseline_reference=baseline.reference_value if baseline is not None else None,
        deviation=deviation.percent if deviation is not None else None,
        deviation_percent=deviation.percent if deviation is not None else None,
        robust_z=deviation.robust_z if deviation is not None else None,
        direction=deviation.direction if deviation is not None else "unknown",
        observed=observed, expected=expected, coverage=_ratio(observed, expected), sample_count=len(matches),
        baseline_observed_days=baseline.distinct_days if baseline is not None else 0,
        baseline_coverage=baseline.coverage_ratio if baseline is not None else 0,
        confidence=confidence, limitations=limitations,
    )


def _recovery(days):
    evaluations = {item.day_offset: _day_recovered(item) for item in days}
    initial = next((offset for offset in sorted(evaluations) if evaluations[offset] is True), None)
    due = [item for item in days if item.status != "not_due"]
    confounded = any(item.status == "confounded" for item in due)
    evaluable = sum(value is not None for value in evaluations.values())
    result = dict(initial=initial, sustained=None, through=None, evaluable=evaluable,
                  legacy_status=RecoveryOutcome.INSUFFICIENT_DATA, status="insufficient_data")
    if confounded:
        result.update(legacy_status=RecoveryOutcome.CONFOUNDED, status="confounded")
        # The legacy combined outcome cannot claim an attributable recovery time.
        result["initial"] = None
        return result
    if initial is not None:
        result["legacy_status"] = RecoveryOutcome.RETURNED_TO_BASELINE
    elif len(due) == 3 and evaluable == 3:
        result["legacy_status"] = RecoveryOutcome.NOT_RETURNED
    if not due:
        result["status"] = "not_due"
        return result
    latest = max(item.day_offset for item in due)
    for candidate in range(1, latest):
        if all(evaluations[offset] is True for offset in range(candidate, latest + 1)):
            result.update(status="sustained", sustained=candidate, through=latest)
            return result
    if initial is not None:
        following = [evaluations[offset] for offset in range(initial + 1, latest + 1)]
        if any(value is False for value in following):
            result["status"] = "not_sustained"
        elif any(value is None for value in following):
            result["status"] = "insufficient_data"
        elif latest < 3:
            result["status"] = "not_due"
    elif evaluable == len(due):
        result["status"] = "not_sustained"
    return result


def _day_recovered(day):
    if day.status in {"not_due", "missing", "confounded"}:
        return None
    hrv = [item for item in day.observations if item.metric in HRV_METRICS]
    rhr = [item for item in day.observations if item.metric in RHR_METRICS]
    sleep = [item for item in day.observations if item.metric == "sleep_duration"]
    if not hrv or not rhr:
        return None
    if any(item.status != Availability.AVAILABLE for item in hrv + rhr + sleep):
        return None
    return (all(item.direction in {"near", "above"} for item in hrv)
            and all(item.direction in {"near", "below"} for item in rhr)
            and all(item.direction in {"near", "above"} for item in sleep))


def _exposure(workout, cutoff, zone):
    data = workout.get("data") or {}
    source = _workout_key(workout)[0]
    day = workout["local_day"]
    start = _parse_datetime(workout.get("started_at") or data.get("started_at"))
    duration = _dose_number(data, "duration", integer=True, explicit_zero=True)
    end = _parse_datetime(workout.get("ended_at") or data.get("ended_at"))
    if end is None and start is not None and duration is not None:
        end = start + timedelta(minutes=duration)
    if start is not None and end is not None and end < start:
        end = None
    set_info = _strength_dose(workout, cutoff)
    values = {
        "duration_minutes": duration, "vendor_load": _dose_number(data, "load"),
        "heart_rate_avg_bpm": _positive_integer(data.get("heart_rate_avg")),
        "heart_rate_max_bpm": _positive_integer(data.get("heart_rate_max")),
        "sets": set_info["sets"], "repetitions_total": set_info["total_reps"],
    }
    family = str(data.get("training_family") or "unknown")
    strength = family == "strength" or str(data.get("type") or "").lower() == "strength"
    expected_fields = ["duration_minutes", "vendor_load", "heart_rate_avg_bpm"]
    if strength:
        expected_fields.extend(["sets", "repetitions_total"])
    observations = []
    fetched = _parse_datetime(workout.get("fetched_at") or data.get("fetched_at"))
    for metric, input_key, unit in (
        ("duration_minutes", "duration", "min"), ("vendor_load", "load", "vendor_load"),
        ("heart_rate_avg_bpm", "heart_rate_avg", "bpm"),
        ("heart_rate_max_bpm", "heart_rate_max", "bpm"),
    ):
        if metric == "heart_rate_max_bpm" and values[metric] is None:
            continue
        present = values[metric] is not None
        observations.append(TrainingDoseMetric(
            metric=metric, value=values[metric], unit=unit, source=source,
            source_scope="workout_summary", source_field=input_key,
            observed_at=start or day, fetched_at=fetched, as_of=cutoff,
            status="available" if present else "missing", observed=int(present), expected=1,
            coverage=float(present),
        ))
    if strength:
        set_present = set_info["sets"] is not None
        observations.append(TrainingDoseMetric(
            metric="sets", value=set_info["sets"], unit="sets", source=source,
            source_scope=set_info["scope"], source_field=set_info["field"],
            observed_at=start or day, fetched_at=fetched, as_of=cutoff,
            status="available" if set_present else "missing", observed=int(set_present), expected=1,
            coverage=float(set_present), limitations=set_info["limitations"],
        ))
        rep_expected = set_info["sets"] or len(set_info["reps"]) or 1
        rep_observed = sum(value is not None for value in set_info["reps"])
        rep_observed = min(rep_observed, rep_expected)
        rep_status = ("available" if set_info["total_reps"] is not None else
                      "partial" if rep_observed else "missing")
        observations.append(TrainingDoseMetric(
            metric="repetitions_total", value=set_info["total_reps"], unit="repetitions", source=source,
            source_scope=set_info["reps_scope"], source_field=set_info["reps_field"],
            observed_at=start or day, fetched_at=fetched, as_of=cutoff, status=rep_status,
            observed=rep_observed, expected=rep_expected, coverage=_ratio(rep_observed, rep_expected),
            limitations=set_info["limitations"],
        ))
    observed_fields = [item.metric for item in observations if item.status == "available"]
    observed = sum(field in observed_fields for field in expected_fields)
    status = "available" if observed == len(expected_fields) else "partial" if observed else "missing"
    limitations = [f"dose_field_missing:{field}" for field in expected_fields if field not in observed_fields]
    limitations.extend(set_info["limitations"] if strength else [])
    if values["vendor_load"] is not None:
        limitations.append("厂商负荷保留来源单位，不等于通用能量或 Vitalis 恢复评分。")
    if fetched is None:
        limitations.append("dose_fetched_at_unavailable")
    quality = TrainingDoseQuality(
        status=status, status_label=WINDOW_STATUS_LABELS[status], source=source,
        source_scope="workout_summary", as_of=cutoff, observations=observations,
        observed_fields=observed_fields, expected_fields=expected_fields,
        observed=observed, expected=len(expected_fields), coverage=observed / len(expected_fields),
        limitations=list(dict.fromkeys(limitations)),
    )
    return WorkoutExposure(
        workout_id=str(workout["workout_id"]), source=source, date=day,
        observed_at=start or day, as_of=cutoff, started_at=start, ended_at=end, fetched_at=fetched,
        type=str(data.get("type") or "other"), sport_mode=str(data.get("sport_mode") or "unknown"),
        sport_mode_label=str(data.get("sport_mode_label") or "未知运动"), training_family=family,
        training_family_label=str(data.get("training_family_label") or "未知训练家族"),
        **values, repetitions_by_set=set_info["reps"], vendor_reported_sets=set_info["vendor_sets"],
        observed_fields=observed_fields, dose_quality=quality,
    )


def _strength_dose(workout, cutoff):
    data = workout.get("data") or {}
    vendor_sets = _positive_integer(data.get("vendor_reported_sets"))
    detail = workout.get("detail") or {}
    raw_sets = detail.get("strength_sets") if isinstance(detail, dict) else None
    rows = []
    seen = set()
    limitations = []
    for index, item in enumerate(raw_sets if isinstance(raw_sets, list) else []):
        if not isinstance(item, dict):
            item = item.model_dump() if hasattr(item, "model_dump") else {}
        if not any(item.get(key) is not None for key in ("order", "repetitions", "duration_seconds", "started_at")):
            continue
        started = _parse_datetime(item.get("started_at"))
        if started is not None and started > cutoff:
            continue
        key = (item.get("source"), item.get("order"), item.get("started_at"))
        if key == (None, None, None):
            key = ("row", index)
        if key in seen:
            limitations.append("duplicate_strength_set_ignored")
            continue
        seen.add(key)
        rows.append(item)
    reps = [_positive_integer(item.get("repetitions")) for item in rows]
    count = vendor_sets or (len(rows) if rows else None)
    scope = "workout_summary" if vendor_sets else "workout_detail"
    field = "vendor_reported_sets" if vendor_sets else "strength_sets"
    reps_scope, reps_field = "workout_detail", "strength_sets.repetitions"
    if rows and vendor_sets is not None and len(rows) != vendor_sets:
        limitations.append("strength_detail_set_count_differs_from_summary")
    if not rows:
        confirmed = []
        for item in workout.get("confirmed_exercises") or []:
            item = item if isinstance(item, dict) else item.model_dump()
            created = _parse_datetime(item.get("created_at"))
            if created is not None and created > cutoff:
                continue
            if item.get("workout_id", workout["workout_id"]) != workout["workout_id"]:
                continue
            if item.get("workout_source", _workout_key(workout)[0]) != _workout_key(workout)[0]:
                continue
            if item.get("source") in {"user_confirmed", "vendor_explicit"}:
                confirmed.append(item)
        counts = [_positive_integer(item.get("sets")) for item in confirmed]
        if confirmed and all(value is not None for value in counts):
            explicit_count = sum(counts)
            count = vendor_sets or explicit_count
            scope, field = "confirmed_exercises", "confirmed_exercises.sets"
            reps_scope, reps_field = "confirmed_exercises", "confirmed_exercises.repetitions"
            reps = [value for item, sets in zip(confirmed, counts)
                    for value in [_positive_integer(item.get("repetitions"))] * sets]
    total_reps = sum(reps) if reps and all(value is not None for value in reps) and len(reps) == count else None
    return dict(sets=count, vendor_sets=vendor_sets, total_reps=total_reps, reps=reps,
                scope=scope, field=field, reps_scope=reps_scope, reps_field=reps_field,
                limitations=limitations)


def _activity_confounders(day, series, baseline_map):
    reasons = []
    for metric in ACTIVITY_METRICS:
        streams = defaultdict(list)
        for point in series.get(metric, []):
            if point.day == day:
                streams[_point_key(point)].append(point.value)
        for key, values in streams.items():
            baseline = baseline_map.get(key)
            if baseline is None or baseline.status != Availability.AVAILABLE:
                continue
            value = float(median(values))
            deviation = BaselineEngine.deviation(value, baseline)
            threshold = 10_000 if metric == "steps" else 60
            if value >= threshold and deviation.percent is not None and deviation.percent >= 50:
                reasons.append(f"elevated_activity:{metric}:{key[1]}:{key[2]}:{key[3] or 'none'}:{day.isoformat()}")
    return reasons


def _qualified_series(raw, cutoff, zone):
    result = {}
    for metric, unit in {**RESPONSE_METRICS, **ACTIVITY_METRICS}.items():
        points = []
        for point in raw.series.get(metric, []):
            if point.metric != metric or point.unit != unit or point.day > raw.day:
                continue
            value = point.value
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                continue
            if value < 0 or (value == 0 and metric not in {"sleep_duration", "steps", "active_minutes"}):
                continue
            if not point.source or point.source == "unknown" or not point.source_scope or point.source_scope == "unknown":
                continue
            if point.source_scope == "device" and not point.device_id:
                continue
            observed = point.observed_at
            if isinstance(observed, datetime):
                if _utc(observed) > cutoff:
                    continue
            elif type(observed) is date:
                if observed > local_day(cutoff, zone):
                    continue
            else:
                continue
            fetched = _parse_datetime(getattr(point, "fetched_at", None))
            if fetched is not None and fetched > cutoff:
                continue
            points.append(point)
        result[metric] = points
    return result


def _history_verified(raw, first, last):
    coverage = raw.training_history_coverage
    if "verified_days" not in coverage:
        return None
    verified = {item.isoformat() if isinstance(item, date) else str(item)
                for item in coverage.get("verified_days", [])}
    return all((first + timedelta(days=index)).isoformat() in verified
               for index in range((last - first).days + 1))


def _known_workout(workout, cutoff, zone):
    data = workout.get("data") or {}
    started = _parse_datetime(workout.get("started_at") or data.get("started_at"))
    if started is not None:
        if started > cutoff or local_day(started, zone) != workout["local_day"]:
            return False
    elif workout["local_day"] > local_day(cutoff, zone):
        return False
    fetched = _parse_datetime(workout.get("fetched_at") or data.get("fetched_at"))
    return fetched is None or fetched <= cutoff


def _window_status(due, observed, comparable, expected, reasons):
    if not due:
        return "not_due"
    if reasons:
        return "confounded"
    if not observed:
        return "missing"
    return "available" if comparable == expected else "partial"


def _workout_key(workout):
    return str(workout.get("source") or "unknown"), str(workout["workout_id"])


def _point_key(point):
    return point.metric, point.source, point.source_scope, point.device_id, point.unit


def _baseline_key(item):
    return item.metric, item.source, item.source_scope, item.device_id, item.unit


def _sort_key(key):
    return tuple(str(item or "") for item in key)


def _positive_integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if isfinite(value) and value > 0 and float(value).is_integer() else None


def _dose_number(data, key, *, integer=False, explicit_zero=False):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return None
    if integer and not float(value).is_integer():
        return None
    if explicit_zero and value == 0 and key not in (data.get("observed_fields") or []):
        return None
    return int(value) if integer else float(value)


def _parse_datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00")) if len(value) > 10 else None
        except ValueError:
            return None
    return _utc(value) if isinstance(value, datetime) else None


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _observation_time_key(value):
    return _utc(value) if isinstance(value, datetime) else datetime.combine(value, datetime.min.time(), timezone.utc)


def _offset_hours(offset):
    return offset * 24 if offset is not None else None


def _ratio(observed, expected):
    return round(observed / expected, 4) if expected else None


def _confidence(coverage, confounded=False):
    if coverage is None or coverage == 0:
        result = ConfidenceBand.NONE
    elif coverage >= 0.8:
        result = ConfidenceBand.HIGH
    elif coverage >= 0.6:
        result = ConfidenceBand.MODERATE
    else:
        result = ConfidenceBand.LOW
    return _cap_confidence(result, ConfidenceBand.LOW) if confounded else result


def _baseline_confidence(baseline: BaselineStats):
    return _confidence(baseline.coverage_ratio)


def _confidence_rank(value):
    return (ConfidenceBand.NONE, ConfidenceBand.LOW, ConfidenceBand.MODERATE, ConfidenceBand.HIGH).index(value)


def _cap_confidence(value, maximum):
    return min((value, maximum), key=_confidence_rank)
