"""Direct 28-day profile computation from normalized history."""

from collections import Counter
from datetime import date, timedelta
from statistics import median

from .contracts import (
    Availability,
    ConfidenceBand,
    HealthEvent,
    MonthlyActions,
    MonthlyActivityFacts,
    MonthlyDataQuality,
    MonthlyFacts,
    MonthlyFeedbackFacts,
    MonthlyInferences,
    MonthlyProfile,
    MonthlyRecoveryFacts,
    MonthlyRecoveryStreamFacts,
    MonthlySleepFacts,
    MonthlyTrainingFacts,
    PersonalAssociation,
    QualityStatus,
    TrendDirection,
    TrendFeature,
    WeeklyRecommendation,
)
from .localization import CONFIDENCE_LABELS, QUALITY_LABELS
from .profile import RawDailyProfile
from .trend import METRIC_LABELS, stream_daily_values


PERIOD_DAYS = 28
RECOVERY_METRICS = ("hrv_rmssd", "hrv_sdnn", "sleep_hrv", "resting_hr", "sleep_rhr")


class MonthlyProfileEngine:
    def build(
        self,
        analysis_run_id: str,
        raw: RawDailyProfile,
        trends: list[TrendFeature],
        events: list[HealthEvent],
        associations: list[PersonalAssociation],
        feedback: list[dict] | None = None,
        evidence_refs: list | None = None,
    ) -> MonthlyProfile:
        period_start = raw.day - timedelta(days=PERIOD_DAYS - 1)
        previous_start = period_start - timedelta(days=PERIOD_DAYS)
        sleep = _sleep_facts(raw, period_start, previous_start)
        recovery = _recovery_facts(raw, period_start, previous_start)
        training = _training_facts(raw, period_start, previous_start)
        activity = _activity_facts(raw, period_start, previous_start)
        feedback_facts = _feedback_facts(feedback or [])
        facts = MonthlyFacts(
            sleep=sleep,
            recovery=recovery,
            training=training,
            activity=activity,
            feedback=feedback_facts,
        )
        quality = _quality(facts)
        relevant_trends = [
            item for item in trends
            if item.window_days in {28, 90} and item.status == Availability.AVAILABLE
        ]
        supported_associations = [
            item for item in associations
            if item.status == Availability.AVAILABLE
            and item.confidence in {ConfidenceBand.MODERATE, ConfidenceBand.HIGH}
        ]
        limitations = []
        if sleep.available_days < 14:
            limitations.append("近 28 天睡眠有效天数不足 14 天。")
        if not recovery.streams:
            limitations.append("近 28 天缺少可比较的设备级 HRV 或静息心率流。")
        if feedback_facts.response_count == 0:
            limitations.append("近 28 天尚未记录主观反馈。")
        if not supported_associations:
            limitations.append("当前没有中等或较高置信度的个人关联可用于月度解释。")
        return MonthlyProfile(
            analysis_run_id=analysis_run_id,
            user_id=raw.user_id,
            period_start=period_start,
            period_end=raw.day,
            data_quality=quality,
            facts=facts,
            inferences=MonthlyInferences(
                trends=relevant_trends,
                events=events,
                personal_associations=supported_associations[:8],
                key_changes=_key_changes(relevant_trends),
                limitations=limitations,
            ),
            actions=MonthlyActions(recommendations=_recommend(facts, events)),
            evidence_refs=evidence_refs or [],
        )


def _sleep_facts(raw, period_start, previous_start) -> MonthlySleepFacts:
    current = _record_values(raw.sleep_by_day, "sleep_duration", period_start, raw.day)
    previous = _record_values(
        raw.sleep_by_day, "sleep_duration", previous_start, period_start - timedelta(days=1)
    )
    bedtimes = [
        value for day, item in raw.sleep_by_day.items()
        if period_start <= day <= raw.day
        and (value := _clock_minutes(item.get("bedtime"))) is not None
    ]
    current_average = _mean(current)
    previous_average = _mean(previous)
    regularity = (
        median(abs(value - median(bedtimes)) for value in bedtimes)
        if len(bedtimes) >= 14 else None
    )
    return MonthlySleepFacts(
        available_days=len(current),
        average_minutes=_rounded(current_average),
        median_minutes=_rounded(float(median(current))) if current else None,
        previous_average_minutes=_rounded(previous_average),
        change_percent=_rounded(_percent_change(current_average, previous_average)),
        bedtime_regularity_minutes=_rounded(float(regularity)) if regularity is not None else None,
    )


