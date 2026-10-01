"""Direct 28-day profile computation from normalized history."""

from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from math import ceil
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
    OpenHealthBundle,
)
from .localization import CONFIDENCE_LABELS, QUALITY_LABELS
from .open_health.projection import coverage_summary, period_summary
from .profile import RawDailyProfile
from .period_activity import (
    build_period_activity_metrics,
    comparison_gate,
    period_training_details,
)
from .report_periods import (
    PeriodMode,
    ReportPeriod,
    normalize_period_mode,
    resolve_month_period,
)
from .trend import METRIC_LABELS, TrendEngine, stream_daily_values


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
        open_health_insights: OpenHealthBundle | None = None,
        generated_at=None,
        *,
        period_mode: PeriodMode | str = PeriodMode.CALENDAR,
        mode: PeriodMode | str | None = None,
    ) -> MonthlyProfile:
        selected_mode = normalize_period_mode(mode if mode is not None else period_mode)
        period = resolve_month_period(raw.day, selected_mode)
        period_raw = _period_raw(raw, period)
        period_start = period.start
        previous_start = period.reference_start
        period_days = period.days
        previous_period_days = period.reference_days
        period_trends = _period_trends(period_raw)
        period_events = [
            item for item in events
            if item.start_date <= period.end and item.end_date >= period.start
        ]
        sleep = _sleep_facts(
            period_raw, period_start, previous_start,
            period_days, previous_period_days, selected_mode,
        )
        recovery = _recovery_facts(
            period_raw, period_start, previous_start,
            period_days, previous_period_days, selected_mode,
        )
        training = _training_facts(
            period_raw, period_start, previous_start,
            period_days, previous_period_days,
        )
        activity = _activity_facts(
            period_raw, period_start, previous_start,
            period_days, previous_period_days, selected_mode,
        )
        feedback_facts = _feedback_facts(feedback or [])
        facts = MonthlyFacts(
            sleep=sleep,
            recovery=recovery,
            training=training,
            activity=activity,
            feedback=feedback_facts,
        )
        quality = _quality(facts, period_days, selected_mode)
        relevant_trends = [
            item for item in period_trends
            if selected_mode is PeriodMode.ROLLING
            and item.window_days in {28, 90}
            and item.status == Availability.AVAILABLE
        ]
        supported_associations = [
            item for item in associations
            if item.status == Availability.AVAILABLE
            and item.confidence in {ConfidenceBand.MODERATE, ConfidenceBand.HIGH}
        ]
        limitations = []
        comparison_days = comparison_gate(period_days, selected_mode)
        if sleep.available_days < comparison_days:
            limitations.append(
                f"本期睡眠有效天数不足 {comparison_days}/{period_days} 天。"
            )
        if not recovery.streams:
            limitations.append("本期缺少可比较的设备级 HRV 或静息心率流。")
        if feedback_facts.response_count == 0:
            limitations.append("本期尚未记录主观反馈。")
        if not supported_associations:
            limitations.append("当前没有中等或较高置信度的个人关联可用于月度解释。")
        return MonthlyProfile(
            analysis_run_id=analysis_run_id,
            user_id=period_raw.user_id,
            generated_at=generated_at if generated_at is not None else raw.as_of,
            period_start=period_start,
            period_end=period.end,
            data_quality=quality,
            report_context=_report_context(period_raw, period, raw),
            facts=facts,
            inferences=MonthlyInferences(
                trends=relevant_trends,
                events=period_events,
                personal_associations=supported_associations[:8],
                key_changes=_key_changes(
                    relevant_trends, period_days, selected_mode, facts
                ),
                limitations=limitations,
            ),
            actions=MonthlyActions(recommendations=_recommend(
                facts,
                period_events,
                getattr(period_raw, "training_preferences", None),
                period_days=period_days,
                period_mode=selected_mode,
            )),
            evidence_refs=evidence_refs or [],
            open_health_period_summary=period_summary(
                open_health_insights, period_start, period.end
            ),
            open_health_coverage=coverage_summary(
                open_health_insights, period_days=period.days
            ),
        )


def _period_raw(raw, period: ReportPeriod):
    """Bound monthly calculations at the completed period end."""
    context = dict(getattr(raw, "report_context", None) or {})
    context.update(period.metadata())
    context.update({
        "as_of": raw.as_of.isoformat(),
        "target_date": raw.day.isoformat(),
        "target_day_complete": (
            True if period.mode is PeriodMode.CALENDAR
            else bool(context.get("target_day_complete", True))
        ),
    })
    return replace(raw, day=period.end, report_context=context)


