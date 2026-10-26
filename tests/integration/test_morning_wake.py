"""The morning report follows the wake time inside one bounded window."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import init_db
from vitalis.adapters.persistence.models import (
    AnalysisRun, MetricSample, NotificationDelivery, SleepRecord, SyncAttempt, SyncChunk, User,
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
            timezone="Asia/Shanghai",
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


def add_sleep_read(db, attempt_id: str, at: datetime) -> None:
    """The dispatcher's successful read of the day's band summary for one attempt."""
    start = datetime(DAY.year, DAY.month, DAY.day, tzinfo=ZONE).astimezone(timezone.utc)
    data_until = db.scalar(select(func.max(MetricSample.timestamp)).where(
        MetricSample.user_id == "owner", MetricSample.metric == "heart_rate",
        MetricSample.timestamp >= start.replace(tzinfo=None),
        MetricSample.timestamp <= at.replace(tzinfo=None),
    ))
    db.add(SyncChunk(
        attempt_id=attempt_id, stable_key=f"sleep:{attempt_id}", stream="sleep",
        health_stream="sleep", status="succeeded",
        window_start=start.replace(tzinfo=None),
        window_end=(start + timedelta(days=1)).replace(tzinfo=None),
        started_at=at.astimezone(timezone.utc).replace(tzinfo=None),
        finished_at=at.astimezone(timezone.utc).replace(tzinfo=None),
        fetch_status="success", parse_status="success", write_status="success", records_written=1,
        stages={"sleep_dates": [DAY.isoformat()], "heart_rate_seen_until": {
            DAY.isoformat(): data_until.replace(tzinfo=timezone.utc).isoformat(),
        } if data_until else {}},
    ))


def read_sleep(state, at: datetime) -> None:
    """A sync outside the probes read the day's sleep at ``at``."""
    with state.factory.begin() as db:
        attempt_id = f"read-{at:%H%M}"
        db.add(SyncAttempt(
            id=attempt_id, user_id="owner", trigger="freshness", request_key=attempt_id,
            window_start=datetime(2026, 10, 13, 16), window_end=datetime(2026, 10, 14, 16),
            status="succeeded", created_at=at.astimezone(timezone.utc).replace(tzinfo=None),
            timezone="Asia/Shanghai",
        ))
        db.flush()
        add_sleep_read(db, attempt_id, at)


def run_until(state, end: datetime, *, step=timedelta(minutes=1), on_tick=None, probe_status="succeeded"):
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
                row.status = probe_status
                if probe_status == "succeeded":
                    add_sleep_read(db, row.id, state.now)
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
def test_morning_sync_starts_once_the_stored_wake_is_an_hour_old(wake, wake_at, written_at):
    wake.now = local(7, 25)
    written = local(*written_at)
    settled = local(*wake_at) + timedelta(hours=1)

    def arrive(now):
        if now == written:
            store_sleep(wake, f"{wake_at[0]:02d}:{wake_at[1]:02d}:00", 470)
        if now == settled:
            upload_heart_rate(wake, settled, settled)

    run_until(wake, local(12, 30), on_tick=arrive)

    syncs = morning_syncs(wake)
    assert len(syncs) == 1
    label, days, ref, sleep_only, queued_at = syncs[0]
    assert (days, ref, sleep_only) == (2, f"morning:{DAY.isoformat()}:1", False)
    # One extra probe reads the source as soon as the wake time is an hour old.
    probe_times = [item[4] for item in probes(wake)]
    assert probe_times[-1] == settled
    assert queued_at == settled + timedelta(minutes=1)
    assert probe_times[0] == local(7, 30)
    regular = probe_times[:-1]
    assert all(later - earlier >= timedelta(minutes=15) for earlier, later in zip(regular, regular[1:]))
    assert all(item[1:4] == (1, f"morning-probe:{DAY.isoformat()}:{index}", True)
               for index, item in enumerate(probes(wake), 1))


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


