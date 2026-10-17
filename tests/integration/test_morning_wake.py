"""The morning report follows the wake time inside one bounded window."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import init_db
from vitalis.adapters.persistence.models import (
    AnalysisRun, MetricSample, NotificationDelivery, SleepRecord, SyncAttempt, User,
)
from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
from vitalis.config import settings
from vitalis.intelligence import contracts
from vitalis.scheduler import jobs

DAY = date(2026, 10, 14)
ZONE = ZoneInfo("Asia/Shanghai")
USUAL_MINUTES = 440


def local(hour: int, minute: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=ZONE).astimezone(timezone.utc)


@pytest.fixture
def wake(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'wake.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id="owner"))
        # Fourteen ordinary nights: a usual night of 440 minutes.
        for offset in range(1, 15):
            night = DAY - timedelta(days=offset)
            db.add(SleepRecord(user_id="owner", date=night, data={
                "date": night.isoformat(), "sleep_duration": USUAL_MINUTES, "wake_time": "08:36:00",
            }))

    @contextmanager
    def isolated_session_scope():
        with factory.begin() as db:
            yield db

    state = SimpleNamespace(now=local(7, 0), queued=[], status="queued", written=0)

    def sync_user(user_id, days, label, *, trigger_ref=None, repository=None, sleep_only=False):
        assert repository is not None
        state.queued.append((label, days, trigger_ref, sleep_only, state.now))
        repository.db.add(SyncAttempt(
            id=f"attempt-{len(state.queued)}", user_id=user_id, trigger=label,
            trigger_ref=trigger_ref, request_key=f"request-{len(state.queued)}",
            window_start=datetime(2026, 10, 13, 16), window_end=datetime(2026, 10, 14, 16),
            status=state.status, records_written=state.written,
            created_at=state.now.replace(tzinfo=None),
        ))
        repository.db.flush()
        return f"attempt-{len(state.queued)}"

    monkeypatch.setattr("vitalis.adapters.persistence.session_scope", isolated_session_scope)
    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    monkeypatch.setattr(settings, "morning_wake_poll_interval_seconds", 900)
    monkeypatch.setattr(settings, "morning_wake_max_probes", 22)
    monkeypatch.setattr(jobs, "_get_authorized_users", lambda: {"owner"})
    monkeypatch.setattr(jobs, "_sync_user", sync_user)
    state.factory = factory
    yield state
    engine.dispose()


def run_until(state, end: datetime, *, step=timedelta(minutes=1), on_tick=None):
    """Advance the worker clock minute by minute, finishing probes as a dispatcher would."""
    while state.now < end:
        if on_tick is not None:
            on_tick(state.now)
        jobs.morning_wake_job(now=state.now)
        with state.factory.begin() as db:
            for row in db.query(SyncAttempt).filter(
                SyncAttempt.trigger == jobs.MORNING_PROBE_TRIGGER,
                SyncAttempt.status == "queued",
            ):
                row.status = "succeeded"
        state.now += step


def store_sleep(state, wake_time: str, minutes: int) -> None:
    with state.factory.begin() as db:
        row = db.query(SleepRecord).filter_by(user_id="owner", date=DAY).one_or_none()
        data = {"date": DAY.isoformat(), "sleep_duration": minutes, "wake_time": wake_time}
        if row is None:
            db.add(SleepRecord(user_id="owner", date=DAY, data=data))
        else:
            row.data = data


def forget_usual_nights(state) -> None:
    with state.factory.begin() as db:
        db.query(SleepRecord).filter(SleepRecord.user_id == "owner", SleepRecord.date < DAY).delete()


def upload_heart_rate(state, start: datetime, end: datetime) -> None:
    """Minute heart rate the phone uploads with the band data, up to ``end``."""
    with state.factory.begin() as db:
        moment = start
        while moment <= end:
            db.add(MetricSample(
                user_id="owner", metric="heart_rate", value=52.0, unit="bpm",
                timestamp=moment.astimezone(timezone.utc).replace(tzinfo=None),
            ))
            moment += timedelta(minutes=1)


def probes(state):
    return [item for item in state.queued if item[0] == jobs.MORNING_PROBE_TRIGGER]


def morning_syncs(state):
    return [item for item in state.queued if item[0] == "morning"]


@pytest.mark.parametrize(("wake_at", "written_at"), [((8, 40), (8, 50)), ((9, 57), (10, 6))])
def test_morning_sync_follows_the_stored_wake_within_one_poll(wake, wake_at, written_at):
    wake.now = local(7, 25)
    written = local(*written_at)

    def arrive(now):
        if now == written:
            store_sleep(wake, f"{wake_at[0]:02d}:{wake_at[1]:02d}:00", 470)

    run_until(wake, local(11, 30), on_tick=arrive)

    syncs = morning_syncs(wake)
    assert len(syncs) == 1
    label, days, ref, sleep_only, queued_at = syncs[0]
    assert (days, ref, sleep_only) == (2, f"morning:{DAY.isoformat()}:1", False)
    assert written <= queued_at <= written + timedelta(seconds=settings.morning_wake_poll_interval_seconds)
    probe_times = [item[4] for item in probes(wake)]
    assert probe_times[0] == local(7, 30)
    assert all(later - earlier >= timedelta(minutes=15) for earlier, later in zip(probe_times, probe_times[1:]))
    assert all(item[1:4] == (1, f"morning-probe:{DAY.isoformat()}:{index}", True)
               for index, item in enumerate(probes(wake), 1))
    assert max(probe_times) < queued_at


def test_no_sleep_probes_up_to_the_cap_then_falls_back_at_the_window_end(wake, monkeypatch):
    monkeypatch.setattr(settings, "morning_wake_max_probes", 6)
    wake.now = local(7, 30)
    run_until(wake, local(14, 45))
    assert len(probes(wake)) == 6
    assert [item[4] for item in morning_syncs(wake)] == [local(13, 0)]


def test_the_default_probes_cover_the_window_until_one_pm(wake):
    wake.now = local(7, 30)
    run_until(wake, local(13, 30))
    probe_times = [item[4] for item in probes(wake)]
    assert (len(probe_times), probe_times[-1]) == (22, local(12, 45))
    assert [item[4] for item in morning_syncs(wake)] == [local(13, 0)]


def test_an_upload_during_a_brief_awakening_waits_for_the_rest_of_the_night(wake):
    """2026-10-17: the phone uploaded at 07:09 while the night went on past 09:30."""
    store_sleep(wake, "07:04:00", 275)
    upload_heart_rate(wake, local(2, 26), local(7, 8))
    wake.now = local(7, 30)
    run_until(wake, local(9, 50))
    assert morning_syncs(wake) == []
    assert len(probes(wake)) == 10

    store_sleep(wake, "09:38:00", 430)
    upload_heart_rate(wake, local(7, 9), local(9, 44))
    run_until(wake, local(10, 10))
    assert [item[4] for item in morning_syncs(wake)] == [local(9, 50)]


def test_a_short_night_starts_once_the_watch_shows_half_an_hour_awake(wake):
    store_sleep(wake, "06:31:00", 300)
    upload_heart_rate(wake, local(1, 30), local(6, 40))
    wake.now = local(7, 30)
    uploaded = local(7, 52)

    def arrive(now):
        if now == uploaded:
            upload_heart_rate(wake, local(6, 41), local(7, 51))

    run_until(wake, local(8, 30), on_tick=arrive)
    assert [item[4] for item in morning_syncs(wake)] == [uploaded]


def test_without_usual_nights_only_watch_data_after_the_wake_counts(wake):
    forget_usual_nights(wake)
    store_sleep(wake, "08:36:00", 455)
    upload_heart_rate(wake, local(1, 0), local(8, 40))
    wake.now = local(8, 45)
    run_until(wake, local(9, 0))
    assert morning_syncs(wake) == []
    upload_heart_rate(wake, local(8, 41), local(9, 10))
    run_until(wake, local(9, 10))
    # Minutes stored ahead of the worker clock do not count: half an hour after
    # the 08:36 wake is 09:06.
    assert [item[4] for item in morning_syncs(wake)] == [local(9, 6)]


def test_a_mid_night_upload_does_not_start_the_morning(wake):
    store_sleep(wake, "01:31:00", 48)
    wake.now = local(7, 30)
    run_until(wake, local(9, 50))
    assert morning_syncs(wake) == []
    store_sleep(wake, "09:57:00", 553)
    run_until(wake, local(10, 20))
    assert [item[4] for item in morning_syncs(wake)] == [local(9, 50)]


def test_nothing_runs_outside_the_window_or_after_a_delivery_exists(wake):
    store_sleep(wake, "08:36:00", 455)
    assert jobs.morning_wake_job(now=local(7, 29)) == 0
    assert jobs.morning_wake_job(now=local(14, 30)) == 0
    with wake.factory.begin() as db:
        db.add(AnalysisRun(
            id="manual-run", user_id="owner", target_date=DAY, status="SUCCEEDED",
            started_at=datetime(2026, 10, 14, 0), completed_at=datetime(2026, 10, 14, 0),
            intelligence_version=contracts.INTELLIGENCE_VERSION,
            decision_policy_version=contracts.DECISION_POLICY_VERSION,
            evidence_version=contracts.EVIDENCE_VERSION,
            config_digest=_current_analysis_config_digest(),
        ))
        db.flush()
        db.add(NotificationDelivery(
            id="manual-morning", user_id="owner", analysis_run_id="manual-run",
            period="morning", target_date=DAY, status="accepted",
        ))
    assert jobs.morning_wake_job(now=local(9, 0)) == 0
    assert wake.queued == []


def test_one_probe_at_a_time(wake):
    wake.now = local(7, 30)
    assert jobs.morning_wake_job(now=wake.now) == 1
    assert jobs.morning_wake_job(now=local(8, 0)) == 0
    assert len(probes(wake)) == 1


@pytest.mark.parametrize(("status", "written", "retried"), [
    ("failed", 0, True),
    ("cancelled", 0, True),
    ("failed", 12, False),
    ("needs_reauth", 0, False),
    ("succeeded", 0, False),
])
def test_morning_sync_is_retried_once_only_when_it_wrote_nothing(wake, status, written, retried):
    store_sleep(wake, "08:36:00", 455)
    wake.status, wake.written = status, written
    assert jobs.morning_wake_job(now=local(8, 50)) == 1
    wake.status, wake.written = "failed", 0
    assert jobs.morning_wake_job(now=local(8, 51)) == int(retried)
    assert jobs.morning_wake_job(now=local(8, 52)) == 0
    assert len(morning_syncs(wake)) == 1 + int(retried)
    if retried:
        assert morning_syncs(wake)[1][2] == f"morning:{DAY.isoformat()}:2"


def test_sleep_only_probe_requests_only_the_band_summary():
    from vitalis.adapters.zepp.fetcher import FetchWindow
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

    coordinator = ZeppSyncCoordinator()
    window = FetchWindow.local_dates(DAY, DAY, "Asia/Shanghai")
    manifest = coordinator._manifest(window, {"sleep_only": True}, "Asia/Shanghai")
    assert [(item["stream"], item["stages"]["operation"]) for item in manifest] == [
        ("sleep", "fetch_band_data"),
    ]
    assert manifest[0]["stages"]["params"]["from_date"] == DAY.isoformat()
    full = coordinator._manifest(window, {}, "Asia/Shanghai")
    sleep_spec = next(item for item in full if item["stream"] == "sleep")
    assert sleep_spec["stable_key"] == manifest[0]["stable_key"]


@pytest.mark.parametrize(("trigger", "options"), [
    ("morning", {"sleep_only": True}),
    ("morning_probe", {}),
    ("morning_probe", {"sleep_only": True, "decode_dense_files": True}),
])
def test_sleep_only_is_reserved_for_the_wake_probe(trigger, options):
    from vitalis.adapters.zepp import ZeppAuthError
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

    with pytest.raises(ZeppAuthError):
        ZeppSyncCoordinator().create_attempt("owner", days=1, trigger=trigger, options=options)
