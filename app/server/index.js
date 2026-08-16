// Petfolk Monday Digest — Node API layer.
//
// HARD RULE: this API computes NOTHING analytical. It serves the
// Python pipeline's output files verbatim (DATA/OUTPUTS/…), parses CSV/JSONL into
// JSON for the browser, and spawns the real pipeline on request. Every number
// a leader sees was written by pipeline/*.py.
//
//   GET  /api/health          what's on disk (weeks, ledger, pipeline, python)
//   GET  /api/llm-modes       which model tiers this machine can ask
//                             (Claude CLI / Anthropic key / OpenAI key /
//                             deterministic) — the panel's model switcher
//   GET  /api/weeks           which Mondays have pipeline output
//   GET  /api/digest/:asOf    digest.json verbatim; partial fallback from
//                             signals.json + validation report if absent
//   GET  /api/ledger          DATA/OUTPUTS/ledger.csv + ledger_log.jsonl as JSON
//   POST /api/ledger/decide {recId, action, note?, newCheckBy?, execution?, asOf?}
//                             close with credit / relaunch / escalate /
//                             dismiss — recorded by pipeline.ledger --decide
//                             and posted to the Monday's thread
//   POST /api/run {asOf, uploadId?}
//                             spawn python -m pipeline.run --as-of <asOf>;
//                             with uploadId, validate + run first on that
//                             upload session via PETFOLK_INPUTS_DIR
//   GET  /api/run/status      phase-tagged progress of the current run
//   POST /api/reset {asOf}    archive the Monday's thread + console, stop any
//                             run in flight, rebuild canonical DATA/TRANSLATION/ from
//                             DATA/INPUTS/ — a clean start, nothing deleted
//   POST /api/reset {all:true}
//                             the same for EVERY Monday, then rotate the shared
//                             ledger into DATA/OUTPUTS/ledger_archive/ via
//                             pipeline.ledger --reset-ledger. Still nothing deleted
//   POST /api/upload[?uploadId=…]
//                             sniff CSV headers, stage under canonical names
//   GET  /api/thread?asOf=…   the Monday's conversation thread + pending ask
//   POST /api/thread/message  ask a question (answered by pipeline.ask, async)
//   GET  /api/ask/stream?asOf=…
//                             server-sent events: the answer as it is written
//                             (provisional deltas) then the validated entry
//   POST /api/thread/cancel   stop the answer being generated
//   GET  /api/ask-log         DATA/OUTPUTS/ask_log.jsonl: what was asked, what was
//                             refused — the product-discovery instrument
//   WS   /api/ws              the session tunnel: the browser sends every call
//                             above through ONE socket and receives the same
//                             live events on it. An ADDITIONAL transport —
//                             every HTTP route here is unchanged — and hosted
//                             it is what keeps a session on one function
//                             instance (lib/ws-tunnel.js).
//
// Env overrides (used by the test harness; defaults fit this repo):
//   PETFOLK_REPO_ROOT  repo root holding pipeline/, DATA/OUTPUTS/, DATA/TRANSLATION/
//   PETFOLK_PYTHON     python binary (default <repo>/.venv/bin/python)
//   PORT               API port (default 4600)

const path = require("path");
const fs = require("fs");
const http = require("http");
const crypto = require("crypto");
const express = require("express");
const multer = require("multer");

const { parseCsv, parseCsvObjects } = require("./lib/csv");
const { assemblePartialDigest } = require("./lib/partial-digest");
const { RunManager } = require("./lib/run-manager");
const { ThreadStore } = require("./lib/thread");
const { AskEngine } = require("./lib/ask-engine");
const { LedgerDecider } = require("./lib/ledger-decide");
const { attachTunnel, INSTANCE_ID } = require("./lib/ws-tunnel");

const REPO_ROOT =
  process.env.PETFOLK_REPO_ROOT || path.resolve(__dirname, "..", "..");
const PYTHON_BIN =
  process.env.PETFOLK_PYTHON || path.join(REPO_ROOT, ".venv", "bin", "python");
const OUTPUTS_DIR = path.join(REPO_ROOT, "DATA", "OUTPUTS");
// Upload staging. PETFOLK_UPLOADS_DIR moves it off the server folder for a
// deployment whose code directory is read-only (see api/index.js); locally it
// is unset and staging stays where it always was.
const UPLOADS_DIR =
  process.env.PETFOLK_UPLOADS_DIR || path.join(__dirname, "uploads");
