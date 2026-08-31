from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.contracts import ConfidenceBand
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.running import RunningAnalyzer
from vitalis.models import WorkoutMetricSample


TARGET = date(2026, 8, 29)
START = datetime(2026, 8, 29, 6, tzinfo=timezone.utc)


def _sample(second: int, metric: str, value: float, unit: str):
    return WorkoutMetricSample(
        workout_id="run-1",
        timestamp=START + timedelta(seconds=second),
        metric=metric,
        value=value,
        unit=unit,
    )


def _run(samples, *, duration=30, distance=5.0, day=TARGET, workout_id="run-1"):
    for sample in samples:
        sample.workout_id = workout_id
    return {
        "workout_id": workout_id,
        "local_day": day,
        "samples": samples,
        "data": {
            "type": "running",
            "training_family": "aerobic",
            "duration": duration,
            "distance_km": distance,
            "heart_rate_avg": 138,
            "heart_rate_max": 150,
        },
    }


def _raw(workouts, threshold=170):
    raw = RawDailyProfile(user_id="runner", day=TARGET, workouts=workouts)
    if threshold is not None:
        raw.series["lactate_threshold_hr"] = [SeriesPoint(
            metric="lactate_threshold_hr",
            value=threshold,
            unit="bpm",
            day=TARGET,
            observed_at=TARGET,
            source="zepp",
            source_scope="daily_metric",
        )]
    return raw


def test_running_analysis_uses_personal_threshold_and_reports_cadence_and_drift():
    samples = []
    for second in range(30 * 60):
        samples.extend([
            _sample(second, "heart_rate", 130 if second < 900 else 143, "bpm"),
            _sample(second, "speed", 3.0, "m/s"),
            _sample(second, "cadence", 174 + second % 3, "spm"),
        ])

    analysis = RunningAnalyzer().analyze(_raw([_run(samples)]))
    session = analysis.recent_sessions[0]

    assert analysis.zone_method == "lactate_threshold"
    assert analysis.lactate_threshold_bpm == 170
    assert session.classification == "RECOVERY_RUN"
    assert session.confidence == ConfidenceBand.HIGH
    assert session.median_cadence_spm == 175
    assert session.cadence_variability_percent == 0.6
    assert session.cardiac_drift_percent == 10.0
    assert sum(zone.duration_seconds for zone in session.heart_rate_zones) == 1800


def test_running_analysis_detects_repeated_work_and_recovery_segments():
    samples = []
    second = 0
    for _ in range(4):
        for _ in range(90):
            samples.extend([
                _sample(second, "heart_rate", 172, "bpm"),
                _sample(second, "speed", 4.2, "m/s"),
            ])
            second += 1
        for _ in range(60):
            samples.extend([
                _sample(second, "heart_rate", 135, "bpm"),
                _sample(second, "speed", 2.0, "m/s"),
            ])
            second += 1

    analysis = RunningAnalyzer().analyze(
        _raw([_run(samples, duration=10, distance=2.0)])
    )
    session = analysis.recent_sessions[0]

    assert session.classification == "INTERVAL_RUN"
    assert sum(item.kind == "work" for item in session.segments) == 4
    assert sum(item.kind == "recovery" for item in session.segments) == 4
    assert "快速段" in session.segments[0].kind_label


def test_low_confidence_steady_run_does_not_report_cardiac_drift():
    samples = []
    for second in range(30 * 60):
        samples.extend([
            _sample(second, "heart_rate", 145 if second < 900 else 155, "bpm"),
            _sample(second, "speed", 3.0, "m/s"),
        ])

    session = RunningAnalyzer().analyze(_raw([_run(samples)])).recent_sessions[0]

    assert session.classification == "STEADY_RUN"
    assert session.confidence == ConfidenceBand.LOW
    assert session.cardiac_drift_percent is None
    assert any("不解释心率漂移" in item for item in session.limitations)


def test_running_analysis_does_not_invent_zones_without_threshold():
    samples = [
        _sample(second, "speed", 3.0, "m/s")
        for second in range(12 * 60)
    ]

    analysis = RunningAnalyzer().analyze(_raw([_run(samples, duration=12)], threshold=None))
    session = analysis.recent_sessions[0]

    assert analysis.zone_method == "unavailable"
    assert session.heart_rate_zones == []
    assert session.classification == "STEADY_RUN"
    assert session.confidence == ConfidenceBand.LOW
    assert any("乳酸阈心率" in item for item in analysis.limitations)


