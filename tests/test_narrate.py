"""Per-phase narrative tests — grounding, phase scoping, and the fallback chain.

Four things must hold for a narrative to sit in a leader's progress stepper:

1. **Deterministic floor.** Template mode produces the same text every run,
   built only from artifact values, and it passes the same harness checks as
   every other mode — so the pipeline always has a verified voice, even with
   no LLM at all.
2. **Phase-scoped grounding.** Each phase narrates from ITS artifacts, with
   prior phases present only as trimmed, deterministic context in the prompt —
   never model memory.
3. **Never fabricates under pressure.** A model that keeps citing numbers the
   run never produced (or leaks internal IDs) is retried, then replaced by the
   deterministic template — and the result says which tier actually wrote it.
4. **Degrades honestly.** An unreachable model walks down the ladder with the
   degradation recorded; no tier is credited for work it never did.

These run against the real committed artifacts for 2026-05-04.
"""

from __future__ import annotations

import json
import shutil
from datetime import date

import pytest

from pipeline import config, harness, narrate, verdicts

AS_OF = date(2026, 5, 4)
PRIOR_MONDAY = date(2026, 4, 27)

# What a good signals narrative looks like: a lead line, then one bullet per
# ranked signal, every number from signals.json. Used wherever a test needs a
# reply that the harness should ACCEPT.
GOOD_SIGNALS = (
    "NARRATIVE: Signals ran against this week's data. Flagged for the leader:\n"
    "• Mount Pleasant — staff call-outs averaging 7.2 a week over the last 4 weeks vs its "
    "12-week norm of 1.6\n"
    "• Daniel Island — no-show rate averaging 8.5% over the last 4 weeks vs its 12-week "
    "norm of 6.6%\n"
    "• Morrisville — record completion within 24h averaging 77.7% over the last 4 weeks vs "
    "its 12-week norm of 87.7%"
)
GOOD_SIGNALS_TEXT = GOOD_SIGNALS.removeprefix("NARRATIVE: ")


@pytest.fixture(scope="module")
def bundle():
    return narrate.load_bundle(AS_OF, narrate.PHASES)


@pytest.fixture(scope="module")
def template_doc():
    return narrate.narrate(AS_OF, mode="template", write=False)


# ---------------------------------------------------------------------------
# 1. Template mode — deterministic, verified, leader-safe
# ---------------------------------------------------------------------------


def test_template_covers_every_phase_and_passes_the_harness(template_doc):
    assert list(template_doc) == list(narrate.PHASES)
    for phase, result in template_doc.items():
        assert result["text"], phase
        assert result["mode"] == "template"
        assert result["decided_by"] == "template"
        assert result["checks"]["number_check"] == "pass"
        assert result["checks"]["language_check"] == "pass"
        assert result["checks"]["numbers_verified"] > 0
        assert result["attempts"] == 1
        assert result["fallbacks"] == []


def test_template_verdicts_gives_each_not_working_plan_its_own_bullet(template_doc, bundle):
    text = template_doc["verdicts"]["text"]
    rows = bundle["phases"]["verdicts"]["artifacts"]["verdicts.json"]["verdicts"]
    not_working = [
        r for r in rows if (r.get("ai_verdict") or {}).get("bucket") == "NOT WORKING"
    ]
    bullets = [
        ln for ln in text.splitlines() if ln.startswith("•") and ln.lower().startswith("• not working")
    ]
    assert len(bullets) == len(not_working)
    for row in not_working:
        assert any(row["center"] in b and " · " not in b for b in bullets), row["center"]


def test_template_is_deterministic(template_doc):
    again = narrate.narrate(AS_OF, mode="template", write=False)
    assert {p: r["text"] for p, r in again.items()} == {
        p: r["text"] for p, r in template_doc.items()
    }


def test_template_texts_pass_an_independent_pool_recheck(template_doc, bundle):
    """Re-verify the harness claim from outside the module: every number in
    every narrative exists in the same artifacts its prompt would carry."""
    for phase, result in template_doc.items():
        numbers, dates = narrate.build_pool(bundle, phase)
        check = harness.check_numbers_pool(result["text"], numbers, dates)
        assert check["ok"], (phase, check["unknown"])


def test_narratives_speak_leader_language(template_doc):
    for phase, result in template_doc.items():
        language = narrate.check_leader_language(result["text"])
        assert language["ok"], (phase, language["problems"])
        assert "PCC_" not in result["text"]
        assert "DVM_" not in result["text"]


