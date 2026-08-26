const baseInput = document.querySelector("#base");
const codeInput = document.querySelector("#code");
const submitButton = document.querySelector("#submit");
const message = document.querySelector("#message");
const state = document.querySelector("#state");
let baseDirty = false;
let codeDirty = false;

persistDraft(baseInput, "vitalisBase", () => { baseDirty = true; });
persistDraft(codeInput, "pairingCode", () => { codeDirty = true; });

document.querySelector("#login").addEventListener("click", async () => {
  await chrome.tabs.create({ url: "https://watchface.zepp.com/" });
});

chrome.storage.local.get(["vitalisBase", "pairingCode", "pairingState", "pairingMessage"], (saved) => {
  if (!baseDirty) baseInput.value = saved.vitalisBase || "";
  if (!codeDirty) codeInput.value = saved.pairingCode || "";
  renderStatus(saved.pairingState, saved.pairingMessage);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.pairingState || changes.pairingMessage) {
    chrome.storage.local.get(["pairingState", "pairingMessage"], (saved) => {
      renderStatus(saved.pairingState, saved.pairingMessage);
    });
  }
});

document.querySelector("#pair").addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy(true, "正在读取 Zepp 登录状态…");
  try {
    const base = normalizedBase(baseInput.value);
    const code = codeInput.value.trim();
    if (!/^[A-Za-z0-9_-]{24,}$/.test(code)) throw new Error("配对码格式不正确");

    const originPattern = `${new URL(base).origin}/*`;
    const granted = await chrome.permissions.request({ origins: [originPattern] });
    if (!granted) throw new Error("需要允许扩展访问你的 Vitalis 地址");

    await chrome.storage.local.set({ vitalisBase: base, pairingCode: code });
    const result = await chrome.runtime.sendMessage({
      type: "startPairing",
      base,
      code
    });
    if (result?.status === "error") throw new Error(result.message || "连接失败");
  } catch (error) {
    message.className = "";
    message.textContent = error.message || String(error);
    state.textContent = "连接失败";
  } finally {
    submitButton.disabled = false;
  }
});

function normalizedBase(raw) {
  const url = new URL(raw.trim());
  if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("Vitalis 地址必须是有效的 HTTP(S) 地址");
  }
  if (url.protocol === "http:" && !["localhost", "127.0.0.1"].includes(url.hostname)) {
    throw new Error("公网 Vitalis 地址必须使用 HTTPS");
  }
  return url.origin;
}

function persistDraft(input, key, markDirty) {
  input.addEventListener("input", () => {
    markDirty();
    void chrome.storage.local.set({ [key]: input.value });
  });
}

function setBusy(busy, text) {
  submitButton.disabled = busy;
  message.className = "";
  message.textContent = text;
  state.textContent = busy ? "处理中" : "待连接";
}

function renderStatus(pairingState, pairingMessage) {
  if (!pairingState) return;
  const labels = {
    connected: "已连接",
    waiting_login: "等待登录",
    needs_login: "需要登录",
    connecting: "连接中",
    checking: "检查中",
    error: "连接失败"
  };
  state.textContent = labels[pairingState] || "待连接";
  message.className = pairingState === "connected" ? "ok" : "";
  message.textContent = pairingMessage || "";
}
