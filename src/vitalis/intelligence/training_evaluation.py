"""Evidence-limited session reviews; unvalidated totals are never published."""

from .contracts import TrainingEvaluation


def review_strength(session, *, target=None):
    c = session.dose_coverage
    observations, suggestions = [], []
    if c.recorded_sets:
        observations.append(f"记录 {c.recorded_sets} 组；明确负重容量覆盖 {c.volume_sets}/{c.recorded_sets} 组。")
    for dose in (session.muscle_doses or session.reference_muscle_doses):
        source = "厂商动作参考" if dose.qualification == "vendor_reference" else "动作知识映射"
        observations.append(f"{dose.muscle_group_label}：直接 {dose.direct_sets:g} 组、间接 {dose.indirect_sets:g} 组（{source}）。")
    if session.muscle_soreness is not None and session.muscle_soreness >= 4:
        suggestions.append("已记录明显酸痛；下次先避开受影响部位，结合实际不适调整安排。")
    elif session.session_rpe is not None and session.session_rpe >= 9:
        suggestions.append("本场主观用力较高；下次先保持或减少剂量，核对恢复后再考虑进阶。")
    elif not c.working_sets or c.working_sets < c.recorded_sets:
        suggestions.append("下次可区分热身和工作组；先沿用熟悉剂量，便于比较肌群训练量。")
    if c.volume_sets < c.recorded_sets:
        suggestions.append("补充缺失组的实际重量和负重口径，可提高容量比较覆盖。")
    review = TrainingEvaluation(
        state="PARTIAL" if observations else "INSUFFICIENT_DATA",
        observations=observations, suggestions=suggestions[:2],
        limitations=["缺少训练前明确目标；仅回顾可观测记录，暂不计算总分。"],
    )
    if target:
        from .training_scoring import candidate_grade, score_strength
        review.target_origin = 'recorded'
        review.dimension_scores = score_strength(session, target)
        review.candidate_score = candidate_grade(review.dimension_scores)
        review.limitations = ['已关联事前计划；候选评分尚待回放审核，不作为公开总分。']
    return review


def review_running(session, *, target=None, workout=None):
    observations = [f"完成跑步 {session.duration_minutes} 分钟。"]
    suggestions = []
    if session.confidence.value in {"MODERATE", "HIGH"}:
        observations.append(f"训练结构推测为{session.classification_label}，未将其视为事先目标。")
    if session.classification == "INTERVAL_RUN" and session.segments:
        work = [x.average_speed_mps for x in session.segments if x.kind == "work" and x.average_speed_mps]
        if len(work) >= 3 and work[-1] < work[0] * .9:
            observations.append("后段工作段速度低于首段；组长、地形和恢复时长也可能影响比较。")
            suggestions.append("下次先保持组数，结合各组目标调整起始速度或恢复时长，再核对后段表现。")
    elif session.cardiac_drift_percent is not None:
        observations.append(f"本场心率漂移 {session.cardiac_drift_percent:+.1f}%；环境与补给差异尚未完全控制。")
        suggestions.append("下次选择相近路线和强度，控制开跑并记录环境，再比较后程表现。")
    if session.comparable_baseline:
        b = session.comparable_baseline
        observations.append(f"与此前 {b.sample_count} 场相近距离记录相比，配速变化 {b.pace_difference_percent:+.1f}%；强度和天气未完全匹配。")
    if not suggestions:
        suggestions.append("下次开始前明确本场目标类型、时长与强度，便于评价完成情况。")
    review = TrainingEvaluation(
        target_origin="inferred" if session.confidence.value in {"MODERATE", "HIGH"} else "unavailable",
        observations=observations, suggestions=suggestions[:2],
        limitations=["训练类型为事后分析；缺少训练前目标与已验证评分规则，暂不计算总分。"],
    )
    if target:
        review.target_origin = "recorded"
        duration = target['session'].get('total_duration_minutes')
        if duration:
            low, high = duration
            review.observations.append(f"明确关联的训练前计划为 {low}–{high} 分钟；实际 {session.duration_minutes} 分钟。")
        review.limitations = ["已关联训练前计划；评分曲线尚待留出验证，暂不发布总分。"]
        if workout:
            from .training_scoring import candidate_grade, score_running
            review.dimension_scores = score_running(session, workout, target)
            review.candidate_score = candidate_grade(review.dimension_scores)
    return review
