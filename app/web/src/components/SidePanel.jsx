import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJson, postJson, subscribeEvents } from "../api.js";
import {
  PHASES,
  PHASE_TITLE,
  phaseIndex,
  stepLabel,
  pickLiveNarration,
  placeholderHidden,
} from "../phases.js";

// The conversation surface — the right-hand panel next to the work surface.
//
// It renders EXCLUSIVELY from GET /api/thread (the server-owned, per-Monday
// thread persisted in DATA/OUTPUTS/<asOf>/thread.jsonl): no entry here is invented
// by the front-end, and the in-flight "answering…" state is the server's
// `pending`, not a local guess — so a reload, or a second browser, sees the
// same truth mid-answer.
//
// Two kinds of exchange live in one thread, on purpose:
//   the run narrates itself (phase summaries, the corrections decision the
//   leader accepts or declines right here, like accepting a diff), and
//   the leader asks questions about it ("Ask this Monday"), answered by
//   pipeline/ask.py from that run's artifacts with its receipts attached.
//
// Every answer shows how it was made: which tier wrote it, how many numbers
// the harness verified, and which artifacts ground it. A refusal is shown as a
// refusal — never dressed up as an answer.

const MODE_LABEL = {
  "claude-cli": "Claude",
  api: "Claude API",
  // A different vendor entirely — it never borrows Claude's name.
  openai: "OpenAI",
  template: "deterministic tier",
  guard: "refused before any model ran",
};

// The switcher's labels: short enough for a chip, specific enough that nobody
// has to guess whose model answered.
const MODE_PICKER_LABEL = {
  "claude-cli": "Claude",
  api: "Claude API",
  openai: "OpenAI",
  template: "Deterministic",
};

// The leader-facing name of a phase, from the client's own rail map (see
// ../phases.js) — the same strings the stepper prints. The server also stamps a
// `title` on the entry, but naming the phase from the rail is what guarantees
// the bubble and the stepper row a reviewer is comparing them against never
// disagree. The server's title is only a fallback for a phase this build has
// never heard of.
function phaseTitle(narration) {
  return PHASE_TITLE[narration.phase] || narration.title || "this phase";
}

// Update one phase's live narration in place, whether the change is a plain
// patch or derived from what is already there. A phase the poll has not
// reported yet is added, so a delta is never dropped on the floor.
function patchPhase(list, phase, change) {
  const current = list.find((n) => n.phase === phase) || { phase, provisional: true };
  const patch = typeof change === "function" ? change(current) : change;
  const next = { ...current, ...patch };
  return list.some((n) => n.phase === phase)
    ? list.map((n) => (n.phase === phase ? next : n))
    : [...list, next];
}

const MODE_PICKER_TITLE = {
  "claude-cli": "Claude Opus, through the Claude Code CLI on this machine.",
  api: "Claude, through the Anthropic API.",
  openai: "OpenAI's model, through the OpenAI API — a different vendor, the same prompt and the same harness.",
  template:
    "No model at all: deterministic answers assembled from this run's artifacts. Instant, free, and limited to a fixed set of questions.",
};

const MODE_KEY = "petfolk.askMode";

const PENDING_LABEL = {
  answering: "Reading this run's artifacts…",
  queued: "Queued — answering the question before it first…",
  waiting_for_run: "Held until the run finishes — answers wait for settled artifacts…",
  waiting_for_decision: "Held until you decide the corrections above…",
};

function modeLabel(mode) {
  return MODE_LABEL[mode] || mode;
}

// ---------------------------------------------------------------------------
// Bubble text: one lead line, then bullets, with the receipted tokens marked
// ---------------------------------------------------------------------------
//
// pipeline.narrate writes a lead line followed by "• " bullets, and hands over
// a receipt for every number, date and center name in it: {token, path, field,
// value}. Nothing here parses meaning out of the text — it only lays out the
// lines the model wrote and underlines the tokens the harness already tied to
// an artifact. A draft (still streaming, nothing checked) renders the same
// lines with no underlines at all: an unverified number must never look like a
// verified one.

function baseName(p) {
  return String(p || "").split("/").pop();
}

