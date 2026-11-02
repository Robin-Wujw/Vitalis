"""Synthetic regressions for vendor facts used by report charts.

Protocol references (independent implementation, no upstream code copied):
https://github.com/J-solan/zepp-dashboard/blob/main/docs/zepp-api.md
https://github.com/J-solan/zepp-dashboard/blob/main/ingest/parsers.py
"""

import base64
import json
from datetime import date, datetime, timedelta, timezone
from itertools import permutations

import pytest

from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.domain import SleepRecord


DAY = date(2026, 10, 2)
DEVICE_A = "A1B2C3D4E5F60708"
DEVICE_B = "B1C2D3E4F5061728"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _band(sleep: dict, *, day: date = DAY, offset: int = 8 * 3600) -> dict:
    summary = base64.b64encode(json.dumps({"tz": offset, "slp": sleep}).encode()).decode()
    return {"data": {"items": [{"date_time": day.isoformat(), "summary": summary}]}}


def _night(**extra) -> dict:
    return {
        "st": _ms("2026-10-01T15:00:00+00:00") // 1000,
        "ed": _ms("2026-10-01T23:00:00+00:00") // 1000,
        "wk": 0,
        "stage": [{"start": 1380, "stop": 1859, "mode": 4}],
        **extra,
    }


@pytest.mark.parametrize("day_id", [20261002, "20261002", "2026-10-02"])
def test_sport_load_has_independent_daily_and_seven_day_metrics(day_id):
    rows = ZeppParser.parse_daily_metrics({"items": [{
        "dayId": day_id, "currnetDayTrainLoad": 0, "wtlSum": 320,
        "wtlSumOptimalMin": 200, "wtlSumOptimalMax": 500,
        "wtlSumOverreaching": 650,
    }]})
    assert {row.metric: row.value for row in rows} == {
        "training_load": 0,
        "vendor_load_7d": 320,
        "vendor_load_optimal_min": 200,
        "vendor_load_optimal_max": 500,
        "vendor_load_overreaching": 650,
    }
    assert {row.date for row in rows} == {DAY}
    assert {row.unit for row in rows} == {"load"}
    assert {row.metric: row.source_field for row in rows} == {
        "training_load": "currnetDayTrainLoad", "vendor_load_7d": "wtlSum",
        "vendor_load_optimal_min": "wtlSumOptimalMin",
        "vendor_load_optimal_max": "wtlSumOptimalMax",
        "vendor_load_overreaching": "wtlSumOverreaching",
    }
    assert {row.qualification for row in rows} == {"vendor_reference"}
    assert all(row.observed_at is None and row.fetched_at is None and row.source_record_id is None for row in rows)


@pytest.mark.parametrize("invalid", [None, -1, True, float("nan"), float("inf"), "unknown"])
def test_sport_load_invalid_daily_never_falls_back_to_other_loads(invalid):
    rows = ZeppParser.parse_daily_metrics({"items": [{
        "dayId": 20261002, "currnetDayTrainLoad": invalid,
        "wtlSum": 340, "wtlSumOptimalMin": -1,
        "wtlSumOptimalMax": 0, "wtlSumOverreaching": None,
        "load": 900, "trainingLoad": 800, "training_load": 700,
    }]})
    assert {row.metric: row.value for row in rows} == {
        "vendor_load_7d": 340, "vendor_load_optimal_max": 0,
    }


def test_sport_load_missing_daily_does_not_reuse_rolling_sum():
    rows = ZeppParser.parse_daily_metrics({"items": [{
        "dayId": 20261002, "wtlSum": 0, "load": 300,
    }]})
    assert [(row.metric, row.value) for row in rows] == [("vendor_load_7d", 0)]


def _readiness(update: str, *, device=DEVICE_A, **values) -> dict:
    return {"eventType": "readiness", "timestamp": _ms(update), "value": {
        "timestamp": _ms("2026-10-01T16:00:00+00:00"),
        "deviceId": device, **values,
    }}


@pytest.mark.parametrize("reverse", [False, True])
def test_readiness_latest_revision_wins_for_both_daily_and_samples(reverse):
    old = _readiness("2026-10-02T00:00:00+00:00", rdnsScore=61, sleepHRV=40, mentScore=77)
    new = _readiness("2026-10-03T03:00:00+00:00", rdnsScore=82, sleepHRV=55, mentScore=255)
    items = [new, old] if reverse else [old, new]
    daily = ZeppParser.parse_daily_metrics({"items": items}, timezone_name="Asia/Shanghai")
    samples = ZeppParser.parse_readiness_samples({"items": items}, timezone_name="Asia/Shanghai")
    expected = {"readiness": 82, "sleep_hrv": 55}
    assert {row.metric: row.value for row in daily} == expected
    assert {row.date for row in daily} == {DAY}
    assert {row.metric: row.value for row in samples} == expected
    assert len(samples) == 2
    anchor = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
    assert {row.timestamp for row in samples} == {anchor}
    assert {row.observed_at for row in daily} == {anchor}
    for rows in (daily, samples):
        assert {row.revision_at for row in rows} == {datetime(2026, 10, 3, 3, tzinfo=timezone.utc)}
        assert {row.metric: row.source_field for row in rows} == {"readiness": "rdnsScore", "sleep_hrv": "sleepHRV"}
        assert {row.qualification for row in rows} == {"vendor_reference"}
        assert all(row.fetched_at is None and row.source_record_id is None for row in rows)


