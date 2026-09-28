from datetime import date

import pytest

from vitalis.adapters import daily_push
from vitalis.adapters.notifications import _render_evening


@pytest.fixture(autouse=True)
def local_day(monkeypatch):
    monkeypatch.setattr(daily_push, "local_today", lambda: date(2026, 8, 29))


def _profile():
    return {
        "date": "2026-08-28", "data_quality": {"status": "PARTIAL"},
        "report_context": {
            "as_of": "2026-08-29T00:10:00Z", "timezone": "UTC",
            "training_history": {"status": "PARTIAL"},
        },
        "features": {
            "sleep": {"status": "AVAILABLE", "wake_time": "08:00"},
            "training": {"recent_workouts": []},
        },
        "decision": {"action_plan": {"expires_at": "2026-08-29T00:00:00Z"}},
    }


def test_explicit_evening_replay_analyzes_target_day_and_omits_expired_decision(monkeypatch, tmp_path):
    syncs, analyses, sent = [], [], []
    profile = _profile()

    def sync_health(user, days):
        syncs.append((user, days))
        return {"status": "synced", "success": True}

    def analyze(user, day):
        analyses.append((user, day))
        return profile

    class Service:
        def __init__(self, **kwargs):
            pass

        def push_daily_profile(self, user, payload, period):
            sent.append(payload)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push, "_sync_health", sync_health)
    monkeypatch.setattr(daily_push, "_analyze", analyze)
    monkeypatch.setattr(daily_push, "PushService", Service)
    result = daily_push.run_daily_push(
        "user", "token", period="evening", target_date=date(2026, 8, 28),
        sync_days=1, test_delivery=True, retrospective=True, state_dir=tmp_path,
    )
    assert syncs == [("user", 2)]
    assert analyses == [("user", date(2026, 8, 28))]
    assert result["status"] == "test_sent"
    assert result["retrospective"] is True
    assert result["scheduled_delivery_unchanged"] is True
    assert "decision" not in sent[0]
    assert "decision" in profile
    assert sent[0]["delivery_metadata"]["retrospective"] is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("period,test,target", [
    ("morning", True, date(2026, 8, 28)),
    ("evening", False, date(2026, 8, 28)),
    ("evening", True, None),
    ("evening", True, date(2026, 8, 30)),
    ("evening", True, date(2026, 8, 22)),
])
def test_replay_rejects_unsafe_modes_before_service_work(monkeypatch, tmp_path, period, test, target):
    monkeypatch.setattr(daily_push, "_sync_health", lambda *_args: pytest.fail("sync must not start"))
    monkeypatch.setattr(daily_push, "_analyze", lambda *_args: pytest.fail("analysis must not start"))
    with pytest.raises(ValueError):
        daily_push.run_daily_push(
            "user", "token", period=period, target_date=target,
            test_delivery=test, retrospective=True, state_dir=tmp_path,
        )


def test_normal_delivery_does_not_silently_replay_a_past_date(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_push, "PushService", lambda **kwargs: pytest.fail("must not send"))
    result = daily_push.deliver_daily_report(
        "user", "token", _profile(), period="evening", target_date=date(2026, 8, 28),
        test_delivery=True, state_dir=tmp_path,
    )
    assert result["reason"] == "stale_report_date"


def test_retrospective_evening_contains_no_current_or_tomorrow_prescription():
    payload = _profile()
    payload["delivery_metadata"] = {"retrospective": True}
    title, lines = _render_evening(payload)
    assert "晚报补发" in title and "2026-08-28" in title
    text = "\n".join(lines)
    assert "仅回顾指定日期" in text
    assert "## 今晚恢复" not in text
    assert "## 明天衔接" not in text


def test_retrospective_without_observations_remains_deferred(monkeypatch, tmp_path):
    payload = _profile()
    payload["report_context"]["training_history"]["status"] = "UNKNOWN"
    payload["features"]["sleep"] = {"status": "INSUFFICIENT_DATA"}
    monkeypatch.setattr(daily_push, "PushService", lambda **kwargs: pytest.fail("must not send empty report"))
    result = daily_push.deliver_daily_report(
        "user", "token", payload, period="evening", target_date=date(2026, 8, 28),
        retrospective=True, test_delivery=True, state_dir=tmp_path,
    )
    assert result["reason"] == "stored_data_incomplete"
