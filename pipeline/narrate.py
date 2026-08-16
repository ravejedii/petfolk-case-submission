"""Per-phase narratives — the stepper's plain-English voice.

For each completed pipeline phase, one lead line and a few bullets for a
non-engineer leader — what was DECIDED or FLAGGED, with the center, the metric
and the numbers; never how the machine worked. Generated ONLY from that run's
real artifacts:

  validation   DATA/OUTPUTS/validation/report.json + corrections_proposed.json,
               plus the accept/decline decisions recorded in DATA/TRANSLATION/MANIFEST.json
  signals      DATA/OUTPUTS/<as-of>/signals.json
  verdicts     DATA/OUTPUTS/<as-of>/facts.json + verdicts.json
  digest       DATA/OUTPUTS/<as-of>/digest.json + this run's ledger events from
               DATA/OUTPUTS/ledger_log.jsonl

Cross-phase context is deterministic, never model memory: each phase's prompt
carries a trimmed block of the PRIOR phases' key results (by pipeline order,
whatever exists on disk), so the signals narrative can truthfully say the peer
groups use the corrected maturity labels — and the number check can verify it.

Three generation modes, the same ladder as pipeline.verdicts / pipeline.ask
(claude-cli > api > template, auto-detected, overridable with --llm-mode).
LLM modes load their prompt from PROMPTS/phase-note.md at runtime. Every
narrative — every mode — passes five checks against the same artifacts that
were pasted into its prompt:

  number check    every figure and date exists in those artifacts
  entity check    every proper name is a center the run actually names
  rule check      per phase: every ranked signal's center is named and no
                  suppressed-only center is; no plan is filed under a bucket
                  the deterministic verdict table did not give it
  language check  no internal IDs, no raw metric ids, never "audit", no
                  process narration, no editorial adjectives
  shape check     one lead line, 1–8 bullets, under ~110 words

and every number, date and name that passed is emitted as a RECEIPT — file,
field, value — so a reader can follow the sentence back to the artifact by
hand. A failed check regenerates
(max harness.MAX_REGENERATIONS retries); a hard content failure falls back to
the deterministic template narrative, which is built from artifact values and
passes by construction — a phase is never left silent, and never narrated
with an unverified number. An unreachable model (usage limit, timeout) is a
transport failure: the ladder degrades one rung, recorded in `fallbacks` and
named in `decided_by`, so no model is credited for work it never did.

Every check, retry, and fallback is appended to
DATA/OUTPUTS/<as-of>/narrate_runlog.jsonl — narrate's OWN append-only log. It
deliberately does not share runlog.jsonl: the stepper narrates phases WHILE
the pipeline is still running, and the verdicts step opens runlog.jsonl fresh
("w") mid-run — a shared file would let one writer overwrite the other's
receipts (and hand run.py's log reader a torn line).

Output: DATA/OUTPUTS/<as-of>/narratives.json, one object keyed by phase:
  {phase: {text, mode, decided_by, checks, attempts, fallbacks, ...}}
Re-running for a subset of phases merges into the existing file, so the
stepper can narrate each phase as it completes without losing earlier ones.

CLI:
    python -m pipeline.narrate --as-of 2026-05-04
    python -m pipeline.narrate --as-of 2026-05-04 --llm-mode template
    python -m pipeline.narrate --as-of 2026-05-04 --phases validation,signals --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from . import ask, config, harness, runctx, verdicts

PROMPT_FILE = config.PROMPTS_DIR / "phase-note.md"
MODES = verdicts.MODES  # ("claude-cli", "api", "template")

PHASES = ("validation", "signals", "verdicts", "digest")

PHASE_TITLES = {
    "validation": "Data Validation & Check",
    "signals": "Signal engine",
    "verdicts": "Claimed vs. Verified",
    "digest": "Digest assembly",
}

# Which prior phases feed each phase's prompt context (pipeline order).
PRIOR_PHASES = {
    "validation": (),
    "signals": ("validation",),
    "verdicts": ("validation", "signals"),
    "digest": ("validation", "signals", "verdicts"),
}

# Artifact labels per phase; required ones must exist for the phase to narrate.
PHASE_REQUIRED = {
    "validation": ("validation/report.json",),
    "signals": ("signals.json",),
    "verdicts": ("verdicts.json",),
    "digest": ("digest.json",),
}
PHASE_OPTIONAL = {
    "validation": ("validation/corrections_proposed.json", "DATA/TRANSLATION/MANIFEST.json"),
    "signals": (),
    "verdicts": ("facts.json",),
    "digest": ("ledger",),
}

ARTIFACT_DESCRIPTIONS = {
    "validation/report.json": "Data Validation & Check — every check that ran, per table, and what it found",
    "validation/corrections_proposed.json": "the corrections proposed for a human accept/decline decision",
    "DATA/TRANSLATION/MANIFEST.json": "the accepted/declined correction decisions and the corrected working copies they produced",
    "signals.json": "ranked signals, suppressed signals (with reasons), and every engine parameter",
    "facts.json": "the deterministic fact row for each judged action plan (baselines, targets, 4-week actuals)",
    "verdicts.json": "Claimed vs. Verified — reported status vs. what the numbers say vs. the AI verdict, per plan",
    "digest.json": "the assembled Monday digest: top signals with recommendations, verdicts, ledger, data checks",
    "ledger": "this run's recommendation-ledger events (re-checks and newly created recommendations)",
}

# Where each artifact label really lives, repo-relative: the `path` half of
# every receipt. "{as_of}" is filled from the bundle.
ARTIFACT_PATHS = {
    # The per-run snapshot, not the shared working copy: a receipt has to point
    # at the file this Monday's numbers actually came from, and the working copy
    # belongs to whichever run validated last. See validation_source().
    "validation/report.json": "DATA/OUTPUTS/{as_of}/validation/report.json",
    "validation/corrections_proposed.json": "DATA/OUTPUTS/{as_of}/validation/corrections_proposed.json",
    "DATA/TRANSLATION/MANIFEST.json": "DATA/OUTPUTS/{as_of}/validation/MANIFEST.json",
    "signals.json": "DATA/OUTPUTS/{as_of}/signals.json",
    "facts.json": "DATA/OUTPUTS/{as_of}/facts.json",
    "verdicts.json": "DATA/OUTPUTS/{as_of}/verdicts.json",
    "digest.json": "DATA/OUTPUTS/{as_of}/digest.json",
    "ledger": "DATA/OUTPUTS/ledger_log.jsonl",
}

# Leader-facing display rules — enforced on every mode's output, not just
# requested in the prompt. Location IDs never reach a leader.
BANNED_ID_RE = re.compile(r"\b(?:PCC_|DVM_|AP_|REC-)\w*")
AUDIT_RE = re.compile(r"\baudit", re.IGNORECASE)

# The bubble says what was DECIDED, not how the machine decided it. Process
# vocabulary belongs in the engineering log and behind the receipts; a leader
# reading it on a Monday morning gets the finding.
PROCESS_WORDS = (
    "combination", "center-metric", "center metric", "checks ran", "harness",
    "attempt", "held back", "suppress",
    "near miss", "near-miss", "pool", "regenerat", "hallucinat", "token",
    "artifact", "pipeline ran", "sifted", "scored 121", "evaluated",
)
# Editorial colour is the tell that a model is writing ABOUT the run rather
# than reporting it. The numbers carry the emphasis; adjectives don't.
EVALUATIVE_WORDS = (
    "worrisome", "alarming", "impossible", "deliberately", "clearly", "sharp",
    "sharply", "striking", "notable", "notably", "significant", "significantly",
    "dramatic", "dramatically", "worrying", "concerning", "troubling", "healthy",
    "encouraging", "reassuring", "surprising", "surprisingly", "crucial",
    "critical", "important", "impressive", "worth a look", "worth attention",
    "big and reliable", "genuinely", "quietly", "carefully", "robust",
)

# Words that identify a metric in prose — reused from pipeline.ask so one
# alias table serves the question router and the verdicts rule check.
METRIC_ALIASES = ask.METRIC_QUERY_WORDS

# Verdict-bucket vocabulary for the verdicts rule check, in priority order:
# "Not working, claimed on track" is a NOT WORKING bullet, not an ON TRACK one.
BUCKET_PHRASES = (
    ("NOT WORKING", ("not working", "isn't working", "is not working", "going backwards")),
    ("ABANDONED", ("abandoned", "abandon", "untouched", "no update in", "overdue")),
    ("EXCEEDED", ("target already", "already met", "already beaten", "already beat",
                  "exceeded", "beat their target", "beat its target", "close them",
                  "close with credit", "target met")),
    ("ON TRACK", ("on track",)),
)


# ---------------------------------------------------------------------------
# Bundle: one run's artifacts, selected per phase, plus trimmed prior context
# ---------------------------------------------------------------------------


def _read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def validation_source(as_of_str: str, name: str) -> Path:
    """Where this Monday's validation artifact really is.

    DATA/OUTPUTS/validation/ and DATA/TRANSLATION/ are the CURRENT working copy —
    the next Monday's run overwrites both. Each run also snapshots its own
    checks into DATA/OUTPUTS/<as_of>/validation/, so an older digest still
    resolves against the data it was actually built on. Prefer the snapshot;
    fall back to the working copy for a run mid-flight, which has not snapshotted
    yet and whose working copy IS its own.
    """
    snapshot = config.OUTPUTS_DIR / as_of_str / "validation" / name
    if snapshot.exists():
        return snapshot
    if name == "MANIFEST.json":
        return config.DATA_DIR / "MANIFEST.json"
    return config.OUTPUTS_DIR / "validation" / name


def _load_raw(as_of_str: str) -> dict:
    run_dir = config.OUTPUTS_DIR / as_of_str
    return {
        "validation/report.json": _read_json(validation_source(as_of_str, "report.json")),
        "validation/corrections_proposed.json": _read_json(
            validation_source(as_of_str, "corrections_proposed.json")
        ),
        "DATA/TRANSLATION/MANIFEST.json": _read_json(validation_source(as_of_str, "MANIFEST.json")),
        "signals.json": _read_json(run_dir / "signals.json"),
        "facts.json": _read_json(run_dir / "facts.json"),
        "verdicts.json": _read_json(run_dir / "verdicts.json"),
        "digest.json": _read_json(run_dir / "digest.json"),
        "ledger": {"events": ask._ledger_events_for_run(as_of_str)},
    }


def _trim_validation(raw: dict) -> dict | None:
    rep = raw.get("validation/report.json")
    if not rep:
        return None
    out: dict = {"totals": rep.get("totals")}
    flagged = []
    for table, checks in (rep.get("tables") or {}).items():
        for c in checks:
            if c.get("result") == "flag":
                flagged.append(
                    {
                        "table": table,
                        "check": c.get("check"),
                        "summary": c.get("summary"),
                        "counts": c.get("counts"),
                    }
                )
    out["flagged_checks"] = flagged
    man = raw.get("DATA/TRANSLATION/MANIFEST.json")
    if man:
        out["corrections_accepted"] = man.get("corrections_accepted", [])
        out["corrections_declined"] = man.get("corrections_declined", [])
    return out


def _trim_signals(raw: dict) -> dict | None:
    s = raw.get("signals.json")
    if not s:
        return None
    return {
        "counts": s.get("counts"),
        "latest_complete_week": s.get("latest_complete_week"),
        "ranked": [
            {
                "rank": g.get("rank"),
                "center": g.get("center"),
                "metric_display": g.get("metric_display"),
                "priority": g.get("priority"),
                "headline": g.get("headline"),
            }
            for g in s.get("signals", [])
        ],
        "suppressed": [
            {
                "center": g.get("center"),
                "metric_display": g.get("metric_display"),
                "rule": g.get("rule"),
                "would_be_priority": g.get("would_be_priority"),
                "reason": g.get("reason"),
            }
            for g in s.get("suppressed", [])
        ],
    }


def _trim_verdicts(raw: dict) -> dict | None:
    v = raw.get("verdicts.json")
    if not v:
        return None
    return {
        "counts": v.get("counts"),
        "verdicts": [
            {
                "plan_id": r.get("plan_id"),
                "center": r.get("center"),
                "metric_display": r.get("metric_display"),
                "reported": (r.get("reported_status") or {}).get("display"),
                "verdict": (r.get("ai_verdict") or {}).get("verdict_display"),
                "sentence": (r.get("ai_verdict") or {}).get("sentence"),
            }
            for r in v.get("verdicts", [])
        ],
    }


def _trim_digest(raw: dict) -> dict | None:
    """What digest assembly returned, for the run context. The digest itself is
    large; what the history needs is what the ledger did with it."""
    d = raw.get("digest.json")
    if not d:
        return None
    ledger = d.get("ledger") or {}
    # The digest's own key names: the ranked items are `top_signals`, and the
    # plan verdicts are the rows of `claimed_vs_verified`. Reading "signals"
    # and "verdicts" here handed the agent 0 and 0 for a run that carried 3
    # and 10 — a tool result that contradicted the artifact it summarized.
    cvv = d.get("claimed_vs_verified") or {}
    return {
        "leader": d.get("leader"),
        "sections": sorted(k for k in d.keys() if isinstance(d.get(k), (list, dict))),
        "ledger_counts": ledger.get("counts") if isinstance(ledger, dict) else None,
        "signal_count": len(d.get("top_signals") or []),
        "verdict_count": len(cvv.get("rows") or []) if isinstance(cvv, dict) else 0,
        "suppressed_count": len(d.get("suppressed") or []),
    }


_TRIMMERS = {
    "validation": _trim_validation,
    "signals": _trim_signals,
    "verdicts": _trim_verdicts,
    "digest": _trim_digest,
}


def _tool_output(phase: str, bundle: dict) -> dict:
    """The structured output a phase's tool call returned — what gets appended
    to the run context, not the full artifact."""
    out = (bundle.get("tool_outputs") or {}).get(phase)
    return out if out is not None else {"phase": phase, "note": "no structured output"}


def _facts_for_verdicts(raw: dict):
    """facts.json trimmed to the plans that were actually judged, so the
    verdicts narrative is grounded in the leader's plans, not all 28."""
    f = raw.get("facts.json")
    if not f:
        return None
    v = raw.get("verdicts.json")
    if not v:
        return f
    ids = {r.get("plan_id") for r in v.get("verdicts", [])}
    doc = {k: val for k, val in f.items() if k != "plans"}
    doc["plans"] = [p for p in f.get("plans", []) if p.get("plan_id") in ids]
    return doc


