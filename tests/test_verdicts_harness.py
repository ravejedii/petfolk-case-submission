"""Claimed vs. Verified tests — ground-truth verdicts + harness attacks.

Two halves:

1. Ground truth (real DATA/TRANSLATION/, template mode, no files written): Priya's 10
   plans must land exactly on these verdict counts —
   3 NOT WORKING (AP_001, AP_002, AP_027) · 1 ABANDONED (AP_003) ·
   5 EXCEEDED (AP_004, 007, 009, 019, 024) · 1 ON TRACK Agree (AP_020).

2. Harness attacks: inject hallucinated numbers and wrong conclusions into
   generated output and confirm every one is caught, retried, and hard-failed.
   The harness is the reason any LLM mode is allowed near a leader's digest;
   these tests are the proof it holds the line.
"""

from __future__ import annotations

from datetime import date

import pytest

from pipeline import harness, verdicts

AS_OF = date(2026, 5, 4)
LEADER = "Dr. Priya Raghunathan"

EXPECTED_BUCKETS = {
    "AP_001": "NOT WORKING",  # records 90.5 -> 77.7 at Morrisville
    "AP_002": "NOT WORKING",  # rechecks 74.0 -> 69.8 at Brier Creek
    "AP_003": "ABANDONED",  # overdue 15d, untouched 71d at Mount Pleasant
    "AP_004": "EXCEEDED",
    "AP_007": "EXCEEDED",
    "AP_009": "EXCEEDED",
    "AP_019": "EXCEEDED",
    "AP_020": "ON TRACK",  # the one genuinely on-track plan
    "AP_024": "EXCEEDED",
    "AP_027": "NOT WORKING",  # throughput 2.5 -> 2.25 at Verdae
}
EXPECTED_AGREE = {"AP_009", "AP_020"}  # complete+EXCEEDED, on_track+ON TRACK


@pytest.fixture(scope="module")
def doc():
    """One template-mode run against real DATA/TRANSLATION/, nothing written to disk."""
    return verdicts.run(AS_OF, LEADER, mode="template", write=False)


@pytest.fixture(scope="module")
def by_plan(doc):
    return {v["plan_id"]: v for v in doc["verdicts"]}


# ---------------------------------------------------------------------------
# 1. Ground truth
# ---------------------------------------------------------------------------


def test_every_priya_plan_judged(doc):
    assert doc["counts"]["plans"] == 10
    assert sorted(v["plan_id"] for v in doc["verdicts"]) == sorted(EXPECTED_BUCKETS)


def test_ground_truth_buckets(by_plan):
    got = {pid: v["ai_verdict"]["bucket"] for pid, v in by_plan.items()}
    assert got == EXPECTED_BUCKETS


def test_agree_flags(by_plan):
    agree = {pid for pid, v in by_plan.items() if v["ai_verdict"]["agree"]}
    assert agree == EXPECTED_AGREE
    assert by_plan["AP_020"]["ai_verdict"]["verdict_display"] == "ON TRACK (Agree)"
    assert by_plan["AP_009"]["ai_verdict"]["verdict_display"] == "EXCEEDED (Agree)"


def test_exceeded_actions_recommend_closing(by_plan):
    for pid, bucket in EXPECTED_BUCKETS.items():
        if bucket == "EXCEEDED":
            action = by_plan[pid]["ai_verdict"]["recommended_action"].lower()
            assert any(w in action for w in harness.CLOSE_WORDS), pid


def test_leader_facing_output_never_shows_location_ids(by_plan):
    for v in by_plan.values():
        av = v["ai_verdict"]
        for text in (av["sentence"], av["recommended_action"], v["what_the_numbers_say"]["summary"]):
            assert "PCC_" not in text


def test_template_passes_harness_first_try(doc):
    assert doc["counts"]["regenerations_total"] == 0
    for v in doc["verdicts"]:
        assert v["ai_verdict"]["harness"]["number_check"] == "pass"
        assert v["ai_verdict"]["harness"]["reasoning_check"] == "pass"


