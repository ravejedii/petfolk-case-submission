// A restored console must be as legible as a live one, and its clocks must
// measure the pipeline.
//
// Two bugs this locks out, both seen on screen in the AI Strategy Lead view:
//
//   1. The Data Validation & Check box rendered EMPTY in the leader view.
//      Every one of that phase's 22 events was a server note, a pipeline.validate
//      stdout line, or a narrate check receipt — none of which had a plain-English
//      rendering, so the leader filter dropped all of them and left a black box
//      beside a phase that had done real work. Restoring now re-translates any
//      line whose `plain` is null, so a run recorded by an earlier build reads
//      correctly without regenerating a single artifact.
//
//   2. Phase timers read "0s". The stored times were stamped when the tail
//      poller first noticed a line of a phase and when it marked the phase
//      done — and the poller batches, so a whole step could land in one sweep.
//      Spans are now recomputed from the pipeline's own timestamps.
//
// The fixture is the repo's committed run: DATA/OUTPUTS/2026-05-04/console.jsonl +
// run_state.json, copied into a temp repo root. Real recorded events, not
// invented ones — if the real feed cannot be made legible, this fails.
//
// Run with: npm test   (node --test, no dependencies)

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { RunManager } = require("../lib/run-manager");

const AS_OF = "2026-05-04";
const REPO = path.resolve(__dirname, "..", "..", "..");
const FIXTURE = path.join(REPO, "DATA", "OUTPUTS", AS_OF);

// A temp repo root holding only this Monday's recorded console + snapshot.
function tempRepoWithRun() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-console-"));
  const dir = path.join(root, "DATA", "OUTPUTS", AS_OF);
  fs.mkdirSync(dir, { recursive: true });
  for (const f of ["console.jsonl", "run_state.json"]) {
    fs.copyFileSync(path.join(FIXTURE, f), path.join(dir, f));
  }
  return root;
}

function statusOrSkip(t) {
  if (!fs.existsSync(path.join(FIXTURE, "console.jsonl"))) {
    t.skip(`no recorded console at DATA/OUTPUTS/${AS_OF}/console.jsonl`);
    return null;
  }
  return new RunManager(tempRepoWithRun(), "/usr/bin/python3", null).status(AS_OF);
}

test("every restored Data Validation & Check line is leader-legible", (t) => {
  const status = statusOrSkip(t);
  if (!status) return;

  const validation = status.events.filter((e) => e.phase === "validation");
  assert.ok(validation.length > 0, "the recorded run has validation events");
  const silent = validation.filter((e) => !e.plain);
  assert.deepEqual(
    silent.map((e) => e.text),
    [],
    "a validation line with no plain-English rendering leaves an empty box on screen"
  );
});

test("no leader-facing line carries a filesystem path", (t) => {
  const status = statusOrSkip(t);
  if (!status) return;

  const leaked = status.events
    .map((e) => e.plain)
    .filter((p) => p && (p.includes("/Users/") || p.includes("/tmp/") || p.includes("/var/folders/")));
  assert.deepEqual(leaked, [], "a leader never sees this machine's filesystem");
});

test("phase spans come from the pipeline's clock, not the poller's", (t) => {
  const status = statusOrSkip(t);
  if (!status) return;

  for (const phase of ["validation", "signals", "verdicts", "digest"]) {
    const span = status.phaseTimes[phase];
    assert.ok(span && span.startedAt && span.endedAt, `${phase} has a span`);
    const ms = Date.parse(span.endedAt) - Date.parse(span.startedAt);
    assert.ok(ms > 0, `${phase} took real time (got ${ms}ms) — "0s" is a bug, not a fact`);
  }
  assert.ok(
    Date.parse(status.phaseTimes.signals.endedAt) >
      Date.parse(status.phaseTimes.signals.startedAt),
    "the signal engine did work; its clock must say so"
  );

  // Narration is spawned after its phase completes and runs alongside the rest
  // of the pipeline, so it must never stretch the phase it talks about.
  const narrationEnd = status.events
    .filter((e) => e.phase === "validation" && /^Narrative for /.test(e.text || ""))
    .map((e) => Date.parse(e.ts));
  if (narrationEnd.length) {
    assert.ok(
      Date.parse(status.phaseTimes.validation.endedAt) < Math.min(...narrationEnd),
      "Data Validation & Check ended before its narrative was written"
    );
  }
});

test("a live run's phase span ends at the last pipeline event, not at the poll", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-console-live-"));
  const mgr = new RunManager(root, "/usr/bin/python3", null);
  mgr.state = {
    running: true,
    started: true,
    asOf: AS_OF,
    inputs: { mode: "repo" },
    events: [],
    phases: { validation: "pending", signals: "pending", verdicts: "pending", digest: "pending" },
    phaseTimes: {},
    _startedMs: Date.now(),
    _phaseLatest: {},
  };

  // One tail sweep delivers a step's worth of log lines at once: the phase's
  // real span is in the lines' own timestamps, not in when they were read.
  const t0 = "2026-08-15T02:59:47+00:00";
  const t1 = "2026-08-15T02:59:58+00:00";
  mgr._event("signals", "runlog", "parameters as_of=2026-05-04", {
    meta: { ts: t0, step: "signals", event: "parameters" },
  });
  mgr._event("signals", "runlog", "summary as_of=2026-05-04", {
    meta: { ts: t1, step: "signals", event: "summary" },
  });
  mgr._markDone("signals");

  const span = mgr.status(AS_OF).phaseTimes.signals;
  assert.equal(Date.parse(span.startedAt), Date.parse(t0));
  assert.equal(Date.parse(span.endedAt), Date.parse(t1));

  // A later line tagged with a finished phase (the run's closing summary
  // print, the async narration) must not stretch it.
  mgr._event("signals", "stdout", "  Top signals (3):");
  mgr._event("signals", "server", "Narrative for Signal engine ready — written by openai, 4 numbers verified, attempts 1, 2.1s.");
  assert.equal(
    Date.parse(mgr.status(AS_OF).phaseTimes.signals.endedAt),
    Date.parse(t1),
    "a done phase's clock is stopped"
  );
});

test("server and stdout lines are translated as they are recorded", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-console-plain-"));
  const mgr = new RunManager(root, "/usr/bin/python3", null);
  mgr.state = {
    running: true,
    started: true,
    asOf: AS_OF,
    inputs: { mode: "repo" },
    events: [],
    phases: { validation: "pending", signals: "pending", verdicts: "pending", digest: "pending" },
    phaseTimes: {},
    _startedMs: Date.now(),
    _phaseLatest: {},
  };
  mgr._refreshPlainCtx();

  mgr._event(
    "validation",
    "server",
    `Started: python -u -m pipeline.validate --as-of ${AS_OF} --accept all (cwd /Users/someone/petfolk)`
  );
  mgr._event("validation", "stdout", "    DATA/TRANSLATION/clinic_weekly.csv: 2211 → 2202 rows, 11 cells changed");

  const [started, rebuilt] = mgr.state.events;
  assert.match(started.plain, /Data Validation & Check/);
  assert.ok(!started.plain.includes("/Users/"), "the command line and cwd are engineer-only");
  assert.match(rebuilt.plain, /2211 rows in, 2202 out, 11 cells changed/);
  // The Engineer view still gets the raw line, unchanged.
  assert.match(started.text, /cwd \/Users\/someone\/petfolk/);
});
