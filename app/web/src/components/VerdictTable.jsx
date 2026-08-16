import React from "react";

// "Claimed vs. Verified" — three columns per commitment:
// Reported Status · What the numbers say · AI Verdict.
// Rendered verbatim from digest.claimed_vs_verified. When the digest is
// partial (verdicts pending) the caller shows a pending panel instead.

const BUCKET_CLASS = {
  "NOT WORKING": "not-working",
  ABANDONED: "abandoned",
  EXCEEDED: "exceeded",
  "ON TRACK": "on-track",
};

// The pipeline writes one summary line (semicolons, then overdue/stale
// sentences). Split for display only — every fragment is still that string.
function numberBullets(summary) {
  return String(summary || "")
    .split(/;\s+/)
    .flatMap((part) => part.split(/(?<=\.)\s+(?=[A-Z])/))
    .map((s) => s.replace(/\.$/, "").trim())
    .filter(Boolean);
}

export default function VerdictTable({ section }) {
  if (!section || section.pending) {
    return (
      <div className="panel-empty">
        {section && section.message
          ? section.message
          : "Verdicts pending — run the pipeline to fill in this section."}
      </div>
    );
  }

  const { columns, rows, counts } = section;

  return (
    <>
      <div className="table-scroll">
        <table className="verdicts">
          <thead>
            <tr>
              <th>Center</th>
              {columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.plan_id}>
                <td className="plan-cell">
                  <div className="center">{r.center}</div>
                  <div className="metric">{r.metric_display}</div>
                </td>
                <td>
                  <span className="badge neutral">{r.reported_status.display}</span>
                  <div className="cell-meta">
                    {r.reported_status.owner}
                    <br />
                    last update {r.reported_status.last_update} (
                    {String(r.reported_status.staleness_days)} days before this
                    digest)
                  </div>
                </td>
                <td>
                  <ul className="numbers-bullets">
                    {numberBullets(r.what_the_numbers_say.summary).map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                  {r.what_the_numbers_say.notes &&
                    r.what_the_numbers_say.notes.length > 0 && (
                      <details className="cell-notes">
                        <summary>
                          {r.what_the_numbers_say.notes.length} note
                          {r.what_the_numbers_say.notes.length > 1 ? "s" : ""}
                        </summary>
                        {r.what_the_numbers_say.notes.map((n, i) => (
                          <p key={i}>{n}</p>
                        ))}
                      </details>
                    )}
                </td>
                <td>
                  <span
                    className={`badge ${BUCKET_CLASS[r.ai_verdict.bucket] || "neutral"}`}
                  >
                    {r.ai_verdict.display}
                  </span>
                  <p className="verdict-sentence">{r.ai_verdict.sentence}</p>
                  <p className="verdict-next">
                    <strong>Next:</strong> {r.ai_verdict.recommended_action}
                  </p>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {counts && (
        <p className="counts-line">
          {String(counts.plans)} plans —{" "}
          {Object.entries(counts.buckets)
            .filter(([, n]) => n > 0)
            .map(([b, n]) => `${b} ${n}`)
            .join(" · ")}{" "}
          · AI agrees with the reported status on {String(counts.agree)} of{" "}
          {String(counts.plans)}.
        </p>
      )}
    </>
  );
}
