// The per-run conversation thread — a first-class server-side resource.
//
// The side panel (and any later client: Slack, email, …) renders EXCLUSIVELY
// from this thread; the server is the only writer of AI entries. Entries are
// appended by the run manager at real moments of a run (phase summaries from
// the plain-English translation layer, the corrections decision request, the
// human decision) and by POST /api/thread/message (user questions).
//
// Persistence: DATA/OUTPUTS/<asOf>/thread.jsonl — append-only, one JSON entry per
// line, so a page reload (or a different client) replays the whole thread.
//
// Entry shape:
//   {id, ts, role: "ai"|"user"|"system", type, payload}
//   type "narration"        payload {text, phase?}
//   type "decision_request" payload {corrections: [...], status: "awaiting"}
//   type "decision"         payload {accepted, declined, by, text}
//   type "question"         payload {text, by?}          — a leader's question
//   type "answer"           payload {text, refused, citations, mode,
//                                    numbers_verified, fallbacks, attempts,
//                                    elapsed_seconds, question, question_id}
//                           — written only from pipeline.ask's own output
//   type "note"             payload {text}               — server/system lines
//
// The JSONL on disk is never rewritten. A decision_request's status is
// derived at read time: it reads "decided" when a decision entry follows it.
//
// One thread belongs to one run of one Monday. Re-running a Monday therefore
// starts a new thread — the previous one is moved into
// DATA/OUTPUTS/<asOf>/thread_archive/ rather than deleted or appended to, so a
// re-run never shows last attempt's narration and no conversation is lost.

const fs = require("fs");
const path = require("path");

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

class ThreadStore {
  constructor(repoRoot) {
    this.repoRoot = repoRoot;
    this._seq = new Map(); // asOf -> next entry number (from line count)
  }

  _file(asOf) {
    return path.join(this.repoRoot, "DATA", "OUTPUTS", asOf, "thread.jsonl");
  }

  _nextSeq(asOf) {
    if (!this._seq.has(asOf)) {
      let n = 0;
      try {
        n = fs
          .readFileSync(this._file(asOf), "utf8")
          .split("\n")
          .filter((l) => l.trim()).length;
      } catch {
        n = 0;
      }
      this._seq.set(asOf, n);
    }
    const n = this._seq.get(asOf);
    this._seq.set(asOf, n + 1);
    return n;
  }

  // Retire the current thread for a Monday so a new run starts clean. Returns
  // the archive path, or null when there was nothing to retire.
  rotate(asOf) {
    if (!DATE_RE.test(asOf)) throw new Error(`bad asOf for thread: ${asOf}`);
    const file = this._file(asOf);
    if (!fs.existsSync(file)) {
      this._seq.set(asOf, 0);
      return null;
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const dir = path.join(path.dirname(file), "thread_archive");
    fs.mkdirSync(dir, { recursive: true });
    const dest = path.join(dir, `thread-${stamp}.jsonl`);
    fs.renameSync(file, dest);
    this._seq.set(asOf, 0);
    return dest;
  }

  append(asOf, role, type, payload) {
    if (!DATE_RE.test(asOf)) throw new Error(`bad asOf for thread: ${asOf}`);
    const entry = {
      id: `${asOf}-${this._nextSeq(asOf)}`,
      ts: new Date().toISOString(),
      role,
      type,
      payload,
    };
    const file = this._file(asOf);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, JSON.stringify(entry) + "\n");
    return entry;
  }

  read(asOf) {
    if (!DATE_RE.test(asOf)) return [];
    let raw;
    try {
      raw = fs.readFileSync(this._file(asOf), "utf8");
    } catch {
      return [];
    }
    const entries = [];
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try {
        entries.push(JSON.parse(line));
      } catch {
        // a torn line mid-append; the next read gets it whole
      }
    }
    // Derive decision_request status: "decided" once a decision entry
    // follows it (the file itself stays append-only).
    let pending = [];
    for (const e of entries) {
      if (e.type === "decision_request") {
        e.payload = { ...e.payload, status: "awaiting" };
        pending.push(e);
      } else if (e.type === "decision") {
        for (const req of pending) req.payload.status = "decided";
        pending = [];
      }
    }
    // Derive narration replacement the same way: a deterministic phase
    // summary (payload.placeholder) is superseded once that phase's generated
    // narrative (payload.kind === "narrative") lands after it. The JSONL is
    // never rewritten — the placeholder stays on disk as the run's record.
    const placeholders = new Map(); // phase -> entries awaiting a narrative
    for (const e of entries) {
      if (e.type !== "narration" || !e.payload) continue;
      const phase = e.payload.phase;
      if (e.payload.placeholder) {
        if (!placeholders.has(phase)) placeholders.set(phase, []);
        placeholders.get(phase).push(e);
      } else if (e.payload.kind === "narrative" && placeholders.has(phase)) {
        for (const ph of placeholders.get(phase)) {
          ph.payload = { ...ph.payload, superseded: true };
        }
        placeholders.delete(phase);
      }
    }
    return entries;
  }
}

module.exports = { ThreadStore };
