"""Build the shared, action-first morning presentation from a DailyProfile."""

from typing import Any

from .contracts import DailyProfile, MorningBriefing


_ACTION_CHANGING_EVENTS = {
    "RECOVERY_SUPPRESSED",
    "SLEEP_DEFICIT",
    "RHR_ELEVATED",
    "HRV_DROP",
    "TRAINING_LOAD_SPIKE",
    "TRAINING_GAP",
}
_FORMAL_ACTIONS = {"TRAIN_HARD", "TRAIN_NORMAL", "TRAIN_LIGHT", "RECOVERY"}


class MorningBriefingEngine:
    """Projection only: it never calculates or changes a training decision."""

    def build(
        self, daily: DailyProfile, delivery_metadata: dict | None = None
    ) -> MorningBriefing:
        return MorningBriefing.model_validate(
            self.build_payload(daily.model_dump(mode="json"), delivery_metadata)
        )

    def build_payload(
        self, daily: dict[str, Any], delivery_metadata: dict | None = None
    ) -> dict[str, Any]:
        decision = daily["decision"]
        action_plan = decision["action_plan"]
        reasons = self._reasons(decision)
        cautions = self._cautions(daily, delivery_metadata or {})
        feedback_prompt = (
            "完成后记录：是否完成、主观用力 RPE（1-10）、"
            "身体疲劳、精神状态和酸痛（1-5）。"
            if decision["action"] in _FORMAL_ACTIONS
            else None
        )
        report_context = dict(daily.get("report_context") or {})
        readiness = (daily.get("features", {}).get("recovery") or {}).get(
            "vendor_readiness"
        )
        if isinstance(readiness, (int, float)):
            report_context["device_recovery_readiness"] = readiness
        if decision["action"] == "INSUFFICIENT_DATA":
            feedback_prompt = None
            gates = [
                item.get("label", "")
                for item in decision.get("evidence", {}).get("gates", [])
                if item.get("triggered", True)
            ]
            reasons = self._unique(
                gates
                + list(decision.get("limitation_labels", []))
                + list(daily.get("data_quality", {}).get("missing_required_signal_labels", []))
            )[:3]
            if not reasons:
                reasons = ["恢复决策所需信号不足，今天不生成训练建议。"]
            cautions = self._unique(
                list(daily.get("data_quality", {}).get("missing_required_signal_labels", []))
                + list(decision.get("limitation_labels", []))
                + cautions
            )[:3]
        return {
            "analysis_run_id": daily.get("analysis_run_id", ""),
            "user_id": daily.get("user_id", ""),
            "date": daily["date"],
            "generated_at": daily.get("generated_at"),
            "decision_action": decision["action"],
            "action_label": decision["action_label"],
            "action_plan": action_plan,
            "key_reasons": [{"text": item} for item in reasons],
            "observations": self._observations(daily),
            "cautions": cautions,
            "feedback_prompt": feedback_prompt,
            "data_quality": daily["data_quality"],
            "evidence": decision.get("evidence", {"facts": [], "gates": []}),
            "report_context": report_context,
        }

    def _observations(self, daily: dict[str, Any]) -> list[dict[str, str]]:
        """Expose a short numeric overnight snapshot without duplicating analysis."""
        features = daily.get("features") or {}
        sleep = features.get("sleep") or {}
        hrv = features.get("hrv") or {}
        recovery = features.get("recovery") or {}
        observations: list[str] = []

        duration = sleep.get("duration_minutes")
        if isinstance(duration, (int, float)):
            text = f"昨晚睡眠 {duration:g} 分钟"
            text += self._baseline_suffix(sleep.get("duration_deviation"))
            observations.append(text)
        else:
            observations.append("昨晚睡眠时长缺失，无法与个人基线比较。")

        hrv_value = hrv.get("value_ms")
        metric = hrv.get("preferred_metric")
        metric_label = {
            "sleep_hrv": "睡眠心率变异性",
            "hrv_rmssd": "心率变异性 RMSSD",
            "hrv_sdnn": "心率变异性 SDNN",
        }.get(metric, "心率变异性")
        source_label = (
            "Zepp "
            if hrv.get("fusion_method") == "vendor_fused_with_device_audit"
            else "设备 "
        )
        if isinstance(hrv_value, (int, float)):
            text = f"{source_label}{metric_label} {hrv_value:g} 毫秒"
            text += self._baseline_suffix(hrv.get("deviation"))
            observations.append(text)
        else:
            observations.append(f"{source_label}{metric_label}缺失，无法与个人基线比较。")

        state_label = recovery.get("state_label")
        if state_label:
            observations.append(f"综合身体状态：{state_label}。")
        else:
            observations.append("综合身体状态缺少可用信号。")
        return [{"text": item} for item in observations[:3]]

    @staticmethod
    def _baseline_suffix(deviation: dict | None) -> str:
        if not isinstance(deviation, dict) or deviation.get("percent") is None:
            return "；个人基线不足，暂不比较"
        return f"；较个人基线 {float(deviation['percent']):+.1f}%"

    def _reasons(self, decision: dict[str, Any]) -> list[str]:
        evidence_labels = [
            item.get("label", "")
            for item in decision.get("evidence", {}).get("facts", [])
        ]
        action_plan = decision["action_plan"]
        primary = action_plan.get("primary_session")
        selection_reason = None
        if primary:
            personalization = primary.get("personalization_reasons", [])
            if personalization:
                selection_reason = personalization[0]
            balance = action_plan.get("weekly_balance") or {}
            if selection_reason is None and primary.get("session_type") == "RUNNING" and balance.get("running_due"):
                completed = balance.get("running_completed_7d")
                if completed is not None:
                    selection_reason = f"近 7 天完成跑步 {completed} 次，今天优先维持跑步频次。"
            elif selection_reason is None and primary.get("session_type") == "STRENGTH" and balance.get("strength_due"):
                completed = balance.get("strength_completed_7d")
                if completed is not None:
                    selection_reason = f"近 7 天完成力量训练 {completed} 次，今天优先维持力量训练频次。"
        if selection_reason:
            reasons = evidence_labels[:2] + [selection_reason]
        else:
            reasons = evidence_labels or list(decision.get("driver_labels", []))
        if not reasons:
            reasons = [decision["action_label"]]
        return self._unique(reasons)[:3]

    def _cautions(self, daily: dict[str, Any], delivery_metadata: dict) -> list[str]:
        decision = daily["decision"]
        action_plan = decision["action_plan"]
        cautions = []
        if delivery_metadata.get("sync_degraded"):
            cautions.append("本次同步未完整完成，结论使用的是已经保存的当天数据。")
        if action_plan.get("safety_status") == "LIMITED":
            cautions.append(action_plan.get("safety_status_label", ""))
            primary = action_plan.get("primary_session") or {}
            cautions.extend(primary.get("stop_conditions", [])[:1])
        hrv = daily.get("features", {}).get("hrv", {})
        if hrv.get("corroboration_affects_decision"):
            cautions.append("今天的心率变异性证据不够稳定，本次安排主要依据其他恢复与训练信号。")
        if daily.get("data_quality", {}).get("status") == "PARTIAL":
            cautions.append(daily["data_quality"].get("status_label", "数据部分可用"))
        for event in daily.get("events", []):
            if event.get("lifecycle") != "RESOLVED" and event.get("type") in _ACTION_CHANGING_EVENTS:
                cautions.append(event.get("summary", ""))
                break
        return self._unique(cautions)[:3]

    @staticmethod
    def safety_lines(briefing: dict[str, Any]) -> list[str]:
        """Return all hard safety restrictions for a separate, untruncated section."""
        plan = briefing.get("action_plan") or {}
        if plan.get("safety_status") != "LIMITED":
            return []
        lines = []
        if plan.get("safety_status_label"):
            lines.append(plan["safety_status_label"])
        primary = plan.get("primary_session") or {}
        lines.extend(primary.get("stop_conditions", []))
        optional = plan.get("optional_session") or {}
        lines.extend(optional.get("stop_conditions", []))
        return MorningBriefingEngine._unique(lines)

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))
