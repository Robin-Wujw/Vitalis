"""Chart projections preserve real time, provenance, and absent observations."""
from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint


def raw_profile():
    return RawDailyProfile(
        user_id="synthetic-chart", day=date(2026, 10, 7),
        as_of=datetime(2026, 10, 7, 14, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )


def point(metric, minute, value, *, device="device-a", source="zepp"):
    stamp = datetime(2026, 10, 7, 0, minute, tzinfo=timezone.utc)
    return SeriesPoint(metric, value, "bpm" if metric == "heart_rate" else "score",
                       date(2026, 10, 7), stamp, source, "device", device)


def test_minute_chart_keeps_gap_and_separates_devices():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    raw.chart_samples = {"heart_rate": [point("heart_rate", 0, 60), point("heart_rate", 1, 62),
                                           point("heart_rate", 20, 80), point("heart_rate", 0, 90, device="device-b")]}
    bundle = build_chart_bundle(raw, raw.day, raw.day, period="daily")
    chart = next(item for item in bundle.charts if item.key == "heart_rate")
    assert len(chart.series) == 2
    first = next(item for item in chart.series if item.device_id == "device-a")
    assert [p.value for p in first.points if p.value is not None] == [60, 62, 80]
    assert any(p.value is None for p in first.points)
    assert first.points[-1].timestamp - first.points[0].timestamp == timedelta(minutes=20)
    assert chart.resolution_seconds == 60
    assert chart.period_start.hour == 16  # local midnight, UTC previous day
    assert len(first.points) < 1440


def test_sparse_stress_is_scatter_and_missing_is_not_zero():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    raw.chart_samples = {"stress": [point("stress", 0, 20), point("stress", 6, 70)]}
    chart = next(c for c in build_chart_bundle(raw, raw.day, raw.day).charts if c.key == "stress")
    assert chart.kind == "scatter"
    assert [p.value for p in chart.series[0].points if p.value is not None] == [20, 70]
    assert all(p.value is None or p.sample_count > 0 for p in chart.series[0].points)


def test_charge_components_survive_missing_total():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    raw.chart_samples = {"physical_charge": [point("physical_charge", 0, 44)],
                         "mental_charge": [point("mental_charge", 0, 77)]}
    chart = next(c for c in build_chart_bundle(raw, raw.day, raw.day).charts if c.key == "charge")
    assert {s.key.split(":")[0] for s in chart.series} == {"physical_charge", "mental_charge"}
    assert chart.state == "partial"
    assert all(s.qualification == "vendor_reference" for s in chart.series)


def test_daily_trends_keep_local_missing_days_and_vendor_load_band():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    start = raw.day - timedelta(days=6)
    for metric, value in (("steps", 6000), ("vendor_load_7d", 350),
                          ("vendor_load_optimal_min", 250), ("vendor_load_optimal_max", 400),
                          ("vendor_load_overreaching", 500)):
        raw.series[metric] = [SeriesPoint(metric, value, "steps" if metric == "steps" else "load",
            start, start, "zepp", "user_fused")]
    bundle = build_chart_bundle(raw, start, raw.day, period="weekly")
    steps = next(c for c in bundle.charts if c.key == "daily_steps")
    assert len(steps.series[0].points) == 7
    assert steps.series[0].points[1].value is None
    load = next(c for c in bundle.charts if c.key == "vendor_load_7d")
    assert load.series[0].points[0].value == 350
    assert load.bands[0].points[0].lower == 250
    assert load.bands[0].points[0].upper == 400
    assert load.bands[0].qualification == "vendor_reference"
    assert any("Zepp" in note for note in load.notes)


def test_sleep_hypnogram_keeps_nap_and_unknown_separate():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    raw.sleep_by_day[raw.day] = {"source": "zepp", "sleep_duration": 420, "sleep_score": 84,
        "stages": [{"stage": "deep", "start_time": "2026-10-06T16:00:00Z", "end_time": "2026-10-06T17:00:00Z"}]}
    raw.chart_naps = {raw.day: [{"started_at": "2026-10-07T05:00:00Z", "ended_at": "2026-10-07T05:20:00Z",
        "sleep_minutes": None, "stages": [{"stage": "unknown", "start_time": "2026-10-07T05:00:00Z", "end_time": "2026-10-07T05:20:00Z"}]}]}
    bundle = build_chart_bundle(raw, raw.day, raw.day)
    chart = next(c for c in bundle.charts if c.key == "sleep")
    assert {i.category for i in chart.intervals} == {"deep", "unknown"}
    assert any("午睡" in i.label for i in chart.intervals)
    assert "420" in " ".join(chart.summary)


def test_chart_cutoff_excludes_future_samples_and_annotations():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    from dataclasses import replace
    future = replace(point("heart_rate", 0, 99), observed_at=raw.as_of + timedelta(minutes=1))
    raw.chart_samples = {"heart_rate": [point("heart_rate", 0, 60), future]}
    raw.chart_inputs = {"annotations": [{"start": "2026-10-07T00:00:00Z", "end": "2026-10-07T00:10:00Z",
                                           "text": "合成会议", "created_at": "2026-10-08T00:00:00Z"}]}
    chart = next(c for c in build_chart_bundle(raw, raw.day, raw.day).charts if c.key == "heart_rate")
    assert 99 not in [p.value for p in chart.series[0].points]
    assert not any(i.label == "合成会议" for i in chart.intervals)


def test_mapped_hevy_sets_use_explicit_roles_and_do_not_double_count_linked_zepp():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    raw.chart_inputs = {"external_workouts": [{"started_at":"2026-10-07T10:00:00Z",
        "linked_workout_source":"zepp","linked_workout_id":"one-session", "sets":[
            {"exercise_name":"卧推", "set_type":"normal", "mapping_status":"user_mapped",
             "primary_muscles":["chest"],"secondary_muscles":["triceps"],"side":"bilateral"},
            {"exercise_name":"卧推", "set_type":"warmup", "mapping_status":"user_mapped",
             "primary_muscles":["chest"],"secondary_muscles":[],"side":"bilateral"},
            {"exercise_name":"未知动作", "set_type":"normal", "mapping_status":"unknown"}]}]}
    bundle=build_chart_bundle(raw,raw.day,raw.day)
    body=next(c for c in bundle.charts if c.key=="muscle_volume")
    chest=next(r for r in body.regions if r.key=="chest")
    assert chest.direct_sets==1 and chest.source=="hevy"
    assert chest.qualification=="user_confirmed_mapping"
    assert any("1 个记录组" in s for s in body.summary)


def test_workout_first_partial_minute_stays_inside_chart_window():
    from vitalis.intelligence.charts import _series
    start=datetime(2026,10,7,12,0,30,tzinfo=timezone.utc)
    points=[{"timestamp":start+timedelta(seconds=10),"value":90,"unit":"bpm","source":"zepp","source_scope":"workout_detail"}]
    series,_=_series("heart_rate","心率",points,start,start+timedelta(minutes=5),start+timedelta(minutes=5),60)
    assert series[0].points[0].timestamp==start
    assert series[0].points[0].value==90


def test_daily_charts_preserve_saved_vendor_qualification():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=raw_profile()
    raw.series["sleep_hrv"]=[SeriesPoint("sleep_hrv",60,"ms",raw.day,raw.day,"zepp","user_fused",qualification="vendor_reference")]
    chart=next(c for c in build_chart_bundle(raw,raw.day,raw.day,period="weekly").charts if c.key=="daily_sleep_hrv")
    assert chart.series[0].qualification=="vendor_reference"


def test_invalid_chart_window_rejected():
    from vitalis.intelligence.charts import build_chart_bundle
    raw = raw_profile()
    with pytest.raises(ValueError):
        build_chart_bundle(raw, raw.day, raw.day - timedelta(days=1))