def load_bundle(as_of: date, phases: tuple[str, ...] | list[str]) -> dict:
    """Load one run's artifacts and assemble, per requested phase, exactly the
    artifacts that phase narrates from plus the trimmed prior-phase context.
    Raises SystemExit naming what is missing when a phase cannot be narrated."""
    as_of_str = str(as_of)
    raw = _load_raw(as_of_str)

    missing = []
    for phase in phases:
        for label in PHASE_REQUIRED[phase]:
            if raw.get(label) is None:
                missing.append(f"{phase}: {label}")
    if missing:
        raise SystemExit(
            f"Cannot narrate {as_of_str} — missing artifacts: {'; '.join(missing)}. "
            f"Run the pipeline for that Monday first (python -m pipeline.run --as-of {as_of_str})."
        )

    leader = latest_week = None
    for label in ("digest.json", "verdicts.json", "signals.json", "facts.json"):
        doc = raw.get(label)
        if isinstance(doc, dict):
            leader = leader or doc.get("leader")
            latest_week = latest_week or doc.get("latest_complete_week")

    bundle = {"as_of": as_of_str, "leader": leader, "latest_complete_week": latest_week, "phases": {}}
    for phase in phases:
        artifacts: dict = {}
        for label in PHASE_REQUIRED[phase] + PHASE_OPTIONAL[phase]:
            value = _facts_for_verdicts(raw) if (phase, label) == ("verdicts", "facts.json") else raw.get(label)
            if value is not None:
                artifacts[label] = value
        prior_context = {}
        for prior in PRIOR_PHASES[phase]:
            trimmed = _TRIMMERS[prior](raw)
            if trimmed is not None:
                prior_context[prior] = trimmed
        bundle["phases"][phase] = {"artifacts": artifacts, "prior_context": prior_context}

    # Each phase's structured tool output, for the run context. Computed for
    # every phase that produced artifacts, not only the requested ones, so a
    # resumed run can rebuild the history it missed.
    bundle["tool_outputs"] = {
        phase: trimmed
        for phase, trimmer in _TRIMMERS.items()
        if (trimmed := trimmer(raw)) is not None
    }
    return bundle


# ---------------------------------------------------------------------------
# The verification pool — every value a phase narrative may cite
# ---------------------------------------------------------------------------


def _add_collection_sizes(obj, numbers: set[float]) -> None:
    """A narrative may count what the artifacts list ("3 signals ranked",
    "4 corrections accepted"), so every collection's size joins the pool."""
    if isinstance(obj, dict):
        for value in obj.values():
            _add_collection_sizes(value, numbers)
    elif isinstance(obj, (list, tuple)):
        numbers.add(float(len(obj)))
        for value in obj:
            _add_collection_sizes(value, numbers)


def build_pool(bundle: dict, phase: str) -> tuple[set[float], set[str]]:
    """Every number and date in this phase's artifacts AND its trimmed prior
    context — the same material pasted into the prompt, so a cross-phase claim
    ("7 relabeled centers changed the peer groups") verifies too."""
    ph = bundle["phases"][phase]
    scope = {"artifacts": ph["artifacts"], "prior_context": ph["prior_context"]}
    numbers, dates = harness.collect_pool(scope)
    _add_collection_sizes(scope, numbers)
    numbers.update({0.0, 100.0})
    dates.add(bundle["as_of"])
    if bundle.get("latest_complete_week"):
        dates.add(str(bundle["latest_complete_week"]))
    return numbers, dates


