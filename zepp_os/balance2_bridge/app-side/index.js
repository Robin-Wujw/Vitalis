import { BaseSideService, settingsLib } from "@zeppos/zml/base-side";

function config() {
  const base = (settingsLib.getItem("vitalisBase") || "").replace(/\/+$/, "");
  const token = settingsLib.getItem("deviceLinkToken") || "";
  if (!/^https:\/\//i.test(base) || token.length < 32) return null;
  return {base, token};
}

function responseBody(response) {
  if (typeof response.body !== "string") return response.body;
  try {
    return JSON.parse(response.body);
  } catch (_error) {
    return null;
  }
}

async function uploadHeartRate(service, samples, res) {
  const value = config();
  if (!value) {
    res(null, {
      transport_status: "configuration_required",
      http_status: null,
      body: null,
    });
    return;
  }
  try {
    const response = await service.fetch({
      url: `${value.base}/api/v1/connect/zepp/device-link/heart-rate`,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${value.token}`,
      },
      body: JSON.stringify({protocol_version: 2, samples}),
      timeout: 10000,
    });
    res(null, {
      transport_status: "completed",
      http_status: response.status,
      body: responseBody(response),
    });
  } catch (_error) {
    res(null, {
      transport_status: "network_error",
      http_status: null,
      body: null,
    });
  }
}

AppSideService(BaseSideService({
  onInit() {},
  onRequest(req, res) {
    if (req.method !== "UPLOAD_HEART_RATE") {
      res(null, {transport_status: "unsupported", http_status: null, body: null});
      return;
    }
    const samples = req.params && Array.isArray(req.params.samples)
      ? req.params.samples.slice(0, 500)
      : [];
    uploadHeartRate(this, samples, res);
  },
  onRun() {},
  onDestroy() {},
}));
