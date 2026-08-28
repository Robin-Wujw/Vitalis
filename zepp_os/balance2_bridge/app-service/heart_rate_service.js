import { HeartRate } from "@zos/sensor";

import { mergeQueue } from "../shared/queue";

const FLUSH_SIZE = 15;
let heartRate = null;
let pending = [];

function flush() {
  if (!pending.length) return;
  mergeQueue(pending);
  pending = [];
}

function onHeartRate() {
  const value = heartRate.getCurrent();
  if (!Number.isFinite(value) || value < 20 || value > 240) return;
  pending.push({timestamp: Date.now(), heart_rate: Math.round(value)});
  if (pending.length >= FLUSH_SIZE) flush();
}

AppService({
  onInit() {
    heartRate = new HeartRate();
    heartRate.onCurrentChange(onHeartRate);
  },
  onDestroy() {
    if (heartRate) heartRate.offCurrentChange(onHeartRate);
    flush();
    heartRate = null;
  },
});
