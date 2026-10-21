"""Offline contract for the observed, evidence-backed Zepp action labels."""

from copy import deepcopy
from importlib import resources
import json

import pytest

from vitalis.adapters.zepp.catalog import (
    LAP_62_NAMESPACE,
    load_catalog,
    parse_catalog,
    resolve_exercise,
    verified_lap_labels,
)


@pytest.fixture
def catalog_payload():
    text = resources.files("vitalis.adapters.zepp").joinpath(
        "data", "strength_exercises.json",
    ).read_text(encoding="utf-8")
    return json.loads(text)


def test_catalog_resource_covers_complete_official_display_catalog(monkeypatch, tmp_path, catalog_payload):
    monkeypatch.chdir(tmp_path)
    catalog = load_catalog()
    assert catalog.catalog_revision == catalog_payload["catalog_revision"]
    assert len(catalog.entries) == 1133
    assert catalog_payload["source_entry_count"] == 1134
    assert catalog_payload["placeholders"] == [{"vendor_code": 0, "label": "选择一项训练"}]
    assert verified_lap_labels()[1770] == "坐姿杠铃劲前推肩"
    assert verified_lap_labels()[3] == "杠铃深蹲"
    assert verified_lap_labels()[114] == "蝴蝶机反向飞鸟"
    for record in catalog_payload["entries"]:
        code = record["vendor_code"]
        assert record["namespace"] == LAP_62_NAMESPACE
        if code == 3:
            assert record["canonical_exercise_id"] == "barbell_squat"
        assert record["verification"] == "verified"
        evidence = catalog.entries[(LAP_62_NAMESPACE, code)].provenance
        assert evidence["app_version"] == "10.8.7-play"
        assert evidence["artifact_digest"] == (
            "64e1d87b6ab79e0ecedfbead176cc6bb781f9754b46915d52c0d2ccfbcef5949"
        )
        assert evidence["catalog_response_digest"] == (
            "7e6b4ba617bd8e61b0c269c9c51fb4d7b430c0e30332273a964fa88f8028abeb"
        )
        assert record["provenance"]["catalog_reference"].endswith(f"actionType={code}")
        assert resolve_exercise(LAP_62_NAMESPACE, code).label_zh == record["labels"]["zh-CN"]


def test_unknown_codes_and_other_numeric_namespaces_are_unmapped():
    for namespace, code in (
        (LAP_62_NAMESPACE, 999999),
        ("zepp.strength.action_type", 64),
        ("zepp.sport.type", 64),
        (LAP_62_NAMESPACE, True),
    ):
        result = resolve_exercise(namespace, code)
        assert result.namespace == namespace
        assert result.vendor_code == code
        assert result.status == "unmapped"
        assert result.label_zh is None
        assert result.canonical_exercise_id is None
        assert result.self_weight_type is None


def test_catalog_preserves_verified_self_weight_type():
    assert resolve_exercise(LAP_62_NAMESPACE, 64).self_weight_type == 1
    assert resolve_exercise(LAP_62_NAMESPACE, 3).self_weight_type == 0
    assert all(entry.self_weight_type in {0, 1} for entry in load_catalog().entries.values())


@pytest.mark.parametrize("value", [True, "1", -1, 2, 1.5])
def test_catalog_rejects_invalid_self_weight_type(catalog_payload, value):
    catalog_payload["entries"][0]["self_weight_type"] = value
    with pytest.raises(ValueError, match="self_weight_type"):
        parse_catalog(json.dumps(catalog_payload))


def test_provisional_bodyweight_metadata_cannot_qualify_an_observation(catalog_payload):
    catalog_payload["entries"][0].update(verification="provisional", self_weight_type=1)
    parsed = parse_catalog(json.dumps(catalog_payload))
    result = parsed.resolve(LAP_62_NAMESPACE, catalog_payload["entries"][0]["vendor_code"])
    assert result.self_weight_type is None


def test_provisional_entry_is_visible_as_uncertain_without_exposing_its_label(catalog_payload):
    provisional = deepcopy(catalog_payload["entries"][0])
    provisional.update(namespace="zepp.strength.action_type", verification="provisional")
    provisional["labels"] = {"zh-CN": "未核实动作"}
    provisional["provenance"] = {}
    catalog_payload["entries"].append(provisional)
    catalog_payload["source_entry_count"] += 1
    catalog = parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))

    result = catalog.resolve("zepp.strength.action_type", provisional["vendor_code"])
    assert result.status == "provisional"
    assert result.vendor_code == provisional["vendor_code"]
    assert result.label_zh is None
    assert result.labels is None
    assert catalog.resolve(LAP_62_NAMESPACE, provisional["vendor_code"]).status == "verified"
    provisional["provenance"] = {"catalog_reference": " "}
    with pytest.raises(ValueError, match="invalid provenance"):
        parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))


def test_duplicate_namespaced_key_is_rejected(catalog_payload):
    catalog_payload["entries"].append(deepcopy(catalog_payload["entries"][0]))
    catalog_payload["source_entry_count"] += 1
    with pytest.raises(ValueError, match="duplicate catalog key"):
        parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))


def test_duplicate_json_members_are_rejected():
    with pytest.raises(ValueError, match="duplicate JSON member"):
        parse_catalog('{"catalog_revision":"a","catalog_revision":"b","entries":[]}')


@pytest.mark.parametrize("field", (
    "app_version", "artifact_digest", "catalog_response_digest",
    "extraction_reference", "catalog_reference",
))
def test_verified_entry_requires_each_evidence_field(catalog_payload, field):
    evidence = (catalog_payload["entries"][0]["provenance"]
                if field == "catalog_reference" else catalog_payload["provenance"])
    del evidence[field]
    with pytest.raises(ValueError, match="evidence"):
        parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))


@pytest.mark.parametrize("field", ("artifact_digest", "catalog_response_digest"))
def test_invalid_digest_is_never_verified(catalog_payload, field):
    catalog_payload["provenance"][field] = "unverifiable"
    with pytest.raises(ValueError, match="invalid .*digest"):
        parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))


def test_empty_evidence_and_invalid_verification_cannot_supply_names(catalog_payload):
    catalog_payload["entries"][0]["provenance"]["catalog_reference"] = " "
    with pytest.raises(ValueError, match="invalid provenance"):
        parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))
    catalog_payload["entries"][0]["verification"] = []
    with pytest.raises(ValueError, match="verification"):
        parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))


@pytest.mark.parametrize("code", (True, 0, "64", -1, 2**31))
def test_catalog_rejects_non_integer_or_out_of_range_codes(catalog_payload, code):
    catalog_payload["entries"][0]["vendor_code"] = code
    with pytest.raises(ValueError, match="vendor_code"):
        parse_catalog(json.dumps(catalog_payload, ensure_ascii=False))
