"""Step 1 — Data Validation & Check.

Fully deterministic — no LLM anywhere in this step. Runs first, one table at a
time, in order: locations → clinic_weekly → provider_weekly → action_plans.

Two kinds of checks per table:
  (a) documented issues from DATA/INPUTS/README_SCHEMA_DATA_ISSUES.md — handled and
      counted (duplicates, negative waits, missing values, employment_type
      capitalization, the 2026-02-09 membership definition change, partial
      history, small denominators);
  (b) undocumented self-consistency checks — maturity_tier vs opened_date,
      appts_per_doctor_hour vs its own formula, clinic rollups vs the
      hours-weighted provider average, calendar sanity, and action-plan
      baseline_value vs what the data actually said the week the plan opened.

Correction flow (locked): the AI never silently edits data. DATA/INPUTS/ stays raw
and untouched forever. This module *proposes* corrections with stable IDs
(C1..Cn); a human accepts or declines. Accepted corrections are applied to CSV
copies in DATA/TRANSLATION/ (normalized filenames) plus DATA/TRANSLATION/MANIFEST.json recording
provenance. Declined corrections are logged, not applied.

CLI:
    python -m pipeline.validate                  # run checks, propose corrections
    python -m pipeline.validate --accept all     # apply every proposed correction to DATA/TRANSLATION/
    python -m pipeline.validate --accept C1,C3   # apply only these; the rest are declined
    python -m pipeline.validate --as-of 2026-05-04

Outputs (all under DATA/OUTPUTS/validation/):
    report.md                    plain-English findings, per table
    report.json                  the same findings, machine-readable
    corrections_proposed.json    the accept/decline menu
    runlog.jsonl                 one line per check: name, result, counts, timestamp
On --accept, additionally: DATA/TRANSLATION/*.csv + DATA/TRANSLATION/MANIFEST.json.

Leader-facing name everywhere: "Data Validation & Check" — never "audit".
Location IDs live in JSON/logs only; report.md uses real center names.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

# ---------------------------------------------------------------------------
# Constants for this step
# ---------------------------------------------------------------------------

VALIDATION_DIR = config.OUTPUTS_DIR / "validation"

TABLES = ["locations", "clinic_weekly", "provider_weekly", "action_plans"]

DAYS_PER_MONTH = 30.4375  # 365.25 / 12
MATURITY_BOUNDARY_TOL_MONTHS = 1.0  # within ±1 month of a tier boundary = consistent

THROUGHPUT_TOL = 0.01  # appts_per_doctor_hour vs appts_completed / doctor_hours_scheduled
ROLLUP_TOL_PTS = 0.5  # clinic pct vs hours-weighted provider average

# Small-denominator watermarks — *recorded* here so the run report shows the
# extent of the problem. Enforcement (suppression) happens in the signal
# engine with thresholds defended in docs/SCORING.md.
SMALL_CSAT_RESPONSES = 25
SMALL_WEEKLY_APPTS = 50

CANONICAL_EMPLOYMENT_TYPES = ("full_time", "part_time", "relief")

PANEL_WEEKS = 52  # the weekly panel covers 52 weeks ending the week before as-of


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One check's outcome: what we looked at, what we found, in plain English."""

    table: str
    check: str  # machine name, e.g. "clinic_weekly.duplicate_rows"
    result: str  # "pass" | "flag"
    summary: str  # one plain-English sentence
    counts: dict = field(default_factory=dict)
    details: list = field(default_factory=list)  # row-level specifics (JSON-safe)


@dataclass
class Correction:
    """A proposed data correction — a real accept/decline decision for a human."""

    id: str  # stable: C1..Cn (order fixed by construction, not by data)
    table: str
    kind: str  # drop_duplicates | negative_wait_to_missing | normalize_employment_type | relabel_maturity_tier
    description: str
    affected_rows: int
    details: list = field(default_factory=list)  # per-location detail for relabels

    # --- decision memory (filled by classify_corrections) -------------------
    scope: str = "standing"  # standing rule | instance decision
    fingerprint: str = ""  # identity across runs; what the decision log is keyed on
    status: str = "new"  # "new" (needs a human) | "carried" (already decided)
    prior_decision: str | None = None  # accepted | declined, when carried
    decided_as_of: str | None = None  # the Monday the human decided it
    new_rows: int = 0  # rows this run that the prior decision had not seen


# ---------------------------------------------------------------------------
# Correction decisions that survive the run
# ---------------------------------------------------------------------------
#
# A correction the leader already ruled on must never come back as a question.
# Two kinds of ruling, because they carry forward differently:
#
#   standing — the decision is about a *rule* ("drop exact duplicates"), so it
#              governs every row the rule ever touches, including rows that
#              arrive in later weeks.
#   instance — the decision is about a *named set of rows* ("relabel these 7
#              centers"), so it carries only while that set is unchanged; a
#              different set is a new question.
#
# The log is append-only; the last record per fingerprint wins, which is how a
# standing rule's coverage grows week over week without rewriting history.

DECISIONS_PATH = config.OUTPUTS_DIR / "correction_decisions.jsonl"
DECISIONS_ARCHIVE = config.OUTPUTS_DIR / "decisions_archive"

CORRECTION_SCOPE = {
    "drop_duplicates": "standing",
    "negative_wait_to_missing": "standing",
    "normalize_employment_type": "standing",
    "relabel_maturity_tier": "instance",
}


def correction_fingerprint(kind: str, details: list) -> str:
    """Stable identity for a correction across runs.

    Standing rules are identified by the rule alone. Instance decisions are
    identified by the exact rows they cover, so adding or dropping a center
    makes it a new decision rather than a silent extension of an old one.
    """
    if CORRECTION_SCOPE.get(kind, "standing") == "standing":
        payload = f"rule:{kind}"
    else:
        keys = sorted(
            f"{d.get('location_id')}={d.get('computed_tier')}" for d in details
        )
        payload = f"rows:{kind}|" + "|".join(keys)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_decisions(path: Path | None = None) -> dict[str, dict]:
    """Latest decision per fingerprint from the append-only log."""
    p = path or DECISIONS_PATH
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:  # a torn line never invalidates the rest
            continue
        fp = rec.get("fingerprint")
        if fp:
            out[fp] = rec
    return out


