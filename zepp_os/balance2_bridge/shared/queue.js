import { readFileSync, statSync, writeFileSync } from "@zos/fs";

const QUEUE_PATH = "vitalis-heart-rate.json";
const MAX_SAMPLES = 3600;

export function readQueue() {
  if (!statSync({ path: QUEUE_PATH })) return [];
  try {
    const value = JSON.parse(readFileSync({
      path: QUEUE_PATH,
      options: { encoding: "utf8" },
    }));
    return Array.isArray(value) ? value : [];
  } catch (_error) {
    return [];
  }
}

export function mergeQueue(samples) {
  const byTimestamp = {};
  [...readQueue(), ...samples].forEach((sample) => {
    if (sample && Number.isFinite(sample.timestamp) && Number.isFinite(sample.heart_rate)) {
      byTimestamp[String(sample.timestamp)] = sample;
    }
  });
  const queue = Object.values(byTimestamp)
    .sort((a, b) => a.timestamp - b.timestamp)
    .slice(-MAX_SAMPLES);
  writeFileSync({
    path: QUEUE_PATH,
    data: JSON.stringify(queue),
    options: { encoding: "utf8" },
  });
  return queue.length;
}

export function acknowledgeQueue(timestamps) {
  const sent = new Set(timestamps.map(String));
  const remaining = readQueue().filter((sample) => !sent.has(String(sample.timestamp)));
  writeFileSync({
    path: QUEUE_PATH,
    data: JSON.stringify(remaining),
    options: { encoding: "utf8" },
  });
  return remaining.length;
}
