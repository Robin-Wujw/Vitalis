"""Descriptive personal patterns over qualified windows and explicit feedback."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median

from .contracts import (
    Availability,
    ConfidenceBand,
    PersonalAssociation,
    PersonalMetricStats,
    PersonalModel,
    PersonalResponsePattern,
    PersonalWindowSummary,
    RecoveryOutcome,
    SubjectiveFeedback,
    TrainingResponse,
)
from .localization import CONFIDENCE_LABELS


FEEDBACK_FIELDS = {
    "session_rpe": ("score_1_10", 1, 10),
    "physical_fatigue": ("score_1_5", 1, 5),
    "mental_state": ("score_1_5", 1, 5),
    "muscle_soreness": ("score_1_5", 1, 5),
}


def summarize_training_responses(
    responses: list[TrainingResponse],
    feedback: list[SubjectiveFeedback],
    *,
    as_of: datetime,
) -> dict:
    """Project response evidence for a caller-owned calendar period.

    Callers select responses and feedback for the desired period first. This
    function intentionally does not infer a period from T+1/T+3 windows, so a
    weekly or monthly report cannot accidentally include a 90-day total.
    """
    timestamp = _utc(as_of)
    response_items = list(responses)
    feedback_items = list({item.id: item for item in feedback}.values())
    windows = _window_summaries(response_items, timestamp)
    observed = sum(item.observed for item in windows)
    expected = sum(item.expected for item in windows)
    confounded = [item for item in response_items if _response_confounded(item)]
    initial_returned = [item for item in response_items if item.initial_recovery_offset is not None]
    sustained = [item for item in response_items if item.sustained_recovery_status == "sustained"]
    return {
        "response_count": len(response_items),
        "window_summaries": [item.model_dump(mode="json") for item in windows],
        "feedback_distributions": [
            item.model_dump(mode="json")
            for item in _feedback_stats(response_items, feedback_items, timestamp)
        ],
        "observed": observed,
        "expected": expected,
        "coverage": round(observed / expected, 4) if expected else None,
        "initial_recovery_returned_count": len(initial_returned),
        "sustained_recovery_count": len(sustained),
        "confounded_response_count": len(confounded),
        "confounded_response_ratio": round(len(confounded) / len(response_items), 4)
        if response_items else 0,
        "as_of": timestamp.isoformat(),
    }


class PersonalModelEngine:
    def build(
        self, analysis_run_id, daily, responses: list[TrainingResponse],
        associations: list[PersonalAssociation], *,
        feedback: list[SubjectiveFeedback] | None = None, generated_at=None,
    ) -> PersonalModel:
        timestamp = _utc(generated_at or getattr(daily, "generated_at", None) or datetime.now(timezone.utc))
        responses = _qualified_responses(responses, daily.user_id, daily.date, timestamp)
        all_feedback = _qualified_feedback(responses, feedback or [], daily.user_id, daily.date, timestamp)
        patterns = []
        for group_type, key_name, label_name in (
            ("training_family", "training_family", "training_family_label"),
            ("sport_mode", "sport_mode", "sport_mode_label"),
        ):
            grouped = defaultdict(list)
            labels = {}
            for response in responses:
                key = getattr(response.exposure, key_name)
                grouped[key].append(response)
                labels[key] = getattr(response.exposure, label_name)
            for key, items in sorted(grouped.items()):
                metrics = _response_metric_stats(items, timestamp)
                comparable_metrics = [item for item in metrics if item.metric.endswith("deviation_percent")
                                      and item.eligible_count]
                coverage = median(item.coverage_ratio for item in comparable_metrics) if comparable_metrics else 0
                independent_samples = max((item.sample_count for item in comparable_metrics), default=0)
                confidence = _confidence(independent_samples, coverage)
                keys = {_response_key(item) for item in items}
                group_feedback = [item for item in all_feedback if (item.workout_source, item.workout_id) in keys]
                confounded_count = sum(_response_confounded(item) for item in items)
                patterns.append(PersonalResponsePattern(
                    group_type=group_type, group_key=key, group_label=labels[key],
                    response_count=len(items), metrics=metrics,
                    feedback_distributions=_feedback_stats(items, group_feedback, timestamp),
                    window_summaries=_window_summaries(items, timestamp),
                    confounded_response_count=confounded_count,
                    confounded_ratio=round(confounded_count / len(items), 4),
                    confidence=confidence, confidence_label=CONFIDENCE_LABELS[confidence.value],
                ))
        supported_associations = [
            item for item in associations if item.status == Availability.AVAILABLE
            and item.confidence in {ConfidenceBand.MODERATE, ConfidenceBand.HIGH}
        ]
        limitations = []
        if not patterns:
            limitations.append("尚无可用于建立训练响应模式的历史训练。")
        elif all(item.confidence in {ConfidenceBand.NONE, ConfidenceBand.LOW} for item in patterns):
            limitations.append("训练响应样本量或覆盖率有限，个人模式仍处于早期阶段。")
        if any(_response_confounded(item) for item in responses):
            limitations.append("混杂窗口不进入单次训练的生理反应分布；已观察和排除的分母仍保留。")
        if not all_feedback:
            limitations.append("尚无显式主观反馈；缺失反馈不推断为正常状态或已完成。")
        if not supported_associations:
            limitations.append("尚无中等或较高置信度的 60/90 天个人关联。")
        limitations.append("个人模式与关联是描述性 observed association，不能解释为训练因果。")
        return PersonalModel(
            analysis_run_id=analysis_run_id, user_id=daily.user_id, date=daily.date, generated_at=timestamp,
            baselines=[item for items in daily.baselines.values() for item in items
                       if item.window_days == 28 and item.status == Availability.AVAILABLE],
            long_term_trends=[item for item in daily.trends
                              if item.window_days == 90 and item.status == Availability.AVAILABLE],
            training_response_patterns=patterns,
            response_window_summaries=_window_summaries(responses, timestamp),
            subjective_feedback_distributions=_feedback_stats(responses, all_feedback, timestamp),
            personal_associations=supported_associations, limitations=limitations,
        )


def _qualified_responses(responses, user_id, target_day, cutoff):
    unique = {}
    for response in responses:
        if response.user_id != user_id or response.exposure.date > target_day:
            continue
        if response.as_of is not None and _utc(response.as_of) > cutoff:
            continue
        key = _response_key(response)
        previous = unique.get(key)
        if previous is None or (response.as_of is not None and
                                (previous.as_of is None or _utc(response.as_of) > _utc(previous.as_of))):
            unique[key] = response
    return sorted(unique.values(), key=lambda item: (item.exposure.date, *_response_key(item)))


def _qualified_feedback(responses, extra, user_id, target_day, cutoff):
    by_workout = {_response_key(item): item for item in responses}
    candidates = list(extra)
    for response in responses:
        candidates.extend(response.feedback)
        for window in response.response_days:
            candidates.extend(window.feedback)
    unique = {}
    for item in candidates:
        if item.user_id != user_id or item.date > target_day or _utc(item.created_at) > cutoff:
            continue
        if bool(item.workout_source) != bool(item.workout_id):
            continue
        if item.workout_id:
            response = by_workout.get((item.workout_source, item.workout_id))
            if response is None or item.date < response.exposure.date:
                continue
        unique.setdefault(item.id, item)
    return sorted(unique.values(), key=lambda item: (_utc(item.created_at), item.id))


def _response_metric_stats(responses, as_of):
    buckets = {}
    for response in responses:
        for window in response.response_days:
            for observation in window.observations:
                if observation.source in {None, "unknown"} or observation.source_scope in {None, "unknown"}:
                    continue
                for quantity in ("deviation_percent", "value"):
                    key = (f"{observation.metric}_t{window.day_offset}_{quantity}",
                           observation.source, observation.source_scope, observation.device_id,
                           "percent" if quantity == "deviation_percent" else observation.unit,
                           observation.unit, window.day_offset, observation.calendar_semantics)
                    bucket = buckets.setdefault(key, _bucket())
                    if window.status == "not_due":
                        bucket["not_due"] += 1
                        continue
                    bucket["eligible"] += 1
                    value = observation.deviation_percent if quantity == "deviation_percent" else observation.value
                    qualified_value = _finite(value)
                    if qualified_value is not None:
                        bucket["observed"] += 1
                    if window.status == "confounded" or window.confounding_reasons:
                        bucket["confounded"] += 1
                        continue
                    if quantity == "deviation_percent" and observation.status != Availability.AVAILABLE:
                        continue
                    if observation.as_of is not None and _utc(observation.as_of) > as_of:
                        continue
                    if isinstance(observation.observed_at, datetime) and _utc(observation.observed_at) > as_of:
                        continue
                    if qualified_value is not None:
                        bucket["values"].append(qualified_value)
        for metric, value, unit, due, qualified in (
            ("initial_recovery_hours", response.initial_recovery_hours, "hours",
             response.recovery_status != RecoveryOutcome.INSUFFICIENT_DATA or response.recovery_expected_windows > 0,
             not _response_confounded(response)),
            ("sustained_recovery_hours", response.sustained_recovery_hours, "hours",
             response.sustained_recovery_status != "not_due",
             response.sustained_recovery_status == "sustained" and not _response_confounded(response)),
        ):
            key = (metric, response.exposure.source, "training_response", None, unit, unit, None, "activity_day")
            bucket = buckets.setdefault(key, _bucket())
            if not due:
                bucket["not_due"] += 1
                continue
            bucket["eligible"] += 1
            if _response_confounded(response):
                bucket["confounded"] += 1
            if _finite(value) is not None:
                bucket["observed"] += 1
                if qualified:
                    bucket["values"].append(float(value))
        dose = response.exposure.dose_quality
        dose_metrics = dose.observations if dose is not None else []
        for observation in dose_metrics:
            key = (f"dose_{observation.metric}", observation.source, observation.source_scope,
                   observation.device_id, observation.unit, observation.unit, 0, "activity_day")
            bucket = buckets.setdefault(key, _bucket())
            bucket["eligible"] += 1
            value = _finite(observation.value)
            if value is not None and observation.status == "available":
                bucket["observed"] += 1
                bucket["values"].append(value)
    result = []
    for key, bucket in sorted(buckets.items(), key=lambda item: tuple(str(value or "") for value in item[0])):
        metric, source, scope, device, unit, input_unit, offset, semantics = key
        result.append(_stats(
            metric, bucket, source, scope, device, unit, input_unit, offset, semantics, as_of,
            "sessions" if offset is None else "due_stream_windows",
        ))
    return result


def _feedback_stats(responses, feedback, as_of):
    result = []
    for metric, (unit, minimum, maximum) in FEEDBACK_FIELDS.items():
        expected_contexts = set()
        if metric == "session_rpe":
            expected_contexts.update(_response_key(item) for item in responses)
        else:
            for response in responses:
                due_dates = {item.date for item in response.response_days if item.status != "not_due"}
                # Direct T+0 feedback is an observation even without a physiological T+0 window.
                due_dates.add(response.exposure.date)
                expected_contexts.update((*_response_key(response), day) for day in due_dates)
        latest = {}
        for item in feedback:
            if item.workout_id:
                context = (item.workout_source, item.workout_id)
                if metric != "session_rpe":
                    context = (*context, item.date)
            elif metric != "session_rpe":
                context = ("unlinked_day", item.date)
            else:
                continue
            expected_contexts.add(context)
            value = _finite(getattr(item, metric))
            if value is not None and minimum <= value <= maximum:
                latest[context] = value
        bucket = _bucket()
        bucket.update(values=list(latest.values()), observed=len(latest), eligible=len(expected_contexts))
        result.append(_stats(metric, bucket, "user", "explicit_feedback", None, unit, unit,
                             None, "activity_day", as_of, "due_feedback_contexts"))
    return result


def _window_summaries(responses, as_of):
    summaries = []
    for offset in (0, 1, 2, 3):
        windows = [item for response in responses for item in response.response_days if item.day_offset == offset]
        counts = Counter(item.status for item in windows)
        expected = sum(item.expected for item in windows if item.status != "not_due")
        observed = sum(item.observed for item in windows if item.status != "not_due")
        summaries.append(PersonalWindowSummary(
            day_offset=offset, response_count=len(windows), not_due_count=counts["not_due"],
            missing_count=counts["missing"], partial_count=counts["partial"],
            available_count=counts["available"], confounded_count=counts["confounded"],
            observed=observed, expected=expected,
            comparable_count=sum(item.comparable_count for item in windows if item.status != "not_due"),
            coverage=round(observed / expected, 4) if expected else None, as_of=as_of,
        ))
    return summaries


def _stats(metric, bucket, source, scope, device, unit, input_unit, offset, semantics, as_of, denominator):
    values = sorted(bucket["values"])
    center = float(median(values)) if values else None
    mad = float(median(abs(value - center) for value in values)) if values else None
    coverage = round(len(values) / bucket["eligible"], 4) if bucket["eligible"] else 0
    return PersonalMetricStats(
        metric=metric, source=source, source_scope=scope, device_id=device, unit=unit,
        input_unit=input_unit, day_offset=offset, calendar_semantics=semantics, as_of=as_of,
        median=round(center, 3) if center is not None else None,
        mad=round(mad, 3) if mad is not None else None,
        percentile_25=_quantile(values, 0.25), percentile_75=_quantile(values, 0.75),
        minimum=values[0] if values else None, maximum=values[-1] if values else None,
        sample_count=len(values), observed_count=bucket["observed"], eligible_count=bucket["eligible"],
        not_due_count=bucket["not_due"], confounded_count=bucket["confounded"], coverage_ratio=coverage,
        distribution=dict(Counter(_number_label(value) for value in values)),
        confidence=_confidence(len(values), coverage), denominator_basis=denominator,
    )


def _bucket():
    return dict(values=[], eligible=0, observed=0, not_due=0, confounded=0)


def _quantile(values, q):
    if not values:
        return None
    position = (len(values) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 3)


def _confidence(sample_count, coverage):
    if sample_count >= 8 and coverage >= 0.75:
        return ConfidenceBand.HIGH
    if sample_count >= 4 and coverage >= 0.6:
        return ConfidenceBand.MODERATE
    if sample_count >= 2 and coverage > 0:
        return ConfidenceBand.LOW
    return ConfidenceBand.NONE


def _response_key(response):
    return response.exposure.source, response.exposure.workout_id


def _response_confounded(response):
    return (response.recovery_status == RecoveryOutcome.CONFOUNDED or bool(response.confounding_reasons)
            or any(item.status == "confounded" for item in response.response_days))


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None
    return float(value)


def _number_label(value):
    return str(int(value)) if value.is_integer() else str(round(value, 3))


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
