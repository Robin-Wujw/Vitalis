"""Weekly coaching stays grounded in the completed period and its safety gates."""

from datetime import UTC, date, datetime, timedelta

import pytest

from vitalis.domain import WorkoutMetricSample
from vitalis.intelligence.contracts import (
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
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

    assert "保留" in item.action
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
    assert "覆盖" in item.action
    assert "上肢→" not in item.action
    assert "WEEKLY_INSUFFICIENT_DATA" in {rec.code for rec in profile.actions.recommendations}


@pytest.mark.parametrize("event_type", ["HRV_DROP", "TRAINING_LOAD_SPIKE", "TRAINING_GAP", "SLEEP_DEFICIT", "BODY_SIGNAL_WARNING"])
def test_active_safety_events_suppress_new_strength_and_running_plans(event_type):
    event = HealthEvent(
        id="coaching-safety", type=event_type, type_label="恢复限制",
        severity=EventSeverity.MODERATE, severity_label="中等", metric="training_load",
        metric_label="训练负荷", start_date=END - timedelta(days=1), end_date=END,
        duration_days=2, confidence=ConfidenceBand.HIGH, confidence_label="较高", summary="合成事件",
    )

    profile = _build(_raw(), [event])

    for code in ("WEEKLY_COACH_STRENGTH", "WEEKLY_COACH_RUNNING"):
        item = _coaching(profile, code)
        assert "暂不编排" in item.action
        assert "上肢→" not in item.action and "轻松跑为主" not in item.action


@pytest.mark.parametrize("restriction", ["pain", "rpe", "fatigue", "soreness", "sleep"])
def test_subjective_and_sleep_safety_gates_are_never_bypassed(restriction):
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
        assert "暂不编排" in _coaching(profile, code).action


def test_running_coaching_uses_easy_effort_and_does_not_invent_quality_training():
    item = _coaching(_build(_raw()), "WEEKLY_COACH_RUNNING")

    assert "轻松跑为主" in item.action
    assert "可交谈" in item.action
    assert "不新增" in item.action and "质量课" in item.action
    assert "下肢" in item.action and "休息" in item.action
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
    assert "暂不编排" in item.action


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

    assert "至多维持 1 次已有同类质量课" in item.action
    assert "其余跑步保持轻松" in item.action
    assert "近 28 天" in " ".join(item.reasons)


def test_quality_training_is_not_approved_from_an_unverified_history_calendar():
    raw = _established_runner()
    raw.training_history_coverage = {"verified_days": [(END - timedelta(days=n)).isoformat() for n in range(7)]}

    item = _coaching(_build(raw), "WEEKLY_COACH_RUNNING")

    assert "至多维持 1 次" not in item.action
    assert "不新增质量课" in item.action
