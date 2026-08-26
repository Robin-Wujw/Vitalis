# Vitalis Zepp Login

This Manifest V3 extension is the user-side half of Vitalis cloud pairing.

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable developer mode, choose **Load unpacked**, and select this directory.
3. Open `/api/v1/connect/zepp/scan?user=<id>` on Vitalis to create a pairing code.
4. Enter the Vitalis address and pairing code in the extension, then choose **Login and connect**.
5. Complete sign-in on the official page. The extension resumes pairing automatically.

The Vitalis address must use browser-trusted HTTPS when it is not localhost. The
extension intentionally rejects public plaintext HTTP origins.

The popup saves both pairing fields as they are pasted, so closing and reopening it
while copying the second value does not discard the first one.

Cookie discovery uses only the known Zepp login-cookie names on permitted Zepp/Huami
domains and is independent of the cookie's URL path.

The extension reads only Zepp/Huami cookies. Access to a Vitalis origin is
requested at runtime and granted for the exact origin entered by the user. It checks
the session when the cookie changes and every 30 minutes, updates Vitalis through a
revocable browser link, and reports when sign-in is required again.
