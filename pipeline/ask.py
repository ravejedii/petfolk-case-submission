""""Ask this Monday" — grounded Q&A on one digest run.

Scope is deliberately tiny: the current run's artifacts, nothing else.

    answer("why did Mount Pleasant rank first?", as_of=date(2026, 5, 4))
      -> {"answer": ..., "refused": False, "refusal_reason": None,
          "citations": [{"artifact": "signals.json", "detail": ...}, ...],
          "numbers_verified": 14, "mode": "claude-cli", "attempts": 1}

Grounding context = ONLY that run's artifacts:
  DATA/OUTPUTS/validation/report.json      Data Validation & Check findings
  DATA/OUTPUTS/<as-of>/signals.json        ranked + suppressed signals + parameters
  DATA/OUTPUTS/<as-of>/facts.json          deterministic plan fact rows
  DATA/OUTPUTS/<as-of>/verdicts.json       Claimed vs. Verified verdicts
  DATA/OUTPUTS/<as-of>/digest.json         the assembled digest (incl. ledger section)
  DATA/OUTPUTS/ledger_log.jsonl            that as-of run's ledger events (incl.
                                      provider-level receipts: which doctors
                                      carry a slide)
  DATA/TRANSLATION/locations.csv                  center-name mapping (leader's centers)

Three answer modes, the same ladder as pipeline.verdicts (claude-cli > api >
template, auto-detected, overridable with --llm-mode). LLM modes load their
prompt from PROMPTS/answer.md at runtime and may ONLY use the artifacts pasted
into the prompt; they must cite receipts and REFUSE with a specific reason
when the artifacts can't ground an answer. Template mode routes a small set of
deterministic question intents (why did a center rank; what changed since last
week; why a plan got its verdict; what was suppressed) straight off the
artifacts — honest, grounded, clearly limited.

Harness: every non-refused answer passes harness.check_numbers_pool against
the same artifacts that were provided as context. A failed check regenerates
(max 2 retries); a hard fail returns an honest refusal pointing at the digest
receipts — never a fabricated answer.

What that check does and does not guarantee: the pool is every number and date
anywhere in this run's artifacts (~500 values), matched to the precision the
answer displays. So a figure the run never produced is caught, but a plausible
value that happens to exist somewhere else in the run is not — a whole-run pool
is necessarily coarser than the verdict harness, which checks one sentence
against one plan's ~30-value fact row. The tight guarantee is scope: an answer
can only speak in numbers this run actually produced.

When the model cannot be reached at all (usage limit, timeout, no CLI), that
is a transport failure, not a content failure: the tier ladder degrades one
rung and the answer is produced by the tier below, with the degradation
recorded in `fallbacks` and named in the answer's own metadata. A leader is
never told "no" because of an unreachable model, and never told an answer came
from Claude when it didn't.

Every question — answered or refused — is appended to DATA/OUTPUTS/ask_log.jsonl
({ts, as_of, question, refused, refusal_reason, mode, fallbacks}):
asked-and-answered is a digest gap signal, asked-and-refused is a data gap
signal. The log is the product-discovery instrument.

Streaming (--stream): the same answer, reported as it is made. Text deltas are
forwarded as JSONL events while the model writes, then the identical harness
runs and a final event carries the validated payload. The streamed text is
PROVISIONAL — a reply whose numbers fail the check is thrown away and
regenerated ({"type":"redo"}), exactly as in the non-streaming path. Work that
never reaches a model (guard refusals, the deterministic tier) emits only the
final event: nothing fake-streams.

CLI:
    python -m pipeline.ask --as-of 2026-05-04 "why did Mount Pleasant rank first?"
    python -m pipeline.ask --as-of 2026-05-04 --llm-mode template "what was suppressed?"
    python -m pipeline.ask --as-of 2026-05-04 --json "what changed since last week?"
    python -m pipeline.ask --as-of 2026-05-04 --stream "why did Morrisville rank first?"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import os
import subprocess
import sys
import threading
import time

CLI_MODEL = os.environ.get("PETFOLK_CLI_MODEL", "opus")  # pin demo CLI calls to Opus
from datetime import date, datetime, timezone

from . import config, harness, verdicts

PROMPT_FILE = config.PROMPTS_DIR / "answer.md"
ASK_LOG_PATH = config.OUTPUTS_DIR / "ask_log.jsonl"
LEDGER_LOG_PATH = config.OUTPUTS_DIR / "ledger_log.jsonl"
CLI_TIMEOUT_SECONDS = 240

MODES = verdicts.MODES  # ("claude-cli", "api", "template")

# The artifact labels an answer may cite (the same labels used to render the
# context into the prompt). Values are the on-disk paths shown to the user.
ARTIFACT_LABELS = {
    "signals.json": "DATA/OUTPUTS/<as-of>/signals.json",
    "facts.json": "DATA/OUTPUTS/<as-of>/facts.json",
    "verdicts.json": "DATA/OUTPUTS/<as-of>/verdicts.json",
    "digest.json": "DATA/OUTPUTS/<as-of>/digest.json",
    "validation/report.json": "DATA/OUTPUTS/<as-of>/validation/report.json",
    "ledger": "DATA/OUTPUTS/ledger.csv + DATA/OUTPUTS/ledger_log.jsonl",
    "locations.csv": "DATA/TRANSLATION/locations.csv",
}

ARTIFACT_DESCRIPTIONS = {
    "signals.json": "ranked signals, suppressed signals (with reasons), and every engine parameter",
    "facts.json": "the deterministic fact row for every action plan (baselines, targets, 4-week actuals)",
    "verdicts.json": "Claimed vs. Verified — reported status vs. what the numbers say vs. the AI verdict, per plan",
    "digest.json": "the assembled Monday digest: top signals with recommendations, verdicts, ledger, data checks",
    "validation/report.json": "Data Validation & Check — every check that ran, what it found, corrections applied",
    "ledger": "this run's recommendation-ledger events: created / re-checked rows, incl. per-doctor receipts",
    "locations.csv": "center name mapping for the leader's centers",
}

# Words in a question that identify a metric (for routing template answers).
METRIC_QUERY_WORDS = {
    "record_completion_24h_pct": ("record", "chart"),
    "recheck_compliance_pct": ("recheck",),
    "callback_compliance_pct": ("callback",),
    "client_csat": ("csat", "satisfaction"),
    "avg_wait_time_min": ("wait",),
    "appts_per_doctor_hour": ("doctor-hour", "throughput", "appointments per"),
    "staff_call_outs": ("call-out", "callout", "call out"),
    "no_show_rate": ("no-show", "no show"),
    "membership_conversion_pct": ("membership",),
    "revenue_per_appt": ("revenue",),
    "open_dvm_requisitions": ("requisition",),
}


# ---------------------------------------------------------------------------
# Context: load one run's artifacts (and nothing else)
# ---------------------------------------------------------------------------


def _ledger_events_for_run(as_of_str: str) -> list[dict]:
    """The LAST ledger run block for this as-of (the log is append-only across
    re-runs; the latest block is the state the digest was built from)."""
    if not LEDGER_LOG_PATH.exists():
        return []
    events = [json.loads(line) for line in LEDGER_LOG_PATH.read_text().splitlines() if line.strip()]
    start = None
    for i, e in enumerate(events):
        if e.get("event") == "run_start" and e.get("as_of") == as_of_str:
            start = i
    if start is None:
        return [e for e in events if e.get("as_of") == as_of_str]
    block = []
    for e in events[start:]:
        if e.get("as_of") == as_of_str:
            block.append(e)
        if e.get("event") == "run_summary" and e.get("as_of") == as_of_str:
            break
    return block


def _leader_locations(center_ids: set[str]) -> list[dict]:
    path = config.DATA_FILES["locations"]
    if not path.exists():
        return []
    with path.open() as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("location_id") in center_ids]
    keep = ("location_id", "location_name", "metro_area", "state", "region",
            "rmp_name", "rop_name", "opened_date", "maturity_tier")
    return [{k: r.get(k) for k in keep} for r in rows]


def _validation_report(as_of_str: str):
    """This Monday's validation report: its own snapshot when one exists, else
    the current working copy (a run still in flight, whose working copy is its
    own). DATA/OUTPUTS/validation/ belongs to whichever run validated last."""
    snapshot = config.OUTPUTS_DIR / as_of_str / "validation" / "report.json"
    return snapshot if snapshot.exists() else config.OUTPUTS_DIR / "validation" / "report.json"


def load_context(as_of: date) -> dict:
    """Load the run's artifacts. Returns {"as_of", "artifacts", "missing"}."""
    as_of_str = str(as_of)
    run_dir = config.OUTPUTS_DIR / as_of_str
    files = {
        "signals.json": run_dir / "signals.json",
        "facts.json": run_dir / "facts.json",
        "verdicts.json": run_dir / "verdicts.json",
        "digest.json": run_dir / "digest.json",
        # This Monday's own snapshot of its checks, not the shared working copy
        # a later run overwrites — an answer about 2026-04-27 must cite the
        # checks 2026-04-27 was built on. See narrate.validation_source().
        "validation/report.json": _validation_report(as_of_str),
    }
    artifacts: dict[str, object] = {}
    missing: list[str] = []
    for label, path in files.items():
        if path.exists():
            artifacts[label] = json.loads(path.read_text())
        else:
            missing.append(f"{label} ({path.relative_to(config.REPO_ROOT)})")

    artifacts["ledger"] = {
        "note": ("Ledger events recorded by this run. Row state as this digest saw it "
                 "is the 'ledger' section of digest.json."),
        "events": _ledger_events_for_run(as_of_str),
    }
    digest = artifacts.get("digest.json")
    center_ids = {c["location_id"] for c in digest.get("centers", [])} if isinstance(digest, dict) else set()
    artifacts["locations.csv"] = _leader_locations(center_ids)

    return {"as_of": as_of_str, "artifacts": artifacts, "missing": missing}


