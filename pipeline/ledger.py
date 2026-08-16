"""Step 3c — the recommendation ledger: the digest's memory.

Every recommendation the Monday digest makes becomes a tracked row with an
owner, an expected metric movement, and a check-by date. The next run does
not start fresh: it re-checks every open row against what actually happened
in DATA/TRANSLATION/ in the week(s) that followed, and says so out loud:

Two questions, kept separate, because the data can only answer one of them:

  OUTCOME    Did the number move?  working / not_working / flat /
             unverifiable. Deterministic arithmetic against the anchor the
             recommendation was created with, sized by the metric's own noise
             floor. This is all the data can prove.
  EXECUTION  Was the work actually carried out?  done / not_done / unknown.
             ATTESTED BY A HUMAN, never inferred, and `unknown` until someone
             says. A metric improving does not prove the team did the thing,
             and a flat metric does not prove they ignored it.

Combined with `reading` (where this run sits against the row's own check-by
date) they derive a `loop_state` — see derive_loop_state and docs/PIPELINE.md.
The consequences that matter:

  - Only an attested `done` whose number went the wrong way by its own
    deadline earns a DIFFERENT mechanism (pipeline.successor). If nobody has
    confirmed the work happened, the honest next step is to ask, not to invent
    a new fix for a hypothesis that was never tested.
  - Escalation follows an attested `not_done` — an accountability fact — never
    a number that failed to move.
  - An interim reading (before the row's own check-by) can neither close a row
    nor escalate it: the system does not overrule the deadline it set itself.
  - A recommendation is still never silently dropped.

Storage — ALL reads and writes go through the Ledger class, so the backend
can later swap to a warehouse table without touching pipeline logic:

  DATA/OUTPUTS/ledger.csv        state table, one row per recommendation
  DATA/OUTPUTS/ledger_log.jsonl  append-only receipts — every event, with the
                            numbers behind it (creation anchors, recheck
                            arithmetic, human decisions)

Recommendations are falsifiable — who / what / expected movement / check-by
— and grounded in provider-level data where the metric has it (how many of
the center's doctors carry the slide). Every classification here is
deterministic arithmetic; the only LLM in this step writes the LANGUAGE of a
successor recommendation (pipeline.successor), and every sentence of it is
number-checked, language-checked and rule-checked before it lands, with a
deterministic successor as the floor. Human decisions (close /
relaunch / escalate / dismiss with a reason) are first-class events too — the
gets-better-with-use memory.

Scope note: action plans have their own loop — Claimed vs. Verified
re-judges every plan every run — so plan-level actions are tracked there.
The ledger tracks the net-new recommendations the signal engine raises.

CLI (the human-decision path):
    python -m pipeline.ledger --show
    python -m pipeline.ledger --decide REC-2026-04-27-mount-pleasant-staff_call_outs \
        --action dismiss --note "Known cause: two medical leaves" --by "Dr. Priya Raghunathan"
    python -m pipeline.ledger --decide REC-... --action relaunch --check-by 2026-05-25 --json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import config, harness, successor
from .facts import fmt_short, window_level
from .signals import (
    BASELINE_WEEKS,
    RECENT_WEEKS,
    SCALE_FLOORS,
    latest_complete_week,
    load_data,
)

LEDGER_CSV = config.OUTPUTS_DIR / "ledger.csv"
LEDGER_LOG = config.OUTPUTS_DIR / "ledger_log.jsonl"
ARCHIVE_DIR = config.OUTPUTS_DIR / "ledger_archive"

FIELDNAMES = [
    "rec_id",
    "created_week",
    "location_id",
    "location_name",
    "metric",
    "recommendation",
    "owner",
    "expected_direction",
    "check_by",
    "status",
    "last_checked",
    "outcome_note",
    "escalation_level",
    # Lineage: a successor points back at the recommendation it replaces, and
    # that recommendation points forward at its successor. A superseded row
    # stops being re-checked (its successor carries the metric now) but stays
    # on file — the chain is the memory.
    "supersedes",
    "superseded_by",
    # The last human decision recorded on this row (close /
    # relaunch / escalate / dismiss), so the UI can show a resolved state that
    # came from the data rather than from local state.
    "decision",
    # --- v2: execution and outcome are separate dimensions --------------------
    # `execution` is ATTESTED BY A HUMAN and never inferred from a metric; a
    # number moving does not prove anyone acted. `outcome` is the deterministic
    # arithmetic (unchanged from v1). `reading` says where this run sits
    # relative to the row's own check-by, so an early look cannot close a row.
    # Contract: docs/PIPELINE.md.
    "execution",
    "execution_by",
    "execution_at",
    "outcome",
    "reading",
    # Whether the data supports the INTERVENTION or only the signal.
    "action_basis",
    "action_assumption",
]

# One Monday out. The digest is weekly, the data is weekly, and a leader reads
# this on a Monday — so a recommendation made this Monday is answered next
# Monday. A two-week check-by sounds more generous (a week to land the fix, a
# week to measure it) but it means the Monday digest can never close its own
# loop: every re-check lands before its own deadline and is only ever an
# interim reading. One week is a short read, and the digest says so — the
# re-check note names how many weeks of data it actually had.
CHECK_BY_DAYS = 7

# Rows in these statuses are re-checked every run; terminal rows are history.
# (A row that has been superseded is excluded separately — see active_rows.)
# `status` is LIFECYCLE ONLY. It carries no measurement claim and no claim
# about whether a human did anything — those live in `outcome` and `execution`.
ACTIVE_STATUSES = ("open", "escalated")
TERMINAL_STATUSES = ("closed", "dismissed")

STATUS_DISPLAY = {
    "open": "Open",
    "escalated": "Escalated — raised to both regional partners",
    "closed": "Closed",
    "dismissed": "Dismissed by a human decision",
    "superseded": "Superseded by a next move",
}

# What the numbers did. Deterministic; the arithmetic is unchanged from v1.
OUTCOME_DISPLAY = {
    "pending": "Not yet re-checked",
    "working": "Moved the right way",
    "not_working": "Moved the wrong way",
    "flat": "No meaningful movement",
    "unverifiable": "Could not be verified — no usable data",
}

# Whether the recommendation was actually carried out. ATTESTED BY A HUMAN.
# Nothing in this module may infer it from a metric.
EXECUTION_DISPLAY = {
    "unknown": "Not confirmed",
    "done": "Done",
    "not_done": "Not done",
}

# Where this run sits relative to the row's own check-by date.
READING_DISPLAY = {
    "pending": "Not yet re-checked",
    "interim": "Interim reading — not due yet",
    "due": "Due",
    "overdue": "Past due",
}

# execution x outcome x maturity -> what the leader is told, and what happens.
# Derived on read, never stored. Table: docs/PIPELINE.md.
LOOP_STATE_DISPLAY = {
    "pending": "Open — awaiting first re-check",
    "unverifiable": "Can't verify — no usable data",
    "confirmed_working": "Done, and it worked — closing with credit",
    "in_flight": "Done — too early to tell yet",
    "intervention_failed": "Done, and it didn't work — next move ready",
    "not_executed": "Not done — an answer is owed",
    "unattributed_gain": "Moved the right way — not confirmed anyone acted",
    "needs_attestation": "Was this tried? — answer before we change the fix",
    "awaiting_evidence": "Too early to tell",
}

# Only this state earns a DIFFERENT mechanism: it is the one case where we know
# what was tried and know it did not work. Everything else either has not been
# tried, has not been confirmed, or has not had time.
SUCCESSOR_STATE = "intervention_failed"


def compute_reading(check_by: str | None, as_of: date, checked: bool = True) -> str:
    """Where `as_of` sits relative to the row's own check-by date. An `interim`
    reading is real data, but the system does not get to overrule the deadline
    it set itself: interim can neither close a row nor escalate it."""
    if not checked or not check_by:
        return "pending"
    due = date.fromisoformat(check_by)
    if as_of < due:
        return "interim"
    return "due" if as_of == due else "overdue"


def derive_loop_state(execution: str, outcome: str, reading: str) -> str:
    """The single place execution, outcome and timing combine. Stored nowhere —
    a row's state is always recomputed from its three independent fields."""
    if outcome == "pending":
        return "pending"
    if outcome == "unverifiable":
        return "unverifiable"
    mature = reading in ("due", "overdue")
    if execution == "not_done":
        return "not_executed"
    if execution == "done":
        if not mature:
            return "in_flight"
        return "confirmed_working" if outcome == "working" else "intervention_failed"
    # execution unknown — the honest states. We can say what the number did; we
    # cannot say whether anyone acted, so we never claim it.
    if outcome == "working":
        return "unattributed_gain"
    return "needs_attestation" if mature else "awaiting_evidence"