const PORT = Number(process.env.PORT || 4600);

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

const app = express();
app.use(express.json());

const threadStore = new ThreadStore(REPO_ROOT);
const runManager = new RunManager(REPO_ROOT, PYTHON_BIN, threadStore);
// Asks wait while a run owns the artifacts, so no answer is ever grounded in a
// half-rewritten set of files — and the panel says which kind of wait it is.
const askEngine = new AskEngine(REPO_ROOT, PYTHON_BIN, threadStore, () => {
  if (!runManager.isRunning()) return null;
  return runManager.isAwaitingCorrections() ? "awaiting_decision" : "running";
});
// Acting on a tracked recommendation goes through the pipeline's own decision
// machinery — this server records nothing into the ledger itself.
const ledgerDecider = new LedgerDecider(REPO_ROOT, PYTHON_BIN, threadStore);

// ---------------------------------------------------------------------------
// Health + week discovery
// ---------------------------------------------------------------------------

function listWeeks() {
  if (!fs.existsSync(OUTPUTS_DIR)) return [];
  return fs
    .readdirSync(OUTPUTS_DIR)
    .filter((name) => DATE_RE.test(name))
    .sort()
    .map((asOf) => ({
      as_of: asOf,
      has_digest: fs.existsSync(path.join(OUTPUTS_DIR, asOf, "digest.json")),
      has_signals: fs.existsSync(path.join(OUTPUTS_DIR, asOf, "signals.json")),
    }));
}

// The most recent Monday with a full digest — the thread a decision belongs to
// when the caller does not name one (no run this session, e.g. after a restart).
function latestWeek() {
  const withDigest = listWeeks().filter((w) => w.has_digest);
  const weeks = withDigest.length > 0 ? withDigest : listWeeks();
  return weeks.length > 0 ? weeks[weeks.length - 1].as_of : null;
}

// ---------------------------------------------------------------------------
// Which models this machine can actually ask
//
// The same detection pipeline/verdicts.py does, in the order of its ladder:
// the Claude CLI on PATH, then an Anthropic key, then an OpenAI key, then the
// deterministic tier that always works. Keys may live in the gitignored
// .env.local that pipeline/config.py loads, so this reads NAMES from that file
// too — never values, which stay with the python process that uses them.
// This is capability reporting only; the pipeline still decides and still
// reports which tier actually wrote each answer.
function envNamesFromLocalFile() {
  try {
    return new Set(
      fs
        .readFileSync(path.join(REPO_ROOT, ".env.local"), "utf8")
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l && !l.startsWith("#") && l.includes("="))
        .map((l) => l.split("=", 1)[0].trim())
    );
  } catch {
    return new Set();
  }
}

function onPath(binary) {
  return (process.env.PATH || "")
    .split(path.delimiter)
    .some((dir) => dir && fs.existsSync(path.join(dir, binary)));
}

function availableLlmModes() {
  const local = envNamesFromLocalFile();
  const has = (name) => Boolean(process.env[name]) || local.has(name);
  const modes = [];
  if (has("PETFOLK_OPENAI_API_KEY") || has("OPENAI_API_KEY")) modes.push("openai");
  if (onPath("claude")) modes.push("claude-cli");
  if (has("ANTHROPIC_API_KEY")) modes.push("api");
  modes.push("template");
  return modes;
}

app.get("/api/llm-modes", (req, res) => {
  const modes = availableLlmModes();
  res.json({
    available: modes,
    // The tier the pipeline picks when the caller does not choose one.
    default: modes[0],
    note:
      "A chosen tier is a request: if it cannot be reached the pipeline " +
      "degrades down the ladder and the answer names the tier that wrote it.",
  });
});

app.get("/api/health", (req, res) => {
  res.json({
    ok: true,
    // Which process answered. Hosted, that is one function instance out of
    // however many are warm — the value the session tunnel exists to keep
    // constant for a browser session (see lib/ws-tunnel.js).
    instance_id: INSTANCE_ID,
    hosted: Boolean(process.env.PETFOLK_HOSTED),
    repo_root: REPO_ROOT,
    pipeline_present: fs.existsSync(path.join(REPO_ROOT, "pipeline", "run.py")),
    python_present: fs.existsSync(PYTHON_BIN),
    ledger_present: fs.existsSync(path.join(OUTPUTS_DIR, "ledger.csv")),
    validation_report_present: fs.existsSync(
      path.join(OUTPUTS_DIR, "validation", "report.json")
    ),
    weeks: listWeeks(),
  });
});

