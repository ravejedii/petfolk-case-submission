"""Step 2 — Signal engine: what earns attention on a Monday morning.

For every center x metric it answers three separate questions. They are scored
separately by design, so an issue cannot hide: a calm week-to-week metric can
water down a slow slide if you blend too early.

  1. Spike     — did the latest week jump away from this center's own normal?
  2. Drift     — has the last month quietly slid vs. the prior three months?
  3. Gap       — is the center simply running below its true peer group?

Each question produces its own score; only movement in the metric's *bad*
direction scores at all. The three scores blend into one priority
(drift 0.40 / gap 0.35 / spike 0.25), signals are ranked, and suppression
rules hold back what shouldn't reach a leader — every suppression is listed
with its reason, visible, never vanished.

Everything here is deterministic — no LLM. Formulas, windows, weights, and
thresholds are documented in docs/SCORING.md (the single source of truth);
this module implements exactly what that document says.

Reads the corrected data in DATA/TRANSLATION/ (built by `pipeline.validate --accept`);
peer groups use the CORRECTED maturity labels.

CLI:
    python -m pipeline.signals --as-of 2026-05-04
    python -m pipeline.signals --as-of 2026-05-04 --leader "Dr. Priya Raghunathan"

Outputs (under DATA/OUTPUTS/<as-of>/):
    signals.json           ranked signals + suppressed list + parameters used
    signals_runlog.jsonl   one line per ranked/suppressed signal + summary

Leader-facing fields use real center names and plain-English metric names.
Location IDs appear only for logs and the audit trail.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

# ---------------------------------------------------------------------------
# Tunable knobs — every value here is documented and defended in docs/SCORING.md.
# Change them there and here together; the doc is the source of truth.
# ---------------------------------------------------------------------------

DEFAULT_LEADER = "Dr. Priya Raghunathan"

RECENT_WEEKS = 4  # "the last month" — drift numerator, gap level
BASELINE_WEEKS = 12  # "the prior quarter" — drift & spike baselines

WEIGHTS = {"drift": 0.40, "gap": 0.35, "spike": 0.25}

SCORE_CAP = 10.0  # each sub-score is capped here; priority lives in [0, 10]
RANK_CUTOFF = 2.0  # blended priority below this does not rank
LIST_FLOOR = 1.0  # candidates in [LIST_FLOOR, RANK_CUTOFF) are listed as suppressed
PER_CENTER_CAP = 2  # max ranked signals per center, so one center can't flood the digest

MIN_HISTORY_WEEKS = 16  # a center needs 4 recent + 12 baseline weeks to be trend-scored
MIN_BASELINE_WEEKS = 8  # of the 12-week baseline window, at least this many non-missing
MIN_RECENT_WEEKS_DRIFT = 3  # of the last 4 weeks, at least this many non-missing
MIN_RECENT_WEEKS_GAP = 2
MIN_PEERS = 4  # gap needs at least this many peers with a level

MIN_APPTS_4WK = 200  # appointment-based rates need this many completed appts pooled over 4wk
CSAT_MIN_POOLED_RESPONSES = 60  # CSAT needs this many pooled survey responses
CSAT_WIDEN_WEEKS = 8  # if 4 weeks pool too few responses, widen the level window to this
CSAT_SPIKE_MIN_WEEK_RESPONSES = 25  # a single-week CSAT spike needs at least this many responses

MAD_TO_SIGMA = 1.4826  # MAD -> sigma for a normal distribution (robust z)

# Minimum scale per metric kind: a flat series must not turn a tiny wobble
# into a giant z-score. Units are the metric's own units.
SCALE_FLOORS = {
    "percentage": 0.5,  # points
    "csat": 0.10,  # CSAT points (weekly CSAT on ~10-60 responses is noisy)
    "throughput": 0.05,
    "wait": 0.5,  # minutes
    "count": 1.0,  # whole events (call-outs, open reqs)
}

# Metrics whose weekly values are built on per-appointment denominators;
# these are the ones the MIN_APPTS_4WK small-denominator rule protects.
APPOINTMENT_BASED = {
    "appts_per_doctor_hour",
    "recheck_compliance_pct",
    "record_completion_24h_pct",
    "callback_compliance_pct",
    "avg_wait_time_min",
    "revenue_per_appt",
    "membership_conversion_pct",
    "no_show_rate",
}

TIER_DISPLAY = {
    "mature": "mature centers (network standard)",
    "ramping": "ramping centers",
    "new": "new centers",
}


# ---------------------------------------------------------------------------
# Formatting — plain-English values for leader-facing fields
# ---------------------------------------------------------------------------


def fmt(metric: config.Metric, v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    if metric.key == "revenue_per_appt":
        return f"${v:,.0f}"
    k = metric.kind
    if k == "percentage":
        return f"{v:.1f}%"
    if k == "csat":
        return f"{v:.2f} / 5"
    if k == "throughput":
        return f"{v:.2f} appts per doctor-hour"
    if k == "wait":
        return f"{v:.1f} min"
    # count
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def _r(v, nd=3):
    """JSON-safe rounding (None passes through)."""
    return None if v is None else round(float(v), nd)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load corrected locations + clinic weekly from DATA/TRANSLATION/ (never DATA/INPUTS/)."""
    missing = [str(p) for p in (config.DATA_FILES["locations"], config.DATA_FILES["clinic_weekly"]) if not p.exists()]
    if missing:
        raise SystemExit(
            "DATA/TRANSLATION/ is missing corrected files: "
            + ", ".join(missing)
            + "\nRun `python -m pipeline.validate --accept all` first."
        )
    loc = pd.read_csv(config.DATA_FILES["locations"], parse_dates=["opened_date"])
    cw = pd.read_csv(config.DATA_FILES["clinic_weekly"], parse_dates=["week_start"])
    # Derived metric: client no-show rate, as a percentage.
    denom = cw["appts_completed"] + cw["appts_no_show"]
    cw["no_show_rate"] = 100.0 * cw["appts_no_show"] / denom.where(denom > 0)
    return loc, cw


