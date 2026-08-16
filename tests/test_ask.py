""""Ask this Monday" tests — grounding, refusals, and the tier ladder.

Four things must hold for a Q&A surface to be allowed near a leader:

1. **Grounded.** Every answer comes from one run's artifacts, every number in
   it verifies against those artifacts, and it cites which artifact grounds it.
2. **Refuses specifically.** Out-of-scope questions (HR, predictions, topics
   the data has nothing to say about) get a named reason, never a guess.
3. **Never fabricates under pressure.** A model that keeps citing numbers the
   artifacts don't contain gets its answer thrown away and the leader gets an
   honest refusal pointing at the receipts.
4. **Degrades honestly.** An unreachable model (usage limit, timeout) drops to
   the tier below and says so — it never answers as if Claude wrote it.

These run against the real committed artifacts for 2026-05-04.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import date

import pytest

from pipeline import ask, harness, verdicts

AS_OF = date(2026, 5, 4)


@pytest.fixture(scope="module")
def context():
    return ask.load_context(AS_OF)


@pytest.fixture(scope="module")
def pool(context):
    return ask.build_pool(context)


# ---------------------------------------------------------------------------
# 1. Context + grounding
# ---------------------------------------------------------------------------


def test_context_loads_every_artifact(context):
    assert context["missing"] == []
    assert set(context["artifacts"]) == set(ask.ARTIFACT_LABELS)


def test_context_scope_is_one_run_only(context):
    """The answer surface sees this Monday's digest and nothing else."""
    digest = context["artifacts"]["digest.json"]
    assert digest["as_of"] == str(AS_OF)
    signals = context["artifacts"]["signals.json"]
    assert signals["as_of"] == str(AS_OF)
    for event in context["artifacts"]["ledger"]["events"]:
        assert event["as_of"] == str(AS_OF)


def test_pool_includes_collection_sizes(context, pool):
    """An answer may say "your 11 centers" or "3 ranked signals", so those
    counts belong in the verifiable pool."""
    numbers, _ = pool
    digest = context["artifacts"]["digest.json"]
    assert float(len(digest["centers"])) in numbers
    assert float(len(digest["top_signals"])) in numbers


# ---------------------------------------------------------------------------
# 2. Template tier — deterministic, grounded, verified
# ---------------------------------------------------------------------------

ROUTABLE_QUESTIONS = [
    "what was suppressed and why?",
    "what changed since last Monday?",
    "why did Morrisville rank first?",
    "why did AP_001 get its verdict?",
    "what earned attention this week?",
]


@pytest.mark.parametrize("question", ROUTABLE_QUESTIONS)
def test_template_answers_are_grounded_and_verified(question, pool):
    result = ask.answer(question, AS_OF, mode="template", write_log=False)
    assert not result["refused"], result["refusal_reason"]
    assert result["numbers_verified"] > 0
    assert result["citations"]

    # Independently re-check the harness claim: every number in the answer and
    # its citations must exist in this run's artifacts.
    numbers, dates = pool
    text = result["answer"] + " " + " ".join(c["detail"] for c in result["citations"])
    assert harness.check_numbers_pool(text, numbers, dates)["ok"]


@pytest.mark.parametrize("question", ROUTABLE_QUESTIONS)
def test_citations_name_real_artifacts(question):
    result = ask.answer(question, AS_OF, mode="template", write_log=False)
    for citation in result["citations"]:
        assert citation["artifact"] in ask.ARTIFACT_LABELS
        assert citation["detail"]


@pytest.mark.parametrize("question", ROUTABLE_QUESTIONS)
def test_answers_never_leak_ids_to_a_leader(question):
    result = ask.answer(question, AS_OF, mode="template", write_log=False)
    assert "PCC_" not in result["answer"]
    assert "DVM_" not in result["answer"]


# ---------------------------------------------------------------------------
# 3. Refusals — specific, by design
# ---------------------------------------------------------------------------


def test_hr_question_is_refused_as_out_of_scope():
    result = ask.answer(
        "should I fire the doctor at Mount Pleasant?", AS_OF, mode="template", write_log=False
    )
    assert result["refused"]
    assert "staffing" in result["refusal_reason"].lower()


def test_prediction_question_is_refused():
    result = ask.answer(
        "will Morrisville's record completion get worse next month?",
        AS_OF, mode="template", write_log=False,
    )
    assert result["refused"]
    assert "forecast" in result["refusal_reason"].lower()


def test_offtopic_question_is_refused():
    result = ask.answer("what's the weather in Charlotte?", AS_OF, mode="template", write_log=False)
    assert result["refused"]
    assert "no data on that topic" in result["refusal_reason"].lower()


