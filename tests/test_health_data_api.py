"""API coverage for timestamped metrics, daily metrics and workouts."""

from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.profile import ProfileLoader
from vitalis.models import (
    DailyMetric,
    DenseDataFile,
    MetricSample,
    Workout,
    WorkoutMetricSample,
    WorkoutType,
)
from vitalis.storage import HealthRepository, session_scope
from vitalis.storage import models as storage_models


def test_metric_series_and_daily_metrics(client):
    user_id = "metrics-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_metric_samples([
            MetricSample(user_id=user_id, metric="heart_rate", timestamp=datetime(2026, 8, 25, 8, 5, tzinfo=timezone.utc), value=60, unit="bpm"),
            MetricSample(user_id=user_id, metric="heart_rate", timestamp=datetime(2026, 8, 25, 8, 35, tzinfo=timezone.utc), value=80, unit="bpm"),
        ])
        repo.save_daily_metrics([
            DailyMetric(user_id=user_id, date=date(2026, 8, 25), metric="readiness", value=84, unit="score")
        ])

    series = client.get(
        "/api/v1/health/metrics/heart_rate?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z&resolution=1h",
        headers={"X-User-Id": user_id},
    )
    assert series.status_code == 200
    point = series.json()["points"][0]
    assert point["value"] == 70
    assert point["min"] == 60
    assert point["max"] == 80

    daily = client.get(
        "/api/v1/health/daily-metrics?from=2026-08-25&to=2026-08-25&metric=readiness",
        headers={"X-User-Id": user_id},
    )
    assert daily.status_code == 200
    assert daily.json()["metrics"][0]["value"] == 84


def test_metric_writes_deduplicate_same_batch(client):
    user_id = "duplicate-metrics-user"
    timestamp = datetime(2026, 8, 25, 8, 5, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        written_samples = repo.save_metric_samples([
            MetricSample(user_id=user_id, metric="hrv_rmssd", timestamp=timestamp, value=52, unit="ms"),
            MetricSample(user_id=user_id, metric="hrv_rmssd", timestamp=timestamp, value=59, unit="ms"),
        ])
        written_daily = repo.save_daily_metrics([
            DailyMetric(user_id=user_id, date=date(2026, 8, 25), metric="hrv_readiness", value=70),
            DailyMetric(user_id=user_id, date=date(2026, 8, 25), metric="hrv_readiness", value=75),
        ])

    assert written_samples == 1
    assert written_daily == 1

    series = client.get(
        "/api/v1/health/metrics/hrv_rmssd?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z",
        headers={"X-User-Id": user_id},
    )
    assert series.status_code == 200
    assert [point["value"] for point in series.json()["points"]] == [59]

    daily = client.get(
        "/api/v1/health/daily-metrics?from=2026-08-25&to=2026-08-25&metric=hrv_readiness",
        headers={"X-User-Id": user_id},
    )
    assert daily.status_code == 200
    assert [metric["value"] for metric in daily.json()["metrics"]] == [75]


def test_data_health_exposes_fetch_parse_write_and_sample_time(client):
    user_id = "data-health-user"
    observed = datetime(2026, 8, 25, 8, 5, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=observed,
                value=58,
                unit="ms",
            )
        ])
        repo.save_sync_stream_state(
            user_id,
            "wellness/hrv_rmssd",
            fetch_status="success",
            parse_status="success",
            write_status="success",
            fetched_at=observed,
            parsed_at=observed,
            written_at=observed,
            raw_records=1,
            records_written=1,
        )

    response = client.get(
        "/api/v1/health/data-health",
        headers={"X-User-Id": user_id},
    )

    assert response.status_code == 200
    stream = response.json()["streams"][0]
    assert stream["stream"] == "wellness/hrv_rmssd"
    assert stream["fetch"]["status"] == "success"
    assert stream["parse"]["status"] == "success"
    assert stream["write"]["status"] == "success"
    assert stream["last_sample_at"] == "2026-08-25T08:05:00Z"


