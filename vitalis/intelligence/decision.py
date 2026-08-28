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
)


class DecisionEngine:
    def decide(
        self,
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
            return TrainingDecision(
                action=DecisionAction.INSUFFICIENT_DATA,
                confidence=ConfidenceBand.NONE,
                limitations=limitations,
                rule_ids=["DECISION.REQUIRED_RECOVERY_SIGNALS"],
                intensity="undetermined",
            )

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
                return TrainingDecision(
                    action=DecisionAction.REST,
                    confidence=confidence,
                    drivers=recovery.negative_signals,
                    limitations=limitations,
                    rule_ids=["DECISION.MULTISIGNAL_SUPPRESSION_REST"],
                    suggested_types=["rest", "gentle_mobility"],
                    intensity="none",
                    duration_minutes=None,
                )
            return TrainingDecision(
                action=DecisionAction.RECOVERY,
                confidence=confidence,
                drivers=recovery.negative_signals,
                limitations=limitations,
                rule_ids=["DECISION.MULTISIGNAL_SUPPRESSION_RECOVERY"],
                suggested_types=["walking", "mobility"],
                intensity="low",
                duration_minutes=(20, 40),
            )

        if recovery.state == RecoveryState.NORMAL and training.load_state == LoadState.ELEVATED:
            return TrainingDecision(
                action=DecisionAction.TRAIN_LIGHT,
                confidence=confidence,
                drivers=["RECOVERY_NORMAL", "TRAINING_LOAD_ELEVATED"],
                limitations=limitations,
                rule_ids=["DECISION.ELEVATED_LOAD_LIGHT"],
                suggested_types=["zone2", "mobility"],
                intensity="low",
                duration_minutes=(30, 45),
            )

        if (
            recovery.state == RecoveryState.GOOD
            and sleep_state == SleepState.ABOVE_BASELINE
            and training.load_state == LoadState.LOW
        ):
            return TrainingDecision(
                action=DecisionAction.TRAIN_HARD,
                confidence=confidence,
                drivers=recovery.positive_signals + ["TRAINING_LOAD_LOW"],
                limitations=limitations,
                rule_ids=["DECISION.GOOD_RECOVERY_LOW_LOAD"],
                suggested_types=_training_types(training),
                intensity="high",
                duration_minutes=(45, 60),
            )

        return TrainingDecision(
            action=DecisionAction.TRAIN_NORMAL,
            confidence=confidence,
            drivers=recovery.positive_signals or ["RECOVERY_NORMAL"],
            limitations=limitations,
            rule_ids=["DECISION.NORMAL_TRAINING"],
            suggested_types=_training_types(training),
            intensity="moderate",
            duration_minutes=(45, 60),
        )


def _training_types(training: TrainingFeatures) -> list[str]:
    suggestions = []
    if training.strength_sessions_7d is not None and training.strength_sessions_7d < 2:
        suggestions.append("resistance")
    if training.aerobic_minutes_7d is not None and training.aerobic_minutes_7d < 150:
        suggestions.append("zone2")
    return suggestions or ["planned_session"]
