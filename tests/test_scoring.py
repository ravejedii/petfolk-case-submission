"""Scoring validation tests — written from docs/SCORING.md, NOT from pipeline/signals.py.

Every expected number below is hand-derived from the formulas, windows, floors,
weights, and suppression rules as WRITTEN IN THE DOC (derivations in comments).
If a test fails, either the code or the doc is wrong — per SCORING.md's own
contract ("if the two ever disagree, the code is wrong"), do not weaken the test.

Doc knobs used throughout (SCORING.md):
  recent window 4 wks · baseline window 12 wks · robust scale = 1.4826 x MAD
  scale floors: percentage 0.5 / csat 0.10 / throughput 0.05 / wait 0.5 / count 1.0
  sub-score cap 10 · blend 0.40 drift + 0.35 gap + 0.25 spike · rank cutoff 2.0
  CSAT pool >= 60 responses over 4 wks, widen to 8 wks, else suppress
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from pipeline import config, signals

# ---------------------------------------------------------------------------
# Synthetic-world helpers (real DATA/TRANSLATION/ schemas; values invented per scenario)
# ---------------------------------------------------------------------------

AS_OF = date(2026, 5, 4)  # digest Monday
LATEST = pd.Timestamp("2026-04-27")  # last complete week (doc: "L")
N_WEEKS = 20  # >= 16 wks so the partial-history rule (doc rule 1) never fires
WEEKS = pd.date_range(end=LATEST, periods=N_WEEKS, freq="7D")  # all Mondays

CENTER = "TEST_00"  # the center under test
PEERS = [f"TEST_{i:02d}" for i in range(1, 9)]  # 8 peers (doc: gap needs >= 4)
ALL_IDS = [CENTER] + PEERS

# Steady defaults for every column the clinic_weekly schema carries. Volumes are
# comfortably above the doc's small-denominator lines (>= 200 completed appts
# pooled over 4 wks; >= 60 pooled CSAT responses) so no suppression fires unless
# a test constructs it on purpose.
CW_DEFAULTS = {
    "doctor_hours_scheduled": 200.0,
    "appts_completed": 500,
    "appts_no_show": 20,
    "appts_cancelled": 10,
    "appts_per_doctor_hour": 2.5,
    "recheck_compliance_pct": 80.0,
    "record_completion_24h_pct": 90.0,
    "callback_compliance_pct": 85.0,
    "client_csat": 4.5,
    "csat_responses": 40,
    "avg_wait_time_min": 12.0,
    "revenue_per_appt": 250.0,
    "membership_conversion_pct": 25.0,
    "staff_call_outs": 1,
    "open_dvm_requisitions": 0,
}


def make_locations() -> pd.DataFrame:
    """All nine synthetic centers: mature, long open, one leader."""
    return pd.DataFrame(
        {
            "location_id": ALL_IDS,
            "location_name": [f"Testville {i}" for i in range(len(ALL_IDS))],
            "metro_area": "Testopolis",
            "state": "NC",
            "region": "Carolinas",
            "rmp_name": "Dr. Test Leader",
            "rop_name": "Pat Ops",
            "opened_date": pd.Timestamp("2024-01-01"),
            "maturity_tier": "mature",
            "exam_rooms": 6,
            "doctor_fte_target": 5.0,
        }
    )


def make_cw(overrides: dict[str, dict[str, object]]) -> pd.DataFrame:
    """Full clinic_weekly frame: N_WEEKS rows per center, defaults + overrides.

    overrides: {location_id: {column: list-of-N_WEEKS | scalar}}
    """
    rows = []
    for lid in ALL_IDS:
        over = overrides.get(lid, {})
        for i, wk in enumerate(WEEKS):
            row = {"location_id": lid, "week_start": wk}
            for col, default in CW_DEFAULTS.items():
                v = over.get(col, default)
                row[col] = v[i] if isinstance(v, (list, tuple)) else v
            rows.append(row)
    return pd.DataFrame(rows)


def scores_for(cw: pd.DataFrame, metric_key: str, lid: str = CENTER):
    """Run the engine's own scoring path for one center x metric."""
    loc = make_locations()
    metric = config.METRICS[metric_key]
    df = signals.metric_frame(cw, metric, LATEST)
    peers = signals.peer_stats(df, metric, loc, LATEST)
    return signals.score_pair(df, metric, lid, "mature", peers, LATEST)


def sub(candidate: dict, name: str) -> float:
    return candidate["scores"][name]["score"]


