"""Bounded layered context for Hermes and other agent consumers."""

from .contracts import (
    AgentContext,
    ContextCurrent,
    ContextAssociation,
    ContextEvent,
    ContextFeedback,
    ContextPattern,
    ContextPatternMetric,
    ContextPersonal,
    ContextRecent,
    ContextTrend,
    ContextProfile,
    EventLifecycle,
    MissingUserInput,
    UserProfile,
    OpenHealthSummary,
)
from .open_health.projection import aggregate_status


class AgentContextEngine:
    def build(
        self, daily, weekly, events, feedback, personal_model,
        profile: UserProfile | None = None,
    ) -> AgentContext:
        profile = profile or UserProfile(user_id=daily.user_id)
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
            primary_session_title=(
                daily.decision.action_plan.primary_session.title
                if daily.decision.action_plan.primary_session else None
            ),
            optional_session_title=(
                daily.decision.action_plan.optional_session.title
                if daily.decision.action_plan.optional_session else None
            ),
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
                    workout_source=item.workout_source,
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
            associations=[
                ContextAssociation(
                    id=item.id,
                    predictor_metric_label=item.predictor_metric_label,
                    outcome_metric_label=item.outcome_metric_label,
                    predictor_source=item.predictor_source,
                    predictor_source_scope=item.predictor_source_scope,
                    predictor_device_id=item.predictor_device_id,
                    outcome_source=item.outcome_source,
                    outcome_source_scope=item.outcome_source_scope,
                    outcome_device_id=item.outcome_device_id,
                    lag_days=item.lag_days,
                    window_days=item.window_days,
                    coefficient=item.coefficient,
                    direction_label=item.direction_label,
                    strength_label=item.strength_label,
                    confidence=item.confidence,
                    confidence_label=item.confidence_label,
                    summary=item.summary,
                )
                for item in sorted(
                    personal_model.personal_associations,
                    key=lambda value: (
                        confidence_rank[value.confidence.value],
                        abs(value.coefficient or 0),
                        value.window_days,
                    ),
                    reverse=True,
                )[:6]
                if item.coefficient is not None
            ],
            limitations=personal_model.limitations[:3],
        )
        profile_projection = ContextProfile(
            revision=profile.revision,
            sex=profile.sex.value if profile.sex else None,
            confirmed_hrmax_bpm=(
                profile.confirmed_hrmax_bpm.value
                if profile.confirmed_hrmax_bpm else None
            ),
            sleep_target_minutes=(
                profile.sleep_target_minutes.value
                if profile.sleep_target_minutes else None
            ),
        )
        missing_inputs = []
        if profile.sex is None:
            missing_inputs.append(MissingUserInput(
                field="sex",
                label="生理性别",
                question="请确认你的生理性别，用于选择公开训练负荷公式。",
                reason="Banister TRIMP v1 仅在用户明确确认为 MALE 时计算。",
                required_for=["banister_trimp", "atl_ctl_tsb"],
            ))
        if profile.confirmed_hrmax_bpm is None:
            missing_inputs.append(MissingUserInput(
                field="confirmed_hrmax_bpm",
                label="确认最大心率",
                question="请提供你确认的最大心率（bpm）。",
                reason="训练观测最大值和设备区间不能替代用户确认的 HRmax。",
                required_for=["banister_trimp", "atl_ctl_tsb"],
            ))
        if profile.sleep_target_minutes is None:
            missing_inputs.append(MissingUserInput(
                field="sleep_target_minutes",
                label="睡眠目标",
                question="你希望每晚睡眠目标是多少分钟或小时？",
                reason="没有用户目标时只能计算睡眠效率和规律性，不能计算目标达成率。",
                blocking=False,
                required_for=["sleep_target_attainment"],
            ))
        open_health_summary, insights_stale = _open_health_context(
            daily, profile.revision
        )
        return AgentContext(
            user_id=daily.user_id,
            date=daily.date,
            current=current,
            recent=recent,
            trend=trends,
            personal=personal,
            profile=profile_projection,
            missing_inputs=missing_inputs,
            open_health_summary=open_health_summary,
            insights_stale=insights_stale,
        )


def _open_health_context(daily, current_profile_revision: int):
    bundle = getattr(daily, "open_health_insights", None)
    if bundle is None:
        return None, True
    insights = [item for item in (
        bundle.readiness, bundle.anomaly, bundle.sleep, bundle.training_load
    ) if item is not None]
    status = aggregate_status(bundle)
    readiness = bundle.readiness.payload if bundle.readiness else None
    anomaly = bundle.anomaly.payload if bundle.anomaly else None
    drivers = []
    readiness_state = getattr(readiness, "state", None)
    readiness_label = {
        "suppressed": "夜间 RMSSD 相对近期个人范围偏低",
        "normal": "夜间 RMSSD 接近近期个人范围",
        "elevated": "夜间 RMSSD 相对近期个人范围偏高",
    }.get(readiness_state)
    if readiness_label:
        drivers.append(readiness_label)
    if getattr(anomaly, "flagged", False):
        drivers.append("夜间多个生理信号连续偏离个人常态")
    refusals = [item.refusal_reason for item in insights if item.refusal_reason][:4]
    missing = [
        value for refusal in refusals for value in refusal.missing_inputs
    ][:8]
    return OpenHealthSummary(
        profile_revision_used=bundle.profile_revision_used,
        status=status,
        readiness_state=getattr(readiness, "state", None),
        anomaly_flagged=getattr(anomaly, "flagged", None),
        load_status=bundle.training_load.status if bundle.training_load else None,
        drivers=drivers,
        refusal_reasons=refusals,
        missing_inputs=missing,
    ), (
        bundle.target_date != daily.date
        or bundle.profile_revision_used != current_profile_revision
    )
