// The console and the thread must always describe the same run.
//
// They used to drift: the thread was persisted to DATA/OUTPUTS/<asOf>/thread.jsonl
// while the console feed lived only in the server's memory. A restart, a new
// tab, or a colleague opening the page therefore showed a fully narrated run
// beside a console claiming no run had ever happened.
//
// The fix these tests lock: the console feed and its state snapshot persist
// next to the thread, status() is scoped by Monday, and a run left mid-flight
// by a dead server restores as interrupted rather than as live.
//
// Run with: npm test   (node --test, no dependencies)

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { RunManager, PHASES } = require("../lib/run-manager");

const AS_OF = "2026-05-04";
const OTHER = "2026-04-27";

function tempRepo() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-run-"));
}

// A manager with a run in progress, without spawning python: start() shells
// out, so the state a run builds is assembled directly here.
function withRun(root, asOf, mutate) {
  const mgr = new RunManager(root, "/usr/bin/python3", null);
  mgr.state = {
    running: true,
    started: true,
    asOf,
    inputs: { mode: "repo" },
    autoAccept: false,
    awaitingCorrections: false,
    corrections: null,
    correctionsDecision: null,
    carryIn: null,
    validation: null,
    startedAt: new Date().toISOString(),
    finishedAt: null,
    exitCode: null,
    error: null,
    events: [],
    phases: Object.fromEntries(PHASES.map((p) => [p, "pending"])),
    phaseTimes: {},
    _startedMs: Date.now(),
    _seenArtifacts: new Set(),
    _env: null,
    _extraArgs: [],
    _secret: "must not be served",
  };
  if (mutate) mutate(mgr);
  return mgr;
}

test("a run's console feed and state are persisted, not just held in memory", () => {
  const root = tempRepo();
  const mgr = withRun(root, AS_OF);
  mgr._event("validation", "server", "21 checks ran, 4 corrections proposed.");
  mgr._markDone("validation");

  const feed = path.join(root, "DATA", "OUTPUTS", AS_OF, "console.jsonl");
  const snap = path.join(root, "DATA", "OUTPUTS", AS_OF, "run_state.json");
  assert.ok(fs.existsSync(feed), "console.jsonl should exist beside thread.jsonl");
  assert.ok(fs.existsSync(snap), "run_state.json should exist");

  const lines = fs.readFileSync(feed, "utf8").trim().split("\n").map(JSON.parse);
  assert.equal(lines.length, 1);
  assert.match(lines[0].text, /21 checks ran/);
  assert.equal(JSON.parse(fs.readFileSync(snap, "utf8")).phases.validation, "done");
});

test("a finished run restores from disk for a brand new server", () => {
  const root = tempRepo();
  const first = withRun(root, AS_OF);
  first._event("validation", "server", "21 checks ran, 4 corrections proposed.");
  first._markDone("validation");
  first._event("digest", "server", "Pipeline finished (exit 0).");
  first._finish(0, null);

  // A new process: nothing in memory at all.
  const fresh = new RunManager(root, "/usr/bin/python3", null);
  const status = fresh.status(AS_OF);

  assert.equal(status.started, true, "the console must not claim the run never happened");
  assert.equal(status.running, false);
  assert.equal(status.restored, true);
  assert.equal(status.interrupted, false);
  assert.equal(status.exitCode, 0);
  assert.equal(status.events.length, 2);
  assert.match(status.events.at(-1).text, /Pipeline finished/);
});

test("a run killed mid-flight restores as interrupted, never as live", () => {
  const root = tempRepo();
  const dying = withRun(root, AS_OF);
  dying._event("signals", "server", "Signal engine running…");
  // No _finish: the server died here, so the snapshot still says running.
  assert.equal(JSON.parse(fs.readFileSync(dying._stateFile(AS_OF), "utf8")).running, true);

  const status = new RunManager(root, "/usr/bin/python3", null).status(AS_OF);
  assert.equal(status.running, false, "a dead run must never look live");
  assert.equal(status.interrupted, true);
  assert.equal(status.awaitingCorrections, false);
  assert.match(status.error, /interrupted/i);
});

test("status is scoped to the Monday asked for, not to whatever ran last", () => {
  const root = tempRepo();
  const mgr = withRun(root, AS_OF);
  mgr._event("validation", "server", "this Monday's run");

  // The live run is 05-04; asking about 04-27 must not return it.
  const other = mgr.status(OTHER);
  assert.equal(other.started, false);
  assert.equal(other.asOf, OTHER);
  assert.match(other.message, new RegExp(OTHER));

  const mine = mgr.status(AS_OF);
  assert.equal(mine.running, true);
  assert.equal(mine.restored, false);
});

