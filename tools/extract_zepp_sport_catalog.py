"""Extract the complete device/cloud sport crosswalk from local Zepp APK assets.

Only public catalog fields and artifact digests are emitted. The APK stays outside
the source tree; existing reviewed display/classification metadata is retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ASSETS = (
    "config_sport_type_group.json",
    "device_sport_categories.json",
    "sport_categories.json",
)
_METADATA = (
    "canonical_code", "display_label_zh", "category", "training_family",
    "classification_source",
)


def _integer(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.lstrip("-").isdigit():
        return int(value)
    raise ValueError("sport id/type must be an integer")


def extract_catalog(
    assets: Path, *, app_version: str, artifact_digest: str,
    metadata: dict | None = None,
) -> dict:
    """Merge every readable entry, retaining both namespaces and placeholders."""
    merged: dict[int, dict] = {}
    digests, counts = {}, {}
    for filename in ASSETS:
        content = (assets / filename).read_bytes()
        source = f"assets/{filename}"
        digests[source] = hashlib.sha256(content).hexdigest()
        payload = json.loads(content)
        localized = filename == ASSETS[0]
        rows = payload if localized else [
            dict(row, asset_category=group["category"])
            for group in payload for row in group["items"]
        ]
        counts[source] = len(rows)
        seen_ids, seen_types = set(), set()
        for row in rows:
            device, cloud = _integer(row["id"]), _integer(row["type"])
            if device in seen_ids or cloud in seen_types:
                raise ValueError(f"duplicate sport id/type in {source}")
            seen_ids.add(device)
            seen_types.add(cloud)
            label = row["name"]
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"empty sport label in {source}")
            entry = merged.setdefault(device, {
                "device_code": device, "cloud_code": cloud, "labels": {}, "sources": [],
            })
            if entry["cloud_code"] != cloud:
                raise ValueError(f"conflicting sport namespaces for device {device}")
            locale = "zh-CN" if localized else "en"
            previous = entry["labels"].get(locale)
            if previous is not None and previous != label:
                raise ValueError(f"conflicting sport labels for device {device}")
            entry["labels"][locale] = label
            entry["sources"].append(source)
            for original, field in (
                ("key", "resource_key"), ("sportGroup", "sport_group"),
                ("asset_category", "asset_category"), ("supportGPS", "support_gps"),
            ):
                if original in row:
                    entry[field] = row[original]

    reviewed = {row["device_code"]: row for row in (metadata or {}).get("entries", [])}
    entries, placeholders = [], []
    for device, entry in sorted(merged.items()):
        if device < 0 or entry["cloud_code"] < 0:
            entry["status"] = "placeholder"
            placeholders.append(entry)
            continue
        previous = reviewed.get(device, {})
        defaults = {
            "canonical_code": f"zepp_sport_{device}",
            "display_label_zh": entry["labels"].get("zh-CN") or entry["labels"].get("en"),
            "category": "other", "training_family": "unknown",
            "classification_source": "unclassified",
        }
        entry.update({field: previous.get(field, defaults[field]) for field in _METADATA})
        entries.append(entry)
    cloud_codes = [row["cloud_code"] for row in entries]
    if len(cloud_codes) != len(set(cloud_codes)):
        raise ValueError("duplicate cloud sport type across merged assets")
    return {
        "catalog_revision": f"zepp-apk-{app_version}-sports-v1",
        "provenance": {
            "app_version": app_version, "artifact_digest": artifact_digest,
            "resources": digests,
        },
        "source_entry_counts": counts,
        "entries": entries, "placeholders": placeholders,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    metadata_path = args.metadata or args.output
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    with args.apk.open("rb") as apk:
        artifact_digest = hashlib.file_digest(apk, "sha256").hexdigest()
    with zipfile.ZipFile(args.apk) as archive:
        for filename in ASSETS:
            if (args.assets / filename).read_bytes() != archive.read(f"assets/{filename}"):
                raise ValueError(f"asset bytes do not match the supplied APK: {filename}")
    catalog = extract_catalog(
        args.assets, app_version=args.app_version,
        artifact_digest=artifact_digest, metadata=metadata,
    )
    output = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != output:
            raise SystemExit("sport catalog differs from APK evidence and reviewed metadata")
    else:
        args.output.write_text(output, encoding="utf-8", newline="\n")
    print(json.dumps({
        "entries": len(catalog["entries"]), "placeholders": len(catalog["placeholders"]),
        "artifact_digest": artifact_digest,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
