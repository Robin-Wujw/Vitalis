"""Synthetic field contracts for previously retained but unused Zepp data."""

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from vitalis.adapters.zepp.parser import ZeppParser

UTC = timezone.utc
DAY = date(2026, 10, 2)
START = datetime(2026, 10, 1, 15, tzinfo=UTC)
END = START + timedelta(hours=8)


def band(**overrides):
    sleep = {"st": int(START.timestamp()), "ed": int(END.timestamp()),
        "lt": 330, "dp": 70, "dt": 60, "wk": 20, "obt": -80,
        "ebt": 430, "is": 20, "lb": 10, "isMerged": 1, **overrides}
    summary = base64.b64encode(json.dumps({"tz": 28800, "slp": sleep}).encode()).decode()
    return {"data": {"items": [{"date_time": DAY.isoformat(), "summary": summary}]}}


def sport(**overrides):
    return {"data": {"summary": [{"trackid": str(int(START.timestamp())),
        "type": 1, "start_time": START.isoformat(), "end_time": (START + timedelta(seconds=1921)).isoformat(),
        "dis": "5000", "highPrecisionDistance": 5000.25,
        "exerciseTimeWithMillis": 1800250, "pauseTimeWithMillis": 120750,
        "totalTimeWithMillis": 1921000, "te": 32, "anaerobic_te": 14,
        "avgEquivPace": 350, "bestEquivPace": 290, **overrides}]}}


def test_sleep_retains_checked_bed_window_and_rem_total():
    payload = band()
    before = deepcopy(payload)
    record = ZeppParser().parse_band(payload, "Asia/Shanghai")[0][DAY]
    assert record.sleep_latency_minutes == 20
    assert record.in_bed_at == START - timedelta(minutes=20)
    assert record.out_of_bed_at == END + timedelta(minutes=10)
    assert record.time_in_bed_minutes == 510
    assert record.out_of_bed_delay_minutes == 10
    assert record.sleep_efficiency_percent == pytest.approx(460 / 510 * 100)
    assert record.rem_sleep == 60
    assert record.fetched_at is None and record.source_record_id is None
    assert payload == before


@pytest.mark.parametrize("overrides", [
    {"obt": None}, {"obt": True}, {"is": -1}, {"ebt": -100},
    {"obt": -120}, {"ebt": 450}, {"is": float("nan")},
])
def test_bad_bed_identity_does_not_manufacture_efficiency(overrides):
    record = ZeppParser().parse_band(band(**overrides), "Asia/Shanghai")[0][DAY]
    assert record.time_in_bed_minutes is None
    assert record.sleep_efficiency_percent is None
    assert record.sleep_duration == 460


def test_sleep_timing_uses_frozen_zone_instead_of_payload_offset():
    record = ZeppParser().parse_band(band(obt=-80, ebt=430), "UTC")[0][DAY]
    assert record.time_in_bed_minutes is None


def test_workout_keeps_seconds_and_high_precision_distance_without_changing_elapsed():
    record, = ZeppParser().parse_sport_history(sport())
    assert record.moving_time_seconds == 1800.25
    assert record.paused_time_seconds == 120.75
    assert record.elapsed_time_seconds == 1921
    assert record.duration == 32
    assert record.distance_km == 5.00025
    assert record.anaerobic_training_effect == 1.4
    assert record.equivalent_pace_seconds_per_km == 350
    assert record.best_equivalent_pace_seconds_per_km == 290


@pytest.mark.parametrize("bad", [True, -1, float("inf"), float("nan"), "unknown", 10**500])
def test_invalid_workout_supplements_are_unknown(bad):
    record, = ZeppParser().parse_sport_history(sport(
        exerciseTimeWithMillis=bad, pauseTimeWithMillis=bad,
        totalTimeWithMillis=bad, highPrecisionDistance=bad,
        anaerobic_te=bad, avgEquivPace=bad))
    assert record.moving_time_seconds is None
    assert record.paused_time_seconds is None
    assert record.elapsed_time_seconds is None
    assert record.anaerobic_training_effect is None
    assert record.equivalent_pace_seconds_per_km is None
    assert record.distance_km == 5


