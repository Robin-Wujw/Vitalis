"""Synthetic regressions for independent Zepp streams and daily report recovery."""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import HealthRepository, init_db, models as orm
from vitalis.adapters.zepp import client as client_module
from vitalis.adapters.zepp.client import MockZeppClient, ZeppAPIClient, ZeppAuthError
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator


DAY = date(2026, 8, 29)
NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
WINDOW = FetchWindow.local_dates(DAY, DAY)


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'sync.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    init_db(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    engine.dispose()


def _drain(coordinator, attempt_id):
    for _ in range(100):
        report = coordinator.drain_once(attempt_id)
        if report.progress["status"] in {
            "succeeded", "partial", "failed", "needs_reauth", "cancelled", "retry_wait",
        }:
            return report
    pytest.fail("bounded synthetic sync did not converge")


@pytest.mark.parametrize("kind", ["not_available", "vendor_response"])
@pytest.mark.parametrize("trigger", ["manual", "morning", "evening"])
def test_failed_first_stream_still_fetches_daily_facts(factory, kind, trigger):
    class Connector(MockZeppClient):
        def fetch_heart_rate(self, *args):
            raise ZeppAuthError("synthetic endpoint failure", kind=kind)

    coordinator = ZeppSyncCoordinator(
        connector=Connector(), session_factory=factory, wall_clock=lambda: NOW,
    )
    attempt = coordinator.create_attempt("owner", window=WINDOW, trigger=trigger)
    first = coordinator.drain_once(attempt.id)
    assert first.progress["status"] == "queued"
    assert first.progress["failed_chunks"] == 1

    report = _drain(coordinator, attempt.id)
    assert report.progress["status"] == "partial"
    assert report.success is False
    assert report.progress["queued_chunks"] == 0
    with factory() as db:
        repo = HealthRepository(db)
        assert len(repo.sleep_range("owner", DAY, DAY)) == 1
        assert len(repo.activity_range("owner", DAY, DAY)) == 1
        chunks = repo.sync_chunks(attempt.id)
        assert chunks[0].status == "failed"
        assert all(chunk.status in {"succeeded", "unavailable", "failed"} for chunk in chunks)
        jobs = db.query(orm.AnalysisJob).filter_by(user_id="owner").all()
        assert len(jobs) == (0 if trigger == "manual" else 1)
        if jobs:
            assert jobs[0].target_date == DAY
            assert jobs[0].delivery_period == trigger


def test_backoff_does_not_delay_other_available_streams(factory):
    class Connector(MockZeppClient):
        def fetch_heart_rate(self, *args):
            raise ZeppAuthError("synthetic offline endpoint", kind="network")

    coordinator = ZeppSyncCoordinator(
        connector=Connector(), session_factory=factory, wall_clock=lambda: NOW,
        random_fn=lambda: 0,
    )
    attempt = coordinator.create_attempt("owner", window=WINDOW)
    first = coordinator.drain_once(attempt.id)
    assert first.progress["status"] == "queued"
    second = coordinator.drain_once(attempt.id)
    assert second.progress["status"] == "queued"
    with factory() as db:
        repo = HealthRepository(db)
        assert repo.sync_chunks(attempt.id)[0].status == "retry_wait"
        assert len(repo.sleep_range("owner", DAY, DAY)) == 1


def test_auth_failure_stops_before_other_stream_requests(factory):
    calls = []

    class Connector(MockZeppClient):
        def fetch_heart_rate(self, *args):
            calls.append("heart_rate")
            raise ZeppAuthError("synthetic expired authorization", kind="auth")

        def fetch_band_data(self, *args):
            calls.append("sleep")
            return super().fetch_band_data(*args)

    coordinator = ZeppSyncCoordinator(
        connector=Connector(), session_factory=factory, wall_clock=lambda: NOW,
    )
    attempt = coordinator.create_attempt("owner", window=WINDOW, trigger="morning")
    report = coordinator.run_attempt(attempt.id)
    assert report.progress["status"] == "needs_reauth"
    assert calls == ["heart_rate"]
    with factory() as db:
        assert db.query(orm.AnalysisJob).count() == 0
        assert HealthRepository(db).sleep_range("owner", DAY, DAY) == []


