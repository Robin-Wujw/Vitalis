import { BasePage } from "@zeppos/zml/base-page";
import * as appService from "@zos/app-service";
import { queryPermission, requestPermission } from "@zos/app";
import { align, createWidget, prop, text_style, widget } from "@zos/ui";

import { acknowledgeQueue, readQueue } from "../shared/queue";

const SERVICE = "app-service/heart_rate_service";
const PERMISSIONS = ["data:user.hd.heart_rate", "device:os.bg_service"];
let statusText = null;

function setStatus(text) {
  if (statusText) statusText.setProperty(prop.TEXT, text);
}

Page(BasePage({
  build() {
    statusText = createWidget(widget.TEXT, {
      x: 40, y: 90, w: 400, h: 100,
      text: `${readQueue().length} 条待上传`,
      text_size: 28, color: 0xffffff,
      align_h: align.CENTER_H, align_v: align.CENTER_V,
      text_style: text_style.WRAP,
    });
    createWidget(widget.BUTTON, {
      x: 80, y: 220, w: 320, h: 72,
      text: "启动后台采集",
      normal_color: 0x177245, press_color: 0x0f5633,
      click_func: () => this.startCollection(),
    });
    createWidget(widget.BUTTON, {
      x: 80, y: 320, w: 320, h: 72,
      text: "同步到 Vitalis",
      normal_color: 0x2d6ca2, press_color: 0x204f78,
      click_func: () => this.uploadPending(),
    });
  },
  startCollection() {
    const results = queryPermission({ permissions: PERMISSIONS });
    if (results.every((value) => value === 2)) {
      this.startService();
      return;
    }
    requestPermission({
      permissions: PERMISSIONS,
      callback: (values) => {
        if (values.every((value) => value === 2)) this.startService();
        else setStatus("未获得心率或后台权限");
      },
    });
  },
  startService() {
    if (appService.getAllAppServices().includes(SERVICE)) {
      setStatus(`${readQueue().length} 条待上传，后台已运行`);
      return;
    }
    appService.start({
      url: SERVICE,
      complete_func: (result) => setStatus(
        result.result ? "后台采集已启动" : "后台采集启动失败"
      ),
    });
  },
  async uploadPending() {
    let uploaded = 0;
    while (true) {
      const batch = readQueue().slice(0, 500);
      if (!batch.length) break;
      try {
        const response = await this.request({
          method: "UPLOAD_HEART_RATE",
          params: { samples: batch },
        });
        if (!response || response.status !== "accepted") throw new Error("upload rejected");
        acknowledgeQueue(batch.map((sample) => sample.timestamp));
        uploaded += batch.length;
      } catch (_error) {
        setStatus(`上传中断，仍有 ${readQueue().length} 条`);
        return;
      }
    }
    setStatus(`已上传 ${uploaded} 条，队列为空`);
  },
}));
