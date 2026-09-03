import {
  O_APPEND,
  O_CREAT,
  O_WRONLY,
  closeSync,
  openSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
  writeSync,
} from "@zos/fs";

import {
  JOURNAL_VERSION,
  MAX_PENDING_SAMPLES,
  applySettlement,
  compactQueue,
  encodeJournal,
  makeManifest,
  parseJournal,
  pruneCheckpoint,
  queueView,
  selectCheckpoint,
  selectJournalGeneration,
  validRecord,
} from "./queue_core.mjs";

const OBSOLETE_QUEUE_PATH = "vitalis-heart-rate.json";
const JOURNAL_PATHS = {
  a: "vitalis-heart-rate-v3-a.ndjson",
  b: "vitalis-heart-rate-v3-b.ndjson",
};
const MANIFEST_PATHS = {
  a: "vitalis-heart-rate-manifest-a.json",
  b: "vitalis-heart-rate-manifest-b.json",
};
const CHECKPOINT_PATHS = {
  a: "vitalis-heart-rate-ack-a.json",
  b: "vitalis-heart-rate-ack-b.json",
};
const FAULT_PATH = "vitalis-heart-rate-fault.json";
const COMPACTION_HEADROOM = 128;

let writerManifest = null;
let writerFaulted = false;

function readText(path) {
  if (!statSync({path})) return null;
  return readFileSync({path, options: {encoding: "utf8"}});
}

function writeText(path, data) {
  writeFileSync({path, data, options: {encoding: "utf8"}});
}

function readGeneration() {
  const slotA = readText(MANIFEST_PATHS.a);
  const slotB = readText(MANIFEST_PATHS.b);
  const generation = selectJournalGeneration(
    slotA,
    slotB,
    readText(JOURNAL_PATHS.a),
    readText(JOURNAL_PATHS.b),
  );
  if (!generation && (slotA !== null || slotB !== null)) {
    throw new Error("queue manifest or journal is corrupt");
  }
  return generation;
}

function readManifest() {
  const generation = readGeneration();
  return generation ? generation.manifest : null;
}

function readCheckpoint() {
  return selectCheckpoint(
    readText(CHECKPOINT_PATHS.a),
    readText(CHECKPOINT_PATHS.b),
  );
}

function manifestFileSlot(generation) {
  return generation % 2 === 1 ? "a" : "b";
}

function publishGeneration(records, previous, counters = {}) {
  const journalSlot = previous && previous.journalSlot === "a" ? "b" : "a";
  const generation = ((previous && previous.generation) || 0) + 1;
  const journalText = encodeJournal(records);
  writeText(JOURNAL_PATHS[journalSlot], journalText);
  const verified = parseJournal(readText(JOURNAL_PATHS[journalSlot]) || "");
  if (verified.tornTail || verified.records.length !== records.length) {
    throw new Error("queue generation verification failed");
  }
  const manifest = makeManifest(generation, journalSlot, {
    droppedCount: counters.droppedCount !== undefined
      ? counters.droppedCount
      : (previous && previous.droppedCount) || 0,
    corruptionCount: counters.corruptionCount !== undefined
      ? counters.corruptionCount
      : (previous && previous.corruptionCount) || 0,
  });
  const slot = manifestFileSlot(generation);
  writeText(MANIFEST_PATHS[slot], JSON.stringify(manifest));
  const selected = readManifest();
  if (!selected || selected.generation !== generation) {
    throw new Error("queue manifest publication failed");
  }
  return selected;
}

function initializeQueue() {
  const existing = readManifest();
  const manifest = existing || publishGeneration([], null);
  if (statSync({path: OBSOLETE_QUEUE_PATH})) rmSync({path: OBSOLETE_QUEUE_PATH});
  return manifest;
}

function readActiveJournal(manifest) {
  const text = readText(JOURNAL_PATHS[manifest.journalSlot]);
  if (text === null) throw new Error("active queue journal is missing");
  return parseJournal(text);
}

function readableState() {
  const generation = readGeneration();
  if (generation) return generation;
  return {
    manifest: {generation: 0, droppedCount: 0, corruptionCount: 0},
    journal: {records: [], tornTail: false},
  };
}

