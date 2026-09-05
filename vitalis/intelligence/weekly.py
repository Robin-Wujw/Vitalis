"""Weekly fact aggregation, inference selection, and deterministic actions."""

from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import mean, median

from .contracts import (
    Availability,
    ConfidenceBand,
    DailySleepHrvPoint,
    HealthEvent,
    QualityStatus,
    TrendDirection,
    TrendFeature,
    WeeklyActions,
    WeeklyActivityFacts,
    WeeklyDataQuality,
    WeeklyFacts,
    WeeklyFeedbackFacts,
    WeeklyInferences,
    WeeklyProfile,
    WeeklyRecommendation,
    WeeklyRecoveryFacts,
    WeeklySleepFacts,
    WeeklyTrainingFacts,
    OpenHealthBundle,
)
from .localization import CONFIDENCE_LABELS, QUALITY_LABELS
from .open_health.projection import coverage_summary, period_summary
from .profile import RawDailyProfile


PERIOD_DAYS = 7
MIN_COMPARISON_DAYS = 4


class WeeklyProfileEngine:
    def build(
        self,
        analysis_run_id: str,
        raw: RawDailyProfile,
        trends: list[TrendFeature],
        events: list[HealthEvent],
        feedback: list[dict] | None = None,
        evidence_refs: list | None = None,
        open_health_insights: OpenHealthBundle | None = None,
    ) -> WeeklyProfile:
        period_start = raw.day - timedelta(days=PERIOD_DAYS - 1)
        previous_start = period_start - timedelta(days=PERIOD_DAYS)
        sleep = _sleep_facts(raw, period_start, previous_start)
        recovery = _recovery_facts(raw, trends)
        training = _training_facts(raw, period_start, previous_start)
        activity = _activity_facts(raw, period_start, previous_start)
        feedback_facts = _feedback_facts(feedback or [])
        quality = _quality(sleep, recovery, training, activity)
        relevant_trends = [
            item for item in trends
            if item.window_days == PERIOD_DAYS
            and item.status == Availability.AVAILABLE
        ]
        key_changes = _key_changes(relevant_trends)
        limitations = []
        if sleep.available_days < MIN_COMPARISON_DAYS:
            limitations.append("本周睡眠有效天数不足 4 天。")
        if recovery.hrv_median_ms is None:
            limitations.append("本周缺少可比较的设备级 HRV 趋势。")
        if feedback_facts.response_count == 0:
            limitations.append("本周尚未记录主观训练反馈。")
        coverage = _training_coverage(raw, period_start)
        if coverage["coverage_status"] != "COMPLETE":
            limitations.append(
                "训练清单尚未完整核实："
                f"已核实 {coverage['record_days']} 天，未知 {coverage['unknown_days']} 天。"
            )
        facts = WeeklyFacts(
            sleep=sleep,
            recovery=recovery,
            training=training,
            activity=activity,
            feedback=feedback_facts,
        )
        inferences = WeeklyInferences(
            trends=relevant_trends,
            events=events,
            key_changes=key_changes,
            limitations=limitations,
        )
        report_context = _report_context(raw, period_start, coverage)
        return WeeklyProfile(
            analysis_run_id=analysis_run_id,
            user_id=raw.user_id,
            period_start=period_start,
            period_end=raw.day,
            data_quality=quality,
            report_context=report_context,
            facts=facts,
            inferences=inferences,
            actions=WeeklyActions(
                recommendations=_recommend(
                    facts,
                    events,
                    getattr(raw, "training_preferences", None),
                    coverage,
                )
            ),
            evidence_refs=evidence_refs or [],
            open_health_period_summary=period_summary(
                open_health_insights, period_start, raw.day
            ),
            open_health_coverage=coverage_summary(
                open_health_insights, period_days=PERIOD_DAYS
            ),
        )


