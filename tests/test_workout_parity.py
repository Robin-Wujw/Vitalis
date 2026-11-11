"""Synthetic protocol parity: summary, sections, and independent statistics."""
import json
from datetime import UTC, date, datetime

import pytest

from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.adapters.zepp.parsers.workout_detail import ParseContext, parse_workout_detail
from vitalis.adapters.zepp.workout_streaming import spool_bytes


START = datetime(2026, 9, 1, 8, tzinfo=UTC)


def summary(**fields):
    return ZeppParser().parse_sport_history({"data": [{
        "trackid": int(START.timestamp()), "end_time": int(START.timestamp()) + 600,
        "type": 14, "source": "cloud", **fields,
    }]})[0]


def test_summary_preserves_specialties_plan_pb_and_units():
    row = summary(swolf=42, total_strokes=280, total_trips=20, swim_pool_length=25,
                  swim_style=2, averageGct=247, averageVo=82, avgVertStrideRatio=80,
                  rope_skipping_count=250, numberOfConsecutive=125,
                  runningProgram=9, runningType=2, dailyPlanFinished=True, dailyScore=98.5,
                  pb='{"swim_furthest_m":500}', lactateThresholdHr=162, VO2_max=49)
    metrics = row.vendor_summary_metrics
    assert metrics["swolf"]["value"] == 42
    assert metrics["strokes"]["source_field"] == "total_strokes"
    assert metrics["ground_contact_time"]["unit"] == "ms"
    assert metrics["vertical_stride_ratio"]["value"] == 8
    assert metrics["total_jumps"]["value"] == 250
    assert row.vendor_training_plan["plan_session_finished"] is True
    assert row.vendor_personal_bests["values"] == {"swim_furthest_m": 500}
    assert metrics["lactate_threshold_hr"]["value"] == 162
    assert row.vendor_summary_metrics["swolf"]["observed_at"] == START.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("bad", [-1, True, float("nan"), float("inf"), "unknown"])
def test_summary_sentinels_never_become_facts(bad):
    row = summary(swolf=bad, total_strokes=bad, averageGct=bad)
    assert not row.vendor_summary_metrics


def test_streaming_and_normal_sections_match_and_location_is_separate():
    lap = "1,30,25,-1,120,30,-1,-1,-1,1,-1,-1,20,12,42"
    split = "1,300,private-geohash,-1,130,300,-1,-1,-1,100,-1,-1,-1,165"
    data = {"data": {"trackid": int(START.timestamp()), "lap": lap,
                     "kilo_pace": split, "longitude_latitude": "1,101.1,1.2",
                     "accuracy": "1,5", "unknown_sensor": "1,7;2,8"}}
    expected = parse_workout_detail(data, ParseContext())
    spool = spool_bytes(json.dumps(data).encode(), max_bytes=1024 * 1024)
    try:
        actual = parse_workout_detail(spool, ParseContext())
    finally:
        spool.cleanup()
    assert actual.status == expected.status == "recognized"
    assert actual.detail.model_dump() == expected.detail.model_dump()
    assert actual.detail.laps[0].vendor_metrics["swolf"] == 42
    assert actual.detail.swimming["swolf_check"]["identity_holds"] == 1
    assert actual.detail.splits["km"][0]["duration_seconds"] == 300
    assert "private-geohash" not in json.dumps(actual.detail.splits)
    assert "longitude_latitude" in actual.detail.gps
    assert "longitude_latitude" not in actual.detail.vendor_streams
    assert "unknown_sensor" in actual.detail.vendor_streams


def workout(identifier, *, source="zepp", sport="running", **fields):
    return {"workout_id": identifier, "source": source, "type": sport,
            "sport_mode": sport, "training_family": "aerobic",
            "started_at": START.isoformat(), "vendor_summary_metrics": {}, **fields}