def test_out_of_scope_refusal_credits_the_guard_not_the_model(monkeypatch):
    """An out-of-scope question never reaches a model, so the result must not
    imply one wrote the refusal — the panel labels answers by `decided_by`."""
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt: pytest.fail("a model was called for an out-of-scope question"),
    )
    result = ask.answer(
        "should we fire the Mount Pleasant manager?", AS_OF, mode="claude-cli", write_log=False
    )
    assert result["refused"]
    assert result["mode"] == "claude-cli"   # the tier this run was configured for
    assert result["decided_by"] == "guard"  # what actually produced the refusal


def test_answered_question_is_credited_to_the_tier_that_wrote_it():
    result = ask.answer("what was suppressed?", AS_OF, mode="template", write_log=False)
    assert not result["refused"]
    assert result["decided_by"] == "template"


def test_missing_run_refusal_names_what_is_missing_and_how_to_fix_it():
    result = ask.answer("what earned attention?", date(2026, 6, 1), mode="template", write_log=False)
    assert result["refused"]
    assert "No complete run exists for 2026-06-01" in result["refusal_reason"]
    assert "python -m pipeline.run --as-of 2026-06-01" in result["refusal_reason"]


def test_unroutable_template_question_names_the_tier_limitation():
    result = ask.answer(
        "how does revenue per appointment compare to the industry?",
        AS_OF, mode="template", write_log=False,
    )
    assert result["refused"]
    assert ask.TEMPLATE_SCOPE in result["refusal_reason"]


# ---------------------------------------------------------------------------
# 4. LLM reply parsing — the shape contract with PROMPTS/answer.md
# ---------------------------------------------------------------------------


def test_parse_answer_with_citations():
    parsed = ask.parse_llm_answer(
        "ANSWER: Call-outs at Mount Pleasant tripled over three weeks.\n"
        "CITATIONS:\n"
        "- signals.json: rank 3, staff call-outs, priority 6.49\n"
    )
    assert not parsed["refused"]
    assert parsed["citations"] == [
        {"artifact": "signals.json", "detail": "rank 3, staff call-outs, priority 6.49"}
    ]


def test_parse_refusal():
    parsed = ask.parse_llm_answer("REFUSED: this run has no shift-schedule data.")
    assert parsed["refused"]
    assert parsed["refusal_reason"] == "this run has no shift-schedule data."


def test_answer_without_citations_is_rejected():
    with pytest.raises(ValueError, match="CITATIONS"):
        ask.parse_llm_answer("ANSWER: Everything looks fine.")


def test_citation_to_an_unknown_artifact_is_rejected():
    with pytest.raises(ValueError, match="unknown artifacts"):
        ask.parse_llm_answer(
            "ANSWER: Call-outs tripled.\nCITATIONS:\n- payroll_system: shift records\n"
        )


def test_empty_reply_is_rejected():
    with pytest.raises(ValueError):
        ask.parse_llm_answer("Sure, here's what I think.")


# ---------------------------------------------------------------------------
# 5. The harness holds the line on a model that fabricates
# ---------------------------------------------------------------------------


def test_hallucinated_number_is_refused_never_answered(monkeypatch):
    """A model citing a figure this run never produced is retried, then
    refused — the leader never sees the number."""
    calls = {"n": 0}

    def fabricating(mode, prompt):
        calls["n"] += 1
        return (
            "ANSWER: Call-outs at Mount Pleasant hit 9137.42 last week.\n"
            "CITATIONS:\n- signals.json: staff call-outs at Mount Pleasant\n"
        )

    monkeypatch.setattr(ask, "call_llm", fabricating)
    result = ask.answer("why did Mount Pleasant rank?", AS_OF, mode="claude-cli", write_log=False)

    assert result["refused"]
    assert result["answer"] is None
    assert "9137.42" in result["refusal_reason"]
    assert calls["n"] == harness.MAX_REGENERATIONS + 1  # initial + 2 retries


def test_number_check_pool_is_the_whole_run_not_the_cited_artifact(context, pool):
    """The honest boundary of the pooled check, asserted so nobody oversells
    it: the pool is every value anywhere in this run's artifacts, so a figure
    that is real *somewhere* in the run passes even when it is wrong for the
    metric being discussed. What the check guarantees is scope — an answer can
    only speak in numbers this run produced. The tight, per-sentence guarantee
    lives in the verdict harness, which checks one sentence against one plan's
    fact row."""
    numbers, dates = pool
    # A real value from this run (a signal's priority score), misused in a
    # sentence about an unrelated metric: the pool check cannot catch this.
    real_value = context["artifacts"]["digest.json"]["top_signals"][0]["priority"]
    misused = f"Client wait times averaged {real_value} minutes."
    assert harness.check_numbers_pool(misused, numbers, dates)["ok"]

    # A figure the run never produced anywhere: caught.
    assert not harness.check_numbers_pool("Wait times averaged 9137.42.", numbers, dates)["ok"]