function escapeRe(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// One regex for every receipted token, longest first so "77.7" wins over "7".
// The boundaries keep "7.2" from matching inside "77.25".
function receiptMatcher(receipts) {
  const tokens = [...new Set((receipts || []).map((r) => r.token).filter(Boolean))];
  if (tokens.length === 0) return null;
  tokens.sort((a, b) => b.length - a.length);
  return new RegExp(`(?<![\\w.])(${tokens.map(escapeRe).join("|")})(?![\\w.])`, "g");
}

function receiptTitle(r) {
  return `${r.path} · ${r.field} = ${r.value}`;
}

const CENTER_FIELDS = new Set(["center", "location_name", "center_name", "centers"]);

function receiptFieldLeaf(field) {
  return String(field || "").split(".").pop().replace(/\[\d+\]/g, "");
}

function isCenterReceipt(r) {
  if (!r) return false;
  if (CENTER_FIELDS.has(receiptFieldLeaf(r.field))) return true;
  const token = String(r.token || "");
  return /^[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+)*$/.test(token);
}

// Verdicts used to pack several centers onto one line, separated by " · ".
// Split those so each center is its own bullet; keep the bucket prefix on each.
function expandPackedBullet(text) {
  const parts = String(text)
    .split(/\s*·\s*/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length < 2) return [text];
  const head = parts[0];
  const colon = head.indexOf(": ");
  if (colon === -1) return parts;
  const prefix = head.slice(0, colon + 2);
  return [head, ...parts.slice(1).map((part) => prefix + part)];
}

function ReceiptedLine({ text, byToken, matcher, onOpen }) {
  if (!matcher) return text;
  const out = [];
  let last = 0;
  matcher.lastIndex = 0;
  let m;
  while ((m = matcher.exec(text)) !== null) {
    const receipt = byToken.get(m[1]);
    if (!receipt) continue;
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(
      <button
        type="button"
        key={`${m.index}-${m[1]}`}
        className={`receipt-token${isCenterReceipt(receipt) ? " center" : ""}`}
        title={receiptTitle(receipt)}
        onClick={onOpen}
      >
        {m[1]}
      </button>
    );
    last = m.index + m[1].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function BubbleText({ text, receipts, onOpenReceipts }) {
  const matcher = useMemo(() => receiptMatcher(receipts), [receipts]);
  const byToken = useMemo(
    () => new Map((receipts || []).map((r) => [r.token, r])),
    [receipts]
  );
  const lines = String(text || "").split("\n").filter((l) => l.trim());
  const render = (line) => (
    <ReceiptedLine text={line} byToken={byToken} matcher={matcher} onOpen={onOpenReceipts} />
  );

  const out = [];
  let bullets = [];
  const flush = (key) => {
    if (bullets.length === 0) return;
    out.push(
      <ul className="bubble-bullets" key={`ul-${key}`}>
        {bullets.flatMap((b) => expandPackedBullet(b)).map((b, i) => (
          <li key={i}>{render(b)}</li>
        ))}
      </ul>
    );
    bullets = [];
  };
  lines.forEach((line, i) => {
    const trimmed = line.trim();
    if (trimmed.startsWith("•")) {
      bullets.push(trimmed.replace(/^•\s*/, ""));
      return;
    }
    flush(i);
    out.push(
      <p className="bubble-lead" key={`p-${i}`}>
        {render(trimmed)}
      </p>
    );
  });
  flush("end");
  return <>{out}</>;
}

// The line under every narrative and every answer: what it read, how much of
// it was verified, how many attempts it took, and which tier wrote it. Every
// value comes from the payload — nothing here is inferred, and the tier is
// whatever actually produced the text.
function CheckLine({ artifactsRead = [], verified, attempts, tier, receipts = [], open, setOpen }) {
  const files = [...new Set(artifactsRead.map(baseName))];
  const shown = files.slice(0, 3);
  const extra = files.length - shown.length;
  const hasReceipts = receipts.length > 0;
  return (
    <div className="check-line">
      <button
        type="button"
        className={`check-line-btn ${open ? "open" : ""}`}
        onClick={() => setOpen(!open)}
        disabled={!hasReceipts}
        title={
          hasReceipts
            ? "Every number and name below, with the file and field it came from."
            : "No receipts were recorded for this one."
        }
      >
        {files.length > 0 && (
          <span className="check-read">
            read: {shown.join(" · ")}
            {extra > 0 ? ` +${extra}` : ""}
          </span>
        )}
        {verified > 0 && (
          <span className="check-verified">
            → {verified}/{verified} verified
          </span>
        )}
        {attempts != null && <span className="check-attempt">attempt {attempts}</span>}
        {tier && (
          <span
            className={`check-tier ${tier === "openai" ? "vendor" : ""}`}
            title={
              tier === "openai"
                ? "Written by OpenAI's model, not by Claude. Same prompt, same harness — every number was still checked against this run's artifacts."
                : "Which tier actually wrote this text."
            }
          >
            {modeLabel(tier)}
          </span>
        )}
        {hasReceipts && <span className="check-caret" aria-hidden="true">{open ? "⌃" : "⌄"}</span>}
      </button>
      {open && hasReceipts && (
        <ul className="receipt-list">
          {receipts.map((r, i) => (
            <li key={i}>
              <span className="receipt-tok">{r.token}</span>
              <code>{r.path}</code>
              <span className="receipt-field">{r.field}</span>
              <span className="receipt-val">= {String(r.value)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The corrections decision — accepted or declined right in the thread
// ---------------------------------------------------------------------------

// One proposed correction: an approval to give or withhold, reviewed like a
// diff. Undecided until the reviewer says so — there is no default answer, and
// a decision resolves into a plain status rather than staying a live control.
function CorrectionRow({ correction: c, decision, onDecide, onReopen }) {
  const decided = decision === "accepted" || decision === "declined";
  const changeRef = useRef(null);
  const acceptRef = useRef(null);
  // Deciding swaps the buttons out for a status, which would drop keyboard
  // focus to the top of the document mid-review. Keep focus on this row.
  const followFocus = useRef(false);

  useEffect(() => {
    if (!followFocus.current) return;
    followFocus.current = false;
    const next = decided ? changeRef.current : acceptRef.current;
    if (next) next.focus();
  }, [decided]);

  return (
    <div className={`thread-corr ${decision || "undecided"}`}>
      <div className="thread-corr-head">
        <span className="correction-id">{c.id}</span>
        <span className="correction-rows">{c.affected_rows} rows</span>
        {decided ? (
          <div className="corr-resolved">
            <span className={`corr-status ${decision}`}>
              {decision === "accepted" ? "✓ Accepted" : "× Declined"}
            </span>
            <button
              type="button"
              className="corr-change"
              ref={changeRef}
              onClick={() => {
                followFocus.current = true;
                onReopen();
              }}
              aria-label={`Change the decision on ${c.id} — currently ${decision}`}
            >
              Change
            </button>
          </div>
        ) : (
          <div className="corr-decide" role="group" aria-label={`${c.id} decision`}>
            <button
              type="button"
              className="corr-accept"
              ref={acceptRef}
              onClick={() => {
                followFocus.current = true;
                onDecide("accepted");
              }}
            >
              Accept
            </button>
            <button
              type="button"
              className="corr-decline"
              onClick={() => {
                followFocus.current = true;
                onDecide("declined");
              }}
            >
              Decline
            </button>
          </div>
        )}
      </div>
      <div className="thread-corr-desc">{c.description}</div>
      {c.kind === "relabel_maturity_tier" && (c.details || []).length > 0 && (
        <details className="thread-corr-details">
          <summary>Per-center relabels ({c.details.length})</summary>
          <ul>
            {c.details.map((d) => (
              <li key={d.location_id}>
                {d.location_name}: <em>{d.labeled_tier}</em> → <em>{d.computed_tier}</em>{" "}
                (opened {d.opened_date})
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function DecisionCard({ payload, busy, error, onSubmit }) {
  const corrections = payload.corrections || [];
  const key = corrections.map((c) => c.id).join(",");
  // id -> "accepted" | "declined". Absent means undecided.
  const [decisions, setDecisions] = useState({});
  useEffect(() => {
    setDecisions({});
  }, [key]);

  const decided = payload.status === "decided";
  const decide = (id, verdict) => setDecisions((d) => ({ ...d, [id]: verdict }));
  const reopen = (id) =>
    setDecisions((d) => {
      const next = { ...d };
      delete next[id];
      return next;
    });

  const chosen = corrections.filter((c) => decisions[c.id] === "accepted").map((c) => c.id);
  const declinedCount = corrections.filter((c) => decisions[c.id] === "declined").length;
  const decidedCount = chosen.length + declinedCount;
  const allDecided = decidedCount === corrections.length && corrections.length > 0;
  const allAccepted = allDecided && declinedCount === 0;

  // Deciding the last correction IS the decision — continue on its own after
  // a short grace window (a Change click within it cancels the auto-continue).
  // Hooks run on every render, including the "decided" one — the guard is
  // inside the effect, never an early return above it.
  useEffect(() => {
    if (decided || !allDecided || busy) return undefined;
    const t = setTimeout(() => onSubmit(chosen), 1400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decided, allDecided, busy, decidedCount]);

  if (decided) {
    return (
      <div className="thread-card decided">
        <div className="thread-card-title">Corrections reviewed</div>
        <div className="thread-card-sub">
          {corrections.length} proposed — decision recorded below.
        </div>
      </div>
    );
  }

  return (
    <div className="thread-card">
      <div className="thread-card-title">
        {corrections.length} corrections proposed — your call
      </div>
      <div className="thread-card-sub">
        Declined corrections are logged and DATA/TRANSLATION/ is built without them.
        DATA/INPUTS/ is never modified either way.
      </div>
      {corrections.map((c) => (
        <CorrectionRow
          key={c.id}
          correction={c}
          decision={decisions[c.id]}
          onDecide={(verdict) => decide(c.id, verdict)}
          onReopen={() => reopen(c.id)}
        />
      ))}
      {error && <div className="panel-error" style={{ marginTop: 8 }}>{error}</div>}
      {allDecided && (
        <div className="corr-progress">
          {allAccepted
            ? `Continuing — all ${chosen.length} accepted…`
            : `Continuing — ${chosen.length} accepted, ${declinedCount} declined…`}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// An answer, with the receipts that make it checkable
// ---------------------------------------------------------------------------

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // clipboard blocked (no https / no permission) — leave the label alone
    }
  };
  return (
    <button type="button" className="answer-copy" onClick={copy} title="Copy this answer">
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function AnswerCard({ payload }) {
  const {
    text,
    refused,
    citations = [],
    numbers_verified: verified,
    artifacts_read: artifactsRead = [],
    attempts,
    mode,
    elapsed_seconds: elapsed,
  } = payload;
  const [open, setOpen] = useState(false);
  // An answer's receipts are its citations: the artifact it named and what it
  // read there. Same line as a narrative's, built from what ask.py reported.
  const receipts = citations.map((c) => ({
    token: baseName(c.artifact),
    path: c.artifact,
    field: c.detail || "",
    value: "",
  }));

  return (
    <div className={`answer ${refused ? "refused" : ""}`}>
      {refused && (
        <div className="answer-refusal-tag">
          Refused — this run's data can't ground that
        </div>
      )}
      <div className="answer-text">
        <BubbleText text={text} receipts={[]} />
      </div>

      <div className="answer-meta">
        {elapsed != null && <span className="chip quiet">{elapsed}s</span>}
        <CopyButton text={text} />
      </div>

      <CheckLine
        artifactsRead={refused ? [] : artifactsRead}
        verified={refused ? 0 : verified}
        attempts={attempts}
        tier={mode}
        receipts={refused ? [] : receipts}
        open={open}
        setOpen={setOpen}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// A phase narrative — generated by pipeline.narrate from this run's real
// artifacts, every number harness-verified. The check line names the tier
// that actually wrote it; provider-fallback reasons stay out of the thread.
// ---------------------------------------------------------------------------

function NarrativeBubble({ payload }) {
  const {
    text,
    decided_by: decidedBy,
    numbers_verified: verified = 0,
    entities_verified: names = 0,
    receipts = [],
    artifacts_read: artifactsRead = [],
    attempts,
  } = payload;
  const [open, setOpen] = useState(false);
  return (
    <div className="thread-bubble ai narrative">
      <div className="narrative-text">
        <BubbleText
          text={text}
          receipts={receipts}
          onOpenReceipts={() => setOpen(true)}
        />
      </div>
      <CheckLine
        artifactsRead={artifactsRead}
        verified={verified + names}
        attempts={attempts}
        tier={decidedBy}
        receipts={receipts}
        open={open}
        setOpen={setOpen}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// One thread entry
// ---------------------------------------------------------------------------

function Entry({ entry, decisionBusy, decisionError, onDecide }) {
  if (entry.type === "narration" && entry.payload) {
    // A deterministic placeholder already replaced by its phase's narrative.
    if (entry.payload.superseded) return null;
    if (entry.payload.kind === "narrative") {
      // "AI" is reserved for text a model actually wrote. Everything the
      // server composed itself is labelled System, below.
      return (
        <div className="thread-entry ai">
          <NarrationMeta phase={entry.payload.phase} title={entry.payload.title} />
          <NarrativeBubble payload={entry.payload} />
        </div>
      );
    }
  }

  if (entry.type === "decision_request") {
    return (
      <div className="thread-entry system">
        <div className="thread-meta">System (Deterministic)</div>
        <DecisionCard
          payload={entry.payload}
          busy={decisionBusy}
          error={decisionError}
          onSubmit={onDecide}
        />
      </div>
    );
  }
  if (entry.type === "decision") {
    return (
      <div className="thread-entry user">
        <div className="thread-meta">{entry.payload.by || "You"}</div>
        <div className="thread-bubble user decision">{entry.payload.text}</div>
      </div>
    );
  }
  if (entry.type === "answer") {
    return (
      <div className="thread-entry ai">
        <div className="thread-meta">AI</div>
        <AnswerCard payload={entry.payload} />
      </div>
    );
  }
  if (entry.role === "user") {
    return (
      <div className="thread-entry user">
        <div className="thread-meta">{entry.payload.by || "You"}</div>
        <div className="thread-bubble user">{entry.payload.text}</div>
      </div>
    );
  }
  if (entry.role === "system") {
    // Server-written lines: the run's own bookkeeping (started, phase
    // summaries, failures). Labelled System (Deterministic), never AI —
    // code wrote them, no model did.
    return (
      <div className="thread-entry system">
        <div className="thread-meta">System (Deterministic)</div>
        <SystemBubble text={entry.payload.text} />
      </div>
    );
  }
  return (
    <div className="thread-entry ai">
      <div className="thread-meta">AI</div>
      <div className="thread-bubble ai">{entry.payload.text}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The answer as it is being written — provisional text, honestly labelled
// ---------------------------------------------------------------------------

// The model's reply arrives in the shape the harness parses ("ANSWER: …" then
// a "CITATIONS:" block). While it is still being written we show the prose
// only: the label prefix is dropped and everything from the CITATIONS: line on
// is held back — those become the receipts under the finished answer. Nothing
// here rewords, reorders or adds to what the model produced.
function provisionalText(raw) {
  let text = raw || "";
  const cut = text.search(/(^|\n)\s*CITATIONS\s*:/i);
  if (cut >= 0) text = text.slice(0, cut);
  return text.replace(/^\s*(ANSWER|REFUSED|NARRATIVE)\s*:\s*/i, "").trimStart();
}

// Pace the DISPLAY of text already received, one character at a time, so a
// person can actually read it. Nothing is invented, reordered or held back.
function useTypewriter(full) {
  const fullRef = useRef(full);
  fullRef.current = full;
  const [shown, setShown] = useState(0);

  useEffect(() => {
    const t = setInterval(() => {
      setShown((n) => {
        const target = fullRef.current.length;
        if (n > target) return target; // a regeneration cleared the draft
        if (n === target) return n;
        return Math.min(target, n + 2);
      });
    }, 42);
    return () => clearInterval(t);
  }, []);

  const flush = useCallback(() => setShown(fullRef.current.length), []);
  return [full.slice(0, shown), flush];
}

function SystemBubble({ text }) {
  return <div className="thread-bubble system">{text}</div>;
}

// Text arrives from the model in bursts. Display is paced so it can be read.
function StreamingAnswer({ raw, state, redos, onStop, stopping }) {
  const [shown] = useTypewriter(provisionalText(raw));

  const verifying = state === "verifying";

  return (
    <div className="thread-entry ai" aria-live="polite">
      <div className="thread-meta">AI · {verifying ? "checking" : "writing"}</div>
      <div className="answer streaming">
        {redos > 0 && (
          <div className="answer-redo">
            The harness rejected a number in that draft — regenerating.
          </div>
        )}
        <div className="answer-text">
          <BubbleText text={shown} receipts={[]} />
          {!verifying && <span className="stream-cursor" aria-hidden="true" />}
        </div>
        <div className="answer-meta">
          <span
            className="chip provisional"
            title="Nothing here is checked yet. When the model stops, every number in this text is verified against this run's artifacts — and the draft is thrown away if any of them fails."
          >
            {verifying ? "verifying every number…" : "draft — not verified yet"}
          </span>
          <button
            type="button"
            className="pending-stop"
            style={{ marginLeft: "auto" }}
            onClick={onStop}
            disabled={stopping}
          >
            Stop
          </button>
        </div>
      </div>
    </div>
  );
}

// The meta row above a narration bubble. Every narration — the draft being
// written and the checked note that replaces it — says which step of the run it
// is explaining, in the rail's own words, so the bubble and the stepper row
// above it are legibly the same thing. The role label is unchanged: "AI" is
// only ever text a model wrote (the step name is a label on that text, not a
// claim about who wrote it).
function NarrationMeta({ phase, title, state }) {
  return (
    <div className="thread-meta">
      Agent <span className="thread-step">· {stepLabel(phase, title)}</span>
      {state ? <span className="thread-state"> · {state}</span> : null}
    </div>
  );
}

// A phase narrating itself, live. Same contract as a streaming answer: the
// text is the model's, shown as it is written, and it is a DRAFT until
// pipeline.narrate's checks pass — at which point the thread's verified
// narrative replaces it. Before the first token there is nothing to show but
// the wait, so the old dots stand in.
function StreamingNarrative({ narration }) {
  const { phase, text, stream_state: state, redos = 0 } = narration;
  // A live stream event carries only its phase; the title comes from the run
  // manager's snapshot, which a mid-stream client may not have yet.
  const title = phaseTitle(narration);
  const [shown] = useTypewriter(provisionalText(text));

  if (!shown && state !== "verifying") {
    return (
      <div className="thread-entry ai" key={`narrating-${phase}`} aria-live="polite">
        <NarrationMeta phase={phase} title={narration.title} state="writing" />
        <div className="pending narr-pending">
          <span className="pending-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span className="pending-text">
            {redos > 0
              ? `Rewriting the ${title} narrative — a number in the last draft didn't verify…`
              : `Writing the ${title} narrative from this run's artifacts…`}
          </span>
        </div>
      </div>
    );
  }

  const verifying = state === "verifying";
  return (
    <div className="thread-entry ai" aria-live="polite">
      <NarrationMeta
        phase={phase}
        title={narration.title}
        state={verifying ? "checking" : "writing"}
      />
      <div className="thread-bubble ai narrative streaming">
        {/* The draft renders as the bullets it is becoming, but with no
            receipts and no underlines: nothing here has been checked yet. */}
        <div className="narrative-text">
          <BubbleText text={shown} receipts={[]} />
          {!verifying && <span className="stream-cursor" aria-hidden="true" />}
        </div>
        <div className="narr-meta">
          <span
            className="chip provisional"
            title="Nothing here is checked yet. When the phase's narrative is finished, every number in it is verified against this run's artifacts — and the draft is thrown away if any of them fails."
          >
            {verifying ? "verifying every number…" : "draft — not verified yet"}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The in-flight answer — the server's pending state, with a way out
// ---------------------------------------------------------------------------

function Pending({ pending, onStop, stopping, redos = 0 }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!pending || !pending.started_at) {
      setElapsed(0);
      return undefined;
    }
    const started = Date.parse(pending.started_at);
    const tick = () => setElapsed(Math.max(0, Math.round((Date.now() - started) / 1000)));
    tick();
    const t = setInterval(tick, 500);
    return () => clearInterval(t);
  }, [pending && pending.started_at]);

  if (!pending) return null;

  return (
    <div className="thread-entry ai" aria-live="polite">
      <div className="thread-meta">AI · working</div>
      <div className="pending">
        <span className="pending-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="pending-text">
          {PENDING_LABEL[pending.state] || "Working…"}
          {pending.started_at ? ` ${elapsed}s` : ""}
        </span>
        <button type="button" className="pending-stop" onClick={onStop} disabled={stopping}>
          Stop
        </button>
      </div>
      {redos > 0 && (
        <div className="pending-queue">
          The harness rejected a number in the last draft — regenerating.
        </div>
      )}
      {pending.queued > 1 && (
        <div className="pending-queue">{pending.queued - 1} more question(s) waiting</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The composer's model picker
// ---------------------------------------------------------------------------

// Which tier gets asked, as a compact dropdown in the composer's footer
// rather than a permanent row of pills: it is a setting, not a legend.
//
// The list is only ever the tiers this machine can really reach
// (GET /api/llm-modes), and the choice is a REQUEST, not a promise — an
// unreachable tier still degrades down the ladder and the answer names
// whoever actually wrote it. That sentence is in the menu, not just in a
// tooltip, because it is the honest part.
function ModePicker({ modes, askMode, setAskMode }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);
  const btnRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      if (btnRef.current) btnRef.current.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!modes || !modes.available || modes.available.length === 0) return null;

  const label = (m) => MODE_PICKER_LABEL[m] || m;

  // One reachable tier is not a choice. Name it and leave it — a dropdown
  // that can only pick what is already picked would be theatre.
  if (modes.available.length === 1) {
    const only = modes.available[0];
    return (
      <span
        className="composer-model static"
        title={MODE_PICKER_TITLE[only] || only}
      >
        {label(only)}
      </span>
    );
  }

  const current = modes.available.includes(askMode) ? askMode : modes.default;

  return (
    <div className="composer-model-wrap" ref={wrapRef}>
      <button
        type="button"
        ref={btnRef}
        className={`composer-model ${open ? "open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Model asked: ${label(current)}`}
        title={MODE_PICKER_TITLE[current] || current}
      >
        <span className="composer-model-name">{label(current)}</span>
        <span className="caret" aria-hidden="true">⌄</span>
      </button>

      {open && (
        <div className="composer-menu" role="listbox" aria-label="Which model answers">
          {modes.available.map((m) => (
            <button
              key={m}
              type="button"
              role="option"
              aria-selected={m === current}
              className={`composer-menu-opt ${m === current ? "on" : ""}`}
              title={MODE_PICKER_TITLE[m] || m}
              onClick={() => {
                setAskMode(m);
                setOpen(false);
                if (btnRef.current) btnRef.current.focus();
              }}
            >
              <span className="tick" aria-hidden="true">
                {m === current ? "✓" : ""}
              </span>
              {label(m)}
            </button>
          ))}
          <p className="composer-menu-note">
            A request, not a promise — the answer names the tier that wrote it.
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The panel
// ---------------------------------------------------------------------------

const MIN_WIDTH = 320;
const MAX_WIDTH = 640;
const WIDTH_KEY = "petfolk.panelWidth";

export default function SidePanel({ asOf, collapsed, onToggle }) {
  const [entries, setEntries] = useState([]);
  const [pending, setPending] = useState(null);
  const [narrating, setNarrating] = useState([]);
  // The answer currently being written, streamed from the server:
  // {text, state: "working"|"generating"|"verifying", redos}. Provisional —
  // it is never an answer until the thread says so.
  const [stream, setStream] = useState(null);
  const [draft, setDraft] = useState("");

  // Phases whose checked narrative is already in this thread. A landed
  // narrative is the end of that phase's story, whatever else arrives about it
  // afterwards — which the two rules below both depend on.
  const narratedPhases = useMemo(() => {
    const done = new Set();
    for (const e of entries) {
      if (
        e.type === "narration" &&
        e.payload &&
        e.payload.kind === "narrative" &&
        e.payload.phase
      ) {
        done.add(e.payload.phase);
      }
    }
    return done;
  }, [entries]);

  // ONE narration is live at a time. The server's list is the follower's queue,
  // not a set of concurrent writers — rendering all of it puts two "AI ·
  // writing" bubbles side by side, one of which will never type a character.
  // The phases waiting behind the head render nothing at all; the stepper is
  // already reporting their state. (Rule and reasoning: ../phases.js.)
  const liveNarration = useMemo(
    () => pickLiveNarration(narrating, narratedPhases),
    [narrating, narratedPhases]
  );

  // Deterministic phase summaries are placeholders, not the between-step
  // voice. Showing them while the agent is writing (or after it has written)
  // makes a system line appear and then vanish. They stay hidden unless the
  // narrative never arrives. The durable between-step line is the gate note
  // the server posts after each phase. (../phases.js.)
  const visibleEntries = useMemo(
    () =>
      entries.filter((e) => {
        if (e.type !== "narration" || !e.payload || !e.payload.placeholder) return true;
        return !placeholderHidden(e.payload.phase, {
          live: liveNarration,
          narratedPhases,
        });
      }),
    [entries, liveNarration, narratedPhases]
  );

  // Durable thread entries render atomically. Only the single provisional
  // draft below is paced character-by-character; replaying every entry that a
  // poll returns makes completed phase notes appear to stream concurrently.

  // What this thread is about, from the thread itself: the last thing that
  // happened in it. The panel is a conversation, and a conversation needs a
  // subject line.
  const runLabel = (() => {
    if (pending) return "answering";
    if (liveNarration) {
      const i = phaseIndex(liveNarration.phase);
      return i < PHASES.length
        ? `narrating step ${i + 1} of ${PHASES.length} — ${phaseTitle(liveNarration)}`
        : "narrating the run";
    }
    const last = entries[entries.length - 1];
    if (!last) return "";
    const when = (last.ts || "").slice(11, 16);
    return when ? `last activity ${when} UTC` : "";
  })();

  // Cards elsewhere can hand the agent a question (e.g. "Ask the agent" on a
  // ledger recommendation). Prefill the composer and focus it — the person
  // still presses Ask, so nothing fires without them seeing it.
  // `collapsed` is owned by the view, so opening the panel goes through
  // onToggle; refs keep the listener registered once with fresh values.
  const collapsedRef = useRef(collapsed);
  collapsedRef.current = collapsed;
  const onToggleRef = useRef(onToggle);
  onToggleRef.current = onToggle;
  useEffect(() => {
    const onAskEvent = (e) => {
      const q = e && e.detail && e.detail.question;
      if (!q) return;
      if (collapsedRef.current && onToggleRef.current) onToggleRef.current();
      setDraft(q);
      setTimeout(() => {
        const el = document.querySelector(".composer textarea, .composer input");
        if (el) el.focus();
      }, 50);
    };
    window.addEventListener("petfolk:ask", onAskEvent);
    return () => window.removeEventListener("petfolk:ask", onAskEvent);
  }, []);
  const [sendError, setSendError] = useState(null);
  const [sendBusy, setSendBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [decisionError, setDecisionError] = useState(null);
  const [askLog, setAskLog] = useState(null);
  // Which model to ask. The choice is the asker's; what actually wrote the
  // answer is reported by the answer itself, so the two can never disagree
  // silently (a chosen tier that cannot be reached degrades and says so).
  const [modes, setModes] = useState(null);
  const [askMode, setAskMode] = useState(
    () => window.localStorage.getItem(MODE_KEY) || ""
  );
  const [pinned, setPinned] = useState(true); // stuck to the latest entry
  const pinnedRef = useRef(true); // the observers below read it without re-binding
  const [unseen, setUnseen] = useState(0);
  const [width, setWidth] = useState(() => {
    const saved = Number(window.localStorage.getItem(WIDTH_KEY));
    return saved >= MIN_WIDTH && saved <= MAX_WIDTH ? saved : 400;
  });

  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const seenCount = useRef(0);

  // --- data ---------------------------------------------------------------

  const refresh = useCallback(async () => {
    try {
      const t = await getJson(`/api/thread?asOf=${encodeURIComponent(asOf)}`);
      setEntries(t.entries);
      setPending(t.pending || null);
      setNarrating(t.narrating || []);
    } catch {
      // server briefly unreachable; the next poll retries
    }
  }, [asOf]);

  // Which tiers this machine can actually serve — the switcher only offers
  // models that are really reachable.
  useEffect(() => {
    let live = true;
    getJson("/api/llm-modes")
      .then((m) => {
        if (!live) return;
        setModes(m);
        setAskMode((current) => {
          if (
            current === "claude-cli" &&
            m.default &&
            m.default !== "claude-cli" &&
            m.available.includes(m.default)
          ) {
            return m.default;
          }
          return current && m.available.includes(current) ? current : m.default;
        });
      })
      .catch(() => setModes(null));
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (askMode) window.localStorage.setItem(MODE_KEY, askMode);
  }, [askMode]);

  const refreshAskLog = useCallback(async () => {
    try {
      setAskLog(await getJson(`/api/ask-log?asOf=${encodeURIComponent(asOf)}`));
    } catch {
      setAskLog(null);
    }
  }, [asOf]);

  useEffect(() => {
    setEntries([]);
    setPending(null);
    setNarrating([]);
    setStream(null);
    setUnseen(0);
    setPinned(true);
    seenCount.current = 0;
    refresh();
    refreshAskLog();
  }, [asOf, refresh, refreshAskLog]);

  // --- the answer as it is written ----------------------------------------
  //
  // subscribeEvents() is a live view of pipeline.ask working: the text the
  // model is producing, and what the harness then does with it. It rides the
  // session tunnel where there is one and GET /api/ask/stream where there is
  // not (api.js) — same events either way. It is presentation only: the thread
  // above is still the record, and the answer that lands there is the
  // validated one. If neither connection opens, the panel behaves exactly as
  // it did before: the polled `pending` state, then the finished answer.
  const streamHandler = useRef(null);
  streamHandler.current = (ev) => {
    switch (ev.type) {
      case "hello":
        if (ev.pending) setPending(ev.pending);
        // Joining mid-answer (a reload, a second browser) picks up the draft
        // already in flight instead of staring at a spinner.
        setStream(
          ev.stream
            ? { text: ev.stream.text || "", state: ev.stream.state || "working",
                redos: ev.stream.redos || 0 }
            : null
        );
        break;
      case "start":
        setStream({ text: "", state: "working", redos: 0 });
        break;
      case "generating":
        setStream((s) => ({ ...(s || { text: "", redos: 0 }), state: "generating" }));
        break;
      case "delta":
        setStream((s) => ({
          ...(s || { redos: 0 }),
          state: "generating",
          text: (s ? s.text : "") + (ev.text || ""),
        }));
        break;
      case "verifying":
        setStream((s) => ({ ...(s || { text: "", redos: 0 }), state: "verifying" }));
        break;
      case "redo":
        // A number in that draft did not verify. The draft is not an answer
        // and never becomes one — it is dropped, and generation restarts.
        setStream((s) => ({ text: "", state: "working", redos: (s ? s.redos : 0) + 1 }));
        break;
      case "tier_fallback":
        setStream((s) => ({ ...(s || { redos: 0 }), text: "", state: "working" }));
        break;
      case "final":
      case "note":
      case "cancelled":
        // Swap the draft for what the thread actually recorded, in one paint:
        // React batches these, so there is no gap and no double bubble.
        refresh().then(() => setStream(null));
        break;

      // --- the run narrating itself, one stream per phase ------------------
      case "narration_generating":
        setNarrating((list) => patchPhase(list, ev.phase, { stream_state: "generating" }));
        break;
      case "narration_delta":
        setNarrating((list) =>
          patchPhase(list, ev.phase, (n) => ({
            stream_state: "generating",
            text: (n.text || "") + (ev.text || ""),
          }))
        );
        break;
      case "narration_verifying":
        setNarrating((list) => patchPhase(list, ev.phase, { stream_state: "verifying" }));
        break;
      case "narration_redo":
        setNarrating((list) =>
          patchPhase(list, ev.phase, (n) => ({
            text: "",
            stream_state: "working",
            redos: (n.redos || 0) + 1,
          }))
        );
        break;
      case "narration_tier_fallback":
        setNarrating((list) =>
          patchPhase(list, ev.phase, { text: "", stream_state: "working" })
        );
        break;
      case "narration_final":
      case "narration_failed":
        // The checked narrative (or the honest failure note) is in the thread
        // now; drop the draft in the same paint as the entries arrive.
        refresh().then(() =>
          setNarrating((list) => list.filter((n) => n.phase !== ev.phase))
        );
        break;
      default:
        break;
    }
  };

  useEffect(
    () => subscribeEvents(asOf, (ev) => streamHandler.current(ev)),
    [asOf]
  );

  // Faster polling while an answer or a narrative is in flight; calmer idle.
  useEffect(() => {
    const t = setInterval(refresh, pending ? 600 : narrating.length > 0 ? 800 : 1500);
    return () => clearInterval(t);
  }, [refresh, pending, narrating.length]);

  const answerCount = useMemo(
    () => entries.filter((e) => e.type === "answer").length,
    [entries]
  );
  useEffect(() => {
    refreshAskLog();
  }, [answerCount, refreshAskLog]);

  // --- scrolling ----------------------------------------------------------

  const scrollToLatest = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setPinned(true);
    setUnseen(0);
    seenCount.current = entries.length;
  }, [entries.length]);

  // Opening a Monday (or switching to one) starts at the latest entry: the
  // thread is a running conversation and its newest line is the point.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setPinned(true);
    setUnseen(0);
    seenCount.current = entries.length;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asOf]);

  // Entries arrive after mount, bubbles grow as their text lands, and fonts
  // reflow a beat later — each of which changes the content height AFTER the
  // effects above have run. Watching the scroller's own size is what makes
  // "stay at the bottom" hold in every one of those cases instead of most.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver !== "function") return undefined;
    const stick = () => {
      if (pinnedRef.current) el.scrollTop = el.scrollHeight;
    };
    const ro = new ResizeObserver(stick);
    ro.observe(el);
    for (const child of el.children) ro.observe(child);
    const mo =
      typeof MutationObserver === "function"
        ? new MutationObserver(() => {
            for (const child of el.children) ro.observe(child);
            stick();
          })
        : null;
    if (mo) mo.observe(el, { childList: true });
    return () => {
      ro.disconnect();
      if (mo) mo.disconnect();
    };
  }, []);

  // Text written into a bubble that already exists — a narration draft
  // growing, an answer being typed — changes no count, so the length of what
  // is in flight is part of what the view reacts to.
  const draftLength =
    (stream && stream.text ? stream.text.length : 0) +
    ((liveNarration && liveNarration.text) || "").length;

  useEffect(() => {
    if (pinned) {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
      seenCount.current = visibleEntries.length;
      setUnseen(0);
    } else {
      // Counted against what is actually on screen: a held closing line is not
      // something the reader missed.
      setUnseen(Math.max(0, visibleEntries.length - seenCount.current));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleEntries.length, pending, narrating.length, draftLength, pinned]);

  // Between renders the same text keeps arriving, so while ANYTHING is being
  // written — an answer or any phase's narration — the view keeps itself at
  // the bottom. A reader who scrolls up stays where they put themselves; the
  // "N new" button brings them back.
  const streaming = Boolean(stream) || Boolean(liveNarration);
  useEffect(() => {
    if (!streaming || !pinned) return undefined;
    const t = setInterval(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, 120);
    return () => clearInterval(t);
  }, [streaming, pinned]);

  useEffect(() => {
    pinnedRef.current = pinned;
  }, [pinned]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setPinned(atBottom);
    if (atBottom) {
      seenCount.current = entries.length;
      setUnseen(0);
    }
  };

  // --- actions ------------------------------------------------------------

  const decide = async (acceptIds) => {
    setDecisionBusy(true);
    setDecisionError(null);
    try {
      await postJson("/api/run/corrections", { accept: acceptIds });
      await refresh();
    } catch (e) {
      setDecisionError(e.message);
    } finally {
      setDecisionBusy(false);
    }
  };

  const send = async (text) => {
    const question = (text || "").trim();
    if (!question || sendBusy) return;
    setSendBusy(true);
    setSendError(null);
    try {
      await postJson("/api/thread/message", {
        text: question,
        asOf,
        ...(askMode ? { llmMode: askMode } : {}),
      });
      setDraft("");
      setPinned(true);
      await refresh();
    } catch (e) {
      setSendError(e.message); // the draft stays, so nothing typed is lost
    } finally {
      setSendBusy(false);
    }
  };

  const stop = async () => {
    setStopping(true);
    try {
      await postJson("/api/thread/cancel", { asOf });
      await refresh();
    } catch (e) {
      setSendError(e.message);
    } finally {
      setStopping(false);
    }
  };

  // --- composer sizing + shortcuts ---------------------------------------

  const grow = (el) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
  };

  useEffect(() => {
    const onKey = (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (inputRef.current) inputRef.current.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // --- resize handle ------------------------------------------------------

  const startResize = (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = width;
    const move = (ev) => {
      const next = Math.min(
        MAX_WIDTH,
        Math.max(MIN_WIDTH, startWidth + (startX - ev.clientX))
      );
      setWidth(next);
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.classList.remove("resizing-panel");
    };
    document.body.classList.add("resizing-panel");
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  useEffect(() => {
    window.localStorage.setItem(WIDTH_KEY, String(width));
  }, [width]);

  // Let the grid know how wide the panel wants to be.
  useEffect(() => {
    document.documentElement.style.setProperty("--panel-width", `${width}px`);
  }, [width]);

  // --- render -------------------------------------------------------------

  if (collapsed) {
    return (
      <aside className="side-panel collapsed">
        <button
          type="button"
          className="panel-toggle"
          onClick={onToggle}
          title="Open Agent"
          aria-label="Open Agent"
        >
          ‹
        </button>
        <span className="panel-collapsed-label">Agent</span>
      </aside>
    );
  }

  return (
    <aside className="side-panel">
      <div
        className="panel-resize"
        onMouseDown={startResize}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the panel"
        title="Drag to resize"
      />

      <div className="panel-head">
        <div>
          <div className="panel-title">Agent</div>
          {runLabel ? <div className="panel-scope">{runLabel}</div> : null}
        </div>
        <button
          type="button"
          className="panel-toggle"
          onClick={onToggle}
          title="Collapse"
          aria-label="Collapse Agent"
        >
          ›
        </button>
      </div>

      <div className="panel-scroll" ref={scrollRef} onScroll={onScroll} role="log">
        {entries.length === 0 && !pending ? (
          <div className="panel-empty">
            Nothing in this Monday's session yet — start a run and the pipeline
            narrates it here, or ask a question about the digest below.
          </div>
        ) : (
          visibleEntries.map((en) => (
            <Entry
              key={en.id}
              entry={en}
              decisionBusy={decisionBusy}
              decisionError={decisionError}
              onDecide={decide}
            />
          ))
        )}
        {liveNarration ? (
          <StreamingNarrative
            key={`narrating-${liveNarration.phase}`}
            narration={liveNarration}
          />
        ) : null}
        {stream && (stream.text || stream.state === "verifying") ? (
          <StreamingAnswer
            raw={stream.text}
            state={stream.state}
            redos={stream.redos}
            onStop={stop}
            stopping={stopping}
          />
        ) : (
          <Pending
            pending={
              pending ||
              // The stream said work started before the next thread poll came
              // back; show the same waiting state rather than nothing.
              (stream ? { state: "answering", started_at: null, queued: 1 } : null)
            }
            onStop={stop}
            stopping={stopping}
            redos={stream ? stream.redos : 0}
          />
        )}
      </div>

      {!pinned && unseen > 0 && (
        <button type="button" className="jump-latest" onClick={scrollToLatest}>
          {unseen} new ↓
        </button>
      )}

      {sendError && <div className="panel-error panel-error-inline">{sendError}</div>}

      {/* The composer: one card that says what the question can see, takes
          the question, and shows who will be asked. */}
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        {/* Scope, stated before the question is typed. There is nothing to
            pick here because there is nothing else it may read: an answer is
            grounded in this Monday's artifacts or it is refused by name. */}
        <div className="composer-context">
          <span
            className="composer-scope"
            title={`Scope: the ${asOf} run's artifacts only — validation report, signals, verdicts, ledger. A question they cannot ground is refused by name, never guessed.`}
          >
            <span className="at" aria-hidden="true">@</span>
            this Monday&rsquo;s run · {asOf}
          </span>
        </div>

        <div className="composer-field">
          <textarea
            ref={inputRef}
            rows={1}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              grow(e.target);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(draft);
              }
            }}
            placeholder={
              pending
                ? "Waiting on the current question…"
                : "Ask about this Monday's digest…"
            }
            aria-label="Ask a question about this Monday's digest"
          />
          {/* One slot, two jobs: ask while idle, stop what is being written
              while an answer is in flight — the same `stop` the thread's own
              buttons call, so a cancel means one thing wherever it is hit. */}
          {pending || stream ? (
            <button
              type="button"
              className="composer-send stop"
              onClick={stop}
              disabled={stopping}
              title="Stop the answer being written"
              aria-label="Stop the answer being written"
            >
              <span aria-hidden="true">■</span>
            </button>
          ) : (
            <button
              type="submit"
              className="composer-send"
              disabled={sendBusy || !draft.trim()}
              title="Ask — Enter to send, Shift+Enter for a new line"
              aria-label="Ask"
            >
              <span aria-hidden="true">↑</span>
            </button>
          )}
        </div>

        <div className="composer-foot">
          <span
            className="composer-kind"
            title="Questions only. The panel reads this run's artifacts; it never changes them, and it makes no call the data cannot ground."
          >
            Ask
          </span>
          <ModePicker modes={modes} askMode={askMode} setAskMode={setAskMode} />
        </div>
      </form>
    </aside>
  );
}