def test_language_check_catches_every_banned_form():
    bad = "The audit flagged PCC_006 because staff_call_outs rose."
    result = narrate.check_leader_language(bad)
    assert not result["ok"]
    joined = " ".join(result["problems"])
    assert "PCC_006" in joined
    assert "audit" in joined
    assert "staff_call_outs" in joined


def test_language_check_rejects_process_narration_and_editorial_colour():
    """The two failure modes that made the old bubbles read as AI slop: telling
    the leader how the machine worked, and telling them how to feel."""
    process = narrate.check_leader_language(
        "This step sifted 121 center-metric combinations and held back four near misses."
    )
    assert not process["ok"]
    joined = " ".join(process["problems"])
    assert "combination" in joined and "held back" in joined

    colour = narrate.check_leader_language(
        "Verdae's CSAT looked worrisome and the drift is clearly significant."
    )
    assert not colour["ok"]
    assert "editorial" in " ".join(colour["problems"])


def test_plan_ids_never_reach_the_chat_either(template_doc):
    for phase, result in template_doc.items():
        assert "AP_0" not in result["text"], phase
        language = narrate.check_leader_language(result["text"])
        assert language["ok"], (phase, language["problems"])
    assert not narrate.check_leader_language("AP_002 is not working.")["ok"]


# ---------------------------------------------------------------------------
# 2. Per-phase artifact selection — each phase narrates from ITS artifacts
# ---------------------------------------------------------------------------


def test_each_phase_gets_exactly_its_artifacts(bundle):
    got = {phase: set(bundle["phases"][phase]["artifacts"]) for phase in narrate.PHASES}
    assert got["validation"] == {
        "validation/report.json",
        "validation/corrections_proposed.json",
        "DATA/TRANSLATION/MANIFEST.json",
    }
    assert got["signals"] == {"signals.json"}
    assert got["verdicts"] == {"verdicts.json", "facts.json"}
    assert got["digest"] == {"digest.json", "ledger"}


def test_verdicts_facts_are_trimmed_to_the_judged_plans(bundle):
    """facts.json holds all 28 plans; the verdicts narrative is grounded in
    the plans that were actually judged."""
    arts = bundle["phases"]["verdicts"]["artifacts"]
    judged = {r["plan_id"] for r in arts["verdicts.json"]["verdicts"]}
    assert {p["plan_id"] for p in arts["facts.json"]["plans"]} == judged


def test_digest_ledger_events_belong_to_this_run(bundle):
    for event in bundle["phases"]["digest"]["artifacts"]["ledger"]["events"]:
        assert event["as_of"] == str(AS_OF)


def test_prior_context_follows_pipeline_order(bundle):
    prior = {phase: set(bundle["phases"][phase]["prior_context"]) for phase in narrate.PHASES}
    assert prior["validation"] == set()
    assert prior["signals"] == {"validation"}
    assert prior["verdicts"] == {"validation", "signals"}
    assert prior["digest"] == {"validation", "signals", "verdicts"}


# ---------------------------------------------------------------------------
# 3. Prompt assembly — cross-phase context is in the prompt, deterministically
# ---------------------------------------------------------------------------


def test_prompt_carries_prior_phase_data_for_truthful_connections(bundle):
    template = narrate.load_prompt_template()
    prompt = narrate.render_prompt(template, "digest", bundle, None)
    # Prior phases' key results ride along, trimmed: the validation step's
    # accepted corrections, the top signal, and the verdict split.
    assert "duplicate rows" in prompt              # validation correction text
    assert "Mount Pleasant" in prompt              # signals headline center
    assert "corrections_accepted" in prompt
    assert "{{" not in prompt                      # every placeholder substituted


def test_first_phase_has_no_prior_context(bundle):
    template = narrate.load_prompt_template()
    prompt = narrate.render_prompt(template, "validation", bundle, None)
    assert "(none — this is the first phase of the run)" in prompt
    assert "signals.json" not in prompt


def test_signals_prompt_scopes_artifacts_to_the_phase(bundle):
    template = narrate.load_prompt_template()
    prompt = narrate.render_prompt(template, "signals", bundle, None)
    assert "### signals.json" in prompt
    # Later phases' artifacts must not leak in — verdicts are not signals context.
    assert "### verdicts.json" not in prompt
    assert "### digest.json" not in prompt