def test_model_refusal_is_passed_through_verbatim(monkeypatch):
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt: "REFUSED: this run records no reason for the call-outs.",
    )
    result = ask.answer(
        "why were the doctors out at Mount Pleasant?", AS_OF, mode="claude-cli", write_log=False
    )
    assert result["refused"]
    assert result["refusal_reason"] == "this run records no reason for the call-outs."
    assert result["fallbacks"] == []  # the model answered; no tier was skipped


def test_verified_llm_answer_reports_its_check_count(monkeypatch):
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt: (
            "ANSWER: 4 signals were held back this Monday, each listed with its reason.\n"
            "CITATIONS:\n- signals.json: suppressed list, 4 entries\n"
        ),
    )
    result = ask.answer("what was suppressed?", AS_OF, mode="claude-cli", write_log=False)
    assert not result["refused"], result["refusal_reason"]
    assert result["mode"] == "claude-cli"
    assert result["numbers_verified"] >= 2


# ---------------------------------------------------------------------------
# 6. The tier ladder — an unreachable model costs nobody their answer
# ---------------------------------------------------------------------------


def test_usage_limit_degrades_to_the_tier_below(monkeypatch):
    def rate_limited(mode, prompt):
        raise verdicts.LLMUnavailable(
            "claude CLI exited 1: Claude usage limit reached. Your limit will reset at 8pm."
        )

    monkeypatch.setattr(ask, "call_llm", rate_limited)
    result = ask.answer("what was suppressed?", AS_OF, mode="claude-cli", write_log=False)

    assert not result["refused"], result["refusal_reason"]
    assert result["mode"] == "template"  # answered by the rung below
    assert result["fallbacks"] == [
        {
            "from": "claude-cli",
            "to": "template",
            "reason": "claude CLI exited 1: Claude usage limit reached. "
                      "Your limit will reset at 8pm.",
        }
    ]


def test_usage_limit_does_not_burn_retries(monkeypatch):
    """A quota refusal will not clear on a retry, so the ladder degrades at
    once rather than spending the retry budget on the same wall."""
    calls = {"n": 0}

    def rate_limited(mode, prompt):
        calls["n"] += 1
        raise verdicts.LLMUnavailable("HTTP 429 rate limit exceeded")

    monkeypatch.setattr(ask, "call_llm", rate_limited)
    ask.answer("what was suppressed?", AS_OF, mode="claude-cli", write_log=False)
    assert calls["n"] == 1


def test_degraded_refusal_names_the_real_reason(monkeypatch):
    """A question the template tier cannot route, after a degrade, must blame
    the unreachable model — not pretend template mode was the plan."""
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt: (_ for _ in ()).throw(
            verdicts.LLMUnavailable("claude CLI exited 1: Claude usage limit reached")
        ),
    )
    result = ask.answer(
        "how does revenue per appointment compare to the industry?",
        AS_OF, mode="claude-cli", write_log=False,
    )
    assert result["refused"]
    assert "usage limit reached" in result["refusal_reason"]
    assert "claude-cli could not be reached" in result["refusal_reason"]


def test_no_tier_left_refuses_and_points_at_the_digest(monkeypatch):
    monkeypatch.setattr(verdicts, "fallback_mode", lambda mode: None)
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt: (_ for _ in ()).throw(verdicts.LLMUnavailable("connection refused")),
    )
    result = ask.answer("what was suppressed?", AS_OF, mode="claude-cli", write_log=False)
    assert result["refused"]
    assert "no tier below it" in result["refusal_reason"]
    assert "digest.json" in result["refusal_reason"]


# ---------------------------------------------------------------------------
# 7. The ask log — the product-discovery instrument
# ---------------------------------------------------------------------------


