// The pipeline's four phases, in rail order, and the leader-facing name of
// each. This is the client's ONE source of truth for those names: the run
// page's stepper and the Agent panel's narration bubbles both read it, so a
// reviewer can point at a stepper row and at the chat bubble explaining it and
// see the same words.
//
// The server keeps its own copy (PHASE_TITLES in app/server/lib/run-manager.js)
// for the Engineer console and for the `title` it stamps on thread entries.
// The two are not byte-identical today — the server still says "Signal engine"
// and "Digest assembly" — so the panel deliberately names a phase from THIS map
// rather than from the server's `title`. The name a leader reads must match the
// rail in front of them; the server's string stays where it is (engineer-facing
// log lines) until that map is realigned, which is a server change.
export const PHASES = ["validation", "signals", "verdicts", "digest"];

export const PHASE_TITLE = {
  validation: "Data Validation & Check",
  signals: "Signal Engine",
  verdicts: "Claimed vs. Verified",
  digest: "Assemble Monday Digest",
};

// Rail position, 0-based. An unrecognised phase sorts last rather than first,
// so a name this client does not know can never jump the queue.
export function phaseIndex(phase) {
  const i = PHASES.indexOf(phase);
  return i === -1 ? PHASES.length : i;
}

// "Step 2 of 4 — Signal Engine". A phase the client does not recognise gets its
// server-supplied title with no step number, rather than a wrong one.
export function stepLabel(phase, fallbackTitle) {
  const title = PHASE_TITLE[phase] || fallbackTitle || "this phase";
  const i = PHASES.indexOf(phase);
  return i === -1 ? title : `Step ${i + 1} of ${PHASES.length} — ${title}`;
}

// ---------------------------------------------------------------------------
// What the panel shows while the run narrates itself
//
// One follower narrates the whole run in rail order (pipeline.narrate --follow),
// so exactly one phase is ever being written. The server's in-flight list is a
// QUEUE — it marks every finished phase the moment its artifact lands — and the
// two rules below turn that queue into what a reader should actually see. They
// live here, as pure functions of (queue, thread), because they are the part
// worth testing: app/server/test/panel-narration.test.js is their harness.
// ---------------------------------------------------------------------------

// The single narration to render, or null. Phases already narrated are dropped
// first: on a fast run the follower can finish a phase before the run manager
// marks it done, and that mark re-flags the finished phase as in-flight — take
// the head blindly and the panel pins itself to a bubble that will never type
// while the phase after it is the one actually being written.
export function pickLiveNarration(narrating, narratedPhases) {
  const done = narratedPhases || new Set();
  const queue = (narrating || []).filter((n) => n && n.phase && !done.has(n.phase));
  if (queue.length === 0) return null;
  return [...queue].sort((a, b) => phaseIndex(a.phase) - phaseIndex(b.phase))[0];
}

// Is this phase's deterministic placeholder hidden right now?
//
// Placeholders are the fallback if the agent never speaks — not a preview.
// Showing them while a narrative is being written (or after it has landed)
// makes a system line appear between steps and then vanish. Hide them whenever
// the agent is writing, or has already written that phase. If narration never
// arrives they stay: the honest fallback, and the only thing a reader sees.
//
// Both are rendering rules. Every line stays in DATA/OUTPUTS/<asOf>/thread.jsonl
// exactly as the run wrote it.
export function placeholderHidden(phase, { live, narratedPhases } = {}) {
  const done = narratedPhases || new Set();
  if (done.has(phase)) return true;
  return Boolean(live);
}