def test_readiness_revision_selection_does_not_merge_devices_or_unknown_identity():
    items = [
        _readiness("2026-10-02T01:00:00+00:00", rdnsScore=60),
        _readiness("2026-10-02T05:00:00+00:00", rdnsScore=80),
        _readiness("2026-10-02T02:00:00+00:00", device=DEVICE_B, rdnsScore=70),
        _readiness("2026-10-02T06:00:00+00:00", device=None, rdnsScore=90),
    ]
    for rows in (ZeppParser.parse_daily_metrics({"items": items}), ZeppParser.parse_readiness_samples({"items": items})):
        assert {(row.device_id, row.source_scope, row.value) for row in rows} == {
            (DEVICE_A, "device", 80), (DEVICE_B, "device", 70), (None, "user_fused", 90),
        }


@pytest.mark.parametrize(("zone", "expected_day"), [
    ("Asia/Shanghai", DAY), ("America/New_York", date(2026, 10, 1)),
])
def test_readiness_day_uses_value_anchor_in_requested_timezone(zone, expected_day):
    event = _readiness("2026-10-03T12:00:00+00:00", rdnsScore=82)
    event["date"] = "2026-10-03"
    event["value"]["date"] = "2026-10-03"
    rows = ZeppParser.parse_daily_metrics({"items": [event]}, timezone_name=zone)
    assert [row.date for row in rows] == [expected_day]


def test_readiness_uses_configured_timezone_when_not_explicit(monkeypatch):
    import vitalis.time as time_module

    monkeypatch.setattr(time_module, "local_timezone", lambda *_: timezone(timedelta(hours=-4)))
    rows = ZeppParser.parse_daily_metrics({"items": [_readiness("2026-10-03T12:00:00+00:00", rdnsScore=82)]})
    assert [row.date for row in rows] == [date(2026, 10, 1)]


def test_readiness_missing_anchor_is_not_assigned_to_update_day():
    event = _readiness("2026-10-03T12:00:00+00:00", rdnsScore=82)
    del event["value"]["timestamp"]
    assert ZeppParser.parse_daily_metrics({"items": [event]}) == []
    assert ZeppParser.parse_readiness_samples({"items": [event]}) == []


def test_readiness_revision_metadata_retains_latest_all_missing_revision():
    old = _readiness("2026-10-02T01:00:00+00:00", rdnsScore=80, sleepHRV=50)
    retracted = _readiness("2026-10-03T06:00:00+00:00", rdnsScore=255, sleepHRV=255)
    unknown = _readiness("2026-10-02T04:00:00+00:00", device=None, rdnsScore=255)
    del unknown["timestamp"]
    for ordered in permutations([old, retracted, unknown]):
        payload = {"items": list(ordered)}
        revisions = ZeppParser.parse_readiness_revisions(payload, timezone_name="Asia/Shanghai")
        assert revisions == [
            {"day": DAY, "device_id": None, "source_scope": "user_fused",
             "observed_at": datetime(2026, 10, 1, 16, tzinfo=timezone.utc), "revision_at": None},
            {"day": DAY, "device_id": DEVICE_A, "source_scope": "device",
             "observed_at": datetime(2026, 10, 1, 16, tzinfo=timezone.utc),
             "revision_at": datetime(2026, 10, 3, 6, tzinfo=timezone.utc)},
        ]
        assert ZeppParser.parse_readiness_samples(payload, timezone_name="Asia/Shanghai") == []
        assert ZeppParser.parse_daily_metrics(payload, timezone_name="Asia/Shanghai") == []


def test_readiness_missing_revision_cannot_override_verified_revision():
    updated = _readiness("2026-10-02T06:00:00+00:00", rdnsScore=82)
    unknown = _readiness("2026-10-02T06:00:00+00:00", rdnsScore=60)
    del unknown["timestamp"]
    for ordered in permutations([updated, unknown]):
        assert [row.value for row in ZeppParser.parse_daily_metrics({"items": list(ordered)})] == [82]


