// Acting on a tracked recommendation — the server side of the ledger cards.
//
// HARD RULE: this file decides nothing and writes nothing to the
// ledger itself. It shells out to the real pipeline module
// (`python -m pipeline.ledger --decide <REC_ID> --action <action> … --json`),
// which is the single door to DATA/OUTPUTS/ledger.csv + ledger_log.jsonl, and then
// posts the decision into the Monday's conversation thread. The state a
// reloaded page reads back comes from those pipeline files, never from
// anything remembered here.
//
// Four actions, matching the buttons on a ledger card:
//
//   close              it is done — close the row
//   relaunch           still worth doing, new deadline (needs a new check-by)
//   escalate           raise it to both regional partners; stays open
//   dismiss            drop it — and a dismissal must carry its reason, since
//                      that reason is what the next run's memory keeps
//
// Every decision lands twice: as a receipt in ledger_log.jsonl (written by the
// pipeline) and as a decision entry in the thread (written here), so the panel
// shows who decided what, next to the run that raised it.

const path = require("path");
const { spawn } = require("child_process");

const DECIDE_TIMEOUT_MS = 30_000;
const REC_ID_RE = /^REC-[A-Za-z0-9_-]+$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// Mirrors pipeline/config.py METRICS display names (presentation only — the
// same mapping lib/plain.js keeps for the console).
const METRIC_SHORT = {
  appts_per_doctor_hour: "appointments per doctor-hour",
  recheck_compliance_pct: "recheck compliance",
  record_completion_24h_pct: "record completion",
  callback_compliance_pct: "callback",
  client_csat: "client satisfaction",
  avg_wait_time_min: "wait time",
  revenue_per_appt: "revenue per appointment",
  membership_conversion_pct: "membership conversion",
  staff_call_outs: "staff call-out",
  no_show_rate: "no-show",
  open_dvm_requisitions: "open doctor requisition",
};

const ACTIONS = {
  // `attest` is the one action that answers "did this actually happen?".
  // It is the ONLY way execution is ever set — the pipeline never infers it
  // from a metric — and unlike the others it does not change the lifecycle.
  attest: { needsNote: false, needsDate: false, needsExecution: true },
  close: { needsNote: false, needsDate: false },
  relaunch: { needsNote: false, needsDate: true },
  escalate: { needsNote: false, needsDate: false },
  dismiss: { needsNote: true, needsDate: false },
};

// What the thread says happened — plain English only. The rec id lives in the
// entry payload (engineer view / audit trail), never in the leader-facing line.
function decisionText(action, row, { by, note, checkBy }) {
  const who = by || "Lucas";
  const what =
    `${row.location_name} ${METRIC_SHORT[row.metric] || row.metric} recommendation`;
  if (action === "close") {
    return `${who} closed the ${what}.`;
  }
  if (action === "relaunch") {
    return `${who} relaunched the ${what} with a new check-by date of ${checkBy}.`;
  }
  if (action === "escalate") {
    return `${who} escalated the ${what} to both regional partners.`;
  }
  if (action === "attest") {
    const said =
      row.execution === "done"
        ? "was carried out"
        : row.execution === "not_done"
          ? "was not carried out"
          : "is no longer confirmed either way";
    return `${who} confirmed the ${what} ${said}.`;
  }
  return `${who} dismissed the ${what} — "${note}".`;
}

class LedgerDecider {
  constructor(repoRoot, pythonBin, threadStore) {
    this.repoRoot = repoRoot;
    this.pythonBin = pythonBin;
    this.threadStore = threadStore || null;
  }