def _recovery_facts(raw, period_start, previous_start) -> MonthlyRecoveryFacts:
    output = []
    for metric in RECOVERY_METRICS:
        for (source, scope, device_id, unit), daily in sorted(
            stream_daily_values(raw.series.get(metric, [])).items(),
            key=lambda item: tuple(value or "" for value in item[0]),
        ):
            current = [
                value for day, value in daily.items() if period_start <= day <= raw.day
            ]
            previous = [
                value for day, value in daily.items()
                if previous_start <= day < period_start
            ]
            if not current:
                continue
            current_median = float(median(current))
            previous_median = float(median(previous)) if previous else None
            output.append(MonthlyRecoveryStreamFacts(
                metric=metric,
                metric_label=METRIC_LABELS[metric],
                source=source,
                source_scope=scope,
                device_id=device_id,
                unit=unit,
                available_days=len(current),
                previous_available_days=len(previous),
                median=round(current_median, 3),
                previous_median=(round(previous_median, 3) if previous_median is not None else None),
                change_percent=_rounded(_percent_change(current_median, previous_median)),
            ))
    return MonthlyRecoveryFacts(streams=output)


def _training_facts(raw, period_start, previous_start) -> MonthlyTrainingFacts:
    current_records = [
        item for day, item in raw.training_by_day.items() if period_start <= day <= raw.day
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
    if not current_records:
        return MonthlyTrainingFacts(record_days=0)
    recorded_workout_count = sum(
        int(item.get("workout_count", 0) or 0) for item in current_records
    )
    training_days = {
        item["date"] for item in current_records
        if int(item.get("workout_count", 0) or 0) > 0
        or int(item.get("total_duration", 0) or 0) > 0
    }
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
    details_complete = len(workouts) >= recorded_workout_count
    return MonthlyTrainingFacts(
        record_days=len(current_records),
        workout_count=recorded_workout_count,
        training_days=len(training_days),
        rest_days=PERIOD_DAYS - len(training_days),
        duration_minutes=sum(int(item.get("total_duration", 0) or 0) for item in current_records),
        vendor_load=round(current_load, 1),
        previous_vendor_load=round(previous_load, 1) if previous_records else None,
        load_change_percent=(
            _rounded(_percent_change(current_load, previous_load))
            if previous_records else None
        ),
        aerobic_minutes=aerobic_minutes if details_complete else None,
        strength_sessions=strength_sessions if details_complete else None,
        sport_mode_counts=dict(sorted(modes.items())),
    )


def _activity_facts(raw, period_start, previous_start) -> MonthlyActivityFacts:
    current = _record_values(raw.activity_by_day, "steps", period_start, raw.day)
    previous = _record_values(
        raw.activity_by_day, "steps", previous_start, period_start - timedelta(days=1)
    )
    current_average = _mean(current)
    previous_average = _mean(previous)
    active_minutes = [
        int(item["active_minutes"])
        for day, item in raw.activity_by_day.items()
        if period_start <= day <= raw.day and item.get("active_minutes") is not None
    ]
    return MonthlyActivityFacts(
        available_days=len(current),
        total_steps=int(sum(current)) if current else None,
        average_steps=_rounded(current_average),
        previous_average_steps=_rounded(previous_average),
        steps_change_percent=_rounded(_percent_change(current_average, previous_average)),
        active_minutes=sum(active_minutes) if active_minutes else None,
    )


def _feedback_facts(items: list[dict]) -> MonthlyFeedbackFacts:
    return MonthlyFeedbackFacts(
        response_count=len(items),
        average_session_rpe=_field_average(items, "session_rpe"),
        average_physical_fatigue=_field_average(items, "physical_fatigue"),
        average_mental_state=_field_average(items, "mental_state"),
        average_muscle_soreness=_field_average(items, "muscle_soreness"),
    )


def _quality(facts: MonthlyFacts) -> MonthlyDataQuality:
    hrv_days = max(
        (item.available_days for item in facts.recovery.streams if item.metric.startswith("hrv") or item.metric == "sleep_hrv"),
        default=0,
    )
    sufficient = int(facts.sleep.available_days >= 14) + int(hrv_days >= 14)
    if sufficient == 2:
        status = QualityStatus.SUFFICIENT
        confidence = ConfidenceBand.HIGH if min(facts.sleep.available_days, hrv_days) >= 23 else ConfidenceBand.MODERATE
    elif sufficient == 1:
        status = QualityStatus.PARTIAL
        confidence = ConfidenceBand.LOW
    else:
        status = QualityStatus.INSUFFICIENT
        confidence = ConfidenceBand.NONE
    limitations = []
    if facts.sleep.available_days < 14:
        limitations.append("睡眠有效天数不足 14 天。")
    if hrv_days < 14:
        limitations.append("同一设备 HRV 有效天数不足 14 天。")
    return MonthlyDataQuality(
        status=status,
        status_label=QUALITY_LABELS[status.value],
        sleep_days=facts.sleep.available_days,
        hrv_days=hrv_days,
        activity_days=facts.activity.available_days,
        training_record_days=facts.training.record_days,
        training_days=facts.training.training_days,
        confidence=confidence,
        confidence_label=CONFIDENCE_LABELS[confidence.value],
        limitations=limitations,
    )


def _key_changes(trends: list[TrendFeature]) -> list[str]:
    selected = [
        item for item in trends
        if item.window_days == 28
        and item.direction != TrendDirection.STABLE
        and item.change_percent is not None
    ]
    selected.sort(key=lambda item: (-abs(item.change_percent or 0), item.metric, item.device_id or ""))
    return [
        f"{item.metric_label}较前 28 天{item.direction_label} {abs(item.change_percent):.1f}%。"
        for item in selected[:8]
    ]


def _recommend(facts: MonthlyFacts, events: list[HealthEvent]) -> list[WeeklyRecommendation]:
    output = []
    if facts.sleep.available_days < 14 and facts.training.record_days < 14:
        return [WeeklyRecommendation(
            priority=1,
            code="MONTHLY_INSUFFICIENT_DATA",
            title="周期数据不足",
            action="先完成新数据同步，再形成下一周期的训练与恢复建议。",
            reasons=["近 28 天睡眠和训练记录覆盖均不足 14 天。"],
        )]
    if any(item.type in {"RECOVERY_SUPPRESSED", "HRV_DROP", "RHR_ELEVATED"} for item in events):
        output.append(WeeklyRecommendation(
            priority=1,
            code="MONTHLY_PRIORITIZE_RECOVERY",
            title="先恢复再加量",
            action="下一个 28 天周期先控制连续高负荷训练，并保留每周至少 1 个完整休息日。",
            reasons=["近 28 天存在恢复相关持续事件。"],
        ))
    if facts.sleep.average_minutes is not None and facts.sleep.average_minutes < 420:
        output.append(WeeklyRecommendation(
            priority=len(output) + 1,
            code="MONTHLY_IMPROVE_SLEEP",
            title="提高平均睡眠时长",
            action="下一个周期优先把平均睡眠提高到每晚至少 7 小时。",
            reasons=[f"近 28 天平均睡眠 {facts.sleep.average_minutes:.0f} 分钟。"],
        ))
    if facts.training.aerobic_minutes is not None and facts.training.aerobic_minutes < 600:
        output.append(WeeklyRecommendation(
            priority=len(output) + 1,
            code="MONTHLY_AEROBIC_BALANCE",
            title="补足基础有氧",
            action=f"在恢复允许时，下一个 28 天周期补充约 {600 - facts.training.aerobic_minutes} 分钟低到中等强度有氧。",
            reasons=[f"近 28 天记录到 {facts.training.aerobic_minutes} 分钟有氧训练。"],
        ))
    if facts.training.strength_sessions is not None and facts.training.strength_sessions < 8:
        output.append(WeeklyRecommendation(
            priority=len(output) + 1,
            code="MONTHLY_STRENGTH_BALANCE",
            title="补足力量训练",
            action=f"下一个 28 天周期安排至少 {8 - facts.training.strength_sessions} 次全身力量训练。",
            reasons=[f"近 28 天完成 {facts.training.strength_sessions} 次力量训练。"],
        ))
    if not output:
        output.append(WeeklyRecommendation(
            priority=1,
            code="MONTHLY_MAINTAIN_PLAN",
            title="保持当前周期安排",
            action="下一个 28 天周期保持当前训练与恢复节奏，不额外堆叠高强度负荷。",
            reasons=["本周期没有触发需要优先调整的确定性规则。"],
        ))
    return output


def _record_values(records, field, start, end) -> list[float]:
    return [
        float(item[field]) for day, item in records.items()
        if start <= day <= end and item.get(field) is not None
    ]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return 100 * (current - previous) / abs(previous)


def _field_average(items: list[dict], field: str) -> float | None:
    values = [float(item[field]) for item in items if item.get(field) is not None]
    return _rounded(_mean(values))


def _clock_minutes(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        hours, minutes, *_ = value.split(":")
        result = int(hours) * 60 + int(minutes)
    else:
        result = value.hour * 60 + value.minute
    return result - 1440 if result >= 12 * 60 else result
