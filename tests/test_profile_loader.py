from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.activity import ActivityAnalyzer
from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import Availability, QualityStatus
from vitalis.intelligence.profile import ProfileLoader
from vitalis.domain import (
    ActivityRecord,
    DailyMetric,
    DenseDataFile,
    Device,
    MetricSample,
    NormalizedDaily,
    SleepRecord,
    User,
)
from vitalis.adapters.persistence import HealthRepository, session_scope


def test_profile_loader_reports_missing_signals_without_fabricating_facts():
    user_id = "intelligence-missing"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        raw = ProfileLoader(repo).load(user_id, day)

    assert raw.data_quality.status == QualityStatus.INSUFFICIENT
    assert raw.data_quality.missing_required_signals == ["sleep_duration"]
    assert raw.facts == {}


def test_training_coverage_reaches_previous_calendar_month(monkeypatch):
    user_id = "calendar-coverage-start"
    day = date(2026, 10, 8)
    captured = {}
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

        def fake_coverage(user, start, end, as_of, *, timezone_name=None):
            captured.update({"user": user, "start": start, "end": end})
            return {"status": "UNKNOWN", "verified_days": []}

        monkeypatch.setattr(repo, "training_history_coverage", fake_coverage)
        ProfileLoader(repo).load(user_id, day)

    assert captured == {
        "user": user_id,
        "start": date(2026, 8, 1),
        "end": day,
    }


def test_open_health_load_uses_only_persisted_verified_days(monkeypatch):
    user_id = "open-health-coverage-wiring"
    day = date(2026, 10, 8)
    verified = [
        (day - timedelta(days=offset)).isoformat()
        for offset in range(42)
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        monkeypatch.setattr(
            repo,
            "training_history_coverage",
            lambda *_args, **_kwargs: {
                "status": "PARTIAL",
                "verified_days": verified,
                "truncated": False,
                "budget_exhausted": False,
            },
        )
        raw = ProfileLoader(repo).load(user_id, day)

    assert raw.open_health_load_queried_days == sorted(
        date.fromisoformat(value) for value in verified
    )
    assert raw.open_health_load_upstream_coverage_verified is True


def test_open_health_coverage_flag_stays_false_when_ledger_is_truncated(monkeypatch):
    user_id = "open-health-coverage-truncated"
    day = date(2026, 10, 8)
    verified = [
        (day - timedelta(days=offset)).isoformat()
        for offset in range(42)
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        monkeypatch.setattr(
            repo,
            "training_history_coverage",
            lambda *_args, **_kwargs: {
                "status": "COMPLETE",
                "verified_days": verified,
                "truncated": True,
                "budget_exhausted": False,
            },
        )
        raw = ProfileLoader(repo).load(user_id, day)

    assert len(raw.open_health_load_queried_days) == 42
    assert raw.open_health_load_upstream_coverage_verified is False


def test_training_coverage_accepts_date_values_and_derives_prior_week_verification(monkeypatch):
    user_id = "training-coverage-date-values"
    day = date(2026, 10, 8)
    verified = {day - timedelta(days=offset) for offset in range(42)}
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        monkeypatch.setattr(
            repo,
            "training_history_coverage",
            lambda *_args, **_kwargs: {
                "status": "COMPLETE",
                "verified_days": sorted(verified),
                "truncated": False,
                "budget_exhausted": False,
            },
        )
        raw = ProfileLoader(repo).load(user_id, day)

    assert raw.training_history_coverage["prior_7d_verified"] is True
    assert raw.open_health_load_upstream_coverage_verified is True


def test_missing_optional_hrv_does_not_block_activity_analysis():
    user_id = "activity-without-hrv"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            sleep=SleepRecord(user_id=user_id, date=day, sleep_duration=450),
            activity=ActivityRecord(
                user_id=user_id,
                date=day,
                steps=8000,
                observed_fields=["steps"],
            ),
        ))
        raw = ProfileLoader(repo).load(user_id, day)

    assert raw.data_quality.status == QualityStatus.SUFFICIENT
    assert "hrv" not in raw.data_quality.missing_required_signals
    activity = ActivityAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )
    assert activity.status == Availability.AVAILABLE
    assert activity.steps is not None
    assert activity.steps.value == 8000


