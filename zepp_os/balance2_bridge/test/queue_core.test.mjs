import assert from "node:assert/strict";
import test from "node:test";

import {
  JOURNAL_VERSION,
  applySettlement,
  compactQueue,
  createSampleId,
  encodeJournal,
  makeCheckpoint,
  makeManifest,
  parseJournal,
  queueView,
  selectCheckpoint,
  selectJournalGeneration,
  selectManifest,
} from "../shared/queue_core.mjs";

function record(id, timestamp, heartRate = 70, sampleOrdinal = 0) {
  return {
    version: JOURNAL_VERSION,
    id,
    timestamp,
    sample_ordinal: sampleOrdinal,
    heart_rate: heartRate,
  };
}

test("journal parser accepts complete records and ignores only a torn final line", () => {
  const records = [record("one", 1000), record("two", 1001, 71)];
  assert.deepEqual(parseJournal(encodeJournal(records)), {records, tornTail: false});
  assert.deepEqual(
    parseJournal(`${JSON.stringify(records[0])}\n{"version":3`),
    {records: [records[0]], tornTail: true},
  );
  assert.deepEqual(
    parseJournal(JSON.stringify(records[0])),
    {records: [], tornTail: true},
  );
  assert.throws(
    () => parseJournal(`{"broken":true}\n${JSON.stringify(records[0])}\n`),
    /invalid journal line 1/,
  );
  assert.throws(
    () => parseJournal(`${JSON.stringify(records[0])}\n${JSON.stringify(records[0])}\n`),
    /duplicate journal id/,
  );
});

test("manifest and checkpoint readers fall back to the highest valid slot", () => {
  const first = makeManifest(1, "a");
  const second = makeManifest(2, "b", {droppedCount: 3});
  assert.equal(
    selectManifest(JSON.stringify(first), JSON.stringify(second)).generation,
    2,
  );
  assert.equal(selectManifest(JSON.stringify(first), "{broken").generation, 1);
  const recovered = selectJournalGeneration(
    JSON.stringify(first),
    JSON.stringify(second),
    encodeJournal([record("recoverable", 1000)]),
    "{broken journal}\n",
  );
  assert.equal(recovered.manifest.generation, 1);
  assert.deepEqual(recovered.journal.records.map((item) => item.id), ["recoverable"]);

  const checkpoint1 = makeCheckpoint(null, ["one"]);
  const checkpoint2 = makeCheckpoint(checkpoint1, ["two"], {permanentRejectedCount: 1});
  const selected = selectCheckpoint(JSON.stringify(checkpoint1), JSON.stringify(checkpoint2));
  assert.equal(selected.generation, 2);
  assert.deepEqual(selected.settledIds, ["one", "two"]);
  assert.equal(selected.permanentRejectedCount, 1);
});

test("queue view applies acknowledgements before retaining the newest bounded window", () => {
  const records = Array.from({length: 5}, (_, index) => record(`id-${index}`, 1000 + index));
  const checkpoint = makeCheckpoint(null, ["id-1"]);
  const view = queueView(records, checkpoint, 3);
  assert.deepEqual(view.pending.map((item) => item.id), ["id-2", "id-3", "id-4"]);
  assert.equal(view.overflow, 1);
  assert.equal(view.settledInJournal, 1);
  const compacted = compactQueue(records, checkpoint, 3);
  assert.deepEqual(compacted.records, view.pending);
  assert.equal(compacted.droppedCount, 1);
  assert.equal(compacted.removedSettledCount, 1);
});

test("queue retention uses timestamp order rather than append order", () => {
  const records = [
    record("late", 1004),
    record("old", 1000),
    record("newest", 1005),
    record("middle", 1003),
  ];
  const view = queueView(records, makeCheckpoint(), 2);
  assert.deepEqual(view.pending.map((item) => item.id), ["late", "newest"]);
  assert.equal(view.overflow, 2);
});


test("settlement removes only submitted acknowledged and permanent-rejection IDs", () => {
  const checkpoint = makeCheckpoint(null, ["older"]);
  const next = applySettlement(
    checkpoint,
    {
      protocol_version: 2,
      status: "processed",
      acknowledged: [{sample_id: "accepted"}],
      rejected: [{sample_id: "stale", code: "timestamp_too_old", retryable: false}],
    },
    ["accepted", "stale", "retry"],
  );
  assert.deepEqual(next.settledIds, ["accepted", "older", "stale"]);
  assert.equal(next.permanentRejectedCount, 1);
  assert.throws(
    () => applySettlement(checkpoint, {
      protocol_version: 2,
      status: "processed",
      acknowledged: [{sample_id: "unknown"}],
      rejected: [],
    }, ["accepted"]),
    /unknown or duplicate/,
  );
  assert.throws(
    () => applySettlement(checkpoint, {
      protocol_version: 2,
      status: "processed",
      acknowledged: [],
      rejected: [],
    }, ["accepted"]),
    /no progress/,
  );
});

test("sample IDs preserve same-millisecond and service-session identity", () => {
  assert.equal(
    createSampleId(1700000000000, 0, "abcd", 0),
    "z2:1700000000000:0:abcd:0",
  );
  assert.notEqual(
    createSampleId(1700000000000, 0, "abcd", 0),
    createSampleId(1700000000000, 0, "efgh", 0),
  );
  assert.notEqual(
    createSampleId(1700000000000, 0, "abcd", 0),
    createSampleId(1700000000000, 1, "abcd", 1),
  );
  assert.notEqual(
    createSampleId(1700000000000, 0, "abcd", 0),
    createSampleId(1700000000000, 0, "abcd", 1),
  );
});
