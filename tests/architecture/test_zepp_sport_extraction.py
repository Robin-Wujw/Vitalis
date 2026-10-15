"""Local APK extraction never conflates display keys, device IDs, or cloud types."""

import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest


def _tool():
    path = Path(__file__).parents[2] / "tools" / "extract_zepp_sport_catalog.py"
    spec = importlib.util.spec_from_file_location("extract_zepp_sport_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assets(path):
    free = {"id": 5, "type": 16, "key": 64, "name": "Free training"}
    other = {"id": 219, "type": 219, "key": 64, "name": "Skierg"}
    placeholder = {"id": -1, "type": -1, "key": -1, "name": "Training"}
    documents = {
        "config_sport_type_group.json": [{"id": "5", "type": "16", "name": "自由训练"}],
        "device_sport_categories.json": [{"category": "IndoorSports", "items": [free, other]}],
        "sport_categories.json": [{"category": "IndoorSports", "items": [free, other, placeholder]}],
    }
    for filename, payload in documents.items():
        (path / filename).write_text(json.dumps(payload), encoding="utf-8")
    return documents


def test_merges_all_assets_without_treating_resource_keys_as_sport_ids(tmp_path):
    _assets(tmp_path)
    catalog = _tool().extract_catalog(tmp_path, app_version="test", artifact_digest="a" * 64)
    assert [(row["device_code"], row["cloud_code"]) for row in catalog["entries"]] == [(5, 16), (219, 219)]
    assert all(row["resource_key"] == 64 for row in catalog["entries"])
    assert catalog["entries"][0]["labels"] == {"zh-CN": "自由训练", "en": "Free training"}
    assert all(row["training_family"] == "unknown" for row in catalog["entries"])
    assert catalog["placeholders"][0]["status"] == "placeholder"
    assert catalog["source_entry_counts"] == {
        "assets/config_sport_type_group.json": 1,
        "assets/device_sport_categories.json": 2,
        "assets/sport_categories.json": 3,
    }
    assert _tool().extract_catalog(
        tmp_path, app_version="test", artifact_digest="a" * 64, metadata=catalog,
    ) == catalog


def test_conflicting_cloud_type_is_rejected_instead_of_overwriting(tmp_path):
    documents = _assets(tmp_path)
    documents["device_sport_categories.json"][0]["items"][0]["type"] = 17
    (tmp_path / "device_sport_categories.json").write_text(
        json.dumps(documents["device_sport_categories.json"]), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conflicting sport namespaces"):
        _tool().extract_catalog(tmp_path, app_version="test", artifact_digest="a" * 64)


def _apk(path):
    apk = path / "base.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        for filename in _tool().ASSETS:
            archive.write(path / filename, f"assets/{filename}")
    return apk


def test_cli_check_does_not_rewrite_the_catalog(tmp_path, monkeypatch):
    _assets(tmp_path)
    apk = _apk(tmp_path)
    output = tmp_path / "catalog.json"
    command = ["extract", "--assets", str(tmp_path), "--apk", str(apk),
               "--app-version", "test", "--output", str(output)]
    monkeypatch.setattr(sys, "argv", command)
    assert _tool().main() == 0
    before = output.read_bytes(), output.stat().st_mtime_ns
    monkeypatch.setattr(sys, "argv", command + ["--check"])
    assert _tool().main() == 0
    assert (output.read_bytes(), output.stat().st_mtime_ns) == before
    output.write_text(output.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="differs"):
        _tool().main()


def test_cli_rejects_assets_that_do_not_match_the_claimed_apk(tmp_path, monkeypatch):
    documents = _assets(tmp_path)
    apk = _apk(tmp_path)
    documents["sport_categories.json"][0]["items"][0]["type"] = 17
    (tmp_path / "sport_categories.json").write_text(
        json.dumps(documents["sport_categories.json"]), encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", [
        "extract", "--assets", str(tmp_path), "--apk", str(apk),
        "--app-version", "test", "--output", str(tmp_path / "catalog.json"),
    ])
    with pytest.raises(ValueError, match="match.*APK"):
        _tool().main()