def test_profile_loader_keeps_device_streams_and_local_identities_separate():
    day = date(2026, 8, 28)
    user_id = "intelligence-primary"
    sibling_id = "intelligence-sibling"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.delete_for_user(sibling_id)
        repo.upsert_user(user_id)
        repo.upsert_user(sibling_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            sleep=SleepRecord(user_id=user_id, date=day, sleep_duration=450),
        ))
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
                value=52,
                unit="ms",
                source_scope="device",
                device_id="helio",
            ),
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=1),
                value=71,
                unit="ms",
                source_scope="device",
                device_id="balance",
            ),
            MetricSample(
                user_id=sibling_id,
                metric="hrv_rmssd",
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
                value=99,
                unit="ms",
                source_scope="device",
                device_id="helio",
            ),
        ])
        raw = ProfileLoader(repo).load(user_id, day)

    assert raw.data_quality.status == QualityStatus.SUFFICIENT
    assert {point.device_id for point in raw.series["hrv_rmssd"]} == {"helio", "balance"}
    assert {point.value for point in raw.series["hrv_rmssd"]} == {52, 71}
    assert not any(
        flag.code == "SOURCE_IDENTITY_SHARED" for flag in raw.data_quality.flags
    )


def test_open_health_groups_pre_midnight_rmssd_into_wake_date_sleep_window():
    user_id = "open-health-cross-midnight"
    day = date(2026, 9, 2)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            sleep=SleepRecord(
                user_id=user_id,
                date=day,
                sleep_duration=450,
                bedtime=datetime.strptime("23:00", "%H:%M").time(),
                wake_time=datetime.strptime("07:00", "%H:%M").time(),
            ),
        ))
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=timestamp,
                value=value,
                unit="ms",
                source_scope="device",
                device_id="strap",
            )
            for timestamp, value in (
                (datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc), 90),
                (datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc), 100),
                (datetime(2026, 9, 1, 17, 30, tzinfo=timezone.utc), 110),
            )
        ])
        raw = ProfileLoader(repo).load(user_id, day)

    target = next(item for item in raw.open_health_observations if item.date == day)
    assert target.rmssd_ms == 100
    assert target.sample_count == 3
    assert target.model_extra["span_minutes"] == 120


def test_open_health_prefers_zepp_fused_sleep_hrv_over_device_samples():
    user_id = "open-health-vendor-fused"
    day = date(2026, 9, 2)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            sleep=SleepRecord(
                user_id=user_id,
                date=day,
                sleep_duration=450,
                bedtime=datetime.strptime("23:00", "%H:%M").time(),
                wake_time=datetime.strptime("07:00", "%H:%M").time(),
            ),
        ))
        repo.save_daily_metrics([DailyMetric(
            user_id=user_id,
            date=day,
            metric="sleep_hrv",
            value=65,
            unit="ms",
            source_scope="user_fused",
        )])
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=timestamp,
                value=value,
                unit="ms",
                source_scope="device",
                device_id="balance",
            )
            for timestamp, value in (
                (datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc), 90),
                (datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc), 100),
                (datetime(2026, 9, 1, 17, 30, tzinfo=timezone.utc), 110),
            )
        ])
        raw = ProfileLoader(repo).load(user_id, day)

    target = next(item for item in raw.open_health_observations if item.date == day)
    assert target.rmssd_ms == 65
    assert target.source_scope == "user_fused"
    assert target.device_id is None
    assert target.sample_count is None


def test_profile_loader_groups_utc_samples_by_shanghai_natural_day():
    user_id = "intelligence-local-day"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            sleep=SleepRecord(user_id=user_id, date=day, sleep_duration=450),
        ))
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc),
                value=50,
                unit="ms",
                source_scope="device",
                device_id="helio",
            ),
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime(2026, 8, 28, 0, 30, tzinfo=timezone.utc),
                value=70,
                unit="ms",
                source_scope="device",
                device_id="helio",
            ),
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
                value=200,
                unit="ms",
                source_scope="device",
                device_id="helio",
            ),
        ])
        raw = ProfileLoader(repo).load(user_id, day)

    target_values = [point.value for point in raw.series["hrv_rmssd"] if point.day == day]
    assert target_values == [50, 70]
    assert raw.facts["hrv_rmssd"][0].value == 60


def test_profile_loader_uses_explicit_timezone_for_windows_and_context():
    user_id = "intelligence-explicit-timezone"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            sleep=SleepRecord(user_id=user_id, date=day, sleep_duration=450),
        ))
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime(2026, 8, 28, 3, 30, tzinfo=timezone.utc),
                value=50,
                unit="ms",
                source_scope="device",
                device_id="watch",
            ),
            MetricSample(
                user_id=user_id,
                metric="hrv_rmssd",
                timestamp=datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc),
                value=70,
                unit="ms",
                source_scope="device",
                device_id="watch",
            ),
        ])
        raw = ProfileLoader(repo).load(
            user_id,
            day,
            as_of=datetime(2026, 8, 29, tzinfo=timezone.utc),
            timezone_name="America/New_York",
        )

    target_values = [
        point.value for point in raw.series["hrv_rmssd"] if point.day == day
    ]
    assert target_values == [70]
    assert raw.report_context["timezone"] == "America/New_York"


