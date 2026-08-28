"""Explainable, deterministic training decision policy."""

from .contracts import (
    ConfidenceBand,
    DecisionAction,
    HrvFeatures,
    LoadState,
    RecoveryFeatures,
    RecoveryState,
    SleepState,
    TrainingDecision,
    TrainingFeatures,
    TrainingPrescription,
    TrainingStep,
)
from .localization import (
    ACTION_LABELS,
    CONFIDENCE_LABELS,
    DRIVER_LABELS,
    INTENSITY_LABELS,
    LIMITATION_LABELS,
    SUGGESTED_TYPE_LABELS,
    labels,
)


class DecisionEngine:
    def decide(
        self,
        recommendation_id: str,
        sleep_state: SleepState,
        hrv: HrvFeatures,
        recovery: RecoveryFeatures,
        training: TrainingFeatures,
    ) -> TrainingDecision:
        limitations = list(dict.fromkeys(
            hrv.limitations + recovery.limitations + training.limitations
        ))
        signal_count = len(recovery.positive_signals) + len(recovery.negative_signals)
        if recovery.state == RecoveryState.INSUFFICIENT_DATA:
            return _localized(TrainingDecision(
                recommendation_id=recommendation_id,
                action=DecisionAction.INSUFFICIENT_DATA,
                confidence=ConfidenceBand.NONE,
                limitations=limitations,
                rule_ids=["DECISION.REQUIRED_RECOVERY_SIGNALS"],
                intensity="undetermined",
            ))

        confidence = (
            ConfidenceBand.HIGH
            if signal_count >= 3 and not any("baseline_insufficient" in item for item in limitations)
            else ConfidenceBand.MODERATE
        )
        if recovery.state == RecoveryState.SUPPRESSED:
            severe = {
                "HRV_BELOW_BASELINE",
                "RHR_ABOVE_BASELINE",
                "SLEEP_BELOW_BASELINE",
            }.issubset(recovery.negative_signals)
            if severe:
                return _localized(TrainingDecision(
                    recommendation_id=recommendation_id,
                    action=DecisionAction.REST,
                    confidence=confidence,
                    drivers=recovery.negative_signals,
                    limitations=limitations,
                    rule_ids=["DECISION.MULTISIGNAL_SUPPRESSION_REST"],
                    suggested_types=["rest", "gentle_mobility"],
                    intensity="none",
                    duration_minutes=None,
                ))
            return _localized(TrainingDecision(
                recommendation_id=recommendation_id,
                action=DecisionAction.RECOVERY,
                confidence=confidence,
                drivers=recovery.negative_signals,
                limitations=limitations,
                rule_ids=["DECISION.MULTISIGNAL_SUPPRESSION_RECOVERY"],
                suggested_types=["walking", "mobility"],
                intensity="low",
                duration_minutes=(20, 40),
            ))

        if recovery.state == RecoveryState.NORMAL and training.load_state == LoadState.ELEVATED:
            return _localized(TrainingDecision(
                recommendation_id=recommendation_id,
                action=DecisionAction.TRAIN_LIGHT,
                confidence=confidence,
                drivers=["RECOVERY_NORMAL", "TRAINING_LOAD_ELEVATED"],
                limitations=limitations,
                rule_ids=["DECISION.ELEVATED_LOAD_LIGHT"],
                suggested_types=["zone2", "mobility"],
                intensity="low",
                duration_minutes=(30, 45),
            ))

        if (
            recovery.state == RecoveryState.GOOD
            and sleep_state == SleepState.ABOVE_BASELINE
            and training.load_state == LoadState.LOW
        ):
            return _localized(TrainingDecision(
                recommendation_id=recommendation_id,
                action=DecisionAction.TRAIN_HARD,
                confidence=confidence,
                drivers=recovery.positive_signals + ["TRAINING_LOAD_LOW"],
                limitations=limitations,
                rule_ids=["DECISION.GOOD_RECOVERY_LOW_LOAD"],
                suggested_types=_training_types(training),
                intensity="high",
                duration_minutes=(45, 60),
            ))

        return _localized(TrainingDecision(
            recommendation_id=recommendation_id,
            action=DecisionAction.TRAIN_NORMAL,
            confidence=confidence,
            drivers=recovery.positive_signals or ["RECOVERY_NORMAL"],
            limitations=limitations,
            rule_ids=["DECISION.NORMAL_TRAINING"],
            suggested_types=_training_types(training),
            intensity="moderate",
            duration_minutes=(45, 60),
        ))


def _training_types(training: TrainingFeatures) -> list[str]:
    suggestions = []
    strength_is_due = (
        training.strength_sessions_7d is not None
        and training.strength_sessions_7d < 2
        and (
            training.days_since_last_strength is None
            or training.days_since_last_strength >= 2
        )
    )
    if strength_is_due:
        suggestions.append("resistance")
    if training.aerobic_minutes_7d is not None and training.aerobic_minutes_7d < 150:
        suggestions.append("zone2")
    return suggestions or ["planned_session"]