def latest_complete_week(as_of: date) -> pd.Timestamp:
    """The Monday of the last complete week strictly before as-of.

    Delegates to config so the signal engine and Data Validation & Check read
    the panel to exactly the same week.
    """
    return pd.Timestamp(config.latest_complete_week(as_of))


def metric_frame(cw: pd.DataFrame, metric: config.Metric, latest: pd.Timestamp) -> pd.DataFrame:
    """Weekly values for one metric, restricted to usable weeks (<= latest;
    membership additionally restricted to post-definition-change weeks)."""
    col = metric.column or metric.key
    cols = ["location_id", "week_start", col]
    if metric.key == "client_csat":
        cols.append("csat_responses")
    df = cw.loc[cw["week_start"] <= latest, cols].rename(columns={col: "value"})
    if metric.post_definition_change_only:
        df = df[df["week_start"] >= pd.Timestamp(config.MEMBERSHIP_DEFINITION_CHANGE)]
    return df


# ---------------------------------------------------------------------------
# Score arithmetic
# ---------------------------------------------------------------------------


def robust_scale(values: pd.Series, kind: str) -> float:
    """MAD-based sigma with a per-kind floor so flat series don't cry wolf."""
    mad = float((values - values.median()).abs().median())
    return max(MAD_TO_SIGMA * mad, SCALE_FLOORS[kind])


def bad_move(reference: float, current: float, bad_direction: str) -> float:
    """How far current sits from reference *in the bad direction* (>= 0)."""
    move = (reference - current) if bad_direction == "down" else (current - reference)
    return max(move, 0.0)


def csat_pooled_level(window: pd.DataFrame) -> tuple[float | None, int, int]:
    """Response-weighted CSAT over a window -> (level, pooled_responses, weeks)."""
    d = window.dropna(subset=["value"])
    d = d[d["csat_responses"] > 0]
    if d.empty:
        return None, 0, 0
    resp = int(d["csat_responses"].sum())
    level = float((d["value"] * d["csat_responses"]).sum() / resp)
    return level, resp, len(d)


