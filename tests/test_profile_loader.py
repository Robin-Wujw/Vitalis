from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.contracts import QualityStatus
from vitalis.intelligence.profile import ProfileLoader
from vitalis.models import (
    DenseDataFile,
    Device,
    MetricSample,
    NormalizedDaily,
    SleepRecord,
    User,
)
from vitalis.storage import HealthRepository, session_scope


def test_profile_loader_reports_missing_signals_without_fabricating_facts():
    user_id = "intelligence-missing"
    day = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        raw = ProfileLoader(repo).load(user_id, day)

    assert raw.data_quality.status == QualityStatus.INSUFFICIENT
    assert raw.data_quality.missing_required_signals == ["sleep_duration", "hrv"]
    assert raw.facts == {}


def test_profile_loader_keeps_device_streams_and_local_identities_separate():
    day = date(2026, 8, 28)
    user_id = "intelligence-primary"
    sibling_id = "intelligence-sibling"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.delete_for_user(sibling_id)
        repo.upsert_user(user_id, source_user_id="vendor-shared")
        repo.upsert_user(sibling_id, source_user_id="vendor-shared")
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
    assert any(flag.code == "SOURCE_IDENTITY_SHARED" for flag in raw.data_quality.flags)


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
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id,
                metric="heart_rate",
                timestamp=datetime(2026, 8, 27, 18, minute, tzinfo=timezone.utc),
                value=55 + minute % 2,
                unit="bpm",
                source_scope="device",
                device_id="helio",
            )
            for minute in range(20)
        ])
        raw = ProfileLoader(repo).load(user_id, day)

    assert len(raw.heart_rate_samples) == 20
    assert {item.device_id for item in raw.heart_rate_samples} == {"helio"}
    assert "heart_rate" not in raw.series
    assert "heart_rate" not in raw.facts