@pytest.mark.parametrize("trigger", ["manual", "nightly", "morning", "evening", "weekly", "monthly"])
def test_wire_resource_limit_uses_optional_scheduled_detail_gate(factory, monkeypatch, trigger):
    monkeypatch.setattr(client_module, "MAX_WORKOUT_DETAIL_BYTES", 32)
    client = ZeppAPIClient("synthetic-token", "synthetic-user", "api-mifitcn.zepp.com")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-length": "33"}, content=b"{}", request=request,
        ),
    ), trust_env=False)

    class Connector(MockZeppClient):
        def fetch_sport_detail(self, workout_id, source):
            return client.fetch_sport_detail(workout_id, source)

    try:
        coordinator = ZeppSyncCoordinator(
            connector=Connector(), session_factory=factory, wall_clock=lambda: NOW,
        )
        attempt = coordinator.create_attempt("owner", window=WINDOW, trigger=trigger)
        report = _drain(coordinator, attempt.id)
        assert report.progress["status"] in {"succeeded", "partial"}
        with factory() as db:
            repo = HealthRepository(db)
            detail = next(chunk for chunk in repo.sync_chunks(attempt.id) if chunk.stream == "workout_detail")
            assert detail.error_kind == "resource_limit"
            assert detail.status == ("failed" if trigger == "manual" else "unavailable")
            assert not repo.workout("owner", detail.stages["params"]["workout_id"]).detail_synced
            assert len(repo.sleep_range("owner", DAY, DAY)) == 1
            jobs = db.query(orm.AnalysisJob).all()
            assert len(jobs) == (0 if trigger == "manual" else 1)
    finally:
        client.close()


@pytest.mark.parametrize("start_day,end_day,zone", [
    (date(2026, 8, 23), date(2026, 8, 29), "Asia/Shanghai"),
    (date(2026, 3, 7), date(2026, 3, 9), "America/New_York"),
])
def test_each_daily_core_date_is_fetched_when_range_responses_keep_only_latest(
    factory, monkeypatch, start_day, end_day, zone,
):
    from vitalis.config import settings
    from vitalis.time import local_day_utc_bounds

    monkeypatch.setattr(settings, "timezone", zone)
    calls = {"sleep": [], "daily": []}

    class Connector(MockZeppClient):
        def fetch_band_data(self, from_date, to_date, *args):
            calls["sleep"].append((from_date, to_date))
            return super().fetch_band_data(to_date, to_date, *args)

        def fetch_events(self, event_type, sub_type, from_ms, to_ms, *args):
            payload = super().fetch_events(event_type, sub_type, from_ms, to_ms, *args)
            if event_type == "DailyHealth":
                calls["daily"].append((from_ms, to_ms))
                payload["data"]["items"] = payload["data"]["items"][:1]
            return payload

    window = FetchWindow.local_dates(start_day, end_day, zone)
    coordinator = ZeppSyncCoordinator(
        connector=Connector(timezone_name=zone), session_factory=factory,
        wall_clock=lambda: NOW,
    )
    attempt = coordinator.create_attempt("owner", window=window, timezone_name=zone)
    report = _drain(coordinator, attempt.id)
    assert report.progress["status"] == "succeeded"
    expected = [
        start_day + timedelta(days=offset)
        for offset in range((end_day - start_day).days + 1)
    ]
    assert calls["sleep"] == [(day.isoformat(), day.isoformat()) for day in expected]
    assert calls["daily"] == [
        (int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        for start, end in (local_day_utc_bounds(day, zone) for day in expected)
    ]
    with factory() as db:
        repo = HealthRepository(db)
        assert {row["date"] for row in repo.sleep_range("owner", start_day, end_day)} == {
            day.isoformat() for day in expected
        }
        assert {row["date"] for row in repo.activity_range("owner", start_day, end_day)} == {
            day.isoformat() for day in expected
        }
