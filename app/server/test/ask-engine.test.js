// The ask engine's contract, tested against a stand-in "python" so the paths
// that matter are exercised without a model or a real run:
//
//   1. A question is persisted before any work starts (it survives a reload).
//   2. Whatever pipeline.ask returns is written through verbatim — the server
//      never composes an answer of its own.
//   3. Every failure (crash, garbage output, timeout, cancel) becomes a system
//      note that says nothing was generated. No path invents an answer.
//   4. One question at a time; the rest queue.
//   5. Asks are held while a run owns the artifacts, and the wait says which
//      kind of wait it is.
//
// Run with: npm test   (node --test, no dependencies)

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { AskEngine } = require("../lib/ask-engine");

const AS_OF = "2026-05-04";

// A thread that keeps its entries in memory — the real ThreadStore's contract
// (append returns the entry with an id) without touching DATA/OUTPUTS/.
function fakeThread() {
  const entries = [];
  return {
    entries,
    append(asOf, role, type, payload) {
      const entry = { id: `${asOf}-${entries.length}`, asOf, role, type, payload };
      entries.push(entry);
      return entry;
    },
    last() {
      return entries[entries.length - 1];
    },
  };
}

// A fake interpreter that replays pipeline.ask --stream: one JSON event per
// line, paced so a test can observe the middle of an answer, ending with the
// `final` event that carries the validated payload.
function streamPython(events, pauseSeconds = 0.15) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "askpy-"));
  const file = path.join(dir, "python");
  const body = events
    .map((e) => `printf '%s\\n' '${JSON.stringify(e)}'\nsleep ${pauseSeconds}`)
    .join("\n");
  fs.writeFileSync(file, `#!/bin/sh\n${body}\nexit 0\n`, { mode: 0o755 });
  return file;
}

// A fake interpreter: ignores the module args and behaves as told.
//   mode "answer"  prints a preamble line then the JSON object, exits 0
//   mode "garbage" prints non-JSON, exits 0
//   mode "crash"   writes to stderr, exits 3
//   mode "hang"    sleeps until killed
function fakePython(mode, payload) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "askpy-"));
  const file = path.join(dir, "python");
  const body = {
    answer: `echo "loading artifacts…"\ncat <<'JSON'\n${JSON.stringify(payload)}\nJSON\nexit 0`,
    garbage: 'echo "Traceback (most recent call last):"\nexit 0',
    crash: 'echo "boom: pandas is not installed" >&2\nexit 3',
    hang: "sleep 60",
  }[mode];
  fs.writeFileSync(file, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return file;
}

function engine(pythonBin, busyState) {
  const thread = fakeThread();
  return { thread, eng: new AskEngine(process.cwd(), pythonBin, thread, busyState) };
}

// Wait for a predicate, so tests never sleep longer than they must.
async function until(fn, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error("condition not met in time");
}

test("the question is in the thread before any answer exists", () => {
  const { thread, eng } = engine(fakePython("hang"));
  eng.ask(AS_OF, "why did Mount Pleasant rank first?");
  const first = thread.entries[0];
  assert.equal(first.type, "question");
  assert.equal(first.payload.text, "why did Mount Pleasant rank first?");
  eng.cancel(AS_OF);
});

test("pipeline.ask's answer is written through verbatim, crediting decided_by", async () => {
  const payload = {
    answer: "Mount Pleasant ranks #1 of 3 on staff call-outs.",
    refused: false,
    citations: [{ artifact: "signals.json", detail: "rank 1 — priority 5.28" }],
    numbers_verified: 7,
    mode: "claude-cli",
    decided_by: "template",
    fallbacks: [{ from: "claude-cli", to: "template", reason: "usage limit" }],
    attempts: 2,
    elapsed_seconds: 0.5,
  };
  const { thread, eng } = engine(fakePython("answer", payload));
  eng.ask(AS_OF, "why did Mount Pleasant rank first?");
  await until(() => thread.entries.some((e) => e.type === "answer"));

  const answer = thread.entries.find((e) => e.type === "answer").payload;
  assert.equal(answer.text, payload.answer);
  assert.equal(answer.refused, false);
  assert.equal(answer.numbers_verified, 7);
  assert.deepEqual(answer.citations, payload.citations);
  // The tier that actually produced it, not the one the run asked for.
  assert.equal(answer.mode, "template");
  assert.deepEqual(answer.fallbacks, payload.fallbacks);
  assert.equal(answer.question_id, thread.entries[0].id);
  assert.equal(eng.pending(AS_OF), null);
});

