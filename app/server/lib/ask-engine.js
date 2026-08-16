// "Ask this Monday" — the server side of the conversation panel.
//
// HARD RULE: this file generates nothing. It spawns the real
// pipeline module (`python -m pipeline.ask --as-of <Monday> --json <question>`)
// and writes whatever that returns into the Monday's thread. Every answer the
// panel shows was produced by pipeline/ask.py, grounded in that run's
// artifacts, and passed through the harness there.
//
// Why a queue instead of an awaited request:
//   an answer takes 15-40s in claude-cli mode, so the POST returns at once
//   and the panel watches the thread. One question is answered at a time —
//   ten parallel claude processes would fight over the same subscription —
//   and questions asked while one is in flight wait their turn instead of
//   being dropped.
//
// Why asks wait for a pipeline run:
//   a run rewrites the artifacts an answer is grounded on. Answering mid-run
//   could mix last week's signals with this week's digest, so a question
//   asked during a run is held (visibly: the panel says so) until the run
//   lands, then answered against a consistent set of files.
//
// Failure is reported, never smoothed over: a python crash, unparseable
// output, or a timeout becomes a system note in the thread carrying the real
// error text. There is no path here that invents an answer.
//
// Streaming (`--stream`): pipeline.ask reports its own progress as JSONL —
// text deltas while the model writes, a `verifying` event when generation ends
// and the harness starts, `redo` when a reply is rejected, and one `final`
// event carrying the validated payload. This file forwards those events to
// subscribers (the SSE endpoint) and still writes ONLY the final payload into
// the thread. The rules that follow from that are not stylistic:
//   * streamed text is provisional — it is never persisted, never labelled
//     with a tier, and never credited with verified numbers;
//   * the thread entry is byte-for-byte what pipeline.ask decided, exactly as
//     in the non-streaming path;
//   * a run that emits no `final` event falls back to reading the last JSON
//     line of stdout, so a caller using plain `--json` still works.

const path = require("path");
const { spawn } = require("child_process");

// pipeline.ask's own claude timeout is 240s; give the child a little more so
// its honest "the model did not return in time" refusal wins the race.
const ASK_TIMEOUT_MS = 280_000;

class AskEngine {
  // busyState: () => null | "running" | "awaiting_decision"
  //   Non-null while a pipeline run owns the artifacts. The two reasons read
  //   very differently to the person waiting: a run in flight lands on its own,
  //   a run paused on the corrections card is waiting on *them*.
  constructor(repoRoot, pythonBin, threadStore, busyState) {
    this.repoRoot = repoRoot;
    this.pythonBin = pythonBin;
    this.threadStore = threadStore;
    this.busyState = busyState || (() => null);
    this.queue = [];
    this.current = null; // {asOf, question, startedAt, child, cancelled}
    this._retry = null;
    this._listeners = new Set(); // {asOf, fn} — live SSE subscribers
  }

  // --- live progress --------------------------------------------------------

  // Subscribe to this Monday's ask events. Returns an unsubscribe function.
  // Subscribers are pure observers: nothing here waits on them, and dropping
  // the last one changes nothing about the answer being produced.
  subscribe(asOf, fn) {
    const entry = { asOf, fn };
    this._listeners.add(entry);
    return () => this._listeners.delete(entry);
  }

  _emit(asOf, event) {
    const payload = { asOf, ...event };
    for (const l of this._listeners) {
      if (l.asOf && l.asOf !== asOf) continue;
      try {
        l.fn(payload);
      } catch {
        // a dead client must never break the answer in flight
      }
    }
  }

  // What a client joining mid-answer needs to catch up: the text so far and
  // what is happening to it. Provisional by construction — `state` says so,
  // and no tier or verified count appears here.
  streamState(asOf) {
    const c = this.current;
    if (!c || (asOf && c.asOf !== asOf)) return null;
    return {
      question: c.question,
      question_id: c.questionId,
      started_at: c.startedAt,
      // "working" until the model starts producing text, then "generating",
      // then "verifying" while the harness checks it.
      state: c.streamState,
      text: c.text,
      redos: c.redos,
      provisional: true,
    };
  }

  // Accept a question: persist it to the thread immediately (so it survives a
  // reload even if the answer never arrives), then queue the work.
  // `llmMode` (optional) is the tier the asker CHOSE — the panel's model
  // switcher. It is a request, not a promise: an unreachable tier still
  // degrades down the ladder, and the answer says which tier actually wrote
  // it. Omitted means "let the pipeline auto-detect", exactly as before.
  ask(asOf, question, by, llmMode) {
    const entry = this.threadStore.append(asOf, "user", "question", {
      text: question,
      ...(by ? { by } : {}),
      ...(llmMode ? { asked_of: llmMode } : {}),
    });
    this.queue.push({ asOf, question, questionId: entry.id, llmMode: llmMode || null });
    this._pump();
    return { entry, position: this.queue.length };
  }