test("a paused run's pending decision survives a reload", () => {
  const root = tempRepo();
  const mgr = withRun(root, AS_OF);
  mgr.state.corrections = [{ id: "C1", description: "drop 9 duplicate rows" }];
  mgr.state.awaitingCorrections = true;
  mgr._persistState();

  const snap = JSON.parse(fs.readFileSync(mgr._stateFile(AS_OF), "utf8"));
  assert.equal(snap.awaitingCorrections, true);
  assert.equal(snap.corrections[0].id, "C1");
});

test("Step 0 is served from run context before digest assembly finishes", () => {
  const root = tempRepo();
  const mgr = withRun(root, AS_OF);
  const carryIn = {
    previous_run: OTHER,
    is_first_run: false,
    open_recommendation_count: 2,
    due_this_week: ["REC-A", "REC-B"],
    new_data: { corrections_carried: 4, corrections_new: 0 },
  };
  writeLog(root, `DATA/OUTPUTS/${AS_OF}/run_context.jsonl`, [
    { seq: 0, kind: "carry_in", phase: null, payload: carryIn },
  ]);

  mgr._pollArtifacts();

  assert.deepEqual(mgr.status(AS_OF).carryIn, carryIn);
  assert.equal(mgr.state.phases.signals, "pending");
  assert.equal(
    fs.existsSync(path.join(root, "DATA", "OUTPUTS", AS_OF, "digest.json")),
    false,
    "the memory band must not wait for digest.json"
  );
});

test("internal bookkeeping is never served or persisted", () => {
  const root = tempRepo();
  const mgr = withRun(root, AS_OF);
  mgr._event("validation", "server", "line");

  assert.equal(mgr.status(AS_OF)._secret, undefined);
  assert.equal(
    JSON.parse(fs.readFileSync(mgr._stateFile(AS_OF), "utf8"))._secret,
    undefined
  );
});

// A snapshot is the fast path, not the only one: runs recorded before the
// server persisted its console (and any checkout with committed DATA/OUTPUTS/) have
// only the pipeline's own run logs. The console must rebuild from those rather
// than tell a leader the run never happened while the thread narrates it.
function writeLog(root, rel, rows) {
  const file = path.join(root, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, rows.map((r) => JSON.stringify(r) + "\n").join(""));
}

function runWithLogsOnly(root, asOf) {
  writeLog(root, `DATA/OUTPUTS/validation/runlog.jsonl`, [
    { ts: `${asOf}T09:00:00+00:00`, step: "validate", check: "locations.maturity_tier_vs_opened_date", result: "flag", counts: { mismatched_labels: 7 } },
  ]);
  fs.writeFileSync(
    path.join(root, "DATA", "OUTPUTS", "validation", "report.json"),
    JSON.stringify({ as_of: asOf, totals: { checks_run: 21, checks_flagged: 9, corrections_proposed: 4 } })
  );
  writeLog(root, `DATA/OUTPUTS/${asOf}/signals_runlog.jsonl`, [
    { ts: `${asOf}T09:00:01+00:00`, step: "signals", event: "summary", as_of: asOf, counts: { scored: 121, digest: 3 } },
  ]);
  writeLog(root, `DATA/OUTPUTS/${asOf}/runlog.jsonl`, [
    { ts: `${asOf}T09:00:02+00:00`, step: "verdicts", event: "summary", as_of: asOf, counts: { plans: 10 } },
    { ts: `${asOf}T09:00:03+00:00`, step: "run", event: "digest_assembled", as_of: asOf },
  ]);
  // The ledger log is append-only across runs AND weeks: an earlier attempt at
  // this same Monday sits in here too.
  writeLog(root, `DATA/OUTPUTS/ledger_log.jsonl`, [
    { ts: `${asOf}T07:00:00+00:00`, step: "ledger", event: "run_summary", as_of: asOf, counts: { new: 7 } },
    { ts: `${asOf}T09:00:03+00:00`, step: "ledger", event: "run_summary", as_of: asOf, counts: { new: 2 } },
    { ts: "2026-04-27T09:00:03+00:00", step: "ledger", event: "run_summary", as_of: OTHER, counts: { new: 5 } },
  ]);
  fs.writeFileSync(path.join(root, "DATA", "OUTPUTS", asOf, "digest.json"), JSON.stringify({ as_of: asOf }));
  fs.mkdirSync(path.join(root, "DATA", "TRANSLATION"), { recursive: true });
  fs.writeFileSync(
    path.join(root, "DATA", "TRANSLATION", "MANIFEST.json"),
    JSON.stringify({ as_of: asOf, created_at: `${asOf}T09:00:00+00:00`, corrections_accepted: ["C1", "C2"], corrections_declined: ["C3"] })
  );
}