def test_charge_daily_keeps_each_components_latest_valid_observation():
    raw = {"items": [{"eventType": "Charge", "value": {
        "startTime": _ms("2026-10-02T00:00:00+00:00"),
        "deviceId": f"3,{DEVICE_A}",
        "samples": [
            {"s": 120_000, "total": 255, "physical": 73, "mental": None},
            {"s": 0, "total": 65, "physical": 70, "mental": 52},
            {"s": 60_000, "total": 64, "physical": None, "mental": 56},
        ],
    }}]}
    rows = ZeppParser.parse_daily_metrics(raw)
    assert {row.metric: row.value for row in rows} == {
        "hybrid_charge": 64, "physical_charge": 73, "mental_charge": 56,
    }
    assert {(row.source_scope, row.device_id) for row in rows} == {("device", DEVICE_A)}
    assert {row.metric: row.source_field for row in rows} == {
        "hybrid_charge": "total", "physical_charge": "physical", "mental_charge": "mental",
    }
    assert {row.metric: row.observed_at.minute for row in rows} == {
        "hybrid_charge": 1, "physical_charge": 2, "mental_charge": 1,
    }


def test_charge_daily_orders_events_by_observation_time_and_keeps_device_streams():
    def event(hour, device, total, physical):
        return {"eventType": "Charge", "value": {
            "startTime": _ms(f"2026-10-02T{hour:02d}:00:00+00:00"),
            "deviceId": device, "samples": [{"s": 0, "total": total, "physical": physical}],
        }}

    events = [event(8, DEVICE_A, 255, 61), event(7, DEVICE_A, 68, 62), event(6, DEVICE_B, 75, 80)]
    for ordered in permutations(events):
        rows = ZeppParser.parse_daily_metrics({"items": list(ordered)}, timezone_name="Asia/Shanghai")
        assert {(row.metric, row.device_id): row.value for row in rows} == {
            ("hybrid_charge", DEVICE_A): 68, ("physical_charge", DEVICE_A): 61,
            ("hybrid_charge", DEVICE_B): 75, ("physical_charge", DEVICE_B): 80,
        }


def test_charge_components_reject_booleans_and_keep_explicit_zero():
    raw = {"items": [{"eventType": "Charge", "value": {
        "startTime": _ms("2026-10-02T00:00:00+00:00"),
        "samples": [{"s": 0, "total": 255, "physical": 0, "mental": True}],
    }}]}
    for rows in (ZeppParser.parse_daily_metrics(raw), ZeppParser.parse_charge_samples(raw)):
        assert [(row.metric, row.value) for row in rows] == [("physical_charge", 0)]


def test_naps_merge_exactly_adjacent_stages_and_keep_awake_inside_session():
    raw = _band(_night(odd_stage=[
        {"start": 2250, "stop": 2250, "mode": 8},
        {"start": 2221, "stop": 2231, "mode": 7},
        {"start": 2220, "stop": 2220, "mode": 4},
        {"start": 2232, "stop": 2235, "mode": 5},
        {"start": 2237, "stop": 2240, "mode": 4},
    ]))
    parser = ZeppParser()
    naps = parser.parse_band_naps(raw)[DAY]
    assert len(naps) == 3  # One unobserved minute is a real gap, not a guessed threshold.
    first = naps[0]
    assert first.started_at == datetime(2026, 10, 2, 5, tzinfo=timezone.utc)
    assert first.ended_at == datetime(2026, 10, 2, 5, 16, tzinfo=timezone.utc)
    assert first.sleep_minutes == 5 and first.awake_minutes == 11
    assert [stage.stage for stage in first.stages] == ["light", "awake", "deep"]
    assert first.source == "zepp"
    sleep = parser.parse_band(raw)[0][DAY]
    assert sleep.sleep_duration == 480
    assert sleep.naps_known is True
    assert sleep.nap_minutes == 10
    assert sleep.naps == naps
    restored = SleepRecord.model_validate_json(sleep.model_dump_json())
    assert restored.naps == naps and restored.nap_minutes == 10


def test_nap_only_payload_never_creates_zero_minute_main_sleep():
    raw = _band({"st": 0, "ed": 0, "stage": [], "odd_stage": [{
        "start": 2220, "stop": 2249, "mode": 4,
    }]})
    parser = ZeppParser()
    sleeps, _ = parser.parse_band(raw)
    assert sleeps == {}
    nap, = parser.parse_band_naps(raw)[DAY]
    assert nap.sleep_minutes == 30 and nap.awake_minutes == 0


def test_explicit_empty_naps_are_known_but_absent_odd_stage_is_unknown():
    parser = ZeppParser()
    absent_raw = _band(_night())
    empty_raw = _band(_night(odd_stage=[]))
    absent = parser.parse_band(absent_raw)[0][DAY]
    empty = parser.parse_band(empty_raw)[0][DAY]
    assert absent.naps == [] and absent.naps_known is False and absent.nap_minutes is None
    assert empty.naps == [] and empty.naps_known is True and empty.nap_minutes == 0
    assert parser.parse_band_naps(absent_raw) == {}
    assert parser.parse_band_naps(empty_raw) == {DAY: []}


