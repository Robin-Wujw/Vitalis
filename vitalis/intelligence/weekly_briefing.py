"""Build the complete rolling seven-day report from a WeeklyProfile."""
from __future__ import annotations

from typing import Any

from .contracts import ReportBriefing
from .report_formatting import (
    as_of_line,
    coverage_text,
    date_text,
    energy_label,
    metric_label,
    number,
    payload_of,
    percent,
    running_class_label,
    unique,
)


_ACTIVITY_UNIT_LABELS = {
    "steps": "步",
    "km": "公里",
    "m": "米",
    "min": "分钟",
    "minutes": "分钟",
    "kcal": "千卡",
}


def _activity_metric_label(metric: dict[str, Any]) -> str:
    name = metric.get("metric")
    if name == "calories":
        return energy_label(metric.get("role"))
    return metric_label(name)


def _activity_metric_value(metric: dict[str, Any], value: Any) -> str | None:
    shown = number(value)
    if shown is None:
        return None
    raw_unit = metric.get("unit")
    unit = _ACTIVITY_UNIT_LABELS.get(raw_unit, raw_unit or "单位未提供")
    return f"{shown} {unit}" if unit else shown


class WeeklyBriefingEngine:
    """Projection only: it preserves the weekly analysis and its coverage gates."""

    def build_payload(self, profile: Any) -> dict[str, Any]:
        payload = payload_of(profile)
        facts = payload.get("facts") or {}
        inferences = payload.get("inferences") or {}
        sections = [
            self._coverage(payload),
            self._sleep_recovery(facts, inferences),
            self._training(facts, inferences),
            self._activity_feedback(facts, inferences),
            self._actions(payload),
        ]
        context = dict(payload.get("report_context") or {})
        return ReportBriefing(
            period="weekly",
            analysis_run_id=payload.get("analysis_run_id", ""),
            user_id=payload.get("user_id", ""),
            date=payload.get("period_end"),
            period_start=payload.get("period_start"),
            period_end=payload.get("period_end"),
            generated_at=payload.get("generated_at"),
            report_context=context,
            data_quality=payload.get("data_quality") or {},
            summary=self._summary(payload, sections),
            sections=sections,
        ).model_dump(mode="json")

    def build(self, profile: Any) -> ReportBriefing:
        return ReportBriefing.model_validate(self.build_payload(profile))

    def _coverage(self, payload: dict[str, Any]) -> dict[str, Any]:
        training = (payload.get("facts") or {}).get("training") or {}
        facts = [coverage_text(training.get("coverage_status"), training.get("record_days"), training.get("unknown_days"), 7)]
        quality = payload.get("data_quality") or {}
        if any(quality.get(key) is not None for key in ("sleep_days", "hrv_days", "activity_days", "training_days")):
            facts.append("当前窗口有效天数：" + "；".join(
                f"{label} {quality.get(key, 0)}/7"
                for key, label in (("sleep_days", "睡眠"), ("hrv_days", "HRV"), ("activity_days", "活动"), ("training_days", "训练"))
                if quality.get(key) is not None
            ) + "。")
        as_of = as_of_line(payload.get("report_context") or {})
        if as_of:
            facts.append(as_of)
        return {
            "key": "coverage", "title": "两期七日覆盖", "facts": facts,
            "interpretation": ["只有有效覆盖足够且两期可比时才解释变化；尚未核实的日期不算休息日。"],
            "limitations": list((payload.get("data_quality") or {}).get("limitations") or []),
        }

    def _sleep_recovery(self, facts: dict[str, Any], inferences: dict[str, Any]) -> dict[str, Any]:
        sleep = facts.get("sleep") or {}
        recovery = facts.get("recovery") or {}
        fact_lines = []
        if sleep.get("available_days") is not None:
            text = f"睡眠有效 {number(sleep['available_days'], 0)} 天"
            if sleep.get("average_minutes") is not None:
                text += f"，平均 {number(sleep['average_minutes'], 0)} 分钟"
            if sleep.get("previous_average_minutes") is not None:
                text += f"；前一期平均 {number(sleep['previous_average_minutes'], 0)} 分钟"
            if sleep.get("change_percent") is not None:
                text += f"，变化 {percent(sleep['change_percent'])}"
            fact_lines.append(text + "。")
        hrv = recovery.get("hrv_median_ms")
        if hrv is not None:
            label = recovery.get("hrv_metric_label") or metric_label(recovery.get("hrv_metric"))
            text = f"{label}有效 {recovery.get('hrv_available_days', 0)} 天，中位数 {number(hrv)} 毫秒"
            if recovery.get("hrv_previous_median_ms") is not None:
                text += f"；前一期 {number(recovery['hrv_previous_median_ms'])} 毫秒"
            if recovery.get("hrv_change_percent") is not None:
                text += f"，变化 {percent(recovery['hrv_change_percent'])}"
            fact_lines.append(text + "。")
        if recovery.get("rhr_median_bpm") is not None:
            text = f"静息心率有效 {recovery.get('rhr_available_days', 0)} 天，中位数 {number(recovery['rhr_median_bpm'])} 次/分钟"
            if recovery.get("rhr_previous_median_bpm") is not None:
                text += f"；前一期 {number(recovery['rhr_previous_median_bpm'])} 次/分钟"
            if recovery.get("rhr_change_percent") is not None:
                text += f"，变化 {percent(recovery['rhr_change_percent'])}"
            fact_lines.append(text + "。")
        if not fact_lines:
            fact_lines.append("本周期没有足够的睡眠、HRV 或静息心率事实可比较。")
        interpretations = list(inferences.get("key_changes") or [])
        if not interpretations:
            interpretations = self._recovery_impact(sleep, recovery)
        return {"key": "sleep_recovery", "title": "睡眠与恢复变化", "facts": fact_lines, "interpretation": interpretations[:8], "limitations": []}

    @staticmethod
    def _recovery_impact(sleep: dict[str, Any], recovery: dict[str, Any]) -> list[str]:
        output = []
        if sleep.get("change_percent") is not None:
            direction = "增加" if sleep["change_percent"] > 0 else "减少"
            output.append(f"平均睡眠较前一期{direction} {abs(sleep['change_percent']):.1f}%，这会影响本周训练剂量的可持续性判断。")
        if recovery.get("hrv_change_percent") is not None or recovery.get("rhr_change_percent") is not None:
            parts = []
            if recovery.get("hrv_change_percent") is not None:
                parts.append(f"HRV {recovery['hrv_change_percent']:+.1f}%")
            if recovery.get("rhr_change_percent") is not None:
                parts.append(f"静息心率 {recovery['rhr_change_percent']:+.1f}%")
            output.append("恢复信号较前一期变化：" + "；".join(parts) + "，训练结构调整仍需结合覆盖和既有门控。")
        return output or ["恢复数据没有达到可比较门槛，暂不据此调整训练结构。"]

    def _training(self, facts: dict[str, Any], inferences: dict[str, Any]) -> dict[str, Any]:
        training = facts.get("training") or {}
        lines = []
        for key, label, unit in (("workout_count", "训练场次", "次"), ("duration_minutes", "训练时长", "分钟"), ("vendor_load", "设备训练负荷", ""), ("aerobic_minutes", "有氧时长", "分钟"), ("strength_sessions", "力量场次", "次")):
            if training.get(key) is not None:
                lines.append(f"{label} {number(training[key])} {unit}。" if unit else f"{label} {number(training[key])}。")
        details = training
        if details.get("running_sessions") is not None:
            run = f"跑步 {number(details['running_sessions'], 0)} 次"
            if details.get("running_distance_km") is not None:
                run += f"、{number(details['running_distance_km'], 2)} 公里"
            if details.get("running_duration_minutes") is not None:
                run += f"、{number(details['running_duration_minutes'], 0)} 分钟"
            if details.get("running_classification_counts"):
                run += "；本次分析的课型 " + "、".join(
                    f"{running_class_label(k)} {v} 次"
                    for k, v in details["running_classification_counts"].items()
                )
            lines.append(run + "。")
        if details.get("strength_duration_minutes") is not None or details.get("strength_explicit_sessions") is not None:
            strength = []
            if details.get("strength_duration_minutes") is not None:
                strength.append(f"{number(details['strength_duration_minutes'], 0)} 分钟")
            if details.get("strength_explicit_sessions") is not None:
                strength.append(f"明确明细 {number(details['strength_explicit_sessions'], 0)} 场")
            if details.get("strength_sets") is not None:
                strength.append(f"明确组数 {number(details['strength_sets'], 0)} 组")
            lines.append("力量：" + "；".join(strength) + "。")
        if details.get("vendor_reported_sets") is not None:
            lines.append(f"设备记录组数合计 {number(details['vendor_reported_sets'], 0)} 组，来自 {number(details.get('vendor_sets_sessions', 0), 0)} 场有组数记录的力量训练；不等同于已取得逐组动作明细。")
        if details.get("workout_calories_kcal") is not None:
            lines.append(f"训练热量 {number(details['workout_calories_kcal'])} 千卡，来自 {number(details.get('workout_calories_sessions', 0), 0)} 场有热量记录。")
        if not lines:
            lines.append("本周期没有可用训练总量或专项结构事实。")
        limitations = list(training.get("limitations") or [])
        if training.get("totals_are_partial"):
            limitations.append("训练合计只覆盖已记录日期，不能当作完整周期总量。")
        interpretation = self._training_impact(training, inferences)
        if not interpretation:
            interpretation = ["两期可比记录不足，本节不判断是否需要调整跑步或力量安排。"]
        return {"key": "training", "title": "跑步与力量结构", "facts": lines, "interpretation": interpretation, "limitations": unique(limitations)}

    def _training_impact(self, training: dict[str, Any], inferences: dict[str, Any]) -> list[str]:
        changes = list(inferences.get("key_changes") or [])
        if changes:
            return changes[:5]
        if training.get("load_change_percent") is not None:
            change = training["load_change_percent"]
            direction = "增加" if change > 0 else "减少"
            return [f"训练负荷较前一期{direction} {abs(change):.1f}%，本周建议沿用既有门控，不因总量变化自动追加强度。"]
        return ["训练量缺少足够的两期可比数据，暂不判断增减是否合适。"]

    def _activity_feedback(self, facts: dict[str, Any], inferences: dict[str, Any]) -> dict[str, Any]:
        activity = facts.get("activity") or {}
        feedback = facts.get("feedback") or {}
        lines = []
        if activity.get("available_days") is not None:
            text = f"活动有效 {number(activity['available_days'], 0)} 天"
            if activity.get("total_steps") is not None:
                text += f"；累计步数 {number(activity['total_steps'], 0)}"
            if activity.get("average_steps") is not None:
                text += f"；日均 {number(activity['average_steps'], 0)}"
            if activity.get("steps_change_percent") is not None:
                text += f"；日均变化 {percent(activity['steps_change_percent'])}"
            if activity.get("active_minutes") is not None:
                text += f"；活动分钟 {number(activity['active_minutes'], 0)}"
            lines.append(text + "。")
        limitations = []
        for metric in activity.get("metrics") or []:
            label = _activity_metric_label(metric)
            text = f"{label}："
            if metric.get("role") == "unspecified":
                text += "设备估算口径未确认；"
            total = _activity_metric_value(metric, metric.get("total"))
            average = _activity_metric_value(metric, metric.get("average"))
            if total is not None:
                text += f"本期 {total}"
            if average is not None:
                text += f"，日均 {average}"
            if metric.get("change_percent") is not None:
                text += f"，均值变化 {percent(metric['change_percent'])}"
            text += f"；本期有效日 {metric.get('available_days', 0)}，完整日 {metric.get('complete_days', 0)}；前期有效日 {metric.get('previous_available_days', 0)}，完整日 {metric.get('previous_complete_days', 0)}。"
            lines.append(text)
            limitations.extend(metric.get("limitations") or [])
        if feedback.get("response_count", 0):
            lines.append(f"已记录主观反馈 {number(feedback['response_count'], 0)} 条" + self._feedback_tail(feedback) + "。")
        if not lines:
            lines.append("本周期没有可用活动或已记录反馈事实。")
        activity_change = activity.get("steps_change_percent")
        if activity_change is not None:
            impact = "增加" if activity_change > 0 else "减少"
            interpretation = [f"日均步数较前一期{impact} {abs(activity_change):.1f}%，活动变化与训练总量分开考虑。"]
        elif feedback.get("response_count", 0):
            interpretation = ["已记录反馈显示主观体验可纳入本周调整；它不替代设备观测。"]
        else:
            interpretation = ["活动和反馈没有足够的两期变化证据，暂不据此改变训练处方。"]
        return {"key": "activity_feedback", "title": "活动、能量与反馈", "facts": lines, "interpretation": interpretation, "limitations": unique(limitations)}

    @staticmethod
    def _feedback_tail(feedback: dict[str, Any]) -> str:
        parts = []
        for key, label in (("average_session_rpe", "平均训练 RPE"), ("average_physical_fatigue", "平均身体疲劳"), ("average_muscle_soreness", "平均肌肉酸痛")):
            if feedback.get(key) is not None:
                parts.append(f"；{label} {number(feedback[key])}")
        return "".join(parts)

    def _actions(self, payload: dict[str, Any]) -> dict[str, Any]:
        inferences = payload.get("inferences") or {}
        facts = [item for item in inferences.get("key_changes") or []]
        interpretation = []
        for item in (payload.get("actions") or {}).get("recommendations") or []:
            title = item.get("title", "周期建议")
            action = item.get("action", "")
            reasons = "；".join(item.get("reasons") or [])
            interpretation.append(f"{title}：{action}" + (f"（依据：{reasons}）" if reasons else ""))
        if not interpretation:
            interpretation.append("当前没有可由周期事实支持的新增调整；保持项仍须服从既有门控。")
        return {"key": "actions", "title": "既有门控建议", "facts": facts or ["本周期没有达到比较门槛的显著变化条目。"], "interpretation": interpretation, "limitations": list(inferences.get("limitations") or [])}

    @staticmethod
    def _summary(payload: dict[str, Any], sections: list[dict[str, Any]]) -> list[str]:
        start, end = date_text(payload.get("period_start")), date_text(payload.get("period_end"))
        return [f"滚动 7 日（{start} 至 {end}）的主要变化。", sections[1]["facts"][0], sections[2]["facts"][0]]