def classify_corrections(
    corrections: list[Correction], decisions: dict[str, dict]
) -> list[Correction]:
    """Mark each correction new (needs a human) or carried (already decided)."""
    for c in corrections:
        prior = decisions.get(c.fingerprint)
        if prior is None:
            c.status = "new"
            c.new_rows = c.affected_rows
            continue
        c.status = "carried"
        c.prior_decision = prior.get("decision")
        c.decided_as_of = prior.get("as_of")
        if c.scope == "standing":
            covered = int(prior.get("rows_covered") or 0)
            c.new_rows = max(0, c.affected_rows - covered)
        else:
            c.new_rows = 0
    return corrections


def effective_accepted(corrections: list[Correction], accepted_ids: list[str]) -> list[str]:
    """Ids to actually apply: this run's accepts plus every standing accept."""
    out = []
    for c in corrections:
        if c.status == "carried":
            if c.prior_decision == "accepted":
                out.append(c.id)
        elif c.id in accepted_ids:
            out.append(c.id)
    return out


def record_decisions(
    corrections: list[Correction],
    accepted_ids: list[str],
    as_of: date,
    actor: str = "operator",
    path: Path | None = None,
) -> list[dict]:
    """Append this run's rulings to the decision log.

    New corrections record the human's ruling. Carried corrections record only
    their widened coverage, so next week can tell which rows are genuinely new.
    """
    p = path or DECISIONS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    written: list[dict] = []
    with p.open("a") as fh:
        for c in corrections:
            if c.status == "new":
                decision = "accepted" if c.id in accepted_ids else "declined"
                event = "decided"
                decided_as_of = str(as_of)
            elif c.scope == "standing" and c.new_rows:
                decision = c.prior_decision or "declined"
                event = "carried"
                decided_as_of = c.decided_as_of or str(as_of)
            else:
                continue  # nothing changed; the log stays quiet
            rec = {
                "ts": ts,
                "event": event,
                "as_of": decided_as_of,
                "recorded_on": str(as_of),
                "correction_id": c.id,
                "kind": c.kind,
                "table": c.table,
                "scope": c.scope,
                "fingerprint": c.fingerprint,
                "decision": decision,
                "actor": actor if event == "decided" else "carried-forward",
                "rows_covered": c.affected_rows,
            }
            fh.write(json.dumps(rec) + "\n")
            written.append(rec)
    return written


def archive_decisions(path: Path | None = None) -> Path | None:
    """Rotate the decision log out of the way for a clean canonical run."""
    p = path or DECISIONS_PATH
    if not p.exists():
        return None
    DECISIONS_ARCHIVE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = DECISIONS_ARCHIVE / f"correction_decisions_{stamp}.jsonl"
    p.rename(dest)
    return dest


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def panel_cutoff(as_of: date) -> pd.Timestamp:
    """Last week of the panel this Monday's digest is allowed to see."""
    return pd.Timestamp(config.latest_complete_week(as_of))


def as_of_mask(table: str, df: pd.DataFrame, as_of: date) -> pd.Series:
    """Rows of `table` that existed as of the digest Monday.

    Weekly panels are cut at the last complete week (the same boundary the
    signal engine scores on); centers and plans are cut at their opened_date.
    A row dated after this Monday has not happened yet — this Monday's run must
    not see it, and next Monday's run must see it as new.
    """
    if "week_start" in df.columns:
        return pd.to_datetime(df["week_start"], errors="coerce") <= panel_cutoff(as_of)
    if "opened_date" in df.columns:
        return pd.to_datetime(df["opened_date"], errors="coerce") <= pd.Timestamp(as_of)
    return pd.Series(True, index=df.index)


def load_parsed(as_of: date) -> dict[str, pd.DataFrame]:
    """Load the four raw CSVs with dates parsed and numerics typed (for checks),
    scoped to what existed as of the digest Monday."""
    raw = {
        "locations": pd.read_csv(config.RAW_FILES["locations"], parse_dates=["opened_date"]),
        "clinic_weekly": pd.read_csv(config.RAW_FILES["clinic_weekly"], parse_dates=["week_start"]),
        "provider_weekly": pd.read_csv(config.RAW_FILES["provider_weekly"], parse_dates=["week_start"]),
        "action_plans": pd.read_csv(
            config.RAW_FILES["action_plans"],
            parse_dates=["opened_date", "due_date", "last_status_update"],
        ),
    }
    return {
        t: df[as_of_mask(t, df, as_of)].reset_index(drop=True) for t, df in raw.items()
    }


def load_text(table: str, as_of: date) -> pd.DataFrame:
    """Load a raw CSV as pure strings (for surgical corrections), scoped to as-of.

    dtype=str + keep_default_na=False preserves every cell verbatim, so the
    corrected copies in DATA/TRANSLATION/ differ from DATA/INPUTS/ only where a correction
    actually touched a cell (or removed a duplicate row).
    """
    df = pd.read_csv(config.RAW_FILES[table], dtype=str, keep_default_na=False)
    return df[as_of_mask(table, df, as_of)].reset_index(drop=True)


