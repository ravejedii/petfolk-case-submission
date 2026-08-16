"""Shared harness — nothing the AI writes reaches a leader unchecked.

Three jobs — the third layer of "Claimed vs. Verified":

  (a) Number check   — every numeric token in a generated sentence must match a
      value in that plan's fact row (formatting-tolerant: rounding to the
      displayed precision, percent signs, sign/magnitude). A number the fact
      table doesn't know is a hallucination: fail.
  (b) Reasoning check — a deterministic rule table bounds what the AI may
      conclude. Below baseline can never be "on track"; a target already met
      must be called EXCEEDED and come with a recommendation to close; a plan
      past due and untouched for 6+ weeks must be flagged ABANDONED. The LLM
      adds nuance; it never contradicts arithmetic.
  (c) Run log        — every check pass/fail/retry lands in
      DATA/OUTPUTS/<as-of>/runlog.jsonl with details.

A failed check triggers regeneration; after MAX_REGENERATIONS retries the run
fails hard and loudly — a digest with unverified sentences is worse than no
digest. Every generation mode (claude-cli / api / template) passes through
here identically.

This module is deliberately stdlib-only so any step can import it.
"""

from __future__ import annotations

import json
import threading
import re
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants the rule table is built on
# ---------------------------------------------------------------------------

MAX_REGENERATIONS = 2  # initial attempt + 2 retries, then hard fail

STALE_DAYS_ABANDONED = 42  # 6 weeks without a status update
GAP_EXCEEDED_PCT = 100.0  # >= 100% of the gap closed -> target met

BUCKETS = ("NOT WORKING", "ABANDONED", "EXCEEDED", "ON TRACK")

# Owner status -> the AI bucket that counts as telling the same story.
# on_track matches ON TRACK; complete matches EXCEEDED (owner said done, the
# numbers confirm the target is met); at_risk matches NOT WORKING (both sides
# say the plan is in trouble). not_started never matches a bucket.
AGREE_BUCKET = {"on_track": "ON TRACK", "complete": "EXCEEDED", "at_risk": "NOT WORKING"}

# Words that count as "recommends closing" for the EXCEEDED rule.
CLOSE_WORDS = ("close", "closing", "closed", "credit")

DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
NUM_RE = re.compile(r"[-+−]?\d+(?:,\d{3})*(?:\.\d+)?")

# Fact-row fields whose values a sentence may cite (plus the derived values
# built in allowed_numbers below).
NUMERIC_FACT_FIELDS = (
    "baseline",
    "target",
    "actual_4wk",
    "prior_4wk",
    "baseline_at_open",
    "change_from_baseline",
    "gap_closed_pct",
    "days_overdue",
    "staleness_days",
    "weeks_in_actual",
    "weeks_in_prior",
    "window_weeks",
)
# String fields scanned for embedded numbers/dates the sentence may echo
# (e.g. the "24" in "Medical records completed within 24 hours").
STRING_FACT_FIELDS = ("metric_display", "center", "notes")
DATE_FACT_FIELDS = ("opened_date", "due_date", "last_status_update")


class HarnessError(RuntimeError):
    """Raised when generated output cannot be validated after all retries."""


# ---------------------------------------------------------------------------
# (a) Number check
# ---------------------------------------------------------------------------