app.get("/api/weeks", (req, res) => {
  res.json({ weeks: listWeeks() });
});

// ---------------------------------------------------------------------------
// Digest
// ---------------------------------------------------------------------------

app.get("/api/digest/:asOf", (req, res) => {
  const asOf = req.params.asOf;
  if (!DATE_RE.test(asOf)) {
    return res
      .status(400)
      .json({ error: "asOf must be a date like 2026-05-04." });
  }

  // Full digest: serve the pipeline's file byte-for-byte.
  const digestPath = path.join(OUTPUTS_DIR, asOf, "digest.json");
  if (fs.existsSync(digestPath)) {
    return res.type("application/json").send(fs.readFileSync(digestPath, "utf8"));
  }

  // Partial digest: signals exist but verdicts/ledger/digest do not yet.
  const partial = assemblePartialDigest(REPO_ROOT, asOf);
  if (partial) return res.json(partial);

  return res.status(404).json({
    error:
      `No pipeline output for ${asOf}. Neither DATA/OUTPUTS/${asOf}/digest.json ` +
      `nor DATA/OUTPUTS/${asOf}/signals.json exists. Run the pipeline for this ` +
      `Monday first (POST /api/run).`,
    weeks_available: listWeeks(),
  });
});

// ---------------------------------------------------------------------------
// Ledger
// ---------------------------------------------------------------------------

app.get("/api/ledger", (req, res) => {
  const csvPath = path.join(OUTPUTS_DIR, "ledger.csv");
  if (!fs.existsSync(csvPath)) {
    return res.status(404).json({
      error:
        "DATA/OUTPUTS/ledger.csv does not exist yet. The ledger is created by the " +
        "first pipeline run (POST /api/run).",
    });
  }
  const rows = parseCsvObjects(fs.readFileSync(csvPath, "utf8"));

  const logPath = path.join(OUTPUTS_DIR, "ledger_log.jsonl");
  let log = [];
  if (fs.existsSync(logPath)) {
    log = fs
      .readFileSync(logPath, "utf8")
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => {
        try {
          return JSON.parse(l);
        } catch {
          return { unparseable_line: l };
        }
      });
  }
  res.json({
    state_file: "DATA/OUTPUTS/ledger.csv",
    log_file: "DATA/OUTPUTS/ledger_log.jsonl",
    rows,
    log,
  });
});

// A leader acting on a tracked recommendation: close it with credit, relaunch
// it with a new date, escalate it, or dismiss it with a reason. The decision is
// recorded by `python -m pipeline.ledger --decide` (state table + append-only
// receipt) and posted to the Monday's thread, so a reload shows the resolved
// state from the pipeline's own files rather than from the browser's memory.
app.post("/api/ledger/decide", async (req, res) => {
  const body = req.body || {};
  const asOf =
    typeof body.asOf === "string" && DATE_RE.test(body.asOf)
      ? body.asOf
      : (runManager.state && runManager.state.asOf) || latestWeek();
  const result = await ledgerDecider.decide({
    recId: body.recId,
    action: body.action,
    note: body.note,
    newCheckBy: body.newCheckBy,
    // Only used by action:"attest" — whether the work was actually carried out.
    execution: typeof body.execution === "string" ? body.execution : null,
    by: typeof body.by === "string" ? body.by : null,
    asOf,
  });
  if (result.error) {
    return res.status(result.status || 500).json({ error: result.error });
  }
  res.json({ ok: true, asOf, ...result });
});

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

