from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.contracts import QualityStatus
from vitalis.intelligence.profile import ProfileLoader
from vitalis.models import DailyHealth, MetricSample, SleepRecord, User
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
        repo.save_daily(DailyHealth(
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
        repo.save_daily(DailyHealth(
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