def _naive_csat_scores(
    g: pd.DataFrame,
    recent: pd.Series,
    drift_base: pd.Series,
    spike_base: pd.Series,
    latest_val: float | None,
    metric: config.Metric,
    tier: str,
    peers: "PeerStats",
    latest: pd.Timestamp,
) -> dict:
    """What thin weekly CSAT numbers *would have* scored with no pooling.

    Used only to make the small-sample suppression visible: when the pooled
    level tells a calmer story than the raw weeks, the suppressed listing
    shows both. Same formulas as the real sub-scores, but the level is the
    plain 4-week mean and the single-week spike ignores the response minimum.
    """
    naive_level = float(recent.mean()) if len(recent) else None
    subs: dict[str, float | None] = {"drift": None, "gap": None, "spike": None}
    if naive_level is not None and len(drift_base) >= MIN_BASELINE_WEEKS:
        scale = robust_scale(drift_base, metric.kind)
        subs["drift"] = min(bad_move(float(drift_base.mean()), naive_level, metric.bad_direction) / scale, SCORE_CAP)
    if latest_val is not None and len(spike_base) >= MIN_BASELINE_WEEKS:
        scale = robust_scale(spike_base, metric.kind)
        subs["spike"] = min(bad_move(float(spike_base.median()), latest_val, metric.bad_direction) / scale, SCORE_CAP)
    members = peers.tier_members.get(tier, [])
    peer_levels = peers.levels[peers.levels.index.isin(members)].dropna()
    if naive_level is not None and len(peer_levels) >= MIN_PEERS:
        spread = robust_scale(peer_levels, metric.kind)
        subs["gap"] = min(bad_move(float(peer_levels.median()), naive_level, metric.bad_direction) / spread, SCORE_CAP)
    avail = {k: v for k, v in subs.items() if v is not None}
    wsum = sum(WEIGHTS[k] for k in avail)
    priority = sum(WEIGHTS[k] * v for k, v in avail.items()) / wsum if wsum else None
    latest_resp = 0
    if latest in g.index and not pd.isna(g.loc[latest, "csat_responses"]):
        latest_resp = int(g.loc[latest, "csat_responses"])
    return {
        "level_4wk_unweighted": _r(naive_level),
        "latest_week_value": _r(latest_val),
        "latest_week_responses": latest_resp,
        "scores": {k: _r(v, 2) for k, v in subs.items()},
        "priority": _r(priority, 2),
    }


# ---------------------------------------------------------------------------
# Per (center x metric) scoring
# ---------------------------------------------------------------------------


@dataclass
class PeerStats:
    """4-week levels for every center on one metric, plus tier membership."""

    levels: pd.Series  # index location_id -> 4wk level (CSAT: response-weighted)
    tier_members: dict[str, list[str]]  # tier -> location_ids


def peer_stats(df: pd.DataFrame, metric: config.Metric, loc: pd.DataFrame, latest: pd.Timestamp) -> PeerStats:
    recent = df[df["week_start"] > latest - pd.Timedelta(weeks=RECENT_WEEKS)]
    if metric.key == "client_csat":
        levels = {}
        for lid, g in recent.groupby("location_id"):
            level, _, _ = csat_pooled_level(g)
            if level is not None:
                levels[lid] = level
        levels = pd.Series(levels, dtype=float)
    else:
        levels = recent.dropna(subset=["value"]).groupby("location_id")["value"].mean()
    tiers = {t: list(g["location_id"]) for t, g in loc.groupby("maturity_tier")}
    return PeerStats(levels=levels, tier_members=tiers)


