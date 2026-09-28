import { HeartRate } from "@zos/sensor";

import { appendQueue, initializeQueueWriter, maintainQueue } from "../shared/queue.js";
import { JOURNAL_VERSION, createSampleId } from "../shared/queue_core.mjs";

const MAINTENANCE_INTERVAL = 128;
const RECOVERY_DELAY_MS = 60000;
const SERVICE_NONCE = `${Date.now().toString(36)}${Math.floor(
  Math.random() * 0xffffffff
).toString(36)}`;
let heartRate = null;
let lastTimestamp = null;
let sameMillisecondOrdinal = 0;
let sampleSequence = 0;
let callbacksSinceMaintenance = 0;
let maintenanceScheduled = false;
let recoveryScheduled = false;
let destroying = false;

function nextSampleIdentity(timestamp) {
  if (timestamp === lastTimestamp) {
    sameMillisecondOrdinal += 1;
  } else {
    lastTimestamp = timestamp;
    sameMillisecondOrdinal = 0;
  }
  const identity = {
    id: createSampleId(
      timestamp,
      sameMillisecondOrdinal,
      SERVICE_NONCE,
      sampleSequence,
    ),
    sample_ordinal: sameMillisecondOrdinal,
  };
  sampleSequence += 1;
  return identity;
}

function scheduleMaintenance() {
  if (maintenanceScheduled || callbacksSinceMaintenance < MAINTENANCE_INTERVAL) return;
  maintenanceScheduled = true;
  callbacksSinceMaintenance = 0;
  setTimeout(() => {
    try {
      maintainQueue();
    } catch (_error) {
      // Appends remain in the active journal; maintenance retries later.
    } finally {
      maintenanceScheduled = false;
    }
  }, 0);
}

function startCollection() {
  initializeQueueWriter();
  heartRate = new HeartRate();
  heartRate.onCurrentChange(onHeartRate);
}

function scheduleRecovery() {
  if (destroying || recoveryScheduled) return;
  recoveryScheduled = true;
  setTimeout(() => {
    recoveryScheduled = false;
    if (destroying) return;
    try {
      startCollection();
    } catch (_error) {
      scheduleRecovery();
    }
  }, RECOVERY_DELAY_MS);
}

function onHeartRate() {
  const value = heartRate.getCurrent();
  if (!Number.isFinite(value) || value < 20 || value > 240) return;
  const timestamp = Date.now();
  const identity = nextSampleIdentity(timestamp);
  try {
    appendQueue({
      version: JOURNAL_VERSION,
      id: identity.id,
      timestamp,
      sample_ordinal: identity.sample_ordinal,
      heart_rate: Math.round(value),
    });
  } catch (_error) {
    heartRate.offCurrentChange(onHeartRate);
    heartRate = null;
    scheduleRecovery();
    return;
  }
  callbacksSinceMaintenance += 1;
  scheduleMaintenance();
}

AppService({
  onInit() {
    destroying = false;
    try {
      startCollection();
    } catch (_error) {
      scheduleRecovery();
    }
  },
  onDestroy() {
    destroying = true;
    if (heartRate) heartRate.offCurrentChange(onHeartRate);
    try {
      maintainQueue();
    } catch (_error) {
      // The active generation remains readable and will be recovered on restart.
    }
    heartRate = null;
  },
});