def _sleep_facts(
    raw: RawDailyProfile,
    period_start: date,
    previous_start: date,
) -> WeeklySleepFacts:
    current = [
        float(item["sleep_duration"])
        for day, item in raw.sleep_by_day.items()
        if period_start <= day <= raw.day and item.get("sleep_duration") is not None
    ]
    previous = [
        float(item["sleep_duration"])
        for day, item in raw.sleep_by_day.items()
        if previous_start <= day < period_start and item.get("sleep_duration") is not None
    ]
    bedtimes = [
        value for day, item in raw.sleep_by_day.items()
        if period_start <= day <= raw.day
        and (value := _clock_minutes(item.get("bedtime"))) is not None
    ]
    current_average = _mean(current)
    previous_average = _mean(previous)
    comparable = (
        len(current) >= MIN_COMPARISON_DAYS
        and len(previous) >= MIN_COMPARISON_DAYS
    )
    regularity = (
        median(abs(value - median(bedtimes)) for value in bedtimes)
        if len(bedtimes) >= 3 else None
    )
    return WeeklySleepFacts(
        available_days=len(current),
        previous_available_days=len(previous),
        average_minutes=_rounded(current_average),
        median_minutes=_rounded(float(median(current))) if current else None,
        previous_average_minutes=_rounded(previous_average) if comparable else None,
        change_percent=_rounded(_percent_change(current_average, previous_average))
        if comparable else None,
        bedtime_regularity_minutes=(
            _rounded(float(regularity)) if regularity is not None else None
        ),
    )


def _recovery_facts(
    raw: RawDailyProfile, trends: list[TrendFeature]
) -> WeeklyRecoveryFacts:
    hrv = _best_weekly_trend(trends, ("sleep_hrv", "hrv_sdnn", "hrv_rmssd"))
    rhr = _best_weekly_trend(trends, ("sleep_rhr", "resting_hr"))
    sleep_hrv_device_id, sleep_hrv_daily = _weekly_sleep_hrv_daily(
        raw, hrv.device_id if hrv and hrv.metric == "sleep_hrv" else None
    )
    return WeeklyRecoveryFacts(
        hrv_available_days=hrv.current_distinct_days if hrv else 0,
        hrv_metric=hrv.metric if hrv else None,
        hrv_metric_label=hrv.metric_label if hrv else None,
        hrv_device_id=hrv.device_id if hrv else None,
        hrv_median_ms=hrv.current_median if hrv else None,
        hrv_previous_median_ms=hrv.previous_median if hrv else None,
        hrv_change_percent=hrv.change_percent if hrv else None,
        sleep_hrv_device_id=sleep_hrv_device_id,
        sleep_hrv_daily=sleep_hrv_daily,
        rhr_available_days=rhr.current_distinct_days if rhr else 0,
        rhr_metric=rhr.metric if rhr else None,
        rhr_median_bpm=rhr.current_median if rhr else None,
        rhr_previous_median_bpm=rhr.previous_median if rhr else None,
        rhr_change_percent=rhr.change_percent if rhr else None,
    )


def _weekly_sleep_hrv_daily(
    raw: RawDailyProfile, preferred_device_id: str | None
) -> tuple[str | None, list[DailySleepHrvPoint]]:
    period_start = raw.day - timedelta(days=PERIOD_DAYS - 1)
    streams = defaultdict(list)
    for point in raw.series.get("sleep_hrv", []):
        if period_start <= point.day <= raw.day and point.value > 0:
            streams[(point.source, point.source_scope, point.device_id)].append(point)
    if not streams:
        return None, []
    (_, _, device_id), points = max(
        streams.items(),
        key=lambda item: (
            int(_is_vendor_fused_stream(item[0])),
            int(item[0][2] == preferred_device_id and preferred_device_id is not None),
            len({point.day for point in item[1]}),
            len(item[1]),
        ),
    )
    by_day = defaultdict(list)
    for point in points:
        by_day[point.day].append(point.value)
    return device_id, [
        DailySleepHrvPoint(
            date=day,
            value_ms=round(float(median(values)), 1),
            sample_count=len(values),
        )
        for day, values in sorted(by_day.items())
    ]


