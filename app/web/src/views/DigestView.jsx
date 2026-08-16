import React, { useEffect, useState } from "react";
import {useSearchParams, Link } from "react-router-dom";
import { getJson } from "../api.js";
import SignalCard from "../components/SignalCard.jsx";
import VerdictTable from "../components/VerdictTable.jsx";
import LedgerSection from "../components/LedgerSection.jsx";
import DataChecks from "../components/DataChecks.jsx";
import SidePanel from "../components/SidePanel.jsx";
import MemoryBand from "../components/MemoryBand.jsx";
import {
  DEMO_MONDAYS,
  rememberWeek,
  selectedWeek,
} from "../selected-week.js";

// Dr. Priya's Monday view. Section order is locked (PLAN §5):
//   0. Step 0 — what this Monday inherited   1. Top signals
//   2. Claimed vs. Verified   3. Ledger
//   4. Bottom strip: "Data checks: N ran, M corrections" (click-deep)
//
// Step 0 comes FIRST for the same reason the run does it first: on a later
// Monday the digest is not a fresh report, it is the continuation of one, and
// a reader has to know that before reading a single signal.
// The UI computes nothing — every value is rendered verbatim from
// /api/digest/<asOf>, which serves the pipeline's digest.json.
//
// The thread panel rides along here too (it mounts in both views): the leader's
// follow-up questions belong next to the digest that provoked them, and they
// land in the same per-Monday thread the run narrated itself into.

export default function DigestView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const week = selectedWeek(searchParams.get("week"));

  const [digest, setDigest] = useState(null);
  const [error, setError] = useState(null);
  const [weeks, setWeeks] = useState([]);
  const [panelCollapsed, setPanelCollapsed] = useState(false);

  useEffect(() => {
    getJson("/api/weeks")
      .then((d) => setWeeks(d.weeks))
      .catch(() => setWeeks([]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    const load = () =>
      getJson(`/api/digest/${week}`)
        .then((d) => {
          if (cancelled) return;
          setDigest(d);
          setError(null);
          // A partial digest lights up on its own once the full run lands.
          if (d.partial) timer = setTimeout(load, 5000);
        })
        .catch((e) => {
          if (cancelled) return;
          setDigest(null);
          setError(e);
          // Nothing on disk yet: keep watching for the first artifacts.
          if (e.status === 404) timer = setTimeout(load, 5000);
        });

    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [week]);

  // Week choices: whatever the pipeline has produced, plus the default.
  const weekOptions = Array.from(
    new Set([...weeks.map((w) => w.as_of), ...DEMO_MONDAYS])
  ).sort();

  return (
    <main className="page page-wide">
      <div className="page-head">
        <div>
          <p className="eyebrow">Monday digest</p>
          <h1>{week}</h1>
          {digest && (
            <p className="page-sub">
              Prepared for {digest.leader} · {digest.centers.length} centers ·
              covering the week of {digest.latest_complete_week}
            </p>
          )}
        </div>
        <label className="week-select">
          Week
          <select
            value={week}
            onChange={(e) => {
              rememberWeek(e.target.value);
              setSearchParams({ week: e.target.value });
            }}
          >
            {weekOptions.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className={`work-shell ${panelCollapsed ? "panel-collapsed" : ""}`} style={{ marginTop: 26 }}>
        <div className="digest-column">
          {error && error.status === 404 && (
            <div className="digest-awaiting">
              <div className="digest-awaiting-title">
                This Monday&rsquo;s digest hasn&rsquo;t been prepared yet.
              </div>
              <p>
                Run the pipeline from the{" "}
                <Link to="/admin">AI Strategy Lead console</Link> and the digest
                will appear here on its own.
              </p>
            </div>
          )}
          {error && error.status !== 404 && (
            <div className="panel-error">{error.message}</div>
          )}

          {digest && digest.partial && (
            <div className="banner">
              Partial digest — {digest.partial_reason}
            </div>
          )}

          {digest && !digest.partial && (
            <MemoryBand
              carryIn={digest.carry_in}
              ledger={digest.ledger}
              asOf={digest.as_of || week}
            />
          )}

          {digest && (
            <>
              <section className="section card">
                <p className="eyebrow">What earns attention</p>
                <h2>Top signals</h2>
                <p className="section-note">
                  The few center-metric movements that earned a place this Monday,
                  ranked. Open a signal's receipts to see exactly which
                  calculations drove its rank.
                </p>
                {digest.top_signals.length === 0 ? (
                  <div className="panel-empty">
                    No signals cleared the bar this week.
                  </div>
                ) : (
                  digest.top_signals.map((s) => (
                    <SignalCard key={`${s.center}-${s.metric}`} signal={s} />
                  ))
                )}

                {digest.suppressed && digest.suppressed.length > 0 && (
                  <details className="quiet">
                    <summary>
                      Not shown: {digest.suppressed.length} signal
                      {digest.suppressed.length > 1 ? "s" : ""} held back — see why
                    </summary>
                    <div className="quiet-list">
                      {digest.suppressed.map((s, i) => (
                        <div className="item" key={i}>
                          <div className="head-line">
                            <strong>{s.center}</strong>
                            <span style={{ color: "var(--muted)" }}>
                              {s.metric_display}
                            </span>
                            <span className="badge neutral">{s.rule}</span>
                          </div>
                          <div className="reason">{s.reason}</div>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </section>

              <section className="section card">
                <p className="eyebrow">Every commitment, verified</p>
                <h2>Claimed vs. Verified</h2>
                <p className="section-note">
                  Each open action plan: the owner's reported status, what the
                  numbers actually say, and the AI's independent verdict.
                </p>
                <VerdictTable section={digest.claimed_vs_verified} />
              </section>

              <section className="section card">
                <p className="eyebrow">Closing the loop</p>
                <h2>Recommendation ledger</h2>
                <p className="section-note">
                  Grouped by where each one came from: carried in from the
                  previous Monday and re-checked against this week's evidence,
                  or opened for the first time today. One that did not work
                  comes back with the next move, not a shrug; you can close,
                  relaunch, escalate or dismiss any of them right here.
                </p>
                <LedgerSection
                  ledger={digest.ledger}
                  asOf={digest.as_of || week}
                  carryIn={digest.carry_in}
                />
              </section>

              <DataChecks dataChecks={digest.data_checks} harness={digest.harness} />
            </>
          )}
        </div>

        <SidePanel
          asOf={week}
          collapsed={panelCollapsed}
          onToggle={() => setPanelCollapsed(!panelCollapsed)}
        />
      </div>
    </main>
  );
}