def raw_row_counts() -> dict[str, int]:
    """Rows in each raw file on disk, before any as-of scoping (provenance only)."""
    return {
        t: sum(1 for _ in config.RAW_FILES[t].open()) - 1 for t in TABLES
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _week_monday(d: pd.Timestamp) -> pd.Timestamp:
    """Monday of the week containing date d."""
    return d - pd.Timedelta(days=int(d.weekday()))


# ---------------------------------------------------------------------------
# Maturity-tier logic
# ---------------------------------------------------------------------------


def months_open(opened: pd.Timestamp, as_of: date) -> float:
    return (pd.Timestamp(as_of) - opened).days / DAYS_PER_MONTH


def expected_tier(months: float) -> str:
    if months < 12:
        return "new"
    if months < 24:
        return "ramping"
    return "mature"


def tier_consistent(months: float, label: str) -> bool:
    """A label is consistent if it matches the age, or the age sits within
    ±1 month of the boundary between the labeled tier and the computed tier."""
    exp = expected_tier(months)
    if label == exp:
        return True
    if {label, exp} == {"new", "ramping"} and abs(months - 12) <= MATURITY_BOUNDARY_TOL_MONTHS:
        return True
    if {label, exp} == {"ramping", "mature"} and abs(months - 24) <= MATURITY_BOUNDARY_TOL_MONTHS:
        return True
    return False


# ---------------------------------------------------------------------------
# Per-table checks. Each returns a list[Finding]; corrections are built after.
# ---------------------------------------------------------------------------


def check_locations(loc: pd.DataFrame, cw_dedup: pd.DataFrame, as_of: date) -> list[Finding]:
    findings: list[Finding] = []

    # -- self-consistency: maturity_tier vs opened_date --------------------
    detail = []
    for _, r in loc.iterrows():
        m = months_open(r.opened_date, as_of)
        if not tier_consistent(m, r.maturity_tier):
            detail.append(
                {
                    "location_id": r.location_id,
                    "location_name": r.location_name,
                    "opened_date": str(r.opened_date.date()),
                    "months_open": round(m, 1),
                    "labeled_tier": r.maturity_tier,
                    "computed_tier": expected_tier(m),
                }
            )
    findings.append(
        Finding(
            table="locations",
            check="locations.maturity_tier_vs_opened_date",
            result="flag" if detail else "pass",
            summary=(
                f"{len(detail)} of {len(loc)} centers carry a maturity label that "
                f"contradicts their opened date (as of {as_of}; new <12mo, ramping "
                f"12–24, mature >24; ±1 month boundary tolerance)."
            ),
            counts={"locations_checked": len(loc), "mismatched_labels": len(detail)},
            details=detail,
        )
    )

    # -- documented: partial-history centers -------------------------------
    panel_start = pd.Timestamp(as_of) - pd.Timedelta(weeks=PANEL_WEEKS)
    first_weeks = cw_dedup.groupby("location_id")["week_start"].agg(["min", "count"])
    partial = first_weeks[first_weeks["min"] > panel_start]
    names = loc.set_index("location_id")["location_name"]
    detail = [
        {
            "location_id": lid,
            "location_name": names.get(lid, lid),
            "first_week": str(row["min"].date()),
            "weeks_of_history": int(row["count"]),
        }
        for lid, row in partial.iterrows()
    ]
    findings.append(
        Finding(
            table="locations",
            check="locations.partial_history_centers",
            result="flag" if detail else "pass",
            summary=(
                f"{len(detail)} of {len(loc)} centers opened inside the {PANEL_WEEKS}-week "
                f"window and have partial history — trend windows must respect each "
                f"center's own start, and the signal engine treats them as partial-history."
            ),
            counts={"partial_history_centers": len(detail)},
            details=sorted(detail, key=lambda d: d["location_id"]),
        )
    )

    return findings


def check_clinic_weekly(
    cw_raw: pd.DataFrame, cw_text: pd.DataFrame, loc: pd.DataFrame, as_of: date
) -> tuple[list[Finding], pd.DataFrame]:
    """Checks on the clinic weekly panel. Returns (findings, deduplicated frame)."""
    findings: list[Finding] = []
    names = loc.set_index("location_id")["location_name"]

    # -- documented: exact duplicate rows ----------------------------------
    dup_mask = cw_text.duplicated()  # exact string duplicates (keep first)
    dup_detail = [
        {
            "location_id": r.location_id,
            "location_name": names.get(r.location_id, r.location_id),
            "week_start": r.week_start,
        }
        for r in cw_text[dup_mask].itertuples()
    ]
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.duplicate_rows",
            result="flag" if dup_detail else "pass",
            summary=(
                f"{len(dup_detail)} exact duplicate rows (double-run of the weekly load) "
                f"in {len(cw_raw)} raw rows — proposed for removal."
            ),
            counts={"raw_rows": len(cw_raw), "duplicate_rows": len(dup_detail)},
            details=dup_detail,
        )
    )

    cw = cw_raw.drop_duplicates().reset_index(drop=True)

    # After dropping exact duplicates, (location, week) must be unique.
    key_dups = int(cw.duplicated(subset=["location_id", "week_start"]).sum())
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.unique_center_week",
            result="flag" if key_dups else "pass",
            summary=(
                f"After removing exact duplicates, every (center, week) pair is unique "
                f"({key_dups} conflicting rows)."
            ),
            counts={"conflicting_center_weeks": key_dups},
        )
    )

    # -- documented: negative wait times → missing -------------------------
    neg_mask = cw["avg_wait_time_min"] < 0
    neg_detail = [
        {
            "location_id": r.location_id,
            "location_name": names.get(r.location_id, r.location_id),
            "week_start": str(r.week_start.date()),
            "recorded_value": float(r.avg_wait_time_min),
        }
        for r in cw[neg_mask].itertuples()
    ]
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.negative_wait_times",
            result="flag" if neg_detail else "pass",
            summary=(
                f"{len(neg_detail)} rows show a negative average wait time (known "
                f"clock-sync bug) — proposed to be treated as missing."
            ),
            counts={"negative_wait_rows": len(neg_detail)},
            details=neg_detail,
        )
    )

    # -- documented: missing values ----------------------------------------
    n = len(cw)
    missing = {
        "client_csat": int(cw["client_csat"].isna().sum()),
        "revenue_per_appt": int(cw["revenue_per_appt"].isna().sum()),
        "avg_wait_time_min_already_missing": int(cw["avg_wait_time_min"].isna().sum()),
    }
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.missing_values",
            result="flag",
            summary=(
                f"Missing values counted (of {n} deduplicated rows): client_csat "
                f"{missing['client_csat']} ({missing['client_csat'] / n:.1%}), "
                f"revenue_per_appt {missing['revenue_per_appt']} "
                f"({missing['revenue_per_appt'] / n:.1%}); avg_wait_time_min already "
                f"missing in {missing['avg_wait_time_min_already_missing']} rows before "
                f"the negative-value correction. Left missing — never imputed."
            ),
            counts={"rows": n, **missing},
        )
    )

    # -- documented: membership definition change (usage flag, not a row edit)
    change = pd.Timestamp(config.MEMBERSHIP_DEFINITION_CHANGE)
    pre = int((cw["week_start"] < change).sum())
    post = int((cw["week_start"] >= change).sum())
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.membership_definition_change",
            result="flag",
            summary=(
                f"membership_conversion_pct changed definition on "
                f"{config.MEMBERSHIP_DEFINITION_CHANGE} (denominator widened to include "
                f"urgent care). Recorded as a usage flag: the {pre} pre-change "
                f"center-weeks are excluded from membership trend windows; {post} "
                f"post-change center-weeks remain usable. No cell is edited."
            ),
            counts={"pre_change_weeks": pre, "post_change_weeks": post},
        )
    )

    # -- documented: small denominators (recorded; enforced at scoring time)
    small_csat = int((cw["csat_responses"] < SMALL_CSAT_RESPONSES).sum())
    small_appts = int((cw["appts_completed"] < SMALL_WEEKLY_APPTS).sum())
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.small_denominators",
            result="flag",
            summary=(
                f"Small denominators recorded: {small_csat} center-weeks have fewer than "
                f"{SMALL_CSAT_RESPONSES} CSAT responses and {small_appts} have fewer than "
                f"{SMALL_WEEKLY_APPTS} completed appointments. Weekly rates on these rows "
                f"are noisy; suppression is enforced by the signal engine per docs/SCORING.md."
            ),
            counts={
                "weeks_under_csat_response_threshold": small_csat,
                "weeks_under_appt_threshold": small_appts,
                "csat_response_threshold": SMALL_CSAT_RESPONSES,
                "appt_threshold": SMALL_WEEKLY_APPTS,
            },
        )
    )

    # -- self-consistency: appts_per_doctor_hour vs its formula ------------
    computable = cw["doctor_hours_scheduled"] > 0
    calc = cw.loc[computable, "appts_completed"] / cw.loc[computable, "doctor_hours_scheduled"]
    diff = (calc - cw.loc[computable, "appts_per_doctor_hour"]).abs()
    bad = cw.loc[computable][diff > THROUGHPUT_TOL]
    tp_detail = [
        {
            "location_id": r.location_id,
            "location_name": names.get(r.location_id, r.location_id),
            "week_start": str(r.week_start.date()),
            "recorded": float(r.appts_per_doctor_hour),
            "computed": round(float(r.appts_completed / r.doctor_hours_scheduled), 3),
        }
        for r in bad.itertuples()
    ]
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.throughput_formula",
            result="flag" if tp_detail else "pass",
            summary=(
                f"Recomputed appts_per_doctor_hour = appts_completed / "
                f"doctor_hours_scheduled for all {int(computable.sum())} rows: "
                f"{len(tp_detail)} disagree beyond {THROUGHPUT_TOL}."
            ),
            counts={"rows_checked": int(computable.sum()), "mismatches": len(tp_detail)},
            details=tp_detail,
        )
    )

    # -- self-consistency: no rows before a center's opening week ----------
    merged = cw.merge(loc[["location_id", "opened_date", "location_name"]], on="location_id")
    before_open = merged[merged["week_start"] < merged["opened_date"]]
    # a row whose week *contains* the opened date is a legitimate partial first week
    partial_first = before_open[before_open["opened_date"] - before_open["week_start"] <= pd.Timedelta(days=6)]
    violations = before_open[before_open["opened_date"] - before_open["week_start"] > pd.Timedelta(days=6)]
    v_detail = [
        {
            "location_id": r.location_id,
            "location_name": r.location_name,
            "week_start": str(r.week_start.date()),
            "opened_date": str(r.opened_date.date()),
        }
        for r in violations.itertuples()
    ]
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.rows_before_opened_date",
            result="flag" if v_detail else "pass",
            summary=(
                f"{len(violations)} rows fall before their center's opening week. "
                f"({len(partial_first)} rows start in the week the center opened "
                f"mid-week — legitimate partial first weeks, noted, not flagged.)"
            ),
            counts={
                "rows_before_opening_week": len(violations),
                "partial_first_weeks": len(partial_first),
            },
            details=v_detail,
        )
    )

    # -- self-consistency: week_start is always a Monday -------------------
    non_monday = int((cw["week_start"].dt.weekday != 0).sum())
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.week_start_mondays",
            result="flag" if non_monday else "pass",
            summary=f"{non_monday} of {len(cw)} week_start values are not Mondays.",
            counts={"non_monday_rows": non_monday},
        )
    )

    # -- referential integrity: every row joins to a known center ----------
    unknown = int((~cw["location_id"].isin(loc["location_id"])).sum())
    findings.append(
        Finding(
            table="clinic_weekly",
            check="clinic_weekly.known_location_ids",
            result="flag" if unknown else "pass",
            summary=f"{unknown} rows reference a location_id missing from locations.",
            counts={"unknown_location_rows": unknown},
        )
    )

    return findings, cw


