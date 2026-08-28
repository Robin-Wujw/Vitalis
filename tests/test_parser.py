"""Zepp 厂商格式 -> Vitalis Schema 解析器测试。"""

import base64
import json

import pytest

from datetime import date, datetime, timedelta, timezone

from vitalis.connectors.zepp.parser import ZeppParser
from vitalis.connectors.zepp.sport_types import ZEPP_SPORT_MODES
from vitalis.models import WorkoutType

SLEEP_RAW = {
    "code": 0,
    "data": {
        "date": "2026-08-25",
        "sleepScore": 88,
        "stages": {"deep": 100, "light": 250, "rem": 90},
        "duration": 440,
        "awake": 20,
        "bedTime": "23:10",
        "wakeTime": "07:20",
    },
}

ACTIVITY_RAW = {
    "code": 0,
    "data": {
        "date": "2026-08-25",
        "steps": 12000,
        "activeMinutes": 65,
        "calories": 2200,
        "distanceKm": 8.4,
        "restingHr": 58,
    },
}

TRAINING_RAW = {
    "code": 0,
    "data": {
        "date": "2026-08-25",
        "items": [
            {"workoutId": "w1", "type": "running", "duration": 50, "load": 55},
            {"workoutId": "w2", "type": "strength", "duration": 45, "load": 40},
        ],
    },
}


def test_public_workout_catalog_has_all_current_zepp_and_legacy_cloud_ids():
    assert len(ZEPP_SPORT_MODES) == 122
    assert ZEPP_SPORT_MODES[0xBF].label_zh == "足球"


def test_parse_sleep():
    sleep = ZeppParser().parse_sleep(SLEEP_RAW)
    assert sleep is not None
    assert sleep.date == date(2026, 8, 25)
    assert sleep.sleep_duration == 440
    assert sleep.deep_sleep == 100
    assert sleep.rem_sleep == 90
    assert sleep.sleep_score == 88
    assert sleep.quality.value == "excellent"  # score 88 >= 85


def test_parse_sleep_missing_data_returns_none():
    assert ZeppParser().parse_sleep({"code": 0, "data": {}}) is None


def test_parse_activity():
    act = ZeppParser().parse_activity(ACTIVITY_RAW)
    assert act is not None
    assert act.steps == 12000
    assert act.resting_hr == 58
    assert act.distance_km == 8.4


def test_parse_training_aggregates():
    tr = ZeppParser().parse_training(TRAINING_RAW)
    assert tr is not None
    assert tr.workout_count == 2
    assert tr.total_duration == 95
    assert tr.total_load == 95


def test_parse_training_empty_items():
    assert ZeppParser().parse_training({"code": 0, "data": {"items": []}}) is None


def test_parse_workout_type_map():
    wo = ZeppParser().parse_workout({"type": "strength", "duration": 60})
    assert wo.type == WorkoutType.STRENGTH
    wo2 = ZeppParser().parse_workout({"type": "zzz", "duration": 10})
    assert wo2.type == WorkoutType.OTHER


def test_parse_real_sport_history_summary():
    rows = ZeppParser().parse_sport_history({
        "code": 1,
        "message": "success",
        "data": {
            "summary": [{
                "trackid": 1_777_334_400,
                "end_time": 1_777_336_290,
                "type": 1,
                "avg_heart_rate": "151.0",
                "max_heart_rate": "173.0",
                "exercise_load": "105.0",
                "calorie": "312.0",
                "dis": 5_100,
                "source": "opaque-detail-source",
            }]
        },
    }, sport_hint="run")

    assert len(rows) == 1
    assert rows[0].type == WorkoutType.RUNNING
    assert rows[0].duration == 31
    assert rows[0].heart_rate_avg == 151
    assert rows[0].heart_rate_max == 173
    assert rows[0].load == 105
    assert rows[0].calories == 312
    assert rows[0].distance_km == 5.1
    assert rows[0].vendor_source == "opaque-detail-source"


def test_verified_zepp_strength_type_is_preserved_and_normalized():
    rows = ZeppParser().parse_sport_history({
        "data": {
            "summary": [{
                "trackid": 1_777_334_400,
                "end_time": 1_777_334_700,
                "type": 52,
            }]
        }
    }, sport_hint="run")

    assert rows[0].type == WorkoutType.STRENGTH
    assert rows[0].vendor_type_id == 52
    assert rows[0].sport_mode_label == "力量训练"
    assert rows[0].recognition_confidence_label == "较高"


