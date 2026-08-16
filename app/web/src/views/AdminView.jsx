import React, { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getJson, postJson, postFiles, subscribeEvents } from "../api.js";
import SidePanel from "../components/SidePanel.jsx";
import LedgerSection from "../components/LedgerSection.jsx";
import MemoryBand, { RecheckBand } from "../components/MemoryBand.jsx";
import { PHASES, PHASE_TITLE } from "../phases.js";
import { mondayShort } from "../memory.js";
import {
  DEMO_MONDAYS,
  rememberWeek,
  selectedWeek,
} from "../selected-week.js";

// The AI Strategy Lead view: drop the 4 CSVs in, run the pipeline for a
// Monday, and watch the real phases execute as a vertical stepper that
// mirrors the pipeline order in docs/PIPELINE.md — phase sections top to bottom
// with gate checks between them. Every line in every section is a real
// event — pipeline stdout/stderr, its own run-log entries, or an artifact
// landing on disk. If the pipeline is silent, the feed is silent.
//
// Feed language: plain English by default (translated server-side in ONE
// place, app/server/lib/plain.js, from the pipeline's own event fields);
// the Engineer view toggle shows the raw lines instead.

// The console opens on the FIRST Monday, not the latest one. This page exists to
// run the sequence, and the sequence starts here: 2026-04-27 is what opens the
// recommendations that 2026-05-04 then grades. Landing on the later Monday
// invites running it first, which produces a digest with nothing to re-check.
// The Monday Digest shares this selection so the two views never describe
// different Mondays while the reviewer moves between them.
// The rail, from the client's one phase map (../phases.js) — the Agent panel
// labels its narration bubbles from the same map, so the row a reader is
// looking at and the bubble explaining it always carry the same name.
const PHASE_LABELS = PHASES.map((key) => [key, PHASE_TITLE[key]]);

// The Monday before this one in the sequence, or null for the first run.
function previousMonday(week) {
  const earlier = DEMO_MONDAYS.filter((m) => m < week);
  return earlier.length ? earlier[earlier.length - 1] : null;
}

const EXPECTED = ["locations", "clinic_weekly", "provider_weekly", "action_plans"];

