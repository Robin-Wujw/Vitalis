"""Family-level descriptive queries and identifiable range extrema."""
from datetime import date

import pytest

from tests.test_workout_parity import START, workout
from vitalis.intelligence.workout_statistics import METRICS, workout_statistics


def test_same_family_modes_get_weighted_pace_and_one_full_range_trend():
    rows = [workout(str(i), sport_mode=mode, started_at=f"2026-09-0{i}T08:00:00+00:00",
        distance_km=distance, moving_time_seconds=seconds)
        for i, mode, distance, seconds in (
            (1, "outdoor_running", 5, 1500), (2, "trail_running", 2, 800),
            (3, "treadmill", 1, 500))]
    result = workout_statistics(rows, start=date(2026, 9, 1), end=date(2026, 9, 3),
        timezone_name="UTC", metric="pace", group_by="sport_family")
    assert result["overall"]["metrics"]["pace"]["value"] == 350
    assert result["by_sport_family"][0]["metrics"]["pace"]["used_observations"] == 3
    series = result["series"]["family_partitions"]
    assert len(series) == 1 and series[0]["sport_family"] == "running"
    assert series[0]["observations"] == series[0]["workouts"] == 3
    assert series[0]["trend"]["change_per_30_days"] == 3000
    assert series[0]["best"]["workout_id"] == "1"
    assert series[0]["best"]["selection"] == "minimum"


def test_family_series_keeps_sources_families_and_unknown_modes_separate():
    rows = [workout("run", distance_km=5, moving_time_seconds=1500),
        workout("ride", sport="cycling", distance_km=20, moving_time_seconds=1800),
        workout("other-source", source="other", distance_km=5, moving_time_seconds=1200),
        workout("unknown-a", sport="other", sport_mode="alpha", calories=20),
        workout("unknown-b", sport="other", sport_mode="beta", calories=30)]
    result = workout_statistics(rows, start=START.date(), end=START.date(), timezone_name="UTC", metric="pace")
    assert result["overall"]["metrics"]["pace"]["value"] is None
    assert len(result["series"]["family_partitions"]) == 3
    unknown = workout_statistics(rows[-2:], start=START.date(), end=START.date(), timezone_name="UTC", metric="calories")
    assert len(unknown["series"]["family_partitions"]) == 2
    collision = workout_statistics([workout("known", calories=10),
        workout("unclassified", sport="other", sport_mode="running", calories=30)],
        start=START.date(), end=START.date(), timezone_name="UTC", metric="calories")
    assert len(collision["series"]["family_partitions"]) == 2


@pytest.mark.parametrize("metric", METRICS)
def test_every_metric_extremum_identifies_the_observed_workout(metric):
    evidence = {key: {"value": 12, "unit": unit, "qualification": "vendor_reported"}
        for key, (unit, _) in METRICS.items()}
    rows = [workout("run", distance_km=5, moving_time_seconds=1500, heart_rate_avg=140,
        heart_rate_max=170, load=70, training_effect=3, calories=300, vendor_summary_metrics=evidence),
        workout("swim", sport="swimming", distance_km=.5, moving_time_seconds=900, vendor_summary_metrics=evidence),
        workout("strength", sport="strength", strength_sets=[{"repetitions": 8}])]
    result = workout_statistics(rows, start=START.date(), end=START.date(), timezone_name="UTC", metric=metric)
    for group in result["series"]["partitions"]:
        assert group["best"]["workout_id"] in {point["workout_id"] for point in group["values"]}
        assert group["best"]["unit"] == METRICS[metric][0]
        assert group["best"]["observed_at"] and group["best"]["source"]
        assert group["best"]["scope"] == "queried_range"
