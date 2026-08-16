"""Step 3c(b) — successor recommendations: a failed loop still ends in a move.

The ledger re-checks every open recommendation against the weeks that actually
followed. Two of those outcomes used to be dead ends:

  intervention_failed  a human attested the work was DONE and the number
                       still went the wrong way by its own check-by date —
                       so the approach itself is falsified, and the next move
                       must be a different mechanism
  not_executed         a human attested the work was NOT carried out — the
                       same ask is re-issued, with the escalation attached

Neither is an action. A leader with thirty minutes on a Monday cannot do
anything with "rethink"; the whole point of the digest is the next move. So
every failed or ignored re-check now generates a SUCCESSOR recommendation:

  who        the accountable partner, by name (escalated to both regional
             partners once the ladder says so)
  what       a DIFFERENT mechanism from the one that already failed — never a
             restatement of the same ask
  expected   which number should move, from where toward where
  check-by   a new date, so next Monday can call it worked or not worked

Grounded only in this run's own facts: the failed recommendation's text, what
the metric actually did week by week since, how many of the center's doctors
carry the slide (where provider data reaches the metric), and the rest of that
center's picture in this run — its signal rank, its other open recommendations,
and the verdicts on its improvement plans.

Generation is the same three-tier ladder as pipeline.verdicts and
pipeline.narrate — claude-cli > api > template — with the prompt loaded at
runtime from PROMPTS/next-move.md. Every sentence passes three checks before it
can reach a leader:

  number check    every figure must exist in the context that was pasted into
                  the prompt (harness.check_numbers_pool)
  language check  no internal IDs, no raw column names, never "audit"
  rule check      names the owner, carries the new check-by date, expects a
                  number to move, is not a restatement of what already failed,
                  escalates when the ladder requires it, and never lands on a
                  dead-end verb ("rethink", "reconsider", "look into")

A failed check regenerates (max harness.MAX_REGENERATIONS); a model that
answers but never verifiably falls back to the deterministic successor, which
is built from the same context values and passes by construction. That fallback
still names a concrete next action — the escalation ladder is code, not prose:

  first failure            same owner, a different named intervention
  ignored, or failed
  twice / escalated        both regional partners jointly, an explicit review
                           meeting with a named single owner coming out of it

An unreachable model is a transport failure: the ladder degrades one rung and
`decided_by` records what actually wrote the text, so no tier is credited for
work it never did.
"""

from __future__ import annotations

import json
import re
import time

from . import ask, config, harness, verdicts

PROMPT_FILE = config.PROMPTS_DIR / "next-move.md"
MODES = verdicts.MODES  # ("claude-cli", "api", "template")

# The loop states that owe the leader a next move. Note what is NOT here:
# `needs_attestation` and `awaiting_evidence`. If nobody has confirmed the work
# happened, inventing a different mechanism is the wrong answer — we do not yet
# know that the first one failed. That question goes back to the owner instead.
SUCCESSOR_STATES = ("intervention_failed", "not_executed")

# Only this state earns a DIFFERENT mechanism: a human attested the work was
# done, and the number still moved the wrong way (or not at all) by its own
# check-by date. That is the one case where the approach itself is falsified.
DIFFERENT_MECHANISM_STATE = "intervention_failed"

