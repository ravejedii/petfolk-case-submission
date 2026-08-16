import React from "react";
import { mondayShort } from "../memory.js";

// One top signal. Every value on screen comes verbatim from digest.json —
// this component formats layout, never numbers. Center names only; the
// digest's location_id fields are audit-trail data and are never rendered.
//
// A signal whose recommendation an EARLIER Monday opened says so on the card.
// Without that line a second Monday shows three ranked signals carrying three
// recommendations and reads as three independent discoveries, when two of them
// exist only because the system remembered and came back to grade them.
// `carried_forward` / `created_week` are set by pipeline/run.py:build_top_signals.

// The three sub-scores, opened on click ("receipts"): which calculations
// drove this rank, with the exact inputs the pipeline logged for each.
function Receipts({ signal }) {
  const { scores, weights_used: weights, contributions, peer_context: peer } = signal;
  const drift = scores.drift || {};
  const gap = scores.gap || {};
  const spike = scores.spike || {};

  const row = (name, s, weight, contribution, detail) => (
    <tr key={name}>
      <td>{name}</td>
      <td>{s.available ? String(s.score) : "n/a"}</td>
      <td>{String(weight)}</td>
      <td>{contribution === undefined ? "n/a" : String(contribution)}</td>
      <td className="detail">{detail}</td>
    </tr>
  );

  return (
    <details className="receipts">
      <summary>Receipts — the three scores behind this rank</summary>
      <div className="tape">
        <table>
          <thead>
            <tr>
              <th>Score</th>
              <th>Value</th>
              <th>Weight</th>
              <th>Contrib.</th>
              <th>What it compared</th>
            </tr>
          </thead>
          <tbody>
            {row(
              "Drift risk",
              drift,
              weights.drift,
              contributions.drift,
              drift.available
                ? `last ${drift.recent_weeks} wks ${String(drift.recent_level)} vs prior ${drift.baseline_weeks}-wk norm ${String(drift.baseline_level)} (change ${String(drift.change)}, scale ${String(drift.scale)})`
                : "not available for this metric"
            )}
            {row(
              "Gap to standard",
              gap,
              weights.gap,
              contributions.gap,
              gap.available
                ? `level ${String(gap.center_level)} vs ${gap.peer_group} median ${String(gap.peer_median)} — rank ${String(gap.rank_from_worst)} from worst of ${String(gap.peer_count)}`
                : "not available for this metric"
            )}
            {row(
              "Spike",
              spike,
              weights.spike,
              contributions.spike,
              spike.available
                ? `latest week ${String(spike.latest_value)} vs ${spike.baseline_weeks}-wk median ${String(spike.baseline_median)} (change ${String(spike.change)}, scale ${String(spike.scale)})`
                : "not available for this metric"
            )}
          </tbody>
        </table>
        <div className="foot">
          blended priority {String(signal.priority)}
          {peer
            ? ` · peer group: ${peer.peer_group} (median ${peer.peer_median_display}, ${String(peer.peer_count)} centers)`
            : ""}
        </div>
      </div>
    </details>
  );
}

export default function SignalCard({ signal }) {
  const action = signal.action;
  return (
    <article className="signal">
      <div className="signal-rank" aria-label={`rank ${signal.rank}`}>
        {String(signal.rank)}
      </div>
      <div className="signal-body">
        <div className="signal-title">
          <span className="center">{signal.center}</span>
          <span className="metric">{signal.metric_display}</span>
          <span className="signal-priority">priority {String(signal.priority)}</span>
        </div>

        <p className="signal-headline">{signal.headline}</p>

        {Array.isArray(signal.notes) && signal.notes.length > 0 && (
          <details className="cell-notes">
            <summary>
              {signal.notes.length} note{signal.notes.length > 1 ? "s" : ""}
            </summary>
            {signal.notes.map((n, i) => (
              <p key={i}>{n}</p>
            ))}
          </details>
        )}

        {action && (
          <div className={`action-box ${action.carried_forward ? "carried" : ""}`}>
            <div className="label">
              {action.carried_forward ? (
                <>
                  Carried from {mondayShort(action.created_week)}
                  {action.rechecked_this_run ? " · re-checked this Monday" : ""}
                </>
              ) : (
                "Do this — new this Monday"
              )}
            </div>
            <div>{action.recommendation}</div>
            <div className="action-meta">
              Owner {action.owner} · check by {action.check_by} ·{" "}
              {action.loop_state_display || action.status_display}
            </div>
          </div>
        )}

        <Receipts signal={signal} />
      </div>
    </article>
  );
}
