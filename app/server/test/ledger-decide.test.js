// Acting on a tracked recommendation — the promises POST /api/ledger/decide
// makes, locked here:
//
//   the decision is recorded by the REAL pipeline module (this server never
//   writes the ledger itself) with the arguments the pipeline expects,
//   a dismissal without a reason is refused before anything is spawned,
//   a relaunch without a new date is refused the same way,
//   the decision lands in the Monday's thread as a decision entry, and
//   a pipeline refusal (unknown rec id, bad date) reaches the caller in the
//   pipeline's own words rather than as a generic failure.
//
// Run with: npm test   (node --test, no dependencies)

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { LedgerDecider } = require("../lib/ledger-decide");
const { ThreadStore } = require("../lib/thread");

const AS_OF = "2026-05-04";
const REC_ID = "REC-2026-04-27-mount-pleasant-staff_call_outs";

function tempRepo() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-decide-"));
}

// A stand-in for `python -m pipeline.ledger --decide … --json`: records the
// arguments it was called with and prints the row shape the real CLI prints.
function fakePython(root, { fail } = {}) {
  const bin = path.join(root, "fake-python");
  const body = fail
    ? `echo "no ledger row with rec_id 'REC-nope'" >&2\nexit 1\n`
    : `echo "$@" > "$PWD/decide-args"\n` +
      `printf '%s\\n' '{"ok":true,"row":{"rec_id":"${REC_ID}","location_name":"Mount Pleasant",` +
      `"metric":"staff_call_outs","status":"dismissed","check_by":"2026-05-11","decision":"dismiss"}}'\n`;
  fs.writeFileSync(bin, `#!/bin/sh\n${body}`, { mode: 0o755 });
  return bin;
}

test("a dismissal is recorded by the pipeline and posted to the thread", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const decider = new LedgerDecider(root, fakePython(root), threads);

  const result = await decider.decide({
    recId: REC_ID,
    action: "dismiss",
    note: "Two medical leaves — known cause",
    asOf: AS_OF,
    by: "Lucas",
  });

  assert.equal(result.ok, true);
  assert.equal(result.row.status, "dismissed");

  // The pipeline's own CLI did the writing, with the arguments it documents.
  const args = fs.readFileSync(path.join(root, "decide-args"), "utf8");
  assert.match(args, /-m pipeline\.ledger --decide REC-2026-04-27-mount-pleasant-staff_call_outs/);
  assert.match(args, /--action dismiss/);
  assert.match(args, /--by Lucas/);
  assert.match(args, /--json/);
  assert.match(args, /--note Two medical leaves/);

  // ...and the decision is in the Monday's thread, in plain English.
  const entries = threads.read(AS_OF);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].type, "decision");
  assert.equal(entries[0].role, "user");
  assert.equal(entries[0].payload.action, "dismiss");
  assert.equal(entries[0].payload.rec_id, REC_ID);
  assert.match(entries[0].payload.text, /^Lucas dismissed the Mount Pleasant staff call-out/);
  assert.match(entries[0].payload.text, /Two medical leaves/);
});

test("closing a row needs no reason, and says so in the thread", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const decider = new LedgerDecider(root, fakePython(root), threads);

  const result = await decider.decide({
    recId: REC_ID,
    action: "close",
    asOf: AS_OF,
    by: "Lucas",
  });
  assert.equal(result.ok, true);
  const args = fs.readFileSync(path.join(root, "decide-args"), "utf8");
  assert.match(args, /--action close/);
  assert.doesNotMatch(args, /--note/);
  assert.match(threads.read(AS_OF)[0].payload.text, /closed the Mount Pleasant staff call-out recommendation/);
});

test("relaunching carries the new check-by date through to the pipeline", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const decider = new LedgerDecider(root, fakePython(root), threads);

  const result = await decider.decide({
    recId: REC_ID,
    action: "relaunch",
    newCheckBy: "2026-05-25",
    asOf: AS_OF,
    by: "Lucas",
  });
  assert.equal(result.ok, true);
  const args = fs.readFileSync(path.join(root, "decide-args"), "utf8");
  assert.match(args, /--action relaunch/);
  assert.match(args, /--check-by 2026-05-25/);
  assert.match(threads.read(AS_OF)[0].payload.text, /new check-by date of 2026-05-25/);
});

test("a dismissal without a reason is refused before anything runs", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const decider = new LedgerDecider(root, fakePython(root), threads);

  const result = await decider.decide({
    recId: REC_ID,
    action: "dismiss",
    note: "   ",
    asOf: AS_OF,
  });
  assert.equal(result.status, 400);
  assert.match(result.error, /needs a short reason/);
  assert.equal(fs.existsSync(path.join(root, "decide-args")), false, "nothing was spawned");
  assert.equal(threads.read(AS_OF).length, 0, "nothing was written to the thread");
});

test("a relaunch without a new date is refused the same way", async () => {
  const root = tempRepo();
  const decider = new LedgerDecider(root, fakePython(root), new ThreadStore(root));
  const result = await decider.decide({ recId: REC_ID, action: "relaunch", asOf: AS_OF });
  assert.equal(result.status, 400);
  assert.match(result.error, /new check-by date/);
  assert.equal(fs.existsSync(path.join(root, "decide-args")), false);
});

test("an unknown action or rec id never reaches the pipeline", async () => {
  const root = tempRepo();
  const decider = new LedgerDecider(root, fakePython(root), new ThreadStore(root));

  const badAction = await decider.decide({ recId: REC_ID, action: "delete", asOf: AS_OF });
  assert.equal(badAction.status, 400);
  assert.match(badAction.error, /close/);

  const badId = await decider.decide({ recId: "../../etc/passwd", action: "escalate", asOf: AS_OF });
  assert.equal(badId.status, 400);
  assert.match(badId.error, /recId/);
  assert.equal(fs.existsSync(path.join(root, "decide-args")), false);
});

test("a pipeline refusal reaches the caller in the pipeline's own words", async () => {
  const root = tempRepo();
  const threads = new ThreadStore(root);
  const decider = new LedgerDecider(root, fakePython(root, { fail: true }), threads);

  const result = await decider.decide({ recId: "REC-nope", action: "escalate", asOf: AS_OF });
  assert.equal(result.status, 400);
  assert.match(result.error, /no ledger row with rec_id/);
  assert.equal(threads.read(AS_OF).length, 0, "a refused decision is never narrated as done");
});
