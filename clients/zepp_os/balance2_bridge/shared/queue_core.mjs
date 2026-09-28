export const JOURNAL_VERSION = 3;
export const MANIFEST_VERSION = 1;
export const CHECKPOINT_VERSION = 1;
export const MAX_PENDING_SAMPLES = 3600;

function checksumPayload(payload) {
  const text = JSON.stringify(payload);
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function seal(payload) {
  const value = Object.assign({}, payload);
  value.checksum = checksumPayload(payload);
  return value;
}

function unseal(value, expectedVersion) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const checksum = value.checksum;
  const payload = Object.assign({}, value);
  delete payload.checksum;
  if (payload.version !== expectedVersion || typeof checksum !== "string") return null;
  return checksum === checksumPayload(payload) ? payload : null;
}

export function makeManifest(generation, journalSlot, counters = {}) {
  if (!Number.isInteger(generation) || generation < 1) throw new Error("invalid manifest generation");
  if (!new Set(["a", "b"]).has(journalSlot)) throw new Error("invalid journal slot");
  return seal({
    version: MANIFEST_VERSION,
    generation,
    journalSlot,
    droppedCount: Number.isInteger(counters.droppedCount) ? counters.droppedCount : 0,
    corruptionCount: Number.isInteger(counters.corruptionCount) ? counters.corruptionCount : 0,
  });
}

export function makeCheckpoint(previous = null, settledIds = [], deltas = {}) {
  const prior = previous || {
    generation: 0,
    settledIds: [],
    permanentRejectedCount: 0,
  };
  const ids = Array.from(new Set(prior.settledIds.concat(settledIds))).sort();
  return seal({
    version: CHECKPOINT_VERSION,
    generation: prior.generation + 1,
    settledIds: ids,
    permanentRejectedCount: prior.permanentRejectedCount
      + (deltas.permanentRejectedCount || 0),
  });
}

function parseSlot(text, expectedVersion) {
  if (typeof text !== "string" || !text.length) return null;
  try {
    return unseal(JSON.parse(text), expectedVersion);
  } catch (_error) {
    return null;
  }
}

export function selectManifest(slotA, slotB) {
  const values = [
    parseSlot(slotA, MANIFEST_VERSION),
    parseSlot(slotB, MANIFEST_VERSION),
  ].filter((value) => Boolean(
    value
    && Number.isInteger(value.generation)
    && value.generation >= 1
    && (value.journalSlot === "a" || value.journalSlot === "b")
    && Number.isInteger(value.droppedCount)
    && Number.isInteger(value.corruptionCount)
  ));
  if (!values.length) return null;
  return values.sort((left, right) => right.generation - left.generation)[0];
}

export function selectJournalGeneration(slotA, slotB, journalA, journalB) {
  const candidates = [
    selectManifest(slotA, null),
    selectManifest(null, slotB),
  ].filter(Boolean).sort((left, right) => right.generation - left.generation);
  for (let index = 0; index < candidates.length; index += 1) {
    const manifest = candidates[index];
    const text = manifest.journalSlot === "a" ? journalA : journalB;
    if (typeof text !== "string") continue;
    try {
      return {manifest, journal: parseJournal(text)};
    } catch (_error) {
      // Try the previous complete generation.
    }
  }
  return null;
}

export function selectCheckpoint(slotA, slotB) {
  const values = [
    parseSlot(slotA, CHECKPOINT_VERSION),
    parseSlot(slotB, CHECKPOINT_VERSION),
  ].filter((value) => Boolean(
    value
    && Number.isInteger(value.generation)
    && value.generation >= 1
    && Array.isArray(value.settledIds)
    && value.settledIds.every((id) => typeof id === "string")
    && Number.isInteger(value.permanentRejectedCount)
  ));
  if (!values.length) {
    return {
      version: CHECKPOINT_VERSION,
      generation: 0,
      settledIds: [],
      permanentRejectedCount: 0,
    };
  }
  return values.sort((left, right) => right.generation - left.generation)[0];
}

export function createSampleId(timestamp, ordinal, nonce, sequence) {
  if (!Number.isInteger(timestamp) || timestamp <= 0) throw new Error("invalid sample timestamp");
  if (!Number.isInteger(ordinal) || ordinal < 0 || ordinal >= 1000) {
    throw new Error("invalid sample ordinal");
  }
  if (typeof nonce !== "string" || !/^[a-z0-9]{4,32}$/.test(nonce)) {
    throw new Error("invalid sample nonce");
  }
  if (!Number.isInteger(sequence) || sequence < 0) {
    throw new Error("invalid sample sequence");
  }
  return `z2:${timestamp}:${ordinal}:${nonce}:${sequence}`;
}

