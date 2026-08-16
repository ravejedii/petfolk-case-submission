"""Test-wide environment control.

The tier ladder (pipeline/verdicts.py) is built from what the MACHINE can
reach: the Claude CLI on PATH, an Anthropic key, an OpenAI key, then the
deterministic tier. That is right for a demo and wrong for a test suite — the
same assertions would pass or fail depending on which keys happen to be
exported (or sitting in .env.local, which pipeline.config loads at import).

So every test runs with the API keys removed: the ladder is exactly
claude-cli -> template, and "falls back to the tier below" means one thing on
every machine. Tests that exercise a key-bearing tier set the key themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def deterministic_tier_ladder(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PETFOLK_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
