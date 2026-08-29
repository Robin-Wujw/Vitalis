"""Health-first concurrent running and strength planning."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from vitalis.time import local_timezone

from .contracts import (
    ActionPlan,
    ConcurrentWeeklyBalance,
    ConfidenceBand,
    DecisionAction,
    HrvFeatures,
    LoadState,
    PlannedSession,
    RecoveryFeatures,
    RecoveryState,
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

        if recovery.state == RecoveryState.INSUFFICIENT_DATA:
            return self._decision(
                recommendation_id,
                DecisionAction.INSUFFICIENT_DATA,
                ConfidenceBand.NONE,
                [],
                limitations,
                ["DECISION.REQUIRED_RECOVERY_SIGNALS"],
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
                recovery.negative_signals,
                limitations,
                ["DECISION.PAIN_OR_INJURY_SAFETY_GATE"],
                plan(self._rest("已记录疼痛或伤病", stop)),
            )

        if preferences.available_weekdays and day.isoweekday() not in preferences.available_weekdays:
            return self._decision(
                recommendation_id,
                DecisionAction.RECOVERY,
                confidence,
                recovery.positive_signals or ["RECOVERY_NORMAL"],
                limitations,
                ["DECISION.UNAVAILABLE_TRAINING_DAY"],
                plan(self._recovery(preferences, "今天不在设定的可训练日内")),
            )

        if recovery.state == RecoveryState.SUPPRESSED:
            severe = {
                "HRV_BELOW_BASELINE",
                "RHR_ABOVE_BASELINE",
                "SLEEP_BELOW_BASELINE",
            }.issubset(recovery.negative_signals)
            if severe:
                return self._decision(
                    recommendation_id,
                    DecisionAction.REST,
                    confidence,
                    recovery.negative_signals,
                    limitations,
                    ["DECISION.MULTISIGNAL_SUPPRESSION_REST"],
                    plan(self._rest("多项恢复信号同时受抑制")),
                )
            return self._decision(
                recommendation_id,
                DecisionAction.RECOVERY,
                confidence,
                recovery.negative_signals,
                limitations,
                ["DECISION.MULTISIGNAL_SUPPRESSION_RECOVERY"],
                plan(self._recovery(preferences, "当前恢复状态受抑制")),
            )

        conflicts = self._conflicts(day, training)
        action = self._training_action(sleep_state, recovery, training)
        intensity = {
            DecisionAction.TRAIN_HARD: "high",
            DecisionAction.TRAIN_NORMAL: "moderate",
            DecisionAction.TRAIN_LIGHT: "low",
        }[action]
        primary_type = self._primary_type(training, balance)
        primary, optional, relationship = self._sessions(
            day, primary_type, intensity, training, preferences, balance, conflicts
        )
        rule = {
            DecisionAction.TRAIN_HARD: "DECISION.CONCURRENT_GOOD_RECOVERY",
            DecisionAction.TRAIN_NORMAL: "DECISION.CONCURRENT_BALANCE",
            DecisionAction.TRAIN_LIGHT: "DECISION.CONCURRENT_LOAD_REDUCTION",
        }[action]
        drivers = recovery.positive_signals or ["RECOVERY_NORMAL"]
        if training.load_state == LoadState.ELEVATED:
            drivers.append("TRAINING_LOAD_ELEVATED")
        elif training.load_state == LoadState.LOW:
            drivers.append("TRAINING_LOAD_LOW")
        return self._decision(
            recommendation_id,
            action,
            confidence,
            list(dict.fromkeys(drivers)),
            limitations,
            [rule],
            plan(primary, optional, relationship, conflicts),
        )

    @staticmethod
    def _confidence(recovery, limitations) -> ConfidenceBand:
        signal_count = len(recovery.positive_signals) + len(recovery.negative_signals)
        if recovery.state == RecoveryState.INSUFFICIENT_DATA:
            return ConfidenceBand.NONE
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
    def _training_action(sleep_state, recovery, training) -> DecisionAction:
        if recovery.state == RecoveryState.NORMAL and training.load_state == LoadState.ELEVATED:
            return DecisionAction.TRAIN_LIGHT
        if (
            recovery.state == RecoveryState.GOOD
            and sleep_state == SleepState.ABOVE_BASELINE
            and training.load_state == LoadState.LOW
        ):
            return DecisionAction.TRAIN_HARD
        return DecisionAction.TRAIN_NORMAL

    @staticmethod
    def _primary_type(training, balance) -> str:
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
        latest = training.recent_workouts[0].training_family if training.recent_workouts else None
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
            primary = self._running(preferences, training, "PRIMARY", quality_allowed)
            if balance.strength_due:
                optional = self._strength(preferences, training, "OPTIONAL", "moderate")
                relationship = "ALTERNATIVE" if quality_allowed or lower_focus else "ADDITION"
            else:
                optional = self._recovery(preferences, "跑步后的低负担恢复", "OPTIONAL")
                relationship = "ADDITION"
            return primary, optional, relationship

        primary = self._strength(preferences, training, "PRIMARY", intensity)
        if balance.running_due:
            optional = self._running(preferences, training, "OPTIONAL", False)
            relationship = "ALTERNATIVE" if lower_focus else "ADDITION"
        else:
            optional = self._recovery(preferences, "力量训练后的低负担恢复", "OPTIONAL")
            relationship = "ADDITION"
        return primary, optional, relationship

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
        self, preferences, training, role: str, quality: bool
    ) -> PlannedSession:
        threshold = training.running.lactate_threshold_bpm if training.running else None
        if quality:
            duration = self._duration(preferences, (42, 55))
            intensity = "high"
            code = "threshold_intervals"
            title = "阈值间歇跑"
            focus = "一次质量跑，避免连续两天高负荷下肢刺激"
            main_intensity = (
                f"工作段约 {round(threshold * 0.95)}–{round(threshold) - 1} 次/分钟"
                if threshold else "工作段主观用力 7/10，仍能维持动作稳定"
            )
            steps = [
                TrainingStep(order=1, name="热身", duration_minutes=(10, 12), intensity="轻松", instructions=["从快走逐渐过渡到慢跑"]),
                TrainingStep(order=2, name="阈值工作段", sets=3, repetitions="每组 6 分钟", rest_seconds=(120, 120), intensity=main_intensity, instructions=["组间慢跑恢复", "最后一组不冲刺"]),
                TrainingStep(order=3, name="冷身", duration_minutes=(8, 10), intensity="轻松"),
            ]
        else:
            duration = self._duration(preferences, (30, 45))
            intensity = "low"
            code = "easy_run"
            title = "轻松跑"
            focus = "补足有氧频次并控制恢复成本"
            zone = (
                f"约 {round(threshold * 0.85)}–{round(threshold * 0.90) - 1} 次/分钟"
                if threshold else "以谈话测试为准，不使用推测心率区间"
            )
            steps = [
                TrainingStep(order=1, name="热身", duration_minutes=(6, 8), intensity="轻松", instructions=["快走后逐渐进入慢跑"]),
                TrainingStep(order=2, name="轻松跑", duration_minutes=(18, 30), intensity=zone, instructions=["保持能连续说出完整短句", "步频保持自然，不追求固定数值"]),
                TrainingStep(order=3, name="冷身", duration_minutes=(5, 7), intensity="轻松"),
            ]
        return PlannedSession(
            role=role,
            role_label=ROLE_LABELS[role],
            session_type="RUNNING",
            session_type_label=TYPE_LABELS["RUNNING"],
            code=code,
            title=title,
            focus=focus,
            goal="在健康优先前提下维持跑步连续性",
            intensity=intensity,
            intensity_label=INTENSITY_LABELS[intensity],
            total_duration_minutes=duration,
            steps=steps,
            evidence=[
                f"近 7 天完成跑步 {training.running.sessions_7d if training.running else 0} 次。",
                f"近 28 天完成跑步 {training.running.sessions_28d if training.running else 0} 次。",
            ],
            progression=["连续两次按当前剂量完成且次日恢复未受抑制时，再增加 5 分钟或一个工作段。"],
            stop_conditions=["出现胸痛、异常气短、头晕、步态改变或持续疼痛时立即停止。"],
        )

    def _strength(self, preferences, training, role: str, intensity: str) -> PlannedSession:
        focus = training.strength.next_focus if training.strength else None
        focus = focus or "FULL_BODY"
        title, focus_text, steps = self._strength_steps(focus, intensity)
        planned_intensity = "moderate" if intensity == "high" else intensity
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
            progression=["所有工作组达到次数上限且仍满足余力要求时，下次增加 2.5%–5% 重量或每组 1 次。"],
            stop_conditions=["出现尖锐疼痛、动作失控、异常头晕或胸闷时立即停止。", "无法保持目标余力时减少重量或结束该动作。"],
        )

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
            action_plan=action_plan,
        )
