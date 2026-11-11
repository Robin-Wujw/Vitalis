"""Synthetic protocol fixtures for MCP health parity; no personal captures."""
import base64
import json
from datetime import datetime, timezone

from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.adapters.zepp.streams import WELLNESS_STREAMS

STAMP = datetime(2026, 1, 3, tzinfo=timezone.utc)


def test_scale_extras_keep_ambiguous_units_and_bad_bmi():
    payload = {"items": [{"memberId": -1, "generatedTime": STAMP.timestamp(),
        "summary": {"weight": 72, "height": 180, "bmi": 90, "muscleRate": 32,
            "fatRate": 22, "bodyWaterRate": 55, "boneMass": 3, "metabolism": 1500,
            "muscleAge": 28, "proteinRatio": 16, "standBodyWeight": 70,
            "visceralFat": 7, "bodyScore": 85, "bodyStyle": 4,
            "bodyBalanceScore": 80, "oneFootMeasureTime": 20,
            "encryptImpedance": "451", "age": 30}}]}
    _, rows = ZeppParser.parse_wellness(payload, "wellness:weight:weight:synthetic")
    fields = {r.metric: r for r in rows}
    assert "bmi" not in fields
    assert fields["vendor_bmi"].value == 90
    assert fields["vendor_bmi"].qualification == "inconsistent_record"
    assert fields["muscle_rate"].unit == "unknown"
    assert fields["body_fat_percent"].value == 22
    assert fields["body_fat_percent"].qualification == "community_schema_inconsistent_record"
    assert fields["impedance"].source_field == "summary.encryptImpedance"
    assert len(fields) == 18


def test_single_stress_has_its_own_stream_and_preserves_source():
    assert any(s.label == "single_stress" for s in WELLNESS_STREAMS)
    _, rows = ZeppParser.parse_wellness({"items": [
        {"timestamp": STAMP.timestamp() * 1000, "value": 61, "deviceSource": "synthetic-watch"},
        {"timestamp": STAMP.timestamp() * 1000, "value": 255},
    ]}, "wellness:single_stress:user:synthetic")
    assert len(rows) == 1
    assert (rows[0].metric, rows[0].value, rows[0].unit) == ("single_stress", 61, "score")
    assert rows[0].timestamp == STAMP
    assert rows[0].source_field == "single_stress.value"


def test_respiratory_minutes_keep_gaps_and_timestamp_anchor():
    payload = {"items": [{"timestamp": STAMP.timestamp() * 1000,
        "value": {"measurements": base64.b64encode(bytes([0, 12, 255, 18])).decode()}}]}
    _, rows = ZeppParser.parse_wellness(payload, "wellness:respiratory_rate:v2:synthetic")
    assert [r.value for r in rows] == [12, 18]
    assert [r.timestamp.minute for r in rows] == [1, 3]
    assert all(r.unit == "brpm" for r in rows)


def test_band_minutes_require_day_and_stage_reconciliation():
    from vitalis.adapters.zepp.parsers.activity import band_activity
    raw = bytes([0, 0, 1]) * 1440
    summary = {"tz": 28800, "stp": {"ttl": 1440, "stage": [
        {"start": 0, "stop": 59, "step": 60, "dis": 40, "mode": 3}]}}
    result = band_activity({"data": base64.b64encode(raw).decode()}, summary)
    assert result["minute_steps"][0] == 1
    assert result["hourly_steps"] == [60] * 24
    assert result["qualification"] == "reconciled_day_and_stages"
    summary["stp"]["ttl"] = 999
    unknown = band_activity({"data": base64.b64encode(raw).decode()}, summary)
    assert unknown["minute_steps"] is None
    assert unknown["qualification"] == "unverified_total_mismatch"
    assert unknown["activity_periods"][0]["mode_code"] == 3


def test_profile_and_device_projection_never_include_pairing_secrets():
    from vitalis.adapters.zepp.parsers.identity import profile, devices
    result = profile({"data": {"height": 180, "weight_float": 72,
        "birthday": "1996-03", "gender": 1, "config": json.dumps({"wearHand": "left"}),
        "nickname": "private", "token": "secret"}})
    assert result["height_cm"] == 180
    assert result["birth_month"] == "1996-03"
    assert result["gender_code_unverified"] == 1
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)
    result = devices({"items": [{"deviceSource": "synthetic-watch", "firmwareVersion": "1.2",
        "additionalInfo": {"auth_key": "secret"}, "mac": "private"}]})
    assert result == [{"deviceSource": "synthetic-watch", "firmwareVersion": "1.2"}]


def test_raw_request_rejects_host_identity_and_credential_overrides():
    import pytest
    from vitalis.application.vendor_request import validate_raw_request
    for path, params in [("//evil.test/data", {}), ("/users/another/events", {}),
        ("/v2/users/me/events?token=x", {}), ("/%2e%2e/secret", {}),
        ("/events", {"userId": "another"}), ("/events", {"apptoken": "secret"})]:
        with pytest.raises(ValueError):
            validate_raw_request({"path": path, "params": params})
    assert validate_raw_request({"path": "/v2/users/me/events",
        "params": {"eventType": "Food", "limit": 1}})["params"]["eventType"] == "Food"
    assert validate_raw_request({"path": "/users/me/members/-1/weightRecords"})["path"].endswith("weightRecords")


def test_raw_result_masks_credentials_recursively():
    from vitalis.application.vendor_request import public_raw_payload
    result = public_raw_payload({"items": [{"value": 12, "additionalInfo": {"auth_key": "secret"},
        "refreshToken": "secret", "nested": [{"password": "secret", "score": 80}]}]})
    assert "secret" not in json.dumps(result)
    assert result["items"][0]["nested"][0]["score"] == 80