# ---------------------------------------------------------------------------
# 2. Harness attacks — number check
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fact_ap001(by_plan):
    """Rebuild the full AP_001 fact row (the doc keeps a trimmed copy)."""
    import pipeline.facts as facts

    rows = facts.build_facts(AS_OF, write=False)["plans"]
    return next(f for f in rows if f["plan_id"] == "AP_001")


def test_number_check_accepts_true_numbers(fact_ap001):
    ok = harness.check_numbers(
        "Records at Morrisville averaged 77.7% over the last 4 weeks, "
        "down 12.8 points from the 90.5% baseline (target 94.0%).",
        fact_ap001,
    )
    assert ok["ok"], ok


def test_number_check_accepts_rounding_to_displayed_precision(fact_ap001):
    # gap_closed_pct is -365.7; citing it as a whole "366%" magnitude is fine.
    assert harness.check_numbers("The gap widened by 366%.", fact_ap001)["ok"]


def test_number_check_rejects_hallucinated_number(fact_ap001):
    result = harness.check_numbers(
        "Records at Morrisville averaged 88.3% over the last 4 weeks.", fact_ap001
    )
    assert not result["ok"]
    assert "88.3" in result["unknown"]


def test_number_check_rejects_unknown_date(fact_ap001):
    result = harness.check_numbers("The plan was opened on 2026-02-14.", fact_ap001)
    assert not result["ok"]
    assert "2026-02-14" in result["unknown"]


def test_number_check_allows_dates_from_the_fact_row(fact_ap001):
    assert harness.check_numbers(f"Due {fact_ap001['due_date']}.", fact_ap001)["ok"]


# ---------------------------------------------------------------------------
# 2. Harness attacks — reasoning rule table
# ---------------------------------------------------------------------------


def _fact(**over):
    base = {
        "below_baseline": False,
        "target_met": False,
        "is_overdue": False,
        "days_overdue": 0,
        "staleness_days": 0,
    }
    base.update(over)
    return base


def test_below_baseline_cannot_be_on_track():
    res = harness.check_reasoning(_fact(below_baseline=True), "ON TRACK", "keep going")
    assert not res["ok"]
    assert any(f["rule"] == "below_baseline_not_on_track" for f in res["failures"])


def test_target_met_must_be_exceeded():
    res = harness.check_reasoning(_fact(target_met=True), "ON TRACK", "keep going")
    assert any(f["rule"] == "target_met_must_be_exceeded" for f in res["failures"])


def test_exceeded_must_recommend_closing():
    res = harness.check_reasoning(_fact(target_met=True), "EXCEEDED", "keep monitoring weekly")
    assert any(f["rule"] == "exceeded_must_recommend_close" for f in res["failures"])
    ok = harness.check_reasoning(_fact(target_met=True), "EXCEEDED", "Close the plan and credit the team.")
    assert ok["ok"]


def test_overdue_and_stale_must_be_abandoned():
    fact = _fact(is_overdue=True, days_overdue=15, staleness_days=71)
    res = harness.check_reasoning(fact, "NOT WORKING", "rework it")
    assert any(f["rule"] == "overdue_stale_must_be_abandoned" for f in res["failures"])
    assert harness.check_reasoning(fact, "ABANDONED", "relaunch or close it")["ok"]


def test_exceeded_requires_target_met():
    res = harness.check_reasoning(_fact(), "EXCEEDED", "close it and credit the team")
    assert any(f["rule"] == "exceeded_requires_target_met" for f in res["failures"])


def test_abandoned_requires_overdue_and_stale():
    res = harness.check_reasoning(_fact(staleness_days=50), "ABANDONED", "close it")
    assert any(f["rule"] == "abandoned_requires_overdue_and_stale" for f in res["failures"])


# ---------------------------------------------------------------------------
# 2. Harness attacks — end-to-end retry then hard fail
# ---------------------------------------------------------------------------


