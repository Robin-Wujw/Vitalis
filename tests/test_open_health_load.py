from datetime import date, datetime, timedelta, timezone
from math import exp, isclose

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    ProfileField,
    ProfileSource,
    Sex,
    UserProfile,
)
from vitalis.intelligence.open_health.load import (
    HeartRatePoint,
    LoadWorkout,
    PauseInterval,
    compute_training_load,
    compute_workout_trimp,
)
from vitalis.models import Workout, WorkoutMetricSample, WorkoutType
from vitalis.storage import HealthRepository, session_scope


UTC = timezone.utc


def profile(hrmax=190, sex=Sex.MALE, revision=7):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return UserProfile(
        user_id="load-test-profile",
        revision=revision,
        sex=ProfileField(value=sex, source=ProfileSource.USER_CONFIRMED, confidence=ConfidenceBand.HIGH, revision=revision, updated_at=now),
        confirmed_hrmax_bpm=ProfileField(value=hrmax, source=ProfileSource.USER_CONFIRMED, confidence=ConfidenceBand.HIGH, revision=revision, updated_at=now),
    )


def workout(day=date(2026, 1, 1), *, source="zepp", workout_id="w", hr=120, count=21, cadence=30, duration_minutes=11.0, device_id="watch"):
    started = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    points = tuple(
        HeartRatePoint(started + timedelta(seconds=cadence * index), hr, source=source, device_id=device_id)
        for index in range(count)
    )
    return LoadWorkout(source, workout_id, started, started + timedelta(minutes=duration_minutes), duration_minutes, points)


def test_constant_hr_trimp_uses_male_190_and_rhr_50_without_hardcoding_user():
    result = compute_workout_trimp(workout(), profile(), rhr_bpm=50)
    hrr = (120 - 50) / (190 - 50)
    expected = hrr * 0.64 * exp(1.92 * hrr) * 10.5
    assert result.status.value == "AVAILABLE"
    assert isclose(result.payload.trimp, expected, rel_tol=1e-12)
    assert result.profile_revision_used == 7
    assert result.provenance[0].source == "zepp"


def test_pause_is_removed_from_credited_minutes():
    base = workout(count=29, cadence=30, duration_minutes=14.0)
    pause = PauseInterval(base.started_at + timedelta(seconds=300), 120)
    paused = LoadWorkout(base.source, base.workout_id, base.started_at, base.ended_at, base.duration_minutes, base.heart_rate, (pause,))
    result = compute_workout_trimp(paused, profile(), rhr_bpm=50)
    assert result.status.value == "AVAILABLE"
    assert isclose(result.payload.credited_minutes, 12.0, abs_tol=1e-9)


def test_overlapping_pause_intervals_are_subtracted_once():
    base = workout(count=29, cadence=30, duration_minutes=14.0)
    pause = PauseInterval(base.started_at + timedelta(seconds=300), 120)
    duplicated = LoadWorkout(
        base.source, base.workout_id, base.started_at, base.ended_at,
        base.duration_minutes, base.heart_rate, (pause, pause),
    )

    result = compute_workout_trimp(duplicated, profile(), rhr_bpm=50)

    assert result.status.value == "AVAILABLE"
    assert isclose(result.payload.active_duration_minutes, 12.0, abs_tol=1e-9)
    assert isclose(result.payload.credited_minutes, 12.0, abs_tol=1e-9)


def test_credited_time_is_capped_to_declared_active_duration():
    base = workout(count=121, cadence=30, duration_minutes=10.0)
    inconsistent = LoadWorkout(
        base.source,
        base.workout_id,
        base.started_at,
        base.started_at + timedelta(minutes=60),
        10.0,
        base.heart_rate,
    )

    result = compute_workout_trimp(inconsistent, profile(), rhr_bpm=50)

    assert result.status.value == "AVAILABLE"
    assert isclose(result.payload.credited_minutes, 10.0, abs_tol=1e-9)


def test_large_sample_gap_is_capped_to_median_cadence():
    base = workout(count=20, cadence=30, duration_minutes=10.0)
    started = base.started_at
    times = [started + timedelta(seconds=(index * 30 if index < 10 else index * 30 + 300)) for index in range(20)]
    points = tuple(
        HeartRatePoint(timestamp, 120, source="zepp", device_id="watch")
        for timestamp in times
    )
    gap = LoadWorkout("zepp", "gap", started, started + timedelta(minutes=20), 10.0, points)
    result = compute_workout_trimp(gap, profile(), rhr_bpm=50)
    assert result.status.value == "AVAILABLE"
    assert isclose(result.payload.credited_minutes, 10.0, abs_tol=1e-9)


def test_hr_above_hrmax_is_clamped_and_counted():
    item = workout(hr=220)
    result = compute_workout_trimp(item, profile(), rhr_bpm=50)
    expected = 0.64 * exp(1.92) * 10.5  # x is clamped to 1
    assert isclose(result.payload.trimp, expected, rel_tol=1e-12)
    assert result.payload.clamped_high_hr_points == 21


def test_one_source_device_stream_is_selected_without_averaging_devices():
    base = workout(count=20, cadence=30, duration_minutes=10.0, device_id="short")
    long_points = tuple(
        HeartRatePoint(
            base.started_at + timedelta(seconds=30 * index),
            160,
            source="zepp",
            device_id="long",
        )
        for index in range(21)
    )
    combined = LoadWorkout("zepp", "multi", base.started_at, base.ended_at + timedelta(seconds=30), 10.0, base.heart_rate + long_points)
    result = compute_workout_trimp(combined, profile(), rhr_bpm=50)
    assert result.status.value == "AVAILABLE"
    assert result.payload.selected_device_id == "long"
    assert result.payload.sample_count == 21