def _localized(decision: TrainingDecision) -> TrainingDecision:
    decision.action_label = ACTION_LABELS[decision.action.value]
    decision.confidence_label = CONFIDENCE_LABELS[decision.confidence.value]
    decision.intensity_label = INTENSITY_LABELS[decision.intensity]
    decision.driver_labels = labels(decision.drivers, DRIVER_LABELS)
    decision.limitation_labels = labels(decision.limitations, LIMITATION_LABELS)
    decision.suggested_type_labels = labels(decision.suggested_types, SUGGESTED_TYPE_LABELS)
    decision.prescriptions = [
        _prescription(code, decision.duration_minutes, decision.intensity)
        for code in decision.suggested_types
    ]
    if len(decision.prescriptions) > 1:
        decision.prescription_guidance = "以下方案二选一，不要在同一次训练中叠加完成。"
    elif decision.prescriptions:
        decision.prescription_guidance = "按以下结构完成本次训练。"
    return decision


def _prescription(
    code: str,
    duration: tuple[int, int] | None,
    intensity: str,
) -> TrainingPrescription:
    if code == "zone2":
        return TrainingPrescription(
            code=code,
            title="二区有氧跑",
            goal="补充有氧基础训练，同时控制本次恢复成本",
            total_duration_minutes=duration or (45, 60),
            steps=[
                TrainingStep(
                    order=1,
                    name="热身",
                    duration_minutes=(8, 10),
                    intensity="轻松",
                    instructions=["先快走，再逐渐过渡到慢跑", "结束时呼吸应仍然平稳"],
                ),
                TrainingStep(
                    order=2,
                    name="二区主训练",
                    duration_minutes=(30, 40),
                    intensity="低到中等强度",
                    instructions=[
                        "保持呼吸稳定，能够连续说出完整短句",
                        "不冲刺，不安排高强度间歇",
                        "心率过高时主动降速或采用跑走结合",
                    ],
                ),
                TrainingStep(
                    order=3,
                    name="冷身",
                    duration_minutes=(7, 10),
                    intensity="轻松",
                    instructions=["逐渐减速到步行", "结束后做小腿和髋部轻柔活动"],
                ),
            ],
            progression=["连续两次完成且主训练呼吸稳定时，下次主训练增加 5 分钟"],
            cautions=["当前尚无可靠个体心率区间，优先使用谈话测试", "出现疼痛、胸闷或头晕时停止训练"],
        )

    if code == "resistance":
        effort = "每组保留 1-2 次余力" if intensity == "high" else "每组保留 2-3 次余力"
        return TrainingPrescription(
            code=code,
            title="全身力量训练",
            goal="覆盖蹲、推、拉、髋伸和核心五类基础动作模式",
            total_duration_minutes=duration or (45, 60),
            steps=[
                TrainingStep(order=1, name="动态热身", duration_minutes=(6, 8), intensity="轻松", instructions=["髋、踝、肩关节活动", "每个主动作先做 1-2 组轻重量热身"]),
                TrainingStep(order=2, name="深蹲模式", sets=3, repetitions="6-10 次", rest_seconds=(90, 150), intensity=effort, instructions=["深蹲、杯式深蹲或腿举任选一种"]),
                TrainingStep(order=3, name="水平推", sets=3, repetitions="6-10 次", rest_seconds=(90, 150), intensity=effort, instructions=["卧推、哑铃卧推或俯卧撑任选一种"]),
                TrainingStep(order=4, name="水平或垂直拉", sets=3, repetitions="8-12 次", rest_seconds=(90, 150), intensity=effort, instructions=["坐姿划船、单臂划船或高位下拉任选一种"]),
                TrainingStep(order=5, name="髋伸模式", sets=3, repetitions="6-10 次", rest_seconds=(120, 180), intensity=effort, instructions=["罗马尼亚硬拉、壶铃硬拉或臀推任选一种"]),
                TrainingStep(order=6, name="核心稳定", sets=2, repetitions="30-45 秒", rest_seconds=(45, 75), intensity="动作稳定", instructions=["平板支撑、死虫或农夫行走任选一种"]),
                TrainingStep(order=7, name="冷身", duration_minutes=(5, 8), intensity="轻松", instructions=["步行并放松本次主要训练肌群"]),
            ],
            progression=[
                "同一动作所有组都达到次数上限且仍满足余力要求时，下次增加 2.5%-5% 重量",
                "动作质量下降时不加重量，先保持或减少负荷",
            ],
            cautions=["没有记录伤病、器械和训练经验，动作选择需以无痛和可控为前提", "尖锐疼痛或动作失控时立即停止该动作"],
        )

    if code in {"walking", "mobility", "gentle_mobility"}:
        return TrainingPrescription(
            code=code,
            title="恢复性活动",
            goal="促进活动恢复，不额外堆积训练疲劳",
            total_duration_minutes=duration or (20, 40),
            steps=[
                TrainingStep(order=1, name="轻松步行", duration_minutes=(15, 30), intensity="能够轻松交谈"),
                TrainingStep(order=2, name="关节活动", duration_minutes=(5, 10), intensity="轻柔", instructions=["活动髋、踝、胸椎和肩关节", "不追求拉伸疼痛感"]),
            ],
            cautions=["今天不安排冲刺、力竭组或高强度间歇"],
        )

    if code == "rest":
        return TrainingPrescription(
            code=code,
            title="完全休息",
            goal="优先恢复，不增加训练刺激",
            cautions=["只保留日常轻微活动，不进行计划训练"],
        )

    return TrainingPrescription(
        code=code,
        title="按原计划训练",
        goal="在当前建议强度和时长内完成既定训练",
        total_duration_minutes=duration,
        cautions=["当前缺少该项目的专项结构化处方，避免临时增加训练量"],
    )