def test_inconsistent_timing_is_not_promoted_to_precise_moving_time():
    record, = ZeppParser().parse_sport_history(sport(totalTimeWithMillis=2000000))
    assert record.moving_time_seconds is None
    assert record.paused_time_seconds is None
    assert record.elapsed_time_seconds == 2000


def test_daily_goals_remain_separate_from_observed_totals():
    payload = {"items": [{"eventType": "DailyHealth", "value": {
        "deviceId": "1,", "samples": [{"dateString": DAY.isoformat(),
            "totalSteps": 120, "stepGoal": 8000, "calorieGoal": 300,
            "burningDurationGoal": 30}]}}]}
    facts = {m.metric: m for m in ZeppParser.parse_daily_metrics(payload, "Asia/Shanghai")}
    assert facts["step_goal"].value == 8000
    assert facts["calorie_goal"].value == 300
    assert facts["active_minutes_goal"].value == 30
    assert facts["steps"].value == 120
    assert facts["step_goal"].source_field == "stepGoal"
    assert facts["step_goal"].source_scope == "user_summary"
    assert facts["step_goal"].qualification == "vendor_user_target"
    assert "step_goal_met" not in facts


def test_weight_history_filters_other_members_and_preserves_timestamp():
    payload = {"items": [{"memberId": member, "generatedTime": int(START.timestamp()),
        "summary": {"weight": 72, "height": 180, "bmi": 22.22}}
        for member in (-1, 2, None, True)]}
    _, samples = ZeppParser.parse_wellness(payload, "wellness:weight:weight:synthetic")
    assert {m.metric for m in samples} == {"weight", "height", "bmi"}
    assert len(samples) == 3
    assert {m.timestamp for m in samples} == {START}
    assert {m.source_scope for m in samples} == {"user_summary"}
    assert {m.qualification for m in samples} == {"vendor_recorded"}
    assert {m.metric: m.unit for m in samples} == {"weight": "kg", "height": "cm", "bmi": "kg/m2"}
    assert all(m.fetched_at is None for m in samples)


def test_weight_does_not_calculate_or_trust_inconsistent_bmi():
    payload = {"items": [{"memberId": -1, "generatedTime": int(START.timestamp()),
        "summary": {"weight": 72, "height": 180, "bmi": 90, "muscleRate": 32}}]}
    _, samples = ZeppParser.parse_wellness(payload, "wellness:weight:weight:synthetic")
    assert {m.metric for m in samples} == {"weight", "height"}


def test_vendor_exertion_keeps_signed_balance_and_vendor_units():
    payload = {"items": [{"timestamp": int(END.timestamp() * 1000),
        "value": {"atl": 20, "ctl": 15, "tsb": -5, "atlTotal": 24,
            "ctlTotal": 16, "tsbTotal": -8, "recoveryFactor": 0.8,
            "lastUpdateTime": int((END + timedelta(hours=1)).timestamp() * 1000)}}]}
    daily, _ = ZeppParser.parse_wellness(payload, "wellness:exertion:v2:synthetic", timezone_name="Asia/Shanghai")
    facts = {m.metric: m for m in daily}
    assert facts["vendor_tsb"].value == -5
    assert facts["vendor_tsb_total"].value == -8
    assert facts["vendor_atl"].date == DAY
    assert facts["vendor_atl"].qualification == "vendor_estimate_unvalidated"
    assert facts["vendor_atl"].unit == "vendor_load"
    assert facts["vendor_atl"].source_scope == "user_summary"
    assert facts["vendor_atl"].observed_at == END
    assert facts["vendor_atl"].revision_at == END + timedelta(hours=1)


def test_opaque_charge_response_never_falls_through_to_generic_daily_parser():
    payload = {"items": [{"eventType": "Charge", "value": {"samples": [
        {"s": 0, "stressInfo": "opaque", "total": 80, "steps": 999}
    ]}}]}
    daily, samples = ZeppParser.parse_wellness(payload, "wellness:charge_stress:v2:synthetic")
    assert daily == [] and samples == []