def test_profile_rhr_and_coverage_refusals_are_structured():
    assert compute_workout_trimp(workout(count=10), profile(), rhr_bpm=50).refusal_reason.code == "INSUFFICIENT_HEART_RATE_COVERAGE"
    assert compute_workout_trimp(workout(), profile()).refusal_reason.code == "MISSING_SAME_DAY_RHR"
    unsupported = profile(sex=Sex.FEMALE)
    assert compute_workout_trimp(workout(), unsupported, rhr_bpm=50).refusal_reason.code == "UNSUPPORTED_LOAD_PROFILE"


def test_rhr_stream_must_be_selected_before_load_calculation():
    result = compute_training_load(
        [workout()],
        profile(),
        target_date=date(2026, 1, 1),
        queried_days=[date(2026, 1, 1)],
        rhr_by_day={date(2026, 1, 1): [48, 52]},
    )

    assert result.payload.daily_points[0].status == "UNKNOWN"
    assert result.payload.workout_trimp[0].trimp is None


def test_missing_query_coverage_does_not_infer_rest_days():
    result = compute_training_load(
        [workout()],
        profile(),
        target_date=date(2026, 1, 14),
        rhr_by_day={date(2026, 1, 1): 50},
    )

    assert result.status.value == "REFUSED"
    assert result.refusal_reason.code == "INSUFFICIENT_LOAD_HISTORY"


def test_42_rest_days_decay_to_zero_and_keep_daily_points():
    target = date(2026, 2, 11)
    days = [target - timedelta(days=index) for index in range(41, -1, -1)]
    result = compute_training_load(
        [], profile(), target_date=target, queried_days=days, rhr_by_day={},
        upstream_coverage_verified=True,
    )
    payload = result.payload
    assert result.status.value == "AVAILABLE"
    assert len(payload.daily_points) == 42
    assert all(point.status == "REST" and point.trimp == 0 for point in payload.daily_points)
    assert payload.atl == payload.ctl == payload.tsb == 0
    assert result.tier == "high"


def test_unverified_upstream_coverage_is_always_partial_lower_bound():
    target = date(2026, 2, 11)
    days = [target - timedelta(days=index) for index in range(41, -1, -1)]

    result = compute_training_load([], profile(), target_date=target, queried_days=days)

    assert result.status.value == "PARTIAL"
    assert result.tier == "low"
    assert result.payload.lower_bound is True
    assert result.coverage["upstream_coverage_verified"] is False


def test_unscorable_workout_day_is_unknown_not_zero():
    target = date(2026, 2, 11)
    days = [target - timedelta(days=index) for index in range(41, -1, -1)]
    bad = workout(day=target, count=5, duration_minutes=5)
    result = compute_training_load([bad], profile(), target_date=target, queried_days=days, rhr_by_day={target: 50})
    point = result.payload.daily_points[-1]
    assert point.status == "UNKNOWN"
    assert point.trimp is None
    assert target in result.payload.unknown_dates


def test_history_and_coverage_thresholds():
    target = date(2026, 2, 11)
    twelve = [target - timedelta(days=index) for index in range(11, -1, -1)]
    short = compute_training_load([], profile(), target_date=target, queried_days=twelve)
    assert short.refusal_reason.code == "INSUFFICIENT_LOAD_HISTORY"

    days = [target - timedelta(days=index) for index in range(41, -1, -1)]
    unknown_workouts = [workout(day=days[0], workout_id="bad-0", count=5, duration_minutes=5)]
    unknown_workouts.extend(workout(day=days[index], workout_id=f"good-{index}") for index in range(1, 5))
    partial = compute_training_load(unknown_workouts, profile(), target_date=target, queried_days=days, rhr_by_day={day: 50 for day in days})
    assert partial.status.value == "PARTIAL"
    assert partial.tier == "low"
    assert partial.payload.lower_bound is True

    # A complete 42-day query reaches HIGH; the recent-7d coverage gate also holds.
    full = compute_training_load(
        [], profile(), target_date=target, queried_days=days,
        upstream_coverage_verified=True,
    )
    assert full.status.value == "AVAILABLE"
    assert full.tier == "high"
    assert full.payload.coverage_ratio == 1


def test_cross_source_same_workout_id_isolated_in_storage_query():
    user_id = "load-storage-isolation"
    started = datetime(2026, 1, 2, 8, tzinfo=UTC)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        for source, value in (("zepp", 120), ("garmin", 150)):
            repo.save_workout(Workout(user_id=user_id, source=source, workout_id="same", started_at=started, ended_at=started + timedelta(minutes=10), type=WorkoutType.RUNNING, duration=10))
            repo.save_workout_detail(user_id, "same", {"schema_version": "4.0"}, [
                WorkoutMetricSample(source=source, workout_id="same", timestamp=started, metric="heart_rate", value=value, unit="bpm"),
                WorkoutMetricSample(source=source, workout_id="same", timestamp=started, metric="speed", value=3, unit="m/s"),
            ], source=source)
        rows = repo.open_health_load_inputs(user_id, date(2026, 1, 1), date(2026, 1, 3), metric="heart_rate")
    assert {(row.source, row.workout_id, tuple(point.value for point in row.heart_rate)) for row in rows} == {("zepp", "same", (120,)), ("garmin", "same", (150,))}
    assert all(point.unit == "bpm" for row in rows for point in row.heart_rate)