# ---------------------------------------------------------------------------
# The receipt map — where every number and name in a bubble came from
# ---------------------------------------------------------------------------
#
# The pool above answers "does this number exist in the artifacts?". The index
# below answers the question a reader actually asks: "where?" — file, field,
# value. It is built from the same artifacts the prompt carried, so a receipt
# is a pointer a reviewer can follow by hand.

# Field names whose string value is a center name (the only proper nouns a
# narrative may use — real center names only, never location IDs).
NAME_FIELDS = {"center", "location_name", "center_name", "centers"}
# Field names whose string value is a person/label a narrative may echo.
VOCAB_FIELDS = {"leader", "owner", "rmp_name", "peer_group", "title", "step"}

CAP_WORD = r"[A-Z][A-Za-z'’\-]*"
CAP_SEQ_RE = re.compile(rf"{CAP_WORD}(?:\s+{CAP_WORD})*")

# Ordinary words that are capitalized by grammar or by the calendar, not by
# being a name. Everything else that looks like a proper name has to come from
# the artifacts — that is what stops an invented center from slipping through.
BASE_VOCAB = {
    "A", "An", "The", "This", "That", "These", "Those", "It", "Its", "They", "We",
    "And", "But", "Or", "No", "Not", "Nothing", "None", "All", "Both", "Each",
    "Every", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
    "Nine", "Ten", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December", "Dr", "Data",
    "Validation", "Check", "Signal", "Signals", "Digest", "Ledger", "Claimed",
    "Verified", "Target", "On", "Track", "Abandoned", "Exceeded", "Working",
    "Flagged", "Fixed", "Peer", "Standard", "Plans", "Plan", "Center", "Centers",
}


# Fields whose digits are machinery, not findings: run timestamps, file
# hashes, paths. They still feed the number POOL (harness.collect_pool sees
# the whole artifact) — they just never win a receipt, because "the 2 in
# 02:59:47" is not where a leader's number came from.
NOISY_FIELDS = {"ts", "sha256", "reference", "path", "file", "step", "id", "rec_id"}
# Objects whose numbers ARE the finding: a receipt points here before it points
# at the same digits sitting somewhere incidental.
HEADLINE_PARENTS = ("counts", "totals", "what_the_numbers_say", "scores",
                    "peer_context", "reported_status")

# Field-name words that say nothing about which value this is.
GENERIC_FIELD_WORDS = {"value", "values", "display", "length", "details", "counts",
                       "count", "json", "data", "row", "rows", "level", "pct"}

# Receipt quality, best first: a headline value beats an incidental one, which
# beats digits found inside a sentence, which beats "this list is 3 long".
RANK_HEADLINE, RANK_VALUE, RANK_IN_TEXT, RANK_LENGTH = 0, 1, 2, 3


def _noisy_field(leaf: str) -> bool:
    """True for fields whose digits are machinery (run timestamps, hashes,
    file paths) rather than a finding."""
    return leaf in NOISY_FIELDS or leaf.endswith(("_at", "_file", "_path"))


def _field_context_hits(field: str, context: str) -> int:
    """How much of a field's own name the LINE citing it uses — the cheap way
    to tell which of several fields holding the same number a bullet means
    ("1 working" is ledger.counts.outcome_working, not a score's scale of 1.0).

    Only the field's own name counts: the objects around it are too broad to
    be evidence — a bullet that says "peer median" would otherwise pull any
    number under peer_context, including one that merely coincides."""
    if not context:
        return 0
    leaf = field.lower().split(".")[-1].split("[")[0]
    words = {w for w in re.split(r"[_\s]+", leaf) if len(w) >= 4} - GENERIC_FIELD_WORDS
    return sum(1 for w in words if w in context)


def _value_rank(field: str) -> int:
    parts = [p.split("[")[0] for p in field.split(".")]
    return RANK_HEADLINE if any(p in HEADLINE_PARENTS for p in parts[:-1]) else RANK_VALUE


class ArtifactIndex:
    """Every value in one phase's world, with the path and field it came from.

    Insertion order is provenance order: the phase's OWN artifacts are indexed
    first, so a value that appears in several files is credited to the file the
    phase is actually narrating. Within one value, the clearest field wins.
    """

    def __init__(self):
        # Every (value, where-it-came-from) pair, not one per value: several
        # fields can hold the same number, and which one a bullet MEANS is
        # decided when the text exists (see number_receipt).
        self.numbers: list[tuple[float, dict, int]] = []
        self._number_seen: set[tuple[float, str, str]] = set()
        self.dates: dict[str, dict] = {}
        self.names: dict[str, dict] = {}  # lowercased center name -> receipt
        self._name_rank: dict[str, int] = {}
        self.vocab: set[str] = set()      # capitalized words the artifacts use
        self.paths: list[str] = []

    def add_number(self, value: float, path: str, field: str, shown, rank: int = RANK_VALUE) -> None:
        key = (value, path, field)
        if key in self._number_seen:
            return
        self._number_seen.add(key)
        self.numbers.append((value, {"path": path, "field": field, "value": shown}, rank))

    def add_date(self, value: str, path: str, field: str) -> None:
        self.dates.setdefault(value, {"path": path, "field": field, "value": value})

    def add_name(self, value: str, path: str, field: str, rank: int = 0) -> None:
        key = value.lower()
        if key not in self.names or rank < self._name_rank.get(key, 9):
            self.names[key] = {"path": path, "field": field, "value": value}
            self._name_rank[key] = rank

    def number_receipt(self, token: str, context: str = "") -> dict | None:
        """The best provenance for a numeric token: the closest indexed value
        the number check would accept; then the field the sentence is actually
        talking about ("1 working" belongs to ledger.counts.outcome_working, not
        to the same 1.0 sitting in a score scale); then the clearest field, and
        the earliest artifact."""
        target = float(token.replace(",", "").replace("−", "-"))
        decimals = len(token.split(".")[1]) if "." in token else 0
        tol = 0.5 * 10**-decimals + 1e-9
        best = None
        for order, (value, receipt, rank) in enumerate(self.numbers):
            delta = min(abs(value - target), abs(abs(value) - target), abs(-value - target))
            if delta > tol:
                continue
            key = (round(delta, 9), -_field_context_hits(receipt["field"], context), rank, order)
            if best is None or key < best[0]:
                best = (key, receipt)
        return best[1] if best else None


def _index_string(text: str, path: str, field: str, index: ArtifactIndex, noisy: bool) -> None:
    for match in CAP_SEQ_RE.finditer(text):
        for word in match.group(0).split():
            index.vocab.add(word)
    for d in harness.DATE_RE.findall(text):
        index.add_date(d, path, field)
    if noisy:
        return
    cleaned = harness.DATE_RE.sub(" ", harness.ID_TOKEN_RE.sub(" ", text))
    shown = text if len(text) <= 120 else text[:117] + "…"
    for token in harness.NUM_RE.findall(cleaned):
        index.add_number(
            float(token.replace(",", "").replace("−", "-")), path, field, shown, RANK_IN_TEXT
        )


def _index_walk(obj, path: str, field: str, index: ArtifactIndex) -> None:
    if obj is None or isinstance(obj, bool):
        return
    leaf = field.split(".")[-1].split("[")[0]
    noisy = _noisy_field(leaf)
    if isinstance(obj, dict):
        for key, value in obj.items():
            _index_walk(value, path, f"{field}.{key}" if field else key, index)
        return
    if isinstance(obj, (list, tuple)):
        # A narrative may cite how many things a list holds ("3 signals"), so
        # the length is a receipt too — pointing at the list, not at a value.
        index.add_number(
            float(len(obj)), path, f"{field}.length" if field else "length", len(obj), RANK_LENGTH
        )
        for i, value in enumerate(obj):
            _index_walk(value, path, f"{field}[{i}]", index)
        return
    if isinstance(obj, (int, float)):
        if not noisy:
            index.add_number(float(obj), path, field, obj, _value_rank(field))
        return
    if isinstance(obj, str):
        if leaf in NAME_FIELDS and obj and obj[0].isupper():
            # A name on the row being narrated beats the same name in the
            # run's roster of every center.
            index.add_name(obj, path, field, rank=1 if field.startswith("centers") else 0)
        if leaf in VOCAB_FIELDS:
            for word in obj.split():
                index.vocab.add(word.strip(".,;:()"))
        _index_string(obj, path, field, index, noisy)


def visible_labels(phase: str) -> list[str]:
    """The artifact labels a phase's prompt carries: its own, then the prior
    phases' (whose trimmed context rides along)."""
    labels: list[str] = []
    for label in PHASE_REQUIRED[phase] + PHASE_OPTIONAL[phase]:
        if label not in labels:
            labels.append(label)
    for prior in PRIOR_PHASES[phase]:
        for label in PHASE_REQUIRED[prior] + PHASE_OPTIONAL[prior]:
            if label not in labels:
                labels.append(label)
    return labels