def _period_trends(raw):
    """Recompute trends at the selected period end."""
    return TrendEngine().calculate(raw, windows=(7, 28, 90))


def _sleep_facts(
    raw,
    period_start,
    previous_start,
    period_days=PERIOD_DAYS,
    previous_period_days=PERIOD_DAYS,
    period_mode: PeriodMode | str = PeriodMode.CALENDAR,
) -> MonthlySleepFacts:
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
    current_gate = comparison_gate(period_days, period_mode)
    previous_gate = comparison_gate(previous_period_days, period_mode)
    regularity = (
        median(abs(value - median(bedtimes)) for value in bedtimes)
        if len(bedtimes) >= current_gate else None
    )
    return MonthlySleepFacts(
        available_days=len(current),
        previous_available_days=len(previous),
        average_minutes=_rounded(current_average),
        median_minutes=_rounded(float(median(current))) if current else None,
        previous_average_minutes=_rounded(previous_average),
        change_percent=(
            _rounded(_percent_change(current_average, previous_average))
            if len(current) >= current_gate and len(previous) >= previous_gate
            else None
        ),
        bedtime_regularity_minutes=_rounded(float(regularity)) if regularity is not None else None,
    )


def _recovery_facts(
    raw,
    period_start,
    previous_start,
    period_days=PERIOD_DAYS,
    previous_period_days=PERIOD_DAYS,
    period_mode: PeriodMode | str = PeriodMode.CALENDAR,
) -> MonthlyRecoveryFacts:
    current_gate = comparison_gate(period_days, period_mode)
    previous_gate = comparison_gate(previous_period_days, period_mode)
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
                change_percent=(
                    _rounded(_percent_change(current_median, previous_median))
                    if len(current) >= current_gate and len(previous) >= previous_gate
                    else None
                ),
            ))
    return MonthlyRecoveryFacts(streams=output)


def _training_facts(
    raw,
    period_start,
    previous_start,
    period_days=PERIOD_DAYS,
    previous_period_days=PERIOD_DAYS,
) -> MonthlyTrainingFacts:
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
    coverage = _monthly_training_coverage(
        raw, period_start, period_days, previous_start, previous_period_days
    )
    recorded_workout_count = (
        max(
            sum(int(item.get("workout_count", 0) or 0) for item in current_records),
            len(workouts),
        )
        if current_records or workouts else None
    )
    training_days_from_records = {
        item.get("date")
        for item in current_records
        if isinstance(item.get("date"), date)
        and (
            int(item.get("workout_count", 0) or 0) > 0
            or int(item.get("total_duration", 0) or 0) > 0
        )
    }
    training_days_from_workouts = {
        item["local_day"] for item in workouts
        if isinstance(item.get("local_day"), date)
    }
    training_days = training_days_from_records | training_days_from_workouts
    covered_days = coverage.get("covered_days")
    if covered_days is not None:
        rest_days = len(covered_days - training_days)
    elif coverage["coverage_status"] == "COMPLETE" and coverage["unknown_days"] == 0:
        rest_days = max(period_days - len(training_days), 0)
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
    current_load = _optional_sum(current_records, "total_load", float)
    previous_load = _optional_sum(previous_records, "total_load", float)
    details_complete = bool(workouts) or bool(
        current_records and recorded_workout_count == 0
    )
    details = period_training_details(raw, period_start, raw.day)
    if not current_records and not workouts:
        details_complete = False
    return MonthlyTrainingFacts(
        record_days=coverage["record_days"],
        unknown_days=coverage["unknown_days"],
        coverage_status=coverage["coverage_status"],
        totals_are_partial=coverage["totals_are_partial"],
        workout_count=recorded_workout_count,
        training_days=len(training_days) if current_records or workouts else None,
        rest_days=rest_days,
        duration_minutes=_optional_sum(current_records, "total_duration", int),
        vendor_load=round(current_load, 1) if current_load is not None else None,
        previous_vendor_load=round(previous_load, 1) if previous_load is not None else None,
        load_change_percent=(
            _rounded(_percent_change(current_load, previous_load))
            if current_load is not None
            and previous_load not in (None, 0)
            and len(current_records) >= comparison_gate(period_days)
            and len(previous_records) >= comparison_gate(previous_period_days)
            and coverage["current_complete"]
            and coverage["previous_complete"]
            else None
        ),
        aerobic_minutes=aerobic_minutes if details_complete else None,
        strength_sessions=strength_sessions if details_complete else None,
        sport_mode_counts=dict(sorted(modes.items())),
        **details,
    )