app.post("/api/run", (req, res) => {
  const asOf = req.body && req.body.asOf;
  if (!asOf || !DATE_RE.test(asOf)) {
    return res
      .status(400)
      .json({ error: "Body must be JSON like {\"asOf\": \"2026-05-04\"}." });
  }

  // Optional uploadId: run the pipeline on that upload session's files
  // (PETFOLK_INPUTS_DIR) instead of the repo's DATA/INPUTS/. The session must
  // hold all four tables.
  const uploadId = req.body && req.body.uploadId;
  let inputsDir = null;
  if (uploadId !== undefined && uploadId !== null && uploadId !== "") {
    if (typeof uploadId !== "string" || !UPLOAD_ID_RE.test(uploadId)) {
      return res.status(400).json({
        error: "uploadId must be an id returned by POST /api/upload.",
      });
    }
    inputsDir = path.join(UPLOADS_DIR, uploadId);
    if (!fs.existsSync(inputsDir)) {
      return res.status(400).json({
        error: `Upload session ${uploadId} not found. POST /api/upload first.`,
      });
    }
    const tables = sessionTables(inputsDir);
    const missing = EXPECTED_TABLES.filter((t) => !tables[t]);
    if (missing.length > 0) {
      return res.status(400).json({
        error:
          `Upload session ${uploadId} is missing ${missing.length} of the 4 ` +
          `tables: ${missing.join(", ")}. Upload them ` +
          `(POST /api/upload?uploadId=${uploadId}) before running on uploaded files.`,
        missing,
      });
    }
  }

  // Optional autoAccept: apply every proposed correction without pausing.
  // Default (false) pauses the run at "awaiting corrections" until
  // POST /api/run/corrections decides accept/decline per correction.
  const autoAccept = Boolean(req.body && req.body.autoAccept);

  const result = runManager.start(asOf, {
    autoAccept,
    ...(inputsDir ? { inputsDir, uploadId } : {}),
  });
  if (result.error) {
    return res.status(result.status || 500).json({ error: result.error });
  }
  res.status(202).json({
    ok: true,
    asOf,
    autoAccept,
    inputs: inputsDir ? { mode: "uploaded", uploadId } : { mode: "repo" },
    poll: "/api/run/status",
  });
});

// ?asOf=YYYY-MM-DD scopes the console to one Monday: the live run when it is
// that Monday's, otherwise that Monday's last run restored from
// DATA/OUTPUTS/<asOf>/run_state.json + console.jsonl. Without it, the console went
// blank on a server restart while the thread beside it still showed the run.
app.get("/api/run/status", (req, res) => {
  const asOf = req.query.asOf;
  if (asOf !== undefined && !DATE_RE.test(String(asOf))) {
    return res.status(400).json({ error: "asOf must look like 2026-05-04." });
  }
  res.json(runManager.status(asOf ? String(asOf) : undefined));
});

// The human accept/decline decision for a paused run. Body:
//   {"accept": ["C1", "C2"]}        apply these, decline the rest
//   {"accept": [...], "by": "..."}  optional attribution, shown in the feed
// The server re-runs `pipeline.validate --accept <ids>` — declined
// corrections are logged by the pipeline and DATA/TRANSLATION/ is built without them —
// then continues with the digest run. DATA/INPUTS/ is never modified.
app.post("/api/run/corrections", (req, res) => {
  const accept = req.body && req.body.accept;
  const by = req.body && typeof req.body.by === "string" ? req.body.by : null;
  const result = runManager.resolveCorrections(accept, by);
  if (result.error) {
    return res.status(result.status || 500).json({ error: result.error });
  }
  res.status(202).json({
    ok: true,
    accepted: result.accepted,
    declined: result.declined,
    poll: "/api/run/status",
  });
});

// Session reset — so the whole demo can run again from scratch, any number of
// times. Archives (never deletes) the Monday's thread and console, stops any
// pipeline step in flight, clears the persisted + in-memory run state, and
// rebuilds canonical DATA/TRANSLATION/ from DATA/INPUTS/ via the real validate step. In-flight
// questions for that Monday are stopped first, so their cancellation notes
// land in the thread being archived, not the fresh one.
app.post("/api/reset", async (req, res) => {
  const by = req.body && typeof req.body.by === "string" ? req.body.by : null;

  // {all: true} — every Monday plus the ledger. This is what the header's
  // Reset button sends: a per-Monday reset leaves the shared, append-only
  // ledger holding the rows a practice run wrote, so the next demo's second
  // run re-checks them.
  if (req.body && req.body.all === true) {
    const weeks = listWeeks().map((w) => w.as_of);
    if (weeks.length === 0) {
      return res.status(400).json({ error: "No Mondays on disk to reset." });
    }
    for (const asOf of weeks) askEngine.cancel(asOf);
    const result = await runManager.resetAll(by, weeks);
    if (result.error) {
      return res.status(result.status || 500).json({ error: result.error });
    }
    return res.json(result);
  }

  const asOf = req.body && req.body.asOf;
  if (!asOf || !DATE_RE.test(asOf)) {
    return res
      .status(400)
      .json({ error: "Body must be JSON like {\"asOf\": \"2026-05-04\"} or {\"all\": true}." });
  }
  askEngine.cancel(asOf); // a 409 "nothing to cancel" is fine here
  const result = await runManager.reset(asOf, by);
  if (result.error) {
    return res.status(result.status || 500).json({ error: result.error });
  }
  res.json(result);
});

