"""Build the complete rolling 28-day report from a MonthlyProfile."""
from __future__ import annotations

from typing import Any

from .contracts import ReportBriefing
from .report_formatting import as_of_line, coverage_text, date_text, energy_label, metric_label, number, payload_of, percent, running_class_label, unique


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


class MonthlyBriefingEngine:
    """Projection only; it does not schedule delivery or recalculate associations."""

    def build_payload(self, profile: Any) -> dict[str, Any]:
        payload = payload_of(profile)
        facts = payload.get("facts") or {}
        inferences = payload.get("inferences") or {}
        sections = [
            self._coverage(payload),
            self._sleep_recovery(facts, inferences),
            self._training_activity(facts, inferences),
            self._associations(payload),
            self._actions(payload),
        ]
        return ReportBriefing(
            period="monthly",
            analysis_run_id=payload.get("analysis_run_id", ""),
            user_id=payload.get("user_id", ""),
            date=payload.get("period_end"),
            period_start=payload.get("period_start"),
            period_end=payload.get("period_end"),
            generated_at=payload.get("generated_at"),
            report_context=dict(payload.get("report_context") or {}),
            data_quality=payload.get("data_quality") or {},
            summary=self._summary(payload, sections),
            sections=sections,
        ).model_dump(mode="json")

    def build(self, profile: Any) -> ReportBriefing:
        return ReportBriefing.model_validate(self.build_payload(profile))

    def _coverage(self, payload: dict[str, Any]) -> dict[str, Any]:
        training = (payload.get("facts") or {}).get("training") or {}
        facts = [coverage_text(training.get("coverage_status"), training.get("record_days"), training.get("unknown_days"), 28)]
        quality = payload.get("data_quality") or {}
        if any(quality.get(key) is not None for key in ("sleep_days", "hrv_days", "activity_days", "training_record_days")):
            facts.append("当前窗口有效天数：" + "；".join(
                f"{label} {quality.get(key, 0)}/28"
                for key, label in (("sleep_days", "睡眠"), ("hrv_days", "HRV"), ("activity_days", "活动"), ("training_record_days", "训练记录"))
                if quality.get(key) is not None
            ) + "。")
        as_of = as_of_line(payload.get("report_context") or {})
        if as_of:
            facts.append(as_of)
        facts.append("比较窗口为最近 28 日与此前 28 日；未核实日期不被当作休息日或训练缺口。")
        quality = payload.get("data_quality") or {}
        limitations = list(quality.get("limitations") or [])
        return {"key": "coverage", "title": "两期二十八日覆盖", "facts": facts, "interpretation": ["只有两期覆盖和有效日数足够时，长期变化才具有可比性。"], "limitations": limitations}

    def _sleep_recovery(self, facts: dict[str, Any], inferences: dict[str, Any]) -> dict[str, Any]:
        sleep = facts.get("sleep") or {}
        recovery = facts.get("recovery") or {}
        lines = []
        if sleep.get("available_days") is not None:
            text = f"睡眠有效 {number(sleep['available_days'], 0)} 天"
            if sleep.get("average_minutes") is not None:
                text += f"，平均 {number(sleep['average_minutes'], 0)} 分钟"
            if sleep.get("previous_average_minutes") is not None:
                text += f"；前一期 {number(sleep['previous_average_minutes'], 0)} 分钟"
            if sleep.get("change_percent") is not None:
                text += f"，变化 {percent(sleep['change_percent'])}"
            if sleep.get("bedtime_regularity_minutes") is not None:
                text += f"；入睡时间离散度 {number(sleep['bedtime_regularity_minutes'])} 分钟"
            lines.append(text + "。")
        streams = recovery.get("streams") or []
        for stream in streams:
            label = stream.get("metric_label") or metric_label(stream.get("metric"))
            text = f"{label}（{stream.get('available_days', 0)} 天）中位数 {number(stream.get('median')) if stream.get('median') is not None else '没有可用值'}"
            if stream.get("previous_median") is not None:
                text += f"；前一期 {number(stream['previous_median'])}"
            if stream.get("change_percent") is not None:
                text += f"；变化 {percent(stream['change_percent'])}"
            lines.append(text + "。")
        if not lines:
            lines.append("本周期没有足够的睡眠或同源恢复流可用于长期比较。")
        interpretation = list(inferences.get("key_changes") or [])
        if not interpretation:
            interpretation = ["睡眠、HRV 和静息心率没有足够的两期一致变化，暂不据此改变阶段训练方向。"]
        return {"key": "sleep_recovery", "title": "持续恢复变化", "facts": lines, "interpretation": interpretation[:8], "limitations": []}

    def _training_activity(self, facts: dict[str, Any], inferences: dict[str, Any]) -> dict[str, Any]:
        training = facts.get("training") or {}
        activity = facts.get("activity") or {}
        feedback = facts.get("feedback") or {}
        lines = []
        for key, label, unit in (("workout_count", "训练场次", "次"), ("duration_minutes", "训练时长", "分钟"), ("vendor_load", "设备训练负荷", ""), ("aerobic_minutes", "有氧时长", "分钟"), ("strength_sessions", "力量场次", "次")):
            if training.get(key) is not None:
                lines.append(f"{label} {number(training[key])} {unit}。" if unit else f"{label} {number(training[key])}。")
        if training.get("running_sessions") is not None:
            run = f"跑步 {number(training['running_sessions'], 0)} 次"
            if training.get("running_distance_km") is not None:
                run += f"、{number(training['running_distance_km'], 2)} 公里"
            if training.get("running_duration_minutes") is not None:
                run += f"、{number(training['running_duration_minutes'], 0)} 分钟"
            if training.get("running_classification_counts"):
                run += "；本次分析的课型 " + "、".join(f"{running_class_label(k)} {v} 次" for k, v in training["running_classification_counts"].items())
            lines.append(run + "。")
        strength = []
        if training.get("strength_duration_minutes") is not None:
            strength.append(f"{number(training['strength_duration_minutes'], 0)} 分钟")
        if training.get("strength_explicit_sessions") is not None:
            strength.append(f"明确明细 {number(training['strength_explicit_sessions'], 0)} 场")
        if training.get("strength_sets") is not None:
            strength.append(f"明确组数 {number(training['strength_sets'], 0)} 组")
        if strength:
            lines.append("力量：" + "；".join(strength) + "。")
        if training.get("vendor_reported_sets") is not None:
            lines.append(f"设备记录组数合计 {number(training['vendor_reported_sets'], 0)} 组，来自 {number(training.get('vendor_sets_sessions', 0), 0)} 场有组数记录的力量训练；不等同于已取得逐组动作明细。")
        if training.get("workout_calories_kcal") is not None:
            lines.append(f"训练热量 {number(training['workout_calories_kcal'])} 千卡，来自 {number(training.get('workout_calories_sessions', 0), 0)} 场有热量记录。")
        if activity.get("total_steps") is not None or activity.get("active_minutes") is not None:
            text = []
            if activity.get("total_steps") is not None:
                text.append(f"累计步数 {number(activity['total_steps'], 0)}")
            if activity.get("average_steps") is not None:
                text.append(f"日均 {number(activity['average_steps'], 0)}")
            if activity.get("active_minutes") is not None:
                text.append(f"活动分钟 {number(activity['active_minutes'], 0)}")
            lines.append("日常活动：" + "；".join(text) + "。")
        limitations = []
        for metric in activity.get("metrics") or []:
            name = _activity_metric_label(metric)
            prefix = "设备估算口径未确认；" if metric.get("role") == "unspecified" else ""
            text = f"{name}{prefix}本期有效日 {metric.get('available_days', 0)}、完整日 {metric.get('complete_days', 0)}；前期有效日 {metric.get('previous_available_days', 0)}、完整日 {metric.get('previous_complete_days', 0)}"
            total = _activity_metric_value(metric, metric.get("total"))
            average = _activity_metric_value(metric, metric.get("average"))
            if total is not None:
                text += f"；合计 {total}"
            if average is not None:
                text += f"；日均 {average}"
            if metric.get("change_percent") is not None:
                text += f"；均值变化 {percent(metric['change_percent'])}"
            lines.append(text + "。")
            limitations.extend(metric.get("limitations") or [])
        if feedback.get("response_count", 0):
            lines.append(f"已记录主观反馈 {number(feedback['response_count'], 0)} 条；这些反馈用于解释周期体验，不是自动问卷结果。")
        changes = list(inferences.get("key_changes") or [])
        if not changes:
            changes = ["训练结构和活动没有足够的两期可比变化，暂不据此增加下一阶段总量。"]
        return {"key": "training_activity", "title": "训练结构、活动与能量", "facts": lines or ["本周期没有足够的训练、活动或能量事实。"], "interpretation": changes[:6], "limitations": unique(list(training.get("limitations") or []) + list(inferences.get("limitations") or []) + limitations)}

    def _associations(self, payload: dict[str, Any]) -> dict[str, Any]:
        associations = (payload.get("inferences") or {}).get("personal_associations") or []
        facts = []
        limitations = []
        for item in associations:
            summary = item.get("summary")
            if summary:
                facts.append(summary + "（仅表示个人关联，不表示因果。）")
            else:
                facts.append(f"{item.get('predictor_metric_label', '指标')} 与 {item.get('outcome_metric_label', '结果')}：配对 {item.get('paired_days', 0)} 天，{item.get('direction_label', '方向未确定')}。")
            limitations.extend(item.get("limitations") or [])
        if not facts:
            facts.append("当前没有达到个人关联最低配对和置信度门槛的结果。")
        return {"key": "associations", "title": "合格的个人关联", "facts": facts, "interpretation": ["关联只用于提出下一阶段可观察的方向，不把相关性写成原因或临床结论。"], "limitations": unique(limitations)}

    def _actions(self, payload: dict[str, Any]) -> dict[str, Any]:
        inferences = payload.get("inferences") or {}
        facts = list(inferences.get("key_changes") or []) or ["本周期没有达到比较门槛的显著变化条目。"]
        interpretations = []
        for item in (payload.get("actions") or {}).get("recommendations") or []:
            action = item.get("action", "")
            reasons = "；".join(item.get("reasons") or [])
            interpretations.append(f"{item.get('title', '阶段建议')}：{action}" + (f"（依据：{reasons}）" if reasons else ""))
        if not interpretations:
            interpretations.append("本期没有足够证据给出新的阶段建议，不能把记录不足解释为应保持或增加训练。")
        return {"key": "actions", "title": "阶段建议", "facts": facts, "interpretation": interpretations, "limitations": list(inferences.get("limitations") or [])}

    @staticmethod
    def _summary(payload: dict[str, Any], sections: list[dict[str, Any]]) -> list[str]:
        return [f"滚动 28 日（{date_text(payload.get('period_start'))} 至 {date_text(payload.get('period_end'))}）的持续变化。", sections[1]["facts"][0], sections[2]["facts"][0]]