export function validRecord(record) {
  return Boolean(
    record
    && record.version === JOURNAL_VERSION
    && typeof record.id === "string"
    && /^[A-Za-z0-9._:-]{1,128}$/.test(record.id)
    && Number.isInteger(record.timestamp)
    && record.timestamp > 0
    && Number.isInteger(record.sample_ordinal)
    && record.sample_ordinal >= 0
    && record.sample_ordinal < 1000
    && Number.isInteger(record.heart_rate)
    && record.heart_rate >= 20
    && record.heart_rate <= 240
  );
}

export function encodeJournal(records) {
  records.forEach((record) => {
    if (!validRecord(record)) throw new Error("invalid journal record");
  });
  return records.length
    ? `${records.map((record) => JSON.stringify(record)).join("\n")}\n`
    : "";
}

export function parseJournal(text) {
  if (typeof text !== "string") throw new Error("journal must be text");
  if (!text.length) return {records: [], tornTail: false};
  const hasCompleteTail = text.endsWith("\n");
  const lines = text.split("\n");
  let tornTail = false;
  if (hasCompleteTail) {
    lines.pop();
  } else {
    lines.pop();
    tornTail = true;
  }
  const records = [];
  const ids = new Set();
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.length) throw new Error("empty interior journal line");
    let record;
    try {
      record = JSON.parse(line);
    } catch (_error) {
      throw new Error(`corrupt journal line ${index + 1}`);
    }
    if (!validRecord(record)) throw new Error(`invalid journal line ${index + 1}`);
    if (ids.has(record.id)) throw new Error(`duplicate journal id ${record.id}`);
    ids.add(record.id);
    records.push(record);
  }
  return {records, tornTail};
}

export function queueView(records, checkpoint, maxPending = MAX_PENDING_SAMPLES) {
  const settled = new Set(checkpoint.settledIds);
  const unsettled = records
    .filter((record) => !settled.has(record.id))
    .sort((left, right) => left.timestamp - right.timestamp || left.id.localeCompare(right.id));
  const overflow = Math.max(0, unsettled.length - maxPending);
  return {
    pending: unsettled.slice(overflow),
    overflow,
    settledInJournal: records.length - unsettled.length,
  };
}

export function compactQueue(records, checkpoint, maxPending = MAX_PENDING_SAMPLES) {
  const view = queueView(records, checkpoint, maxPending);
  return {
    records: view.pending,
    droppedCount: view.overflow,
    removedSettledCount: view.settledInJournal,
  };
}

export function applySettlement(checkpoint, response, submittedIds) {
  if (
    !response
    || response.protocol_version !== 2
    || response.status !== "processed"
    || !Array.isArray(response.acknowledged)
    || !Array.isArray(response.rejected)
  ) {
    throw new Error("invalid settlement response");
  }
  const submitted = new Set(submittedIds);
  if (submitted.size !== submittedIds.length) throw new Error("duplicate submitted id");
  const settled = [];
  const seen = new Set();
  response.acknowledged.forEach((item) => {
    const id = item && item.sample_id;
    if (!submitted.has(id) || seen.has(id)) throw new Error("unknown or duplicate acknowledged id");
    seen.add(id);
    settled.push(id);
  });
  let permanentRejectedCount = 0;
  response.rejected.forEach((item) => {
    const id = item && item.sample_id;
    if (!item || item.retryable !== false || !submitted.has(id) || seen.has(id)) {
      throw new Error("invalid rejected settlement");
    }
    seen.add(id);
    settled.push(id);
    permanentRejectedCount += 1;
  });
  if (!settled.length) throw new Error("settlement made no progress");
  return makeCheckpoint(checkpoint, settled, {permanentRejectedCount});
}

export function pruneCheckpoint(checkpoint, records) {
  const present = new Set(records.map((record) => record.id));
  const settledIds = checkpoint.settledIds.filter((id) => present.has(id));
  if (settledIds.length === checkpoint.settledIds.length) return checkpoint;
  return seal(Object.assign({}, checkpoint, {settledIds}));
}