def test_retry_note_lands_in_the_next_prompt(bundle):
    template = narrate.load_prompt_template()
    prompt = narrate.render_prompt(
        template, "signals", bundle,
        "these numbers do not exist anywhere in the artifacts provided (hallucination): 9137.42",
    )
    assert "Fix required" in prompt
    assert "9137.42" in prompt


def test_parse_llm_narrative_joins_continuation_lines():
    text = "NARRATIVE: First sentence.\nSecond sentence carries on.\n"
    assert narrate.parse_llm_narrative(text) == "First sentence. Second sentence carries on."


def test_parse_rejects_a_reply_with_no_narrative_line():
    with pytest.raises(ValueError, match="NARRATIVE"):
        narrate.parse_llm_narrative("Sure, here's a summary of the run.")


# ---------------------------------------------------------------------------
# 4. The harness holds the line — fabrication ends in the template, never
#    in front of a leader
# ---------------------------------------------------------------------------


def test_fabricated_numbers_are_rejected_then_template_takes_over(monkeypatch):
    calls = {"n": 0}

    def fabricating(mode, prompt):
        calls["n"] += 1
        return "NARRATIVE: The engine found 9137.42 problems this Monday."

    monkeypatch.setattr(narrate, "call_llm", fabricating)
    doc = narrate.narrate(AS_OF, mode="claude-cli", phases=["signals"], write=False)
    result = doc["signals"]

    assert calls["n"] == harness.MAX_REGENERATIONS + 1  # initial + 2 retries
    assert result["mode"] == "claude-cli"               # the tier configured
    assert result["decided_by"] == "template"           # the tier that wrote it
    assert result["attempts"] == harness.MAX_REGENERATIONS + 2  # 3 LLM + 1 template
    assert "9137.42" not in result["text"]
    assert result["checks"]["number_check"] == "pass"
    fallback = result["fallbacks"][-1]
    assert fallback["from"] == "claude-cli" and fallback["to"] == "template"
    assert "9137.42" in fallback["reason"]


def test_leader_language_violations_also_end_in_the_template(monkeypatch):
    # Numbers all verify (3 ranked / 4 suppressed exist in the run); the text
    # still leaks an internal ID and says "audit" — rejected all the same.
    monkeypatch.setattr(
        narrate, "call_llm",
        lambda mode, prompt: "NARRATIVE: The audit ranked 3 signals at PCC_006 and suppressed 4.",
    )
    doc = narrate.narrate(AS_OF, mode="claude-cli", phases=["signals"], write=False)
    result = doc["signals"]
    assert result["decided_by"] == "template"
    assert "PCC_" not in result["text"]
    # The recorded reason names what was wrong with the rejected draft (the
    # 300-char record leads with the violations the checks found first).
    assert "PCC_006" in result["fallbacks"][-1]["reason"]


def test_verified_llm_narrative_is_credited_to_the_tier_that_wrote_it(monkeypatch):
    monkeypatch.setattr(narrate, "call_llm", lambda mode, prompt: GOOD_SIGNALS)
    doc = narrate.narrate(AS_OF, mode="claude-cli", phases=["signals"], write=False)
    result = doc["signals"]
    assert result["decided_by"] == "claude-cli"
    assert result["attempts"] == 1
    assert result["fallbacks"] == []
    assert result["checks"]["numbers_verified"] >= 2
    assert result["text"] == GOOD_SIGNALS_TEXT  # bullets survive the parser
    assert all(c == "pass" for c in (
        result["checks"]["number_check"], result["checks"]["entity_check"],
        result["checks"]["rule_check"], result["checks"]["language_check"],
        result["checks"]["shape_check"],
    ))


