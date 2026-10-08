# Vitalis Zepp Login Extension

[简体中文](README.md)

This Manifest V3 extension is the browser side of Vitalis Zepp pairing. The directory is self-contained when packaged and does not rely on repository-relative documentation.

## Pairing

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable developer mode, choose **Load unpacked**, and select this directory.
3. On an initialized Vitalis service, call `POST /api/connect/zepp/pair` with a Bearer token that has the `manage` scope. The response contains a one-time pairing code and `scan_url`; open that URL only on the official Zepp page.
4. Enter the Vitalis service origin and pairing code in the popup, then choose **Login and connect**.
5. Complete sign-in on the official page. The extension resumes pairing and reports when sign-in is required again.

The non-local Vitalis origin must use browser-trusted HTTPS. Configure the exact `chrome-extension://<extension ID>` origin shown by the browser in `VITALIS_PAIRING_ALLOWED_ORIGINS`, then restart the API. Host permission does not authorize the extension on the server or bypass API authentication.

Starting a new pairing flow clears the locally retained browser-link token. The popup keeps both pasted fields across a close/reopen. Cookie discovery uses only allowlisted Zepp/Huami domains and known login-cookie names, and the page bridge reads only fixed credential keys. Credentials are never written to extension storage or logs. A local diagnostic may show cookie counts and name/domain pairs, never values; it is cleared after pairing.