def check_provider_weekly(
    pw: pd.DataFrame, cw_dedup: pd.DataFrame, loc: pd.DataFrame
) -> list[Finding]:
    findings: list[Finding] = []
    names = loc.set_index("location_id")["location_name"]

    # -- documented: employment_type capitalization ------------------------
    lowered = pw["employment_type"].str.lower()
    changed = pw["employment_type"] != lowered
    variant_counts = pw.loc[changed, "employment_type"].value_counts().to_dict()
    unrecognized = int((~lowered.isin(CANONICAL_EMPLOYMENT_TYPES)).sum())
    findings.append(
        Finding(
            table="provider_weekly",
            check="provider_weekly.employment_type_capitalization",
            result="flag" if changed.any() else "pass",
            summary=(
                f"{int(changed.sum())} of {len(pw)} rows carry a non-canonical "
                f"capitalization of employment_type ({', '.join(f'{k}: {v}' for k, v in sorted(variant_counts.items()))}) "
                f"— proposed lowercase normalization. {unrecognized} rows remain "
                f"unrecognized after lowercasing."
            ),
            counts={
                "rows": len(pw),
                "rows_to_normalize": int(changed.sum()),
                "unrecognized_after_lowercase": unrecognized,
            },
            details=[{"variant": k, "rows": int(v)} for k, v in sorted(variant_counts.items())],
        )
    )

    # -- documented: missing avg_appt_duration_min -------------------------
    miss = int(pw["avg_appt_duration_min"].isna().sum())
    findings.append(
        Finding(
            table="provider_weekly",
            check="provider_weekly.missing_values",
            result="flag",
            summary=(
                f"avg_appt_duration_min is missing in {miss} of {len(pw)} rows "
                f"({miss / len(pw):.1%}). Left missing — never imputed."
            ),
            counts={"rows": len(pw), "missing_avg_appt_duration": miss},
        )
    )

    # -- self-consistency: week_start is always a Monday -------------------
    non_monday = int((pw["week_start"].dt.weekday != 0).sum())
    findings.append(
        Finding(
            table="provider_weekly",
            check="provider_weekly.week_start_mondays",
            result="flag" if non_monday else "pass",
            summary=f"{non_monday} of {len(pw)} week_start values are not Mondays.",
            counts={"non_monday_rows": non_monday},
        )
    )

    # -- self-consistency: clinic rollups vs hours-weighted provider average
    for metric in ("recheck_compliance_pct", "record_completion_24h_pct"):
        sub = pw[["location_id", "week_start", "scheduled_hours", metric]].dropna()
        sub = sub[sub["scheduled_hours"] > 0].copy()
        sub["_wv"] = sub[metric] * sub["scheduled_hours"]
        g = sub.groupby(["location_id", "week_start"], as_index=False).agg(
            _wv=("_wv", "sum"), _w=("scheduled_hours", "sum")
        )
        g["provider_weighted"] = g["_wv"] / g["_w"]
        j = cw_dedup.merge(
            g[["location_id", "week_start", "provider_weighted"]],
            on=["location_id", "week_start"],
            how="inner",
        )
        j["absdiff"] = (j[metric] - j["provider_weighted"]).abs()
        mismatches = j[j["absdiff"] > ROLLUP_TOL_PTS]
        worst = j.nlargest(3, "absdiff")
        worst_detail = [
            {
                "location_id": r.location_id,
                "location_name": names.get(r.location_id, r.location_id),
                "week_start": str(r.week_start.date()),
                "clinic_value": float(getattr(r, metric)),
                "provider_weighted": round(float(r.provider_weighted), 2),
                "abs_diff": round(float(r.absdiff), 3),
            }
            for r in worst.itertuples()
        ]
        findings.append(
            Finding(
                table="provider_weekly",
                check=f"provider_weekly.rollup_consistency.{metric}",
                result="flag" if len(mismatches) else "pass",
                summary=(
                    f"Clinic {metric} re-derived as the hours-weighted average of its "
                    f"doctors across {len(j)} overlapping center-weeks: "
                    f"{len(mismatches)} disagree beyond {ROLLUP_TOL_PTS} pts "
                    f"(worst observed gap {j['absdiff'].max():.2f} pts)."
                ),
                counts={
                    "overlapping_center_weeks": len(j),
                    "mismatches": len(mismatches),
                    "worst_abs_diff": round(float(j["absdiff"].max()), 3),
                },
                details=worst_detail,
            )
        )

    # -- referential integrity ---------------------------------------------
    unknown = int((~pw["location_id"].isin(loc["location_id"])).sum())
    findings.append(
        Finding(
            table="provider_weekly",
            check="provider_weekly.known_location_ids",
            result="flag" if unknown else "pass",
            summary=f"{unknown} rows reference a location_id missing from locations.",
            counts={"unknown_location_rows": unknown},
        )
    )

    return findings