def _activity_facts(
    raw,
    period_start,
    previous_start,
    period_days=PERIOD_DAYS,
    previous_period_days=PERIOD_DAYS,
    period_mode: PeriodMode | str = PeriodMode.CALENDAR,
) -> MonthlyActivityFacts:
    metrics = build_period_activity_metrics(
        raw, period_start, period_days, previous_start, period_mode=period_mode
    )
    steps_metric = next((item for item in metrics if item.metric == "steps"), None)
    active_metric = next((item for item in metrics if item.metric == "active_minutes"), None)
    if steps_metric is None and not (getattr(raw, "series", {}) or {}):
        current = _record_values(raw.activity_by_day, "steps", period_start, raw.day)
        previous = _record_values(
            raw.activity_by_day, "steps", previous_start, period_start - timedelta(days=1)
        )
        step_values = {
            "available_days": len(current),
            "previous_available_days": len(previous),
            "total_steps": int(sum(current)) if current else None,
            "average_steps": _rounded(_mean(current)),
            "previous_average_steps": _rounded(_mean(previous)),
            "steps_change_percent": (
                _rounded(_percent_change(_mean(current), _mean(previous)))
                if (
                len(current) >= comparison_gate(period_days)
                and len(previous) >= comparison_gate(previous_period_days)
            ) else None
            ),
        }
    else:
        step_values = {
            "available_days": steps_metric.available_days if steps_metric else 0,
            "previous_available_days": steps_metric.previous_available_days if steps_metric else 0,
            "total_steps": int(steps_metric.total) if steps_metric and steps_metric.total is not None else None,
            "average_steps": steps_metric.average if steps_metric else None,
            "previous_average_steps": steps_metric.previous_average if steps_metric else None,
            "steps_change_percent": steps_metric.change_percent if steps_metric else None,
        }
    active_minutes = (
        int(active_metric.total) if active_metric and active_metric.total is not None else None
    )
    return MonthlyActivityFacts(
        metrics=metrics,
        active_minutes=active_minutes,
        **step_values,
    )


def _feedback_facts(items: list[dict]) -> MonthlyFeedbackFacts:
    return MonthlyFeedbackFacts(
        response_count=len(items),
        average_session_rpe=_field_average(items, "session_rpe"),
        average_physical_fatigue=_field_average(items, "physical_fatigue"),
        average_mental_state=_field_average(items, "mental_state"),
        average_muscle_soreness=_field_average(items, "muscle_soreness"),
    )


def _quality(
    facts: MonthlyFacts,
    period_days: int = PERIOD_DAYS,
    period_mode: PeriodMode | str = PeriodMode.CALENDAR,
) -> MonthlyDataQuality:
    hrv_days = max(
        (item.available_days for item in facts.recovery.streams if item.metric.startswith("hrv") or item.metric == "sleep_hrv"),
        default=0,
    )
    sufficient_gate = comparison_gate(period_days, period_mode)
    sufficient = int(facts.sleep.available_days >= sufficient_gate) + int(hrv_days >= sufficient_gate)
    if sufficient == 2:
        status = QualityStatus.SUFFICIENT
        confidence = ConfidenceBand.HIGH if min(facts.sleep.available_days, hrv_days) >= ceil(period_days * 0.90) else ConfidenceBand.MODERATE
    elif sufficient == 1:
        status = QualityStatus.PARTIAL
        confidence = ConfidenceBand.LOW
    else:
        status = QualityStatus.INSUFFICIENT
        confidence = ConfidenceBand.NONE
    limitations = []
    if facts.sleep.available_days < sufficient_gate:
        limitations.append(
            f"睡眠有效天数不足 {sufficient_gate}/{period_days} 天。"
        )
    if hrv_days < sufficient_gate:
        limitations.append(
            f"同一设备 HRV 有效天数不足 {sufficient_gate}/{period_days} 天。"
        )
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


