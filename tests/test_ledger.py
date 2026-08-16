"""Ledger behavior tests — the close-the-loop mechanics.

The load-bearing property here is that OUTCOME (what the numbers did) and
EXECUTION (whether a human actually carried the work out) are independent.
Arithmetic may conclude the first and is never allowed to conclude the second.
These tests pin that: a metric moving never sets execution, a flat metric never
escalates a named person, and an interim reading never closes a row.

Synthetic weekly series drive every case; all storage goes through a tmp_path
Ledger so the real DATA/OUTPUTS/ ledger is never touched.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from pipeline import config
from pipeline import ledger as ledger_mod
from pipeline.ledger import Ledger, append_new_recommendations, recheck_open_rows

AS_OF_A = date(2026, 4, 27)
AS_OF_B = date(2026, 5, 4)
METRIC = "recheck_compliance_pct"  # percentage kind — noise floor 0.5


def make_cw(followup_value: float) -> pd.DataFrame:
    """16 weeks at 80.0 ending 2026-04-20, then one follow-up week."""
    weeks = pd.date_range("2026-01-05", "2026-04-20", freq="W-MON")
    rows = [
        {"location_id": "PCC_X", "week_start": w, METRIC: 80.0} for w in weeks
    ]
    rows.append(
        {
            "location_id": "PCC_X",
            "week_start": pd.Timestamp("2026-04-27"),
            METRIC: followup_value,
        }
    )
    return pd.DataFrame(rows)


def make_row(**overrides) -> dict:
    row = {
        "rec_id": "REC-2026-04-27-testville-" + METRIC,
        "created_week": str(AS_OF_A),
        "location_id": "PCC_X",
        "location_name": "Testville",
        "metric": METRIC,
        "recommendation": "test recommendation",
        "owner": "Test Owner",
        "expected_direction": "up",
        "check_by": str(AS_OF_B),  # due at the re-check, unless a test says otherwise
        "status": "open",
        "last_checked": "",
        "outcome_note": "",
        "escalation_level": 0,
        "execution": "unknown",
        "execution_by": "",
        "execution_at": "",
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


def recheck_one(ledger: Ledger, followup_value: float) -> dict:
    ledger.rows = [make_row()]
    results = recheck_open_rows(ledger, AS_OF_B, make_cw(followup_value))
    assert len(results) == 1
    return results[0]


def recheck_attested(ledger: Ledger, followup_value: float, execution: str) -> dict:
    ledger.rows = [make_row(execution=execution)]
    return recheck_open_rows(ledger, AS_OF_B, make_cw(followup_value))[0]


# --- outcome is measured; execution is never inferred from it ---------------


def test_a_metric_moving_the_right_way_does_not_prove_anyone_acted(ledger):
    """The v1 bug: +2.0 pts was called "acted_working" and the row closed with
    credit. Movement is evidence about the number, not about the team."""
    r = recheck_one(ledger, 82.0)  # +2.0 pts, well past the 0.5 floor
    assert r["outcome"] == "working"
    assert r["execution"] == "unknown"
    assert r["loop_state"] == "unattributed_gain"
    # credited to the center, but NOT closed and NOT claimed as our result
    assert ledger.rows[0]["status"] == "open"
    assert ledger.rows[0] in ledger.active_rows()
    assert "not confirmed" in r["loop_state_display"].lower()


def test_a_metric_moving_the_wrong_way_does_not_prove_anyone_ignored_it(ledger):
    r = recheck_one(ledger, 78.0)  # -2.0 pts against expected "up"
    assert r["outcome"] == "not_working"
    assert r["execution"] == "unknown"
    assert r["loop_state"] == "needs_attestation"
    assert ledger.rows[0] in ledger.active_rows()


def test_a_flat_metric_never_escalates_a_named_person(ledger):
    """v1 incremented escalation_level because a number sat still, which
    asserted the owner had ignored the ask. Nothing in the data says that."""
    r = recheck_one(ledger, 80.2)  # +0.2 — under the 0.5 noise floor
    assert r["outcome"] == "flat"
    assert r["loop_state"] == "needs_attestation"
    assert ledger.rows[0]["escalation_level"] == 0
    assert ledger.rows[0] in ledger.active_rows()  # never silently dropped


def test_no_usable_data_is_unverifiable_not_a_verdict(ledger):
    ledger.rows = [make_row()]
    empty = pd.DataFrame(columns=["location_id", "week_start", METRIC])
    r = recheck_open_rows(ledger, AS_OF_B, empty)[0]
    assert r["outcome"] == "unverifiable"
    assert r["loop_state"] == "unverifiable"


# --- attestation is the only thing that writes execution --------------------


def test_attested_done_and_working_closes_with_credit(ledger):
    r = recheck_attested(ledger, 82.0, "done")
    assert (r["outcome"], r["execution"]) == ("working", "done")
    assert r["loop_state"] == "confirmed_working"
    assert ledger.rows[0]["status"] == "closed"
    assert ledger.rows[0] not in ledger.active_rows()


def test_attested_done_and_wrong_way_falsifies_the_intervention(ledger):
    r = recheck_attested(ledger, 78.0, "done")
    assert r["loop_state"] == "intervention_failed"
    assert ledger.rows[0]["escalation_level"] == 0  # the person did the work
    assert ledger.rows[0]["status"] == "open"


def test_attested_not_done_is_the_only_thing_that_escalates(ledger):
    r = recheck_attested(ledger, 78.0, "not_done")
    assert r["loop_state"] == "not_executed"
    assert ledger.rows[0]["escalation_level"] == 1
    assert ledger.rows[0]["status"] == "escalated"


def test_attestation_is_recorded_with_an_actor_and_never_inferred(ledger):
    ledger.rows = [make_row()]
    row = ledger.record_attestation(
        make_row()["rec_id"], "done", "Talia Okonkwo", ""
    )
    assert row["execution"] == "done"
    assert row["execution_by"] == "Talia Okonkwo"
    assert row["execution_at"]
    # attesting is evidence, not a lifecycle event
    assert row["status"] == "open"
    events = [json.loads(l) for l in ledger.log_path.read_text().splitlines()]
    assert events[-1]["event"] == "execution_attested"
    assert events[-1]["execution_before"] == "unknown"


def test_execution_must_be_a_known_value(ledger):
    ledger.rows = [make_row()]
    with pytest.raises(ValueError):
        ledger.record_attestation(make_row()["rec_id"], "probably", "A", "")


# --- the system does not overrule its own deadline --------------------------


def test_an_interim_reading_cannot_close_or_escalate(ledger):
    """The row is not due until 2026-05-11, so a 2026-05-04 look is an early
    read. v1 closed rows and escalated people a week before their own
    check-by date."""
    ledger.rows = [make_row(check_by="2026-05-11", execution="done")]
    r = recheck_open_rows(ledger, AS_OF_B, make_cw(82.0))[0]
    assert r["reading"] == "interim"
    assert r["loop_state"] == "in_flight"
    assert ledger.rows[0]["status"] == "open"

    ledger.rows = [make_row(check_by="2026-05-11", execution="not_done")]
    r = recheck_open_rows(ledger, AS_OF_B, make_cw(78.0))[0]
    assert r["loop_state"] == "not_executed"  # accountability does not wait
    assert r["reading"] == "interim"


def test_same_monday_recheck_replays_without_double_escalation(ledger):
    cw = make_cw(78.0)
    ledger.rows = [make_row(execution="not_done")]
    first = recheck_open_rows(ledger, AS_OF_B, cw)
    second = recheck_open_rows(ledger, AS_OF_B, cw)  # re-run of the same run
    assert first[0]["outcome"] == second[0]["outcome"] == "not_working"
    assert ledger.rows[0]["escalation_level"] == 1  # not 2


def test_a_v1_ledger_migrates_without_gaining_an_execution_claim(ledger, tmp_path):
    """Old rows keep their history, but a v1 `acted_working` never becomes an
    execution claim — nobody attested those, so they read as unknown."""
    csv_path = tmp_path / "v1.csv"
    csv_path.write_text(
        "rec_id,created_week,location_id,location_name,metric,recommendation,owner,"
        "expected_direction,check_by,status,last_checked,outcome_note,escalation_level\n"
        f"R1,2026-04-27,PCC_X,Testville,{METRIC},r,O,up,2026-05-04,acted_working,"
        "2026-05-04,note,0\n"
    )
    migrated = Ledger(csv_path=csv_path, log_path=tmp_path / "l.jsonl").rows[0]
    assert migrated["status"] == "closed"
    assert migrated["outcome"] == "working"
    assert migrated["execution"] == "unknown"


def test_active_pair_is_not_duplicated_by_new_signal(ledger):
    ledger.rows = [make_row()]
    signals_doc = {
        "signals": [
            {
                "rank": 1,
                "center": "Testville",
                "location_id": "PCC_X",
                "metric": METRIC,
                "priority": 5.0,
                "scores": {},
            }
        ]
    }
    loc = pd.DataFrame(
        [
            {
                "location_id": "PCC_X",
                "location_name": "Testville",
                "rmp_name": "Dr. R",
                "rop_name": "O. P.",
            }
        ]
    )
    pw = pd.DataFrame(
        columns=["provider_id", "location_id", "week_start", "scheduled_hours",
                 "appts_completed", METRIC]
    )
    created = append_new_recommendations(
        ledger, signals_doc, loc, pw, make_cw(80.0), AS_OF_B
    )
    assert created == []  # active row keeps escalating instead
    assert len(ledger.rows) == 1


def test_rec_ids_never_contain_location_ids():
    # Hard display rule: location IDs are audit-trail only.
    assert ledger_mod.center_slug("Mount Pleasant") == "mount-pleasant"
    assert "PCC" not in f"REC-{AS_OF_A}-{ledger_mod.center_slug('Mount Pleasant')}-{METRIC}"


def test_all_four_doctors_affected_is_center_wide_not_concentrated():
    """Regression: 4/4 can never be described as a concentrated minority."""
    basis, assumption = ledger_mod._action_evidence(
        {"doctors_active_4wk": 4, "doctors_sliding": 4},
        "appointments per doctor-hour",
        "Testville",
        str(AS_OF_B),
    )

    assert basis == "measured"
    assert "all 4 of 4 doctors" in assumption
    assert "broad-based" in assumption
    assert "center-wide" in assumption
    assert "concentrated" not in assumption
