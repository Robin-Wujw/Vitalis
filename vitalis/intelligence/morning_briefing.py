"""Build the complete, action-first morning presentation from a DailyProfile."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

from vitalis.time import local_day

from .contracts import DailyProfile, MorningBriefing, ReportSection
from .report_formatting import (
    baseline_text,
    clock_text,
    date_text,
    energy_label,
    metric_label,
    minutes_text,
    number,
    payload_of,
    range_text,
    repetitions_text,
    timestamp_text,
    unique,
    value_with_unit,
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
            "summary": [],
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
            {"key": "recovery", "title": "今早恢复信号", "facts": self._recovery_facts(hrv, vitals, features.get("recovery") or {}),
             "interpretation": [], "limitations": []},
            *self._observed_sections(payload),
        ]
        observations = [fact for section in sections for fact in section["facts"]]
        report_context = self._facts_only_context(payload.get("report_context"), metadata)
        history_notice = "部分运动记录来源还未查全；已记录的训练照常展示，今天暂不生成训练安排。"
        cautions = []
        if metadata.get("sync_degraded"):
            cautions.append("本次同步未完整完成，仅使用已保存的数据。")
        summary = [history_notice]
        return {
            "schema_version": "4.0",
            "analysis_run_id": payload.get("analysis_run_id", ""),
            "user_id": payload.get("user_id", ""),
            "date": payload.get("date"),
            "generated_at": payload.get("generated_at"),
            "decision_action": "INSUFFICIENT_DATA",
            "action_label": "已记录数据回顾；暂不生成训练安排",
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

    @staticmethod
    def _on_day(value: Any, day: date) -> bool:
        try:
            if isinstance(value, date) and not isinstance(value, datetime):
                return value == day
            text = str(value)
            if len(text) == 10:
                return date.fromisoformat(text) == day
            return local_day(datetime.fromisoformat(text.replace("Z", "+00:00"))) == day
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _source_label(value: dict[str, Any]) -> str:
        provenance = value.get("provenance") or {}
        if provenance.get("source") == "zepp":
            return "Zepp 设备记录" if provenance.get("source_scope") == "device" else "Zepp 汇总"
        return "设备记录" if provenance.get("source_scope") == "device" else "已保存记录"

    def _activity_facts(self, activity: dict[str, Any], day: date) -> list[str]:
        facts = []
        for key, label in (("steps", "步数"), ("distance_km", "活动距离"),
                           ("active_minutes", "活动时长")):
            metric = activity.get(key) or {}
            if not isinstance(metric, dict) or not self._on_day(metric.get("observed_at"), day):
                continue
            value = value_with_unit(metric.get("value"), metric.get("unit"), 0 if key == "steps" else 1)
            if value is not None:
                facts.append(f"{label} {value}（{self._source_label(metric)}）")
        for energy in activity.get("energy") or []:
            if energy.get("role") == "workout" or not self._on_day(energy.get("observed_at"), day):
                continue
            value = value_with_unit(energy.get("value"), energy.get("unit"))
            if value is not None:
                facts.append(f"{energy_label(energy.get('role'))} {value}（{self._source_label(energy)}）")
        labels = {
            "stress": "平均压力评分", "stress_min": "最低压力评分", "stress_max": "最高压力评分",
            "stress_relaxed_pct": "放松区间", "stress_normal_pct": "正常区间",
            "stress_medium_pct": "中等压力区间", "stress_high_pct": "高压力区间",
        }
        stress = []
        for item in activity.get("stress_summary") or []:
            metric = item.get("metric")
            expected_unit = "%" if str(metric).endswith("_pct") else "score"
            if metric not in labels or item.get("unit") != expected_unit or not self._on_day(item.get("observed_at"), day):
                continue
            shown = number(item.get("value"))
            if shown is not None:
                stress.append(f"{labels[metric]} {shown}{'%' if expected_unit == '%' else ''}")
        if stress:
            facts.append("设备压力日记录：" + "；".join(stress))
        for key, label, unit in (("heart_rate", "心率", "次/分钟"), ("stress", "压力", "设备评分")):
            window = activity.get(key) or {}
            count = window.get("sample_count")
            if not isinstance(count, int) or count <= 0:
                continue
            parts = [f"{number(count, 0)} 条采样"]
            if window.get("observed_minutes") is not None:
                parts.append(f"分布在 {number(window['observed_minutes'], 0)} 个有记录的分钟")
            if window.get("average") is not None:
                parts.append(f"记录均值 {number(window['average'])} {unit}")
            facts.append(f"{label}：" + "；".join(parts))
        return facts

    @staticmethod
    def _previous_activity(payload: dict[str, Any], day: date) -> dict[str, Any]:
        context = payload.get("report_context") or {}
        candidate = context.get("previous_day_activity") or {}
        if (not isinstance(candidate, dict)
                or candidate.get("user_id") != payload.get("user_id")
                or candidate.get("date") != (day - timedelta(days=1)).isoformat()):
            return {}
        try:
            cutoff = datetime.fromisoformat(str(candidate.get("as_of")).replace("Z", "+00:00"))
            if cutoff.tzinfo is None or local_day(cutoff) < day:
                return {}
        except ValueError:
            return {}
        activity = candidate.get("activity")
        return activity if isinstance(activity, dict) and activity.get("status") == "AVAILABLE" else {}

    def _observed_sections(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            day = date.fromisoformat(date_text(payload.get("date")))
        except ValueError:
            return []
        context = payload.get("report_context") or {}
        features = payload.get("features") or {}
        sections = []
        yesterday = day - timedelta(days=1)
        previous_activity = self._previous_activity(payload, day)
        if previous_activity:
            facts = self._activity_facts(previous_activity, yesterday)
            if facts:
                sections.append({"key": "yesterday_activity", "title": "昨天的活动", "facts": facts,
                                 "interpretation": [], "limitations": []})
        workouts = []
        training = features.get("training") or {}
        for item in training.get("recent_workouts") or []:
            if not isinstance(item, dict) or date_text(item.get("date")) != yesterday.isoformat():
                continue
            label = item.get("sport_mode_label") or item.get("type_label") or "训练"
            parts = []
            if item.get("started_at"):
                parts.append(f"开始 {timestamp_text(item['started_at'], context.get('timezone'), short=True)}")
            duration = item.get("duration_minutes")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
                parts.append(f"时长 {number(duration, 0)} 分钟")
            if item.get("training_family") != "strength" and item.get("distance_km") is not None:
                parts.append(f"距离 {number(item['distance_km'], 2)} 公里")
            if item.get("calories_kcal") is not None:
                parts.append(f"本次训练估算热量 {number(item['calories_kcal'])} 千卡")
            if item.get("heart_rate_avg_bpm") is not None:
                parts.append(f"平均心率 {number(item['heart_rate_avg_bpm'], 0)} 次/分钟")
            if item.get("vendor_reported_sets") is not None:
                parts.append(f"设备记录组数 {number(item['vendor_reported_sets'], 0)} 组")
            workouts.append(f"已记录{label}" + ("：" + "；".join(parts) if parts else ""))
        if workouts:
            sections.append({"key": "observed_training", "title": "昨天已记录的训练", "facts": workouts,
                             "interpretation": [], "limitations": []})
        today_activity = features.get("activity") or {}
        if today_activity.get("status") == "AVAILABLE":
            facts = self._activity_facts(today_activity, day)
            if facts:
                sections.append({"key": "today_activity", "title": "今天截至分析时的活动", "facts": facts,
                                 "interpretation": [], "limitations": []})
        return sections

    def _sections(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        features = payload.get("features") or {}
        sleep = features.get("sleep") or {}
        hrv = features.get("hrv") or {}
        vitals = features.get("overnight_vitals") or {}
        decision = payload.get("decision") or {}
        return [
            {"key": "sleep", "title": "昨晚睡眠", "facts": self._sleep_facts(sleep),
             "interpretation": self._sleep_interpretation(sleep), "limitations": self._limitations(sleep)},
            {"key": "recovery", "title": "今早恢复信号", "facts": self._recovery_facts(hrv, vitals, features.get("recovery") or {}),
             "interpretation": self._recovery_interpretation(features, hrv, vitals),
             "limitations": unique(self._limitations(hrv) + self._limitations(vitals)
                                   + self._limitations(vitals.get("oxygen") or {}))},
            *self._observed_sections(payload),
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
        if bedtime:
            facts.append(f"入睡 {bedtime}")
        if wake:
            facts.append(f"醒来 {wake}")
        for key, label in (("deep_minutes", "深睡"), ("light_minutes", "浅睡"),
                           ("rem_minutes", "快速眼动睡眠"), ("awake_minutes", "夜间清醒")):
            value = minutes_text(sleep.get(key))
            if value is not None:
                facts.append(f"设备记录{label} {value}")
        if sleep.get("wake_count") is not None:
            facts.append(f"夜间醒来 {sleep['wake_count']} 次")
        if sleep.get("vendor_sleep_score") is not None:
            facts.append(f"设备睡眠评分 {number(sleep['vendor_sleep_score'], 0)}/100")
        if sleep.get("duration_deviation"):
            facts.append(f"睡眠时长与个人参照：{baseline_text(sleep['duration_deviation'])}。")
        if sleep.get("regularity_minutes") is not None:
            facts.append(f"近期入睡时刻离散度 {number(sleep['regularity_minutes'])} 分钟")
        return facts or ["昨晚没有可用的睡眠时长、时间或连续性记录"]

    def _sleep_interpretation(self, sleep: dict[str, Any]) -> list[str]:
        wake_deviation = sleep.get("wake_count_deviation")
        return ([f"醒来次数{baseline_text(wake_deviation, noun='个人通常水平')}。"]
                if wake_deviation else [])

    def _recovery_facts(
        self, hrv: dict[str, Any], vitals: dict[str, Any], recovery: dict[str, Any]
    ) -> list[str]:
        output = []
        value = hrv.get("value_ms")
        preferred = hrv.get("preferred_metric")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            source = hrv.get("preferred_device_label")
            if source == "Zepp 厂商汇总":
                source = "Zepp 汇总"
            suffix = f"（{source}）" if source else ""
            output.append(f"{metric_label(preferred)} {number(value)} 毫秒{suffix}")
            if hrv.get("deviation"):
                output.append(f"{metric_label(preferred)}：{baseline_text(hrv['deviation'])}。")
            if hrv.get("recent_7d_median_ms") is not None and hrv.get("recent_7d_days"):
                output.append(f"近 7 日同源 {metric_label(preferred)}：中位数 {number(hrv['recent_7d_median_ms'])} 毫秒（有效 {number(hrv['recent_7d_days'], 0)} 天）")
            if hrv.get("previous_7d_median_ms") is not None and hrv.get("previous_7d_days"):
                output.append(f"此前 7 日同源 {metric_label(preferred)}：中位数 {number(hrv['previous_7d_median_ms'])} 毫秒（有效 {number(hrv['previous_7d_days'], 0)} 天）")
        else:
            output.append("睡眠 HRV 未取得可用读数")
        if isinstance(hrv.get("rhr_bpm"), (int, float)) and not isinstance(hrv.get("rhr_bpm"), bool):
            label = "夜间心率中位数" if hrv.get("rhr_metric") == "nocturnal_heart_rate" else metric_label(hrv.get("rhr_metric") or "resting_hr")
            output.append(f"{label} {number(hrv['rhr_bpm'])} 次/分钟")
            if hrv.get("rhr_deviation"):
                output.append(f"{label}：{baseline_text(hrv['rhr_deviation'])}。")
        night_hr = hrv.get("nocturnal_heart_rate") or {}
        if night_hr.get("status") == "AVAILABLE":
            if night_hr.get("median_bpm") is not None and hrv.get("rhr_metric") != "nocturnal_heart_rate":
                output.append(f"夜间心率中位数 {number(night_hr['median_bpm'])} 次/分钟")
            if night_hr.get("low_5m_bpm") is not None:
                output.append(f"夜间最低五分钟心率中位数 {number(night_hr['low_5m_bpm'])} 次/分钟")
            if night_hr.get("sample_count"):
                coverage = night_hr.get("coverage_ratio")
                scope = f"；记录覆盖约 {number(coverage * 100, 0)}%" if isinstance(coverage, (int, float)) and coverage > 0 else ""
                output.append(f"夜间心率采样 {number(night_hr['sample_count'], 0)} 条{scope}")
        if isinstance(vitals.get("respiratory_rate"), (int, float)) and not isinstance(vitals.get("respiratory_rate"), bool):
            output.append(f"夜间呼吸频率 {number(vitals['respiratory_rate'])} 次/分钟")
        if isinstance(vitals.get("skin_temperature_delta_c"), (int, float)) and not isinstance(vitals.get("skin_temperature_delta_c"), bool):
            output.append(f"夜间皮肤温度相对设备基线 {float(vitals['skin_temperature_delta_c']):+.1f} °C")
        oxygen = vitals.get("oxygen") or {}
        if isinstance(oxygen.get("median_percent"), (int, float)) and not isinstance(oxygen.get("median_percent"), bool):
            coverage = []
            if oxygen.get("sample_count"):
                coverage.append(f"{number(oxygen['sample_count'], 0)} 条读数")
            if oxygen.get("measured_minutes") is not None:
                coverage.append(f"{number(oxygen['measured_minutes'], 0)} 分钟有记录")
            if oxygen.get("coverage_ratio") is not None:
                coverage.append(f"约 {number(oxygen['coverage_ratio'] * 100, 0)}% 睡眠时段")
            suffix = f"（{'；'.join(coverage)}）" if coverage else ""
            output.append(f"夜间血氧中位数 {number(oxygen['median_percent'])}%{suffix}")
            if oxygen.get("status") == "AVAILABLE" and oxygen.get("lower_10th_percent") is not None:
                output.append(f"夜间血氧较低的十分位 {number(oxygen['lower_10th_percent'])}%")
        if oxygen.get("status") == "AVAILABLE" and oxygen.get("odi_events_per_hour") is not None:
            output.append(f"设备记录夜间血氧下降频率 {number(oxygen['odi_events_per_hour'])} 次/小时")
        for key, label in (("vendor_readiness", "准备度"), ("vendor_charge", "能量")):
            score = recovery.get(key)
            if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 100:
                output.append(f"设备{label}评分 {number(score, 0)}/100（厂商参考值）")
        components = recovery.get("vendor_readiness_components") or {}
        if not isinstance(components, dict):
            components = {}
        for label in ("身体", "心理"):
            score = components.get(label)
            if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 100:
                output.append(f"设备准备度（{label}）{number(score, 0)}/100（厂商参考值）")
        return output

    def _recovery_interpretation(self, features: dict[str, Any], hrv: dict[str, Any], vitals: dict[str, Any]) -> list[str]:
        recovery = features.get("recovery") or {}
        output = []
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
        return unique([
            f"{label}尚无可用记录，本次安排不依据该信号。"
            for label in quality.get("missing_required_signal_labels") or []
        ])

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
        output = [plan.get("safety_status_label", "")] if plan.get("safety_status") == "LIMITED" else []
        for session_key in ("primary_session", "optional_session"):
            output.extend((plan.get(session_key) or {}).get("stop_conditions", []))
        return unique(output)