def _key_changes(
    trends: list[TrendFeature],
    period_days: int = PERIOD_DAYS,
    period_mode: PeriodMode | str = PeriodMode.ROLLING,
    facts: MonthlyFacts | None = None,
) -> list[str]:
    mode = normalize_period_mode(period_mode)
    reference_label = f"前 {period_days} 天" if mode is PeriodMode.ROLLING else "前一周期"
    if mode is PeriodMode.CALENDAR and facts is not None:
        changes: list[tuple[str, float]] = []
        if facts.sleep.change_percent is not None:
            changes.append(("睡眠时长", facts.sleep.change_percent))
        if facts.activity.steps_change_percent is not None:
            changes.append(("步数", facts.activity.steps_change_percent))
        if facts.training.load_change_percent is not None:
            changes.append(("设备训练负荷", facts.training.load_change_percent))
        for stream in facts.recovery.streams:
            if stream.change_percent is not None:
                changes.append((stream.metric_label, stream.change_percent))
        changes.sort(key=lambda item: (-abs(item[1]), item[0]))
        return [
            f"{label}{reference_label}{'上升' if change > 0 else '下降'} {abs(change):.1f}%。"
            for label, change in changes[:8]
        ]
    selected = [
        item for item in trends
        if item.window_days == 28
        and item.direction != TrendDirection.STABLE
        and item.change_percent is not None
    ]
    selected.sort(key=lambda item: (-abs(item.change_percent or 0), item.metric, item.device_id or ""))
    return [
        f"{item.metric_label}{reference_label}{item.direction_label} {abs(item.change_percent):.1f}%。"
        for item in selected[:8]
    ]


def _recommend(
    facts: MonthlyFacts,
    events: list[HealthEvent],
    training_preferences=None,
    *,
    period_days: int = PERIOD_DAYS,
    period_mode: PeriodMode | str = PeriodMode.CALENDAR,
) -> list[WeeklyRecommendation]:
    output = []
    active_events = [
        item for item in events
        if getattr(item, "lifecycle", None) != "RESOLVED"
    ]
    comparison_days = comparison_gate(period_days, period_mode)
    pain_present = bool(
        training_preferences is not None
        and getattr(training_preferences, "pain_or_injury_status", None) == "PRESENT"
    )
    if pain_present:
        output.append(WeeklyRecommendation(
            priority=1,
            code="MONTHLY_RESPECT_PAIN_OR_INJURY",
            title="遵守疼痛或伤病限制",
            action="下一个周期暂停会诱发疼痛的训练；疼痛持续、加重或影响日常活动时寻求专业评估。",
            reasons=["训练偏好中已记录疼痛或伤病状态。"],
        ))
    elif any(item.type in {"RECOVERY_SUPPRESSED", "HRV_DROP", "RHR_ELEVATED"} for item in active_events):
        output.append(WeeklyRecommendation(
            priority=1,
            code="MONTHLY_PRIORITIZE_RECOVERY",
            title="先恢复再加量",
            action="下一个周期先控制连续高负荷训练，并保留每周至少 1 个完整休息日。",
            reasons=["本期存在恢复相关持续事件。"],
        ))

    if output and (
        facts.sleep.available_days < comparison_days
        and facts.training.record_days < comparison_days
    ):
        return output[:3]
    if (
        facts.sleep.available_days < comparison_days
        and facts.training.record_days < comparison_days
    ):
        return [WeeklyRecommendation(
            priority=1,
            code="MONTHLY_INSUFFICIENT_DATA",
            title="周期数据不足",
            action="先完成新数据同步，再形成下一周期的训练与恢复建议。",
            reasons=[
                f"本期睡眠和训练记录覆盖均不足 {comparison_days}/{period_days} 天。"
            ],
        )]

    complete = (
        facts.training.coverage_status == "COMPLETE"
        and facts.training.unknown_days == 0
    )
    if not complete:
        if output:
            return output[:3]
        return [WeeklyRecommendation(
            priority=1,
            code="MONTHLY_INSUFFICIENT_DATA",
            title="先补齐周期记录",
            action="先完成后续同步，待本期训练覆盖明确后再评估训练量变化。",
            reasons=[
                f"本期训练已记录 {facts.training.record_days}/{period_days} 天，"
                f"仍有 {facts.training.unknown_days} 天未知。"
            ],
        )]

    if facts.sleep.average_minutes is not None and facts.sleep.average_minutes < 420:
        output.append(WeeklyRecommendation(
            priority=len(output) + 1,
            code="MONTHLY_IMPROVE_SLEEP",
            title="提高平均睡眠时长",
            action="下一个周期优先把平均睡眠提高到每晚至少 7 小时。",
            reasons=[f"本期平均睡眠 {facts.sleep.average_minutes:.0f} 分钟。"],
        ))
    if not output:
        output.append(WeeklyRecommendation(
            priority=1,
            code="MONTHLY_MAINTAIN_PLAN",
            title="保持当前周期安排",
            action="下一个周期保持当前训练与恢复节奏，不额外堆叠高强度负荷。",
            reasons=["本周期没有触发需要优先调整的确定性规则。"],
        ))
    return output


