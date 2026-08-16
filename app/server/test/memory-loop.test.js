// Week 1 creates memory → Week 2 loads it → the new evidence re-checks it.
//
// The UI's whole job on a later Monday is to make that visible, and it does it
// by putting every recommendation in exactly one of three buckets: carried in
// and re-checked, genuinely new, or carried but not yet due. If that partition
// is wrong — a row in two buckets, a carried row counted as new — the page
// tells a reviewer the opposite of what happened, which is the one failure mode
// worth a test.
//
// These are the pure functions behind it (app/web/src/memory.js), tested the
// same way phases.js is: the real ESM source, imported through a data: URL, no
// build step.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const MEMORY_SRC = path.join(__dirname, "..", "..", "web", "src", "memory.js");

let memory;
test.before(async () => {
  const src = fs.readFileSync(MEMORY_SRC, "utf8");
  memory = await import(`data:text/javascript,${encodeURIComponent(src)}`);
});

// Shapes taken from a real DATA/OUTPUTS/<as_of>/digest.json, trimmed to the
// fields the partition reads.
const MP = "REC-2026-04-27-mount-pleasant-staff_call_outs";
const MV = "REC-2026-04-27-morrisville-record_completion_24h_pct";
const DI = "REC-2026-05-04-daniel-island-no_show_rate";

const week2CarryIn = {
  previous_run: "2026-04-27",
  is_first_run: false,
  open_recommendations: [
    { rec_id: MP, center: "Mount Pleasant", created_week: "2026-04-27" },
    { rec_id: MV, center: "Morrisville", created_week: "2026-04-27" },
  ],
  open_recommendation_count: 2,
  due_this_week: [MP, MV],
  new_data: {
    panel_read_through: "2026-04-27",
    corrections_carried: 4,
    corrections_new: 0,
    rows_under_standing_corrections: 12,
    provider_rows_under_standing_corrections: 12,
  },
};

const week2Ledger = {
  changed: [
    {
      rec_id: MP,
      center: "Mount Pleasant",
      created_week: "2026-04-27",
      outcome: "not_working",
      execution: "unknown",
    },
    {
      rec_id: MV,
      center: "Morrisville",
      created_week: "2026-04-27",
      outcome: "working",
      execution: "unknown",
    },
  ],
  created: [{ rec_id: DI, center: "Daniel Island", created_week: "2026-05-04" }],
  open: [
    { rec_id: MV, center: "Morrisville", created_week: "2026-04-27" },
    { rec_id: MP, center: "Mount Pleasant", created_week: "2026-04-27" },
    { rec_id: DI, center: "Daniel Island", created_week: "2026-05-04", owner: "Talia Okonkwo" },
  ],
};

// ---------------------------------------------------------------------------
// The partition
// ---------------------------------------------------------------------------

test("week 2 splits carried-and-re-checked from genuinely new", () => {
  const m = memory.summarizeMemory({
    carryIn: week2CarryIn,
    ledger: week2Ledger,
    asOf: "2026-05-04",
  });

  assert.equal(m.isFirstRun, false);
  assert.equal(m.previousRun, "2026-04-27");
  assert.equal(m.carriedCount, 2);
  assert.equal(m.dueCount, 2);
  assert.equal(m.correctionsCarried, 4);
  assert.equal(m.correctionsNew, 0);
  assert.equal(m.providerRowsUnderStandingCorrections, 12);
  assert.equal(m.panelReadThrough, "2026-04-27");

  assert.deepEqual(m.rechecked.map((r) => r.rec_id).sort(), [MP, MV].sort());
  assert.deepEqual(m.created.map((r) => r.rec_id), [DI]);
  assert.deepEqual(m.stillOpen, []);
});

test("no recommendation appears in two buckets", () => {
  const m = memory.summarizeMemory({
    carryIn: week2CarryIn,
    ledger: week2Ledger,
    asOf: "2026-05-04",
  });
  const ids = [...m.rechecked, ...m.created, ...m.stillOpen].map((r) => r.rec_id);
  assert.equal(new Set(ids).size, ids.length, `a row was rendered twice: ${ids}`);
  // And every active row is accounted for — a carried row silently dropped
  // would read as "we never made that recommendation".
  assert.equal(new Set(ids).size, 3);
});

test("a new row is rendered from its full ledger.open copy, not the creation receipt", () => {
  const m = memory.summarizeMemory({
    carryIn: week2CarryIn,
    ledger: week2Ledger,
    asOf: "2026-05-04",
  });
  // ledger.created carries the ask; ledger.open carries the lifecycle fields a
  // card needs. The open copy wins.
  assert.equal(m.created[0].owner, "Talia Okonkwo");
});