def build_pool(context: dict) -> tuple[set[float], set[str]]:
    """Every number and date the provided artifacts contain — the only values
    a non-refused answer may cite. Collection sizes are added so an answer may
    say e.g. '3 ranked signals' or 'your 11 centers'."""
    numbers, dates = harness.collect_pool(context["artifacts"])
    a = context["artifacts"]
    for count in (
        len((a.get("digest.json") or {}).get("centers", [])),
        len((a.get("signals.json") or {}).get("signals", [])),
        len((a.get("signals.json") or {}).get("suppressed", [])),
        len((a.get("facts.json") or {}).get("plans", [])),
        len((a.get("verdicts.json") or {}).get("verdicts", [])),
        len((a.get("ledger") or {}).get("events", [])),
        len(a.get("locations.csv") or []),
    ):
        numbers.add(float(count))
    numbers.update({0.0, 100.0})
    dates.add(context["as_of"])
    return numbers, dates


# ---------------------------------------------------------------------------
# Out-of-scope pre-router (deterministic, every mode)
# ---------------------------------------------------------------------------

HR_RE = re.compile(
    r"\b(fire|firing|fired|hire|hiring|terminate|terminating|termination|"
    r"lay(?:ing)?\s+off|layoffs?|discipline|disciplining|disciplinary|demote|promote|"
    r"write\s+(?:him|her|them)\s+up|pip)\b"
)
PREDICT_RE = re.compile(
    r"\b(predict|forecast|projection)s?\b|"
    r"\bwill\b.{0,60}\b(get\s+worse|get\s+better|improve|worsen|continue|happen|recover|rise|fall|drop|hit|reach)\b|"
    r"\bgoing\s+to\s+(get|be|improve|worsen)\b"
)
OFFTOPIC_RE = re.compile(r"\b(weather|traffic|stock\s*market|stock\s+price|sports?\s+scores?|recipe|horoscope)\b")


