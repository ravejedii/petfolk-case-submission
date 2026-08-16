// Plain-English translation layer for the pipeline console's progress feed.
//
// THE one place raw pipeline events become leader-legible sentences. Every
// sentence is built ONLY from fields the pipeline actually wrote in the event
// (plus name lookups from the pipeline's own CSVs) — nothing is invented.
// An event this module cannot confidently translate returns null, and the UI
// falls back to the raw line.
//
// Mappings used:
//   location_id -> center name       DATA/TRANSLATION/locations.csv (fallback: DATA/INPUTS/)
//   center slug -> center name       slugified from the same file (rec_ids)
//   plan_id     -> center + metric   DATA/TRANSLATION/action_plans.csv (fallback: DATA/INPUTS/)
//   metric id   -> display name      the pipeline's own registry (pipeline/config.py)
//   Cn          -> correction kind   DATA/OUTPUTS/validation/corrections_proposed.json

const fs = require("fs");
const path = require("path");
const { parseCsvObjects } = require("./csv");

// Mirrors pipeline/config.py METRICS display names (presentation only).
const METRIC_DISPLAY = {
  appts_per_doctor_hour: "Appointments per doctor-hour",
  recheck_compliance_pct: "Recheck compliance",
  record_completion_24h_pct: "Medical records completed within 24 hours",
  callback_compliance_pct: "Client callback compliance",
  client_csat: "Client satisfaction (CSAT)",
  avg_wait_time_min: "Average client wait time",
  revenue_per_appt: "Revenue per appointment",
  membership_conversion_pct: "Membership conversion",
  staff_call_outs: "Staff call-outs",
  no_show_rate: "Client no-show rate",
  open_dvm_requisitions: "Open doctor job requisitions",
};

// Compact phrases for mid-sentence use ("the Pelham Row callbacks plan").
const METRIC_SHORT = {
  appts_per_doctor_hour: "appointments per doctor-hour",
  recheck_compliance_pct: "recheck compliance",
  record_completion_24h_pct: "record completion",
  callback_compliance_pct: "callbacks",
  client_csat: "CSAT",
  avg_wait_time_min: "wait times",
  revenue_per_appt: "revenue per appointment",
  membership_conversion_pct: "membership conversion",
  staff_call_outs: "staff call-outs",
  no_show_rate: "no-show",
  open_dvm_requisitions: "open doctor requisitions",
};

// Leader-facing phase titles (the same names run-manager.js and
// pipeline/narrate.py use — the leader-facing naming rules).
const PHASE_TITLES = {
  validation: "Data Validation & Check",
  signals: "Signal engine",
  verdicts: "Claimed vs. Verified",
  digest: "Digest assembly",
};

// The four tables, named the way a leader says them (used for the DATA/TRANSLATION/
// rebuild lines pipeline.validate prints).
const TABLE_FRIENDLY = {
  "locations.csv": "center list",
  "clinic_weekly.csv": "weekly clinic table",
  "provider_weekly.csv": "weekly doctor table",
  "action_plans.csv": "improvement plans table",
};

// What each python module the server starts is doing, in plain words.
const MODULE_WORK = {
  "pipeline.validate": "Data Validation & Check",
  "pipeline.run": "the digest pipeline",
  "pipeline.narrate": "the narrative writer",
  "pipeline.ledger": "the recommendation ledger",
};

// Who actually wrote a piece of text, named honestly (a different
// vendor's model is never labelled as Claude).
const WRITER_NAME = {
  "claude-cli": "Claude",
  api: "Claude",
  openai: "OpenAI",
  template: "the deterministic template",
};

function writerVerb(mode) {
  if (mode === "openai") return "OpenAI is writing";
  if (mode === "claude-cli" || mode === "api") return "Claude is writing";
  return "Writing"; // template, or a tier this build does not name
}

function plural(n, word) {
  return `${n} ${word}${Number(n) === 1 ? "" : "s"}`;
}

function writerName(mode) {
  return WRITER_NAME[mode] || mode || "the pipeline";
}