test("a carried row whose check-by has not arrived is neither re-checked nor new", () => {
  const ledger = {
    changed: [],
    created: [],
    open: [{ rec_id: MP, center: "Mount Pleasant", created_week: "2026-04-27" }],
  };
  const m = memory.summarizeMemory({
    carryIn: { ...week2CarryIn, due_this_week: [] },
    ledger,
    asOf: "2026-05-04",
  });
  assert.deepEqual(m.rechecked, []);
  assert.deepEqual(m.created, []);
  assert.deepEqual(m.stillOpen.map((r) => r.rec_id), [MP]);
  assert.equal(m.dueCount, 0);
});

// ---------------------------------------------------------------------------
// Week 1 says it is week 1
// ---------------------------------------------------------------------------

test("the first Monday reports itself as the first Monday", () => {
  const m = memory.summarizeMemory({
    carryIn: {
      previous_run: null,
      is_first_run: true,
      open_recommendations: [],
      open_recommendation_count: 0,
      due_this_week: [],
      new_data: { panel_read_through: "2026-04-20", corrections_carried: 0, corrections_new: 4 },
    },
    ledger: { changed: [], created: [{ rec_id: MP }, { rec_id: MV }], open: [] },
    asOf: "2026-04-27",
  });
  assert.equal(m.isFirstRun, true);
  assert.equal(m.carriedCount, 0);
  assert.deepEqual(m.rechecked, []);
  assert.equal(m.created.length, 2);
});

// ---------------------------------------------------------------------------
// A digest written before Step 0 was recorded in it
// ---------------------------------------------------------------------------

test("without a recorded carry-in the buckets still resolve, and say the memory facts are unknown", () => {
  const m = memory.summarizeMemory({
    carryIn: null,
    ledger: week2Ledger,
    asOf: "2026-05-04",
  });
  assert.equal(m.known, false);
  assert.equal(m.isFirstRun, false);
  // Derived from each row's own created_week rather than invented.
  assert.equal(m.carriedCount, 2);
  assert.deepEqual(m.rechecked.map((r) => r.rec_id).sort(), [MP, MV].sort());
  assert.deepEqual(m.created.map((r) => r.rec_id), [DI]);
  // Facts only carry_in knows are absent, not guessed.
  assert.equal(m.previousRun, null);
  assert.equal(m.dueCount, null);
  assert.equal(m.correctionsNew, null);
});

test("an old first-Monday digest with no carry-in still reads as the first Monday", () => {
  const m = memory.summarizeMemory({
    carryIn: null,
    ledger: { changed: [], created: [{ rec_id: MP }], open: [{ rec_id: MP, created_week: "2026-04-27" }] },
    asOf: "2026-04-27",
  });
  assert.equal(m.isFirstRun, true);
});

// ---------------------------------------------------------------------------
// Outcome and execution never merge
// ---------------------------------------------------------------------------

test("the re-check tallies keep what the numbers did apart from whether the work happened", () => {
  const c = memory.recheckCounts(week2Ledger.changed);
  assert.equal(c.total, 2);
  assert.equal(c.working, 1);
  assert.equal(c.notWorking, 1);
  // One metric moved the right way, and that is NOT evidence anyone acted.
  assert.equal(c.executionDone, 0);
  assert.equal(c.executionUnknown, 2);
});

test("with nothing attested the execution sentence claims nothing", () => {
  const c = memory.recheckCounts(week2Ledger.changed);
  assert.equal(memory.outcomeClause(c), "1 moved the right way, 1 moved the wrong way");
  assert.equal(memory.executionClause(c), "none confirmed as carried out");
});

test("an attested re-check is reported as attested, still separately from the outcome", () => {
  const c = memory.recheckCounts([
    { outcome: "working", execution: "done" },
    { outcome: "not_working", execution: "not_done" },
  ]);
  assert.equal(memory.outcomeClause(c), "1 moved the right way, 1 moved the wrong way");
  assert.equal(
    memory.executionClause(c),
    "1 confirmed carried out, 1 confirmed not carried out"
  );
});

test("mondayShort renders the date in UTC, never the viewer's timezone", () => {
  assert.equal(memory.mondayShort("2026-04-27"), "Apr 27");
  assert.equal(memory.mondayShort(null), "");
});
