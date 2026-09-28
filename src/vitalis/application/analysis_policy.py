"""Stable, non-secret fingerprint of inputs that change analysis semantics."""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from importlib import resources
import json


def analysis_policy_digest(
    timezone_name: str,
    intelligence_version: str,
    decision_policy_version: str,
    evidence_version: str,
    catalog_revision: str,
    rules_digest: str,
) -> str:
    payload = {
        "timezone": timezone_name,
        "intelligence": intelligence_version,
        "decision": decision_policy_version,
        "evidence": evidence_version,
        "catalog": catalog_revision,
        "rules": rules_digest,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=1)
def installed_analysis_rules_digest() -> str:
    """Fingerprint the shipped Python rules and time interpretation, not secrets."""
    digest = sha256()
    for package in ("vitalis.intelligence", "vitalis.domain", "vitalis.application"):
        root = resources.files(package)
        for file in sorted(root.rglob("*.py")):
            relative = file.relative_to(root).as_posix()
            digest.update(f"{package}/{relative}\0".encode())
            digest.update(file.read_bytes())
    for package, name in (
        ("vitalis", "time.py"),
        ("vitalis.adapters.zepp", "catalog.py"),
        ("vitalis.adapters.zepp", "data/strength_exercises.json"),
    ):
        digest.update(f"{package}/{name}\0".encode())
        digest.update(resources.files(package).joinpath(name).read_bytes())
    return digest.hexdigest()