# ---------------------------------------------------------------------------
# 1. A gradual multi-week slide trips DRIFT RISK but not SPIKE
# ---------------------------------------------------------------------------
# Record completion (percentage, down = bad) slides from ~90 into an ~81
# plateau — "the killer: slow slides no weekly threshold ever trips".
#
# Series (weeks oldest -> newest; first 4 are pad outside every window):
#   pad: 90 90 91 89
#   baseline 12 (drift window): 90 90 89 91 90 89 91 90 | 87 84 81 81.5
#   recent 4: 81 80.5 81.5 81
#
# Doc drift = badness(baseline_mean - recent_mean) / max(1.4826*MAD(baseline), 0.5)
#   baseline_mean = 1053.5/12 = 87.7917; recent_mean = 81.0
#   baseline median = 89.5, MAD = 1.0 -> scale 1.4826
#   drift = 6.7917 / 1.4826 = 4.581  -> well past the 2.0 "worth action" line
# Doc spike = badness(median(prior 12 wks) - latest) / max(1.4826*MAD, 0.5)
#   the 12 weeks before the latest include the slide + plateau, so the median
#   (85.5) is already pulled down and the MAD (4.5) inflated:
#   spike = (85.5 - 81) / (1.4826*4.5) = 0.67 -> nowhere near tripping.
# That asymmetry IS the doc's design: no single week ever looked alarming.
# ---------------------------------------------------------------------------


def test_gradual_slide_trips_drift_not_spike():
    series = (
        [90, 90, 91, 89]  # pad (outside 4+12 windows)
        + [90, 90, 89, 91, 90, 89, 91, 90]  # stable normal
        + [87, 84, 81, 81.5]  # the slide
        + [81, 80.5, 81.5, 81]  # the new plateau (recent 4)
    )
    cw = make_cw({CENTER: {"record_completion_24h_pct": series}})
    c = scores_for(cw, "record_completion_24h_pct")

    assert c is not None, "a real slide must produce a candidate"
    drift, spike = sub(c, "drift"), sub(c, "spike")
    assert drift >= 2.0, f"slide must trip drift risk (got {drift})"
    assert drift == pytest.approx(4.581, abs=0.02), (
        "drift must equal the doc formula: (87.7917-81)/1.4826"
    )
    assert spike < 1.0, f"a gradual slide must NOT trip spike (got {spike})"
    assert drift > 2 * spike, "drift, not spike, must carry this signal"


# ---------------------------------------------------------------------------
# 2. A one-week jump trips SPIKE but not DRIFT RISK
# ---------------------------------------------------------------------------
# Wait time (up = bad) alternates 10/12 for 19 weeks, then the latest week
# jumps to 20 — "a sudden break: a call-out wave, a one-week collapse".
#
#   spike: prior-12 median = 11, MAD = 1 -> scale 1.4826
#          spike = (20 - 11) / 1.4826 = 6.070  -> trips hard
#   drift: baseline (12 wks before recent 4) mean = 11; recent 4 = 10,12,10,20
#          mean 13 -> drift = 2 / 1.4826 = 1.349 -> below the 2.0 line
# (one bad week moves a 4-week mean by only a quarter of the jump).
# ---------------------------------------------------------------------------


def test_one_week_jump_trips_spike_not_drift():
    series = [10, 12] * 9 + [10, 20]  # 19 calm weeks, then the jump
    cw = make_cw({CENTER: {"avg_wait_time_min": series}})
    c = scores_for(cw, "avg_wait_time_min")

    assert c is not None
    drift, spike = sub(c, "drift"), sub(c, "spike")
    assert spike >= 2.0, f"one-week jump must trip spike (got {spike})"
    assert spike == pytest.approx(6.070, abs=0.05), (
        "spike must equal the doc formula: (20-11)/(1.4826*MAD 1)"
    )
    assert drift < 2.0, f"one bad week must NOT trip drift risk (got {drift})"
    assert drift == pytest.approx(1.349, abs=0.05), (
        "drift must equal the doc formula: (13-11)/1.4826"
    )


# ---------------------------------------------------------------------------
# 3. Flat-but-below-peers trips only GAP TO STANDARD
# ---------------------------------------------------------------------------
# The center runs a dead-flat 70% record completion (alternating 69.5/70.5,
# every window mean exactly 70) while its 8 mature peers sit at 88–92.
#
#   drift = badness(70 - 70) = 0        spike = badness(median - 70.5) = 0
#   gap: peer median ~90, peer MAD ~1 -> spread ~1.4826
#        gap = ~20 / ~1.4826 = ~13.5 -> capped at the doc's sub-score cap 10.
# "Chronic underperformance that trends alone can't see."
# ---------------------------------------------------------------------------