# A v1 ledger collapsed all of this into `status`. Migrate on read so existing
# rows keep their history. Mapping documented in docs/PIPELINE.md.
V1_STATUS_MIGRATION = {
    "acted_working": ("closed", "working"),
    "acted_not_working": ("open", "not_working"),
    "ignored": ("open", "flat"),
}

# What each UI action means in the state table. Dismiss is the only one that
# requires a reason; relaunch is the only one that needs a new date.
DECISION_ACTIONS = {
    "close": "closed",
    "relaunch": "open",
    "escalate": "escalated",
    "dismiss": "dismissed",
    # `attest` is the only action that writes `execution`, and the only one
    # that does NOT change `status`: saying "we did it" is not a lifecycle
    # event, it is evidence about the world that the re-check needs.
    "attest": None,
}

EXECUTION_VALUES = ("unknown", "done", "not_done")

# Who owns the fix: the center's Regional Medical Partner for doctor-behavior
# metrics, the Regional Operating Partner for operational ones.
MEDICAL_METRICS = {
    "recheck_compliance_pct",
    "record_completion_24h_pct",
    "callback_compliance_pct",
    "client_csat",
    "appts_per_doctor_hour",
}

# Metrics provider_weekly can ground at the individual-doctor level.
PROVIDER_METRICS = {
    "recheck_compliance_pct",
    "record_completion_24h_pct",
    "appts_per_doctor_hour",
}

# Deterministic "what to do" verb phrase per metric (template layer; an LLM
# narrative can be layered on later — the numbers always come from receipts).
ACTION_HINTS = {
    "appts_per_doctor_hour": "review scheduling density and appointment mix",
    "recheck_compliance_pct": "reinstate recheck booking at checkout",
    "record_completion_24h_pct": "reset same-day charting expectations",
    "callback_compliance_pct": "reinstate the post-visit callback list",
    "client_csat": "review recent client feedback with the team",
    "avg_wait_time_min": "walk the check-in-to-exam-room flow",
    "revenue_per_appt": "review visit mix and estimate adherence",
    "membership_conversion_pct": "re-train the membership offer at checkout",
    "staff_call_outs": "review the call-out log and shift coverage plan",
    "no_show_rate": "tighten appointment confirmation outreach",
    "open_dvm_requisitions": "review the doctor recruiting pipeline",
}


def _r(v, nd: int = 3):
    """JSON-safe rounding (None / NaN pass through as None)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return round(float(v), nd)


def center_slug(name: str) -> str:
    """Leader-safe center identifier for rec_ids ("Mount Pleasant" →
    "mount-pleasant"). rec_ids may appear in the digest UI, and location IDs
    never reach a leader — so the ID is built from the real center name
    (unique across all 47 centers)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _loop_state_note(
    loop_state: str, row: dict, name: str, metric_l: str, as_of: date
) -> str:
    """The sentence that follows the measured fact. Each one is careful to
    claim only what is actually known: the numbers are measured, execution is
    attested, and where nobody has attested we say so instead of guessing."""
    owner = row["owner"]
    check_by = row["check_by"]
    if loop_state == "confirmed_working":
        return f"{owner} confirmed the work was done, so this closes with credit."
    if loop_state == "intervention_failed":
        return (
            f"{owner} confirmed the work was done, so the approach itself is not "
            f"moving this — a different mechanism takes over."
        )
    if loop_state == "not_executed":
        return (
            f"{owner} confirmed this was not carried out, so the same ask is "
            f"re-issued with the escalation attached."
        )
    if loop_state == "unattributed_gain":
        return (
            f"Nobody has confirmed whether the recommended work happened, so the "
            f"improvement is credited to {name} without being claimed as this "
            f"recommendation's result."
        )
    if loop_state == "needs_attestation":
        return (
            f"Nobody has confirmed whether the recommended work happened. That "
            f"answer comes before a different fix is designed — asking {owner}."
        )
    if loop_state == "awaiting_evidence":
        return f"Too early to read: this is not due until {check_by}."
    if loop_state == "in_flight":
        return f"{owner} confirmed the work was done; not due until {check_by}."
    if loop_state == "unverifiable":
        return "Carried forward until it can be checked."
    return ""


def _lower_first(text: str) -> str:
    return text[0].lower() + text[1:] if text else text


def _decision_line(action: str | None, decision: str, check_by: str | None) -> str:
    """The standard sentence for a decision the human left unexplained. Only
    a dismissal is required to carry its own reason."""
    if action == "close":
        return "closed it out."
    if action == "relaunch":
        return f"relaunched with a new check-by date of {check_by}."
    if action == "escalate":
        return "escalated to both regional partners."
    return f"status set to {decision}."


# ---------------------------------------------------------------------------
# Storage — the single door to ledger.csv + ledger_log.jsonl
# ---------------------------------------------------------------------------


