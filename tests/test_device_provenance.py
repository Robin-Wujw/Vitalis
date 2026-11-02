"""Synthetic device provenance boundaries, independent of any private response.

Behavior review (no upstream source copied): lingcang728/ZeppBridge,
1012a00abbb41865e8abcfb8f59b3d264fbafe1b, normalizer/device.rs:17-70,
normalizer/band_items.rs:37-39 and connectors/zepp/mod.rs:445-463.
The upstream longest-segment heuristic is deliberately NOT adopted.
"""

import base64
from datetime import date
import json

import pytest

from vitalis.adapters.zepp.parser import ZeppParser


A = "A1B2C3D4E5F60708"
B = "B1C2D3E4F5061728"
MAC = "AABBCCDDEEFF"
DAY = date(2026, 10, 2)
START = 1_791_072_000_000
MISSING = object()


def _event(event_type, device=MISSING, **value):
    if device is not MISSING:
        value["deviceId"] = device
    return {"eventType": event_type, "timestamp": START + 60_000, "value": value}


def _charge(device=MISSING, **extra):
    return _event("Charge", device, startTime=START, samples=[
        {"s": 0, "total": 60}, {"s": 60_000, "total": 70},
    ], **extra)


@pytest.mark.parametrize("encoded", [
    f"{A},{B}", f"1,{A};1,{B}", f"2,{A};1,{B}",
    f"1,{A},unexpected", f"bogus,{A}", f"-1,{A}",
    f"0,{A}", "0.91.20.5", "设备设备设备设备", True, [A], {"id": A},
])
def test_ambiguous_or_malformed_device_id_never_selects_longest(encoded):
    assert ZeppParser._device_id({"deviceId": encoded}) is None


@pytest.mark.parametrize("encoded,expected", [
    (A, A), (f"3,{A}", A), (f"1,{A};2,{A};", A),
    ("aa:bb:cc:dd:ee:ff", MAC), ("aa-bb-cc-dd-ee-ff", MAC),
    ("aabbccddeeff", MAC), ("D8:80:3C:FF:FE:D5:44:71", "D8803CFFFED54471"),
])
def test_identifier_decoding_preserves_only_unambiguous_identity(encoded, expected):
    assert ZeppParser._device_id({"deviceId": encoded}) == expected


@pytest.mark.parametrize("value", ["0.91.20.5", "AA:BB:CC:DD:EE:ZZ", "AB CD EF GH", {"id": A}, True])
def test_normalization_does_not_turn_malformed_values_into_devices(value):
    assert ZeppParser._normalize_device_identifier(value) is None


@pytest.mark.parametrize("encoded", [
    f"0,{A};2,{B}", f"+2,{A}", f"1,{A};", f"3,{A}",
    f"1,{A},extra;1,{B}", f"1,{A};;1,{B}",
])
def test_run_length_invalid_structure_or_coverage_is_unknown(encoded):
    assert ZeppParser._expand_sample_device_ids(encoded, 2) == [None, None]


def test_run_length_keeps_slots_for_unknown_device_and_normalizes_mac():
    assert ZeppParser._expand_sample_device_ids(f"1,{A};1,app;1,aa:bb:cc:dd:ee:ff;", 3) == [A, None, MAC]


@pytest.mark.parametrize("parser_name,key", [
    ("parse_charge_samples", "total"), ("parse_hrv_samples", "sdnn"),
    ("parse_dense_file_index", "fileId"),
])
def test_sample_maps_are_applied_before_filtering_invalid_readings(parser_name, key):
    raw = {"items": [_event("Charge", f"1,{A};1,{B}", startTime=START, samples=[
        {"s": 0, key: None}, {"s": 60_000, key: "synthetic-file" if key == "fileId" else 55},
    ])]}
    rows = getattr(ZeppParser, parser_name)(raw)
    assert [(row.device_id, row.source_scope) for row in rows] == [(B, "device")]


@pytest.mark.parametrize("device,scope", [
    (MISSING, "user_summary"), (None, "user_summary"), (A, "device"),
    (f"2,{A}", "device"), ("broken,", "unknown"), (f"1,{A}", "unknown"),
    (True, "unknown"), (f"1,{A},bad;1,{B}", "unknown"),
])
def test_charge_missing_id_is_account_summary_but_malformed_map_is_unknown(device, scope):
    rows = ZeppParser.parse_charge_samples({"items": [_charge(device)]})
    assert len(rows) == 2
    assert {row.source_scope for row in rows} == {scope}