def check_action_plans(
    ap: pd.DataFrame, cw_dedup: pd.DataFrame, loc: pd.DataFrame
) -> list[Finding]:
    findings: list[Finding] = []
    names = loc.set_index("location_id")["location_name"]
    opened_dates = loc.set_index("location_id")["opened_date"]

    # -- self-consistency: target_metric must be a real clinic column ------
    bad_metric = ap[~ap["target_metric"].isin(cw_dedup.columns)]
    findings.append(
        Finding(
            table="action_plans",
            check="action_plans.target_metric_is_real_column",
            result="flag" if len(bad_metric) else "pass",
            summary=(
                f"{len(bad_metric)} of {len(ap)} plans target a metric that is not a "
                f"clinic_weekly column."
            ),
            counts={"plans": len(ap), "invalid_target_metric": len(bad_metric)},
            details=[
                {"plan_id": r.plan_id, "target_metric": r.target_metric}
                for r in bad_metric.itertuples()
            ],
        )
    )

    # -- self-consistency: baseline_value vs actual value at plan open -----
    # Report-only: Step 3 (Claimed vs. Verified) recomputes baselines from
    # DATA/TRANSLATION/ rather than trusting the recorded figure.
    col_to_kind = {m.column: m.kind for m in config.METRICS.values() if m.column}
    discrepancies: list[dict] = []
    no_data: list[dict] = []
    checked = 0
    for r in ap.itertuples():
        if r.target_metric not in cw_dedup.columns:
            continue
        wk = _week_monday(r.opened_date)
        row = cw_dedup[(cw_dedup["location_id"] == r.location_id) & (cw_dedup["week_start"] == wk)]
        base = {
            "plan_id": r.plan_id,
            "location_id": r.location_id,
            "location_name": names.get(r.location_id, r.location_id),
            "target_metric": r.target_metric,
            "recorded_baseline": float(r.baseline_value),
            "plan_opened": str(r.opened_date.date()),
            "week_checked": str(wk.date()),
        }
        if len(row) == 0 or pd.isna(row.iloc[0][r.target_metric]):
            center_opened = opened_dates.get(r.location_id)
            base["note"] = (
                "plan opened before the center's first operating week"
                if center_opened is not None and r.opened_date < center_opened
                else "no clinic data for that week"
            )
            if center_opened is not None:
                base["center_opened"] = str(center_opened.date())
            no_data.append(base)
            continue
        checked += 1
        actual = float(row.iloc[0][r.target_metric])
        tol = config.KIND_TOLERANCES[col_to_kind.get(r.target_metric, "percentage")]
        if abs(actual - float(r.baseline_value)) > tol:
            base.update(
                {
                    "actual_that_week": round(actual, 2),
                    "abs_diff": round(abs(actual - float(r.baseline_value)), 2),
                    "tolerance": tol,
                }
            )
            discrepancies.append(base)
    findings.append(
        Finding(
            table="action_plans",
            check="action_plans.baseline_vs_actual_at_open",
            result="flag" if (discrepancies or no_data) else "pass",
            summary=(
                f"Recorded baseline_value disagrees with the metric's actual value in "
                f"the week the plan opened for {len(discrepancies)} of {checked} "
                f"checkable plans; {len(no_data)} plans have no clinic data for their "
                f"opening week at all. Baselines are self-reported; downstream steps "
                f"recompute them from the data instead of trusting the recorded figure. "
                f"Report-only — no correction proposed."
            ),
            counts={
                "plans_checked": checked,
                "baseline_discrepancies": len(discrepancies),
                "plans_without_open_week_data": len(no_data),
            },
            details=discrepancies + no_data,
        )
    )

    # -- referential integrity ---------------------------------------------
    unknown = int((~ap["location_id"].isin(loc["location_id"])).sum())
    findings.append(
        Finding(
            table="action_plans",
            check="action_plans.known_location_ids",
            result="flag" if unknown else "pass",
            summary=f"{unknown} plans reference a location_id missing from locations.",
            counts={"unknown_location_rows": unknown},
        )
    )

    return findings


# ---------------------------------------------------------------------------
# Corrections: proposal + application
# ---------------------------------------------------------------------------


