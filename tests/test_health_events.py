from datetime import date, timedelta

from vitalis.intelligence.analyzers import HrvAnalyzer, RecoveryAnalyzer, SleepAnalyzer, TrainingAnalyzer
from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import EventLifecycle, RecoveryState
from vitalis.intelligence.events import HealthEventEngine
from vitalis.intelligence.lifecycle import EventLifecycleEngine
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.trend import TrendEngine
from vitalis.storage import HealthRepository, session_scope


TARGET = date(2026, 8, 28)


def _series(metric, values, *, device=None, unit="ms"):
    return [
        SeriesPoint(
            metric=metric,
            value=value,
            unit=unit,
            day=TARGET - timedelta(days=offset),
            observed_at=TARGET - timedelta(days=offset),
            source="zepp",
            source_scope="device" if device else "normalized_daily_record",
            device_id=device,
        )
        for offset, value in enumerate(values)
    ]


def _detect(raw):
    baselines = BaselineEngine().build(raw.series, raw.day)
    sleep, sleep_state = SleepAnalyzer().analyze(raw, baselines)
    hrv = HrvAnalyzer().analyze(raw, baselines)
    training = TrainingAnalyzer().analyze(raw, baselines)
    recovery = RecoveryAnalyzer().analyze(raw, sleep, sleep_state, hrv, training)
    trends = TrendEngine().calculate(raw)
    return HealthEventEngine().detect(raw, baselines, trends, hrv, recovery)


def test_event_engine_requires_persistent_hrv_deviation():
    raw = RawDailyProfile(user_id="event-persistence", day=TARGET)
    raw.series = {
        "hrv_rmssd": _series(
            "hrv_rmssd",
            [38, 39, 40] + [50 + offset % 2 for offset in range(3, 24)],
            device="helio",
        ),
        "sleep_duration": _series(
            "sleep_duration", [450] * 24, unit="min"
        ),
        "resting_hr": _series("resting_hr", [56] * 24, unit="bpm"),
        "training_load": _series("training_load", [20] * 24, unit="load"),
    }
    raw.sleep_by_day = {
        point.day: {"date": point.day, "sleep_duration": point.value}
        for point in raw.series["sleep_duration"]
    }
    raw.training_by_day = {
        point.day: {"date": point.day, "total_load": point.value, "total_duration": 20, "workout_count": 1}
        for point in raw.series["training_load"]
    }

    events = _detect(raw)
    hrv_event = next(item for item in events if item.type == "HRV_DROP")

    assert hrv_event.duration_days == 3
    assert hrv_event.deviation_percent < -15
    assert hrv_event.confidence_label in {"中等", "较高"}
    assert "连续 3 天" in hrv_event.summary


def test_event_engine_does_not_emit_for_one_day_deviation():
    raw = RawDailyProfile(user_id="event-single", day=TARGET)
    raw.series = {
        "hrv_rmssd": _series(
            "hrv_rmssd", [38] + [51 + offset % 2 for offset in range(1, 24)], device="helio"
        ),
        "sleep_duration": _series("sleep_duration", [450] * 24, unit="min"),
    }
    raw.sleep_by_day = {
        point.day: {"date": point.day, "sleep_duration": point.value}
        for point in raw.series["sleep_duration"]
    }

    assert all(item.type != "HRV_DROP" for item in _detect(raw))


def test_health_event_persistence_and_acknowledgement_are_user_scoped():
    raw = RawDailyProfile(user_id="event-storage", day=TARGET)
    raw.series = {
        "sleep_duration": _series("sleep_duration", [360, 370, 380] + [450] * 21, unit="min"),
    }
    raw.sleep_by_day = {
        point.day: {"date": point.day, "sleep_duration": point.value}
        for point in raw.series["sleep_duration"]
    }
    events = _detect(raw)
    event = next(item for item in events if item.type == "SLEEP_DEFICIT")

    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("event-storage")
        repo.upsert_user("event-storage")
        repo.save_health_event("event-storage", event)
        assert repo.acknowledge_health_event("another-user", event.id) is None
        acknowledged = repo.acknowledge_health_event("event-storage", event.id)
        assert acknowledged is not None and acknowledged.acknowledged is True
        stored = repo.health_events("event-storage", TARGET - timedelta(days=7), TARGET)

    assert stored[0].id == event.id
    assert stored[0].acknowledged is True


def test_event_lifecycle_and_acknowledgement_progress_independently():
    user_id = "event-lifecycle"
    from vitalis.intelligence.contracts import ConfidenceBand, EventSeverity, HealthEvent

    candidate = HealthEvent(
        id="lifecycle-event",
        type="SLEEP_DEFICIT",
        type_label="持续睡眠不足",
        severity=EventSeverity.MODERATE,
        severity_label="中等",
        metric="sleep_duration",
        metric_label="睡眠时长",
        start_date=TARGET - timedelta(days=2),
        end_date=TARGET,
        duration_days=3,
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
        summary="睡眠时长连续偏低。",
    )
    engine = EventLifecycleEngine()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        detected = engine.reconcile(repo, "run-1", user_id, TARGET, [candidate])[0]
        acknowledged = repo.acknowledge_health_event(user_id, detected.id)
        persisting = engine.reconcile(
            repo,
            "run-2",
            user_id,
            TARGET + timedelta(days=1),
            [candidate.model_copy(update={"end_date": TARGET + timedelta(days=1)})],
        )[0]
        improving = engine.reconcile(
            repo, "run-3", user_id, TARGET + timedelta(days=2), []
        )[0]
        resolved = engine.reconcile(
            repo, "run-4", user_id, TARGET + timedelta(days=3), []
        )[0]
        observations = repo.event_observations(user_id, candidate.id)
        resolved_query = repo.health_events(
            user_id, TARGET + timedelta(days=3), TARGET + timedelta(days=3)
        )

    assert detected.lifecycle == EventLifecycle.DETECTED
    assert acknowledged.acknowledged is True
    assert persisting.lifecycle == EventLifecycle.PERSISTING
    assert persisting.acknowledged is True
    assert improving.lifecycle == EventLifecycle.IMPROVING
    assert improving.acknowledged is True
    assert resolved.lifecycle == EventLifecycle.RESOLVED
    assert resolved.resolved_at == TARGET + timedelta(days=3)
    assert resolved.acknowledged is True
    assert [item.id for item in resolved_query] == [candidate.id]
    assert [item.lifecycle for item in observations] == [
        EventLifecycle.DETECTED,
        EventLifecycle.PERSISTING,
        EventLifecycle.IMPROVING,
        EventLifecycle.RESOLVED,
    ]