def score_pair(
    df: pd.DataFrame,
    metric: config.Metric,
    lid: str,
    tier: str,
    peers: PeerStats,
    latest: pd.Timestamp,
) -> dict | None:
    """Compute the three sub-scores + blended priority for one center x metric.

    Returns a dict of receipts, or None when nothing is computable at all.
    """
    g = df[df["location_id"] == lid].set_index("week_start").sort_index()
    s = g["value"].dropna()
    history_weeks = len(g)  # weeks the center reported at all (value may be missing)

    recent_cut = latest - pd.Timedelta(weeks=RECENT_WEEKS)
    base_cut = latest - pd.Timedelta(weeks=RECENT_WEEKS + BASELINE_WEEKS)
    spike_cut = latest - pd.Timedelta(weeks=BASELINE_WEEKS)

    recent = s[s.index > recent_cut]
    drift_base = s[(s.index > base_cut) & (s.index <= recent_cut)]
    spike_base = s[(s.index >= spike_cut) & (s.index < latest)]
    latest_val = float(s.loc[latest]) if latest in s.index else None

    notes: list[str] = []

    # ----- recent level (CSAT pools responses; may widen the window) --------
    csat_pool: dict | None = None
    csat_naive: dict | None = None
    if metric.key == "client_csat":
        win4 = g[g.index > recent_cut].reset_index()
        level, resp, weeks = csat_pooled_level(win4)
        pool_weeks = RECENT_WEEKS
        if resp < CSAT_MIN_POOLED_RESPONSES:
            # Before widening, record what the thin weekly numbers would have
            # said — so the suppression (if it changes the verdict) is visible.
            csat_naive = _naive_csat_scores(
                g, recent, drift_base, spike_base, latest_val, metric, tier, peers, latest
            )
            csat_naive["responses_4wk"] = resp
            win8 = g[g.index > latest - pd.Timedelta(weeks=CSAT_WIDEN_WEEKS)].reset_index()
            level8, resp8, weeks8 = csat_pooled_level(win8)
            if level8 is not None:
                notes.append(
                    f"CSAT level pooled over {CSAT_WIDEN_WEEKS} weeks ({resp8} responses) "
                    f"because the last {RECENT_WEEKS} weeks had only {resp}."
                )
                level, resp, weeks, pool_weeks = level8, resp8, weeks8, CSAT_WIDEN_WEEKS
        csat_pool = {"level": level, "pooled_responses": resp, "weeks": weeks, "window_weeks": pool_weeks}
        recent_level = level
        recent_weeks_used = weeks
    else:
        recent_level = float(recent.mean()) if len(recent) else None
        recent_weeks_used = len(recent)

    # ----- 1. drift risk: last 4wk mean vs prior 12wk mean ------------------
    drift = None
    if (
        recent_level is not None
        and recent_weeks_used >= MIN_RECENT_WEEKS_DRIFT
        and len(drift_base) >= MIN_BASELINE_WEEKS
    ):
        if metric.key == "client_csat":
            base_win = g[(g.index > base_cut) & (g.index <= recent_cut)].reset_index()
            base_level, _, _ = csat_pooled_level(base_win)
        else:
            base_level = float(drift_base.mean())
        if base_level is not None:
            scale = robust_scale(drift_base, metric.kind)
            move = bad_move(base_level, recent_level, metric.bad_direction)
            drift = {
                "score": min(move / scale, SCORE_CAP),
                "recent_level": recent_level,
                "baseline_level": base_level,
                "change": recent_level - base_level,
                "scale": scale,
                "recent_weeks": recent_weeks_used,
                "baseline_weeks": len(drift_base),
            }

    # ----- 2. spike: latest week vs prior 12wk (robust z) -------------------
    spike = None
    spike_ok = latest_val is not None and len(spike_base) >= MIN_BASELINE_WEEKS
    if spike_ok and metric.key == "client_csat":
        latest_resp = g.loc[latest, "csat_responses"] if latest in g.index else 0
        if pd.isna(latest_resp) or latest_resp < CSAT_SPIKE_MIN_WEEK_RESPONSES:
            spike_ok = False
            notes.append(
                f"Single-week CSAT spike not scored: the latest week has only "
                f"{0 if pd.isna(latest_resp) else int(latest_resp)} responses "
                f"(need {CSAT_SPIKE_MIN_WEEK_RESPONSES})."
            )
    if spike_ok:
        med = float(spike_base.median())
        scale = robust_scale(spike_base, metric.kind)
        move = bad_move(med, latest_val, metric.bad_direction)
        spike = {
            "score": min(move / scale, SCORE_CAP),
            "latest_value": latest_val,
            "baseline_median": med,
            "change": latest_val - med,
            "scale": scale,
            "baseline_weeks": len(spike_base),
        }

    # ----- 3. gap to standard: 4wk level vs corrected-tier peer median ------
    gap = None
    members = peers.tier_members.get(tier, [])
    peer_levels = peers.levels[peers.levels.index.isin(members)].dropna()
    if (
        recent_level is not None
        and recent_weeks_used >= MIN_RECENT_WEEKS_GAP
        and len(peer_levels) >= MIN_PEERS
    ):
        peer_median = float(peer_levels.median())
        spread = robust_scale(peer_levels, metric.kind)
        move = bad_move(peer_median, recent_level, metric.bad_direction)
        if metric.bad_direction == "down":
            worse = int((peer_levels < recent_level).sum())
        else:
            worse = int((peer_levels > recent_level).sum())
        gap = {
            "score": min(move / spread, SCORE_CAP),
            "center_level": recent_level,
            "peer_median": peer_median,
            "gap": recent_level - peer_median,
            "spread": spread,
            "peer_group": TIER_DISPLAY[tier],
            "peer_count": len(peer_levels),
            "rank_from_worst": worse + 1,  # 1 = worst in the peer group
        }

    subs = {"drift": drift, "gap": gap, "spike": spike}
    available = {k: v for k, v in subs.items() if v is not None}
    if not available:
        return None

    weight_sum = sum(WEIGHTS[k] for k in available)
    priority = sum(WEIGHTS[k] * v["score"] for k, v in available.items()) / weight_sum
    contributions = {k: WEIGHTS[k] * v["score"] / weight_sum for k, v in available.items()}

    return {
        "location_id": lid,
        "metric": metric.key,
        "priority": priority,
        "scores": subs,
        "contributions": contributions,
        "weights_used": {k: WEIGHTS[k] / weight_sum for k in available},
        "recent_level": recent_level,
        "latest_value": latest_val,
        "history_weeks": history_weeks,
        "csat_pool": csat_pool,
        "csat_naive": csat_naive,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Suppression rules — each firing is recorded with a plain-English reason
# ---------------------------------------------------------------------------


def apply_suppression(
    candidates: list[dict],
    cw: pd.DataFrame,
    loc: pd.DataFrame,
    latest: pd.Timestamp,
) -> tuple[list[dict], list[dict]]:
    """Split listed candidates (priority >= LIST_FLOOR) into ranked + suppressed."""
    names = loc.set_index("location_id")["location_name"]

    recent = cw[(cw["week_start"] > latest - pd.Timedelta(weeks=RECENT_WEEKS)) & (cw["week_start"] <= latest)]
    appts_4wk = recent.groupby("location_id")["appts_completed"].sum()
    weeks_history = cw[cw["week_start"] <= latest].groupby("location_id")["week_start"].nunique()

    ranked: list[dict] = []
    suppressed: list[dict] = []

    def suppress(c: dict, rule: str, reason: str) -> None:
        suppressed.append({**c, "suppression_rule": rule, "suppression_reason": reason})

    for c in sorted(candidates, key=lambda x: (-x["priority"], x["location_id"], x["metric"])):
        lid = c["location_id"]
        name = names[lid]
        metric = config.METRICS[c["metric"]]

        # Rule 1 — new-center partial history
        hist = int(weeks_history.get(lid, 0))
        if hist < MIN_HISTORY_WEEKS:
            suppress(
                c,
                "partial_history",
                f"{name} has only {hist} weeks of operating history — fewer than the "
                f"{MIN_HISTORY_WEEKS} needed for a trustworthy trend baseline. "
                f"Re-check once it has a full baseline.",
            )
            continue

        # Rule 2a — small denominator: pooled CSAT responses
        if metric.key == "client_csat" and c["csat_pool"]["pooled_responses"] < CSAT_MIN_POOLED_RESPONSES:
            pool = c["csat_pool"]
            suppress(
                c,
                "csat_small_sample",
                f"Client satisfaction at {name} rests on too few surveys: "
                f"{pool['pooled_responses']} responses even pooled across "
                f"{pool['window_weeks']} weeks (need {CSAT_MIN_POOLED_RESPONSES}). "
                f"Pooled level {fmt(metric, pool['level'])} — too thin to act on.",
            )
            continue

        # Rule 2b — small denominator: appointment-based rates
        if metric.key in APPOINTMENT_BASED:
            appts = int(appts_4wk.get(lid, 0))
            if appts < MIN_APPTS_4WK:
                suppress(
                    c,
                    "min_appointments",
                    f"{name} completed only {appts} appointments across the last "
                    f"{RECENT_WEEKS} weeks (need {MIN_APPTS_4WK}); rates this thin "
                    f"move on their own.",
                )
                continue

        # Rule 3 — score cutoff
        if c["priority"] < RANK_CUTOFF:
            suppress(
                c,
                "below_cutoff",
                f"Blended priority {c['priority']:.2f} is below the ranking cutoff "
                f"{RANK_CUTOFF:.1f} — worth a glance, not a Monday action.",
            )
            continue

        ranked.append(c)

    # Rule 4 — per-center cap (max PER_CENTER_CAP ranked signals per center)
    kept: list[dict] = []
    by_center: dict[str, list[dict]] = {}
    for c in ranked:  # already sorted by priority desc
        held = by_center.setdefault(c["location_id"], [])
        if len(held) < PER_CENTER_CAP:
            held.append(c)
            kept.append(c)
        else:
            winners = " and ".join(config.METRICS[w["metric"]].display_name for w in held)
            suppress(
                c,
                "per_center_cap",
                f"Held back by the per-center cap (max {PER_CENTER_CAP} signals per "
                f"center): outranked at {names[c['location_id']]} by {winners}.",
            )

    suppressed.sort(key=lambda x: -x["priority"])
    return kept, suppressed


# ---------------------------------------------------------------------------
# Plain-English headline (deterministic; the LLM narrative comes later)
# ---------------------------------------------------------------------------


def headline(c: dict, name: str) -> str:
    metric = config.METRICS[c["metric"]]
    worse_word = "below" if metric.bad_direction == "down" else "above"
    # recent_level is the MEAN of the recent weekly values, and the sentence has
    # to say so. A count reads as a total otherwise: "5.5 over the last 4 weeks"
    # sounds like 5.5 call-outs in the month, when it is 5.5 every week.
    per_week = " a week" if metric.kind == "count" else ""
    parts: list[str] = []

    drift, gap, spike = c["scores"]["drift"], c["scores"]["gap"], c["scores"]["spike"]
    if drift is not None and drift["score"] > 0:
        parts.append(
            f"{metric.display_name} at {name}: averaging {fmt(metric, drift['recent_level'])}"
            f"{per_week} over the last {RECENT_WEEKS} weeks vs a {BASELINE_WEEKS}-week norm of "
            f"{fmt(metric, drift['baseline_level'])} — {drift['score']:.1f}x the usual weekly "
            f"wobble in the wrong direction."
        )
    else:
        parts.append(
            f"{metric.display_name} at {name}: averaging {fmt(metric, c['recent_level'])}"
            f"{per_week} over the last {RECENT_WEEKS} weeks."
        )
    if gap is not None and gap["score"] > 0:
        group_short = gap["peer_group"].split(" (")[0]
        rank_note = (
            f" — the worst of {gap['peer_count']} {group_short}"
            if gap["rank_from_worst"] == 1
            else f" (ranks {gap['rank_from_worst']} from the bottom of {gap['peer_count']})"
        )
        parts.append(
            f"That is {fmt(metric, abs(gap['gap']))} {worse_word} the median of its peer group, "
            f"{gap['peer_group']}, at {fmt(metric, gap['peer_median'])}{rank_note}."
        )
    if spike is not None and spike["score"] >= 2.0:
        parts.append(
            f"The latest week alone ({fmt(metric, spike['latest_value'])}) sits "
            f"{spike['score']:.1f}x the usual wobble {worse_word} its recent normal."
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def _sub_scores_json(c: dict) -> dict:
    out = {}
    for k, v in c["scores"].items():
        if v is None:
            out[k] = {"available": False, "score": None}
        else:
            out[k] = {"available": True, **{kk: _r(vv) if isinstance(vv, float) else vv for kk, vv in v.items()}}
            out[k]["score"] = _r(v["score"], 2)
    return out


def signal_json(c: dict, rank: int, names: pd.Series, tiers: pd.Series) -> dict:
    metric = config.METRICS[c["metric"]]
    lid = c["location_id"]
    gap = c["scores"]["gap"]
    entry = {
        "rank": rank,
        "center": str(names[lid]),
        "location_id": lid,  # audit trail only — the digest UI never shows this
        "metric": metric.key,
        "metric_display": metric.display_name,
        "bad_direction": metric.bad_direction,
        "priority": _r(c["priority"], 2),
        "headline": headline(c, names[lid]),
        "recent_level": _r(c["recent_level"]),
        "recent_level_display": fmt(metric, c["recent_level"]),
        "latest_week_value": _r(c["latest_value"]),
        "scores": _sub_scores_json(c),
        "weights_used": {k: _r(v, 3) for k, v in c["weights_used"].items()},
        "contributions": {k: _r(v, 2) for k, v in c["contributions"].items()},
        "peer_context": (
            {
                "peer_group": gap["peer_group"],
                "peer_median": _r(gap["peer_median"]),
                "peer_median_display": fmt(metric, gap["peer_median"]),
                "peer_count": gap["peer_count"],
                "rank_from_worst": gap["rank_from_worst"],
            }
            if gap is not None
            else {"peer_group": TIER_DISPLAY[str(tiers[lid])], "note": "gap not computable"}
        ),
        "notes": c["notes"],
    }
    if metric.post_definition_change_only:
        entry["notes"] = entry["notes"] + [
            f"Uses only weeks on/after {config.MEMBERSHIP_DEFINITION_CHANGE} "
            f"(metric definition changed that day)."
        ]
    return entry


def suppressed_json(c: dict, names: pd.Series) -> dict:
    metric = config.METRICS[c["metric"]]
    return {
        "center": str(names[c["location_id"]]),
        "location_id": c["location_id"],
        "metric": metric.key,
        "metric_display": metric.display_name,
        "would_be_priority": _r(c["priority"], 2),
        "rule": c["suppression_rule"],
        "reason": c["suppression_reason"],
        "scores": _sub_scores_json(c),
        "notes": c["notes"],
    }


def csat_pool_suppressed_json(c: dict, names: pd.Series) -> dict:
    """A CSAT signal the response-pooling rule talked down — listed, not vanished.

    The raw weekly numbers would have ranked; pooled over enough responses the
    story is calm. Both versions are shown so the suppression is auditable.
    """
    metric = config.METRICS[c["metric"]]
    naive, pool = c["csat_naive"], c["csat_pool"]
    name = str(names[c["location_id"]])
    reason = (
        f"Weekly CSAT at {name} looked alarming — latest week "
        f"{fmt(metric, naive['latest_week_value'])} on {naive['latest_week_responses']} "
        f"responses; the raw {RECENT_WEEKS}-week mean of "
        f"{fmt(metric, naive['level_4wk_unweighted'])} on {naive['responses_4wk']} responses "
        f"would have scored {naive['priority']:.2f}. Pooled over {pool['window_weeks']} weeks "
        f"it is {fmt(metric, pool['level'])} on {pool['pooled_responses']} responses and "
        f"scores {c['priority']:.2f} — too few surveys to call this real yet."
    )
    return {
        "center": name,
        "location_id": c["location_id"],
        "metric": metric.key,
        "metric_display": metric.display_name,
        "would_be_priority": naive["priority"],
        "rule": "csat_small_sample",
        "reason": reason,
        "scores": _sub_scores_json(c),
        "csat_naive": naive,
        "notes": c["notes"],
    }


def parameters_json() -> dict:
    return {
        "windows": {"recent_weeks": RECENT_WEEKS, "baseline_weeks": BASELINE_WEEKS},
        "weights": WEIGHTS,
        "score_cap": SCORE_CAP,
        "rank_cutoff": RANK_CUTOFF,
        "list_floor": LIST_FLOOR,
        "per_center_cap": PER_CENTER_CAP,
        "min_history_weeks": MIN_HISTORY_WEEKS,
        "min_baseline_weeks": MIN_BASELINE_WEEKS,
        "min_recent_weeks_drift": MIN_RECENT_WEEKS_DRIFT,
        "min_recent_weeks_gap": MIN_RECENT_WEEKS_GAP,
        "min_peers": MIN_PEERS,
        "min_appointments_4wk": MIN_APPTS_4WK,
        "csat_min_pooled_responses": CSAT_MIN_POOLED_RESPONSES,
        "csat_widen_weeks": CSAT_WIDEN_WEEKS,
        "csat_spike_min_week_responses": CSAT_SPIKE_MIN_WEEK_RESPONSES,
        "mad_to_sigma": MAD_TO_SIGMA,
        "scale_floors": SCALE_FLOORS,
        "reference": "docs/SCORING.md",
    }


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------


class RunLog:
    """DATA/OUTPUTS/<as-of>/signals_runlog.jsonl — one line per event.

    A None path means "compute, log nothing": the scoring tests run the real
    engine without leaving anything behind (same contract as harness.RunLog).
    """

    def __init__(self, path: Path | None):
        self._fh = None
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w")

    def write(self, event: str, payload: dict) -> None:
        if self._fh is None:
            return
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "step": "signals",
            "event": event,
            **payload,
        }
        self._fh.write(json.dumps(line) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(as_of: date, leader: str, write: bool = True) -> dict:
    """Score every center × metric for one leader and one Monday.

    `write=False` computes without touching DATA/OUTPUTS/ — how the scoring tests
    exercise the real engine on the real data without rewriting the artifacts a
    live run just produced (the same flag facts.build_facts and verdicts.run
    already take).
    """
    loc, cw = load_data()
    latest = latest_complete_week(as_of)
    names = loc.set_index("location_id")["location_name"]
    tiers = loc.set_index("location_id")["maturity_tier"]

    mine = loc[loc["rmp_name"] == leader]
    if mine.empty:
        raise SystemExit(f"No centers found for leader {leader!r} in locations.rmp_name.")

    candidates: list[dict] = []
    pool_suppressed: list[dict] = []  # CSAT pairs the pooling rule talked down
    evaluated = 0
    not_computable = 0
    below_floor = 0
    for metric in config.METRICS.values():
        df = metric_frame(cw, metric, latest)
        peers = peer_stats(df, metric, loc, latest)
        for lid in mine["location_id"]:
            evaluated += 1
            c = score_pair(df, metric, lid, str(tiers[lid]), peers, latest)
            if c is None:
                not_computable += 1
            elif c["priority"] < LIST_FLOOR:
                # If the *unpooled* weekly CSAT would have ranked, the pooling
                # rule just suppressed a would-be signal: list it, visibly.
                naive = c.get("csat_naive")
                if naive is not None and naive["priority"] is not None and naive["priority"] >= RANK_CUTOFF:
                    pool_suppressed.append(csat_pool_suppressed_json(c, names))
                else:
                    below_floor += 1
            else:
                candidates.append(c)

    ranked, suppressed = apply_suppression(candidates, cw, loc, latest)

    out_dir = config.OUTPUTS_DIR / str(as_of)
    log = RunLog(out_dir / "signals_runlog.jsonl" if write else None)
    log.write("parameters", {"as_of": str(as_of), "leader": leader, "parameters": parameters_json()})

    signals = [signal_json(c, i + 1, names, tiers) for i, c in enumerate(ranked)]
    suppressed_out = [suppressed_json(c, names) for c in suppressed] + pool_suppressed
    suppressed_out.sort(key=lambda s: -(s["would_be_priority"] or 0.0))

    for s in signals:
        log.write(
            "ranked",
            {
                "rank": s["rank"],
                "location_id": s["location_id"],
                "metric": s["metric"],
                "priority": s["priority"],
                "sub_scores": {k: v["score"] for k, v in s["scores"].items()},
            },
        )
    for s in suppressed_out:
        log.write(
            "suppressed",
            {
                "location_id": s["location_id"],
                "metric": s["metric"],
                "would_be_priority": s["would_be_priority"],
                "rule": s["rule"],
            },
        )

    counts = {
        "center_metric_pairs_evaluated": evaluated,
        "not_computable": not_computable,
        "below_list_floor": below_floor,
        "listed_candidates": len(candidates),
        "ranked": len(ranked),
        "suppressed": len(suppressed_out),
        "suppressed_by_rule": {
            rule: sum(1 for s in suppressed_out if s["rule"] == rule)
            for rule in sorted({s["rule"] for s in suppressed_out})
        },
    }
    log.write("summary", {"as_of": str(as_of), "counts": counts})
    log.close()

    manifest_path = config.DATA_DIR / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    doc = {
        "step": "Signal engine",
        "as_of": str(as_of),
        "latest_complete_week": str(latest.date()),
        "leader": leader,
        "centers": [
            {"center": str(r.location_name), "location_id": r.location_id, "peer_group": TIER_DISPLAY[str(r.maturity_tier)]}
            for r in mine.sort_values("location_id").itertuples()
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parameters": parameters_json(),
        "data_provenance": {
            "computed_from": "DATA/TRANSLATION/ (corrected copies; DATA/INPUTS/ untouched)",
            "manifest_created_at": manifest.get("created_at"),
            "corrections_accepted": [c["id"] for c in manifest.get("corrections_accepted", [])],
        },
        "signals": signals,
        "suppressed": suppressed_out,
        "counts": counts,
    }
    if write:
        (out_dir / "signals.json").write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.signals",
        description="Step 2 — signal engine (spike / drift risk / gap-to-standard).",
    )
    config.add_as_of_argument(parser)
    parser.add_argument(
        "--leader",
        default=DEFAULT_LEADER,
        help=f"Regional Medical Partner whose centers are scored. Default: {DEFAULT_LEADER}.",
    )
    args = parser.parse_args(argv)

    doc = run(args.as_of, args.leader)

    out_dir = config.OUTPUTS_DIR / str(args.as_of)
    print(f"Signal engine — as of {args.as_of} (latest complete week {doc['latest_complete_week']})")
    print(f"  Leader: {doc['leader']} ({len(doc['centers'])} centers)")
    print(f"  {doc['counts']['ranked']} signals ranked, {doc['counts']['suppressed']} suppressed (listed with reasons).")
    for s in doc["signals"]:
        subs = ", ".join(
            f"{k} {v['score']:.2f}" for k, v in s["scores"].items() if v["available"]
        )
        print(f"   {s['rank']}. {s['center']} — {s['metric_display']}: priority {s['priority']:.2f} ({subs})")
    for s in doc["suppressed"]:
        print(
            f"   suppressed [{s['rule']}] {s['center']} — {s['metric_display']} "
            f"(would-be {s['would_be_priority']:.2f})"
        )
    print(f"  Output: {out_dir / 'signals.json'}")


if __name__ == "__main__":
    main()
