import React, { useState, useEffect } from "react";

// The bottom strip: "Data checks: N ran, M corrections" — one click deep,
// exactly as the design locks it. Data-quality detail lives ONLY here, never
// in the digest sections above. The ⓘ opens the three-layer harness
// explanation (the checking machinery behind every AI sentence in the
// digest), plus the harness's own rule list and counts from this run.

function HarnessModal({ harness, onClose }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay" onClick={onClose} role="dialog" aria-modal="true">
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>How this digest is checked</h3>
        <p className="section-note" style={{ marginBottom: 0 }}>
          Three layers stand between the raw data and every sentence above.
        </p>

        <h4>1 · Deterministic facts</h4>
        <ul>
          <li>
            Code — no AI — computes each plan's baseline, target, 4-week
            actual, % of gap closed, trend direction, days overdue, and
            staleness of the last update.
          </li>
        </ul>

        <h4>2 · AI judgment</h4>
        <ul>
          <li>
            The AI gets those facts plus the owner's reported status, and
            independently rules agree/disagree with one plain-English sentence
            and one recommended action — every plan, every run.
          </li>
        </ul>

        <h4>3 · Harness — validates the reasoning, not just the numbers</h4>
        <ul>
          <li>
            Every figure in an AI sentence must exist in the fact table, or
            the sentence is rejected and regenerated.
          </li>
          <li>
            A deterministic rule table bounds conclusions — the AI adds
            nuance, never contradicts arithmetic.
          </li>
          <li>Every check pass, fail, and retry is written to a run log.</li>
        </ul>

        {harness && (
          <>
            <h4>This run</h4>
            <ul>
              {harness.rules.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
            <p className="mono-note">
              mode: {harness.mode} · {String(harness.checks_passed)} checks
              passed · {String(harness.checks_failed)} failed ·{" "}
              {String(harness.retries)} retries · log: {harness.runlog}
            </p>
          </>
        )}

        <div className="close-row">
          <button className="secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

export default function DataChecks({ dataChecks, harness }) {
  const [open, setOpen] = useState(false);
  const [showInfo, setShowInfo] = useState(false);

  if (!dataChecks) return null;

  const carried = Number(dataChecks.corrections_carried || 0);
  const newRequired = Number(dataChecks.corrections_new || 0);
  const providerRows = Number(
    dataChecks.provider_rows_under_standing_corrections || 0
  );
  const coveredRows = providerRows || Number(dataChecks.rows_under_standing_corrections || 0);
  const summary = carried
    ? `Data checks: ${dataChecks.n_ran} ran · ${carried} prior data-quality decision${carried === 1 ? "" : "s"} carried forward · ${newRequired} new decision${newRequired === 1 ? "" : "s"} required.`
    : `Data checks: ${dataChecks.n_ran} ran, ${dataChecks.corrections} corrections.`;

  return (
    <div>
      <div className="strip">
        <span className="data-checks-copy">
          <span>{summary}</span>
          {carried > 0 && coveredRows > 0 && (
            <span className="data-checks-standing">
              {coveredRows} newly arrived {providerRows ? "provider " : ""}row{coveredRows === 1 ? " was" : "s were"} covered automatically by an existing standing rule.
            </span>
          )}
        </span>
        <button className="linkish" onClick={() => setOpen(!open)}>
          {open ? "Hide details" : "Details"}
        </button>
        <button
          className="info-btn"
          title="How this digest is checked"
          aria-label="How this digest is checked"
          onClick={() => setShowInfo(true)}
        >
          i
        </button>
      </div>

      {open && (
        <div className="quiet-list" style={{ marginTop: 12 }}>
          {dataChecks.plain_english.map((line, i) => (
            <div className="item" key={i}>
              {line}
            </div>
          ))}
        </div>
      )}

      {showInfo && (
        <HarnessModal harness={harness} onClose={() => setShowInfo(false)} />
      )}
    </div>
  );
}