def _training_facts(
    raw: RawDailyProfile,
    period_start: date,
    previous_start: date,
) -> WeeklyTrainingFacts:
    coverage = _training_coverage(raw, period_start)
    covered_days = coverage.get("covered_days")
    current_records = [
        item for day, item in raw.training_by_day.items()
        if period_start <= day <= raw.day
    ]
    previous_records = [
        item for day, item in raw.training_by_day.items()
        if previous_start <= day < period_start
    ]
    workouts = [
        item for item in raw.workouts
        if isinstance(item.get("local_day"), date)
        and period_start <= item["local_day"] <= raw.day
    ]
    record_days = coverage["record_days"]
    unknown_days = coverage["unknown_days"]
    complete = coverage["coverage_status"] == "COMPLETE" and unknown_days == 0
    training_days_from_records = {
        day for day, item in raw.training_by_day.items()
        if period_start <= day <= raw.day
        and (
            int(item.get("workout_count", 0) or 0) > 0
            or int(item.get("total_duration", 0) or 0) > 0
        )
    }
    training_days_from_workouts = {
        item["local_day"] for item in workouts if isinstance(item.get("local_day"), date)
    }
    training_days = len(training_days_from_records | training_days_from_workouts)
    if covered_days is not None:
        confirmed_training_days = {
            day for day in (training_days_from_records | training_days_from_workouts)
            if day in covered_days
        }
        rest_days = len(covered_days - confirmed_training_days)
    elif coverage["coverage_status"] == "COMPLETE" and unknown_days == 0:
        rest_days = max(record_days - min(training_days, record_days), 0)
    else:
        rest_days = None

    modes: Counter[str] = Counter()
    aerobic_minutes = 0
    strength_sessions = 0
    for workout in workouts:
        data = workout.get("data", {}) or {}
        modes[str(data.get("sport_mode_label") or "未知运动")] += 1
        if data.get("training_family") == "aerobic":
            aerobic_minutes += int(data.get("duration", 0) or 0)
        if data.get("training_family") == "strength":
            strength_sessions += 1

    record_workout_count = sum(
        int(item.get("workout_count", 0) or 0) for item in current_records
    )
    workout_count = (
        record_workout_count
        if current_records
        else len(workouts) if workouts else 0
    )
    duration = _optional_sum(current_records, "total_duration", int)
    current_load = _optional_sum(current_records, "total_load", float)
    previous_load = _optional_sum(previous_records, "total_load", float)
    details_known = bool(workouts) or bool(current_records and workout_count == 0)
    aerobic_value = aerobic_minutes if details_known else None
    strength_value = strength_sessions if details_known else None
    comparable_load = (
        current_load is not None
        and previous_load not in (None, 0)
        and len(current_records) >= MIN_COMPARISON_DAYS
        and len(previous_records) >= MIN_COMPARISON_DAYS
        and coverage.get("current_complete", False)
        and coverage.get("previous_complete", False)
    )
    values = dict(
        workout_count=workout_count,
        training_days=training_days,
        rest_days=rest_days,
        duration_minutes=duration,
        vendor_load=round(current_load, 1) if current_load is not None else None,
        previous_vendor_load=(
            round(previous_load, 1) if comparable_load else None
        ),
        load_change_percent=(
            round(_percent_change(current_load, previous_load), 1)
            if comparable_load else None
        ),
        aerobic_minutes=aerobic_value,
        strength_sessions=strength_value,
        sport_mode_counts=dict(sorted(modes.items())),
        record_days=record_days,
        unknown_days=unknown_days,
        coverage_status=coverage["coverage_status"],
        totals_are_partial=coverage["totals_are_partial"],
    )
    return WeeklyTrainingFacts(**values)


