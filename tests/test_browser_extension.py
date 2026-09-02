"""Static contract checks for automatic browser login handoff."""

import json
from pathlib import Path


EXTENSION = Path(__file__).parents[1] / "browser_extension"


def test_extension_registers_background_cookie_refresh():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["background"]["service_worker"] == "background.js"
    assert {"alarms", "cookies", "storage"}.issubset(manifest["permissions"])

    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "chrome.cookies.onChanged.addListener" in background
    assert "chrome.alarms.onAlarm.addListener" in background
    assert "/api/v1/connect/zepp/link/credentials" in background
    assert "Authorization" in background


def test_extension_collects_no_account_credentials():
    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    assert 'type="password"' not in popup
    assert 'name="password"' not in popup
    assert "官方登录" in popup


def test_extension_persists_pairing_drafts_between_popup_opens():
    popup_script = (EXTENSION / "popup.js").read_text(encoding="utf-8")
    assert 'persistDraft(baseInput, "vitalisBase"' in popup_script
    assert 'persistDraft(codeInput, "pairingCode"' in popup_script
    assert 'input.addEventListener("input"' in popup_script
    assert "if (!baseDirty)" in popup_script
    assert "if (!codeDirty)" in popup_script


def test_extension_cookie_search_is_not_limited_by_cookie_path():
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "for (const name of COOKIE_NAMES)" in background
    assert "chrome.cookies.getAll({ name })" in background
    assert "COOKIE_URLS" not in background
    assert 'hostname.endsWith(".zepp.com")' in background
    assert 'hostname.endsWith(".huami.com")' in background


def test_extension_cookie_diagnostics_are_local_and_value_free():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.2.7"

    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    diagnostics_body = background.split("async function collectCookieDiagnostics()", 1)[1]
    diagnostics_body = diagnostics_body.split("function isLoginCookie", 1)[0]
    assert "chrome.cookies.getAll({})" in diagnostics_body
    assert "isAllowedCookieDomain" in diagnostics_body
    assert "cookie.name" in diagnostics_body
    assert "cookie.domain" in diagnostics_body
    assert "cookie.value" not in diagnostics_body
    assert "fetch(" not in diagnostics_body
    assert "chrome.storage.local.set" in diagnostics_body

    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    popup_script = (EXTENSION / "popup.js").read_text(encoding="utf-8")
    assert 'id="diagnostics"' in popup
    assert "renderDiagnostics" in popup_script
    assert '"pairingCode", "cookieDiagnostics", "pageStorageDiagnostics"' in background


def test_extension_bridges_allowlisted_page_storage_credentials_without_persisting_them():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    content_script = manifest["content_scripts"][0]
    assert content_script["js"] == ["page_credential.js"]
    assert set(content_script["matches"]) == {
        "https://*.zepp.com/*",
        "https://*.huami.com/*",
    }

    page_script = (EXTENSION / "page_credential.js").read_text(encoding="utf-8")
    assert 'localStorage.getItem(key)' in page_script
    assert 'sessionStorage.getItem(key)' in page_script
    assert 'type: "zeppPageCredential"' in page_script
    assert '"apptoken"' in page_script
    assert '"userid"' in page_script
    assert "chrome.storage" not in page_script
    assert "fetch(" not in page_script
    assert "console." not in page_script
    assert 'type: "zeppPageStorageDiagnostics"' in page_script
    assert "storageKeys(localStorage)" in page_script
    assert "storageKeys(sessionStorage)" in page_script
    assert 'document.cookie.split(";")' in page_script
    assert "pageCookies()" in page_script
    assert "documentCookieNames" in page_script

    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert 'request?.type === "zeppPageCredential"' in background
    assert "isAllowedPageSender(_sender)" in background
    assert 'url.protocol === "https:"' in background
    assert "isSafeCredentialMessage(request.credential)" in background
    assert 'request?.type === "zeppPageStorageDiagnostics"' in background
    assert "safeStorageKeys" in background
    assert "request.pageStorageDiagnostics?.documentCookieNames" in background

    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    popup_script = (EXTENSION / "popup.js").read_text(encoding="utf-8")
    assert 'id="page-storage-diagnostics"' in popup
    assert "renderPageStorageDiagnostics" in popup_script
    assert "value.documentCookieNames" in popup_script


def test_extension_uses_official_watchface_login_handoff():
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    popup_script = (EXTENSION / "popup.js").read_text(encoding="utf-8")
    for script in (background, popup_script):
        assert "https://user.zepp.com/universalLogin/index.html#/login" in script
        assert "project_name=watchface" in script
        assert "project_redirect_uri=" in script
        assert "platform_app=com.huami.webapp" in script
        assert "encodeURIComponent" in script


def test_missing_cookie_validates_saved_cloud_credential_before_reauth():
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    refresh = background.split("async function refreshCredential()", 1)[1]
    refresh = refresh.split("async function submitCredential", 1)[0]
    assert "validateSavedCredential()" in refresh
    assert "reportDisconnected(" not in refresh
    validator = background.split("async function validateSavedCredential()", 1)[1]
    validator = validator.split("async function submitCredential", 1)[0]
    assert "/api/v1/connect/zepp/link/validate" in validator
    assert 'response.status === 400' in validator