def _string_field_text(fact: dict) -> str:
    parts: list[str] = []
    for field in STRING_FACT_FIELDS:
        value = fact.get(field)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def allowed_numbers(fact: dict) -> set[float]:
    """Every number a sentence about this plan is allowed to contain.

    Fact-row values, the obvious derived differences (progress made, distance
    left, plan gap size), day counts re-expressed as weeks, the reporting
    window, and any number embedded in the fact row's own text fields.
    """
    values: set[float] = set()

    def add(v) -> None:
        if v is None or isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            values.add(float(v))

    for field in NUMERIC_FACT_FIELDS:
        add(fact.get(field))

    b, t = fact.get("baseline"), fact.get("target")
    a, p = fact.get("actual_4wk"), fact.get("prior_4wk")
    bo = fact.get("baseline_at_open")
    for x, y in ((t, a), (a, b), (t, b), (a, p), (bo, b)):
        if x is not None and y is not None:
            add(x - y)
            add(y - x)

    for field in ("days_overdue", "staleness_days"):
        days = fact.get(field)
        if days:
            add(days // 7)
            add(round(days / 7))

    values.update({0.0, 100.0})  # "no progress", "100% of the gap"
    if fact.get("metric_kind") == "csat":
        values.add(5.0)  # the CSAT scale ("4.70 out of 5")

    text = DATE_RE.sub(" ", _string_field_text(fact))
    for token in NUM_RE.findall(text):
        values.add(float(token.replace(",", "").replace("−", "-")))

    return values


def allowed_dates(fact: dict, extra_dates: tuple[str, ...] = ()) -> set[str]:
    dates = {str(fact[f]) for f in DATE_FACT_FIELDS if fact.get(f)}
    dates.update(str(d) for d in extra_dates)
    dates.update(DATE_RE.findall(_string_field_text(fact)))
    return dates


def _token_matches(token: str, allowed: set[float]) -> bool:
    """Formatting-tolerant match: the token may be any allowed value rounded
    to the precision it displays, with sign or magnitude ("fell 12.8 points"
    may cite a change of -12.8)."""
    t = float(token.replace(",", "").replace("−", "-"))
    decimals = len(token.split(".")[1]) if "." in token else 0
    tol = 0.5 * 10**-decimals + 1e-9
    for v in allowed:
        if abs(v - t) <= tol or abs(abs(v) - t) <= tol or abs(-v - t) <= tol:
            return True
    return False


def check_numbers(text: str, fact: dict, extra_dates: tuple[str, ...] = ()) -> dict:
    """Validate every numeric token in `text` against the plan's fact row.

    Dates are checked first (full YYYY-MM-DD tokens must match a fact-row
    date), then stripped so their digits aren't re-parsed as numbers.
    Returns {"ok": bool, "checked": n, "unknown": [offending tokens]}.
    """
    unknown: list[str] = []

    dates = DATE_RE.findall(text)
    permitted_dates = allowed_dates(fact, extra_dates)
    unknown.extend(d for d in dates if d not in permitted_dates)

    stripped = DATE_RE.sub(" ", text)
    tokens = NUM_RE.findall(stripped)
    allowed = allowed_numbers(fact)
    unknown.extend(tok for tok in tokens if not _token_matches(tok, allowed))

    return {"ok": not unknown, "checked": len(dates) + len(tokens), "unknown": unknown}


# ---------------------------------------------------------------------------
# (a2) Number check against an artifact pool (phase narratives, chat answers)
# ---------------------------------------------------------------------------
# The pooled variant of (a) — for text written about a whole run rather than
# one plan — lives at the bottom of this module (collect_pool /
# check_numbers_pool), since it needs _token_matches defined above.

# ---------------------------------------------------------------------------
# (b) Reasoning rule table
# ---------------------------------------------------------------------------


def _abandoned_required(fact: dict) -> bool:
    return (
        bool(fact.get("is_overdue"))
        and (fact.get("staleness_days") or 0) >= STALE_DAYS_ABANDONED
        and not bool(fact.get("target_met"))
    )


def deterministic_bucket(fact: dict) -> str:
    """The bucket the rule table itself would assign (first match wins).

    Template mode generates from this; LLM modes are checked against the same
    rules, so no mode can drift from the arithmetic.
    """
    if fact.get("target_met"):
        return "EXCEEDED"
    if _abandoned_required(fact):
        return "ABANDONED"
    if fact.get("below_baseline"):
        return "NOT WORKING"
    return "ON TRACK"


def check_reasoning(fact: dict, bucket: str, action_text: str) -> dict:
    """Deterministic bounds on the AI's conclusion. Returns
    {"ok": bool, "failures": [{"rule", "why"}, ...]}."""
    failures: list[dict] = []

    def fail(rule: str, why: str) -> None:
        failures.append({"rule": rule, "why": why})

    if bucket not in BUCKETS:
        fail("unknown_bucket", f"{bucket!r} is not one of {BUCKETS}.")
        return {"ok": False, "failures": failures}

    target_met = bool(fact.get("target_met"))
    abandoned = _abandoned_required(fact)

    if fact.get("below_baseline") and bucket == "ON TRACK":
        fail(
            "below_baseline_not_on_track",
            "The 4-week actual sits below the plan's baseline; the verdict cannot be ON TRACK.",
        )
    if target_met and bucket != "EXCEEDED":
        fail(
            "target_met_must_be_exceeded",
            f"gap_closed_pct >= {GAP_EXCEEDED_PCT:.0f} — the target is met; the verdict must be EXCEEDED.",
        )
    if target_met and bucket == "EXCEEDED" and not any(w in action_text.lower() for w in CLOSE_WORDS):
        fail(
            "exceeded_must_recommend_close",
            "The target is met; the recommended action must include closing the plan / crediting the team.",
        )
    if abandoned and bucket != "ABANDONED":
        fail(
            "overdue_stale_must_be_abandoned",
            f"Plan is past due with no status update for >= {STALE_DAYS_ABANDONED} days; the verdict must be ABANDONED.",
        )
    if bucket == "EXCEEDED" and not target_met:
        fail(
            "exceeded_requires_target_met",
            "EXCEEDED claimed but the fact table says the target is not met.",
        )
    if bucket == "ABANDONED" and not (
        bool(fact.get("is_overdue")) and (fact.get("staleness_days") or 0) >= STALE_DAYS_ABANDONED
    ):
        fail(
            "abandoned_requires_overdue_and_stale",
            "ABANDONED claimed but the plan is not both past due and 6+ weeks without an update.",
        )

    return {"ok": not failures, "failures": failures}


def agree_flag(reported_status: str, bucket: str) -> bool:
    """True when the AI's bucket tells the same story as the owner's status."""
    return AGREE_BUCKET.get(reported_status) == bucket


def verdict_display(bucket: str, agree: bool) -> str:
    return f"{bucket} (Agree)" if agree else bucket


# ---------------------------------------------------------------------------
# (c) Run log
# ---------------------------------------------------------------------------


class RunLog:
    """Append JSON lines to DATA/OUTPUTS/<as-of>/runlog.jsonl (one event per line).

    Pass path=None for a no-op log (used by tests that must not touch disk).
    Mode "w" starts the file fresh for the step that owns it; a later step
    sharing the same file can open with mode "a".
    """

    def __init__(self, path: Path | None, step: str, mode: str = "w"):
        self.step = step
        self._lock = threading.Lock()
        if path is None:
            self._fh = None
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open(mode)

    def write(self, event: str, payload: dict) -> None:
        if self._fh is None:
            return
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "step": self.step,
            "event": event,
            **payload,
        }
        # verdict generation runs plans concurrently; one line at a time
        with self._lock:
            self._fh.write(json.dumps(line) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


# ---------------------------------------------------------------------------
# One call that runs both checks and logs them
# ---------------------------------------------------------------------------


def validate_verdict(
    fact: dict,
    bucket: str,
    sentence: str,
    action: str,
    log: RunLog | None = None,
    plan_id: str | None = None,
    attempt: int = 1,
    extra_dates: tuple[str, ...] = (),
) -> tuple[bool, dict]:
    """Run the number check (sentence + action) and the reasoning check for
    one generated verdict; log both; return (ok, details)."""
    numbers = check_numbers(f"{sentence}\n{action}", fact, extra_dates)
    reasoning = check_reasoning(fact, bucket, action)

    if log is not None:
        log.write(
            "number_check",
            {
                "plan_id": plan_id,
                "attempt": attempt,
                "result": "pass" if numbers["ok"] else "fail",
                "tokens_checked": numbers["checked"],
                "unknown_tokens": numbers["unknown"],
            },
        )
        log.write(
            "reasoning_check",
            {
                "plan_id": plan_id,
                "attempt": attempt,
                "result": "pass" if reasoning["ok"] else "fail",
                "bucket": bucket,
                "failures": reasoning["failures"],
            },
        )

    ok = numbers["ok"] and reasoning["ok"]
    return ok, {"number_check": numbers, "reasoning_check": reasoning}


# ---------------------------------------------------------------------------
# Pool-based number check (shared by narrate/ask)
#
# check_numbers validates a sentence against ONE plan's fact row. Steps that
# write about a whole run (per-phase narratives, "Ask this Monday" answers)
# validate against a POOL: every number and date that exists anywhere in the
# run's own artifacts. Same tolerance rules, same contract — a number the
# artifacts don't know is a hallucination: fail.
# ---------------------------------------------------------------------------

# Identifier tokens whose digits are not "numbers" a sentence cites
# (plan/location/provider/recommendation IDs, e.g. AP_003, PCC_006,
# DVM_0056, REC-2026-04-27-mount-pleasant-staff_call_outs).
ID_TOKEN_RE = re.compile(r"\b(?:AP_|PCC_|DVM_|REC-)[A-Za-z0-9_\-]*")


def collect_pool(obj, numbers: set[float] | None = None, dates: set[str] | None = None) -> tuple[set[float], set[str]]:
    """Recursively harvest every numeric value, every number embedded in a
    string, and every YYYY-MM-DD date from a JSON-like structure. Returns
    (numbers, dates) — the pool check_numbers_pool validates against."""
    if numbers is None:
        numbers = set()
    if dates is None:
        dates = set()
    if obj is None or isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        numbers.add(float(obj))
    elif isinstance(obj, str):
        dates.update(DATE_RE.findall(obj))
        cleaned = DATE_RE.sub(" ", ID_TOKEN_RE.sub(" ", obj))
        for token in NUM_RE.findall(cleaned):
            numbers.add(float(token.replace(",", "").replace("−", "-")))
    elif isinstance(obj, dict):
        for value in obj.values():
            collect_pool(value, numbers, dates)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            collect_pool(value, numbers, dates)
    return numbers, dates


def check_numbers_pool(text: str, numbers: set[float], dates: set[str] | frozenset = frozenset()) -> dict:
    """Validate every numeric token in `text` against an explicit pool.

    Identifier tokens (AP_/PCC_/DVM_/REC-) are stripped first — their digits
    are names, not figures. Dates are checked as whole YYYY-MM-DD tokens, then
    stripped so their digits aren't re-parsed. Everything else must match a
    pool value (formatting-tolerant, sign/magnitude-tolerant — same rules as
    check_numbers). Returns {"ok": bool, "checked": n, "unknown": [tokens]}.
    """
    unknown: list[str] = []

    cleaned = ID_TOKEN_RE.sub(" ", text)
    found_dates = DATE_RE.findall(cleaned)
    unknown.extend(d for d in found_dates if d not in dates)

    stripped = DATE_RE.sub(" ", cleaned)
    tokens = NUM_RE.findall(stripped)
    unknown.extend(tok for tok in tokens if not _token_matches(tok, numbers))

    return {"ok": not unknown, "checked": len(found_dates) + len(tokens), "unknown": unknown}