def _activity_facts(
    raw: RawDailyProfile,
    period_start: date,
    previous_start: date,
) -> WeeklyActivityFacts:
    current = [
        item for day, item in raw.activity_by_day.items()
        if period_start <= day <= raw.day
    ]
    previous = [
        item for day, item in raw.activity_by_day.items()
        if previous_start <= day < period_start
    ]
    steps = [float(item["steps"]) for item in current if item.get("steps") is not None]
    prior_steps = [float(item["steps"]) for item in previous if item.get("steps") is not None]
    average = _mean(steps)
    previous_average = _mean(prior_steps)
    comparable = len(steps) >= MIN_COMPARISON_DAYS and len(prior_steps) >= MIN_COMPARISON_DAYS
    active_values = [
        int(item["active_minutes"])
        for item in current if item.get("active_minutes") is not None
    ]
    return WeeklyActivityFacts(
        available_days=len(steps),
        previous_available_days=len(prior_steps),
        total_steps=int(sum(steps)) if steps else None,
        average_steps=_rounded(average),
        previous_average_steps=_rounded(previous_average) if comparable else None,
        steps_change_percent=(
            _rounded(_percent_change(average, previous_average))
            if comparable else None
        ),
        active_minutes=sum(active_values) if active_values else None,
    )


def _feedback_facts(feedback: list[dict]) -> WeeklyFeedbackFacts:
    return WeeklyFeedbackFacts(
        response_count=len(feedback),
        average_session_rpe=_average(feedback, "session_rpe"),
        average_physical_fatigue=_average(feedback, "physical_fatigue"),
        average_mental_state=_average(feedback, "mental_state"),
        average_muscle_soreness=_average(feedback, "muscle_soreness"),
    )


def _quality(
    sleep: WeeklySleepFacts,
    recovery: WeeklyRecoveryFacts,
    training: WeeklyTrainingFacts,
    activity: WeeklyActivityFacts,
) -> WeeklyDataQuality:
    hrv_days = recovery.hrv_available_days
    sufficient_signals = int(sleep.available_days >= MIN_COMPARISON_DAYS) + int(hrv_days >= MIN_COMPARISON_DAYS)
    if sufficient_signals == 2:
        status = QualityStatus.SUFFICIENT
        confidence = ConfidenceBand.HIGH if sleep.available_days >= 6 else ConfidenceBand.MODERATE
    elif sufficient_signals == 1:
        status = QualityStatus.PARTIAL
        confidence = ConfidenceBand.LOW
    else:
        status = QualityStatus.INSUFFICIENT
        confidence = ConfidenceBand.NONE
    return WeeklyDataQuality(
        status=status,
        status_label=QUALITY_LABELS[status.value],
        sleep_days=sleep.available_days,
        hrv_days=hrv_days,
        activity_days=activity.available_days,
        training_days=training.training_days or 0,
        confidence=confidence,
        confidence_label=CONFIDENCE_LABELS[confidence.value],
    )


def _key_changes(trends: list[TrendFeature]) -> list[str]:
    priority = {
        "sleep_duration": 1,
        "hrv_rmssd": 2,
        "sleep_hrv": 2,
        "hrv_sdnn": 2,
        "sleep_rhr": 3,
        "resting_hr": 3,
        "training_load": 4,
        "steps": 5,
    }
    selected = [
        item for item in trends
        if item.metric in priority
        and item.direction != TrendDirection.STABLE
        and item.change_percent is not None
    ]
    selected.sort(key=lambda item: (priority[item.metric], item.device_id or ""))
    return [
        f"{item.metric_label}较前一周{item.direction_label} {abs(item.change_percent):.1f}%。"
        for item in selected[:6]
    ]


