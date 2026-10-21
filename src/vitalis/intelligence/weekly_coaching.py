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
    restriction = _restriction_reason(facts, events, coverage, recommendations)
    if blocked and restriction and not any(
        item.code in _SAFETY_ACTIONS - {"IMPROVE_SLEEP_DURATION"} for item in recommendations
    ):
        output.append(WeeklyRecommendation(
            priority=1, code="WEEKLY_COACH_ADJUSTMENT", title="先调整训练",
            action=restriction + "下周先不加训练量，具体安排看当天恢复情况。",
        ))
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


def _restriction_reason(facts, events, coverage, recommendations) -> str:
    active = {item.type for item in events if item.lifecycle != "RESOLVED"}
    if "TRAINING_LOAD_SPIKE" in active:
        return "近期训练量增加较快。"
    if "TRAINING_GAP" in active:
        return "此前训练有中断。"
    if "SLEEP_DEFICIT" in active and not any(item.code == "IMPROVE_SLEEP_DURATION" for item in recommendations):
        return "近期睡眠偏少。"
    if "BODY_SIGNAL_WARNING" in active:
        return "近期多个身体指标偏离平时水平。"
    if (facts.feedback.average_physical_fatigue or 0) >= 4:
        return "你记录的身体疲劳较重。"
    if (facts.feedback.average_muscle_soreness or 0) >= 4:
        return "你记录的肌肉酸痛较重。"
    if coverage.get("coverage_status") != "COMPLETE" or coverage.get("unknown_days") or coverage.get("totals_are_partial"):
        return "本周训练记录还没核实完整。"
    return ""


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
    summary = f"本周力量训练 {analysis.sessions_7d} 次。"
    reasons = [f"{start:%m-%d} 至 {raw.day:%m-%d}：力量训练 {analysis.sessions_7d} 次。"]
    if distribution:
        reasons.append(f"已记录的肌群接触天数：{distribution}；复合动作可涉及多个部位。")
    truncated = len(sessions) < analysis.sessions_7d
    if truncated:
        reasons.append(f"动作分析仅覆盖最近 {len(sessions)}/{analysis.sessions_7d} 场，不代表完整肌群分布。")
    if coverage.get("coverage_status") != "COMPLETE":
        reasons.append("训练覆盖未完整核实；未记录的日期和部位保持未知。")
    strain_reasons = []
    if any((item.muscle_soreness or 0) >= 4 for item in sessions):
        strain_reasons.append("明显肌肉酸痛")
    if any((item.session_rpe or 0) >= 9 for item in sessions):
        strain_reasons.append("用力程度达到 9 分或以上")
    session_strain = bool(strain_reasons)
    if len(known) < len(sessions):
        references = analysis.reference_muscle_doses_7d
        mapped_names = any(
            item.source == "lap_62" and item.exercise_name
            and "exercise_name_reference_mapping" in item.limitations
            for session in sessions for item in session.observed_sets
        )
        if references:
            primary = list(dict.fromkeys(d.muscle_group_label for d in references if d.direct_sets > 0))
            secondary = list(dict.fromkeys(d.muscle_group_label for d in references if d.indirect_sets > 0 and d.muscle_group_label not in primary))
            parts = (['主要练到' + '、'.join(primary)] if primary else [])
            if secondary:
                parts.append('、'.join(secondary) + '辅助参与')
            reasons.append('根据 Zepp 动作目录，' + '；'.join(parts) + '。实际刺激会随动作做法和用力程度变化。')
            action = summary + '可参考已有部位分布选择下一次方向；工作组和局部恢复信息不足，先沿用熟悉剂量，不补课或自动加量。'
        elif mapped_names:
            action = summary + "手表记录的动作名称目前仅供参考，还不能据此判断练了哪些部位，暂时排不了按部位轮换的计划。"
        elif any(item.explicit_exercises for item in sessions):
            action = summary + "部分动作对应的部位仍未知，暂时排不了按部位轮换的计划。"
        else:
            action = summary + "没有可用于判断部位的动作明细，暂时排不了按部位轮换的计划。"
    elif truncated:
        action = summary + f"目前只分析了最近 {len(sessions)} 次的动作，暂时不能判断整周的部位安排。"
    elif blocked or session_strain:
        action = summary
        if distribution:
            action += f"从已确认动作看，练到{distribution}。"
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
            action = f"继续按{sequence}轮换，不必每周重新开始一轮。"
        elif frequency in _FREQUENCY_SEQUENCES:
            action = f"下周按 {frequency} 次力量训练安排：{_FREQUENCY_SEQUENCES[frequency]}。"
        else:
            action = "暂时没有匹配当前训练次数的部位轮换方案，先沿用已熟悉的动作，不额外加量。"
        if sequence or frequency in _FREQUENCY_SEQUENCES:
            action += "同一部位别连续两天练重，漏练就顺延，不补课。"
        action = summary + action
    if session_strain:
        action += "有训练记录了" + "、".join(strain_reasons) + "，下次先别加重量或组数。"
    return WeeklyRecommendation(
        priority=1, code="WEEKLY_COACH_STRENGTH", title="力量",
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
        reasons.append(f"已记录跑步距离 {analysis.distance_km_7d:.1f} 公里。")
    if types:
        reasons.append("已分析课型：" + "、".join(f"{name} {count} 次" for name, count in sorted(types.items())) + "。")
    truncated = len(sessions) < analysis.sessions_7d
    if truncated:
        reasons.append(f"课型明细仅覆盖最近 {len(sessions)}/{analysis.sessions_7d} 场。")
    summary = f"本周跑步 {analysis.sessions_7d} 次"
    if analysis.distance_km_7d is not None:
        summary += f"，共 {analysis.distance_km_7d:.1f} 公里"
    summary += f"{'、' if analysis.distance_km_7d is not None else '，共 '}{analysis.duration_minutes_7d} 分钟。"
    if blocked or truncated:
        action = summary
        if truncated:
            action += "部分跑步明细未纳入本次分析，先不据此安排下周快跑。"
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
        action = summary + "下周以能完整说话的轻松跑为主，次数和时长先不增加。"
        if established_quality:
            action += "恢复正常时，最多保留 1 次你已习惯的较快跑步，其余慢跑。"
            reasons.append("近 28 天已有明确节奏或间歇训练，且已记录中高阶跑步经验；质量课仅维持已有习惯。")
        else:
            action += "先不新增快跑或加长单次距离。"
        if strength_sessions:
            action += "练腿和快跑别连着安排，中间留轻松或休息日。"
    return WeeklyRecommendation(
        priority=1, code="WEEKLY_COACH_RUNNING", title="跑步",
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
