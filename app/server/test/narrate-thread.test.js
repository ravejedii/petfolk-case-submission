// LLM narration in the thread — the side panel must read as real conversation.
//
// The contract these tests lock (pipeline.narrate, wired into the app):
//   ONE narrator per run. The first completed phase starts the REAL pipeline
//   module (`python -m pipeline.narrate --follow --stream`) async — the run
//   never waits — and that single process walks every phase in order over one
//   accumulating context, announcing each checked note as a `phase_final`
//   event. The run manager appends each one to the thread with its tier
//   metadata (mode / decided_by / fallbacks), so the UI can label who actually
//   wrote it. The deterministic one-liner posted at phase completion is only a
//   placeholder: the thread read layer marks it superseded once the narrative
//   lands (the JSONL itself stays append-only, never rewritten).
//   If narration fails, a system note says so and the placeholder stands —
//   the same honest-fallback pattern as ask-engine: nothing is invented.
//
// Run with: npm test   (node --test, no dependencies)

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { RunManager, PHASES } = require("../lib/run-manager");
const { ThreadStore } = require("../lib/thread");

const AS_OF = "2026-05-04";

const NARRATIVE_TEXT =
  "Of the 121 combinations scored, three rose above the noise this Monday.";

function tempRepo() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-narrate-"));
}

// A stand-in "python" that answers `-m pipeline.narrate --follow --stream` the
// way the real module does: JSONL progress events, a `phase_final` carrying
// each checked note as it passes, then a final event with the phase-keyed
// document. (`-m pipeline.validate` calls — reset's rebuild — exit 0 quietly.)
function fakeNarratePython(root, opts = {}) {
  const bin = path.join(root, "fake-python");
  const narratives = JSON.stringify({
    signals: {
      phase: "signals",
      title: "Signal engine",
      text: NARRATIVE_TEXT,
      mode: "claude-cli",
      decided_by: opts.decidedBy || "claude-cli",
      checks: {
        number_check: "pass",
        entity_check: "pass",
        rule_check: "pass",
        language_check: "pass",
        shape_check: "pass",
        numbers_verified: 5,
        entities_verified: 1,
      },
      receipts: [
        { token: "7.2", path: "DATA/OUTPUTS/2026-05-04/signals.json",
          field: "signals[0].scores.drift.recent_level", value: 7.25 },
      ],
      artifacts_read: ["DATA/OUTPUTS/2026-05-04/signals.json"],
      attempts: 1,
      fallbacks: opts.fallbacks || [],
      elapsed_seconds: 1.2,
    },
  });
  // Under --follow the narrator announces each phase's checked note as it
  // passes, then prints the whole document as its last line.
  const phaseFinal =
    `{"phase":"signals","type":"phase_final","narrative":${JSON.stringify(
      JSON.parse(narratives).signals
    )}}`;
  const result = `printf '%s\n' '${phaseFinal}'
echo '{"type":"final","narratives":${narratives}}'`;
  // Progress events the real module emits while the model writes. Each is
  // printed on its own line, with a pause so a test can observe the middle.
  const progress = (opts.deltas || [])
    .map(
      (text) =>
        `printf '%s\\n' '${JSON.stringify({ phase: "signals", type: "delta", text })}'\n` +
        `sleep ${opts.deltaPause || 0.1}`
    )
    .join("\n");
  const body = opts.fail
    ? `#!/bin/sh\ncase "$*" in *pipeline.validate*) exit 0;; esac\necho "boom: no artifacts" >&2\nexit 2\n`
    : opts.sleep
      ? `#!/bin/sh\ncase "$*" in *pipeline.validate*) exit 0;; esac\nsleep ${opts.sleep}\n${result}\n`
      : `#!/bin/sh\ncase "$*" in *pipeline.validate*) exit 0;; esac\necho "$@" > "$PWD/.narrate-args"\n${progress}\n${result}\n`;
  fs.writeFileSync(bin, body, { mode: 0o755 });
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
    narrations: {},
    _startedMs: Date.now(),
    _seenArtifacts: new Set(),
    _narrated: new Set(),
    _env: null,
    _extraArgs: [],
  };
}

// A manager mid-run with validation already done (so completing "signals"
// narrates exactly that one phase) and the signals artifact on disk.
function signalsRun(root, python) {
  const threads = new ThreadStore(root);
  const mgr = new RunManager(root, python, threads);
  mgr.state = liveState(AS_OF);
  mgr.state.phases.validation = "done";
  mgr.state._narrated.add("validation");
  const outDir = path.join(root, "DATA", "OUTPUTS", AS_OF);
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, "signals.json"), JSON.stringify({ as_of: AS_OF }));
  return { mgr, threads };
}

