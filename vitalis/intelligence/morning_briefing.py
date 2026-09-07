"""Build the complete, action-first morning presentation from a DailyProfile."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import DailyProfile, MorningBriefing, ReportSection
from .report_formatting import (
    baseline_text,
    clock_text,
    list_facts,
    metric_label,
    minutes_text,
    number,
    payload_of,
    range_text,
    repetitions_text,
    unique,
)

_ACTION_CHANGING_EVENTS = {
    "RECOVERY_SUPPRESSED", "SLEEP_DEFICIT", "RHR_ELEVATED", "HRV_DROP",
    "TRAINING_LOAD_SPIKE", "TRAINING_GAP",
}


class MorningBriefingEngine:
    """Projection only: it never calculates or changes a training decision."""

    def build(self, daily: DailyProfile, delivery_metadata: dict | None = None) -> MorningBriefing:
        return MorningBriefing.model_validate(
            self.build_payload(payload_of(daily), delivery_metadata)
        )

    def build_payload(self, daily: dict[str, Any], delivery_metadata: dict | None = None) -> dict[str, Any]:
        payload = payload_of(daily)
        metadata = dict(delivery_metadata or payload.get("delivery_metadata") or {})
        if metadata.get("facts_only"):
            return self._facts_only_payload(payload, metadata)
        sections = self._sections(payload)
        report_context = dict(payload.get("report_context") or {})
        if delivery_metadata:
            report_context["delivery_metadata"] = dict(delivery_metadata)
        observations = [fact for section in sections for fact in section["facts"]]
        reasons = self._reasons(payload)
        cautions = self._cautions(payload, delivery_metadata or {})
        return {
            "schema_version": "4.0",
            "analysis_run_id": payload.get("analysis_run_id", ""),
            "user_id": payload.get("user_id", ""),
            "date": payload.get("date"),
            "generated_at": payload.get("generated_at"),
            "decision_action": (payload.get("decision") or {}).get("action", "INSUFFICIENT_DATA"),
            "action_label": (payload.get("decision") or {}).get("action_label", "暂不生成训练建议"),
            "action_plan": (payload.get("decision") or {}).get("action_plan") or {},
            "report_context": report_context,
            "summary": self._summary(payload, sections),
            "sections": sections,
            "observations": [{"text": item} for item in observations],
            "key_reasons": [{"text": item} for item in reasons],
            "cautions": cautions,
            "data_quality": payload.get("data_quality") or {},
            "evidence": ((payload.get("decision") or {}).get("evidence") or {"facts": [], "gates": []}),
        }

    def _facts_only_payload(self, payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        """Project only measured sleep/body facts for an unverified training window."""
        features = payload.get("features") or {}
        sleep = features.get("sleep") or {}
        hrv = features.get("hrv") or {}
        vitals = features.get("overnight_vitals") or {}
        # Do not evaluate the full report: recovery interpretations can depend
        # on unverified training and carry advice or free-form labels.
        sections = [
            {"key": "sleep", "title": "昨晚睡眠", "facts": self._sleep_facts(sleep),
             "interpretation": [], "limitations": []},
            {"key": "recovery", "title": "今早恢复信号", "facts": self._recovery_facts(hrv, vitals),
             "interpretation": [], "limitations": []},
        ]
        observations = [fact for section in sections for fact in section["facts"]]
        report_context = self._facts_only_context(payload.get("report_context"), metadata)
        history_notice = "训练历史覆盖尚未核验，本次仅发送睡眠和身体状态事实。"
        cautions = [history_notice]
        if metadata.get("sync_degraded"):
            cautions.insert(0, "本次同步未完整完成，结论仅使用已经保存的数据。")
        summary = [history_notice]
        return {
            "schema_version": "4.0",
            "analysis_run_id": payload.get("analysis_run_id", ""),
            "user_id": payload.get("user_id", ""),
            "date": payload.get("date"),
            "generated_at": payload.get("generated_at"),
            "decision_action": "INSUFFICIENT_DATA",
            "action_label": "事实版晨报：训练历史覆盖尚未核验",
            "report_context": report_context,
            "summary": summary,
            "sections": sections,
            "observations": [{"text": item} for item in observations],
            "key_reasons": [],
            "cautions": cautions,
            "data_quality": {"status": (payload.get("data_quality") or {}).get("status", "INSUFFICIENT")},
            "evidence": {"facts": [], "gates": []},
        }

    @staticmethod
    def _facts_only_context(context: Any, metadata: dict[str, Any]) -> dict[str, Any]:
        source = context if isinstance(context, dict) else {}
        safe = {
            key: deepcopy(source[key])
            for key in ("as_of", "timezone", "target_date", "target_day_complete")
            if key in source
        }
        safe_metadata = {
            key: metadata[key]
            for key in ("facts_only", "coverage_reason", "sync_degraded", "sync_status")
            if key in metadata
        }
        safe_metadata["facts_only"] = True
        safe["delivery_metadata"] = safe_metadata
        return safe

    def _sections(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        features = payload.get("features") or {}
        sleep = features.get("sleep") or {}
        hrv = features.get("hrv") or {}
        vitals = features.get("overnight_vitals") or {}
        decision = payload.get("decision") or {}
        return [
            {"key": "sleep", "title": "昨晚睡眠", "facts": self._sleep_facts(sleep),
             "interpretation": self._sleep_interpretation(sleep), "limitations": self._limitations(sleep)},
            {"key": "recovery", "title": "今早恢复信号", "facts": self._recovery_facts(hrv, vitals),
             "interpretation": self._recovery_interpretation(features, hrv, vitals),
             "limitations": self._limitations(hrv) + self._limitations(vitals)},
            {"key": "today_plan", "title": "今天的安排", "facts": self._plan_facts(decision),
             "interpretation": self._plan_interpretation(payload),
             "limitations": self._plan_limitations(payload)},
        ]

    def _sleep_facts(self, sleep: dict[str, Any]) -> list[str]:
        facts = []
        duration = minutes_text(sleep.get("duration_minutes"))
        if duration:
            facts.append(f"睡眠时长 {duration}")
        bedtime, wake = clock_text(sleep.get("bedtime")), clock_text(sleep.get("wake_time"))
        if bedtime and wake:
            facts.append(f"入睡 {bedtime}，醒来 {wake}")
        for key, label in (("deep_minutes", "深睡"), ("rem_minutes", "快速眼动睡眠"), ("awake_minutes", "清醒")):
            value = minutes_text(sleep.get(key))
            if value:
                facts.append(f"{label} {value}")
        if sleep.get("wake_count") is not None:
            facts.append(f"夜间醒来 {sleep['wake_count']} 次")
        if sleep.get("vendor_sleep_score") is not None:
            facts.append(f"睡眠评分 {number(sleep['vendor_sleep_score'], 0)}")
        return facts or ["昨晚没有可用的睡眠时长、时间或连续性记录"]

    def _sleep_interpretation(self, sleep: dict[str, Any]) -> list[str]:
        output = []
        if sleep.get("duration_minutes") is not None:
            output.append(f"睡眠时长：{baseline_text(sleep.get('duration_deviation'))}。")
        if sleep.get("regularity_minutes") is not None:
            output.append(f"近期入睡时刻的波动约 {number(sleep['regularity_minutes'])} 分钟。")
        wake_deviation = sleep.get("wake_count_deviation")
        if wake_deviation:
            output.append(f"醒来次数{baseline_text(wake_deviation, noun='个人通常水平')}。")
        return output or ["睡眠事实已保留，但缺少可用个人参照，不能判断优劣。"]

    def _recovery_facts(self, hrv: dict[str, Any], vitals: dict[str, Any]) -> list[str]:
        output = []
        value = hrv.get("value_ms")
        preferred = hrv.get("preferred_metric")
        if isinstance(value, (int, float)):
            output.append(f"{metric_label(preferred, overnight=preferred == 'hrv_rmssd')} {number(value)} 毫秒")
        else:
            output.append("没有可用于本次判断的 HRV 读数")
        if isinstance(hrv.get("rhr_bpm"), (int, float)):
            output.append(f"{metric_label(hrv.get('rhr_metric') or 'resting_hr')} {number(hrv['rhr_bpm'])} 次/分钟")
        else:
            output.append("静息心率没有可用夜间读数")
        if isinstance(vitals.get("respiratory_rate"), (int, float)):
            output.append(f"夜间呼吸频率 {number(vitals['respiratory_rate'])} 次/分钟")
        oxygen = vitals.get("oxygen") or {}
        if isinstance(oxygen.get("median_percent"), (int, float)):
            output.append(f"夜间血氧中位数 {number(oxygen['median_percent'])}%")
        return output

    def _recovery_interpretation(self, features: dict[str, Any], hrv: dict[str, Any], vitals: dict[str, Any]) -> list[str]:
        recovery = features.get("recovery") or {}
        output = []
        if hrv.get("value_ms") is not None:
            output.append(f"{metric_label(hrv.get('preferred_metric'))}：{baseline_text(hrv.get('deviation'))}。")
        if hrv.get("rhr_bpm") is not None:
            output.append(f"{metric_label(hrv.get('rhr_metric') or 'resting_hr')}：{baseline_text(hrv.get('rhr_deviation'))}。")
        positive = recovery.get("positive_signal_labels") or []
        negative = recovery.get("negative_signal_labels") or []
        if positive:
            output.append("相对有利的信号：" + "；".join(positive) + "。")
        if negative:
            output.append("需要留意的信号：" + "；".join(negative) + "。")
        state = recovery.get("state_label")
        if state:
            output.append(f"结合上述信号，当前综合判定为{state}。")
        if hrv.get("corroboration_affects_decision"):
            output.append("HRV 证据存在分歧，本次安排主要依据其他有效恢复信号，不凭单个 HRV 读数加量。")
        elif hrv.get("corroboration_status") == "conflicting":
            output.append("不同 HRV 记录方向不一致，不能直接混成一个值比较。")
        if not output:
            output.append("恢复信号不足以支持明确的好坏判断，训练安排将优先遵守数据与安全门控。")
        return output

    def _plan_facts(self, decision: dict[str, Any]) -> list[str]:
        plan = decision.get("action_plan") or {}
        primary = plan.get("primary_session")
        optional = plan.get("optional_session")
        if decision.get("action") == "INSUFFICIENT_DATA":
            return ["今天不生成训练建议。"]
        output = [f"主要安排：{primary.get('title', decision.get('action_label', '按计划活动'))}" if primary else decision.get("action_label", "按计划活动")]
        if primary:
            output.extend(self._session_facts(primary))
        if optional:
            relation = plan.get("session_relationship")
            heading = "可选加做" if relation == "ADDITION" else "替代方案"
            relationship = plan.get("session_relationship_label") or ("可另行加做" if relation == "ADDITION" else "二选一，不在同一天叠加")
            output.append(f"{heading}：{optional.get('title', '另一项训练')}（{relationship}）")
            output.extend(self._session_facts(optional))
        return output

    @staticmethod
    def _session_facts(session: dict[str, Any]) -> list[str]:
        lines = []
        duration = range_text(session.get("total_duration_minutes"), "分钟")
        if duration:
            lines.append(f"建议时长：{duration}")
        if session.get("intensity_label"):
            lines.append(f"强度：{session['intensity_label']}")
        for step in session.get("steps") or []:
            dose = []
            duration = range_text(step.get("duration_minutes"), "分钟")
            sets = number(step.get("sets"), 0)
            repetitions = repetitions_text(step.get("repetitions"))
            weight = number(step.get("load_kg"))
            rest = range_text(step.get("rest_seconds"), "秒")
            if duration:
                dose.append(duration)
            if sets is not None:
                dose.append(f"{sets} 组")
            if repetitions:
                dose.append(repetitions)
            if weight is not None:
                dose.append(f"{weight} 千克")
            if rest:
                dose.append(f"休息 {rest}")
            if step.get("intensity"):
                dose.append(str(step["intensity"]))
            dose.extend(step.get("instructions") or [])
            lines.append(f"{step.get('name', '训练步骤')}：{'；'.join(dose)}。")
        return lines

    def _plan_interpretation(self, payload: dict[str, Any]) -> list[str]:
        decision = payload.get("decision") or {}
        plan = decision.get("action_plan") or {}
        primary = plan.get("primary_session") or {}
        if decision.get("action") == "INSUFFICIENT_DATA":
            gates = [item.get("label", "") for item in (decision.get("evidence") or {}).get("gates", []) if item.get("triggered")]
            return unique(gates) or unique(decision.get("limitation_labels") or [])[:3]
        reasons = list(primary.get("personalization_reasons") or []) + [
            item.get("label", "") for item in (decision.get("evidence") or {}).get("facts", [])
        ]
        output = unique(reasons)
        if output:
            return output[:6]
        drivers = decision.get("driver_labels") or []
        return drivers[:6] or ["安排沿用已计算的恢复、负荷和安全门控结果。"]

    def _plan_limitations(self, payload: dict[str, Any]) -> list[str]:
        quality = payload.get("data_quality") or {}
        output = list(quality.get("missing_required_signal_labels") or [])
        plan = (payload.get("decision") or {}).get("action_plan") or {}
        if plan.get("safety_status") != "LIMITED":
            for key in ("primary_session", "optional_session"):
                output.extend((plan.get(key) or {}).get("stop_conditions") or [])
        return unique(output)

    def _summary(self, payload: dict[str, Any], sections: list[dict[str, Any]]) -> list[str]:
        decision = payload.get("decision") or {}
        sleep = sections[0]["interpretation"][:1]
        recovery = sections[1]["interpretation"][:1]
        action = decision.get("action_label") or "暂不生成训练建议"
        return unique(sleep + recovery + [f"今天的安排：{action}。"])[:3]

    def _reasons(self, payload: dict[str, Any]) -> list[str]:
        decision = payload.get("decision") or {}
        return self._plan_interpretation(payload)[:6]

    def _cautions(self, payload: dict[str, Any], delivery_metadata: dict[str, Any]) -> list[str]:
        decision = payload.get("decision") or {}
        plan = decision.get("action_plan") or {}
        output = []
        if delivery_metadata.get("sync_degraded"):
            output.append("本次同步未完整完成，结论仅使用已经保存的数据。")
        hrv = (payload.get("features") or {}).get("hrv") or {}
        if hrv.get("corroboration_affects_decision"):
            output.append("HRV 证据存在分歧，安排主要依据其他恢复与训练信号。")
        for event in payload.get("events", []):
            if event.get("lifecycle") != "RESOLVED" and event.get("type") in _ACTION_CHANGING_EVENTS:
                if event.get("summary"):
                    output.append(event["summary"])
                break
        return unique(output)

    @staticmethod
    def _limitations(value: dict[str, Any]) -> list[str]:
        labels = value.get("limitation_labels") or []
        if labels:
            return unique(labels)
        return unique([item for item in value.get("limitations") or [] if "_" not in item])

    @staticmethod
    def safety_lines(briefing: dict[str, Any]) -> list[str]:
        plan = briefing.get("action_plan") or {}
        if plan.get("safety_status") != "LIMITED":
            return []
        output = [plan.get("safety_status_label", "")]
        for session_key in ("primary_session", "optional_session"):
            output.extend((plan.get(session_key) or {}).get("stop_conditions", []))
        return unique(output)
