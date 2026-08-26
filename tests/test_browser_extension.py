"""Static contract checks for automatic browser login handoff."""

import json
from pathlib import Path


EXTENSION = Path(__file__).parents[1] / "browser_extension"


def test_extension_registers_background_cookie_refresh():
    manifest = json.loads((EXTENSION / "manifest.json").read_text())
    assert manifest["background"]["service_worker"] == "background.js"
    assert {"alarms", "cookies", "storage"}.issubset(manifest["permissions"])

    background = (EXTENSION / "background.js").read_text()
    assert "chrome.cookies.onChanged.addListener" in background
    assert "chrome.alarms.onAlarm.addListener" in background
    assert "/api/v1/connect/zepp/link/credentials" in background
    assert "Authorization" in background


def test_extension_collects_no_account_credentials():
    popup = (EXTENSION / "popup.html").read_text()
    assert 'type="password"' not in popup
    assert 'name="password"' not in popup
    assert "官方登录" in popup


def test_extension_persists_pairing_drafts_between_popup_opens():
    popup_script = (EXTENSION / "popup.js").read_text()
    assert 'persistDraft(baseInput, "vitalisBase"' in popup_script
    assert 'persistDraft(codeInput, "pairingCode"' in popup_script
    assert 'input.addEventListener("input"' in popup_script
    assert "if (!baseDirty)" in popup_script
    assert "if (!codeDirty)" in popup_script


def test_extension_cookie_search_is_not_limited_by_cookie_path():
    background = (EXTENSION / "background.js").read_text()
    assert "for (const name of COOKIE_NAMES)" in background
    assert "chrome.cookies.getAll({ name })" in background
    assert "COOKIE_URLS" not in background
    assert 'cookie.domain.endsWith("zepp.com")' in background
    assert 'cookie.domain.endsWith("huami.com")' in background
