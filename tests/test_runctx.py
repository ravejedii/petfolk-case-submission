"""The run context — one ordered, accumulated history per run.

What these lock: the run is ONE agent walking a sequence of tool calls, not
four narrators rendered in order. Concretely — every phase's reasoning must be
able to see every tool result and every piece of reasoning before it, in order,
and week two must open with what week one left behind.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest

from pipeline import config, narrate, runctx


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    """Point the context at a scratch DATA/OUTPUTS/."""
    monkeypatch.setattr(config, "OUTPUTS_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Shape and ordering
# ---------------------------------------------------------------------------


def test_a_new_context_opens_with_its_carry_in(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    assert [e.kind for e in ctx.entries] == ["carry_in"]
    assert ctx.entries[0].seq == 0


def test_concurrent_open_seeds_step_zero_once(outputs, monkeypatch):
    """The pipeline and its one follower may open the new run together."""
    workers = 8
    barrier = Barrier(workers)
    original = runctx.build_carry_in

    def together(as_of):
        payload = original(as_of)
        barrier.wait()
        return payload

    monkeypatch.setattr(runctx, "build_carry_in", together)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        contexts = list(pool.map(lambda _: runctx.RunContext.open("2026-04-27"), range(workers)))

    assert all([entry.kind for entry in ctx.entries] == ["carry_in"] for ctx in contexts)
    lines = runctx.context_path("2026-04-27").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "carry_in"


def test_entries_keep_the_order_they_happened(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("validation", {"corrections": 4})
    ctx.append_reasoning("validation", "Data checked.")
    ctx.append_tool_result("signals", {"ranked": 3})
    ctx.append_reasoning("signals", "Three signals.")
    assert [(e.kind, e.phase) for e in ctx.entries] == [
        ("carry_in", None),
        ("tool_result", "validation"),
        ("reasoning", "validation"),
        ("tool_result", "signals"),
        ("reasoning", "signals"),
    ]
    assert [e.seq for e in ctx.entries] == [0, 1, 2, 3, 4]


def test_the_history_is_persisted_append_only(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("validation", {"corrections": 4})
    ctx.append_reasoning("validation", "Data checked.")

    lines = runctx.context_path("2026-04-27").read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(l)["kind"] for l in lines] == ["carry_in", "tool_result", "reasoning"]

    # and it reloads as the same history — a restarted process resumes the run
    reopened = runctx.RunContext.open("2026-04-27")
    assert [(e.kind, e.phase) for e in reopened.entries] == [
        (e.kind, e.phase) for e in ctx.entries
    ]


def test_a_rerun_starts_a_new_history(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("validation", {"corrections": 4})
    ctx.reset()
    assert [e.kind for e in ctx.entries] == ["carry_in"]
    assert len(runctx.context_path("2026-04-27").read_text().splitlines()) == 1


def test_a_torn_line_does_not_destroy_the_history(outputs):
    path = runctx.context_path("2026-04-27")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"seq": 0, "kind": "carry_in", "phase": None, "payload": {}})
        + "\n{not json\n"
        + json.dumps({"seq": 1, "kind": "reasoning", "phase": "signals", "payload": {"text": "x"}})
        + "\n"
    )
    ctx = runctx.RunContext.open("2026-04-27")
    assert [e.kind for e in ctx.entries] == ["carry_in", "reasoning"]


# ---------------------------------------------------------------------------
# What the agent actually sees
# ---------------------------------------------------------------------------


def test_reasoning_sees_every_earlier_tool_result_and_its_own_notes(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("validation", {"corrections_accepted": 4})
    ctx.append_reasoning("validation", "Data checked. 4 corrections.")
    ctx.append_tool_result("signals", {"ranked": 3})

    rendered = ctx.render(upto_phase="signals")
    assert "corrections_accepted" in rendered           # the earlier tool result
    assert "Data checked. 4 corrections." in rendered   # what it already wrote
    assert "ranked" not in rendered                     # not its own result, which it reads raw


def test_the_first_phase_sees_only_the_carry_in(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("validation", {"corrections_accepted": 4})
    rendered = ctx.render(upto_phase="validation")
    assert "Step 0" in rendered
    assert "corrections_accepted" not in rendered


def test_a_first_run_says_it_inherited_nothing(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    assert ctx.carry_in["is_first_run"] is True
    assert "first run" in ctx.render(upto_phase="validation")


def test_render_is_ordered_by_sequence(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("validation", {"a": 1})
    ctx.append_reasoning("validation", "FIRST-NOTE")
    ctx.append_tool_result("signals", {"b": 2})
    ctx.append_reasoning("signals", "SECOND-NOTE")
    ctx.append_tool_result("verdicts", {"c": 3})

    rendered = ctx.render(upto_phase="verdicts")
    assert rendered.index("FIRST-NOTE") < rendered.index("SECOND-NOTE")


def test_lookups_find_a_phase_and_report_what_is_missing(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_reasoning("signals", "the note")
    assert ctx.reasoning("signals") == "the note"
    assert ctx.reasoning("verdicts") is None
    assert ctx.has_reasoning("signals") is True
    assert ctx.has_tool_result("signals") is False


# ---------------------------------------------------------------------------
# One pass per phase — narration can be asked for again, a run cannot happen twice
# ---------------------------------------------------------------------------


def test_a_phase_is_never_recorded_twice(outputs):
    """Re-narrating a finished run must not write a second history into it."""
    ctx = runctx.RunContext.open("2026-04-27")
    for _ in range(2):  # e.g. `--follow` and then a `--phases signals` pass
        ctx.append_tool_result("signals", {"ranked": 3})
        ctx.append_reasoning("signals", "Three signals.")

    kinds = [(e.kind, e.phase) for e in ctx.entries]
    assert kinds == [("carry_in", None), ("tool_result", "signals"), ("reasoning", "signals")]
    assert len(runctx.context_path("2026-04-27").read_text().splitlines()) == 3


def test_the_first_pass_is_the_one_that_stands(outputs):
    """A later pass re-reads the same artifacts — it is not new information,
    so it never overwrites what the run actually recorded."""
    ctx = runctx.RunContext.open("2026-04-27")
    first = ctx.append_reasoning("signals", "what the run wrote", {"decided_by": "claude-cli"})
    again = ctx.append_reasoning("signals", "a later re-narration", {"decided_by": "template"})
    assert again is first
    assert ctx.reasoning("signals") == "what the run wrote"


def test_a_full_run_is_exactly_carry_in_plus_four_pairs(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    for phase in runctx.TOOLS:
        ctx.append_tool_result(phase, {"phase": phase})
        ctx.append_reasoning(phase, f"note on {phase}")
    # and a whole second narration pass over the finished run
    for phase in runctx.TOOLS:
        ctx.append_tool_result(phase, {"phase": phase})
        ctx.append_reasoning(phase, f"note on {phase}")

    assert len(ctx.entries) == 1 + 2 * len(runctx.TOOLS)
    assert ctx.completed_tools() == list(runctx.TOOLS)


def test_a_reload_does_not_let_a_finished_phase_back_in(outputs):
    """The guard has to survive a restarted process: the duplicate arrives from
    a SECOND narrator reading the same file, not from the same object."""
    first = runctx.RunContext.open("2026-04-27")
    first.append_tool_result("validation", {"corrections": 4})
    first.append_reasoning("validation", "Data checked.")

    second = runctx.RunContext.open("2026-04-27")
    second.append_tool_result("validation", {"corrections": 4})
    second.append_reasoning("validation", "Data checked.")

    assert len(runctx.context_path("2026-04-27").read_text().splitlines()) == 3


def test_a_rerun_of_the_monday_is_allowed_to_record_the_phase_again(outputs):
    """The guard bounds ONE run's history. Re-running the Monday is a new run,
    and reset() is the door that says so."""
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("signals", {"ranked": 3})
    ctx.reset()
    ctx.append_tool_result("signals", {"ranked": 5})
    assert ctx.tool_result("signals") == {"ranked": 5}


def test_an_unknown_entry_kind_is_a_programming_error(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    with pytest.raises(ValueError):
        ctx.append("speculation", "signals", {})


# ---------------------------------------------------------------------------
# Carry-in: what week two opens with
# ---------------------------------------------------------------------------


def test_previous_run_is_the_last_monday_that_produced_a_digest(outputs):
    for week in ("2026-04-20", "2026-04-27"):
        (outputs / week).mkdir(parents=True)
        (outputs / week / "digest.json").write_text("{}")
    # a Monday that ran but produced no digest is not a previous run
    (outputs / "2026-05-04").mkdir(parents=True)
    assert runctx.previous_run("2026-05-04") == "2026-04-27"
    assert runctx.previous_run("2026-04-20") is None


def test_carry_in_brings_forward_open_recommendations(outputs, monkeypatch):
    rows = [
        # inherited: created earlier, still open
        {"rec_id": "REC-A", "created_week": "2026-04-27", "status": "open",
         "location_name": "Morrisville", "metric": "records", "check_by": "2026-05-04",
         "execution": "unknown", "outcome": "working", "reading": "due",
         "owner": "Dr. X", "escalation_level": 0},
        # not inherited: already closed
        {"rec_id": "REC-B", "created_week": "2026-04-27", "status": "closed",
         "location_name": "Ballantyne", "metric": "CSAT", "check_by": "2026-05-04",
         "execution": "done", "outcome": "working", "reading": "due",
         "owner": "Dr. Y", "escalation_level": 0},
        # not inherited: this run created it
        {"rec_id": "REC-C", "created_week": "2026-05-04", "status": "open",
         "location_name": "Cary", "metric": "no-shows", "check_by": "2026-05-11",
         "execution": "unknown", "outcome": "pending", "reading": "pending",
         "owner": "Dr. Z", "escalation_level": 0},
    ]

    class FakeLedger:
        def __init__(self, *a, **k):
            self.rows = rows

    from pipeline import ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "Ledger", FakeLedger)

    carry = runctx.build_carry_in("2026-05-04")
    assert [r["rec_id"] for r in carry["open_recommendations"]] == ["REC-A"]
    assert carry["open_recommendation_count"] == 1
    assert carry["due_this_week"] == ["REC-A"]
    assert carry["is_first_run"] is False


def test_carry_in_reports_the_data_that_arrived(outputs, tmp_path, monkeypatch):
    data_dir = tmp_path / "TRANSLATION"
    data_dir.mkdir()
    (data_dir / "MANIFEST.json").write_text(
        json.dumps(
            {
                "as_of": "2026-05-04",
                "latest_complete_week": "2026-04-27",
                "tables": {"clinic_weekly": {"rows_before": 2211}},
                "corrections_carried": [{"id": "C3", "new_rows": 12}],
                "corrections_accepted": [
                    {
                        "id": "C3",
                        "table": "provider_weekly",
                        "decision_status": "carried",
                        "scope": "standing",
                        "new_rows": 12,
                    }
                ],
                "corrections_new": [],
            }
        )
    )
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    (outputs / "2026-04-27").mkdir(parents=True)
    (outputs / "2026-04-27" / "digest.json").write_text("{}")

    new_data = runctx.build_carry_in("2026-05-04")["new_data"]
    assert new_data["panel_read_through"] == "2026-04-27"
    assert new_data["center_weeks_in_scope"] == 2211
    assert new_data["rows_under_standing_corrections"] == 12
    assert new_data["provider_rows_under_standing_corrections"] == 12
    assert new_data["corrections_new"] == 0
    assert new_data["previous_run"] == "2026-04-27"


def test_a_manifest_from_another_monday_is_not_reported_as_this_week(outputs, tmp_path, monkeypatch):
    data_dir = tmp_path / "TRANSLATION"
    data_dir.mkdir()
    (data_dir / "MANIFEST.json").write_text(json.dumps({"as_of": "2026-04-27"}))
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    assert runctx.build_carry_in("2026-05-04")["new_data"] is None


# ---------------------------------------------------------------------------
# The prompt actually carries it
# ---------------------------------------------------------------------------


def test_the_prompt_renders_the_accumulated_history(outputs):
    ctx = runctx.RunContext.open("2026-04-27")
    ctx.append_tool_result("validation", {"corrections_accepted": 4})
    ctx.append_reasoning("validation", "Data checked. 4 corrections.")

    bundle = {
        "as_of": "2026-04-27",
        "leader": "Dr. Priya Raghunathan",
        "latest_complete_week": "2026-04-20",
        "run_context": ctx,
        "phases": {"signals": {"artifacts": {"signals.json": {"signals": []}}, "prior_context": {}}},
    }
    prompt = narrate.render_prompt(
        narrate.load_prompt_template(), "signals", bundle, retry_note=None
    )
    assert "Data checked. 4 corrections." in prompt
    assert "{{RUN_CONTEXT}}" not in prompt
    assert "{{PRIOR_CONTEXT}}" not in prompt