def test_every_question_lands_in_the_ask_log(tmp_path):
    log = tmp_path / "ask_log.jsonl"
    ask.answer("what was suppressed?", AS_OF, mode="template", log_path=log)
    ask.answer("should I fire someone?", AS_OF, mode="template", log_path=log)

    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(lines) == 2
    answered, refused = lines

    assert answered["refused"] is False
    assert answered["question"] == "what was suppressed?"
    assert answered["as_of"] == str(AS_OF)
    assert answered["mode"] == "template"

    # asked-and-refused is the data-gap signal: the reason is what makes it
    # useful, so it is recorded, not just the fact of refusal.
    assert refused["refused"] is True
    assert refused["refusal_reason"]


# ---------------------------------------------------------------------------
# 8. Streaming — the answer as it is written, without loosening a single check
# ---------------------------------------------------------------------------
#
# Streaming changes what a waiting leader can SEE, never what they are told.
# The events are a view of the same loop: text is provisional until the harness
# passes it, a rejected draft is announced and thrown away, and work that never
# reaches a model does not pretend to be written by one.


def _collect(events):
    return lambda event: events.append(event)


def test_streaming_reports_deltas_then_a_validated_final(monkeypatch):
    """The deltas are display; the returned answer is the product."""
    def streaming_llm(mode, prompt, on_delta=None):
        for piece in ("ANSWER: 4 signals were held back ", "this Monday.\n",
                      "CITATIONS:\n- signals.json: suppressed list, 4 entries\n"):
            if on_delta:
                on_delta(piece)
        return (
            "ANSWER: 4 signals were held back this Monday.\n"
            "CITATIONS:\n- signals.json: suppressed list, 4 entries\n"
        )

    monkeypatch.setattr(ask, "call_llm", streaming_llm)
    events = []
    result = ask.answer("what was suppressed?", AS_OF, mode="claude-cli",
                        write_log=False, on_event=_collect(events))

    kinds = [e["type"] for e in events]
    assert kinds[0] == "generating"
    assert kinds.count("delta") == 3
    # Generation ends, then the harness runs — the panel's "verifying…" state.
    assert kinds.index("verifying") > kinds.index("delta")
    assert "".join(e["text"] for e in events if e["type"] == "delta").startswith("ANSWER:")

    assert not result["refused"], result["refusal_reason"]
    assert result["numbers_verified"] >= 1
    assert result["citations"]


def test_streaming_result_is_identical_to_the_non_streaming_one(monkeypatch):
    reply = (
        "ANSWER: 4 signals were held back this Monday.\n"
        "CITATIONS:\n- signals.json: suppressed list, 4 entries\n"
    )
    monkeypatch.setattr(ask, "call_llm", lambda mode, prompt, on_delta=None: reply)
    quiet = ask.answer("what was suppressed?", AS_OF, mode="claude-cli", write_log=False)
    streamed = ask.answer("what was suppressed?", AS_OF, mode="claude-cli",
                          write_log=False, on_event=lambda e: None)
    for key in ("answer", "refused", "citations", "numbers_verified", "mode",
                "decided_by", "fallbacks", "attempts"):
        assert quiet[key] == streamed[key]


def test_a_rejected_draft_is_announced_and_thrown_away(monkeypatch):
    """The harness rejecting a number is visible as it happens — the draft is
    not quietly patched, and the leader is not shown the bad figure as fact."""
    replies = [
        "ANSWER: Call-outs hit 9137.42 last week.\nCITATIONS:\n- signals.json: call-outs\n",
        "ANSWER: 4 signals were held back this Monday.\n"
        "CITATIONS:\n- signals.json: suppressed list, 4 entries\n",
    ]

    def flaky(mode, prompt, on_delta=None):
        reply = replies.pop(0)
        if on_delta:
            on_delta(reply)
        return reply

    monkeypatch.setattr(ask, "call_llm", flaky)
    events = []
    result = ask.answer("what was suppressed?", AS_OF, mode="claude-cli",
                        write_log=False, on_event=_collect(events))

    redos = [e for e in events if e["type"] == "redo"]
    assert len(redos) == 1
    assert redos[0]["kind"] == "unverified_number"
    assert "9137.42" in redos[0]["unknown"]
    # …and the regeneration streams too, so the wait is never silent.
    assert [e["type"] for e in events].count("generating") == 2
    assert not result["refused"], result["refusal_reason"]
    assert "9137.42" not in result["answer"]


def test_deterministic_tier_never_fake_streams():
    """Template answers are assembled, not written. Emitting deltas for them
    would claim a model wrote something no model touched."""
    events = []
    result = ask.answer("what was suppressed?", AS_OF, mode="template",
                        write_log=False, on_event=_collect(events))
    assert not result["refused"]
    assert events == []


