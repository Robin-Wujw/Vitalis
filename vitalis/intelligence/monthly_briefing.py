"""Build the complete rolling 28-day report from a MonthlyProfile."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .contracts import ReportBriefing
from .report_formatting import as_of_line, coverage_text, date_text, energy_label, metric_label, number, payload_of, percent, running_class_label, unique, value_with_unit


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


def _day_count(label: str, value: Any) -> str:
    shown = number(value, 0)
    return f"{label} {shown}/28 天" if shown is not None else f"{label}天数未记录"


class MonthlyBriefingEngine:
    """Projection only; it does not schedule delivery or recalculate associations."""

    def build_payload(self, profile: Any) -> dict[str, Any]:
        payload = payload_of(profile)
        facts = payload.get("facts") or {}
        inferences = payload.get("inferences") or {}
        sections = [
            self._coverage(payload),
            self._sleep_recovery(facts),
            self._training_activity(facts),
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
            summary=self._summary(payload),
            sections=sections,
        ).model_dump(mode="json")

    def build(self, profile: Any) -> ReportBriefing:
        return ReportBriefing.model_validate(self.build_payload(profile))

    def _coverage(self, payload: dict[str, Any]) -> dict[str, Any]:
        training = (payload.get("facts") or {}).get("training") or {}
        facts = [coverage_text(training.get("coverage_status"), training.get("record_days"), training.get("unknown_days"), 28)]
        quality = payload.get("data_quality") or {}
        if any(quality.get(key) is not None for key in ("sleep_days", "hrv_days", "activity_days")):
            facts.append("当前窗口有效天数：" + "；".join(
                f"{label} {quality[key]}/28"
                for key, label in (("sleep_days", "睡眠"), ("hrv_days", "HRV"), ("activity_days", "活动"))
                if quality.get(key) is not None
            ) + "。")
        as_of = as_of_line(payload.get("report_context") or {})
        if as_of:
            facts.append(as_of)
        facts.append("比较窗口为最近 28 日与此前 28 日；未核实日期不被当作休息日或训练缺口。")
        quality = payload.get("data_quality") or {}
        limitations = list(quality.get("limitations") or [])
        return {"key": "coverage", "title": "两期二十八日覆盖", "facts": facts, "interpretation": ["只有两期覆盖和有效日数足够时，长期变化才具有可比性。"], "limitations": limitations}

    def _sleep_recovery(self, facts: dict[str, Any]) -> dict[str, Any]:
        sleep = facts.get("sleep") or {}
        recovery = facts.get("recovery") or {}
        lines = []
        if sleep.get("available_days") is not None:
            current_days = sleep["available_days"]
            previous_days = sleep.get("previous_available_days")
            text = f"睡眠：本期有效 {number(current_days, 0)}/28 天"
            if sleep.get("average_minutes") is not None:
                text += f"，平均 {number(sleep['average_minutes'], 0)} 分钟/晚"
            if previous_days is not None:
                text += f"；前期有效 {number(previous_days, 0)}/28 天"
            elif sleep.get("previous_average_minutes") is not None:
                text += "；前期有效天数未记录"
            if sleep.get("previous_average_minutes") is not None:
                text += f"，平均 {number(sleep['previous_average_minutes'], 0)} 分钟/晚"
            if (sleep.get("change_percent") is not None
                    and sleep.get("average_minutes") is not None
                    and sleep.get("previous_average_minutes") is not None
                    and current_days >= 14 and previous_days is not None and previous_days >= 14):
                text += f"；两期均值变化 {percent(sleep['change_percent'])}"
            if sleep.get("bedtime_regularity_minutes") is not None:
                text += f"；入睡时间离散度 {number(sleep['bedtime_regularity_minutes'])} 分钟"
            lines.append(text + "。")
        streams = recovery.get("streams") or []
        counts = Counter(stream.get("metric") for stream in streams)
        indices: Counter[str] = Counter()
        for stream in streams:
            metric = stream.get("metric")
            indices[metric] += 1
            label = stream.get("metric_label") or metric_label(metric)
            if counts[metric] > 1:
                source = "设备记录" if stream.get("source_scope") == "device" else "汇总记录"
                label += f"（{source} {indices[metric]}）"
            current_days = stream.get("available_days")
            previous_days = stream.get("previous_available_days")
            current_count = f"{number(current_days, 0)}/28" if current_days is not None else "未记录"
            text = f"{label}：本期有效 {current_count} 天，中位数 {value_with_unit(stream.get('median'), stream.get('unit')) or '未记录'}"
            if previous_days is not None:
                text += f"；前期有效 {number(previous_days, 0)}/28 天"
            elif stream.get("previous_median") is not None:
                text += "；前期有效天数未记录"
            if stream.get("previous_median") is not None:
                text += f"，中位数 {value_with_unit(stream['previous_median'], stream.get('unit'))}"
            if (stream.get("change_percent") is not None
                    and stream.get("median") is not None and stream.get("previous_median") is not None
                    and current_days is not None and previous_days is not None
                    and current_days >= 14 and previous_days >= 14):
                text += f"；两期中位数变化 {percent(stream['change_percent'])}"
            lines.append(text + "。")
        if not lines:
            lines.append("本周期没有足够的睡眠或同源恢复流可用于长期比较。")
        return {"key": "sleep_recovery", "title": "持续恢复变化", "facts": lines, "interpretation": [], "limitations": []}

    def _training_activity(self, facts: dict[str, Any]) -> dict[str, Any]:
        training = facts.get("training") or {}
        activity = facts.get("activity") or {}
        feedback = facts.get("feedback") or {}
        lines = []
        partial = training.get("totals_are_partial")
        if partial is None:
            partial = training.get("coverage_status") != "COMPLETE" or training.get("unknown_days") != 0
        for key, label, unit in (("workout_count", "训练场次", "次"), ("duration_minutes", "训练时长", "分钟"), ("vendor_load", "设备训练负荷指数", ""), ("aerobic_minutes", "有氧时长", "分钟"), ("strength_sessions", "力量场次", "次")):
            if training.get(key) is not None:
                name = f"已记录{label}" if partial else label
                lines.append(f"{name} {number(training[key])} {unit}。" if unit else f"{name} {number(training[key])}。")
        if training.get("running_sessions") is not None:
            run = f"{'已记录' if partial else ''}跑步 {number(training['running_sessions'], 0)} 次"
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
            lines.append(("已记录力量：" if partial else "力量：") + "；".join(strength) + "。")
        if training.get("vendor_reported_sets") is not None:
            lines.append(f"设备已记录组数小计 {number(training['vendor_reported_sets'], 0)} 组，来自 {number(training.get('vendor_sets_sessions', 0), 0)} 场有组数记录的力量训练；不等同于逐组动作明细。")
        if training.get("workout_calories_kcal") is not None:
            lines.append(f"已记录训练热量小计 {number(training['workout_calories_kcal'])} 千卡，来自 {number(training.get('workout_calories_sessions', 0), 0)} 场有热量记录。")
        metrics = activity.get("metrics") or []
        if activity.get("total_steps") is not None and not any(
            item.get("metric") == "steps" and item.get("total") is not None for item in metrics
        ):
            days = activity.get("available_days")
            scope = f"{number(days, 0)} 个有记录日" if days is not None else "有记录日"
            lines.append(f"日常步数：{scope}小计 {number(activity['total_steps'], 0)} 步。")
        if activity.get("average_steps") is not None and not any(
            item.get("metric") == "steps" and item.get("average") is not None for item in metrics
        ):
            lines.append(f"有记录日平均步数 {number(activity['average_steps'], 0)} 步/日。")
        if activity.get("active_minutes") is not None and not any(
            item.get("metric") == "active_minutes" and item.get("total") is not None for item in metrics
        ):
            lines.append(f"已记录活动时长小计 {number(activity['active_minutes'], 0)} 分钟（有效天数未单独统计）。")
        limitations = []
        counts = Counter((item.get("metric"), item.get("role")) for item in metrics)
        indices: Counter[tuple[str | None, str | None]] = Counter()
        for metric in metrics:
            key = (metric.get("metric"), metric.get("role"))
            indices[key] += 1
            name = _activity_metric_label(metric)
            if counts[key] > 1:
                scope = (metric.get("provenance") or {}).get("source_scope")
                name += f"（{'设备记录' if scope == 'device' else '汇总记录'} {indices[key]}）"
            prefix = "设备估算口径未确认；" if metric.get("role") == "unspecified" else ""
            text = (f"{name}：{prefix}{_day_count('本期有记录', metric.get('available_days'))}、{_day_count('完整', metric.get('complete_days'))}；"
                    f"{_day_count('前期有记录', metric.get('previous_available_days'))}、{_day_count('完整', metric.get('previous_complete_days'))}")
            total = _activity_metric_value(metric, metric.get("total"))
            average = _activity_metric_value(metric, metric.get("average"))
            partial_metric = (
                metric.get("totals_are_partial") is not False
                or metric.get("complete_days") != 28
                or metric.get("available_days") != 28
            )
            if total is not None:
                text += f"；{'已记录小计' if partial_metric else '合计'} {total}"
            if average is not None:
                text += f"；有记录日均 {average}"
            if (metric.get("change_percent") is not None
                    and metric.get("average") is not None and metric.get("previous_average") is not None
                    and isinstance(metric.get("complete_days"), int)
                    and isinstance(metric.get("previous_complete_days"), int)
                    and metric["complete_days"] >= 14 and metric["previous_complete_days"] >= 14):
                text += f"；完整日均值变化 {percent(metric['change_percent'])}"
            lines.append(text + "。")
            limitations.extend(metric.get("limitations") or [])
        if feedback.get("response_count", 0):
            lines.append(f"已记录主观反馈 {number(feedback['response_count'], 0)} 条；这些反馈用于解释周期体验，不是自动问卷结果。")
        return {"key": "training_activity", "title": "训练结构、活动与能量", "facts": lines or ["本周期没有可用的训练、活动或能量事实。"], "interpretation": [], "limitations": unique(list(training.get("limitations") or []) + limitations)}

    def _associations(self, payload: dict[str, Any]) -> dict[str, Any]:
        associations = (payload.get("inferences") or {}).get("personal_associations") or []
        facts = []
        limitations = []
        for item in associations:
            summary = item.get("summary")
            if summary:
                days = item.get("paired_days")
                detail = f"配对 {number(days, 0)} 天；" if days is not None else ""
                facts.append(f"{summary}（{detail}仅表示个人关联，不表示因果。）")
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
    def _summary(payload: dict[str, Any]) -> list[str]:
        return [f"滚动 28 日：{date_text(payload.get('period_start'))} 至 {date_text(payload.get('period_end'))}。"]
