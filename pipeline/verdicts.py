"""Step 3b — "Claimed vs. Verified": an AI verdict on every plan, every run.

For each action plan owned by the digest's leader, three columns land in
DATA/OUTPUTS/<as-of>/verdicts.json:

  Reported Status         the owner's self-reported picture of the plan
  What the numbers say    the deterministic fact row (pipeline.facts)
  AI Verdict              bucket + one plain-English sentence + one
                          recommended action, validated by the harness

Verdict buckets: NOT WORKING · ABANDONED · EXCEEDED · ON TRACK. When the AI's
bucket tells the same story as the owner's status, the verdict reads "(Agree)".

Three generation modes, auto-detected in this order (overridable with
--llm-mode):

  claude-cli   the Claude Code CLI is on PATH → `claude -p ... --output-format
               text` (headless; runs on a Claude subscription, no API tokens)
  api          ANTHROPIC_API_KEY is set → the Anthropic SDK, model
               claude-opus-5
  openai       OPENAI_API_KEY is set → OpenAI's HTTP API (no SDK to install),
               model gpt-5.1 by default. A different vendor's model, so it is
               never labelled as Claude anywhere: verdicts, narratives and
               answers all carry the tier that actually wrote them.
  template     none of the above → deterministic sentences built from the fact
               row, so the pipeline runs end-to-end for anyone with no LLM

Every mode's output passes through the harness identically (number check +
reasoning rule table, logged to DATA/OUTPUTS/<as-of>/runlog.jsonl). A failed check
regenerates; after 2 retries the run fails hard and loudly. The prompt the
LLM modes execute is loaded at runtime from PROMPTS/claimed-vs-verified.md — the PROMPTS/
folder is the code path, not documentation.

Two failure kinds, deliberately handled differently:

  content     the model answered, but the answer is malformed or fails a
              harness check. The harness is doing its job: regenerate, and
              after MAX_REGENERATIONS attempts fail the run loudly. A digest
              with an unverified sentence is worse than no digest.
  transport   the model never answered at all — CLI missing, non-zero exit
              (a Claude usage limit looks like this), timeout, API error.
              Nothing was generated, so there is nothing to check. The tier
              ladder walks down one rung (claude-cli -> api -> template) and
              the run continues; the fallback is logged, counted, and named
              in verdicts.json so the digest can say which tier wrote what.
              An unreachable model must never cost a leader their Monday
              digest, and it must never be hidden either.

CLI:
    python -m pipeline.verdicts --as-of 2026-05-04
    python -m pipeline.verdicts --as-of 2026-05-04 --llm-mode template
    python -m pipeline.verdicts --as-of 2026-05-04 --llm-mode claude-cli --plans AP_020
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

# Pin the CLI tier to Opus so a run's verdicts are always written by the same
# model rather than whatever the session happens to default to. Override with
# PETFOLK_CLI_MODEL.
CLI_MODEL = os.environ.get("PETFOLK_CLI_MODEL", "opus")

from . import config, facts, harness

DEFAULT_LEADER = "Dr. Priya Raghunathan"
MODES = ("claude-cli", "api", "openai", "template")

PROMPT_FILE = config.PROMPTS_DIR / "claimed-vs-verified.md"
API_MODEL = "claude-opus-5"
API_MAX_TOKENS = 1024
CLI_TIMEOUT_SECONDS = 120

# The OpenAI rung: a different vendor's model, reached over plain HTTPS so
# nothing has to be pip-installed for it. It exists so a Claude subscription
# running out of quota mid-demo costs nobody their digest — and it is labelled
# as itself everywhere it writes, never as Claude.
OPENAI_MODEL = os.environ.get("PETFOLK_OPENAI_MODEL", "gpt-5.1")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MAX_TOKENS = 2048
OPENAI_TIMEOUT_SECONDS = 180


# ---------------------------------------------------------------------------
# Mode detection + the availability ladder
# ---------------------------------------------------------------------------


class LLMUnavailable(RuntimeError):
    """The model could not be reached: CLI missing, non-zero exit (a usage
    limit looks like this), timeout, or an API error. A transport failure, not
    a content failure — no answer arrived, so the harness has nothing to check
    and the ladder degrades a tier instead of retrying into the same wall."""


# Signs in an error message that the model is refusing on quota, not broken.
# Retrying the same tier cannot fix these, so the ladder degrades immediately.
# The Claude CLI's own wording is the first case: it prints
# "You've hit your session limit · resets 7:40pm (America/New_York)" and
# exits 1.
USAGE_LIMIT_RE = re.compile(
    r"(?:session|usage|rate|token|message)[ _]limit|limit\s*(?:reached|exceeded)|"
    r"resets?\s+(?:at\s+)?\d|rate_limit|too many requests|\b429\b|quota|"
    r"insufficient[_ ]quota|overloaded|out of (?:credits|tokens)",
    re.IGNORECASE,
)


def is_usage_limit(message: str) -> bool:
    return bool(USAGE_LIMIT_RE.search(message or ""))


def available_modes() -> tuple[str, ...]:
    """The tiers this environment can actually serve, best first. Template is
    always available — that is what makes the repo runnable for a reviewer
    with no Claude subscription and no API key."""
    modes = []
    if openai_key():
        modes.append("openai")
    if shutil.which("claude"):
        modes.append("claude-cli")
    if os.environ.get("ANTHROPIC_API_KEY"):
        modes.append("api")
    modes.append("template")
    return tuple(modes)


def detect_mode() -> str:
    """openai when a key is set, else claude-cli when the CLI is installed,
    else Anthropic api, else template. Every mode is validated identically."""
    return available_modes()[0]


def fallback_mode(mode: str) -> str | None:
    """The next tier below `mode` that this environment can serve, or None
    when `mode` is already the last rung."""
    usable = available_modes()
    below = MODES[MODES.index(mode) + 1:] if mode in MODES else ()
    for candidate in below:
        if candidate in usable:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Prompt handling (LLM modes)
# ---------------------------------------------------------------------------


def load_prompt_template() -> str:
    if not PROMPT_FILE.exists():
        raise SystemExit(f"Prompt file missing: {PROMPT_FILE} (PROMPTS/ is the code path).")
    return PROMPT_FILE.read_text()


def render_prompt(template: str, fact: dict, retry_note: str | None) -> str:
    fact_block = json.dumps({k: v for k, v in fact.items() if k != "summary"}, indent=2)
    reported = (
        f"{fact['reported_status_display']} (status \"{fact['reported_status']}\", "
        f"last updated {fact['last_status_update']}, {fact['staleness_days']} days ago)"
    )
    note = ""
    if retry_note:
        note = (
            "\n## Fix required — your previous answer failed validation\n"
            f"{retry_note}\n"
            "Rewrite your answer so it passes every rule above.\n"
        )
    return (
        template.replace("{{PLAN_FACTS_JSON}}", fact_block)
        .replace("{{REPORTED_STATUS}}", reported)
        .replace("{{RETRY_NOTE}}", note)
    )


def parse_llm_output(text: str) -> tuple[str, str, str]:
    """Extract BUCKET / WHY / ACTION lines; raise ValueError when malformed."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip().lstrip("*-#• ").strip()
        for key in ("BUCKET", "WHY", "ACTION"):
            prefix = key + ":"
            if s.upper().startswith(prefix) and key not in fields:
                fields[key] = s[len(prefix):].strip().strip("*").strip()
    missing = [k for k in ("BUCKET", "WHY", "ACTION") if not fields.get(k)]
    if missing:
        raise ValueError(f"LLM answer missing line(s): {', '.join(missing)}")

    bucket = fields["BUCKET"].upper().replace("-", " ").strip(" .")
    for suffix in ("(AGREE)", "(DISAGREE)"):
        if bucket.endswith(suffix):
            bucket = bucket[: -len(suffix)].strip()
    if bucket not in harness.BUCKETS:
        raise ValueError(f"Unknown bucket {fields['BUCKET']!r} (expected one of {harness.BUCKETS}).")
    return bucket, fields["WHY"], fields["ACTION"]


