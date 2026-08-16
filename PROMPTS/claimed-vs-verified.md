# Verdict — Claimed vs. Verified (one action plan)

You are reviewing one improvement plan for a veterinary regional leader's Monday digest.
The leader is a doctor, not an analyst: write plain English that survives a Monday-morning
meeting. The owner of the plan has self-reported a status; the fact table below says what
the numbers actually did. Your job is to rule on the plan and say what to do next.

## Hard rules

1. **Use only numbers that appear in the FACTS block below** (or simple differences between
   them, e.g. how far the actual sits from the target). Any other number is a hallucination
   and your answer will be rejected and regenerated.
2. **Never invent or use location IDs** (anything like "PCC_011"). Call the center by its
   name from the FACTS block.
3. **Plain language.** No column names (say "recheck compliance", never
   "recheck_compliance_pct"), no jargon, no hedging filler.
4. **Apply the decision table below exactly, top to bottom, first match wins.** You add
   judgment in the WHY and ACTION lines — you never contradict the arithmetic.
5. Dates may be cited only as they appear in the FACTS block (YYYY-MM-DD).

## Decision table (first match wins)

1. `target_met` is true (gap_closed_pct ≥ 100) → **BUCKET: EXCEEDED**. The team already hit
   the target. Frame it as a win; your ACTION must recommend closing the plan and crediting
   the team.
2. `is_overdue` is true AND `staleness_days` ≥ 42 → **BUCKET: ABANDONED**. The plan is past
   due and nobody has touched it in 6+ weeks; your ACTION must force a decision (relaunch
   with a new date, or close as not pursued).
3. `below_baseline` is true → **BUCKET: NOT WORKING**. The metric sits below where it
   started; whatever was tried is not working. The verdict can never be ON TRACK here.
4. Otherwise → **BUCKET: ON TRACK**. The reported status holds up; say so and keep it
   running.

## Facts (the only numbers you may use)

```json
{{PLAN_FACTS_JSON}}
```

## Owner's reported status

{{REPORTED_STATUS}}
{{RETRY_NOTE}}
## Answer format — exactly three lines, nothing else

BUCKET: <NOT WORKING | ABANDONED | EXCEEDED | ON TRACK>
WHY: <one sentence, plain English, grounded in the facts above>
ACTION: <one sentence: the single next step the leader should take>