def test_unknown_nap_stage_is_preserved_without_guessing_awake_or_sleep_minutes():
    raw = _band(_night(odd_stage=[
        {"start": 2220, "stop": 2224, "mode": 4},
        {"start": 2225, "stop": 2229, "mode": 99},
    ]))
    sleep = ZeppParser().parse_band(raw)[0][DAY]
    nap, = sleep.naps
    assert nap.stages[-1].stage == "unknown"
    assert nap.stages[-1].vendor_mode == 99
    assert nap.sleep_minutes is None and nap.awake_minutes is None
    assert sleep.naps_known is True and sleep.nap_minutes is None


def test_unknown_main_sleep_stage_is_not_mislabelled_awake():
    raw = _band(_night(stage=[{"start": 1380, "stop": 1859, "mode": 99}]))
    sleep = ZeppParser().parse_band(raw)[0][DAY]
    assert [stage.stage for stage in sleep.stages] == ["unknown"]


def test_naps_follow_verified_same_day_anchor_from_main_sleep():
    raw = _band(_night(stage=[{"start": 0, "stop": 419, "mode": 4}],
                       st=_ms("2026-10-01T16:00:00+00:00") // 1000,
                       odd_stage=[{"start": 780, "stop": 809, "mode": 4}]))
    nap, = ZeppParser().parse_band_naps(raw)[DAY]
    assert nap.started_at == datetime(2026, 10, 2, 5, tzinfo=timezone.utc)


def test_nap_stage_anchor_uses_explicit_timezone_with_dst_transition():
    raw = _band({"odd_stage": [{"start": 2220, "stop": 2249, "mode": 4}]},
                day=date(2026, 11, 2), offset=0)
    nap, = ZeppParser().parse_band_naps(raw, timezone_name="America/New_York")[date(2026, 11, 2)]
    assert nap.started_at == datetime(2026, 11, 2, 18, tzinfo=timezone.utc)
    assert nap.ended_at == datetime(2026, 11, 2, 18, 30, tzinfo=timezone.utc)


def test_workout_preserves_vendor_metadata_and_zone_seconds():
    workout, = ZeppParser().parse_sport_history({"data": {"summary": [{
        "trackid": 1_700_000_000, "type": 223,
        "auto_recognition": True, "sport_title": "  午间散步  ", "te": "32.0",
        "heart_range": "0,98;242,118;20,137;",
    }]}})
    assert workout.auto_recognized is True
    assert workout.title == "午间散步"
    assert workout.training_effect == 3.2
    assert [(zone.seconds, zone.upper_bpm) for zone in workout.vendor_heart_rate_zones] == [
        (0, 98), (242, 118), (20, 137),
    ]
    assert workout.heart_rate_zone_boundaries_bpm == []
    assert set(workout.observed_fields) >= {
        "auto_recognized", "title", "training_effect", "vendor_heart_rate_zones",
    }


@pytest.mark.parametrize("invalid", [None, -1, -20000, -274, -361, 51, True, "bad", float("inf"), float("nan")])
def test_workout_training_effect_invalid_values_remain_unknown(invalid):
    workout, = ZeppParser().parse_sport_history({"items": [{"te": invalid}]})
    assert workout.training_effect is None
    assert "training_effect" not in workout.observed_fields


def test_workout_metadata_does_not_infer_autorecognition_from_sport_code():
    workout, = ZeppParser().parse_sport_history({"items": [{"type": 223}]})
    assert workout.auto_recognized is None and workout.title is None
    assert workout.training_effect is None and workout.vendor_heart_rate_zones == []


@pytest.mark.parametrize("flag", [1, 0, "true", "false", -1])
def test_workout_nonboolean_recognition_flag_stays_unknown(flag):
    workout, = ZeppParser().parse_sport_history({"items": [{"auto_recognition": flag, "sport_title": 99}]})
    assert workout.auto_recognized is None and workout.title is None


def test_workout_false_and_zero_are_explicit_observations():
    workout, = ZeppParser().parse_sport_history({"items": [{"auto_recognition": False, "te": 0}]})
    assert workout.auto_recognized is False and workout.training_effect == 0
    assert set(workout.observed_fields) >= {"auto_recognized", "training_effect"}


def test_workout_zone_parser_keeps_only_complete_finite_numeric_pairs():
    workout, = ZeppParser().parse_sport_history({"items": [{
        "heart_range": "bad,100;10;1,2,3;inf,130;-1,140;3.5,150;4,160.5;4,0;0,98;242,118;20,137;",
    }]})
    assert [(zone.seconds, zone.upper_bpm) for zone in workout.vendor_heart_rate_zones] == [
        (0, 98), (242, 118), (20, 137),
    ]