def test_flat_below_peers_trips_only_gap():
    flat = [69.5, 70.5] * (N_WEEKS // 2)  # ends on 70.5: above median, no spike
    peer_levels = [88, 89, 89.5, 90, 90.5, 91, 91.5, 92]
    overrides = {CENTER: {"record_completion_24h_pct": flat}}
    for lid, level in zip(PEERS, peer_levels):
        overrides[lid] = {"record_completion_24h_pct": float(level)}
    cw = make_cw(overrides)
    c = scores_for(cw, "record_completion_24h_pct")

    assert c is not None
    assert sub(c, "drift") == pytest.approx(0.0, abs=1e-9), "flat series: no drift"
    assert sub(c, "spike") == pytest.approx(0.0, abs=1e-9), "flat series: no spike"
    gap = sub(c, "gap")
    assert gap >= 2.0, f"chronic below-peers must trip gap (got {gap})"
    assert gap == pytest.approx(10.0), (
        "a ~13-sigma gap must land exactly on the doc's sub-score cap of 10"
    )


# ---------------------------------------------------------------------------
# 4. Small-denominator noise gets SUPPRESSED — visible, not vanished
# ---------------------------------------------------------------------------
# CSAT crashes 4.6 -> ~3.2 on 5 responses/week. Doc rule 2: CSAT needs >= 60
# pooled responses over 4 wks (here 20); widen to 8 wks (here 40); still under
# 60 -> the signal is suppressed, and every suppression is listed with a
# plain-English reason. "One bad afternoon of seven surveys is not a signal."
# ---------------------------------------------------------------------------


def test_small_denominator_csat_is_suppressed():
    crash = [4.55, 4.65] * 8 + [3.2, 3.1, 3.3, 3.2]  # scary numbers, tiny n
    cw = make_cw({CENTER: {"client_csat": crash, "csat_responses": 5}})
    loc = make_locations()
    metric = config.METRICS["client_csat"]
    df = signals.metric_frame(cw, metric, LATEST)
    peers = signals.peer_stats(df, metric, loc, LATEST)

    candidates = []
    for lid in ALL_IDS:
        c = signals.score_pair(df, metric, lid, "mature", peers, LATEST)
        if c is not None:
            candidates.append(c)

    ranked, suppressed = signals.apply_suppression(candidates, cw, loc, LATEST)

    assert not any(
        c["location_id"] == CENTER and c["metric"] == "client_csat" for c in ranked
    ), "a CSAT crash on 5 responses/week must never rank"

    entry = next(
        (
            s
            for s in suppressed
            if s["location_id"] == CENTER and s["metric"] == "client_csat"
        ),
        None,
    )
    assert entry is not None, (
        "the suppressed CSAT signal must be LISTED with a reason (doc: "
        "'a suppressed signal is visible, not vanished')"
    )
    assert entry["suppression_rule"] != "below_cutoff", (
        "suppression must be for the small sample, not the score"
    )
    assert "response" in entry["suppression_reason"].lower(), (
        "the plain-English reason must name the response count"
    )


# ---------------------------------------------------------------------------
# 5. Good-direction movement does NOT score
# ---------------------------------------------------------------------------
# "Only movement in the metric's bad direction scores at all." Both polarities:
#   (a) record completion (down = bad) IMPROVING 82 -> 94
#   (b) wait time (up = bad) FALLING 20 -> 12
# Every sub-score must be zero — improvement never earns a Monday alarm.
# ---------------------------------------------------------------------------


def assert_scores_nothing(c: dict | None, label: str) -> None:
    if c is None:
        return  # no candidate at all is an acceptable "does not score"
    for name in ("drift", "gap", "spike"):
        s = c["scores"].get(name, {}).get("score")
        assert s in (None, 0) or s == pytest.approx(0.0, abs=1e-9), (
            f"{label}: good-direction movement scored {name}={s}"
        )
    assert c["priority"] == pytest.approx(0.0, abs=1e-9), (
        f"{label}: good-direction movement produced priority {c['priority']}"
    )


def test_good_direction_movement_does_not_score():
    # (a) percentage metric rising (down is bad -> rising is good)
    improving = (
        [82, 82, 83, 81]
        + [82, 81, 83, 82, 81, 83, 82, 82]
        + [85, 88, 91, 92]
        + [93, 92.5, 93.5, 94]
    )
    overrides = {CENTER: {"record_completion_24h_pct": improving}}
    for lid in PEERS:  # peers sit at ~85 so the improver is ABOVE peer median
        overrides[lid] = {"record_completion_24h_pct": 85.0}
    cw = make_cw(overrides)
    assert_scores_nothing(
        scores_for(cw, "record_completion_24h_pct"), "record completion rising"
    )

    # (b) wait metric falling (up is bad -> falling is good)
    falling = (
        [20, 20, 21, 19]
        + [20, 19, 21, 20, 19, 21, 20, 20]
        + [18, 16, 14, 13]
        + [12, 12.5, 11.5, 12]
    )
    overrides = {CENTER: {"avg_wait_time_min": falling}}
    for lid in PEERS:  # peers wait ~15 so the improver is BELOW peer median
        overrides[lid] = {"avg_wait_time_min": 15.0}
    cw = make_cw(overrides)
    assert_scores_nothing(scores_for(cw, "avg_wait_time_min"), "wait time falling")


# ---------------------------------------------------------------------------
# 6. Real data — the doc's own worked example must come out of the engine
# ---------------------------------------------------------------------------
# SCORING.md's worked example (as of 2026-05-04): Morrisville record completion
# drift 2.08 / gap 3.17 / spike 0.58 -> priority 2.09, ranked. PCC_011 must
# surface among the top signals with drift as a tripped (>= 2) component —
# the known slow slide the drift score exists to catch.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_run() -> dict:
    assert config.DATA_FILES["clinic_weekly"].exists(), (
        "DATA/TRANSLATION/ missing — run validate.py --accept first"
    )
    # write=False: the suite exercises the real engine on the real data
    # without rewriting DATA/OUTPUTS/ — a live run's artifacts stay its own.
    return signals.run(AS_OF, "Dr. Priya Raghunathan", write=False)


def test_real_data_morrisville_drift_signal(real_run):
    ranked = real_run["signals"]
    assert ranked, "the 2026-05-04 run must rank at least one signal"

    sig = next(
        (
            s
            for s in ranked
            if s["location_id"] == "PCC_011"
            and s["metric"] == "record_completion_24h_pct"
        ),
        None,
    )
    assert sig is not None, (
        "PCC_011 record completion (the known slow slide) must be ranked"
    )
    assert sig["rank"] <= 3, "must be among the top signals"
    assert sig["center"] == "Morrisville", "leaders see real center names"

    drift = sig["scores"]["drift"]
    gap = sig["scores"]["gap"]
    spike = sig["scores"]["spike"]

    # Drift-driven: drift tripped; the spike alarm (existing weekly reporting)
    # never did — exactly the doc's argument for why the drift score exists.
    assert drift["score"] >= 2.0
    assert spike["score"] < 1.0

    # The doc's worked-example numbers, verbatim:
    assert sig["recent_level"] == pytest.approx(77.7, abs=0.05)
    assert drift["score"] == pytest.approx(2.08, abs=0.01)
    assert drift["baseline_level"] == pytest.approx(87.7, abs=0.1)
    assert drift["scale"] == pytest.approx(4.8, abs=0.1)
    assert gap["score"] == pytest.approx(3.17, abs=0.01)
    assert gap["peer_median"] == pytest.approx(90.1, abs=0.1)
    assert gap["peer_count"] == 27, "median of 27 mature centers"
    assert gap["rank_from_worst"] == 1, "worst of the mature centers"
    assert spike["score"] == pytest.approx(0.58, abs=0.01)
    assert spike["baseline_median"] == pytest.approx(83.8, abs=0.05)
    assert sig["latest_week_value"] == pytest.approx(79.5, abs=0.05)

    # The blend, exactly as documented (0.02 tolerance for JSON rounding):
    blend = 0.40 * drift["score"] + 0.35 * gap["score"] + 0.25 * spike["score"]
    assert sig["priority"] == pytest.approx(2.09, abs=0.01)
    assert sig["priority"] == pytest.approx(blend, abs=0.02)

    # No location IDs in leader-facing prose:
    assert "PCC_" not in sig["headline"]


def test_real_run_parameters_match_the_doc(real_run):
    """Every knob the doc defends must be recorded per run, at the doc's value."""
    p = real_run["parameters"]
    assert p["windows"] == {"recent_weeks": 4, "baseline_weeks": 12}
    assert p["weights"] == {"drift": 0.40, "gap": 0.35, "spike": 0.25}
    assert p["score_cap"] == 10.0
    assert p["rank_cutoff"] == 2.0
    assert p["list_floor"] == 1.0
    assert p["per_center_cap"] == 2
    assert p["min_history_weeks"] == 16
    assert p["csat_min_pooled_responses"] == 60
    assert p["csat_widen_weeks"] == 8
    assert p["csat_spike_min_week_responses"] == 25
    assert p["min_appointments_4wk"] == 200
    assert p["mad_to_sigma"] == 1.4826
    assert p["scale_floors"] == {
        "percentage": 0.5,
        "csat": 0.10,
        "throughput": 0.05,
        "wait": 0.5,
        "count": 1.0,
    }
    # Doc: sub-score minimums for computability
    assert p["min_baseline_weeks"] == 8
    assert p["min_recent_weeks_drift"] == 3
    assert p["min_recent_weeks_gap"] == 2
    assert p["min_peers"] == 4

    # Doc: every suppression is listed with its rule and its reason.
    for entry in real_run["suppressed"]:
        assert entry.get("rule") or entry.get("suppression_rule")
        assert entry.get("reason") or entry.get("suppression_reason")