def test_running_volume_keeps_incomplete_distance_explicit():
    current = _run([], duration=40, distance=None, workout_id="run-current")
    previous = _run(
        [],
        duration=35,
        distance=5.0,
        day=TARGET - timedelta(days=30),
        workout_id="run-previous",
    )

    analysis = RunningAnalyzer().analyze(_raw([current, previous]))

    assert analysis.sessions_28d == 1
    assert analysis.distance_km_28d is None
    assert analysis.distance_change_percent is None
    assert any("总距离不可计算" in item for item in analysis.limitations)


def test_running_analysis_uses_device_zones_dynamics_and_comparable_runs():
    target_samples = []
    for second in range(60):
        target_samples.extend([
            _sample(second, "heart_rate", 165, "bpm"),
            _sample(second, "speed", 3.2, "m/s"),
            _sample(second, "running_power", 250, "W"),
            _sample(second, "equivalent_pace", 350, "s/km"),
            _sample(second, "ground_contact_time", 263, "ms"),
            _sample(second, "vertical_oscillation", 88, "mm"),
            _sample(second, "vertical_stride_ratio", 8.7, "%"),
        ])
    target = _run(target_samples, duration=29, distance=5.0)
    target["data"]["heart_rate_zone_setting_type"] = 3
    target["data"]["heart_rate_zone_boundaries_bpm"] = [113, 141, 154, 162, 173, 190]

    history = []
    for index, (days_ago, duration, heart_rate, power) in enumerate([
        (20, 30, 148, 230),
        (14, 31, 150, 235),
        (7, 32, 152, 240),
    ]):
        samples = [
            _sample(second, "running_power", power, "W")
            for second in range(10)
        ]
        run = _run(
            samples,
            duration=duration,
            distance=5.0,
            day=TARGET - timedelta(days=days_ago),
            workout_id=f"run-history-{index}",
        )
        run["data"]["heart_rate_avg"] = heart_rate
        history.append(run)

    analysis = RunningAnalyzer().analyze(_raw([*history, target], threshold=200))
    session = analysis.recent_sessions[0]

    assert analysis.schema_version == "2.0"
    assert analysis.zone_method == "device_workout"
    assert session.heart_rate_zone_source == "device_workout"
    assert session.heart_rate_zones[3].lower_bpm == 162
    assert session.heart_rate_zones[3].duration_seconds == 60
    assert session.average_power_watts == 250
    assert session.median_equivalent_pace_seconds_per_km == 350
    assert session.median_ground_contact_time_ms == 263
    assert session.median_vertical_oscillation_mm == 88
    assert session.median_vertical_stride_ratio_percent == 8.7
    assert session.comparable_baseline is not None
    assert session.comparable_baseline.sample_count == 3
    assert session.comparable_baseline.median_pace_seconds_per_km == 372
    assert session.comparable_baseline.pace_difference_percent == -6.5
    assert session.comparable_baseline.heart_rate_difference_bpm == 15
    assert session.comparable_baseline.power_difference_percent == 6.4


def test_running_analysis_computes_moving_time_stride_and_kilometer_splits():
    samples = [
        _sample(0, "distance", 0, "m"),
        _sample(300, "distance", 1000, "m"),
        _sample(660, "distance", 2000, "m"),
        _sample(900, "distance", 3000, "m"),
    ]
    for second in range(0, 901, 30):
        samples.extend([
            _sample(second, "heart_rate", 140 + second // 300, "bpm"),
            _sample(second, "altitude", 10 + second / 300, "m"),
            _sample(second, "stride_length", 108, "cm"),
        ])
    workout = _run(samples, duration=15, distance=3.0)
    workout["detail"] = {
        "pauses": [{
            "started_at": (START + timedelta(seconds=300)).isoformat(),
            "duration_seconds": 60,
        }]
    }

    session = RunningAnalyzer().analyze(_raw([workout])).recent_sessions[0]

    assert session.pause_duration_seconds == 60
    assert session.moving_duration_minutes == 14
    assert session.average_pace_seconds_per_km == 280
    assert session.median_stride_length_cm == 108
    assert len(session.kilometer_splits) == 3
    assert [split.moving_seconds for split in session.kilometer_splits] == [300, 300, 240]
    assert session.kilometer_splits[1].average_pace_seconds_per_km == 300
    assert session.kilometer_splits[0].average_heart_rate_bpm is not None
    assert session.kilometer_splits[0].elevation_gain_m is not None


def test_pause_overlap_accepts_naive_database_samples_and_aware_pause_time():
    start = START.replace(tzinfo=None)
    pauses = [(START + timedelta(seconds=30), 60)]

    overlap = RunningAnalyzer._pause_overlap_seconds(
        pauses, start, start + timedelta(seconds=120)
    )

    assert overlap == 60