@pytest.mark.parametrize("placeholder", ["1440,app", "1,", "1,-1"])
def test_verified_bookkeeping_is_account_summary_only_for_known_account_events(placeholder):
    # ZeppBridge normalizer/tests/band.rs:123-165 at the reviewed SHA.
    charge = ZeppParser.parse_charge_samples({"items": [_charge(placeholder)]})
    daily = ZeppParser.parse_daily_metrics({"items": [_event("DailyHealth", placeholder, date=DAY.isoformat(), steps=100)]})
    assert {(row.source_scope, row.device_id) for row in [*charge, *daily]} == {("user_summary", None)}
    readiness = ZeppParser.parse_readiness_samples({"items": [_event("readiness", placeholder, timestamp=START, rdnsScore=80)]})
    assert {(row.source_scope, row.device_id) for row in readiness} == {("unknown", None)}


def test_unverified_placeholder_variants_stay_unknown_on_known_account_events():
    for placeholder in ("app", "3,app", "1440,app;1,app", "1440,appp", "1,-2"):
        rows = ZeppParser.parse_charge_samples({"items": [_charge(placeholder)]})
        assert {row.source_scope for row in rows} == {"unknown"}


def test_dynamic_app_run_is_account_only_when_count_matches_actual_samples():
    rows = ZeppParser.parse_charge_samples({"items": [_charge("2,app")]})
    assert {row.source_scope for row in rows} == {"user_summary"}
    bad = ZeppParser.parse_charge_samples({"items": [_charge("3,app")]})
    assert {row.source_scope for row in bad} == {"unknown"}


def test_explicit_fusion_is_preserved_but_never_inferred_from_missing_id():
    fused = _charge(A, is_fused=True)
    rows = ZeppParser.parse_charge_samples({"items": [fused]})
    assert {(row.source_scope, row.device_id) for row in rows} == {("user_fused", A)}
    ordinary = ZeppParser.parse_charge_samples({"items": [_charge()]})
    assert {row.source_scope for row in ordinary} == {"user_summary"}


@pytest.mark.parametrize("device,scope", [
    (MISSING, "user_summary"), (None, "user_summary"), (A, "device"),
    (f"1,{A};1,{B}", "unknown"), ("1,", "unknown"), (False, "unknown"),
])
def test_readiness_samples_daily_and_revisions_share_provenance(device, scope):
    raw = {"items": [_event("readiness", device, timestamp=START, rdnsScore=80)]}
    for rows in (ZeppParser.parse_readiness_samples(raw), ZeppParser.parse_daily_metrics(raw)):
        assert len(rows) == 1
        assert rows[0].source_scope == scope
    assert ZeppParser.parse_readiness_revisions(raw)[0]["source_scope"] == scope


def test_unknown_readiness_revision_cannot_replace_account_summary_revision():
    good = _event("readiness", timestamp=START, rdnsScore=80)
    invalid = _event("readiness", "1,", timestamp=START, rdnsScore=40)
    invalid["timestamp"] = START + 120_000
    for ordered in ([good, invalid], [invalid, good]):
        rows = ZeppParser.parse_readiness_samples({"items": ordered})
        assert {(row.source_scope, row.value) for row in rows} == {("user_summary", 80), ("unknown", 40)}


def test_daily_health_keeps_device_a_device_b_and_account_summary_separate():
    raw = {"items": [
        _event("DailyHealth", A, date=DAY.isoformat(), steps=100),
        _event("DailyHealth", B, date=DAY.isoformat(), steps=200),
        _event("DailyHealth", date=DAY.isoformat(), steps=250),
        _event("DailyHealth", "broken,", date=DAY.isoformat(), steps=300),
    ]}
    rows = ZeppParser.parse_daily_metrics(raw)
    assert {(row.device_id, row.source_scope, row.value) for row in rows} == {
        (A, "device", 100), (B, "device", 200), (None, "user_summary", 250), (None, "unknown", 300),
    }


def test_daily_health_nested_sample_mapping_does_not_broadcast_last_device():
    raw = {"items": [_event("DailyHealth", f"1,{A};1,{B}", samples=[
        {"date": DAY.isoformat(), "steps": 100}, {"date": DAY.isoformat(), "steps": 200},
    ])]}
    rows = ZeppParser.parse_daily_metrics(raw)
    assert {(row.device_id, row.source_scope, row.value) for row in rows} == {(A, "device", 100), (B, "device", 200)}


@pytest.mark.parametrize("source_key,scope", [
    (None, "unknown"), ("unknown:SPORT_LOAD", "unknown"),
    ("WatchSportStatistics:SPORT_LOAD:2026-10-02:2026-10-02", "user_summary"),
    ("WatchSportStatistics:VO2_MAX:2026-10-02:2026-10-02", "user_summary"),
    ("events:DailyHealth:summary:1:2", "user_summary"),
])
def test_generic_daily_shape_needs_verified_endpoint_to_claim_account_scope(source_key, scope):
    rows = ZeppParser.parse_daily_metrics({"items": [{"date": DAY.isoformat(), "steps": 100}]}, source_key=source_key)
    assert rows[0].source_scope == scope