def test_malformed_reply_burns_a_retry_then_a_good_one_lands(monkeypatch):
    replies = iter([
        "Here are my thoughts on the run.",  # no NARRATIVE: line — malformed
        GOOD_SIGNALS,
    ])
    prompts = []

    def scripted(mode, prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(narrate, "call_llm", scripted)
    doc = narrate.narrate(AS_OF, mode="claude-cli", phases=["signals"], write=False)
    result = doc["signals"]
    assert result["decided_by"] == "claude-cli"
    assert result["attempts"] == 2
    assert "could not be used" in prompts[1]  # the retry prompt names the problem


# ---------------------------------------------------------------------------
# 5. The tier ladder — an unreachable model costs nobody their narration
# ---------------------------------------------------------------------------


def test_usage_limit_degrades_down_the_ladder_and_says_so(monkeypatch):
    calls = {"n": 0}

    def rate_limited(mode, prompt):
        calls["n"] += 1
        raise verdicts.LLMUnavailable(
            "claude CLI exited 1: Claude usage limit reached. Your limit will reset at 8pm."
        )

    monkeypatch.setattr(narrate, "call_llm", rate_limited)
    doc = narrate.narrate(AS_OF, mode="claude-cli", phases=["verdicts"], write=False)
    result = doc["verdicts"]

    assert result["mode"] == "claude-cli"
    assert result["decided_by"] == "template"
    assert result["text"]  # the leader still gets a narrated phase
    assert result["fallbacks"][0]["from"] == "claude-cli"
    assert result["fallbacks"][-1]["to"] == "template"
    assert "usage limit" in result["fallbacks"][0]["reason"].lower()
    # One no-answer per LLM tier: a quota wall is never retried into.
    assert calls["n"] == len(result["fallbacks"])


# ---------------------------------------------------------------------------
# 6. Orchestration — phase selection, missing runs, and the output file
# ---------------------------------------------------------------------------


def test_requested_phases_come_back_in_pipeline_order():
    doc = narrate.narrate(AS_OF, mode="template", phases="digest,validation", write=False)
    assert list(doc) == ["validation", "digest"]


def test_unknown_phase_is_rejected_by_name():
    with pytest.raises(SystemExit, match="Unknown phase"):
        narrate.narrate(AS_OF, mode="template", phases="signals,uploading", write=False)


def test_missing_run_names_whats_missing_and_how_to_fix_it():
    with pytest.raises(SystemExit) as exc:
        narrate.narrate(date(2026, 6, 1), mode="template", phases=["signals"], write=False)
    message = str(exc.value)
    assert "signals: signals.json" in message
    assert "python -m pipeline.run --as-of 2026-06-01" in message


def test_write_merges_phases_and_logs_to_narrates_own_runlog(tmp_path, monkeypatch):
    """Narrating phase-by-phase (how the stepper calls this) accumulates one
    narratives.json, and every check lands in narrate's OWN append-only log
    (narrate_runlog.jsonl). The pipeline's runlog.jsonl is never touched:
    phases are narrated while the pipeline still runs, and the verdicts step
    reopens that file fresh mid-run — sharing it would clobber receipts."""
    clone = tmp_path / "outputs"
    (clone / str(AS_OF)).mkdir(parents=True)
    shutil.copytree(config.OUTPUTS_DIR / "validation", clone / "validation")
    for name in ("signals.json", "facts.json", "verdicts.json", "digest.json", "runlog.jsonl"):
        shutil.copy(config.OUTPUTS_DIR / str(AS_OF) / name, clone / str(AS_OF) / name)
    runlog = clone / str(AS_OF) / "runlog.jsonl"
    runlog_before = runlog.read_text()
    monkeypatch.setattr(config, "OUTPUTS_DIR", clone)

    narrate.narrate(AS_OF, mode="template", phases=["validation"])
    narrate.narrate(AS_OF, mode="template", phases=["signals"])

    written = json.loads((clone / str(AS_OF) / "narratives.json").read_text())
    assert list(written) == ["validation", "signals"]  # merged, pipeline order
    assert written["validation"]["decided_by"] == "template"

    assert runlog.read_text() == runlog_before  # the pipeline's log: untouched

    narrate_log = clone / str(AS_OF) / "narrate_runlog.jsonl"
    narrate_events = [json.loads(l) for l in narrate_log.read_text().splitlines()]
    assert all(e["step"] == "narrate" for e in narrate_events)
    assert {e["event"] for e in narrate_events} >= {
        "run_start", "narrative_number_check", "narrative_language_check", "narrative", "summary",
    }
    # Two invocations appended to one file — the second never truncated the first.
    assert sum(1 for e in narrate_events if e["event"] == "run_start") == 2


# ---------------------------------------------------------------------------
# 7. Streaming — the run narrating itself, live
# ---------------------------------------------------------------------------
#
# A narrative is an agentic message: it is a model reading this run's artifacts
# and talking about them, so it types like one. The checks are untouched — the
# streamed text is a draft, and only the checked narrative is returned.


def test_narration_streams_deltas_then_returns_the_checked_text(monkeypatch):
    reply = GOOD_SIGNALS
    halfway = reply.index("• Daniel")

    def streaming(mode, prompt, on_delta=None):
        for piece in (reply[:halfway], reply[halfway:]):
            if on_delta:
                on_delta(piece)
        return reply

    monkeypatch.setattr(narrate, "call_llm", streaming)
    events = []
    doc = narrate.narrate(AS_OF, mode="claude-cli", phases=["signals"], write=False,
                          on_event=events.append)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "generating"
    assert kinds.count("delta") == 2
    assert kinds.index("verifying") > kinds.index("delta")
    # Every event names its phase — phases are narrated concurrently, and a
    # watcher has to keep the streams apart.
    assert {e["phase"] for e in events} == {"signals"}
    assert doc["signals"]["text"] == GOOD_SIGNALS_TEXT
    assert doc["signals"]["checks"]["number_check"] == "pass"


def test_a_rejected_narration_draft_is_announced_and_regenerated(monkeypatch):
    replies = [
        "NARRATIVE: The engine found 9137.42 problems this Monday.",
        GOOD_SIGNALS,
    ]

    def flaky(mode, prompt, on_delta=None):
        # The good reply repeats if the loop ever asks again, so a regression
        # shows up as a failed assertion rather than an IndexError.
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if on_delta:
            on_delta(reply)
        return reply

    monkeypatch.setattr(narrate, "call_llm", flaky)
    events = []
    doc = narrate.narrate(AS_OF, mode="claude-cli", phases=["signals"], write=False,
                          on_event=events.append)

    redos = [e for e in events if e["type"] == "redo"]
    assert redos and redos[0]["phase"] == "signals"
    assert "9137.42" in redos[0]["reason"]
    # The rejected figure never reaches the narrative that is returned.
    assert "9137.42" not in doc["signals"]["text"]


def test_template_narratives_never_fake_stream():
    """A deterministic narrative is assembled from artifact values. Emitting
    deltas for it would imply a model typed something no model touched."""
    events = []
    doc = narrate.narrate(AS_OF, mode="template", phases=["signals"], write=False,
                          on_event=events.append)
    assert doc["signals"]["decided_by"] == "template"
    assert events == []


# ---------------------------------------------------------------------------
# 8. The voice — what earns a place in a leader's bubble
# ---------------------------------------------------------------------------
#
# The bubble says what was DECIDED or FLAGGED: a lead line, then bullets with a
# center, a metric and the numbers. Everything else — how many combinations
# were scored, what was held back, which check ran — belongs in the log. These
# tests hold that line for the deterministic floor AND for anything a model
# writes, because both go through the same five checks.


@pytest.fixture(scope="module")
def prior_template_doc():
    return narrate.narrate(PRIOR_MONDAY, mode="template", write=False)


@pytest.mark.parametrize("as_of_fixture", ["template_doc", "prior_template_doc"])
def test_template_bubbles_are_lead_line_plus_bullets_on_both_mondays(as_of_fixture, request):
    """Both committed Mondays, every phase: the shape holds and all five
    checks pass — the floor is never the thing that breaks the format."""
    doc = request.getfixturevalue(as_of_fixture)
    for phase, result in doc.items():
        lines = result["text"].splitlines()
        assert not lines[0].startswith("•"), (phase, lines[0])
        assert len(lines) > 1, phase
        assert all(ln.startswith("•") for ln in lines[1:]), (phase, lines)
        assert len(result["text"].split()) <= narrate.MAX_WORDS, phase
        assert result["checks"]["entity_check"] == "pass", phase
        assert result["checks"]["rule_check"] == "pass", phase
        assert result["checks"]["shape_check"] == "pass", phase


def test_shape_check_rejects_a_paragraph_and_a_wall_of_bullets():
    paragraph = narrate.check_shape("The run went well and everything was verified.")
    assert not paragraph["ok"]
    assert "bullet" in " ".join(paragraph["problems"])

    too_many = narrate.check_shape("Data checked.\n" + "\n".join(f"• item {i}" for i in range(9)))
    assert not too_many["ok"]

    too_long = narrate.check_shape("Data checked.\n• " + " ".join(["word"] * 130))
    assert not too_long["ok"]
    assert "words" in " ".join(too_long["problems"])

    good = narrate.check_shape("Data checked. 2 corrections:\n• dropped 9 rows\n• set 11 to missing")
    assert good["ok"] and good["bullets"] == 2


def test_entity_check_accepts_real_centers_and_rejects_invented_ones(bundle):
    index = narrate.build_index(bundle, "signals")
    good = narrate.check_entities("Mount Pleasant — staff call-outs 7.2", index)
    assert good["ok"] and "Mount Pleasant" in good["verified"]

    # A name this run's artifacts never contain — the failure the number check
    # cannot see, because "Fernwood Commons 7.2" is arithmetically fine.
    invented = narrate.check_entities("Fernwood Commons — staff call-outs 7.2", index)
    assert not invented["ok"]
    assert "Fernwood Commons" in " ".join(invented["problems"])

    leaked = narrate.check_entities("PCC_006 — staff call-outs 7.2", index)
    assert not leaked["ok"]


def test_signals_rule_check_names_every_ranked_center_and_no_suppressed_one(bundle):
    ranked = narrate.check_phase_rules(
        "signals",
        "Flagged:\n• Mount Pleasant\n• Daniel Island\n• Morrisville",
        bundle,
    )
    assert ranked["ok"]

    missing = narrate.check_phase_rules("signals", "Flagged:\n• Mount Pleasant", bundle)
    assert not missing["ok"]
    assert "Morrisville" in " ".join(missing["problems"])

    # Verdae's CSAT was suppressed, not flagged. A bubble that names it is
    # narrating what the engine chose NOT to raise.
    suppressed = narrate.check_phase_rules(
        "signals",
        "Flagged:\n• Mount Pleasant\n• Daniel Island\n• Morrisville\n• Verdae — CSAT 3.54",
        bundle,
    )
    assert not suppressed["ok"]
    assert "Verdae" in " ".join(suppressed["problems"])


def test_verdicts_rule_check_refuses_to_refile_a_plan(bundle):
    ok = narrate.check_phase_rules(
        "verdicts", "10 plans verified.\n• On track: Cotswold rechecks", bundle
    )
    assert ok["ok"]

    # Cotswold's recheck plan is the one genuinely ON TRACK; calling it not
    # working contradicts the deterministic verdict table.
    wrong = narrate.check_phase_rules(
        "verdicts", "10 plans verified.\n• Not working: Cotswold rechecks 66.0% → 76.4%", bundle
    )
    assert not wrong["ok"]
    assert "Cotswold" in " ".join(wrong["problems"])

    # Packed on one line still checks, and one bullet per plan does too.
    mixed = narrate.check_phase_rules(
        "verdicts",
        "10 plans verified.\n• Not working, claimed on track: Morrisville records 90.5% → 77.7% "
        "· Brier Creek rechecks 74.0% → 69.8% · Verdae throughput 2.50 → 2.25",
        bundle,
    )
    assert mixed["ok"], mixed["problems"]

    split = narrate.check_phase_rules(
        "verdicts",
        "10 plans verified.\n"
        "• Not working, claimed on track: Morrisville records 90.5% → 77.7%\n"
        "• Not working, claimed on track: Brier Creek rechecks 74.0% → 69.8%\n"
        "• Not working, claimed on track: Verdae throughput 2.50 → 2.25",
        bundle,
    )
    assert split["ok"], split["problems"]


# ---------------------------------------------------------------------------
# 9. The receipt map — every number and name traced to a file and a field
# ---------------------------------------------------------------------------


def test_every_bubble_carries_receipts_that_point_at_real_files(template_doc):
    for phase, result in template_doc.items():
        receipts = result["receipts"]
        assert receipts, phase
        for r in receipts:
            assert set(r) == {"token", "path", "field", "value"}, (phase, r)
            assert (config.REPO_ROOT / r["path"]).exists(), (phase, r["path"])
            assert r["token"] in result["text"], (phase, r["token"])
        # What the panel prints on the "read:" line is a real file too.
        for path in result["artifacts_read"]:
            assert (config.REPO_ROOT / path).exists(), (phase, path)


def test_receipts_point_at_the_field_the_bullet_actually_means(template_doc):
    """Provenance, not a lucky match: the digest's ledger counts resolve to the
    ledger, and a verdict's baseline to that plan's fact row."""
    receipts = template_doc["digest"]["receipts"]
    fields = {r["field"] for r in receipts}
    # The ledger bullet's numbers resolve to the ledger's own counts, not to a
    # same-valued number that happens to sit somewhere else in the run.
    assert any(f.startswith("ledger.counts.") for f in fields), fields
    created = next(r for r in receipts if r["field"] == "ledger.counts.created")
    # ...and the token really is the created count, read back from the artifact.
    doc = json.loads((config.OUTPUTS_DIR / str(AS_OF) / "digest.json").read_text())
    assert created["token"] == str(doc["ledger"]["counts"]["created"])

    verdicts_receipts = {r["token"]: r for r in template_doc["verdicts"]["receipts"]}
    assert verdicts_receipts["90.5"]["field"].endswith("baseline")
    assert verdicts_receipts["10"]["field"] == "counts.plans"
    assert verdicts_receipts["Morrisville"]["path"].endswith("verdicts.json")


def test_every_number_in_a_bubble_gets_a_receipt(template_doc, bundle):
    """A figure without provenance is a figure a reviewer cannot check."""
    for phase, result in template_doc.items():
        receipted = {r["token"] for r in result["receipts"]}
        cleaned = harness.ID_TOKEN_RE.sub(" ", result["text"])
        tokens = set(harness.NUM_RE.findall(harness.DATE_RE.sub(" ", cleaned)))
        assert tokens <= receipted, (phase, tokens - receipted)


def test_receipt_counts_match_the_reported_check_totals(template_doc):
    for phase, result in template_doc.items():
        assert result["checks"]["numbers_verified"] == result["numbers_verified"]
        assert result["checks"]["entities_verified"] == len(
            {r["token"] for r in result["receipts"] if not r["token"][0].isdigit()}
        ), phase


# ---------------------------------------------------------------------------
# Validation phase: the maturity-tier relabel is the one bullet that names
# centers, and grouping them under the wrong tier is a factual error every
# other check passes — the names are real and the counts exist. Seen from a
# live OpenAI run: "4 ramping centers (…, Daniel Island)" when Daniel Island
# was relabeled new.
# ---------------------------------------------------------------------------


def _validation_bundle():
    return narrate.load_bundle(date(2026, 5, 4), ["validation"])


def test_relabel_bullet_grouping_the_wrong_way_is_rejected():
    bundle = _validation_bundle()
    text = (
        "Data checked. The pass made 4 corrections:\n"
        "• relabeled maturity tier for 4 ramping centers (Dilworth, Steele Creek, "
        "Mount Pleasant, Daniel Island) and 3 new centers (Brier Creek, Pelham Row, "
        "Cary Crossing)"
    )
    result = narrate.check_phase_rules("validation", text, bundle)
    assert not result["ok"]
    joined = " ".join(result["problems"])
    assert "Daniel Island was relabeled new" in joined
    assert "Cary Crossing was relabeled mature" in joined


@pytest.mark.parametrize(
    "bullet",
    [
        "• relabeled maturity tier for 7 centers: Dilworth, Steele Creek, Mount Pleasant "
        "→ ramping; Brier Creek, Daniel Island, Pelham Row → new; Cary Crossing → mature",
        "• relabeled maturity tier for 7 centers: ramping — Dilworth, Steele Creek, "
        "Mount Pleasant; new — Brier Creek, Daniel Island, Pelham Row; mature — Cary Crossing",
    ],
)
def test_relabel_bullet_grouped_correctly_passes(bullet):
    bundle = _validation_bundle()
    result = narrate.check_phase_rules("validation", "Data checked.\n" + bullet, bundle)
    assert result["ok"], result["problems"]


# ---------------------------------------------------------------------------
# The structured tool output the agent reasons over
# ---------------------------------------------------------------------------


def test_digest_tool_result_counts_what_the_digest_actually_carries(bundle):
    """The digest's ranked items are `top_signals` and its plan verdicts are the
    rows of `claimed_vs_verified`. Reading "signals"/"verdicts" reported 0 and 0
    for a run carrying 3 and 10 — a tool result that contradicted its artifact."""
    digest = json.loads(
        (config.OUTPUTS_DIR / str(AS_OF) / "digest.json").read_text()
    )
    out = narrate._tool_output("digest", bundle)

    assert out["signal_count"] == len(digest["top_signals"]) == 3
    assert out["verdict_count"] == len(digest["claimed_vs_verified"]["rows"]) == 10
    assert out["suppressed_count"] == len(digest["suppressed"])
    assert out["leader"] == digest["leader"]


def test_every_phase_hands_the_agent_a_real_tool_result(bundle):
    for phase in narrate.PHASES:
        out = narrate._tool_output(phase, bundle)
        assert out.get("note") != "no structured output", phase