# A different mechanism per metric — deliberately NOT the phrasing
# pipeline.ledger.ACTION_HINTS used the first time. This is the deterministic
# tier's answer to "what would you try instead?", and it is what the rule check
# holds the LLM tiers to as well.
ALTERNATE_ACTIONS = {
    "appts_per_doctor_hour": (
        "rebuild the appointment template itself for the days that carry the most "
        "visits, and give same-day overflow its own block instead of letting it land "
        "wherever there is room"
    ),
    "recheck_compliance_pct": (
        "make the recheck a hand-off at discharge — the client leaves with the next "
        "visit already booked — instead of a reminder sent afterwards"
    ),
    "record_completion_24h_pct": (
        "protect charting time on the schedule rather than asking for it: block the "
        "end of each shift for records and review completion per doctor every week"
    ),
    "callback_compliance_pct": (
        "give the callback list a named owner each day with a same-day completion "
        "check, instead of leaving it to whoever is free"
    ),
    "client_csat": (
        "read the individual survey comments with the team and fix the single thing "
        "clients name most, instead of reviewing the score"
    ),
    "avg_wait_time_min": (
        "walk and time the check-in-to-exam-room hand-off for a full day, then change "
        "the step that costs the most minutes, instead of asking the team to move faster"
    ),
    "revenue_per_appt": (
        "review the estimates presented on recent visits case by case with the "
        "doctors, instead of a general reminder about visit mix"
    ),
    "membership_conversion_pct": (
        "give one person at check-out ownership of the membership conversation with a "
        "scripted offer, instead of asking everyone to remember it"
    ),
    "staff_call_outs": (
        "put a named backup on the schedule for every shift and hold a short "
        "return-to-work conversation after each call-out, instead of reviewing the "
        "call-out log after the fact"
    ),
    "no_show_rate": (
        "confirm in two steps — an automated message plus a live call to anyone still "
        "unconfirmed the day before — instead of a single reminder"
    ),
    "open_dvm_requisitions": (
        "give each open role a single named owner and a weekly candidate count, "
        "instead of reviewing the recruiting pipeline as a whole"
    ),
}

# Verbs that are not moves. Banned from the NEXT MOVE line in every tier —
# this is the exact failure mode the successor exists to end.
DEAD_END_RE = re.compile(
    r"\brethink\b|\breconsider\b|\bre-?evaluate\b|\bre-?assess\b|"
    r"\blook into\b|\bfigure out\b|\bexplore\b|\bdetermine whether\b",
    re.IGNORECASE,
)

# Internal identifiers that must never reach a leader (narrate's rule, plus
# plan ids — a successor talks about the work, not the row it lives in).
BANNED_ID_RE = re.compile(r"\b(?:PCC_|DVM_|AP_)\w*")


def _lower_first(text: str) -> str:
    return text[0].lower() + text[1:] if text else text


# ---------------------------------------------------------------------------
# The escalation ladder (deterministic — the LLM never decides this)
# ---------------------------------------------------------------------------


def escalation_required(loop_state: str, attempt: int, escalation_level: int) -> bool:
    """Does this successor go to both regional partners jointly?

    Yes when a human has attested the work was NOT carried out — that is an
    accountability problem, and a louder version of the same ask from the same
    person is not the answer. Also yes when the row has already escalated once,
    or when this is the third attempt at the same center-metric.

    Note what no longer escalates: a flat number. v1 escalated a named person to
    both regional partners because a metric sat still, which asserted they had
    ignored the ask. The data never supported that.
    """
    return loop_state == "not_executed" or escalation_level >= 1 or attempt >= 3


def escalation_instruction(context: dict) -> str:
    """The tier-specific paragraph pasted into the prompt."""
    if context["escalated"]:
        return (
            f"**This one escalates.** {context['what_was_tried']['attempts_so_far']} attempt(s) "
            f"have now failed or gone untouched at {context['center']}, so the next move is not "
            f"another version of the same ask from one person: it goes to {context['owner']} "
            f"({context['owner_role']}) and {context['escalation_partner']} "
            f"({context['escalation_partner_role']}) TOGETHER. Name both of them, ask for a "
            f"specific joint review of {_lower_first(context['metric_display'])} at "
            f"{context['center']} this week, and require one named owner and one named change "
            f"to come out of that meeting."
        )
    return (
        f"**This is the second attempt.** {context['owner']} ({context['owner_role']}) stays "
        f"accountable, but the intervention must change: the first one is in the context below "
        f"and it did not move the number. Use the center's own detail — the week-by-week values, "
        f"and which of its doctors carry the slide where that is known — to pick something "
        f"specific enough that a team could start it tomorrow."
    )


# ---------------------------------------------------------------------------
# Verification pool + the three checks
# ---------------------------------------------------------------------------


def _add_collection_sizes(obj, numbers: set[float]) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            _add_collection_sizes(value, numbers)
    elif isinstance(obj, (list, tuple)):
        numbers.add(float(len(obj)))
        for value in obj:
            _add_collection_sizes(value, numbers)