def cli_failure_message(returncode: int, stdout: str, stderr: str) -> str:
    """What actually went wrong, in the CLI's own words.

    Both streams matter and neither can be trusted alone: the Claude CLI reports
    a session limit on STDOUT while exiting 1, and it writes advisory warnings
    (e.g. about stdin) to STDERR. Reading stderr first therefore let a harmless
    warning mask the real cause — which is how a whole run of plans came back
    reporting a stdin warning instead of the failure that actually stopped it.
    Warnings are kept only when nothing else was said.
    """
    lines = [ln.strip() for ln in (stderr + "\n" + stdout).splitlines() if ln.strip()]
    real = [ln for ln in lines if not ln.lower().startswith("warning:")]
    said = " ".join(real or lines)
    return f"claude CLI exited {returncode}" + (f": {said[:500]}" if said else "")


def call_claude_cli(prompt: str) -> str:
    """Headless `claude -p <prompt>`.

    stdin is closed explicitly: when this pipeline is spawned by the Node API,
    the process inherits an open-but-empty stdin pipe, and the CLI then waits
    seconds for input that never comes before warning and failing. DEVNULL is
    an immediate EOF, so the CLI reads the prompt from argv and starts at once.
    """
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", CLI_MODEL, "--output-format", "text"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(cli_failure_message(proc.returncode, proc.stdout, proc.stderr))
    return proc.stdout


