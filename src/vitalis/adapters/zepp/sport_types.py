"""APK-backed Zepp sport catalog with separate device and cloud namespaces."""

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
import json
import re
from types import MappingProxyType
from typing import Mapping


DEVICE_SPORT_NAMESPACE = "zepp.sport.device"
CLOUD_SPORT_NAMESPACE = "zepp.sport.cloud_history"
_ASSETS = frozenset({
    "assets/config_sport_type_group.json",
    "assets/device_sport_categories.json",
    "assets/sport_categories.json",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True)
class SportMode:
    code: str
    label_zh: str
    category: str
    family: str
    recognition_confidence: str = "HIGH"
    recognition_confidence_label: str = "较高"
    recognition_source: str = "zepp_apk_device_catalog"
    recognition_source_label: str = "Zepp APK 设备运动目录"
    namespace: str = DEVICE_SPORT_NAMESPACE
    vendor_code: int | None = None
    catalog_revision: str | None = None


@dataclass(frozen=True)
class SportCatalog:
    catalog_revision: str
    app_version: str
    artifact_digest: str
    resource_digests: Mapping[str, str]
    device_modes: Mapping[int, SportMode]
    cloud_modes: Mapping[int, SportMode]


CATEGORY_LABELS = {
    "strength": "力量训练", "running": "跑步", "cycling": "骑行",
    "swimming": "游泳", "walking": "步行与徒步", "hiit": "高强度间歇训练",
    "yoga": "瑜伽与灵活性训练", "other": "其他运动",
}
FAMILY_LABELS = {
    "aerobic": "有氧", "strength": "力量", "mobility": "灵活性与恢复",
    "mixed": "混合体能", "skill": "技巧与休闲", "unknown": "未知",
}


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unique_members(pairs: list[tuple[str, object]]) -> dict:
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON member: {key}")
        output[key] = value
    return output


def parse_sport_catalog(text: str) -> SportCatalog:
    """Reject incomplete extraction, ambiguous codes, or unsupported evidence."""
    payload = json.loads(text, object_pairs_hook=_unique_members)
    if not isinstance(payload, dict) or not _text(payload.get("catalog_revision")):
        raise ValueError("sport catalog revision is required")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or not _text(provenance.get("app_version")):
        raise ValueError("sport catalog app_version is required")
    artifact = provenance.get("artifact_digest")
    digests = provenance.get("resources")
    if not isinstance(artifact, str) or not _DIGEST.fullmatch(artifact):
        raise ValueError("sport catalog artifact digest is invalid")
    if not isinstance(digests, dict) or set(digests) != _ASSETS or any(
        not isinstance(value, str) or not _DIGEST.fullmatch(value)
        for value in digests.values()
    ):
        raise ValueError("sport catalog resource digest is invalid")
    counts = payload.get("source_entry_counts")
    if not isinstance(counts, dict) or set(counts) != _ASSETS or any(
        type(value) is not int or value < 1 for value in counts.values()
    ):
        raise ValueError("sport catalog source coverage is invalid")
    entries, placeholders = payload.get("entries"), payload.get("placeholders")
    if not isinstance(entries, list) or not entries or not isinstance(placeholders, list):
        raise ValueError("sport catalog entries and placeholders are required")
    coverage = Counter()
    device_modes, cloud_modes = {}, {}
    revision, version = payload["catalog_revision"], provenance["app_version"]
    for entry in entries + placeholders:
        if not isinstance(entry, dict):
            raise ValueError("sport catalog entry must be an object")
        sources = entry.get("sources")
        if (not isinstance(sources, list) or not sources
            or any(not isinstance(source, str) or source not in _ASSETS for source in sources)
            or len(sources) != len(set(sources))):
            raise ValueError("sport catalog entry source coverage is invalid")
        coverage.update(sources)
        device, cloud = entry.get("device_code"), entry.get("cloud_code")
        if any(type(value) is not int for value in (device, cloud)):
            raise ValueError("sport codes must be integers")
        if entry.get("status") == "placeholder":
            if device >= 0 or cloud >= 0 or entry not in placeholders:
                raise ValueError("sport placeholder must have negative codes")
            continue
        if entry in placeholders or any(not 0 < value < 2**31 for value in (device, cloud)):
            raise ValueError("sport codes must be positive; placeholders stay separate")
        if device in device_modes or cloud in cloud_modes:
            raise ValueError("duplicate namespaced sport code")
        labels = entry.get("labels")
        if not isinstance(labels, dict) or not labels or any(
            locale not in {"en", "zh-CN"} or not _text(label)
            for locale, label in labels.items()
        ):
            raise ValueError("sport catalog labels are invalid")
        canonical, display = entry.get("canonical_code"), entry.get("display_label_zh")
        if not isinstance(canonical, str) or not _CODE.fullmatch(canonical) or not _text(display):
            raise ValueError("sport canonical code and display label are required")
        category, family = entry.get("category"), entry.get("training_family")
        if category not in CATEGORY_LABELS or family not in FAMILY_LABELS:
            raise ValueError("sport category or training family is invalid")
        classification = entry.get("classification_source")
        if classification == "unclassified":
            if category != "other" or family != "unknown":
                raise ValueError("unclassified sport must not infer training facts")
        elif classification != "reviewed_vitalis_mapping":
            raise ValueError("sport classification evidence is required")
        for namespace, vendor, modes, source, source_label in (
            (DEVICE_SPORT_NAMESPACE, device, device_modes, "zepp_apk_device_catalog", "设备编号"),
            (CLOUD_SPORT_NAMESPACE, cloud, cloud_modes, "zepp_apk_cloud_catalog", "云端类型"),
        ):
            modes[vendor] = SportMode(
                code=canonical, label_zh=display, category=category, family=family,
                recognition_source=source,
                recognition_source_label=f"Zepp APK {version} 运动目录（{source_label}）",
                namespace=namespace, vendor_code=vendor, catalog_revision=revision,
            )
    if dict(coverage) != counts:
        raise ValueError("sport catalog source coverage is incomplete")
    return SportCatalog(
        revision, version, artifact, MappingProxyType(dict(digests)),
        MappingProxyType(device_modes), MappingProxyType(cloud_modes),
    )


@lru_cache(maxsize=1)
def load_sport_catalog() -> SportCatalog:
    text = resources.files("vitalis.adapters.zepp").joinpath(
        "data", "sport_modes.json",
    ).read_text(encoding="utf-8")
    return parse_sport_catalog(text)


ZEPP_SPORT_MODES = load_sport_catalog().device_modes
CLOUD_SPORT_MODES = load_sport_catalog().cloud_modes


def _unknown_sport_mode(vendor_type_id: object, namespace: str) -> SportMode:
    code = vendor_type_id if type(vendor_type_id) is int else None
    return SportMode(
        code=f"unknown_{code}" if code is not None else "unknown",
        label_zh=f"未知运动（编号 {code}）" if code is not None else "未知运动",
        category="other", family="skill", recognition_confidence="NONE",
        recognition_confidence_label="无法识别",
        recognition_source="unknown_vendor_code" if code is not None else "missing_vendor_type",
        recognition_source_label="厂商编号未核验" if code is not None else "缺少厂商运动类型",
        namespace=namespace, vendor_code=code,
    )


def resolve_sport_mode(vendor_type_id: int | None) -> SportMode:
    if type(vendor_type_id) is int and vendor_type_id in ZEPP_SPORT_MODES:
        return ZEPP_SPORT_MODES[vendor_type_id]
    return _unknown_sport_mode(vendor_type_id, DEVICE_SPORT_NAMESPACE)


def resolve_cloud_sport_mode(vendor_type_id: int | None) -> SportMode:
    if type(vendor_type_id) is int and vendor_type_id in CLOUD_SPORT_MODES:
        return CLOUD_SPORT_MODES[vendor_type_id]
    return _unknown_sport_mode(vendor_type_id, CLOUD_SPORT_NAMESPACE)