def test_a_normal_night_read_minutes_after_the_wake_waits_an_hour(wake):
    store_sleep(wake, "08:36:00", 440)
    upload_heart_rate(wake, local(1, 30), local(8, 40))
    read_sleep(wake, local(8, 45))
    wake.now = local(8, 46)
    run_until(wake, local(9, 36))
    assert morning_syncs(wake) == []
    upload_heart_rate(wake, local(8, 41), local(9, 36))
    run_until(wake, local(9, 45))
    assert [item[4] for item in morning_syncs(wake)] == [local(9, 37)]


def test_a_cut_short_night_waits_for_the_real_wake(wake):
    """2026-10-18: a 08:27 wake read at 08:35 started the report at 08:40; the
    user slept on and the phone uploaded the 09:35 wake at 09:39."""
    store_sleep(wake, "08:27:00", 364)
    upload_heart_rate(wake, local(2, 10), local(8, 29))
    read_sleep(wake, local(8, 35))
    wake.now = local(8, 36)
    run_until(wake, local(9, 39))
    assert morning_syncs(wake) == []
    store_sleep(wake, "09:35:00", 429)
    upload_heart_rate(wake, local(8, 30), local(9, 39))
    run_until(wake, local(10, 35))
    assert morning_syncs(wake) == []
    upload_heart_rate(wake, local(9, 40), local(10, 35))
    run_until(wake, local(10, 45))
    assert [item[4] for item in morning_syncs(wake)] == [local(10, 36)]


def test_an_extended_sleep_restarts_the_hour(wake):
    store_sleep(wake, "08:27:00", 420)
    read_sleep(wake, local(8, 35))
    wake.now = local(8, 36)
    extended = local(9, 10)

    def arrive(now):
        if now == extended:
            # The user fell asleep again; the next upload moved the wake time.
            store_sleep(wake, "09:05:00", 455)
        if now == local(10, 5):
            upload_heart_rate(wake, now, now)

    run_until(wake, local(10, 15), on_tick=arrive)
    assert [item[4] for item in morning_syncs(wake)] == [local(10, 6)]


def test_a_failed_hour_probe_falls_back_to_the_poll_interval(wake):
    store_sleep(wake, "08:27:00", 420)
    read_sleep(wake, local(8, 35))
    upload_heart_rate(wake, local(9, 27), local(9, 27))
    wake.now = local(9, 27)
    run_until(wake, local(9, 28), probe_status="failed")
    run_until(wake, local(9, 50))
    assert [item[4] for item in probes(wake)] == [local(9, 27), local(9, 42)]
    assert [item[4] for item in morning_syncs(wake)] == [local(9, 43)]


def test_an_upload_during_a_brief_awakening_waits_for_the_rest_of_the_night(wake):
    """2026-10-17: the phone uploaded at 07:09 while the night went on past 10:00."""
    store_sleep(wake, "07:04:00", 275)
    upload_heart_rate(wake, local(2, 26), local(7, 8))
    wake.now = local(7, 30)
    run_until(wake, local(9, 50))
    assert morning_syncs(wake) == []

    store_sleep(wake, "09:38:00", 430)
    upload_heart_rate(wake, local(7, 9), local(9, 44))
    run_until(wake, local(10, 38))
    assert morning_syncs(wake) == []
    upload_heart_rate(wake, local(9, 45), local(10, 38))
    run_until(wake, local(10, 45))
    assert [item[4] for item in morning_syncs(wake)] == [local(10, 39)]


def test_a_short_night_needs_a_fresh_band_read_after_the_upload(wake):
    store_sleep(wake, "06:31:00", 300)
    upload_heart_rate(wake, local(1, 30), local(6, 40))
    wake.now = local(7, 30)
    uploaded = local(7, 52)

    def arrive(now):
        if now == uploaded:
            upload_heart_rate(wake, local(6, 41), local(7, 51))

    run_until(wake, local(8, 30), on_tick=arrive)
    assert [item[4] for item in morning_syncs(wake)] == [local(8, 2)]


