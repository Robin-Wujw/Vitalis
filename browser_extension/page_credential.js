const LOGIN_INFO_KEYS = ["hm-user-login-info", "hm_user_login_info"];
const USER_ID_KEYS = ["userid", "user_id", "userId"];
const APP_TOKEN_KEYS = ["apptoken", "app_token", "app-token", "appToken"];
const PROFILE_KEYS = ["storage_user_register_info"];
let lastSubmittedCredential = "";

void reportPageStorageDiagnostics();
void submitPageCredential();
setInterval(() => void submitPageCredential(), 5000);

async function submitPageCredential() {
  const credential = readPageCredential();
  if (!credential || credential === lastSubmittedCredential) return;
  lastSubmittedCredential = credential;
  try {
    const result = await chrome.runtime.sendMessage({ type: "zeppPageCredential", credential });
    if (result?.status === "error") lastSubmittedCredential = "";
  } catch (_error) {
    lastSubmittedCredential = "";
  }
}

function readPageCredential() {
  const cookies = pageCookies();
  for (const key of LOGIN_INFO_KEYS) {
    const value = readStorageValue(key) || cookies.get(key) || "";
    if (value) return value;
  }
  const profile = firstStoredObject(PROFILE_KEYS);
  const userId = firstStorageValue(USER_ID_KEYS) || firstObjectValue(profile, USER_ID_KEYS) ||
    firstMapValue(cookies, USER_ID_KEYS);
  const appToken = firstStorageValue(APP_TOKEN_KEYS) || firstObjectValue(profile, APP_TOKEN_KEYS) ||
    firstMapValue(cookies, APP_TOKEN_KEYS);
  return userId && appToken ? JSON.stringify({ userid: userId, apptoken: appToken }) : "";
}

async function reportPageStorageDiagnostics() {
  const pageStorageDiagnostics = {
    documentCookieNames: [...pageCookies().keys()].sort(),
    localStorageKeys: storageKeys(localStorage),
    sessionStorageKeys: storageKeys(sessionStorage)
  };
  try {
    await chrome.runtime.sendMessage({ type: "zeppPageStorageDiagnostics", pageStorageDiagnostics });
  } catch (_error) {
    // The next page load reports again after the extension worker is available.
  }
}

function firstStorageValue(keys) {
  for (const key of keys) {
    const value = readStorageValue(key);
    if (value) return value;
  }
  return "";
}

function readStorageValue(key) {
  try {
    return localStorage.getItem(key) || sessionStorage.getItem(key) || "";
  } catch (_error) {
    return "";
  }
}

function firstStoredObject(keys) {
  for (const key of keys) {
    try {
      const parsed = JSON.parse(readStorageValue(key) || "null");
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    } catch (_error) {
      // Ignore malformed page state and keep looking for fixed credential keys.
    }
  }
  return {};
}

function firstObjectValue(object, keys) {
  for (const key of keys) {
    if (typeof object[key] === "string" && object[key]) return object[key];
  }
  return "";
}

function firstMapValue(map, keys) {
  for (const key of keys) {
    const value = map.get(key);
    if (value) return value;
  }
  return "";
}

function pageCookies() {
  const result = new Map();
  try {
    for (const part of document.cookie.split(";")) {
      const separator = part.indexOf("=");
      if (separator < 0) continue;
      const name = part.slice(0, separator).trim();
      const rawValue = part.slice(separator + 1).trim();
      if (!name) continue;
      try {
        result.set(name, decodeURIComponent(rawValue));
      } catch (_error) {
        result.set(name, rawValue);
      }
    }
  } catch (_error) {
    return result;
  }
  return result;
}

function storageKeys(storage) {
  try {
    return Array.from({ length: storage.length }, (_value, index) => storage.key(index))
      .filter((key) => typeof key === "string")
      .sort();
  } catch (_error) {
    return [];
  }
}