def build_pool(context: dict) -> tuple[set[float], set[str]]:
    """Every number and date the successor may cite — exactly the context that
    was pasted into the prompt, nothing else."""
    numbers, dates = harness.collect_pool(context)
    _add_collection_sizes(context, numbers)
    numbers.update({0.0, 100.0})
    dates.add(context["as_of"])
    dates.add(context["check_by"])
    return numbers, dates


def check_language(text: str) -> dict:
    """Leader-facing language rules, enforced on every tier's output rather than
    merely requested in the prompt."""
    problems: list[str] = []
    ids = sorted(set(BANNED_ID_RE.findall(text)))
    if ids:
        problems.append("internal IDs must never reach a leader: " + ", ".join(ids))
    lowered = text.lower()
    if "audit" in lowered:
        problems.append('the data-quality step is called "Data Validation & Check" — never "audit"')
    raw_ids = sorted(k for k in config.METRICS if k in lowered)
    if raw_ids:
        problems.append("raw metric ids must be written in plain English: " + ", ".join(raw_ids))
    return {"ok": not problems, "problems": problems}


def check_rules(context: dict, parts: dict) -> dict:
    """The deterministic bounds on a successor. A recommendation that fails any
    of these is not falsifiable, not different, or not an action."""
    failures: list[dict] = []

    def fail(rule: str, why: str) -> None:
        failures.append({"rule": rule, "why": why})

    move = parts["next_move"]
    expected = parts["expected"]
    whole = f"{move}\n{expected}\n{parts['why']}"

    if context["owner"] not in whole:
        fail(
            "names_the_owner",
            f"the recommendation must name {context['owner']} — who is accountable is half of a falsifiable ask",
        )
    if context["check_by"] not in whole:
        fail(
            "names_the_new_check_by",
            f"the new check-by date {context['check_by']} must appear, so next Monday can rule on it",
        )
    if not harness.NUM_RE.findall(harness.DATE_RE.sub(" ", expected)):
        fail(
            "expects_a_number_to_move",
            "the expected line must say which number should move and roughly how far",
        )
    tried = (context["what_was_tried"].get("action_phrase") or "").lower()
    if tried and tried in move.lower():
        fail(
            "different_from_what_failed",
            f'"{tried}" is what was already tried and did not work; the next move must be a different mechanism',
        )
    if DEAD_END_RE.search(move):
        fail(
            "no_dead_end_verbs",
            'the next move cannot be "rethink"/"reconsider"/"look into" — those are not actions a team can start',
        )
    if context["escalated"] and context["escalation_partner"] not in whole:
        fail(
            "escalates_when_required",
            f"this attempt escalates: {context['escalation_partner']} must be named alongside {context['owner']}",
        )

    return {"ok": not failures, "failures": failures}


def run_checks(context: dict, parts: dict, pool_numbers: set[float], pool_dates: set[str],
               mode: str, attempt: int, log: harness.RunLog | None) -> tuple[bool, dict, str]:
    """Number check + language check + rule check, all logged."""
    text = f"{parts['next_move']}\n{parts['expected']}\n{parts['why']}"
    number = harness.check_numbers_pool(text, pool_numbers, pool_dates)
    language = check_language(text)
    rules = check_rules(context, parts)

    if log is not None:
        common = {"rec_id": context["successor_rec_id"], "mode": mode, "attempt": attempt}
        log.write(
            "successor_number_check",
            {
                **common,
                "result": "pass" if number["ok"] else "fail",
                "tokens_checked": number["checked"],
                "unknown_tokens": number["unknown"],
            },
        )
        log.write(
            "successor_language_check",
            {**common, "result": "pass" if language["ok"] else "fail", "problems": language["problems"]},
        )
        log.write(
            "successor_rule_check",
            {**common, "result": "pass" if rules["ok"] else "fail", "failures": rules["failures"]},
        )

    problems: list[str] = []
    if not number["ok"]:
        problems.append(
            "these numbers do not exist anywhere in the context provided (hallucination): "
            + ", ".join(number["unknown"])
        )
    problems.extend(language["problems"])
    problems.extend(f"{f['rule']}: {f['why']}" for f in rules["failures"])
    ok = number["ok"] and language["ok"] and rules["ok"]
    return ok, {"number": number, "language": language, "rules": rules}, "; ".join(problems)