// ---------------------------------------------------------------------------
// Conversation thread — the server-owned, per-Monday thread the side panel
// renders from (DATA/OUTPUTS/<asOf>/thread.jsonl, append-only). The run manager is
// the only writer of AI entries; this API serves the thread and appends user
// messages.
// ---------------------------------------------------------------------------

app.get("/api/thread", (req, res) => {
  const asOf =
    typeof req.query.asOf === "string" && DATE_RE.test(req.query.asOf)
      ? req.query.asOf
      : null;
  if (!asOf) {
    return res
      .status(400)
      .json({ error: "Pass ?asOf=YYYY-MM-DD (the digest Monday)." });
  }
  res.json({
    asOf,
    entries: threadStore.read(asOf),
    // The in-flight question, if any — the panel's "thinking" state comes
    // from the server, so a reload or a second browser sees the same truth.
    pending: askEngine.pending(asOf),
    // Phases whose narrative pipeline.narrate is writing right now — the
    // panel's lightweight "AI is writing…" state, also server-owned.
    narrating: runManager.narrating(asOf),
  });
});

// Ask a question about a Monday. The question is persisted to the thread at
// once (it survives a reload even if the answer never lands) and the real
// pipeline module answers it in the background — 15-40s in claude-cli mode —
// so this returns 202 and the panel watches GET /api/thread.
app.post("/api/thread/message", (req, res) => {
  const text = req.body && typeof req.body.text === "string" ? req.body.text.trim() : "";
  if (!text) {
    return res.status(400).json({ error: 'Body must be JSON like {"text": "…"}.' });
  }
  if (text.length > 1000) {
    return res.status(400).json({
      error: `That question is ${text.length} characters; keep it under 1000.`,
    });
  }
  const bodyAsOf =
    req.body && typeof req.body.asOf === "string" && DATE_RE.test(req.body.asOf)
      ? req.body.asOf
      : null;
  const runAsOf = runManager.state && runManager.state.asOf;
  const asOf = bodyAsOf || runAsOf;
  if (!asOf) {
    return res.status(400).json({
      error: "No run this session — pass asOf (YYYY-MM-DD) to say which Monday's thread this is for.",
    });
  }
  const by = req.body && typeof req.body.by === "string" ? req.body.by : null;
  // Which model to ask (the panel's switcher). Only tiers this machine can
  // actually serve are accepted, so a stale browser cannot request a model
  // that is not there; omitted means the pipeline auto-detects as before.
  const requested = req.body && req.body.llmMode;
  if (requested !== undefined && requested !== null && requested !== "") {
    if (!availableLlmModes().includes(requested)) {
      return res.status(400).json({
        error:
          `llmMode ${JSON.stringify(requested)} is not available here. ` +
          `Available: ${availableLlmModes().join(", ")}.`,
      });
    }
  }
  const llmMode = requested || null;
  const { entry, position } = askEngine.ask(asOf, text, by, llmMode);
  res.status(202).json({
    ok: true,
    asOf,
    entries: [entry],
    queue_position: position,
    pending: askEngine.pending(asOf),
    poll: `/api/thread?asOf=${asOf}`,
  });
});