// Friendly names for the Data Validation & Check runlog entries.
const CHECK_FRIENDLY = {
  "locations.maturity_tier_vs_opened_date": "maturity labels vs. opening dates",
  "locations.partial_history_centers": "centers with partial operating history",
  "clinic_weekly.duplicate_rows": "duplicate weekly rows",
  "clinic_weekly.unique_center_week": "one row per center-week",
  "clinic_weekly.negative_wait_times": "negative wait times",
  "clinic_weekly.missing_values": "missing values in the clinic file",
  "clinic_weekly.membership_definition_change": "membership metric definition change",
  "clinic_weekly.small_denominators": "small denominators (noisy weekly rates)",
  "clinic_weekly.throughput_formula": "throughput recomputed from its own formula",
  "clinic_weekly.rows_before_opened_date": "rows dated before a center opened",
  "clinic_weekly.week_start_mondays": "week starts fall on Mondays (clinic file)",
  "clinic_weekly.known_location_ids": "every clinic row joins to a known center",
  "provider_weekly.employment_type_capitalization": "employment-type capitalization",
  "provider_weekly.missing_values": "missing values in the doctor file",
  "provider_weekly.week_start_mondays": "week starts fall on Mondays (doctor file)",
  "provider_weekly.rollup_consistency.recheck_compliance_pct":
    "clinic recheck-compliance rollups vs. doctor-level data",
  "provider_weekly.rollup_consistency.record_completion_24h_pct":
    "clinic record-completion rollups vs. doctor-level data",
  "provider_weekly.known_location_ids": "every doctor row joins to a known center",
  "action_plans.target_metric_is_real_column": "plan targets are real metrics",
  "action_plans.baseline_vs_actual_at_open": "recorded plan baselines vs. actuals at open",
  "action_plans.known_location_ids": "every plan joins to a known center",
};

// What accepting / declining each correction kind means, in plain words.
const KIND_APPLIED = {
  drop_duplicates: (n) => `removed ${n} duplicate weekly rows`,
  negative_wait_to_missing: (n) => `set ${n} negative wait times to missing`,
  normalize_employment_type: (n) => `normalized employment-type capitalization on ${n} rows`,
  relabel_maturity_tier: (n) => `relabeled maturity for ${n} centers`,
};
const KIND_DECLINED = {
  drop_duplicates: "duplicate weekly rows kept",
  negative_wait_to_missing: "negative wait times left in place",
  normalize_employment_type: "employment-type capitalization left as-is",
  relabel_maturity_tier: "maturity labels left as-is",
};

// The four actions a leader can take on a ledger card (pipeline/ledger.py
// DECISION_ACTIONS), in the past tense the feed reads in.
const DECISION_VERBS = {
  close: "closed",
  relaunch: "relaunched",
  escalate: "escalated",
  dismiss: "dismissed",
};

const SUPPRESSION_RULES = {
  csat_small_sample: "too few CSAT survey responses to trust the weekly average",
  min_appointments: "too few appointments for a stable rate",
  partial_history: "not enough operating history for a trustworthy baseline",
  below_cutoff: "scored below the attention cutoff",
};

const PCC_RE = /\bPCC_\d+\b/g;

