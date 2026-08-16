import React, { useCallback, useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../api.js";
import { mondayShort, summarizeMemory } from "../memory.js";

// The recommendation ledger — what changed since last Monday, what is open,
// and what to do about each one. All content verbatim from digest.ledger and
// DATA/OUTPUTS/ledger.csv; the UI computes nothing.
//
// Rows are grouped by WHERE THEY CAME FROM, not by status, because that is the
// distinction the loop turns on and the one a flat "open recommendations" list
// destroyed: a carried-forward row that was just graded and a row opened for
// the first time this morning are different kinds of thing, and reading three
// undifferentiated cards made a second Monday look like a first one. The
// partition itself lives in ../memory.js — the digest page and the run console
// must bucket the same rows the same way.
//
// Three things make this section the loop closing rather than a status list:
//
// 0. A carried row shows what was asked LAST Monday, then what the numbers did
//    this Monday, then — as a separate fact — whether anyone confirmed the work
//    happened. Those last two never merge: metric movement can establish
//    working or not working, and can never establish that the action occurred.
//
// 1. A re-check that failed carries its SUCCESSOR — the next move the pipeline
//    generated (pipeline/successor.py, harness-checked). The next move is the
//    headline; "what was tried → what happened" sits behind it as the story,
//    because a leader with thirty minutes needs the action first.
// 2. Every card can be acted on: close it out, relaunch with a new date,
//    escalate, or dismiss with a reason. The decision goes to
//    POST /api/ledger/decide, which records it through the pipeline's own
//    ledger machinery — so the resolved state a reload shows comes from
//    DATA/OUTPUTS/ledger.csv, not from anything this component remembered.
//
// The decision UX is deliberately the same as the corrections card in the
// thread panel: separate buttons while undecided, then a plain status with an
// understated Change link. Nothing resolved looks pressable but Change.

// Colour follows the LOOP STATE — what the number did AND whether a human
// confirmed the work happened — never the number alone. An unattested gain is
// deliberately not green: we cannot claim credit for it.
const LOOP_STATE_CLASS = {
  confirmed_working: "worked",
  intervention_failed: "rethink",
  not_executed: "escalated",
  needs_attestation: "escalated",
  unattributed_gain: "neutral",
  awaiting_evidence: "neutral",
  in_flight: "neutral",
  unverifiable: "neutral",
  pending: "neutral",
};

// Does the data support the INTERVENTION, or only the signal?
const BASIS_LABEL = { measured: "Evidence-backed", playbook: "Playbook hypothesis" };

const EXECUTION_LABEL = { done: "Done", not_done: "Not done", unknown: "Not confirmed" };

const DECISION_LABEL = {
  close: "✓ Closed",
  relaunch: "↻ Relaunched",
  escalate: "↑ Escalated",
  dismiss: "× Dismissed",
};

const DECISION_CLASS = {
  close: "accepted",
  relaunch: "neutral",
  escalate: "neutral",
  dismiss: "declined",
};

function addDays(isoDate, days) {
  const d = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return "";
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------------------
// Acting on one recommendation
// ---------------------------------------------------------------------------

function LedgerActions({ recId, decision, defaultCheckBy, busy, error, onDecide, onReopen, onAskAgent }) {
  // "dismiss" | "relaunch" while that action is collecting its input.
  const [prompting, setPrompting] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [checkBy, setCheckBy] = useState(defaultCheckBy);
  const changeRef = useRef(null);
  const firstRef = useRef(null);
  const inputRef = useRef(null);
  // Deciding swaps the buttons for a status, which would drop keyboard focus
  // to the top of the document mid-review. Keep it on this card.
  const followFocus = useRef(false);

  useEffect(() => {
    if (!followFocus.current) return;
    followFocus.current = false;
    const next = decision ? changeRef.current : firstRef.current;
    if (next) next.focus();
  }, [decision]);

  useEffect(() => {
    if (prompting && inputRef.current) inputRef.current.focus();
  }, [prompting]);

  if (decision) {
    return (
      <div className="rec-resolved">
        <span className={`corr-status ${DECISION_CLASS[decision] || "neutral"}`}>
          {DECISION_LABEL[decision] || decision}
        </span>
        <button
          type="button"
          className="corr-change"
          ref={changeRef}
          onClick={() => {
            followFocus.current = true;
            onReopen();
          }}
          aria-label={`Change the decision on this recommendation — currently ${decision}`}
        >
          Change
        </button>
        {error && <span className="rec-error">{error}</span>}
      </div>
    );
  }

  if (prompting === "dismiss") {
    return (
      <form
        className="rec-prompt"
        onSubmit={(e) => {
          e.preventDefault();
          followFocus.current = true;
          onDecide("dismiss", { note: reason.trim() });
        }}
      >
        <input
          ref={inputRef}
          type="text"
          value={reason}
          maxLength={500}
          placeholder="Why is this being dropped? (e.g. two medical leaves — known cause)"
          onChange={(e) => setReason(e.target.value)}
          aria-label="Reason for dismissing this recommendation"
        />
        <button type="submit" className="rec-confirm" disabled={busy || !reason.trim()}>
          Dismiss
        </button>
        <button type="button" className="corr-change" onClick={() => setPrompting(null)}>
          Cancel
        </button>
        {error && <span className="rec-error">{error}</span>}
      </form>
    );
  }

  if (prompting === "relaunch") {
    return (
      <form
        className="rec-prompt"
        onSubmit={(e) => {
          e.preventDefault();
          followFocus.current = true;
          onDecide("relaunch", { newCheckBy: checkBy });
        }}
      >
        <label className="rec-date">
          New check-by
          <input
            ref={inputRef}
            type="date"
            value={checkBy}
            onChange={(e) => setCheckBy(e.target.value)}
            aria-label="New check-by date"
          />
        </label>
        <button type="submit" className="rec-confirm" disabled={busy || !checkBy}>
          Relaunch
        </button>
        <button type="button" className="corr-change" onClick={() => setPrompting(null)}>
          Cancel
        </button>
        {error && <span className="rec-error">{error}</span>}
      </form>
    );
  }

  return (
    <div className="rec-decide" role="group" aria-label={`Decide on ${recId}`}>
      {onAskAgent && (
        <button
          type="button"
          className="rec-ask-agent"
          disabled={busy}
          onClick={onAskAgent}
          title="Hand this recommendation to the agent for grounded advice"
        >
          ✦ Ask the agent
        </button>
      )}
      <span className="rec-attest">
        <span className="rec-attest-label">Did this happen?</span>
        <button
          type="button"
          className="rec-ghost"
          disabled={busy}
          title="Only a person can answer this — the pipeline never infers it from a metric"
          onClick={() => { followFocus.current = true; onDecide("attest", { execution: "done" }); }}
        >
          Done
        </button>
        <button
          type="button"
          className="rec-ghost"
          disabled={busy}
          onClick={() => { followFocus.current = true; onDecide("attest", { execution: "not_done" }); }}
        >
          Not done
        </button>
      </span>
      <div className="rec-menu-wrap">
        <button
          type="button"
          className="rec-ghost rec-menu-trigger"
          ref={firstRef}
          disabled={busy}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((o) => !o)}
        >
          Decide &#9662;
        </button>
        {menuOpen && (
          <div className="rec-menu" role="menu" onMouseLeave={() => setMenuOpen(false)}>
            <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); followFocus.current = true; onDecide("close", {}); }}>
              Close — done, stop tracking
            </button>
            <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); setCheckBy(defaultCheckBy); setPrompting("relaunch"); }}>
              Relaunch — new check-by date
            </button>
            <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); followFocus.current = true; onDecide("escalate", {}); }}>
              Escalate — to both regional partners
            </button>
            <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); setReason(""); setPrompting("dismiss"); }}>
              Dismiss — with a reason
            </button>
          </div>
        )}
      </div>
      {error && <span className="rec-error">{error}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The next move — what a failed re-check hands back
// ---------------------------------------------------------------------------

function SuccessorBlock({ successor }) {
  const checks = successor.checks || {};
  return (
    <div className="rec-successor">
      <div className="rec-successor-head">
        <span className="eyebrow">
          {successor.escalated ? "Next move — escalated" : "Next move"}
        </span>
        <span className="rec-successor-meta">
          attempt {String(successor.attempt)} · owner {successor.owner}
          {successor.escalation_partner ? ` with ${successor.escalation_partner}` : ""} ·
          check by {successor.check_by}
        </span>
      </div>
      <p className="rec-next-move">{successor.next_move}</p>
      <p className="rec-expected">{successor.expected}</p>
      <details className="rec-story">
        <summary>What was tried → what happened → why this instead</summary>
        <p>
          <strong>Tried:</strong> {successor.what_was_tried}
        </p>
        <p>
          <strong>Happened:</strong> {successor.what_happened}
        </p>
        <p>
          <strong>Why this instead:</strong> {successor.why}
        </p>
      </details>
      <div className="rec-chips">
        {checks.numbers_verified > 0 && (
          <span
            className="chip verified"
            title="Every number in this next move was checked against this run's own facts before you saw it."
          >
            {String(checks.numbers_verified)} numbers verified
          </span>
        )}
        {successor.decided_by && successor.decided_by !== "claude-cli" && (
          <span className="chip quiet" title="Which tier actually wrote this next move.">
            {successor.decided_by === "template" ? "deterministic tier" : successor.decided_by}
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

// The two questions a re-check answers, and they are never the same question.
// The left one is arithmetic on the weeks that followed. The right one can only
// be answered by a person, so an unanswered right-hand side stays unanswered —
// the number moving the right way is not permission to fill it in.
function RecheckVerdict({ row }) {
  return (
    <div className="recheck-verdict">
      <div className="recheck-axis">
        <span className="recheck-q">Did the number move?</span>
        <span className={`recheck-a outcome-${row.outcome || "pending"}`}>
          {row.outcome_display}
        </span>
        <span className="recheck-evidence">
          {row.anchor_level_display} → {row.followup_level_display} over{" "}
          {String(row.followup_weeks)} wk
        </span>
      </div>
      <div className="recheck-axis">
        <span className="recheck-q">Did the work happen?</span>
        <span className={`recheck-a execution-${row.execution || "unknown"}`}>
          {EXECUTION_LABEL[row.execution] || row.execution_display}
        </span>
        <span className="recheck-evidence">
          {row.execution === "unknown"
            ? "only a person can answer this — the numbers cannot"
            : `attested${row.execution_by ? ` by ${row.execution_by}` : ""}`}
        </span>
      </div>
    </div>
  );
}

function ChangedCard({ row, actions, carriedFrom, asOf }) {
  const successor = row.successor;
  return (
    <article className={`ledger-card carried ${successor ? "has-successor" : ""}`}>
      <div className="head">
        <span className="who">{row.center}</span>
        <span className="metric">{row.metric_display}</span>
        <span className={`badge ${LOOP_STATE_CLASS[row.loop_state] || "neutral"}`}>
          {row.loop_state_display || row.outcome_display}
        </span>
      </div>
      <div className="rec-lineage-strip">
        Carried from {mondayShort(carriedFrom || row.created_week)} → re-checked {mondayShort(asOf)}
        {" → "}outcome {String(row.outcome || "unknown").replace(/_/g, " ")}
        {" → "}execution {String(row.execution || "unknown").replace(/_/g, " ")}
      </div>
      {row.recommendation && (
        <p className="rec carried-ask">
          <span className="carried-ask-label">
            What we asked on {mondayShort(carriedFrom || row.created_week)}
          </span>
          {row.recommendation}
        </p>
      )}
      <RecheckVerdict row={row} />
      {successor ? (
        <>
          <SuccessorBlock successor={successor} />
          <div className="meta">
            owner {row.owner} · first asked {row.created_week}
          </div>
        </>
      ) : (
        <>
          <p className="note">{row.note}</p>
          <div className="meta">
            owner {row.owner} · recommended {row.created_week} · check by{" "}
            {row.check_by}
          </div>
        </>
      )}
      {actions}
    </article>
  );
}

function EvidenceLine({ row }) {
  if (!row.action_basis && !row.execution) return null;
  return (
    <div className="rec-chips">
      {row.action_basis && (
        <span
          className={`chip ${row.action_basis === "measured" ? "verified" : "quiet"}`}
          title={row.action_assumption || ""}
        >
          {BASIS_LABEL[row.action_basis] || row.action_basis}
        </span>
      )}
      {row.execution && (
        <span className={`chip ${row.execution === "unknown" ? "quiet" : "verified"}`}>
          Did it happen? {EXECUTION_LABEL[row.execution] || row.execution}
          {row.execution_by ? ` — ${row.execution_by}` : ""}
        </span>
      )}
      {row.action_assumption && (
        <details className="rec-evidence">
          <summary>What this assumes — and what the data does not prove</summary>
          <p>{row.action_assumption}</p>
        </details>
      )}
    </div>
  );
}

function OpenCard({ row, live, actions, isNew }) {
  const status = live || row;
  return (
    <article className={`ledger-card ${isNew ? "brand-new" : ""}`}>
      <div className="head">
        <span className="who">{row.center}</span>
        <span className="metric">{row.metric_display || row.metric}</span>
        <span className={`badge ${LOOP_STATE_CLASS[row.loop_state] || "neutral"}`}>
          {row.loop_state_display || row.status_display || row.status}
        </span>
      </div>
      {isNew && (
        <div className="rec-lineage-strip new">
          Genuinely new this Monday · opened {mondayShort(row.created_week)} → first re-check {row.check_by}
        </div>
      )}
      {row.supersedes && (
        <p className="rec-lineage">
          Successor — replaces an earlier recommendation here that was carried out
          and still did not move the number.
        </p>
      )}
      <p className="rec">{row.recommendation}</p>
      <EvidenceLine row={row} />
      {status.outcome_note && <p className="note">{String(status.outcome_note).replace(/^human:\s*/i, "")}</p>}
      <div className="meta">
        owner {row.owner} · created {row.created_week} · check by{" "}
        {(live && live.check_by) || row.check_by}
        {row.last_checked ? ` · last checked ${row.last_checked}` : ""}
      </div>
      {actions}
    </article>
  );
}

// ---------------------------------------------------------------------------
// The section
// ---------------------------------------------------------------------------

export default function LedgerSection({ ledger, asOf, carryIn, hideIntro }) {
  // Live state table (DATA/OUTPUTS/ledger.csv) keyed by rec_id: decisions taken
  // after the run landed live here, not in digest.json.
  const [live, setLive] = useState({});
  const [busy, setBusy] = useState(null);
  const [errors, setErrors] = useState({});
  const [reopened, setReopened] = useState({});

  const refreshLive = useCallback(async () => {
    try {
      const data = await getJson("/api/ledger");
      const byId = {};
      for (const r of data.rows || []) byId[r.rec_id] = r;
      setLive(byId);
    } catch {
      setLive({}); // no ledger file yet — cards fall back to the digest's copy
    }
  }, []);

  useEffect(() => {
    refreshLive();
  }, [refreshLive, ledger]);

  const decide = async (recId, action, extra) => {
    setBusy(recId);
    setErrors((e) => ({ ...e, [recId]: null }));
    try {
      await postJson("/api/ledger/decide", { recId, action, asOf, ...extra });
      setReopened((r) => ({ ...r, [recId]: false }));
      await refreshLive();
    } catch (e) {
      setErrors((err) => ({ ...err, [recId]: e.message }));
    } finally {
      setBusy(null);
    }
  };

  // Display names live on the digest's own rows. The /api/ledger rows carry
  // raw warehouse column names (staff_call_outs) — those must never reach a
  // person, so anything read from `live` gets humanized as a last resort.
  const displayRow = (recId) => {
    if (!ledger) return {};
    const pools = [ledger.changed, ledger.open, ledger.rows];
    for (const pool of pools) {
      if (!Array.isArray(pool)) continue;
      const hit = pool.find((r) => r && r.rec_id === recId);
      if (hit) return hit;
    }
    return {};
  };
  const humanMetric = (m) =>
    m ? String(m).replace(/_/g, " ").replace(/\bpct\b/g, "rate").trim() : "";

  const actionsFor = (recId, fallbackCheckBy) => {
    const liveRow = live[recId];
    const decision = reopened[recId] ? null : (liveRow && liveRow.decision) || null;
    return (
      <LedgerActions
        recId={recId}
        decision={decision}
        defaultCheckBy={
          addDays(asOf || (liveRow && liveRow.check_by) || fallbackCheckBy || "", 14) ||
          fallbackCheckBy ||
          ""
        }
        busy={busy === recId}
        error={errors[recId]}
        onDecide={(action, extra) => decide(recId, action, extra)}
        onReopen={() => setReopened((r) => ({ ...r, [recId]: true }))}
        onAskAgent={() => {
          const row = live[recId] || {};
          const shown = displayRow(recId);
          const center = shown.center || row.location_name || row.center || "";
          const metric =
            shown.metric_display ||
            row.metric_display ||
            humanMetric(row.metric) ||
            "";
          window.dispatchEvent(
            new CustomEvent("petfolk:ask", {
              detail: {
                question: `What should I do about the ${center} ${metric} recommendation — close it, relaunch it, escalate it, or change the approach?`,
              },
            })
          );
        }}
      />
    );
  };

  if (!ledger) {
    return (
      <div className="panel-empty">
        No ledger yet for this digest. Recommendations are tracked from the
        first full pipeline run; open items and re-check outcomes appear here.
      </div>
    );
  }

  // Partial digest: the ledger file exists on disk but has not been
  // re-checked for this week. Show the rows as they stand, labeled honestly.
  if (ledger.partial) {
    return (
      <>
        <div className="banner">{ledger.note}</div>
        {ledger.rows.length === 0 ? (
          <div className="panel-empty" style={{ marginTop: 14 }}>
            The ledger has no rows yet.
          </div>
        ) : (
          <div style={{ marginTop: 14 }}>
            {ledger.rows.map((r) => (
              <OpenCard
                key={r.rec_id}
                row={r}
                live={live[r.rec_id]}
                actions={actionsFor(r.rec_id, r.check_by)}
              />
            ))}
          </div>
        )}
      </>
    );
  }

  // Where each row came from. One partition, shared with the digest's memory
  // band (../memory.js), so the two surfaces can never disagree about which
  // recommendations were inherited and which were opened today.
  const m = summarizeMemory({ carryIn, ledger, asOf });
  const prevLabel = m.previousRun ? mondayShort(m.previousRun) : "the previous Monday";
  const nothingAtAll =
    m.rechecked.length === 0 && m.created.length === 0 && m.stillOpen.length === 0;

  return (
    <>
      {m.isFirstRun && !hideIntro && (
        <p className="ledger-first-run">
          First Monday of the sequence — nothing to re-check. Everything below is
          new, and every row is written to the ledger so next Monday opens by
          grading it.
        </p>
      )}

      {m.rechecked.length > 0 && (
        <>
          <h3 className="ledger-sub">
            Carried from {prevLabel} — re-checked against this week
            <span className="ledger-sub-count">{m.rechecked.length}</span>
          </h3>
          <p className="ledger-sub-note">
            These were not found this morning. {prevLabel} opened them; this run
            graded them against the weeks that actually followed.
          </p>
          {m.rechecked.map((r) => (
            <ChangedCard
              key={r.rec_id}
              row={r}
              carriedFrom={r.created_week || m.previousRun}
              asOf={asOf}
              actions={actionsFor(r.rec_id, r.check_by)}
            />
          ))}
        </>
      )}

      {m.created.length > 0 && (
        <>
          <h3 className="ledger-sub">
            Genuinely new this Monday
            <span className="ledger-sub-count">{m.created.length}</span>
          </h3>
          <p className="ledger-sub-note">
            {m.isFirstRun
              ? "Opened by this run's signals, with an owner and a check-by date so next Monday can rule on them."
              : `Nothing carried in covered ${m.created.length === 1 ? "this" : "these"} — opened by this run's signals and re-checked next Monday.`}
          </p>
          {m.created.map((r) => (
            <OpenCard
              key={r.rec_id}
              row={r}
              live={live[r.rec_id]}
              isNew
              actions={actionsFor(r.rec_id, r.check_by)}
            />
          ))}
        </>
      )}

      {m.stillOpen.length > 0 && (
        <>
          <h3 className="ledger-sub">
            Carried forward, not yet due
            <span className="ledger-sub-count">{m.stillOpen.length}</span>
          </h3>
          <p className="ledger-sub-note">
            Still tracked from an earlier Monday. No re-check this run — their
            check-by date has not arrived.
          </p>
          {m.stillOpen.map((r) => (
            <OpenCard
              key={r.rec_id}
              row={r}
              live={live[r.rec_id]}
              actions={actionsFor(r.rec_id, r.check_by)}
            />
          ))}
        </>
      )}

      {nothingAtAll && (
        <div className="panel-empty">
          Nothing open. New recommendations from this digest's signals appear
          here and are re-checked automatically next Monday.
        </div>
      )}
    </>
  );
}
