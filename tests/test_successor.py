"""Successor recommendations — a failed re-check must still end in a move.

The rule these tests exist to defend: a recommendation that
was acted on and did not work, or was ignored outright, can never leave the
leader with "rethink the fix". Every failed loop hands back a next move that
names who, what to try differently, what should move, and by when — through
the same harness as every other generated sentence, with a deterministic
successor as the floor when the model cannot produce a verifiable one.

All storage goes through a tmp_path Ledger, so the real DATA/OUTPUTS/ ledger is
never touched, and every LLM tier is faked — no test calls a model.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from pipeline import config, harness, successor
from pipeline import ledger as ledger_mod
from pipeline.ledger import (
    Ledger,
    attach_successors,
    build_successor_context,
    recheck_open_rows,
)

AS_OF_A = date(2026, 4, 27)
AS_OF_B = date(2026, 5, 4)
COUNT_METRIC = "staff_call_outs"  # count kind — noise floor 1.0
PCT_METRIC = "record_completion_24h_pct"  # percentage, provider-level detail

OWNER = "Talia Okonkwo"  # Regional Operating Partner (operational metrics)
PARTNER = "Dr. Ada Mensah"  # Regional Medical Partner


def make_cw(followup_value: float, base: float = 2.0, metric: str = COUNT_METRIC) -> pd.DataFrame:
    """16 quiet weeks ending 2026-04-20, then the week that followed."""
    weeks = pd.date_range("2026-01-05", "2026-04-20", freq="W-MON")
    rows = [{"location_id": "PCC_X", "week_start": w, metric: base} for w in weeks]
    rows.append(
        {"location_id": "PCC_X", "week_start": pd.Timestamp("2026-04-27"), metric: followup_value}
    )
    return pd.DataFrame(rows)


def make_loc() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "location_id": "PCC_X",
                "location_name": "Testville",
                "rmp_name": PARTNER,
                "rop_name": OWNER,
            }
        ]
    )


def empty_pw(metric: str = COUNT_METRIC) -> pd.DataFrame:
    return pd.DataFrame(
        columns=["provider_id", "location_id", "week_start", "scheduled_hours",
                 "appts_completed", metric]
    )


def make_row(metric: str = COUNT_METRIC, **overrides) -> dict:
    row = {
        "rec_id": f"REC-2026-04-27-testville-{metric}",
        "created_week": str(AS_OF_A),
        "location_id": "PCC_X",
        "location_name": "Testville",
        "metric": metric,
        "recommendation": (
            f"{OWNER} (Regional Operating Partner) to "
            f"{ledger_mod.ACTION_HINTS[metric]} at Testville — the last 4 weeks averaged 2. "
            f"Expected: it improves by 2026-05-11."
        ),
        "owner": OWNER,
        "expected_direction": "down" if metric == COUNT_METRIC else "up",
        "check_by": str(AS_OF_B),
        "status": "open",
        "last_checked": "",
        "outcome_note": "",
        "escalation_level": 0,
        "supersedes": "",
        "superseded_by": "",
        "decision": "",
        # A successor is only ever generated for work a human confirmed was
        # carried out — that is the whole point of the execution dimension.
        "execution": "done",
        "execution_by": "Talia Okonkwo",
        "execution_at": "2026-05-01T09:00:00+00:00",
        "outcome": "pending",
        "reading": "pending",
        "action_basis": "",
        "action_assumption": "",
    }
    row.update(overrides)
    return row


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(csv_path=tmp_path / "ledger.csv", log_path=tmp_path / "log.jsonl")


def run_recheck(ledger: Ledger, cw: pd.DataFrame, rows: list[dict]) -> list[dict]:
    ledger.rows = rows
    return recheck_open_rows(ledger, AS_OF_B, cw)


def attach(ledger: Ledger, rechecks, cw, pw=None, log=None, mode="template", **kw):
    return attach_successors(
        ledger, rechecks, AS_OF_B, make_loc(), pw if pw is not None else empty_pw(),
        cw, mode=mode, log=log, **kw
    )


def whole_text(entry: dict) -> str:
    return f"{entry['next_move']}\n{entry['expected']}\n{entry['why']}"


# ---------------------------------------------------------------------------
# The escalation ladder
# ---------------------------------------------------------------------------


def test_failed_once_keeps_the_owner_and_names_a_different_intervention(ledger):
    cw = make_cw(9.0)
    rechecks = run_recheck(ledger, cw, [make_row()])
    assert rechecks[0]["outcome"] == "not_working"
    assert rechecks[0]["loop_state"] == "intervention_failed"

    entries = attach(ledger, rechecks, cw)
    assert len(entries) == 1
    entry = entries[0]

    assert entry["escalated"] is False
    assert entry["attempt"] == 2
    assert entry["owner"] == OWNER
    text = whole_text(entry)
    assert OWNER in text  # who
    assert "2026-05-11" in text  # a new check-by date (as-of + 7 days)
    # ...and something genuinely different from what was already tried.
    assert ledger_mod.ACTION_HINTS[COUNT_METRIC] not in entry["next_move"]
    assert successor.ALTERNATE_ACTIONS[COUNT_METRIC] in entry["next_move"]


def test_attested_not_done_escalates_to_both_regional_partners(ledger):
    """v1 escalated on a flat number, which asserted the owner had ignored the
    ask. Now escalation follows a human saying the work did not happen."""
    cw = make_cw(2.2)  # +0.2 against a 1.0 noise floor
    rechecks = run_recheck(ledger, cw, [make_row(execution="not_done")])
    assert rechecks[0]["outcome"] == "flat"
    assert rechecks[0]["loop_state"] == "not_executed"

    entry = attach(ledger, rechecks, cw)[0]
    assert entry["escalated"] is True
    text = whole_text(entry)
    assert OWNER in text and PARTNER in text  # jointly owned now
    assert "together" in text.lower() or "review" in text.lower()
    assert entry["escalation_partner"] == PARTNER


def test_a_second_failure_escalates_even_though_it_was_acted_on(ledger):
    """Attempt 1 failed, attempt 2 failed: one team has now tried twice."""
    first = make_row(rec_id="REC-2026-04-13-testville-staff_call_outs",
                     created_week="2026-04-13", status="open", outcome="not_working",
                     superseded_by="REC-2026-04-27-testville-staff_call_outs")
    second = make_row(supersedes=first["rec_id"])
    cw = make_cw(9.0)
    rechecks = run_recheck(ledger, cw, [first, second])
    # only the row that has not been superseded is re-checked
    assert [r["rec_id"] for r in rechecks] == [second["rec_id"]]

    entry = attach(ledger, rechecks, cw)[0]
    assert entry["attempt"] == 3
    assert entry["escalated"] is True
    assert PARTNER in whole_text(entry)


def test_every_metric_has_a_concrete_alternative(ledger):
    """The deterministic floor must name a real next action for any metric the
    engine can raise — never a placeholder, never a dead end."""
    for key in config.METRICS:
        assert key in successor.ALTERNATE_ACTIONS, key
        alternate = successor.ALTERNATE_ACTIONS[key]
        assert not successor.DEAD_END_RE.search(alternate), key
        assert ledger_mod.ACTION_HINTS[key] not in alternate, key


# ---------------------------------------------------------------------------
# Lineage — the chain reads both ways, and nothing is escalated twice
# ---------------------------------------------------------------------------


def test_lineage_is_recorded_and_the_failed_row_stops_being_rechecked(ledger):
    cw = make_cw(9.0)
    failed = make_row()
    rechecks = run_recheck(ledger, cw, [failed])
    entry = attach(ledger, rechecks, cw)[0]

    successor_row = ledger.get(entry["rec_id"])
    assert successor_row is not None
    assert successor_row["supersedes"] == failed["rec_id"]
    assert successor_row["status"] == "open"
    assert successor_row["check_by"] == "2026-05-11"
    assert successor_row["recommendation"] == entry["recommendation"]

    assert failed["superseded_by"] == successor_row["rec_id"]
    # The failed row keeps its measured outcome AND the attestation behind it
    # (history worth reading) while the lifecycle moves on — the successor owns
    # the metric now, and re-checking both would double-escalate.
    assert failed["outcome"] == "not_working"
    assert failed["execution"] == "done"
    assert failed["status"] == "superseded"
    assert failed not in ledger.active_rows()
    assert successor_row in ledger.active_rows()

    events = [e["event"] for e in ledger.events()]
    assert "rec_superseded" in events and "rec_successor_created" in events


def test_rerunning_the_same_monday_replays_instead_of_regenerating(ledger, monkeypatch):
    cw = make_cw(9.0)
    rechecks = run_recheck(ledger, cw, [make_row()])
    first = attach(ledger, rechecks, cw)[0]

    calls = []
    real_generate = successor.generate
    monkeypatch.setattr(
        successor, "generate", lambda *a, **kw: calls.append(1) or real_generate(*a, **kw)
    )
    again = recheck_open_rows(ledger, AS_OF_B, cw)  # a re-run of the same Monday
    replayed = attach(ledger, again, cw)

    assert calls == [], "a replayed Monday must never generate a second successor"
    assert len(replayed) == 1
    assert replayed[0]["rec_id"] == first["rec_id"]
    assert replayed[0]["recommendation"] == first["recommendation"]
    assert replayed[0]["replayed"] is True
    assert len([r for r in ledger.rows if r.get("supersedes")]) == 1


def test_a_recheck_that_worked_gets_no_successor(ledger):
    cw = make_cw(0.0)  # call-outs fell: the fix worked
    rechecks = run_recheck(ledger, cw, [make_row()])
    assert rechecks[0]["outcome"] == "working"
    assert attach(ledger, rechecks, cw) == []


def test_the_recheck_note_no_longer_dead_ends(ledger):
    cw = make_cw(9.0)
    note = run_recheck(ledger, cw, [make_row()])[0]["note"]
    assert "rethink" not in note.lower()
    assert "different mechanism" in note.lower()


# ---------------------------------------------------------------------------
# The harness — nothing generated reaches a leader unchecked
# ---------------------------------------------------------------------------


def context_for(ledger: Ledger, cw: pd.DataFrame, pw: pd.DataFrame | None = None,
                rows: list[dict] | None = None, **kw) -> dict:
    rechecks = run_recheck(ledger, cw, rows or [make_row()])
    row = ledger.get(rechecks[0]["rec_id"])
    return build_successor_context(
        ledger, rechecks[0], row, AS_OF_B, make_loc().set_index("location_id").loc["PCC_X"],
        pw if pw is not None else empty_pw(), cw,
        pd.Timestamp("2026-04-27"), **kw
    )


def fake_llm(reply: str):
    return lambda mode, prompt: reply


def test_a_verifiable_llm_answer_is_used(ledger, monkeypatch, tmp_path):
    context = context_for(ledger, make_cw(9.0))
    reply = (
        f"NEXT MOVE: {OWNER} to put a named backup on every shift at Testville and hold a "
        "return-to-work conversation after each call-out.\n"
        "EXPECTED: Staff call-outs fall from 9 back toward 2 by 2026-05-11.\n"
        "WHY: The call-out log review has been in place since 2026-04-27 and call-outs went "
        "from 2 to 9 over the 1 week since."
    )
    monkeypatch.setattr(successor, "call_llm", fake_llm(reply))
    log = harness.RunLog(tmp_path / "runlog.jsonl", step="successor")
    result = successor.generate(context, mode="claude-cli", prompt_template="{{CONTEXT_JSON}}", log=log)
    log.close()

    assert result["decided_by"] == "claude-cli"
    assert result["fallbacks"] == []
    assert OWNER in result["next_move"]
    assert result["checks"]["number_check"] == "pass"
    assert result["checks"]["numbers_verified"] > 0

    events = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    checks = [e for e in events if e["event"] == "successor_number_check"]
    assert checks and all(e["result"] == "pass" for e in checks)


@pytest.mark.parametrize(
    "bad_reply, expected_problem",
    [
        (
            # a number that exists nowhere in the context: a hallucination
            f"NEXT MOVE: {OWNER} to add 3 float staff to every shift at Testville.\n"
            "EXPECTED: Staff call-outs fall from 9 to 4.4 by 2026-05-11.\n"
            "WHY: The 47 call-outs since 2026-04-27 show the log review failed.",
            "successor_number_check",
        ),
        (
            # the exact dead end this whole feature exists to delete
            f"NEXT MOVE: {OWNER} should rethink the fix at Testville.\n"
            "EXPECTED: Staff call-outs fall from 9 toward 2 by 2026-05-11.\n"
            "WHY: What was tried has not worked.",
            "successor_rule_check",
        ),
        (
            # an internal ID a leader must never see
            f"NEXT MOVE: {OWNER} to put a named backup on every shift at PCC_X.\n"
            "EXPECTED: Staff call-outs fall from 9 toward 2 by 2026-05-11.\n"
            "WHY: Call-outs went from 2 to 9 in the 1 week since.",
            "successor_language_check",
        ),
        (
            # no owner, no date: not falsifiable
            "NEXT MOVE: Someone should look at the schedule at Testville.\n"
            "EXPECTED: Staff call-outs improve.\n"
            "WHY: Call-outs went from 2 to 9.",
            "successor_rule_check",
        ),
    ],
)
def test_an_unverifiable_answer_falls_back_to_a_concrete_next_action(
    ledger, monkeypatch, tmp_path, bad_reply, expected_problem
):
    context = context_for(ledger, make_cw(9.0))
    monkeypatch.setattr(successor, "call_llm", fake_llm(bad_reply))
    log = harness.RunLog(tmp_path / "runlog.jsonl", step="successor")
    result = successor.generate(context, mode="claude-cli", prompt_template="x", log=log)
    log.close()

    # The model answered, but never verifiably: the deterministic successor
    # takes over — and it still names a concrete action.
    assert result["decided_by"] == "template"
    assert result["mode"] == "claude-cli"  # the tier asked for is not hidden
    assert [f["to"] for f in result["fallbacks"]] == ["template"]
    assert OWNER in result["next_move"]
    assert "2026-05-11" in result["expected"]
    assert not successor.DEAD_END_RE.search(result["next_move"])

    events = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    assert any(
        e["event"] == expected_problem and e["result"] == "fail" for e in events
    ), f"{expected_problem} should have caught this answer"
    # It was given its retries before the fallback fired.
    assert sum(1 for e in events if e["event"] == "llm_call") == harness.MAX_REGENERATIONS + 1
    assert any(e["event"] == "template_fallback" for e in events)


def test_a_malformed_answer_is_retried_then_falls_back(ledger, monkeypatch, tmp_path):
    context = context_for(ledger, make_cw(9.0))
    monkeypatch.setattr(successor, "call_llm", fake_llm("I would suggest looking into it."))
    log = harness.RunLog(tmp_path / "runlog.jsonl", step="successor")
    result = successor.generate(context, mode="claude-cli", prompt_template="x", log=log)
    log.close()

    assert result["decided_by"] == "template"
    events = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    assert any(e["event"] == "generation_error" and e["kind"] == "malformed" for e in events)


def test_an_unreachable_model_degrades_a_tier_instead_of_dying(ledger, monkeypatch, tmp_path):
    from pipeline import verdicts

    context = context_for(ledger, make_cw(9.0))

    def unavailable(mode, prompt):
        raise verdicts.LLMUnavailable("You've hit your session limit · resets 7:40pm")

    monkeypatch.setattr(successor, "call_llm", unavailable)
    monkeypatch.setattr(verdicts, "available_modes", lambda: ("claude-cli", "template"))
    log = harness.RunLog(tmp_path / "runlog.jsonl", step="successor")
    result = successor.generate(context, mode="claude-cli", prompt_template="x", log=log)
    log.close()

    assert result["decided_by"] == "template"
    assert result["fallbacks"][0]["from"] == "claude-cli"
    events = [json.loads(l) for l in (tmp_path / "runlog.jsonl").read_text().splitlines()]
    assert any(e["event"] == "mode_fallback" and e["kind"] == "usage_limit" for e in events)


def test_the_context_carries_the_week_by_week_story_and_provider_detail(ledger):
    """A successor is only as grounded as what it is handed."""
    weeks = pd.date_range("2026-01-05", "2026-04-20", freq="W-MON")
    cw = pd.DataFrame(
        [{"location_id": "PCC_X", "week_start": w, PCT_METRIC: 90.0} for w in weeks]
        + [{"location_id": "PCC_X", "week_start": pd.Timestamp("2026-04-27"), PCT_METRIC: 70.0}]
    )
    pw = pd.DataFrame(
        [
            {
                "provider_id": f"DVM_{i}",
                "location_id": "PCC_X",
                "week_start": w,
                "scheduled_hours": 30.0,
                "appts_completed": 60,
                PCT_METRIC: 90.0 if w < pd.Timestamp("2026-04-06") else 70.0,
            }
            for i in range(2)
            for w in weeks
        ]
    )
    context = context_for(
        ledger, cw, pw=pw, rows=[make_row(metric=PCT_METRIC, expected_direction="up")]
    )
    assert context["what_happened"]["weekly_values_since"][0]["week_start"] == "2026-04-27"
    assert context["provider_concentration"]["sentence"]
    # Provider IDs are audit-trail only: they never reach a prompt.
    blob = json.dumps(context)
    assert "DVM_" not in blob
    assert "PCC_" not in blob

    entry = attach(
        ledger,
        run_recheck(ledger, cw, [make_row(metric=PCT_METRIC, expected_direction="up")]),
        cw,
        pw=pw,
    )[0]
    assert "doctors" in entry["why"]


# ---------------------------------------------------------------------------
# Human decisions — the four actions the UI offers
# ---------------------------------------------------------------------------


def test_closing_a_row_records_the_action_and_the_state(ledger):
    ledger.rows = [make_row()]
    row = ledger.record_human_decision(
        make_row()["rec_id"], "closed", "", "Lucas", action="close"
    )
    assert row["status"] == "closed"
    assert row["decision"] == "close"
    assert "closed it out" in row["outcome_note"]
    assert row not in ledger.active_rows()
    event = ledger.last_event("human_decision", rec_id=row["rec_id"])
    assert event["action"] == "close" and event["actor"] == "Lucas"


def test_dismiss_requires_a_reason(ledger):
    ledger.rows = [make_row()]
    with pytest.raises(ValueError, match="reason"):
        ledger.record_human_decision(
            make_row()["rec_id"], "dismissed", "  ", "Lucas", action="dismiss"
        )
    row = ledger.record_human_decision(
        make_row()["rec_id"], "dismissed", "Two medical leaves — known cause", "Lucas",
        action="dismiss",
    )
    assert row["status"] == "dismissed"
    assert "medical leaves" in row["outcome_note"]


def test_relaunch_needs_a_new_date_and_restarts_the_clock(ledger):
    ledger.rows = [make_row(status="acted_not_working", last_checked=str(AS_OF_B))]
    rec_id = make_row()["rec_id"]
    with pytest.raises(ValueError, match="check-by"):
        ledger.record_human_decision(rec_id, "open", "", "Lucas", action="relaunch")
    row = ledger.record_human_decision(
        rec_id, "open", "", "Lucas", action="relaunch", check_by="2026-05-25"
    )
    assert row["status"] == "open"
    assert row["check_by"] == "2026-05-25"
    assert row["last_checked"] == ""  # re-checked against the weeks that follow now
    assert row in ledger.active_rows()


def test_escalate_keeps_the_row_active_and_says_so(ledger):
    ledger.rows = [make_row()]
    row = ledger.record_human_decision(
        make_row()["rec_id"], "escalated", "", "Lucas", action="escalate"
    )
    assert row["status"] == "escalated"
    assert row in ledger.active_rows()
    assert ledger_mod.STATUS_DISPLAY["escalated"].startswith("Escalated")


def test_a_human_decision_survives_a_reload(ledger, tmp_path):
    ledger.rows = [make_row()]
    ledger.record_human_decision(
        make_row()["rec_id"], "dismissed", "Known cause", "Lucas", action="dismiss"
    )
    reloaded = Ledger(csv_path=ledger.csv_path, log_path=ledger.log_path)
    row = reloaded.get(make_row()["rec_id"])
    assert row["status"] == "dismissed"
    assert row["decision"] == "dismiss"
