import { BaseSideService, settingsLib } from "@zeppos/zml/base-side";

function config() {
  const base = (settingsLib.getItem("vitalisBase") || "").replace(/\/+$/, "");
  const token = settingsLib.getItem("deviceLinkToken") || "";
  if (!/^https:\/\//i.test(base) || token.length < 32) return null;
  return { base, token };
}

async function uploadHeartRate(samples, res) {
  const value = config();
  if (!value) {
    res(null, { status: "configuration_required" });
    return;
  }
  try {
    const response = await fetch({
      url: `${value.base}/api/v1/connect/zepp/device-link/heart-rate`,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${value.token}`,
      },
      body: JSON.stringify({ samples }),
    });
    const body = typeof response.body === "string" ? JSON.parse(response.body) : response.body;
    res(null, response.status >= 200 && response.status < 300
      ? body
      : { status: "rejected" });
  } catch (_error) {
    res(null, { status: "network_error" });
  }
}

AppSideService(BaseSideService({
  onInit() {},
  onRequest(req, res) {
    if (req.method !== "UPLOAD_HEART_RATE") {
      res(null, { status: "unsupported" });
      return;
    }
    const samples = Array.isArray(req.params?.samples) ? req.params.samples.slice(0, 500) : [];
    uploadHeartRate(samples, res);
  },
  onRun() {},
  onDestroy() {},
}));