test("a refusal is stored as the refusal text, still marked refused", async () => {
  const { thread, eng } = engine(
    fakePython("answer", {
      answer: null,
      refused: true,
      refusal_reason: "That is a staffing decision, which this system does not make.",
      citations: [],
      numbers_verified: 0,
      mode: "claude-cli",
      decided_by: "guard",
      attempts: 0,
    })
  );
  eng.ask(AS_OF, "should I fire someone?");
  await until(() => thread.entries.some((e) => e.type === "answer"));

  const answer = thread.entries.find((e) => e.type === "answer").payload;
  assert.equal(answer.refused, true);
  assert.match(answer.text, /staffing decision/);
  assert.equal(answer.mode, "guard");
});

test("a crash becomes one note carrying the real error, never an answer", async () => {
  const { thread, eng } = engine(fakePython("crash"));
  eng.ask(AS_OF, "what was suppressed?");
  await until(() => thread.entries.some((e) => e.type === "note"));

  assert.equal(thread.entries.filter((e) => e.type === "answer").length, 0);
  const notes = thread.entries.filter((e) => e.type === "note");
  assert.equal(notes.length, 1);
  assert.match(notes[0].payload.text, /exited 3/);
  assert.match(notes[0].payload.text, /pandas is not installed/);
});

test("unreadable output becomes a note, never a guessed answer", async () => {
  const { thread, eng } = engine(fakePython("garbage"));
  eng.ask(AS_OF, "what was suppressed?");
  await until(() => thread.entries.some((e) => e.type === "note"));

  assert.equal(thread.entries.filter((e) => e.type === "answer").length, 0);
  assert.match(thread.entries.at(-1).payload.text, /could not read/);
});

test("a python that never starts is reported once, not twice", async () => {
  const { thread, eng } = engine("/nonexistent/python");
  eng.ask(AS_OF, "what was suppressed?");
  await until(() => thread.entries.some((e) => e.type === "note"));
  // 'error' and 'close' both fire for a failed spawn; the job settles once.
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(thread.entries.filter((e) => e.type === "note").length, 1);
  assert.match(thread.entries.at(-1).payload.text, /failed to start/);
});

test("cancelling an in-flight answer records it and generates nothing", async () => {
  const { thread, eng } = engine(fakePython("hang"));
  eng.ask(AS_OF, "why did Mount Pleasant rank first?");
  await until(() => eng.pending(AS_OF) && eng.pending(AS_OF).state === "answering");

  const result = eng.cancel(AS_OF);
  assert.equal(result.cancelled_running, true);
  await until(() => thread.entries.some((e) => e.type === "note"));

  assert.equal(thread.entries.filter((e) => e.type === "answer").length, 0);
  assert.match(thread.entries.at(-1).payload.text, /nothing was generated/i);
  // The question itself stays: it really was asked.
  assert.equal(thread.entries[0].type, "question");
  assert.equal(eng.pending(AS_OF), null);
});

test("cancelling with nothing in flight is an error, not a silent no-op", () => {
  const { eng } = engine(fakePython("hang"));
  const result = eng.cancel(AS_OF);
  assert.equal(result.status, 409);
});

