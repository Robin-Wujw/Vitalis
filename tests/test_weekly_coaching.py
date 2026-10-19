"""Weekly coaching stays grounded in the completed period and its safety gates."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from vitalis.domain import WorkoutMetricSample
from vitalis.intelligence.contracts import (
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
    HealthEventEvidence,
    StrengthExerciseInput,
    TrainingPreferences,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.strength import normalize_exercise
from vitalis.intelligence.weekly import WeeklyProfileEngine

END = date(2026, 10, 11)


def _strength(offset, names=("卧推", "深蹲"), focus=None):
    workout_id = f"strength-{offset}"
    return {
        "workout_id": workout_id,
        "source": "zepp",
        "local_day": END - timedelta(days=offset),
        "confirmed_exercises": [
            normalize_exercise(
                "coach-user", workout_id, order,
                StrengthExerciseInput(exercise_name=name, sets=3, repetitions=8),
                focus,
            )
            for order, name in enumerate(names, start=1)
        ],
        "data": {"type": "strength", "training_family": "strength", "duration": 40},
    }


def _run(offset):
    return {
        "workout_id": f"run-{offset}",
        "source": "zepp",
        "local_day": END - timedelta(days=offset),
        "data": {"type": "running", "training_family": "aerobic", "duration": 30},
    }


def _raw(target=3):
    raw = RawDailyProfile(
        user_id="coach-user", day=END,
        as_of=datetime(2026, 10, 12, tzinfo=UTC),
        workouts=[_strength(5), _strength(2), _run(4), _run(1)],
        training_preferences=TrainingPreferences(
            user_id="coach-user", weekly_strength_target=target,
            pain_or_injury_status="NONE", max_session_minutes=45,
        ),
    )
    raw.training_history_coverage = {
        "verified_days": [(END - timedelta(days=n)).isoformat() for n in range(35)],
    }
    for offset in range(7):
        day = END - timedelta(days=offset)
        raw.sleep_by_day[day] = {"sleep_duration": 450}
        raw.training_by_day[day] = {"total_load": 20, "workout_count": 0, "total_duration": 0}
    return raw


def _build(raw, events=(), feedback=None):
    return WeeklyProfileEngine().build(
        "coaching-test", raw, [], list(events), feedback=feedback, period_mode="rolling",
    )


def _coaching(profile, code):
    return next(item for item in profile.actions.recommendations if item.code == code)


@pytest.mark.parametrize(("target", "structure"), [(2, "全身 A/B"), (3, "上肢→下肢→全身"), (4, "上肢→下肢→上肢→下肢")])
def test_weekly_strength_split_uses_declared_frequency_and_known_muscles(target, structure):
    item = _coaching(_build(_raw(target)), "WEEKLY_COACH_STRENGTH")

    assert structure in item.action
    assert "不补课" in item.action
    assert "周一" not in item.action
    evidence = " ".join(item.reasons)
    assert "胸部 2 天" in evidence
    assert "股四头肌 2 天" in evidence
    assert "10-05" in evidence and "10-11" in evidence
    assert "kg" not in evidence


def test_high_confidence_split_is_preserved_without_forcing_a_weekly_reset():
    raw = _raw()
    raw.workouts = [
        _strength(6, ("卧推",), "PUSH"),
        _strength(4, ("划船",), "PULL"),
        _strength(2, ("深蹲",), "LEGS"),
    ]

    item = _coaching(_build(raw), "WEEKLY_COACH_STRENGTH")

    assert "继续" in item.action
    assert "推→拉→腿" in item.action
    assert "上肢→下肢→全身" not in item.action


def test_unknown_actions_never_become_a_muscle_split_or_effective_set_count():
    raw = _raw()
    raw.workouts = [_strength(2, ("未知代码 62",))]

    item = _coaching(_build(raw), "WEEKLY_COACH_STRENGTH")

    assert "动作" in item.action and "未知" in item.action
    assert "上肢→" not in item.action
    assert "有效组" not in " ".join(item.reasons)
    assert "胸部" not in " ".join(item.reasons)


def test_incomplete_week_keeps_observed_muscle_facts_without_prescribing_a_split():
    raw = _raw()
    raw.training_history_coverage = {"verified_days": [END.isoformat()]}

    profile = _build(raw)
    item = _coaching(profile, "WEEKLY_COACH_STRENGTH")

    assert "胸部 2 天" in " ".join(item.reasons)
    assert "上肢→" not in item.action
    explanation = _coaching(profile, "WEEKLY_INSUFFICIENT_DATA").action
    assert "6 天的训练记录没核实" in explanation
    assert "不增加训练量" in explanation
    assert "疼痛" not in explanation


@pytest.mark.parametrize(("event_type", "reason"), [
    ("HRV_DROP", "心率变异性低于平时"),
    ("TRAINING_LOAD_SPIKE", "训练量增加较快"),
    ("TRAINING_GAP", "训练有中断"),
    ("SLEEP_DEFICIT", "睡眠偏少"),
    ("BODY_SIGNAL_WARNING", "身体指标偏离平时水平"),
])
def test_active_safety_events_suppress_new_strength_and_running_plans(event_type, reason):
    event = HealthEvent(
        id="coaching-safety", type=event_type, type_label="恢复限制",
        severity=EventSeverity.MODERATE, severity_label="中等", metric="training_load",
        metric_label="训练负荷", start_date=END - timedelta(days=1), end_date=END,
        duration_days=2, confidence=ConfidenceBand.HIGH, confidence_label="较高", summary="合成事件",
    )

    profile = _build(_raw(), [event])

    for code in ("WEEKLY_COACH_STRENGTH", "WEEKLY_COACH_RUNNING"):
        item = _coaching(profile, code)
        assert "本周" in item.action
        assert "上肢→" not in item.action and "轻松跑为主" not in item.action
    advice = " ".join(item.action for item in profile.actions.recommendations)
    assert reason in advice
    assert advice.count(reason) == 1
    assert "疼痛" not in advice and "没核实" not in advice
    assert "MAINTAIN_PLAN" not in {item.code for item in profile.actions.recommendations}


@pytest.mark.parametrize(("restriction", "reason"), [
    ("pain", "会诱发疼痛"), ("rpe", "用力程度 8 分"),
    ("fatigue", "身体疲劳较重"), ("soreness", "肌肉酸痛较重"), ("sleep", "睡眠"),
])
def test_subjective_and_sleep_safety_gates_are_never_bypassed(restriction, reason):
    raw = _raw()
    feedback = None
    if restriction == "pain":
        raw.training_preferences = raw.training_preferences.model_copy(update={
            "pain_or_injury_status": "PRESENT", "pain_or_injury_notes": "合成疼痛限制",
        })
    elif restriction == "sleep":
        raw.sleep_by_day = {day: {"sleep_duration": 360} for day in raw.sleep_by_day}
    else:
        feedback = [{
            "rpe": {"session_rpe": 9}, "fatigue": {"physical_fatigue": 5},
            "soreness": {"muscle_soreness": 5},
        }[restriction]]

    profile = _build(raw, feedback=feedback)

    for code in ("WEEKLY_COACH_STRENGTH", "WEEKLY_COACH_RUNNING"):
        action = _coaching(profile, code).action
        assert "本周" in action
        assert "上肢→" not in action and "轻松跑为主" not in action
        assert "较快跑步" not in action
    advice = " ".join(item.action for item in profile.actions.recommendations)
    assert reason in advice
    if restriction != "pain":
        assert "疼痛" not in advice
    assert "没核实" not in advice


def test_running_coaching_uses_easy_effort_and_does_not_invent_quality_training():
    item = _coaching(_build(_raw()), "WEEKLY_COACH_RUNNING")

    assert "轻松跑为主" in item.action
    assert "能完整说话" in item.action
    assert "不新增快跑" in item.action
    assert "练腿" in item.action and "休息" in item.action
    assert "2 次" in " ".join(item.reasons)
    assert "60 分钟" in " ".join(item.reasons)
    assert "配速" not in item.action


def test_calendar_coaching_excludes_workouts_after_the_completed_week():
    raw = _raw()
    raw.day = END + timedelta(days=1)
    raw.workouts.append(_strength(-1, ("划船",), "PULL"))

    profile = WeeklyProfileEngine().build("calendar-coaching", raw, [], [])
    item = _coaching(profile, "WEEKLY_COACH_STRENGTH")

    assert profile.period_end == END
    assert "背部" not in " ".join(item.reasons)
    assert "胸部 2 天" in " ".join(item.reasons)


def test_many_weekly_sessions_do_not_silently_turn_the_eight_session_view_into_a_full_week():
    raw = _raw()
    raw.workouts = [_strength(0) for _ in range(9)]
    for index, workout in enumerate(raw.workouts):
        workout["workout_id"] = f"distinct-{index}"

    item = _coaching(_build(raw), "WEEKLY_COACH_STRENGTH")

    assert "8/9" in " ".join(item.reasons)
    assert "只分析了最近 8 次" in item.action
    assert "上肢→" not in item.action and "全身 A/B" not in item.action


def test_frequency_without_saved_preferences_is_an_observed_candidate_only():
    raw = _raw()
    raw.training_preferences = None

    item = _coaching(_build(raw), "WEEKLY_COACH_STRENGTH")

    assert "全身 A/B" in item.action
    assert "本周已记录的 2 个力量训练日作为候选" in " ".join(item.reasons)
    assert raw.training_preferences is None


def test_declared_availability_limits_the_split_without_inventing_weekdays():
    raw = _raw(4)
    raw.training_preferences = raw.training_preferences.model_copy(update={"available_weekdays": [2, 6]})

    item = _coaching(_build(raw), "WEEKLY_COACH_STRENGTH")

    assert "全身 A/B" in item.action
    assert "4 次力量" not in item.action
    assert "周二" not in item.action and "周六" not in item.action


def _established_runner():
    raw = _raw()
    raw.training_preferences = raw.training_preferences.model_copy(update={"running_experience": "INTERMEDIATE"})
    raw.workouts = [item for item in raw.workouts if item["data"]["type"] == "strength"]
    raw.series["lactate_threshold_hr"] = [SeriesPoint(
        metric="lactate_threshold_hr", value=170, unit="bpm", day=END - timedelta(days=27),
        observed_at=END - timedelta(days=27), source="zepp", source_scope="daily_metric",
    )]
    for offset in [1, 4, 8, 11, 15, 18]:
        run = _run(offset)
        start = datetime.combine(run["local_day"], datetime.min.time(), UTC)
        run["samples"] = [WorkoutMetricSample(
            workout_id=run["workout_id"], timestamp=start + timedelta(seconds=second),
            metric="heart_rate", value=170 if offset == 1 else 130, unit="bpm",
        ) for second in range(0, 30 * 60, 30)]
        raw.workouts.append(run)
    return raw


def test_quality_training_requires_established_observed_history_and_experience():
    item = _coaching(_build(_established_runner()), "WEEKLY_COACH_RUNNING")

    assert "最多保留 1 次你已习惯的较快跑步" in item.action
    assert "其余慢跑" in item.action
    assert "近 28 天" in " ".join(item.reasons)


def test_quality_training_is_not_approved_from_an_unverified_history_calendar():
    raw = _established_runner()
    raw.training_history_coverage = {"verified_days": [(END - timedelta(days=n)).isoformat() for n in range(7)]}

    item = _coaching(_build(raw), "WEEKLY_COACH_RUNNING")

    assert "最多保留 1 次" not in item.action
    assert "不新增快跑" in item.action


def test_recovery_advice_names_its_evidence_and_does_not_repeat_blocked_plans():
    from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine

    event = HealthEvent(
        id="readable-recovery", type="HRV_DROP", type_label="心率变异性持续下降",
        severity=EventSeverity.MODERATE, severity_label="中等", metric="sleep_hrv",
        metric_label="睡眠 HRV", start_date=END - timedelta(days=2), end_date=END,
        duration_days=3, confidence=ConfidenceBand.HIGH, confidence_label="较高",
        summary="合成恢复事件", lifecycle="IMPROVING",
    )
    report = WeeklyBriefingEngine().build(_build(_raw(), [event]))
    advice = "\n".join(report.suggestions)

    assert "心率变异性" in advice and "好转" in advice
    assert "完整休息日" in advice and "能完整说话" in advice
    assert "疼痛" not in advice and "覆盖" not in advice
    assert "暂不编排" not in advice and "限制尚未解除" not in advice
    assert "本周跑步 2 次" in advice


def test_running_summary_is_readable_and_does_not_expose_classification_audit():
    from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine

    raw = _raw()
    for item in raw.workouts:
        if item["data"]["type"] == "running":
            item["data"]["distance_km"] = 8.0195
    report = WeeklyBriefingEngine().build(_build(raw))
    advice = "\n".join(report.suggestions)

    assert "16.0 公里" in advice and "60 分钟" in advice
    assert "16.039" not in advice and " km" not in advice
    assert "已分析课型" not in advice and "质量课" not in advice
    assert "10-05 至 10-11" not in advice


@pytest.mark.parametrize(("field", "reason", "absent"), [
    ("session_rpe", "用力程度达到 9 分", "酸痛"),
    ("muscle_soreness", "肌肉酸痛", "用力程度"),
])
def test_unknown_strength_actions_do_not_hide_specific_session_strain(field, reason, absent):
    raw = _raw()
    raw.workouts = [_strength(2, ("未识别动作",))]
    raw.feedback_by_workout = {("zepp", "strength-2"): [SimpleNamespace(
        created_at=raw.as_of, session_rpe=9 if field == "session_rpe" else None,
        muscle_soreness=5 if field == "muscle_soreness" else None,
    )]}

    action = _coaching(_build(raw), "WEEKLY_COACH_STRENGTH").action

    assert reason in action and absent not in action
    assert "先别加重量或组数" in action
    assert "上肢→" not in action


def test_recovery_reason_names_only_saved_signals_and_respects_event_resolution():
    recovery = HealthEvent(
        id="specific-recovery", type="RECOVERY_SUPPRESSED", type_label="恢复偏低",
        severity=EventSeverity.MODERATE, severity_label="中等",
        start_date=END - timedelta(days=2), end_date=END, duration_days=3,
        confidence=ConfidenceBand.HIGH, confidence_label="较高", summary="合成事件",
        lifecycle="IMPROVING", evidence=[
            HealthEventEvidence(fact="RHR_ABOVE_BASELINE", fact_label="静息心率高于基线"),
            HealthEventEvidence(fact="SLEEP_SHORT_DURATION", fact_label="睡眠不足 7 小时"),
        ],
    )
    resolved = recovery.model_copy(update={"id": "resolved-hrv", "type": "HRV_DROP", "lifecycle": "RESOLVED"})

    action = _coaching(_build(_raw(), [recovery, resolved]), "PRIORITIZE_RECOVERY").action

    assert "静息心率高于平时" in action and "睡眠不足 7 小时" in action
    assert "最近已有好转" in action
    assert "心率变异性" not in action and "疼痛" not in action


def test_specific_strength_plan_does_not_compete_with_a_remaining_sessions_quota():
    profile = _build(_raw(3))

    assert "MAINTAIN_STRENGTH_RHYTHM" not in {item.code for item in profile.actions.recommendations}
    assert "下周按 3 次力量训练安排" in _coaching(profile, "WEEKLY_COACH_STRENGTH").action