def test_persistent_bad_number_hard_fails_after_max_retries(fact_ap001, monkeypatch):
    calls = {"n": 0}

    def poisoned(fact):
        calls["n"] += 1
        return ("NOT WORKING", "Records averaged 88.3% recently.", "Rework the plan.")

    monkeypatch.setattr(verdicts, "generate_template", poisoned)
    log = harness.RunLog(None, step="test")
    with pytest.raises(harness.HarnessError):
        verdicts.judge_plan(fact_ap001, "template", None, log, extra_dates=())
    assert calls["n"] == harness.MAX_REGENERATIONS + 1  # initial + 2 retries


def test_wrong_conclusion_hard_fails_even_with_clean_numbers(fact_ap001, monkeypatch):
    def rosy(fact):
        return (
            "ON TRACK",
            "Records at Morrisville averaged 77.7% over the last 4 weeks.",
            "Keep the plan running.",
        )

    monkeypatch.setattr(verdicts, "generate_template", rosy)
    log = harness.RunLog(None, step="test")
    with pytest.raises(harness.HarnessError):
        verdicts.judge_plan(fact_ap001, "template", None, log, extra_dates=())


# ---------------------------------------------------------------------------
# 3. The tier ladder — an unreachable model must not cost a leader the digest
#
# Transport failures (usage limit, timeout, missing CLI) are not content
# failures: nothing was generated, so there is nothing for the harness to
# check. The run degrades one rung and names the degradation. Content failures
# keep the behavior above: retry, then fail loudly.
# ---------------------------------------------------------------------------

PROMPT_STUB = "{{PLAN_FACTS_JSON}} {{REPORTED_STATUS}} {{RETRY_NOTE}}"


def test_usage_limit_degrades_the_plan_to_the_next_tier(fact_ap001, monkeypatch):
    calls = {"n": 0}

    def rate_limited(mode, prompt):
        calls["n"] += 1
        raise verdicts.LLMUnavailable("claude CLI exited 1: Claude usage limit reached")

    monkeypatch.setattr(verdicts, "call_llm", rate_limited)
    log = harness.RunLog(None, step="test")
    verdict = verdicts.judge_plan(fact_ap001, "claude-cli", PROMPT_STUB, log, extra_dates=())

    assert verdict["mode"] == "template"  # written by the rung below
    assert verdict["bucket"] == "NOT WORKING"  # and still the right answer
    assert verdict["mode_fallbacks"][0]["from"] == "claude-cli"
    assert "usage limit" in verdict["mode_fallbacks"][0]["reason"]
    assert calls["n"] == 1  # a quota refusal does not clear on a retry


def test_transient_failure_retries_the_same_tier_before_degrading(fact_ap001, monkeypatch):
    calls = {"n": 0}

    def flaky(mode, prompt):
        calls["n"] += 1
        raise verdicts.LLMUnavailable("connection reset by peer")

    monkeypatch.setattr(verdicts, "call_llm", flaky)
    log = harness.RunLog(None, step="test")
    verdict = verdicts.judge_plan(fact_ap001, "claude-cli", PROMPT_STUB, log, extra_dates=())

    assert calls["n"] == 2  # one retry covers a blip, then the ladder moves
    assert verdict["mode"] == "template"