test("one question at a time; the rest queue and are answered in order", async () => {
  const payload = {
    answer: "answered.",
    refused: false,
    citations: [],
    numbers_verified: 1,
    mode: "template",
    attempts: 1,
  };
  const { thread, eng } = engine(fakePython("answer", payload));
  eng.ask(AS_OF, "first question");
  const second = eng.ask(AS_OF, "second question");
  assert.equal(second.position >= 1, true);

  await until(() => thread.entries.filter((e) => e.type === "answer").length === 2);
  const answered = thread.entries
    .filter((e) => e.type === "answer")
    .map((e) => e.payload.question);
  assert.deepEqual(answered, ["first question", "second question"]);
});

test("asks are held while a run owns the artifacts, and say which wait it is", async () => {
  let busy = "running";
  const payload = {
    answer: "answered after the run.",
    refused: false,
    citations: [],
    numbers_verified: 1,
    mode: "template",
    attempts: 1,
  };
  const { thread, eng } = engine(fakePython("answer", payload), () => busy);

  eng.ask(AS_OF, "what changed since last Monday?");
  assert.equal(eng.pending(AS_OF).state, "waiting_for_run");

  busy = "awaiting_decision";
  assert.equal(eng.pending(AS_OF).state, "waiting_for_decision");
  // Nothing ran while the run held the artifacts.
  assert.equal(thread.entries.filter((e) => e.type === "answer").length, 0);

  busy = null;
  await until(() => thread.entries.some((e) => e.type === "answer"));
  assert.match(thread.entries.at(-1).payload.text, /answered after the run/);
});

// ---------------------------------------------------------------------------
// Streaming — the answer as it is written
//
// The rule under test is not "text appears sooner". It is that streamed text
// stays provisional: nothing reaches the thread except the final, validated
// payload, and a draft the harness rejects is dropped rather than shown as an
// answer.
// ---------------------------------------------------------------------------

const FINAL = {
  type: "final",
  answer: "4 signals were held back this Monday.",
  refused: false,
  citations: [{ artifact: "signals.json", detail: "suppressed list, 4 entries" }],
  numbers_verified: 4,
  mode: "claude-cli",
  decided_by: "claude-cli",
  fallbacks: [],
  attempts: 1,
  elapsed_seconds: 1.2,
};

test("deltas reach subscribers while the thread stays empty until the end", async () => {
  const { thread, eng } = engine(
    streamPython([
      { type: "generating", mode: "claude-cli", attempt: 1 },
      { type: "delta", text: "ANSWER: 4 signals " },
      { type: "delta", text: "were held back this Monday." },
      { type: "verifying", mode: "claude-cli", attempt: 1 },
      FINAL,
    ])
  );
  const seen = [];
  eng.subscribe(AS_OF, (e) => seen.push(e));
  eng.ask(AS_OF, "what was suppressed?");

  await until(() => seen.filter((e) => e.type === "delta").length === 2);
  // Text is on its way to the browser, and nothing has been written down.
  assert.equal(thread.entries.filter((e) => e.type === "answer").length, 0);
  const live = eng.streamState(AS_OF);
  assert.equal(live.text, "ANSWER: 4 signals were held back this Monday.");
  assert.equal(live.provisional, true);

  await until(() => seen.some((e) => e.type === "final"));
  const answer = thread.entries.find((e) => e.type === "answer").payload;
  assert.equal(answer.text, FINAL.answer);
  assert.equal(answer.numbers_verified, 4);
  assert.deepEqual(answer.citations, FINAL.citations);
  // The final event hands the client the entry that was actually persisted.
  assert.deepEqual(seen.at(-1).entry.payload, answer);
  assert.equal(eng.streamState(AS_OF), null);
});

