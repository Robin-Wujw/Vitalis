const LOGIN_URL = "https://watchface.zepp.com/";
const COOKIE_NAMES = new Set(["hm-user-login-info", "hm_user_login_info"]);
const REFRESH_ALARM = "vitalis-zepp-session-refresh";

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: 30 });
  void refreshCredential();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: 30 });
  void refreshCredential();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === REFRESH_ALARM) void refreshCredential();
});

chrome.cookies.onChanged.addListener((change) => {
  if (!isLoginCookie(change.cookie)) return;
  if (change.removed) {
    void reportDisconnected("Zepp 网页登录已退出，请重新登录");
  } else {
    void submitCredential(change.cookie.value);
  }
});

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  if (request?.type !== "startPairing") return false;
  startPairing(request.base, request.code)
    .then(sendResponse)
    .catch((error) => sendResponse({ status: "error", message: error.message || String(error) }));
  return true;
});

async function startPairing(base, code) {
  await chrome.storage.local.set({
    vitalisBase: base,
    pairingCode: code,
    pendingPairing: true,
    pairingState: "checking",
    pairingMessage: "正在检查 Zepp 登录状态"
  });
  const cookie = await findLoginCookie();
  if (cookie) return submitCredential(cookie.value);

  await setPairingStatus("waiting_login", "请在新页面完成 Zepp 官方登录，登录后会自动连接");
  await chrome.tabs.create({ url: LOGIN_URL });
  return { status: "waiting_login" };
}

async function refreshCredential() {
  const state = await chrome.storage.local.get([
    "pendingPairing", "browserLinkToken", "vitalisBase"
  ]);
  if (!state.vitalisBase || (!state.pendingPairing && !state.browserLinkToken)) return;
  const cookie = await findLoginCookie();
  if (cookie) {
    await submitCredential(cookie.value);
  } else if (state.browserLinkToken) {
    await reportDisconnected("未检测到 Zepp 网页登录，请重新登录以继续自动更新");
  }
}

async function submitCredential(cookieValue) {
  const state = await chrome.storage.local.get([
    "vitalisBase", "pairingCode", "browserLinkToken"
  ]);
  if (!state.vitalisBase) return { status: "idle" };

  const headers = { "Content-Type": "application/json" };
  let endpoint;
  if (state.browserLinkToken) {
    endpoint = `${state.vitalisBase}/api/v1/connect/zepp/link/credentials`;
    headers.Authorization = `Bearer ${state.browserLinkToken}`;
  } else if (state.pairingCode) {
    endpoint = `${state.vitalisBase}/api/v1/connect/zepp/pair/${encodeURIComponent(state.pairingCode)}/credentials`;
  } else {
    return { status: "idle" };
  }

  await setPairingStatus("connecting", "登录成功，正在安全连接 Vitalis");
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({ cookie: cookieValue })
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `连接失败（HTTP ${response.status}）`);

    const changes = {
      pendingPairing: false,
      pairingState: "connected",
      pairingMessage: result.message || "已连接，登录状态将自动保持更新",
      lastCredentialSync: new Date().toISOString()
    };
    if (result.browser_link_token) changes.browserLinkToken = result.browser_link_token;
    await chrome.storage.local.set(changes);
    await chrome.storage.local.remove("pairingCode");
    return { status: "connected" };
  } catch (error) {
    await setPairingStatus("error", error.message || String(error));
    return { status: "error", message: error.message || String(error) };
  }
}

async function reportDisconnected(reason) {
  const state = await chrome.storage.local.get(["vitalisBase", "browserLinkToken"]);
  await setPairingStatus("needs_login", reason);
  if (!state.vitalisBase || !state.browserLinkToken) return;
  try {
    await fetch(`${state.vitalisBase}/api/v1/connect/zepp/link/disconnected`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${state.browserLinkToken}`
      },
      body: JSON.stringify({ reason })
    });
  } catch (_error) {
    // The local warning remains visible and the next alarm retries session discovery.
  }
}

async function findLoginCookie() {
  for (const name of COOKIE_NAMES) {
    const cookies = await chrome.cookies.getAll({ name });
    const found = cookies.find((cookie) => isLoginCookie(cookie) && cookie.value);
    if (found) return found;
  }
  return null;
}

function isLoginCookie(cookie) {
  return COOKIE_NAMES.has(cookie.name) &&
    (cookie.domain.endsWith("zepp.com") || cookie.domain.endsWith("huami.com"));
}

async function setPairingStatus(state, message) {
  await chrome.storage.local.set({ pairingState: state, pairingMessage: message });
}