function slugify(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function readCsvObjects(filePath) {
  try {
    return parseCsvObjects(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

// Build the lookup context from the pipeline's own files. Cheap; call again
// after a Data Validation & Check step so a rebuilt DATA/TRANSLATION/ (e.g. from uploaded
// files) is reflected.
function loadContext(repoRoot, inputsDir) {
  // repoRoot is kept on the context so filesystem paths in a line can be cut
  // back to repo-relative ones: a leader never sees /Users/… or /tmp/….
  const ctx = { repoRoot: repoRoot || null, names: {}, slugs: {}, plans: {}, corrections: {} };

  const locCandidates = [
    path.join(repoRoot, "DATA", "TRANSLATION", "locations.csv"),
    inputsDir ? path.join(inputsDir, "locations.csv") : null,
    path.join(repoRoot, "DATA", "INPUTS", "locations (1).csv"),
  ].filter(Boolean);
  for (const p of locCandidates) {
    const rows = readCsvObjects(p);
    if (rows && rows.length) {
      for (const r of rows) {
        if (r.location_id && r.location_name) {
          ctx.names[r.location_id] = r.location_name;
          ctx.slugs[slugify(r.location_name)] = r.location_name;
        }
      }
      break;
    }
  }

  const planCandidates = [
    path.join(repoRoot, "DATA", "TRANSLATION", "action_plans.csv"),
    inputsDir ? path.join(inputsDir, "action_plans.csv") : null,
    path.join(repoRoot, "DATA", "INPUTS", "action_plans.csv"),
  ].filter(Boolean);
  for (const p of planCandidates) {
    const rows = readCsvObjects(p);
    if (rows && rows.length) {
      for (const r of rows) {
        if (r.plan_id) {
          ctx.plans[r.plan_id] = {
            location_id: r.location_id,
            metric: r.target_metric,
          };
        }
      }
      break;
    }
  }

  try {
    const doc = JSON.parse(
      fs.readFileSync(
        path.join(repoRoot, "DATA", "OUTPUTS", "validation", "corrections_proposed.json"),
        "utf8"
      )
    );
    for (const c of doc.corrections || []) ctx.corrections[c.id] = c;
  } catch {
    // no proposals on disk yet — correction lines fall back to table/rows
  }
  return ctx;
}

function centerName(ctx, locationId) {
  return ctx.names[locationId] || null;
}

// "REC-2026-04-27-mount-pleasant-staff_call_outs" -> {created, center, metric}
// A successor carries an "-attempt2" suffix; the rest of the id is identical,
// so it is stripped before the center/metric are read off.
function parseRecId(ctx, recId) {
  const m = /^REC-(\d{4}-\d{2}-\d{2})-(.+)$/.exec(recId || "");
  if (!m) return null;
  const rest = m[2].replace(/-attempt\d+$/, "");
  const cut = rest.lastIndexOf("-");
  if (cut < 0) return null;
  const slug = rest.slice(0, cut);
  const metric = rest.slice(cut + 1);
  const center = ctx.slugs[slug];
  if (!center || !METRIC_SHORT[metric]) return null;
  return { created: m[1], center, metric };
}

// "last Monday's" when the rec was created exactly one week before as_of.
function createdPhrase(created, asOf) {
  if (created && asOf) {
    const diff = Date.parse(asOf) - Date.parse(created);
    if (diff === 7 * 24 * 3600 * 1000) return "last Monday's";
  }
  return created ? `the ${created}` : "the earlier";
}

function planPhrase(ctx, planId) {
  const p = ctx.plans[planId];
  if (!p) return null;
  const center = centerName(ctx, p.location_id);
  const metric = METRIC_SHORT[p.metric];
  if (!center || !metric) return null;
  return `${center} ${metric}`;
}

function countsPhrase(counts) {
  const bits = [];
  for (const [k, v] of Object.entries(counts || {})) {
    if (v === null || typeof v === "object") continue;
    bits.push(`${k.replace(/_/g, " ")} ${v}`);
  }
  return bits.join(", ");
}

// What the NUMBER did. Deliberately says nothing about whether anyone acted —
// that is a separate, human-attested field (see LOOP_STATES below). v1
// collapsed the two and told leaders things like "still being ignored", which
// the data never supported.
const RECHECK_OUTCOMES = {
  working: "the number moved the right way",
  not_working: "the number moved the wrong way",
  flat: "the number did not really move",
  unverifiable: "there was not enough data to check",
};

// What follows from the number AND the attestation together.
const LOOP_STATES = {
  confirmed_working: "confirmed done and it worked — closing with credit",
  intervention_failed: "confirmed done but it did not work — a different mechanism takes over",
  not_executed: "confirmed not done — the same ask goes back, escalated",
  unattributed_gain: "nobody has confirmed the work happened, so the gain is not claimed as ours",
  needs_attestation: "nobody has confirmed the work happened — asking before changing the fix",
  awaiting_evidence: "too early to tell",
  in_flight: "confirmed done — too early to tell",
  unverifiable: "cannot be checked yet",
  pending: "awaiting its first re-check",
};

// ---------------------------------------------------------------------------
// Validation runlog lines ({step:"validate", check, result, counts})
// ---------------------------------------------------------------------------

function translateValidate(evt, ctx) {
  const check = evt.check || "";
  if (check.startsWith("apply.")) {
    const id = check.slice("apply.".length);
    const c = ctx.corrections[id];
    const kind = c && c.kind;
    if (evt.result === "applied") {
      const n = (evt.counts && evt.counts.rows_affected) ?? "?";
      const what = KIND_APPLIED[kind]
        ? KIND_APPLIED[kind](n)
        : `${n} rows updated in ${(evt.counts && evt.counts.table) || "the data"}`;
      return `Correction ${id} applied — ${what}.`;
    }
    if (evt.result === "declined") {
      const what = KIND_DECLINED[kind] || "left as-is";
      return `Correction ${id} declined — ${what}. DATA/TRANSLATION/ is being built without it.`;
    }
    return null;
  }
  // decision.<id> — what the decision log said about a correction BEFORE the
  // human was asked anything. A correction already ruled on is not a question
  // again, and saying so is the difference between "week two found nothing"
  // and "week two asked nothing because week one already answered".
  if (check.startsWith("decision.")) {
    const id = check.slice("decision.".length);
    const counts = evt.counts || {};
    if (evt.result === "carried") {
      const when = counts.decided_as_of ? ` on ${counts.decided_as_of}` : " earlier";
      const ruling = counts.prior_decision === "declined" ? "declined" : "accepted";
      const fresh = counts.new_rows
        ? `, covering ${counts.new_rows} row${counts.new_rows === 1 ? "" : "s"} that arrived since`
        : "";
      return `Correction ${id} was ${ruling}${when} and still applies${fresh} — not asked again.`;
    }
    if (evt.result === "new") {
      return `Correction ${id} has never been ruled on — it goes to you for accept or decline.`;
    }
    return null;
  }
  const friendly = CHECK_FRIENDLY[check];
  if (!friendly) return null;
  if (evt.result === "pass") return `Data check passed: ${friendly}.`;
  const detail = countsPhrase(evt.counts);
  return `Data check flagged: ${friendly}${detail ? ` (${detail})` : ""}.`;
}

// ---------------------------------------------------------------------------
// Run-log JSONL events (signals / verdicts / ledger / run)
// ---------------------------------------------------------------------------

function translateLedger(evt, ctx) {
  switch (evt.event) {
    case "run_start":
      return (
        `Recommendation ledger opened — ${evt.rows_total} recommendation(s) on file, ` +
        `${evt.rows_active} active to re-check.`
      );
    case "rec_rechecked": {
      const parsed = parseRecId(ctx, evt.rec_id) || {};
      const center = evt.center || parsed.center;
      const metric = METRIC_SHORT[evt.metric || parsed.metric];
      const outcome = RECHECK_OUTCOMES[evt.outcome];
      if (!center || !metric || !outcome) return null;
      const when = createdPhrase(parsed.created, evt.as_of);
      const state = LOOP_STATES[evt.loop_state];
      // Escalation is an accountability event: it only ever follows a human
      // saying the work was not done.
      const esc =
        evt.loop_state === "not_executed" && evt.escalation_level
          ? ` (escalation level ${evt.escalation_level})`
          : "";
      const tail = state ? `; ${state}` : "";
      return `Re-checked ${when} ${center} ${metric} recommendation — ${outcome}${tail}${esc}.`;
    }
    case "rec_recheck_replayed": {
      const parsed = parseRecId(ctx, evt.rec_id);
      if (!parsed) return null;
      const outcome = RECHECK_OUTCOMES[evt.outcome] || evt.outcome;
      const when = createdPhrase(parsed.created, evt.as_of);
      return (
        `Re-checked ${when} ${parsed.center} ` +
        `${METRIC_SHORT[parsed.metric]} recommendation — ${outcome}.`
      );
    }
    case "rec_already_tracked": {
      const center = centerName(ctx, evt.location_id);
      const metric = METRIC_SHORT[evt.metric];
      if (!center || !metric) return null;
      if (evt.status === "open")
        return `${center} ${metric} recommendation is open — tracking for next Monday.`;
      if (evt.status === "escalated")
        return (
          `${center} ${metric} recommendation is escalated — ` +
          `level ${evt.escalation_level}; raised to both regional partners.`
        );
      if (evt.outcome === "not_working")
        return `${center} ${metric} recommendation is tracked and the number is still moving the wrong way.`;
      return `${center} ${metric} recommendation already tracked (${evt.status}).`;
    }
    case "execution_attested": {
      const parsed = parseRecId(ctx, evt.rec_id) || {};
      const center = evt.center || parsed.center;
      const metric = METRIC_SHORT[evt.metric || parsed.metric];
      if (!center || !metric) return null;
      const said =
        evt.execution === "done"
          ? "was carried out"
          : evt.execution === "not_done"
            ? "was not carried out"
            : "is no longer confirmed either way";
      return `${evt.actor} confirmed the ${center} ${metric} recommendation ${said}.`;
    }
    case "rec_created": {
      const center = evt.center || centerName(ctx, evt.location_id);
      const metric = METRIC_SHORT[evt.metric];
      if (!center || !metric) return null;
      const owner = evt.owner ? ` — owner ${evt.owner}` : "";
      const by = evt.check_by ? `, check back by ${evt.check_by}` : "";
      return `New recommendation: ${center} ${metric}${owner}${by}.`;
    }
    case "run_summary": {
      const c = evt.counts || {};
      return (
        `Ledger updated — ${c.created} new, ${c.rechecked} re-checked ` +
        `(${c.outcome_working} moved the right way, ${c.outcome_not_working} the wrong way, ` +
        `${c.outcome_flat} flat; execution confirmed on ${c.execution_done}, ` +
        `unconfirmed on ${c.execution_unknown}), ${c.open} open going into next Monday.`
      );
    }
    case "rec_superseded": {
      const center = evt.center || (parseRecId(ctx, evt.rec_id) || {}).center;
      const metric = METRIC_SHORT[evt.metric];
      if (!center || !metric) return null;
      return (
        `The ${center} ${metric} recommendation did not work — replaced by a ` +
        `successor recommendation rather than left as a dead end.`
      );
    }
    case "rec_successor_created": {
      const center = evt.center || (parseRecId(ctx, evt.rec_id) || {}).center;
      const metric = METRIC_SHORT[evt.metric];
      if (!center || !metric) return null;
      const who = evt.escalated
        ? `${evt.owner} and the other regional partner jointly`
        : evt.owner;
      return (
        `Next move for ${center} ${metric} — attempt ${evt.attempt}, owned by ${who}, ` +
        `check back by ${evt.check_by} (written by ${evt.decided_by}, ` +
        `${evt.numbers_verified} numbers verified).`
      );
    }
    case "ledger_reset":
      return "Previous ledger archived — starting fresh.";
    case "human_decision": {
      const parsed = parseRecId(ctx, evt.rec_id) || {};
      const what =
        parsed.center && METRIC_SHORT[parsed.metric]
          ? `the ${parsed.center} ${METRIC_SHORT[parsed.metric]} recommendation`
          : evt.rec_id;
      const verb = DECISION_VERBS[evt.action] || `set ${what} to ${evt.decision}`;
      return evt.action
        ? `${evt.actor} ${verb} ${what}.`
        : `${evt.rec_id} ${evt.decision} by ${evt.actor} — ${evt.note}`;
    }
    default:
      return null;
  }
}

// The successor step's own harness receipts (number / language / rule checks
// on the generated next move), written into the run log beside the verdicts'.
function translateSuccessor(evt, ctx) {
  const parsed = parseRecId(ctx, evt.rec_id) || {};
  const what =
    parsed.center && METRIC_SHORT[parsed.metric]
      ? `${parsed.center} ${METRIC_SHORT[parsed.metric]}`
      : "the next move";
  switch (evt.event) {
    case "successor_number_check":
      return evt.result === "pass"
        ? `Harness: every one of the ${evt.tokens_checked} numbers in the ${what} next move exists in this run's own facts.`
        : `Harness caught invented number(s) in the ${what} next move (${(evt.unknown_tokens || []).join(", ")}) — regenerating.`;
    case "successor_language_check":
      return evt.result === "pass"
        ? null // quiet when it passes; the number check already says "checked"
        : `Harness caught leader-facing language problems in the ${what} next move — regenerating.`;
    case "successor_rule_check":
      return evt.result === "pass"
        ? `Next move for ${what} is falsifiable: names the owner, a new check-by date, and a different intervention.`
        : `Harness rejected the ${what} next move (${(evt.failures || [])
            .map((f) => f.rule)
            .join(", ")}) — regenerating.`;
    case "successor_generated":
      return (
        `Next move written for ${what} — attempt ${evt.attempt}, by ${evt.decided_by}, ` +
        `${evt.numbers_verified} numbers verified.`
      );
    case "template_fallback":
      return `No verifiable next move from the model for ${what} — the deterministic successor stands.`;
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// pipeline.narrate's own run log (DATA/OUTPUTS/<asOf>/narrate_runlog.jsonl).
// These are the receipts for the panel's narration: which tier wrote it, and
// what the harness checked before it was allowed to be shown.
// ---------------------------------------------------------------------------

function translateNarrate(evt) {
  // Per-phase events name their phase; run_start/summary carry the list.
  const phase =
    evt.phase ||
    (Array.isArray(evt.phases) && evt.phases.length === 1 ? evt.phases[0] : null);
  const title = PHASE_TITLES[phase];
  if (!title) return null; // a multi-phase call: the per-phase lines say it all
  switch (evt.event) {
    case "run_start":
      return `Reading this run's own artifacts to write the ${title} narrative.`;
    case "llm_call": {
      const attempt = evt.attempt > 1 ? ` (attempt ${evt.attempt})` : "";
      return `${writerVerb(evt.mode)} the ${title} narrative${attempt}…`;
    }
    case "narrative_number_check":
      return evt.result === "pass"
        ? `Harness checked the ${plural(evt.tokens_checked, "number")} in the ${title} narrative against this run's artifacts — pass.`
        : `Harness found number(s) in the ${title} narrative that are not in this run's artifacts (${(
            evt.unknown_tokens || []
          ).join(", ")}) — regenerating.`;
    case "narrative_language_check":
      return evt.result === "pass"
        ? `Harness checked the ${title} narrative for leader-facing language — pass.`
        : `Harness caught leader-facing language problems in the ${title} narrative (${(
            evt.problems || []
          ).join("; ")}) — regenerating.`;
    case "narrative_entity_check":
      return evt.result === "pass"
        ? `Harness checked the ${plural(evt.entities_verified, "name")} in the ${title} narrative against this run's artifacts — pass.`
        : `Harness found name(s) in the ${title} narrative that this run's artifacts do not contain (${(
            evt.problems || []
          ).join("; ")}) — regenerating.`;
    case "narrative_rule_check":
      return evt.result === "pass"
        ? `Harness checked the ${title} narrative against the phase's own results — pass.`
        : `Harness caught the ${title} narrative saying something its results do not support (${(
            evt.problems || []
          ).join("; ")}) — regenerating.`;
    case "narrative_shape_check":
      return evt.result === "pass"
        ? `Harness checked the shape of the ${title} narrative (${plural(evt.bullets, "bullet")}) — pass.`
        : `Harness rejected the shape of the ${title} narrative (${(
            evt.problems || []
          ).join("; ")}) — regenerating.`;
    case "narrative":
      return (
        `Narrative for ${title}: ${plural(evt.numbers_verified, "number")} verified against ` +
        `the run's artifacts, written by ${writerName(evt.decided_by)} in ` +
        `${plural(evt.attempts, "attempt")}.`
      );
    case "retry":
      return `The ${title} narrative failed a check (${evt.reason}) — regenerating.`;
    case "generation_error":
      return `No narrative came back for ${title} (${evt.kind}) — nothing was generated, so nothing is shown.`;
    case "mode_fallback":
      return (
        `${writerName(evt.from_mode)} was unavailable for the ${title} narrative ` +
        `(${evt.kind}) — falling back to ${writerName(evt.to_mode)}.`
      );
    case "template_fallback":
      return (
        `No verifiable ${title} narrative from the model after ` +
        `${plural(evt.attempts, "attempt")} — the deterministic summary stands.`
      );
    case "hard_fail":
      return `The ${title} narrative could not be produced (${evt.reason}) — nothing was invented.`;
    case "summary":
      return (
        `${title} narrative finished — ${plural(evt.attempts_total, "attempt")}, ` +
        `${plural(evt.fallbacks_total, "tier fallback")}.`
      );
    default:
      return null;
  }
}

function translateSignals(evt, ctx) {
  switch (evt.event) {
    case "parameters":
      return (
        `Signal engine started for ${evt.as_of} — scoring every center-metric ` +
        `for drift, gap, and spike.`
      );
    case "ranked": {
      const center = centerName(ctx, evt.location_id);
      const metric = METRIC_SHORT[evt.metric];
      if (!center || !metric) return null;
      const s = evt.sub_scores || {};
      const sub =
        s.drift != null
          ? ` (drift ${s.drift}, gap ${s.gap}, spike ${s.spike})`
          : "";
      return `Signal #${evt.rank}: ${center} ${metric} — priority ${evt.priority}${sub}.`;
    }
    case "suppressed": {
      const center = centerName(ctx, evt.location_id);
      const metric = METRIC_SHORT[evt.metric];
      if (!center || !metric) return null;
      const why = SUPPRESSION_RULES[evt.rule] || evt.rule;
      return `Held back: ${center} ${metric} (would-be priority ${evt.would_be_priority}) — ${why}.`;
    }
    case "summary": {
      const c = evt.counts || {};
      return (
        `Signal engine finished — ${c.center_metric_pairs_evaluated} center-metric ` +
        `pairs scored, ${c.ranked} made the digest, ${c.suppressed} held back (listed, not hidden).`
      );
    }
    default:
      return null;
  }
}

function translateVerdicts(evt, ctx) {
  const plan = evt.plan_id ? planPhrase(ctx, evt.plan_id) : null;
  switch (evt.event) {
    case "run_start":
      return (
        `Claimed vs. Verified started — re-judging all ${evt.plans} improvement ` +
        `plans against the data (${evt.mode} mode).`
      );
    case "facts_summary_number_check":
      if (!plan) return null;
      return (
        `Built the fact sheet for ${plan} (${evt.plan_id}) — ` +
        `every number checked against the data: ${evt.result}.`
      );
    case "llm_call": {
      if (!plan) return null;
      const attempt = evt.attempt > 1 ? ` (attempt ${evt.attempt})` : "";
      return `${writerVerb(evt.mode)} the verdict for ${plan}${attempt}…`;
    }
    case "number_check":
      if (!plan) return null;
      return `Harness checked every number in the ${plan} verdict — ${evt.result}.`;
    case "reasoning_check":
      if (!plan) return null;
      return (
        `Harness checked the reasoning in the ${plan} verdict — ` +
        `"${evt.bucket}" fits the movement rules — ${evt.result}.`
      );
    case "retry":
      if (!plan) return null;
      return (
        `Verdict for ${plan} failed a harness check — regenerating ` +
        `(attempt ${evt.attempt}: ${evt.reason}).`
      );
    case "verdict": {
      if (!plan) return null;
      const agree = evt.agree
        ? "agrees with the reported status"
        : "differs from the reported status";
      return `Verdict written for ${plan} — ${evt.bucket}, harness checks passed; ${agree}.`;
    }
    case "summary": {
      const c = evt.counts || {};
      const buckets = Object.entries(c.buckets || {})
        .filter(([, n]) => n)
        .map(([b, n]) => `${b} ${n}`)
        .join(", ");
      return (
        `Claimed vs. Verified finished — ${c.plans} plans judged (${buckets}); ` +
        `agrees with the reported status on ${c.agree}, differs on ${c.disagree}.`
      );
    }
    default:
      return null;
  }
}

function translateRunStep(evt) {
  if (evt.event !== "digest_assembled") return null;
  const c = evt.counts || {};
  return (
    `Monday digest assembled for ${evt.as_of} — ${c.top_signals} top signals, ` +
    `${c.plans_verified} plans verified, ${c.data_checks_ran} data checks ran ` +
    `with ${c.corrections} corrections. digest.json written.`
  );
}

// One JSONL run-log event -> one plain sentence, or null (fall back to raw).
function translateLogEvent(evt, ctx) {
  try {
    if (evt.step === "validate") return translateValidate(evt, ctx);
    if (evt.step === "ledger") return translateLedger(evt, ctx);
    if (evt.step === "signals") return translateSignals(evt, ctx);
    if (evt.step === "verdicts") return translateVerdicts(evt, ctx);
    if (evt.step === "successor") return translateSuccessor(evt, ctx);
    if (evt.step === "narrate") return translateNarrate(evt);
    if (evt.step === "run") return translateRunStep(evt);
  } catch {
    return null; // an odd payload must never break the feed
  }
  return null;
}

// ---------------------------------------------------------------------------
// Text lines: the server's own notes and the pipeline's stdout.
//
// These are English already, but they carry things a leader must never see —
// absolute filesystem paths, command lines, location IDs. So every line gets
// scrubbed, and the ones with a known shape (the Data Validation & Check
// vocabulary, the "Started: …" spawn notes) are rewritten as sentences. The
// Engineer view still shows the raw `text` untouched.
// ---------------------------------------------------------------------------

// An absolute path token: starts at a boundary so "DATA/TRANSLATION/locations.csv" (no
// leading slash) is left alone.
const ABS_PATH_RE = /(^|[\s(<'"=])(\/[^\s)>'",]+)/g;

// A path, cut back to something a leader can read: repo-relative when it is
// inside the repo, otherwise its last two segments. The machine is dropped.
function shortPath(p, repoRoot) {
  if (repoRoot && p.startsWith(`${repoRoot}/`)) return p.slice(repoRoot.length + 1);
  const parts = String(p).split("/").filter(Boolean);
  return parts.slice(-2).join("/");
}

function scrubPaths(line, ctx) {
  return line.replace(ABS_PATH_RE, (m, pre, p) => pre + shortPath(p, ctx.repoRoot));
}

function scrubIds(line, ctx) {
  PCC_RE.lastIndex = 0;
  return line.replace(PCC_RE, (id) => ctx.names[id] || id);
}

// "Started: python -u -m pipeline.validate --as-of 2026-05-04 --accept all
//  (cwd /Users/…)" — the command is the Engineer view's business.
function translateStarted(line) {
  const m = /^Started:\s+\S+\s+(.*?)(?:\s+\(cwd\s+[^)]*\))?\s*$/.exec(line);
  if (!m) return null;
  const args = m[1].split(/\s+/);
  const mod = args[args.indexOf("-m") + 1] || "";
  const asOf = args[args.indexOf("--as-of") + 1] || null;
  const work = MODULE_WORK[mod];
  if (!work) return null;
  const when = asOf && /^\d{4}-\d{2}-\d{2}$/.test(asOf) ? ` for ${asOf}` : "";
  if (mod === "pipeline.validate") {
    return args.includes("--accept")
      ? `Running ${work}${when} — applying the accepted corrections to a copy of the data in DATA/TRANSLATION/ (DATA/INPUTS/ is never touched).`
      : `Running ${work}${when} — proposing corrections only; nothing changes until you decide.`;
  }
  if (mod === "pipeline.run") {
    return `Running ${work}${when} — signal engine, then Claimed vs. Verified, then digest assembly.`;
  }
  return `Running ${work}${when}.`;
}

// The lines pipeline/validate.py prints, and the two the digest run prints
// with a path in them. Every value in the sentence comes from the line.
function translateValidateStdout(line, ctx) {
  const t = line.trim();
  let m;
  if ((m = /^Data Validation & Check — as of (\d{4}-\d{2}-\d{2})$/.exec(t))) {
    return `Data Validation & Check running over the four source tables, as of ${m[1]}.`;
  }
  if ((m = /^(\d+) checks ran, (\d+) found something\.$/.exec(t))) {
    return `${m[1]} data checks ran — ${m[2]} found something.`;
  }
  if ((m = /^Report: (.+)$/.exec(t))) {
    return `Check-by-check report written (${shortPath(m[1], ctx.repoRoot)}).`;
  }
  if ((m = /^Corrections proposed: (.*?) \((.+)\)$/.exec(t))) {
    const ids = m[1].trim();
    return ids
      ? `Corrections proposed: ${ids} — DATA/INPUTS/ is never edited; accepted ones are applied to a copy in DATA/TRANSLATION/.`
      : "No corrections proposed — the data needed no fixes.";
  }
  if (/^No corrections applied \(propose-only run\)\./.test(t)) {
    return "Nothing changed yet — this pass only proposes corrections; the accept/decline decision is yours.";
  }
  if ((m = /^Applied: (.+?) · Declined \(logged\): (.+)$/.exec(t))) {
    return `Applied ${m[1]}; declined ${m[2]} — declines are logged, never silently dropped.`;
  }
  if ((m = /^DATA\/TRANSLATION\/([\w.]+): (\d+) → (\d+) rows, (\d+) cells changed$/.exec(t))) {
    const what = TABLE_FRIENDLY[m[1]] || m[1];
    return `Rebuilt the ${what} in DATA/TRANSLATION/: ${m[2]} rows in, ${m[3]} out, ${m[4]} cells changed.`;
  }
  if ((m = /^Manifest: (.+)$/.exec(t))) {
    return `Manifest written (${shortPath(m[1], ctx.repoRoot)}) — exactly which corrections built this copy of the data.`;
  }
  if ((m = /^Monday digest — (\S+) \(week of (\S+)\)$/.exec(t))) {
    return `Monday digest for ${m[1]} assembled from the week of ${m[2]}.`;
  }
  if ((m = /^Digest: (.+)$/.exec(t))) {
    return `Digest written (${shortPath(m[1], ctx.repoRoot)}).`;
  }
  return null;
}

// The server's own notes about spawning and narration.
function translateServerNote(line) {
  let m;
  if ((m = /^Narrating (.+?): /.exec(line))) {
    return `Writing the plain-English narrative for ${m[1]} — it runs alongside the pipeline, which never waits for it.`;
  }
  if (
    (m = /^Narrative for (.+?) ready — written by (\S+), (\d+) numbers verified, attempts (\d+), ([\d.]+)s/.exec(
      line
    ))
  ) {
    return (
      `The ${m[1]} narrative is ready — written by ${writerName(m[2])}, ` +
      `${plural(Number(m[3]), "number")} verified against this run's artifacts, ` +
      `${plural(Number(m[4]), "attempt")}, ${m[5]}s.`
    );
  }
  return null;
}

// One server/stdout/artifact line -> the leader-facing sentence. Never null
// for a non-empty line: a line with no known shape is still shown, scrubbed,
// because a silent phase box is a lie about a phase that was working.
function translateText(line, ctx) {
  const raw = String(line == null ? "" : line);
  if (!raw.trim()) return null;
  const context = ctx || { repoRoot: null, names: {} };
  try {
    const known =
      translateStarted(raw) ||
      translateServerNote(raw) ||
      translateValidateStdout(raw, context);
    if (known) return scrubIds(known, context);
    return scrubIds(scrubPaths(raw.trim(), context), context);
  } catch {
    return null; // an odd line must never break the feed
  }
}

// Attach leader-legible labels to a run-log event before it is served as
// structured `meta` (the UI's per-plan progress rows and gate chips). The
// event itself is never altered — this returns an annotated copy.
function enrichMeta(evt, ctx) {
  const meta = { ...evt };
  if (evt.plan_id) {
    const label = planPhrase(ctx, evt.plan_id);
    if (label) meta.plan_label = label;
  }
  if (evt.location_id && ctx.names[evt.location_id]) {
    meta.center = ctx.names[evt.location_id];
  }
  if (evt.metric && METRIC_SHORT[evt.metric]) {
    meta.metric_label = METRIC_SHORT[evt.metric];
  }
  return meta;
}

// One declined correction -> the thread/feed line, e.g.
// "C3 declined — employment-type capitalization left as-is."
function declinedCorrectionLine(correction) {
  const what = (correction && KIND_DECLINED[correction.kind]) || "left as-is";
  return `${correction.id} declined — ${what}.`;
}

module.exports = {
  loadContext,
  translateLogEvent,
  translateText,
  enrichMeta,
  declinedCorrectionLine,
  METRIC_DISPLAY,
  METRIC_SHORT,
  PHASE_TITLES,
};
