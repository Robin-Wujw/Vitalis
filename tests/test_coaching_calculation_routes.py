"""Synthetic read-only coaching tools keep transport and computation separate."""
from datetime import date
from types import SimpleNamespace


def test_strength_review_route_defaults_and_scope(client, issue_token, monkeypatch):
    from vitalis.entrypoints.api.routes import coach
    calls = []
    query = SimpleNamespace(strength_review=lambda *a: calls.append(a) or {"sessions": []})
    monkeypatch.setattr(coach, "get_health_query", lambda: query)
    monkeypatch.setattr(coach, "local_today", lambda: date(2026, 4, 20))
    headers = {"Authorization": "Bearer " + issue_token("synthetic-review", {"read"})}
    response = client.get("/api/coach/strength-review", headers=headers)
    assert response.status_code == 200
    assert calls == [("synthetic-review", date(2026, 3, 24), date(2026, 4, 20), 100)]
    for params in ({"from": "2026-04-01"}, {"from": "2026-04-20", "to": "2026-04-01"},
                   {"from": "2025-01-01", "to": "2026-04-20"}, {"limit": 101}):
        assert client.get("/api/coach/strength-review", params=params, headers=headers).status_code == 422
    assert len(calls) == 1
    denied = {"Authorization": "Bearer " + issue_token("synthetic-review", {"coach"})}
    assert client.get("/api/coach/strength-review", headers=denied).status_code == 403


def test_race_equivalence_route_uses_selected_record_and_explicit_basis(client, issue_token, monkeypatch):
    from vitalis.entrypoints.api.routes import coach
    calls = []
    def calculate(*args, **kwargs):
        calls.append((args, kwargs))
        return {"kind": "race_equivalence", "effort_basis": kwargs["effort_basis"]}
    monkeypatch.setattr(coach, "get_health_query", lambda: SimpleNamespace(race_equivalence=calculate))
    headers = {"Authorization": "Bearer " + issue_token("synthetic-race", {"read"})}
    result = client.get("/api/coach/race-equivalence", params={"workout_id": "run-1", "source": "mock"}, headers=headers)
    assert result.status_code == 200 and result.json()["effort_basis"] == "scenario"
    assert calls == [(("synthetic-race", "run-1", "mock"), {"target_km": 21.0975, "effort_basis": "scenario"})]
    for params in ({"workout_id": "run-1"}, {"workout_id": "run-1", "source": "mock", "target_km": "nan"},
                   {"workout_id": "run-1", "source": "mock", "effort_basis": "verified"}):
        assert client.get("/api/coach/race-equivalence", params=params, headers=headers).status_code == 422
    monkeypatch.setattr(coach, "get_health_query", lambda: SimpleNamespace(race_equivalence=lambda *a, **kw: None))
    assert client.get("/api/coach/race-equivalence", params={"workout_id": "absent", "source": "mock"}, headers=headers).status_code == 404
