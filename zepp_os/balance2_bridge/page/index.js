import { BasePage } from "@zeppos/zml/base-page";
import * as appService from "@zos/app-service";
import { queryPermission, requestPermission } from "@zos/app";
import { align, createWidget, prop, text_style, widget } from "@zos/ui";

import { queueStats, readQueue, settleQueue } from "../shared/queue.js";

const SERVICE = "app-service/heart_rate_service";
const PERMISSIONS = ["data:user.hd.heart_rate", "device:os.bg_service"];
let statusText = null;
let uploading = false;

function queueSummary() {
  const stats = queueStats();
  const details = [`${stats.pending} 条待上传`];
  if (stats.droppedCount) details.push(`容量丢弃 ${stats.droppedCount} 条`);
  if (stats.permanentRejectedCount) {
    details.push(`服务端拒绝 ${stats.permanentRejectedCount} 条`);
  }
  if (stats.corruptionCount) details.push(`存储异常 ${stats.corruptionCount} 次`);
  if (stats.faulted) details.push("队列需恢复");
  return details.join("，");
}

function setStatus(text) {
  if (statusText) statusText.setProperty(prop.TEXT, text);
}

function uploadSamples(records) {
  return records.map((record) => ({
    sample_id: record.id,
    timestamp: record.timestamp,
    sample_ordinal: record.sample_ordinal,
    heart_rate: record.heart_rate,
  }));
}

function settlementBody(response) {
  if (!response || response.transport_status !== "completed") {
    throw new Error((response && response.transport_status) || "network_error");
  }
  if (response.http_status < 200 || response.http_status >= 300) {
    throw new Error(`http_${response.http_status}`);
  }
  return response.body;
}

Page(BasePage({
  build() {
    statusText = createWidget(widget.TEXT, {
      x: 40, y: 90, w: 400, h: 100,
      text: queueSummary(),
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
    const results = queryPermission({permissions: PERMISSIONS});
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
      setStatus(`${queueSummary()}，后台已运行`);
      return;
    }
    appService.start({
      file: SERVICE,
      complete_func: (result) => setStatus(
        result.result ? `后台采集已启动，${queueSummary()}` : "后台采集启动失败"
      ),
    });
  },
  async uploadPending() {
    if (uploading) {
      setStatus("同步已在进行");
      return;
    }
    uploading = true;
    let acknowledged = 0;
    let rejected = 0;
    try {
      const snapshot = readQueue();
      if (!snapshot.length) {
        setStatus(queueSummary());
      } else {
        for (let offset = 0; offset < snapshot.length; offset += 500) {
          const batch = snapshot.slice(offset, offset + 500);
          const response = await this.request({
            method: "UPLOAD_HEART_RATE",
            params: {samples: uploadSamples(batch)},
          });
          const settlement = settlementBody(response);
          settleQueue(settlement, batch.map((record) => record.id));
          acknowledged += settlement.acknowledged.length;
          rejected += settlement.rejected.length;
        }
        setStatus(`已确认 ${acknowledged} 条，拒绝 ${rejected} 条，${queueSummary()}`);
      }
    } catch (_error) {
      setStatus(`上传中断，未确认记录保留，${queueSummary()}`);
    }
    uploading = false;
  },
}));
