// What the Agent panel shows while the run narrates itself.
//
// The run has ONE narrator (`pipeline.narrate --follow`) walking the rail in
// order, so exactly one phase is ever being written. The run manager, though,
// marks every finished phase in-flight the moment its artifact lands: its
// `narrating()` list is that narrator's QUEUE. Rendering the whole queue is
// what put two "AI · writing" bubbles on screen at once, one of them typing
// nothing.
//
// These are the panel's two rules for turning that queue into what a reader
// sees, tested as the pure functions they are (app/web/src/phases.js):
//   pickLiveNarration  — the one narration to render, or nothing
//   placeholderHidden  — whether a deterministic phase summary is on screen
//
// The client module is ESM and this package is CommonJS, so it is imported
// through a data: URL — the real source, byte for byte, with no build step.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const WEB_SRC = path.join(__dirname, "..", "..", "web", "src");
const PHASES_SRC = path.join(WEB_SRC, "phases.js");

let phases;
test.before(async () => {
  const src = fs.readFileSync(PHASES_SRC, "utf8");
  phases = await import(`data:text/javascript,${encodeURIComponent(src)}`);
});

const inFlight = (phase, extra = {}) => ({ phase, stream_state: "working", ...extra });

// ---------------------------------------------------------------------------
// One bubble, and it is the right one
// ---------------------------------------------------------------------------

test("two phases in flight render one narration — the earlier one in rail order", () => {
  const { pickLiveNarration } = phases;
  // Exactly what the server reports when validation and signals both complete
  // in the same tick: the follower is on validation, signals is queued.
  const live = pickLiveNarration(
    [inFlight("validation", { stream_state: "generating" }), inFlight("signals")],
    new Set()
  );
  assert.equal(live.phase, "validation");
});

test("queue order is rail order, not the order the server happened to list them", () => {
  const { pickLiveNarration } = phases;
  assert.equal(
    pickLiveNarration([inFlight("digest"), inFlight("verdicts")], new Set()).phase,
    "verdicts"
  );
  assert.equal(
    pickLiveNarration([inFlight("signals"), inFlight("validation")], new Set()).phase,
    "validation"
  );
});

test("nothing in flight renders nothing", () => {
  assert.equal(phases.pickLiveNarration([], new Set()), null);
  assert.equal(phases.pickLiveNarration(undefined, undefined), null);
});

test("a phase whose narrative already landed is not in flight, whatever the server says", () => {
  const { pickLiveNarration } = phases;
  // The fast-run race: the follower narrates verdicts before the run manager
  // marks that phase done, and the mark re-flags the finished phase as
  // generating. Taking the head blindly would pin the panel to a verdicts
  // bubble that will never type while the digest is the one being written.
  const live = pickLiveNarration(
    [inFlight("verdicts"), inFlight("digest", { stream_state: "generating" })],
    new Set(["validation", "signals", "verdicts"])
  );
  assert.equal(live.phase, "digest");
});

test("a queue of only already-narrated phases renders nothing", () => {
  assert.equal(
    phases.pickLiveNarration([inFlight("verdicts")], new Set(["verdicts"])),
    null
  );
});

test("durable thread entries never replay as concurrent typewriter streams", () => {
  const src = fs.readFileSync(path.join(WEB_SRC, "components", "SidePanel.jsx"), "utf8");
  const narrative = src.slice(
    src.indexOf("function NarrativeBubble"),
    src.indexOf("// One thread entry")
  );
  const entry = src.slice(
    src.indexOf("function Entry"),
    src.indexOf("// The answer as it is being written")
  );

  assert.doesNotMatch(narrative, /useTypewriter|stream-cursor|animate/);
  assert.doesNotMatch(entry, /animate=/);
  assert.match(src, /function StreamingNarrative[\s\S]*useTypewriter/);
});

test("an unknown phase sorts last — a name this build does not know never jumps the queue", () => {
  const { pickLiveNarration } = phases;
  assert.equal(
    pickLiveNarration([inFlight("cleanup"), inFlight("digest")], new Set()).phase,
    "digest"
  );
});

// ---------------------------------------------------------------------------
// The closing lines wait for the narrative they stand in for
// ---------------------------------------------------------------------------

test("the digest's closing lines are held while a narration is still being written", () => {
  const { placeholderHidden } = phases;
  const live = { phase: "verdicts" };
  assert.equal(placeholderHidden("digest", { live, narratedPhases: new Set() }), true);
});

test("no placeholder flashes while a narration is being written", () => {
  const { placeholderHidden } = phases;
  const live = { phase: "signals" };
  assert.equal(placeholderHidden("validation", { live, narratedPhases: new Set() }), true);
  assert.equal(placeholderHidden("signals", { live, narratedPhases: new Set() }), true);
  assert.equal(placeholderHidden("verdicts", { live, narratedPhases: new Set() }), true);
});

test("narration failed: the closing lines appear — the honest fallback stands", () => {
  const { placeholderHidden } = phases;
  // Nothing in flight and no digest narrative: the deterministic summary is all
  // there is, and hiding it would leave the run with no ending at all.
  assert.equal(placeholderHidden("digest", { live: null, narratedPhases: new Set() }), false);
});

test("a landed narrative supersedes its placeholders whichever order they arrived in", () => {
  const { placeholderHidden } = phases;
  // The run-log tail can post a phase's summary a beat AFTER that phase was
  // narrated; the server only supersedes placeholders that precede it.
  assert.equal(
    placeholderHidden("verdicts", { live: null, narratedPhases: new Set(["verdicts"]) }),
    true
  );
  assert.equal(
    placeholderHidden("digest", { live: null, narratedPhases: new Set(["digest"]) }),
    true
  );
});

// ---------------------------------------------------------------------------
// Step labels — one name for the stepper row and the bubble explaining it
// ---------------------------------------------------------------------------

test("every phase is labelled by its position in the rail", () => {
  const { PHASES, stepLabel } = phases;
  assert.deepEqual(
    PHASES.map((p) => stepLabel(p)),
    [
      "Step 1 of 4 — Data Validation & Check",
      "Step 2 of 4 — Signal Engine",
      "Step 3 of 4 — Claimed vs. Verified",
      "Step 4 of 4 — Assemble Monday Digest",
    ]
  );
});

test("an unknown phase gets the server's title and no invented step number", () => {
  const { stepLabel } = phases;
  assert.equal(stepLabel("cleanup", "Tidying up"), "Tidying up");
  assert.equal(stepLabel(undefined), "this phase");
});

test("the stepper and the panel read the same map — there is no second copy", () => {
  // Anti-drift: the run page's rail and the panel's bubbles both import
  // ../phases.js. A phase title hardcoded back into either file is the exact
  // regression that let the bubble say "Signal engine" while the row it
  // explained said "Signal Engine".
  const { PHASES, PHASE_TITLE } = phases;
  for (const file of ["views/AdminView.jsx", "components/SidePanel.jsx"]) {
    const src = fs.readFileSync(path.join(WEB_SRC, file), "utf8");
    assert.match(src, /from "\.\.\/phases\.js"/, `${file} must read the shared phase map`);
    for (const phase of PHASES) {
      assert.equal(
        src.includes(`"${PHASE_TITLE[phase]}"`),
        false,
        `${file} hardcodes the title for ${phase} instead of importing it`
      );
    }
  }
});