@pytest.mark.parametrize("encoded", [False, True])
def test_unattributed_heart_rate_is_unknown_not_account_fused(encoded):
    item = {"generatedTime": START, "heartRateData": "SA=="} if encoded else {"timestamp": START, "heartRate": 72}
    row, = ZeppParser.parse_heart_rate_samples({"items": [item]})
    assert (row.source_scope, row.device_id) == ("unknown", None)


def _band(*, item_identity=None, summary_identity=None):
    summary = {"tz": 0, "slp": {"st": START // 1000, "ed": START // 1000 + 3600, "wk": 0,
               "odd_stage": [{"start": 2220, "stop": 2229, "mode": 4}]}, "stp": {"ttl": 100}}
    summary.update(summary_identity or {})
    return {"data": {"items": [{"date_time": DAY.isoformat(), **(item_identity or {}),
            "summary": base64.b64encode(json.dumps(summary).encode()).decode(),
            "data_hr": base64.b64encode(bytes([70, 71])).decode()}]}}


@pytest.mark.parametrize("item_identity,summary_identity,expected", [
    ({"deviceId": A}, {}, ("device", A)),
    ({}, {"sn": A}, ("device", A)),
    ({}, {}, ("unknown", None)),
    ({"deviceId": f"1,{A};1,{B}"}, {}, ("unknown", None)),
])
def test_band_sleep_activity_and_nap_keep_item_provenance(item_identity, summary_identity, expected):
    raw = _band(item_identity=item_identity, summary_identity=summary_identity)
    sleeps, activities = ZeppParser().parse_band(raw)
    for row in (sleeps[DAY], activities[DAY], *sleeps[DAY].naps):
        assert (row.source_scope, row.device_id) == expected


def test_band_minute_device_map_applies_before_invalid_heart_rate_filtering():
    raw = _band(item_identity={"deviceId": f"1,{A};1,{B}"})
    raw["data"]["items"][0]["data_hr"] = base64.b64encode(bytes([0, 72])).decode()
    rows = ZeppParser.parse_band_heart_rate(raw)
    assert [(row.device_id, row.source_scope) for row in rows] == [(B, "device")]


def test_band_does_not_attach_another_devices_naps_to_main_sleep():
    first = _band(item_identity={"deviceId": A})["data"]["items"][0]
    second = _band(item_identity={"deviceId": B})["data"]["items"][0]
    summary = json.loads(base64.b64decode(second["summary"]))
    summary["slp"].update(st=0, ed=0)
    second["summary"] = base64.b64encode(json.dumps(summary).encode()).decode()
    sleep = ZeppParser().parse_band({"data": {"items": [first, second]}})[0][DAY]
    assert sleep.device_id == A
    assert {nap.device_id for nap in sleep.naps} == {A}


@pytest.mark.parametrize("label,payload", [
    ("lactate_threshold", {"value": {"samples": [{"dateString": DAY.isoformat(), "lactateThresholdHr": 170}]}}),
    ("all_day_stress", {"date": DAY.isoformat(), "avgStress": 30}),
])
def test_wellness_unattributed_is_unknown_and_explicit_fusion_is_preserved(label, payload):
    rows, _ = ZeppParser.parse_wellness({"items": [payload]}, f"wellness:{label}:v2:1:2")
    assert {row.source_scope for row in rows} == {"unknown"}
    payload["is_fused"] = True
    rows, _ = ZeppParser.parse_wellness({"items": [payload]}, f"wellness:{label}:v2:1:2")
    assert {row.source_scope for row in rows} == {"user_fused"}


@pytest.mark.parametrize("mark", [{"isFused": True}, {"source_scope": "user_fused"}, {"sourceScope": "USER_FUSED"}])
def test_explicit_fusion_aliases_are_not_lost(mark):
    row, = ZeppParser.parse_heart_rate_samples({"items": [{"timestamp": START, "heartRate": 72, **mark}]})
    assert row.source_scope == "user_fused"


def test_inventory_aliases_only_join_identifiers_present_on_same_physical_item():
    raw = {"items": [{"deviceId": A, "macAddress": "aa:bb:cc:dd:ee:ff", "additionalInfo": json.dumps({
        "btmac": "aa-bb-cc-dd-ee-ff", "sn": "12345678901234", "serial": "0.91.20.5", "auth_key": "not-an-alias",
    })}, {"deviceId": B}]}
    first, second = ZeppParser().parse_devices(raw)
    assert first.device_id == MAC
    assert set(first.aliases) == {A, MAC, "12345678901234"}
    assert second.device_id == B and second.aliases == [B]
    assert "not-an-alias" not in repr([first, second])
