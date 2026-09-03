"""Health-first concurrent running and strength planning."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from statistics import median

from vitalis.time import local_timezone

from .contracts import (
    ActionPlan,
    ConcurrentWeeklyBalance,
    ConfidenceBand,
    DecisionAction,
    DecisionEvidence,
    DecisionEvidenceFact,
    DecisionGateEvidence,
    HrvFeatures,
    LoadState,
    PlannedSession,
    RecoveryFeatures,
    RecoveryState,
    SleepFeatures,
    SleepState,
    TrainingDecision,
    TrainingFeatures,
    TrainingPreferences,
    TrainingStep,
)
from .localization import (
    ACTION_LABELS,
    CONFIDENCE_LABELS,
    DRIVER_LABELS,
    LIMITATION_LABELS,
    labels,
)


ROLE_LABELS = {"PRIMARY": "主要训练", "OPTIONAL": "可选训练"}
TYPE_LABELS = {
    "RUNNING": "跑步",
    "STRENGTH": "力量训练",
    "RECOVERY": "恢复活动",
    "REST": "休息",
}
INTENSITY_LABELS = {
    "high": "较高强度",
    "moderate": "中等强度",
    "low": "低强度",
    "none": "不训练",
}


class DecisionEngine:
    def decide(
        self,
        recommendation_id: str,
        day: date,
        sleep: SleepFeatures,
        sleep_state: SleepState,
        hrv: HrvFeatures,
        recovery: RecoveryFeatures,
        training: TrainingFeatures,
        preferences: TrainingPreferences,
    ) -> TrainingDecision:
        limitations = list(dict.fromkeys(
            hrv.limitations + recovery.limitations + training.limitations
        ))
        confidence = self._confidence(recovery, limitations)
        balance = self._balance(training, preferences)
        missing_gates = self._missing_gates(preferences)
        safety_status, safety_label = self._safety(preferences)
        expires_at = datetime.combine(
            day + timedelta(days=1), time.min, tzinfo=local_timezone()
        )

        def plan(
            primary: PlannedSession | None,
            optional: PlannedSession | None = None,
            relationship: str = "NONE",
            conflicts: list[str] | None = None,
        ) -> ActionPlan:
            relationship_labels = {
                "ADDITION": "可分开完成，至少间隔 6 小时",
                "ALTERNATIVE": "二选一，不在同一天叠加",
                "NONE": "没有附加训练",
            }
            return ActionPlan(
                valid_for_date=day,
                expires_at=expires_at,
                safety_status=safety_status,
                safety_status_label=safety_label,
                weekly_balance=balance,
                primary_session=primary,
                optional_session=optional,
                session_relationship=relationship,
                session_relationship_label=relationship_labels[relationship],
                conflict_checks=conflicts or [],
                missing_input_gates=missing_gates,
            )

        def evidence_for(
            drivers: list[str],
            gates: list[DecisionGateEvidence] | None = None,
            extra_facts: list[DecisionEvidenceFact] | None = None,
        ) -> DecisionEvidence:
            return self._decision_evidence(
                sleep,
                hrv,
                training,
                drivers,
                gates or [],
                extra_facts or [],
            )

        if recovery.state == RecoveryState.INSUFFICIENT_DATA:
            return self._decision(
                recommendation_id,
                DecisionAction.INSUFFICIENT_DATA,
                ConfidenceBand.NONE,
                [],
                limitations,
                ["DECISION.REQUIRED_RECOVERY_SIGNALS"],
                evidence_for([], [DecisionGateEvidence(
                    code="DECISION.REQUIRED_RECOVERY_SIGNALS",
                    label="恢复决策所需信号不足",
                    triggered=True,
                    observed_value=recovery.state.value,
                    expected_condition="至少两个可解释的恢复或负荷信号",
                )]),
                plan(None),
            )

        if preferences.pain_or_injury_status == "PRESENT":
            stop = [
                "暂停计划训练；如疼痛持续、加重或影响日常活动，寻求专业评估。"
            ]
            return self._decision(
                recommendation_id,
                DecisionAction.REST,
                confidence,
                ["PAIN_OR_INJURY_PRESENT"],
                limitations,
                ["DECISION.PAIN_OR_INJURY_SAFETY_GATE"],
                evidence_for(
                    ["PAIN_OR_INJURY_PRESENT"],
                    [DecisionGateEvidence(
                        code="DECISION.PAIN_OR_INJURY_SAFETY_GATE",
                        label="疼痛或伤病安全门控已触发",
                        triggered=True,
                        observed_value=preferences.pain_or_injury_status,
                        expected_condition="疼痛或伤病状态不是 PRESENT",
                        observed_at=preferences.updated_at,
                    )],
                    [
                        DecisionEvidenceFact(
                            code="pain_or_injury_status",
                            label="疼痛或伤病状态",
                            value=preferences.pain_or_injury_status,
                        ),
                        DecisionEvidenceFact(
                            code="pain_or_injury_notes",
                            label="疼痛或伤病说明",
                            value=preferences.pain_or_injury_notes,
                        ),
                    ],
                ),
                plan(self._rest("已记录疼痛或伤病", stop)),
            )

        if preferences.available_weekdays and day.isoweekday() not in preferences.available_weekdays:
            return self._decision(
                recommendation_id,
                DecisionAction.RECOVERY,
                confidence,
                ["TRAINING_DAY_UNAVAILABLE"],
                limitations,
                ["DECISION.UNAVAILABLE_TRAINING_DAY"],
                evidence_for(
                    ["TRAINING_DAY_UNAVAILABLE"],
                    [DecisionGateEvidence(
                        code="DECISION.UNAVAILABLE_TRAINING_DAY",
                        label="今天不在设定的可训练日内",
                        triggered=True,
                        observed_value=day.isoweekday(),
                        expected_condition="目标日期属于已设置的可训练日",
                        observed_at=preferences.updated_at,
                    )],
                    [
                        DecisionEvidenceFact(
                            code="target_iso_weekday",
                            label="目标日期星期",
                            value=day.isoweekday(),
                        ),
                        DecisionEvidenceFact(
                            code="available_weekdays",
                            label="已设置的可训练日",
                            value=preferences.available_weekdays,
                        ),
                    ],
                ),
                plan(self._recovery(preferences, "今天不在设定的可训练日内")),
            )

        if recovery.state == RecoveryState.SUPPRESSED:
            severe = (
                "HRV_BELOW_BASELINE" in recovery.negative_signals
                and "RHR_ABOVE_BASELINE" in recovery.negative_signals
                and any(
                    signal in recovery.negative_signals
                    for signal in ("SLEEP_BELOW_BASELINE", "SLEEP_SHORT_DURATION")
                )
            )
            if severe:
                return self._decision(
                    recommendation_id,
                    DecisionAction.REST,
                    confidence,
                    recovery.negative_signals,
                    limitations,
                    ["DECISION.MULTISIGNAL_SUPPRESSION_REST"],
                    evidence_for(recovery.negative_signals, [DecisionGateEvidence(
                        code="DECISION.MULTISIGNAL_SUPPRESSION_REST",
                        label="HRV、静息心率和睡眠信号同时受抑制",
                        triggered=True,
                        observed_value=True,
                        expected_condition="三项严重恢复信号不同时出现",
                    )]),
                    plan(self._rest("多项恢复信号同时受抑制")),
                )
            return self._decision(
                recommendation_id,
                DecisionAction.RECOVERY,
                confidence,
                recovery.negative_signals,
                limitations,
                ["DECISION.MULTISIGNAL_SUPPRESSION_RECOVERY"],
                evidence_for(recovery.negative_signals, [DecisionGateEvidence(
                    code="DECISION.MULTISIGNAL_SUPPRESSION_RECOVERY",
                    label="多个恢复信号提示降低训练负担",
                    triggered=True,
                    observed_value=len(recovery.negative_signals),
                    expected_condition="少于两个负向恢复信号",
                )]),
                plan(self._recovery(preferences, "当前恢复状态受抑制")),
            )

        conflicts = self._conflicts(day, training)
        action, action_drivers = self._training_action(sleep_state, hrv, recovery, training)
        intensity = {
            DecisionAction.TRAIN_HARD: "high",
            DecisionAction.TRAIN_NORMAL: "moderate",
            DecisionAction.TRAIN_LIGHT: "low",
        }[action]
        primary_type = self._primary_type(training, balance, preferences)
        primary, optional, relationship = self._sessions(
            day, primary_type, intensity, training, preferences, balance, conflicts
        )
        rule = {
            DecisionAction.TRAIN_HARD: "DECISION.CONCURRENT_GOOD_RECOVERY",
            DecisionAction.TRAIN_NORMAL: "DECISION.CONCURRENT_BALANCE",
            DecisionAction.TRAIN_LIGHT: "DECISION.CONCURRENT_LOAD_REDUCTION",
        }[action]
        drivers = list(recovery.positive_signals + recovery.negative_signals) or ["RECOVERY_NORMAL"]
        drivers.extend(action_drivers)
        if training.load_state == LoadState.ELEVATED:
            drivers.append("TRAINING_LOAD_ELEVATED")
        elif training.load_state == LoadState.LOW:
            drivers.append("TRAINING_LOAD_LOW")
        selected_drivers = list(dict.fromkeys(drivers))
        return self._decision(
            recommendation_id,
            action,
            confidence,
            selected_drivers,
            limitations,
            [rule],
            evidence_for(selected_drivers, [DecisionGateEvidence(
                code=rule,
                label="训练动作规则已匹配",
                triggered=True,
                observed_value=action.value,
                expected_condition="采用与恢复和负荷状态匹配的训练剂量",
            )]),
            plan(primary, optional, relationship, conflicts),
        )

    @staticmethod
    def _evidence_fact(code, label, value, unit, deviation=None, direction=None):
        return DecisionEvidenceFact(
            code=code,
            label=label,
            value=value,
            unit=unit,
            baseline_reference=deviation.baseline_reference if deviation else None,
            baseline_window_days=deviation.baseline_window_days if deviation else None,
            deviation_percent=deviation.percent if deviation else None,
            robust_z=deviation.robust_z if deviation else None,
            direction=direction or (deviation.direction if deviation else None),
            source=deviation.source if deviation else None,
            source_scope=deviation.source_scope if deviation else None,
            device_id=deviation.device_id if deviation else None,
        )

    @classmethod
    def _decision_evidence(cls, sleep, hrv, training, drivers, gates, extra_facts):
        facts = list(extra_facts)
        for driver in drivers:
            label = DRIVER_LABELS.get(driver, driver)
            if driver in {"HRV_ABOVE_BASELINE", "HRV_BELOW_BASELINE"}:
                facts.append(cls._evidence_fact(
                    driver,
                    label,
                    hrv.value_ms,
                    "毫秒",
                    hrv.deviation,
                    hrv.fusion_direction,
                ))
            elif driver in {"RHR_BELOW_BASELINE", "RHR_ABOVE_BASELINE"}:
                facts.append(cls._evidence_fact(
                    driver, label, hrv.rhr_bpm, "次/分钟", hrv.rhr_deviation
                ))
            elif driver in {
                "SLEEP_ABOVE_BASELINE",
                "SLEEP_BELOW_BASELINE",
                "SLEEP_SHORT_DURATION",
            }:
                facts.append(cls._evidence_fact(
                    driver,
                    label,
                    sleep.duration_minutes,
                    "分钟",
                    sleep.duration_deviation,
                ))
            elif driver in {"TRAINING_LOAD_ELEVATED", "TRAINING_LOAD_LOW"}:
                fact = cls._evidence_fact(
                    driver, label, training.load_7d, "负荷", training.load_deviation
                )
                if fact.baseline_reference is None:
                    fact.baseline_reference = training.load_7d_reference
                if fact.deviation_percent is None:
                    fact.deviation_percent = training.load_7d_change_percent
                facts.append(fact)
            elif driver == "RECOVERY_NORMAL":
                if hrv.fusion_direction == "near" and hrv.value_ms is not None:
                    facts.append(cls._evidence_fact(
                        "HRV_NEAR_BASELINE",
                        "HRV 接近个人基线",
                        hrv.value_ms,
                        "毫秒",
                        hrv.deviation,
                        hrv.fusion_direction,
                    ))
                if hrv.rhr_deviation and hrv.rhr_deviation.direction == "near":
                    facts.append(cls._evidence_fact(
                        "RHR_NEAR_BASELINE",
                        "静息心率接近个人基线",
                        hrv.rhr_bpm,
                        "次/分钟",
                        hrv.rhr_deviation,
                    ))
                if sleep.duration_deviation and sleep.duration_deviation.direction == "near":
                    facts.append(cls._evidence_fact(
                        "SLEEP_NEAR_BASELINE",
                        "睡眠接近个人基线",
                        sleep.duration_minutes,
                        "分钟",
                        sleep.duration_deviation,
                    ))
                if training.load_state == LoadState.NORMAL:
                    facts.append(cls._evidence_fact(
                        "TRAINING_LOAD_NORMAL",
                        "近期训练负荷处于正常范围",
                        training.load_7d,
                        "负荷",
                        training.load_deviation,
                    ))
            elif driver == "HRV_RECENT_7D_BELOW":
                facts.append(DecisionEvidenceFact(
                    code=driver,
                    label=label,
                    value=hrv.recent_7d_median_ms,
                    unit="毫秒",
                    baseline_reference=hrv.previous_7d_median_ms,
                    baseline_window_days=7,
                    deviation_percent=hrv.recent_7d_change_percent,
                    direction=hrv.recent_7d_direction,
                ))
        unique = {fact.code: fact for fact in facts}
        return DecisionEvidence(facts=list(unique.values()), gates=gates)

    @staticmethod
    def _confidence(recovery, limitations) -> ConfidenceBand:
        signal_count = len(recovery.positive_signals) + len(recovery.negative_signals)
        if recovery.state == RecoveryState.INSUFFICIENT_DATA:
            return ConfidenceBand.NONE
        if "multi_device_hrv_disagreement" in limitations:
            return ConfidenceBand.LOW
        if signal_count >= 3 and not any(
            "baseline_insufficient" in item for item in limitations
        ):
            return ConfidenceBand.HIGH
        return ConfidenceBand.MODERATE

    @staticmethod
    def _balance(training, preferences) -> ConcurrentWeeklyBalance:
        running = training.running
        strength = training.strength
        running_7d = running.sessions_7d if running else 0
        running_28d = running.sessions_28d if running else 0
        strength_7d = strength.sessions_7d if strength else 0
        strength_28d = strength.sessions_28d if strength else 0
        run_target = preferences.weekly_running_target
        strength_target = preferences.weekly_strength_target
        return ConcurrentWeeklyBalance(
            running_completed_7d=running_7d,
            running_target_7d=run_target,
            running_completed_28d=running_28d,
            running_target_28d=run_target * 4,
            strength_completed_7d=strength_7d,
            strength_target_7d=strength_target,
            strength_completed_28d=strength_28d,
            strength_target_28d=strength_target * 4,
            running_due=running_7d < run_target or running_28d < run_target * 4,
            strength_due=strength_7d < strength_target or strength_28d < strength_target * 4,
        )

    @staticmethod
    def _missing_gates(preferences: TrainingPreferences) -> list[str]:
        gates = []
        if not preferences.available_weekdays:
            gates.append("未设置每周可训练日，只规划今天，不生成固定周历。")
        if preferences.max_session_minutes is None:
            gates.append("未设置单次可用时长，采用 60 分钟以内的保守剂量。")
        if preferences.running_experience is None:
            gates.append("未设置跑步经验，不安排高风险速度或最大努力测试。")
        if preferences.strength_experience is None:
            gates.append("未设置力量经验，不给出绝对重量或力竭训练。")
        if not preferences.equipment:
            gates.append("未设置可用器械，力量动作以模式和可替换选项表达。")
        if preferences.pain_or_injury_status == "UNKNOWN":
            gates.append("伤病与疼痛状态未确认；出现疼痛时停止对应训练。")
        return gates

    @staticmethod
    def _safety(preferences: TrainingPreferences) -> tuple[str, str]:
        if preferences.pain_or_injury_status == "PRESENT":
            return "LIMITED", "存在疼痛或伤病限制"
        if preferences.pain_or_injury_status == "UNKNOWN":
            return "UNKNOWN", "伤病与疼痛状态未确认"
        return "CLEAR", "未记录疼痛或伤病限制"

    @staticmethod
    def _training_action(
        sleep_state, hrv, recovery, training
    ) -> tuple[DecisionAction, list[str]]:
        if recovery.state == RecoveryState.NORMAL and training.load_state == LoadState.ELEVATED:
            return DecisionAction.TRAIN_LIGHT, []
        if (
            hrv.recent_7d_direction == "below"
            and hrv.fusion_direction != "above"
        ):
            return DecisionAction.TRAIN_LIGHT, ["HRV_RECENT_7D_BELOW"]
        if (
            recovery.state == RecoveryState.GOOD
            and sleep_state == SleepState.ABOVE_BASELINE
            and training.load_state == LoadState.LOW
        ):
            return DecisionAction.TRAIN_HARD, []
        return DecisionAction.TRAIN_NORMAL, []

    @staticmethod
    def _primary_type(training, balance, preferences) -> str:
        latest = training.recent_workouts[0].training_family if training.recent_workouts else None
        if preferences.rotation_policy == "ALTERNATE" and latest in {"aerobic", "strength"}:
            return "STRENGTH" if latest == "aerobic" else "RUNNING"
        if balance.running_due and not balance.strength_due:
            return "RUNNING"
        if balance.strength_due and not balance.running_due:
            return "STRENGTH"
        run_gap = (
            balance.running_target_7d - balance.running_completed_7d
        ) / balance.running_target_7d
        strength_gap = (
            balance.strength_target_7d - balance.strength_completed_7d
        ) / balance.strength_target_7d
        if run_gap != strength_gap:
            return "RUNNING" if run_gap > strength_gap else "STRENGTH"
        return "STRENGTH" if latest == "aerobic" else "RUNNING"

    def _sessions(
        self, day, primary_type, intensity, training, preferences, balance, conflicts
    ):
        focus = training.strength.next_focus if training.strength else None
        lower_focus = focus in {"LEGS", "LOWER"}
        if primary_type == "RUNNING":
            quality_allowed = (
                intensity == "high"
                and "近 48 小时已有腿部力量训练，今天不安排高强度跑。" not in conflicts
                and not self._quality_run_this_week(day, training)
            )
            primary = self._running(day, preferences, training, "PRIMARY", quality_allowed, intensity)
            if balance.strength_due:
                optional = self._strength(
                    preferences,
                    training,
                    "OPTIONAL",
                    "low" if intensity == "low" else "moderate",
                )
                relationship = "ALTERNATIVE" if quality_allowed or lower_focus else "ADDITION"
            else:
                optional = self._recovery(preferences, "跑步后的低负担恢复", "OPTIONAL")
                relationship = "ADDITION"
            self._add_rotation_reason(primary, training, preferences)
            return primary, optional, relationship

        primary = self._strength(preferences, training, "PRIMARY", intensity)
        if balance.running_due:
            optional = self._running(day, preferences, training, "OPTIONAL", False, "low")
            relationship = "ALTERNATIVE" if lower_focus else "ADDITION"
        else:
            optional = self._recovery(preferences, "力量训练后的低负担恢复", "OPTIONAL")
            relationship = "ADDITION"
        self._add_rotation_reason(primary, training, preferences)
        return primary, optional, relationship

    @staticmethod
    def _add_rotation_reason(primary, training, preferences) -> None:
        if preferences.rotation_policy != "ALTERNATE" or not training.recent_workouts:
            return
        latest = training.recent_workouts[0].training_family
        if latest == "aerobic" and primary.session_type == "STRENGTH":
            reason = "最近一次正式训练是跑步，按你的跑步与力量轮换偏好安排力量训练。"
        elif latest == "strength" and primary.session_type == "RUNNING":
            reason = "最近一次正式训练是力量，按你的跑步与力量轮换偏好安排跑步。"
        else:
            return
        primary.personalization_reasons.insert(0, reason)

    @staticmethod
    def _conflicts(day: date, training: TrainingFeatures) -> list[str]:
        conflicts = []
        if training.running:
            hard = {"TEMPO_RUN", "INTERVAL_RUN", "LONG_RUN"}
            if any(
                session.classification in hard and (day - session.date).days <= 1
                for session in training.running.recent_sessions
            ):
                conflicts.append("近 48 小时已有高负荷跑步，腿部力量不与其连续堆叠。")
        if training.strength:
            if any(
                session.focus in {"LEGS", "LOWER"} and (day - session.date).days <= 1
                for session in training.strength.recent_sessions
            ):
                conflicts.append("近 48 小时已有腿部力量训练，今天不安排高强度跑。")
            if any(
                item.latest_soreness is not None and item.latest_soreness >= 4
                and item.muscle_group in {"quadriceps", "hamstrings", "glutes", "calves"}
                for item in training.strength.muscle_recovery
            ):
                conflicts.append("已记录下肢明显酸痛，跑步与腿部力量都降低剂量。")
        if not conflicts:
            conflicts.append("未发现高强度跑与腿部力量在 48 小时内冲突。")
        return conflicts

    @staticmethod
    def _quality_run_this_week(day: date, training: TrainingFeatures) -> bool:
        if not training.running:
            return False
        hard = {"TEMPO_RUN", "INTERVAL_RUN", "LONG_RUN"}
        return any(
            item.classification in hard and item.date >= day - timedelta(days=6)
            for item in training.running.recent_sessions
        )

    def _running(
        self, day, preferences, training, role: str, quality: bool, intensity: str
    ) -> PlannedSession:
        running = training.running
        threshold = running.lactate_threshold_bpm if running else None
        recent = running.recent_sessions if running else []
        durations = [item.duration_minutes for item in recent if item.duration_minutes >= 15]
        typical = int(round(median(durations))) if durations else 35
        latest = recent[0] if recent else None
        recent_hard = any(
            item.classification in {"TEMPO_RUN", "INTERVAL_RUN", "LONG_RUN"}
            and (day - item.date).days <= 2
            for item in recent
        )
        high_drift = bool(
            latest and latest.cardiac_drift_percent is not None
            and (day - latest.date).days <= 7
            and latest.classification in {"RECOVERY_RUN", "EASY_RUN"}
            and latest.confidence in {ConfidenceBand.MODERATE, ConfidenceBand.HIGH}
            and latest.cardiac_drift_percent > 5
        )
        established = len(durations) >= 4
        quality = (
            quality
            and threshold is not None
            and not recent_hard
            and not high_drift
            and (preferences.max_session_minutes or 60) >= 40
        )
        long_due = (
            intensity != "low" and established and not recent_hard
            and not any(
                item.classification == "LONG_RUN" and (day - item.date).days <= 13
                for item in recent
            )
        )
        reasons = []
        if high_drift:
            reasons.append(
                f"最近一次可解释的心率漂移为 {latest.cardiac_drift_percent:+.1f}%，"
                "本次因此缩短并保持轻松。"
            )
        elif recent_hard:
            reasons.append("近 72 小时已有质量跑或长跑，本次因此降低刺激。")
        if latest:
            detail = f"最近一次跑步为{latest.classification_label}，{latest.duration_minutes} 分钟"
            if latest.average_pace_seconds_per_km is not None:
                pace_seconds = int(round(latest.average_pace_seconds_per_km))
                detail += f"，平均配速 {pace_seconds // 60}:{pace_seconds % 60:02d}/公里"
            if latest.median_cadence_spm is not None:
                detail += f"，自然步频约 {latest.median_cadence_spm:.0f} 步/分钟"
            reasons.append(f"{detail}。")
            if latest.cardiac_drift_percent is not None and not high_drift:
                reasons.append(f"最近一次可解释的心率漂移为 {latest.cardiac_drift_percent:+.1f}%。")

        if quality:
            duration = self._duration_around(preferences, typical, 0.95, 1.15, 40, 60)
            planned_intensity = "high"
            code = "threshold_intervals"
            title = "阈值间歇跑"
            focus = "今天安排本周质量跑；工作段受控，不做冲刺"
            main_intensity = f"工作段约 {round(threshold * 0.95)}–{round(threshold) - 1} 次/分钟"
            steps = [
                TrainingStep(order=1, name="热身", duration_minutes=(10, 12), intensity="轻松", instructions=["从快走逐渐过渡到慢跑"]),
                TrainingStep(order=2, name="阈值工作段", sets=3, repetitions="每组 6 分钟", rest_seconds=(120, 120), intensity=main_intensity, instructions=["组间慢跑恢复", "最后一组不冲刺"]),
                TrainingStep(order=3, name="冷身", duration_minutes=(8, 10), intensity="轻松"),
            ]
            reasons.append("恢复和训练间隔允许，且本周尚无质量跑。")
        elif intensity == "low" or recent_hard or high_drift:
            duration = self._duration_around(preferences, typical, 0.65, 0.8, 20, 35)
            planned_intensity = "low"
            code = "recovery_run"
            title = "恢复跑"
            focus = "缩短跑量，用很轻松的强度维持跑步节奏"
            steps = self._continuous_run_steps(duration, "恢复跑", self._easy_zone(threshold, True), latest)
        elif long_due:
            duration = self._duration_around(preferences, typical, 1.15, 1.3, 45, 75)
            planned_intensity = "low"
            code = "long_easy_run"
            title = "长距离轻松跑"
            focus = "在近期个人时长基础上小幅延长，全程保持低强度"
            steps = self._continuous_run_steps(duration, "长距离轻松跑", self._easy_zone(threshold), latest)
            reasons.append(f"近期跑步时长中位数约 {typical} 分钟，且近两周没有长跑。")
        elif established and intensity == "moderate" and latest and latest.classification in {"RECOVERY_RUN", "EASY_RUN"}:
            duration = self._duration_around(preferences, typical, 0.9, 1.1, 30, 55)
            planned_intensity = "moderate"
            code = "steady_run"
            title = "稳定跑"
            focus = "在可控呼吸下连续完成，不进入阈值强度"
            zone = (
                f"主体约 {round(threshold * 0.90)}–{round(threshold * 0.95) - 1} 次/分钟"
                if threshold else "主观用力约 5–6/10，呼吸加深但可控"
            )
            steps = self._continuous_run_steps(duration, "稳定跑", zone, latest)
        else:
            duration = self._duration_around(preferences, typical, 0.85, 1.05, 25, 55)
            planned_intensity = "low"
            code = "easy_run"
            title = "轻松跑"
            focus = "按近期可完成的时长维持低强度有氧"
            steps = self._continuous_run_steps(duration, "轻松跑", self._easy_zone(threshold), latest)
        return PlannedSession(
            role=role,
            role_label=ROLE_LABELS[role],
            session_type="RUNNING",
            session_type_label=TYPE_LABELS["RUNNING"],
            code=code,
            title=title,
            focus=focus,
            goal="在健康优先前提下维持跑步连续性",
            intensity=planned_intensity,
            intensity_label=INTENSITY_LABELS[planned_intensity],
            total_duration_minutes=duration,
            steps=steps,
            evidence=[
                f"近 7 天完成跑步 {running.sessions_7d if running else 0} 次。",
                f"近 28 天完成跑步 {running.sessions_28d if running else 0} 次。",
            ],
            evidence_ref_ids=["WHO_PHYSICAL_ACTIVITY", "CONCURRENT_TRAINING_2022"],
            personalization_reasons=reasons[:3],
            progression=["本次完成轻松且次日恢复正常时，下次只增加时长或强度其中一项。"],
            stop_conditions=["出现胸痛、异常气短、头晕、步态改变或持续疼痛时立即停止。"],
        )

    @staticmethod
    def _easy_zone(threshold: float | None, recovery: bool = False) -> str:
        if threshold is None:
            return "能连续说完整句子；不使用推测心率区间"
        upper_ratio = 0.85 if recovery else 0.90
        lower_ratio = 0.75 if recovery else 0.85
        return f"约 {round(threshold * lower_ratio)}–{round(threshold * upper_ratio) - 1} 次/分钟"

    @staticmethod
    def _continuous_run_steps(duration, name: str, zone: str, latest):
        low, high = duration
        warm_low, warm_high = 6, min(10, max(6, low // 5))
        cool_low, cool_high = 5, min(8, max(5, low // 6))
        main_low = max(10, low - warm_high - cool_high)
        main_high = max(main_low, high - warm_low - cool_low)
        instructions = ["保持能连续说出完整短句", "步频自然，不追求统一目标"]
        if latest and latest.median_cadence_spm is not None:
            instructions[-1] = f"沿用你最近自然步频约 {latest.median_cadence_spm:.0f} 步/分钟，不刻意提频"
        return [
            TrainingStep(order=1, name="热身", duration_minutes=(warm_low, warm_high), intensity="轻松", instructions=["快走后逐渐进入慢跑"]),
            TrainingStep(order=2, name=name, duration_minutes=(main_low, main_high), intensity=zone, instructions=instructions),
            TrainingStep(order=3, name="冷身", duration_minutes=(cool_low, cool_high), intensity="轻松"),
        ]

    @staticmethod
    def _duration_around(preferences, typical, low_ratio, high_ratio, minimum, maximum):
        cap = preferences.max_session_minutes or 60
        low = max(minimum, int(round(typical * low_ratio)))
        high = max(low, int(round(typical * high_ratio)))
        high = min(high, maximum, cap)
        low = min(low, high)
        return low, high

    def _strength(self, preferences, training, role: str, intensity: str) -> PlannedSession:
        focus = training.strength.next_focus if training.strength else None
        focus = focus or "FULL_BODY"
        prior = next(
            (
                item for item in (training.strength.recent_sessions if training.strength else [])
                if item.focus == focus and item.explicit_exercises
            ),
            None,
        )
        title, focus_text, steps = (
            self._strength_from_prior(prior, intensity)
            if prior else self._strength_steps(focus, intensity)
        )
        planned_intensity = "moderate" if intensity == "high" else intensity
        reasons = []
        if prior:
            reasons.append(
                f"沿用最近一次{prior.focus_label}训练中已记录的 {len(prior.explicit_exercises)} 个动作。"
            )
            if prior.session_rpe is not None:
                reasons.append(f"上次整堂训练主观用力为 {prior.session_rpe:.1f}/10。")
        elif training.strength and training.strength.next_focus_label:
            reasons.append(f"按已识别的训练轮换，下一项是{training.strength.next_focus_label}。")
        else:
            reasons.append("近期没有可复用的已确认动作，按全身动作模式安排。")
            latest_strength = (
                training.strength.recent_sessions[0]
                if training.strength and training.strength.recent_sessions else None
            )
            if latest_strength and latest_strength.estimated_work_bouts is not None:
                planned_sets = sum(step.sets or 0 for step in steps)
                reasons.append(
                    f"最近一次力量训练约有 {latest_strength.estimated_work_bouts} 个工作段；"
                    f"心率无法识别动作，本次明确安排 {planned_sets} 个工作组。"
                )
        return PlannedSession(
            role=role,
            role_label=ROLE_LABELS[role],
            session_type="STRENGTH",
            session_type_label=TYPE_LABELS["STRENGTH"],
            code=f"strength_{focus.lower()}",
            title=title,
            focus=focus_text,
            goal="维持主要动作模式和肌群训练频次，同时不给跑步关键课堆叠下肢疲劳",
            intensity=planned_intensity,
            intensity_label=INTENSITY_LABELS[planned_intensity],
            total_duration_minutes=self._duration(preferences, (40, 60)),
            steps=steps,
            evidence=[
                f"近 7 天完成力量训练 {training.strength.sessions_7d if training.strength else 0} 次。",
                f"近 28 天完成力量训练 {training.strength.sessions_28d if training.strength else 0} 次。",
                (
                    f"已识别训练结构：{training.strength.detected_split_label}，下一重点为 {training.strength.next_focus_label}。"
                    if training.strength and training.strength.next_focus
                    else "训练分化证据不足，本次采用全身基础动作模式。"
                ),
            ],
            evidence_ref_ids=[
                "ACSM_RESISTANCE_TRAINING_2026",
                "CONCURRENT_TRAINING_2022",
                "RIR_RPE_SCALE_2016",
                "RIR_ACCURACY_REVIEW_2026",
            ],
            personalization_reasons=reasons,
            progression=["所有工作组达到次数上限且仍满足余力要求时，下次增加少量重量或每组 1–2 次。"],
            stop_conditions=["出现尖锐疼痛、动作失控、异常头晕或胸闷时立即停止。", "无法保持目标余力时减少重量或结束该动作。"],
        )

    @staticmethod
    def _strength_from_prior(prior, intensity: str):
        target_rir = "每组保留 2 次余力" if intensity in {"high", "moderate"} else "每组保留 3–4 次余力"
        title = f"{prior.focus_label}训练"
        steps = [
            TrainingStep(
                order=1,
                name="动态热身",
                duration_minutes=(6, 8),
                intensity="轻松",
                instructions=["为第一个主动作做 2 组逐步加重的热身"],
            )
        ]
        seen = set()
        for exercise in prior.explicit_exercises:
            key = (exercise.exercise_id or exercise.exercise_name).lower()
            if key in seen:
                continue
            seen.add(key)
            instructions = []
            previous_effort = exercise.rir
            if previous_effort is not None:
                instructions.append(f"上次记录还可做 {previous_effort:g} 次")
            elif exercise.rpe is not None:
                instructions.append(f"上次记录主观用力 {exercise.rpe:g}/10")
            if exercise.rir is not None and exercise.rir <= 1 or exercise.rpe is not None and exercise.rpe >= 9:
                instructions.append("本次保持或略降重量，不做进阶")
            elif exercise.rir is not None and exercise.rir >= 3 or exercise.rpe is not None and exercise.rpe <= 7:
                instructions.append("动作稳定时可增加少量重量或每组 1 次")
            steps.append(TrainingStep(
                order=len(steps) + 1,
                name=exercise.exercise_name,
                sets=exercise.sets or 3,
                repetitions=exercise.repetitions or "6–12 次",
                load_kg=exercise.weight_kg,
                rest_seconds=(exercise.rest_seconds, exercise.rest_seconds) if exercise.rest_seconds is not None else (90, 150),
                intensity=target_rir,
                instructions=instructions,
            ))
        return title, f"沿用上次已确认的{prior.focus_label}动作和剂量", steps

    @staticmethod
    def _strength_steps(focus: str, intensity: str):
        rir = "每组保留 2 次余力" if intensity in {"high", "moderate"} else "每组保留 3–4 次余力"
        common = [TrainingStep(order=1, name="动态热身", duration_minutes=(6, 8), intensity="轻松", instructions=["活动髋、踝、肩，并为首个主动作做 2 组递增热身"])]
        templates = {
            "PUSH": ("推类力量训练", "胸、肩和肱三头肌", [("水平推", 3, "6–10 次", 120), ("垂直推", 3, "8–12 次", 120), ("肱三头肌", 2, "10–15 次", 75)]),
            "PULL": ("拉类力量训练", "背部和肱二头肌", [("水平拉", 3, "6–10 次", 120), ("垂直拉", 3, "8–12 次", 120), ("肱二头肌", 2, "10–15 次", 75)]),
            "LEGS": ("腿部力量训练", "股四头肌、臀部和腘绳肌", [("下蹲模式", 3, "6–10 次", 150), ("髋伸模式", 3, "6–10 次", 150), ("单腿模式", 2, "每侧 8–12 次", 90), ("核心稳定", 2, "30–45 秒", 60)]),
            "LOWER": ("下肢力量训练", "下肢主要动作模式", [("下蹲模式", 3, "6–10 次", 150), ("髋伸模式", 3, "6–10 次", 150), ("单腿模式", 2, "每侧 8–12 次", 90)]),
            "UPPER": ("上肢力量训练", "推拉平衡", [("水平推", 3, "6–10 次", 120), ("水平拉", 3, "6–10 次", 120), ("垂直推或拉", 2, "8–12 次", 90)]),
            "CHEST": ("胸部力量训练", "胸部为主，兼顾肱三头肌", [("水平推主动作", 4, "6–10 次", 150), ("上斜推或俯卧撑", 3, "8–12 次", 120), ("胸部孤立动作", 2, "10–15 次", 75)]),
            "BACK": ("背部力量训练", "背部为主，兼顾肱二头肌", [("水平拉主动作", 4, "6–10 次", 150), ("垂直拉", 3, "8–12 次", 120), ("肱二头肌", 2, "10–15 次", 75)]),
            "SHOULDERS": ("肩部力量训练", "肩部和肩胛控制", [("垂直推", 3, "6–10 次", 120), ("侧向抬举", 3, "10–15 次", 75), ("后束或肩胛稳定", 2, "10–15 次", 75)]),
            "ARMS": ("手臂力量训练", "肱二头肌和肱三头肌", [("肱二头肌", 3, "8–12 次", 75), ("肱三头肌", 3, "8–12 次", 75), ("轻量补充组", 2, "12–15 次", 60)]),
            "FULL_BODY": ("全身力量训练", "蹲、推、拉、髋伸和核心", [("下蹲模式", 3, "6–10 次", 150), ("水平推", 3, "6–10 次", 120), ("水平或垂直拉", 3, "8–12 次", 120), ("髋伸模式", 2, "6–10 次", 150), ("核心稳定", 2, "30–45 秒", 60)]),
        }
        title, focus_text, items = templates.get(focus, templates["FULL_BODY"])
        steps = list(common)
        for order, (name, sets, repetitions, rest) in enumerate(items, start=2):
            steps.append(TrainingStep(
                order=order,
                name=name,
                sets=sets,
                repetitions=repetitions,
                rest_seconds=(rest, rest),
                intensity=rir,
                instructions=["选择当前器械条件下无痛、可稳定控制的对应动作"],
            ))
        return title, focus_text, steps

    def _recovery(self, preferences, reason: str, role: str = "PRIMARY") -> PlannedSession:
        return PlannedSession(
            role=role,
            role_label=ROLE_LABELS[role],
            session_type="RECOVERY",
            session_type_label=TYPE_LABELS["RECOVERY"],
            code="recovery_activity",
            title="恢复性活动",
            focus="降低额外疲劳",
            goal=reason,
            intensity="low",
            intensity_label=INTENSITY_LABELS["low"],
            total_duration_minutes=self._duration(preferences, (20, 35)),
            steps=[
                TrainingStep(order=1, name="轻松步行", duration_minutes=(15, 25), intensity="能够轻松交谈"),
                TrainingStep(order=2, name="关节活动", duration_minutes=(5, 10), intensity="轻柔"),
            ],
            evidence=[reason],
            stop_conditions=["活动使症状或疲劳加重时停止。"],
        )

    @staticmethod
    def _rest(reason: str, stop_conditions: list[str] | None = None) -> PlannedSession:
        return PlannedSession(
            role="PRIMARY",
            role_label=ROLE_LABELS["PRIMARY"],
            session_type="REST",
            session_type_label=TYPE_LABELS["REST"],
            code="rest",
            title="完全休息",
            focus="恢复",
            goal=reason,
            intensity="none",
            intensity_label=INTENSITY_LABELS["none"],
            evidence=[reason],
            stop_conditions=stop_conditions or ["如有明显不适或异常症状，寻求专业评估。"],
        )

    @staticmethod
    def _duration(preferences: TrainingPreferences, default: tuple[int, int]):
        cap = preferences.max_session_minutes or 60
        return min(default[0], cap), min(default[1], cap)

    @staticmethod
    def _decision(
        recommendation_id,
        action,
        confidence,
        drivers,
        limitations,
        rule_ids,
        evidence,
        action_plan,
    ) -> TrainingDecision:
        return TrainingDecision(
            recommendation_id=recommendation_id,
            action=action,
            action_label=ACTION_LABELS[action.value],
            confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
            drivers=drivers,
            driver_labels=labels(drivers, DRIVER_LABELS),
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
            rule_ids=rule_ids,
            evidence=evidence,
            action_plan=action_plan,
        )
