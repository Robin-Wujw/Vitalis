"""Previously omitted vendor metadata remains explicit and uninterpreted."""
from datetime import UTC, datetime

import pytest

from vitalis.adapters.zepp.parser import ZeppParser


@pytest.mark.parametrize("code", [0, 1, 2, 7])
def test_odi_validity_code_is_saved_and_readable_without_inferred_validity(client, issue_token, code):
    from vitalis.adapters.persistence import HealthRepository, session_scope
    owner = f"synthetic-odi-code-{code}"
    daily, _ = ZeppParser.parse_wellness({"items": [{"subType": "odi", "date": "2026-09-01",
        "timestamp": 1788220800000, "odi": 3, "valid": code, "deviceId": "SYNTHETICWATCH"}]}, "wellness:spo2:user")
    codes = [row for row in daily if row.metric == "spo2_odi_validity_code"]
    assert len(codes) == 1 and codes[0].value == code
    assert codes[0].unit == "vendor_code" and codes[0].source_field == "odi.valid"
    assert codes[0].qualification == "vendor_semantics_unverified"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(owner)
        repo.save_daily_metrics([row.model_copy(update={"user_id": owner}) for row in daily])
    response = client.get("/api/health/summary/spo2?from=2026-09-01&to=2026-09-01",
        headers={"Authorization": "Bearer " + issue_token(owner, ["read"])})
    assert response.status_code == 200
    signal = response.json()["signals"]["spo2_odi_validity_code"]
    assert signal["daily"][0]["days"][0]["observations"][0]["value"] == code
    assert "trend" not in signal["daily"][0]
    assert "mean" not in signal["daily"][0]["summary"]


@pytest.mark.parametrize("value", [None, True, "unknown", -1, 1.5])
def test_missing_or_malformed_odi_validity_is_not_coerced_to_a_code(value):
    daily, _ = ZeppParser.parse_wellness({"items": [{"subType": "odi", "date": "2026-09-01",
        "timestamp": 1788220800000, "valid": value}]}, "wellness:spo2:user")
    assert not any(row.metric == "spo2_odi_validity_code" for row in daily)


@pytest.mark.parametrize("expiry", [datetime(2030, 1, 1, tzinfo=UTC), None])
def test_auth_status_exposes_only_saved_expiry_without_decryption(client, issue_token, monkeypatch, expiry):
    from vitalis.adapters import credentials
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.domain import AuthToken
    owner = "synthetic-expiry-" + str(expiry is None)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(owner)
        repo.save_token(AuthToken(user_id=owner, source="zepp", source_user_id=owner,
            access_token="synthetic-secret", expires_at=expiry))
    monkeypatch.setattr(credentials, "decrypt_token", lambda _: pytest.fail("status must not decrypt credentials"))
    response = client.get("/api/health/token-status", headers={"Authorization": "Bearer " + issue_token(owner, ["read"])})
    assert response.status_code == 200
    result = response.json()
    assert result["token_expires_at"] == ("2030-01-01T00:00:00Z" if expiry else None)
    assert "synthetic-secret" not in response.text
