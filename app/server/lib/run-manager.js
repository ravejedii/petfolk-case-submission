// Runs the real Python pipeline and exposes its progress for polling.
//
// Run flow (both repo-inputs and uploaded-files runs):
//   1. `python -m pipeline.validate` (propose only, no --accept) runs first.
//      Its proposed corrections are parsed from
//      DATA/OUTPUTS/validation/corrections_proposed.json.
//   2. Unless autoAccept was requested, the run PAUSES in
//      "awaiting corrections": /api/run/status exposes the proposals and the
//      operator accepts/declines each one (POST /api/run/corrections).
//      Zero proposals skip the pause. autoAccept applies all without pausing.
//   3. `python -m pipeline.validate --accept <ids|all>` applies the decision
//      (declined corrections are logged by the pipeline, never applied;
//      DATA/TRANSLATION/ is rebuilt without them). DATA/INPUTS/ is never modified.
//   4. `python -m pipeline.run` computes the digest as before.
//
// Every event in the feed is REAL: pipeline stdout/stderr, lines the pipeline
// appends to its own run logs (tailed from the byte offset at run start), and
// artifacts landing in DATA/OUTPUTS/. Nothing is animated or invented; if the
// pipeline is silent, the feed is silent. Each event carries:
//   text   the raw line (Engineer view)
//   plain  a plain-English rendering from lib/plain.js, or null (fall back)
//   meta   the parsed run-log event for structured UI (per-plan progress,
//          gate counts) — only for JSONL run-log events
//
// Phases (leader-facing names):
//   validation -> "Data Validation & Check"
//   signals    -> "Signal engine"
//   verdicts   -> "Claimed vs. Verified"
//   digest     -> "Digest assembly" (includes the ledger re-check)

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const {
  loadContext,
  translateLogEvent,
  translateText,
  enrichMeta,
  declinedCorrectionLine,
} = require("./plain");

const PHASES = ["validation", "signals", "verdicts", "digest"];
const MINUTE = 60 * 1000;

// Leader-facing phase titles.
const PHASE_TITLES = {
  validation: "Data Validation & Check",
  signals: "Signal engine",
  verdicts: "Claimed vs. Verified",
  digest: "Digest assembly",
};

// Run narration (pipeline.narrate --follow): ONE process for the whole run,
// and the run can sit on the human corrections gate for as long as a human
// takes — so this budget is the run's, not a single phase's. Generous ceiling:
// the module has its own per-call timeouts and a deterministic template
// fallback, so this only catches a truly wedged process.
const NARRATE_FOLLOW_TIMEOUT_MS = 1_800_000;

// Map a run-log JSONL event to a phase from its `step` field. narrate events
// name the phase they narrate explicitly (evt.phase).
function phaseForStep(step, evt) {
  if (step === "signals") return "signals";
  if (step === "verdicts" || step === "facts") return "verdicts";
  // The successor step runs inside the ledger pass, which the rail shows as
  // part of digest assembly.
  if (step === "ledger" || step === "run" || step === "successor") return "digest";
  if (step === "validate") return "validation";
  if (step === "narrate" && evt && PHASES.includes(evt.phase)) return evt.phase;
  return null;
}

// Map a raw stdout line to a phase by content (the pipeline's own summary
// vocabulary). Unmatched lines carry no phase tag — they are still shown.
function phaseForLine(line) {
  const l = line.toLowerCase();
  if (l.includes("data validation") || l.includes("data checks")) return "validation";
  if (l.includes("signal")) return "signals";
  if (l.includes("claimed vs. verified") || l.includes("verdict") || l.includes("harness"))
    return "verdicts";
  if (l.includes("ledger") || l.includes("digest") || l.includes("rec-")) return "digest";
  return null;
}

// One-line raw rendering of a run-log JSONL event (the Engineer view).
// Values come straight from the event the pipeline wrote — no interpretation
// (objects/arrays are skipped; the JSONL file itself is the full receipt).
function describeLogEvent(evt) {
  const bits = [`${evt.event || evt.check || "event"}`];
  for (const [key, val] of Object.entries(evt)) {
    if (key === "ts" || key === "step" || key === "event") continue;
    if (val === null || typeof val === "object") continue;
    bits.push(`${key}=${val}`);
  }
  return bits.join("  ");
}

// ---------------------------------------------------------------------------
// Honest phase spans
//
// A phase's clock must measure the PIPELINE, not the poller. The tail poller
// batches: it can notice a whole step's worth of log lines in one 300ms sweep,
// which is how a real 12-second step used to render as "0s". So:
//   * an event's time is the pipeline's own timestamp when it has one
//     (run-log lines carry `ts`), else the moment the server saw the line;
//   * a phase starts at the earliest such time among its own events — a line
//     written at 09:00:01 proves the phase was working at 09:00:01, even if
//     the server only read it later;
//   * a phase ends at the latest such time up to the event that COMPLETED it.
//     Everything after belongs to the next step: the run's closing summary
//     print (tagged by content) and the async narration must never stretch a
//     finished phase.
// Run-log timestamps are second-resolution, so a span is accurate to ~1s —
// which is the pipeline's own record, not an invention of this layer.
// ---------------------------------------------------------------------------

function pipelineTime(event) {
  const raw = (event.meta && event.meta.ts) || event.ts;
  const t = Date.parse(raw);
  return Number.isNaN(t) ? null : t;
}

// Narration is spawned AFTER its phase is done and runs alongside the rest of
// the pipeline, so its events are shown in the phase's feed but are never part
// of the phase's span.
function isNarrationEvent(event) {
  if (event.meta && event.meta.step === "narrate") return true;
  const text = event.text || "";
  return (
    (event.source === "server" || event.source === "stdout") &&
    /^(Narrating |Narrative for |Narration for )/.test(text)
  );
}

// The event that completes a phase — the same thing _markDone reacts to on the
// live path, recognized from a persisted feed.
function isPhaseDoneMarker(phase, event) {
  const m = event.meta;
  if (m && m.step) {
    if (phase === "signals") return m.step === "signals" && m.event === "summary";
    if (phase === "verdicts") return m.step === "verdicts" && m.event === "summary";
    if (phase === "digest") return m.event === "digest_assembled";
    return false;
  }
  const text = event.text || "";
  if (phase === "validation")
    return /^Data Validation & Check (finished|exited|proposed no corrections)/.test(text);
  if (phase === "signals") return /^Signal engine wrote signals\.json/.test(text);
  if (phase === "verdicts") return /^Verdicts written/.test(text);
  if (phase === "digest")
    return /^digest\.json assembled/.test(text) || /^Pipeline finished/.test(text);
  return false;
}

// Recompute every phase's span from the feed itself. Used when a run is
// restored or rebuilt from disk: the snapshot's stored times came from an
// earlier, poller-based method, and the events are the better record.
// A phase that is not done keeps a null end — the UI counts it live.
function computePhaseSpans(events, phaseStates) {
  const spans = {};
  for (const p of PHASES) {
    const own = events.filter((e) => e.phase === p && !isNarrationEvent(e));
    const times = own.map(pipelineTime);
    const starts = times.filter((t) => t != null);
    if (!starts.length) {
      spans[p] = { startedAt: null, endedAt: null };
      continue;
    }
    let cut = own.length;
    for (let i = 0; i < own.length; i += 1) {
      if (isPhaseDoneMarker(p, own[i])) {
        cut = i + 1;
        break;
      }
    }
    const startedMs = Math.min(...starts);
    const upTo = times.slice(0, cut).filter((t) => t != null);
    const endedMs = Math.max(startedMs, ...(upTo.length ? upTo : [startedMs]));
    const done =
      !phaseStates || phaseStates[p] === "done" || phaseStates[p] === "failed";
    spans[p] = {
      startedAt: new Date(startedMs).toISOString(),
      endedAt: done ? new Date(endedMs).toISOString() : null,
    };
  }
  return spans;
}

// Run-log events whose plain-English line is also posted to the conversation
// thread (the side panel) as the phase-summary narration.
const THREAD_SUMMARY_EVENTS = new Set([
  "signals:summary",
  "verdicts:summary",
  "ledger:run_summary",
  "run:digest_assembled",
]);

class RunManager {
  // threadStore (lib/thread.js) is optional — without it the run still works,
  // it just has no conversation thread.
  constructor(repoRoot, pythonBin, threadStore) {
    this.repoRoot = repoRoot;
    this.pythonBin = pythonBin;
    this.threadStore = threadStore || null;
    this.state = null; // no run yet this session
    this._timer = null;
    this._tails = [];
    this._plainCtx = { names: {}, slugs: {}, plans: {}, corrections: {} };
    this._child = null; // the python step currently running, so reset can stop it
    this._narrateChildren = new Set(); // in-flight pipeline.narrate processes
    this._resetting = false;
  }

  // Append to the run's conversation thread (DATA/OUTPUTS/<asOf>/thread.jsonl).
  // Returns the appended entry (or null) so callers can reference it.
  _thread(role, type, payload) {
    if (!this.threadStore || !this.state) return null;
    try {
      return this.threadStore.append(this.state.asOf, role, type, payload);
    } catch {
      // the thread must never break a run
      return null;
    }
  }

  _consoleFile(asOf) {
    return path.join(this.repoRoot, "DATA", "OUTPUTS", asOf, "console.jsonl");
  }

  _stateFile(asOf) {
    return path.join(this.repoRoot, "DATA", "OUTPUTS", asOf, "run_state.json");
  }

  // The console feed is persisted next to the thread, for the same reason: both
  // surfaces describe one run, so both must survive a reload, a second browser,
  // and a server restart. Without this the thread showed a finished run while
  // the console claimed no run had ever happened.
  _persistEvent(event) {
    if (!this.state) return;
    try {
      const file = this._consoleFile(this.state.asOf);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.appendFileSync(file, JSON.stringify(event) + "\n");
    } catch {
      // persistence must never break a run
    }
  }

