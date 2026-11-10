"""Recorded additions stay qualified, bounded, and separate from decisions."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.application.report_sources import report_source_view
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.recorded_context import build_recorded_context


DAY = date(2026, 11, 8)
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def point(metric, value, day=DAY, unit="min", **kwargs):
    return SeriesPoint(metric, value, unit, day, day, "zepp", "user_summary",
                       fetched_at=NOW, **kwargs)


def test_sleep_timing_survives_report_source_selection():
    raw = RawDailyProfile(user_id="test", day=DAY, as_of=NOW)
    raw.sleep_by_day[DAY] = dict(source="zepp", source_scope="user_fused",
        sleep_duration=420, time_in_bed_minutes=480, sleep_latency_minutes=0,
        sleep_efficiency_percent=87.5, fetched_at=NOW)
    view = report_source_view(raw)
    assert view.series["sleep_latency_minutes"][0].value == 0
    assert view.series["time_in_bed_minutes"][0].fetched_at == NOW


def test_sleep_week_comparison_uses_daily_values_and_same_source_gate():
    raw = RawDailyProfile(user_id="test", day=DAY, as_of=NOW)
    raw.series["sleep_latency_minutes"] = [
        point("sleep_latency_minutes", 10 if offset < 7 else 20, DAY-timedelta(days=offset))
        for offset in range(14)
    ]
    result = build_recorded_context(raw, DAY-timedelta(days=6))
    item = result.metrics[0]
    assert (item.average, item.previous_average, item.change) == (10, 20, -10)
    assert (item.available_days, item.sample_count, item.coverage_ratio) == (7, 7, 1)
    assert item.reference_start == DAY-timedelta(days=13)
    assert item.date_basis == "sleep_day"
    raw.series["sleep_latency_minutes"] = raw.series["sleep_latency_minutes"][:11]
    item = build_recorded_context(raw, DAY-timedelta(days=6)).metrics[0]
    assert item.previous_available_days == 4
    assert item.previous_average is None and item.change is None


def test_goal_is_not_a_measurement_and_unknown_or_incomplete_is_not_failure():
    raw = RawDailyProfile(user_id="test", day=DAY, as_of=NOW,
        report_context={"target_day_complete": False})
    raw.series = {
        "step_goal": [point("step_goal", 8000, unit="steps", qualification="vendor_goal")],
        "steps": [point("steps", 4000, unit="steps")],
        "calorie_goal": [point("calorie_goal", 500, unit="kcal", qualification="vendor_goal")],
    }
    goals = {item.metric: item for item in build_recorded_context(raw, DAY).goals}
    assert goals["step_goal"].actual_value == 4000
    assert goals["step_goal"].evaluated_days == 0
    assert goals["step_goal"].met_days is None
    assert goals["calorie_goal"].actual_value is None
    raw.report_context["target_day_complete"] = True
    goal = build_recorded_context(raw, DAY).goals[0]
    assert goal.evaluated_days == 1 and goal.met_days == 0


def test_sparse_body_and_signed_vendor_load_never_become_trends_or_totals():
    raw = RawDailyProfile(user_id="test", day=DAY, as_of=NOW)
    raw.series = {
        "weight": [point("weight", 70, DAY-timedelta(days=30), "kg", qualification="vendor_body_measurement")],
        "vendor_tsb": [point("vendor_tsb", -8, unit="vendor_load", qualification="vendor_only_unvalidated")],
        "vendor_tsb_total": [point("vendor_tsb_total", -12, unit="vendor_load", qualification="vendor_only_unvalidated")],
    }
    facts = {item.metric: item for item in build_recorded_context(raw, DAY).metrics}
    assert facts["weight"].latest_observed_at == DAY-timedelta(days=30)
    assert facts["weight"].average is None and facts["weight"].change is None
    assert facts["vendor_tsb"].latest_value == -8
    assert facts["vendor_tsb_total"].latest_value == -12
    assert facts["vendor_tsb"].qualification == "vendor_only_unvalidated"
    assert facts["vendor_tsb"].average is None


@pytest.mark.parametrize("alter", [
    {"source_scope": "device"}, {"unit": "lb"}, {"value": float("nan")},
    {"fetched_at": NOW+timedelta(seconds=1)}, {"day": DAY+timedelta(days=1)},
    {"qualification": "retained_raw"},
])
def test_context_excludes_unqualified_future_or_wrong_unit_facts(alter):
    raw = RawDailyProfile(user_id="test", day=DAY, as_of=NOW)
    raw.series["weight"] = [replace(point("weight", 70, unit="kg"), **alter)]
    assert build_recorded_context(raw, DAY).metrics == []


def test_goal_can_compare_unique_account_actual_with_its_own_provenance():
    raw = RawDailyProfile(user_id="test", day=DAY, as_of=NOW)
    raw.series = {"step_goal": [point("step_goal", 8000, unit="steps")],
        "steps": [replace(point("steps", 9000, unit="steps"), source_scope="user_fused"),
                  point("steps", 10000, DAY-timedelta(days=1), "steps")]}
    actual = build_recorded_context(raw, DAY).goals[0]
    assert actual.actual_value == 9000
    assert actual.provenance.source_scope == "user_summary"
    assert actual.actual_provenance.source_scope == "user_fused"


def test_goal_actual_does_not_resolve_competing_account_streams_or_other_dates():
    raw = RawDailyProfile(user_id="test", day=DAY, as_of=NOW)
    raw.series = {"step_goal": [point("step_goal", 8000, unit="steps")],
        "steps": [replace(point("steps", 9000, unit="steps"), source_scope="user_fused"),
                  point("steps", 7000, unit="steps")]}
    assert build_recorded_context(raw, DAY).goals[0].actual_value is None
    raw.series["steps"] = [point("steps", 9000, DAY-timedelta(days=1), unit="steps")]
    assert build_recorded_context(raw, DAY).goals[0].actual_value is None


def test_saved_sleep_and_context_are_visible_in_both_report_formats():
    from vitalis.intelligence.morning_briefing import MorningBriefingEngine
    from vitalis.intelligence.report_rendering import render_report
    from tests.test_report_content import synthetic_daily_fixture

    payload = synthetic_daily_fixture()
    payload["features"]["sleep"].update(sleep_latency_minutes=0,
        time_in_bed_minutes=480, sleep_efficiency_percent=87.5)
    for target in ("markdown", "html"):
        morning = render_report(MorningBriefingEngine().build_payload(payload), target=target).content
        assert "入睡用时" in morning and "87.5%" in morning
        daily = render_report(payload, target=target).content
        assert "在床时间" in daily and "87.5%" in daily


def test_weekly_projection_does_not_reintroduce_changes_filtered_by_analysis():
    from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine
    section = WeeklyBriefingEngine()._sleep_recovery({"sleep": {
        "available_days": 7, "average_minutes": 450, "change_percent": 0.3,
    }, "recovery": {"hrv_change_percent": -0.2}}, {"key_changes": []})
    assert not any("这会影响" in text or "恢复信号较前" in text for text in section["interpretation"])