@pytest.mark.parametrize(
    ("vendor_type_id", "expected_type", "expected_label"),
    [
        (1, WorkoutType.RUNNING, "户外跑"),
        (6, WorkoutType.SWIMMING, "泳池游泳"),
        (8, WorkoutType.CYCLING, "室内骑行"),
        (9, WorkoutType.OTHER, "椭圆机"),
        (10, WorkoutType.OTHER, "攀登"),
        (18, WorkoutType.OTHER, "足球"),
        (92, WorkoutType.OTHER, "羽毛球"),
        (146, WorkoutType.OTHER, "民族舞"),
    ],
)
def test_public_zepp_workout_modes_use_decimal_vendor_ids(
    vendor_type_id, expected_type, expected_label
):
    rows = ZeppParser().parse_sport_history({
        "data": {"summary": [{
            "trackid": 1_777_334_400,
            "end_time": 1_777_334_700,
            "type": vendor_type_id,
        }]}
    })

    assert rows[0].type == expected_type
    assert rows[0].sport_mode_label == expected_label
    assert rows[0].recognition_confidence == "HIGH"
    assert rows[0].recognition_source == "public_zepp_enum"


def test_unknown_numeric_sport_type_does_not_inherit_run_endpoint():
    rows = ZeppParser().parse_sport_history({
        "data": {
            "summary": [{
                "trackid": 1_777_334_400,
                "end_time": 1_777_334_700,
                "type": 999,
            }]
        }
    }, sport_hint="run")

    assert rows[0].type == WorkoutType.OTHER
    assert rows[0].vendor_type_id == 999
    assert rows[0].sport_mode_label == "未知运动（编号 999）"
    assert rows[0].recognition_confidence_label == "无法识别"


def test_sport_history_normalizes_vendor_negative_sentinels():
    rows = ZeppParser().parse_sport_history({
        "data": {
            "summary": [{
                "trackid": 1_777_334_400,
                "end_time": 1_777_334_700,
                "type": 1,
                "avg_heart_rate": -1,
                "max_heart_rate": -1,
                "exercise_load": -1,
                "calorie": -1,
                "dis": -1,
            }]
        }
    })

    assert rows[0].heart_rate_avg == 0
    assert rows[0].heart_rate_max == 0
    assert rows[0].load == 0
    assert rows[0].calories == 0
    assert rows[0].distance_km == 0


def test_decode_workout_heart_rate_to_second_level_samples():
    start = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    rows = ZeppParser.parse_workout_heart_rate({
        "data": {
            "trackid": int(start.timestamp()),
            "time": "1;1;1;",
            "heart_rate": "1,80;1,2;1,-1;",
        }
    }, summary_end=start + timedelta(seconds=3))

    assert [row.timestamp for row in rows] == [
        start,
        start + timedelta(seconds=1),
        start + timedelta(seconds=2),
        start + timedelta(seconds=3),
    ]
    assert [row.heart_rate for row in rows] == [80, 80, 82, 81]
    assert {row.source_scope for row in rows} == {"unknown"}
    assert {row.device_id for row in rows} == {None}


def test_decode_workout_heart_rate_rejects_missing_series():
    assert ZeppParser.parse_workout_heart_rate({"data": {"trackid": 1_700_000_000}}) == []


def test_parse_heart_rate_samples():
    rows = ZeppParser.parse_heart_rate_samples({
        "items": [
            {"timestamp": 1_700_000_000, "value": 72, "deviceId": "A1B2C3D4E5F60708"},
            {"timestamp": 1_700_000_060_000, "heartRate": "75"},
            {"timestamp": 1_700_000_120, "value": 999},
        ]
    })
    assert [row.value for row in rows] == [72, 75]
    assert rows[0].unit == "bpm"
    assert rows[0].device_id == "A1B2C3D4E5F60708"


def test_parse_daily_metrics():
    rows = ZeppParser.parse_daily_metrics({
        "items": [{
            "eventType": "readiness",
            "timestamp": 1_777_334_400_000,
            "value": {"date": "2026-04-30", "watchScore": 83, "phyScore": 79},
        }]
    })
    values = {row.metric: row.value for row in rows}
    assert values == {"readiness": 83, "physical_readiness": 79}