def _monthly_training_coverage(
    raw,
    period_start: date,
    period_days: int = PERIOD_DAYS,
    previous_start: date | None = None,
    previous_period_days: int | None = None,
) -> dict:
    records = {
        day for day in raw.training_by_day
        if period_start <= day <= raw.day
    }
    previous_start = previous_start or period_start - timedelta(days=period_days)
    previous_period_days = previous_period_days or (period_start - previous_start).days
    context = getattr(raw, "training_history_coverage", None) or {}
    if not isinstance(context, dict):
        context = {}
    nested = context.get(f"current_{period_days}d")
    if nested is None and period_days == 28:
        nested = context.get("current_28d")
    # A target-day loader's 28-day summary cannot describe a calendar month
    # with a different length; use it only when its window matches.
    values = {**context, **nested} if isinstance(nested, dict) else context
    status = str(values.get("coverage_status") or values.get("status") or "").upper()
    verified_days = values.get("verified_days")
    previous_days = {
        previous_start + timedelta(days=index)
        for index in range(previous_period_days)
    }
    current_days = {
        period_start + timedelta(days=index) for index in range(period_days)
    }
    target_day_complete = bool(
        (getattr(raw, "report_context", None) or {}).get("target_day_complete", True)
    )
    complete_current_days = set(current_days)
    if not target_day_complete:
        complete_current_days.discard(raw.day)
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
        if candidate in complete_current_days:
            verified_current.add(candidate)
    if verified_days is not None:
        record_days = len(verified_current)
        status = (
            "COMPLETE"
            if target_day_complete and complete_current_days <= verified_all
            else "PARTIAL" if verified_current else "UNKNOWN"
        )
    elif values.get("record_days") is not None:
        record_days = int(values["record_days"])
    elif status == "UNKNOWN":
        record_days = 0
    else:
        record_days = len(records)
    record_days = max(0, min(record_days, period_days))
    if not target_day_complete and status == "COMPLETE":
        status = "PARTIAL"
        record_days = min(record_days, period_days - 1)
    explicit_unknown = values.get("unknown_days")
    unknown_days = max(
        int(explicit_unknown) if explicit_unknown is not None else 0,
        period_days - record_days,
    )
    if status not in {"COMPLETE", "PARTIAL", "UNKNOWN"}:
        status = "PARTIAL" if records else "UNKNOWN"
    unknown_days = max(0, min(unknown_days, period_days - record_days))
    if status == "COMPLETE":
        record_days, unknown_days = period_days, 0
    return {
        "record_days": record_days,
        "unknown_days": unknown_days,
        "coverage_status": status,
        "totals_are_partial": bool(
            values.get("totals_are_partial", status != "COMPLETE")
        ),
        "covered_days": verified_current if verified_days is not None else None,
        "current_complete": (
            verified_days is not None
            and target_day_complete
            and complete_current_days <= verified_all
        ),
        "previous_complete": (
            verified_days is not None and previous_days <= verified_all
        ),
    }


def _report_context(
    period_raw,
    period: ReportPeriod,
    original_raw,
) -> dict:
    context = dict(getattr(period_raw, "report_context", None) or {})
    context.update(period.metadata())
    context.update({
        "as_of": original_raw.as_of.isoformat(),
        "target_date": original_raw.day.isoformat(),
        "training_coverage": getattr(
            original_raw, "training_history_coverage", {}
        ) or {},
    })
    return context


def _record_values(records, field, start, end) -> list[float]:
    return [
        float(item[field]) for day, item in records.items()
        if start <= day <= end and item.get(field) is not None
    ]


def _optional_sum(items: list[dict], field: str, converter):
    values = [converter(item[field]) for item in items if item.get(field) is not None]
    return sum(values) if values else None


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
