"""Pure public projections of saved facts; no queries, clocks, or health algorithms."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import math
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .report_formatting import energy_label, metric_label, number, payload_of, unique
from .report_presentation import daily_profile_presentation


PUBLIC_REPORT_SCHEMA_VERSION = "1.0"
PublicKind = Literal["daily", "morning", "evening", "weekly", "monthly"]


SignalStatus = Literal["AVAILABLE", "PARTIAL", "STALE", "UNKNOWN", "INSUFFICIENT"]


class ReportBlock(BaseModel):
    """One reading theme, with each stream's qualification kept beside its value."""

    section_id: str
    title: str
    priority: int = Field(ge=1, le=5)
    status: SignalStatus
    freshness: SignalStatus | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)
    comparisons: list[dict[str, Any]] = Field(default_factory=list)
    interpretation: list[str] = Field(default_factory=list)
    action: str | None = None
    source: list[dict[str, Any]] = Field(default_factory=list)
    unit: str | None = None
    observed_at: datetime | date | None = None
    as_of: datetime | None = None
    coverage: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    value: float | str | None = None
    baseline: float | dict[str, Any] | None = None
    deviation: float | dict[str, Any] | None = None
    workouts: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PublicReportView(BaseModel):
    """The only report content contract consumed by channels."""

    schema_version: Literal["1.0"] = PUBLIC_REPORT_SCHEMA_VERSION
    analysis_run_id: str
    analysis: dict[str, Any] = Field(default_factory=dict)
    kind: PublicKind
    title: str
    user_id: str
    date: date
    period_start: date
    period_end: date
    generated_at: datetime | None = None
    as_of: datetime | None = None
    state: str = "current"
    source_mode: Literal["real", "mock", "replay", "unknown"] = "unknown"
    report_context: dict[str, Any] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    facts_only: bool = False
    late: bool = False
    partial: bool = False
    delivered_as_of: datetime | None = None
    summary: list[str] = Field(default_factory=list)
    blocks: list[ReportBlock] = Field(min_length=3, max_length=5)
    findings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    decision_drivers: list[str] = Field(default_factory=list)
    observation_focus: str | None = None
    alerts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_public_contract(self):
        if self.period_start > self.period_end or self.date != self.period_end:
            raise ValueError("public report requires an explicit, ordered date range")
        if self.kind in {"morning", "daily", "evening"} and self.period_start != self.date:
            raise ValueError("daily public reports must cover their explicit local date")
        identifiers = [block.section_id for block in self.blocks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("public report themes must be unique")
        if self.facts_only:
            if self.findings or self.suggestions or self.decision_drivers or self.observation_focus:
                raise ValueError("facts-only public reports cannot contain conclusions or actions")
            for block in self.blocks:
                if block.interpretation or block.action or block.comparisons or block.baseline is not None or block.deviation is not None:
                    raise ValueError("facts-only blocks cannot contain interpretations or comparisons")
                if any(fact.get("baseline") is not None or fact.get("deviation") is not None for fact in block.facts):
                    raise ValueError("facts-only facts cannot contain comparisons")
                if any(exercise.get("comparison") for workout in block.workouts for exercise in workout.get("exercises") or []):
                    raise ValueError("facts-only workouts cannot contain exercise comparisons")
        return self


_THEMES = {
    "sleep": "睡眠", "recovery": "恢复背景", "activity": "活动",
    "training": "训练记录", "patterns": "变化、反馈与下一步",
}
_METRIC_LABELS = {
    "sleep_duration": "睡眠时长", "sleep_score": "设备睡眠评分",
    "deep_sleep": "深睡", "light_sleep": "浅睡", "rem_sleep": "快速眼动睡眠",
    "awake": "夜间清醒", "sleep_awake": "夜间清醒", "sleep_wake_count": "夜间醒来次数",
    "sleep_regularity": "入睡时刻离散度", "bedtime": "入睡", "wake_time": "醒来",
    "sleep_efficiency": "睡眠效率", "sleep_hrv": "睡眠 HRV", "hrv": "HRV",
    "oxygen": "夜间血氧中位数", "spo2": "血氧", "spo2_median": "夜间血氧中位数",
    "odi": "设备记录血氧下降频率", "skin_temperature": "皮肤温度",
    "skin_temperature_delta": "皮肤温度相对设备基线", "skin_temperature_delta_c": "皮肤温度相对设备基线",
    "nocturnal_heart_rate": "夜间心率", "vendor_readiness": "设备准备度评分（厂商参考值）",
    "vendor_charge": "设备能量评分（厂商参考值）", "readiness": "设备准备度评分（厂商参考值）",
    "charge": "设备能量评分（厂商参考值）", "training_load": "设备训练负荷",
    "training_duration": "训练时长", "workout_count": "已记录训练场次",
    "strength_duration": "力量训练时长", "strength_sessions": "力量训练场次",
    "strength_sets": "明确动作组数", "running_sessions": "跑步场次",
    "running_distance": "跑步距离", "running_duration": "跑步时长",
    "running_pace": "跑步配速", "running_heart_rate": "跑步心率",
    "running_hr_drift": "跑步心率漂移", "vo2_max": "设备 VO₂max", "vo2max": "设备 VO₂max",
    "pai": "设备 PAI", "lactate_threshold": "设备乳酸阈值",
    "lactate_threshold_hr": "设备乳酸阈值心率", "lactate_threshold_pace": "设备乳酸阈值配速",
    "training_state": "设备训练状态", "stress_min": "最低压力评分", "stress_max": "最高压力评分",
    "stress_relaxed_pct": "放松区间", "stress_normal_pct": "正常区间",
    "stress_medium_pct": "中等压力区间", "stress_high_pct": "高压力区间",
    "heart_rate_average": "心率记录均值", "stress_average": "压力记录均值",
    "session_rpe": "训练 RPE", "physical_fatigue": "身体疲劳",
    "mental_state": "精神状态", "muscle_soreness": "肌肉酸痛",
}
_PRESENTATION_METRICS = {
    "sleep": "sleep_duration", "sleep_average": "sleep_duration", "rhr": "resting_hr",
    "steps_average": "steps", "activity_average": "active_minutes",
    "strength_frequency": "strength_sessions",
}
_CONTEXT_FIELDS = {
    "as_of", "timezone", "target_date", "target_day_complete", "period_mode", "period_start", "period_end",
    "reference_period", "reference_period_start", "reference_period_end", "source_mode", "report_state",
    "training_history", "delivery_metadata", "warmup_progress",
}
_COVERAGE_FIELDS = (
    "source", "source_scope", "device_id", "unit", "status", "observed_at", "as_of",
    "sample_count", "distinct_days", "expected_days", "coverage_ratio", "target_day_coverage",
    "baseline_distinct_days", "baseline_expected_days", "baseline_coverage_ratio",
    "period_start", "period_end", "calendar_semantics", "aggregation",
)


def _theme(key: str) -> str:
    key = key.lower()
    if any(part in key for part in ("training_response", "feedback", "association", "goal", "product")):
        return "patterns"
    if any(part in key for part in ("hrv", "rhr", "resting_hr", "recovery", "vitals", "spo2", "oxygen", "odi", "respiratory", "skin_temp", "readiness", "charge", "nocturnal")):
        return "recovery"
    if key.startswith(("sleep", "deep_sleep", "rem_sleep", "light_sleep", "awake", "bedtime", "wake_time")):
        return "sleep"
    if any(part in key for part in ("training", "workout", "strength", "running", "vo2", "lactate", "pai", "cadence", "pace", "drift")):
        return "training"
    if any(part in key for part in ("activity", "steps", "distance", "calorie", "energy", "stress", "heart_rate", "intraday")):
        return "activity"
    return "patterns"


def _label(metric: str, item: dict[str, Any] | None = None) -> str:
    item = item or {}
    if metric == "calories":
        return energy_label(item.get("role"))
    return str(item.get("label") or item.get("metric_label") or _METRIC_LABELS.get(metric) or metric_label(metric))


def _status_name(value: Any, *, available: bool) -> SignalStatus:
    text = str(value or ("AVAILABLE" if available else "UNKNOWN")).upper()
    if text in {"INSUFFICIENT_DATA", "UNAVAILABLE", "REFUSED"}:
        return "INSUFFICIENT"
    return text if text in {"AVAILABLE", "PARTIAL", "STALE", "UNKNOWN", "INSUFFICIENT"} else "UNKNOWN"


def _numeric(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value):
        raise ValueError("public report values must be finite")
    return float(value)


def _date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and len(value) == 10:
        return date.fromisoformat(value)
    raise ValueError("public report requires an explicit date")


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("public report as_of must include a timezone")
    return parsed


def _fact(metric: str, item: dict[str, Any], as_of: datetime | None) -> dict[str, Any]:
    """Copy one saved fact, retaining unknown denominators instead of filling them."""
    output = deepcopy(item)
    provenance = output.pop("provenance", None)
    if isinstance(provenance, dict):
        for key in ("source", "source_scope", "device_id"):
            output.setdefault(key, provenance.get(key))
    output["metric"] = metric
    output["label"] = _label(metric, output)
    for key in ("value", "unit", "source", "source_scope", "device_id", "observed_at", "calendar_semantics",
                "sample_count", "distinct_days", "expected_days", "coverage_ratio", "baseline", "deviation"):
        output.setdefault(key, None)
    if isinstance(output["value"], (int, float)):
        _numeric(output["value"])
    output["status"] = _status_name(output.get("status"), available=output["value"] is not None or bool(output.get("text")))
    output["freshness"] = _status_name(output.get("freshness") or output["status"], available=output["value"] is not None or bool(output.get("text")))
    role = str(output.get("decision_role") or "").lower()
    if role == "shadow" or output.get("shadow_only") is True:
        output["decision_role"] = "shadow"
        output["shadow_only"] = True
        output["decision_role_label"] = "shadow-only"
    else:
        output.setdefault("shadow_only", False)
    target_coverage = output.get("target_day_coverage")
    if isinstance(target_coverage, dict):
        output.setdefault("coverage", deepcopy(target_coverage))
    else:
        output.setdefault("coverage", {
            key: output[key]
            for key in ("status", "sample_count", "distinct_days", "expected_days", "coverage_ratio")
            if output.get(key) is not None
        })
    output["as_of"] = output.get("as_of") or (as_of.isoformat() if as_of else None)
    if output.get("as_of"):
        _datetime(output["as_of"])
    return output


def _saved_signals(context: dict[str, Any], as_of: datetime | None) -> list[dict[str, Any]]:
    """SignalState and PeriodSignalState are saved by analysis, never rebuilt here."""
    output = []
    for metric, streams in (context.get("signals") or {}).items():
        for item in streams if isinstance(streams, list) else []:
            if isinstance(item, dict):
                output.append(_fact(str(metric), item, as_of))
    return output


def _profile_facts(raw: dict[str, Any], as_of: datetime | None) -> list[dict[str, Any]]:
    output = []
    for metric, items in (raw.get("facts") or {}).items():
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and "value" in item:
                output.append(_fact(str(item.get("metric") or metric), item, as_of))
    return output


def _feature_facts(raw: dict[str, Any], as_of: datetime | None) -> list[dict[str, Any]]:
    """Read the current DailyProfile's computed features, including local gaps."""
    features = raw.get("features") or {}
    day = raw.get("date")
    output = []

    def add(metric: str, value: Any, unit: str | None, feature: dict[str, Any], *, deviation: Any = None, semantics: str = "sleep_day", label: str | None = None):
        if value is None:
            return
        entry = {"value": value, "unit": unit, "observed_at": feature.get("observed_at") or day,
                 "calendar_semantics": semantics, "status": feature.get("status"),
                 "provenance": feature.get("provenance"), "deviation": deviation,
                 "sample_count": feature.get("sample_count"), "distinct_days": feature.get("distinct_days"),
                 "expected_days": feature.get("expected_days"), "coverage_ratio": feature.get("coverage_ratio")}
        if isinstance(deviation, dict):
            entry["baseline"] = deviation.get("baseline_reference")
        if label:
            entry["label"] = label
        output.append(_fact(metric, entry, as_of))

    sleep = features.get("sleep") or {}
    for key, metric in (("duration_minutes", "sleep_duration"), ("deep_minutes", "deep_sleep"), ("light_minutes", "light_sleep"),
                        ("rem_minutes", "rem_sleep"), ("awake_minutes", "sleep_awake"), ("regularity_minutes", "sleep_regularity")):
        add(metric, sleep.get(key), "min", sleep, deviation=sleep.get("duration_deviation") if key == "duration_minutes" else None)
    for key, unit in (("bedtime", "clock"), ("wake_time", "clock"), ("wake_count", "times"), ("vendor_sleep_score", "score")):
        add({"wake_count": "sleep_wake_count", "vendor_sleep_score": "sleep_score"}.get(key, key), sleep.get(key), unit, sleep)
    hrv = features.get("hrv") or {}
    if hrv.get("status") in {None, "AVAILABLE"} and not hrv.get("corroboration_affects_decision") and hrv.get("corroboration_status") not in {"conflicting", "insufficient"}:
        add(str(hrv.get("preferred_metric") or "hrv"), hrv.get("value_ms"), "ms", hrv, deviation=hrv.get("deviation"))
    add(str(hrv.get("rhr_metric") or "resting_hr"), hrv.get("rhr_bpm"), "bpm", hrv, deviation=hrv.get("rhr_deviation"))
    vitals = features.get("overnight_vitals") or {}
    for key, metric, unit in (("respiratory_rate", "respiratory_rate", "brpm"), ("skin_temperature_delta_c", "skin_temperature_delta_c", "°C")):
        add(metric, vitals.get(key), unit, vitals, deviation=vitals.get(f"{key}_deviation"))
    oxygen = vitals.get("oxygen") or {}
    add("spo2_median", oxygen.get("median_percent"), "%", oxygen)
    add("odi", oxygen.get("odi_events_per_hour"), "events/hour", oxygen)
    recovery = features.get("recovery") or {}
    for key in ("vendor_readiness", "vendor_charge"):
        add(key, recovery.get(key), "score", {"status": "AVAILABLE"})
    activity = features.get("activity") or {}
    for key in ("steps", "distance_km", "active_minutes"):
        item = activity.get(key)
        if isinstance(item, dict):
            units = {"steps": "steps", "distance_km": "km", "active_minutes": "min"}
            add(key, item.get("value"), item.get("unit") or units[key], item, deviation=item.get("deviation"), semantics="activity_day")
    for item in [*(activity.get("energy") or []), *(activity.get("stress_summary") or [])]:
        if isinstance(item, dict) and item.get("role") != "workout":
            output.append(_fact(str(item.get("metric") or "calories"), {"calendar_semantics": "activity_day", **item}, as_of))
    for metric, unit in (("heart_rate", "bpm"), ("stress", "score")):
        window = activity.get(metric) or {}
        if window.get("sample_count") and window.get("average") is not None:
            output.append(_fact(f"{metric}_average", {**window, "value": window["average"], "unit": unit,
                               "observed_at": window.get("last_observed_at"), "calendar_semantics": "activity_day"}, as_of))
    training = features.get("training") or {}
    for key, metric, unit in (("today_duration_minutes", "training_duration", "min"), ("today_load", "training_load", "load")):
        add(metric, training.get(key), unit, training, semantics="activity_day")
    return output


def _period_facts(raw: dict[str, Any], as_of: datetime | None, days: int) -> list[dict[str, Any]]:
    """Project real period aggregates; the target-day shadow bundle is excluded."""
    facts = raw.get("facts") or {}
    output = []

    def add(metric: str, value: Any, unit: str | None, *, count: Any = None, aggregation: str = "median", source: dict[str, Any] | None = None):
        if value is None and count is None:
            return
        source = source or {}
        entry = {**source, "value": value, "unit": unit, "distinct_days": count,
                 "expected_days": days, "aggregation": aggregation,
                 "period_start": raw.get("period_start"), "period_end": raw.get("period_end"),
                 "calendar_semantics": "sleep_day" if metric == "sleep_duration" else "activity_day",
                 "status": "UNKNOWN" if value is None else "PARTIAL" if isinstance(count, int) and count < days else "AVAILABLE"}
        output.append(_fact(metric, entry, as_of))

    sleep = facts.get("sleep") or {}
    add("sleep_duration", sleep.get("average_minutes"), "min", count=sleep.get("available_days"), aggregation="mean")
    recovery = facts.get("recovery") or {}
    streams = recovery.get("streams") or []
    for stream in streams:
        add(str(stream.get("metric")), stream.get("median"), stream.get("unit"), count=stream.get("available_days"), source=stream)
    if not streams:
        add(str(recovery.get("hrv_metric") or "hrv"), recovery.get("hrv_median_ms"), "ms", count=recovery.get("hrv_available_days"))
        add(str(recovery.get("rhr_metric") or "resting_hr"), recovery.get("rhr_median_bpm"), "bpm", count=recovery.get("rhr_available_days"))
    activity = facts.get("activity") or {}
    for item in activity.get("metrics") or []:
        if isinstance(item, dict):
            add(str(item.get("metric")), item.get("average"), item.get("unit"), count=item.get("available_days"), aggregation="mean", source=item)
    if not any(item.get("metric") == "steps" for item in output):
        add("steps", activity.get("average_steps"), "steps", count=activity.get("available_days"), aggregation="mean")
    training = facts.get("training") or {}
    for key, metric, unit in (("workout_count", "workout_count", "sessions"), ("duration_minutes", "training_duration", "min"),
                             ("vendor_load", "training_load", "load"), ("strength_sessions", "strength_sessions", "sessions"),
                             ("strength_sets", "strength_sets", "sets"), ("strength_duration_minutes", "strength_duration", "min"),
                             ("running_sessions", "running_sessions", "sessions"), ("running_distance_km", "running_distance", "km"),
                             ("running_duration_minutes", "running_duration", "min")):
        value = training.get(key)
        if value == 0 and training.get("coverage_status") != "COMPLETE":
            continue
        add(metric, value, unit, count=training.get("record_days"), aggregation="sum")
    feedback = facts.get("feedback") or {}
    for key, label in (("session_rpe", "训练 RPE"), ("physical_fatigue", "身体疲劳"), ("mental_state", "精神状态"), ("muscle_soreness", "肌肉酸痛")):
        if feedback.get(f"average_{key}") is not None:
            output.append(_fact(f"feedback_{key}", {"label": f"已记录反馈平均{label}", "value": feedback[f"average_{key}"],
                               "unit": "score", "source": "user", "aggregation": "mean", "sample_count": feedback.get("response_count")}, as_of))
    return output


def _presentation_facts(presentation: dict[str, Any], as_of: datetime | None) -> list[dict[str, Any]]:
    output = []
    for metric in presentation.get("metrics") or []:
        key = _PRESENTATION_METRICS.get(str(metric.get("key")), str(metric.get("key")))
        entry = {**metric, "metric": key}
        comparison = entry.pop("comparison", None)
        if isinstance(comparison, dict):
            entry["baseline"] = comparison.get("reference_value")
            entry["deviation"] = {"percent": comparison.get("change_percent")}
            entry["comparison"] = comparison
        output.append(_fact(key, entry, as_of))
    return output


def _eligible_comparison(fact: dict[str, Any]) -> dict[str, Any] | None:
    if fact.get("status") != "AVAILABLE" or fact.get("comparison_available") is False:
        return None
    deviation = fact.get("deviation")
    existing = fact.get("comparison")
    if not isinstance(deviation, (dict, int, float)) and not isinstance(existing, dict):
        return None
    baseline = fact.get("baseline")
    reference = baseline.get("reference_value", baseline.get("median")) if isinstance(baseline, dict) else baseline
    entry = deepcopy(existing) if isinstance(existing, dict) else {}
    entry.update({key: fact.get(key) for key in ("metric", "label", "unit", "source", "source_scope", "device_id", "as_of", "observed_at",
                                              "distinct_days", "expected_days", "coverage_ratio", "baseline_distinct_days", "baseline_coverage_ratio")})
    entry["reference_value"] = entry.get("reference_value", reference)
    entry["label"] = (existing or {}).get("label") or fact.get("baseline_label") or ("上期同源参照" if fact.get("aggregation") else "个人同源参照")
    if isinstance(deviation, dict):
        entry["change_percent"] = deviation.get("percent", deviation.get("change_percent"))
        entry["change_absolute"] = deviation.get("absolute", deviation.get("change_absolute"))
        entry["direction"] = deviation.get("direction")
        entry["baseline_window_days"] = deviation.get("baseline_window_days") or fact.get("baseline_window_days")
    elif _numeric(deviation) is not None:
        entry["change_absolute"] = deviation
    return entry if entry.get("reference_value") is not None or entry.get("change_percent") is not None or entry.get("change_absolute") is not None else None


def _change_text(fact: dict[str, Any]) -> str | None:
    comparison = _eligible_comparison(fact)
    if not comparison:
        return None
    percent = _numeric(comparison.get("change_percent"))
    change = _numeric(comparison.get("change_absolute"))
    label = str(fact.get("label"))
    window = fact.get("window_days")
    if window:
        label = f"近 {window} 日{label}"
    if percent is not None:
        return f"{label}较{comparison['label']}变化 {percent:+.1f}%。"
    if change is not None:
        unit = {"min": "分钟", "steps": "步", "ms": "毫秒", "bpm": "次/分钟"}.get(str(fact.get("unit")), fact.get("unit") or "单位未记录")
        return f"{label}较{comparison['label']}变化 {number(change)} {unit}。"
    return None


def _cross_domain_findings(facts: list[dict[str, Any]], existing: list[str]) -> list[str]:
    by_theme: dict[str, list[str]] = {key: [] for key in _THEMES}
    for fact in facts:
        text = _change_text(fact)
        if text:
            by_theme[_theme(str(fact.get("metric")))].append(text)
    # Select domains before selecting another change in the same domain.
    selected = [items[0] for items in by_theme.values() if items]
    remainder = [text for items in by_theme.values() for text in items[1:]]
    return unique(selected + existing + remainder)[:3]


def _status(facts: list[dict[str, Any]], workouts: list[dict[str, Any]]) -> SignalStatus:
    states = {str(item.get("status") or "UNKNOWN") for item in facts}
    states.update(str(item.get("status") or "AVAILABLE") for item in workouts)
    if not states:
        return "UNKNOWN"
    if len(states) == 1:
        return _status_name(next(iter(states)), available=False)
    return "PARTIAL"


def _period_details(context: dict[str, Any], as_of: datetime | None, *, facts_only: bool, days: int = 1) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts, comparisons = [], []
    response = context.get("training_response_summary") or {}
    if isinstance(response, dict):
        for window in response.get("window_summaries") or []:
            if not isinstance(window, dict) or window.get("day_offset") not in {0, 1, 2, 3}:
                continue
            offset = window["day_offset"]
            parts = [f"T+{offset} 训练反应"]
            observed, expected = window.get("observed"), window.get("expected")
            if observed is not None and expected is not None:
                parts.append(f"已观察 {observed}/{expected} 项到期信号")
            for key, label in (("not_due_count", "尚未到达"), ("missing_count", "数据缺失"), ("partial_count", "部分记录"), ("confounded_count", "混杂")):
                if window.get(key) is not None:
                    parts.append(f"{label} {window[key]} 个窗口")
            ratio = _numeric(window.get("coverage"))
            if ratio is not None:
                parts.append(f"覆盖 {ratio:.0%}")
            facts.append(_fact(f"training_response_T+{offset}", {**window, "text": " · ".join(parts),
                               "label": f"T+{offset} 训练反应", "source": "vitalis", "unit": "observations",
                               "sample_count": observed, "coverage_ratio": ratio, "as_of": window.get("as_of") or response.get("as_of"),
                               "status": "AVAILABLE" if expected and expected == observed else "PARTIAL" if observed else "INSUFFICIENT"}, as_of))
        for distribution in response.get("feedback_distributions") or []:
            if not isinstance(distribution, dict):
                continue
            metric = str(distribution.get("metric") or "feedback")
            label = _METRIC_LABELS.get(metric, "已记录主观反馈")
            offset = distribution.get("day_offset")
            prefix = f"T+{offset} " if offset in {0, 1, 2, 3} else ""
            observed, eligible = distribution.get("sample_count"), distribution.get("eligible_count")
            text = f"{prefix}{label}：样本 {observed if observed is not None else '未记录'}/{eligible if eligible is not None else '未记录'}"
            facts.append(_fact(f"feedback_{metric}", {**distribution, "label": prefix + label, "text": text,
                               "value": distribution.get("median"), "source": "user", "aggregation": "median"}, as_of))
    associations = context.get("personal_associations") or []
    for association in associations if isinstance(associations, list) else []:
        if not isinstance(association, dict):
            continue
        item = deepcopy(association)
        available = _status_name(item.get("status"), available=False) == "AVAILABLE"
        parts = []
        if available and not facts_only and item.get("summary"):
            parts.append(str(item["summary"]))
        paired, expected = item.get("paired_days"), item.get("expected_pair_days")
        if paired is not None:
            parts.append(f"配对 {paired}" + (f"/{expected}" if expected is not None else "") + " 天")
        if item.get("sample_count") is not None:
            parts.append(f"分析样本 {item['sample_count']}")
        for key, label in (("coverage_ratio", "配对覆盖"), ("confounded_ratio", "混杂比例")):
            ratio = _numeric(item.get(key))
            if ratio is not None:
                parts.append(f"{label} {ratio:.0%}")
        if available and not facts_only:
            if item.get("coefficient") is not None:
                parts.append(f"关联系数 {number(item['coefficient'], 2)}")
            if item.get("q_value") is not None:
                parts.append(f"BH q={number(item['q_value'], 2)}")
            if item.get("confidence_label"):
                parts.append(f"置信度 {item['confidence_label']}")
            comparisons.append({"type": "association", **item})
        else:
            for key in ("summary", "coefficient", "p_value", "q_value", "direction", "direction_label", "strength", "strength_label"):
                item.pop(key, None)
        if parts:
            parts.append("个人数据观测关联，不表示因果")
            facts.append(_fact("personal_association", {**item, "text": " · ".join(parts), "label": "个人数据关联",
                               "value": None, "unit": None, "source": "vitalis", "as_of": item.get("as_of")}, as_of))
    if not facts_only and days > 1:
        for trend in context.get("long_term_trends") or []:
            if not isinstance(trend, dict) or trend.get("window_days") not in {28, 60, 90}:
                continue
            if _status_name(trend.get("status"), available=False) != "AVAILABLE" or trend.get("comparison_available") is not True:
                continue
            current = _numeric(trend.get("current_value"))
            if current is None:
                current = _numeric(trend.get("current_median"))
            if current is None:
                continue
            previous = _numeric(trend.get("previous_median"))
            change_percent = _numeric(trend.get("change_percent"))
            change_absolute = _numeric(trend.get("change_absolute"))
            window = int(trend["window_days"])
            label = str(trend.get("metric_label") or _label(str(trend.get("metric") or "trend"), trend))
            coverage_ratio = _numeric(trend.get("coverage_ratio"))
            previous_coverage = _numeric(trend.get("previous_coverage_ratio"))
            parts = [f"近 {window} 日{label}：当前 {number(current)} {trend.get('unit') or '单位未记录'}"]
            if trend.get("period_start") and trend.get("period_end"):
                parts.append(f"窗口 {trend['period_start']}—{trend['period_end']}")
            if trend.get("current_distinct_days") is not None and trend.get("expected_days"):
                parts.append(f"覆盖 {trend['current_distinct_days']}/{trend['expected_days']} 天")
            if coverage_ratio is not None:
                parts.append(f"{coverage_ratio:.0%}")
            if previous is not None:
                parts.append(f"前期 {number(previous)}")
            if change_percent is not None:
                parts.append(f"变化 {change_percent:+.1f}%")
            if trend.get("previous_distinct_days") is not None and trend.get("previous_expected_days"):
                parts.append(f"前期覆盖 {trend['previous_distinct_days']}/{trend['previous_expected_days']} 天")
                if previous_coverage is not None:
                    parts.append(f"{previous_coverage:.0%}")
            trend_as_of = trend.get("as_of")
            if isinstance(trend_as_of, str) and "T" not in trend_as_of:
                trend_as_of = None
            facts.append(_fact(
                f"long_term_trend_{trend.get('metric')}_{window}",
                {**trend, "text": " · ".join(parts), "label": label, "value": current,
                 "baseline": previous, "deviation": {"percent": change_percent, "absolute": change_absolute}
                 if change_percent is not None or change_absolute is not None else None,
                 "source": trend.get("source"), "as_of": trend_as_of,
                 "sample_count": trend.get("current_sample_count"), "distinct_days": trend.get("current_distinct_days"),
                 "expected_days": trend.get("expected_days"), "coverage_ratio": coverage_ratio,
                 "baseline_distinct_days": trend.get("previous_distinct_days"),
                 "baseline_expected_days": trend.get("previous_expected_days"),
                 "baseline_coverage_ratio": previous_coverage},
                as_of,
            ))
    product = context.get("product_summary") or {}
    if isinstance(product, dict):
        for key, label in (("feedback_count", "已记录报告反馈"), ("useful_count", "明确评价有用"),
                           ("completed_count", "明确记录建议完成"), ("correction_count", "已记录数据纠错")):
            if product.get(key) is not None:
                facts.append(_fact(f"product_{key}", {"label": label, "value": product[key], "unit": "times",
                                   "source": "user", "as_of": product.get("as_of")}, as_of))
        for goal in product.get("goals") or []:
            if not isinstance(goal, dict):
                continue
            parts = [str(goal.get("label") or goal.get("goal_type_label") or "已记录目标")]
            if goal.get("target_value") is not None:
                parts.append(f"目标 {number(goal['target_value'])} {goal.get('unit') or '单位未记录'}")
            if goal.get("observed_value") is not None:
                parts.append(f"已记录 {number(goal['observed_value'])} {goal.get('unit') or '单位未记录'}")
            if goal.get("target_date"):
                parts.append(f"目标日期 {goal['target_date']}")
            facts.append(_fact("product_goal", {**goal, "text": " · ".join(parts), "source": "user"}, as_of))
    return facts, comparisons


def _presentation(raw: dict[str, Any], kind: PublicKind) -> dict[str, Any]:
    # Briefing payloads already contain their display projection; rebuilding
    # them here would discard caller-supplied findings and safety notes.
    if "features" not in raw and ("metrics" in raw or raw.get("period") in {"morning", "evening", "weekly", "monthly"}):
        return raw
    if kind == "daily":
        return {**raw, **daily_profile_presentation(raw)}
    if kind == "morning":
        from .morning_briefing import MorningBriefingEngine
        return MorningBriefingEngine().build_payload(raw)
    if kind == "evening":
        from .evening_briefing import EveningBriefingEngine
        return EveningBriefingEngine().build_payload(raw)
    if kind == "weekly":
        from .weekly_briefing import WeeklyBriefingEngine
        return WeeklyBriefingEngine().build_payload(raw)
    from .monthly_briefing import MonthlyBriefingEngine
    return MonthlyBriefingEngine().build_payload(raw)


def report_presentation(value: Any, kind: PublicKind) -> dict[str, Any]:
    """The curated reading content a report engine built for one saved report."""
    return _presentation(payload_of(value), kind)


def _public_context(context: dict[str, Any], *, facts_only: bool) -> dict[str, Any]:
    result = {key: deepcopy(value) for key, value in context.items() if key in _CONTEXT_FIELDS}
    if facts_only:
        metadata = result.get("delivery_metadata") or {}
        result["delivery_metadata"] = {key: value for key, value in metadata.items() if key in {
            "facts_only", "partial", "late", "delivered_as_of", "scheduled_for", "deadline_at",
            "required_signals", "missing_signals", "coverage_reason", "sync_degraded", "sync_status", "retrospective",
        }}
    return result


def to_public_report_view(value: Any, kind: PublicKind | None = None) -> PublicReportView:
    if isinstance(value, PublicReportView):
        if kind is not None and kind != value.kind:
            raise ValueError("public report kind mismatch")
        return value
    raw = payload_of(value)
    if "blocks" in raw and "kind" in raw:
        report = PublicReportView.model_validate(raw)
        if kind is not None and kind != report.kind:
            raise ValueError("public report kind mismatch")
        return report
    inferred = kind or raw.get("period") or ("daily" if "features" in raw else None)
    if inferred is None and ("decision_action" in raw or raw.get("schema_version") == "4.0"):
        inferred = "morning"
    if inferred is None and raw.get("period_start") and raw.get("period_end"):
        try:
            period_days = (_date(raw["period_end"]) - _date(raw["period_start"])).days + 1
        except (TypeError, ValueError):
            period_days = 0
        inferred = "weekly" if period_days == 7 else "monthly" if 28 <= period_days <= 31 else "daily" if period_days == 1 else None
    if inferred not in {"daily", "morning", "evening", "weekly", "monthly"}:
        raise ValueError("unsupported public report kind")
    if "features" not in raw and raw.get("period") and raw["period"] != inferred:
        raise ValueError("public report kind mismatch")
    day = _date(raw.get("date") or raw.get("period_end"))
    start = _date(raw.get("period_start") or day)
    end = _date(raw.get("period_end") or day)
    days = (end - start).days + 1
    presentation = _presentation(raw, inferred)
    context = {**deepcopy(raw.get("report_context") or {}), **deepcopy(presentation.get("report_context") or {})}
    metadata = {**(context.get("delivery_metadata") or {}), **(raw.get("delivery_metadata") or {})}
    if metadata:
        context["delivery_metadata"] = metadata
    as_of = _datetime(raw.get("as_of") or context.get("as_of"))
    facts_only = bool(metadata.get("facts_only") or raw.get("facts_only"))
    signals = _saved_signals(context, as_of)
    facts = list(signals)
    held_metrics = {item["metric"] for item in signals}
    measured = _profile_facts(raw, as_of)
    facts.extend(item for item in measured if item["metric"] not in held_metrics)
    held_metrics.update(item["metric"] for item in measured)
    profile_rows = _period_facts(raw, as_of, days) if inferred in {"weekly", "monthly"} else _feature_facts(raw, as_of)
    facts.extend(item for item in profile_rows if item["metric"] not in held_metrics)
    held_metrics.update(item["metric"] for item in profile_rows)
    facts.extend(item for item in _presentation_facts(presentation, as_of) if item["metric"] not in held_metrics)
    period_facts, association_comparisons = _period_details(context, as_of, facts_only=facts_only, days=days)
    facts.extend(period_facts)
    grouped: dict[str, list[dict[str, Any]]] = {theme: [] for theme in _THEMES}
    for fact in facts:
        theme = _theme(str(fact["metric"]))
        if inferred == "evening" and theme == "sleep":
            theme = "recovery"
        grouped[theme].append(fact)
    # These are current briefing audit sections, selected narrowly by content.
    # Numeric signal streams above always win; internal qualification prose is
    # never promoted wholesale to the public report.
    section_themes = {"sleep": "sleep", "daily_sleep": "sleep", "recovery": "recovery",
                      "display_recovery": "recovery", "display_activity": "activity", "display_signals": "activity",
                      "yesterday_activity": "activity", "today_activity": "activity",
                      "daily_facts": "patterns", "daily_quality": "patterns", "daily_audit": "patterns",
                      # Briefing sections retain their already-qualified prose;
                      # only the period-relevant fact groups are promoted below.
                      "activity": "activity", "coverage": "patterns", "sleep_recovery": "sleep",
                      "training": "training", "training_activity": "training", "activity_feedback": "activity",
                      "associations": "patterns", "actions": "patterns"}
    if inferred == "evening":
        full_section_keys = {"activity"}
    elif inferred in {"weekly", "monthly"}:
        full_section_keys = {"coverage", "sleep_recovery", "training", "training_activity", "activity_feedback", "associations", "actions"}
    elif inferred == "morning" and facts_only:
        full_section_keys = {"yesterday_activity", "observed_training", "today_activity"}
    else:
        full_section_keys = set()
    for section in presentation.get("sections") or []:
        key = section.get("key")
        theme = section_themes.get(key)
        if not theme:
            continue
        # DailyProfile audit sections are part of the complete daily snapshot;
        # briefing sections are promoted only when their period-specific public
        # facts are needed (for example evening activity and period totals).
        if "features" in raw and key not in {"daily_facts", "daily_quality", "daily_audit"}:
            continue
        if key not in {"daily_facts", "daily_quality", "daily_audit"} and key not in full_section_keys:
            continue
        target_theme = "recovery" if inferred == "evening" and theme == "sleep" else theme
        if key in {"daily_facts", "daily_quality", "daily_audit"}:
            title = str(section.get("title") or "")
            if title and not any(item.get("text") == title for item in grouped[target_theme]):
                grouped[target_theme].append(_fact(f"{key}_section", {"text": title, "source": "vitalis"}, as_of))
            for text in section.get("facts") or []:
                text = str(text)
                if text and not any(item.get("text") == text for item in grouped[target_theme]):
                    grouped[target_theme].append(_fact(f"{key}_record", {"text": text, "label": title, "source": "vitalis"}, as_of))
            continue
        if key in {"yesterday_activity", "observed_training", "today_activity"}:
            for text in section.get("facts") or []:
                text = str(text)
                if text and not any(item.get("text") == text for item in grouped[target_theme]):
                    grouped[target_theme].append(_fact(f"{key}_record", {"text": text, "label": section.get("title"), "source": "vitalis"}, as_of))
            continue
        if key in full_section_keys:
            for text in section.get("facts") or []:
                text = str(text)
                if facts_only and any(term in text for term in ("个人参照", "个人基线", "近期同源", "较自己", "较个人", "高于自己的", "低于自己的", "变化 +", "变化 -")):
                    continue
                if key == "associations" and "不表示因果" not in text:
                    text += "；个人数据观测关联，不表示因果"
                # Running class counts are useful internal bookkeeping, but
                # the public period block keeps the measured dose and coverage.
                if key in {"training", "training_activity"} and "本次分析的课型" in text:
                    continue
                if text and not any(item.get("text") == text for item in grouped[target_theme]):
                    grouped[target_theme].append(_fact(f"{key}_record", {"text": text, "label": section.get("title"), "source": "vitalis"}, as_of))
            continue
        for text in section.get("facts") or []:
            text = str(text)
            if any(term in text for term in ("个人参照", "个人基线", "近期同源", "近 7 日", "此前 7 日", "暂不比较", "信号显示")):
                continue
            clock_fact = text.startswith(("入睡 ", "醒来 "))
            if grouped[target_theme] and not clock_fact:
                continue
            if text and not any(item.get("text") == text for item in grouped[target_theme]):
                grouped[target_theme].append(_fact(f"{section['key']}_record", {"text": text, "label": section.get("title")}, as_of))
    quality = deepcopy(raw.get("data_quality") or presentation.get("data_quality") or {})
    if facts_only:
        quality = {key: item for key, item in quality.items() if key in {"status", "coverage"}}
    if inferred == "daily" and quality.get("status"):
        status_label = quality.get("status_label") or {"SUFFICIENT": "数据完整", "PARTIAL": "部分可用", "INSUFFICIENT": "数据不足"}.get(quality["status"], "资格未确认")
        grouped["patterns"].append(_fact("data_quality", {"text": f"数据质量与覆盖：{status_label}", "source": "vitalis"}, as_of))
    workouts = deepcopy(presentation.get("training") or [])
    workouts.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("started_at") or "")))
    decision = raw.get("decision") or {}
    drivers = unique([str(item) for item in decision.get("driver_labels") or []] +
                     [str(item["text"]) for item in presentation.get("key_reasons") or [] if isinstance(item, dict) and item.get("text")]) if inferred == "morning" and not facts_only else []
    fact_texts = {
        str(item.get("text"))
        for rows in grouped.values()
        for item in rows
        if item.get("text")
    }
    findings = [] if facts_only else [
        item for item in _cross_domain_findings(facts, [str(item) for item in presentation.get("findings") or []])
        if item not in fact_texts
    ]
    suggestions = [] if facts_only or metadata.get("retrospective") else unique([str(item) for item in presentation.get("suggestions") or []])
    observation_focus = context.get("observation_focus") if not facts_only else None
    if not observation_focus and inferred == "morning" and suggestions:
        observation_focus = next(iter(drivers or findings), None)
    if inferred in {"weekly", "monthly"} and not facts_only:
        product = context.get("product_summary") or {}
        proposal = product.get("next_experiment") if isinstance(product, dict) else None
        proposal = proposal if isinstance(proposal, dict) else {}
        experiment_title = str(proposal.get("title") or "").strip()
        experiment_source = str(proposal.get("source") or "user") if experiment_title else "vitalis"
        if not experiment_title:
            experiment_title = str((suggestions or [observation_focus or ""])[0] or "").strip()
        if experiment_title:
            accepted = proposal.get("accepted") is True
            state = "已确认" if accepted else "待用户确认"
            grouped["patterns"].append(_fact(
                "next_experiment", {
                    "text": f"下一周期可验证重点（{state}）：{experiment_title}",
                    "label": "下一周期可验证重点", "source": experiment_source,
                    "status": "AVAILABLE", "as_of": product.get("as_of") if "T" in str(product.get("as_of") or "") else None,
                }, as_of,
            ))
    if facts_only:
        for fact in facts:
            fact["baseline"] = fact["deviation"] = None
            for key in ("comparison", "previous_average", "previous_total", "previous_median", "change_percent", "total_change_percent"):
                fact.pop(key, None)
        for workout in workouts:
            for exercise in workout.get("exercises") or []:
                exercise["comparison"] = None
                exercise["reference_date"] = None
    order = ("sleep", "recovery", "training", "activity", "patterns") if inferred == "morning" else ("activity", "training", "recovery", "patterns") if inferred == "evening" else ("sleep", "recovery", "activity", "training", "patterns")
    blocks = []
    for priority, theme in enumerate(order, start=1):
        rows = grouped[theme]
        training_rows = workouts if theme == "training" else []
        sources, coverage = [], {}
        for fact in rows:
            provenance = {key: fact.get(key) for key in ("source", "source_scope", "device_id")}
            if fact.get("source") and provenance not in sources:
                sources.append(provenance)
            coverage.setdefault(str(fact["metric"]), []).append({key: fact.get(key) for key in _COVERAGE_FIELDS})
        first = next((item for item in rows if item.get("value") is not None), rows[0] if rows else {})
        comparisons = [] if facts_only else [item for fact in rows if (item := _eligible_comparison(fact)) is not None]
        if theme == "patterns" and not facts_only:
            comparisons.extend(association_comparisons)
        interpretation = findings + ([f"决策依据：{item}" for item in drivers[:3]] if inferred == "morning" else []) if theme == "patterns" else []
        if theme == "patterns" and observation_focus:
            interpretation.append(f"观察重点：{observation_focus}")
        title = "昨夜恢复背景" if inferred == "evening" and theme == "recovery" else "昨天的活动与今天截至分析时的记录" if inferred == "morning" and theme == "activity" else _THEMES[theme]
        block_status = _status(rows, training_rows)
        blocks.append(ReportBlock(
            section_id=theme, title=title, priority=priority, status=block_status,
            freshness=block_status, facts=rows, comparisons=comparisons, interpretation=unique(interpretation),
            action=suggestions[0] if theme == "patterns" and suggestions else None,
            source=sources, unit=first.get("unit"), value=first.get("value"),
            baseline=None if facts_only else first.get("baseline"), deviation=None if facts_only else first.get("deviation"),
            observed_at=first.get("observed_at"), as_of=_datetime(first.get("as_of")) or as_of,
            coverage=coverage, workouts=training_rows,
        ))
    state = context.get("report_state") or {}
    mode = context.get("source_mode") or "unknown"
    versions = {key: raw[key] for key in ("schema_version", "intelligence_version", "decision_policy_version", "evidence_version") if raw.get(key)}
    versions.update(context.get("analysis_versions") or {})
    audit = {key: deepcopy(item) for key, item in (raw.get("metadata") or {}).items() if key in {"profile_revision_used", "input_revision_used", "correction_chain", "supersedes"}}
    summary = []
    if facts_only:
        summary = ["部分运动记录来源还未查全；已记录的训练照常展示，今天暂不生成训练安排。"] if metadata.get("coverage_reason") else ["仅展示已保存事实。"]
    title = "已记录昨夜事实" if inferred == "morning" and facts_only else "已记录本日事实" if inferred in {"daily", "evening"} and facts_only else "已记录周期事实" if facts_only else str(presentation.get("headline") or "已记录数据回顾")
    return PublicReportView(
        analysis_run_id=str(raw.get("analysis_run_id") or ""), analysis={"versions": versions, **audit}, user_id=str(raw.get("user_id") or ""),
        kind=inferred, title=title, date=day, period_start=start, period_end=end, generated_at=_datetime(raw.get("generated_at")),
        as_of=as_of, state=str(state.get("state") or "current"), source_mode=mode if mode in {"real", "mock", "replay"} else "unknown",
        report_context=_public_context(context, facts_only=facts_only), data_quality=quality,
        facts_only=facts_only, late=bool(metadata.get("late")), partial=bool(metadata.get("partial")), delivered_as_of=_datetime(metadata.get("delivered_as_of")),
        summary=summary, blocks=blocks, findings=findings, suggestions=suggestions, decision_drivers=drivers,
        observation_focus=str(observation_focus) if observation_focus else None,
        alerts=[] if facts_only else unique([str(item) for item in presentation.get("alerts") or []]),
    )