def test_parse_spo2_wellness():
    rows, samples = ZeppParser.parse_wellness({
        "items": [{
            "subType": "click",
            "extra": '{"spo2": 97, "timestamp": 1777334400000}',
        }]
    }, "wellness:spo2:user:2026-04-28:2026-04-29")
    assert rows == []
    assert len(samples) == 1
    assert samples[0].metric == "spo2"
    assert samples[0].value == 97


def test_parse_hrv_wellness_preserves_sample_device_ids():
    rows, samples = ZeppParser.parse_wellness({
        "items": [{
            "value": {
                "startTime": 1_777_334_400_000,
                "deviceId": "2,A1B2C3D4E5F60708;1,B1C2D3E4F5061728",
                "samples": [
                    {"s": 0, "hrv": 52},
                    {"s": 60_000, "hrv": 54},
                    {"s": 120_000, "hrv": 59},
                ],
            },
        }]
    }, "wellness:hrv_rmssd:v2:2026-04-28:2026-04-29")

    assert rows == []
    assert [sample.device_id for sample in samples] == [
        "A1B2C3D4E5F60708",
        "A1B2C3D4E5F60708",
        "B1C2D3E4F5061728",
    ]


def test_parse_hrv_wellness_rejects_incomplete_device_map():
    _, samples = ZeppParser.parse_wellness({
        "items": [{
            "value": {
                "startTime": 1_777_334_400_000,
                "deviceId": "1,A1B2C3D4E5F60708",
                "samples": [{"s": 0, "hrv": 52}, {"s": 60_000, "hrv": 54}],
            },
        }]
    }, "wellness:hrv_rmssd:v2:2026-04-28:2026-04-29")

    assert [sample.device_id for sample in samples] == [None, None]


def test_parse_band_heart_rate_uses_local_midnight_and_device():
    summary = base64.b64encode(json.dumps({"tz": 8 * 60 * 60}).encode()).decode()
    readings = base64.b64encode(bytes([0, 72, 255, 75])).decode()
    rows = ZeppParser.parse_band_heart_rate({
        "data": {"items": [{
            "date_time": "2026-04-30",
            "device_id": "A1B2C3D4E5F60708",
            "summary": summary,
            "data_hr": readings,
        }]},
    })

    assert [row.value for row in rows] == [72, 75]
    assert [row.timestamp for row in rows] == [
        datetime(2026, 4, 29, 16, 1, tzinfo=timezone.utc),
        datetime(2026, 4, 29, 16, 3, tzinfo=timezone.utc),
    ]
    assert {row.device_id for row in rows} == {"A1B2C3D4E5F60708"}


def test_parse_band_sleep_renders_vendor_local_clock_time():
    start = datetime(2026, 8, 27, 16, 58, tzinfo=timezone.utc)
    end = datetime(2026, 8, 28, 0, 36, tzinfo=timezone.utc)
    summary = base64.b64encode(json.dumps({
        "tz": 8 * 60 * 60,
        "slp": {
            "st": int(start.timestamp()),
            "ed": int(end.timestamp()),
            "wk": 11,
            "dp": 70,
            "lt": 300,
            "ss": 83,
        },
    }).encode()).decode()

    sleeps, _ = ZeppParser().parse_band({"data": {"items": [{
        "date_time": "2026-08-28",
        "summary": summary,
    }]}})

    sleep = sleeps[date(2026, 8, 28)]
    assert sleep.bedtime.isoformat() == "00:58:00"
    assert sleep.wake_time.isoformat() == "08:36:00"


def test_parse_charge_daily_and_timestamped_samples():
    raw = {"items": [{
        "eventType": "Charge",
        "timestamp": 1_777_334_400_000,
        "value": {
            "startTime": 1_777_334_400_000,
            "deviceId": "2,A1B2C3D4E5F60708",
            "samples": [
                {"s": 0, "total": 60, "physical": 70, "mental": 50},
                {"s": 60_000, "total": 61, "physical": 71, "mental": 51},
            ],
        },
    }]}

    daily = {row.metric: row.value for row in ZeppParser.parse_daily_metrics(raw)}
    samples = ZeppParser.parse_charge_samples(raw)
    assert daily == {"hybrid_charge": 61, "physical_charge": 71, "mental_charge": 51}
    assert len(samples) == 6
    assert {sample.device_id for sample in samples} == {"A1B2C3D4E5F60708"}
    assert {sample.metric for sample in samples} == {
        "hybrid_charge", "physical_charge", "mental_charge",
    }


