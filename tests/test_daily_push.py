from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from vitalis.adapters.zepp import ZeppAuthError
from vitalis.adapters.zepp.sync_manager import StreamReport, SyncReport
from vitalis.adapters import daily_push
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import AnalysisRun, NotificationDelivery, User
from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
from vitalis.intelligence import contracts


@pytest.fixture(autouse=True)
def offline_daily_push(monkeypatch):
    monkeypatch.setattr(daily_push, "local_today", lambda: date(2026, 8, 29))
    monkeypatch.setattr(
        daily_push, "local_day_utc_bounds",
        lambda day: (datetime(2026, 8, 29, tzinfo=timezone.utc),
                     datetime(2026, 12, 31, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(
        httpx, "Client", lambda *args, **kwargs: pytest.fail("no internal HTTP or real network")
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *args, **kwargs: pytest.fail("no internal HTTP or real network")
    )
    monkeypatch.setattr(
        daily_push, "PushService", lambda **kwargs: pytest.fail("no real PushPlus delivery")
    )


def _daily(*, wake_time="08:15:00", sleep_status="AVAILABLE"):
    return {
        "analysis_run_id": "synthetic-analysis-run",
        "user_id": "explicit-user",
        "date": "2026-08-29",
        "data_quality": {"status": "SUFFICIENT"},
        "report_context": {
            "as_of": "2026-08-29T12:00:00Z",
            "timezone": "UTC",
            "target_date": "2026-08-29",
            "target_day_complete": True,
            "training_history": {
                "status": "COMPLETE",
                "verified_days": ["2026-08-22", "2026-08-29"],
                "last_synced_at": "2026-08-29T12:00:00Z",
                "prior_7d_verified": True,
            },
        },
        "features": {"sleep": {"status": sleep_status, "wake_time": wake_time}},
    }


@pytest.fixture
def workflow(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def isolated_session_scope():
        with factory.begin() as db:
            yield db

    monkeypatch.setattr(daily_push, "session_scope", isolated_session_scope)
    with factory.begin() as db:
        db.add(User(id="explicit-user"))
        db.add(AnalysisRun(
            id="synthetic-analysis-run",
            user_id="explicit-user",
            target_date=date(2026, 8, 29),
            status="SUCCEEDED",
            started_at=datetime(2026, 8, 29, 11, 0, 0),
            completed_at=datetime(2026, 8, 29, 12, 0, 0),
            intelligence_version=contracts.INTELLIGENCE_VERSION,
            decision_policy_version=contracts.DECISION_POLICY_VERSION,
            evidence_version=contracts.EVIDENCE_VERSION,
            config_digest=_current_analysis_config_digest(),
        ))

    calls = {
        "sync": [], "analyze": [],
        "sync_result": {"status": "synced", "success": True},
        "profiles": [_daily()], "factory": factory,
    }

    def sync(user_id, days):
        calls["sync"].append((user_id, days))
        return calls["sync_result"]

    def analyze(user_id, day):
        calls["analyze"].append((user_id, day))
        profile = calls["profiles"].pop(0)
        run_id = f"synthetic-analysis-{len(calls['analyze'])}"
        profile.update(analysis_run_id=run_id, user_id=user_id)
        with factory.begin() as db:
            db.add(AnalysisRun(
                id=run_id,
                user_id=user_id,
                target_date=day,
                status="SUCCEEDED",
                started_at=datetime.combine(day, datetime.min.time()),
                completed_at=datetime.combine(day, datetime.min.time()),
                intelligence_version=contracts.INTELLIGENCE_VERSION,
                decision_policy_version=contracts.DECISION_POLICY_VERSION,
                evidence_version=contracts.EVIDENCE_VERSION,
                config_digest=_current_analysis_config_digest(),
            ))
        return profile

    monkeypatch.setattr(daily_push, "_sync_health", sync)
    monkeypatch.setattr(daily_push, "_analyze", analyze)
    yield calls
    engine.dispose()


def _delivery(workflow, period="morning"):
    with workflow["factory"]() as db:
        return db.query(NotificationDelivery).filter_by(
            user_id="explicit-user", target_date=date(2026, 8, 29), period=period,
        ).one_or_none()


def _capture_push(monkeypatch, *, outcomes=None):
    sent = []
    default = {
        "status": "accepted", "provider_id": "synthetic-code",
        "send_attempt_id": "synthetic-send-attempt",
    }
    sequence = list(outcomes) if outcomes is not None else [default]
    last = sequence[-1] if sequence else default

    class Service:
        def __init__(self, pushplus_token, **kwargs):
            assert pushplus_token == "private-token"
            self.kwargs = kwargs

        def push_daily_profile(self, user_id, profile, period):
            sent.append((user_id, profile, period))
            result = sequence.pop(0) if sequence else last
            return {"_pushplus_result": dict(result)}

    monkeypatch.setattr(daily_push, "PushService", Service)
    return sent


def _run(tmp_path, *, period="morning", **kwargs):
    return daily_push.run_daily_push(
        "explicit-user", "private-token", period=period,
        state_dir=tmp_path, **kwargs,
    )


def test_daily_push_validates_arguments_before_service_calls(workflow, tmp_path):
    with pytest.raises(ValueError, match="VITALIS_USER"):
        daily_push.run_daily_push("", "private-token", period="morning", state_dir=tmp_path)
    with pytest.raises(ValueError, match="PUSHPLUS_TOKEN"):
        daily_push.run_daily_push("user", "", period="morning", state_dir=tmp_path)
    with pytest.raises(ValueError, match="period"):
        _run(tmp_path, period="midday")
    with pytest.raises(ValueError, match="sync_days"):
        _run(tmp_path, sync_days=0)
    with pytest.raises(ValueError, match="sync_days"):
        _run(tmp_path, sync_days=731)
    assert workflow["sync"] == workflow["analyze"] == []


@pytest.mark.parametrize("api", ["https://vitalis.example.test", "http://localhost:8000/api", "not-a-url"])
def test_remote_or_nonlocal_api_fails_without_service_or_http(workflow, tmp_path, api):
    with pytest.raises(ValueError, match="VITALIS_API must be local"):
        _run(tmp_path, api=api)
    assert workflow["sync"] == workflow["analyze"] == []


def test_morning_test_delivery_returns_accepted_without_filesystem_state(monkeypatch, workflow, tmp_path):
    profile = workflow["profiles"][0]
    sent = _capture_push(monkeypatch)
    result = _run(
        tmp_path, api="http://127.0.0.1:8000/", target_date=date(2026, 8, 29),
        test_delivery=True,
    )

    assert workflow["sync"] == [("explicit-user", 2)]
    assert workflow["analyze"] == [("explicit-user", date(2026, 8, 29))]
    assert sent == [("explicit-user", profile, "morning")]
    assert result == {
        "status": "test_accepted", "period": "morning", "date": "2026-08-29",
        "quality": "SUFFICIENT", "sync_degraded": False, "sync_status": "synced",
        "provider_id": "synthetic-code", "scheduled_delivery_unchanged": True,
    }
    assert not list(tmp_path.glob("*.sent"))


def test_manual_test_push_isolated_from_scheduled_intent(monkeypatch, workflow, tmp_path):
    sent = _capture_push(monkeypatch)

    result = _run(tmp_path, period="evening", test_delivery=True)

    assert workflow["sync"] == [("explicit-user", 1)]
    assert len(sent) == 1
    assert result["status"] == "test_accepted"
    assert result["scheduled_delivery_unchanged"] is True
    assert not list(tmp_path.glob("*.sent"))


@pytest.mark.parametrize(("sleep_status", "wake_time"), [("INSUFFICIENT_DATA", None), ("AVAILABLE", None)])
def test_morning_push_defers_without_complete_sleep(monkeypatch, workflow, tmp_path, sleep_status, wake_time):
    workflow["profiles"] = [_daily(sleep_status=sleep_status, wake_time=wake_time)]
    result = _run(tmp_path)

    assert workflow["sync"] == [("explicit-user", 2)]
    assert workflow["analyze"] == [("explicit-user", date(2026, 8, 29))]
    assert result == {
        "status": "deferred", "period": "morning", "date": "2026-08-29",
        "reason": "sleep_incomplete", "sync_degraded": False, "sync_status": "synced",
    }
    assert not list(tmp_path.glob("*.sent"))


def test_hourly_retry_sends_after_wake_then_skips_later_runs(monkeypatch, workflow, tmp_path):
    workflow["profiles"] = [_daily(wake_time=None), _daily(wake_time="10:05:00")]
    sent = _capture_push(monkeypatch)

    first = _run(tmp_path)
    second = _run(tmp_path)
    third = _run(tmp_path)

    assert first["status"] == "deferred"
    assert second["status"] == "accepted"
    assert third == {
        "status": "accepted", "period": "morning", "date": "2026-08-29",
        "provider_id": "synthetic-code",
    }
    assert len(sent) == 1
    assert len(workflow["analyze"]) == 2
    assert workflow["sync"] == [("explicit-user", 2), ("explicit-user", 2)]
    assert _delivery(workflow).status == "accepted"
    assert _delivery(workflow).provider_id == "synthetic-code"
    assert not list(tmp_path.glob("*.sent"))


def test_evening_push_uses_one_day_and_ignores_morning_sleep_gate(monkeypatch, workflow, tmp_path):
    workflow["profiles"] = [_daily(sleep_status="INSUFFICIENT_DATA", wake_time=None)]
    sent = _capture_push(monkeypatch)
    result = _run(tmp_path, period="evening")

    assert workflow["sync"] == [("explicit-user", 1)]
    assert [item[2] for item in sent] == ["evening"]
    assert result["status"] == "accepted"
    assert _delivery(workflow, "evening").status == "accepted"


def test_uncertain_delivery_is_not_resent_after_provider_may_have_accepted(
    monkeypatch, workflow, tmp_path,
):
    workflow["profiles"] = [_daily(), _daily()]
    sent = _capture_push(monkeypatch, outcomes=[{
        "status": "uncertain", "provider_id": "synthetic-code",
        "send_attempt_id": "attempt-uncertain",
    }, {
        "status": "accepted", "provider_id": "synthetic-code",
        "send_attempt_id": "attempt-should-not-send",
    }])

    with pytest.raises(daily_push.DailyPushDeliveryError) as error:
        _run(tmp_path)
    assert error.value.ambiguous is True
    assert _delivery(workflow).status == "uncertain"
    second = _run(tmp_path)
    assert second == {
        "status": "uncertain", "period": "morning", "date": "2026-08-29",
        "provider_id": "synthetic-code",
    }
    assert len(sent) == 1
    assert len(workflow["analyze"]) == 1
    assert not list(tmp_path.glob("*.sent"))


def test_failed_delivery_is_not_marked_and_can_retry(monkeypatch, workflow, tmp_path):
    workflow["profiles"] = [_daily(), _daily()]
    sent = _capture_push(monkeypatch, outcomes=[
        {"status": "failed", "provider_id": "synthetic-code", "send_attempt_id": "attempt-failed"},
        {"status": "accepted", "provider_id": "synthetic-code", "send_attempt_id": "attempt-retry"},
    ])

    with pytest.raises(RuntimeError, match="PushPlus delivery failed"):
        _run(tmp_path)
    assert _delivery(workflow).status == "failed"
    assert not list(tmp_path.glob("*.sent"))
    assert _run(tmp_path)["status"] == "accepted"
    assert len(sent) == 2
    assert _delivery(workflow).status == "accepted"
    assert not list(tmp_path.glob("*.sent"))


@pytest.mark.parametrize("sync", [
    {"status": "needs_reauth"},
    {"status": "token_required"},
    {"status": "failed"},
    {"status": "cancelled"},
    {"status": "synced", "success": False, "streams": [{"needs_reauth": True}]},
])
def test_hard_sync_failure_never_analyzes_or_sends(workflow, tmp_path, sync):
    workflow["sync_result"] = sync
    with pytest.raises(RuntimeError, match="Vitalis sync did not complete"):
        _run(tmp_path)
    assert workflow["analyze"] == []
    assert not list(tmp_path.glob("*.sent"))


@pytest.mark.parametrize("sync", [
    {"status": "synced", "success": False, "message": "one stream unavailable"},
    {"status": "incomplete", "success": False, "message": "partial coverage"},
    {"status": "transient_error", "retryable": True, "detail": "sync retry pending"},
    {"status": "timeout", "retryable": True, "detail": "attempt still running"},
])
def test_retryable_sync_uses_complete_stored_profile(monkeypatch, workflow, tmp_path, sync):
    workflow["sync_result"] = sync
    sent = _capture_push(monkeypatch)
    result = _run(tmp_path)

    assert result["status"] == "accepted"
    assert result["sync_degraded"] is True
    assert result["sync_status"] == sync["status"]
    assert sent[0][1]["delivery_metadata"] == {
        "sync_degraded": True, "sync_status": sync["status"],
        "sync_detail": sync.get("detail") or sync.get("message"),
    }
    assert _delivery(workflow).status == "accepted"
    assert not list(tmp_path.glob("*.sent"))


@pytest.mark.parametrize("profile", [
    _daily(wake_time=None),
    {"date": "2026-08-29", "data_quality": {"status": "INSUFFICIENT"},
     "features": {"sleep": {"status": "AVAILABLE", "wake_time": "08:15:00"}}},
    {**_daily(), "date": "2026-08-28"},
])
def test_degraded_sync_defers_when_stored_profile_is_not_usable(workflow, tmp_path, profile):
    workflow["sync_result"] = {"status": "incomplete", "success": False}
    workflow["profiles"] = [profile]
    assert _run(tmp_path) == {
        "status": "deferred", "period": "morning", "date": "2026-08-29",
        "reason": "stored_data_incomplete", "sync_degraded": True, "sync_status": "incomplete",
    }
    assert not list(tmp_path.glob("*.sent"))


@pytest.mark.parametrize(("attempt", "success", "expected", "retryable"), [
    ("succeeded", True, "synced", False),
    ("partial", False, "incomplete", False),
    ("retry_wait", False, "transient_error", True),
    ("queued", False, "timeout", True),
    ("running", False, "timeout", True),
    ("failed", False, "failed", False),
    ("cancelled", False, "cancelled", False),
    ("needs_reauth", False, "needs_reauth", False),
])
def test_sync_report_status_preserves_terminal_and_retry_states(attempt, success, expected, retryable):
    report = SyncReport(success, progress={"attempt_id": "attempt-1", "status": attempt})
    result = daily_push._sync_report_result(report)
    assert result["status"] == expected
    assert result["retryable"] is retryable
    assert result["attempt_status"] == attempt
    if expected == "timeout":
        assert "可重试" in result["detail"]


def test_stream_reauth_overrides_healthy_attempt():
    report = SyncReport(True, streams=[StreamReport("heart_rate", "failed", needs_reauth=True)],
                        progress={"status": "succeeded"})
    result = daily_push._sync_report_result(report)
    assert result["status"] == "needs_reauth"
    with pytest.raises(RuntimeError, match="needs_reauth"):
        daily_push._assess_sync(result)


def test_pending_sync_resumes_same_attempt_until_complete(monkeypatch):
    waits = []
    monkeypatch.setattr(daily_push.time, "sleep", lambda seconds: waits.append(seconds))
    replies = iter([
        SyncReport(False, progress={"status": "retry_wait", "attempt_id": "a-1"}),
        SyncReport(True, progress={"status": "succeeded", "attempt_id": "a-1"}),
    ])
    calls = []

    class Connector:
        def sync_with_report(self, user, **kwargs):
            calls.append((user.id, kwargs))
            return next(replies)

    user = daily_push.User(id="explicit-user")
    initial = SyncReport(False, progress={"status": "queued", "attempt_id": "a-1"})
    final, timed_out = daily_push._await_sync_report(Connector(), user, 2, initial)
    assert timed_out is False
    assert final.progress["status"] == "succeeded"
    assert waits == [2, 2]
    assert calls == [("explicit-user", {"days": 2, "attempt_id": "a-1", "trigger": "manual"})] * 2


def test_retry_wait_timeout_is_retryable_not_a_false_success(monkeypatch):
    monkeypatch.setattr(daily_push, "SYNC_POLL_MAX_ATTEMPTS", 2)
    waits = []
    monkeypatch.setattr(daily_push.time, "sleep", lambda seconds: waits.append(seconds))
    monkeypatch.setattr(daily_push, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(daily_push, "HealthRepository", lambda db: object())
    calls = []

    class Connector:
        mock = False

        def load_token(self, repo, user_id):
            return object()

        def sync_with_report(self, user, **kwargs):
            calls.append(kwargs)
            return SyncReport(False, progress={"status": "retry_wait", "attempt_id": "a-1"})

    monkeypatch.setattr(daily_push, "ZeppConnector", Connector)
    result = daily_push._sync_health("explicit-user", 2)
    assert result["status"] == "timeout"
    assert result["attempt_status"] == "retry_wait"
    assert result["retryable"] is True
    assert waits == [2, 2]
    assert calls[0] == {"days": 2, "trigger": "manual"}
    assert calls[1:] == [{"days": 2, "attempt_id": "a-1", "trigger": "manual"}] * 2


def test_pending_sync_stops_on_reauthentication(monkeypatch):
    monkeypatch.setattr(daily_push.time, "sleep", lambda seconds: None)
    calls = []

    class Connector:
        def sync_with_report(self, user, **kwargs):
            calls.append(kwargs)
            return SyncReport(False, streams=[StreamReport("heart_rate", "failed", needs_reauth=True)],
                              progress={"status": "needs_reauth", "attempt_id": "a-1"})

    first = SyncReport(False, progress={"status": "queued", "attempt_id": "a-1"})
    final, timed_out = daily_push._await_sync_report(
        Connector(), daily_push.User(id="explicit-user"), 2, first,
    )
    assert timed_out is False
    assert daily_push._sync_report_result(final)["status"] == "needs_reauth"
    assert len(calls) == 1


@pytest.mark.parametrize("mock,auth,expected_kwargs", [
    (True, None, {"days": 2, "trigger": "manual", "repo": "repository"}),
    (False, object(), {"days": 2, "trigger": "manual"}),
])
def test_sync_uses_connector_and_short_storage_sessions(monkeypatch, mock, auth, expected_kwargs):
    events = []
    db = object()

    @contextmanager
    def session():
        events.append("open")
        yield db
        events.append("closed")

    class Repository:
        def __init__(self, actual_db):
            assert actual_db is db
            events.append("repository")

    class Connector:
        def __init__(self):
            self.mock = mock

        def load_token(self, repo, user_id):
            assert isinstance(repo, Repository)
            events.append(("token", user_id))
            return auth

        def sync_with_report(self, user, **kwargs):
            assert events[-1] == ("repository" if mock else "closed")
            assert user.id == "explicit-user"
            assert {key: "repository" if isinstance(value, Repository) else value
                    for key, value in kwargs.items()} == expected_kwargs
            events.append("sync")
            return SyncReport(True, progress={"status": "succeeded"})

    monkeypatch.setattr(daily_push, "session_scope", session)
    monkeypatch.setattr(daily_push, "HealthRepository", Repository)
    monkeypatch.setattr(daily_push, "ZeppConnector", Connector)
    assert daily_push._sync_health("explicit-user", 2)["status"] == "synced"
    assert events.count("open") == (2 if mock else 1)


def test_missing_real_token_stops_before_sync(monkeypatch):
    monkeypatch.setattr(daily_push, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(daily_push, "HealthRepository", lambda db: object())

    class Connector:
        mock = False

        def load_token(self, repo, user_id):
            return None

        def sync_with_report(self, user, **kwargs):
            pytest.fail("missing Zepp token must not start sync")

    monkeypatch.setattr(daily_push, "ZeppConnector", Connector)
    result = daily_push._sync_health("explicit-user", 2)
    assert result["status"] == "token_required"
    with pytest.raises(RuntimeError, match="token_required"):
        daily_push._assess_sync(result)


@pytest.mark.parametrize(("kind", "expected", "retryable"), [
    ("auth", "needs_reauth", False),
    ("timeout", "transient_error", True),
    ("network", "transient_error", True),
    ("service", "transient_error", True),
    ("invalid_request", "failed", False),
])
def test_connector_error_classification(monkeypatch, kind, expected, retryable):
    monkeypatch.setattr(daily_push, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(daily_push, "HealthRepository", lambda db: object())

    class Connector:
        mock = False

        def load_token(self, repo, user_id):
            return object()

        def sync_with_report(self, user, **kwargs):
            raise ZeppAuthError("vendor error", kind=kind)

    monkeypatch.setattr(daily_push, "ZeppConnector", Connector)
    result = daily_push._sync_health("explicit-user", 2)
    assert result == {"status": expected, "retryable": retryable, "detail": "vendor error"}


def test_storage_failure_is_not_turned_into_retryable_sync(monkeypatch):
    @contextmanager
    def unavailable_storage():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(daily_push, "session_scope", unavailable_storage)
    with pytest.raises(RuntimeError, match="database unavailable"):
        daily_push._sync_health("explicit-user", 2)


def test_analysis_uses_intelligence_command_and_json_daily(monkeypatch):
    calls = []

    class Daily:
        def model_dump(self, **kwargs):
            calls.append(("serialize", kwargs))
            return _daily()

    class Command:
        def analyze(self, user_id, day):
            calls.append(("analyze", user_id, day))
            return type("Result", (), {"daily": Daily()})()

    monkeypatch.setattr(daily_push, "IntelligenceCommand", Command)
    assert daily_push._analyze("explicit-user", date(2026, 8, 29)) == _daily()
    assert calls == [("analyze", "explicit-user", date(2026, 8, 29)),
                     ("serialize", {"mode": "json"})]


def test_stale_report_is_not_delivered_across_midnight(workflow, tmp_path):
    workflow["profiles"] = [{**_daily(), "date": "2026-08-28"}]
    result = _run(tmp_path)
    assert result["status"] == "deferred"
    assert result["reason"] == "stale_report_date"


def test_delivery_rechecks_local_day_after_sync_wait(monkeypatch, workflow, tmp_path):
    days = iter([date(2026, 8, 29), date(2026, 8, 30)])
    monkeypatch.setattr(daily_push, "local_today", lambda: next(days))
    result = _run(tmp_path)
    assert workflow["sync"] == [("explicit-user", 2)]
    assert result["reason"] == "stale_report_date"


def test_evening_unknown_history_can_send_same_day_facts(monkeypatch, workflow, tmp_path):
    daily = _daily()
    daily["report_context"]["training_history"] = {
        "status": "UNKNOWN", "verified_days": [], "last_synced_at": None,
        "prior_7d_verified": False,
    }
    sent = _capture_push(monkeypatch)
    result = daily_push.deliver_daily_report(
        "explicit-user", "private-token", daily,
        period="evening", target_date=date(2026, 8, 29), state_dir=tmp_path,
    )
    assert result["status"] == "accepted"
    assert sent[0][2] == "evening"
    assert _delivery(workflow, "evening").status == "accepted"


def test_evening_unknown_history_without_same_day_facts_defers(tmp_path):
    daily = _daily(sleep_status="INSUFFICIENT_DATA", wake_time=None)
    daily["report_context"]["training_history"] = {
        "status": "UNKNOWN", "verified_days": [], "last_synced_at": None,
        "prior_7d_verified": False,
    }
    result = daily_push.deliver_daily_report(
        "explicit-user", "private-token", daily,
        period="evening", target_date=date(2026, 8, 29), state_dir=tmp_path,
    )
    assert result["status"] == "deferred"
    assert result["reason"] == "stored_data_incomplete"


def test_morning_unverified_history_sends_facts_only_once(monkeypatch, workflow, tmp_path):
    profile = _daily()
    profile["report_context"]["training_history"].update(
        status="PARTIAL", prior_7d_verified=False,
    )
    workflow["profiles"] = [profile]
    sent = _capture_push(monkeypatch)
    first = _run(tmp_path)
    second = _run(tmp_path)

    assert first["status"] == "accepted"
    assert first["mode"] == "facts_only"
    assert first["facts_only"] is True
    assert first["coverage_reason"] == "prior_7d_unverified"
    assert second == {
        "status": "accepted", "period": "morning", "date": "2026-08-29",
        "provider_id": "synthetic-code",
    }
    assert workflow["sync"] == [("explicit-user", 2)]
    assert sent[0][1]["delivery_metadata"] == {
        "facts_only": True, "coverage_reason": "prior_7d_unverified",
    }
    assert "delivery_metadata" not in profile
    assert _delivery(workflow).status == "accepted"
    assert not list(tmp_path.glob("*.sent"))


@pytest.mark.parametrize(("target", "expected_days"), [
    (date(2026, 8, 28), 2), (date(2026, 8, 27), 3),
])
def test_retrospective_test_evening_extends_sync_and_excludes_decision(
    monkeypatch, workflow, tmp_path, target, expected_days,
):
    profile = _daily()
    profile.update(date=target.isoformat(), decision={"action": "train"})
    workflow["profiles"] = [profile]
    sent = _capture_push(monkeypatch)
    result = _run(tmp_path, period="evening", sync_days=1,
                  target_date=target, retrospective=True, test_delivery=True)
    assert workflow["sync"] == [("explicit-user", expected_days)]
    assert workflow["analyze"] == [("explicit-user", target)]
    assert result["status"] == "test_accepted"
    assert result["retrospective"] is True
    assert result["scheduled_delivery_unchanged"] is True
    assert sent[0][1]["delivery_metadata"]["retrospective"] is True
    assert "decision" not in sent[0][1]
    assert "decision" in profile
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("period", "test_delivery", "target"), [
    ("morning", True, date(2026, 8, 28)),
    ("evening", False, date(2026, 8, 28)),
    ("evening", True, None),
    ("evening", True, date(2026, 8, 30)),
    ("evening", True, date(2026, 8, 22)),
])
def test_retrospective_rejects_unsafe_modes_before_services(
    workflow, tmp_path, period, test_delivery, target,
):
    with pytest.raises(ValueError):
        _run(tmp_path, period=period, retrospective=True,
             target_date=target, test_delivery=test_delivery)
    assert workflow["sync"] == workflow["analyze"] == []


def test_normal_delivery_and_expired_plan_never_deliver(workflow, tmp_path):
    with pytest.raises(ValueError, match="非当日报告"):
        _run(tmp_path, period="evening", target_date=date(2026, 8, 28))
    assert workflow["sync"] == []
    result = daily_push.deliver_daily_report(
        "explicit-user", "private-token", _daily(), period="morning",
        state_dir=tmp_path, plan_expires_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert result["reason"] == "stale_plan_expired"
    assert not list(tmp_path.glob("*.sent"))