def test_metric_writes_preserve_two_devices_at_same_timestamp(client):
    user_id = "multi-device-metrics-user"
    timestamp = datetime(2026, 8, 25, 8, 5, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        written = repo.save_metric_samples([
            MetricSample(
                user_id=user_id, metric="hrv_rmssd", timestamp=timestamp,
                value=52, unit="ms", device_id="A1B2C3D4E5F60708",
            ),
            MetricSample(
                user_id=user_id, metric="hrv_rmssd", timestamp=timestamp,
                value=59, unit="ms", device_id="B1C2D3E4F5061728",
            ),
        ])

    assert written == 2
    series = client.get(
        "/api/v1/health/metrics/hrv_rmssd"
        "?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z&resolution=raw",
        headers={"X-User-Id": user_id},
    )
    assert series.status_code == 200
    assert {point["device_id"] for point in series.json()["points"]} == {
        "A1B2C3D4E5F60708", "B1C2D3E4F5061728",
    }


def test_workout_list_and_detail(client):
    user_id = "workout-user"
    workout = Workout(
        user_id=user_id,
        workout_id="run-1",
        started_at=datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc),
        type=WorkoutType.RUNNING,
        duration=42,
        heart_rate_avg=145,
        vendor_source="run.gps",
    )
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_workout(workout)
        assert repo.save_workout_detail(user_id, "run-1", {"samples": [{"heart_rate": 145}]})

    listing = client.get(
        "/api/v1/health/workouts?from=2026-08-01&to=2026-08-31",
        headers={"X-User-Id": user_id},
    )
    assert listing.status_code == 200
    assert listing.json()["workouts"][0]["detail_available"] is True

    detail = client.get(
        "/api/v1/health/workouts/run-1?source=zepp",
        headers={"X-User-Id": user_id},
    )
    assert detail.status_code == 200
    assert detail.json()["detail"]["samples"][0]["heart_rate"] == 145


def test_dense_file_coverage_withholds_file_ids_and_reports_indexed_state(client):
    user_id = "dense-file-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        written = repo.save_dense_data_files([
            DenseDataFile(
                user_id=user_id,
                stream="second_heart_rate",
                file_id="private-opaque-file-id",
                file_type="SEC_HR",
                date=date(2026, 8, 25),
                start_utc=datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
                end_utc=datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
                source_scope="device",
                device_id="A1B2C3D4E5F60708",
            ),
            DenseDataFile(
                user_id=user_id,
                stream="second_heart_rate",
                file_id="private-opaque-file-id",
                file_type="SEC_HR",
                date=date(2026, 8, 25),
                start_utc=datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc),
                end_utc=datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc),
                source_scope="device",
                device_id="A1B2C3D4E5F60708",
            ),
        ])
        assert written == 2

    response = client.get(
        "/api/v1/health/dense-files/second_heart_rate"
        "?from=2026-08-25&to=2026-08-25",
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["payload_decoded"] is False
    assert len(payload["files"]) == 2
    assert payload["files"][0]["parse_status"] == "indexed"
    assert payload["files"][0]["sample_count"] == 0
    assert "file_id" not in payload["files"][0]
    assert "private-opaque-file-id" not in response.text


def test_normalized_workout_metric_samples_are_isolated_and_returned_in_order(client):
    workout_id = "second-level-run"
    start = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        for user_id in ("sample-user", "other-sample-user"):
            repo.upsert_user(user_id)
            repo.save_workout(Workout(
                user_id=user_id,
                workout_id=workout_id,
                started_at=start,
                duration=1,
                type=WorkoutType.RUNNING,
                vendor_source="opaque-source",
            ))
        assert repo.save_workout_detail(
            "sample-user",
            workout_id,
            {
                "schema_version": "4.0",
                "workout_id": workout_id,
                "metrics_present": ["heart_rate"],
                "metric_sample_counts": {"heart_rate": 2},
                "laps": [],
                "pauses": [],
                "strength_sets": [],
            },
            samples=[
                WorkoutMetricSample(workout_id=workout_id, timestamp=start.replace(second=1), metric="heart_rate", value=121, unit="bpm"),
                WorkoutMetricSample(workout_id=workout_id, timestamp=start, metric="heart_rate", value=120, unit="bpm"),
            ],
        )

    detail = client.get(
        f"/api/v1/health/workouts/{workout_id}?source=zepp",
        headers={"X-User-Id": "sample-user"},
    )
    assert detail.status_code == 200
    payload = detail.json()["detail"]
    assert payload["metric_sample_counts"] == {"heart_rate": 2}
    assert [sample["metric"] for sample in payload["samples"]] == ["heart_rate", "heart_rate"]
    assert [sample["value"] for sample in payload["samples"]] == [120, 121]
    assert {sample["unit"] for sample in payload["samples"]} == {"bpm"}
    assert {sample["source_scope"] for sample in payload["samples"]} == {"workout_detail"}

    other = client.get(
        f"/api/v1/health/workouts/{workout_id}?source=zepp",
        headers={"X-User-Id": "other-sample-user"},
    )
    assert other.status_code == 200
    assert other.json()["detail"] is None


def test_analysis_workout_samples_are_aggregated_to_five_second_bins():
    user_id = "analysis-sample-bins"
    workout_id = "run-bins"
    start = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
    samples = [
        WorkoutMetricSample(
            workout_id=workout_id,
            timestamp=start + timedelta(seconds=second),
            metric=metric,
            value=value,
            unit=unit,
        )
        for second, metric, value, unit in (
            (0, "heart_rate", 100, "bpm"),
            (1, "heart_rate", 110, "bpm"),
            (5, "heart_rate", 120, "bpm"),
            (0, "distance", 0, "m"),
            (1, "distance", 20, "m"),
            (5, "distance", 30, "m"),
        )
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id=workout_id,
            started_at=start,
            type=WorkoutType.RUNNING,
            duration=1,
        ))
        repo.save_workout_detail(
            user_id, workout_id, {"schema_version": "4.0"}, samples
        )
        rows = repo.workout_metric_samples_for_workouts(user_id, [workout_id])

    assert [row.value for row in rows if row.metric == "heart_rate"] == [105, 120]
    assert [row.value for row in rows if row.metric == "distance"] == [20, 30]


