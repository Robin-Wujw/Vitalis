"""Explainable health-event detection over normalized facts and personal baselines."""

from datetime import date, timedelta
from hashlib import sha256
from statistics import median

from .baseline import BaselineEngine
from .contracts import (
    Availability,
    BaselineStats,
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
    HealthEventEvidence,
    HrvFeatures,
    RecoveryFeatures,
    RecoveryState,
    TrendDirection,
    TrendFeature,
)
from .localization import CONFIDENCE_LABELS
from .profile import RawDailyProfile
from .trend import METRIC_LABELS, stream_daily_values


EVENT_LABELS = {
    "HRV_DROP": "心率变异性持续下降",
    "HRV_RECOVERY": "心率变异性持续恢复",
    "RHR_ELEVATED": "静息心率持续偏高",
    "SLEEP_DEFICIT": "持续睡眠不足",
    "SLEEP_IMPROVEMENT": "睡眠持续改善",
    "TRAINING_LOAD_SPIKE": "训练负荷明显增加",
    "TRAINING_GAP": "训练连续中断",
    "RECOVERY_SUPPRESSED": "恢复状态受抑制",
    "ACTIVITY_DROP": "活动量明显下降",
    "ACTIVITY_SURGE": "活动量明显增加",
}
SEVERITY_LABELS = {
    EventSeverity.INFO: "提示",
    EventSeverity.MODERATE: "中等",
    EventSeverity.HIGH: "较高",
}


class HealthEventEngine:
    def detect(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
        trends: list[TrendFeature],
        hrv: HrvFeatures,
        recovery: RecoveryFeatures,
    ) -> list[HealthEvent]:
        events: list[HealthEvent] = []
        hrv_metric = hrv.preferred_metric
        if hrv_metric:
            baseline = _best_baseline(baselines, hrv_metric, hrv.preferred_device_id)
            if baseline:
                daily = _daily_for_baseline(raw, baseline)
                events.extend(_persistent_deviation_event(
                    raw.user_id,
                    raw.day,
                    daily,
                    baseline,
                    below_type="HRV_DROP",
                    above_type="HRV_RECOVERY",
                ))

        rhr_metric, rhr_baseline = _best_rhr_baseline(baselines)
        if rhr_metric and rhr_baseline:
            daily = _daily_for_baseline(raw, rhr_baseline)
            events.extend(_persistent_deviation_event(
                raw.user_id,
                raw.day,
                daily,
                rhr_baseline,
                below_type=None,
                above_type="RHR_ELEVATED",
            ))

        sleep_baseline = _best_baseline(baselines, "sleep_duration", None)
        if sleep_baseline:
            daily = _daily_for_baseline(raw, sleep_baseline)
            events.extend(_persistent_deviation_event(
                raw.user_id,
                raw.day,
                daily,
                sleep_baseline,
                below_type="SLEEP_DEFICIT",
                above_type="SLEEP_IMPROVEMENT",
                absolute_below=420,
            ))

        load_trend = _best_trend(trends, "training_load", 7)
        if (
            load_trend
            and load_trend.status == Availability.AVAILABLE
            and load_trend.change_percent is not None
            and load_trend.change_percent >= 25
        ):
            events.append(_period_event(
                raw.user_id,
                raw.day,
                "TRAINING_LOAD_SPIKE",
                "training_load",
                load_trend,
            ))

        steps_trend = _best_trend(trends, "steps", 7)
        if steps_trend and steps_trend.status == Availability.AVAILABLE:
            if steps_trend.change_percent is not None and steps_trend.change_percent <= -25:
                events.append(_period_event(
                    raw.user_id, raw.day, "ACTIVITY_DROP", "steps", steps_trend
                ))
            elif steps_trend.change_percent is not None and steps_trend.change_percent >= 25:
                events.append(_period_event(
                    raw.user_id, raw.day, "ACTIVITY_SURGE", "steps", steps_trend
                ))

        workout_days = [
            item.get("local_day") for item in raw.workouts
            if isinstance(item.get("local_day"), date)
        ]
        recent_workout = any(
            raw.day - timedelta(days=6) <= workout_day <= raw.day
            for workout_day in workout_days
        )
        prior_workout = any(
            raw.day - timedelta(days=27) <= workout_day < raw.day - timedelta(days=6)
            for workout_day in workout_days
        )
        if not recent_workout and prior_workout:
            events.append(_simple_event(
                raw.user_id,
                "TRAINING_GAP",
                raw.day - timedelta(days=6),
                raw.day,
                "过去 7 天没有训练记录，但此前 21 天存在训练。",
                ConfidenceBand.HIGH,
                [HealthEventEvidence(
                    fact="workout_count_7d",
                    fact_label="近 7 天训练次数",
                    value=0,
                    unit="次",
                )],
            ))

        if recovery.state == RecoveryState.SUPPRESSED:
            events.append(_simple_event(
                raw.user_id,
                "RECOVERY_SUPPRESSED",
                raw.day,
                raw.day,
                "多个恢复信号同时偏离个人基线。",
                ConfidenceBand.HIGH if len(recovery.negative_signals) >= 3 else ConfidenceBand.MODERATE,
                [
                    HealthEventEvidence(
                        fact=code,
                        fact_label=label,
                    )
                    for code, label in zip(
                        recovery.negative_signals,
                        recovery.negative_signal_labels,
                    )
                ],
                severity=EventSeverity.HIGH if len(recovery.negative_signals) >= 3 else EventSeverity.MODERATE,
            ))
        severity_rank = {EventSeverity.INFO: 1, EventSeverity.MODERATE: 2, EventSeverity.HIGH: 3}
        return sorted(
            events,
            key=lambda item: (severity_rank[item.severity], item.type),
            reverse=True,
        )


