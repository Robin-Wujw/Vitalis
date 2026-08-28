"""Bounded layered context for Hermes and other agent consumers."""

from .contracts import (
    AgentContext,
    ContextCurrent,
    ContextEvent,
    ContextFeedback,
    ContextPattern,
    ContextPatternMetric,
    ContextPersonal,
    ContextRecent,
    ContextTrend,
    EventLifecycle,
)


class AgentContextEngine:
    def build(self, daily, weekly, events, feedback, personal_model) -> AgentContext:
        current = ContextCurrent(
            analysis_run_id=daily.analysis_run_id,
            date=daily.date,
            data_quality_status=daily.data_quality.status,
            data_quality_label=daily.data_quality.status_label,
            sleep_minutes=daily.features.sleep.duration_minutes,
            hrv_metric=daily.features.hrv.preferred_metric,
            hrv_value_ms=daily.features.hrv.value_ms,
            hrv_device_id=daily.features.hrv.preferred_device_id,
            rhr_bpm=daily.features.hrv.rhr_bpm,
            recovery_state=daily.states.recovery,
            recovery_state_label=daily.states.recovery_label,
            training_load_state=daily.states.training_load,
            training_load_state_label=daily.states.training_load_label,
            action=daily.decision.action,
            action_label=daily.decision.action_label,
            confidence=daily.decision.confidence,
            confidence_label=daily.decision.confidence_label,
            recommendation_id=daily.decision.recommendation_id,
            suggested_type_labels=daily.decision.suggested_type_labels[:3],
            driver_labels=daily.decision.driver_labels[:5],
            limitation_labels=daily.decision.limitation_labels[:5],
        )
        active = [item for item in events if item.lifecycle != EventLifecycle.RESOLVED]
        severity_rank = {"HIGH": 3, "MODERATE": 2, "INFO": 1}
        active.sort(
            key=lambda item: (severity_rank[item.severity.value], item.end_date),
            reverse=True,
        )
        recent = ContextRecent(
            period_start=weekly.period_start,
            period_end=weekly.period_end,
            sleep_average_minutes=weekly.facts.sleep.average_minutes,
            hrv_change_percent=weekly.facts.recovery.hrv_change_percent,
            rhr_change_percent=weekly.facts.recovery.rhr_change_percent,
            workout_count=weekly.facts.training.workout_count,
            training_duration_minutes=weekly.facts.training.duration_minutes,
            sport_mode_counts=dict(list(weekly.facts.training.sport_mode_counts.items())[:8]),
            active_events=[
                ContextEvent(
                    id=item.id,
                    type=item.type,
                    type_label=item.type_label,
                    lifecycle=item.lifecycle,
                    lifecycle_label=item.lifecycle_label,
                    severity=item.severity,
                    summary=item.summary,
                    acknowledged=item.acknowledged,
                )
                for item in active[:5]
            ],
            feedback=[
                ContextFeedback(
                    id=item.id,
                    date=item.date,
                    workout_id=item.workout_id,
                    session_rpe=item.session_rpe,
                    physical_fatigue=item.physical_fatigue,
                    mental_state=item.mental_state,
                    muscle_soreness=item.muscle_soreness,
                )
                for item in sorted(feedback, key=lambda value: (value.date, value.created_at), reverse=True)[:5]
            ],
        )
        trends = [
            ContextTrend(
                metric=item.metric,
                metric_label=item.metric_label,
                window_days=item.window_days,
                device_id=item.device_id,
                change_percent=item.change_percent,
                direction=item.direction,
                direction_label=item.direction_label,
                confidence=item.confidence,
                confidence_label=item.confidence_label,
            )
            for item in sorted(
                daily.trends,
                key=lambda value: (value.window_days, value.metric, value.device_id or ""),
                reverse=True,
            )[:12]
        ]
        confidence_rank = {"HIGH": 3, "MODERATE": 2, "LOW": 1, "NONE": 0}
        patterns = sorted(
            personal_model.training_response_patterns,
            key=lambda item: (confidence_rank[item.confidence.value], item.response_count),
            reverse=True,
        )[:6]
        personal = ContextPersonal(
            patterns=[
                ContextPattern(
                    group_type=item.group_type,
                    group_key=item.group_key,
                    group_label=item.group_label,
                    response_count=item.response_count,
                    confidence=item.confidence,
                    confidence_label=item.confidence_label,
                    metrics=[
                        ContextPatternMetric(
                            metric=metric.metric,
                            device_id=metric.device_id,
                            median=metric.median,
                            unit=metric.unit,
                            sample_count=metric.sample_count,
                            coverage_ratio=metric.coverage_ratio,
                        )
                        for metric in item.metrics[:3]
                    ],
                )
                for item in patterns
            ],
            limitations=personal_model.limitations[:3],
        )
        return AgentContext(
            user_id=daily.user_id,
            date=daily.date,
            current=current,
            recent=recent,
            trend=trends,
            personal=personal,
        )