def build_corrections(findings: list[Finding]) -> list[Correction]:
    """Turn correctable findings into the stable accept/decline menu C1..C4.

    IDs are stable by construction: the order below is fixed in code and does
    not depend on the data.
    """
    by_check = {f.check: f for f in findings}
    corrections: list[Correction] = []

    dup = by_check["clinic_weekly.duplicate_rows"]
    corrections.append(
        Correction(
            id="C1",
            table="clinic_weekly",
            kind="drop_duplicates",
            description=(
                f"Remove {dup.counts['duplicate_rows']} exact duplicate rows left by a "
                f"double-run of the weekly load (first occurrence kept)."
            ),
            affected_rows=dup.counts["duplicate_rows"],
            details=dup.details,
        )
    )

    neg = by_check["clinic_weekly.negative_wait_times"]
    corrections.append(
        Correction(
            id="C2",
            table="clinic_weekly",
            kind="negative_wait_to_missing",
            description=(
                f"Set {neg.counts['negative_wait_rows']} negative avg_wait_time_min "
                f"values (clock-sync bug) to missing."
            ),
            affected_rows=neg.counts["negative_wait_rows"],
            details=neg.details,
        )
    )

    emp = by_check["provider_weekly.employment_type_capitalization"]
    corrections.append(
        Correction(
            id="C3",
            table="provider_weekly",
            kind="normalize_employment_type",
            description=(
                f"Lowercase {emp.counts['rows_to_normalize']} employment_type values to "
                f"the canonical full_time / part_time / relief."
            ),
            affected_rows=emp.counts["rows_to_normalize"],
            details=emp.details,
        )
    )

    mat = by_check["locations.maturity_tier_vs_opened_date"]
    corrections.append(
        Correction(
            id="C4",
            table="locations",
            kind="relabel_maturity_tier",
            description=(
                f"Relabel maturity_tier for {mat.counts['mismatched_labels']} centers "
                f"whose label contradicts their opened date (each center listed in "
                f"details). Peer groups downstream use the corrected labels."
            ),
            affected_rows=mat.counts["mismatched_labels"],
            details=mat.details,
        )
    )

    for c in corrections:
        c.scope = CORRECTION_SCOPE[c.kind]
        c.fingerprint = correction_fingerprint(c.kind, c.details)

    return corrections