class Ledger:
    """State table + append-only receipts. Swap this class's internals to
    move the ledger into a warehouse; nothing else in the pipeline changes."""

    def __init__(self, csv_path: Path = LEDGER_CSV, log_path: Path = LEDGER_LOG):
        self.csv_path = csv_path
        self.log_path = log_path
        self.rows: list[dict] = self._read()

    def _read(self) -> list[dict]:
        if not self.csv_path.exists():
            return []
        with self.csv_path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            r["escalation_level"] = int(r.get("escalation_level") or 0)
            # A ledger written before these columns existed reads back with
            # them empty rather than missing.
            for field in (
                "supersedes", "superseded_by", "decision",
                "execution_by", "execution_at", "action_basis", "action_assumption",
            ):
                r[field] = r.get(field) or ""
            # A v1 ledger stored one collapsed `status`. Split it, so old rows
            # keep their history without ever gaining an execution claim they
            # never earned: migrated rows are `unknown` by definition.
            if not r.get("execution"):
                r["execution"] = "unknown"
            if not r.get("outcome"):
                status, outcome = V1_STATUS_MIGRATION.get(
                    r.get("status", ""), (r.get("status", "open"), "pending")
                )
                r["status"], r["outcome"] = status, outcome
            if not r.get("reading"):
                r["reading"] = "pending" if not r.get("last_checked") else "due"
        return rows

    def save(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            writer.writeheader()
            for r in self.rows:
                writer.writerow({k: r.get(k, "") for k in FIELDNAMES})

    def event(self, event: str, payload: dict) -> None:
        """Append one receipt line — the numbers behind every state change."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "step": "ledger",
            "event": event,
            **payload,
        }
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(line) + "\n")

    def events(self) -> list[dict]:
        """Every receipt on file (the append-only log), oldest first."""
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text().splitlines()
            if line.strip()
        ]

    def last_event(self, event: str, **match) -> dict | None:
        """The most recent receipt of one kind matching the given fields — how
        a replayed run recovers what it already wrote (e.g. a successor's
        generated text) instead of generating it a second time."""
        for e in reversed(self.events()):
            if e.get("event") != event:
                continue
            if all(e.get(k) == v for k, v in match.items()):
                return e
        return None

    # -- queries ------------------------------------------------------------

    def active_rows(self) -> list[dict]:
        """Rows still being carried. A superseded row is not one of them: its
        successor now owns that center-metric, so re-checking both would
        escalate the same problem twice."""
        return [
            r
            for r in self.rows
            if r["status"] in ACTIVE_STATUSES and not r.get("superseded_by")
        ]

    def find_active(self, location_id: str, metric: str) -> dict | None:
        for r in self.active_rows():
            if r["location_id"] == location_id and r["metric"] == metric:
                return r
        return None

    def find_terminal(self, location_id: str, metric: str) -> dict | None:
        for r in reversed(self.rows):
            if (
                r["location_id"] == location_id
                and r["metric"] == metric
                and r["status"] in TERMINAL_STATUSES
            ):
                return r
        return None

    def get(self, rec_id: str) -> dict | None:
        for r in self.rows:
            if r["rec_id"] == rec_id:
                return r
        return None

    def add(self, row: dict) -> None:
        self.rows.append(row)

    # -- human decisions ----------------------------------------------------

    def record_human_decision(
        self,
        rec_id: str,
        decision: str,
        note: str,
        actor: str,
        action: str | None = None,
        check_by: str | None = None,
        execution: str | None = None,
    ) -> dict:
        """Record what a human decided about a recommendation — logged, never
        silent. `decision` is the resulting state; `action` is the button that
        produced it (close / relaunch / escalate / dismiss), kept
        so the UI can render a resolved state straight from the data.

        A dismissal must carry a reason — dropping a tracked recommendation is
        exactly the decision that needs one. The others get a standard line
        when the human adds nothing.
        """
        if action == "attest":
            return self.record_attestation(rec_id, execution, actor, note)
        allowed = {"closed", "dismissed", "open", "escalated"}
        if decision not in allowed:
            raise ValueError(f"decision must be one of {sorted(allowed)}, got {decision!r}")
        if action is not None and action not in DECISION_ACTIONS:
            raise ValueError(
                f"action must be one of {sorted(DECISION_ACTIONS)}, got {action!r}"
            )
        note = (note or "").strip()
        if decision == "dismissed" and not note:
            raise ValueError("dismissing a recommendation needs a reason (--note)")
        if action == "relaunch" and not check_by:
            raise ValueError("relaunching a recommendation needs a new check-by date (--check-by)")
        if check_by:
            date.fromisoformat(check_by)  # ValueError on a malformed date
        row = self.get(rec_id)
        if row is None:
            raise KeyError(f"no ledger row with rec_id {rec_id!r}")

        before = row["status"]
        previous_check_by = row["check_by"]
        row["status"] = decision
        if action:
            row["decision"] = action
        if check_by:
            row["check_by"] = check_by
        if decision == "open":
            # Relaunched: the clock restarts, so the next run re-checks it
            # against the weeks that follow this decision, not the old ones.
            row["last_checked"] = ""
        row["outcome_note"] = f"{actor}: {note or _decision_line(action, decision, check_by)}"
        self.event(
            "human_decision",
            {
                "rec_id": rec_id,
                "actor": actor,
                "action": action,
                "decision": decision,
                "status_before": before,
                "check_by": row["check_by"],
                "check_by_before": previous_check_by,
                "note": row["outcome_note"],
            },
        )
        self.save()
        return row

    def record_attestation(
        self, rec_id: str, execution: str | None, actor: str, note: str = ""
    ) -> dict:
        """A human says whether the recommendation was actually carried out.

        This is the ONLY way `execution` is ever written. Nothing in this
        pipeline infers it from a metric: a number moving the right way does
        not prove the team did the thing, and a flat number does not prove they
        ignored it. Until someone says, the answer is `unknown` and the digest
        says so out loud.

        Attesting does not change `status` — it is evidence, not a lifecycle
        event. The next re-check reads it and decides what follows.
        """
        if execution not in EXECUTION_VALUES:
            raise ValueError(
                f"execution must be one of {sorted(EXECUTION_VALUES)}, got {execution!r}"
            )
        row = self.get(rec_id)
        if row is None:
            raise KeyError(f"no ledger row with rec_id {rec_id!r}")
        before = row.get("execution") or "unknown"
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row["execution"] = execution
        row["execution_by"] = actor
        row["execution_at"] = stamp
        row["decision"] = "attest"
        note = (note or "").strip()
        row["outcome_note"] = f"{actor}: {note or _attestation_line(execution)}"
        self.event(
            "execution_attested",
            {
                "rec_id": rec_id,
                "actor": actor,
                "action": "attest",
                "execution": execution,
                "execution_before": before,
                "execution_at": stamp,
                "note": row["outcome_note"],
            },
        )
        self.save()
        return row


def _attestation_line(execution: str) -> str:
    if execution == "done":
        return "confirmed this was carried out."
    if execution == "not_done":
        return "confirmed this was not carried out."
    return "set execution back to not confirmed."


def reset_ledger() -> str | None:
    """Archive the current ledger files (never delete receipts) and start
    fresh. Returns the archive directory, or None when there was nothing."""
    if not LEDGER_CSV.exists() and not LEDGER_LOG.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = ARCHIVE_DIR / stamp
    target.mkdir(parents=True, exist_ok=True)
    for p in (LEDGER_CSV, LEDGER_LOG):
        if p.exists():
            shutil.move(str(p), str(target / p.name))
    return str(target)


# ---------------------------------------------------------------------------
# (a) Re-check every open row against what actually happened
# ---------------------------------------------------------------------------


def recheck_open_rows(
    ledger: Ledger, as_of: date, cw: pd.DataFrame
) -> list[dict]:
    """For every active row created before this run, compare the real week(s)
    that followed against the 4-week level the recommendation was anchored to.

    The anchor is *defined* as the metric's 4-week level ending at the last
    complete week of the row's created_week — recomputed deterministically
    from DATA/TRANSLATION/ so the re-check never depends on parsing its own history.

    Re-running the same Monday is idempotent: rows already checked this week
    replay their stored outcome (no double escalation, no duplicate state
    change) so digest.json comes out the same.
    """
    new_latest = latest_complete_week(as_of)
    results: list[dict] = []

    recheck_outcomes = ("working", "not_working", "flat", "unverifiable")
    for row in ledger.rows:
        created = date.fromisoformat(row["created_week"])
        if created >= as_of:
            continue  # created this run (or later) — nothing to re-check yet

        replay = row["last_checked"] == str(as_of) and row.get("outcome") in recheck_outcomes
        if not replay:
            if row.get("superseded_by"):
                continue  # replaced in an earlier run — its successor is the live row
            if row["status"] not in ACTIVE_STATUSES:
                continue  # closed / dismissed / superseded in an earlier week — history
            if row["last_checked"] and date.fromisoformat(row["last_checked"]) >= as_of:
                continue  # checked by a later run — never rewind state

        metric = config.METRICS[row["metric"]]
        g = cw[cw["location_id"] == row["location_id"]]
        created_latest = pd.Timestamp(created) - pd.Timedelta(days=7)

        anchor, anchor_weeks = window_level(
            g, metric, created_latest - pd.Timedelta(weeks=RECENT_WEEKS), created_latest
        )
        follow, follow_weeks = window_level(g, metric, created_latest, new_latest)

        # Weekly receipts for the follow-up window — the actual new data. The
        # successor pass reads these too ("4 → 7 → 9 → 9"), so they are built
        # before the replay branch and carried on every re-check entry.
        col = metric.column or metric.key
        win = g[(g["week_start"] > created_latest) & (g["week_start"] <= new_latest)]
        weekly = [
            {
                "week_start": str(w.date()),
                "value": _r(v),
                "display": fmt_short(metric, None if pd.isna(v) else v),
            }
            for w, v in zip(win["week_start"], win[col])
        ]
        floor = SCALE_FLOORS[metric.kind]

        # Replay: already re-checked for this very week — report the stored
        # outcome (no second escalation, no state change), same shape.
        if replay:
            ledger.event(
                "rec_recheck_replayed",
                {"rec_id": row["rec_id"], "as_of": str(as_of), "outcome": row.get("outcome")},
            )
            results.append(
                {
                    "rec_id": row["rec_id"],
                    "center": row["location_name"],
                    "location_id": row["location_id"],  # audit trail only
                    "metric": metric.key,
                    "metric_display": metric.display_name,
                    "owner": row["owner"],
                    "expected_direction": row["expected_direction"],
                    "created_week": row["created_week"],
                    "check_by": row["check_by"],
                    "outcome": row.get("outcome"),
                    "outcome_display": OUTCOME_DISPLAY.get(row.get("outcome"), ""),
                    "execution": row.get("execution") or "unknown",
                    "execution_display": EXECUTION_DISPLAY.get(
                        row.get("execution") or "unknown", ""
                    ),
                    "reading": row.get("reading") or "pending",
                    "loop_state": derive_loop_state(
                        row.get("execution") or "unknown",
                        row.get("outcome") or "pending",
                        row.get("reading") or "pending",
                    ),
                    "loop_state_display": LOOP_STATE_DISPLAY.get(
                        derive_loop_state(
                            row.get("execution") or "unknown",
                            row.get("outcome") or "pending",
                            row.get("reading") or "pending",
                        ),
                        "",
                    ),
                    "escalation_level": int(row["escalation_level"]),
                    "anchor_level_display": fmt_short(metric, anchor),
                    "followup_level_display": fmt_short(metric, follow),
                    "followup_weeks": follow_weeks,
                    "followup_weekly_values": weekly,
                    "noise_floor": floor,
                    "note": row["outcome_note"],
                    "recommendation": row["recommendation"],
                }
            )
            continue

        expected = row["expected_direction"]  # "up" or "down"
        name = row["location_name"]
        metric_l = _lower_first(metric.display_name)
        weeks_word = f"{follow_weeks} week" + ("s" if follow_weeks != 1 else "")

        # ------------------------------------------------------------------
        # (1) OUTCOME — what the numbers did. Deterministic, and the ONLY thing
        # arithmetic is allowed to conclude. It says nothing about whether a
        # human acted, because the data cannot know that.
        # ------------------------------------------------------------------
        if anchor is None or follow is None:
            outcome = "unverifiable"
            movement = None
            fact = (
                f"No usable {metric_l} data at {name} in the week(s) since "
                f"{row['created_week']}, so the effect cannot be measured yet."
            )
        else:
            toward = (follow - anchor) if expected == "up" else (anchor - follow)
            movement = follow - anchor
            a_disp, f_disp = fmt_short(metric, anchor), fmt_short(metric, follow)
            if toward >= floor:
                outcome = "working"
                fact = (
                    f"{metric_l.capitalize()} at {name} averaged {f_disp} over the "
                    f"{weeks_word} since the recommendation, vs the {a_disp} it was "
                    f"anchored to — moving the right way."
                )
            elif toward <= -floor:
                outcome = "not_working"
                fact = (
                    f"{metric_l.capitalize()} at {name} averaged {f_disp} over the "
                    f"{weeks_word} since the recommendation, vs the {a_disp} it was "
                    f"anchored to — moving the wrong way."
                )
            else:
                outcome = "flat"
                fact = (
                    f"{metric_l.capitalize()} at {name} averaged {f_disp} over the "
                    f"{weeks_word} since the recommendation (anchored at {a_disp}), "
                    f"a move smaller than the {floor:g} noise floor."
                )

        # ------------------------------------------------------------------
        # (2) EXECUTION — read, never written here. Only a human attestation
        # sets it (Ledger.record_attestation). Default `unknown`.
        # (3) READING — an interim look is real data, but it cannot close a row
        # or escalate one: the system does not overrule the deadline it set.
        # ------------------------------------------------------------------
        execution = row.get("execution") or "unknown"
        reading = compute_reading(row["check_by"], as_of)
        loop_state = derive_loop_state(execution, outcome, reading)

        note = fact + " " + _loop_state_note(
            loop_state, row, name, metric_l, as_of
        )

        # Escalation is an ACCOUNTABILITY event, so it fires only when a human
        # has said the work did not happen — never because a number sat still.
        if loop_state == "not_executed":
            row["escalation_level"] = int(row["escalation_level"]) + 1
            note += f" Escalation level {row['escalation_level']}."
        if reading == "overdue":
            note += f" Past its check-by date ({row['check_by']})."

        # `status` is lifecycle only. The one outcome-driven transition is a
        # confirmed win: attested done AND measured working AND actually due.
        if loop_state == "confirmed_working":
            row["status"] = "closed"
        elif loop_state == "not_executed":
            row["status"] = "escalated"
        row["outcome"] = outcome
        row["reading"] = reading
        row["last_checked"] = str(as_of)
        row["outcome_note"] = note

        receipt = {
            "rec_id": row["rec_id"],
            "as_of": str(as_of),
            "location_id": row["location_id"],
            "center": name,
            "metric": metric.key,
            "expected_direction": expected,
            "anchor_level": _r(anchor),
            "anchor_weeks": anchor_weeks,
            "followup_level": _r(follow),
            "followup_weeks": follow_weeks,
            "followup_weekly_values": weekly,
            "movement": _r(movement),
            "noise_floor": floor,
            "outcome": outcome,
            "execution": execution,
            "execution_attested_by": row.get("execution_by") or None,
            "reading": reading,
            "loop_state": loop_state,
            "escalation_level": row["escalation_level"],
            "note": note,
        }
        ledger.event("rec_rechecked", receipt)

        results.append(
            {
                "rec_id": row["rec_id"],
                "center": name,
                "location_id": row["location_id"],  # audit trail only
                "metric": metric.key,
                "metric_display": metric.display_name,
                "owner": row["owner"],
                "expected_direction": expected,
                "created_week": row["created_week"],
                "check_by": row["check_by"],
                "outcome": outcome,
                "outcome_display": OUTCOME_DISPLAY[outcome],
                "execution": execution,
                "execution_display": EXECUTION_DISPLAY[execution],
                "reading": reading,
                "reading_display": READING_DISPLAY[reading],
                "loop_state": loop_state,
                "loop_state_display": LOOP_STATE_DISPLAY[loop_state],
                "escalation_level": row["escalation_level"],
                "anchor_level_display": fmt_short(metric, anchor),
                "followup_level_display": fmt_short(metric, follow),
                "followup_weeks": follow_weeks,
                "followup_weekly_values": weekly,
                "noise_floor": floor,
                "note": note,
                "recommendation": row["recommendation"],
            }
        )

    return results


# ---------------------------------------------------------------------------
# (b) Append new recommendations from this run's top signals
# ---------------------------------------------------------------------------


def provider_grounding(
    pw: pd.DataFrame, location_id: str, metric: config.Metric, latest: pd.Timestamp, center: str
) -> tuple[str | None, dict | None]:
    """How many of the center's doctors carry the slide, from provider_weekly.

    Each doctor's last-4-week level is compared with their own prior 12-week
    norm; a doctor "carries the slide" when their own move in the bad
    direction exceeds the metric's noise floor. Provider IDs stay in the
    receipts — a leader sees counts, never IDs.
    """
    if metric.key not in PROVIDER_METRICS:
        return None, None
    sub = pw[(pw["location_id"] == location_id) & (pw["week_start"] <= latest)]
    if sub.empty:
        return None, None
    recent_cut = latest - pd.Timedelta(weeks=RECENT_WEEKS)
    base_cut = recent_cut - pd.Timedelta(weeks=BASELINE_WEEKS)

    def level(win: pd.DataFrame) -> float | None:
        if win.empty:
            return None
        if metric.key == "appts_per_doctor_hour":
            hours = win["scheduled_hours"].sum()
            return float(win["appts_completed"].sum() / hours) if hours > 0 else None
        vals = win[metric.column].dropna()
        return float(vals.mean()) if len(vals) else None

    floor = SCALE_FLOORS[metric.kind]
    per: list[dict] = []
    for pid, gg in sub.groupby("provider_id"):
        recent = gg[gg["week_start"] > recent_cut]
        if recent.empty:
            continue  # not working at this center in the last 4 weeks
        r = level(recent)
        b = level(gg[(gg["week_start"] > base_cut) & (gg["week_start"] <= recent_cut)])
        if r is None or b is None:
            continue
        move = (b - r) if metric.bad_direction == "down" else (r - b)
        per.append(
            {
                "provider_id": pid,  # receipts only — never leader-facing
                "recent_4wk": _r(r),
                "own_12wk_norm": _r(b),
                "bad_move": _r(move),
                "sliding": bool(move > floor),
                "recent_weeks": len(recent),
            }
        )
    if not per:
        return None, None

    sliding = [p for p in per if p["sliding"]]
    n, total = len(sliding), len(per)
    if sliding:
        worst = max(sliding, key=lambda p: p["bad_move"])
        sentence = (
            f"{n} of the center's {total} doctors carry the slide (each is off "
            f"their own 12-week norm over the last 4 weeks; the furthest is at "
            f"{fmt_short(metric, worst['recent_4wk'])} vs a personal norm of "
            f"{fmt_short(metric, worst['own_12wk_norm'])})"
        )
    else:
        sentence = (
            f"the slip is spread across all {total} of the center's doctors "
            f"rather than carried by one or two"
        )
    receipt = {
        "source": "provider_weekly",
        "doctors_active_4wk": total,
        "doctors_sliding": n,
        "noise_floor": floor,
        "per_provider": per,
    }
    return sentence, receipt


def _reference_from_signal(sig: dict, metric: config.Metric) -> tuple[float | None, str | None]:
    """The falsifiable target level: the center's own 12-week norm when the
    drift score has one, else the peer-group median from the gap score."""
    drift = sig["scores"].get("drift") or {}
    if drift.get("available") and drift.get("baseline_level") is not None:
        return float(drift["baseline_level"]), f"its own {BASELINE_WEEKS}-week norm"
    gap = sig["scores"].get("gap") or {}
    if gap.get("available") and gap.get("peer_median") is not None:
        return float(gap["peer_median"]), "its peer-group median"
    return None, None


def _successor_evidence(context: dict, has_provider_detail: bool) -> tuple[str, str]:
    """A successor's evidence is strictly better than the first attempt's: the
    previous mechanism was carried out and demonstrably did not move the
    number. That is a falsified hypothesis, which is information — but it still
    is not a diagnosis, so the tier stays honest about what remains unproven."""
    center = context["center"]
    metric_l = _lower_first(context["metric_display"])
    check_by = context["check_by"]
    ruled_out = (
        f"What the last round established: the previous approach was carried "
        f"out at {center} and {metric_l} still did not move, so that mechanism "
        f"is ruled out."
    )
    if has_provider_detail:
        return (
            "measured",
            f"{ruled_out} Provider-level data also shows how the slip is spread "
            f"across the roster, which narrows what is worth trying. The new play "
            f"is still a hypothesis — it is tested by {check_by}.",
        )
    return (
        "playbook",
        f"{ruled_out} Nothing in these four datasets says why. The new play is "
        f"the next standard move — if it does not shift by {check_by}, that is "
        f"information too.",
    )


def _action_evidence(
    grounding_receipt: dict | None, metric_l: str, center: str, check_by: str
) -> tuple[str, str]:
    """Does the data support the INTERVENTION, or only the signal?

    The harness can prove a number is off its norm and worst among its peers.
    It cannot prove WHY. `ACTION_HINTS` are operating hypotheses, and presenting
    a hypothesis as a diagnosis is how a trustworthy system starts lying.

    Provider-level data is the one thing that constrains the answer: it shows
    whether the problem is concentrated in one or two doctors or spread across
    the whole roster, which rules interventions in and out. That is `measured`.
    Everything else is `playbook` — the standard first move, stated as such.
    """
    if grounding_receipt:
        total = grounding_receipt.get("doctors_active_4wk")
        sliding = grounding_receipt.get("doctors_sliding") or 0
        if not total:
            shape = "the provider panel has no active doctors to classify"
        elif sliding == total:
            shape = (
                f"all {total} of {total} doctors are off their own norm, so this is "
                f"a broad-based, center-wide pattern"
            )
        elif sliding / total >= 0.75:
            shape = (
                f"{sliding} of {total} doctors are off their own norm — nearly all of "
                f"the roster — so this is broad-based rather than concentrated"
            )
        elif sliding * 2 < total:
            shape = (
                f"the issue is concentrated in {sliding} of {total} doctors who are "
                f"off their own norm, rather than center-wide"
            )
        elif sliding:
            shape = (
                f"{sliding} of {total} doctors are off their own norm, so the pattern "
                f"is spread across the roster rather than isolated to a small minority"
            )
        else:
            shape = (
                f"none of the {total} doctors is individually off their own norm beyond "
                f"the noise floor, so the provider data does not identify a concentrated source"
            )
        return (
            "measured",
            f"Measured: {shape}. That is what the provider data proves. The "
            f"specific play is standard practice for that pattern, not a cause "
            f"this data establishes.",
        )
    return (
        "playbook",
        f"The signal is measured; the cause is not. Nothing in these four "
        f"datasets says why {metric_l} moved at {center}. This is the standard "
        f"first move — if it does not shift by {check_by}, that is information.",
    )


def build_recommendation(
    sig: dict,
    loc_row: pd.Series,
    pw: pd.DataFrame,
    cw: pd.DataFrame,
    latest: pd.Timestamp,
    as_of: date,
) -> tuple[dict, dict]:
    """One falsifiable recommendation row (+ its receipt) from one ranked
    signal: who / what / expected movement / check-by."""
    metric = config.METRICS[sig["metric"]]
    center = sig["center"]
    lid = sig["location_id"]

    medical = metric.key in MEDICAL_METRICS
    owner = str(loc_row["rmp_name"] if medical else loc_row["rop_name"])
    owner_role = "Regional Medical Partner" if medical else "Regional Operating Partner"
    expected_direction = "up" if metric.bad_direction == "down" else "down"
    check_by = as_of + timedelta(days=CHECK_BY_DAYS)

    # The anchor the next run's re-check is measured against — computed the
    # same way recheck_open_rows will recompute it.
    g = cw[cw["location_id"] == lid]
    anchor, anchor_weeks = window_level(
        g, metric, latest - pd.Timedelta(weeks=RECENT_WEEKS), latest
    )

    grounding, grounding_receipt = provider_grounding(pw, lid, metric, latest, center)
    reference, reference_label = _reference_from_signal(sig, metric)
    metric_l = _lower_first(metric.display_name)
    action_basis, action_assumption = _action_evidence(
        grounding_receipt, metric_l, center, str(check_by)
    )

    if grounding is None:
        if reference is not None:
            grounding = (
                f"the last {RECENT_WEEKS} weeks averaged "
                f"{fmt_short(metric, anchor)} vs {reference_label} of "
                f"{fmt_short(metric, reference)}"
            )
        else:
            grounding = f"the last {RECENT_WEEKS} weeks averaged {fmt_short(metric, anchor)}"

    dir_phrase = "back up" if expected_direction == "up" else "back down"
    if reference is not None:
        expected_clause = (
            f"Expected: {metric_l} moves {dir_phrase} from "
            f"{fmt_short(metric, anchor)} toward {fmt_short(metric, reference)} "
            f"by {check_by}."
        )
    else:
        expected_clause = (
            f"Expected: {metric_l} improves from {fmt_short(metric, anchor)} by {check_by}."
        )

    recommendation = (
        f"{owner} ({owner_role}) to {ACTION_HINTS[metric.key]} at {center} — "
        f"{grounding}. {expected_clause}"
    )

    row = {
        "rec_id": f"REC-{as_of}-{center_slug(center)}-{metric.key}",
        "created_week": str(as_of),
        "location_id": lid,
        "location_name": center,
        "metric": metric.key,
        "recommendation": recommendation,
        "owner": owner,
        "expected_direction": expected_direction,
        "check_by": str(check_by),
        "status": "open",
        "last_checked": "",
        "outcome_note": "",
        "escalation_level": 0,
        # v2: execution is attested, never inferred, so a new row starts at
        # "nobody has said yet" rather than at an assumption.
        "execution": "unknown",
        "execution_by": "",
        "execution_at": "",
        "outcome": "pending",
        "reading": "pending",
        # Whether the data supports the INTERVENTION or only the signal. Kept
        # as its own field and NEVER spliced into `recommendation`: the action
        # stays one line, the epistemology sits one click deep.
        "action_basis": action_basis,
        "action_assumption": action_assumption,
    }
    receipt = {
        "rec_id": row["rec_id"],
        "as_of": str(as_of),
        "location_id": lid,
        "center": center,
        "metric": metric.key,
        "signal_priority": sig["priority"],
        "signal_sub_scores": {
            k: v.get("score") for k, v in sig["scores"].items() if isinstance(v, dict)
        },
        "anchor_level": _r(anchor),
        "anchor_weeks": anchor_weeks,
        "reference_level": _r(reference),
        "reference_label": reference_label,
        "expected_direction": expected_direction,
        "check_by": str(check_by),
        "owner": owner,
        "owner_role": owner_role,
        "provider_grounding": grounding_receipt,
        "action_basis": action_basis,
        "action_assumption": action_assumption,
        "recommendation": recommendation,
    }
    return row, receipt


def append_new_recommendations(
    ledger: Ledger,
    signals_doc: dict,
    loc: pd.DataFrame,
    pw: pd.DataFrame,
    cw: pd.DataFrame,
    as_of: date,
) -> list[dict]:
    """Turn this run's ranked signals into tracked rows. A signal whose
    (center, metric) already has an active row is not duplicated — the
    existing row keeps escalating instead."""
    latest = latest_complete_week(as_of)
    loc_by_id = loc.set_index("location_id")
    created: list[dict] = []

    for sig in signals_doc["signals"]:
        lid, metric_key = sig["location_id"], sig["metric"]
        existing = ledger.find_active(lid, metric_key)
        if existing:
            ledger.event(
                "rec_already_tracked",
                {
                    "as_of": str(as_of),
                    "rec_id": existing["rec_id"],
                    "location_id": lid,
                    "metric": metric_key,
                    "status": existing["status"],
                    "escalation_level": existing["escalation_level"],
                    "signal_priority": sig["priority"],
                },
            )
            # Replay: created earlier this same Monday — report it as this
            # run's creation so re-runs assemble the same digest.
            if existing["created_week"] == str(as_of):
                metric = config.METRICS[metric_key]
                entry = {
                    "rec_id": existing["rec_id"],
                    "center": existing["location_name"],
                    "metric": metric_key,
                    "metric_display": metric.display_name,
                    "recommendation": existing["recommendation"],
                    "owner": existing["owner"],
                    "expected_direction": existing["expected_direction"],
                    "check_by": existing["check_by"],
                    "status": existing["status"],
                    "status_display": STATUS_DISPLAY[existing["status"]],
                    "from_signal_rank": sig["rank"],
                }
                prior = ledger.find_terminal(lid, metric_key)
                if prior is not None:
                    entry["note"] = (
                        f"Re-raised: a previous recommendation ({prior['rec_id']}) "
                        f"ended as \"{STATUS_DISPLAY[prior['status']]}\", but the "
                        f"signal engine still ranks this center-metric."
                    )
                created.append(entry)
            continue

        row, receipt = build_recommendation(
            sig, loc_by_id.loc[lid], pw, cw, latest, as_of
        )
        prior = ledger.find_terminal(lid, metric_key)
        note = None
        if prior is not None:
            note = (
                f"Re-raised: a previous recommendation ({prior['rec_id']}) ended "
                f"as \"{STATUS_DISPLAY[prior['status']]}\", but the signal engine "
                f"still ranks this center-metric."
            )
            receipt["prior_rec_id"] = prior["rec_id"]
            receipt["prior_status"] = prior["status"]
        ledger.add(row)
        ledger.event("rec_created", receipt)

        metric = config.METRICS[metric_key]
        entry = {
            "rec_id": row["rec_id"],
            "center": row["location_name"],
            "metric": metric_key,
            "metric_display": metric.display_name,
            "recommendation": row["recommendation"],
            "owner": row["owner"],
            "expected_direction": row["expected_direction"],
            "check_by": row["check_by"],
            "status": row["status"],
            "status_display": STATUS_DISPLAY[row["status"]],
            "from_signal_rank": sig["rank"],
        }
        if note:
            entry["note"] = note
        created.append(entry)

    return created


# ---------------------------------------------------------------------------
# (c) Successor recommendations — a failed re-check still ends in a move
#
# A failed or unexecuted re-check used to end the loop with a shrug. It
# now end it with the next move: a different, falsifiable recommendation that
# supersedes the one that failed, written by pipeline.successor through the
# harness and tracked from this run on like any other row.
# ---------------------------------------------------------------------------


def _lineage_depth(ledger: Ledger, row: dict) -> int:
    """How many attempts this recommendation already represents (1 = the
    original ask, 2 = the first successor, …), by walking the supersedes
    chain backwards."""
    depth = 1
    seen = {row["rec_id"]}
    current = row
    while current.get("supersedes") and current["supersedes"] not in seen:
        seen.add(current["supersedes"])
        previous = ledger.get(current["supersedes"])
        if previous is None:
            break
        depth += 1
        current = previous
    return depth


def _reference_level(
    cw: pd.DataFrame, lid: str, metric: config.Metric, latest: pd.Timestamp, sig: dict | None
) -> tuple[float | None, str | None]:
    """The level the successor aims the metric back at: the center's own
    12-week norm, or the peer-group median when this run's signal carries one."""
    if sig is not None:
        level, label = _reference_from_signal(sig, metric)
        if level is not None:
            return level, label
    g = cw[cw["location_id"] == lid]
    recent_cut = latest - pd.Timedelta(weeks=RECENT_WEEKS)
    level, _weeks = window_level(
        g, metric, recent_cut - pd.Timedelta(weeks=BASELINE_WEEKS), recent_cut
    )
    return level, (f"its own {BASELINE_WEEKS}-week norm" if level is not None else None)


def _center_context(
    ledger: Ledger,
    row: dict,
    signals_doc: dict | None,
    verdicts_doc: dict | None,
) -> tuple[dict, dict | None]:
    """The rest of this run's picture for the same center — what the signal
    engine ranked, the center's other open recommendations, and the verdicts on
    its improvement plans. Returns (context, this center-metric's signal)."""
    center, metric_key = row["location_name"], row["metric"]
    signals = (signals_doc or {}).get("signals", [])
    this_signal = next(
        (s for s in signals if s["center"] == center and s["metric"] == metric_key), None
    )
    context = {
        "ranked_again_this_run": (
            {
                "rank": this_signal["rank"],
                "priority": this_signal["priority"],
                "headline": this_signal.get("headline"),
            }
            if this_signal
            else None
        ),
        "other_signals_at_this_center": [
            {"metric_display": s["metric_display"], "rank": s["rank"], "priority": s["priority"]}
            for s in signals
            if s["center"] == center and s["metric"] != metric_key
        ],
        "other_open_recommendations_here": [
            {
                "metric_display": config.METRICS[r["metric"]].display_name,
                "recommendation": r["recommendation"],
                "owner": r["owner"],
                "check_by": r["check_by"],
                "status_display": STATUS_DISPLAY.get(r["status"], r["status"]),
            }
            for r in ledger.active_rows()
            if r["location_name"] == center and r["rec_id"] != row["rec_id"]
        ],
        "improvement_plans_here": [
            {
                "metric_display": v["metric_display"],
                "reported_status": (v.get("reported_status") or {}).get("display"),
                "verdict": (v.get("ai_verdict") or {}).get("verdict_display"),
                "what_the_numbers_say": (v.get("what_the_numbers_say") or {}).get("summary"),
                "recommended_action": (v.get("ai_verdict") or {}).get("recommended_action"),
            }
            for v in (verdicts_doc or {}).get("verdicts", [])
            if v.get("center") == center
        ],
    }
    return context, this_signal


def build_successor_context(
    ledger: Ledger,
    recheck: dict,
    row: dict,
    as_of: date,
    loc_row: pd.Series,
    pw: pd.DataFrame,
    cw: pd.DataFrame,
    latest: pd.Timestamp,
    signals_doc: dict | None = None,
    verdicts_doc: dict | None = None,
) -> dict:
    """Everything the successor may know: what was tried, what the metric did
    week by week since, who carries it, and the rest of this run's picture for
    that center. This dict IS the verification pool — a successor can cite
    nothing that is not in here."""
    metric = config.METRICS[row["metric"]]
    center = row["location_name"]
    lid = row["location_id"]

    medical = metric.key in MEDICAL_METRICS
    owner_role = "Regional Medical Partner" if medical else "Regional Operating Partner"
    partner = str(loc_row["rop_name"] if medical else loc_row["rmp_name"])
    partner_role = "Regional Operating Partner" if medical else "Regional Medical Partner"

    attempt = _lineage_depth(ledger, row) + 1
    escalated = successor.escalation_required(
        recheck["loop_state"], attempt, int(row["escalation_level"])
    )
    check_by = as_of + timedelta(days=CHECK_BY_DAYS)

    grounding_sentence, grounding_receipt = provider_grounding(pw, lid, metric, latest, center)
    if grounding_receipt:  # provider ids are audit-trail only — never in a prompt
        grounding_receipt = {
            **grounding_receipt,
            "per_provider": [
                {k: v for k, v in p.items() if k != "provider_id"}
                for p in grounding_receipt["per_provider"]
            ],
        }
    reference, reference_label = _reference_level(cw, lid, metric, latest, None)
    center_context, this_signal = _center_context(ledger, row, signals_doc, verdicts_doc)
    if this_signal is not None:
        ref_from_signal, label_from_signal = _reference_from_signal(this_signal, metric)
        if ref_from_signal is not None:
            reference, reference_label = ref_from_signal, label_from_signal

    prior_attempts = []
    cursor = row
    while cursor.get("supersedes"):
        previous = ledger.get(cursor["supersedes"])
        if previous is None:
            break
        prior_attempts.append(
            {
                "created_week": previous["created_week"],
                "recommendation": previous["recommendation"],
                "outcome": STATUS_DISPLAY.get(previous["status"], previous["status"]),
                "outcome_note": previous["outcome_note"],
            }
        )
        cursor = previous

    return {
        "as_of": str(as_of),
        "center": center,
        "metric": metric.key,
        "metric_display": metric.display_name,
        "owner": row["owner"],
        "owner_role": owner_role,
        "escalation_partner": partner,
        "escalation_partner_role": partner_role,
        "escalated": escalated,
        "attempt": attempt,
        "expected_direction": row["expected_direction"],
        "check_by": str(check_by),
        "supersedes": row["rec_id"],
        "successor_rec_id": f"REC-{as_of}-{center_slug(center)}-{metric.key}-attempt{attempt}",
        "loop_state": recheck["loop_state"],
        # True only when a human attested the work was done and the number still
        # did not move: the approach itself is falsified, so the next move must
        # be a different mechanism. When the work was never done, the same ask is
        # re-issued instead — inventing a new fix would be answering a question
        # nobody asked.
        "different_mechanism_required": (
            recheck["loop_state"] == successor.DIFFERENT_MECHANISM_STATE
        ),
        "what_was_tried": {
            "recommendation": row["recommendation"],
            "action_phrase": ACTION_HINTS.get(metric.key),
            "created_week": row["created_week"],
            "check_by": row["check_by"],
            "attempts_so_far": attempt - 1,
            "escalation_level": int(row["escalation_level"]),
            "prior_attempts": prior_attempts,
        },
        "what_happened": {
            "outcome": recheck["outcome"],
            "outcome_display": recheck["outcome_display"],
            "note": recheck["note"],
            "anchor_level_display": recheck["anchor_level_display"],
            "followup_level_display": recheck["followup_level_display"],
            "followup_weeks": recheck["followup_weeks"],
            "weekly_values_since": recheck.get("followup_weekly_values") or [],
            "noise_floor": recheck.get("noise_floor"),
        },
        "reference": {
            "level_display": fmt_short(metric, reference) if reference is not None else None,
            "label": reference_label,
        },
        "provider_concentration": (
            {"sentence": grounding_sentence, "detail": grounding_receipt}
            if grounding_sentence
            else None
        ),
        "center_context": center_context,
    }


def _successor_entry(context: dict, generated: dict, row: dict) -> dict:
    """The digest-ready successor: what was tried → what happened → next move."""
    return {
        "rec_id": context["successor_rec_id"],
        "supersedes": context["supersedes"],
        "attempt": context["attempt"],
        "escalated": context["escalated"],
        "center": context["center"],
        "metric": context["metric"],
        "metric_display": context["metric_display"],
        "owner": context["owner"],
        "owner_role": context["owner_role"],
        "escalation_partner": context["escalation_partner"] if context["escalated"] else None,
        "expected_direction": context["expected_direction"],
        "check_by": row["check_by"],
        "status": row["status"],
        "status_display": STATUS_DISPLAY.get(row["status"], row["status"]),
        "next_move": generated["next_move"],
        "expected": generated["expected"],
        "why": generated["why"],
        "recommendation": generated["recommendation"],
        "what_was_tried": context["what_was_tried"]["recommendation"],
        "what_happened": context["what_happened"]["note"],
        "movement": {
            "from": context["what_happened"]["anchor_level_display"],
            "to": context["what_happened"]["followup_level_display"],
            "weeks": context["what_happened"]["followup_weeks"],
            "weekly_values": context["what_happened"]["weekly_values_since"],
        },
        "mode": generated["mode"],
        "decided_by": generated["decided_by"],
        "checks": generated["checks"],
        "attempts": generated["attempts"],
        "fallbacks": generated["fallbacks"],
    }


def attach_successors(
    ledger: Ledger,
    rechecks: list[dict],
    as_of: date,
    loc: pd.DataFrame,
    pw: pd.DataFrame,
    cw: pd.DataFrame,
    signals_doc: dict | None = None,
    verdicts_doc: dict | None = None,
    mode: str | None = None,
    log: harness.RunLog | None = None,
) -> list[dict]:
    """For every re-check that failed or went ignored, write the next move.

    The successor becomes a tracked row of its own (status open, new check-by)
    and the recommendation it replaces records `superseded_by`, so the chain
    reads forwards and backwards and the same problem is never escalated twice.
    Re-running the same Monday is idempotent: a successor that already exists
    is replayed from its receipt rather than regenerated.
    """
    targets = [r for r in rechecks if r["loop_state"] in successor.SUCCESSOR_STATES]
    if not targets:
        return []

    latest = latest_complete_week(as_of)
    loc_by_id = loc.set_index("location_id")
    pending: list[tuple[dict, dict, dict]] = []  # (recheck, row, context)
    entries: list[dict] = []

    for recheck in targets:
        row = ledger.get(recheck["rec_id"])
        if row is None:
            continue
        context = build_successor_context(
            ledger, recheck, row, as_of, loc_by_id.loc[recheck["location_id"]],
            pw, cw, latest, signals_doc, verdicts_doc,
        )
        existing = ledger.get(context["successor_rec_id"])
        if existing is not None:
            # Replay (a re-run of this same Monday): recover what was written
            # the first time from the append-only log — never a second call.
            receipt = ledger.last_event("rec_successor_created", rec_id=existing["rec_id"])
            if receipt and receipt.get("successor"):
                entry = {**receipt["successor"]}
                entry["status"] = existing["status"]
                entry["status_display"] = STATUS_DISPLAY.get(
                    existing["status"], existing["status"]
                )
                entry["check_by"] = existing["check_by"]
                entry["replayed"] = True
                recheck["successor"] = entry
                entries.append(entry)
            continue
        pending.append((recheck, row, context))

    if not pending:
        return entries

    # The language is an LLM call per successor; they are independent, so a
    # Monday with several failed recommendations does not run them in series.
    def _generate(context: dict) -> dict:
        return successor.generate(context, mode=mode, log=log)

    if len(pending) == 1:
        generated_all = [_generate(pending[0][2])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(pending), 4)) as pool:
            generated_all = list(pool.map(lambda item: _generate(item[2]), pending))

    for (recheck, row, context), generated in zip(pending, generated_all):
        prior = (context.get("what_was_tried") or {}).get("recommendation") or ""
        successor_basis, successor_assumption = _successor_evidence(
            context, bool(context.get("provider_concentration"))
        )
        successor_row = {
            "rec_id": context["successor_rec_id"],
            "created_week": str(as_of),
            "location_id": row["location_id"],
            "location_name": context["center"],
            "metric": context["metric"],
            "recommendation": generated["recommendation"],
            "owner": context["owner"],
            "expected_direction": context["expected_direction"],
            "check_by": context["check_by"],
            "status": "open",
            "last_checked": "",
            "outcome_note": "",
            "escalation_level": int(row["escalation_level"]),
            "supersedes": row["rec_id"],
            "superseded_by": "",
            "decision": "",
            # A successor starts unattested like any other recommendation —
            # being the second attempt earns it no assumption that it happened.
            "execution": "unknown",
            "execution_by": "",
            "execution_at": "",
            "outcome": "pending",
            "reading": "pending",
            # A successor is a better-informed hypothesis, not a proven cause:
            # what the last round established is that the FIRST mechanism does
            # not work here, which is real evidence but still not a diagnosis.
            "action_basis": successor_basis,
            "action_assumption": successor_assumption,
        }
        ledger.add(successor_row)

        entry = _successor_entry(context, generated, successor_row)
        recheck["successor"] = entry
        entries.append(entry)

        # Lineage, both directions. The superseded row keeps its outcome (the
        # failure is history worth reading) and stops being re-checked.
        row["superseded_by"] = successor_row["rec_id"]
        # Lifecycle: this row's successor owns the metric now. Its `outcome`
        # and `execution` stay exactly as measured/attested — the history is
        # the point of the chain.
        row["status"] = "superseded"
        ledger.event(
            "rec_superseded",
            {
                "as_of": str(as_of),
                "rec_id": row["rec_id"],
                "superseded_by": successor_row["rec_id"],
                "outcome": recheck["outcome"],
                "center": context["center"],
                "metric": context["metric"],
            },
        )
        ledger.event(
            "rec_successor_created",
            {
                "as_of": str(as_of),
                "rec_id": successor_row["rec_id"],
                "supersedes": row["rec_id"],
                "center": context["center"],
                "metric": context["metric"],
                "owner": context["owner"],
                "escalated": context["escalated"],
                "attempt": context["attempt"],
                "check_by": context["check_by"],
                "decided_by": generated["decided_by"],
                "numbers_verified": generated["checks"]["numbers_verified"],
                "generation_attempts": generated["attempts"],
                "fallbacks": generated["fallbacks"],
                "successor": entry,
            },
        )

    return entries


