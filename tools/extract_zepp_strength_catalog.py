"""Normalize the official public muscle catalog; never ingest workout records."""

import argparse
import hashlib
import json
from pathlib import Path


APKM_DIGEST = "64e1d87b6ab79e0ecedfbead176cc6bb781f9754b46915d52c0d2ccfbcef5949"
CATALOG_DIGEST = "7e6b4ba617bd8e61b0c269c9c51fb4d7b430c0e30332273a964fa88f8028abeb"


def extract(response: bytes, artifact_digest: str) -> dict:
    """Require the reviewed artifacts before asserting their field relationship."""
    digest = hashlib.sha256(response).hexdigest()
    if artifact_digest != APKM_DIGEST or digest != CATALOG_DIGEST:
        raise ValueError("unreviewed artifact: verify APK field semantics and catalog before updating digests")
    records = json.loads(response)
    if not isinstance(records, list) or len(records) != 1134:
        raise ValueError("incomplete official catalog")
    codes, entries, placeholders = set(), [], []
    for record in records:
        code, name = record.get("actionType"), record.get("actionName")
        self_weight_type = record.get("selfWeightType")
        if type(self_weight_type) is not int or self_weight_type not in {0, 1}:
            raise ValueError("invalid selfWeightType")
        if type(code) is not int or not 0 <= code < 2**31 or code in codes:
            raise ValueError("invalid or duplicate actionType")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("missing actionName")
        codes.add(code)
        if code == 0:
            if name != "选择一项训练":
                raise ValueError("unreviewed placeholder")
            placeholders.append({"vendor_code": code, "label": name})
            continue
        entries.append({
            "namespace": "zepp.strength.lap_62", "vendor_code": code,
            "canonical_exercise_id": None, "labels": {"zh-CN": name},
            "verification": "verified",
            "self_weight_type": self_weight_type,
            "provenance": {"catalog_reference": f"/sport/config/muscle?lang=zh_CN#actionType={code}"},
        })
    return {
        "catalog_revision": "zepp-apk-10.8.7-play-muscle-7e6b4ba6-v3",
        "provenance": {
            "app_version": "10.8.7-play", "artifact_digest": artifact_digest,
            "catalog_response_digest": digest,
            "extraction_reference": "APK lap_62[28] -> strengthTrainType -> /sport/config/muscle actionType/actionName/selfWeightType; tc20.g bodyweight display",
        },
        "source_entry_count": len(records), "placeholders": placeholders,
        "entries": sorted(entries, key=lambda entry: entry["vendor_code"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--apkm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with args.apkm.open("rb") as artifact:
        digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    output = json.dumps(extract(args.catalog.read_bytes(), digest), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if args.output.read_text(encoding="utf-8") != output:
            raise SystemExit("strength catalog differs from reviewed evidence")
    else:
        args.output.write_text(output, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