def build_index(bundle: dict, phase: str, raw: dict | None = None) -> ArtifactIndex:
    """Index every artifact this phase can see, own artifacts first."""
    raw = raw if raw is not None else _load_raw(bundle["as_of"])
    index = ArtifactIndex()
    own = set(PHASE_REQUIRED[phase] + PHASE_OPTIONAL[phase])
    for label in visible_labels(phase):
        doc = bundle["phases"][phase]["artifacts"].get(label, raw.get(label))
        if doc is None:
            continue
        path = ARTIFACT_PATHS[label].format(as_of=bundle["as_of"])
        if label in own:
            index.paths.append(path)
        _index_walk(doc, path, "", index)
    if bundle.get("leader"):
        for word in str(bundle["leader"]).split():
            index.vocab.add(word.strip(".,;:()"))
    return index


def collect_receipts(text: str, index: ArtifactIndex) -> list[dict]:
    """One receipt per number, date and center name the bubble uses:
    {token, path, field, value}. Only tokens the checks already accepted get
    here, so a receipt is never an excuse for an unverified figure."""
    receipts: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(token: str, receipt: dict | None) -> None:
        # Keyed by token AND field: one bullet can say "1 working" while the
        # next says "1 new recommendation created", and those two 1s are
        # different facts. Deduping on the token alone bound every later 1 to
        # the first bullet's field, which is the lucky-match provenance this
        # function exists to prevent.
        if receipt is None:
            return
        key = (token, receipt["field"])
        if key in seen:
            return
        seen.add(key)
        receipts.append({"token": token, **receipt})

    # Provenance is decided line by line: the bullet a number sits in is the
    # sentence that means it, so "3 new recommendations" is not resolved by a
    # word three bullets away.
    for line in text.splitlines():
        context = line.lower()
        cleaned = harness.ID_TOKEN_RE.sub(" ", line)
        for token in harness.DATE_RE.findall(cleaned):
            add(token, index.dates.get(token))
        stripped = harness.DATE_RE.sub(" ", cleaned)
        for token in harness.NUM_RE.findall(stripped):
            add(token, index.number_receipt(token, context))
    for name, receipt in index.names.items():
        if re.search(rf"\b{re.escape(receipt['value'])}\b", text):
            add(receipt["value"], receipt)
    return receipts


# ---------------------------------------------------------------------------
# The checks — language, entities, phase rules, shape
# ---------------------------------------------------------------------------


def check_leader_language(text: str) -> dict:
    """The leader-facing display and voice rules, enforced deterministically on
    every mode: no internal IDs, no raw metric ids, never the word "audit", no
    process narration, no editorial adjectives."""
    problems: list[str] = []
    ids = sorted(set(BANNED_ID_RE.findall(text)))
    if ids:
        problems.append("internal IDs must never reach a leader: " + ", ".join(ids))
    if AUDIT_RE.search(text):
        problems.append('the data-quality step is called "Data Validation & Check" — never "audit"')
    lowered = text.lower()
    raw_ids = sorted(k for k in config.METRICS if k in lowered)
    if raw_ids:
        problems.append("raw metric ids must be written in plain English: " + ", ".join(raw_ids))
    process = sorted({w for w in PROCESS_WORDS if w in lowered})
    if process:
        problems.append(
            "how the machine worked belongs in the log, not in the bubble — remove: "
            + ", ".join(process)
        )
    evaluative = sorted({w for w in EVALUATIVE_WORDS if re.search(rf"\b{re.escape(w)}\b", lowered)})
    if evaluative:
        problems.append(
            "no editorial adjectives — the numbers carry the emphasis; remove: "
            + ", ".join(evaluative)
        )
    return {"ok": not problems, "problems": problems}


def check_entities(text: str, index: ArtifactIndex) -> dict:
    """Every proper name in a bubble must be a center this run's artifacts
    actually name (or vocabulary those artifacts use). A center the run never
    saw is a hallucination the number check cannot catch."""
    problems: list[str] = []
    verified: list[str] = []
    ids = sorted(set(BANNED_ID_RE.findall(text)))
    if ids:
        problems.append("internal IDs are never center names: " + ", ".join(ids))

    # Credit every center the run really names, wherever it appears — including
    # inside a longer capitalized run ("Dilworth CSAT").
    for name, receipt in index.names.items():
        if re.search(rf"\b{re.escape(receipt['value'])}\b", text):
            verified.append(receipt["value"])

    known = set(index.vocab) | BASE_VOCAB
    unknown: list[str] = []
    for match in CAP_SEQ_RE.finditer(text):
        candidate = match.group(0).strip()
        if candidate.lower() in index.names:
            continue
        words = candidate.split()
        # One capitalized word is grammar (a sentence or a bullet starting), not
        # a name claim. Two or more in a row is a name, and it has to be real.
        if len(words) < 2:
            continue
        if all(w in known for w in words):
            continue
        unknown.append(candidate)
    if unknown:
        problems.append(
            "these names are not in this run's artifacts (only real center names may appear): "
            + ", ".join(sorted(set(unknown)))
        )
    return {"ok": not problems, "problems": problems, "verified": sorted(set(verified))}


def _bullets(text: str) -> list[str]:
    return [ln.strip()[1:].strip() for ln in text.splitlines() if ln.strip().startswith("•")]


def _bucket_of(line: str) -> str | None:
    lowered = line.lower()
    for bucket, phrases in BUCKET_PHRASES:
        if any(p in lowered for p in phrases):
            return bucket
    return None


def check_phase_rules(phase: str, text: str, bundle: dict) -> dict:
    """Per-phase rules the artifacts themselves decide.

    signals  — every ranked signal's center is named; a center that only ever
               appears in the suppressed list is never named (a suppressed
               signal is not a finding, and saying it was held back is process
               narration).
    verdicts — a plan named under a bucket must BE in that bucket. The
               deterministic verdict table is the authority; the sentence
               cannot quietly re-file a plan.
    validation — a center named beside a maturity tier must have been moved to
               THAT tier. The relabel bullet is the one place the validation
               narrative names centers, and grouping them wrongly ("Daniel
               Island → ramping" when the correction moved it to new) is a
               factual error every other check would pass: the names are real
               and the counts exist. The correction's own details decide.
    """
    problems: list[str] = []
    artifacts = bundle["phases"][phase]["artifacts"]

    if phase == "signals":
        s = artifacts.get("signals.json") or {}
        ranked = s.get("signals", [])
        ranked_centers = {g.get("center") for g in ranked if g.get("center")}
        missing = sorted(c for c in ranked_centers if not re.search(rf"\b{re.escape(c)}\b", text))
        if missing:
            problems.append(
                "every ranked signal's center has to be named: missing " + ", ".join(missing)
            )
        suppressed_only = {
            g.get("center") for g in s.get("suppressed", []) if g.get("center")
        } - ranked_centers
        named = sorted(c for c in suppressed_only if re.search(rf"\b{re.escape(c)}\b", text))
        if named:
            problems.append(
                "these centers were not flagged this week and must not appear: " + ", ".join(named)
            )

    if phase == "validation":
        proposed = artifacts.get("validation/corrections_proposed.json") or {}
        moved: dict[str, str] = {}
        for corr in proposed.get("corrections", []):
            for det in corr.get("details") or []:
                name, tier = det.get("location_name"), det.get("computed_tier")
                if name and tier:
                    moved[name] = tier
        if moved:
            tiers = sorted({t.lower() for t in moved.values()})
            tier_rx = re.compile(rf"\b({'|'.join(tiers)})\b", re.I)

            def _tier_of_chunk(chunk: str, tier: str) -> None:
                for name, real in moved.items():
                    if real != tier and re.search(rf"\b{re.escape(name)}\b", chunk):
                        problems.append(f"{name} was relabeled {real}, not {tier}")

            for line in _bullets(text):
                if not tier_rx.search(line):
                    continue
                if "→" in line or "->" in line:
                    # "A, B → ramping; C → new" — the names precede their tier.
                    for clause in re.split(r"[;·]", line):
                        head, sep, tail = clause.partition("→")
                        if not sep:
                            head, sep, tail = clause.partition("->")
                        m = tier_rx.search(tail) if sep else None
                        if m:
                            _tier_of_chunk(head, m.group(1).lower())
                else:
                    # "ramping centers (A, B) and new centers (C)" — the names
                    # follow their tier, up to wherever the next tier begins.
                    hits = list(tier_rx.finditer(line))
                    for i, m in enumerate(hits):
                        stop = hits[i + 1].start() if i + 1 < len(hits) else len(line)
                        _tier_of_chunk(line[m.end():stop], m.group(1).lower())

    if phase == "verdicts":
        rows = (artifacts.get("verdicts.json") or {}).get("verdicts", [])
        plans_per_center: dict[str, int] = {}
        for row in rows:
            plans_per_center[row.get("center")] = plans_per_center.get(row.get("center"), 0) + 1
        for line in _bullets(text):
            bucket = _bucket_of(line)
            if bucket is None:
                continue
            # A bullet lists several plans separated by · or ;. Each item is
            # judged on its own, so one plan's name cannot borrow another's
            # metric word ("Morrisville records" is not "Morrisville
            # throughput" just because both sit in the same line).
            body = line.split(":", 1)[1] if ":" in line else line
            for segment in re.split(r"[·;]", body):
                lowered = segment.lower()
                for row in rows:
                    center = row.get("center") or ""
                    if not center or not re.search(rf"\b{re.escape(center)}\b", segment):
                        continue
                    aliases = METRIC_ALIASES.get(row.get("metric"), ())
                    named_metric = any(a in lowered for a in aliases)
                    if not named_metric and plans_per_center.get(center, 0) > 1:
                        continue  # ambiguous: that center has more than one plan
                    actual = (row.get("ai_verdict") or {}).get("bucket")
                    if actual and actual != bucket:
                        problems.append(
                            f"{center} — {row.get('metric_display')} is {actual}, not {bucket}: "
                            f"\"{segment.strip()[:80]}\""
                        )
    return {"ok": not problems, "problems": sorted(set(problems))}


