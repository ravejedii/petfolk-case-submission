import React from "react";
import {
  mondayShort,
  summarizeMemory,
  recheckCounts,
  outcomeClause,
  executionClause,
} from "../memory.js";

// Step 0 — what this Monday started with, stated before the new analysis.
//
// The console splits this from the re-check: Progress shows only what was
// inherited (this file's default band, variant "step0"). The ledger card
// shows what happened when that memory met the new week (RecheckBand).
// The digest page still renders both in one band (variant "full") so a
// reviewer opening that page sees the whole loop without scrolling to a
// second card.

function carriedLine(m) {
  const n = m.carriedCount;
  const rec = `${n} recommendation${n === 1 ? "" : "s"} carried forward`;
  if (m.dueCount === null) return rec;
  if (m.dueCount === 0) return `${rec} — none due for re-check yet`;
  if (m.dueCount === n)
    return `${rec} — ${n === 2 ? "both" : n === 1 ? "it is" : `all ${n}`} due for re-check this Monday`;
  return `${rec} — ${m.dueCount} due for re-check this Monday`;
}

function correctionsLine(m) {
  if (m.correctionsCarried === null && m.correctionsNew === null) return null;
  const carried = m.correctionsCarried || 0;
  if (!carried && !m.correctionsNew) return null;
  const head = carried
    ? `${carried} prior data-quality decision${carried === 1 ? "" : "s"} carried forward`
    : "No prior data-quality decisions carried forward";
  if (m.correctionsNew === null) return head;
  return m.correctionsNew === 0
    ? `${head} · 0 new decisions required`
    : `${head} · ${m.correctionsNew} new decision${m.correctionsNew === 1 ? "" : "s"} needed your approval`;
}

function evidenceLine(m) {
  if (!m.panelReadThrough) return null;
  const providerRows = m.providerRowsUnderStandingCorrections;
  const rows = providerRows ?? m.rowsUnderStandingCorrections;
  const tail =
    rows > 0
      ? providerRows !== null
        ? ` ${rows} newly arrived provider row${rows === 1 ? " was" : "s were"} covered automatically by an existing standing rule.`
        : ` ${rows} newly arrived row${rows === 1 ? " was" : "s were"} covered automatically by an existing standing rule.`
      : "";
  return `New operating evidence has arrived: the panel now reads through the week of ${m.panelReadThrough}.${tail}`;
}

function stateWord(value) {
  return String(value || "unknown").replace(/_/g, " ");
}

function projectedBody(text) {
  const cleaned = String(text || "")
    .replace(/^Not run yet\.\s*/i, "")
    .replace(/^When it runs it starts from\s*/i, "Starts from ");
  return cleaned || "The prior Monday's open recommendations will be loaded before new analysis begins.";
}

function RecheckBody({ m, asOf }) {
  const counts = recheckCounts(m.rechecked);
  const outcome = outcomeClause(counts);
  const execution = executionClause(counts);
  const newCount = (m.created || []).length;

  if (m.isFirstRun) {
    const n = (m.created || []).length;
    return (
      <p>
        {n > 0
          ? `Nothing to re-check — this is the first Monday. ${n} recommendation${n === 1 ? "" : "s"} opened below ${n === 1 ? "is" : "are"} what next Monday grades.`
          : "Nothing to re-check — this is the first Monday. Anything this digest opens is graded next Monday."}
      </p>
    );
  }

  if (counts.total > 0) {
    return (
      <>
        <p>
          {counts.total} prior recommendation{counts.total === 1 ? "" : "s"} were re-checked
          against the new week{outcome ? ` — ${outcome}` : ""}.
          {execution
            ? ` Separately: ${execution}; metric movement never proves the work happened.`
            : ""}
          {newCount > 0
            ? ` ${newCount} recommendation${newCount === 1 ? " is" : "s are"} genuinely new this Monday.`
            : " Nothing genuinely new cleared the bar this Monday."}
        </p>
        <ul className="memory-result-lines">
          {m.rechecked.map((row) => (
            <li key={row.rec_id}>
              <strong>{row.center}</strong>: carried from {mondayShort(row.created_week || m.previousRun)}
              {" → "}re-checked {mondayShort(asOf)}
              {" → "}outcome {stateWord(row.outcome)}
              {" → "}execution {stateWord(row.execution)}
            </li>
          ))}
          {m.created.map((row) => (
            <li key={row.rec_id}>
              <strong>{row.center}</strong>: genuinely new this Monday
            </li>
          ))}
        </ul>
      </>
    );
  }

  return (
    <p>
      Prior memory is loaded. As the run reaches the ledger re-check, the carried
      recommendations will be graded against this week's evidence before the digest
      is finalized.
    </p>
  );
}