function UploadCard({ upload, onUpload }) {
  const [over, setOver] = useState(false);
  const [error, setError] = useState(null);
  // Uploading your own extracts is the exception, not the way in: the console
  // runs on the repo's CSVs. Collapsed, it is one line; the dropzone opens on
  // request and stays open once a session has files in it.
  const [open, setOpen] = useState(false);
  const inputRef = useRef(null);

  const send = async (files) => {
    setError(null);
    try {
      // Repeat drops add to the same upload session (one staging dir).
      const qs = upload && upload.upload_id
        ? `?uploadId=${encodeURIComponent(upload.upload_id)}`
        : "";
      onUpload(await postFiles(`/api/upload${qs}`, Array.from(files)));
    } catch (e) {
      setError(e.message);
    }
  };

  const tables = upload && upload.tables ? upload.tables : {};
  const rejected = upload
    ? upload.received.filter((r) => r.rejected)
    : [];

  const staged = EXPECTED.filter((t) => tables[t]).length;
  const expanded = open || Boolean(upload);

  if (!expanded) {
    return (
      <div className="card intake-collapsed">
        <div>
          <h2>1 · Data intake</h2>
          <p className="sub" style={{ marginBottom: 0 }}>
            Running on the four CSVs in the repo. Files are recognized by their
            header columns, never their names.
          </p>
        </div>
        <button type="button" className="secondary" onClick={() => setOpen(true)}>
          Use my own files
        </button>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="intake-head">
        <h2>1 · Data intake</h2>
        {!upload && (
          <button type="button" className="link-button" onClick={() => setOpen(false)}>
            Cancel
          </button>
        )}
      </div>
      <p className="sub">
        Drop the four weekly extracts. Each is recognized by its header columns,
        staged under its canonical name, and the raw DATA/INPUTS/ folder is never
        touched — with all four staged the run computes on your files.
      </p>
      <div
        className={`dropzone ${over ? "over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (e.dataTransfer.files.length) send(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current && inputRef.current.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" && inputRef.current) inputRef.current.click();
        }}
      >
        Drag the CSVs here, or click to choose files
        <input
          ref={inputRef}
          type="file"
          accept=".csv"
          multiple
          hidden
          onChange={(e) => e.target.files.length && send(e.target.files)}
        />
      </div>

      <div className="slots">
        {EXPECTED.map((t) => (
          <div key={t} className={`slot ${tables[t] ? "got" : ""}`}>
            <span>{tables[t] ? "✓" : "·"}</span>
            {t}.csv
          </div>
        ))}
      </div>

      {error && (
        <div className="panel-error" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
      {rejected.length > 0 && (
        <div className="panel-error" style={{ marginTop: 12 }}>
          {rejected
            .map((r) => `${r.filename} rejected — ${r.reason}`)
            .join(" ")}
        </div>
      )}
      {upload && (
        <div className="upload-note">
          Recognized {staged}/4 tables by their header columns →{" "}
          {upload.staged_to}.{" "}
          {upload.missing.length > 0
            ? `Still missing: ${upload.missing.join(", ")} — drop them here to add to this session.`
            : "All four tables recognized."}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Derivations — everything on screen comes from real run events.
// ---------------------------------------------------------------------------

const HARNESS_CHECK_EVENTS = new Set([
  "facts_summary_number_check",
  "number_check",
  "reasoning_check",
]);

function deriveRun(status) {
  const events = (status && status.events) || [];
  const byPhase = { validation: [], signals: [], verdicts: [], digest: [] };
  let lastPhase = "validation";
  const plans = new Map(); // plan_id -> row
  let plansExpected = null;
  let signalsSummary = null;
  let verdictsSummary = null;
  let harnessTotal = 0;
  let harnessPassed = 0;
  let retries = 0;
  let ledgerDone = false;

  for (const e of events) {
    const phase = e.phase && byPhase[e.phase] ? e.phase : lastPhase;
    lastPhase = phase;
    byPhase[phase].push(e);

    const m = e.meta;
    if (!m) continue;
    if (m.step === "signals" && m.event === "summary") signalsSummary = m.counts || null;
    if (m.step === "ledger" && m.event === "run_summary") ledgerDone = true;
    if (m.step === "verdicts") {
      if (m.event === "run_start") plansExpected = m.plans || null;
      if (m.event === "summary") verdictsSummary = m.counts || null;
      if (m.event === "retry") retries += 1;
      if (HARNESS_CHECK_EVENTS.has(m.event)) {
        harnessTotal += 1;
        if (m.result === "pass") harnessPassed += 1;
      }
      if (m.plan_id) {
        if (!plans.has(m.plan_id)) {
          plans.set(m.plan_id, {
            id: m.plan_id,
            label: m.plan_label || null,
            state: "pending",
            bucket: null,
            mode: null,
            factsPass: false,
            numberPass: false,
            reasoningPass: false,
          });
        }
        const p = plans.get(m.plan_id);
        if (m.plan_label) p.label = m.plan_label;
        if (m.event === "facts_summary_number_check" && m.result === "pass")
          p.factsPass = true;
        if (m.event === "llm_call") {
          p.state = "generating";
          p.mode = m.mode || null;
        }
        if (m.event === "number_check" && m.result === "pass") p.numberPass = true;
        if (m.event === "reasoning_check" && m.result === "pass") {
          p.reasoningPass = true;
          if (p.state === "generating") p.state = "checked";
        }
        if (m.event === "verdict") {
          p.state = "done";
          p.bucket = m.bucket || null;
        }
      }
    }
  }

  return {
    byPhase,
    planRows: Array.from(plans.values()),
    plansExpected,
    signalsSummary,
    verdictsSummary,
    harnessTotal,
    harnessPassed,
    retries,
    ledgerDone,
  };
}

// A finished phase is reported to a tenth of a second: several of these steps
// are genuinely sub-second, and rounding an honest 0.4s down to "0s" reads as
// a broken clock rather than as a fast pipeline.
function fmtElapsed(ms, precise) {
  if (ms == null || ms < 0) return null;
  if (precise && ms < 10000) return ms < 100 ? "<0.1s" : `${(ms / 1000).toFixed(1)}s`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function phaseElapsed(times, key, now) {
  const t = times && times[key];
  if (!t || !t.startedAt) return null;
  const done = Boolean(t.endedAt);
  const end = done ? Date.parse(t.endedAt) : now;
  return fmtElapsed(end - Date.parse(t.startedAt), done);
}

// ---------------------------------------------------------------------------
// Stepper pieces
// ---------------------------------------------------------------------------

function GateChip({ state, label }) {
  // state: "pending" | "awaiting" | "passed"
  return (
    <div className={`gate ${state}`}>
      <span className="gate-diamond" aria-hidden="true" />
      <span className="gate-label">GATE: {label}</span>
    </div>
  );
}

function FeedLines({ events, engineerView, feedRef }) {
  const translated = engineerView ? events : events.filter((e) => e.plain);
  // A phase that did work must never render as an empty black box. If nothing
  // in it has a plain-English rendering (a stack trace, a log line this build
  // has no translation for), the raw lines are shown as they were written
  // rather than the feed pretending the phase was silent.
  const rawFallback = translated.length === 0 && events.length > 0;
  const lines = rawFallback ? events : translated;
  const showRaw = engineerView || rawFallback;
  return (
    <div className="log-feed" ref={feedRef || undefined}>
      {lines.length === 0 ? (
        <div className="empty">No events yet for this phase.</div>
      ) : (
        <>
          {rawFallback && (
            <div className="empty">
              {events.length} engineer-level line{events.length === 1 ? "" : "s"} — shown
              exactly as the pipeline wrote them.
            </div>
          )}
          {lines.map((e, i) => (
            <div key={i} className={`line ${e.source === "stderr" ? "stderr" : ""}`}>
              <span className="ts">{e.ts.slice(11, 19)}</span>
              <span className="tag">[{showRaw ? e.source : e.phase || e.source}]</span>
              {showRaw ? (
                e.text
              ) : /^Correction C\d+ (applied|declined)/.test(e.plain || "") ? (
                <>
                  <span className="feed-decision-tag">your decision</span> {e.plain}
                </>
              ) : (
                e.plain
              )}
            </div>
          ))}
        </>
      )}
    </div>
  );
}

const BUCKET_CLASS = {
  EXCEEDED: "exceeded",
  "ON TRACK": "on-track",
  "NOT WORKING": "not-working",
  ABANDONED: "abandoned",
};

// The tier that actually wrote the verdict sentences, from the run log's own
// llm_call events — never a hardcoded vendor (production runs on OpenAI, a
// laptop on the Claude CLI, tests on the deterministic template).
const WRITER_NAME = {
  "claude-cli": "Claude",
  api: "Claude",
  openai: "OpenAI",
  template: "deterministic template",
};

function verdictWriter(planRows) {
  const modes = new Set(planRows.map((p) => p.mode).filter(Boolean));
  if (modes.size === 0) return "pending";
  return Array.from(modes)
    .map((m) => WRITER_NAME[m] || m)
    .join(" + ");
}

function VerdictSubflow({ derived, phaseState }) {
  const { planRows, plansExpected, harnessTotal, harnessPassed, retries, ledgerDone } =
    derived;
  const factsChecked = planRows.filter((p) => p.factsPass).length;
  const factsDone = plansExpected != null && factsChecked >= plansExpected;
  const anyStarted = planRows.length > 0;

  const planStatus = (p) => {
    if (p.state === "done")
      return (
        <span className={`badge ${BUCKET_CLASS[p.bucket] || ""}`}>{p.bucket} ✓</span>
      );
    if (p.state === "checked") return <span className="plan-note ok">checked ✓</span>;
    if (p.state === "generating")
      return (
        <span className="plan-note gen">
          {p.mode === "template" ? "writing…" : `${WRITER_NAME[p.mode] || "model"} writing…`}
        </span>
      );
    return <span className="plan-note">fact sheet ready</span>;
  };

  return (
    <div className="subflow">
      <div className={`subrow det ${factsDone ? "ok" : ""}`}>
        <span className="submark">{factsDone ? "✓" : anyStarted ? "…" : "·"}</span>
        <div>
          <div className="subtitle">
            Plan Facts + Harness Rules <span className="subtag">deterministic</span>
          </div>
          <div className="subnote">
            {plansExpected != null
              ? `${factsChecked}/${plansExpected} fact sheets number-checked`
              : "waiting for the fact tables"}
          </div>
        </div>
      </div>
      <div className="subarrow" aria-hidden="true">↓</div>
      <div className="subrow ai">
        <div className="subtitle">
          Verdict Narrative{" "}
          <span className="subtag ai">AI / LLM · {verdictWriter(planRows)}</span>
          <span className="subnote-inline">
            {harnessTotal > 0 &&
              ` harness ${harnessPassed}/${harnessTotal} passed · ${retries} retries`}
          </span>
        </div>
        {planRows.length > 0 && (
          <div className="plan-grid">
            {planRows.map((p) => (
              <div key={p.id} className={`plan-row ${p.state}`}>
                <span className={`plan-dot ${p.state}`} aria-hidden="true" />
                <span className="plan-label">{p.label || p.id}</span>
                {planStatus(p)}
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="subarrow" aria-hidden="true">↓</div>
      <div className={`subrow det ${ledgerDone ? "ok" : ""}`}>
        <span className="submark">
          {ledgerDone || phaseState === "done" ? "✓" : "·"}
        </span>
        <div>
          <div className="subtitle">
            Ledger + Orchestrator <span className="subtag">deterministic</span>
          </div>
          <div className="subnote">
            re-checks every open recommendation, then assembles the digest
          </div>
        </div>
      </div>
    </div>
  );
}

function PhaseSection({
  title,
  state, // pending | active | done | failed
  awaiting,
  elapsed,
  headline,
  events,
  engineerView,
  expanded,
  onToggle,
  feedRef,
  children,
}) {
  const stateWord =
    state === "done"
      ? "done"
      : state === "failed"
        ? "failed"
        : state === "active"
          ? awaiting
            ? "waiting for you"
            : "running"
          : "pending";
  return (
    <section className={`phase-step ${state} ${awaiting ? "awaiting" : ""}`}>
      <button type="button" className="phase-head" onClick={onToggle} aria-expanded={expanded}>
        <span className={`phase-dot ${state} ${awaiting ? "awaiting" : ""}`} aria-hidden="true" />
        <span className="phase-title">{title}</span>
        {headline && <span className="phase-stat">{headline}</span>}
        <span className="phase-meta">
          {elapsed && <span className="phase-elapsed">{elapsed}</span>}
          <span className={`phase-state ${state}`}>{stateWord}</span>
          <span className="phase-caret">{expanded ? "▾" : "▸"}</span>
        </span>
      </button>
      {expanded && (
        <div className="phase-body">
          {children}
          <FeedLines events={events} engineerView={engineerView} feedRef={feedRef} />
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------

// Step 0 — what this Monday inherited, BEFORE the run has one of its own.
//
// Once a Monday has run, its memory state is the Step 0 it actually recorded
// (digest.carry_in, from `runctx.build_carry_in()`) and MemoryBand renders that
// directly. Until then there is no run to report, so the console says what this
// Monday WILL start from, read from the previous Monday's own digest — never an
// invented empty carry-in, and never a number re-derived here.
function projectedMemory(prev, prevLedger, week) {
  if (!prev) {
    return {
      first: true,
      head: "Week 1 creates the memory that Week 2 will use.",
      text:
        "No earlier Monday has run, so there is nothing to carry in. " +
        "What this digest opens is written to the ledger and re-checked next Monday.",
    };
  }
  const head = `This Monday starts from ${mondayShort(prev)}.`;
  if (!prevLedger) {
    return {
      head,
      text: `No output from ${mondayShort(prev)} yet. Run that Monday first — its recommendations are what this one re-checks.`,
    };
  }
  const open = prevLedger.open || [];
  if (open.length === 0) {
    return { head, text: `${mondayShort(prev)} left nothing open — this run starts clean.` };
  }
  const due = open.filter((r) => (r.check_by || "") <= week).length;
  return {
    head,
    text:
      `Not run yet. When it runs it starts from ${mondayShort(prev)}: ` +
      `${open.length} recommendation${open.length === 1 ? "" : "s"} still open` +
      `${due ? `, ${due} due this Monday` : ""} — re-checked before anything new is written.`,
  };
}

// Validation's report total includes standing corrections that were already
// decided on an earlier Monday. The console copy must use the classified run
// state (or Step 0 once it lands), never that raw object count.
function correctionMemory(status, carryIn) {
  const data = carryIn && carryIn.new_data;
  const number = (value) => (typeof value === "number" ? value : null);
  const carriedRows = Array.isArray(status && status.correctionsCarried)
    ? status.correctionsCarried
    : null;
  const newRows = Array.isArray(status && status.corrections)
    ? status.corrections
    : null;
  return {
    carried: number(data && data.corrections_carried) ??
      (carriedRows ? carriedRows.length : null),
    newRequired: number(data && data.corrections_new) ??
      (newRows ? newRows.length : null),
    providerRowsCovered: number(
      data && data.provider_rows_under_standing_corrections
    ),
    rowsCovered: number(data && data.rows_under_standing_corrections) ??
      (carriedRows
        ? carriedRows.reduce((sum, row) => sum + Number(row.new_rows || 0), 0)
        : null),
  };
}

function validationHeadline(validation, memory, decision) {
  if (!validation) return null;
  const lead = `${validation.checks_run} checks ran`;
  if (memory.carried > 0) {
    const covered = memory.providerRowsCovered ?? memory.rowsCovered;
    return (
      `${lead} · ${memory.carried} prior data-quality decision${memory.carried === 1 ? "" : "s"} carried forward` +
      ` · ${memory.newRequired ?? 0} new decision${memory.newRequired === 1 ? "" : "s"} required` +
      (covered > 0 ? ` · standing rules covered ${covered} newly arrived provider rows` : "")
    );
  }
  if (memory.newRequired !== null) {
    const n = memory.newRequired;
    if (!decision) return `${lead} · ${n} new decision${n === 1 ? "" : "s"} required`;
    if (decision.declined.length) {
      return `${lead} · ${n} new decisions resolved · ${decision.accepted.length} accepted, ${decision.declined.length} declined`;
    }
    return `${lead} · ${n} new decision${n === 1 ? "" : "s"} resolved`;
  }
  return lead;
}

function validationGate(memory, decision, awaiting) {
  if (awaiting) {
    const n = memory.newRequired ?? 0;
    return {
      state: "awaiting",
      label: `data quality — ${n} new decision${n === 1 ? "" : "s"} awaiting your approval`,
    };
  }
  if (memory.carried > 0 && memory.newRequired === 0) {
    return {
      state: "passed",
      label:
        `data-quality memory — ${memory.carried} prior decision${memory.carried === 1 ? "" : "s"} carried forward` +
        " · 0 new decisions required",
    };
  }
  if (decision) {
    const accepted = decision.accepted.length;
    const declined = decision.declined.length;
    return {
      state: "passed",
      label: declined
        ? `data quality — ${accepted} accepted, ${declined} declined`
        : `data quality — ${accepted} new decision${accepted === 1 ? "" : "s"} accepted`,
    };
  }
  return { state: "pending", label: "data quality — waiting for the checks" };
}

export default function AdminView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [health, setHealth] = useState(null);
  const week = selectedWeek(searchParams.get("week"));
  const [upload, setUpload] = useState(null);
  const [status, setStatus] = useState(null);
  const [runError, setRunError] = useState(null);
  const [polling, setPolling] = useState(false);
  const [engineerView, setEngineerView] = useState(false);
  const [autoAccept, setAutoAccept] = useState(false);
  const [expandOverride, setExpandOverride] = useState({});
  const [panelCollapsed, setPanelCollapsed] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [ledger, setLedger] = useState(null);
  // This Monday's completed digest copy of Step 0. During a live run the same
  // record arrives earlier through status.carryIn, straight from run_context.
  const [runCarryIn, setRunCarryIn] = useState(null);
  // The previous Monday's ledger, read once per week selection: what this run
  // will inherit. Null until it loads, and null forever on the first Monday.
  const [prevLedger, setPrevLedger] = useState(null);
  const activeFeedRef = useRef(null);

  const setWeek = (nextWeek) => {
    rememberWeek(nextWeek);
    setSearchParams({ week: nextWeek });
  };

  const uploadReady = Boolean(upload && upload.complete);

  useEffect(() => {
    getJson("/api/health").then(setHealth).catch(() => setHealth(null));
  }, []);

  // The console is scoped to the selected Monday, exactly like the thread panel
  // beside it: a live run when it is this Monday's, otherwise this Monday's last
  // run restored from disk. Switching weeks switches both surfaces together.
  useEffect(() => {
    let cancelled = false;
    getJson(`/api/run/status?asOf=${encodeURIComponent(week)}`)
      .then((s) => {
        if (cancelled) return;
        setStatus(s);
        if (s.running) setPolling(true);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [week]);

  // The live console. The server pushes every line the moment it sees it
  // (console_event) and the rail's state whenever it changes (run_state), on
  // the same channel the narration streams over — so the phase boxes fill line
  // by line while the panel narrates, instead of jumping one poll at a time.
  // The poll below stays as the reconciliation path: it returns the whole feed,
  // so a line that arrived here is simply replaced by the server's own copy.
  useEffect(() => {
    return subscribeEvents(week, (ev) => {
      if (!ev || typeof ev.type !== "string") return;
      if (ev.type === "console_event" && ev.event) {
        setStatus((prev) => {
          if (!prev || (prev.asOf && prev.asOf !== week)) return prev;
          const events = prev.events || [];
          const e = ev.event;
          // The snapshot may already carry this line: never show it twice.
          if (events.some((x) => x.ts === e.ts && x.text === e.text)) return prev;
          return { ...prev, started: true, events: [...events, e] };
        });
        setNow(Date.now());
        return;
      }
      if (ev.type === "run_state") {
        setStatus((prev) => {
          if (!prev || (prev.asOf && prev.asOf !== week)) return prev;
          return {
            ...prev,
            started: true,
            phases: ev.phases || prev.phases,
            phaseTimes: ev.phaseTimes || prev.phaseTimes,
            awaitingCorrections: Boolean(ev.awaitingCorrections),
            running: Boolean(ev.running),
            finishedAt: ev.finishedAt ?? prev.finishedAt,
            exitCode: ev.exitCode ?? prev.exitCode,
            error: ev.error ?? null,
            carryIn: Object.prototype.hasOwnProperty.call(ev, "carryIn")
              ? ev.carryIn
              : prev.carryIn,
          };
        });
        setNow(Date.now());
        // A run started somewhere else (another tab, the API) is followed here.
        if (ev.running) setPolling(true);
      }
    });
  }, [week]);

  useEffect(() => {
    if (!polling) return undefined;
    const t = setInterval(async () => {
      try {
        const s = await getJson(`/api/run/status?asOf=${encodeURIComponent(week)}`);
        setStatus(s);
        setNow(Date.now());
        if (!s.running) setPolling(false);
      } catch {
        // server briefly unreachable; keep polling
      }
    }, 700);
    return () => clearInterval(t);
  }, [polling, week]);

  const running = Boolean(status && status.running);
  const finished = Boolean(status && status.started && !status.running);

  // The ledger section this Monday produced — reloaded when a run finishes, so
  // the successors it just wrote (and the decisions taken on them) are here on
  // the run page too, not only in the digest.
  const runFinishedAt = status && status.finishedAt;
  useEffect(() => {
    let cancelled = false;
    getJson(`/api/digest/${week}`)
      .then((d) => {
        if (cancelled) return;
        setLedger(d.ledger || null);
        // A partial digest has no run behind it yet, so it has no Step 0.
        setRunCarryIn(d.partial ? null : d.carry_in || null);
      })
      .catch(() => {
        if (cancelled) return;
        setLedger(null);
        setRunCarryIn(null);
      });
    return () => {
      cancelled = true;
    };
  }, [week, runFinishedAt]);

  // What the previous Monday left open — reloaded when this Monday's run
  // finishes too, because a re-run of the earlier week changes what this one
  // inherits.
  const prevWeek = previousMonday(week);
  useEffect(() => {
    if (!prevWeek) {
      setPrevLedger(null);
      return undefined;
    }
    let cancelled = false;
    getJson(`/api/digest/${prevWeek}`)
      .then((d) => {
        if (!cancelled) setPrevLedger((d && d.ledger) || null);
      })
      .catch(() => {
        // That Monday has not run yet: the card says so rather than inventing
        // an empty carry-in.
        if (!cancelled) setPrevLedger(null);
      });
    return () => {
      cancelled = true;
    };
  }, [prevWeek, runFinishedAt]);
  const phases = status && status.phases ? status.phases : {};
  const awaiting = Boolean(status && status.awaitingCorrections);
  const derived = useMemo(() => deriveRun(status), [status]);

  // Keep the running phase's feed pinned to its latest line.
  const eventCount = status && status.events ? status.events.length : 0;
  useEffect(() => {
    if (activeFeedRef.current)
      activeFeedRef.current.scrollTop = activeFeedRef.current.scrollHeight;
  }, [eventCount]);

  const startRun = async () => {
    setRunError(null);
    setExpandOverride({});
    try {
      const body = { asOf: week };
      if (uploadReady) body.uploadId = upload.upload_id;
      if (autoAccept) body.autoAccept = true;
      await postJson("/api/run", body);
      setLedger(null);
      setRunCarryIn(null);
      setPolling(true);
    } catch (e) {
      setRunError(e.message);
    }
  };

  const weekOptions = Array.from(
    new Set([...DEMO_MONDAYS, ...(health ? health.weeks.map((w) => w.as_of) : [])])
  ).sort();

  // Headline stat per phase — every number comes from real run events/files.
  const v = status && status.validation;
  const decision = status && status.correctionsDecision;
  const statusCarryIn =
    status && status.asOf === week ? status.carryIn || null : null;
  const activeCarryIn = statusCarryIn || runCarryIn;
  const correctionState = correctionMemory(status, activeCarryIn);
  const headlines = {
    validation: validationHeadline(v, correctionState, decision),
    signals:
      derived.signalsSummary &&
      `${derived.signalsSummary.ranked} signals earned attention, ${derived.signalsSummary.suppressed} suppressed`,
    verdicts:
      derived.verdictsSummary &&
      `${derived.verdictsSummary.plans} verdicts · ${derived.harnessTotal} harness checks · ${derived.retries} retries`,
    digest: phases.digest === "done" ? "digest.json ready" : null,
  };

  // Gate chips between phases — the diagram's green diamonds, with the real
  // condition each one enforced.
  const gates = {
    validation: validationGate(correctionState, decision, awaiting),
    signals: derived.signalsSummary
      ? {
          state: "passed",
          label: `suppression applied — ${derived.signalsSummary.ranked} ranked, ${derived.signalsSummary.suppressed} held back`,
        }
      : { state: "pending", label: "suppression — pending" },
    verdicts:
      phases.verdicts === "done"
        ? {
            state: "passed",
            label: `harness — ${derived.harnessPassed}/${derived.harnessTotal} checks passed, ${derived.retries} retries`,
          }
        : derived.harnessTotal > 0
          ? {
              state: "pending",
              label: `harness — ${derived.harnessPassed}/${derived.harnessTotal} checks so far`,
            }
          : { state: "pending", label: "harness — pending" },
  };

  const isExpanded = (key) => {
    if (key in expandOverride) return expandOverride[key];
    return phases[key] === "active" || phases[key] === "failed";
  };
  const toggle = (key) =>
    setExpandOverride({ ...expandOverride, [key]: !isExpanded(key) });

  // One Monday drives both surfaces. The console fetches status for `week` and
  // the thread panel reads that same Monday's thread, so the two can no longer
  // describe different runs.
  const threadAsOf = week;

  return (
    <main className="page page-wide">
      <div className="page-head">
        <div>
          <p className="eyebrow">AI Strategy Lead</p>
          <h1>Pipeline console</h1>
          <p className="page-sub">
            Run the real pipeline for a Monday and watch each phase work. The
            panel on the right narrates the run and takes your decisions.
          </p>
        </div>
      </div>

      <div className={`work-shell ${panelCollapsed ? "panel-collapsed" : ""}`} style={{ marginTop: 26 }}>
      <div className="admin-grid">
        <div className="run-bar">

          {health && !health.pipeline_present && (
            <div className="banner" style={{ marginBottom: 14 }}>
              pipeline/run.py is not on disk yet — Run will report an error
              until the pipeline is built.
            </div>
          )}

          <div className="run-row">
            <label className="week-select" style={{ marginLeft: 0 }}>
              Digest Monday
              <select value={week} onChange={(e) => setWeek(e.target.value)}>
                {weekOptions.map((w) => (
                  <option key={w} value={w}>
                    {w}
                  </option>
                ))}
              </select>
            </label>
            <button className="primary" onClick={startRun} disabled={running}>
              {running ? (awaiting ? "Paused…" : "Running…") : "Run pipeline"}
            </button>
            <label className="check-label">
              <input
                type="checkbox"
                checked={autoAccept}
                onChange={(e) => setAutoAccept(e.target.checked)}
                disabled={running}
              />
              Auto-accept corrections (no pause)
            </label>
            {/* Reset moved to the masthead — it resets every Monday and the
                shared ledger, which was never a this-page-only action. */}
          </div>

          {status && status.started && !running && (
            <div className="run-state-strip">
              <span className={`state-dot ${status.exitCode === 0 ? "ok" : "bad"}`} aria-hidden="true" />
              <span className="state-text">
                {status.exitCode === 0
                  ? `${week} ran ${status.finishedAt ? status.finishedAt.slice(11, 16) + " UTC" : "earlier"} — four phases complete`
                  : `${week}'s last run did not finish`}
              </span>
              {status.exitCode === 0 && (
                <Link className="state-link" to={`/?week=${week}`}>
                  Open the digest it produced →
                </Link>
              )}
            </div>
          )}

          {uploadReady && (
            <p className="run-source">
              Running on your uploaded files (4/4 recognized) — Data Validation
              &amp; Check runs on them first.
            </p>
          )}

          {runError && (
            <div className="panel-error" style={{ marginTop: 12 }}>
              {runError}
            </div>
          )}
        </div>
        <UploadCard upload={upload} onUpload={setUpload} />


        <div className="card">
          <div className="progress-head">
            <div>
              <h2>2 · Progress</h2>
              <p className="sub" style={{ marginBottom: 0 }}>
                Real events only — the pipeline's own output and run logs.
              </p>
            </div>
            <label className="check-label eng-toggle">
              <input
                type="checkbox"
                checked={engineerView}
                onChange={(e) => setEngineerView(e.target.checked)}
              />
              Engineer view
            </label>
          </div>

          {/* Step 0, above the four phase rows: the same band the digest page
              opens with, so the console and the digest tell one story. Once
              this Monday has run it is that run's OWN recorded carry-in; before
              then, what it will start from. */}
          {activeCarryIn ? (
            <MemoryBand
              variant="step0"
              carryIn={activeCarryIn}
              ledger={ledger}
              asOf={week}
            />
          ) : (
            <MemoryBand
              variant="step0"
              projected={projectedMemory(prevWeek, prevLedger, week)}
            />
          )}

          {!status || !status.started ? (
            <div className="log-feed" style={{ marginTop: 14 }}>
              <div className="empty">
                {(status && status.message) ||
                  `No run recorded for ${week} yet. Start one above.`}
              </div>
            </div>
          ) : (
            <div className="stepper" style={{ marginTop: 14 }}>
              {status.restored && (status.interrupted || status.reconstructed || (health && health.hosted)) && (
                <div className="banner" style={{ marginBottom: 12 }}>
                  {status.interrupted
                    ? `Restored from ${week}'s last run, which was interrupted before it finished — run it again for a complete digest.`
                    : status.reconstructed
                      ? `Rebuilt from ${week}'s run logs in DATA/OUTPUTS/${week}/ (finished ${status.finishedAt ? status.finishedAt.slice(0, 16).replace("T", " ") : "earlier"} UTC) — the pipeline's own record of the run the panel beside this describes.`
                      : `This is ${week}'s last run, restored — the same run the panel beside this describes.`}
                  {/* Hosted honesty: a run's artifacts live with the server
                      instance that produced them, so a reload can land on a
                      fresh one and find only the committed runs. Say so rather
                      than let a reviewer wonder where their run went. */}
                  {health && health.hosted && !status.interrupted && (
                    <div className="banner-note">
                      On this hosted deployment a run stays with the server that
                      produced it — reload the page and you may land on a fresh
                      one, which serves the last committed run instead. Run it
                      again to see it live.
                    </div>
                  )}
                </div>
              )}
              {PHASE_LABELS.map(([key, label], i) => (
                <React.Fragment key={key}>
                  <PhaseSection
                    title={
                      key === "digest"
                        ? `${label} · ${mondayShort(week)}`
                        : label
                    }
                    state={phases[key] || "pending"}
                    awaiting={key === "validation" && awaiting}
                    elapsed={phaseElapsed(status.phaseTimes, key, now)}
                    headline={headlines[key] || null}
                    events={derived.byPhase[key]}
                    engineerView={engineerView}
                    expanded={isExpanded(key)}
                    onToggle={() => toggle(key)}
                    feedRef={phases[key] === "active" ? activeFeedRef : null}
                  >
                    {key === "verdicts" &&
                      (phases.verdicts !== "pending" || derived.planRows.length > 0) && (
                        <VerdictSubflow derived={derived} phaseState={phases.verdicts} />
                      )}
                  </PhaseSection>
                  {i < PHASE_LABELS.length - 1 && gates[key] && (
                    <div className="gate-slot">
                      <GateChip state={gates[key].state} label={gates[key].label} />
                    </div>
                  )}
                </React.Fragment>
              ))}
            </div>
          )}

          {finished && status.exitCode === 0 && (
            <p className="done-link">
              Run finished.{" "}
              <Link to={`/?week=${status.asOf}`}>
                Open the Monday Digest for {status.asOf} →
              </Link>
            </p>
          )}
          {finished && status.exitCode !== 0 && status.error && (
            <div className="panel-error" style={{ marginTop: 12 }}>
              {status.error}
            </div>
          )}
        </div>

        <div className="card">
          <h2>3 · Recommendation ledger</h2>
          <RecheckBand carryIn={activeCarryIn} ledger={ledger} asOf={week} />
          <LedgerSection ledger={ledger} asOf={week} carryIn={activeCarryIn} hideIntro />
        </div>
      </div>

      <SidePanel
        asOf={threadAsOf}
        collapsed={panelCollapsed}
        onToggle={() => setPanelCollapsed(!panelCollapsed)}
      />
      </div>
    </main>
  );
}
