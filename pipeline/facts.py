"""Step 3a — the deterministic fact table behind "Claimed vs. Verified".

For every action plan (all owners, every run) this module computes, with no
LLM anywhere near it:

  baseline / target        as recorded on the plan
  baseline_at_open         what the metric actually was the week the plan
                           opened (Data Validation & Check flags mismatches)
  actual_4wk               the metric's level over the last 4 complete weeks
                           (CSAT weeks are weighted by survey responses)
  gap_closed_pct           sign-aware: 100% = target reached from baseline;
                           negative = moved the wrong way
  trend_direction          last 4 weeks vs the 4 weeks before them —
                           improving / worsening / flat (toward the target)
  days_overdue             vs the digest's as-of Monday
  staleness_days           days since the owner last touched the record
  summary                  a one-line plain-English statement of the facts

Every judgment layer above (pipeline.verdicts) reads this table; the harness
allows a generated sentence to contain only numbers that live here.

Reads the corrected data in DATA/TRANSLATION/ (built by `pipeline.validate --accept`).
Leader-facing fields use real center names and plain-English metric names;
location IDs are for logs and the audit trail only.

CLI:
    python -m pipeline.facts --as-of 2026-05-04

Output: DATA/OUTPUTS/<as-of>/facts.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import pandas as pd

from . import config
from .signals import RECENT_WEEKS, latest_complete_week

STATUS_DISPLAY = {
    "not_started": "Not started",
    "on_track": "On track",
    "at_risk": "At risk",
    "complete": "Complete",
}

# A 4-week mean within this much of the prior 4-week mean counts as flat
# (half the metric kind's comparison tolerance — movement smaller than half
# a tolerance band is noise, not a trend).
TREND_FLAT_TOLERANCE = {
    "percentage": 0.5,
    "csat": 0.025,
    "throughput": 0.025,
    "wait": 0.25,
    "count": 0.5,
}


def fmt_short(metric: config.Metric, v: float | None) -> str:
    """Compact leader-facing value for use inside sentences: 77.7%, 4.70,
    2.85, 9.7 min. (signals.fmt spells out units; sentences already name
    the metric, so the short form avoids saying 'appts per doctor-hour'
    twice.)"""
    if v is None or pd.isna(v):
        return "n/a"
    if metric.key == "revenue_per_appt":
        return f"${v:,.0f}"
    k = metric.kind
    if k == "percentage":
        return f"{v:.1f}%"
    if k == "csat":
        return f"{v:.2f}"
    if k == "throughput":
        return f"{v:.2f}"
    if k == "wait":
        return f"{v:.1f} min"
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def _r(v, nd=3):
    """JSON-safe rounding (None passes through)."""
    return None if v is None else round(float(v), nd)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_tables() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Corrected locations + clinic weekly + action plans from DATA/TRANSLATION/."""
    needed = [config.DATA_FILES[k] for k in ("locations", "clinic_weekly", "action_plans")]
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        raise SystemExit(
            "DATA/TRANSLATION/ is missing corrected files: "
            + ", ".join(missing)
            + "\nRun `python -m pipeline.validate --accept all` first."
        )
    loc = pd.read_csv(config.DATA_FILES["locations"], parse_dates=["opened_date"])
    cw = pd.read_csv(config.DATA_FILES["clinic_weekly"], parse_dates=["week_start"])
    ap = pd.read_csv(
        config.DATA_FILES["action_plans"],
        parse_dates=["opened_date", "due_date", "last_status_update"],
    )
    return loc, cw, ap


# ---------------------------------------------------------------------------
# Per-plan arithmetic
# ---------------------------------------------------------------------------