def _persistent_deviation_event(
    user_id: str,
    target_day: date,
    daily: dict[date, float],
    baseline: BaselineStats,
    *,
    below_type: str | None,
    above_type: str | None,
    absolute_below: float | None = None,
) -> list[HealthEvent]:
    directions: list[tuple[date, str, float]] = []
    for offset in range(0, 14):
        day = target_day - timedelta(days=offset)
        value = daily.get(day)
        if value is None:
            break
        deviation = BaselineEngine.deviation(value, baseline)
        direction = deviation.direction
        if absolute_below is not None and value < absolute_below:
            direction = "below"
        directions.append((day, direction, value))
    if not directions:
        return []
    target_direction = directions[0][1]
    event_type = below_type if target_direction == "below" else above_type if target_direction == "above" else None
    if event_type is None:
        return []
    persistent = []
    for item in directions:
        if item[1] != target_direction:
            break
        persistent.append(item)
    if len(persistent) < 3:
        return []
    values = [item[2] for item in persistent]
    center = median(values)
    deviation = BaselineEngine.deviation(center, baseline)
    severity = (
        EventSeverity.HIGH
        if len(persistent) >= 5 or abs(deviation.percent or 0) >= 20
        else EventSeverity.MODERATE
    )
    confidence = ConfidenceBand.HIGH if baseline.coverage_ratio >= 0.8 else ConfidenceBand.MODERATE
    start = persistent[-1][0]
    summary = (
        f"{METRIC_LABELS.get(baseline.metric, baseline.metric)}已连续 "
        f"{len(persistent)} 天{('低于' if target_direction == 'below' else '高于')}个人基线。"
    )
    return [_simple_event(
        user_id,
        event_type,
        start,
        target_day,
        summary,
        confidence,
        [
            HealthEventEvidence(
                fact="period_median",
                fact_label="事件期间中位数",
                value=round(float(center), 2),
                unit=baseline.unit,
            ),
            HealthEventEvidence(
                fact="baseline_reference",
                fact_label="28 天个人基线",
                value=baseline.reference_value,
                unit=baseline.unit,
            ),
        ],
        metric=baseline.metric,
        deviation_percent=deviation.percent,
        baseline_window_days=baseline.window_days,
        severity=severity,
    )]


def _period_event(
    user_id: str,
    target_day: date,
    event_type: str,
    metric: str,
    trend: TrendFeature,
) -> HealthEvent:
    return _simple_event(
        user_id,
        event_type,
        target_day - timedelta(days=trend.window_days - 1),
        target_day,
        f"{trend.metric_label}较前一周期变化 {trend.change_percent:+.1f}%。",
        trend.confidence,
        [
            HealthEventEvidence(
                fact="period_change",
                fact_label="较前一周期变化",
                value=trend.change_percent,
                unit="%",
            )
        ],
        metric=metric,
        deviation_percent=trend.change_percent,
        severity=EventSeverity.MODERATE,
    )


def _simple_event(
    user_id: str,
    event_type: str,
    start: date,
    end: date,
    summary: str,
    confidence: ConfidenceBand,
    evidence: list[HealthEventEvidence],
    *,
    metric: str | None = None,
    deviation_percent: float | None = None,
    baseline_window_days: int | None = None,
    severity: EventSeverity = EventSeverity.MODERATE,
) -> HealthEvent:
    identity = sha256(
        f"{user_id}|{event_type}|{metric or ''}|{start.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    return HealthEvent(
        id=identity,
        type=event_type,
        type_label=EVENT_LABELS[event_type],
        severity=severity,
        severity_label=SEVERITY_LABELS[severity],
        metric=metric,
        metric_label=METRIC_LABELS.get(metric) if metric else None,
        start_date=start,
        end_date=end,
        duration_days=(end - start).days + 1,
        deviation_percent=deviation_percent,
        baseline_window_days=baseline_window_days,
        confidence=confidence,
        confidence_label=CONFIDENCE_LABELS[confidence.value],
        summary=summary,
        evidence=evidence,
    )


def _best_baseline(
    baselines: dict[str, list[BaselineStats]],
    metric: str,
    device_id: str | None,
) -> BaselineStats | None:
    candidates = [
        item for item in baselines.get(metric, [])
        if item.window_days == 28
        and item.status == Availability.AVAILABLE
        and (device_id is None or item.device_id == device_id)
    ]
    return max(candidates, key=lambda item: item.distinct_days, default=None)


def _best_rhr_baseline(
    baselines: dict[str, list[BaselineStats]],
) -> tuple[str | None, BaselineStats | None]:
    for metric in ("sleep_rhr", "resting_hr"):
        baseline = _best_baseline(baselines, metric, None)
        if baseline:
            return metric, baseline
    return None, None


def _daily_for_baseline(raw: RawDailyProfile, baseline: BaselineStats) -> dict[date, float]:
    streams = stream_daily_values(raw.series.get(baseline.metric, []))
    return streams.get(
        (baseline.source, baseline.source_scope, baseline.device_id, baseline.unit),
        {},
    )


def _best_trend(
    trends: list[TrendFeature],
    metric: str,
    window_days: int,
) -> TrendFeature | None:
    candidates = [
        item for item in trends
        if item.metric == metric and item.window_days == window_days
    ]
    return max(candidates, key=lambda item: item.current_distinct_days, default=None)