def test_parse_sdnn_samples_and_readiness_extensions():
    hrv = ZeppParser.parse_hrv_samples({"items": [{
        "value": {
            "startTime": 1_777_334_400_000,
            "deviceId": "1,A1B2C3D4E5F60708",
            "samples": [{"s": 60_000, "sdnn": 58}],
        },
    }]})
    readiness_raw = {"items": [{
        "eventType": "readiness",
        "timestamp": 1_777_334_400_000,
        "value": {
            "deviceId": "A1B2C3D4E5F60708",
            "rdnsScore": 88,
            "skinTempScore": 93,
            "skinTempCalibrated": -27,
            "sleepHRV": 61,
            "sleepRHR": 49,
            "ahiScore": 100,
        },
    }]}
    readiness = ZeppParser.parse_daily_metrics(readiness_raw)
    readiness_samples = ZeppParser.parse_readiness_samples(readiness_raw)
    values = {row.metric: row.value for row in readiness}

    assert hrv[0].metric == "hrv_sdnn"
    assert hrv[0].timestamp == datetime(2026, 4, 28, 0, 1, tzinfo=timezone.utc)
    assert hrv[0].device_id == "A1B2C3D4E5F60708"
    assert values["skin_temp_delta"] == -0.27
    assert values["sleep_hrv"] == 61
    assert values["sleep_rhr"] == 49
    assert values["ahi_readiness"] == 100
    assert {sample.metric for sample in readiness_samples} >= {
        "readiness", "skin_temp_delta", "sleep_hrv", "sleep_rhr",
    }
    assert {sample.device_id for sample in readiness_samples} == {"A1B2C3D4E5F60708"}


def test_parse_lactate_threshold_and_full_stress_summary():
    lactate, _ = ZeppParser.parse_wellness({"items": [{
        "value": {"samples": [{
            "dateString": "2026-04-30",
            "lactateThresholdHr": 175,
            "lactateThresholdPace": 309,
        }]},
    }]}, "wellness:lactate_threshold:v2:2026-04-30:2026-04-30")
    stress, _ = ZeppParser.parse_wellness({"items": [{
        "timestamp": 1_777_334_400_000,
        "avgStress": 32,
        "minStress": 10,
        "maxStress": 71,
        "relaxProportion": 40,
        "normalProportion": 35,
        "mediumProportion": 20,
        "highProportion": 5,
    }]}, "wellness:all_day_stress:user:2026-04-30:2026-04-30")

    assert {row.metric for row in lactate} == {
        "lactate_threshold_hr", "lactate_threshold_pace",
    }
    assert {row.metric for row in stress} == {
        "stress", "stress_min", "stress_max", "stress_relaxed_pct",
        "stress_normal_pct", "stress_medium_pct", "stress_high_pct",
    }


def test_parse_dense_file_index_preserves_device_and_coverage_only():
    start = 1_777_334_400_000
    rows = ZeppParser.parse_dense_file_index({"items": [{
        "value": {
            "startTime": start,
            "deviceId": "1,A1B2C3D4E5F60708;1,B1C2D3E4F5061728",
            "samples": [
                {
                    "s": 60_000,
                    "e": 120_000,
                    "fileId": "opaque-file-one",
                    "fileType": "SEC_HR",
                    "dateString": "2026-04-28",
                },
                {
                    "s": 180_000,
                    "e": 0,
                    "fileId": "opaque-file-two",
                    "fileType": "SEC_HR",
                    "dateString": "2026-04-28",
                },
            ],
        },
    }]})

    assert len(rows) == 2
    assert rows[0].start_utc == datetime(2026, 4, 28, 0, 1, tzinfo=timezone.utc)
    assert rows[0].end_utc == datetime(2026, 4, 28, 0, 2, tzinfo=timezone.utc)
    assert rows[1].end_utc is None
    assert [row.device_id for row in rows] == [
        "A1B2C3D4E5F60708", "B1C2D3E4F5061728",
    ]
    assert {row.parse_status for row in rows} == {"indexed"}
    assert {row.sample_count for row in rows} == {0}