def test_statistics_never_mix_sources_sports_or_missing_with_zero():
    from vitalis.intelligence.workout_statistics import workout_statistics
    data = [workout("a", distance_km=5, moving_time_seconds=1500, heart_rate_avg=140),
            workout("b", distance_km=1, moving_time_seconds=600, heart_rate_avg=160),
            workout("c", sport="swimming", distance_km=.5, moving_time_seconds=900),
            workout("d", source="other", distance_km=5, moving_time_seconds=1000)]
    result = workout_statistics(data, start=date(2026, 9, 1), end=date(2026, 9, 1),
                                timezone_name="UTC", group_by="month", metric="pace")
    assert result["overall"]["metrics"]["calories"]["value"] is None
    assert result["overall"]["metrics"]["calories"]["observations"] == 0
    assert result["overall"]["metrics"]["pace"]["value"] is None
    running = next(x for x in result["by_sport"] if x["source"] == "zepp" and x["sport"] == "running")
    assert running["metrics"]["pace"]["value"] == 350
    assert running["metrics"]["avg_heart_rate"]["value"] == pytest.approx(145.714286)
    assert len(result["series"]["partitions"]) == 2
    assert result["records"]["scope"] == "queried_range"
    assert len(result["metric_catalog"]) == 20


def test_statistics_local_activity_day_and_trend():
    from vitalis.intelligence.workout_statistics import workout_statistics
    rows = [workout(str(i), started_at=f"2026-09-0{i}T23:30:00+00:00", distance_km=i)
            for i in (1, 2, 3)]
    result = workout_statistics(rows, start=date(2026, 9, 2), end=date(2026, 9, 4),
                                timezone_name="Asia/Singapore", group_by="week", metric="distance")
    series = result["series"]["partitions"][0]
    assert series["values"][0]["activity_day"] == "2026-09-02"
    assert series["trend"]["change_per_30_days"] == 30000
    assert result["range"]["day_semantics"] == "activity_day"


def test_statistics_all_twenty_metrics_have_real_values_and_empty_is_unknown():
    from vitalis.intelligence.workout_statistics import METRICS, workout_statistics
    extras = {key: {"value": 12, "unit": unit, "qualification": "vendor_reported"}
              for key, (unit, _) in METRICS.items()}
    rows = [workout("r", distance_km=5, moving_time_seconds=1500, heart_rate_avg=140,
                    heart_rate_max=170, load=70, training_effect=3, calories=300,
                    vendor_summary_metrics=extras),
            workout("s", sport="swimming", distance_km=.5, moving_time_seconds=900, vendor_summary_metrics=extras),
            workout("g", sport="strength", strength_sets=[{"repetitions": 8}, {"repetitions": 10}])]
    for metric in METRICS:
        result = workout_statistics(rows, start=START.date(), end=START.date(), timezone_name="UTC", metric=metric)
        assert result["series"]["partitions"], metric
        assert all(p["values"] and p["values"][0]["value"] > 0 for p in result["series"]["partitions"])
    empty = workout_statistics([], start=START.date(), end=START.date(), timezone_name="UTC")
    assert empty["state"] == "no_data"
    assert all(item["value"] is None and item["coverage"] is None for item in empty["overall"]["metrics"].values())