def out_of_scope_refusal(question: str, context: dict) -> str | None:
    q = question.lower()
    digest = context["artifacts"].get("digest.json") or {}
    leader = digest.get("leader", "the leader")
    latest_week = digest.get("latest_complete_week", context["as_of"])
    if HR_RE.search(q):
        return (
            "That is a staffing/HR decision, which this system does not make — the run's "
            "artifacts describe metric movement and plan verdicts, not people decisions. "
            "The digest can show the numbers behind the situation (see its receipts); "
            "who to hire, fire, or discipline is a judgment call that stays with you."
        )
    if PREDICT_RE.search(q):
        return (
            f"This run's artifacts only describe what has already happened, through the week of "
            f"{latest_week} — the pipeline does not forecast. What it can do is re-check every "
            "open recommendation next Monday and tell you which way each metric actually moved."
        )
    if OFFTOPIC_RE.search(q):
        return (
            f"This run's artifacts contain operating metrics, action-plan verdicts, and the "
            f"recommendation ledger for {leader}'s centers — no data on that topic exists in this run."
        )
    return None


# ---------------------------------------------------------------------------
# Template mode — deterministic answers for a small set of intents
# ---------------------------------------------------------------------------

TEMPLATE_SCOPE = (
    "why a center ranked (or was suppressed), what changed since last week, "
    "why a plan got its verdict, and what was suppressed and why"
)


def _centers_in_question(q: str, context: dict) -> list[str]:
    digest = context["artifacts"].get("digest.json") or {}
    return [c["center"] for c in digest.get("centers", []) if c["center"].lower() in q]


def _metrics_in_question(q: str) -> set[str]:
    return {key for key, words in METRIC_QUERY_WORDS.items() if any(w in q for w in words)}


def _signal_sentences(sig: dict, ranked_total: int, score_cap: float, digest: dict) -> tuple[str, list[dict]]:
    c = sig["contributions"]
    w = sig["weights_used"]
    text = (
        f"{sig['center']} ranks #{sig['rank']} of {ranked_total} this Monday on "
        f"{sig['metric_display'].lower()}, with a priority score of {sig['priority']} "
        f"(scores cap at {score_cap:g}). {sig['headline']} "
        f"What drove the rank: drift contributed {c['drift']}, gap-to-peers {c['gap']}, "
        f"and spike {c['spike']} (weights {w['drift']} / {w['gap']} / {w['spike']})."
    )
    citations = [{
        "artifact": "signals.json",
        "detail": (
            f"rank {sig['rank']} — {sig['center']}, {sig['metric_display']}, priority {sig['priority']}; "
            f"sub-scores drift {sig['scores']['drift']['score']}, gap {sig['scores']['gap']['score']}, "
            f"spike {sig['scores']['spike']['score']}"
        ),
    }]
    for top in digest.get("top_signals", []):
        if top["center"] == sig["center"] and top["metric"] == sig["metric"] and top.get("action"):
            text += f" The digest's recommendation: {top['action']['recommendation']}"
            citations.append({
                "artifact": "digest.json",
                "detail": f"recommendation for {sig['center']} ({sig['metric_display']}), "
                          f"owner {top['action']['owner']}, check by {top['action']['check_by']}",
            })
    return text, citations


def _answer_rank(q: str, context: dict, centers: list[str]) -> tuple[str, list[dict]] | None:
    signals = context["artifacts"].get("signals.json") or {}
    digest = context["artifacts"].get("digest.json") or {}
    ranked = signals.get("signals", [])
    suppressed = signals.get("suppressed", [])
    total = signals.get("counts", {}).get("ranked", len(ranked))
    cap = signals.get("parameters", {}).get("score_cap", 10.0)
    metric_keys = _metrics_in_question(q)

    if centers:
        hits = [s for s in ranked if s["center"] in centers]
        if metric_keys and any(s["metric"] in metric_keys for s in hits):
            hits = [s for s in hits if s["metric"] in metric_keys]
        if hits:
            parts, citations = [], []
            for s in hits:
                t, cits = _signal_sentences(s, total, cap, digest)
                parts.append(t)
                citations.extend(cits)
            return " ".join(parts), citations
        sup_hits = [s for s in suppressed if s["center"] in centers]
        if metric_keys:
            sup_hits = [s for s in sup_hits if s["metric"] in metric_keys] or sup_hits
        if sup_hits:
            parts = [
                f"{s['center']} did not rank on {s['metric_display'].lower()} this Monday — that signal "
                f"was suppressed (rule: {s['rule']}, would-be priority {s['would_be_priority']}). {s['reason']}"
                for s in sup_hits
            ]
            return " ".join(parts), [{
                "artifact": "signals.json",
                "detail": "suppressed list — " + "; ".join(
                    f"{s['center']} {s['metric_display']} ({s['rule']}, would-be {s['would_be_priority']})"
                    for s in sup_hits),
            }]
        name = centers[0]
        return (
            f"{name} has no ranked or suppressed signal this run — nothing at {name} crossed the "
            f"signal engine's attention thresholds this Monday ({total} signals ranked network-wide "
            f"for this digest, {len(suppressed)} suppressed)."
        ), [{"artifact": "signals.json",
             "detail": f"ranked and suppressed lists contain no entry for {name}"}]

    if not ranked:
        return None
    # "why is this first?" — one signal. "what earned attention?" — the whole
    # ranked list, which suppression already keeps short.
    if any(k in q for k in ("top signal", "top issue", "first", "most important", "#1")):
        return _signal_sentences(ranked[0], total, cap, digest)
    parts, citations = [], []
    for sig in ranked:
        text, cits = _signal_sentences(sig, total, cap, digest)
        parts.append(text)
        citations.extend(cits)
    return " ".join(parts), citations


