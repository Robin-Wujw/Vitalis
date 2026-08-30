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