def test_guard_refusal_never_streams():
    """An out-of-scope question is decided before any model runs."""
    events = []
    result = ask.answer("should I fire the doctor at Mount Pleasant?", AS_OF,
                        mode="claude-cli", write_log=False, on_event=_collect(events))
    assert result["refused"]
    assert result["decided_by"] == "guard"
    assert events == []


def test_tier_fallback_is_streamed_so_the_panel_can_say_why_it_changed(monkeypatch):
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt, on_delta=None: (_ for _ in ()).throw(
            verdicts.LLMUnavailable("claude CLI exited 1: Claude usage limit reached")
        ),
    )
    events = []
    result = ask.answer("what was suppressed?", AS_OF, mode="claude-cli",
                        write_log=False, on_event=_collect(events))
    fallback = [e for e in events if e["type"] == "tier_fallback"]
    assert fallback and fallback[0]["from"] == "claude-cli" and fallback[0]["to"] == "template"
    # The tier below answered, and nothing streamed as if Claude had written it.
    assert result["decided_by"] == "template"
    assert not any(e["type"] == "delta" for e in events)


# --- the streaming transport, against a stand-in for the real CLI -----------


def _fake_claude(tmp_path, monkeypatch, script: str) -> None:
    """Put a `claude` on PATH that replays canned stream-json output — the
    exact line shapes the real CLI emits with --output-format stream-json
    --verbose --include-partial-messages (verified by running it)."""
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\ncat >/dev/null\n" + script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_stream_transport_forwards_cli_text_deltas(tmp_path, monkeypatch):
    lines = [
        {"type": "system", "subtype": "init", "session_id": "x"},
        {"type": "stream_event",
         "event": {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "ANSWER: hello "}}},
        {"type": "stream_event",
         "event": {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "world"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ANSWER: hello world"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "ANSWER: hello world"},
    ]
    script = "".join(f"echo '{json.dumps(line)}'\n" for line in lines)
    _fake_claude(tmp_path, monkeypatch, script)

    seen = []
    reply = ask.call_claude_cli_stream("prompt", seen.append)
    assert seen == ["ANSWER: hello ", "world"]
    # The CLI's own final text is what the harness gets — never the deltas
    # stitched together, so a dropped chunk cannot truncate an answer.
    assert reply == "ANSWER: hello world"


def test_stream_transport_reports_a_usage_limit_as_a_transport_failure(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch,
                 "echo \"You've hit your session limit · resets 7:40pm\"\nexit 1\n")
    with pytest.raises(RuntimeError, match="session limit"):
        ask.call_claude_cli_stream("prompt", lambda text: None)
    # …which the ladder classifier turns into a degrade, not a wrong answer.
    with pytest.raises(verdicts.LLMUnavailable):
        verdicts.call_guarded(
            "claude-cli", lambda p: ask.call_claude_cli_stream(p, lambda t: None), "prompt"
        )


def test_stream_cli_prints_jsonl_whose_last_line_is_the_whole_result(tmp_path, monkeypatch, capsys):
    """The API layer's contract with this module: one JSON object per line, and
    the last one is the validated result — identical to what --json prints, so
    a non-streaming caller loses nothing."""
    monkeypatch.setattr(ask, "ASK_LOG_PATH", tmp_path / "ask_log.jsonl")
    ask.main(["--as-of", str(AS_OF), "--llm-mode", "template", "--stream", "what was suppressed?"])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert [l["type"] for l in lines] == ["final"]  # deterministic tier: no fake typing
    assert lines[0]["refused"] is False
    assert lines[0]["decided_by"] == "template"

    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt, on_delta=None: (
            on_delta("ANSWER: 4 signals were held back this Monday.\n") if on_delta else None,
            "ANSWER: 4 signals were held back this Monday.\n"
            "CITATIONS:\n- signals.json: suppressed list, 4 entries\n",
        )[1],
    )
    ask.main(["--as-of", str(AS_OF), "--llm-mode", "claude-cli", "--stream", "what was suppressed?"])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert [l["type"] for l in lines[:2]] == ["generating", "delta"]
    assert lines[-1]["type"] == "final" and lines[-1]["numbers_verified"] >= 1


def test_ask_log_records_which_tier_answered(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt: (_ for _ in ()).throw(
            verdicts.LLMUnavailable("claude CLI exited 1: Claude usage limit reached")
        ),
    )
    log = tmp_path / "ask_log.jsonl"
    ask.answer("what changed since last Monday?", AS_OF, mode="claude-cli", log_path=log)

    entry = json.loads(log.read_text().splitlines()[0])
    assert entry["mode"] == "template"
    assert entry["fallbacks"][0]["from"] == "claude-cli"
