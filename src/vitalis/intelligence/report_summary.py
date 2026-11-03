"""Short morning/evening summaries of saved analysis, never a health engine.

Input is a saved DailyProfile payload (or its model); callers own channel wiring.
Directions, states and decisions are quoted, not recomputed from measurements or
thresholds. Events remain untouched for API/audit use. Evening text never carries
an expired daily plan forward as permission to train tomorrow.
"""
from __future__ import annotations

from typing import Any, Literal

from .report_formatting import number, payload_of


_DIRECTION = {"above": "高于", "near": "接近", "below": "低于"}
_SLEEP_DIRECTION = {"above": "多于", "near": "接近", "below": "少于"}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _recovery_evidence(raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Use qualified saved directions; never give an absolute deviation a sign."""
    features = _mapping(raw.get("features"))
    hrv = _mapping(features.get("hrv"))
    sleep = _mapping(features.get("sleep"))
    recovery = _mapping(features.get("recovery"))
    states = _mapping(raw.get("states"))
    missing = _mapping(raw.get("data_quality")).get("missing_required_signals") or []
    negatives = recovery.get("negative_signals") or []
    evidence, gaps = [], []
    hrv_conflict = (
        hrv.get("corroboration_status") in {"conflicting", "insufficient"}
        or hrv.get("corroboration_affects_decision")
    )
    hrv_direction = hrv.get("fusion_direction")
    if hrv_direction not in _DIRECTION:
        hrv_direction = _mapping(hrv.get("deviation")).get("direction")
    sleep_direction = _mapping(sleep.get("duration_deviation")).get("direction")
    if sleep_direction not in _DIRECTION and "SLEEP_SHORT_DURATION" not in negatives:
        sleep_direction = {"ABOVE_BASELINE": "above", "NEAR_BASELINE": "near", "BELOW_BASELINE": "below"}.get(states.get("sleep"))
    rows = (
        ("HRV", hrv, "value_ms", hrv_direction, "HRV_BELOW_BASELINE", "below", "hrv", _DIRECTION),
        ("静息心率", hrv, "rhr_bpm", _mapping(hrv.get("rhr_deviation")).get("direction"),
         "RHR_ABOVE_BASELINE", "above", "resting_hr", _DIRECTION),
        ("睡眠时长", sleep, "duration_minutes", sleep_direction, "SLEEP_BELOW_BASELINE", "below", "sleep_duration", _SLEEP_DIRECTION),
    )
    for label, feature, value_key, direction, negative_code, negative_direction, metric, words in rows:
        short = "睡眠" if metric == "sleep_duration" else label
        if label == "HRV" and hrv_conflict:
            gaps.append("HRV 来源方向冲突或互证不足，无法判断 HRV 恢复方向")
            continue
        # HrvFeatures.status qualifies HRV, not its independently saved RHR.
        unavailable = label != "静息心率" and feature.get("status") not in {None, "AVAILABLE"}
        if metric in missing or (feature and (feature.get(value_key) is None or unavailable)):
            gaps.append(f"{short}记录缺失或不可用，无法判断{short}相对平时的变化")
            continue
        if direction not in words and negative_code in negatives:
            direction = negative_direction
        if direction in words:
            space = " " if label == "HRV" else ""
            evidence.append(f"{label}{space}{words[direction]}个人参照")
            if metric == "sleep_duration" and "SLEEP_SHORT_DURATION" in negatives:
                evidence[-1] += "（分析仍提示睡眠时长不足）"
        elif metric == "sleep_duration" and "SLEEP_SHORT_DURATION" in negatives:
            evidence.append("分析提示睡眠时长不足")
        elif feature and feature.get(value_key) is not None:
            gaps.append(f"{short}个人参照不足，无法判断{short}相对平时的变化")
    return evidence, gaps


def _plan_summary(raw: dict[str, Any], kind: str, *, load_notice: str | None) -> str | None:
    """Keep specific restrictions; ordinary days need no boilerplate advice."""
    decision = _mapping(raw.get("decision"))
    plan = _mapping(decision.get("action_plan"))
    primary = _mapping(plan.get("primary_session"))
    action = decision.get("action")
    when = "今晚" if kind == "evening" else "今天"
    if plan.get("safety_status") == "LIMITED":
        stops = [str(item).split("。", 1)[0] for item in primary.get("stop_conditions") or [] if item]
        return f"训练限制：{stops[0]}。" if stops else "先遵守现有训练限制，不要加练。"
    if action == "REST" or primary.get("session_type") == "REST":
        return f"{when}安排休息，先别加练。"
    if action == "RECOVERY" or primary.get("session_type") == "RECOVERY":
        return f"{when}只安排恢复活动，不加练。"
    if load_notice:
        return f"{load_notice}，累了就适当休息。"
    return None


def reader_briefing_summary(
    payload: Any, *, kind: Literal["morning", "evening"] = "morning", facts_only: bool = False,
) -> list[str]:
    """Return at most three reader sentences, without altering saved facts/plans.

    A facts-only or retrospective delivery emits no interpretations or advice.
    No steps/completeness badges, vendor score substitution, new thresholds,
    fatigue diagnosis, training doses, or general safety assurances are added.
    """
    if kind not in {"morning", "evening"}:
        raise ValueError("reader summaries are only for morning and evening briefings")
    raw = payload_of(payload)
    context = _mapping(raw.get("report_context"))
    metadata = _mapping(context.get("delivery_metadata"))
    if facts_only or raw.get("facts_only") or metadata.get("facts_only") or metadata.get("retrospective"):
        return []
    features = _mapping(raw.get("features"))
    recovery = _mapping(features.get("recovery"))
    training = _mapping(features.get("training"))
    evidence, gaps = _recovery_evidence(raw)
    lines = []
    state = recovery.get("state") or _mapping(raw.get("states")).get("recovery")
    score = number(recovery.get("readiness_score"), 0)
    # State is already resolved by analysis, including readiness vs vote-state
    # differences. A high HRV or low RHR is never relabelled as adverse evidence.
    status = {"GOOD": "恢复较好", "NORMAL": "恢复状态平稳", "SUPPRESSED": "恢复判断偏弱"}.get(state)
    if evidence:
        lead = "、".join(evidence)
        if score and status:
            lead += f"；准备度 {score}/100，{status}"
        lines.append(lead + "。")
    elif score and status:
        lines.append(f"准备度 {score}/100，{status}。")
    events = [event for event in raw.get("events") or [] if isinstance(event, dict)]
    load_event = next((event for event in events if event.get("type") == "TRAINING_LOAD_SPIKE"
                       and event.get("lifecycle") not in {"RESOLVED", "IMPROVING"}), None)
    load_notice = None
    if load_event:
        comparisons = [item for item in raw.get("trends") or [] if isinstance(item, dict)
                       and item.get("metric") == "training_load" and item.get("status") == "AVAILABLE"
                       and item.get("window_days") == 7 and item.get("change_percent") is not None]
        comparison = next((item for item in comparisons if item.get("change_percent") == load_event.get("deviation_percent")), None)
        change = number(comparison.get("change_percent"), 0) if comparison else None
        load_notice = (f"近 7 天训练负荷比前 7 天增加 {change}%" if change else "最近加量比较快")
    elif (
        training.get("load_state") == "ELEVATED"
        or _mapping(raw.get("states")).get("training_load") == "ELEVATED"
        or "TRAINING_LOAD_ELEVATED" in (recovery.get("negative_signals") or [])
    ):
        load_notice = "最近练得比较多"
    history = _mapping(training.get("history_coverage"))
    if history.get("prior_7d_verified") is False:
        gaps.append("此前 7 天训练记录尚未查全，无法判断这段训练量是否完整")
    action = _plan_summary(raw, kind, load_notice=load_notice)
    if action:
        lines.append(action)
    if gaps:
        lines.append("；".join(gaps) + "。")
    return lines