def _recommend(
    facts: WeeklyFacts,
    events: list[HealthEvent],
    training_preferences=None,
    coverage: dict | None = None,
) -> list[WeeklyRecommendation]:
    """Select evidence-backed actions without treating targets as a deficit ledger."""
    active_events = {
        item.type for item in events if getattr(item, "lifecycle", None) != "RESOLVED"
    }
    coverage = coverage or {"coverage_status": "UNKNOWN", "unknown_days": PERIOD_DAYS}
    complete = coverage.get("coverage_status") == "COMPLETE" and not coverage.get("unknown_days")
    output: list[WeeklyRecommendation] = []

    preferences = training_preferences
    pain_present = bool(
        preferences is not None
        and getattr(preferences, "pain_or_injury_status", None) == "PRESENT"
    )
    recovery_events = active_events & {"RECOVERY_SUPPRESSED", "HRV_DROP", "RHR_ELEVATED"}
    if pain_present:
        output.append(WeeklyRecommendation(
            priority=1,
            code="RESPECT_PAIN_OR_INJURY",
            title="遵守疼痛或伤病限制",
            action="本周暂停会诱发疼痛的训练；疼痛持续、加重或影响日常活动时寻求专业评估。",
            reasons=["训练偏好中已记录疼痛或伤病状态。"],
        ))
    elif recovery_events:
        output.append(WeeklyRecommendation(
            priority=1,
            code="PRIORITIZE_RECOVERY",
            title="优先恢复",
            action="下周先降低高强度训练密度，并保留至少 1 个完整休息日。",
            reasons=["本周存在持续恢复相关事件。"],
        ))

    if facts.sleep.average_minutes is not None and facts.sleep.average_minutes < 420:
        output.append(WeeklyRecommendation(
            priority=len(output) + 1,
            code="IMPROVE_SLEEP_DURATION",
            title="补足睡眠",
            action="下周优先把平均睡眠恢复到每晚至少 7 小时。",
            reasons=[f"本周平均睡眠 {facts.sleep.average_minutes:.0f} 分钟。"],
        ))
    if (
        facts.feedback.average_session_rpe is not None
        and facts.feedback.average_session_rpe >= 8
    ):
        output.append(WeeklyRecommendation(
            priority=len(output) + 1,
            code="REDUCE_SUBJECTIVE_LOAD",
            title="降低主观训练压力",
            action="下周避免连续安排主观用力程度 8 分以上的训练。",
            reasons=[f"本周平均训练 RPE 为 {facts.feedback.average_session_rpe:.1f}。"],
        ))

    # A partial/unknown calendar cannot justify catch-up volume or a normal-plan claim.
    if not complete:
        if output:
            return output[:3]
        return [WeeklyRecommendation(
            priority=1,
            code="WEEKLY_INSUFFICIENT_DATA",
            title="先补齐周期记录",
            action="先完成后续同步，待 7 天训练覆盖明确后再评估训练量变化。",
            reasons=[
                f"本周训练历史已记录 {coverage.get('record_days', 0)} 天，"
                f"仍有 {coverage.get('unknown_days', PERIOD_DAYS)} 天未知。"
            ],
        )]

    # User-specific strength cadence is actionable only when its calendar is complete;
    # generic aerobic/strength quotas are intentionally not used as catch-up goals.
    if (
        preferences is not None
        and getattr(preferences, "strength_required", True)
        and getattr(preferences, "weekly_strength_target", None) is not None
        and facts.training.strength_sessions is not None
        and facts.training.strength_sessions < preferences.weekly_strength_target
        and not (
            recovery_events
            or pain_present
            or active_events & {"TRAINING_LOAD_SPIKE", "TRAINING_GAP", "SLEEP_DEFICIT"}
        )
    ):
        gap = preferences.weekly_strength_target - facts.training.strength_sessions
        output.append(WeeklyRecommendation(
            priority=len(output) + 1,
            code="MAINTAIN_STRENGTH_RHYTHM",
            title="维持力量训练节奏",
            action=f"在恢复允许时，安排 {gap} 次符合当前偏好的力量训练，不做补偿性堆量。",
            reasons=[
                f"完整 7 天记录显示本周力量训练 {facts.training.strength_sessions} 次，"
                f"当前个人目标为 {preferences.weekly_strength_target} 次。"
            ],
        ))
    if not output:
        output.append(WeeklyRecommendation(
            priority=1,
            code="MAINTAIN_PLAN",
            title="保持当前节奏",
            action="下周保持当前训练与恢复安排，不额外增加高强度负荷。",
            reasons=["完整周期内没有触发需要优先调整的确定性规则。"],
        ))
    return output[:3]


def _best_weekly_trend(
    trends: list[TrendFeature],
    metrics: tuple[str, ...],
) -> TrendFeature | None:
    candidates = [
        item for item in trends
        if item.window_days == PERIOD_DAYS
        and item.metric in metrics
        and item.status == Availability.AVAILABLE
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            -metrics.index(item.metric),
            int(_is_vendor_fused_stream((
                item.source, item.source_scope, item.device_id
            ))),
            item.current_distinct_days,
            item.coverage_ratio,
            item.device_id or "",
        ),
    )