// Watch one Monday's answer being written, as server-sent events. The panel
// opens this once and keeps it open; every event is a `data:` line holding one
// JSON object with a `type`:
//
//   hello          {pending, stream}  the state on connect — including the
//                                     text so far, so joining mid-answer (or
//                                     reloading) catches up instead of waiting
//   start          a question left the queue and is being worked on
//   generating     a model call has begun
//   delta          {text}  provisional text, exactly as the model produced it
//   verifying      generation finished; the harness is checking it
//   redo           that reply was rejected (a number did not verify) —
//                  the text so far is discarded and generation restarts
//   tier_fallback  {from,to,reason}  the tier could not be reached
//   final          {entry}  the validated thread entry — the only text here
//                  that carries citations, a tier label and a verified count
//   note/cancelled the answer ended without one (crash, timeout, stop)
//
// The run's own narratives stream on the same channel, one set per phase
// (phases are narrated concurrently, so every event names its phase):
//   narration_generating / narration_delta / narration_verifying /
//   narration_redo / narration_tier_fallback / narration_final /
//   narration_failed. Same rule: the draft is provisional, and the phase's
//   narrative is whatever lands in the thread after its checks pass.
//
// The honesty rule this endpoint exists to keep: `delta` text is provisional
// and is never persisted; only `final` is an answer. A client that ignores
// this stream entirely still sees the same thread via GET /api/thread.
// One live view of a Monday, used by BOTH transports: this SSE endpoint and
// the WebSocket tunnel (lib/ws-tunnel.js). Whatever a subscriber receives here
// it receives there, in the same order, starting with the same `hello` — so
// there is one live-event contract, not two that can drift apart.
function subscribeSession(asOf, send) {
  send({
    type: "hello",
    asOf,
    pending: askEngine.pending(asOf),
    stream: askEngine.streamState(asOf),
    // Narratives in flight, with the text written so far — the run talking
    // about itself streams exactly like an answer does.
    narrating: runManager.narrating(asOf),
  });
  const unsubscribeAsk = askEngine.subscribe(asOf, send);
  const unsubscribeRun = runManager.subscribe(asOf, send);
  return () => {
    unsubscribeAsk();
    unsubscribeRun();
  };
}

app.get("/api/ask/stream", (req, res) => {
  const asOf =
    typeof req.query.asOf === "string" && DATE_RE.test(req.query.asOf)
      ? req.query.asOf
      : null;
  if (!asOf) {
    return res
      .status(400)
      .json({ error: "Pass ?asOf=YYYY-MM-DD (the digest Monday)." });
  }

  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no", // no proxy may buffer this
  });
  if (typeof res.flushHeaders === "function") res.flushHeaders();

  const send = (event) => {
    try {
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    } catch {
      // client vanished mid-write; the close handler cleans up
    }
  };

  const unsubscribe = subscribeSession(asOf, send);
  // Comment heartbeats keep proxies (and dev servers) from closing an idle
  // stream; they are not events and clients ignore them.
  const beat = setInterval(() => {
    try {
      res.write(": keep-alive\n\n");
    } catch {
      /* handled by close */
    }
  }, 15000);

  req.on("close", () => {
    clearInterval(beat);
    unsubscribe();
    res.end();
  });
});

// Stop the answer being generated for a Monday. The question stays in the
// thread and the stop is recorded — nothing is ever half-generated into it.
app.post("/api/thread/cancel", (req, res) => {
  const asOf =
    req.body && typeof req.body.asOf === "string" && DATE_RE.test(req.body.asOf)
      ? req.body.asOf
      : null;
  if (!asOf) {
    return res.status(400).json({ error: 'Body must be JSON like {"asOf": "2026-05-04"}.' });
  }
  const result = askEngine.cancel(asOf);
  if (result.error) {
    return res.status(result.status || 500).json({ error: result.error });
  }
  res.status(202).json({ ok: true, asOf, ...result });
});