// The result half — what happened when inherited memory met this week's
// evidence. Lives on the ledger card in the console; stays inside the full
// band on the digest page.
export function RecheckBand({ carryIn, ledger, asOf }) {
  if (!carryIn && !ledger) {
    return (
      <div className="memory-recheck">
        <span className="memory-result-label">Re-check</span>
        <p>
          After this Monday runs, what happened to last week's recommendations
          lands here.
        </p>
      </div>
    );
  }

  const m = summarizeMemory({ carryIn, ledger, asOf });
  return (
    <div className="memory-recheck">
      <span className="memory-result-label">
        {m.isFirstRun ? "Re-check" : "Memory → new evidence → this week's digest"}
      </span>
      <RecheckBody m={m} asOf={asOf} />
    </div>
  );
}

export default function MemoryBand({
  carryIn,
  ledger,
  asOf,
  projected,
  variant = "full",
}) {
  const step0Only = variant === "step0";
  const m = summarizeMemory({ carryIn, ledger, asOf });

  if (projected) {
    const first = Boolean(projected.first);
    return (
      <div className={`memory-band pending${first ? " first" : ""}`}>
        <div className="memory-eyebrow">Step 0 · Durable memory</div>
        <div className="memory-head">
          {projected.head ||
            (first
              ? "Week 1 creates the memory that Week 2 will use."
              : "This Monday starts from last week's ledger.")}
        </div>
        <div className="memory-body">{projectedBody(projected.text)}</div>
      </div>
    );
  }

  if (m.isFirstRun) {
    const n = (m.created || []).length;
    return (
      <div className="memory-band first">
        <div className="memory-eyebrow">Step 0 · Durable memory</div>
        <div className="memory-head">
          Week 1 creates the memory that Week 2 will use.
        </div>
        <div className="memory-body">
          No prior recommendations exist yet, so there is nothing to re-check.{" "}
          {n > 0
            ? `This digest opens ${n} recommendation${n === 1 ? "" : "s"}; ${n === 1 ? "it is" : "they are"} written to the ledger with an owner and check-by date so next Monday starts by grading ${n === 1 ? "it" : "them"} against the evidence that arrives in between.`
            : "Anything this digest opens is written to the ledger with an owner and check-by date, then graded next Monday."}
        </div>
      </div>
    );
  }

  const corrections = correctionsLine(m);
  const evidence = evidenceLine(m);

  return (
    <div className="memory-band">
      <div className="memory-eyebrow">Step 0 · Durable memory loaded</div>
      <div className="memory-head">
        {m.previousRun
          ? `Building on ${mondayShort(m.previousRun)} — this is not a fresh report.`
          : "Building on the previous Monday — this is not a fresh report."}
      </div>
      <ul className="memory-lines">
        <li>{carriedLine(m)}</li>
        {corrections && <li>{corrections}</li>}
        {evidence && <li>{evidence}</li>}
      </ul>

      {!step0Only && (
        <div className="memory-result">
          <span className="memory-result-label">Memory → new evidence → this week's digest</span>
          <RecheckBody m={m} asOf={asOf} />
        </div>
      )}
    </div>
  );
}