def test_profile_loader_attaches_device_identity_and_dense_hr_coverage():
    user_id = "intelligence-device-context"
    day = date(2026, 8, 28)
    start = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.upsert_device(Device(
            user_id=user_id,
            source="zepp",
            model="Amazfit Helio Strap",
            device_id="CE4A84921FA6",
        ))
        repo.save_dense_data_files([DenseDataFile(
            user_id=user_id,
            source="zepp",
            stream="second_heart_rate",
            file_id="private-file-id",
            file_type="SEC_HR",
            date=day,
            start_utc=start,
            end_utc=start + timedelta(hours=8),
            source_scope="device",
            device_id="CE4A84FFFF921FA6",
            parse_status="indexed",
        )])
        raw = ProfileLoader(repo).load(user_id, day)

    assert raw.device_models == {
        "CE4A84921FA6": "Amazfit Helio Strap",
        "CE4A84FFFF921FA6": "Amazfit Helio Strap",
    }
    coverage = raw.dense_heart_rate_coverage["CE4A84FFFF921FA6"]
    assert coverage["today_coverage_seconds"] == 8 * 60 * 60
    validity = raw.data_quality.device_validity[0]
    assert validity.device_label == "Amazfit Helio Strap"
    assert validity.measurement_site == "upper_arm"
    assert validity.status == "LIMITED_BY_EVIDENCE"
    assert "private-file-id" not in repr(raw.data_quality)


def test_profile_loader_keeps_timestamped_heart_rate_out_of_daily_series():
    user_id = "intelligence-nocturnal-heart-rate"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            sleep=SleepRecord(
                user_id=user_id,
                date=day,
                sleep_duration=120,
                bedtime=datetime.strptime("02:00", "%H:%M").time(),
                wake_time=datetime.strptime("04:00", "%H:%M").time(),
            ),
        ))
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=datetime(2026, 8, 27, 18, minute, second, tzinfo=timezone.utc),
                value=value,
                unit="bpm",
                source_scope="device",
                device_id=device_id,
            )
            for minute, second, value, device_id in (
                (0, 1, 50, "helio"),
                (0, 20, 60, "helio"),
                (0, 40, 70, "helio"),
                (1, 1, 80, "helio"),
                (1, 20, 90, "helio"),
                (1, 30, 45, None),
                (1, 40, 55, None),
            )
        ] + [
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=datetime(2026, 8, 27, 17, 59, tzinfo=timezone.utc),
                value=200,
                unit="bpm",
                source_scope="device",
                device_id="helio",
            )
        ])
        raw = ProfileLoader(repo).load(user_id, day)

    assert len(raw.heart_rate_samples) == 3
    assert [item.value for item in raw.heart_rate_samples] == [60, 50, 85]
    assert {item.device_id for item in raw.heart_rate_samples} == {"helio", None}
    assert "heart_rate" not in raw.series
    assert "heart_rate" not in raw.facts


def test_profile_loader_caches_target_day_window_summaries_by_one_source_stream():
    user_id = "intelligence-window-summary"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc),
                value=60,
                unit="bpm",
                source_scope="device",
                device_id="watch-a",
            ),
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=datetime(2026, 8, 28, 1, 1, tzinfo=timezone.utc),
                value=62,
                unit="bpm",
                source_scope="device",
                device_id="watch-a",
            ),
            MetricSample(
                user_id=user_id,
                metric="stress",
                timestamp=datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc),
                value=31,
                unit="score",
                source_scope="user_fused",
                device_id=None,
            ),
        ])
        raw = ProfileLoader(repo).load(user_id, day)

    heart_rate = raw.sample_window_summaries["heart_rate"]
    stress = raw.sample_window_summaries["stress"]
    assert heart_rate.provenance.source_scope == "device"
    assert heart_rate.provenance.device_id == "watch-a"
    assert heart_rate.sample_count == 2
    assert heart_rate.observed_minutes == 2
    assert heart_rate.minimum == 60
    assert heart_rate.maximum == 62
    assert heart_rate.average == 61
    assert stress.provenance.source_scope == "user_fused"
    assert stress.sample_count == 1
    assert stress.observed_minutes == 1


def test_profile_loader_ignores_legacy_activity_default_zero_but_keeps_positive_value():
    user_id = "intelligence-activity-missing"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily(NormalizedDaily(
            user_id=user_id,
            date=day,
            activity={
                "user_id": user_id,
                "date": day,
                "steps": 0,
                "active_minutes": 25,
                "calories": 0,
                "distance_km": 0,
                "resting_hr": 0,
            },
        ))
        raw = ProfileLoader(repo).load(user_id, day)

    assert "steps" not in raw.series
    assert [point.value for point in raw.series["active_minutes"]] == [25]
    assert "calories" not in raw.series
    assert "distance_km" not in raw.series
    assert "resting_hr" not in raw.series