  // What the panel renders as the in-flight bubble. null when idle.
  pending(asOf) {
    const mine = (job) => !asOf || job.asOf === asOf;
    const waiting = this.queue.filter(mine).length;
    // A job only becomes `current` once its child is spawned, and _pump never
    // spawns during a run — so anything current is genuinely being answered.
    if (this.current && mine(this.current)) {
      return {
        state: "answering",
        question: this.current.question,
        started_at: this.current.startedAt,
        queued: waiting,
      };
    }
    if (waiting > 0) {
      const next = this.queue.find(mine);
      const busy = this.busyState();
      return {
        state: busy === "awaiting_decision"
          ? "waiting_for_decision"
          : busy
            ? "waiting_for_run"
            : "queued",
        question: next.question,
        started_at: null,
        queued: waiting,
      };
    }
    return null;
  }

  // Stop the in-flight answer (and drop anything queued behind it for that
  // Monday). The question stays in the thread — it was really asked — and the
  // cancellation is recorded, so the log never implies an answer existed.
  cancel(asOf) {
    const mine = (job) => !asOf || job.asOf === asOf;
    const dropped = this.queue.filter(mine);
    this.queue = this.queue.filter((job) => !mine(job));
    const running = Boolean(this.current && mine(this.current));

    if (!running && dropped.length === 0) {
      return { error: "No question is being answered right now.", status: 409 };
    }
    if (running) {
      // The close handler turns this into the thread's cancellation note.
      this.current.cancelled = true;
      this.current.child.kill("SIGTERM");
    }
    for (const job of dropped) {
      this.threadStore.append(job.asOf, "system", "note", {
        text: `Stopped before "${job.question}" was answered — nothing was generated.`,
      });
    }
    return { ok: true, cancelled_running: running, dropped: dropped.length };
  }

  _finishCancelled(asOf) {
    const text = "Answer stopped before it finished — nothing was generated.";
    const entry = this.threadStore.append(asOf, "system", "note", { text });
    this.current = null;
    // Live clients drop the provisional text they were showing: it was never
    // an answer, and stopping does not turn it into one.
    this._emit(asOf, { type: "cancelled", entry, text });
    this._pump();
  }