def test_a_recovering_model_keeps_its_own_tier(fact_ap001, monkeypatch):
    """One blip must not permanently demote a plan to the deterministic tier."""
    calls = {"n": 0}

    def recovers(mode, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise verdicts.LLMUnavailable("connection reset by peer")
        return (
            "BUCKET: NOT WORKING\n"
            "WHY: Records at Morrisville averaged 77.7% over the last 4 weeks, "
            "below the 90.5% baseline.\n"
            "ACTION: Rework the plan this week with a fresh check-by date.\n"
        )

    monkeypatch.setattr(verdicts, "call_llm", recovers)
    log = harness.RunLog(None, step="test")
    verdict = verdicts.judge_plan(fact_ap001, "claude-cli", PROMPT_STUB, log, extra_dates=())

    assert verdict["mode"] == "claude-cli"
    assert "mode_fallbacks" not in verdict


def test_malformed_answers_never_degrade_the_tier(fact_ap001, monkeypatch):
    """A model that answers badly is a content failure: the harness retries and
    then fails the run loudly. Silently dropping to template would hide a
    broken model behind deterministic prose."""
    calls = {"n": 0}

    def garbage(mode, prompt):
        calls["n"] += 1
        return "Sure! Here's my take: things look great."

    monkeypatch.setattr(verdicts, "call_llm", garbage)
    log = harness.RunLog(None, step="test")
    with pytest.raises(harness.HarnessError):
        verdicts.judge_plan(fact_ap001, "claude-cli", PROMPT_STUB, log, extra_dates=())
    assert calls["n"] == harness.MAX_REGENERATIONS + 1


def test_run_records_which_tier_wrote_each_verdict(monkeypatch):
    def rate_limited(mode, prompt):
        raise verdicts.LLMUnavailable("HTTP 429 rate limit exceeded")

    monkeypatch.setattr(verdicts, "call_llm", rate_limited)
    doc = verdicts.run(AS_OF, LEADER, mode="claude-cli", write=False)

    # The whole digest still gets produced — and says exactly who wrote it.
    assert doc["counts"]["plans"] == 10
    assert doc["llm_modes_used"] == {"template": 10}
    assert len(doc["mode_fallbacks"]) == 10
    assert doc["prompt_file"] is None  # no prompt was actually executed
    assert {v["ai_verdict"]["bucket"] for v in doc["verdicts"]} <= set(harness.BUCKETS)


def test_no_tier_below_raises_instead_of_inventing(fact_ap001, monkeypatch):
    monkeypatch.setattr(verdicts, "call_llm", lambda mode, prompt: (_ for _ in ()).throw(
        verdicts.LLMUnavailable("claude CLI is not on PATH")))
    monkeypatch.setattr(verdicts, "fallback_mode", lambda mode: None)
    log = harness.RunLog(None, step="test")
    with pytest.raises(harness.HarnessError, match="no tier left"):
        verdicts.judge_plan(fact_ap001, "claude-cli", PROMPT_STUB, log, extra_dates=())


# ---------------------------------------------------------------------------
# 4. Why a tier dropped must be diagnosable
#
# A degradation is only acceptable if the recorded reason is the real one. A
# whole run once came back blaming a harmless stdin warning while the actual
# failure sat unread on the other stream — these lock that shut.
# ---------------------------------------------------------------------------


def test_failure_message_reports_the_error_not_a_warning_that_precedes_it():
    msg = verdicts.cli_failure_message(
        1,
        stdout="You've hit your session limit. Resets at 8pm.",
        stderr="Warning: no stdin data received in 3s, proceeding without it.",
    )
    assert "session limit" in msg
    assert "no stdin data" not in msg


def test_failure_message_falls_back_to_a_warning_when_nothing_else_was_said():
    msg = verdicts.cli_failure_message(
        1, stdout="", stderr="Warning: no stdin data received in 3s."
    )
    assert "no stdin data" in msg  # better than an empty reason


def test_failure_message_reads_a_usage_limit_printed_on_stdout():
    msg = verdicts.cli_failure_message(1, stdout="Claude usage limit reached", stderr="")
    assert verdicts.USAGE_LIMIT_RE.search(msg), "a quota refusal must be recognizable as one"


def test_cli_never_inherits_stdin(monkeypatch):
    """The CLI is spawned with stdin closed. Inheriting the API server's empty
    pipe makes it wait on input that never arrives, then fail — which is how a
    real run lost every LLM verdict to a stdin warning."""
    seen = {}

    class Done:
        returncode = 0
        stdout = "BUCKET: NOT WORKING\nWHY: x\nACTION: y"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(verdicts.subprocess, "run", fake_run)
    verdicts.call_claude_cli("the prompt")

    assert seen["stdin"] is verdicts.subprocess.DEVNULL
    assert "the prompt" in seen["cmd"]  # sent on argv, so stdin is free to close
