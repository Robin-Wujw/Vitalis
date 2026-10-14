"""Weekly load target: week start, catching up mid-week, and over target."""

from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.contracts import TrainingPreferences
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.weekly_load import weekly_load_plan

MONDAY = date(2026, 10, 12)


def _raw(day: date, loads: dict[date, float]) -> RawDailyProfile:
    verified = [MONDAY - timedelta(days=35) + timedelta(days=offset) for offset in range(42)]
    return RawDailyProfile(
        user_id="weekly-load", day=day,
        as_of=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=7),
        timezone_name="UTC",
        training_history_coverage={"verified_days": [item.isoformat() for item in verified if item <= day]},
        training_by_day={
            item: {"total_load": value, "total_duration": value / 2, "workout_count": 1}
            for item, value in loads.items() if item <= day
        },
    )


def _history() -> dict[date, float]:
    # Four complete weeks of 3 sessions x 100 load before MONDAY.
    return {
        MONDAY - timedelta(days=28) + timedelta(days=offset): 100.0
        for offset in range(28) if offset % 7 in (0, 2, 4)
    }


PREFS = TrainingPreferences(user_id="weekly-load", weekly_running_target=2, weekly_strength_target=1)


def test_week_start_targets_four_week_average_with_gentle_progression():
    plan = weekly_load_plan(_raw(MONDAY, _history()), PREFS)
    assert plan.status == "WEEK_START"
    assert plan.chronic_weekly_load == 300 and plan.target_load == 315
    assert plan.today_target_load == 105 and plan.sessions_left == 3
    assert plan.today_minutes == 52


def test_mid_week_behind_pace_asks_to_catch_up_with_a_capped_larger_share():
    loads = {**_history(), MONDAY: 40.0}
    plan = weekly_load_plan(_raw(MONDAY + timedelta(days=3), loads), PREFS)
    assert plan.status == "CATCH_UP"
    even = plan.remaining_load / min(plan.days_left, plan.sessions_left)
    assert even < plan.today_target_load <= round(even * 1.25, 1)


def test_over_target_turns_today_into_recovery():
    from vitalis.intelligence.decision import DecisionEngine

    loads = {**_history(), MONDAY: 200.0, MONDAY + timedelta(days=1): 180.0}
    plan = weekly_load_plan(_raw(MONDAY + timedelta(days=2), loads), PREFS)
    assert plan.status == "OVER" and plan.today_target_load is None
    assert plan.load_tier == "EXCESSIVE"
    engine = DecisionEngine()
    primary = engine._running(MONDAY, PREFS, type("T", (), {"running": None})(), "PRIMARY", False, "moderate")
    primary, optional, relationship = engine._apply_weekly_load(plan, PREFS, primary, None, "ADDITION")
    assert primary.session_type == "RECOVERY" and optional is None
    assert plan.reason in primary.personalization_reasons


def test_unknown_day_in_the_reference_leaves_no_plan():
    raw = _raw(MONDAY, _history())
    gap = MONDAY - timedelta(days=28)
    raw.training_history_coverage["verified_days"].remove(gap.isoformat())
    raw.training_by_day.pop(gap)
    assert weekly_load_plan(raw, PREFS) is None


def test_most_of_the_target_early_in_the_week_is_ahead_not_on_track():
    loads = {**_history(), MONDAY: 240.0}
    plan = weekly_load_plan(_raw(MONDAY + timedelta(days=1), loads), PREFS)
    assert plan.status == "AHEAD" and plan.status_label == "已超前"


def test_strength_day_names_rested_and_recent_muscle_groups():
    from types import SimpleNamespace

    from vitalis.intelligence.decision import DecisionEngine

    def status(muscle, label, days, soreness=None):
        return SimpleNamespace(
            muscle_group=muscle, muscle_group_label=label,
            days_since_last_trained=days, latest_soreness=soreness,
        )

    training = SimpleNamespace(strength=SimpleNamespace(muscle_recovery=[
        status("back", "背部", 5), status("chest", "胸部", 4), status("quadriceps", "股四头肌", 1),
        status("shoulders", "肩部", 6, soreness=4),
    ]))
    lines = DecisionEngine._muscle_freshness(training)
    assert lines == ["今天适合练：背部、胸部（已休息 3 天以上）。", "避开：股四头肌（48 小时内练过）。"]