async function until(fn, ms = 8000) {
  const t0 = Date.now();
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() - t0 > ms) throw new Error("condition not met in time");
    await new Promise((r) => setTimeout(r, 50));
  }
}

test("a completed phase is narrated by pipeline.narrate and the narrative replaces the placeholder", async () => {
  const root = tempRepo();
  const { mgr, threads } = signalsRun(root, fakeNarratePython(root));

  // The deterministic phase summary lands first — the immediate placeholder.
  mgr._threadPlaceholder("signals", "Signal engine: 121 scored, 3 ranked.");
  mgr._markDone("signals");
  assert.equal(mgr.state.narrations.signals.status, "generating");
  assert.deepEqual(mgr.narrating(AS_OF).map((n) => n.phase), ["signals"]);

  const narrative = await until(() =>
    threads.read(AS_OF).find((e) => e.payload && e.payload.kind === "narrative")
  );

  // The narrative is the module's own output, verbatim, with tier metadata.
  assert.equal(narrative.role, "ai");
  assert.equal(narrative.type, "narration");
  assert.equal(narrative.payload.text, NARRATIVE_TEXT);
  assert.equal(narrative.payload.phase, "signals");
  assert.equal(narrative.payload.mode, "claude-cli");
  assert.equal(narrative.payload.decided_by, "claude-cli");
  assert.equal(narrative.payload.numbers_verified, 5);
  // The receipt map rides along verbatim — the panel renders it, the server
  // never derives it — and the model's own text is the only "ai" entry here.
  assert.equal(narrative.payload.entities_verified, 1);
  assert.deepEqual(narrative.payload.artifacts_read, ["DATA/OUTPUTS/2026-05-04/signals.json"]);
  assert.equal(narrative.payload.receipts.length, 1);
  assert.equal(narrative.payload.receipts[0].field, "signals[0].scores.drift.recent_level");
  assert.equal(narrative.payload.checks.entity_check, "pass");

  // The real module was invoked ONCE for the whole run, in follow mode — not
  // once per phase. `--phases` must not appear: a narrator scoped to a single
  // phase could not carry the run's accumulated context into the next one.
  const args = fs.readFileSync(path.join(root, ".narrate-args"), "utf8");
  assert.match(args, /-m pipeline\.narrate --as-of 2026-05-04 --follow/);
  assert.match(args, /--stream/);
  assert.doesNotMatch(args, /--phases/);

  // The placeholder is superseded at read time; the JSONL is untouched.
  const placeholder = threads
    .read(AS_OF)
    .find((e) => e.payload && e.payload.placeholder);
  assert.equal(placeholder.payload.superseded, true);
  // A line the SERVER composed is posted as "system": only text a model wrote
  // is credited to the AI.
  assert.equal(placeholder.role, "system");
  const raw = fs.readFileSync(path.join(root, "DATA", "OUTPUTS", AS_OF, "thread.jsonl"), "utf8");
  assert.ok(!raw.includes("superseded"), "the file itself is never rewritten");

  // The run state records the narration honestly, and narrating() is empty.
  assert.equal(mgr.state.narrations.signals.status, "done");
  assert.equal(mgr.state.narrations.signals.decided_by, "claude-cli");
  assert.deepEqual(mgr.narrating(AS_OF), []);

  // The console (Engineer view) got the raw lifecycle lines, phase-tagged.
  // The narrator is announced once for the run, not once per phase.
  const texts = mgr.state.events.map((e) => e.text);
  assert.equal(texts.filter((t) => /Narrating the run/.test(t)).length, 1);
  assert.ok(texts.some((t) => /Narrative for Signal engine ready — written by claude-cli/.test(t)));
});

