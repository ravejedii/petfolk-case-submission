// Session reset — the demo must be repeatable, and nothing may be deleted.
//
// POST /api/reset promises four things, locked here:
//   the Monday's thread + console rotate into thread_archive/ (kept, whole),
//   any pipeline step in flight is genuinely stopped (the process exits),
//   the console then reports a clean start — even though the pipeline's own
//   run logs are still on disk, the archived run is NOT rebuilt from them,
//   and DATA/TRANSLATION/ is rebuilt from DATA/INPUTS/ by the real validate step.
//
// Run with: npm test   (node --test, no dependencies)

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { RunManager, PHASES } = require("../lib/run-manager");
const { ThreadStore } = require("../lib/thread");

const AS_OF = "2026-05-04";

function tempRepo() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-reset-"));
}

// A stand-in "python" that behaves like `pipeline.validate --accept all` from
// the server's point of view: exits 0 and leaves evidence it was invoked with
// the right arguments. The reset flow spawns and awaits it for real.
function fakePython(root) {
  const bin = path.join(root, "fake-python");
  fs.writeFileSync(
    bin,
    `#!/bin/sh\nmkdir -p "$PWD/DATA/TRANSLATION"\necho "$@" > "$PWD/DATA/TRANSLATION/.rebuild-args"\necho "Applied: C1"\n`,
    { mode: 0o755 }
  );
  return bin;
}

function liveState(asOf) {
  return {
    running: true,
    started: true,
    asOf,
    inputs: { mode: "repo" },
    autoAccept: false,
    awaitingCorrections: false,
    corrections: null,
    correctionsDecision: null,
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
  };
}

test("reset archives the thread and console, then reports a clean start", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const mgr = new RunManager(root, fakePython(root), threads);

  // A finished run with both surfaces written, plus the pipeline's own logs on
  // disk — the raw material the console could (wrongly) rebuild the run from.
  mgr.state = liveState(AS_OF);
  mgr._event("validation", "server", "21 checks ran.");
  mgr._thread("ai", "narration", { text: "Run started.", phase: "validation" });
  mgr._finish(0, null);
  const outDir = path.join(root, "DATA", "OUTPUTS", AS_OF);
  fs.writeFileSync(path.join(outDir, "digest.json"), JSON.stringify({ as_of: AS_OF }));
  fs.writeFileSync(
    path.join(outDir, "runlog.jsonl"),
    JSON.stringify({ ts: `${AS_OF}T09:00:00+00:00`, step: "run", event: "digest_assembled" }) + "\n"
  );

  const result = await mgr.reset(AS_OF, "Lucas");
  assert.equal(result.ok, true);
  assert.equal(result.stoppedRun, false);
  assert.equal(result.dataRebuilt, true);
  assert.match(result.threadArchived, /thread_archive\/thread-.*\.jsonl$/);

  // Nothing deleted: the archive holds the previous thread, console, and state.
  const kept = fs.readdirSync(path.join(outDir, "thread_archive"));
  assert.ok(kept.some((f) => f.startsWith("thread-")));
  assert.ok(kept.some((f) => f.startsWith("console-")));
  assert.ok(kept.some((f) => f.startsWith("run_state-")));

  // The console reports a clean start — no resurrection from runlog.jsonl,
  // which is still sitting right there on disk.
  const status = mgr.status(AS_OF);
  assert.equal(status.started, false, "after a reset, no run is recorded");
  assert.equal(status.reset, true);
  assert.equal(status.events.length, 0);
  assert.match(status.message, /ask me anything about this workflow/i);
  for (const p of PHASES) assert.equal(status.phases[p], "pending");

  // A brand-new server (fresh memory) reads the same clean start from disk.
  const fresh = new RunManager(root, fakePython(root), threads);
  assert.equal(fresh.status(AS_OF).started, false);
  assert.equal(fresh.status(AS_OF).reset, true);

  // The fresh thread opens with the system entry, and only that.
  const entries = threads.read(AS_OF);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].role, "system");
  // The panel's empty state invites a question; it never reports internal
  // folder paths to a leader.
  assert.match(entries[0].payload.text, /Ask me anything about this workflow/);
  assert.doesNotMatch(entries[0].payload.text, /DATA\//);

  // DATA/TRANSLATION/ was rebuilt by the real spawn, with the canonical arguments.
  const args = fs.readFileSync(path.join(root, "DATA", "TRANSLATION", ".rebuild-args"), "utf8");
  assert.match(args, /-m pipeline\.validate --as-of 2026-05-04 --accept all/);
});

test("reset stops a run in flight — the process really exits", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const mgr = new RunManager(root, fakePython(root), threads);
  mgr.state = liveState(AS_OF);
  mgr.state.phases.signals = "active";
  mgr._persistState();

  // A long-lived child standing in for a pipeline step mid-run.
  const child = spawn("sleep", ["60"], { stdio: "ignore" });
  mgr._child = child;

  const result = await mgr.reset(AS_OF, null);
  assert.equal(result.ok, true);
  assert.equal(result.stoppedRun, true);
  assert.ok(
    child.exitCode !== null || child.signalCode !== null,
    "the in-flight process must actually be gone"
  );
  assert.equal(mgr.state, null, "in-memory run state is cleared");
  assert.equal(mgr.isRunning(), false);

  // The archived console records why the run ended; the live status is clean.
  const archiveDir = path.join(root, "DATA", "OUTPUTS", AS_OF, "thread_archive");
  const consoleFile = fs
    .readdirSync(archiveDir)
    .find((f) => f.startsWith("console-"));
  const feed = fs.readFileSync(path.join(archiveDir, consoleFile), "utf8");
  assert.match(feed, /Run stopped — session reset/);
  assert.equal(mgr.status(AS_OF).started, false);
});

test("a run can start again after a reset, on a clean page", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const mgr = new RunManager(root, fakePython(root), threads);
  mgr.state = liveState(AS_OF);
  mgr._event("validation", "server", "the run before the reset");
  mgr._finish(0, null);

  await mgr.reset(AS_OF, null);

  // A new run's console starts empty (the reset marker rotates away like any
  // retired snapshot), and its first event is the new run's own.
  mgr.state = liveState(AS_OF);
  mgr._rotateConsole(AS_OF);
  mgr._event("validation", "server", "the run after the reset");
  const feed = fs
    .readFileSync(path.join(root, "DATA", "OUTPUTS", AS_OF, "console.jsonl"), "utf8")
    .trim()
    .split("\n");
  assert.equal(feed.length, 1);
  assert.match(JSON.parse(feed[0]).text, /after the reset/);
});

test("reset reports a rebuild failure instead of hiding it", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const bad = path.join(root, "broken-python");
  fs.writeFileSync(bad, `#!/bin/sh\necho "boom" >&2\nexit 3\n`, { mode: 0o755 });
  const mgr = new RunManager(root, bad, threads);

  const result = await mgr.reset(AS_OF, null);
  assert.equal(result.ok, true, "the archive/clean-state half still happened");
  assert.equal(result.dataRebuilt, false);
  assert.match(result.dataRebuildError, /exited 3/);
  const entries = threads.read(AS_OF);
  // A failure is still stated plainly — it just does not name internal paths.
  assert.match(entries[0].payload.text, /could not be prepared/i);
});
