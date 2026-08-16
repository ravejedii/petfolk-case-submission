"""The OpenAI rung of the tier ladder.

Why it exists: the demo runs on a personal Claude subscription, and a
subscription can run out mid-Monday. When it does, the digest must still be
written, the questions must still be answered — and nobody may be told Claude
wrote something Claude did not.

So this tier is held to the same three rules as every other one:

1. **It is a rung, not a replacement.** claude-cli -> api -> openai ->
   template, in that order, and only when a key is actually present.
2. **It is labelled as itself.** Verdicts, narratives and answers produced here
   report mode/decided_by "openai"; the panel shows a different vendor's name
   and the fallback that got there.
3. **It goes through the same harness.** Same prompt file, same number check,
   same regeneration budget. Nothing about the transport relaxes validation.

The transport is stubbed here — these tests never call OpenAI.
"""

from __future__ import annotations

import io
import json
from datetime import date

import pytest

from pipeline import ask, verdicts

AS_OF = date(2026, 5, 4)

KEY = "PETFOLK_OPENAI_API_KEY"


class FakeResponse(io.BytesIO):
    """Just enough of urlopen's return value: a context manager that reads, and
    iterates line by line for the streaming case."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def stub_openai(monkeypatch, *, body: dict | None = None, lines: list[str] | None = None):
    """Capture the request this module would send, and reply with `body`
    (non-streaming) or `lines` (an SSE stream)."""
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["headers"] = dict(request.headers)
        sent["body"] = json.loads(request.data.decode())
        sent["timeout"] = timeout
        if lines is not None:
            return FakeResponse("\n".join(lines).encode())
        return FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(verdicts.urllib.request, "urlopen", fake_urlopen)
    return sent


# ---------------------------------------------------------------------------
# 1. The ladder
# ---------------------------------------------------------------------------


def test_openai_appears_in_the_ladder_only_when_a_key_is_present(monkeypatch):
    assert "openai" not in verdicts.available_modes()  # conftest cleared the keys
    monkeypatch.setenv(KEY, "sk-test")
    assert verdicts.available_modes() == ("openai", "claude-cli", "template")
    # OpenAI is the default when a key is present. Claude stays on the ladder
    # as an explicit choice; a failed OpenAI call degrades to template.
    assert verdicts.detect_mode() == "openai"
    assert verdicts.fallback_mode("openai") == "template"
    assert verdicts.fallback_mode("claude-cli") == "openai"


def test_a_project_key_wins_over_an_unrelated_ambient_one(monkeypatch):
    """This machine already had an NVIDIA `nvapi-…` key in OPENAI_API_KEY. A
    stale ambient key must not be what the fallback tier tries to use."""
    monkeypatch.setenv("OPENAI_API_KEY", "nvapi-something-else")
    monkeypatch.setenv(KEY, "sk-the-project-key")
    assert verdicts.openai_key() == "sk-the-project-key"


# ---------------------------------------------------------------------------
# 2. The transport
# ---------------------------------------------------------------------------


def test_request_is_shaped_for_cost(monkeypatch):
    """Three deliberate choices, each one money: no hidden reasoning pass, a
    bounded completion, and one call (the harness owns retries)."""
    monkeypatch.setenv(KEY, "sk-test")
    sent = stub_openai(monkeypatch, body={"choices": [{"message": {"content": "BUCKET: X"}}]})
    verdicts.call_openai("hello")

    assert sent["url"] == verdicts.OPENAI_URL
    assert sent["body"]["model"] == verdicts.OPENAI_MODEL
    assert sent["body"]["reasoning_effort"] == "none"
    assert sent["body"]["max_completion_tokens"] == verdicts.OPENAI_MAX_TOKENS
    assert sent["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert sent["headers"]["Authorization"] == "Bearer sk-test"


def test_streaming_transport_forwards_each_chunk_and_returns_the_whole_reply(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test")
    chunk = lambda text: "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})
    stub_openai(monkeypatch, lines=[
        chunk("ANSWER: 4 signals "), chunk("were held back."), "data: [DONE]",
    ])

    seen = []
    reply = verdicts.call_openai_stream("prompt", seen.append)
    assert seen == ["ANSWER: 4 signals ", "were held back."]
    assert reply == "ANSWER: 4 signals were held back."


def test_a_spent_budget_is_a_transport_failure_that_degrades_at_once(monkeypatch):
    """A 429 (or an exhausted quota) cannot be fixed by asking again, so it is
    classified as unreachable — the ladder drops a rung instead of burning the
    retry budget on the same wall."""
    monkeypatch.setenv(KEY, "sk-test")

    def rate_limited(request, timeout=None):
        raise verdicts.urllib.error.HTTPError(
            verdicts.OPENAI_URL, 429, "Too Many Requests", {},
            io.BytesIO(b'{"error":{"message":"Rate limit reached","code":"rate_limit_exceeded"}}'),
        )

    monkeypatch.setattr(verdicts.urllib.request, "urlopen", rate_limited)
    with pytest.raises(verdicts.LLMUnavailable) as exc:
        verdicts.call_llm("openai", "prompt")
    assert "429" in str(exc.value)
    assert verdicts.is_usage_limit(str(exc.value))


def test_a_missing_key_is_reported_not_guessed(monkeypatch):
    with pytest.raises(verdicts.LLMUnavailable, match="OpenAI key"):
        verdicts.call_llm("openai", "prompt")


def test_an_empty_reply_is_never_passed_off_as_an_answer(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test")
    stub_openai(monkeypatch, body={"choices": [{"message": {"content": "  "},
                                                "finish_reason": "length"}]})
    with pytest.raises(verdicts.LLMUnavailable, match="no text"):
        verdicts.call_llm("openai", "prompt")


# ---------------------------------------------------------------------------
# 3. Through the ask surface — same harness, honest label
# ---------------------------------------------------------------------------


def test_an_openai_answer_is_verified_and_credited_to_openai(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test")
    monkeypatch.setattr(
        ask, "call_llm",
        lambda mode, prompt, on_delta=None: (
            "ANSWER: 4 signals were held back this Monday.\n"
            "CITATIONS:\n- signals.json: suppressed list, 4 entries\n"
        ),
    )
    result = ask.answer("what was suppressed?", AS_OF, mode="openai", write_log=False)
    assert not result["refused"], result["refusal_reason"]
    assert result["numbers_verified"] >= 1        # the same pool check
    assert result["mode"] == result["decided_by"] == "openai"


def test_openai_answers_stream_like_claude_does(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test")

    def streaming(mode, prompt, on_delta=None):
        assert mode == "openai"
        if on_delta:
            on_delta("ANSWER: 4 signals were held back this Monday.\n")
        return (
            "ANSWER: 4 signals were held back this Monday.\n"
            "CITATIONS:\n- signals.json: suppressed list, 4 entries\n"
        )

    monkeypatch.setattr(ask, "call_llm", streaming)
    events = []
    ask.answer("what was suppressed?", AS_OF, mode="openai", write_log=False,
               on_event=events.append)
    assert [e["type"] for e in events][:2] == ["generating", "delta"]


def test_claude_running_out_falls_to_openai_and_says_so(monkeypatch):
    """The whole point of this tier: a spent Claude subscription costs nobody
    their answer, and the record shows which vendor stepped in."""
    monkeypatch.setenv(KEY, "sk-test")
    calls = []

    def ladder(mode, prompt, on_delta=None):
        calls.append(mode)
        if mode == "claude-cli":
            raise verdicts.LLMUnavailable(
                "claude CLI exited 1: Claude usage limit reached. Your limit will reset at 8pm."
            )
        return (
            "ANSWER: 4 signals were held back this Monday.\n"
            "CITATIONS:\n- signals.json: suppressed list, 4 entries\n"
        )

    monkeypatch.setattr(ask, "call_llm", ladder)
    result = ask.answer("what was suppressed?", AS_OF, mode="claude-cli", write_log=False)

    assert calls == ["claude-cli", "openai"]
    assert not result["refused"], result["refusal_reason"]
    assert result["decided_by"] == "openai"
    assert result["fallbacks"][0]["from"] == "claude-cli"
    assert result["fallbacks"][0]["to"] == "openai"
    assert "usage limit reached" in result["fallbacks"][0]["reason"]


def test_a_verdict_written_by_openai_carries_that_tier(monkeypatch):
    """Same for the pipeline's own judgments — verdicts.json records the tier
    that wrote each one, so the digest can never imply Claude did."""
    monkeypatch.setenv(KEY, "sk-test")
    monkeypatch.setattr(
        verdicts, "call_llm",
        lambda mode, prompt: (
            "BUCKET: NOT WORKING\n"
            "WHY: Record completion at Morrisville averaged 77.7% over the last 4 weeks, "
            "below the 90.5% baseline, so none of the gap to the 94.0% target is closed.\n"
            "ACTION: Rework the plan this week and set a fresh check-by date.\n"
        ),
    )
    doc = verdicts.run(AS_OF, mode="openai", plans_filter=["AP_001"], write=False)
    assert doc["llm_modes_used"] == {"openai": 1}
    assert doc["verdicts"][0]["ai_verdict"]["mode"] == "openai"