def test_without_usual_nights_only_watch_data_after_the_wake_counts(wake):
    forget_usual_nights(wake)
    store_sleep(wake, "08:36:00", 455)
    upload_heart_rate(wake, local(1, 0), local(8, 40))
    wake.now = local(8, 45)
    run_until(wake, local(10, 0))
    # An hour has passed, but nothing shows more than a brief awakening.
    assert morning_syncs(wake) == []
    upload_heart_rate(wake, local(8, 41), local(9, 59))
    run_until(wake, local(10, 10))
    assert [item[4] for item in morning_syncs(wake)] == [local(10, 7)]


def test_a_mid_night_upload_does_not_start_the_morning(wake):
    store_sleep(wake, "01:31:00", 48)
    wake.now = local(7, 30)
    run_until(wake, local(9, 50))
    assert morning_syncs(wake) == []
    store_sleep(wake, "09:47:00", 553)
    run_until(wake, local(10, 47))
    upload_heart_rate(wake, local(10, 47), local(10, 47))
    run_until(wake, local(11, 0))
    assert [item[4] for item in morning_syncs(wake)] == [local(10, 48)]


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


def test_deferred_sleep_keeps_probing_without_a_second_morning_sync(wake):
    store_sleep(wake, "08:00:00", 420)
    upload_heart_rate(wake, local(9), local(9))
    read_sleep(wake, local(9))
    wake.now = local(9, 1)
    assert jobs.morning_wake_job(now=wake.now) == 1
    with wake.factory.begin() as db:
        db.query(SyncAttempt).filter_by(trigger="morning").one().status = "succeeded"
        db.add(AnalysisRun(
            id="extended-run", user_id="owner", target_date=DAY, status="SUCCEEDED",
            started_at=local(9, 5).replace(tzinfo=None),
            intelligence_version=contracts.INTELLIGENCE_VERSION,
            decision_policy_version=contracts.DECISION_POLICY_VERSION,
            evidence_version=contracts.EVIDENCE_VERSION,
        ))
        db.flush()
        db.add(NotificationDelivery(
            id="extended-morning", user_id="owner", analysis_run_id="extended-run",
            period="morning", target_date=DAY, status="deferred", last_error="sleep_incomplete",
        ))
    # The full sync found a later wake. Keep reading until that wake settles.
    store_sleep(wake, "09:10:00", 480)
    wake.now = local(9, 11)
    run_until(wake, local(10, 10))
    assert probes(wake)
    upload_heart_rate(wake, local(9, 1), local(10, 10))
    run_until(wake, local(10, 12))
    assert probes(wake)[-1][4] == local(10, 10)
    assert len(morning_syncs(wake)) == 1


def test_thirty_nine_minutes_of_device_data_cannot_finish_a_short_night(wake):
    store_sleep(wake, "08:00:00", 350)
    upload_heart_rate(wake, local(8), local(8, 39))
    wake.now = local(8, 40)
    run_until(wake, local(9, 10))
    assert morning_syncs(wake) == []
    # A later upload contains the continuation, restarting the waiting hour.
    store_sleep(wake, "09:15:00", 425)
    upload_heart_rate(wake, local(8, 40), local(9, 20))
    run_until(wake, local(10, 15))
    assert morning_syncs(wake) == []
    upload_heart_rate(wake, local(9, 21), local(10, 15))
    run_until(wake, local(10, 17))
    assert [item[4] for item in morning_syncs(wake)] == [local(10, 16)]


@pytest.mark.parametrize(("status", "written", "retried"), [
    ("failed", 0, True),
    ("cancelled", 0, True),
    ("failed", 12, False),
    ("needs_reauth", 0, False),
    ("succeeded", 0, False),
])
def test_morning_sync_is_retried_once_only_when_it_wrote_nothing(wake, status, written, retried):
    store_sleep(wake, "08:36:00", 455)
    upload_heart_rate(wake, local(9, 36), local(9, 36))
    read_sleep(wake, local(9, 36))
    wake.status, wake.written = status, written
    assert jobs.morning_wake_job(now=local(9, 37)) == 1
    wake.status, wake.written = "failed", 0
    assert jobs.morning_wake_job(now=local(9, 38)) == int(retried)
    assert jobs.morning_wake_job(now=local(9, 39)) == 0
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
