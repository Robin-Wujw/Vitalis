"""Evidence-checked, display-only Zepp strength action catalog."""

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
import json
import re
from types import MappingProxyType
from typing import Literal, Mapping


LAP_62_NAMESPACE = "zepp.strength.lap_62"
_VERIFICATION = {"verified", "provisional"}
_EVIDENCE_FIELDS = (
    "app_version", "artifact_digest", "catalog_response_digest",
    "extraction_reference", "catalog_reference",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NAMESPACE = re.compile(r"zepp(?:\.[a-z][a-z0-9_]*){2,}\Z")


@dataclass(frozen=True)
class ExerciseResolution:
    namespace: str
    vendor_code: int
    status: Literal["verified", "provisional", "unmapped"]
    canonical_exercise_id: str | None = None
    labels: Mapping[str, str] | None = None
    self_weight_type: int | None = None
    primary_regions: tuple[int, ...] = ()
    secondary_regions: tuple[int, ...] = ()

    @property
    def label_zh(self) -> str | None:
        return self.labels.get("zh-CN") if self.labels is not None else None


@dataclass(frozen=True)
class CatalogEntry:
    namespace: str
    vendor_code: int
    canonical_exercise_id: str | None
    labels: Mapping[str, str]
    verification: Literal["verified", "provisional"]
    provenance: Mapping[str, str]
    self_weight_type: int | None = None
    primary_regions: tuple[int, ...] = ()
    secondary_regions: tuple[int, ...] = ()


@dataclass(frozen=True)
class StrengthCatalog:
    catalog_revision: str
    entries: Mapping[tuple[str, int], CatalogEntry]

    def resolve(self, namespace: str, vendor_code: int) -> ExerciseResolution:
        entry = self.entries.get((namespace, vendor_code)) if type(vendor_code) is int else None
        if entry is None:
            return ExerciseResolution(namespace, vendor_code, "unmapped")
        if entry.verification == "provisional":
            return ExerciseResolution(namespace, vendor_code, "provisional")
        return ExerciseResolution(
            namespace, vendor_code, "verified", entry.canonical_exercise_id, entry.labels,
            entry.self_weight_type,
            entry.primary_regions, entry.secondary_regions,
        )


def _unique_members(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_catalog(text: str) -> StrengthCatalog:
    """Validate an asset before any of its labels can be used."""
    payload = json.loads(text, object_pairs_hook=_unique_members)
    if not isinstance(payload, dict) or not _nonempty_text(payload.get("catalog_revision")):
        raise ValueError("catalog_revision is required")
    records = payload.get("entries")
    if not isinstance(records, list):
        raise ValueError("entries must be a list")
    shared = payload.get("provenance", {})
    if not isinstance(shared, dict):
        raise ValueError("invalid shared provenance")
    if "source_entry_count" in payload:
        placeholders = payload.get("placeholders")
        if (type(payload["source_entry_count"]) is not int
            or placeholders != [{"vendor_code": 0, "label": "选择一项训练"}]
            or payload["source_entry_count"] != len(records) + len(placeholders)):
            raise ValueError("incomplete source catalog coverage")

    entries: dict[tuple[str, int], CatalogEntry] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"entry {index} must be an object")
        namespace = record.get("namespace")
        code = record.get("vendor_code")
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
            raise ValueError(f"entry {index} needs a namespaced Zepp key")
        if type(code) is not int or not 1 <= code < 2**31:
            raise ValueError(f"entry {index} needs a positive integer vendor_code")
        key = namespace, code
        if key in entries:
            raise ValueError(f"duplicate catalog key: {namespace}:{code}")
        verification = record.get("verification")
        if not isinstance(verification, str) or verification not in _VERIFICATION:
            raise ValueError(f"entry {index} needs verified or provisional verification")
        if "canonical_exercise_id" not in record:
            raise ValueError(f"entry {index} needs canonical_exercise_id (null if unknown)")
        canonical_id = record["canonical_exercise_id"]
        if canonical_id is not None and not _nonempty_text(canonical_id):
            raise ValueError(f"entry {index} has invalid canonical_exercise_id")
        labels = record.get("labels")
        self_weight_type = record.get("self_weight_type")
        region_arrays = []
        for field in ("primary_regions", "secondary_regions"):
            if verification == 'verified' and field not in record:
                raise ValueError(f"entry {index} needs {field} evidence")
            values = record.get(field, [])
            if not isinstance(values, list) or any(type(v) is not int or v not in set(range(1, 50)) - {9, 18, 21} for v in values):
                raise ValueError(f"entry {index} has invalid {field}")
            region_arrays.append(tuple(values))
        if self_weight_type is not None and (
            type(self_weight_type) is not int or self_weight_type not in {0, 1}
        ):
            raise ValueError(f"entry {index} has invalid self_weight_type")
        if not isinstance(labels, dict) or not labels or any(
            locale not in {"zh-CN", "en"} or not _nonempty_text(label)
            for locale, label in labels.items()
        ):
            raise ValueError(f"entry {index} needs nonempty supported labels")
        local_provenance = record.get("provenance")
        if not isinstance(local_provenance, dict):
            raise ValueError(f"entry {index} has invalid provenance")
        if shared.keys() & local_provenance.keys():
            raise ValueError(f"entry {index} overrides shared provenance")
        provenance = shared | local_provenance
        if not isinstance(provenance, dict) or any(
            field not in _EVIDENCE_FIELDS or not _nonempty_text(value)
            for field, value in provenance.items()
        ):
            raise ValueError(f"entry {index} has invalid provenance")
        for field in ("artifact_digest", "catalog_response_digest"):
            if field in provenance and not _DIGEST.fullmatch(provenance[field]):
                raise ValueError(f"entry {index} has invalid {field}")
        if verification == "verified" and any(
            field not in provenance for field in _EVIDENCE_FIELDS
        ):
            raise ValueError(f"entry {index} needs app and catalog evidence")
        entries[key] = CatalogEntry(
            namespace, code, canonical_id,
            MappingProxyType(dict(labels)), verification,
            MappingProxyType(dict(provenance)),
            self_weight_type,
            *region_arrays,
        )
    return StrengthCatalog(payload["catalog_revision"], MappingProxyType(entries))


@lru_cache(maxsize=1)
def load_catalog() -> StrengthCatalog:
    text = resources.files("vitalis.adapters.zepp").joinpath(
        "data", "strength_exercises.json",
    ).read_text(encoding="utf-8")
    return parse_catalog(text)


def resolve_exercise(namespace: str, vendor_code: int) -> ExerciseResolution:
    return load_catalog().resolve(namespace, vendor_code)


def verified_lap_labels() -> dict[int, str]:
    return {
        code: entry.labels["zh-CN"]
        for (namespace, code), entry in load_catalog().entries.items()
        if namespace == LAP_62_NAMESPACE
        and entry.verification == "verified"
        and "zh-CN" in entry.labels
    }