def window_level(
    g: pd.DataFrame, metric: config.Metric, lo: pd.Timestamp, hi: pd.Timestamp
) -> tuple[float | None, int]:
    """Level of one metric at one center over (lo, hi] -> (level, weeks used).

    Plain mean of non-missing weeks; CSAT weeks are weighted by survey
    responses so a 12-response week can't swing the level.
    """
    win = g[(g["week_start"] > lo) & (g["week_start"] <= hi)]
    col = metric.column or metric.key
    if metric.key == "client_csat":
        d = win.dropna(subset=[col])
        d = d[d["csat_responses"] > 0]
        if d.empty:
            return None, 0
        resp = d["csat_responses"].sum()
        return float((d[col] * d["csat_responses"]).sum() / resp), len(d)
    vals = win[col].dropna()
    if vals.empty:
        return None, 0
    return float(vals.mean()), len(vals)


def trend_direction(
    actual: float | None, prior: float | None, direction_to_target: str, kind: str
) -> str:
    if actual is None or prior is None:
        return "unknown"
    delta = actual - prior
    if abs(delta) <= TREND_FLAT_TOLERANCE[kind]:
        return "flat"
    toward_target = delta > 0 if direction_to_target == "up" else delta < 0
    return "improving" if toward_target else "worsening"


def facts_summary(fact: dict, metric: config.Metric) -> str:
    """One deterministic plain-English line — the middle column's voice."""
    parts = [
        f"Baseline {fact['baseline_display']} → target {fact['target_display']}; "
        f"last {fact['window_weeks']} weeks averaged {fact['actual_4wk_display']} "
        f"({fact['trend_direction']}); {fact['gap_closed_display']} of the gap closed."
    ]
    if fact["is_overdue"]:
        parts.append(f"Due {fact['due_date']} — {fact['days_overdue']} days overdue.")
    if fact["staleness_days"] >= 28:
        parts.append(f"No status update in {fact['staleness_days']} days.")
    return " ".join(parts)