  // Snapshot of everything the console needs besides its events.
  _persistState() {
    if (!this.state) return;
    const snap = {};
    for (const [k, val] of Object.entries(this.state)) {
      if (k.startsWith("_") || k === "events") continue;
      snap[k] = val;
    }
    try {
      const file = this._stateFile(this.state.asOf);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, JSON.stringify(snap, null, 2) + "\n");
    } catch {
      // persistence must never break a run
    }
  }

  // Retire this Monday's console feed + state snapshot into the same archive
  // folder the thread rotates into. Nothing is deleted.
  _rotateConsole(asOf) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const dir = path.join(this.repoRoot, "DATA", "OUTPUTS", asOf, "thread_archive");
    for (const [file, name] of [
      [this._consoleFile(asOf), `console-${stamp}.jsonl`],
      [this._stateFile(asOf), `run_state-${stamp}.json`],
    ]) {
      try {
        if (!fs.existsSync(file)) continue;
        fs.mkdirSync(dir, { recursive: true });
        fs.renameSync(file, path.join(dir, name));
      } catch {
        // rotation must never stop a run
      }
    }
  }

  // Retire this Monday's run context — the ordered record of what each tool
  // returned and what the agent reasoned about it. A run gets one history, so a
  // re-run starts a new one; the old one is archived beside the thread it
  // belonged to, never deleted.
  _rotateRunContext(asOf) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const dir = path.join(this.repoRoot, "DATA", "OUTPUTS", asOf, "thread_archive");
    const file = path.join(this.repoRoot, "DATA", "OUTPUTS", asOf, "run_context.jsonl");
    try {
      if (!fs.existsSync(file)) return;
      fs.mkdirSync(dir, { recursive: true });
      fs.renameSync(file, path.join(dir, `run_context-${stamp}.jsonl`));
    } catch {
      // rotation must never stop a run
    }
  }

  // Step 0 is written as the first line of run_context.jsonl before signals,
  // verdicts, the ledger re-check, or digest assembly can run. Surface that
  // exact record immediately; waiting for digest.json made the live Week 2
  // console look like a fresh report until the work was already over.
  _readCarryIn(asOf, allowDigest = true) {
    const context = path.join(
      this.repoRoot, "DATA", "OUTPUTS", asOf, "run_context.jsonl"
    );
    try {
      for (const line of fs.readFileSync(context, "utf8").split("\n")) {
        if (!line.trim()) continue;
        const entry = JSON.parse(line);
        if (entry.kind === "carry_in" && entry.payload) return entry.payload;
      }
    } catch {
      // The writer may be between creating the file and completing line one;
      // the next 300ms poll tries again.
    }

    // A re-run may still have the last attempt's digest on disk while its new
    // run context is being seeded. Live polling must wait for the new context,
    // never borrow that stale digest.
    if (!allowDigest) return null;

    // Finished runs created before the live status field existed still carry
    // the same immutable Step 0 in digest.json.
    try {
      const digest = JSON.parse(
        fs.readFileSync(
          path.join(this.repoRoot, "DATA", "OUTPUTS", asOf, "digest.json"),
          "utf8"
        )
      );
      return digest.carry_in || null;
    } catch {
      return null;
    }
  }

  _pollCarryIn() {
    if (!this.state || this.state.carryIn) return;
    const carryIn = this._readCarryIn(this.state.asOf, false);
    if (!carryIn) return;
    this.state.carryIn = carryIn;
    this._persistState();
    this._emitRunState();
  }

  // One persisted event -> its plain-English line, recomputed. Run-log events
  // keep their parsed payload in `meta`, which is exactly what translateLogEvent
  // reads; text lines go through the same translator the live path uses.
  _replain(evt, ctx) {
    try {
      if (evt.meta && evt.meta.step) return translateLogEvent(evt.meta, ctx);
      return this._defaultPlainWith(evt.source, evt.text || "", ctx);
    } catch {
      return null; // an odd line must never break a restore
    }
  }

  _defaultPlainWith(source, text, ctx) {
    if (source === "server" || source === "stdout" || source === "artifact") {
      return translateText(text, ctx);
    }
    return null;
  }

  // Rebuild a finished run's console from disk. `running` is never restored as
  // true: a snapshot that still says running belongs to a server that died
  // mid-run, which is reported as interrupted rather than pretended to be live.
  _restore(asOf) {
    let snap;
    try {
      snap = JSON.parse(fs.readFileSync(this._stateFile(asOf), "utf8"));
    } catch {
      return null;
    }
    // A reset marker instead of a run snapshot: the session was reset after the
    // last run, its thread and console were archived, and the console must NOT
    // rebuild that run from the pipeline's logs — the thread beside it is empty
    // now, and the two surfaces tell one story. This is the clean start state.
    if (snap.reset) {
      return {
        running: false,
        started: false,
        restored: false,
        interrupted: false,
        reset: true,
        resetAt: snap.resetAt || null,
        asOf,
        message:
          "Ready. Ask me anything about this workflow, or start a run above.",
        events: [],
        phases: Object.fromEntries(PHASES.map((p) => [p, "pending"])),
      };
    }
    if (!snap.carryIn) snap.carryIn = this._readCarryIn(asOf);
    // A feed recorded by an earlier build has `plain: null` on lines this one
    // can now translate (every server note, every stdout line, narrate's own
    // check receipts). Re-translating at restore time keeps those runs
    // leader-legible without regenerating a single artifact.
    const ctx = loadContext(this.repoRoot, (snap.inputs && snap.inputs.dir) || null);
    const events = [];
    try {
      for (const line of fs.readFileSync(this._consoleFile(asOf), "utf8").split("\n")) {
        if (!line.trim()) continue;
        try {
          const evt = JSON.parse(line);
          events.push(evt.plain ? evt : { ...evt, plain: this._replain(evt, ctx) });
        } catch {
          // a torn line mid-append; the next read gets it whole
        }
      }
    } catch {
      // a snapshot without a feed still restores the phase rail
    }
    const interrupted = Boolean(snap.running);
    // A narrative still "generating" in the snapshot belongs to a server that
    // stopped before it landed — report that, never pretend it is still live.
    if (snap.narrations) {
      for (const [p, n] of Object.entries(snap.narrations)) {
        if (n && n.status === "generating") {
          snap.narrations[p] = {
            ...n,
            status: "interrupted",
            error: "the server stopped before this narrative landed",
          };
        }
      }
    }
    return {
      ...snap,
      running: false,
      awaitingCorrections: false,
      events,
      // Recomputed from the events, not read from the snapshot: the stored
      // times were stamped when the poller noticed a line, which is how a
      // 12-second step could restore as "0s".
      phaseTimes: computePhaseSpans(events, snap.phases),
      restored: true,
      interrupted,
      ...(interrupted
        ? {
            error:
              snap.error ||
              "This run was interrupted — the server stopped before it finished. Run it again.",
          }
        : {}),
    };
  }

  // A console rebuilt from the pipeline's OWN run logs, for a finished run with
  // no console snapshot — one recorded before this server persisted the feed, a
  // checkout with committed DATA/OUTPUTS/, or a lost snapshot. Those logs are what
  // the live console tails, translated by the same plain.js layer, so the
  // rebuilt feed says what the live one said (minus the server's own stdout
  // chatter, which is not part of the pipeline's record).
  _reconstruct(asOf) {
    const outDir = path.join(this.repoRoot, "DATA", "OUTPUTS", asOf);
    let finishedMs;
    try {
      finishedMs = fs.statSync(path.join(outDir, "digest.json")).mtimeMs;
    } catch {
      return null; // no finished run for this Monday to rebuild
    }

    const ctx = loadContext(this.repoRoot, null);
    const rows = [];
    const readLog = (file, label, keep) => {
      let text;
      try {
        text = fs.readFileSync(file, "utf8");
      } catch {
        return false;
      }
      let any = false;
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        let evt;
        try {
          evt = JSON.parse(line);
        } catch {
          continue;
        }
        if (keep && !keep(evt)) continue;
        rows.push({ evt, label });
        any = true;
      }
      return any;
    };

    // The per-Monday logs are rewritten by each run, so they contain exactly one
    // run and define its window. Everything else is filtered to that window:
    // the ledger log is append-only across runs and weeks, so an earlier attempt
    // at the same Monday would otherwise stretch the digest phase over hours.
    readLog(path.join(outDir, "signals_runlog.jsonl"), "runlog");
    readLog(path.join(outDir, "runlog.jsonl"), "runlog");
    const stamps = rows.map((r) => Date.parse(r.evt.ts)).filter((t) => !Number.isNaN(t));
    if (!stamps.length) return null; // nothing dated to rebuild a timeline from
    const runStart = Math.min(...stamps);
    const runEnd = Math.max(Math.max(...stamps), finishedMs);
    const within = (evt, leadMs = 0) => {
      const t = Date.parse(evt.ts);
      return !Number.isNaN(t) && t >= runStart - leadMs && t <= runEnd + MINUTE;
    };

    // The validation log and report are one shared pair rewritten by every
    // validate pass: they belong to this run only when the report names this
    // Monday AND the checks ran just before it (validate runs first).
    let validation = null;
    try {
      const report = JSON.parse(
        fs.readFileSync(
          path.join(this.repoRoot, "DATA", "OUTPUTS", "validation", "report.json"),
          "utf8"
        )
      );
      if (report.as_of === asOf) {
        const found = readLog(
          path.join(this.repoRoot, "DATA", "OUTPUTS", "validation", "runlog.jsonl"),
          "runlog",
          (e) => within(e, 30 * MINUTE)
        );
        // Counts without their checks would be a claim with no receipts.
        if (found) {
          validation = {
            checks_run: report.totals.checks_run,
            checks_flagged: report.totals.checks_flagged,
            corrections_proposed: report.totals.corrections_proposed,
            as_of: report.as_of,
          };
        }
      }
    } catch {
      // a run whose validation record moved on still rebuilds the rest
    }
    readLog(
      path.join(this.repoRoot, "DATA", "OUTPUTS", "ledger_log.jsonl"),
      "ledger_log",
      (e) => e.as_of === asOf && within(e)
    );
    // narrate's log is append-only across runs of the same Monday, so it is
    // window-filtered like the ledger log (narratives land up to ~40s after
    // digest.json, inside the window's minute of tolerance).
    readLog(path.join(outDir, "narrate_runlog.jsonl"), "runlog", (e) => within(e));

    rows.sort((a, b) => String(a.evt.ts || "").localeCompare(String(b.evt.ts || "")));
    const events = rows.map(({ evt, label }) => ({
      ts: evt.ts || null,
      phase: phaseForStep(evt.step, evt),
      source: label,
      text: describeLogEvent(evt),
      plain: translateLogEvent(evt, ctx),
      meta: enrichMeta(evt, ctx),
    }));

    // The corrections decision this run computed on, from the manifest DATA/TRANSLATION/
    // was built with (same caveat as validation: it is the latest build).
    let correctionsDecision = null;
    try {
      const man = JSON.parse(
        fs.readFileSync(path.join(this.repoRoot, "DATA", "TRANSLATION", "MANIFEST.json"), "utf8")
      );
      // The manifest keeps whole correction records; the console's decision is
      // a list of ids, as a live run reports it.
      const ids = (list) => (list || []).map((c) => (c && c.id) || c);
      if (man.as_of === asOf) {
        correctionsDecision = {
          accepted: ids(man.corrections_accepted),
          declined: ids(man.corrections_declined),
          by: null,
          auto: null,
          decidedAt: man.created_at || null,
        };
      }
    } catch {
      // no manifest: the decision stays unknown rather than invented
    }

    // digest.json is the pipeline's last write, so its presence means every
    // phase completed. Each phase's span comes from its own events.
    const phases = Object.fromEntries(PHASES.map((p) => [p, "done"]));
    // Same span rule as the live path: the pipeline's own timestamps, ended at
    // the event that completed the phase, narration excluded.
    const phaseTimes = computePhaseSpans(events, phases);

    return {
      running: false,
      started: true,
      asOf,
      inputs: null, // which files fed it is not in the pipeline's own logs
      autoAccept: null,
      awaitingCorrections: false,
      corrections: null,
      correctionsCarried: null,
      correctionsDecision,
      carryIn: this._readCarryIn(asOf),
      validation,
      startedAt: events.length ? events[0].ts : null,
      finishedAt: new Date(finishedMs).toISOString(),
      exitCode: 0,
      error: null,
      events,
      phases,
      phaseTimes,
      restored: true,
      reconstructed: true,
      interrupted: false,
    };
  }

  // asOf scopes the answer: the live run when it is that Monday's, otherwise
  // that Monday's last run restored from disk.
  status(asOf) {
    if (this.state && (!asOf || this.state.asOf === asOf)) {
      // Strip internal bookkeeping (keys starting with "_") before serving.
      const publicState = { restored: false, interrupted: false };
      for (const [k, v] of Object.entries(this.state)) {
        if (!k.startsWith("_")) publicState[k] = v;
      }
      return publicState;
    }
    if (asOf) {
      const restored = this._restore(asOf) || this._reconstruct(asOf);
      if (restored) return restored;
    }
    return {
      running: false,
      started: false,
      restored: false,
      interrupted: false,
      asOf: asOf || null,
      message: asOf
        ? `No run recorded for ${asOf} yet.`
        : "No run started this session.",
      events: [],
      phases: Object.fromEntries(PHASES.map((p) => [p, "pending"])),
    };
  }

  isRunning() {
    return Boolean(this.state && this.state.running);
  }

  // Running, but stopped on the corrections card: the run is waiting on a human
  // decision, not on compute.
  isAwaitingCorrections() {
    return Boolean(this.state && this.state.running && this.state.awaitingCorrections);
  }

  _refreshPlainCtx() {
    const inputsDir = this.state && this.state.inputs && this.state.inputs.dir;
    this._plainCtx = loadContext(this.repoRoot, inputsDir || null);
  }

  // extra: {plain, meta} — plain is the leader-legible sentence (or null to
  // fall back to text), meta the parsed run-log event for structured UI.
  // No-op after a reset cleared the state: a stopped child's last output has
  // no run to land in.
  _event(phase, source, text, extra = {}) {
    if (!this.state) return;
    const event = {
      ts: new Date().toISOString(),
      phase: phase || null,
      source,
      text,
      plain: extra.plain || this._defaultPlain(source, text),
      meta: extra.meta || null,
    };
    this.state.events.push(event);
    this._persistEvent(event);
    this._touchPhaseSpan(event);
    if (phase && this.state.phases[phase] === "pending") {
      this.state.phases[phase] = "active";
      this._persistState();
      this._emitRunState();
    }
    // Pushed to any watching client the moment it happens, so the phase boxes
    // fill line by line instead of a poll at a time. The poll still returns
    // the whole feed and remains the reconciliation path.
    this._emitLive({ type: "console_event", event });
  }

  // The server's own notes, the pipeline's stdout and the artifact lines are
  // English already, but they carry command lines and absolute filesystem
  // paths a leader must never see, so they go through the same translation
  // layer as the run-log events. stderr stays untranslated on purpose (it is
  // engineer content, shown as engineer lines), and run-log events arrive with
  // their translation already attached.
  _defaultPlain(source, text) {
    return this._defaultPlainWith(source, text, this._plainCtx);
  }

  // Widen the phase's span with this event's pipeline timestamp. The end is
  // tracked privately and only published by _markDone, so a running phase
  // keeps counting live and a finished one is never stretched by a later line.
  _touchPhaseSpan(event) {
    const phase = event.phase;
    if (!phase || !this.state.phaseTimes || isNarrationEvent(event)) return;
    const t = pipelineTime(event);
    if (t == null) return;
    const iso = new Date(t).toISOString();
    const times = this.state.phaseTimes[phase] || { startedAt: null, endedAt: null };
    if (!times.startedAt || t < Date.parse(times.startedAt)) times.startedAt = iso;
    this.state.phaseTimes[phase] = times;
    if (!this.state._phaseLatest) this.state._phaseLatest = {};
    const last = this.state._phaseLatest[phase];
    if (!last || t > Date.parse(last)) this.state._phaseLatest[phase] = iso;
  }

  // The rail's state, pushed the moment it changes so a watching client does
  // not wait for the next poll. Same fields the poll serves.
  _emitRunState() {
    if (!this.state) return;
    this._emitLive({
      type: "run_state",
      phases: { ...this.state.phases },
      phaseTimes: JSON.parse(JSON.stringify(this.state.phaseTimes || {})),
      awaitingCorrections: Boolean(this.state.awaitingCorrections),
      running: Boolean(this.state.running),
      finishedAt: this.state.finishedAt || null,
      exitCode: this.state.exitCode === undefined ? null : this.state.exitCode,
      error: this.state.error || null,
      carryIn: this.state.carryIn || null,
    });
  }

  _markDone(phase) {
    if (!this.state) return;
    // Everything earlier in the rail is necessarily finished too.
    for (const p of PHASES.slice(0, PHASES.indexOf(phase) + 1)) {
      if (this.state.phases[p] !== "done") {
        this.state.phases[p] = "done";
        const t = this.state.phaseTimes[p] || { startedAt: null, endedAt: null };
        // The phase ends at the last thing the PIPELINE did in it, not at the
        // moment this poller noticed — see the phase-span notes above.
        t.endedAt = (this.state._phaseLatest || {})[p] || new Date().toISOString();
        if (!t.startedAt) t.startedAt = t.endedAt;
        this.state.phaseTimes[p] = t;
        // A phase that just completed narrates itself: the real pipeline
        // module (pipeline.narrate) is spawned async — the run never waits.
        this._narratePhase(p);
      }
    }
    this._persistState();
    this._emitRunState();
  }

  // -------------------------------------------------------------------------
  // Run narration — the thread's real conversational voice.
  //
  // ONE `python -m pipeline.narrate --follow` process walks the whole run
  // (async, detached — the pipeline is never serialized behind it), waiting
  // for each phase's artifact in order and reasoning over one accumulating
  // run context. Its verified note for a phase arrives as a `phase_final`
  // event and is appended to the thread as that phase's AI entry. The
  // deterministic one-liner the run posted at phase completion is only a
  // PLACEHOLDER (payload.placeholder: true): once the narrative lands, the
  // thread read layer marks the placeholder superseded, exactly like a
  // decision_request resolving — the JSONL itself is never rewritten. If
  // narration fails, a system note says so and the placeholder stands: the
  // same honest-fallback pattern as ask-engine. The narrative entry carries
  // mode/decided_by/fallbacks so the UI can label the tier truthfully.
  // -------------------------------------------------------------------------

  // A deterministic phase summary posted to the thread as the immediate
  // placeholder the phase's narrative will replace.
  _threadPlaceholder(phase, text) {
    // role "system": this line was composed by code from the run log. Only
    // text a model actually wrote is posted as "ai" — the panel labels the
    // two differently, so nobody is credited for a sentence they never wrote.
    return this._thread("system", "narration", { text, phase, placeholder: true });
  }

  // A short line that stays between steps: the gate after this phase passed.
  // Not a placeholder — the agent narrative does not replace it.
  _threadGate(phase) {
    if (!this.state) return;
    if (!this.state.gatesPosted) this.state.gatesPosted = [];
    if (this.state.gatesPosted.includes(phase)) return;
    const text = this._gatePassedText(phase);
    if (!text) return;
    this.state.gatesPosted.push(phase);
    this._thread("system", "note", { text, phase, kind: "gate" });
  }

  _gatePassedText(phase) {
    if (phase === "validation") {
      const proposed = (this.state.corrections || []).length;
      const decision = this.state.correctionsDecision;
      const carried = this.state.correctionsCarried || [];
      if (proposed === 0 && carried.length) {
        return (
          `Gate passed — ${carried.length} prior data-quality ` +
          `decision${carried.length === 1 ? "" : "s"} carried forward, ` +
          "no new decision required."
        );
      }
      if (proposed === 0) {
        return "Gate passed — no corrections to decide. Next step can run.";
      }
      const accepted = (decision && decision.accepted) ? decision.accepted.length : 0;
      const declined = (decision && decision.declined) ? decision.declined.length : 0;
      return (
        `Gate passed — ${accepted} correction${accepted === 1 ? "" : "s"} accepted, ` +
        `${declined} declined. Next step can run.`
      );
    }
    if (phase === "signals") {
      try {
        const doc = JSON.parse(
          fs.readFileSync(
            path.join(this.repoRoot, "DATA", "OUTPUTS", this.state.asOf, "signals.json"),
            "utf8"
          )
        );
        const ranked = (doc.signals || []).length;
        const suppressed = (doc.suppressed || []).length;
        return `Gate passed — suppression applied. ${ranked} ranked, ${suppressed} held back.`;
      } catch {
        return "Gate passed — suppression applied. Next step can run.";
      }
    }
    if (phase === "verdicts") {
      const HARNESS = new Set([
        "facts_summary_number_check",
        "number_check",
        "reasoning_check",
      ]);
      let total = 0;
      let passed = 0;
      let retries = 0;
      for (const event of this.state.events || []) {
        const meta = event.meta;
        if (!meta || meta.step !== "verdicts") continue;
        if (meta.event === "retry") retries += 1;
        if (HARNESS.has(meta.event)) {
          total += 1;
          if (meta.result === "pass") passed += 1;
        }
      }
      if (total) {
        return (
          `Gate passed — harness ${passed}/${total} checks passed` +
          (retries ? `, ${retries} ${retries === 1 ? "retry" : "retries"}` : "") +
          "."
        );
      }
      return "Gate passed — verdicts checked. Next step can run.";
    }
    return null;
  }

  // A phase finished computing. There is ONE narrator per run — a single agent
  // walking the run in order over one accumulating context — so this marks the
  // phase as in flight and makes sure that narrator is running. The narrator
  // itself waits for each phase's artifact and decides when to speak; nothing
  // here spawns a process per phase.
  _narratePhase(phase) {
    if (!this.state || !this.threadStore) return;
    if (!this.state._narrated) this.state._narrated = new Set();
    if (this.state._narrated.has(phase)) return;
    this.state._narrated.add(phase);
    if (!this.state.narrations) this.state.narrations = {};

    this.state.narrations[phase] = {
      status: "generating",
      startedAt: new Date().toISOString(),
    };
    this._persistState();
    this._startNarrator();
  }

  // One JSONL line from pipeline.narrate --stream. Narratives are the run
  // talking about itself, so they stream like an answer does: the text appears
  // as the model writes it, and it stays PROVISIONAL until the phase's checks
  // pass. Nothing here writes to the thread — _narrateReady still does that,
  // once, with the checked text.
  _onNarrateLine(token, line, spawnedMs) {
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      return; // not an event; the settle path still sees the raw stdout
    }
    if (!event || typeof event.type !== "string") return;
    // A late line from a superseded run must not touch the live one.
    if (!this.state || this.state._startedMs !== token) return;
    // One stream carries the whole run, so every event names the phase it
    // belongs to. The final document event is the only one that does not.
    const phase = event.phase;
    if (!phase && event.type !== "final") return;
    const live = (this.state.narrations && this.state.narrations[phase]) || null;

    switch (event.type) {
      case "phase_started":
        // The narrator reached this phase: its artifact is on disk and the
        // agent is about to reason over it with the run so far in hand.
        if (live) live.streamState = "working";
        this._emitLive({ type: "narration_phase_started", phase });
        break;
      case "phase_final": {
        // This phase's note passed its checks. It lands now rather than at
        // process exit — the narrator is still walking the rest of the run.
        const narrative = event.narrative;
        if (!narrative || typeof narrative.text !== "string") {
          this._narrateFailed(
            phase,
            PHASE_TITLES[phase],
            "the run narrator produced no readable narrative for this phase"
          );
          break;
        }
        this._narrateReady(
          phase,
          PHASE_TITLES[phase],
          narrative,
          (Date.now() - spawnedMs) / 1000
        );
        break;
      }
      case "timeout":
        this._narrateFailed(
          phase,
          PHASE_TITLES[phase],
          event.message || "this phase produced no artifact in time"
        );
        break;
      case "generating":
        if (live) live.streamState = "generating";
        this._emitLive({ type: "narration_generating", phase, attempt: event.attempt });
        break;
      case "delta":
        if (live) {
          live.streamState = "generating";
          live.text = (live.text || "") + (event.text || "");
        }
        this._emitLive({ type: "narration_delta", phase, text: event.text || "" });
        break;
      case "verifying":
        if (live) live.streamState = "verifying";
        this._emitLive({ type: "narration_verifying", phase });
        break;
      case "redo":
        // The checks rejected that draft: it was never a narrative, so it is
        // dropped rather than patched.
        if (live) {
          live.text = "";
          live.streamState = "working";
          live.redos = (live.redos || 0) + 1;
        }
        this._emitLive({ type: "narration_redo", phase, kind: event.kind });
        break;
      case "tier_fallback":
        if (live) {
          live.text = "";
          live.streamState = "working";
        }
        this._emitLive({
          type: "narration_tier_fallback", phase, from: event.from, to: event.to,
        });
        break;
      default:
        break;
    }
  }

  // Live progress for watching clients (the SSE endpoint). Pure observation:
  // nothing in the run waits on a subscriber, and a broken one cannot break a
  // narration.
  subscribe(asOf, fn) {
    if (!this._listeners) this._listeners = new Set();
    const entry = { asOf, fn };
    this._listeners.add(entry);
    return () => this._listeners.delete(entry);
  }

  _emitLive(event) {
    if (!this._listeners || this._listeners.size === 0) return;
    const asOf = this.state && this.state.asOf;
    const payload = { asOf, ...event };
    for (const l of this._listeners) {
      if (l.asOf && asOf && l.asOf !== asOf) continue;
      try {
        l.fn(payload);
      } catch {
        // a dead client must never break a run
      }
    }
  }

  // ONE narrator per run. `pipeline.narrate --follow` waits for each phase's
  // artifact in pipeline order, appends the tool result to the run context,
  // reasons over everything accumulated so far, and appends that reasoning
  // before moving on. It is started once — at the first phase that completes —
  // and lives until the run is narrated, which is why it can sit through the
  // human corrections gate without any special handling here.
  _startNarrator() {
    if (!this.state || this.state._narrator) return;
    const token = this.state._startedMs; // ties the async result to THIS run
    const asOf = this.state.asOf;
    const env =
      (this.state && this.state._env) || { ...process.env, PYTHONUNBUFFERED: "1" };
    const spawnedMs = Date.now();

    this._event(
      "validation",
      "server",
      `Narrating the run: python -m pipeline.narrate --as-of ${asOf} --follow --stream ` +
        "(one agent, one accumulating context, phases in order)."
    );

    // detached: like pipeline steps, narrate shells out to the claude CLI, so
    // a session reset must be able to stop the whole process group.
    const child = spawn(
      this.pythonBin,
      [
        "-u", "-m", "pipeline.narrate",
        "--as-of", asOf,
        "--follow",
        "--follow-timeout", String(Math.round(NARRATE_FOLLOW_TIMEOUT_MS / 1000)),
        "--stream",
      ],
      { cwd: this.repoRoot, env, stdio: ["ignore", "pipe", "pipe"], detached: true }
    );
    this.state._narrator = child;
    this._narrateChildren.add(child);

    let stderr = "";
    let lineBuf = "";
    child.stdout.on("data", (c) => {
      lineBuf += c.toString("utf8");
      let nl;
      while ((nl = lineBuf.indexOf("\n")) >= 0) {
        const line = lineBuf.slice(0, nl).trim();
        lineBuf = lineBuf.slice(nl + 1);
        // The phase comes off the event now, not off the spawn: one stream
        // carries the whole run, in the order it happened.
        if (line) this._onNarrateLine(token, line, spawnedMs);
      }
    });
    child.stderr.on("data", (c) => (stderr += c.toString("utf8")));

    let timedOut = false;
    const killTimer = setTimeout(() => {
      timedOut = true;
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        try {
          child.kill("SIGTERM");
        } catch {
          // already gone
        }
      }
    }, NARRATE_FOLLOW_TIMEOUT_MS + 30_000);

    let settled = false;
    const settle = (fn) => {
      if (settled) return;
      settled = true;
      clearTimeout(killTimer);
      this._narrateChildren.delete(child);
      if (this.state) this.state._narrator = null;
      // The result belongs to one run: after a reset (state null) or a re-run
      // (new token + fresh thread), a late narrative must not land anywhere.
      if (!this.state || this.state._startedMs !== token) return;
      fn();
    };

    child.on("error", (err) =>
      settle(() => this._narratorStopped(`pipeline.narrate failed to start (${err.message})`))
    );

    child.on("close", (code) =>
      settle(() => {
        if (timedOut) {
          this._narratorStopped(
            `no narrative within ${Math.round(NARRATE_FOLLOW_TIMEOUT_MS / 1000)}s — the attempt was stopped`
          );
          return;
        }
        if (code !== 0) {
          const tail =
            stderr.trim().split("\n").slice(-2).join(" ").slice(0, 300) || "no error output";
          this._narratorStopped(`pipeline.narrate exited ${code} (${tail})`);
          return;
        }
        // A clean exit with a phase still in flight means the follower gave up
        // waiting for that artifact. Say so per phase rather than silently
        // leaving a bubble spinning.
        this._narratorStopped(null);
      })
    );
  }

  // The narrator is gone. Every phase still marked generating gets an honest
  // note; a clean exit with nothing outstanding says nothing at all.
  _narratorStopped(reason) {
    if (!this.state || !this.state.narrations) return;
    for (const [phase, entry] of Object.entries(this.state.narrations)) {
      if (!entry || entry.status !== "generating") continue;
      this._narrateFailed(
        phase,
        PHASE_TITLES[phase],
        reason || "the run narrator exited before this phase was narrated"
      );
    }
  }


  // The narrative landed: append it to the thread verbatim (with its tier
  // metadata, so the panel labels honestly who wrote it) and record it in the
  // run state. The thread read layer supersedes the phase's placeholders.
  _narrateReady(phase, title, result, latencySeconds) {
    // Narration can outlive the pipeline (whose _finish stopped the poll
    // timer): sweep the tails so this narration's own check events — written
    // to narrate_runlog.jsonl before the process printed its JSON — reach the
    // console like every other harness receipt.
    this._pollTails();
    const verified =
      (result.checks && result.checks.numbers_verified) || 0;
    const fallbacks = result.fallbacks || [];
    this._thread("ai", "narration", {
      kind: "narrative",
      phase,
      title: result.title || title,
      text: result.text,
      // mode = the tier this run was configured for; decided_by = what
      // actually wrote it. They differ only when a recorded fallback fired.
      mode: result.mode,
      decided_by: result.decided_by,
      numbers_verified: verified,
      entities_verified: (result.checks && result.checks.entities_verified) || 0,
      // The receipt map, verbatim from pipeline.narrate: every number, date
      // and center name in the bubble with the file and field it came from,
      // plus which checks passed and which artifacts were read. The panel
      // renders these; it never derives them.
      receipts: result.receipts || [],
      checks: result.checks || null,
      artifacts_read: result.artifacts_read || [],
      attempts: result.attempts,
      fallbacks,
      elapsed_seconds: result.elapsed_seconds,
    });
    this.state.narrations[phase] = {
      status: "done",
      mode: result.mode,
      decided_by: result.decided_by,
      numbers_verified: verified,
      attempts: result.attempts,
      fallbacks: fallbacks.length,
      elapsed_seconds: result.elapsed_seconds,
      latency_seconds: Math.round(latencySeconds * 100) / 100,
    };
    this._persistState();
    this._event(
      phase,
      "server",
      `Narrative for ${title} ready — written by ${result.decided_by}, ` +
        `${verified} numbers verified, attempts ${result.attempts}, ` +
        `${result.elapsed_seconds}s` +
        (fallbacks.length ? `, ${fallbacks.length} tier fallback(s)` : "") +
        "."
    );
    // The draft is finished with: the checked narrative is in the thread, and
    // that is what the panel must show from here.
    this._threadGate(phase);
    this._emitLive({ type: "narration_final", phase });
  }

  // Narration failed: the deterministic placeholder stands, and a system note
  // says so — the ask-engine disclosure pattern. Nothing is invented.
  _narrateFailed(phase, title, reason) {
    this._pollTails(); // any checks the failed attempt still logged
    this.state.narrations[phase] = { status: "failed", error: reason };
    this._persistState();
    this._event(phase, "server", `Narration for ${title} failed: ${reason}.`);
    this._thread("system", "note", {
      text:
        `The ${title} narrative could not be generated (${reason}) — ` +
        "the deterministic summary above stands. Nothing was invented.",
    });
    // Whatever draft text was on screen is dropped: it never passed a check,
    // so it never becomes the phase's narrative.
    this._threadGate(phase);
    this._emitLive({ type: "narration_failed", phase, reason });
  }

  // Phases whose narrative is being generated right now (the panel's
  // lightweight "writing…" state). Scoped to a Monday like status().
  narrating(asOf) {
    if (!this.state || (asOf && this.state.asOf !== asOf)) return [];
    return Object.entries(this.state.narrations || {})
      .filter(([, n]) => n && n.status === "generating")
      .map(([phase, n]) => ({
        phase,
        title: PHASE_TITLES[phase],
        started_at: n.startedAt || null,
        // The draft so far, so a reload (or a second browser) picks up a
        // narrative mid-sentence instead of watching a spinner. Provisional
        // by construction: it is not in the thread and carries no verified
        // count until the phase's checks pass.
        text: n.text || "",
        stream_state: n.streamState || "working",
        redos: n.redos || 0,
        provisional: true,
      }));
  }

  // Tail a JSONL file from its size at run start; emit each appended line.
  // dedupe: skip lines already emitted this run (ignoring the timestamp) —
  // used for the validation runlog, which the accept step rewrites from
  // scratch after re-running the same deterministic checks. The apply.*
  // decision lines are new and always shown.
  _addTail(filePath, label, opts = {}) {
    const tail = {
      filePath,
      label,
      offset: fs.existsSync(filePath) ? fs.statSync(filePath).size : 0,
      headSig: null, // first bytes of the file at the current offset baseline
      dedupe: Boolean(opts.dedupe),
      seen: new Set(),
    };
    tail.headSig = this._readHead(filePath);
    this._tails.push(tail);
  }

  // First bytes of a file — a cheap identity for "was this file rewritten?".
  // The pipeline reopens its run logs fresh ("w" mode) each run; the first
  // line's timestamp changes, so a rewrite always changes this signature.
  _readHead(filePath, len = 128) {
    try {
      const fd = fs.openSync(filePath, "r");
      const buf = Buffer.alloc(len);
      const n = fs.readSync(fd, buf, 0, len, 0);
      fs.closeSync(fd);
      return buf.toString("utf8", 0, n);
    } catch {
      return null;
    }
  }

  _pollTails() {
    for (const tail of this._tails) {
      let size;
      try {
        size = fs.statSync(tail.filePath).size;
      } catch {
        continue; // file not created yet
      }
      // Detect a truncate-and-rewrite even when the rewritten file is not
      // smaller than the old offset (a fast step can rewrite the whole log
      // between two polls): a shrink OR a changed head means "new file" —
      // restart the tail from the top.
      const head = this._readHead(tail.filePath);
      const rewritten =
        tail.offset > 0 &&
        head !== null &&
        tail.headSig !== null &&
        head !== tail.headSig &&
        // tolerate a shorter earlier capture of the same content
        !head.startsWith(tail.headSig) &&
        !tail.headSig.startsWith(head);
      if (size < tail.offset || rewritten) tail.offset = 0;
      if (tail.offset === 0) tail.headSig = head;
      if (size <= tail.offset) continue;
      const fd = fs.openSync(tail.filePath, "r");
      const buf = Buffer.alloc(size - tail.offset);
      fs.readSync(fd, buf, 0, buf.length, tail.offset);
      fs.closeSync(fd);
      tail.offset = size;
      for (const line of buf.toString("utf8").split("\n")) {
        if (!line.trim()) continue;
        let evt;
        try {
          evt = JSON.parse(line);
        } catch {
          continue; // partial line; picked up complete on the next poll
        }
        if (tail.dedupe) {
          const { ts, ...rest } = evt;
          const key = JSON.stringify(rest);
          if (tail.seen.has(key)) continue;
          tail.seen.add(key);
        }
        const phase = phaseForStep(evt.step, evt);
        const plain = translateLogEvent(evt, this._plainCtx);
        this._event(phase, tail.label, describeLogEvent(evt), {
          plain,
          meta: enrichMeta(evt, this._plainCtx),
        });
        // Phase summaries also land in the conversation thread — as the
        // immediate placeholder the phase's generated narrative replaces.
        if (plain && THREAD_SUMMARY_EVENTS.has(`${evt.step}:${evt.event}`)) {
          this._threadPlaceholder(phase, plain);
        }
        // "summary" is each step's own final run-log event.
        if (evt.event === "summary" && evt.step === "signals") this._markDone("signals");
        if (evt.event === "summary" && evt.step === "verdicts") this._markDone("verdicts");
        if (evt.event === "digest_assembled") this._markDone("digest");
      }
    }
  }

  // Watch for the pipeline's own artifacts being (re)written during this run.
  _pollArtifacts() {
    if (!this.state) return;
    this._pollCarryIn();
    const outDir = path.join(this.repoRoot, "DATA", "OUTPUTS", this.state.asOf);
    const artifacts = [
      ["signals.json", "signals", "Signal engine wrote signals.json"],
      ["facts.json", "verdicts", "Fact table written (facts.json)"],
      ["verdicts.json", "verdicts", "Verdicts written (verdicts.json)"],
      ["digest.json", "digest", "digest.json assembled"],
    ];
    for (const [file, phase, msg] of artifacts) {
      const p = path.join(outDir, file);
      if (this.state._seenArtifacts.has(file)) continue;
      try {
        const mtime = fs.statSync(p).mtimeMs;
        if (mtime >= this.state._startedMs) {
          this.state._seenArtifacts.add(file);
          this._event(phase, "artifact", `${msg} (DATA/OUTPUTS/${this.state.asOf}/${file})`);
          if (file === "signals.json") this._markDone("signals");
          if (file === "verdicts.json") this._markDone("verdicts");
          if (file === "digest.json") this._markDone("digest");
        }
      } catch {
        // not written yet
      }
    }
  }

  // End the run session: stop polling, do a final artifact/tail sweep, and
  // record the outcome. errMsg === null means success.
  _finish(code, errMsg) {
    clearInterval(this._timer);
    if (!this.state) return; // the session was reset out from under this run
    this._pollArtifacts();
    this._pollTails();
    this.state.running = false;
    this.state.awaitingCorrections = false;
    this.state.finishedAt = new Date().toISOString();
    this.state.exitCode = code;
    if (errMsg) {
      this.state.error = errMsg;
      // The phase that was underway is the one that failed.
      for (const p of PHASES) {
        if (this.state.phases[p] === "active") this.state.phases[p] = "failed";
      }
      this._event(null, "server", errMsg);
      this._thread("system", "note", { text: errMsg });
    }
    this._persistState();
    this._emitRunState();
  }

  // Spawn one python step, stream its stdout/stderr into the feed (tagged
  // with defaultPhase when given, else by line content), and call onClose
  // with the exit code. Every line shown is the process's real output.
  _spawnStep(args, env, defaultPhase, onClose) {
    // stdin is closed, not piped: the pipeline shells out to the claude CLI,
    // which would otherwise inherit an open-but-empty pipe and stall waiting
    // for input that never arrives.
    // detached: the step runs as its own process group, so a session reset can
    // stop the step AND its children (the pipeline shells out to the claude
    // CLI) with one group signal — no orphaned model calls left running.
    const child = spawn(this.pythonBin, args, {
      cwd: this.repoRoot,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    });
    this._child = child;
    this._event(
      defaultPhase,
      "server",
      `Started: ${path.basename(this.pythonBin)} ${args.join(" ")} (cwd ${this.repoRoot})`
    );

    const lineBuffer = { out: "", err: "" };
    const emitLine = (line, streamName) => {
      this._event(defaultPhase || phaseForLine(line), streamName, line, {
        plain: translateText(line, this._plainCtx),
      });
    };
    const onChunk = (which, streamName) => (chunk) => {
      lineBuffer[which] += chunk.toString("utf8");
      let idx;
      while ((idx = lineBuffer[which].indexOf("\n")) >= 0) {
        const line = lineBuffer[which].slice(0, idx).replace(/\r$/, "");
        lineBuffer[which] = lineBuffer[which].slice(idx + 1);
        if (line.trim() === "") continue;
        emitLine(line, streamName);
      }
    };
    child.stdout.on("data", onChunk("out", "stdout"));
    child.stderr.on("data", onChunk("err", "stderr"));

    child.on("close", (code) => {
      if (this._child === child) this._child = null;
      // Flush any trailing partial stdout/stderr line.
      for (const which of ["out", "err"]) {
        const rest = lineBuffer[which].trim();
        if (rest) emitLine(rest, which === "out" ? "stdout" : "stderr");
      }
      onClose(code);
    });

    child.on("error", (err) => {
      this._finish(null, `Failed to start ${args.join(" ")}: ${err.message}`);
    });
  }

  // Read the validation report + proposals the validate step just wrote.
  _readValidationOutputs() {
    const vdir = path.join(this.repoRoot, "DATA", "OUTPUTS", "validation");
    try {
      const report = JSON.parse(fs.readFileSync(path.join(vdir, "report.json"), "utf8"));
      this.state.validation = {
        checks_run: report.totals.checks_run,
        checks_flagged: report.totals.checks_flagged,
        corrections_proposed: report.totals.corrections_proposed,
        as_of: report.as_of,
      };
    } catch {
      this.state.validation = null;
    }
    try {
      const doc = JSON.parse(
        fs.readFileSync(path.join(vdir, "corrections_proposed.json"), "utf8")
      );
      const all = (doc.corrections || []).map((c) => ({
        id: c.id,
        table: c.table,
        kind: c.kind,
        description: c.description,
        affected_rows: c.affected_rows,
        details: c.details || [],
        status: c.status || "new",
        scope: c.scope || "standing",
        prior_decision: c.prior_decision || null,
        decided_as_of: c.decided_as_of || null,
        new_rows: c.new_rows || 0,
      }));
      // Only corrections this Monday has never ruled on are a question. The
      // rest were decided on an earlier Monday and apply on their own — they
      // are reported, never re-asked.
      this.state.corrections = all.filter((c) => c.status === "new");
      this.state.correctionsCarried = all.filter((c) => c.status === "carried");
    } catch {
      this.state.corrections = null;
      this.state.correctionsCarried = null;
    }
  }

  // The digest run proper (python -m pipeline.run). Ends the session.
  _startDigestRun(asOf, extraArgs, env) {
    this._spawnStep(
      ["-u", "-m", "pipeline.run", "--as-of", asOf, ...extraArgs],
      env,
      null,
      (code) => {
        if (code === 0) {
          this._markDone("digest");
          this._finish(0, null);
          this._event(
            "digest",
            "server",
            `Pipeline finished (exit 0). digest.json for ${asOf} is ready.`
          );
          // Placeholder only until the digest wrap-up narrative lands; skip it
          // entirely if that narrative somehow already arrived.
          const digestNarration =
            this.state && this.state.narrations && this.state.narrations.digest;
          if (!digestNarration || digestNarration.status !== "done") {
            this._threadPlaceholder(
              "digest",
              `Run finished. The Monday digest for ${asOf} is ready.`
            );
          }
        } else {
          this._finish(code, `Pipeline exited with code ${code}. See stderr lines above.`);
        }
      }
    );
  }

  // Run `pipeline.validate --accept <ids|all>`: applies the decision, rebuilds
  // DATA/TRANSLATION/, then hands off to the digest run.
  _applyCorrections(acceptArg, onApplied) {
    const { asOf } = this.state;
    this._spawnStep(
      ["-u", "-m", "pipeline.validate", "--as-of", asOf, "--accept", acceptArg, "--actor", "Lucas"],
      this.state._env,
      "validation",
      (code) => {
        if (code !== 0) {
          this._finish(
            code,
            `Data Validation & Check exited with code ${code} while applying corrections. See stderr lines above.`
          );
          return;
        }
        this._refreshPlainCtx(); // DATA/TRANSLATION/ was just rebuilt
        this._markDone("validation");
        const rebuiltMsg = `Data Validation & Check finished (exit 0) — DATA/TRANSLATION/ rebuilt from ${
          this.state.inputs.mode === "uploaded" ? "the uploaded files" : "DATA/INPUTS/"
        } with the accepted corrections.`;
        this._event("validation", "server", rebuiltMsg);
        this._threadPlaceholder(
          "validation",
          "Data checks finished — the accepted corrections are applied to a " +
            "working copy. The source data was not changed."
        );
        onApplied();
      }
    );
  }

  // The operator's accept/decline decision (POST /api/run/corrections).
  // acceptIds: correction ids to apply; everything else proposed is declined.
  resolveCorrections(acceptIds, by) {
    if (!this.state || !this.state.running || !this.state.awaitingCorrections) {
      return {
        error: "No run is waiting for a corrections decision.",
        status: 409,
      };
    }
    const proposed = (this.state.corrections || []).map((c) => c.id);
    if (!Array.isArray(acceptIds) || acceptIds.some((id) => typeof id !== "string")) {
      return { error: 'Body must be JSON like {"accept": ["C1", "C2"]}.', status: 400 };
    }
    const unknown = acceptIds.filter((id) => !proposed.includes(id));
    if (unknown.length > 0) {
      return {
        error: `Unknown correction id(s): ${unknown.join(", ")}. Proposed: ${proposed.join(", ")}.`,
        status: 400,
      };
    }
    const accepted = proposed.filter((id) => acceptIds.includes(id));
    const declined = proposed.filter((id) => !acceptIds.includes(id));

    this.state.awaitingCorrections = false;
    this.state.correctionsDecision = {
      accepted,
      declined,
      by: by || null,
      auto: false,
      decidedAt: new Date().toISOString(),
    };

    const who = by ? ` by ${by}` : "";
    this._event(
      "validation",
      "server",
      `Corrections decision received${who}: accepted ${accepted.join(", ") || "none"}` +
        (declined.length ? ` · declined ${declined.join(", ")}` : " · declined none") +
        ". Applying to DATA/TRANSLATION/ now (DATA/INPUTS/ stays untouched)."
    );

    // The human decision is a first-class thread entry, followed by one
    // visible line per declined correction.
    const decisionText =
      `${by || "You"} accepted ${accepted.join(", ") || "none"}` +
      (declined.length ? `; declined ${declined.join(", ")}.` : ` — all ${accepted.length} corrections.`);
    this._thread("user", "decision", { accepted, declined, by: by || null, text: decisionText });
    for (const id of declined) {
      const c = (this.state.corrections || []).find((x) => x.id === id);
      const line = declinedCorrectionLine(c || { id });
      this._event("validation", "server", `${line} DATA/TRANSLATION/ is built without it.`);
      this._thread("system", "narration", { text: line, phase: "validation" });
    }

    this._persistState(); // the decision is part of the run's record
    this._emitRunState();
    const acceptArg = declined.length === 0 ? "all" : accepted.join(",");
    this._applyCorrections(acceptArg, () =>
      this._startDigestRun(this.state.asOf, this.state._extraArgs, this.state._env)
    );
    return { ok: true, accepted, declined };
  }

  // Stop a child process for real: SIGTERM, and SIGKILL if it ignores that.
  // Steps spawned by _spawnStep are their own process group (detached), so the
  // signal goes to the whole group — the python step and anything it started
  // (the claude CLI) — with a plain child.kill fallback for anything else.
  // Resolves once the process has actually exited.
  _stopChild(child) {
    return new Promise((resolve) => {
      if (!child || child.exitCode !== null || child.signalCode) return resolve();
      const signalIt = (sig) => {
        try {
          process.kill(-child.pid, sig); // the group, when child leads one
        } catch {
          try {
            child.kill(sig);
          } catch {
            // already gone
          }
        }
      };
      const killTimer = setTimeout(() => signalIt("SIGKILL"), 3000);
      child.once("close", () => {
        clearTimeout(killTimer);
        // The step is gone; sweep its group for survivors (a claude CLI call
        // can outlive a SIGTERM'd parent). ESRCH — nothing left — is the
        // normal case and is swallowed by the fallback's own catch.
        try {
          process.kill(-child.pid, "SIGKILL");
        } catch {
          // no survivors
        }
        resolve();
      });
      signalIt("SIGTERM");
    });
  }

  // Session reset (POST /api/reset) — the demo's "run it again from scratch".
  // Everything is archived, nothing deleted:
  //   1. any in-flight pipeline step is stopped (recorded in its console and
  //      thread first, so the archive says why the run ended),
  //   2. the Monday's thread + console + state snapshot rotate into
  //      DATA/OUTPUTS/<asOf>/thread_archive/,
  //   3. a reset marker replaces the state snapshot so the console reports a
  //      clean start instead of rebuilding the archived run from pipeline logs,
  //   4. DATA/TRANSLATION/ is rebuilt canonically from DATA/INPUTS/ (pipeline.validate
  //      --accept all — the same deterministic step a run uses),
  //   5. the fresh thread opens with a system entry saying what happened.
  async reset(asOf, by) {
    if (this._resetting) {
      return { error: "A session reset is already in progress.", status: 409 };
    }
    this._resetting = true;
    try {
      const child = this._child;
      const hadLiveRun = Boolean(this.state && this.state.running);
      if (hadLiveRun) {
        this._finish(
          null,
          `Run stopped — session reset${by ? ` by ${by}` : ""} before it finished.`
        );
      }
      // Clear in-memory run state BEFORE the kill lands: the child's close
      // handler then has no state to write into (all mutators no-op on null).
      clearInterval(this._timer);
      this.state = null;
      this._tails = [];
      if (child) await this._stopChild(child);
      // Any in-flight narration dies with the session too — python and the
      // claude CLI calls it shelled out to. Their close handlers see a null
      // state and write nothing into the fresh thread.
      for (const c of [...this._narrateChildren]) await this._stopChild(c);

      // Archive the thread and the console/state pair together — the two
      // surfaces retire as one story, exactly as they were told.
      let threadArchived = null;
      if (this.threadStore) {
        try {
          threadArchived = this.threadStore.rotate(asOf);
        } catch {
          threadArchived = null;
        }
      }
      this._rotateConsole(asOf);

      // Archive the run artifacts too — the digest page then shows its
      // "no digest yet" state and the next run creates the Monday digest
      // from scratch on screen (the demo moment). Archived, never deleted.
      try {
        const runDir = path.join(this.repoRoot, "DATA", "OUTPUTS", asOf);
        const archDir = path.join(runDir, "thread_archive");
        fs.mkdirSync(archDir, { recursive: true });
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        for (const f of [
          "digest.json", "verdicts.json", "facts.json", "signals.json",
          "narratives.json", "runlog.jsonl", "signals_runlog.jsonl",
          "narrate_runlog.jsonl", "run_context.jsonl",
        ]) {
          const src = path.join(runDir, f);
          if (fs.existsSync(src)) {
            fs.renameSync(src, path.join(archDir, `run-${stamp}-${f}`));
          }
        }
      } catch {
        // archiving artifacts is best-effort; a failed move never blocks reset
      }

      // The reset marker: status() serves a clean start from this, and start()
      // rotates it into the archive like any other retired snapshot.
      try {
        const file = this._stateFile(asOf);
        fs.mkdirSync(path.dirname(file), { recursive: true });
        fs.writeFileSync(
          file,
          JSON.stringify(
            { reset: true, resetAt: new Date().toISOString(), by: by || null },
            null,
            2
          ) + "\n"
        );
      } catch {
        // status() then simply reports "no run recorded" — still a clean start
      }

      // Rebuild canonical DATA/TRANSLATION/ from the repo's DATA/INPUTS/ — the real validate
      // step, applying every proposed correction, no upload dir in the env.
      const env = { ...process.env, PYTHONUNBUFFERED: "1" };
      delete env.PETFOLK_INPUTS_DIR;
      const rebuild = await new Promise((resolve) => {
        const args = ["-u", "-m", "pipeline.validate", "--as-of", asOf, "--accept", "all"];
        const proc = spawn(this.pythonBin, args, {
          cwd: this.repoRoot,
          env,
          stdio: ["ignore", "pipe", "pipe"],
        });
        let out = "";
        let errOut = "";
        proc.stdout.on("data", (c) => (out += c.toString("utf8")));
        proc.stderr.on("data", (c) => (errOut += c.toString("utf8")));
        proc.on("error", (err) =>
          resolve({ ok: false, error: `pipeline.validate failed to start: ${err.message}` })
        );
        proc.on("close", (code) =>
          resolve(
            code === 0
              ? { ok: true, output: out.trim() }
              : {
                  ok: false,
                  error:
                    `pipeline.validate --accept all exited ${code}. ` +
                    (errOut.trim().split("\n").slice(-3).join(" ").slice(0, 400) ||
                      "No error output."),
                }
          )
        );
      });

      // The fresh thread's first entry says what just happened — the same
      // honesty rule as every other system note.
      if (this.threadStore) {
        try {
          this.threadStore.append(asOf, "system", "note", {
            text: rebuild.ok
              ? "Ready. Ask me anything about this workflow — what earned " +
                "attention, why a center was flagged, what happened to last " +
                "week's recommendations — or start a run above." +
                (hadLiveRun ? " (The run in flight was stopped.)" : "")
              : "The data could not be prepared, so nothing can be answered " +
                `yet: ${rebuild.error}`,
          });
        } catch {
          // a note that cannot be written must not undo the reset
        }
      }

      return {
        ok: true,
        asOf,
        stoppedRun: hadLiveRun,
        threadArchived: threadArchived
          ? path.relative(this.repoRoot, threadArchived)
          : null,
        dataRebuilt: rebuild.ok,
        ...(rebuild.ok ? {} : { dataRebuildError: rebuild.error }),
      };
    } finally {
      this._resetting = false;
    }
  }

  // Full reset — every Monday, plus the ledger.
  //
  // reset(asOf) is per-Monday and deliberately leaves the ledger alone: it is
  // append-only memory SHARED by both weeks, and that is the point of it. But
  // "start the demo over" means the ledger too, or the second run re-checks
  // rows a practice run created. So: reset each Monday in turn, then rotate
  // ledger.csv + ledger_log.jsonl into DATA/OUTPUTS/ledger_archive/.
  //
  // The rotation shells out to `pipeline.ledger --reset-ledger`. This file does
  // not move those two files itself — the pipeline module is the single door to
  // them, the same rule POST /api/ledger/decide follows. Nothing is deleted.
  async resetAll(by, weeks) {
    const results = [];
    for (const asOf of weeks) {
      // Each call takes and releases the _resetting guard, so run them in turn.
      results.push(await this.reset(asOf, by));
    }
    const failed = results.filter((r) => r && r.error);
    if (failed.length === weeks.length && weeks.length > 0) {
      return { error: failed[0].error, status: failed[0].status || 500 };
    }

    const ledger = await new Promise((resolve) => {
      const proc = spawn(
        this.pythonBin,
        ["-u", "-m", "pipeline.ledger", "--reset-ledger", "--json"],
        { cwd: this.repoRoot, env: { ...process.env, PYTHONUNBUFFERED: "1" }, stdio: ["ignore", "pipe", "pipe"] }
      );
      let out = "";
      let errOut = "";
      proc.stdout.on("data", (c) => (out += c.toString("utf8")));
      proc.stderr.on("data", (c) => (errOut += c.toString("utf8")));
      proc.on("error", (err) =>
        resolve({ ok: false, error: `pipeline.ledger failed to start: ${err.message}` })
      );
      proc.on("close", (code) => {
        if (code !== 0) {
          return resolve({
            ok: false,
            error:
              `pipeline.ledger --reset-ledger exited ${code}. ` +
              (errOut.trim().split("\n").slice(-3).join(" ").slice(0, 400) || "No error output."),
          });
        }
        try {
          const last = out.trim().split("\n").filter(Boolean).pop();
          resolve({ ok: true, archivedTo: JSON.parse(last).archived_to });
        } catch {
          // It exited 0, so the rotation happened; only the report is unreadable.
          resolve({ ok: true, archivedTo: null });
        }
      });
    });

    // Correction decisions are shared, append-only memory like the ledger, so
    // only the full reset clears them. It has to happen LAST: each week's reset
    // rebuilds DATA/TRANSLATION/ with `validate --accept all`, which records the
    // decisions again — rotating before that would leave them right back.
    const decisions = await new Promise((resolve) => {
      const proc = spawn(
        this.pythonBin,
        ["-u", "-m", "pipeline.validate", "--rotate-decisions", "--json"],
        { cwd: this.repoRoot, env: { ...process.env, PYTHONUNBUFFERED: "1" }, stdio: ["ignore", "pipe", "pipe"] }
      );
      let out = "";
      let errOut = "";
      proc.stdout.on("data", (c) => (out += c.toString("utf8")));
      proc.stderr.on("data", (c) => (errOut += c.toString("utf8")));
      proc.on("error", (err) =>
        resolve({ ok: false, error: `pipeline.validate failed to start: ${err.message}` })
      );
      proc.on("close", (code) => {
        if (code !== 0) {
          return resolve({
            ok: false,
            error:
              `pipeline.validate --rotate-decisions exited ${code}. ` +
              (errOut.trim().split("\n").slice(-3).join(" ").slice(0, 400) || "No error output."),
          });
        }
        try {
          const last = out.trim().split("\n").filter(Boolean).pop();
          resolve({ ok: true, archivedTo: JSON.parse(last).archived_to });
        } catch {
          resolve({ ok: true, archivedTo: null });
        }
      });
    });

    return {
      ok: true,
      weeks: results.map((r, i) => ({ asOf: weeks[i], ok: Boolean(r && r.ok), error: (r && r.error) || null })),
      stoppedRun: results.some((r) => r && r.stoppedRun),
      dataRebuilt: results.some((r) => r && r.dataRebuilt),
      ledgerArchived: ledger.ok ? ledger.archivedTo : null,
      ...(ledger.ok ? {} : { ledgerResetError: ledger.error }),
      decisionsArchived: decisions.ok ? decisions.archivedTo : null,
      ...(decisions.ok ? {} : { decisionsResetError: decisions.error }),
    };
  }

  // opts:
  //   extraArgs   extra CLI args for pipeline.run (tests use this)
  //   inputsDir   absolute path to an upload session holding the four tables
  //               under canonical names; when set, every step runs with
  //               PETFOLK_INPUTS_DIR=<inputsDir> so the whole run computes
  //               off the uploaded files.
  //   uploadId    the session id (surfaced in /api/run/status only)
  //   autoAccept  apply every proposed correction without pausing
  start(asOf, opts = {}) {
    if (this._resetting) {
      return {
        error: "A session reset is in progress — start the run once it finishes.",
        status: 409,
      };
    }
    if (this.isRunning()) {
      return {
        error: this.state.awaitingCorrections
          ? "A run is paused waiting for a corrections decision (POST /api/run/corrections)."
          : "A run is already in progress.",
        status: 409,
      };
    }
    const runModule = path.join(this.repoRoot, "pipeline", "run.py");
    if (!fs.existsSync(runModule)) {
      return {
        error:
          "pipeline/run.py does not exist yet — the pipeline entrypoint has not been built. Nothing was started.",
        status: 503,
      };
    }
    if (!fs.existsSync(this.pythonBin)) {
      return {
        error: `Python not found at ${this.pythonBin}. Create the venv at the repo root first (python3 -m venv .venv && .venv/bin/pip install pandas numpy pytest).`,
        status: 503,
      };
    }
    if (!fs.existsSync(path.join(this.repoRoot, "pipeline", "validate.py"))) {
      return {
        error: "pipeline/validate.py does not exist — cannot run Data Validation & Check.",
        status: 503,
      };
    }
    const inputsDir = opts.inputsDir || null;
    const autoAccept = Boolean(opts.autoAccept);

    const startedMs = Date.now();
    this.state = {
      running: true,
      started: true,
      asOf,
      inputs: inputsDir
        ? { mode: "uploaded", dir: inputsDir, uploadId: opts.uploadId || null }
        : { mode: "repo" },
      autoAccept,
      awaitingCorrections: false,
      corrections: null,
      correctionsCarried: null,
      correctionsDecision: null,
      carryIn: null,
      validation: null,
      startedAt: new Date(startedMs).toISOString(),
      finishedAt: null,
      exitCode: null,
      error: null,
      events: [],
      phases: Object.fromEntries(PHASES.map((p) => [p, "pending"])),
      phaseTimes: {},
      narrations: {}, // per-phase narration lifecycle (generating/done/failed)
      _startedMs: startedMs,
      _seenArtifacts: new Set(),
      _narrated: new Set(),
      _phaseLatest: {}, // last pipeline timestamp seen per phase (span ends)
      _env: null,
      _extraArgs: opts.extraArgs || [],
    };

    // A previous run's narrations must not outlive it into this run: their
    // results are token-guarded out of the new thread, and their processes
    // (python + any claude CLI children) are stopped, not orphaned.
    for (const c of [...this._narrateChildren]) this._stopChild(c);

    this._tails = [];
    const outDir = path.join(this.repoRoot, "DATA", "OUTPUTS", asOf);
    // The validation step's own run log (one line per check) — real events.
    // dedupe: the accept step re-runs the same deterministic checks; each
    // check line is shown once, and the new apply.* decision lines always show.
    this._addTail(
      path.join(this.repoRoot, "DATA", "OUTPUTS", "validation", "runlog.jsonl"),
      "runlog",
      { dedupe: true }
    );
    this._addTail(path.join(outDir, "signals_runlog.jsonl"), "runlog");
    this._addTail(path.join(outDir, "runlog.jsonl"), "runlog");
    // narrate's own append-only log (separate from runlog.jsonl, which the
    // verdicts step reopens fresh mid-run) — its number/language check events
    // are real receipts and belong in the console like every other check.
    this._addTail(path.join(outDir, "narrate_runlog.jsonl"), "runlog");
    this._addTail(path.join(this.repoRoot, "DATA", "OUTPUTS", "ledger_log.jsonl"), "ledger_log");

    this._timer = setInterval(() => {
      this._pollArtifacts();
      this._pollTails();
    }, 300);

    const env = { ...process.env, PYTHONUNBUFFERED: "1" };
    if (inputsDir) env.PETFOLK_INPUTS_DIR = inputsDir;
    this.state._env = env;

    this._refreshPlainCtx();

    // A thread and a console belong to ONE run: retire the previous attempt's
    // pair for this Monday (kept in DATA/OUTPUTS/<asOf>/thread_archive/) so this run
    // starts on a clean page instead of under stale entries. Rotating them
    // together is what keeps the two surfaces telling the same story.
    let archived = null;
    if (this.threadStore) {
      try {
        archived = this.threadStore.rotate(asOf);
      } catch {
        // a thread that cannot be rotated must not stop a run
      }
    }
    this._rotateConsole(asOf);
    // A run owns exactly one history. Rotating it here — with the thread and
    // the console — is what stops a re-run's agent from reading the previous
    // run's reasoning as if it were its own.
    this._rotateRunContext(asOf);
    if (archived) {
      this._event(
        "validation",
        "server",
        `The previous ${asOf} session is archived in ` +
          `${path.relative(this.repoRoot, path.dirname(archived))}/.`
      );
    }

    if (inputsDir) {
      this._event(
        "validation",
        "server",
        `Running on uploaded files: PETFOLK_INPUTS_DIR=${inputsDir}. ` +
          "Data Validation & Check runs on these files and rebuilds DATA/TRANSLATION/ from them."
      );
    } else {
      this._event(
        "validation",
        "server",
        "Running on the repo's DATA/INPUTS/. Data Validation & Check runs first and proposes corrections."
      );
    }
    this._thread("system", "narration", {
      text:
        `Run started for ${asOf} on ` +
        (inputsDir ? "your uploaded files" : "the repo's DATA/INPUTS/") +
        (autoAccept ? " (auto-accept corrections is on)." : "."),
      phase: "validation",
    });

    this._emitRunState(); // a watching client sees the run open immediately

    if (autoAccept) {
      // No-pause run: one validate pass that proposes AND applies everything.
      this._event(
        "validation",
        "server",
        "Auto-accept is on — every proposed correction will be applied without pausing."
      );
      this._spawnStep(
        ["-u", "-m", "pipeline.validate", "--as-of", asOf, "--accept", "all"],
        env,
        "validation",
        (code) => {
          if (code !== 0) {
            this._finish(
              code,
              `Data Validation & Check exited with code ${code}. See stderr lines above.`
            );
            return;
          }
          this._readValidationOutputs();
          this._refreshPlainCtx();
          const ids = (this.state.corrections || []).map((c) => c.id);
          const carried = this.state.correctionsCarried || [];
          const newlyCovered = carried.reduce(
            (sum, correction) => sum + Number(correction.new_rows || 0), 0
          );
          this.state.correctionsDecision = {
            accepted: ids,
            declined: [],
            by: null,
            auto: true,
            decidedAt: new Date().toISOString(),
          };
          this._markDone("validation");
          const v = this.state.validation;
          if (carried.length > 0 && ids.length === 0) {
            const memoryMsg =
              `Data Validation & Check finished — ${carried.length} prior data-quality ` +
              `decision${carried.length === 1 ? "" : "s"} carried forward · 0 new ` +
              `decisions required${newlyCovered ? ` · ${newlyCovered} newly arrived rows covered by standing rules` : ""}.`;
            this._event("validation", "server", memoryMsg);
            this._threadPlaceholder("validation", memoryMsg);
          } else {
            this._event(
              "validation",
              "server",
              `Data Validation & Check finished (exit 0) — ${ids.length} new correction(s) ` +
                "auto-accepted, DATA/TRANSLATION/ rebuilt."
            );
            this._threadPlaceholder(
              "validation",
              (v ? `Data checks finished — ${v.checks_run} checks ran. ` : "Data checks finished. ") +
                `All ${ids.length} new decisions were accepted automatically and applied to a ` +
                "working copy. The source data was not changed."
            );
          }
          this._startDigestRun(asOf, this.state._extraArgs, env);
        }
      );
      return { ok: true };
    }

    // Default flow: propose first, then pause for the human decision.
    this._spawnStep(
      ["-u", "-m", "pipeline.validate", "--as-of", asOf],
      env,
      "validation",
      (code) => {
        if (code !== 0) {
          this._finish(
            code,
            `Data Validation & Check exited with code ${code}. See stderr lines above.`
          );
          return;
        }
        this._readValidationOutputs();
        this._refreshPlainCtx(); // corrections_proposed.json now on disk
        const n = this.state.corrections ? this.state.corrections.length : 0;
        const v = this.state.validation;
        if (n === 0) {
          // Nothing to decide. Either the checks found nothing, or every
          // correction was already ruled on — say which, because "no
          // corrections" and "four corrections you already decided" are very
          // different statements to a leader.
          const carried = this.state.correctionsCarried || [];
          const newRows = carried.reduce((sum, c) => sum + (c.new_rows || 0), 0);
          const msg = carried.length
            ? `Data Validation & Check: ${carried.length} prior data-quality decision${
                carried.length === 1 ? "" : "s"
              } carried forward from ${
                carried[0].decided_as_of || "an earlier Monday"
              }${
                newRows ? ` (${newRows} newly covered rows)` : ""
              } · 0 new decisions required — continuing without a pause.`
            : "Data Validation & Check proposed no corrections — continuing without a pause.";
          this._event("validation", "server", msg);
          this._threadPlaceholder("validation", msg);
          this.state.correctionsDecision = {
            accepted: [],
            declined: [],
            by: null,
            auto: true,
            decidedAt: new Date().toISOString(),
          };
          // Still run the apply step: it writes DATA/TRANSLATION/ + MANIFEST.json.
          this._applyCorrections("all", () =>
            this._startDigestRun(asOf, this.state._extraArgs, env)
          );
          return;
        }
        this.state.awaitingCorrections = true;
        const pauseMsg =
          (v
            ? `Data Validation & Check: ${v.checks_run} checks ran, ${n} corrections proposed`
            : `Data Validation & Check proposed ${n} corrections`) +
          " — run paused for your accept/decline decision.";
        this._event("validation", "server", pauseMsg);
        this._thread("system", "narration", {
          text: v
            ? `Data Validation & Check finished — ${v.checks_run} checks ran, ${n} corrections proposed.`
            : `Data Validation & Check proposed ${n} corrections.`,
          phase: "validation",
        });
        this._thread("system", "decision_request", {
          corrections: this.state.corrections,
          status: "awaiting",
        });
        // A paused run is where a run sits longest, so the pause itself is
        // snapshotted: reload, and the console still shows what it is waiting on.
        this._persistState();
        this._emitRunState();
      }
    );

    return { ok: true };
  }
}

module.exports = { RunManager, PHASES };
