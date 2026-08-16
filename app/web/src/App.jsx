import React, { useState } from "react";
import { Routes, Route, NavLink } from "react-router-dom";
import { Analytics } from "@vercel/analytics/react";
import DigestView from "./views/DigestView.jsx";
import AdminView from "./views/AdminView.jsx";
import WorkflowView from "./views/WorkflowView.jsx";
import { postJson } from "./api.js";

// Start over, from every view — the demo's "run it again from zero" control.
//
// It lives in the masthead rather than on the admin page because it is not an
// admin-page action: it resets EVERY Monday and rotates the shared ledger, so
// the next run re-checks nothing a practice run left behind. Nothing is
// deleted — threads, consoles, run artifacts and the ledger are all archived.
//
// On success the page reloads. Every view holds its own copy of run state, and
// after this there is no run state left to hold; a reload is the honest way to
// show that rather than reconciling five components against an empty server.
function ResetEverything() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // No confirm dialog. Reset deletes nothing — every thread, console, run
  // artifact and ledger file moves into an archive folder — so a blocking
  // browser prompt guards against nothing and costs a click mid-demo.
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await postJson("/api/reset", { all: true });
      window.location.reload();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="masthead-actions">
      {error && <span className="masthead-error">{error}</span>}
      <button
        type="button"
        className="danger-ghost"
        onClick={run}
        disabled={busy}
        title="Archive every Monday's session and run output, stop any run in flight, archive the recommendation ledger, and rebuild DATA/TRANSLATION/ from DATA/INPUTS/ — a clean start, nothing deleted."
      >
        {busy ? "Resetting…" : "Reset"}
      </button>
    </div>
  );
}

export default function App() {
  return (
    <>
      <header className="masthead">
        <div className="masthead-inner">
          <div className="wordmark">
            <img src="/petfolk-logo.svg" alt="Petfolk" className="wordmark-logo" />
            <span className="wordmark-suffix">Monday Digest</span>
          </div>
          <nav>
            <NavLink to="/workflow" className={({ isActive }) => (isActive ? "active" : "")}>
              Workflow Diagram
            </NavLink>
            <NavLink to="/admin" className={({ isActive }) => (isActive ? "active" : "")}>
              AI Strategy Lead
            </NavLink>
            <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
              Monday Digest
            </NavLink>
          </nav>
          <ResetEverything />
        </div>
      </header>
      <Routes>
        <Route path="/" element={<DigestView />} />
        <Route path="/admin" element={<AdminView />} />
        <Route path="/workflow" element={<WorkflowView />} />
      </Routes>
      {/* Page-view analytics on the deployed site; a no-op when running locally. */}
      <Analytics />
    </>
  );
}