# ---------------------------------------------------------------------------
# One call per digest run
# ---------------------------------------------------------------------------


def _open_row_json(row: dict) -> dict:
    metric = config.METRICS[row["metric"]]
    execution = row.get("execution") or "unknown"
    outcome = row.get("outcome") or "pending"
    reading = row.get("reading") or "pending"
    loop_state = derive_loop_state(execution, outcome, reading)
    return {
        "rec_id": row["rec_id"],
        "center": row["location_name"],
        "metric": row["metric"],
        "metric_display": metric.display_name,
        "recommendation": row["recommendation"],
        "owner": row["owner"],
        "expected_direction": row["expected_direction"],
        "created_week": row["created_week"],
        "check_by": row["check_by"],
        "status": row["status"],
        "status_display": STATUS_DISPLAY.get(row["status"], row["status"]),
        # The two independent dimensions, plus the state they derive.
        "execution": execution,
        "execution_display": EXECUTION_DISPLAY.get(execution, ""),
        "execution_by": row.get("execution_by") or None,
        "execution_at": row.get("execution_at") or None,
        "outcome": outcome,
        "outcome_display": OUTCOME_DISPLAY.get(outcome, ""),
        "reading": reading,
        "reading_display": READING_DISPLAY.get(reading, ""),
        "loop_state": loop_state,
        "loop_state_display": LOOP_STATE_DISPLAY.get(loop_state, ""),
        # Whether the data supports the INTERVENTION or only the signal.
        "action_basis": row.get("action_basis") or None,
        "action_assumption": row.get("action_assumption") or None,
        "last_checked": row["last_checked"] or None,
        "outcome_note": row["outcome_note"] or None,
        "escalation_level": int(row["escalation_level"]),
        # Lineage + the last human decision, so the UI renders resolved state
        # from the data rather than from whatever it remembers locally.
        "supersedes": row.get("supersedes") or None,
        "superseded_by": row.get("superseded_by") or None,
        "decision": row.get("decision") or None,
    }


