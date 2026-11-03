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
_ACTION = {"TRAIN_HARD": "高负荷训练", "TRAIN_NORMAL": "正常训练", "TRAIN_LIGHT": "轻量训练"}


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
    decision = _mapping(raw.get("decision"))
    plan = _mapping(decision.get("action_plan"))
    primary = _mapping(plan.get("primary_session"))
    action = decision.get("action")
    evening = kind == "evening"
    prefix = (f"{load_notice}；" if "改善" in (load_notice or "") else f"{load_notice}，但不等于已经疲劳；") if load_notice else ""
    ending = "，明日安排以次晨更新为准" if evening else ""
    if plan.get("safety_status") == "LIMITED":
        stops = [str(item).split("。", 1)[0] for item in primary.get("stop_conditions") or [] if item]
        limit = f"训练限制仍有效：{stops[0]}" if stops else "原有训练限制仍有效"
        return prefix + limit + "；即使主观不累，也不解除限制或加练" + ending + "。"
    if action == "REST" or primary.get("session_type") == "REST":
        when = "目前" if evening else "今天"
        return prefix + f"{when}安排休息；即使主观不累，也先不加练" + ending + "。"
    if action == "RECOVERY" or primary.get("session_type") == "RECOVERY":
        when = "目前" if evening else "今天"
        return prefix + f"{when}只安排恢复活动；即使主观不累，也不升级训练或加量" + ending + "。"
    if action not in _ACTION or not primary:
        if load_notice:
            return prefix + "目前没有可沿用的训练安排，不另加训练" + ending + "。"
        return None
    action_label = _ACTION[action]
    if primary.get("intensity") == "low":
        action_label = "轻量训练"
    elif action == "TRAIN_HARD" and primary.get("intensity") == "moderate":
        action_label = "正常训练"
    if evening:
        if load_notice:
            return (
                prefix + "明日若次晨计划允许训练，且不累、热身正常，可按晨报计划练、不额外加量；"
                "明显疲劳或热身异常时，不勉强完成，仍遵守计划中的减量或停止要求。"
            )
        return "今晚照顾好睡眠，明日训练以次晨更新的安排为准。"
    if load_notice:
        return (
            prefix + f"若无明显疲劳且热身正常，今天按计划{action_label}、不额外加量；"
            "若明显疲劳或热身异常，不勉强完成，仍遵守计划中的减量或停止要求。"
        )
    return f"今天按计划{action_label}，原有训练限制不变。"


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
    load_event = next((event for event in events if event.get("type") == "TRAINING_LOAD_SPIKE" and event.get("lifecycle") != "RESOLVED"), None)
    load_notice = None
    if load_event and load_event.get("lifecycle") == "IMPROVING":
        load_notice = "前期训练加量提示正在改善"
    elif load_event:
        comparisons = [item for item in raw.get("trends") or [] if isinstance(item, dict)
                       and item.get("metric") == "training_load" and item.get("status") == "AVAILABLE"
                       and item.get("window_days") == 7 and item.get("change_percent") is not None]
        comparison = next((item for item in comparisons if item.get("change_percent") == load_event.get("deviation_percent")), None)
        change = number(comparison.get("change_percent"), 0) if comparison else None
        load_notice = (f"近 7 天训练负荷比前 7 天增加 {change}%" if change else "近期训练负荷增加较快")
    elif (
        training.get("load_state") == "ELEVATED"
        or _mapping(raw.get("states")).get("training_load") == "ELEVATED"
        or "TRAINING_LOAD_ELEVATED" in (recovery.get("negative_signals") or [])
    ):
        load_notice = "近期训练负荷高于个人参照"
    history = _mapping(training.get("history_coverage"))
    if history.get("prior_7d_verified") is False:
        gaps.append("此前 7 天训练记录尚未查全，无法判断这段训练量是否完整")
    action = _plan_summary(raw, kind, load_notice=load_notice)
    if action:
        lines.append(action)
    if gaps:
        lines.append("；".join(gaps) + "。")
    return lines
