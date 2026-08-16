// Assemble a PARTIAL digest when DATA/OUTPUTS/<asOf>/digest.json does not exist
// yet but the signal engine has already written DATA/OUTPUTS/<asOf>/signals.json.
//
// Hard rule: this API computes nothing analytical. Everything in
// the partial digest is copied verbatim from files the Python pipeline wrote:
//   - DATA/OUTPUTS/<asOf>/signals.json      -> top signals + suppressed list
//   - DATA/OUTPUTS/validation/report.json   -> the "Data checks" strip
//   - DATA/OUTPUTS/ledger.csv               -> current ledger rows (as-is on disk)
//   - DATA/TRANSLATION/locations.csv               -> location_id -> center name map, used
//     only to scrub PCC_xxx ids out of leader-facing sentences (same scrub
//     pipeline/run.py applies when it assembles the full digest).
// The only thing added is the `partial` flag and pending-section placeholders.

const fs = require("fs");
const path = require("path");
const { parseCsvObjects } = require("./csv");

const PCC_RE = /\bPCC_\d+\b/g;

function readJsonIfExists(p) {
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

function locationNames(repoRoot) {
  // Prefer the corrected DATA/TRANSLATION/ copy (what the pipeline computes from).
  const p = path.join(repoRoot, "DATA", "TRANSLATION", "locations.csv");
  if (!fs.existsSync(p)) return {};
  const rows = parseCsvObjects(fs.readFileSync(p, "utf8"));
  const names = {};
  for (const r of rows) {
    if (r.location_id && r.location_name) names[r.location_id] = r.location_name;
  }
  return names;
}

// Same plain-English list pipeline/run.py builds for the full digest:
// accepted corrections read "Fixed:", flagged findings read "Flagged:".
// Here corrections are only *proposed* (a run that accepts them writes
// DATA/TRANSLATION/MANIFEST.json) so we label honestly from what is on disk.
function buildDataChecks(report, manifest, scrub) {
  const accepted = manifest && Array.isArray(manifest.corrections_accepted)
    ? manifest.corrections_accepted
    : [];
  const plain = accepted.map((c) =>
    c.decision_status === "carried"
      ? `Standing decision carried forward: ${scrub(c.description)}`
      : `Fixed: ${scrub(c.description)}`
  );
  const carried = manifest && Array.isArray(manifest.corrections_carried)
    ? manifest.corrections_carried
    : [];
  const newlyRequired = manifest && Array.isArray(manifest.corrections_new)
    ? manifest.corrections_new
    : [];
  for (const tableFindings of Object.values(report.tables || {})) {
    for (const f of tableFindings) {
      if (f.result === "flag") plain.push(`Flagged: ${scrub(f.summary)}`);
    }
  }
  return {
    title: "Data Validation & Check",
    n_ran: report.totals.checks_run,
    corrections: accepted.length,
    corrections_carried: carried.length,
    corrections_new: newlyRequired.length,
    rows_under_standing_corrections: carried.reduce(
      (sum, c) => sum + Number(c.new_rows || 0), 0
    ),
    provider_rows_under_standing_corrections: accepted
      .filter(
        (c) => c.decision_status === "carried" &&
          c.scope === "standing" && c.table === "provider_weekly"
      )
      .reduce((sum, c) => sum + Number(c.new_rows || 0), 0),
    plain_english: plain,
    validated_as_of: report.as_of,
    details: {
      report: "DATA/OUTPUTS/validation/report.md",
      report_json: "DATA/OUTPUTS/validation/report.json",
      manifest: "DATA/TRANSLATION/MANIFEST.json",
    },
  };
}

function assemblePartialDigest(repoRoot, asOf) {
  const outDir = path.join(repoRoot, "DATA", "OUTPUTS", asOf);
  const signalsDoc = readJsonIfExists(path.join(outDir, "signals.json"));
  if (!signalsDoc) return null; // caller turns this into a 404

  const names = locationNames(repoRoot);
  const scrub = (text) =>
    String(text).replace(PCC_RE, (id) => names[id] || id);

  const report = readJsonIfExists(
    path.join(repoRoot, "DATA", "OUTPUTS", "validation", "report.json")
  );
  const manifest = readJsonIfExists(path.join(repoRoot, "DATA", "TRANSLATION", "MANIFEST.json"));

  // Ledger state exactly as it sits on disk right now (it is global, not
  // per-week). "open" here means the row's own status field says so.
  const ledgerCsv = path.join(repoRoot, "DATA", "OUTPUTS", "ledger.csv");
  let ledger = null;
  if (fs.existsSync(ledgerCsv)) {
    const rows = parseCsvObjects(fs.readFileSync(ledgerCsv, "utf8"));
    ledger = {
      partial: true,
      note:
        "Ledger state as currently on disk — not yet re-checked for this week. A full pipeline run updates it.",
      rows: rows.map((r) => ({
        rec_id: r.rec_id,
        center: r.location_name,
        metric: r.metric,
        recommendation: r.recommendation,
        owner: r.owner,
        expected_direction: r.expected_direction,
        created_week: r.created_week,
        check_by: r.check_by,
        status: r.status,
        last_checked: r.last_checked || null,
        outcome_note: r.outcome_note || null,
      })),
    };
  }

  return {
    step: "Monday digest (partial)",
    partial: true,
    partial_reason:
      "verdicts pending — the signal engine has run for this week, but Claimed vs. Verified and the ledger re-check have not. Run the full pipeline to complete this digest.",
    as_of: signalsDoc.as_of,
    latest_complete_week: signalsDoc.latest_complete_week,
    leader: signalsDoc.leader,
    centers: signalsDoc.centers,
    generated_at: signalsDoc.generated_at,
    top_signals: signalsDoc.signals, // verbatim; no ledger action attached yet
    suppressed: signalsDoc.suppressed,
    claimed_vs_verified: {
      pending: true,
      message:
        "Verdicts pending — Claimed vs. Verified runs when the full pipeline runs.",
      rows: [],
      counts: null,
    },
    ledger,
    data_checks: report ? buildDataChecks(report, manifest, scrub) : null,
    harness: null,
    receipts: {
      signals: `DATA/OUTPUTS/${asOf}/signals.json`,
      validation_report: "DATA/OUTPUTS/validation/report.md",
      data_manifest: "DATA/TRANSLATION/MANIFEST.json",
    },
  };
}

module.exports = { assemblePartialDigest };