# ---------------------------------------------------------------------------
# LLM tiers — prompt from PROMPTS/next-move.md, through the checks
# ---------------------------------------------------------------------------


def load_prompt_template() -> str:
    if not PROMPT_FILE.exists():
        raise SystemExit(f"Prompt file missing: {PROMPT_FILE} (PROMPTS/ is the code path).")
    return PROMPT_FILE.read_text()


def render_prompt(template: str, context: dict, retry_note: str | None) -> str:
    note = ""
    if retry_note:
        note = (
            "\n## Fix required — your previous answer failed validation\n"
            f"{retry_note}\n"
            "Rewrite all three lines so every rule above holds.\n"
        )
    return (
        template.replace("{{AS_OF}}", context["as_of"])
        .replace("{{CENTER}}", context["center"])
        .replace("{{METRIC}}", context["metric_display"])
        .replace("{{OWNER}}", context["owner"])
        .replace("{{OWNER_ROLE}}", context["owner_role"])
        .replace("{{CHECK_BY}}", context["check_by"])
        .replace("{{ESCALATION_INSTRUCTION}}", escalation_instruction(context))
        .replace("{{CONTEXT_JSON}}", json.dumps(context, ensure_ascii=False, indent=2))
        .replace("{{RETRY_NOTE}}", note)
    )