test("a narrative streams as it is written, and only the checked text is kept", async () => {
  // A narration is the run talking about itself, so it types like an answer
  // does. What must NOT change: the draft is never written to the thread —
  // the phase's narrative is the one pipeline.narrate finished checking.
  const root = tempRepo();
  const { mgr, threads } = signalsRun(
    root,
    fakeNarratePython(root, { deltas: ["NARRATIVE: The signal engine ", "scored 121 pairs."] })
  );
  const seen = [];
  mgr.subscribe(AS_OF, (e) => seen.push(e));

  mgr._threadPlaceholder("signals", "Signal engine: 121 scored, 3 ranked.");
  mgr._markDone("signals");

  await until(() => seen.filter((e) => e.type === "narration_delta").length === 2);
  // Mid-narrative: text is flowing, the thread has no narrative yet, and the
  // live state says which phase is writing what.
  assert.equal(
    threads.read(AS_OF).filter((e) => e.payload && e.payload.kind === "narrative").length,
    0
  );
  const live = mgr.narrating(AS_OF)[0];
  assert.equal(live.phase, "signals");
  assert.equal(live.text, "NARRATIVE: The signal engine scored 121 pairs.");
  assert.equal(live.provisional, true);
  // The live channel also carries the console's own lines and rail updates,
  // so the deltas are found by type rather than by position.
  const firstDelta = seen.find((e) => e.type === "narration_delta");
  assert.equal(firstDelta.asOf, AS_OF); // every event names its Monday and phase
  assert.equal(firstDelta.phase, "signals");

  // …then the checked narrative lands in the thread and the draft is done.
  const narrative = await until(() =>
    threads.read(AS_OF).find((e) => e.payload && e.payload.kind === "narrative")
  );
  assert.equal(narrative.payload.text, NARRATIVE_TEXT);
  assert.equal(narrative.payload.numbers_verified, 5);
  // The receipt map rides along verbatim — the panel renders it, the server
  // never derives it — and the model's own text is the only "ai" entry here.
  assert.equal(narrative.payload.entities_verified, 1);
  assert.deepEqual(narrative.payload.artifacts_read, ["DATA/OUTPUTS/2026-05-04/signals.json"]);
  assert.equal(narrative.payload.receipts.length, 1);
  assert.equal(narrative.payload.receipts[0].field, "signals[0].scores.drift.recent_level");
  assert.equal(narrative.payload.checks.entity_check, "pass");
  assert.ok(seen.some((e) => e.type === "narration_final" && e.phase === "signals"));
  assert.deepEqual(mgr.narrating(AS_OF), []);
});

test("failed narration annotates the thread; the placeholder stands, nothing invented", async () => {
  const root = tempRepo();
  const { mgr, threads } = signalsRun(root, fakeNarratePython(root, { fail: true }));

  mgr._threadPlaceholder("signals", "Signal engine: 121 scored, 3 ranked.");
  mgr._markDone("signals");

  const note = await until(() =>
    threads.read(AS_OF).find((e) => e.role === "system" && e.type === "note")
  );
  assert.match(note.payload.text, /Signal engine narrative could not be generated/);
  assert.match(note.payload.text, /the deterministic summary above stands/);
  assert.match(note.payload.text, /Nothing was invented/);

  // No narrative entry exists, so the placeholder is NOT superseded.
  const entries = threads.read(AS_OF);
  assert.ok(!entries.some((e) => e.payload && e.payload.kind === "narrative"));
  const placeholder = entries.find((e) => e.payload && e.payload.placeholder);
  assert.equal(placeholder.payload.superseded, undefined);

  assert.equal(mgr.state.narrations.signals.status, "failed");
  assert.match(mgr.state.narrations.signals.error, /exited 2/);
});

test("the thread supersedes placeholders per phase, and only once a narrative follows", () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  threads.append(AS_OF, "ai", "narration", { text: "signals one-liner", phase: "signals", placeholder: true });
  threads.append(AS_OF, "ai", "narration", { text: "digest one-liner", phase: "digest", placeholder: true });
  threads.append(AS_OF, "ai", "narration", { kind: "narrative", phase: "signals", text: "the signals story" });

  const byText = (t) => threads.read(AS_OF).find((e) => e.payload.text === t);
  assert.equal(byText("signals one-liner").payload.superseded, true);
  assert.equal(byText("digest one-liner").payload.superseded, undefined, "another phase's narrative never supersedes it");
  assert.equal(byText("the signals story").payload.superseded, undefined);
});

test("a session reset stops narration in flight — no late narrative lands in the fresh thread", async () => {
  const root = tempRepo();
  const { mgr, threads } = signalsRun(root, fakeNarratePython(root, { sleep: 5 }));

  mgr._markDone("signals");
  await until(() => mgr._narrateChildren.size > 0);

  const result = await mgr.reset(AS_OF, null);
  assert.equal(result.ok, true);

  // Give a killed-too-late close handler every chance to (wrongly) write.
  await new Promise((r) => setTimeout(r, 500));
  const entries = threads.read(AS_OF);
  assert.equal(entries.length, 1, "only the reset system entry");
  assert.equal(entries[0].role, "system");
  assert.ok(!entries.some((e) => e.payload && e.payload.kind === "narrative"));
  assert.equal(mgr._narrateChildren.size, 0, "no orphaned narrate process tracked");
});