def build_fact(plan, loc_row, cw: pd.DataFrame, as_of: date, latest: pd.Timestamp) -> dict:
    """`plan` is one row of action_plans as a namedtuple (df.itertuples())."""
    metric = config.METRICS[plan.target_metric]
    kind = metric.kind
    g = cw[cw["location_id"] == plan.location_id]

    baseline = float(plan.baseline_value)
    target = float(plan.target_value)
    direction = "up" if target >= baseline else "down"

    recent_cut = latest - pd.Timedelta(weeks=RECENT_WEEKS)
    actual, weeks_actual = window_level(g, metric, recent_cut, latest)
    prior, weeks_prior = window_level(
        g, metric, recent_cut - pd.Timedelta(weeks=RECENT_WEEKS), recent_cut
    )

    notes: list[str] = []
    if metric.key == "client_csat":
        notes.append("CSAT levels weight each week by its survey responses.")

    # What the metric actually was the week the plan opened (vs the recorded
    # baseline). Progress is still measured against the recorded baseline;
    # a mismatch is a finding, not a silent rewrite.
    opened = pd.Timestamp(plan.opened_date)
    week_open = opened - pd.Timedelta(days=int(opened.weekday()))
    open_row = g[g["week_start"] == week_open]
    col = metric.column or metric.key
    baseline_at_open = None
    if not open_row.empty and not pd.isna(open_row[col].iloc[0]):
        baseline_at_open = float(open_row[col].iloc[0])
    baseline_matches = None
    if baseline_at_open is not None:
        baseline_matches = abs(baseline_at_open - baseline) <= config.KIND_TOLERANCES[kind]
        if not baseline_matches:
            notes.append(
                f"Recorded baseline {fmt_short(metric, baseline)} does not match the "
                f"metric's actual level the week the plan opened "
                f"({fmt_short(metric, baseline_at_open)}) — flagged by Data Validation & Check; "
                f"progress here is measured against the recorded baseline."
            )
    else:
        notes.append(
            f"No clinic data for the week this plan opened ({plan.opened_date.date()}), "
            f"so the recorded baseline could not be cross-checked."
        )

    gap_closed = None
    if actual is not None and abs(target - baseline) > 1e-9:
        gap_closed = 100.0 * (actual - baseline) / (target - baseline)

    below_baseline = False
    if actual is not None:
        bad_move = (baseline - actual) if direction == "up" else (actual - baseline)
        below_baseline = bad_move > config.KIND_TOLERANCES[kind]
    target_met = gap_closed is not None and gap_closed >= 100.0

    due = pd.Timestamp(plan.due_date).date()
    updated = pd.Timestamp(plan.last_status_update).date()
    is_overdue = due < as_of
    days_overdue = max((as_of - due).days, 0)
    staleness_days = max((as_of - updated).days, 0)

    fact = {
        "plan_id": plan.plan_id,
        "center": str(loc_row["location_name"]),
        "location_id": plan.location_id,  # audit trail only — never shown to a leader
        "owner": plan.owner_name,
        "metric": metric.key,
        "metric_display": metric.display_name,
        "metric_kind": kind,
        "direction_to_target": direction,
        "reported_status": plan.status,
        "reported_status_display": STATUS_DISPLAY.get(plan.status, plan.status),
        "baseline": _r(baseline),
        "baseline_display": fmt_short(metric, baseline),
        "target": _r(target),
        "target_display": fmt_short(metric, target),
        "baseline_at_open": _r(baseline_at_open),
        "baseline_at_open_display": fmt_short(metric, baseline_at_open),
        "baseline_matches_recorded": baseline_matches,
        "actual_4wk": _r(actual),
        "actual_4wk_display": fmt_short(metric, actual),
        "weeks_in_actual": weeks_actual,
        "prior_4wk": _r(prior),
        "prior_4wk_display": fmt_short(metric, prior),
        "weeks_in_prior": weeks_prior,
        "window_weeks": RECENT_WEEKS,
        "change_from_baseline": _r(None if actual is None else actual - baseline),
        "gap_closed_pct": _r(gap_closed, 1),
        "gap_closed_display": "n/a" if gap_closed is None else f"{gap_closed:.0f}%",
        "trend_direction": trend_direction(actual, prior, direction, kind),
        "below_baseline": below_baseline,
        "target_met": target_met,
        "opened_date": str(pd.Timestamp(plan.opened_date).date()),
        "due_date": str(due),
        "is_overdue": is_overdue,
        "days_overdue": days_overdue,
        "last_status_update": str(updated),
        "staleness_days": staleness_days,
        "notes": notes,
    }
    fact["summary"] = facts_summary(fact, metric)
    return fact


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_facts(as_of: date, write: bool = True) -> dict:
    loc, cw, ap = load_tables()
    latest = latest_complete_week(as_of)
    cw = cw[cw["week_start"] <= latest]
    loc_by_id = loc.set_index("location_id")

    plans = [
        build_fact(plan, loc_by_id.loc[plan.location_id], cw, as_of, latest)
        for plan in ap.sort_values("plan_id").itertuples()
    ]

    manifest_path = config.DATA_DIR / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    doc = {
        "step": "Claimed vs. Verified — fact table",
        "as_of": str(as_of),
        "latest_complete_week": str(latest.date()),
        "window_weeks": RECENT_WEEKS,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_provenance": {
            "computed_from": "DATA/TRANSLATION/ (corrected copies; DATA/INPUTS/ untouched)",
            "manifest_created_at": manifest.get("created_at"),
            "corrections_accepted": [c["id"] for c in manifest.get("corrections_accepted", [])],
        },
        "plans": plans,
    }
    if write:
        out_dir = config.OUTPUTS_DIR / str(as_of)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "facts.json").write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.facts",
        description="Step 3a — deterministic fact table for every action plan.",
    )
    config.add_as_of_argument(parser)
    args = parser.parse_args(argv)

    doc = build_facts(args.as_of)
    print(f"Fact table — as of {args.as_of} (latest complete week {doc['latest_complete_week']})")
    print(f"  {len(doc['plans'])} action plans")
    for f in doc["plans"]:
        print(
            f"   {f['plan_id']} {f['center']:<16} {f['metric']:<26} "
            f"reported {f['reported_status_display']:<11} | {f['summary']}"
        )
    print(f"  Output: {config.OUTPUTS_DIR / str(args.as_of) / 'facts.json'}")


if __name__ == "__main__":
    main()