def parse_llm_successor(text: str) -> dict:
    """Extract the NEXT MOVE / EXPECTED / WHY lines; ValueError when malformed."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip().lstrip("*-#• ").strip()
        for key in ("NEXT MOVE", "EXPECTED", "WHY"):
            prefix = key + ":"
            if s.upper().startswith(prefix) and key not in fields:
                fields[key] = s[len(prefix):].strip().strip("*").strip()
    missing = [k for k in ("NEXT MOVE", "EXPECTED", "WHY") if not fields.get(k)]
    if missing:
        raise ValueError(f"answer missing line(s): {', '.join(missing)}")
    return {
        "next_move": fields["NEXT MOVE"],
        "expected": fields["EXPECTED"],
        "why": fields["WHY"],
    }


def call_llm(mode: str, prompt: str) -> str:
    """One generation call through the shared failure classifier. The prompt
    carries a whole context block, so claude-cli reads it from stdin."""
    if mode == "claude-cli":
        return verdicts.call_guarded(mode, ask.call_claude_cli, prompt)
    if mode == "api":
        return verdicts.call_guarded(mode, verdicts.call_api, prompt)
    if mode == "openai":
        return verdicts.call_guarded(mode, verdicts.call_openai, prompt)
    raise ValueError(f"call_llm has no transport for mode {mode!r}.")


# ---------------------------------------------------------------------------
# Template tier — the deterministic escalation ladder, in words
# ---------------------------------------------------------------------------


def _grounding_sentence(context: dict) -> str:
    """The most specific true thing this run knows about the center, for the
    WHY line: who carries the slide, what the weeks since actually read, and
    the level the successor is aiming back at."""
    parts: list[str] = []
    grounding = context.get("provider_concentration") or {}
    if grounding.get("sentence"):
        parts.append(grounding["sentence"][0].upper() + grounding["sentence"][1:] + ".")

    weekly = [
        str(w["display"])
        for w in (context["what_happened"].get("weekly_values_since") or [])
        if w.get("display") and w["display"] != "n/a"
    ]
    if len(weekly) > 1:
        parts.append(f"The weeks since read {', '.join(weekly)}.")

    ref = context.get("reference") or {}
    if ref.get("level_display"):
        parts.append(f"The number to get back to is {ref['level_display']} — {ref['label']}.")
    if not parts:
        parts.append(
            f"{context['metric_display']} at {context['center']} has not come back to where it "
            f"was when the first recommendation was written."
        )
    return " ".join(parts)


def generate_template(context: dict) -> dict:
    """A successor built only from context values — so it passes every check by
    construction, and still names a concrete next action either way."""
    happened = context["what_happened"]
    tried = context["what_was_tried"]
    metric_l = _lower_first(context["metric_display"])
    alternate = ALTERNATE_ACTIONS.get(
        context["metric"],
        f"change how {metric_l} is worked day to day at {context['center']} and name who owns the change",
    )
    ref = context.get("reference") or {}
    toward = (
        f" toward {ref['level_display']} ({ref['label']})"
        if ref.get("level_display")
        else ""
    )
    direction = "back down" if context["expected_direction"] == "down" else "back up"
    weeks = happened["followup_weeks"]
    weeks_word = f"{weeks} week" + ("s" if weeks != 1 else "")

    if context["escalated"]:
        next_move = (
            f"{context['owner']} ({context['owner_role']}) and {context['escalation_partner']} "
            f"({context['escalation_partner_role']}) to review {metric_l} at {context['center']} "
            f"together this week and leave that meeting with one named owner for the change — "
            f"then {alternate}."
        )
        expected = (
            f"Expected: {metric_l} moves {direction} from "
            f"{happened['followup_level_display']}{toward} by {context['check_by']}; if it has "
            f"not moved by then, {context['center']} goes on the regional review agenda with "
            f"these numbers attached."
        )
        why = (
            f"What was tried — {tried['action_phrase']} — has been on {context['owner']}'s desk "
            f"since {tried['created_week']} and {metric_l} went from "
            f"{happened['anchor_level_display']} to {happened['followup_level_display']} over the "
            f"{weeks_word} since, so this stops being one partner's fix. {_grounding_sentence(context)}"
        )
    else:
        next_move = (
            f"{context['owner']} ({context['owner_role']}) to try a different fix at "
            f"{context['center']}: {alternate}."
        )
        expected = (
            f"Expected: {metric_l} moves {direction} from "
            f"{happened['followup_level_display']}{toward} by {context['check_by']}, measured on "
            f"the weeks between now and then."
        )
        why = (
            f"What was tried — {tried['action_phrase']} — has been in place since "
            f"{tried['created_week']} and {metric_l} went from {happened['anchor_level_display']} "
            f"to {happened['followup_level_display']} over the {weeks_word} since, so the ask "
            f"itself has to change rather than be repeated. {_grounding_sentence(context)}"
        )

    return {"next_move": next_move, "expected": expected, "why": why}


# ---------------------------------------------------------------------------
# The generation loop — generate, check, retry, degrade, template-fallback
# ---------------------------------------------------------------------------


def generate(context: dict, mode: str | None = None, prompt_template: str | None = None,
             log: harness.RunLog | None = None) -> dict:
    """One successor recommendation: the LLM ladder through the harness, with
    the deterministic successor as the floor. Never returns without a concrete
    next action."""
    started = time.monotonic()
    mode = mode or verdicts.detect_mode()
    if mode not in MODES:
        raise SystemExit(f"Unknown llm mode {mode!r}; choose from {MODES}.")
    if mode != "template" and prompt_template is None:
        prompt_template = load_prompt_template()

    pool_numbers, pool_dates = build_pool(context)
    requested = mode
    fallbacks: list[dict] = []
    attempts = 0
    retry_note: str | None = None

    while True:
        if mode == "template":
            attempts += 1
            parts = generate_template(context)
            ok, checks, problem = run_checks(
                context, parts, pool_numbers, pool_dates, mode, attempts, log
            )
            if not ok:  # tripwire: the template is built from context values
                if log is not None:
                    log.write(
                        "hard_fail",
                        {"rec_id": context["successor_rec_id"], "mode": mode,
                         "attempts": attempts, "reason": problem},
                    )
                raise harness.HarnessError(
                    f"successor/{context['successor_rec_id']}: the deterministic successor failed "
                    f"its own checks ({problem}) — fix pipeline.successor."
                )
            return _finish(context, parts, requested, mode, checks, attempts, fallbacks, log, started)

        content_attempts = 0
        while content_attempts <= harness.MAX_REGENERATIONS:
            attempts += 1
            content_attempts += 1
            prompt = render_prompt(prompt_template, context, retry_note)
            if log is not None:
                log.write(
                    "llm_call",
                    {"rec_id": context["successor_rec_id"], "mode": mode, "attempt": attempts},
                )
            try:
                raw = call_llm(mode, prompt)
            except verdicts.LLMUnavailable as exc:
                # Transport failure — nothing was generated, nothing to check.
                reason = str(exc)
                limited = verdicts.is_usage_limit(reason)
                if log is not None:
                    log.write(
                        "generation_error",
                        {"rec_id": context["successor_rec_id"], "attempt": attempts, "mode": mode,
                         "kind": "usage_limit" if limited else "unavailable", "error": reason[:500]},
                    )
                nxt = verdicts.fallback_mode(mode) or "template"
                if log is not None:
                    log.write(
                        "mode_fallback",
                        {"rec_id": context["successor_rec_id"], "from_mode": mode, "to_mode": nxt,
                         "kind": "usage_limit" if limited else "unavailable", "reason": reason[:500]},
                    )
                fallbacks.append({"from": mode, "to": nxt, "reason": reason[:300]})
                mode = nxt
                retry_note = None
                break

            try:
                parts = parse_llm_successor(raw)
            except ValueError as exc:
                retry_note = f"Your previous reply could not be used: {exc}"
                if log is not None:
                    log.write(
                        "generation_error",
                        {"rec_id": context["successor_rec_id"], "attempt": attempts, "mode": mode,
                         "kind": "malformed", "error": str(exc)[:500]},
                    )
                    if content_attempts <= harness.MAX_REGENERATIONS:
                        log.write(
                            "retry",
                            {"rec_id": context["successor_rec_id"], "attempt": attempts,
                             "reason": "malformed_answer"},
                        )
                continue

            ok, checks, problem = run_checks(
                context, parts, pool_numbers, pool_dates, mode, attempts, log
            )
            if ok:
                return _finish(context, parts, requested, mode, checks, attempts, fallbacks, log, started)
            retry_note = problem
            if log is not None and content_attempts <= harness.MAX_REGENERATIONS:
                log.write(
                    "retry",
                    {"rec_id": context["successor_rec_id"], "attempt": attempts, "reason": problem[:500]},
                )
        else:
            # The model answered, but never verifiably. The deterministic
            # successor takes over — a next move is never skipped.
            reason = f"no verifiable successor after {content_attempts} attempts ({retry_note})"
            if log is not None:
                log.write(
                    "template_fallback",
                    {"rec_id": context["successor_rec_id"], "from_mode": mode,
                     "attempts": attempts, "reason": reason[:500]},
                )
            fallbacks.append({"from": mode, "to": "template", "reason": reason[:300]})
            mode = "template"
            retry_note = None


def _finish(context: dict, parts: dict, requested: str, decided_by: str, checks: dict,
            attempts: int, fallbacks: list[dict], log: harness.RunLog | None,
            started: float) -> dict:
    if log is not None:
        log.write(
            "successor_generated",
            {
                "rec_id": context["successor_rec_id"],
                "supersedes": context["supersedes"],
                "mode": requested,
                "decided_by": decided_by,
                "escalated": context["escalated"],
                "attempt": context["attempt"],
                "attempts": attempts,
                "numbers_verified": checks["number"]["checked"],
                "fallbacks": len(fallbacks),
            },
        )
    return {
        "next_move": parts["next_move"],
        "expected": parts["expected"],
        "why": parts["why"],
        # The tracked one-line recommendation the ledger stores and re-checks.
        "recommendation": f"{parts['next_move']} {parts['expected']}",
        # `mode` is the tier this run was configured for; `decided_by` is what
        # actually wrote it — they differ only when a recorded fallback fired.
        "mode": requested,
        "decided_by": decided_by,
        "checks": {
            "number_check": "pass",
            "language_check": "pass",
            "rule_check": "pass",
            "numbers_verified": checks["number"]["checked"],
        },
        "attempts": attempts,
        "fallbacks": fallbacks,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