function asciiBuffer(text) {
  const bytes = new Uint8Array(text.length);
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code > 0x7f) throw new Error("journal append must be ASCII");
    bytes[index] = code;
  }
  return bytes.buffer;
}

function writeFault(message) {
  try {
    writeText(FAULT_PATH, JSON.stringify({timestamp: Date.now(), message}));
  } catch (_error) {
    // The in-memory fault still prevents appending after a short or failed write.
  }
}

function clearFault() {
  if (statSync({path: FAULT_PATH})) rmSync({path: FAULT_PATH});
}

export function initializeQueueWriter() {
  writerManifest = initializeQueue();
  let journal = readActiveJournal(writerManifest);
  if (journal.tornTail) {
    writerManifest = publishGeneration(journal.records, writerManifest, {
      corruptionCount: writerManifest.corruptionCount + 1,
    });
    journal = readActiveJournal(writerManifest);
  }
  writerFaulted = false;
  maintainQueue(false);
  clearFault();
  return queueStats();
}

export function appendQueue(record) {
  if (!validRecord(record)) {
    throw new Error("invalid heart-rate queue record");
  }
  if (!writerManifest) initializeQueueWriter();
  if (writerFaulted) throw new Error("queue writer is faulted");
  const line = `${JSON.stringify(record)}\n`;
  const buffer = asciiBuffer(line);
  let fd = null;
  try {
    fd = openSync({
      path: JOURNAL_PATHS[writerManifest.journalSlot],
      flag: O_WRONLY | O_APPEND | O_CREAT,
    });
    const written = writeSync({fd, buffer});
    if (written !== buffer.byteLength) throw new Error("short queue journal write");
  } catch (error) {
    writerFaulted = true;
    writeFault(error instanceof Error ? error.message : "queue append failed");
    throw error;
  } finally {
    if (fd !== null) closeSync({fd});
  }
}

export function maintainQueue(force = false) {
  if (!writerManifest) writerManifest = initializeQueue();
  const journal = readActiveJournal(writerManifest);
  const checkpoint = readCheckpoint();
  const view = queueView(journal.records, checkpoint);
  if (
    !force
    && !journal.tornTail
    && journal.records.length <= MAX_PENDING_SAMPLES + COMPACTION_HEADROOM
    && view.settledInJournal < COMPACTION_HEADROOM
  ) {
    return false;
  }
  const compacted = compactQueue(journal.records, checkpoint);
  writerManifest = publishGeneration(compacted.records, writerManifest, {
    droppedCount: writerManifest.droppedCount + compacted.droppedCount,
    corruptionCount: writerManifest.corruptionCount + (journal.tornTail ? 1 : 0),
  });
  return true;
}

export function readQueue() {
  const state = readableState();
  return queueView(state.journal.records, readCheckpoint()).pending;
}

export function settleQueue(response, submittedIds) {
  const state = readableState();
  const checkpoint = pruneCheckpoint(readCheckpoint(), state.journal.records);
  const next = applySettlement(checkpoint, response, submittedIds);
  const slot = manifestFileSlot(next.generation);
  writeText(CHECKPOINT_PATHS[slot], JSON.stringify(next));
  const selected = readCheckpoint();
  if (selected.generation !== next.generation) {
    throw new Error("queue checkpoint publication failed");
  }
}

export function queueStats() {
  try {
    const state = readableState();
    const checkpoint = readCheckpoint();
    const view = queueView(state.journal.records, checkpoint);
    return {
      pending: view.pending.length,
      droppedCount: state.manifest.droppedCount + view.overflow,
      permanentRejectedCount: checkpoint.permanentRejectedCount,
      corruptionCount: state.manifest.corruptionCount + (state.journal.tornTail ? 1 : 0),
      faulted: writerFaulted || Boolean(readText(FAULT_PATH)),
    };
  } catch (_error) {
    return {
      pending: 0,
      droppedCount: 0,
      permanentRejectedCount: 0,
      corruptionCount: 1,
      faulted: true,
    };
  }
}
