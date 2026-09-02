from datetime import date
import os
import stat

import pytest

from vitalis.services import daily_push


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _daily(*, wake_time="08:15:00", sleep_status="AVAILABLE"):
    return {
        "date": "2026-08-29",
        "data_quality": {"status": "SUFFICIENT"},
        "features": {
            "sleep": {"status": sleep_status, "wake_time": wake_time},
        },
    }


def test_daily_push_requires_identity_token_and_valid_period_before_network(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        daily_push.httpx,
        "Client",
        lambda **kwargs: pytest.fail("network client must not be created"),
    )

    with pytest.raises(ValueError, match="VITALIS_USER"):
        daily_push.run_daily_push("", "token", period="morning", state_dir=tmp_path)
    with pytest.raises(ValueError, match="PUSHPLUS_TOKEN"):
        daily_push.run_daily_push("user", "", period="morning", state_dir=tmp_path)
    with pytest.raises(ValueError, match="period"):
        daily_push.run_daily_push(
            "user", "token", period="midday", state_dir=tmp_path
        )


def test_morning_push_syncs_current_day_and_sends_exactly_once(monkeypatch, tmp_path):
    requests = []
    sent = []
    profile = _daily()
    chmod_calls = []
    real_chmod = daily_push.os.chmod

    def record_chmod(path, mode):
        chmod_calls.append((os.fspath(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(daily_push.os, "chmod", record_chmod)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {
                "base_url": "http://127.0.0.1:8000",
                "headers": {"X-User-Id": "explicit-user"},
                "timeout": 180.0,
                "trust_env": False,
            }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            requests.append((path, kwargs))
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": True})
            return Response({"daily": profile})

    class Service:
        def __init__(self, pushplus_token):
            assert pushplus_token == "private-token"

        def push_daily_profile(self, user_id, daily, period):
            sent.append((user_id, daily, period))
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(daily_push, "PushService", Service)
    result = daily_push.run_daily_push(
        "explicit-user",
        "private-token",
        period="morning",
        api="http://127.0.0.1:8000/",
        target_date=date(2026, 8, 29),
        state_dir=tmp_path,
    )

    assert requests == [
        ("/api/v1/health/sync", {"params": {"days": 2}}),
        ("/api/v1/intelligence/analyze", {"params": {"day": "2026-08-29"}}),
    ]
    assert sent == [("explicit-user", profile, "morning")]
    assert result == {
        "status": "sent",
        "period": "morning",
        "date": "2026-08-29",
        "quality": "SUFFICIENT",
        "sync_degraded": False,
        "sync_status": "synced",
    }
    marker = next(tmp_path.glob("*.sent"))
    assert "explicit-user" not in marker.name
    assert (os.fspath(marker), 0o600) in chmod_calls
    assert (os.fspath(tmp_path), 0o700) in chmod_calls
    if os.name != "nt":
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_manual_test_push_ignores_and_preserves_scheduled_delivery_marker(
    monkeypatch, tmp_path
):
    sent = []
    marker = daily_push._delivery_marker(
        tmp_path, "explicit-user", date(2026, 8, 29), "evening"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("official delivery\n", encoding="utf-8")

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": True})
            return Response({"daily": _daily()})

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append(period)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(daily_push, "PushService", Service)

    result = daily_push.run_daily_push(
        "explicit-user",
        "private-token",
        period="evening",
        target_date=date(2026, 8, 29),
        state_dir=tmp_path,
        test_delivery=True,
    )

    assert sent == ["evening"]
    assert result["status"] == "test_sent"
    assert result["scheduled_delivery_unchanged"] is True
    assert marker.read_text(encoding="utf-8") == "official delivery\n"
    assert list(tmp_path.glob("*.sent")) == [marker]


@pytest.mark.parametrize(
    ("sleep_status", "wake_time"),
    [("INSUFFICIENT_DATA", None), ("AVAILABLE", None)],
)
def test_morning_push_defers_without_complete_sleep(
    monkeypatch, tmp_path, sleep_status, wake_time
):
    requests = []
    sent = []
    profile = _daily(sleep_status=sleep_status, wake_time=wake_time)

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            requests.append((path, kwargs))
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": True})
            return Response({"daily": profile})

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(
        daily_push,
        "PushService",
        lambda **kwargs: sent.append(kwargs),
    )

    result = daily_push.run_daily_push(
        "explicit-user",
        "private-token",
        period="morning",
        target_date=date(2026, 8, 29),
        state_dir=tmp_path,
    )

    assert requests == [
        ("/api/v1/health/sync", {"params": {"days": 2}}),
        ("/api/v1/intelligence/analyze", {"params": {"day": "2026-08-29"}}),
    ]
    assert sent == []
    assert result == {
        "status": "deferred",
        "period": "morning",
        "date": "2026-08-29",
        "reason": "sleep_incomplete",
        "sync_degraded": False,
        "sync_status": "synced",
    }
    assert not list(tmp_path.glob("*.sent"))


def test_hourly_retry_sends_after_wake_then_skips_later_runs(monkeypatch, tmp_path):
    analyses = [_daily(wake_time=None), _daily(wake_time="10:05:00")]
    sent = []
    client_count = 0

    class Client:
        def __init__(self, **kwargs):
            nonlocal client_count
            client_count += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": True})
            return Response({"daily": analyses.pop(0)})

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append((profile, period))
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(daily_push, "PushService", Service)
    arguments = {
        "period": "morning",
        "target_date": date(2026, 8, 29),
        "state_dir": tmp_path,
    }

    first = daily_push.run_daily_push("explicit-user", "private-token", **arguments)
    second = daily_push.run_daily_push("explicit-user", "private-token", **arguments)
    third = daily_push.run_daily_push("explicit-user", "private-token", **arguments)

    assert first["status"] == "deferred"
    assert second["status"] == "sent"
    assert third == {
        "status": "already_sent",
        "period": "morning",
        "date": "2026-08-29",
    }
    assert len(sent) == 1
    assert client_count == 2
    assert len(list(tmp_path.glob("*.sent"))) == 1


def test_evening_push_uses_one_day_and_ignores_morning_sleep_gate(
    monkeypatch, tmp_path
):
    requests = []
    sent = []
    profile = _daily(sleep_status="INSUFFICIENT_DATA", wake_time=None)

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            requests.append((path, kwargs))
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": True})
            return Response({"daily": profile})

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, daily, period):
            sent.append(period)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(daily_push, "PushService", Service)
    result = daily_push.run_daily_push(
        "explicit-user",
        "private-token",
        period="evening",
        target_date=date(2026, 8, 29),
        state_dir=tmp_path,
    )

    assert requests[0] == ("/api/v1/health/sync", {"params": {"days": 1}})
    assert sent == ["evening"]
    assert result["status"] == "sent"
    assert result["period"] == "evening"


def test_failed_delivery_is_not_marked_and_can_retry(monkeypatch, tmp_path):
    outcomes = ["error: unavailable", "ok"]
    sends = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": True})
            return Response({"daily": _daily()})

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sends.append(period)
            return {"_pushplus_handler": outcomes.pop(0)}

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(daily_push, "PushService", Service)
    arguments = {
        "period": "morning",
        "target_date": date(2026, 8, 29),
        "state_dir": tmp_path,
    }

    with pytest.raises(RuntimeError, match="PushPlus delivery failed"):
        daily_push.run_daily_push("explicit-user", "private-token", **arguments)
    assert not list(tmp_path.glob("*.sent"))

    result = daily_push.run_daily_push(
        "explicit-user", "private-token", **arguments
    )
    assert result["status"] == "sent"
    assert sends == ["morning", "morning"]
    assert len(list(tmp_path.glob("*.sent"))) == 1


def test_remote_api_keeps_environment_proxy_support(monkeypatch, tmp_path):
    client_kwargs = []

    class Client:
        def __init__(self, **kwargs):
            client_kwargs.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            return Response({"status": "needs_reauth"})

    monkeypatch.setattr(daily_push.httpx, "Client", Client)

    with pytest.raises(RuntimeError, match="needs_reauth"):
        daily_push.run_daily_push(
            "explicit-user",
            "private-token",
            period="evening",
            api="https://vitalis.example.test",
            state_dir=tmp_path,
        )

    assert client_kwargs[0]["trust_env"] is False


def test_daily_push_does_not_analyze_or_send_after_failed_sync(monkeypatch, tmp_path):
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            requests.append(path)
            return Response({"status": "needs_reauth"})

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(
        daily_push,
        "PushService",
        lambda **kwargs: pytest.fail("push service must not be created"),
    )

    with pytest.raises(RuntimeError, match="needs_reauth"):
        daily_push.run_daily_push(
            "explicit-user",
            "private-token",
            period="morning",
            state_dir=tmp_path,
        )
    assert requests == ["/api/v1/health/sync"]


def test_daily_push_does_not_fallback_when_stream_reports_reauth(
    monkeypatch, tmp_path
):
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            requests.append(path)
            return Response({
                "status": "synced",
                "success": False,
                "streams": [{"stream": "heart_rate", "needs_reauth": True}],
            })

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(
        daily_push,
        "PushService",
        lambda **kwargs: pytest.fail("push service must not be created"),
    )

    with pytest.raises(RuntimeError, match="synced"):
        daily_push.run_daily_push(
            "explicit-user",
            "private-token",
            period="morning",
            state_dir=tmp_path,
        )
    assert requests == ["/api/v1/health/sync"]


@pytest.mark.parametrize(
    "sync_payload",
    [
        {"status": "synced", "success": False, "message": "1 个数据流超时"},
        {
            "status": "incomplete",
            "success": False,
            "message": "部分数据流不可用；成功数据已保存",
        },
        {
            "status": "transient_error",
            "retryable": True,
            "detail": "同步超时",
        },
    ],
)
def test_daily_push_uses_complete_stored_profile_after_retryable_sync_failure(
    monkeypatch, tmp_path, sync_payload
):
    sent = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            if path.endswith("/sync"):
                return Response(sync_payload)
            return Response({"daily": _daily()})

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append(profile)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(daily_push, "PushService", Service)

    result = daily_push.run_daily_push(
        "explicit-user",
        "private-token",
        period="morning",
        target_date=date(2026, 8, 29),
        state_dir=tmp_path,
    )

    assert result["status"] == "sent"
    assert result["sync_degraded"] is True
    assert result["sync_status"] == sync_payload["status"]
    assert sent[0]["delivery_metadata"]["sync_degraded"] is True
    assert sent[0]["delivery_metadata"]["sync_status"] == sync_payload["status"]
    assert len(list(tmp_path.glob("*.sent"))) == 1


@pytest.mark.parametrize(
    "profile",
    [
        _daily(wake_time=None),
        {
            "date": "2026-08-29",
            "data_quality": {"status": "INSUFFICIENT"},
            "features": {
                "sleep": {"status": "AVAILABLE", "wake_time": "08:15:00"}
            },
        },
        {
            **_daily(),
            "date": "2026-08-28",
        },
    ],
)
def test_degraded_sync_defers_when_stored_profile_is_not_current_and_complete(
    monkeypatch, tmp_path, profile
):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": False})
            return Response({"daily": profile})

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(
        daily_push,
        "PushService",
        lambda **kwargs: pytest.fail("incomplete stored data must not be sent"),
    )

    result = daily_push.run_daily_push(
        "explicit-user",
        "private-token",
        period="morning",
        target_date=date(2026, 8, 29),
        state_dir=tmp_path,
    )

    assert result == {
        "status": "deferred",
        "period": "morning",
        "date": "2026-08-29",
        "reason": "stored_data_incomplete",
        "sync_degraded": True,
        "sync_status": "synced",
    }
    assert not list(tmp_path.glob("*.sent"))