def apply_corrections(
    accepted_ids: list[str],
    corrections: list[Correction],
    as_of: date,
    log: "RunLog",
) -> dict:
    """Apply accepted corrections to CSV copies in DATA/TRANSLATION/ (normalized names).

    DATA/INPUTS/ is never modified. Declined corrections are logged, not applied.
    Returns the manifest dict (also written to DATA/TRANSLATION/MANIFEST.json).
    """
    text = {t: load_text(t, as_of) for t in TABLES}
    rows_before = {t: len(df) for t, df in text.items()}
    rows_raw = raw_row_counts()
    cells_changed = {t: 0 for t in TABLES}
    applied: list[dict] = []
    declined: list[dict] = []

    for c in corrections:
        provenance = {
            "decision_status": c.status,
            "scope": c.scope,
            "decided_as_of": c.decided_as_of or str(as_of),
            "new_rows": c.new_rows,
        }
        if c.id not in accepted_ids:
            declined.append(
                {"id": c.id, "table": c.table, "description": c.description, **provenance}
            )
            log.write(
                f"apply.{c.id}", "declined", {"table": c.table, "affected_rows": 0, **provenance}
            )
            continue

        df = text[c.table]
        if c.kind == "drop_duplicates":
            mask = df.duplicated()
            text[c.table] = df[~mask].reset_index(drop=True)
            n = int(mask.sum())
        elif c.kind == "negative_wait_to_missing":
            col = pd.to_numeric(df["avg_wait_time_min"], errors="coerce")
            mask = col < 0
            df.loc[mask, "avg_wait_time_min"] = ""
            n = int(mask.sum())
            cells_changed[c.table] += n
        elif c.kind == "normalize_employment_type":
            lowered = df["employment_type"].str.lower()
            mask = df["employment_type"] != lowered
            df.loc[mask, "employment_type"] = lowered[mask]
            n = int(mask.sum())
            cells_changed[c.table] += n
        elif c.kind == "relabel_maturity_tier":
            n = 0
            for d in c.details:
                hit = df["location_id"] == d["location_id"]
                df.loc[hit, "maturity_tier"] = d["computed_tier"]
                n += int(hit.sum())
            cells_changed[c.table] += n
        else:  # pragma: no cover — unknown kinds are a programming error
            raise ValueError(f"unknown correction kind: {c.kind}")

        applied.append(
            {
                "id": c.id,
                "table": c.table,
                "description": c.description,
                "rows_affected": n,
                **provenance,
            }
        )
        log.write(
            f"apply.{c.id}", "applied", {"table": c.table, "rows_affected": n, **provenance}
        )

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        text[t].to_csv(config.DATA_FILES[t], index=False)

    manifest = {
        "step": "Data Validation & Check",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": str(as_of),
        "latest_complete_week": str(config.latest_complete_week(as_of)),
        "source": {
            t: {
                "path": str(config.RAW_FILES[t].relative_to(config.REPO_ROOT)),
                "sha256": _sha256(config.RAW_FILES[t]),
                "rows": rows_raw[t],
                "rows_in_scope": rows_before[t],
            }
            for t in TABLES
        },
        "corrections_accepted": applied,
        "corrections_declined": declined,
        "corrections_new": [c.id for c in corrections if c.status == "new"],
        "corrections_carried": [
            {
                "id": c.id,
                "decision": c.prior_decision,
                "decided_as_of": c.decided_as_of,
                "new_rows": c.new_rows,
            }
            for c in corrections
            if c.status == "carried"
        ],
        "tables": {
            t: {
                "file": config.DATA_FILES[t].name,
                "rows_before": rows_before[t],
                "rows_after": len(text[t]),
                "cells_changed": cells_changed[t],
            }
            for t in TABLES
        },
        "usage_flags": [
            {
                "flag": "membership_definition_change",
                "date": str(config.MEMBERSHIP_DEFINITION_CHANGE),
                "meaning": (
                    "membership_conversion_pct denominator widened on this date; "
                    "weeks before it are excluded from membership trend windows."
                ),
            }
        ],
        "note": "DATA/INPUTS/ is never modified; the pipeline computes off DATA/TRANSLATION/.",
    }
    (config.DATA_DIR / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------


class RunLog:
    """DATA/OUTPUTS/validation/runlog.jsonl — one line per check (and apply event)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w")

    def write(self, check: str, result: str, counts: dict) -> None:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "step": "validate",
            "check": check,
            "result": result,
            "counts": counts,
        }
        self._fh.write(json.dumps(line) + "\n")

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

TABLE_HEADINGS = {
    "locations": "Centers (locations)",
    "clinic_weekly": "Clinic weekly operating data (clinic_weekly)",
    "provider_weekly": "Doctor weekly data (provider_weekly)",
    "action_plans": "Improvement plans (action_plans)",
}


def _md_maturity_bullets(f: Finding) -> list[str]:
    out = []
    for d in f.details:
        out.append(
            f"  - **{d['location_name']}** — opened {d['opened_date']} "
            f"({d['months_open']} months ago): labeled *{d['labeled_tier']}*, "
            f"should be *{d['computed_tier']}*."
        )
    return out


def _md_baseline_bullets(f: Finding) -> list[str]:
    out = []
    for d in f.details:
        if "actual_that_week" in d:
            out.append(
                f"  - {d['plan_id']} ({d['location_name']}, {d['target_metric']}): recorded "
                f"baseline {d['recorded_baseline']} vs actual {d['actual_that_week']} in the "
                f"week of {d['week_checked']} (gap {d['abs_diff']}, tolerance {d['tolerance']})."
            )
        else:
            extra = f" — center opened {d['center_opened']}" if "center_opened" in d else ""
            out.append(
                f"  - {d['plan_id']} ({d['location_name']}, {d['target_metric']}): "
                f"{d['note']}{extra} (plan opened {d['plan_opened']})."
            )
    return out


def write_report_md(
    findings: list[Finding], corrections: list[Correction], as_of: date, path: Path
) -> None:
    flags = [f for f in findings if f.result == "flag"]
    lines: list[str] = []
    lines.append("# Data Validation & Check")
    lines.append("")
    lines.append(
        f"Run for the Monday **{as_of}** digest · {len(findings)} checks ran, "
        f"{len(flags)} found something · {len(corrections)} corrections proposed."
    )
    lines.append("")
    lines.append(
        "Raw files in `DATA/INPUTS/` are never modified. Each proposed correction below is an "
        "accept/decline decision; accepted ones are applied to CSV copies in `DATA/TRANSLATION/` "
        "(see the *Proposed corrections* section at the end)."
    )

    for table in TABLES:
        lines.append("")
        lines.append(f"## {TABLE_HEADINGS[table]}")
        lines.append("")
        for f in [x for x in findings if x.table == table]:
            marker = "✓" if f.result == "pass" else "•"
            lines.append(f"- {marker} {f.summary}")
            if f.check == "locations.maturity_tier_vs_opened_date" and f.details:
                lines.extend(_md_maturity_bullets(f))
            if f.check == "action_plans.baseline_vs_actual_at_open" and f.details:
                lines.extend(_md_baseline_bullets(f))
            if f.check.startswith("provider_weekly.rollup_consistency") and f.details:
                worst = f.details[0]
                lines.append(
                    f"  - Largest gap: {worst['location_name']}, week of "
                    f"{worst['week_start']} — clinic {worst['clinic_value']} vs "
                    f"doctor-weighted {worst['provider_weighted']} "
                    f"(diff {worst['abs_diff']} pts)."
                )
            if f.check == "clinic_weekly.throughput_formula" and f.details:
                for d in f.details:
                    lines.append(
                        f"  - {d['location_name']}, week of {d['week_start']}: recorded "
                        f"{d['recorded']} vs computed {d['computed']}."
                    )

    lines.append("")
    lines.append("## Proposed corrections (accept / decline)")
    lines.append("")
    for c in corrections:
        lines.append(f"- **{c.id}** ({c.table}, {c.affected_rows} rows): {c.description}")
        if c.kind == "relabel_maturity_tier":
            for d in c.details:
                lines.append(
                    f"  - {d['location_name']}: {d['labeled_tier']} → {d['computed_tier']}"
                )
    lines.append("")
    lines.append(
        "Accept with `python -m pipeline.validate --accept all` (or a comma-separated "
        "subset like `--accept C1,C3`). Accepted corrections are applied to copies in "
        "`DATA/TRANSLATION/`; declined ones are logged, never applied. `DATA/INPUTS/` is untouched either way."
    )
    lines.append("")
    path.write_text("\n".join(lines))


def write_report_json(
    findings: list[Finding], corrections: list[Correction], as_of: date, path: Path
) -> None:
    doc = {
        "step": "Data Validation & Check",
        "as_of": str(as_of),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "checks_run": len(findings),
            "checks_flagged": sum(1 for f in findings if f.result == "flag"),
            "corrections_proposed": len(corrections),
        },
        "tables": {
            t: [
                {
                    "check": f.check,
                    "result": f.result,
                    "summary": f.summary,
                    "counts": f.counts,
                    "details": f.details,
                }
                for f in findings
                if f.table == t
            ]
            for t in TABLES
        },
        "corrections_proposed": [c.id for c in corrections],
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")


def write_corrections_json(corrections: list[Correction], as_of: date, path: Path) -> None:
    doc = {
        "step": "Data Validation & Check",
        "as_of": str(as_of),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "how_to_accept": (
            "python -m pipeline.validate --accept all  (or --accept C1,C3). Accepted "
            "corrections are applied to CSV copies in DATA/TRANSLATION/; DATA/INPUTS/ is never modified. "
            "Only corrections with status 'new' are a question — 'carried' ones were "
            "already decided on an earlier Monday and apply automatically."
        ),
        "latest_complete_week": str(config.latest_complete_week(as_of)),
        # Every correction, so any consumer can look one up by id. What needs a
        # human this Monday is the "pending" list.
        "corrections": [
            {
                "id": c.id,
                "table": c.table,
                "kind": c.kind,
                "description": c.description,
                "affected_rows": c.affected_rows,
                "details": c.details,
                "status": c.status,
                "scope": c.scope,
                "fingerprint": c.fingerprint,
                "prior_decision": c.prior_decision,
                "decided_as_of": c.decided_as_of,
                "new_rows": c.new_rows,
            }
            for c in corrections
        ],
        "pending": [c.id for c in corrections if c.status == "new"],
        "carried": [c.id for c in corrections if c.status == "carried"],
        "totals": {
            "corrections": len(corrections),
            "pending": sum(1 for c in corrections if c.status == "new"),
            "carried": sum(1 for c in corrections if c.status == "carried"),
            "carried_new_rows": sum(c.new_rows for c in corrections if c.status == "carried"),
        },
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_checks(as_of: date, log: RunLog) -> tuple[list[Finding], list[Correction]]:
    """Run every check, table by table, in the locked order. Deterministic."""
    parsed = load_parsed(as_of)
    loc = parsed["locations"]
    cw_text = load_text("clinic_weekly", as_of)

    # clinic checks produce the deduplicated frame the cross-table checks use
    cw_findings, cw_dedup = check_clinic_weekly(parsed["clinic_weekly"], cw_text, loc, as_of)

    findings: list[Finding] = []
    findings += check_locations(loc, cw_dedup, as_of)
    findings += cw_findings
    findings += check_provider_weekly(parsed["provider_weekly"], cw_dedup, loc)
    findings += check_action_plans(parsed["action_plans"], cw_dedup, loc)

    # keep the report in locked table order
    order = {t: i for i, t in enumerate(TABLES)}
    findings.sort(key=lambda f: order[f.table])

    for f in findings:
        log.write(f.check, f.result, f.counts)

    corrections = build_corrections(findings)
    classify_corrections(corrections, load_decisions())
    for c in corrections:
        log.write(
            f"decision.{c.id}",
            c.status,
            {
                "kind": c.kind,
                "scope": c.scope,
                "affected_rows": c.affected_rows,
                "new_rows": c.new_rows,
                "prior_decision": c.prior_decision,
                "decided_as_of": c.decided_as_of,
            },
        )
    return findings, corrections


def snapshot_validation(as_of: date) -> Path:
    """Copy this run's validation artifacts into DATA/OUTPUTS/<as_of>/validation/.

    DATA/OUTPUTS/validation/ is the *current* run's working copy — the next
    Monday's run overwrites it. The snapshot is what makes an old digest's
    receipts still resolve after a later run: each Monday keeps the checks it
    was actually built on. The working copy stays where it is so every existing
    receipt path (and the narrator's artifact map) is unchanged.
    """
    dest = config.OUTPUTS_DIR / str(as_of) / "validation"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("report.md", "report.json", "corrections_proposed.json", "runlog.jsonl"):
        src = VALIDATION_DIR / name
        if src.exists():
            (dest / name).write_bytes(src.read_bytes())
    manifest = config.DATA_DIR / "MANIFEST.json"
    if manifest.exists():
        (dest / "MANIFEST.json").write_bytes(manifest.read_bytes())
    return dest


def parse_accept(value: str, corrections: list[Correction]) -> list[str]:
    """Ids the human accepted this run. "all" means every *pending* correction —
    already-decided ones are not re-accepted, they simply carry."""
    pending = [c.id for c in corrections if c.status == "new"]
    valid = [c.id for c in corrections]
    if value.strip().lower() == "all":
        return pending
    ids = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [i for i in ids if i not in valid]
    if unknown:
        raise SystemExit(
            f"Unknown correction id(s): {', '.join(unknown)}. Valid ids: {', '.join(valid)}."
        )
    return [i for i in ids if i in pending]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.validate",
        description="Step 1 — Data Validation & Check (deterministic; proposes corrections).",
    )
    config.add_as_of_argument(parser)
    parser.add_argument(
        "--accept",
        default=None,
        metavar="all|C1,C2",
        help=(
            "Apply accepted corrections to CSV copies in DATA/TRANSLATION/. "
            '"all" or a comma-separated list of correction ids. '
            "Omit to only propose (default). DATA/INPUTS/ is never modified."
        ),
    )
    parser.add_argument(
        "--actor",
        default="operator",
        help="Who made the accept/decline call; recorded in the decision log.",
    )
    parser.add_argument(
        "--fresh-decisions",
        action="store_true",
        help=(
            "Rotate DATA/OUTPUTS/correction_decisions.jsonl into decisions_archive/ "
            "before running, so every correction is a new question again. Use for a "
            "clean canonical run; nothing is deleted."
        ),
    )
    parser.add_argument(
        "--rotate-decisions",
        action="store_true",
        help=(
            "Rotate the decision log and exit, running no checks. This is the "
            "door the app's full reset uses — it must run AFTER the working copy "
            "is rebuilt, or the rebuild would record the decisions right back."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the result as JSON (how the API layer calls --rotate-decisions).",
    )
    args = parser.parse_args(argv)

    if args.rotate_decisions:
        moved = archive_decisions()
        if args.json:
            print(json.dumps({"ok": True, "archived_to": str(moved) if moved else None}))
        else:
            print(f"Decision log rotated to {moved}" if moved else "No decision log to rotate.")
        return

    if args.fresh_decisions:
        moved = archive_decisions()
        print(f"  Decision log rotated to {moved}" if moved else "  No decision log to rotate.")

    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    log = RunLog(VALIDATION_DIR / "runlog.jsonl")

    findings, corrections = run_checks(args.as_of, log)
    write_report_md(findings, corrections, args.as_of, VALIDATION_DIR / "report.md")
    write_report_json(findings, corrections, args.as_of, VALIDATION_DIR / "report.json")
    write_corrections_json(corrections, args.as_of, VALIDATION_DIR / "corrections_proposed.json")

    flags = sum(1 for f in findings if f.result == "flag")
    pending = [c for c in corrections if c.status == "new"]
    carried = [c for c in corrections if c.status == "carried"]
    print(f"Data Validation & Check — as of {args.as_of}")
    print(f"  {len(findings)} checks ran, {flags} found something.")
    print(f"  Panel read through the week of {config.latest_complete_week(args.as_of)}.")
    print(f"  Report: {VALIDATION_DIR / 'report.md'}")
    if carried:
        new_rows = sum(c.new_rows for c in carried)
        print(
            f"  Carried forward: {', '.join(c.id for c in carried)} — decided on an "
            f"earlier Monday, applied without asking ({new_rows} newly covered rows)."
        )
    print(
        f"  Corrections needing a decision: {', '.join(c.id for c in pending) or 'none'} "
        f"({VALIDATION_DIR / 'corrections_proposed.json'})"
    )

    if args.accept is None:
        print("  No corrections applied (propose-only run). "
              "Use --accept all or --accept C1,C3 to build DATA/TRANSLATION/.")
    else:
        accepted = parse_accept(args.accept, corrections)
        record_decisions(corrections, accepted, args.as_of, actor=args.actor)
        manifest = apply_corrections(
            effective_accepted(corrections, accepted), corrections, args.as_of, log
        )
        applied = ", ".join(a["id"] for a in manifest["corrections_accepted"]) or "none"
        declined = ", ".join(d["id"] for d in manifest["corrections_declined"]) or "none"
        print(f"  Applied: {applied} · Declined (logged): {declined}")
        for t in TABLES:
            info = manifest["tables"][t]
            print(f"    DATA/TRANSLATION/{info['file']}: {info['rows_before']} → {info['rows_after']} rows, "
                  f"{info['cells_changed']} cells changed")
        print(f"  Manifest: {config.DATA_DIR / 'MANIFEST.json'}")

    log.close()
    snapshot = snapshot_validation(args.as_of)
    print(f"  Snapshot for this Monday: {snapshot}")


if __name__ == "__main__":
    main()