test("a run with no console snapshot rebuilds from the pipeline's own run logs", () => {
  const root = tempRepo();
  runWithLogsOnly(root, AS_OF);

  const status = new RunManager(root, "/usr/bin/python3", null).status(AS_OF);
  assert.equal(status.started, true, "the run happened — the console must show it");
  assert.equal(status.restored, true);
  assert.equal(status.reconstructed, true, "the UI must be able to say where this came from");
  assert.equal(status.interrupted, false);
  assert.equal(status.exitCode, 0);
  for (const p of PHASES) assert.equal(status.phases[p], "done", `${p} ran`);

  // Events are the log lines, in time order, with their plain-English rendering.
  assert.equal(status.events.length, 5, "4 per-Monday logs + 1 validation check");
  assert.equal(
    status.events.filter((e) => /new=7/.test(e.text)).length,
    0,
    "an earlier attempt at this Monday is not part of this run"
  );
  // A phase lasts as long as its own events say — a stray older ledger line
  // would stretch digest assembly over hours.
  const span = (p) =>
    Date.parse(status.phaseTimes[p].endedAt) - Date.parse(status.phaseTimes[p].startedAt);
  assert.equal(span("digest"), 0, "digest assembly took a second, not two hours");
  assert.ok(Date.parse(status.startedAt) >= Date.parse(`${AS_OF}T09:00:00+00:00`));
  assert.deepEqual(
    status.events.map((e) => e.phase),
    ["validation", "signals", "verdicts", "digest", "digest"]
  );
  assert.ok(status.events.every((e) => e.ts));
  assert.equal(status.validation.checks_run, 21);
  assert.deepEqual(status.correctionsDecision.accepted, ["C1", "C2"]);
  assert.deepEqual(status.correctionsDecision.declined, ["C3"]);
});

test("a rebuilt console never borrows another Monday's records", () => {
  const root = tempRepo();
  runWithLogsOnly(root, AS_OF);
  // The shared logs/report now belong to a later validate pass for 04-27.
  fs.writeFileSync(
    path.join(root, "DATA", "OUTPUTS", "validation", "report.json"),
    JSON.stringify({ as_of: OTHER, totals: { checks_run: 21, checks_flagged: 9, corrections_proposed: 4 } })
  );
  fs.writeFileSync(
    path.join(root, "DATA", "TRANSLATION", "MANIFEST.json"),
    JSON.stringify({ as_of: OTHER, corrections_accepted: ["C9"], corrections_declined: [] })
  );

  const status = new RunManager(root, "/usr/bin/python3", null).status(AS_OF);
  assert.equal(status.validation, null, "another Monday's checks are not this run's");
  assert.equal(status.correctionsDecision, null, "unknown beats invented");
  assert.ok(
    status.events.every((e) => e.phase !== "validation"),
    "the shared validation log belongs to the other Monday now"
  );
  // 04-27's ledger line must not appear in 05-04's feed.
  assert.equal(status.events.filter((e) => /new=5/.test(e.text)).length, 0);

  // And a Monday that never ran still says so.
  const never = new RunManager(root, "/usr/bin/python3", null).status(OTHER);
  assert.equal(never.started, false);
  assert.match(never.message, new RegExp(OTHER));
});

test("a snapshot wins over the rebuild — the exact record beats the derived one", () => {
  const root = tempRepo();
  runWithLogsOnly(root, AS_OF);
  const mgr = withRun(root, AS_OF);
  mgr._event("validation", "server", "the snapshot's own line");
  mgr._finish(0, null);

  const status = new RunManager(root, "/usr/bin/python3", null).status(AS_OF);
  assert.equal(status.reconstructed, undefined, "snapshot path, not the rebuild");
  assert.match(status.events[0].text, /snapshot's own line/);
  assert.ok(
    status.events.every((e) => e.source !== "runlog"),
    "the rebuilt log lines are not mixed into a snapshot's feed"
  );
});

test("a re-run archives the previous console instead of appending to it", () => {
  const root = tempRepo();
  const first = withRun(root, AS_OF);
  first._event("validation", "server", "the first attempt");
  first._finish(0, null);

  const second = withRun(root, AS_OF);
  second._rotateConsole(AS_OF);
  second._event("validation", "server", "the second attempt");

  const feed = fs.readFileSync(second._consoleFile(AS_OF), "utf8").trim().split("\n");
  assert.equal(feed.length, 1, "a re-run starts a clean console");
  assert.match(JSON.parse(feed[0]).text, /second attempt/);

  const archive = path.join(root, "DATA", "OUTPUTS", AS_OF, "thread_archive");
  const kept = fs.readdirSync(archive);
  assert.ok(kept.some((f) => f.startsWith("console-")), "the old feed is kept, not deleted");
  assert.ok(kept.some((f) => f.startsWith("run_state-")));
});