  // Returns {ok, action, row, entry} or {error, status} — never throws.
  async decide({ recId, action, note, newCheckBy, asOf, by, execution } = {}) {
    if (typeof recId !== "string" || !REC_ID_RE.test(recId)) {
      return {
        error: 'Body must include a recId like "REC-2026-04-27-mount-pleasant-staff_call_outs".',
        status: 400,
      };
    }
    if (!ACTIONS[action]) {
      return {
        error: `action must be one of ${Object.keys(ACTIONS).join(", ")}.`,
        status: 400,
      };
    }
    const reason = typeof note === "string" ? note.trim() : "";
    if (ACTIONS[action].needsNote && !reason) {
      return {
        error:
          "Dismissing a recommendation needs a short reason — it is what the next " +
          "run's memory keeps.",
        status: 400,
      };
    }
    if (ACTIONS[action].needsDate && !DATE_RE.test(String(newCheckBy || ""))) {
      return {
        error: "Relaunching needs a new check-by date (newCheckBy, YYYY-MM-DD).",
        status: 400,
      };
    }
    if (ACTIONS[action].needsExecution && !["done", "not_done"].includes(execution)) {
      return {
        error: 'Confirming whether this happened needs execution "done" or "not_done".',
        status: 400,
      };
    }
    if (reason.length > 500) {
      return { error: `That reason is ${reason.length} characters; keep it under 500.`, status: 400 };
    }

    const args = [
      "-u",
      "-m",
      "pipeline.ledger",
      "--decide",
      recId,
      "--action",
      action,
      "--by",
      by || "Lucas",
      "--json",
    ];
    if (reason) args.push("--note", reason);
    if (ACTIONS[action].needsDate) args.push("--check-by", String(newCheckBy));
    if (ACTIONS[action].needsExecution) args.push("--execution", String(execution));

    const result = await this._spawn(args);
    if (result.error) return result;

    const row = result.row;
    const text = decisionText(action, row, { by, note: reason, checkBy: newCheckBy });
    let entry = null;
    if (this.threadStore && asOf) {
      try {
        entry = this.threadStore.append(asOf, "user", "decision", {
          kind: "ledger",
          rec_id: row.rec_id,
          action,
          status: row.status,
          note: reason || null,
          check_by: row.check_by,
          by: by || null,
          text,
        });
      } catch {
        // a thread that cannot be written must not undo a recorded decision
      }
    }
    return { ok: true, action, row, text, entry };
  }

  _spawn(args) {
    return new Promise((resolve) => {
      const child = spawn(this.pythonBin, args, {
        cwd: this.repoRoot,
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
        stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (c) => (stdout += c.toString("utf8")));
      child.stderr.on("data", (c) => (stderr += c.toString("utf8")));

      let timedOut = false;
      const killTimer = setTimeout(() => {
        timedOut = true;
        try {
          child.kill("SIGTERM");
        } catch {
          // already gone
        }
      }, DECIDE_TIMEOUT_MS);

      let settled = false;
      const settle = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(killTimer);
        resolve(value);
      };

      child.on("error", (err) =>
        settle({
          error:
            `The decision was not recorded: ${path.basename(this.pythonBin)} failed to ` +
            `start (${err.message}).`,
          status: 500,
        })
      );

      child.on("close", (code) => {
        if (timedOut) {
          return settle({
            error: `pipeline.ledger did not answer within ${Math.round(
              DECIDE_TIMEOUT_MS / 1000
            )}s — nothing was recorded.`,
            status: 504,
          });
        }
        if (code !== 0) {
          const said =
            stderr.trim().split("\n").slice(-2).join(" ").slice(0, 400) ||
            stdout.trim().slice(0, 400) ||
            "no error output";
          // The pipeline's own message (unknown rec id, missing reason, bad
          // date) is the useful one — pass it through rather than a generic 500.
          return settle({ error: said, status: 400 });
        }
        let parsed;
        try {
          const line = stdout.trim().split("\n").filter(Boolean).pop();
          parsed = JSON.parse(line);
        } catch (err) {
          return settle({
            error: `pipeline.ledger returned output this server could not read (${err.message}).`,
            status: 500,
          });
        }
        if (!parsed || !parsed.row) {
          return settle({ error: "pipeline.ledger returned no row.", status: 500 });
        }
        settle({ row: parsed.row });
      });
    });
  }
}

module.exports = { LedgerDecider, ACTIONS, DECIDE_TIMEOUT_MS };