def test_api_workout_statistics_sections_and_pagination(client, issue_token):
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.domain import Workout, WorkoutDetail, WorkoutMetricSample
    owner = "workout-parity-owner"
    headers = {"Authorization": "Bearer " + issue_token(owner, scopes=["read"])}
    outsider = {"Authorization": "Bearer " + issue_token("workout-parity-other", scopes=["read"])}
    with session_scope() as db:
        repo = HealthRepository(db)
        for i in range(3):
            repo.save_workout(Workout(user_id=owner, workout_id=f"r{i}", started_at=START,
                duration=30, type="running", sport_mode="running", vendor_type_id=1,
                distance_km=5, moving_time_seconds=1500))
        repo.save_workout_detail(owner, "r0", WorkoutDetail(workout_id="r0",
            gps={"longitude_latitude": {"rows": ["synthetic-coordinate"]}},
            vendor_streams={"opaque": {"rows": ["1,42"]}},
            splits={"km": [{"index": 1, "duration_seconds": 300}]}).model_dump(),
            samples=[WorkoutMetricSample(workout_id="r0", timestamp=START, metric="heart_rate", value=140, unit="bpm")])
    path = "/api/workouts/statistics?from=2026-09-01&to=2026-09-01&metric=pace&sport=1"
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["overall"]["metrics"]["pace"]["value"] == 300
    assert client.get(path, headers=outsider).json()["state"] == "no_data"
    page = client.get("/api/workouts?from=2026-09-01&to=2026-09-01&limit=1&offset=1&sport=run", headers=headers).json()
    assert page["total"] == 3 and page["next_offset"] == 2 and page["returned"] == 1
    detail = "/api/workouts/r0/sections?source=zepp"
    response = client.get(detail, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["splits"]["km"][0]["duration_seconds"] == 300
    assert "gps" not in response.json() and "streams" not in response.json()
    original = client.get("/api/workouts/r0?source=zepp", headers=headers).json()
    assert "gps" not in original["detail"] and "vendor_streams" not in original["detail"]
    selected = client.get(detail + "&include=gps&include=streams", headers=headers).json()
    assert "longitude_latitude" in selected["gps"]
    assert selected["streams"]["samples"][0]["value"] == 140
    assert client.get(detail, headers=outsider).status_code == 404
    invalid = client.get(path + "&group_by=invalid", headers=headers)
    assert invalid.status_code == 422 and invalid.json()["failure_code"] == "validation_error"


def test_summary_new_fields_roundtrip_without_spurious_revision():
    from vitalis.adapters.persistence import HealthRepository, session_scope
    row = summary(type=52, total_group=2, strength_training_group='[{"count":8,"actionType":3},{"count":10}]',
                  pb='{"most_reps":18}', parent_trackid=123, swolf=40)
    row.user_id = "workout-extras-idempotent"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(row.user_id)
        repo.save_workout(row)
        revision = repo.analysis_input_revision(row.user_id)
        first = repo.workout(row.user_id, row.workout_id).data.copy()
        repo.save_workout(row)
        assert repo.analysis_input_revision(row.user_id) == revision
        assert repo.workout(row.user_id, row.workout_id).data == first
        assert len(first["vendor_sets"]) == 2
        assert first["vendor_summary_metrics"]["swolf"]["source"] == "zepp"


def test_workout_schema_documents_normalized_and_opaque_fields(client, issue_token):
    headers = {"Authorization": "Bearer " + issue_token("workout-schema", scopes=["read"])}
    result = client.get("/api/workouts/catalog", headers=headers)
    assert result.status_code == 200
    schema = result.json()
    assert schema["stream_units"]["currentDistance"]["unit"] == "m"
    assert schema["stream_units"]["currentDistance"]["scale"] == .01
    assert schema["stream_units"]["longitude_latitude"]["qualification"] == "vendor_encoded_unverified"
    assert schema["lap_columns"]["14"]["name"] == "swolf"


def test_unknown_mode_never_combines_different_workout_types():
    from vitalis.intelligence.workout_statistics import workout_statistics
    rows = [workout("run", sport_mode="unknown", distance_km=5, moving_time_seconds=1500),
            workout("ride", sport="cycling", sport_mode="unknown", distance_km=20, moving_time_seconds=1800)]
    result = workout_statistics(rows, start=START.date(), end=START.date(), timezone_name="UTC", metric="pace")
    assert result["overall"]["metrics"]["pace"]["value"] is None
    assert len(result["series"]["partitions"]) == 2


def test_summary_power_uses_available_alternative_when_primary_is_sentinel():
    row = summary(type=1, average_power=-1, avg_power=210)
    assert row.vendor_summary_metrics["power"]["value"] == 210
