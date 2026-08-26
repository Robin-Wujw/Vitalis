"""Zepp 厂商格式 -> Vitalis Schema 解析器测试。"""

from datetime import date

from vitalis.connectors.zepp.parser import ZeppParser
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


def test_parse_heart_rate_samples():
    rows = ZeppParser.parse_heart_rate_samples({
        "items": [
            {"timestamp": 1_700_000_000, "value": 72, "deviceId": "watch-1"},
            {"timestamp": 1_700_000_060_000, "heartRate": "75"},
            {"timestamp": 1_700_000_120, "value": 999},
        ]
    })
    assert [row.value for row in rows] == [72, 75]
    assert rows[0].unit == "bpm"
    assert rows[0].device_id == "watch-1"


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
                "deviceId": "2,watch-a;1,watch-b",
                "samples": [
                    {"s": 0, "hrv": 52},
                    {"s": 60_000, "hrv": 54},
                    {"s": 120_000, "hrv": 59},
                ],
            },
        }]
    }, "wellness:hrv_rmssd:v2:2026-04-28:2026-04-29")

    assert rows == []
    assert [sample.device_id for sample in samples] == ["watch-a", "watch-a", "watch-b"]


def test_parse_hrv_wellness_rejects_incomplete_device_map():
    _, samples = ZeppParser.parse_wellness({
        "items": [{
            "value": {
                "startTime": 1_777_334_400_000,
                "deviceId": "1,watch-a",
                "samples": [{"s": 0, "hrv": 52}, {"s": 60_000, "hrv": 54}],
            },
        }]
    }, "wellness:hrv_rmssd:v2:2026-04-28:2026-04-29")

    assert [sample.device_id for sample in samples] == [None, None]