def test_metric_and_daily_metric_identities_include_source_scope_and_device(client):
    user_id = "source-scope-identity-user"
    observed = datetime(2026, 8, 25, 8, 5, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        assert repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=observed,
                value=61,
                source_scope="user_fused",
            ),
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=observed,
                value=63,
                source_scope="unknown",
            ),
            MetricSample(
                user_id=user_id,
                source="garmin",
                metric="heart_rate",
                timestamp=observed,
                value=65,
                source_scope="user_fused",
            ),
        ]) == 3
        assert repo.save_daily_metrics([
            DailyMetric(
                user_id=user_id,
                date=observed.date(),
                metric="readiness",
                value=70,
                source_scope="device",
                device_id="DEVICE-A",
            ),
            DailyMetric(
                user_id=user_id,
                date=observed.date(),
                metric="readiness",
                value=80,
                source_scope="device",
                device_id="DEVICE-B",
            ),
            DailyMetric(
                user_id=user_id,
                date=observed.date(),
                metric="readiness",
                value=75,
                source_scope="user_fused",
            ),
            DailyMetric(
                user_id=user_id,
                source="garmin",
                date=observed.date(),
                metric="readiness",
                value=85,
                source_scope="device",
                device_id="DEVICE-A",
            ),
        ]) == 4

    metric_response = client.get(
        "/api/v1/health/metrics/heart_rate"
        "?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z&resolution=raw",
        headers={"X-User-Id": user_id},
    )
    assert metric_response.status_code == 200
    assert {
        (point["source"], point["source_scope"], point["device_id"], point["value"])
        for point in metric_response.json()["points"]
    } == {
        ("zepp", "user_fused", None, 61),
        ("zepp", "unknown", None, 63),
        ("garmin", "user_fused", None, 65),
    }

    hourly_response = client.get(
        "/api/v1/health/metrics/heart_rate"
        "?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z&resolution=1h",
        headers={"X-User-Id": user_id},
    )
    assert len(hourly_response.json()["points"]) == 3

    daily_response = client.get(
        "/api/v1/health/daily-metrics"
        "?from=2026-08-25&to=2026-08-25&metric=readiness",
        headers={"X-User-Id": user_id},
    )
    assert daily_response.status_code == 200
    assert {
        (item["source"], item["source_scope"], item["device_id"], item["value"])
        for item in daily_response.json()["metrics"]
    } == {
        ("zepp", "device", "DEVICE-A", 70),
        ("zepp", "device", "DEVICE-B", 80),
        ("zepp", "user_fused", None, 75),
        ("garmin", "device", "DEVICE-A", 85),
    }


def test_metric_daily_resolution_uses_configured_local_day(client):
    user_id = "metric-local-day-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=datetime(2026, 8, 25, 15, 59, tzinfo=timezone.utc),
                value=60,
                unit="bpm",
                source_scope="device",
                device_id="DEVICE-A",
            ),
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc),
                value=70,
                unit="bpm",
                source_scope="device",
                device_id="DEVICE-A",
            ),
        ])

    response = client.get(
        "/api/v1/health/metrics/heart_rate"
        "?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z&resolution=1d",
        headers={"X-User-Id": user_id},
    )

    assert [(item["timestamp"], item["value"]) for item in response.json()["points"]] == [
        ("2026-08-25", 60),
        ("2026-08-26", 70),
    ]