MAX_BULLETS = 8
MAX_WORDS = 110


def check_shape(text: str) -> dict:
    """One lead line, then bullets — the shape a leader can scan in seconds."""
    problems: list[str] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "problems": ["the narrative is empty"], "bullets": 0}
    lead, rest = lines[0], lines[1:]
    if lead.startswith("•"):
        problems.append("start with one lead line saying what ran, then the bullets")
    bullets = [ln for ln in rest if ln.startswith("•")]
    stray = [ln for ln in rest if not ln.startswith("•")]
    if stray:
        problems.append(
            "everything after the lead line must be a bullet starting with '• ': "
            + "; ".join(s[:60] for s in stray[:2])
        )
    if not bullets:
        problems.append("say what was decided or flagged as bullets — at least one")
    if len(bullets) > MAX_BULLETS:
        problems.append(f"at most {MAX_BULLETS} bullets; this has {len(bullets)}")
    words = len(text.split())
    if words > MAX_WORDS:
        problems.append(
            f"at most {MAX_WORDS} words; this has {words} — cut {words - MAX_WORDS}+ by using "
            f"the short metric names (records, rechecks, callbacks, CSAT, wait time, "
            f"throughput, no-shows) and dropping every word that is not a center, a metric, "
            f"a number or a verdict"
        )
    return {"ok": not problems, "problems": problems, "bullets": len(bullets)}


# ---------------------------------------------------------------------------
# LLM modes — prompt from PROMPTS/phase-note.md, through the checks
# ---------------------------------------------------------------------------


def load_prompt_template() -> str:
    if not PROMPT_FILE.exists():
        raise SystemExit(f"Prompt file missing: {PROMPT_FILE} (PROMPTS/ is the code path).")
    return PROMPT_FILE.read_text()


def _render_artifacts(artifacts: dict) -> str:
    blocks = []
    for label, obj in artifacts.items():
        desc = ARTIFACT_DESCRIPTIONS.get(label, "")
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        blocks.append(f"### {label} — {desc}\n```json\n{body}\n```")
    return "\n\n".join(blocks)


def render_prompt(template: str, phase: str, bundle: dict, retry_note: str | None) -> str:
    ph = bundle["phases"][phase]
    # The accumulated run history: what this run inherited, every tool result
    # before this one, and what the agent already wrote about them. This is the
    # only continuity there is — nothing is carried in model memory, so the
    # history has to be in the prompt to be in the reasoning.
    ctx = bundle.get("run_context")
    if ctx is not None:
        prior_block = ctx.render(upto_phase=phase)
    elif ph["prior_context"]:
        prior_block = "```json\n" + json.dumps(
            ph["prior_context"], ensure_ascii=False, separators=(",", ":")
        ) + "\n```"
    else:
        prior_block = "(none — this is the first phase of the run)"
    note = ""
    if retry_note:
        note = (
            "\n## Fix required — your previous narrative failed validation\n"
            f"{retry_note}\n"
            "Rewrite it so every number verifies against the artifacts and every rule above holds.\n"
        )
    return (
        template.replace("{{AS_OF}}", bundle["as_of"])
        .replace("{{LATEST_WEEK}}", str(bundle.get("latest_complete_week") or ""))
        .replace("{{LEADER}}", str(bundle.get("leader") or ""))
        .replace("{{PHASE}}", phase)
        .replace("{{PHASE_TITLE}}", PHASE_TITLES[phase])
        .replace("{{RUN_CONTEXT}}", prior_block)
        .replace("{{PRIOR_CONTEXT}}", prior_block)
        .replace("{{ARTIFACTS}}", _render_artifacts(ph["artifacts"]))
        .replace("{{RETRY_NOTE}}", note)
    )


BULLET_MARKERS = ("•", "-", "*", "·", "–", "—")


def parse_llm_narrative(text: str) -> str:
    """Extract the NARRATIVE: block — one lead line, then one line per bullet,
    every bullet normalized to "• ". A line that is neither joins the line
    before it (a wrapped bullet, or a lead that ran on). Raises ValueError when
    the reply carries no NARRATIVE: line at all."""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
    parts: list[str] = []
    started = False
    for raw in lines:
        s = raw.strip()
        if not started:
            if s.upper().startswith("NARRATIVE:"):
                started = True
                head = s[len("NARRATIVE:"):].strip()
                if head:
                    parts.append(head)
            continue
        if not s:
            continue
        if s[0] in BULLET_MARKERS:
            parts.append("• " + s[1:].strip())
        elif parts:
            parts[-1] = f"{parts[-1]} {s}"
        else:
            parts.append(s)
    narrative = "\n".join(p for p in parts if p).strip()
    if not narrative:
        raise ValueError("Reply contains no NARRATIVE: line.")
    return narrative


def call_llm(mode: str, prompt: str, on_delta=None) -> str:
    """One generation call through the shared failure classifier. The prompt
    carries whole artifacts, so the claude-cli transport sends it on stdin
    (pipeline.ask's transport), not argv.

    `on_delta`, when given, receives each chunk of text as the model writes it
    — the narration equivalent of pipeline.ask's streaming. It changes what a
    watching panel can SHOW; it changes nothing about what is returned or what
    the checks are run against."""
    if mode == "claude-cli":
        if on_delta is not None:
            return verdicts.call_guarded(
                mode, lambda p: ask.call_claude_cli_stream(p, on_delta), prompt
            )
        return verdicts.call_guarded(mode, ask.call_claude_cli, prompt)
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
# Template mode — deterministic narratives from artifact values
# ---------------------------------------------------------------------------


def _lower_first(text: str) -> str:
    return text[0].lower() + text[1:] if text else text


