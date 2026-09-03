"""Static safety and architecture contracts for the Balance 2 fallback app."""

import json
from pathlib import Path

from PIL import Image


APP = Path(__file__).parents[1] / "zepp_os" / "balance2_bridge"


def test_balance2_manifest_targets_supported_api_and_background_heart_rate_only():
    manifest = json.loads((APP / "app.json").read_text(encoding="utf-8"))
    api = manifest["runtime"]["apiVersion"]
    assert api["target"] == "4.2"
    assert "app-service/heart_rate_service" in manifest["targets"]["common"]["module"]["app-service"]["services"]
    assert "data:user.hd.heart_rate" in manifest["permissions"]
    assert "device:os.bg_service" in manifest["permissions"]
    assert "device:os.accelerometer" not in manifest["permissions"]
    icon = APP / "assets" / "common.r" / manifest["app"]["icon"]
    assert icon.is_file()
    with Image.open(icon) as image:
        assert image.width >= 248 and image.height >= 248


def test_balance2_queue_is_bounded_and_upload_is_https_authenticated():
    core = (APP / "shared" / "queue_core.mjs").read_text(encoding="utf-8")
    queue = (APP / "shared" / "queue.js").read_text(encoding="utf-8")
    service = (APP / "app-service" / "heart_rate_service.js").read_text(encoding="utf-8")
    page = (APP / "page" / "index.js").read_text(encoding="utf-8")
    side = (APP / "app-side" / "index.js").read_text(encoding="utf-8")
    package = json.loads((APP / "package.json").read_text(encoding="utf-8"))
    assert "MAX_PENDING_SAMPLES = 3600" in core
    assert "parseLegacyQueue" not in core
    assert "applySettlement" in core
    assert "O_APPEND" in queue
    assert "JOURNAL_PATHS" in queue
    assert "CHECKPOINT_PATHS" in queue
    assert "appendQueue" in service
    assert "MAINTENANCE_INTERVAL" in service
    assert "RECOVERY_DELAY_MS" in service
    assert "scheduleRecovery" in service
    assert "onCurrentChange" in service
    assert "offCurrentChange" in service
    assert "HeartRate" in service
    assert "Accelerometer" not in service
    assert "file: SERVICE" in page
    assert "url: SERVICE" not in page
    assert "settleQueue" in page
    assert "const snapshot = readQueue()" in page
    assert "if (uploading)" in page
    assert 'https:\\/\\/' in side
    assert "Authorization" in side
    assert "/api/v1/connect/zepp/device-link/heart-rate" in side
    assert "/heart-rate/v2" not in side
    assert "timeout: 10000" in side
    assert "console." not in side
    assert package["scripts"]["test"] == "node --test test/*.test.mjs"
    assert package["engines"]["node"] == ">=24 <25"
    assert package["_moduleAliases"]["zeppos-app-utils"].endswith(
        "private-modules/zeppos-app-utils"
    )
    assert (APP / "package-lock.json").is_file()
    assert (APP / ".nvmrc").read_text(encoding="utf-8").strip() == "24"


def test_balance2_readme_does_not_claim_fixed_one_hertz_or_helio_support():
    readme = (APP / "README.md").read_text(encoding="utf-8")
    assert "not a guaranteed 1 Hz stream" in readme
    assert "Helio Strap cannot run Zepp OS apps" in readme
    assert "Accelerometer collection is intentionally absent" in readme
    assert "sole writer of a versioned NDJSON journal" in readme
    assert "newest 3,600 pending records" in readme
    assert "settles exact sample IDs" in readme
    assert "no `fsync` guarantee" in readme
