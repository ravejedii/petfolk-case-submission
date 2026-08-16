# How signals are scored

This document is the single source of truth for the signal engine
(`pipeline/signals.py`). The code implements exactly what is written here; if the
two ever disagree, the code is wrong. Prompts never define scoring — they receive
these definitions as input.

Everything below is deterministic arithmetic on the corrected data in `DATA/TRANSLATION/`.
No LLM touches any number in this step.

---

## The three questions

For every center × metric, the engine asks **three separate questions** and scores
each one on its own. This is a deliberate design decision: one blended number lets
a calm week-to-week metric water down a slow slide. Kept separate, an issue can't
hide — and every ranked signal shows all three scores as its receipts.

| # | Question | Score | What it catches |
|---|----------|-------|-----------------|
| 1 | Has the last month quietly slid vs. the prior quarter? | **Drift risk** | The killer: slow slides no weekly threshold ever trips |
| 2 | Is this center simply running below its true peer group? | **Gap to standard** | Chronic underperformance that trends alone can't see |
| 3 | Did the latest week jump away from this center's own normal? | **Spike** | Sudden breaks: a call-out wave, a one-week collapse |

Only movement in the metric's **bad direction** scores at all (falling record
completion scores; rising record completion scores zero — and vice versa for wait
times, call-outs, no-shows, and open requisitions, where *up* is bad). The ~11
watched metrics and their bad directions live in the metric registry
(`pipeline/config.py`); membership conversion uses only weeks on/after
**2026-02-09**, the day its definition changed.

All three scores are expressed in the same unit: **multiples of typical wobble**
(a robust z-score). A score of 2 means "twice as far from normal as this thing
usually wanders" — the same sentence works for every metric, which is what makes
one blended ranking possible.

## The windows (tunable knobs)

- **Recent window: 4 weeks.** Long enough that a real change shows up as a level,
  short enough that Priya isn't reading about February. This is "the last month."
- **Baseline window: 12 weeks.** The prior quarter — long enough to be a stable
  norm, short enough to reflect the center as it currently operates.

Both are knobs, not laws. They are defended here and set once in
`pipeline/signals.py`; every run's output records the values used.

## The three formulas

Let `L` be the latest complete week before the digest Monday (for the 2026-05-04
digest, the week starting 2026-04-27).

**1. Drift risk** — *mean of the last 4 weeks* vs. *mean of the 12 weeks before
those 4* (the recent 4 are excluded from the baseline so a slide can't
contaminate its own norm):

```
drift = badness( baseline_mean − recent_mean ) / scale
scale = max( 1.4826 × MAD(baseline weekly values), floor )
```

`badness(x)` keeps only bad-direction movement (never negative). MAD — the median
absolute deviation, times 1.4826 to put it on standard-deviation footing — is the
center-metric's *own* volatility, so a small noisy center needs a bigger move to
score than a large steady one. Small noisy centers don't cry wolf.

**2. Gap to standard** — the center's 4-week level vs. the **median of its true
peer group**, scaled by how spread out that peer group is:

```
gap = badness( peer_median − center_4wk_level ) / spread
spread = max( 1.4826 × MAD(peer 4wk levels), floor )
```

The peer group is the center's **corrected** maturity tier — new, ramping, or
mature — using the labels fixed by Data Validation & Check, not the raw file
(seven raw labels contradict their own opened dates). For mature centers the
peer median *is* the network standard. This is the honest answer to Priya's real
question: "what exactly are you comparing this center against?"

**3. Spike** — the latest single week vs. the median of the prior 12 weeks, as a
robust z-score:

```
spike = badness( baseline_median − latest_week ) / scale     (direction-aware)
scale = max( 1.4826 × MAD(prior 12 weekly values), floor )
```

Median/MAD rather than mean/SD so that one earlier freak week can't inflate the
yardstick and hide the current one.

**Scale floors.** A perfectly flat series has MAD ≈ 0, which would turn a trivial
wobble into an infinite z. Each metric kind therefore has a minimum scale — the
smallest movement we're willing to call "one wobble":

| Kind | Floor | In plain terms |
|------|-------|----------------|
| percentage metrics | 0.5 pts | half a point of compliance is noise |
| CSAT | 0.10 | weekly CSAT on a few dozen surveys wanders ±0.1 on its own |
| throughput | 0.05 | appts per doctor-hour |
| wait time | 0.5 min | |
| counts (call-outs, open reqs) | 1.0 | you need to be a whole event above normal |

(Revenue per appointment shares the throughput kind in the registry; its weekly
volatility is dollars, always far above the floor, so the floor never binds.)

Each sub-score is capped at **10** so a single absurd z can't dominate everything.

**CSAT is survey data, so its levels are response-weighted**: the recent level,
the drift baseline level and the peer levels are pooled (sum of score ×
responses ÷ sum of responses) rather than averaged across weeks. The volatility
scale (the MAD) and the single-week spike still read the raw weekly values — see
the small-sample rule below, which is what keeps a thin week from scoring.

