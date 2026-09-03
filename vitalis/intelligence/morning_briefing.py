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
        if decision["action"] == "INSUFFICIENT_DATA":
            feedback_prompt = None
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
            "cautions": cautions,
            "feedback_prompt": feedback_prompt,
            "data_quality": daily["data_quality"],
            "evidence": decision.get("evidence", {"facts": [], "gates": []}),
        }

    def _reasons(self, decision: dict[str, Any]) -> list[str]:
        evidence_labels = [
            item.get("label", "")
            for item in decision.get("evidence", {}).get("facts", [])
        ]
        reasons = evidence_labels or list(decision.get("driver_labels", []))
        action_plan = decision["action_plan"]
        primary = action_plan.get("primary_session")
        if primary:
            reasons.extend(primary.get("personalization_reasons", [])[:1])
            balance = action_plan.get("weekly_balance") or {}
            if primary.get("session_type") == "RUNNING" and balance.get("running_due"):
                completed = balance.get("running_completed_7d")
                if completed is not None:
                    reasons.append(f"近 7 天完成跑步 {completed} 次，今天优先维持跑步频次。")
            elif primary.get("session_type") == "STRENGTH" and balance.get("strength_due"):
                completed = balance.get("strength_completed_7d")
                if completed is not None:
                    reasons.append(f"近 7 天完成力量训练 {completed} 次，今天优先维持力量训练频次。")
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
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))
