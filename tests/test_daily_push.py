import pytest

from vitalis.services import daily_push


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_daily_push_requires_identity_and_token_before_network(monkeypatch):
    monkeypatch.setattr(
        daily_push.httpx,
        "Client",
        lambda **kwargs: pytest.fail("network client must not be created"),
    )

    with pytest.raises(ValueError, match="VITALIS_USER"):
        daily_push.run_morning_push("", "token")
    with pytest.raises(ValueError, match="PUSHPLUS_TOKEN"):
        daily_push.run_morning_push("user", "")


def test_daily_push_syncs_analyzes_and_sends_exactly_once(monkeypatch):
    requests = []
    sent = []
    daily = {
        "date": "2026-08-29",
        "data_quality": {"status": "SUFFICIENT"},
        "decision": {},
        "features": {},
    }

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {
                "base_url": "http://127.0.0.1:8000",
                "headers": {"X-User-Id": "explicit-user"},
                "timeout": 180.0,
            }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            requests.append((path, kwargs))
            if path.endswith("/sync"):
                return Response({"status": "synced", "success": True})
            return Response({"daily": daily})

    class Service:
        def __init__(self, pushplus_token):
            assert pushplus_token == "private-token"

        def push_daily_profile(self, user_id, profile, period):
            sent.append((user_id, profile, period))
            return {"_log_handler": "ok", "_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push.httpx, "Client", Client)
    monkeypatch.setattr(daily_push, "PushService", Service)
    result = daily_push.run_morning_push(
        "explicit-user",
        "private-token",
        api="http://127.0.0.1:8000/",
        sync_days=2,
    )

    assert requests == [
        ("/api/v1/health/sync", {"params": {"days": 2}}),
        ("/api/v1/intelligence/analyze", {}),
    ]
    assert sent == [("explicit-user", daily, "morning")]
    assert result == {
        "status": "sent",
        "date": "2026-08-29",
        "quality": "SUFFICIENT",
    }


def test_daily_push_does_not_analyze_or_send_after_failed_sync(monkeypatch):
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
        daily_push.run_morning_push("explicit-user", "private-token")
    assert requests == ["/api/v1/health/sync"]