## The blend

```
priority = 0.40 × drift + 0.35 × gap + 0.25 × spike
```

Rationale for the weights (also tunable knobs):

- **Drift 0.40** — the brief names the slow multi-week slide as the thing current
  reporting misses; it is the failure mode this engine exists to catch, so it
  gets the largest single weight.
- **Gap 0.35** — a center persistently below its true peers is the second thing a
  regional leader is accountable for, and the one an internal-trend view alone
  would never surface.
- **Spike 0.25** — real, but one bad week is the thing existing weekly reports
  are already best at catching, and the most likely to be noise.

When a sub-score is not computable (see the minimums below, and the CSAT
response rules further down), the remaining weights are **renormalized** to sum
to 1, and the output marks the missing piece as unavailable rather than silently
scoring it zero. In the 2026-05-04 run this happens exactly once: Verdae's CSAT
has no single-week spike score (the latest week carried 7 survey responses,
below the 25 a weekly CSAT spike needs), so its priority is drift and gap only —
0.67 and 0.92 reweighted 0.533 / 0.467 → **0.79**.

**Minimums for each sub-score to be computable**

- Drift: ≥ 3 of the last 4 weeks non-missing, ≥ 8 non-missing weeks in the
  12-week baseline window.
- Spike: the latest week present, ≥ 8 non-missing weeks in its 12-week baseline.
- Gap: ≥ 2 of the last 4 weeks non-missing, ≥ 4 peers with a level.

## Suppression rules — held back, but never invisible

Ranking happens per leader (default: Dr. Priya Raghunathan's 11 centers). After
scoring, these rules run in order. **Every suppression is listed in the output
with its reason and its receipts** — a suppressed signal is visible, not
vanished.

1. **New-center partial history.** A center needs **16 weeks** of operating
   history (4 recent + 12 baseline) before its trends are trusted. Candidates
   from younger centers are suppressed with the week count shown.
2. **Small denominators.**
   - **CSAT needs ≥ 60 pooled survey responses** over the last 4 weeks. If the 4
     weeks fall short, the window widens to **8 weeks** (conservative on purpose:
     the widened window overlaps the baseline, pulling the level toward normal).
     If even 8 weeks pool under 60 responses, the signal is suppressed. A
     single-week CSAT spike additionally requires **≥ 25 responses in that week**
     — one bad afternoon of seven surveys is not a signal.
     When thin weekly numbers *would have ranked* (priority ≥ cutoff unpooled)
     but the pooled level tells a calmer story, that is listed as a suppression
     with **both** numbers shown — the scary number stays auditable.
   - **Appointment-based rates need ≥ 200 completed appointments** pooled over
     the last 4 weeks (applies to the compliance percentages, throughput, wait,
     revenue per appointment, membership, and no-show rate — not to raw counts
     or CSAT, which has its own rule).
3. **Score cutoff: priority ≥ 2.0 to rank.** Two typical wobbles of blended
   badness is where "worth a glance" becomes "worth a Monday action."
   Candidates scoring **1.0–2.0** are listed as suppressed ("below cutoff") so
   near-misses stay visible; below 1.0 is indistinguishable from noise and is
   only counted, not listed.
4. **Per-center cap: at most 2 ranked signals per center.** One struggling
   center must not flood the whole digest; a center's third-best issue waits its
   turn and is listed as suppressed, naming the two signals that outranked it.

## What the output contains

`DATA/OUTPUTS/<as-of>/signals.json` — for the leader's centers, using **real center
names** and plain-English metric names (location IDs appear only for the audit
trail):

- **Ranked signals**, each with: the blended priority, all three sub-scores with
  their raw ingredients (levels, baselines, scales, peer medians, peer counts,
  rank-from-worst) as receipts, the peer-group definition, and a deterministic
  plain-English headline.
- **The suppressed list**, each entry with its rule, its would-be priority, and a
  plain-English reason.
- **The parameters used** — every knob above, recorded per run — plus data
  provenance (which corrections built `DATA/TRANSLATION/`).

`DATA/OUTPUTS/<as-of>/signals_runlog.jsonl` logs one line per ranked and suppressed
signal plus a summary of everything evaluated.

## Worked example (as of 2026-05-04)

Morrisville, medical records completed within 24 hours: recent 4-week level
77.7% vs. a prior-12-week norm of 87.7% with a usual wobble of 4.8 pts →
**drift 2.08**. Level 77.7% vs. the mature-center (network standard) median of
90.0% with a peer spread of 3.9 pts → **gap 3.17** (worst of 27 mature centers).
Latest week 79.5% vs. a 12-week median of 83.8% → **spike 0.58** — no single
week ever tripped an alarm, which is exactly why the drift score exists.

Priority = 0.40 × 2.08 + 0.35 × 3.17 + 0.25 × 0.58 = **2.09** → ranked.
