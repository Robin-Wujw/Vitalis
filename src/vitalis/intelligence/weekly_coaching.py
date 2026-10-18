"""Evidence-bounded coaching saved with the weekly analysis, before rendering."""

from collections import defaultdict
from datetime import date, timedelta

from .contracts import ConfidenceBand, WeeklyFacts, WeeklyRecommendation
from .running import RunningAnalyzer
from .strength import MUSCLE_LABELS, StrengthAnalyzer

_SAFETY_EVENTS = {
    "RECOVERY_SUPPRESSED", "HRV_DROP", "RHR_ELEVATED", "TRAINING_LOAD_SPIKE",
    "TRAINING_GAP", "SLEEP_DEFICIT", "BODY_SIGNAL_WARNING",
}
_SAFETY_ACTIONS = {
    "RESPECT_PAIN_OR_INJURY", "PRIORITIZE_RECOVERY", "IMPROVE_SLEEP_DURATION",
    "REDUCE_SUBJECTIVE_LOAD", "WEEKLY_INSUFFICIENT_DATA",
}
_SPLIT_SEQUENCES = {
    "PUSH_PULL_LEGS": "推→拉→腿",
    "FIVE_DAY": "胸→背→腿→肩→手臂",
}
_FREQUENCY_SEQUENCES = {
    2: "全身 A/B 交替",
    3: "上肢→下肢→全身（也可全身 A/B 交替）",
    4: "上肢→下肢→上肢→下肢",
}
_QUALITY_TYPES = {"TEMPO_RUN", "INTERVAL_RUN"}


def build_weekly_coaching(
    raw,
    period_start: date,
    facts: WeeklyFacts,
    events,
    coverage: dict,
    recommendations: list[WeeklyRecommendation],
) -> list[WeeklyRecommendation]:
    """Keep observations visible when safety or coverage prevents a new plan.

    ``raw.day`` must already be the completed report period end. The existing
    weekly actions remain authoritative; coaching can only narrow their scope.
    Muscle distribution counts observed training days, never inferred hard sets.
    """
    strength = StrengthAnalyzer().analyze(raw)
    running = RunningAnalyzer().analyze(raw)
    blocked = _plan_blocked(raw, facts, events, coverage, recommendations)
    output = []
    if strength.sessions_7d:
        output.append(_strength_coaching(raw, period_start, strength, blocked, coverage))
    if running.sessions_7d:
        output.append(_running_coaching(raw, period_start, running, blocked, strength.sessions_7d))
    return [
        item.model_copy(update={"priority": len(recommendations) + index})
        for index, item in enumerate(output, start=1)
    ]


def _plan_blocked(raw, facts, events, coverage, recommendations) -> bool:
    preferences = raw.training_preferences
    return bool(
        coverage.get("coverage_status") != "COMPLETE"
        or coverage.get("unknown_days")
        or coverage.get("totals_are_partial")
        or (
            preferences is not None
            and preferences.pain_or_injury_status == "PRESENT"
        )
        or any(
            item.type in _SAFETY_EVENTS and item.lifecycle != "RESOLVED"
            for item in events
        )
        or any(item.code in _SAFETY_ACTIONS for item in recommendations)
        or (facts.feedback.average_physical_fatigue or 0) >= 4
        or (facts.feedback.average_muscle_soreness or 0) >= 4
    )