test("a rejected draft is dropped, not patched, and the redo is announced", async () => {
  const { thread, eng } = engine(
    streamPython([
      { type: "generating", mode: "claude-cli", attempt: 1 },
      { type: "delta", text: "ANSWER: call-outs hit 9137.42" },
      { type: "verifying", mode: "claude-cli", attempt: 1 },
      { type: "redo", attempt: 1, kind: "unverified_number", unknown: ["9137.42"] },
      { type: "generating", mode: "claude-cli", attempt: 2 },
      { type: "delta", text: "ANSWER: 4 signals were held back this Monday." },
      FINAL,
    ])
  );
  const seen = [];
  eng.subscribe(AS_OF, (e) => seen.push(e));
  eng.ask(AS_OF, "what was suppressed?");

  await until(() => seen.some((e) => e.type === "redo"));
  const redo = seen.find((e) => e.type === "redo");
  assert.deepEqual(redo.unknown, ["9137.42"]);
  // The unverified figure is gone from the live text the moment it is rejected.
  assert.equal(eng.streamState(AS_OF).text, "");
  assert.equal(eng.streamState(AS_OF).redos, 1);

  await until(() => thread.entries.some((e) => e.type === "answer"));
  const answer = thread.entries.find((e) => e.type === "answer").payload;
  assert.equal(answer.text.includes("9137.42"), false);
});

test("streamed text is never written to the thread when the answer is stopped", async () => {
  const { thread, eng } = engine(
    streamPython(
      [
        { type: "generating", mode: "claude-cli", attempt: 1 },
        { type: "delta", text: "ANSWER: half a sentence" },
        FINAL,
      ],
      2 // long enough that the stop lands mid-answer
    )
  );
  const seen = [];
  eng.subscribe(AS_OF, (e) => seen.push(e));
  eng.ask(AS_OF, "what was suppressed?");
  await until(() => seen.some((e) => e.type === "delta"));

  eng.cancel(AS_OF);
  await until(() => seen.some((e) => e.type === "cancelled"));
  assert.equal(thread.entries.filter((e) => e.type === "answer").length, 0);
  assert.match(thread.entries.at(-1).payload.text, /nothing was generated/i);
});

test("the chosen model is passed to the pipeline, and only when one is chosen", async () => {
  // The panel's switcher picks a tier; the server hands it to pipeline.ask as
  // --llm-mode and does nothing else with it. Which tier actually wrote the
  // answer is still whatever the pipeline reports back.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "askpy-"));
  const argvFile = path.join(dir, "argv.txt");
  const file = path.join(dir, "python");
  fs.writeFileSync(
    file,
    `#!/bin/sh\necho "$@" >> ${argvFile}\nprintf '%s\\n' '${JSON.stringify(FINAL)}'\nexit 0\n`,
    { mode: 0o755 }
  );

  const { thread, eng } = engine(file);
  eng.ask(AS_OF, "what was suppressed?", null, "openai");
  await until(() => thread.entries.some((e) => e.type === "answer"));
  assert.match(fs.readFileSync(argvFile, "utf8"), /--llm-mode openai/);
  // …and it is recorded on the question itself: who was asked, not just who answered.
  assert.equal(thread.entries[0].payload.asked_of, "openai");

  eng.ask(AS_OF, "and what changed?");
  await until(() => thread.entries.filter((e) => e.type === "answer").length === 2);
  const runs = fs.readFileSync(argvFile, "utf8").trim().split("\n");
  assert.equal(runs[1].includes("--llm-mode"), false);
});

test("a subscriber that throws cannot break the answer in flight", async () => {
  const { thread, eng } = engine(
    streamPython([{ type: "delta", text: "ANSWER: ok" }, FINAL], 0.05)
  );
  eng.subscribe(AS_OF, () => {
    throw new Error("this client is broken");
  });
  eng.ask(AS_OF, "what was suppressed?");
  await until(() => thread.entries.some((e) => e.type === "answer"));
  assert.equal(thread.entries.find((e) => e.type === "answer").payload.text, FINAL.answer);
});

test("pending is scoped to the Monday being viewed", async () => {
  const { eng } = engine(fakePython("hang"));
  eng.ask(AS_OF, "a question about this Monday");
  await until(() => eng.pending(AS_OF));
  assert.equal(eng.pending("2026-04-27"), null);
  eng.cancel(AS_OF);
});