// The append-only ask log (DATA/OUTPUTS/ask_log.jsonl) for one Monday.
// Asked-and-answered is a digest gap: the leader had to ask for something the
// digest should have surfaced. Asked-and-refused is a data gap: the question
// was fair and this pipeline cannot see the answer. Both are product signal,
// which is why the log is a first-class endpoint and not a debug file.
app.get("/api/ask-log", (req, res) => {
  const asOf =
    typeof req.query.asOf === "string" && DATE_RE.test(req.query.asOf)
      ? req.query.asOf
      : null;
  const logPath = path.join(OUTPUTS_DIR, "ask_log.jsonl");
  let entries = [];
  if (fs.existsSync(logPath)) {
    entries = fs
      .readFileSync(logPath, "utf8")
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => {
        try {
          return JSON.parse(l);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
  }
  if (asOf) entries = entries.filter((e) => e.as_of === asOf);
  res.json({
    log_file: "DATA/OUTPUTS/ask_log.jsonl",
    asOf,
    counts: {
      asked: entries.length,
      answered: entries.filter((e) => !e.refused).length,
      refused: entries.filter((e) => e.refused).length,
    },
    entries,
  });
});

// ---------------------------------------------------------------------------
// Upload — an upload session that a run can actually use.
//
// Each uploaded file is identified by SNIFFING ITS HEADER COLUMNS — never by
// filename — and stored in the session's staging dir under its canonical
// table name (locations.csv, clinic_weekly.csv, …). A file whose header
// matches none of the four tables is rejected (deleted from staging).
// One upload session = one staging dir under app/server/uploads/<uploadId>;
// repeat POSTs with ?uploadId=<id> add to (or replace within) the same
// session. DATA/INPUTS/ is never touched. POST /api/run {asOf, uploadId} then
// runs the real pipeline against the session via PETFOLK_INPUTS_DIR.
// ---------------------------------------------------------------------------

const EXPECTED_TABLES = [
  "locations",
  "clinic_weekly",
  "provider_weekly",
  "action_plans",
];

// Columns that uniquely identify each table (a distinctive subset of its real
// header). A file matches a table iff its header contains every signature
// column of that table and of no other table.
const TABLE_SIGNATURES = {
  locations: ["location_id", "location_name", "opened_date", "maturity_tier"],
  clinic_weekly: [
    "location_id",
    "week_start",
    "appts_per_doctor_hour",
    "avg_wait_time_min",
  ],
  provider_weekly: ["provider_id", "location_id", "week_start", "employment_type"],
  action_plans: ["plan_id", "location_id", "target_metric", "baseline_value", "status"],
};

// Read the header row of a staged CSV and return the one table it identifies
// as, or null. Only the first 64KB is read — the header is line one.
function sniffTable(filePath) {
  let firstChunk;
  try {
    const fd = fs.openSync(filePath, "r");
    const buf = Buffer.alloc(64 * 1024);
    const n = fs.readSync(fd, buf, 0, buf.length, 0);
    fs.closeSync(fd);
    firstChunk = buf.toString("utf8", 0, n);
  } catch {
    return null;
  }
  const firstLine = firstChunk.split(/\r?\n/, 1)[0] || "";
  const headerRow = parseCsv(firstLine)[0] || [];
  const header = new Set(headerRow.map((c) => c.trim().replace(/^\uFEFF/, "")));
  const matches = EXPECTED_TABLES.filter((t) =>
    TABLE_SIGNATURES[t].every((col) => header.has(col))
  );
  return matches.length === 1 ? matches[0] : null;
}

const UPLOAD_ID_RE = /^[A-Za-z0-9_-]{1,80}$/;

function sessionTables(dir) {
  const tables = {};
  for (const t of EXPECTED_TABLES) {
    tables[t] = fs.existsSync(path.join(dir, `${t}.csv`));
  }
  return tables;
}

const upload = multer({
  storage: multer.diskStorage({
    destination: (req, file, cb) => {
      if (!req._stagingDir) {
        const requested =
          typeof req.query.uploadId === "string" ? req.query.uploadId : "";
        req._uploadId = UPLOAD_ID_RE.test(requested)
          ? requested
          : new Date().toISOString().replace(/[:.]/g, "-") +
            "-" +
            crypto.randomBytes(3).toString("hex");
        req._stagingDir = path.join(UPLOADS_DIR, req._uploadId);
        fs.mkdirSync(req._stagingDir, { recursive: true });
      }
      cb(null, req._stagingDir);
    },
    filename: (req, file, cb) => {
      // Temp name only — the file is renamed to its canonical table name
      // after its header is sniffed (or deleted if it matches no table).
      cb(null, `.incoming-${crypto.randomBytes(6).toString("hex")}`);
    },
  }),
  limits: { fileSize: 20 * 1024 * 1024, files: 10 },
});

app.post("/api/upload", upload.array("files"), (req, res) => {
  const files = req.files || [];
  if (files.length === 0) {
    return res.status(400).json({
      error:
        "No files received. Send multipart/form-data with one or more CSVs " +
        "in the \"files\" field.",
    });
  }
  const received = files.map((f) => {
    const table = sniffTable(f.path);
    if (!table) {
      fs.unlinkSync(f.path); // rejected files never stay in staging
      return {
        filename: f.originalname,
        bytes: f.size,
        matched_as: null,
        rejected: true,
        reason:
          "Header columns match none of the four expected tables " +
          "(locations, clinic_weekly, provider_weekly, action_plans).",
      };
    }
    fs.renameSync(f.path, path.join(req._stagingDir, `${table}.csv`));
    return {
      filename: f.originalname,
      bytes: f.size,
      matched_as: table,
      stored_as: `${table}.csv`,
    };
  });
  const tables = sessionTables(req._stagingDir);
  const missing = EXPECTED_TABLES.filter((t) => !tables[t]);
  res.json({
    upload_id: req._uploadId,
    staged_to: path.relative(REPO_ROOT, req._stagingDir),
    received,
    expected: EXPECTED_TABLES,
    tables,
    missing,
    complete: missing.length === 0,
    note:
      "Each file was identified by its header columns, never its filename. " +
      "DATA/INPUTS/ is never touched. " +
      (missing.length === 0
        ? "All four tables recognized — POST /api/run with this uploadId to " +
          "run the pipeline on these files."
        : "Upload the missing tables to this session to run on your files."),
  });
});

// ---------------------------------------------------------------------------
// Static: serve the built web app (web/dist) if it exists, so
// `node server/index.js` alone can serve everything on one port.
// ---------------------------------------------------------------------------

const DIST_DIR = path.join(__dirname, "..", "web", "dist");
if (fs.existsSync(DIST_DIR)) {
  app.use(express.static(DIST_DIR));
  // SPA fallback for client-side routes (/admin, /workflow) — GET requests only.
  app.use((req, res, next) => {
    if (req.method !== "GET" || req.path.startsWith("/api/")) return next();
    res.sendFile(path.join(DIST_DIR, "index.html"));
  });
}

// ---------------------------------------------------------------------------
// Transport
//
// The routes above are the API. This is the plumbing that carries them, and
// there are two pipes, not two implementations:
//
//   HTTP  — every route, unchanged, including GET /api/ask/stream. curl, the
//           tests, and any non-browser client are untouched by the tunnel.
//   WS    — /api/ws, a session tunnel that dispatches request envelopes into
//           THIS SAME app in-process and pushes the same live events. It exists
//           because hosted, a browser's requests otherwise land on different
//           function instances and the run state they need lives on one of
//           them (see lib/ws-tunnel.js for the measurement and the protocol).
//
// The server object is created here rather than by app.listen() so the upgrade
// handler has something to attach to, both locally and in the deployed function
// (api/index.js exports this server).
// ---------------------------------------------------------------------------

const server = http.createServer(app);
const tunnel = attachTunnel(server, app, {
  subscribe: subscribeSession,
  path: "/api/ws",
  // Vercel closes a connection at the function's maxDuration (vercel.json:
  // 300s). Telling the client up front is why its reconnect is planned rather
  // than a surprise; locally there is no such limit and this is null.
  maxSessionSeconds: process.env.PETFOLK_HOSTED ? 300 : null,
});

// `node server/index.js` (npm start) listens, exactly as before — on the http
// server, so the WebSocket shares the port with the API. When this file is
// required instead — by a deployment wrapper (api/index.js) or a test — the
// caller owns the transport, so nothing binds a port here.
if (require.main === module) {
  server.listen(PORT, () => {
    console.log(`Petfolk digest API on http://localhost:${PORT}`);
    console.log(`  repo root: ${REPO_ROOT}`);
    console.log(`  python:    ${PYTHON_BIN} ${fs.existsSync(PYTHON_BIN) ? "(found)" : "(MISSING)"}`);
    console.log(
      `  weeks on disk: ${listWeeks().map((w) => w.as_of + (w.has_digest ? "" : " (partial)")).join(", ") || "none"}`
    );
  });
}

// The Express app stays the default export (existing tests and callers require
// it and hand it to a request injector). The http server carrying it — with the
// tunnel attached — hangs off it for the deployment wrapper.
module.exports = app;
module.exports.server = server;
module.exports.tunnel = tunnel;
module.exports.instanceId = INSTANCE_ID;
