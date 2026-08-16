"""The one-command Monday digest run.

    python -m pipeline.run --as-of 2026-05-04

Order of operations (each step's own module does the work; this file only
sequences them and assembles the digest):

  1. Data Validation & Check — confirm the corrected DATA/TRANSLATION/ copies exist and
     pull the check counts + plain-English findings for the digest's
     "Data checks: N ran, M corrections" line. (Building DATA/TRANSLATION/ is a human
     accept/decline step: `python -m pipeline.validate --accept all`.)
  2. Signal engine — what earns attention this Monday (spike / drift / gap).
  3. Claimed vs. Verified — an AI verdict on every action plan, through the
     harness (number check + reasoning check; retries logged; hard fail).
  4. Recommendation ledger — re-check every open recommendation against the
     real week that followed, then track this run's new ones.
  5. Assemble DATA/OUTPUTS/<as-of>/digest.json — the single file the digest UI
     reads. Every number in it comes from the steps above; the UI computes
     nothing.

Close-the-loop demo (two real runs, no fabrication):

    python -m pipeline.run --as-of 2026-04-27 --fresh-ledger
    python -m pipeline.run --as-of 2026-05-04

The second run re-checks the first run's recommendations against the week
that actually followed and reports the OUTCOME (working / not working / flat)
separately from the EXECUTION a human attested — escalation follows attested
non-execution, never a flat number. The official submitted digest is the
2026-05-04 one.

Leader-facing rule: real center names everywhere; location IDs appear only
in audit-trail fields.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime, timezone

import pandas as pd

from . import config, harness, ledger, runctx, signals, verdicts

VALIDATION_DIR = config.OUTPUTS_DIR / "validation"

_PCC_RE = re.compile(r"\bPCC_\d+\b")


def _rel(path) -> str:
    return str(path.relative_to(config.REPO_ROOT))


def _validation_receipt(as_of, name: str) -> str:
    """A receipt path for one of this Monday's validation artifacts.

    DATA/OUTPUTS/validation/ is the CURRENT working copy — the next Monday
    overwrites it — so a receipt pointing there stops being true the moment a
    later week runs. `pipeline.validate` snapshots every run into
    DATA/OUTPUTS/<as_of>/validation/, and that copy is immutable, so the
    April digest keeps resolving to April's checks after May has run. The
    working copy is only the fallback for a digest built before snapshots
    existed.
    """
    snapshot = config.OUTPUTS_DIR / str(as_of) / "validation" / name
    return _rel(snapshot if snapshot.exists() else VALIDATION_DIR / name)


# ---------------------------------------------------------------------------
# 1. Data Validation & Check — presence + digest-facing summary
# ---------------------------------------------------------------------------


def check_data_ready() -> dict:
    """DATA/TRANSLATION/ must exist (with its manifest) and the validation report must
    have been produced. This step never builds DATA/TRANSLATION/ itself — accepting
    corrections is a human decision, not something a digest run does."""
    missing = [str(p) for p in config.DATA_FILES.values() if not p.exists()]
    manifest_path = config.DATA_DIR / "MANIFEST.json"
    report_path = VALIDATION_DIR / "report.json"
    if missing or not manifest_path.exists():
        raise SystemExit(
            "DATA/TRANSLATION/ is not ready (missing: "
            + ", ".join(missing + ([] if manifest_path.exists() else [str(manifest_path)]))
            + ").\nRun the Data Validation & Check first:\n"
            "    python -m pipeline.validate\n"
            "    python -m pipeline.validate --accept all"
        )
    if not report_path.exists():
        raise SystemExit(
            f"Validation report missing ({report_path}).\n"
            "Run `python -m pipeline.validate` first."
        )
    return {
        "manifest": json.loads(manifest_path.read_text()),
        "report": json.loads(report_path.read_text()),
    }


def build_data_checks(validation: dict, names: dict[str, str], as_of) -> dict:
    """The bottom line, one click deep: "Data checks: N ran, M corrections"
    plus the plain-English list behind it. Location IDs are scrubbed to real
    center names — a leader never sees PCC_xxx."""

    def scrub(text: str) -> str:
        return _PCC_RE.sub(lambda m: names.get(m.group(0), m.group(0)), text)

    report, manifest = validation["report"], validation["manifest"]
    corrections = manifest.get("corrections_accepted", [])
    carried = manifest.get("corrections_carried", [])
    new_decisions = manifest.get("corrections_new", [])

    plain_english: list[str] = [
        (
            f"Standing decision carried forward: {scrub(c['description'])}"
            if c.get("decision_status") == "carried"
            else f"Fixed: {scrub(c['description'])}"
        )
        for c in corrections
    ]
    for table_findings in report["tables"].values():
        for f in table_findings:
            if f["result"] == "flag":
                plain_english.append(f"Flagged: {scrub(f['summary'])}")

    return {
        "title": "Data Validation & Check",
        "n_ran": report["totals"]["checks_run"],
        # `corrections` is retained for older clients. The fields below own the
        # Week 2 wording: a standing decision applied again is not a new
        # proposal or a new human decision.
        "corrections": len(corrections),
        "corrections_carried": len(carried),
        "corrections_new": len(new_decisions),
        "rows_under_standing_corrections": sum(
            int(c.get("new_rows") or 0) for c in carried
        ),
        "provider_rows_under_standing_corrections": sum(
            int(c.get("new_rows") or 0)
            for c in corrections
            if c.get("decision_status") == "carried"
            and c.get("scope") == "standing"
            and c.get("table") == "provider_weekly"
        ),
        "plain_english": plain_english,
        "validated_as_of": report["as_of"],
        "details": {
            "report": _validation_receipt(as_of, "report.md"),
            "report_json": _validation_receipt(as_of, "report.json"),
            "manifest": _validation_receipt(as_of, "MANIFEST.json"),
        },
    }


# ---------------------------------------------------------------------------
# Digest sections
# ---------------------------------------------------------------------------


def build_claimed_vs_verified(verdicts_doc: dict) -> dict:
    """Three columns per commitment: Reported Status · What the numbers say ·
    AI Verdict. Everything comes verbatim from verdicts.json."""
    rows = []
    for v in verdicts_doc["verdicts"]:
        numbers = v["what_the_numbers_say"]
        av = v["ai_verdict"]
        rows.append(
            {
                "plan_id": v["plan_id"],
                "center": v["center"],
                "location_id": v["location_id"],  # audit trail only
                "metric_display": v["metric_display"],
                "reported_status": {
                    "display": v["reported_status"]["display"],
                    "owner": v["reported_status"]["owner"],
                    "last_update": v["reported_status"]["last_update"],
                    "staleness_days": v["reported_status"]["staleness_days"],
                },
                "what_the_numbers_say": {
                    "summary": numbers["summary"],
                    "baseline_display": numbers["baseline_display"],
                    "target_display": numbers["target_display"],
                    "actual_4wk_display": numbers["actual_4wk_display"],
                    "gap_closed_display": numbers["gap_closed_display"],
                    "trend_direction": numbers["trend_direction"],
                    "notes": numbers["notes"],
                },
                "ai_verdict": {
                    "display": av["verdict_display"],
                    "bucket": av["bucket"],
                    "agree": av["agree"],
                    "sentence": av["sentence"],
                    "recommended_action": av["recommended_action"],
                },
            }
        )
    return {
        "columns": ["Reported Status", "What the numbers say", "AI Verdict"],
        "rows": rows,
        "counts": verdicts_doc["counts"],
    }


def harness_summary(as_of: date, verdicts_doc: dict) -> dict:
    """Count every harness check that ran this digest, from the run log —
    the digest reports what was verified, not what was hoped."""
    runlog = config.OUTPUTS_DIR / str(as_of) / "runlog.jsonl"
    passed = failed = 0
    check_events = {
        "number_check",
        "reasoning_check",
        "facts_summary_number_check",
        # The successor recommendations the ledger step wrote pass the same
        # three-layer check, and they are counted here for the same reason.
        "successor_number_check",
        "successor_language_check",
        "successor_rule_check",
    }
    if runlog.exists():
        for line in runlog.read_text().splitlines():
            event = json.loads(line)
            if event.get("event") in check_events:
                if event.get("result") == "pass":
                    passed += 1
                else:
                    failed += 1
    return {
        "mode": verdicts_doc["llm_mode"],
        # Per-tier counts + any mid-run degradation (an unreachable model
        # drops a plan to the tier below; the digest never hides that).
        "modes_used": verdicts_doc.get("llm_modes_used", {}),
        "mode_fallbacks": verdicts_doc.get("mode_fallbacks", []),
        "checks_passed": passed,
        "checks_failed": failed,
        "retries": verdicts_doc["counts"]["regenerations_total"],
        "max_regenerations": verdicts_doc["harness"]["max_regenerations"],
        "rules": verdicts_doc["harness"]["rules"],
        "runlog": _rel(runlog),
    }


def build_top_signals(signals_doc: dict, ledger_section: dict, as_of) -> list[dict]:
    """This run's ranked signals, each tied to its ledger recommendation —
    what / so-what (the headline + receipts) and do-what (the tracked rec)."""
    by_pair: dict[tuple[str, str], dict] = {}
    for row in ledger_section["open"] + ledger_section["changed"]:
        key = (row["center"], row["metric"])
        by_pair.setdefault(key, row)

    # Which of these asks an earlier Monday opened, and which this run graded.
    # Without it a reader sees N ranked signals carrying N recommendations and
    # reads every one of them as discovered today — the opposite of what the
    # loop actually did.
    rechecked = {r["rec_id"] for r in ledger_section["changed"]}

    out = []
    for sig in signals_doc["signals"]:
        entry = dict(sig)
        rec = by_pair.get((sig["center"], sig["metric"]))
        if rec:
            entry["action"] = {
                "rec_id": rec["rec_id"],
                "recommendation": rec["recommendation"],
                "owner": rec["owner"],
                "check_by": rec["check_by"],
                # When this ask was first written, and whether this run graded
                # it. A row created before `as_of` was inherited, not found.
                "created_week": rec.get("created_week"),
                "carried_forward": rec.get("created_week") != str(as_of),
                "rechecked_this_run": rec["rec_id"] in rechecked,
                # Lifecycle, and the two independent dimensions beside it —
                # never collapsed into one field again.
                "status": rec.get("status"),
                "status_display": rec.get("status_display"),
                "execution": rec.get("execution"),
                "execution_display": rec.get("execution_display"),
                "outcome": rec.get("outcome"),
                "outcome_display": rec.get("outcome_display"),
                "loop_state": rec.get("loop_state"),
                "loop_state_display": rec.get("loop_state_display"),
                # Whether the data supports the intervention or only the signal.
                "action_basis": rec.get("action_basis"),
                "action_assumption": rec.get("action_assumption"),
            }
        out.append(entry)
    return out


def build_suppressed(signals_doc: dict) -> list[dict]:
    return [
        {
            "center": s["center"],
            "metric_display": s["metric_display"],
            "would_be_priority": s["would_be_priority"],
            "rule": s["rule"],
            "reason": s["reason"],
        }
        for s in signals_doc["suppressed"]
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    as_of: date,
    leader: str = verdicts.DEFAULT_LEADER,
    mode: str | None = None,
    fresh_ledger: bool = False,
    fresh_context: bool = False,
) -> dict:
    timings: dict[str, float] = {}

    def timed(name: str, fn):
        t0 = time.perf_counter()
        result = fn()
        timings[name] = round(time.perf_counter() - t0, 3)
        return result

    # Open the run context FIRST, before the ledger step below re-checks and
    # supersedes the rows this run inherited. Step 0 must record what the run
    # started with, not what it left behind. Opening is idempotent: when the
    # narrator already seeded it, this is a no-op and the history is one.
    ctx = runctx.RunContext.open(as_of)
    if fresh_context:
        ctx.reset()

    validation = timed("data_validation_check", check_data_ready)
    loc = pd.read_csv(config.DATA_FILES["locations"])
    names = dict(zip(loc["location_id"], loc["location_name"]))

    signals_doc = timed("signal_engine", lambda: signals.run(as_of, leader))
    verdicts_doc = timed(
        "claimed_vs_verified", lambda: verdicts.run(as_of, leader, mode)
    )
    # The ledger step re-checks last week's recommendations and — for every one
    # that failed or went ignored — writes the successor: the next move, on the
    # same LLM ladder and through the same harness as the verdicts above.
    ledger_section = timed(
        "ledger",
        lambda: ledger.update(as_of, signals_doc, fresh=fresh_ledger, mode=mode),
    )

    out_dir = config.OUTPUTS_DIR / str(as_of)
    digest = {
        "step": "Monday digest",
        "as_of": str(as_of),
        "latest_complete_week": signals_doc["latest_complete_week"],
        "leader": leader,
        "centers": signals_doc["centers"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Step 0, verbatim from this run's own append-only history — what the
        # previous Monday handed over, captured before the ledger step below
        # touched any of it. The digest is the artifact a reviewer reads, so
        # what it was built ON belongs in it: which Monday it continues, which
        # recommendations it inherited, which were due, what new evidence
        # arrived, and how many data-quality decisions carried vs. were new.
        # Recording it only in run_context.jsonl left the digest unable to say
        # it had a memory at all.
        "carry_in": ctx.carry_in,
        "top_signals": build_top_signals(signals_doc, ledger_section, as_of),
        "claimed_vs_verified": build_claimed_vs_verified(verdicts_doc),
        "ledger": ledger_section,
        "data_checks": build_data_checks(validation, names, as_of),
        "suppressed": build_suppressed(signals_doc),
        "harness": harness_summary(as_of, verdicts_doc),
        "receipts": {
            "signals": _rel(out_dir / "signals.json"),
            "signals_runlog": _rel(out_dir / "signals_runlog.jsonl"),
            "facts": _rel(out_dir / "facts.json"),
            "verdicts": _rel(out_dir / "verdicts.json"),
            "runlog": _rel(out_dir / "runlog.jsonl"),
            "ledger_state": ledger_section["state_file"],
            "ledger_log": ledger_section["log_file"],
            "validation_report": _validation_receipt(as_of, "report.md"),
            "data_manifest": _validation_receipt(as_of, "MANIFEST.json"),
            "scoring_doc": "docs/SCORING.md",
            "prompt_file": verdicts_doc["prompt_file"],
            "data_provenance": signals_doc["data_provenance"],
        },
    }
    (out_dir / "digest.json").write_text(json.dumps(digest, indent=2) + "\n")

    # Orchestration receipts land in the same runlog the verdict step owns
    # (append mode — verdicts opened it fresh earlier in this run).
    log = harness.RunLog(out_dir / "runlog.jsonl", step="run", mode="a")
    log.write(
        "digest_assembled",
        {
            "as_of": str(as_of),
            "leader": leader,
            "llm_mode": verdicts_doc["llm_mode"],
            "step_seconds": timings,
            "counts": {
                "top_signals": len(digest["top_signals"]),
                "suppressed": len(digest["suppressed"]),
                "plans_verified": digest["claimed_vs_verified"]["counts"]["plans"],
                "ledger": ledger_section["counts"],
                "data_checks_ran": digest["data_checks"]["n_ran"],
                "corrections": digest["data_checks"]["corrections"],
                "harness_checks_passed": digest["harness"]["checks_passed"],
                "harness_retries": digest["harness"]["retries"],
            },
            "output": _rel(out_dir / "digest.json"),
        },
    )
    log.close()
    return digest


def _print_summary(digest: dict) -> None:
    lc = digest["ledger"]["counts"]
    print(f"\nMonday digest — {digest['as_of']} (week of {digest['latest_complete_week']})")
    print(f"  Leader: {digest['leader']} ({len(digest['centers'])} centers)")

    print(f"  Top signals ({len(digest['top_signals'])}):")
    for s in digest["top_signals"]:
        print(f"    {s['rank']}. {s['center']} — {s['metric_display']} (priority {s['priority']:.2f})")

    cv = digest["claimed_vs_verified"]["counts"]
    buckets = ", ".join(f"{b} {n}" for b, n in cv["buckets"].items() if n)
    print(
        f"  Claimed vs. Verified: {cv['plans']} plans — {buckets} · "
        f"agree {cv['agree']} · disagree {cv['disagree']}"
    )

    print(
        f"  Ledger: {lc['created']} new recommendation(s); {lc['rechecked']} re-checked — "
        f"{lc['outcome_working']} moved the right way, "
        f"{lc['outcome_not_working']} the wrong way, {lc['outcome_flat']} flat; "
        f"execution confirmed on {lc['execution_done']}, "
        f"unconfirmed on {lc['execution_unknown']}; {lc['open']} open."
    )
    for r in digest["ledger"]["changed"]:
        print(f"    re-check {r['rec_id']}: {r['outcome']} — {r['note']}")
        s = r.get("successor")
        if s:
            print(
                f"      next move ({s['decided_by']}, {s['checks']['numbers_verified']} "
                f"numbers verified): {s['next_move']} {s['expected']}"
            )
    for r in digest["ledger"]["created"]:
        print(f"    new {r['rec_id']} (owner {r['owner']}, check by {r['check_by']})")

    dc = digest["data_checks"]
    h = digest["harness"]
    print(f"  Data checks: {dc['n_ran']} ran, {dc['corrections']} corrections.")
    print(
        f"  Harness ({h['mode']} mode): {h['checks_passed']} checks passed, "
        f"{h['checks_failed']} failed, {h['retries']} retries."
    )
    if h["mode_fallbacks"]:
        by_tier = ", ".join(f"{tier} {n}" for tier, n in h["modes_used"].items())
        print(
            f"  ! {len(h['mode_fallbacks'])} verdict(s) fell back to a lower tier "
            f"(model unreachable) — written by: {by_tier}."
        )
    print(f"  Digest: {config.OUTPUTS_DIR / digest['as_of'] / 'digest.json'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.run",
        description="Run the full Monday digest pipeline and assemble digest.json.",
    )
    config.add_as_of_argument(parser)
    parser.add_argument(
        "--leader",
        default=verdicts.DEFAULT_LEADER,
        help=f"Leader the digest is for. Default: {verdicts.DEFAULT_LEADER}.",
    )
    parser.add_argument(
        "--llm-mode",
        choices=verdicts.MODES,
        default=None,
        help="Verdict generation mode. Default: auto-detect (claude-cli, api, openai, template).",
    )
    parser.add_argument(
        "--fresh-context",
        action="store_true",
        help=(
            "Start this Monday's run history over. The run context is one ordered "
            "record per run — use this when re-running a Monday from the CLI so the "
            "new run does not append to the old one's history."
        ),
    )
    parser.add_argument(
        "--fresh-ledger",
        action="store_true",
        help="Archive the existing ledger and start clean (for reproducible demos).",
    )
    args = parser.parse_args(argv)

    digest = run(args.as_of, args.leader, args.llm_mode, args.fresh_ledger,
                 args.fresh_context)
    _print_summary(digest)


if __name__ == "__main__":
    main()
