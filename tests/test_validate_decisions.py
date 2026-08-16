"""Data Validation & Check — as-of scoping and correction decision memory.

Two properties this step must have, both of which it lacked until they were
tested here:

  1. A Monday's run reads the panel only through that Monday's last complete
     week. Without it, every run scans the whole file and "what arrived since
     last week" is not a question the system can even ask.
  2. A correction the human already ruled on never comes back as a question.
     Standing rules (drop duplicates, normalize a column) carry to rows that
     arrive later; instance decisions (relabel these named centers) carry only
     while the named set is unchanged.

Together they are what makes the second Monday's run different from the first
one, which is the whole close-the-loop claim.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from pipeline import config, validate


# ---------------------------------------------------------------------------
# As-of scoping
# ---------------------------------------------------------------------------


def test_panel_cutoff_is_the_signal_engines_boundary():
    """One definition of "last complete week" — validation and scoring agree."""
    from pipeline import signals

    for as_of in (date(2026, 4, 27), date(2026, 5, 4)):
        assert validate.panel_cutoff(as_of) == signals.latest_complete_week(as_of)


@pytest.mark.parametrize("as_of", [date(2026, 4, 27), date(2026, 5, 4)])
def test_weekly_panels_stop_at_the_cutoff(as_of):
    parsed = validate.load_parsed(as_of)
    cutoff = validate.panel_cutoff(as_of)
    for table in ("clinic_weekly", "provider_weekly"):
        assert parsed[table]["week_start"].max() <= cutoff


def test_a_later_monday_sees_strictly_more_rows():
    """The week of 2026-04-27 is invisible to the 04-27 run and visible to 05-04."""
    first = validate.load_parsed(date(2026, 4, 27))["clinic_weekly"]
    second = validate.load_parsed(date(2026, 5, 4))["clinic_weekly"]
    assert len(second) > len(first)
    arrived = second[second["week_start"] > validate.panel_cutoff(date(2026, 4, 27))]
    assert len(arrived) == len(second) - len(first)
    assert set(arrived["week_start"].unique()) == {pd.Timestamp("2026-04-27")}


def test_text_and_parsed_loaders_scope_identically():
    """check_clinic_weekly zips the two frames — they must cover the same rows."""
    as_of = date(2026, 4, 27)
    assert len(validate.load_text("clinic_weekly", as_of)) == len(
        validate.load_parsed(as_of)["clinic_weekly"]
    )


def test_centers_and_plans_are_cut_at_opened_date():
    as_of = date(2026, 4, 27)
    parsed = validate.load_parsed(as_of)
    assert (parsed["locations"]["opened_date"] <= pd.Timestamp(as_of)).all()
    assert (parsed["action_plans"]["opened_date"] <= pd.Timestamp(as_of)).all()


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_standing_fingerprint_ignores_the_rows_it_covers():
    """A standing rule keeps its identity as new rows fall under it."""
    a = validate.correction_fingerprint("normalize_employment_type", [])
    b = validate.correction_fingerprint(
        "normalize_employment_type", [{"location_id": "PCC_001"}]
    )
    assert a == b


def test_instance_fingerprint_tracks_the_named_rows():
    seven = [{"location_id": f"PCC_{i:03d}", "computed_tier": "new"} for i in range(7)]
    same_order_reversed = list(reversed(seven))
    plus_one = seven + [{"location_id": "PCC_099", "computed_tier": "mature"}]
    retiered = [dict(d, computed_tier="ramping") for d in seven]

    fp = validate.correction_fingerprint("relabel_maturity_tier", seven)
    assert validate.correction_fingerprint("relabel_maturity_tier", same_order_reversed) == fp
    assert validate.correction_fingerprint("relabel_maturity_tier", plus_one) != fp
    assert validate.correction_fingerprint("relabel_maturity_tier", retiered) != fp


# ---------------------------------------------------------------------------
# Decision memory
# ---------------------------------------------------------------------------


def _correction(cid="C3", kind="normalize_employment_type", rows=213, details=None):
    c = validate.Correction(
        id=cid,
        table="provider_weekly",
        kind=kind,
        description="test",
        affected_rows=rows,
        details=details or [],
    )
    c.scope = validate.CORRECTION_SCOPE[kind]
    c.fingerprint = validate.correction_fingerprint(kind, c.details)
    return c


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    p = tmp_path / "correction_decisions.jsonl"
    monkeypatch.setattr(validate, "DECISIONS_PATH", p)
    return p


def test_first_monday_everything_is_a_question(log_path):
    corrections = [_correction()]
    validate.classify_corrections(corrections, validate.load_decisions())
    assert corrections[0].status == "new"
    assert corrections[0].new_rows == 213


def test_second_monday_does_not_re_ask(log_path):
    first = [_correction()]
    validate.classify_corrections(first, validate.load_decisions())
    validate.record_decisions(first, ["C3"], date(2026, 4, 27), actor="Lucas")

    second = [_correction(rows=225)]
    validate.classify_corrections(second, validate.load_decisions())
    assert second[0].status == "carried"
    assert second[0].prior_decision == "accepted"
    assert second[0].decided_as_of == "2026-04-27"
    assert second[0].new_rows == 12  # only the rows that arrived this week


def test_a_carried_acceptance_still_applies(log_path):
    first = [_correction()]
    validate.classify_corrections(first, validate.load_decisions())
    validate.record_decisions(first, ["C3"], date(2026, 4, 27))

    second = [_correction(rows=225)]
    validate.classify_corrections(second, validate.load_decisions())
    # nobody accepted anything this run, yet the rule still governs the data
    assert validate.effective_accepted(second, []) == ["C3"]


def test_a_decline_carries_too(log_path):
    first = [_correction()]
    validate.classify_corrections(first, validate.load_decisions())
    validate.record_decisions(first, [], date(2026, 4, 27))  # declined

    second = [_correction(rows=225)]
    validate.classify_corrections(second, validate.load_decisions())
    assert second[0].prior_decision == "declined"
    assert validate.effective_accepted(second, []) == []
    # and it is not silently re-applied by an "accept all" on a later Monday
    assert validate.effective_accepted(second, ["C3"]) == []


def test_coverage_grows_so_new_rows_are_counted_once(log_path):
    run1 = [_correction()]
    validate.classify_corrections(run1, validate.load_decisions())
    validate.record_decisions(run1, ["C3"], date(2026, 4, 27))

    run2 = [_correction(rows=225)]
    validate.classify_corrections(run2, validate.load_decisions())
    assert run2[0].new_rows == 12
    validate.record_decisions(run2, [], date(2026, 5, 4))

    run3 = [_correction(rows=225)]
    validate.classify_corrections(run3, validate.load_decisions())
    assert run3[0].new_rows == 0  # nothing arrived; the log does not repeat itself


def test_a_changed_center_set_is_a_new_question(log_path):
    seven = [{"location_id": f"PCC_{i:03d}", "computed_tier": "new"} for i in range(7)]
    first = [_correction(cid="C4", kind="relabel_maturity_tier", rows=7, details=seven)]
    validate.classify_corrections(first, validate.load_decisions())
    validate.record_decisions(first, ["C4"], date(2026, 4, 27))

    unchanged = [_correction(cid="C4", kind="relabel_maturity_tier", rows=7, details=seven)]
    validate.classify_corrections(unchanged, validate.load_decisions())
    assert unchanged[0].status == "carried"

    eighth = seven + [{"location_id": "PCC_042", "computed_tier": "ramping"}]
    changed = [_correction(cid="C4", kind="relabel_maturity_tier", rows=8, details=eighth)]
    validate.classify_corrections(changed, validate.load_decisions())
    assert changed[0].status == "new"  # a different set of centers is a fresh call


def test_carried_decisions_are_not_re_recorded_as_decisions(log_path):
    first = [_correction()]
    validate.classify_corrections(first, validate.load_decisions())
    validate.record_decisions(first, ["C3"], date(2026, 4, 27), actor="Lucas")

    second = [_correction(rows=225)]
    validate.classify_corrections(second, validate.load_decisions())
    validate.record_decisions(second, [], date(2026, 5, 4))

    events = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    assert [e["event"] for e in events] == ["decided", "carried"]
    assert events[0]["actor"] == "Lucas"
    # the human's Monday is preserved on the carried record
    assert events[1]["as_of"] == "2026-04-27"
    assert events[1]["recorded_on"] == "2026-05-04"


def test_accept_all_only_accepts_pending(log_path):
    carried = _correction(cid="C1", kind="drop_duplicates", rows=9)
    carried.status = "carried"
    carried.prior_decision = "accepted"
    pending = _correction(cid="C3", rows=12)
    assert validate.parse_accept("all", [carried, pending]) == ["C3"]


def test_load_decisions_survives_a_torn_line(log_path, tmp_path):
    log_path.write_text(
        json.dumps({"fingerprint": "abc", "decision": "accepted"})
        + "\n{not json\n"
        + json.dumps({"fingerprint": "def", "decision": "declined"})
        + "\n"
    )
    decisions = validate.load_decisions()
    assert set(decisions) == {"abc", "def"}