def _answer_changed(context: dict) -> tuple[str, list[dict]] | None:
    digest = context["artifacts"].get("digest.json") or {}
    ledger = digest.get("ledger") or {}
    changed, created, counts = ledger.get("changed", []), ledger.get("created", []), ledger.get("counts", {})
    if not (changed or created):
        return None
    parts = [
        f"Since last Monday, {counts.get('rechecked', len(changed))} open recommendation(s) were "
        f"re-checked against the week that followed."
    ]
    for ch in changed:
        parts.append(f"{ch['center']} — {ch['metric_display'].lower()}: {ch['outcome_display']}. {ch['note']}")
    if created:
        names = ", ".join(f"{cr['center']} ({cr['metric_display'].lower()})" for cr in created)
        parts.append(f"This run also created {len(created)} new recommendation(s): {names}.")
    citations = [{
        "artifact": "digest.json",
        "detail": "ledger section — " + "; ".join(
            f"{ch['center']} {ch['metric_display']} → {ch['outcome_display']}" for ch in changed),
    }, {
        "artifact": "ledger",
        "detail": f"re-check events for {context['as_of']} (anchor vs. follow-up levels per recommendation)",
    }]
    return " ".join(parts), citations


def _answer_verdict(q: str, context: dict, centers: list[str]) -> tuple[str, list[dict]] | None:
    vdoc = context["artifacts"].get("verdicts.json") or {}
    rows = vdoc.get("verdicts", [])
    plan_ids = set(re.findall(r"\bAP_\d+\b", q.upper()))
    hits = [r for r in rows if r["plan_id"] in plan_ids] if plan_ids else []
    if not hits and centers:
        hits = [r for r in rows if r["center"] in centers]
        metric_keys = _metrics_in_question(q)
        if metric_keys:
            narrowed = [r for r in hits if r["metric"] in metric_keys]
            hits = narrowed or hits
    if not hits:
        return None
    parts, details = [], []
    for r in hits:
        av, rep = r["ai_verdict"], r["reported_status"]
        parts.append(
            f"{r['plan_id']} — {r['metric_display'].lower()} at {r['center']}: the owner reported "
            f"\"{rep['display']}\", the AI verdict is {av['verdict_display']}. {av['sentence']} "
            f"Recommended action: {av['recommended_action']}"
        )
        details.append(f"{r['plan_id']} {r['center']} — reported {rep['display']}, verdict {av['verdict_display']}")
    return " ".join(parts), [{"artifact": "verdicts.json", "detail": "; ".join(details)}]


def _answer_suppressed(context: dict) -> tuple[str, list[dict]] | None:
    signals = context["artifacts"].get("signals.json") or {}
    suppressed = signals.get("suppressed", [])
    if not suppressed:
        return ("No signals were suppressed this run.",
                [{"artifact": "signals.json", "detail": "suppressed list is empty"}])
    parts = [f"{len(suppressed)} signal(s) were suppressed this run — listed with reasons, never silently dropped."]
    for s in suppressed:
        parts.append(
            f"{s['center']} — {s['metric_display'].lower()} (would-be priority {s['would_be_priority']}, "
            f"rule {s['rule']}): {s['reason']}"
        )
    return " ".join(parts), [{
        "artifact": "signals.json",
        "detail": "suppressed list — " + "; ".join(
            f"{s['center']} {s['metric_display']} ({s['rule']})" for s in suppressed),
    }]


VERDICT_WORDS = ("verdict", "abandoned", "exceeded", "on track", "not working", "claimed", "plan")
CHANGED_WORDS = ("what changed", "changed since", "since last", "what happened since", "last week's recommendation")
SUPPRESSED_WORDS = ("suppress", "held back", "left off", "didn't make", "did not make", "excluded", "hidden")
RANK_WORDS = ("rank", "top", "first", "why", "priority", "flag", "signal", "attention")


def template_answer(question: str, context: dict) -> tuple[str, list[dict]] | None:
    """Route one question to a deterministic intent; None when unroutable."""
    q = question.lower()
    centers = _centers_in_question(q, context)

    if any(w in q for w in SUPPRESSED_WORDS):
        result = _answer_suppressed(context)
        if result:
            return result
    if any(w in q for w in CHANGED_WORDS):
        result = _answer_changed(context)
        if result:
            return result
    if re.search(r"\bAP_\d+\b", question.upper()) or any(w in q for w in VERDICT_WORDS):
        result = _answer_verdict(q, context, centers)
        if result:
            return result
    if centers or any(w in q for w in RANK_WORDS):
        result = _answer_rank(q, context, centers)
        if result:
            return result
    return None


def template_refusal(context: dict, fallbacks: list[dict] | None = None) -> str:
    """Why this question went unanswered in template mode — including, when the
    run started on a model tier and dropped, the real reason it dropped."""
    if fallbacks:
        first = fallbacks[0]
        why = (
            f"{first['from']} could not be reached ({first['reason']}), so this answer had to come "
            f"from the deterministic tier, which only answers a fixed set of questions from the "
            f"run's artifacts: {TEMPLATE_SCOPE}."
        )
    else:
        why = (
            f"No language model is available in this environment, and template mode only answers a "
            f"fixed set of questions from the run's artifacts: {TEMPLATE_SCOPE}."
        )
    return (
        f"{why} Try one of those, or open the digest receipts directly: "
        f"DATA/OUTPUTS/{context['as_of']}/digest.json and DATA/OUTPUTS/{context['as_of']}/signals.json."
    )


