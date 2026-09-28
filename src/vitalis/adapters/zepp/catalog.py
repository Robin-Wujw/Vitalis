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
    "extraction_reference", "observation_reference",
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
        if not isinstance(labels, dict) or not labels or any(
            locale not in {"zh-CN", "en"} or not _nonempty_text(label)
            for locale, label in labels.items()
        ):
            raise ValueError(f"entry {index} needs nonempty supported labels")
        provenance = record.get("provenance")
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
            raise ValueError(f"entry {index} needs app, catalog and observation evidence")
        entries[key] = CatalogEntry(
            namespace, code, canonical_id,
            MappingProxyType(dict(labels)), verification,
            MappingProxyType(dict(provenance)),
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