def _strength_coaching(raw, start, analysis, blocked, coverage):
    sessions = [item for item in analysis.recent_sessions if start <= item.date <= raw.day]
    known = [item for item in sessions if item.muscle_groups and item.explicit_exercises]
    muscle_days = defaultdict(set)
    for session in known:
        for muscle in session.muscle_groups:
            muscle_days[muscle].add(session.date)
    distribution = "、".join(
        f"{MUSCLE_LABELS[muscle]} {len(days)} 天"
        for muscle, days in sorted(muscle_days.items())
    )
    reasons = [(
        f"{start:%m-%d} 至 {raw.day:%m-%d}：力量训练 {analysis.sessions_7d} 次；"
        f"动作可识别肌群 {len(known)}/{analysis.sessions_7d} 场。"
    )]
    if distribution:
        reasons.append(f"已记录的肌群接触天数：{distribution}；复合动作可涉及多个部位。")
    truncated = len(sessions) < analysis.sessions_7d
    if truncated:
        reasons.append(f"动作分析仅覆盖最近 {len(sessions)}/{analysis.sessions_7d} 场，不代表完整肌群分布。")
    if coverage.get("coverage_status") != "COMPLETE":
        reasons.append("训练覆盖未完整核实；未记录的日期和部位保持未知。")
    if blocked or truncated or any(
        (item.muscle_soreness or 0) >= 4 or (item.session_rpe or 0) >= 9
        for item in sessions
    ):
        action = "训练覆盖或恢复、疼痛限制尚未解除，暂不编排下周肌群分化；以上仅汇总已记录训练。"
    elif len(known) < analysis.sessions_7d:
        action = "本周部分动作或目标肌群未知，暂不编排肌群分化；补齐明确动作后再评估部位安排。"
    else:
        preferences = raw.training_preferences
        frequency = (
            preferences.weekly_strength_target if preferences is not None
            else len({item.date for item in sessions})
        )
        if preferences is not None and preferences.available_weekdays:
            frequency = min(frequency, len(preferences.available_weekdays))
        if preferences is None:
            reasons.append(f"未设置每周次数；以下仅以本周已记录的 {frequency} 个力量训练日作为候选。")
        else:
            reasons.append(
                f"已设置每周力量训练 {preferences.weekly_strength_target} 次；"
                f"结合可训练日，本次按 {frequency} 次选择候选分化。"
            )
        sequence = _SPLIT_SEQUENCES.get(analysis.detected_split)
        if sequence and analysis.split_confidence == ConfidenceBand.HIGH:
            action = f"保留记录中已识别的{analysis.detected_split_label}，按{sequence}继续轮转，不强求自然周内完成一轮。"
        elif frequency in _FREQUENCY_SEQUENCES:
            action = f"下周 {frequency} 次力量训练可按{_FREQUENCY_SEQUENCES[frequency]}安排。"
        else:
            action = "当前次数尚不足以自动选择合适分化，先保留已知动作模式，待更多可比较训练后再评估。"
        if sequence or frequency in _FREQUENCY_SEQUENCES:
            action += "同一肌群的较重训练间留恢复日；按实际可用时间顺延，不补课、不加量追赶，次晨再按恢复调整。"
    return WeeklyRecommendation(
        priority=1, code="WEEKLY_COACH_STRENGTH", title="肌群覆盖与下周分化",
        action=action, reasons=reasons,
    )


def _running_coaching(raw, start, analysis, blocked, strength_sessions):
    sessions = [item for item in analysis.recent_sessions if start <= item.date <= raw.day]
    types = defaultdict(int)
    for session in sessions:
        types[session.classification_label] += 1
    reasons = [(
        f"{start:%m-%d} 至 {raw.day:%m-%d}：跑步 {analysis.sessions_7d} 次，"
        f"已记录 {analysis.duration_minutes_7d} 分钟。"
    )]
    if analysis.distance_km_7d is not None:
        reasons.append(f"已记录跑步距离 {analysis.distance_km_7d:g} km。")
    if types:
        reasons.append("已分析课型：" + "、".join(f"{name} {count} 次" for name, count in sorted(types.items())) + "。")
    truncated = len(sessions) < analysis.sessions_7d
    if truncated:
        reasons.append(f"课型明细仅覆盖最近 {len(sessions)}/{analysis.sessions_7d} 场。")
    if blocked or truncated:
        action = "训练覆盖或恢复、疼痛限制尚未解除，暂不编排下周跑步课表；以上仅汇总已记录训练。"
    else:
        preferences = raw.training_preferences
        established_quality = (
            analysis.sessions_28d >= 6
            and _history_verified(raw, 28)
            and any(
                item.classification in _QUALITY_TYPES
                and item.confidence == ConfidenceBand.HIGH
                for item in analysis.recent_sessions
            )
            and preferences is not None
            and preferences.running_experience in {"INTERMEDIATE", "ADVANCED"}
            and preferences.pain_or_injury_status == "NONE"
            and preferences.weekly_running_target >= 3
        )
        action = "下周以可交谈的轻松跑为主，先保持已能稳定完成的频率和时长。"
        if established_quality:
            action += "恢复正常时至多维持 1 次已有同类质量课，其余跑步保持轻松，不同时增加速度和总量。"
            reasons.append("近 28 天已有明确节奏或间歇训练，且已记录中高阶跑步经验；质量课仅维持已有习惯。")
        else:
            action += "当前训练基础或强度证据不足，不新增质量课，也不自动延长长跑。"
        if strength_sessions:
            action += "质量跑、较长跑与下肢力量尽量错开，之间留轻松或休息日。"
        action += "疲劳或不适时降为轻松活动或休息，漏训顺延，不补课。"
    return WeeklyRecommendation(
        priority=1, code="WEEKLY_COACH_RUNNING", title="跑步结构与力量搭配",
        action=action, reasons=reasons,
    )


def _history_verified(raw, days: int) -> bool:
    """A handful of observed sessions cannot establish a complete load history."""
    values = raw.training_history_coverage or {}
    verified = set()
    for value in values.get("verified_days") or []:
        try:
            day = value if type(value) is date else date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
        verified.add(day)
    return all(raw.day - timedelta(days=offset) in verified for offset in range(days))