# ---------------------------------------------------------------------------
# LLM modes — prompt from PROMPTS/answer.md, answer through the harness
# ---------------------------------------------------------------------------


def load_prompt_template() -> str:
    if not PROMPT_FILE.exists():
        raise SystemExit(f"Prompt file missing: {PROMPT_FILE} (PROMPTS/ is the code path).")
    return PROMPT_FILE.read_text()


def render_context(context: dict) -> str:
    blocks = []
    for label, obj in context["artifacts"].items():
        desc = ARTIFACT_DESCRIPTIONS.get(label, "")
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        blocks.append(f"### {label} — {desc}\n```json\n{body}\n```")
    return "\n\n".join(blocks)


def render_prompt(template: str, question: str, context: dict, retry_note: str | None) -> str:
    digest = context["artifacts"].get("digest.json") or {}
    note = ""
    if retry_note:
        note = (
            "\n## Fix required — your previous answer failed validation\n"
            f"{retry_note}\n"
            "Rewrite your answer so every number and citation verifies against the artifacts, "
            "or refuse with a specific reason if you cannot.\n"
        )
    return (
        template.replace("{{AS_OF}}", context["as_of"])
        .replace("{{LATEST_WEEK}}", str(digest.get("latest_complete_week", "")))
        .replace("{{LEADER}}", str(digest.get("leader", "")))
        .replace("{{ARTIFACTS}}", render_context(context))
        .replace("{{QUESTION}}", question)
        .replace("{{RETRY_NOTE}}", note)
    )


def parse_llm_answer(text: str) -> dict:
    """Parse ANSWER:/CITATIONS: or REFUSED: out of the model's reply.

    Returns {"refused": bool, "answer": str|None, "refusal_reason": str|None,
    "citations": [{"artifact", "detail"}]}. Raises ValueError when malformed.
    """
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
    mode = None
    answer_parts: list[str] = []
    refusal_parts: list[str] = []
    citations: list[dict] = []
    for raw in lines:
        s = raw.strip()
        upper = s.upper()
        if upper.startswith("REFUSED:"):
            mode = "refused"
            refusal_parts.append(s[len("REFUSED:"):].strip())
            continue
        if upper.startswith("ANSWER:"):
            mode = "answer"
            answer_parts.append(s[len("ANSWER:"):].strip())
            continue
        if upper.startswith("CITATIONS:"):
            mode = "citations"
            continue
        if not s:
            continue
        if mode == "answer":
            answer_parts.append(s)
        elif mode == "refused":
            refusal_parts.append(s)
        elif mode == "citations":
            item = s.lstrip("-•* ").strip()
            if ":" in item:
                artifact, detail = item.split(":", 1)
                citations.append({"artifact": artifact.strip().strip("`"), "detail": detail.strip()})
            elif item:
                citations.append({"artifact": item.strip("`"), "detail": ""})

    if refusal_parts:
        reason = " ".join(p for p in refusal_parts if p).strip()
        if not reason:
            raise ValueError("REFUSED line carries no reason.")
        return {"refused": True, "answer": None, "refusal_reason": reason, "citations": []}
    answer = " ".join(p for p in answer_parts if p).strip()
    if not answer:
        raise ValueError("Reply contains neither an ANSWER: nor a REFUSED: line.")
    if not citations:
        raise ValueError("ANSWER given without any CITATIONS lines.")
    bad = [c["artifact"] for c in citations
           if not any(label in c["artifact"] or c["artifact"] in label for label in ARTIFACT_LABELS)]
    if bad:
        raise ValueError(
            f"Citations reference unknown artifacts {bad!r}; use only these labels: "
            f"{', '.join(ARTIFACT_LABELS)}."
        )
    return {"refused": False, "answer": answer, "refusal_reason": None, "citations": citations}


