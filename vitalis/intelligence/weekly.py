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
        period_start = raw.day - timedelta(days=6)
        previous_start = period_start - timedelta(days=7)
        sleep = _sleep_facts(raw, period_start, previous_start)
        recovery = _recovery_facts(raw, trends)
        training = _training_facts(raw, period_start, previous_start)
        activity = _activity_facts(raw, period_start, previous_start)
        feedback_facts = _feedback_facts(feedback or [])
        quality = _quality(sleep, recovery, training, activity)
        relevant_trends = [
            item for item in trends
            if item.window_days == 7 and item.status == Availability.AVAILABLE
        ]
        key_changes = _key_changes(relevant_trends)
        limitations = []
        if sleep.available_days < 4:
            limitations.append("本周睡眠有效天数不足 4 天。")
        if recovery.hrv_median_ms is None:
            limitations.append("本周缺少可比较的设备级 HRV 趋势。")
        if feedback_facts.response_count == 0:
            limitations.append("本周尚未记录主观训练反馈。")
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
        return WeeklyProfile(
            analysis_run_id=analysis_run_id,
            user_id=raw.user_id,
            period_start=period_start,
            period_end=raw.day,
            data_quality=quality,
            facts=facts,
            inferences=inferences,
            actions=WeeklyActions(
                recommendations=_recommend(facts, events)
            ),
            evidence_refs=evidence_refs or [],
            open_health_period_summary=period_summary(
                open_health_insights, period_start, raw.day
            ),
            open_health_coverage=coverage_summary(
                open_health_insights, period_days=7
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
    current_average = sum(current) / len(current) if current else None
    previous_average = sum(previous) / len(previous) if previous else None
    change = _percent_change(current_average, previous_average)
    regularity = (
        median(abs(value - median(bedtimes)) for value in bedtimes)
        if len(bedtimes) >= 3 else None
    )
    return WeeklySleepFacts(
        available_days=len(current),
        average_minutes=round(current_average, 1) if current_average is not None else None,
        median_minutes=round(float(median(current)), 1) if current else None,
        previous_average_minutes=(
            round(previous_average, 1) if previous_average is not None else None
        ),
        change_percent=round(change, 1) if change is not None else None,
        bedtime_regularity_minutes=round(float(regularity), 1) if regularity is not None else None,
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
    period_start = raw.day - timedelta(days=6)
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
    training_days = {item["local_day"] for item in workouts}
    modes: Counter[str] = Counter()
    aerobic_minutes = 0
    strength_sessions = 0
    for workout in workouts:
        data = workout.get("data", {})
        modes[str(data.get("sport_mode_label") or "未知运动")] += 1
        if data.get("training_family") == "aerobic":
            aerobic_minutes += int(data.get("duration", 0) or 0)
        if data.get("training_family") == "strength":
            strength_sessions += 1
    current_load = sum(float(item.get("total_load", 0) or 0) for item in current_records)
    previous_load = sum(float(item.get("total_load", 0) or 0) for item in previous_records)
    return WeeklyTrainingFacts(
        workout_count=len(workouts),
        training_days=len(training_days),
        rest_days=7 - len(training_days),
        duration_minutes=sum(int(item.get("total_duration", 0) or 0) for item in current_records),
        vendor_load=round(current_load, 1),
        previous_vendor_load=round(previous_load, 1) if previous_records else None,
        load_change_percent=(
            round(_percent_change(current_load, previous_load), 1)
            if previous_records and previous_load else None
        ),
        aerobic_minutes=aerobic_minutes,
        strength_sessions=strength_sessions,
        sport_mode_counts=dict(sorted(modes.items())),
    )


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
    average = sum(steps) / len(steps) if steps else None
    previous_average = sum(prior_steps) / len(prior_steps) if prior_steps else None
    return WeeklyActivityFacts(
        available_days=len(steps),
        total_steps=int(sum(steps)) if steps else None,
        average_steps=round(average, 1) if average is not None else None,
        previous_average_steps=(
            round(previous_average, 1) if previous_average is not None else None
        ),
        steps_change_percent=(
            round(_percent_change(average, previous_average), 1)
            if average is not None and previous_average else None
        ),
        active_minutes=sum(int(item.get("active_minutes", 0) or 0) for item in current),
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
    sufficient_signals = int(sleep.available_days >= 4) + int(hrv_days >= 4)
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
        training_days=training.training_days,
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


def _recommend(facts: WeeklyFacts, events: list[HealthEvent]) -> list[WeeklyRecommendation]:
    recommendations: list[WeeklyRecommendation] = []
    event_types = {item.type for item in events}
    if event_types & {"RECOVERY_SUPPRESSED", "HRV_DROP", "RHR_ELEVATED"}:
        recommendations.append(WeeklyRecommendation(
            priority=1,
            code="PRIORITIZE_RECOVERY",
            title="优先恢复",
            action="下周先降低高强度训练密度，并保留至少 1 个完整休息日。",
            reasons=["本周存在持续恢复相关事件。"],
        ))
    if facts.sleep.average_minutes is not None and facts.sleep.average_minutes < 420:
        recommendations.append(WeeklyRecommendation(
            priority=len(recommendations) + 1,
            code="IMPROVE_SLEEP_DURATION",
            title="补足睡眠",
            action="下周优先把平均睡眠恢复到每晚至少 7 小时。",
            reasons=[f"本周平均睡眠 {facts.sleep.average_minutes:.0f} 分钟。"],
        ))
    if facts.training.aerobic_minutes < 150:
        gap = 150 - facts.training.aerobic_minutes
        recommendations.append(WeeklyRecommendation(
            priority=len(recommendations) + 1,
            code="ADD_AEROBIC_VOLUME",
            title="补充基础有氧",
            action=f"在恢复允许时，下周补充约 {gap} 分钟低到中等强度有氧。",
            reasons=[f"本周记录到 {facts.training.aerobic_minutes} 分钟有氧训练。"],
        ))
    if facts.training.strength_sessions < 2:
        gap = 2 - facts.training.strength_sessions
        recommendations.append(WeeklyRecommendation(
            priority=len(recommendations) + 1,
            code="ADD_STRENGTH_SESSIONS",
            title="补足力量训练",
            action=f"下周安排 {gap} 次全身力量训练，且两次之间至少间隔 1 天。",
            reasons=[f"本周完成 {facts.training.strength_sessions} 次力量训练。"],
        ))
    if (
        facts.feedback.average_session_rpe is not None
        and facts.feedback.average_session_rpe >= 8
    ):
        recommendations.append(WeeklyRecommendation(
            priority=len(recommendations) + 1,
            code="REDUCE_SUBJECTIVE_LOAD",
            title="降低主观训练压力",
            action="下周避免连续安排主观用力程度 8 分以上的训练。",
            reasons=[f"本周平均训练 RPE 为 {facts.feedback.average_session_rpe:.1f}。"],
        ))
    if not recommendations:
        recommendations.append(WeeklyRecommendation(
            priority=1,
            code="MAINTAIN_PLAN",
            title="保持当前节奏",
            action="下周保持当前训练与恢复安排，不额外增加高强度负荷。",
            reasons=["本周没有触发需要优先调整的确定性规则。"],
        ))
    return recommendations


def _best_weekly_trend(
    trends: list[TrendFeature],
    metrics: tuple[str, ...],
) -> TrendFeature | None:
    candidates = [
        item for item in trends
        if item.window_days == 7
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
