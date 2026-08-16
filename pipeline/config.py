"""Shared configuration for the Petfolk Monday-digest pipeline.

Everything the pipeline needs to agree on lives here:
- where the raw and corrected data live (paths are relative to the repo root),
- the "as of" date (which Monday the digest is being written for),
- the metric registry: every metric we watch, what it means in plain English,
  which direction is bad, and how to format / tolerance-check it.

A reader should be able to skim this file and know exactly what the pipeline
looks at without opening any other module.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (repo-root relative; resolved from this file's location)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# Everything the pipeline reads or writes lives under one tree (2026-08-15):
#
#   DATA/INPUTS/       the raw extracts, READ-ONLY forever — the pipeline never
#                      writes here, exactly as if they came from a warehouse
#   DATA/TRANSLATION/  the corrected working copy validate.py builds from
#                      INPUTS once a human accepts each correction, plus the
#                      MANIFEST that records which corrections built it
#   DATA/OUTPUTS/      everything a run produces: signals, verdicts, digests,
#                      the ledger, run logs, threads
DATA_ROOT = REPO_ROOT / "DATA"


# --- local secrets ---------------------------------------------------------
#
# API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY) may live in `.env.local` at the
# repo root — gitignored, never committed, never printed. A real environment
# variable always wins, so nothing here can silently override a deployment.
# This exists so the fallback tiers survive a server restart without anyone
# re-exporting a key by hand; the repo itself stays clean of credentials.
def load_local_env(path: Path | None = None) -> list[str]:
    """Load KEY=VALUE lines from .env.local into the environment. Returns the
    names loaded (names only — a value is never logged)."""
    env_file = path or (REPO_ROOT / ".env.local")
    loaded: list[str] = []
    try:
        text = env_file.read_text()
    except OSError:
        return loaded
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and not os.environ.get(name):
            os.environ[name] = value
            loaded.append(name)
    return loaded


load_local_env()

# Raw inputs — READ-ONLY. Two filenames really do contain " (1)".
#
# PETFOLK_INPUTS_DIR (optional, set by the web app when files are uploaded):
# points the raw-input reads at a staging directory holding the four tables
# under canonical names (locations.csv, clinic_weekly.csv, provider_weekly.csv,
# action_plans.csv). Only where the raw inputs come FROM changes —
# DATA/TRANSLATION/, DATA/OUTPUTS/ and PROMPTS/ below stay where they are.
_INPUTS_OVERRIDE = os.environ.get("PETFOLK_INPUTS_DIR")
if _INPUTS_OVERRIDE:
    INPUTS_DIR = Path(_INPUTS_OVERRIDE).resolve()
    RAW_FILES = {
        table: INPUTS_DIR / f"{table}.csv"
        for table in ("locations", "clinic_weekly", "provider_weekly", "action_plans")
    }
else:
    INPUTS_DIR = DATA_ROOT / "INPUTS"
    RAW_FILES = {
        "locations": INPUTS_DIR / "locations (1).csv",
        "clinic_weekly": INPUTS_DIR / "clinic_weekly (1).csv",
        "provider_weekly": INPUTS_DIR / "provider_weekly.csv",
        "action_plans": INPUTS_DIR / "action_plans.csv",
    }

# Corrected copies (created by `validate.py --accept`); normalized filenames.
DATA_DIR = DATA_ROOT / "TRANSLATION"
DATA_FILES = {
    "locations": DATA_DIR / "locations.csv",
    "clinic_weekly": DATA_DIR / "clinic_weekly.csv",
    "provider_weekly": DATA_DIR / "provider_weekly.csv",
    "action_plans": DATA_DIR / "action_plans.csv",
}

OUTPUTS_DIR = DATA_ROOT / "OUTPUTS"
PROMPTS_DIR = REPO_ROOT / "PROMPTS"

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

# "Today" for the assignment: the Monday the digest lands in Priya's inbox.
AS_OF_DEFAULT = "2026-05-04"

# membership_conversion_pct changed definition on this date; weeks before it
# are excluded from membership trend windows (pre/post are not comparable).
MEMBERSHIP_DEFINITION_CHANGE = date(2026, 2, 9)


def latest_complete_week(as_of: date) -> date:
    """The Monday of the last complete week strictly before as-of.

    One definition, used by every step. The digest for a Monday may only see
    weeks that had finished by then: Data Validation & Check reads the panel up
    to this Monday, and the signal engine scores windows ending on it. Keeping
    both on the same boundary is what makes "what arrived since last week"
    a real question rather than a re-scan of the whole file.
    """
    return as_of - timedelta(days=7)


def parse_as_of(value: str | None = None) -> date:
    """Parse an --as-of value ("YYYY-MM-DD") into a date; must be a Monday.

    The pipeline uses only weeks strictly before this date.
    """
    raw = value or AS_OF_DEFAULT
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--as-of must look like YYYY-MM-DD, got {raw!r}"
        ) from exc
    if parsed.weekday() != 0:
        raise argparse.ArgumentTypeError(
            f"--as-of must be a Monday (the digest day); {raw} is not"
        )
    return parsed


def add_as_of_argument(parser: argparse.ArgumentParser) -> None:
    """Attach the standard --as-of option to a CLI parser."""
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        default=parse_as_of(AS_OF_DEFAULT),
        help=f"Digest Monday (YYYY-MM-DD). Default {AS_OF_DEFAULT}.",
    )


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------
# One entry per metric the signal engine watches. Formulas: docs/SCORING.md.
#
#   column        clinic_weekly column, or None when the metric is derived
#   derivation    plain-English formula for derived metrics
#   display_name  what Priya sees — plain English, no column names
#   bad_direction "down" = falling is bad; "up" = rising is bad
#   kind          drives formatting + comparison tolerances:
#                   percentage → "81.5%", tolerance 1.0 point
#                   csat       → "4.59 / 5", tolerance 0.05
#                   throughput → "2.54 appts/dr-hr", tolerance 0.05
#                   wait       → "11.5 min", tolerance 0.5
#                   count      → whole numbers (call-outs, open reqs)
#   post_definition_change_only  True → only use weeks on/after
#                   MEMBERSHIP_DEFINITION_CHANGE (2026-02-09) in trends.


@dataclass(frozen=True)
class Metric:
    key: str
    column: str | None
    display_name: str
    bad_direction: str  # "up" or "down"
    kind: str  # percentage | csat | throughput | wait | count
    derivation: str | None = None
    post_definition_change_only: bool = False


METRICS: dict[str, Metric] = {
    m.key: m
    for m in [
        Metric(
            key="appts_per_doctor_hour",
            column="appts_per_doctor_hour",
            display_name="Appointments per doctor-hour",
            bad_direction="down",
            kind="throughput",
        ),
        Metric(
            key="recheck_compliance_pct",
            column="recheck_compliance_pct",
            display_name="Recheck compliance",
            bad_direction="down",
            kind="percentage",
        ),
        Metric(
            key="record_completion_24h_pct",
            column="record_completion_24h_pct",
            display_name="Medical records completed within 24 hours",
            bad_direction="down",
            kind="percentage",
        ),
        Metric(
            key="callback_compliance_pct",
            column="callback_compliance_pct",
            display_name="Client callback compliance",
            bad_direction="down",
            kind="percentage",
        ),
        Metric(
            key="client_csat",
            column="client_csat",
            display_name="Client satisfaction (CSAT)",
            bad_direction="down",
            kind="csat",
        ),
        Metric(
            key="avg_wait_time_min",
            column="avg_wait_time_min",
            display_name="Average client wait time",
            bad_direction="up",
            kind="wait",
        ),
        Metric(
            key="revenue_per_appt",
            column="revenue_per_appt",
            display_name="Revenue per appointment",
            bad_direction="down",
            kind="throughput",
        ),
        Metric(
            key="membership_conversion_pct",
            column="membership_conversion_pct",
            display_name="Membership conversion",
            bad_direction="down",
            kind="percentage",
            post_definition_change_only=True,
        ),
        Metric(
            key="staff_call_outs",
            column="staff_call_outs",
            display_name="Staff call-outs",
            bad_direction="up",
            kind="count",
        ),
        Metric(
            key="no_show_rate",
            column=None,
            display_name="Client no-show rate",
            bad_direction="up",
            kind="percentage",
            derivation=(
                "appts_no_show / (appts_completed + appts_no_show)"
            ),
        ),
        Metric(
            key="open_dvm_requisitions",
            column="open_dvm_requisitions",
            display_name="Open doctor job requisitions",
            bad_direction="up",
            kind="count",
        ),
    ]
}

# Comparison tolerances by metric kind (used by validation + the harness).
KIND_TOLERANCES = {
    "percentage": 1.0,
    "csat": 0.05,
    "throughput": 0.05,
    "wait": 0.5,
    "count": 0.0,
}