def call_claude_cli(prompt: str) -> str:
    """Headless `claude -p` reading the (large) prompt from stdin. The whole
    run's artifacts go into this prompt, so it is far too big for argv."""
    proc = subprocess.run(
        ["claude", "-p", "--model", CLI_MODEL, "--output-format", "text"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            verdicts.cli_failure_message(proc.returncode, proc.stdout, proc.stderr)
        )
    return proc.stdout


def call_claude_cli_stream(prompt: str, on_delta) -> str:
    """The same headless `claude -p` call as above, in stream-json mode, with
    each text delta handed to `on_delta` the moment it arrives.

    The CLI's contract (verified by running it): with
    `--output-format stream-json --verbose --include-partial-messages` it
    writes one JSON object per line —

        {"type":"stream_event","event":{"type":"content_block_delta",
         "delta":{"type":"text_delta","text":"…"}}}   the partial text
        {"type":"assistant","message":{"content":[{"type":"text",…}]}}
        {"type":"result","subtype":"success","result":"…full text…"}

    The RETURN VALUE is the whole reply, and it is the only thing the harness
    ever validates: deltas are provisional display, the parsed+checked answer
    is the product. Nothing downstream trusts the streamed text.
    """
    proc = subprocess.Popen(
        ["claude", "-p", "--model", CLI_MODEL, "--output-format", "stream-json",
         "--verbose", "--include-partial-messages"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # The prompt carries the whole run's artifacts (~1MB), far more than a pipe
    # buffer holds, so it is written on its own thread — a blocking write here
    # would deadlock against a stdout nobody is draining yet.
    def feed() -> None:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
        except (ValueError, OSError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=drain_stderr, daemon=True).start()

    # A blocking readline cannot enforce its own deadline: a watchdog kills the
    # child instead, which ends the loop, and the flag turns that into the same
    # TimeoutExpired the non-streaming path raises (→ LLMUnavailable → degrade).
    timed_out = threading.Event()

    def give_up() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(CLI_TIMEOUT_SECONDS, give_up)
    timer.daemon = True
    timer.start()

    chunks: list[str] = []
    assistant_text = ""
    result_text = ""
    plain_stdout: list[str] = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                plain_stdout.append(line)  # kept for the failure message only
                continue
            kind = obj.get("type")
            if kind == "stream_event":
                event = obj.get("event") or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    text = delta.get("text")
                    if delta.get("type") == "text_delta" and text:
                        chunks.append(text)
                        on_delta(text)
            elif kind == "assistant":
                blocks = ((obj.get("message") or {}).get("content")) or []
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                if text:
                    assistant_text = text
            elif kind == "result":
                if isinstance(obj.get("result"), str):
                    result_text = obj["result"]
    finally:
        timer.cancel()
        proc.wait()

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(proc.args, CLI_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(
            verdicts.cli_failure_message(
                proc.returncode,
                "\n".join(plain_stdout + ([result_text] if result_text else [])),
                "".join(stderr_lines),
            )
        )
    # Prefer the CLI's own final text over the concatenated deltas: a dropped
    # delta must never become a silently truncated answer.
    reply = result_text or assistant_text or "".join(chunks)
    if not reply.strip():
        raise RuntimeError("claude CLI exited 0 but produced no text to answer with")
    return reply


def call_llm(mode: str, prompt: str, on_delta=None) -> str:
    """One generation call through the shared failure classifier — any way of
    not getting an answer surfaces as verdicts.LLMUnavailable, which the ladder
    below treats as "degrade a tier", never as "the model was wrong".

    `on_delta`, when given, is called with each chunk of text as the model
    produces it (claude-cli only — the api tier has no streaming transport
    here, so its answer simply arrives whole). It changes what the caller can
    SHOW while waiting; it changes nothing about what is returned or checked.
    """
    if mode == "claude-cli":
        if on_delta is not None:
            return verdicts.call_guarded(
                mode, lambda p: call_claude_cli_stream(p, on_delta), prompt
            )
        return verdicts.call_guarded(mode, call_claude_cli, prompt)
    if mode == "api":
        return verdicts.call_guarded(mode, verdicts.call_api, prompt)
    if mode == "openai":
        if on_delta is not None:
            return verdicts.call_guarded(
                mode, lambda p: verdicts.call_openai_stream(p, on_delta), prompt
            )
        return verdicts.call_guarded(mode, verdicts.call_openai, prompt)
    raise ValueError(f"call_llm has no transport for mode {mode!r}.")


# ---------------------------------------------------------------------------
# The ask loop
# ---------------------------------------------------------------------------


def log_question(as_of: str, question: str, refused: bool, refusal_reason: str | None,
                 mode: str, fallbacks: list[dict] | None = None, log_path=None) -> None:
    """Append-only ask log — the product-discovery instrument. Every question
    lands here: answered = digest gap signal, refused = data gap signal."""
    path = log_path or ASK_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of,
        "question": question,
        "refused": refused,
        "refusal_reason": refusal_reason,
        "mode": mode,
        "fallbacks": fallbacks or [],
    }
    with path.open("a") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _artifacts_read(context: dict | None) -> list[str]:
    """The on-disk files an answer was grounded in, repo-relative — the same
    receipt line pipeline.narrate emits under a phase narrative."""
    if not context:
        return []
    as_of = context.get("as_of", "")
    return [
        ARTIFACT_LABELS.get(label, label).replace("<as-of>", as_of)
        for label in context.get("artifacts", {})
    ]


def _result(context_as_of: str, question: str, mode: str, attempts: int, *,
            answer: str | None = None, citations: list[dict] | None = None,
            numbers_verified: int = 0, refusal_reason: str | None = None,
            elapsed: float | None = None, fallbacks: list[dict] | None = None,
            decided_by: str | None = None, context: dict | None = None,
            log_path=None, write_log: bool = True) -> dict:
    refused = answer is None
    if write_log:
        log_question(context_as_of, question, refused, refusal_reason, mode, fallbacks, log_path)
    return {
        "answer": answer,
        "refused": refused,
        "refusal_reason": refusal_reason,
        "citations": citations or [],
        "numbers_verified": numbers_verified,
        # The receipt line under an answer: which artifacts it was grounded in
        # and which checks it passed. A refusal grounded nothing, so it claims
        # nothing — the fields stay empty rather than implying work.
        "artifacts_read": [] if refused else _artifacts_read(context),
        "checks": {
            "number_check": "not run" if refused else "pass",
            "numbers_verified": numbers_verified,
        },
        "mode": mode,
        # `mode` is the tier this answer was configured to use; `decided_by` is
        # what actually produced it. They differ only where the decision is made
        # before any model is consulted ("guard"), so a caller — the panel, a
        # reviewer reading the log — never credits a model that never ran.
        "decided_by": decided_by or mode,
        # Non-empty when the tier the run started on could not be reached: the
        # answer above came from a lower rung, and says so.
        "fallbacks": fallbacks or [],
        "attempts": attempts,
        "as_of": context_as_of,
        "question": question,
        "elapsed_seconds": round(elapsed, 2) if elapsed is not None else None,
    }


def _llm_answer(question: str, context: dict, mode: str, prompt_template: str,
                pool_numbers: set[float], pool_dates: set[str], emit=None) -> dict:
    """One tier's attempt at the question. Returns exactly one of:

      {"kind": "answer",      answer, citations, numbers_verified, attempts}
      {"kind": "refused",     reason, attempts}   the model refused, by design
      {"kind": "unverified",  unknown, attempts}  answered, never verified
      {"kind": "unavailable", reason, attempts}   never answered at all

    Content failures (malformed reply, unverified numbers) are retried here.
    A transport failure returns immediately so the caller can degrade a tier
    instead of retrying into the same wall.

    `emit`, when given, reports progress as it happens (see `answer`): the
    generation starting, each text delta, generation finishing and validation
    beginning, and a regeneration when the harness rejects a reply. It observes
    the loop; it never changes what the loop accepts.
    """
    retry_note: str | None = None
    last_unknown: list[str] = []
    attempts = 0
    on_delta = (lambda text: emit({"type": "delta", "text": text})) if emit else None
    for attempt in range(1, harness.MAX_REGENERATIONS + 2):
        attempts = attempt
        prompt = render_prompt(prompt_template, question, context, retry_note)
        try:
            if emit:
                emit({"type": "generating", "mode": mode, "attempt": attempt})
                raw = call_llm(mode, prompt, on_delta)
            else:
                raw = call_llm(mode, prompt)
        except verdicts.LLMUnavailable as exc:
            return {"kind": "unavailable", "reason": str(exc), "attempts": attempts}

        # Generation is done; nothing here is an answer yet. Everything below
        # is the harness deciding whether the text may be shown as one.
        if emit:
            emit({"type": "verifying", "mode": mode, "attempt": attempt})

        try:
            parsed = parse_llm_answer(raw)
        except ValueError as exc:
            retry_note = f"Your previous reply could not be used: {exc}"
            if emit and attempt <= harness.MAX_REGENERATIONS:
                emit({"type": "redo", "attempt": attempt, "kind": "malformed",
                      "reason": str(exc)[:300]})
            continue

        if parsed["refused"]:
            return {"kind": "refused", "reason": parsed["refusal_reason"], "attempts": attempts}

        checked_text = parsed["answer"] + " " + " ".join(c["detail"] for c in parsed["citations"])
        check = harness.check_numbers_pool(checked_text, pool_numbers, pool_dates)
        if check["ok"]:
            return {
                "kind": "answer",
                "answer": parsed["answer"],
                "citations": parsed["citations"],
                "numbers_verified": check["checked"],
                "attempts": attempts,
            }
        last_unknown = check["unknown"]
        retry_note = (
            "These numbers do not exist anywhere in the artifacts provided (hallucination): "
            + ", ".join(check["unknown"])
        )
        if emit and attempt <= harness.MAX_REGENERATIONS:
            emit({"type": "redo", "attempt": attempt, "kind": "unverified_number",
                  "unknown": check["unknown"]})

    return {"kind": "unverified", "unknown": last_unknown, "attempts": attempts}


def answer(question: str, as_of: date | str, mode: str | None = None,
           log_path=None, write_log: bool = True, on_event=None) -> dict:
    """Answer one leader question from one run's artifacts, or refuse with a
    specific reason. Every non-refused answer has passed the pool number check
    against the same artifacts it was grounded on. An unreachable model
    degrades a tier (recorded in `fallbacks`); it never fabricates and never
    silently claims a tier it did not use.

    `on_event` (optional) receives progress dicts while the answer is being
    made — how `--stream` feeds a live UI:

        {"type":"generating","mode","attempt"}   a model call has started
        {"type":"delta","text"}                  provisional text, as produced
        {"type":"verifying","mode","attempt"}    generation done, harness running
        {"type":"redo","attempt","kind",…}       that reply was rejected; retry
        {"type":"tier_fallback","from","to",…}   the tier could not be reached

    Two honesty rules are structural here, not stylistic:
      1. deltas are provisional. Only the dict this function RETURNS has been
         through the harness, and only it carries citations/numbers_verified.
      2. work that never reaches a model never streams. Guard refusals and the
         deterministic tier emit no events at all — nothing can be mistaken for
         a model writing when no model ran.
    """
    started = time.monotonic()
    if isinstance(as_of, str):
        as_of = config.parse_as_of(as_of)
    as_of_str = str(as_of)
    mode = mode or verdicts.detect_mode()
    if mode not in MODES:
        raise SystemExit(f"Unknown --llm-mode {mode!r}; choose from {MODES}.")

    context = load_context(as_of)
    if context["missing"]:
        reason = (
            f"No complete run exists for {as_of_str} — missing artifacts: "
            f"{', '.join(context['missing'])}. Run the pipeline for that Monday first "
            f"(python -m pipeline.run --as-of {as_of_str})."
        )
        return _result(as_of_str, question, mode, 0, refusal_reason=reason, decided_by="guard",
                       elapsed=time.monotonic() - started, log_path=log_path, write_log=write_log)

    # Out of scope by design (HR/predictions/off-topic): refused deterministically,
    # before a model is asked — no tier gets credit for this one.
    scope_refusal = out_of_scope_refusal(question, context)
    if scope_refusal:
        return _result(as_of_str, question, mode, 0, refusal_reason=scope_refusal, decided_by="guard",
                       elapsed=time.monotonic() - started, log_path=log_path, write_log=write_log)

    pool_numbers, pool_dates = build_pool(context)

    fallbacks: list[dict] = []
    prompt_template: str | None = None
    attempts_total = 0

    while True:
        if mode == "template":
            routed = template_answer(question, context)
            if routed is None:
                return _result(as_of_str, question, mode, attempts_total,
                               refusal_reason=template_refusal(context, fallbacks),
                               elapsed=time.monotonic() - started, fallbacks=fallbacks,
                               context=context, log_path=log_path, write_log=write_log)
            text, citations = routed
            attempts_total += 1
            check = harness.check_numbers_pool(
                text + " " + " ".join(c["detail"] for c in citations), pool_numbers, pool_dates)
            if not check["ok"]:  # template text is built from artifact values; this is a tripwire
                reason = (
                    f"The generated answer failed number verification against this run's artifacts "
                    f"(unverified: {', '.join(check['unknown'])}). Open the digest receipts instead: "
                    f"DATA/OUTPUTS/{as_of_str}/digest.json."
                )
                return _result(as_of_str, question, mode, attempts_total, refusal_reason=reason,
                               elapsed=time.monotonic() - started, fallbacks=fallbacks,
                               context=context, log_path=log_path, write_log=write_log)
            return _result(as_of_str, question, mode, attempts_total, answer=text,
                           citations=citations, numbers_verified=check["checked"],
                           elapsed=time.monotonic() - started, fallbacks=fallbacks,
                           context=context, log_path=log_path, write_log=write_log)

        # LLM tiers: claude-cli / api
        if prompt_template is None:
            prompt_template = load_prompt_template()
        outcome = _llm_answer(question, context, mode, prompt_template, pool_numbers,
                              pool_dates, on_event)
        attempts_total += outcome["attempts"]

        if outcome["kind"] == "answer":
            return _result(as_of_str, question, mode, attempts_total, answer=outcome["answer"],
                           citations=outcome["citations"],
                           numbers_verified=outcome["numbers_verified"],
                           elapsed=time.monotonic() - started, fallbacks=fallbacks,
                           context=context, log_path=log_path, write_log=write_log)

        if outcome["kind"] == "refused":
            return _result(as_of_str, question, mode, attempts_total,
                           refusal_reason=outcome["reason"],
                           elapsed=time.monotonic() - started, fallbacks=fallbacks,
                           context=context, log_path=log_path, write_log=write_log)

        if outcome["kind"] == "unavailable":
            nxt = verdicts.fallback_mode(mode)
            if nxt is None:
                reason = (
                    f"No language model could be reached to answer this — {mode} is unavailable "
                    f"({outcome['reason']}) and this environment has no tier below it. The digest "
                    f"itself is unaffected: open DATA/OUTPUTS/{as_of_str}/digest.json for the receipts."
                )
                return _result(as_of_str, question, mode, attempts_total, refusal_reason=reason,
                               elapsed=time.monotonic() - started, fallbacks=fallbacks,
                               context=context, log_path=log_path, write_log=write_log)
            fallbacks.append({"from": mode, "to": nxt, "reason": outcome["reason"][:300]})
            if on_event:
                on_event({"type": "tier_fallback", "from": mode, "to": nxt,
                          "reason": outcome["reason"][:300]})
            mode = nxt
            continue

        # Answered, but never verifiable → honest refusal pointing at the
        # receipts. Never a fabricated answer.
        unknown = outcome["unknown"]
        reason = (
            f"I could not produce an answer whose every number verifies against this run's artifacts"
            + (f" (unverified: {', '.join(unknown)})" if unknown else "")
            + f" after {outcome['attempts']} attempts. Rather than risk a wrong figure, open the "
            f"digest receipts directly: DATA/OUTPUTS/{as_of_str}/digest.json and "
            f"DATA/OUTPUTS/{as_of_str}/signals.json."
        )
        return _result(as_of_str, question, mode, attempts_total, refusal_reason=reason,
                       elapsed=time.monotonic() - started, fallbacks=fallbacks,
                       context=context, log_path=log_path, write_log=write_log)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.ask",
        description='"Ask this Monday" — grounded Q&A on one digest run.',
    )
    config.add_as_of_argument(parser)
    parser.add_argument(
        "--llm-mode",
        choices=MODES,
        default=None,
        help="Answer mode. Default: auto-detect (claude-cli, then api, then template).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the result as one JSON object on stdout (how the API layer calls this).",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help=(
            "Emit progress as JSONL on stdout while the answer is made — one event per "
            "line ({\"type\":\"delta\",\"text\":…} as the model writes), ending with "
            "{\"type\":\"final\", …} carrying the validated answer. Implies --json."
        ),
    )
    parser.add_argument("question", help='The leader\'s question, quoted (e.g. "why did Mount Pleasant rank first?").')
    args = parser.parse_args(argv)

    if args.stream:
        # One JSON object per line, flushed as it happens: the API layer turns
        # these into server-sent events. The LAST line is always the `final`
        # event, and it alone carries the harness-validated payload — a reader
        # that ignores every other event still gets exactly what --json gives.
        def emit(event: dict) -> None:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()

        result = answer(args.question, args.as_of, args.llm_mode, on_event=emit)
        emit({"type": "final", **result})
        return

    result = answer(args.question, args.as_of, args.llm_mode)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return

    print(f'Ask this Monday — as of {result["as_of"]} (mode: {result["mode"]}, '
          f'{result["elapsed_seconds"]}s, attempts: {result["attempts"]})')
    print(f'Q: {result["question"]}')
    for fb in result["fallbacks"]:
        print(f'  ! {fb["from"]} unavailable — answered by {fb["to"]} instead ({fb["reason"]})')
    if result["refused"]:
        print(f'REFUSED: {result["refusal_reason"]}')
    else:
        print(f'ANSWER ({result["numbers_verified"]} numbers verified):')
        print(f'  {result["answer"]}')
        print("Citations:")
        for c in result["citations"]:
            print(f'  - {c["artifact"]}: {c["detail"]}')
    print(f"Logged to {ASK_LOG_PATH.relative_to(config.REPO_ROOT)}")


if __name__ == "__main__":
    main()