def _is_vendor_fused_stream(stream: tuple[str, str, str | None]) -> bool:
    source, scope, device_id = stream
    return (
        source == "zepp"
        and device_id is None
        and scope in {"user_fused", "unknown"}
    )


def _average(items: list[dict], field: str) -> float | None:
    values = [float(item[field]) for item in items if item.get(field) is not None]
    return round(sum(values) / len(values), 1) if values else None


def _percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return 100 * (current - previous) / abs(previous)


def _clock_minutes(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        hours, minutes, *_ = value.split(":")
        result = int(hours) * 60 + int(minutes)
    else:
        result = value.hour * 60 + value.minute
    return result - 1440 if result >= 12 * 60 else result


def _mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _optional_sum(items: list[dict], field: str, converter):
    values = [converter(item[field]) for item in items if item.get(field) is not None]
    return sum(values) if values else None


def _training_coverage(raw: RawDailyProfile, period_start: date) -> dict:
    records = [
        day for day in raw.training_by_day
        if period_start <= day <= raw.day
    ]
    context = getattr(raw, "training_history_coverage", None) or {}
    if not isinstance(context, dict):
        context = {}
    nested = context.get("current_7d")
    if isinstance(nested, dict):
        values = {**context, **nested}
    else:
        values = context
    explicit_record_days = values.get("record_days")
    status = str(values.get("coverage_status") or values.get("status") or "").upper()
    verified_days = values.get("verified_days")
    previous_start = period_start - timedelta(days=PERIOD_DAYS)
    previous_days = {
        previous_start + timedelta(days=index) for index in range(PERIOD_DAYS)
    }
    current_days = {
        period_start + timedelta(days=index) for index in range(PERIOD_DAYS)
    }
    verified_all = set()
    verified_current = set()
    for item in verified_days or []:
        if isinstance(item, date):
            candidate = item
        elif isinstance(item, str):
            try:
                candidate = date.fromisoformat(item[:10])
            except ValueError:
                continue
        else:
            continue
        verified_all.add(candidate)
        if period_start <= candidate <= raw.day:
            verified_current.add(candidate)
    if verified_days is not None:
        record_days = len(verified_current)
        status = (
            "COMPLETE" if current_days <= verified_all
            else "PARTIAL" if verified_current
            else "UNKNOWN"
        )
    elif explicit_record_days is not None:
        record_days = int(explicit_record_days)
    elif status == "UNKNOWN":
        record_days = 0
    else:
        record_days = len(records)
    explicit_unknown_days = values.get("unknown_days")
    unknown_days = (
        int(explicit_unknown_days)
        if explicit_unknown_days is not None
        else max(PERIOD_DAYS - record_days, 0)
    )
    if status not in {"COMPLETE", "PARTIAL", "UNKNOWN"}:
        status = "PARTIAL" if records else "UNKNOWN"
    record_days = max(0, min(record_days, PERIOD_DAYS))
    unknown_days = max(0, min(unknown_days, PERIOD_DAYS - record_days))
    if status == "COMPLETE":
        record_days, unknown_days = PERIOD_DAYS, 0
    return {
        "record_days": record_days,
        "unknown_days": unknown_days,
        "coverage_status": status,
        "totals_are_partial": bool(
            values.get("totals_are_partial", status != "COMPLETE")
        ),
        "covered_days": verified_current if verified_days is not None else None,
        "current_complete": (
            verified_days is not None and current_days <= verified_all
        ),
        "previous_complete": (
            verified_days is not None and previous_days <= verified_all
        ),
    }


def _report_context(raw: RawDailyProfile, period_start: date, coverage: dict) -> dict:
    context = dict(getattr(raw, "report_context", None) or {})
    context.setdefault("period_start", period_start.isoformat())
    context.setdefault("period_end", raw.day.isoformat())
    context.setdefault(
        "training_coverage",
        {key: value for key, value in coverage.items() if key != "covered_days"},
    )
    return context
