"""APK-backed sport modes retain the device/cloud crosswalk and provenance."""

from copy import deepcopy
from importlib import resources
import json

import pytest

from vitalis.adapters.zepp.sport_types import (
    CLOUD_SPORT_NAMESPACE,
    DEVICE_SPORT_NAMESPACE,
    load_sport_catalog,
    parse_sport_catalog,
    resolve_cloud_sport_mode,
    resolve_sport_mode,
)


@pytest.fixture
def catalog_payload():
    return json.loads(resources.files("vitalis.adapters.zepp").joinpath(
        "data", "sport_modes.json",
    ).read_text(encoding="utf-8"))


def test_complete_apk_sport_catalog_has_auditable_source_coverage(catalog_payload):
    catalog = load_sport_catalog()
    assert catalog.app_version == "10.8.7-play"
    assert catalog.artifact_digest == "322c37d71a525db026975a60af7852c063218804a5b3258e91299499122acbcf"
    assert catalog_payload["source_entry_counts"] == {
        "assets/config_sport_type_group.json": 189,
        "assets/device_sport_categories.json": 208,
        "assets/sport_categories.json": 209,
    }
    assert len(catalog.device_modes) == len(catalog.cloud_modes) == 208
    assert len(catalog_payload["entries"]) == 208
    assert catalog_payload["placeholders"][0]["device_code"] == -1
    for entry in catalog_payload["entries"]:
        device = resolve_sport_mode(entry["device_code"])
        cloud = resolve_cloud_sport_mode(entry["cloud_code"])
        assert device.namespace == DEVICE_SPORT_NAMESPACE
        assert cloud.namespace == CLOUD_SPORT_NAMESPACE
        assert device.code == cloud.code == entry["canonical_code"]
        assert device.label_zh == cloud.label_zh == entry["display_label_zh"]
        assert device.recognition_confidence == cloud.recognition_confidence == "HIGH"


@pytest.mark.parametrize("device_code,cloud_code,label", [
    (2, 8, "跑步机"), (3, 6, "健走"), (4, 9, "户外骑行"),
    (5, 16, "自由训练"), (6, 14, "泳池游泳"), (7, 15, "公开水域游泳"),
    (8, 10, "室内骑行"), (9, 12, "椭圆机"), (10, 13, "攀登"),
    (11, 7, "越野跑"), (15, 22, "徒步"), (52, 52, "力量训练"),
])
def test_apk_device_id_and_cloud_type_are_distinct(device_code, cloud_code, label):
    assert resolve_sport_mode(device_code).label_zh == label
    assert resolve_cloud_sport_mode(cloud_code).label_zh == label


def test_cloud_does_not_fall_back_to_device_codes_or_resource_keys():
    assert resolve_sport_mode(5).code == "free_training"
    assert resolve_cloud_sport_mode(5).recognition_confidence == "NONE"
    assert resolve_cloud_sport_mode(2).recognition_confidence == "NONE"
    assert resolve_cloud_sport_mode(15).code == "open_water_swimming"
    assert resolve_sport_mode(149).code == resolve_cloud_sport_mode(149).code == "horizontal_bar"


@pytest.mark.parametrize("code", [-1, 0, 999999, None, True, 1.5, "5"])
def test_missing_unknown_and_placeholder_codes_remain_unrecognized(code):
    for resolve in (resolve_sport_mode, resolve_cloud_sport_mode):
        mode = resolve(code)
        assert mode.recognition_confidence == "NONE"
        assert mode.category == "other"


def test_new_label_does_not_infer_training_family(catalog_payload):
    entry = next(row for row in catalog_payload["entries"] if row["device_code"] == 219)
    assert entry["labels"]["en"] == "Skierg"
    assert entry["classification_source"] == "unclassified"
    mode = resolve_cloud_sport_mode(219)
    assert mode.recognition_confidence == "HIGH"
    assert mode.category == "other"
    assert mode.family == "unknown"


@pytest.mark.parametrize("field", ["device_code", "cloud_code"])
def test_duplicate_namespaced_codes_are_rejected(catalog_payload, field):
    payload = deepcopy(catalog_payload)
    payload["entries"][1][field] = payload["entries"][0][field]
    with pytest.raises(ValueError, match="duplicate"):
        parse_sport_catalog(json.dumps(payload))


def test_incomplete_extraction_and_invalid_provenance_are_rejected(catalog_payload):
    payload = deepcopy(catalog_payload)
    payload["entries"].pop()
    with pytest.raises(ValueError, match="coverage"):
        parse_sport_catalog(json.dumps(payload))
    payload = deepcopy(catalog_payload)
    payload["provenance"]["artifact_digest"] = "unverified"
    with pytest.raises(ValueError, match="digest"):
        parse_sport_catalog(json.dumps(payload))


def test_unknown_classification_cannot_be_promoted_by_a_name(catalog_payload):
    payload = deepcopy(catalog_payload)
    entry = next(row for row in payload["entries"] if row["device_code"] == 219)
    entry["training_family"] = "strength"
    with pytest.raises(ValueError, match="unclassified"):
        parse_sport_catalog(json.dumps(payload))
