"""Static safety and architecture contracts for the Balance 2 fallback app."""

import json
from pathlib import Path


APP = Path(__file__).parents[1] / "zepp_os" / "balance2_bridge"


def test_balance2_manifest_targets_supported_api_and_background_heart_rate_only():
    manifest = json.loads((APP / "app.json").read_text())
    api = manifest["runtime"]["apiVersion"]
    assert api["target"] == "4.2"
    assert "app-service/heart_rate_service" in manifest["targets"]["common"]["module"]["app-service"]["services"]
    assert "data:user.hd.heart_rate" in manifest["permissions"]
    assert "device:os.bg_service" in manifest["permissions"]
    assert "device:os.accelerometer" not in manifest["permissions"]


def test_balance2_queue_is_bounded_and_upload_is_https_authenticated():
    queue = (APP / "shared" / "queue.js").read_text()
    service = (APP / "app-service" / "heart_rate_service.js").read_text()
    side = (APP / "app-side" / "index.js").read_text()
    assert "MAX_SAMPLES = 3600" in queue
    assert ".slice(-MAX_SAMPLES)" in queue
    assert "onCurrentChange" in service
    assert "offCurrentChange" in service
    assert "HeartRate" in service
    assert "Accelerometer" not in service
    assert 'https:\\/\\/' in side
    assert "Authorization" in side
    assert "/api/v1/connect/zepp/device-link/heart-rate" in side
    assert "console." not in side


def test_balance2_readme_does_not_claim_fixed_one_hertz_or_helio_support():
    readme = (APP / "README.md").read_text()
    assert "not a guaranteed 1 Hz stream" in readme
    assert "Helio Strap cannot run Zepp OS apps" in readme
    assert "Accelerometer collection is intentionally absent" in readme