def call_api(prompt: str) -> str:
    import anthropic  # imported lazily so template/cli modes need no SDK

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=API_MODEL,
        max_tokens=API_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


# --- the OpenAI rung -------------------------------------------------------
#
# Cost is a design constraint here, not an afterthought: this exists for a
# demo running on a personal card. Three things keep a call to cents —
#   * `reasoning_effort: none` — no hidden reasoning pass. These prompts hand
#     the model the facts and ask it to read them; reasoning tokens are billed
#     like output and would buy nothing.
#   * the question sits AFTER the artifacts in PROMPTS/answer.md, so repeated
#     asks in one run reuse the cached prompt prefix.
#   * no SDK, no retries beyond the harness's own.


def _openai_body(prompt: str, stream: bool) -> dict:
    body = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": OPENAI_MAX_TOKENS,
    }
    if OPENAI_MODEL.startswith("gpt-5"):
        body["reasoning_effort"] = "none"
    if stream:
        body["stream"] = True
    return body


def openai_key() -> str | None:
    """This project's OpenAI key. PETFOLK_OPENAI_API_KEY wins over the generic
    OPENAI_API_KEY on purpose: a machine can already have OPENAI_API_KEY
    exported for something else entirely (this one had an NVIDIA `nvapi-…` key
    in it), and a stale ambient key must not quietly break the fallback tier."""
    return (
        os.environ.get("PETFOLK_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or None
    )


def _openai_open(body: dict):
    key = openai_key()
    if not key:
        raise RuntimeError("no OpenAI key is set (PETFOLK_OPENAI_API_KEY)")
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        return urllib.request.urlopen(request, timeout=OPENAI_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        # The API's own words, including quota wording that is_usage_limit
        # recognises — so a spent budget degrades a tier instead of retrying.
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"OpenAI API returned {exc.code}: {detail}") from exc


def call_openai(prompt: str) -> str:
    with _openai_open(_openai_body(prompt, stream=False)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    choices = payload.get("choices") or []
    text = (choices[0].get("message") or {}).get("content", "") if choices else ""
    if not text.strip():
        finish = choices[0].get("finish_reason") if choices else "no choices returned"
        raise RuntimeError(f"OpenAI returned no text (finish_reason: {finish})")
    return text


def call_openai_stream(prompt: str, on_delta) -> str:
    """The same call with `stream: true`, handing each chunk to `on_delta` as
    it arrives. The return value is the whole reply — the only thing the
    harness ever sees."""
    chunks: list[str] = []
    with _openai_open(_openai_body(prompt, stream=True)) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            for choice in event.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    chunks.append(text)
                    on_delta(text)
    reply = "".join(chunks)
    if not reply.strip():
        raise RuntimeError("OpenAI stream ended without any text")
    return reply


def call_guarded(mode: str, transport, prompt: str) -> str:
    """Run one generation call, normalizing every way of NOT getting an answer
    to LLMUnavailable. Callers can then treat "the model didn't answer"
    (degrade a tier) separately from "the answer was wrong" (harness retry,
    then hard fail). Steps with their own transport — pipeline.ask sends its
    much larger prompt on stdin — reuse this classifier rather than copy it."""
    try:
        return transport(prompt)
    except subprocess.TimeoutExpired as exc:
        raise LLMUnavailable(f"{mode} did not return within its timeout") from exc
    except FileNotFoundError as exc:
        raise LLMUnavailable("the claude CLI is not on PATH") from exc
    except ImportError as exc:
        raise LLMUnavailable(
            f"the anthropic SDK is not installed ({exc}); pip install anthropic"
        ) from exc
    except OSError as exc:
        raise LLMUnavailable(f"{mode} could not be started: {exc}") from exc
    except RuntimeError as exc:  # non-zero exit — usage limits land here
        raise LLMUnavailable(str(exc)) from exc
    except Exception as exc:  # SDK error types aren't imported at module level
        raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc


def call_llm(mode: str, prompt: str) -> str:
    if mode == "claude-cli":
        return call_guarded(mode, call_claude_cli, prompt)
    if mode == "api":
        return call_guarded(mode, call_api, prompt)
    if mode == "openai":
        return call_guarded(mode, call_openai, prompt)
    raise ValueError(f"call_llm has no transport for mode {mode!r}.")


# ---------------------------------------------------------------------------
# Template mode — deterministic sentences from the fact row
# ---------------------------------------------------------------------------


def _lower_first(text: str) -> str:
    return text[0].lower() + text[1:] if text else text


def generate_template(fact: dict) -> tuple[str, str, str]:
    """Bucket + sentence + action built only from fact-row values, using the
    same rule table the harness enforces — template output passes the checks
    by construction, and still goes through them like every other mode."""
    bucket = harness.deterministic_bucket(fact)
    center = fact["center"]
    metric_l = _lower_first(fact["metric_display"])
    w = fact["window_weeks"]
    a, b, t = fact["actual_4wk_display"], fact["baseline_display"], fact["target_display"]
    status_l = fact["reported_status_display"].lower()

    if bucket == "EXCEEDED":
        sentence = (
            f"{center} has already cleared this plan's target: {metric_l} averaged {a} "
            f"over the last {w} weeks against a target of {t} — "
            f"{fact['gap_closed_display']} of the gap from the {b} baseline is closed."
        )
        action = f"Close the plan and credit the {center} team; the {t} target is already met."
    elif bucket == "ABANDONED":
        sentence = (
            f"This plan came due on {fact['due_date']} ({fact['days_overdue']} days ago) and has "
            f"had no status update in {fact['staleness_days']} days, while {metric_l} at "
            f"{center} stands at {a} against a target of {t}."
        )
        action = (
            f"Decide this plan's fate today: relaunch it with a new due date, or close it as "
            f"not pursued — {fact['staleness_days']} days of silence past the due date is "
            f"abandonment in practice."
        )
    elif bucket == "NOT WORKING":
        worse = "below" if fact["direction_to_target"] == "up" else "above"
        sentence = (
            f"The owner reports this plan {status_l}, but {metric_l} at {center} averaged {a} "
            f"over the last {w} weeks — {worse} the {b} baseline, so none of the gap to the "
            f"{t} target has been closed."
        )
        action = (
            f"Rework the plan for {center}: whatever has been tried is not moving {metric_l}; "
            f"pick a different intervention and set a fresh check-by date this week."
        )
    else:  # ON TRACK
        better = "up" if fact["direction_to_target"] == "up" else "down"
        sentence = (
            f"{fact['metric_display']} at {center} averaged {a} over the last {w} weeks, "
            f"{better} from the {b} baseline with {fact['gap_closed_display']} of the gap to "
            f"the {t} target closed — the reported status holds up."
        )
        action = "Agree with the owner and keep the plan running; re-check in next Monday's digest."

    return bucket, sentence, action


# ---------------------------------------------------------------------------
# Judgment loop — generate, validate, retry, hard-fail
# ---------------------------------------------------------------------------


def _describe_failure(details: dict) -> str:
    parts = []
    numbers = details["number_check"]
    if not numbers["ok"]:
        parts.append(
            "these numbers are not in the fact row: " + ", ".join(numbers["unknown"])
        )
    for f in details["reasoning_check"]["failures"]:
        parts.append(f"{f['rule']}: {f['why']}")
    return "; ".join(parts)


def judge_plan(
    fact: dict,
    mode: str,
    prompt_template: str | None,
    log: harness.RunLog,
    extra_dates: tuple[str, ...],
) -> dict:
    """One plan's verdict: generate, validate, retry on content failures, walk
    down the tier ladder on transport failures. Raises HarnessError only when
    a model that DID answer could not produce a verifiable verdict, or when no
    tier is left to try."""
    plan_id = fact["plan_id"]
    retry_note: str | None = None
    fallbacks: list[dict] = []
    content_attempts = 0  # attempts where an answer existed to be checked
    transport_failures = 0  # consecutive no-answers in the current tier
    attempt = 0

    while True:
        attempt += 1
        if mode == "template":
            bucket, sentence, action = generate_template(fact)
        else:
            prompt = render_prompt(prompt_template, fact, retry_note)
            log.write("llm_call", {"plan_id": plan_id, "mode": mode, "attempt": attempt})
            try:
                raw = call_llm(mode, prompt)
                bucket, sentence, action = parse_llm_output(raw)
            except LLMUnavailable as exc:
                # The model never answered. One retry covers a blip; a usage
                # limit will not clear on a retry, so degrade immediately.
                transport_failures += 1
                reason = str(exc)
                limited = is_usage_limit(reason)
                log.write(
                    "generation_error",
                    {
                        "plan_id": plan_id,
                        "attempt": attempt,
                        "mode": mode,
                        "kind": "usage_limit" if limited else "unavailable",
                        "error": reason[:500],
                    },
                )
                if transport_failures < 2 and not limited:
                    log.write(
                        "retry",
                        {"plan_id": plan_id, "attempt": attempt, "reason": "llm_unavailable"},
                    )
                    continue
                nxt = fallback_mode(mode)
                if nxt is None:
                    log.write(
                        "hard_fail",
                        {"plan_id": plan_id, "mode": mode, "attempts": attempt, "reason": reason[:500]},
                    )
                    raise harness.HarnessError(
                        f"{plan_id}: no verdict could be generated — {mode} is unavailable "
                        f"({reason}) and there is no tier left below it."
                    ) from exc
                log.write(
                    "mode_fallback",
                    {
                        "plan_id": plan_id,
                        "from_mode": mode,
                        "to_mode": nxt,
                        "kind": "usage_limit" if limited else "unavailable",
                        "reason": reason[:500],
                    },
                )
                fallbacks.append({"from": mode, "to": nxt, "reason": reason[:300]})
                mode = nxt
                transport_failures = 0
                retry_note = None
                continue
            except ValueError as exc:
                # The model answered in the wrong shape — a content failure,
                # bounded by the same budget as a failed harness check.
                content_attempts += 1
                retry_note = f"Your answer could not be used: {exc}"
                log.write(
                    "generation_error",
                    {
                        "plan_id": plan_id,
                        "attempt": attempt,
                        "mode": mode,
                        "kind": "malformed",
                        "error": str(exc)[:500],
                    },
                )
                if content_attempts <= harness.MAX_REGENERATIONS:
                    log.write(
                        "retry",
                        {"plan_id": plan_id, "attempt": attempt, "reason": "malformed_answer"},
                    )
                    continue
                break

        content_attempts += 1
        ok, details = harness.validate_verdict(
            fact, bucket, sentence, action,
            log=log, plan_id=plan_id, attempt=attempt, extra_dates=extra_dates,
        )
        if ok:
            agree = harness.agree_flag(fact["reported_status"], bucket)
            verdict = {
                "bucket": bucket,
                "verdict_display": harness.verdict_display(bucket, agree),
                "agree": agree,
                "sentence": sentence,
                "recommended_action": action,
                "mode": mode,
                "attempts": attempt,
                "harness": {
                    "number_check": "pass",
                    "reasoning_check": "pass",
                    "regenerations": content_attempts - 1,
                },
            }
            if fallbacks:
                verdict["mode_fallbacks"] = fallbacks
            return verdict

        retry_note = _describe_failure(details)
        if content_attempts <= harness.MAX_REGENERATIONS:
            log.write("retry", {"plan_id": plan_id, "attempt": attempt, "reason": retry_note})
            continue
        break

    log.write(
        "hard_fail",
        {
            "plan_id": plan_id,
            "mode": mode,
            "attempts": attempt,
            "reason": retry_note,
        },
    )
    raise harness.HarnessError(
        f"{plan_id}: generated verdict failed harness validation after "
        f"{content_attempts} attempts in mode '{mode}' ({retry_note}). "
        f"See outputs runlog.jsonl; `--llm-mode template` always passes."
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

NUMBERS_COLUMN_FIELDS = (
    "baseline", "baseline_display", "target", "target_display",
    "actual_4wk", "actual_4wk_display", "weeks_in_actual", "window_weeks",
    "prior_4wk", "prior_4wk_display", "change_from_baseline",
    "gap_closed_pct", "gap_closed_display", "trend_direction",
    "below_baseline", "target_met", "due_date", "is_overdue", "days_overdue",
    "baseline_at_open", "baseline_at_open_display", "baseline_matches_recorded",
    "notes", "summary",
)


def run(
    as_of: date,
    leader: str = DEFAULT_LEADER,
    mode: str | None = None,
    plans_filter: list[str] | None = None,
    write: bool = True,
) -> dict:
    mode = mode or detect_mode()
    if mode not in MODES:
        raise SystemExit(f"Unknown --llm-mode {mode!r}; choose from {MODES}.")

    facts_doc = facts.build_facts(as_of, write=write)
    rows = [f for f in facts_doc["plans"] if f["owner"] == leader]
    if plans_filter:
        rows = [f for f in rows if f["plan_id"] in plans_filter]
    if not rows:
        raise SystemExit(f"No action plans owned by {leader!r}" + (
            f" matching {plans_filter}" if plans_filter else ""))

    out_dir = config.OUTPUTS_DIR / str(as_of)
    log = harness.RunLog(out_dir / "runlog.jsonl" if write else None, step="verdicts")
    log.write(
        "run_start",
        {"as_of": str(as_of), "leader": leader, "mode": mode, "plans": len(rows)},
    )

    prompt_template = load_prompt_template() if mode != "template" else None
    extra_dates = (str(as_of), facts_doc["latest_complete_week"])

    verdicts = []
    for fact in rows:
        # The deterministic facts summary is leader-facing too — hold it to
        # the same number check as the AI's sentence.
        summary_check = harness.check_numbers(fact["summary"], fact, extra_dates)
        log.write(
            "facts_summary_number_check",
            {
                "plan_id": fact["plan_id"],
                "result": "pass" if summary_check["ok"] else "fail",
                "unknown_tokens": summary_check["unknown"],
            },
        )
        if not summary_check["ok"]:
            raise harness.HarnessError(
                f"{fact['plan_id']}: the deterministic facts summary cites numbers "
                f"outside its own fact row ({summary_check['unknown']}) — fix pipeline.facts."
            )

    # LLM calls are I/O-bound; judge every plan concurrently. Results keep
    # the input order, and each still passes the full harness independently.
    if mode == "template" or len(rows) == 1:
        judged = [judge_plan(f, mode, prompt_template, log, extra_dates) for f in rows]
    else:
        with ThreadPoolExecutor(max_workers=min(len(rows), 10)) as pool:
            judged = list(
                pool.map(lambda f: judge_plan(f, mode, prompt_template, log, extra_dates), rows)
            )

    for fact, verdict in zip(rows, judged):
        log.write(
            "verdict",
            {
                "plan_id": fact["plan_id"],
                "bucket": verdict["bucket"],
                "agree": verdict["agree"],
                "attempts": verdict["attempts"],
            },
        )
        verdicts.append(
            {
                "plan_id": fact["plan_id"],
                "center": fact["center"],
                "location_id": fact["location_id"],  # audit trail only
                "metric": fact["metric"],
                "metric_display": fact["metric_display"],
                "reported_status": {
                    "status": fact["reported_status"],
                    "display": fact["reported_status_display"],
                    "owner": fact["owner"],
                    "last_update": fact["last_status_update"],
                    "staleness_days": fact["staleness_days"],
                },
                "what_the_numbers_say": {k: fact[k] for k in NUMBERS_COLUMN_FIELDS},
                "ai_verdict": verdict,
            }
        )

    bucket_counts = {b: sum(1 for v in verdicts if v["ai_verdict"]["bucket"] == b) for b in harness.BUCKETS}
    # Which tier actually wrote each verdict — equal to the requested mode
    # unless a plan degraded down the ladder mid-run (unreachable model).
    modes_used: dict[str, int] = {}
    for v in verdicts:
        used = v["ai_verdict"]["mode"]
        modes_used[used] = modes_used.get(used, 0) + 1
    fallbacks = [
        {"plan_id": v["plan_id"], **fb}
        for v in verdicts
        for fb in v["ai_verdict"].get("mode_fallbacks", [])
    ]
    counts = {
        "plans": len(verdicts),
        "buckets": bucket_counts,
        "agree": sum(1 for v in verdicts if v["ai_verdict"]["agree"]),
        "disagree": sum(1 for v in verdicts if not v["ai_verdict"]["agree"]),
        "regenerations_total": sum(v["ai_verdict"]["harness"]["regenerations"] for v in verdicts),
        "modes": modes_used,
    }
    log.write(
        "summary",
        {"as_of": str(as_of), "mode": mode, "modes_used": modes_used,
         "mode_fallbacks": len(fallbacks), "counts": counts},
    )
    log.close()

    doc = {
        "step": "Claimed vs. Verified",
        "as_of": str(as_of),
        "latest_complete_week": facts_doc["latest_complete_week"],
        "leader": leader,
        "llm_mode": mode,
        "llm_modes_used": modes_used,
        # Named, never hidden: a plan whose model was unreachable was written
        # by the tier below, and the digest says so.
        "mode_fallbacks": fallbacks,
        "prompt_file": (
            "PROMPTS/claimed-vs-verified.md"
            if any(m != "template" for m in modes_used)
            else None
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "harness": {
            "max_regenerations": harness.MAX_REGENERATIONS,
            "rules": [
                "Every number in a generated sentence must exist in the plan's fact row.",
                "Below baseline can never be ON TRACK.",
                f"Gap closed >= {harness.GAP_EXCEEDED_PCT:.0f}% must be EXCEEDED and recommend closing.",
                f"Past due with no update for {harness.STALE_DAYS_ABANDONED}+ days must be ABANDONED.",
                "When the AI bucket matches the reported status, the verdict reads Agree.",
            ],
        },
        "data_provenance": facts_doc["data_provenance"],
        "verdicts": verdicts,
        "counts": counts,
    }
    if plans_filter:
        doc["plans_filter"] = sorted(plans_filter)
    if write:
        (out_dir / "verdicts.json").write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.verdicts",
        description="Step 3b — Claimed vs. Verified: AI verdicts through the harness.",
    )
    config.add_as_of_argument(parser)
    parser.add_argument(
        "--leader",
        default=DEFAULT_LEADER,
        help=f"Leader whose action plans are judged. Default: {DEFAULT_LEADER}.",
    )
    parser.add_argument(
        "--llm-mode",
        choices=MODES,
        default=None,
        help="Generation mode. Default: auto-detect (claude-cli, then api, then template).",
    )
    parser.add_argument(
        "--plans",
        default=None,
        help="Comma-separated plan IDs to judge (debug/testing filter, e.g. AP_001,AP_020).",
    )
    args = parser.parse_args(argv)
    plans_filter = [p.strip() for p in args.plans.split(",")] if args.plans else None

    doc = run(args.as_of, args.leader, args.llm_mode, plans_filter)

    print(
        f"Claimed vs. Verified — as of {args.as_of} "
        f"(latest complete week {doc['latest_complete_week']}, mode: {doc['llm_mode']})"
    )
    for v in doc["verdicts"]:
        av = v["ai_verdict"]
        print(
            f"   {v['plan_id']} {v['center']:<16} reported {v['reported_status']['display']:<11} "
            f"→ {av['verdict_display']}"
        )
        print(f"        {av['sentence']}")
    c = doc["counts"]
    print(
        f"  {c['plans']} plans: "
        + ", ".join(f"{b} {n}" for b, n in c["buckets"].items() if n)
        + f" · agree {c['agree']} · disagree {c['disagree']}"
        + f" · regenerations {c['regenerations_total']}"
    )
    for fb in doc["mode_fallbacks"]:
        print(
            f"  ! {fb['plan_id']}: {fb['from']} was unavailable — verdict written "
            f"by {fb['to']} instead ({fb['reason']})"
        )
    print(f"  Output: {config.OUTPUTS_DIR / str(args.as_of) / 'verdicts.json'}")


if __name__ == "__main__":
    main()