def _n(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _bubble(lead: str, bullets) -> str:
    """The bubble's shape: one lead line, then "• " bullets."""
    lines = [lead.strip()] + [f"• {b.strip()}" for b in bullets if b and b.strip()]
    return "\n".join(lines)


# Shorter than the artifacts' full metric names, but never shorter than the
# meaning. "records 78.1%" makes a reader ask "78.1% of what?" — the label has
# to survive being read once, by someone who has not seen the column. Every
# alias is one the verdicts rule check recognises.
SHORT_METRIC = {
    "record_completion_24h_pct": "record completion within 24h",
    "recheck_compliance_pct": "recheck compliance",
    "callback_compliance_pct": "callback compliance",
    "client_csat": "CSAT",
    "avg_wait_time_min": "average wait time",
    "appts_per_doctor_hour": "appointments per doctor-hour",
    "no_show_rate": "no-show rate",
    "staff_call_outs": "staff call-outs",
    "membership_conversion_pct": "membership conversion",
    "revenue_per_appt": "revenue per appointment",
    "open_dvm_requisitions": "open doctor job requisitions",
}


def _short_metric(metric_key: str | None, fallback: str = "") -> str:
    if metric_key in SHORT_METRIC:
        return SHORT_METRIC[metric_key]
    return _lower_first(fallback or str(metric_key or "").replace("_", " "))


def _level_phrase(metric_key: str | None, value_display: str, weeks) -> str:
    """How to say `recent_level`, which is the MEAN of the recent weekly values.

    Counts are the case that misleads: "staff call-outs 5.5 over the last 4
    weeks" reads as 5.5 call-outs in the whole month. It is 5.5 every week.
    """
    metric = config.METRICS.get(metric_key or "")
    per_week = " a week" if metric is not None and metric.kind == "count" else ""
    window = f" over the last {weeks} weeks" if weeks else ""
    return f"averaging {value_display}{per_week}{window}"


def _fmt_metric(metric_key: str | None, value) -> str:
    """Format a metric value the way the artifacts display it (pipeline.signals'
    rules, without the numpy import)."""
    if value is None:
        return "n/a"
    value = float(value)
    metric = config.METRICS.get(metric_key or "")
    kind = metric.kind if metric else "count"
    if metric_key == "revenue_per_appt":
        return f"${value:,.0f}"
    if kind == "percentage":
        return f"{value:.1f}%"
    if kind == "csat":
        return f"{value:.2f} / 5"
    if kind == "throughput":
        return f"{value:.2f}"
    if kind == "wait":
        return f"{value:.1f} min"
    return f"{value:.0f}" if value.is_integer() else f"{value:.1f}"


def _plain(text: str) -> str:
    """Raw column ids never reach a leader; swap any metric id for its name."""
    out = str(text)
    for key, metric in config.METRICS.items():
        out = out.replace(key, _lower_first(metric.display_name))
    return out


def _correction_bullet(correction: dict, report: dict) -> str:
    """One accepted correction, in the pipeline's own verbs. The description in
    the manifest is written for an engineer; this is the same fact for a
    leader, with the centers named where the correction touched centers."""
    desc = str(correction.get("description", "")).lower()
    rows = correction.get("rows_affected", correction.get("affected_rows"))
    if "duplicate" in desc:
        return f"dropped {rows} duplicate clinic-weeks"
    if "negative" in desc:
        return f"set {rows} negative wait times to missing"
    if "employment_type" in desc or "capitaliz" in desc or "lowercase" in desc:
        return f"normalized {rows} employment_type values to full_time / part_time / relief"
    if "maturity" in desc:
        by_tier: dict[str, list[str]] = {}
        for table_checks in (report.get("tables") or {}).values():
            for check in table_checks:
                if "maturity_tier_vs_opened_date" not in str(check.get("check", "")):
                    continue
                for row in check.get("details", []):
                    by_tier.setdefault(row.get("computed_tier", "?"), []).append(
                        row.get("location_name", "")
                    )
        groups = "; ".join(
            f"{', '.join(names)} → {tier}" for tier, names in by_tier.items() if names
        )
        head = f"relabeled maturity tier for {rows} centers whose label contradicted their opened date"
        return f"{head}: {groups}" if groups else head
    return _plain(f"{correction.get('description', '')}").rstrip(".").lower()


def _template_validation(bundle: dict) -> str:
    artifacts = bundle["phases"]["validation"]["artifacts"]
    report = artifacts["validation/report.json"]
    totals = report.get("totals", {})
    man = artifacts.get("DATA/TRANSLATION/MANIFEST.json")

    if not man:
        proposed = (artifacts.get("validation/corrections_proposed.json") or {}).get(
            "corrections", []
        )
        lead = (
            f"Data checked. {totals.get('corrections_proposed')} corrections are proposed and "
            f"waiting on your decision:"
        )
        return _bubble(lead, [_correction_bullet(c, report) for c in proposed])

    accepted = man.get("corrections_accepted", [])
    declined = man.get("corrections_declined", [])

    # A correction decided on an earlier Monday is not a new decision. Saying so
    # is the point: the leader ruled once and the ruling held, including for the
    # rows that arrived since.
    carried = [c for c in accepted if c.get("decision_status") == "carried"]
    new_rows = sum(int(c.get("new_rows") or 0) for c in carried)
    decided_on = next((c.get("decided_as_of") for c in carried if c.get("decided_as_of")), None)

    if carried and len(carried) == len(accepted) and decided_on:
        lead = (
            f"Data checked. Nothing new to decide — the {len(carried)} corrections "
            f"decided on {decided_on} carried into this week:"
        )
    else:
        lead = f"Data checked. The deterministic pass made {len(accepted)} corrections:"
    bullets = [_correction_bullet(c, report) for c in accepted]
    if new_rows:
        bullets.append(f"{new_rows} of those rows arrived in the week just closed")
    if declined:
        bullets.append(
            f"{len(declined)} proposed {_n(len(declined), 'correction was', 'corrections were')} "
            f"declined and left as they were"
        )
    if not bullets:
        bullets = ["nothing needed correcting — the four tables came in clean"]
    return _bubble(lead, bullets)


def _template_signals(bundle: dict) -> str:
    s = bundle["phases"]["signals"]["artifacts"]["signals.json"]
    ranked = s.get("signals", [])
    leader = s.get("leader") or bundle.get("leader") or "the leader"
    lead = f"Signals ran against this week's data. Flagged for {leader}:"
    if not ranked:
        return _bubble(lead, ["nothing crossed this week's thresholds at any center"])

    bullets = []
    for g in ranked:
        drift = (g.get("scores") or {}).get("drift") or {}
        peer = g.get("peer_context") or {}
        metric_key = g.get("metric")
        level = _level_phrase(
            metric_key,
            g["recent_level_display"],
            drift.get("recent_weeks") if drift.get("available") else None,
        )
        part = f"{g['center']} — {_short_metric(metric_key, g.get('metric_display'))} {level}"
        if drift.get("available"):
            part += (
                f" vs its {drift.get('baseline_weeks')}-week norm of "
                f"{_fmt_metric(metric_key, drift.get('baseline_level'))}"
            )
        if peer.get("peer_median_display") is not None:
            part += f"; peer median {peer['peer_median_display']} among {peer.get('peer_group')}"
        bullets.append(part)
    return _bubble(lead, bullets)


def _template_verdicts(bundle: dict) -> str:
    v = bundle["phases"]["verdicts"]["artifacts"]["verdicts.json"]
    rows = v.get("verdicts", [])
    plans = (v.get("counts") or {}).get("plans", len(rows))
    lead = f"{plans} {_n(plans or 0, 'plan', 'plans')} verified against actuals."

    def by_bucket(bucket):
        return [r for r in rows if (r.get("ai_verdict") or {}).get("bucket") == bucket]

    def named(row):
        return f"{row.get('center')} {_short_metric(row.get('metric'), row.get('metric_display'))}"

    def movement(row):
        nums = row.get("what_the_numbers_say") or {}
        return f"{named(row)} {nums.get('baseline_display')} → {nums.get('actual_4wk_display')}"

    bullets = []
    for row in by_bucket("NOT WORKING"):
        claimed = (row.get("reported_status") or {}).get("display", "")
        claim = f", claimed {_lower_first(claimed)}" if claimed else ""
        bullets.append(f"Not working{claim}: {movement(row)}")
    for row in by_bucket("ABANDONED"):
        nums = row.get("what_the_numbers_say") or {}
        stale = (row.get("reported_status") or {}).get("staleness_days")
        bullets.append(
            f"Abandoned: {named(row)} — {nums.get('days_overdue')} days past due, "
            f"no update in {stale} days"
        )
    exceeded = by_bucket("EXCEEDED")
    if exceeded:
        bullets.append(
            "Target already met, close them: " + " · ".join(named(r) for r in exceeded)
        )
    on_track = by_bucket("ON TRACK")
    if on_track:
        bullets.append("On track: " + " · ".join(named(r) for r in on_track))
    if not bullets:
        bullets = ["no plans were open on this leader's centers"]
    return _bubble(lead, bullets)


def _template_digest(bundle: dict) -> str:
    d = bundle["phases"]["digest"]["artifacts"]["digest.json"]
    counts = (d.get("ledger") or {}).get("counts", {})
    lead = f"Digest is ready for {d.get('leader') or bundle.get('leader') or 'the leader'}."

    bullets = []
    outcomes = []
    # Wording note: each number has to sit next to the leaf word of the counts
    # key it came from ("working" -> ledger.counts.outcome_working), because
    # the receipt map resolves provenance by leaf word. Phrasing that reads
    # well but breaks that link ships a number with the wrong receipt.
    if counts.get("outcome_working"):
        outcomes.append(f"{counts['outcome_working']} working")
    if counts.get("outcome_not_working"):
        outcomes.append(f"{counts['outcome_not_working']} not working")
    if counts.get("outcome_flat"):
        outcomes.append(f"{counts['outcome_flat']} flat")
    if counts.get("execution_unknown"):
        outcomes.append(
            f"execution unknown on {counts['execution_unknown']} — nobody has "
            f"confirmed the work happened"
        )
    if outcomes:
        bullets.append("Ledger re-checked: " + ", ".join(outcomes))
    elif counts.get("rechecked"):
        bullets.append(f"Ledger re-checked {counts['rechecked']} earlier recommendations")
    else:
        bullets.append("No earlier recommendations were due for a re-check this Monday")
    created = counts.get("created", 0)
    if created:
        bullets.append(
            f"Ledger: {created} new {_n(created, 'recommendation', 'recommendations')} created, "
            f"each with an owner and a check-by date"
        )
    return _bubble(lead, bullets)


_TEMPLATES = {
    "validation": _template_validation,
    "signals": _template_signals,
    "verdicts": _template_verdicts,
    "digest": _template_digest,
}


def generate_template(phase: str, bundle: dict) -> str:
    return _TEMPLATES[phase](bundle)


# ---------------------------------------------------------------------------
# The narrate loop — generate, check, retry, degrade, template-fallback
# ---------------------------------------------------------------------------


def _run_checks(phase: str, text: str, pool_numbers: set[float], pool_dates: set[str],
                index: ArtifactIndex, bundle: dict, mode: str, attempt: int,
                log: harness.RunLog) -> tuple[bool, dict, str]:
    """The five checks a bubble passes before anyone sees it: every number is
    in the artifacts, every name is a real center, the phase's own rules hold,
    the language is a leader's, and the shape is lead line + bullets."""
    number = harness.check_numbers_pool(text, pool_numbers, pool_dates)
    language = check_leader_language(text)
    entity = check_entities(text, index)
    rules = check_phase_rules(phase, text, bundle)
    shape = check_shape(text)

    def record(event: str, result: dict, extra: dict) -> None:
        log.write(
            event,
            {"phase": phase, "mode": mode, "attempt": attempt,
             "result": "pass" if result["ok"] else "fail", **extra},
        )

    record("narrative_number_check", number,
           {"tokens_checked": number["checked"], "unknown_tokens": number["unknown"]})
    record("narrative_language_check", language, {"problems": language["problems"]})
    record("narrative_entity_check", entity,
           {"problems": entity["problems"], "entities_verified": len(entity["verified"])})
    record("narrative_rule_check", rules, {"problems": rules["problems"]})
    record("narrative_shape_check", shape,
           {"problems": shape["problems"], "bullets": shape.get("bullets")})

    problems = []
    if not number["ok"]:
        problems.append(
            "these numbers do not exist anywhere in the artifacts provided (hallucination): "
            + ", ".join(number["unknown"])
        )
    for result in (entity, rules, language, shape):
        problems.extend(result["problems"])
    details = {
        "number": number, "language": language, "entity": entity,
        "rules": rules, "shape": shape,
    }
    ok = all(r["ok"] for r in details.values())
    return ok, details, "; ".join(problems)


def narrate_phase(phase: str, bundle: dict, mode: str,
                  prompt_template: str | None, log: harness.RunLog,
                  emit=None) -> dict:
    """One phase's narrative: generate, check, retry on content failures, walk
    down the tier ladder on transport failures, and fall back to the
    deterministic template when an answering model cannot produce a verifiable
    narrative — a phase is never narrated with an unverified number.

    `emit`, when given, reports progress as it happens (see `narrate`). Every
    event carries its phase, because phases are narrated concurrently and a
    watcher has to keep the streams apart. Emitting observes the loop; it never
    changes what the loop accepts."""
    started = time.monotonic()
    pool_numbers, pool_dates = build_pool(bundle, phase)
    index = build_index(bundle, phase)
    requested = mode
    fallbacks: list[dict] = []
    attempts = 0
    retry_note: str | None = None
    say = (lambda event: emit({"phase": phase, **event})) if emit else None
    on_delta = (lambda text: say({"type": "delta", "text": text})) if say else None

    while True:
        if mode == "template":
            attempts += 1
            text = generate_template(phase, bundle)
            ok, details, problem = _run_checks(
                phase, text, pool_numbers, pool_dates, index, bundle, mode, attempts, log
            )
            if not ok:  # template text is built from artifact values; this is a tripwire
                log.write("hard_fail", {"phase": phase, "mode": mode, "attempts": attempts, "reason": problem})
                raise harness.HarnessError(
                    f"narrate/{phase}: the deterministic template narrative failed its own checks "
                    f"({problem}) — fix pipeline.narrate."
                )
            return _finish(phase, text, requested, mode, details, index, attempts,
                           fallbacks, log, started)

        # LLM tiers: claude-cli / api
        content_attempts = 0
        while content_attempts <= harness.MAX_REGENERATIONS:
            attempts += 1
            content_attempts += 1
            prompt = render_prompt(prompt_template, phase, bundle, retry_note)
            log.write("llm_call", {"phase": phase, "mode": mode, "attempt": attempts})
            try:
                if say:
                    say({"type": "generating", "mode": mode, "attempt": attempts})
                    raw = call_llm(mode, prompt, on_delta)
                else:
                    raw = call_llm(mode, prompt)
            except verdicts.LLMUnavailable as exc:
                # Transport failure — nothing was generated, nothing to check.
                # Degrade a tier instead of retrying into the same wall.
                reason = str(exc)
                limited = verdicts.is_usage_limit(reason)
                log.write(
                    "generation_error",
                    {
                        "phase": phase,
                        "attempt": attempts,
                        "mode": mode,
                        "kind": "usage_limit" if limited else "unavailable",
                        "error": reason[:500],
                    },
                )
                nxt = verdicts.fallback_mode(mode) or "template"
                log.write(
                    "mode_fallback",
                    {
                        "phase": phase,
                        "from_mode": mode,
                        "to_mode": nxt,
                        "kind": "usage_limit" if limited else "unavailable",
                        "reason": reason[:500],
                    },
                )
                fallbacks.append({"from": mode, "to": nxt, "reason": reason[:300]})
                if say:
                    say({"type": "tier_fallback", "from": mode, "to": nxt, "reason": reason[:300]})
                mode = nxt
                retry_note = None
                break  # leave the content loop; the outer loop runs the next tier

            # Generation is done; nothing here is a narrative yet. Everything
            # below is the harness deciding whether it may be shown as one.
            if say:
                say({"type": "verifying", "mode": mode, "attempt": attempts})

            try:
                text = parse_llm_narrative(raw)
            except ValueError as exc:
                retry_note = f"Your previous reply could not be used: {exc}"
                if say and content_attempts <= harness.MAX_REGENERATIONS:
                    say({"type": "redo", "attempt": attempts, "kind": "malformed",
                         "reason": str(exc)[:300]})
                log.write(
                    "generation_error",
                    {"phase": phase, "attempt": attempts, "mode": mode, "kind": "malformed",
                     "error": str(exc)[:500]},
                )
                if content_attempts <= harness.MAX_REGENERATIONS:
                    log.write("retry", {"phase": phase, "attempt": attempts, "reason": "malformed_answer"})
                continue

            ok, details, problem = _run_checks(
                phase, text, pool_numbers, pool_dates, index, bundle, mode, attempts, log
            )
            if ok:
                return _finish(phase, text, requested, mode, details, index, attempts,
                               fallbacks, log, started)
            retry_note = problem
            if content_attempts <= harness.MAX_REGENERATIONS:
                log.write("retry", {"phase": phase, "attempt": attempts, "reason": problem[:500]})
                if say:
                    say({"type": "redo", "attempt": attempts, "kind": "failed_check",
                         "reason": problem[:300]})
        else:
            # Content budget exhausted: the model answered but never verifiably.
            # Deterministic template fallback — passes by construction — rather
            # than a silent phase or an unverified sentence.
            reason = f"no verifiable narrative after {content_attempts} attempts ({retry_note})"
            log.write(
                "template_fallback",
                {"phase": phase, "from_mode": mode, "attempts": attempts, "reason": reason[:500]},
            )
            fallbacks.append({"from": mode, "to": "template", "reason": reason[:300]})
            mode = "template"
            retry_note = None


def _finish(phase: str, text: str, requested: str, decided_by: str, details: dict,
            index: ArtifactIndex, attempts: int, fallbacks: list[dict],
            log: harness.RunLog, started: float) -> dict:
    number, entity = details["number"], details["entity"]
    receipts = collect_receipts(text, index)
    # What a reader can click through to: the phase's own artifacts, plus any
    # file a receipt actually points at (a cross-phase fact cites its source).
    artifacts_read = list(index.paths)
    for receipt in receipts:
        if receipt["path"] not in artifacts_read:
            artifacts_read.append(receipt["path"])
    log.write(
        "narrative",
        {
            "phase": phase,
            "mode": requested,
            "decided_by": decided_by,
            "attempts": attempts,
            "numbers_verified": number["checked"],
            "entities_verified": len(entity["verified"]),
            "receipts": len(receipts),
            "artifacts_read": artifacts_read,
            "fallbacks": len(fallbacks),
        },
    )
    return {
        "phase": phase,
        "title": PHASE_TITLES[phase],
        "text": text,
        # `mode` is the tier this run was configured to use; `decided_by` is
        # what actually wrote the narrative. They differ only when a fallback
        # fired — recorded in `fallbacks` — so no model is credited for work
        # it never did.
        "mode": requested,
        "decided_by": decided_by,
        "checks": {
            "number_check": "pass",
            "entity_check": "pass",
            "rule_check": "pass",
            "language_check": "pass",
            "shape_check": "pass",
            "numbers_verified": number["checked"],
            "entities_verified": len(entity["verified"]),
        },
        # The receipt map: every number, date and center name in the bubble,
        # with the file and field it came from. This is what makes the sentence
        # checkable by hand rather than merely checked by us.
        "receipts": receipts,
        "artifacts_read": artifacts_read,
        "numbers_verified": number["checked"],
        "entities_verified": len(entity["verified"]),
        "attempts": attempts,
        "fallbacks": fallbacks,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _normalize_phases(phases) -> list[str]:
    if phases is None:
        return list(PHASES)
    requested = [p.strip() for p in (phases.split(",") if isinstance(phases, str) else phases) if str(p).strip()]
    unknown = [p for p in requested if p not in PHASES]
    if unknown:
        raise SystemExit(f"Unknown phase(s) {unknown!r}; choose from {PHASES}.")
    return [p for p in PHASES if p in requested]  # canonical pipeline order


def narrate(as_of: date | str, mode: str | None = None, phases=None, write: bool = True,
            on_event=None, run_context=None) -> dict:
    """Walk the requested phases of one run in order, reasoning as it goes. Returns (and, when
    write=True, merges into DATA/OUTPUTS/<as-of>/narratives.json) one object keyed
    by phase: {phase: {text, mode, decided_by, checks, attempts, ...}}.

    `on_event` (optional) receives progress dicts while the narratives are
    written — how `--stream` feeds the live panel. Every event carries its
    `phase`; the vocabulary is pipeline.ask's:

        {"phase","type":"generating","mode","attempt"}
        {"phase","type":"delta","text"}          provisional text, as produced
        {"phase","type":"verifying","mode",…}    generation done, checks running
        {"phase","type":"redo","kind",…}         that draft was rejected; retry
        {"phase","type":"tier_fallback",…}       that tier could not be reached

    Phases are walked IN ORDER, one agent, one accumulating context: each
    phase's tool result is appended to `run_context`, the agent reasons over
    everything before it (including its own earlier notes), and that reasoning
    is appended before the next phase starts. Events still name their phase, so
    the stream is one ordered run history rather than four interleaved ones.

    `run_context` (optional) supplies an already-open RunContext — how a
    followed run keeps one history across phases that arrive minutes apart.

    The same honesty rules as the ask surface: deltas are provisional, only the
    returned document has passed the checks, and the template tier emits no
    deltas — a deterministic narrative never pretends a model typed it."""
    if isinstance(as_of, str):
        as_of = config.parse_as_of(as_of)
    mode = mode or verdicts.detect_mode()
    if mode not in MODES:
        raise SystemExit(f"Unknown --llm-mode {mode!r}; choose from {MODES}.")
    phase_list = _normalize_phases(phases)
    bundle = load_bundle(as_of, phase_list)

    # The run's accumulated history. One context per Monday: it is opened once,
    # seeded with what the previous Monday handed over, and appended to as the
    # run walks its tools in order.
    ctx = run_context if run_context is not None else runctx.RunContext.open(as_of, write=write)
    bundle["run_context"] = ctx

    out_dir = config.OUTPUTS_DIR / str(as_of)
    # Narrate's own append-only log. Never runlog.jsonl: phases are narrated
    # while the pipeline still runs, and the verdicts step reopens that file
    # fresh mid-run — sharing it would clobber one writer's receipts.
    log = harness.RunLog(out_dir / "narrate_runlog.jsonl" if write else None, step="narrate", mode="a")
    log.write("run_start", {"as_of": str(as_of), "mode": mode, "phases": phase_list})

    prompt_template = load_prompt_template() if mode != "template" else None

    # ONE agent walks the run, in order. Each phase's tool result is appended to
    # the run context, the agent reasons over everything accumulated so far
    # (including what it already wrote), and that reasoning is appended before
    # the next phase begins. Sequential is the point: a phase narrated in
    # parallel could not have seen the one before it.
    results = []
    for phase in phase_list:
        ctx.append_tool_result(phase, _tool_output(phase, bundle))
        result = narrate_phase(phase, bundle, mode, prompt_template, log, on_event)
        ctx.append_reasoning(
            phase,
            result["text"],
            {
                "decided_by": result["decided_by"],
                "mode": result["mode"],
                "attempts": result["attempts"],
                "checks": result.get("checks"),
            },
        )
        results.append(result)

    doc = {phase: result for phase, result in zip(phase_list, results)}
    log.write(
        "summary",
        {
            "as_of": str(as_of),
            "mode": mode,
            "phases": phase_list,
            "decided_by": {phase: doc[phase]["decided_by"] for phase in phase_list},
            "attempts_total": sum(doc[phase]["attempts"] for phase in phase_list),
            "fallbacks_total": sum(len(doc[phase]["fallbacks"]) for phase in phase_list),
        },
    )
    log.close()

    if write:
        path = out_dir / "narratives.json"
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except (ValueError, OSError):
                existing = {}
        merged = {**{k: v for k, v in existing.items() if k in PHASES}, **doc}
        ordered = {phase: merged[phase] for phase in PHASES if phase in merged}
        path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")
    return doc


def _phase_ready(as_of: date, phase: str) -> bool:
    """Has this phase's required artifact landed yet?"""
    raw = _load_raw(str(as_of))
    return all(raw.get(label) is not None for label in PHASE_REQUIRED[phase])


def narrate_follow(as_of: date | str, mode: str | None = None, phases=None,
                   on_event=None, timeout: float = 1800.0, poll: float = 0.25,
                   should_stop=None) -> dict:
    """Walk a run that is still executing, narrating each phase as it lands.

    This is the orchestrator's live form: one process, one agent, one context,
    started when the run starts. It waits for each tool's artifact in pipeline
    order, appends the result, reasons over the accumulated history, and moves
    on — so the agent's note on the signal engine is written while the verdict
    step is still computing, and it was written having read the validation step
    and its own note about it.

    Waiting is what makes the human corrections gate work: the run pauses there
    for as long as it takes, and the follower simply has not seen the next
    artifact yet.
    """
    if isinstance(as_of, str):
        as_of = config.parse_as_of(as_of)
    phase_list = _normalize_phases(phases)
    # Open, never reset. The carry-in must be captured BEFORE this run's ledger
    # step mutates the rows it inherited, so whichever process starts first —
    # pipeline.run or this narrator — seeds it, and the other appends. Clearing
    # a stale history belongs to whoever starts the run (the run manager, or
    # `pipeline.run --fresh-context`), not to the narrator.
    ctx = runctx.RunContext.open(as_of)

    doc: dict = {}
    deadline = time.monotonic() + timeout
    for phase in phase_list:
        while not _phase_ready(as_of, phase):
            if should_stop is not None and should_stop():
                return doc
            if time.monotonic() > deadline:
                if on_event:
                    on_event({"phase": phase, "type": "timeout",
                              "message": f"{phase} produced no artifact within {timeout:.0f}s"})
                return doc
            time.sleep(poll)
        if on_event:
            on_event({"phase": phase, "type": "phase_started"})
        result = narrate(as_of, mode, [phase], on_event=on_event, run_context=ctx)
        doc.update(result)
        # One process narrates the whole run, so a phase's checked note has to
        # be announced when it passes — not held until the process exits.
        if on_event:
            on_event({"phase": phase, "type": "phase_final", "narrative": result.get(phase)})
    return doc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.narrate",
        description="Per-phase narratives — a verified lead line plus decision bullets per pipeline phase.",
    )
    config.add_as_of_argument(parser)
    parser.add_argument(
        "--llm-mode",
        choices=MODES,
        default=None,
        help="Generation mode. Default: auto-detect (claude-cli, then api, then template).",
    )
    parser.add_argument(
        "--phases",
        default=None,
        help=f"Comma-separated phases to narrate (default: all of {','.join(PHASES)}).",
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
            "Emit progress as JSONL on stdout while the narratives are written — one event "
            "per line, each tagged with its phase, ending with {\"type\":\"final\", "
            "\"narratives\": {…}} carrying the checked document. Implies --json."
        ),
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help=(
            "Walk a run that is still executing: wait for each phase's artifact in "
            "pipeline order and narrate it as it lands, one agent over one accumulating "
            "context. This is how the app runs it — one process per run, not one per phase."
        ),
    )
    parser.add_argument(
        "--follow-timeout",
        type=float,
        default=1800.0,
        help="Seconds to wait for a phase's artifact before giving up (default 1800).",
    )
    args = parser.parse_args(argv)

    if args.stream:
        # One JSON object per line, flushed as it happens; the API layer turns
        # these into server-sent events. Phases run on worker threads, so the
        # lock keeps two concurrent phases from interleaving mid-line — a torn
        # line would be an unparseable event, which is a bug, not a warning.
        lock = threading.Lock()

        def emit(event: dict) -> None:
            line = json.dumps(event, ensure_ascii=False) + "\n"
            with lock:
                sys.stdout.write(line)
                sys.stdout.flush()

        started = time.monotonic()
        doc = (
            narrate_follow(args.as_of, args.llm_mode, args.phases, on_event=emit,
                           timeout=args.follow_timeout)
            if args.follow
            else narrate(args.as_of, args.llm_mode, args.phases, on_event=emit)
        )
        # The LAST line is always the whole document — the same object --json
        # prints, so a reader that ignores every other event loses nothing.
        emit({"type": "final", "narratives": doc,
              "elapsed_seconds": round(time.monotonic() - started, 2)})
        return

    started = time.monotonic()
    doc = (
        narrate_follow(args.as_of, args.llm_mode, args.phases, timeout=args.follow_timeout)
        if args.follow
        else narrate(args.as_of, args.llm_mode, args.phases)
    )
    elapsed = time.monotonic() - started

    if args.json:
        print(json.dumps(doc, ensure_ascii=False))
        return

    print(f"Narratives — as of {args.as_of} ({elapsed:.1f}s total)")
    for phase, result in doc.items():
        checks = result["checks"]
        print(f"  [{result['decided_by']}] {result['title']} "
              f"({checks['numbers_verified']} numbers + {checks['entities_verified']} names "
              f"verified, {len(result['receipts'])} receipts, attempts {result['attempts']}, "
              f"{result['elapsed_seconds']}s)")
        for line in result["text"].splitlines():
            print(f"    {line}")
        print(f"    read: {' · '.join(result['artifacts_read'])}")
        for fb in result["fallbacks"]:
            print(f"    ! {fb['from']} → {fb['to']}: {fb['reason']}")
    print(f"  Output: {config.OUTPUTS_DIR / str(args.as_of) / 'narratives.json'}")


if __name__ == "__main__":
    main()