def test_metric_aggregation_is_not_truncated_at_raw_row_limit(client):
    user_id = "metric-large-aggregate-user"
    start = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        db.execute(storage_models.MetricSample.__table__.insert(), [
            {
                "user_id": user_id,
                "source": "zepp",
                "metric": "heart_rate",
                "timestamp": (start + timedelta(seconds=index)).replace(tzinfo=None),
                "value": 60,
                "unit": "bpm",
                "source_scope": "device",
                "device_id": "DEVICE-A",
            }
            for index in range(50_001)
        ])

    response = client.get(
        "/api/v1/health/metrics/heart_rate"
        "?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z&resolution=1d",
        headers={"X-User-Id": user_id},
    )

    assert response.status_code == 200
    assert sum(item["count"] for item in response.json()["points"]) == 50_001


def test_sync_stream_freshness_is_filtered_by_source():
    user_id = "stream-source-freshness-user"
    zepp_time = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)
    garmin_time = zepp_time + timedelta(days=1)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                source="zepp",
                metric="heart_rate",
                timestamp=zepp_time,
                value=60,
            ),
            MetricSample(
                user_id=user_id,
                source="garmin",
                metric="heart_rate",
                timestamp=garmin_time,
                value=70,
            ),
        ])
        state = repo.save_sync_stream_state(
            user_id,
            "heart_rate",
            source="zepp",
            fetch_status="success",
            parse_status="success",
            write_status="success",
            fetched_at=zepp_time,
            parsed_at=zepp_time,
            written_at=zepp_time,
            raw_records=1,
            records_written=1,
        )

    assert state.last_sample_at == zepp_time.replace(tzinfo=None)


def test_workout_list_uses_configured_local_day_boundaries(client):
    user_id = "workout-local-boundary-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        for workout_id, started_at in (
            ("before", datetime(2026, 8, 24, 15, 59, tzinfo=timezone.utc)),
            ("start", datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)),
            ("end", datetime(2026, 8, 25, 15, 59, tzinfo=timezone.utc)),
            ("after", datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc)),
        ):
            repo.save_workout(Workout(
                user_id=user_id,
                workout_id=workout_id,
                started_at=started_at,
                type=WorkoutType.RUNNING,
                duration=10,
            ))

    response = client.get(
        "/api/v1/health/workouts?from=2026-08-25&to=2026-08-25",
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 200
    assert {item["workout_id"] for item in response.json()["workouts"]} == {
        "start", "end",
    }


def test_workout_detail_samples_are_isolated_by_source(client):
    user_id = "multi-source-workout-user"
    workout_id = "shared-workout-id"
    started_at = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        for source in ("zepp", "garmin"):
            repo.save_workout(Workout(
                user_id=user_id,
                source=source,
                workout_id=workout_id,
                started_at=started_at,
                type=WorkoutType.RUNNING,
                duration=10,
            ))
            assert repo.save_workout_detail(
                user_id,
                workout_id,
                {"schema_version": "4.0", "source": source},
                [WorkoutMetricSample(
                    source=source,
                    workout_id=workout_id,
                    timestamp=started_at,
                    metric="heart_rate",
                    value=100 if source == "zepp" else 120,
                    unit="bpm",
                )],
                source=source,
            )

        zepp = repo.workout_metric_samples(
            user_id, workout_id, source="zepp"
        )
        garmin = repo.workout_metric_samples(
            user_id, workout_id, source="garmin"
        )
        repo.rebuild_training_days(user_id, {date(2026, 8, 25)})
        training = repo.training_range(
            user_id, date(2026, 8, 25), date(2026, 8, 25)
        )
        raw = ProfileLoader(repo).load(user_id, date(2026, 8, 25))

    assert [row.value for row in zepp] == [100]
    assert [row.value for row in garmin] == [120]
    assert training[0]["workout_count"] == 2
    samples = {
        (item["source"], item["samples"][0].value)
        for item in raw.workouts
    }
    assert samples == {("zepp", 100), ("garmin", 120)}

    zepp_response = client.get(
        f"/api/v1/health/workouts/{workout_id}?source=zepp",
        headers={"X-User-Id": user_id},
    )
    garmin_response = client.get(
        f"/api/v1/health/workouts/{workout_id}?source=garmin",
        headers={"X-User-Id": user_id},
    )
    assert zepp_response.json()["detail"]["samples"][0]["value"] == 100
    assert garmin_response.json()["detail"]["samples"][0]["value"] == 120