  _pump() {
    if (this.current || this.queue.length === 0) return;

    // Hold every ask while the pipeline rewrites the artifacts an answer would
    // be grounded on. The job stays at the head of the queue, so pending()
    // reports it honestly as waiting rather than as being answered.
    if (this.busyState()) {
      clearTimeout(this._retry);
      this._retry = setTimeout(() => this._pump(), 1000);
      return;
    }

    const job = this.queue.shift();
    const startedAt = new Date().toISOString();
    const args = [
      "-u",
      "-m",
      "pipeline.ask",
      "--as-of",
      job.asOf,
      // --stream implies --json: the last line is still the whole result, so
      // this is the same contract as before plus progress along the way.
      "--stream",
      ...(job.llmMode ? ["--llm-mode", job.llmMode] : []),
      job.question,
    ];
    const child = spawn(this.pythonBin, args, {
      cwd: this.repoRoot,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      // Closed, not piped — ask.py pipes its own prompt to the claude CLI, and
      // an inherited empty pipe makes that CLI wait on input that never comes.
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.current = {
      ...job,
      startedAt,
      child,
      cancelled: false,
      // Live, provisional state — never persisted anywhere.
      streamState: "working",
      text: "",
      redos: 0,
      finalPayload: null,
    };
    this._emit(job.asOf, {
      type: "start",
      question: job.question,
      question_id: job.questionId,
      started_at: startedAt,
    });

    let stdout = "";
    let stderr = "";
    let lineBuf = "";
    child.stdout.on("data", (c) => {
      const chunk = c.toString("utf8");
      stdout += chunk;
      lineBuf += chunk;
      let nl;
      while ((nl = lineBuf.indexOf("\n")) >= 0) {
        const line = lineBuf.slice(0, nl).trim();
        lineBuf = lineBuf.slice(nl + 1);
        if (line) this._onStreamLine(job, line);
      }
    });
    child.stderr.on("data", (c) => {
      stderr += c.toString("utf8");
    });

    const killTimer = setTimeout(() => {
      if (this.current && this.current.child === child) {
        this.current.timedOut = true;
        child.kill("SIGTERM");
      }
    }, ASK_TIMEOUT_MS);

    // A failed spawn emits both 'error' and 'close': settle the job once, so
    // one failure never writes two notes into the thread.
    let settled = false;
    const settle = (fn) => {
      if (settled) return;
      settled = true;
      clearTimeout(killTimer);
      fn();
    };

    child.on("error", (err) => {
      settle(() =>
        this._note(
          job.asOf,
          `The answer could not be produced: ${path.basename(this.pythonBin)} ` +
            `failed to start (${err.message}). Nothing was generated.`
        )
      );
    });

    child.on("close", (code) => settle(() => this._onClose(job, code, stdout, stderr)));
  }

  // One JSONL line from pipeline.ask --stream. Anything that is not a known
  // event (a stray print, a warning) is ignored here and still reaches the
  // fallback parse at close, so no output is silently lost.
  _onStreamLine(job, line) {
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      return;
    }
    if (!event || typeof event.type !== "string") return;
    const c = this.current;
    const mine = c && c.child && !c.cancelled && c.questionId === job.questionId;

    switch (event.type) {
      case "generating":
        if (mine) c.streamState = "generating";
        this._emit(job.asOf, { type: "generating", attempt: event.attempt });
        break;
      case "delta":
        if (mine) {
          c.streamState = "generating";
          c.text += event.text || "";
        }
        this._emit(job.asOf, { type: "delta", text: event.text || "" });
        break;
      case "verifying":
        if (mine) c.streamState = "verifying";
        this._emit(job.asOf, { type: "verifying", attempt: event.attempt });
        break;
      case "redo":
        // The harness rejected that reply. The text shown so far was never an
        // answer, so it is dropped rather than patched.
        if (mine) {
          c.redos += 1;
          c.text = "";
          c.streamState = "working";
        }
        this._emit(job.asOf, {
          type: "redo",
          kind: event.kind,
          unknown: event.unknown || [],
          reason: event.reason || null,
        });
        break;
      case "tier_fallback":
        if (mine) {
          c.text = "";
          c.streamState = "working";
        }
        this._emit(job.asOf, {
          type: "tier_fallback",
          from: event.from,
          to: event.to,
          reason: event.reason,
        });
        break;
      case "final": {
        // Captured, not acted on: the job settles in _onClose, the single
        // place that also handles cancel, timeout and crash.
        const { type, ...payload } = event;
        if (mine) c.finalPayload = payload;
        break;
      }
      default:
        break;
    }
  }

  _onClose(job, code, stdout, stderr) {
    const state = this.current;
    if (state && state.cancelled) {
      this._finishCancelled(job.asOf);
      return;
    }
    if (state && state.timedOut) {
      this._note(
        job.asOf,
        `No answer within ${Math.round(ASK_TIMEOUT_MS / 1000)}s — the attempt was stopped ` +
          `rather than left hanging. Nothing was generated.`
      );
      return;
    }
    if (code !== 0) {
      this._note(
        job.asOf,
        `pipeline.ask exited ${code} without answering. ` +
          (stderr.trim().split("\n").slice(-3).join(" ").slice(0, 400) || "No error output.")
      );
      return;
    }

    let result = state && state.finalPayload;
    if (!result) {
      try {
        // No `final` event (a plain --json caller, or a build without
        // streaming): the whole result is still the last line of stdout.
        const line = stdout.trim().split("\n").filter(Boolean).pop();
        result = JSON.parse(line);
      } catch (err) {
        this._note(
          job.asOf,
          `pipeline.ask returned output this server could not read (${err.message}). ` +
            "Nothing was generated."
        );
        return;
      }
    }

    const entry = this.threadStore.append(job.asOf, "ai", "answer", {
      question: job.question,
      question_id: job.questionId,
      text: result.refused ? result.refusal_reason : result.answer,
      refused: result.refused,
      citations: result.citations || [],
      numbers_verified: result.numbers_verified || 0,
      // The receipt line under the answer: which files it read and which
      // checks it passed, exactly as pipeline.ask reported them.
      artifacts_read: result.artifacts_read || [],
      checks: result.checks || null,
      // What actually produced this: the tier that wrote it, or the
      // deterministic guard when the question never reached a model.
      mode: result.decided_by || result.mode,
      fallbacks: result.fallbacks || [],
      attempts: result.attempts,
      elapsed_seconds: result.elapsed_seconds,
    });
    this.current = null;
    // The validated entry, as persisted: what a live client swaps in for the
    // provisional text it has been showing.
    this._emit(job.asOf, { type: "final", entry });
    this._pump();
  }

  _note(asOf, text) {
    const entry = this.threadStore.append(asOf, "system", "note", { text });
    this.current = null;
    this._emit(asOf, { type: "note", entry, text });
    this._pump();
  }
}

module.exports = { AskEngine, ASK_TIMEOUT_MS };