def update(
    as_of: date,
    signals_doc: dict,
    fresh: bool = False,
    mode: str | None = None,
    write: bool = True,
) -> dict:
    """The per-digest-run ledger pass: re-check every open row against the new
    week's actuals, write a successor recommendation for every re-check that
    failed or went ignored, then append this run's new recommendations. Returns
    the digest-ready ledger section."""
    if fresh:
        archived = reset_ledger()
        if archived:
            Ledger().event("ledger_reset", {"as_of": str(as_of), "archived_to": archived})

    loc, cw = load_data()
    pw = pd.read_csv(config.DATA_FILES["provider_weekly"], parse_dates=["week_start"])

    ledger = Ledger()
    ledger.event(
        "run_start",
        {
            "as_of": str(as_of),
            "rows_total": len(ledger.rows),
            "rows_active": len(ledger.active_rows()),
        },
    )

    # This run's Claimed vs. Verified output, when it exists: a successor for a
    # center should know what its improvement plans are already claiming.
    out_dir = config.OUTPUTS_DIR / str(as_of)
    verdicts_path = out_dir / "verdicts.json"
    verdicts_doc = json.loads(verdicts_path.read_text()) if verdicts_path.exists() else None

    rechecks = recheck_open_rows(ledger, as_of, cw)
    # Successor language is the one generated text in this step; its checks go
    # into the run's own harness log, alongside the verdict checks.
    log = harness.RunLog(
        out_dir / "runlog.jsonl" if write else None, step="successor", mode="a"
    )
    try:
        successors = attach_successors(
            ledger, rechecks, as_of, loc, pw, cw, signals_doc, verdicts_doc, mode, log
        )
    finally:
        log.close()
    created = append_new_recommendations(ledger, signals_doc, loc, pw, cw, as_of)
    ledger.save()

    open_rows = sorted(
        (_open_row_json(r) for r in ledger.active_rows()),
        key=lambda r: (-r["escalation_level"], r["check_by"], r["rec_id"]),
    )
    counts = {
        "rows_total": len(ledger.rows),
        "open": len(open_rows),
        "rechecked": len(rechecks),
        # What the numbers did. The leaf word "working" is load-bearing:
        # narrate.py resolves receipt provenance by leaf word.
        "outcome_working": sum(1 for r in rechecks if r["outcome"] == "working"),
        "outcome_not_working": sum(1 for r in rechecks if r["outcome"] == "not_working"),
        "outcome_flat": sum(1 for r in rechecks if r["outcome"] == "flat"),
        # Whether a human confirmed the work happened. Counted separately,
        # because it is a different question with a different source of truth.
        "execution_done": sum(1 for r in rechecks if r["execution"] == "done"),
        "execution_not_done": sum(1 for r in rechecks if r["execution"] == "not_done"),
        "execution_unknown": sum(1 for r in rechecks if r["execution"] == "unknown"),
        "created": len(created),
        # Only re-checks whose loop state owes a next move produce one, so this
        # is deliberately NOT equal to the count of failed outcomes.
        "successors": len(successors),
    }
    ledger.event("run_summary", {"as_of": str(as_of), "counts": counts})

    return {
        "state_file": str(LEDGER_CSV.relative_to(config.REPO_ROOT)),
        "log_file": str(LEDGER_LOG.relative_to(config.REPO_ROOT)),
        "open": open_rows,
        "changed": rechecks,
        "created": created,
        # Also listed flat, so a client can render "what was tried → what
        # happened → next move" without walking the re-check list.
        "successors": successors,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# CLI — inspection + the human-decision path
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.ledger",
        description="Step 3c — recommendation ledger (state + receipts).",
    )
    parser.add_argument("--show", action="store_true", help="Print the state table.")
    parser.add_argument(
        "--reset-ledger",
        action="store_true",
        help="Archive ledger.csv + ledger_log.jsonl into DATA/OUTPUTS/ledger_archive/ "
             "and start empty. Receipts are moved, never deleted. This is the door "
             "the app's full reset uses — nothing outside this module touches those files.",
    )
    parser.add_argument("--decide", metavar="REC_ID", help="Record a human decision on a row.")
    parser.add_argument(
        "--action",
        choices=tuple(DECISION_ACTIONS),
        help=(
            "What the human did: close | relaunch (needs --check-by) | "
            "escalate | dismiss (needs --note). This is the path the app uses."
        ),
    )
    parser.add_argument(
        "--set", dest="decision", choices=("closed", "dismissed", "open", "escalated"),
        help="The resulting state, when recording it directly instead of via --action.",
    )
    parser.add_argument(
        "--check-by",
        dest="check_by",
        default=None,
        help="New check-by date (YYYY-MM-DD) — required when relaunching.",
    )
    parser.add_argument(
        "--note",
        default="",
        help="Reason for the decision (required when dismissing).",
    )
    parser.add_argument("--by", default="human", help="Who decided (e.g. \"Dr. Priya Raghunathan\").")
    parser.add_argument(
        "--execution",
        choices=("done", "not_done", "unknown"),
        help="With --action attest: whether the work was actually carried out. "
             "This is the ONLY way execution is ever set — it is never inferred "
             "from a metric.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the resulting row as JSON (how the API layer calls this).",
    )
    args = parser.parse_args(argv)

    if args.reset_ledger:
        archived = reset_ledger()
        if args.json:
            print(json.dumps({"ok": True, "archived_to": archived}))
        else:
            print(f"Ledger archived to {archived}" if archived else "No ledger to archive.")
        return

    ledger = Ledger()
    if args.decide:
        decision = args.decision or DECISION_ACTIONS.get(args.action)
        if args.action == "attest":
            if not args.execution:
                raise SystemExit("--action attest needs --execution done|not_done")
        elif not decision:
            raise SystemExit(
                "--decide needs --action attest|close|relaunch|escalate|dismiss "
                "(or --set closed|dismissed|open|escalated)"
            )
        try:
            row = ledger.record_human_decision(
                args.decide, decision, args.note, args.by,
                action=args.action, check_by=args.check_by,
                execution=args.execution,
            )
        except (KeyError, ValueError) as exc:
            # KeyError's str() quotes its message; the caller (and the API that
            # passes this through to a leader) wants the sentence itself.
            raise SystemExit(exc.args[0] if exc.args else str(exc)) from exc
        if args.json:
            print(json.dumps({"ok": True, "row": {k: row.get(k, "") for k in FIELDNAMES}}))
            return
        if args.action == "attest":
            print(f"{row['rec_id']} -> execution={row['execution']} ({row['outcome_note']})")
        else:
            print(f"{row['rec_id']} -> {row['status']} ({row['outcome_note']})")
        return

    if not ledger.rows:
        print(f"Ledger is empty ({LEDGER_CSV}).")
        return
    print(f"Recommendation ledger — {len(ledger.rows)} rows ({LEDGER_CSV})")
    for r in ledger.rows:
        print(
            f"  {r['rec_id']}\n"
            f"    {r['location_name']} · {config.METRICS[r['metric']].display_name} · "
            f"owner {r['owner']} · check by {r['check_by']}\n"
            f"    status {r['status']} (esc {r['escalation_level']}) · "
            f"last checked {r['last_checked'] or 'never'}"
            + (f"\n    note: {r['outcome_note']}" if r["outcome_note"] else "")
        )


if __name__ == "__main__":
    main()
