"""The run context — one ordered, accumulated history per Monday run.

The pipeline is a sequence of deterministic tools. This module is the state
they run against: every tool call appends its structured output here, the agent
reasons over everything accumulated so far (including its own earlier
reasoning), and that reasoning is appended too. One ordered history per run,
not four independent narrators that happen to render in order.

    carry_in      what this run inherited — the open ledger rows from the
                  previous Monday and the data that arrived since
    tool_result   a deterministic phase executed and returned structured output
    reasoning     the agent's note on that result, written with every prior
                  entry in view

Everything is persisted append-only to DATA/OUTPUTS/<as_of>/run_context.jsonl,
so the context is a real artifact: reloadable after a crash, replayable into
the panel, and auditable — the reasoning at step N provably saw exactly the
entries before it and nothing else. The agent never carries model memory
between steps; continuity comes from this file being fed back into the prompt.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import config

CONTEXT_FILENAME = "run_context.jsonl"

# The tools, in the order the pipeline calls them.
TOOLS = ("validation", "signals", "verdicts", "digest")

ENTRY_KINDS = ("carry_in", "tool_result", "reasoning")


def context_path(as_of: date | str) -> Path:
    return config.OUTPUTS_DIR / str(as_of) / CONTEXT_FILENAME


# ---------------------------------------------------------------------------
# Carry-in — what the previous Monday handed this one
# ---------------------------------------------------------------------------


def previous_run(as_of: date | str) -> str | None:
    """The most recent Monday before this one that actually produced a digest."""
    target = str(as_of)
    if not config.OUTPUTS_DIR.exists():
        return None
    prior = [
        d.name
        for d in config.OUTPUTS_DIR.iterdir()
        if d.is_dir() and len(d.name) == 10 and d.name < target and (d / "digest.json").exists()
    ]
    return max(prior) if prior else None


def _open_ledger_rows(as_of: str) -> list[dict]:
    """Recommendations still live when this Monday opens.

    Read through the ledger's own door so the split status/execution/outcome
    contract (SCHEMA/ledger.md) is applied exactly once, here as everywhere.
    """
    from . import ledger as ledger_mod

    rows = []
    for r in ledger_mod.Ledger().rows:
        if r.get("created_week", "") >= as_of:
            continue  # created by this run or later — not inherited
        if r.get("status") in ("closed", "dismissed", "superseded"):
            continue
        rows.append(
            {
                "rec_id": r.get("rec_id"),
                "center": r.get("location_name"),
                "metric": r.get("metric"),
                "owner": r.get("owner"),
                "check_by": r.get("check_by"),
                "created_week": r.get("created_week"),
                "status": r.get("status"),
                "execution": r.get("execution"),
                "outcome": r.get("outcome"),
                "reading": r.get("reading"),
                "escalation_level": r.get("escalation_level"),
            }
        )
    return rows


def _new_data(as_of: str, prior: str | None) -> dict | None:
    """What arrived in the panel since the previous run, from the working copy's
    own manifest — the same numbers Data Validation & Check computed."""
    manifest_path = config.DATA_DIR / "MANIFEST.json"
    if not manifest_path.exists():
        return None
    try:
        man = json.loads(manifest_path.read_text())
    except (ValueError, OSError):
        return None
    if man.get("as_of") != as_of:
        return None
    carried = man.get("corrections_carried") or []
    applied = man.get("corrections_accepted") or []
    out = {
        "panel_read_through": man.get("latest_complete_week"),
        "center_weeks_in_scope": (man.get("tables", {}).get("clinic_weekly", {}) or {}).get(
            "rows_before"
        ),
        "rows_under_standing_corrections": sum(int(c.get("new_rows") or 0) for c in carried),
        "provider_rows_under_standing_corrections": sum(
            int(c.get("new_rows") or 0)
            for c in applied
            if c.get("decision_status") == "carried"
            and c.get("scope") == "standing"
            and c.get("table") == "provider_weekly"
        ),
        "corrections_carried": len(carried),
        "corrections_new": len(man.get("corrections_new") or []),
    }
    if prior:
        out["previous_run"] = prior
    return out


def build_carry_in(as_of: date | str) -> dict:
    """Step zero. Week one inherits nothing and says so; every later week opens
    with the previous Monday's open recommendations and the new data in hand."""
    as_of_str = str(as_of)
    prior = previous_run(as_of_str)
    open_rows = _open_ledger_rows(as_of_str)
    return {
        "previous_run": prior,
        "is_first_run": prior is None and not open_rows,
        "open_recommendations": open_rows,
        "open_recommendation_count": len(open_rows),
        "due_this_week": [
            r["rec_id"] for r in open_rows if (r.get("check_by") or "") <= as_of_str
        ],
        "new_data": _new_data(as_of_str, prior),
    }


# ---------------------------------------------------------------------------
# The context itself
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    seq: int
    kind: str  # carry_in | tool_result | reasoning
    phase: str | None
    ts: str
    payload: dict

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "phase": self.phase,
            "ts": self.ts,
            "payload": self.payload,
        }


@dataclass
class RunContext:
    """One run's ordered history. Append-only, in memory and on disk."""

    as_of: str
    path: Path | None = None
    entries: list[Entry] = field(default_factory=list)

    # -- construction -------------------------------------------------------

    @classmethod
    def open(cls, as_of: date | str, write: bool = True) -> "RunContext":
        """Load the context for a Monday, or start one seeded with its carry-in."""
        as_of_str = str(as_of)
        path = context_path(as_of_str) if write else None
        ctx = cls(as_of=as_of_str, path=path)
        existing = context_path(as_of_str)
        if existing.exists():
            ctx.entries = cls._read(existing)
        if not ctx.entries:
            ctx._seed(build_carry_in(as_of_str))
        return ctx

    def _seed(self, payload: dict) -> None:
        """Atomically establish Step 0 when pipeline.run and the one follower
        open the same new context at nearly the same instant.

        A complete temporary line is hard-linked into place, so the winner is
        first-write-wins and the loser can only see the winner's whole record.
        This keeps one carry-in plus four tool/reasoning pairs: exactly 9 rows.
        """
        if not self.path:
            self.append("carry_in", None, payload)
            return

        entry = Entry(
            seq=0,
            kind="carry_in",
            phase=None,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            payload=payload,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{CONTEXT_FILENAME}.seed-",
                delete=False,
            ) as fh:
                temp_path = Path(fh.name)
                fh.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
            try:
                os.link(temp_path, self.path)
                self.entries = [entry]
                return
            except FileExistsError:
                self.entries = self._read(self.path)
                if self.entries:
                    return
                # Recover a pre-existing empty/torn file. This is not the
                # normal concurrent path: a linked seed is already complete.
                self.append("carry_in", None, payload)
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _read(path: Path) -> list[Entry]:
        entries: list[Entry] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line never invalidates the history before it
            if rec.get("kind") in ENTRY_KINDS:
                entries.append(
                    Entry(
                        seq=rec.get("seq", len(entries)),
                        kind=rec["kind"],
                        phase=rec.get("phase"),
                        ts=rec.get("ts", ""),
                        payload=rec.get("payload") or {},
                    )
                )
        return entries

    def reset(self) -> None:
        """Start the history over — a re-run of a Monday is a new run."""
        self.entries = []
        if self.path and self.path.exists():
            self.path.unlink()
        self.append("carry_in", None, build_carry_in(self.as_of))

    # -- appending ----------------------------------------------------------

    def append(self, kind: str, phase: str | None, payload: dict) -> Entry:
        if kind not in ENTRY_KINDS:
            raise ValueError(f"unknown context entry kind: {kind}")
        entry = Entry(
            seq=len(self.entries),
            kind=kind,
            phase=phase,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            payload=payload,
        )
        self.entries.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
        return entry

    def append_tool_result(self, phase: str, output: dict) -> Entry:
        """Record what a phase's tool call returned. First write wins.

        A run has exactly one tool_result and one reasoning entry per phase.
        Narration can be asked for again — `--phases signals` on a finished
        run, a second `--follow`, a bulk CLI pass over a Monday that already
        ran — and none of those are a second execution of the pipeline. They
        re-read the same artifacts, so appending them again would write a
        second history into a run that only ever had one. The duplicate is
        refused here, at the one door both paths go through, and the caller
        gets the entry that already stands.
        """
        return self._entry("tool_result", phase) or self.append("tool_result", phase, output)

    def append_reasoning(self, phase: str, text: str, meta: dict | None = None) -> Entry:
        """Record the agent's note on a phase. First write wins — see
        `append_tool_result` for why a re-narration must not append."""
        return self._entry("reasoning", phase) or self.append(
            "reasoning", phase, {"text": text, **(meta or {})}
        )

    def _entry(self, kind: str, phase: str) -> Entry | None:
        for e in self.entries:
            if e.kind == kind and e.phase == phase:
                return e
        return None

    # -- reading ------------------------------------------------------------

    @property
    def carry_in(self) -> dict:
        for e in self.entries:
            if e.kind == "carry_in":
                return e.payload
        return {}

    def tool_result(self, phase: str) -> dict | None:
        e = self._entry("tool_result", phase)
        return e.payload if e else None

    def reasoning(self, phase: str) -> str | None:
        e = self._entry("reasoning", phase)
        return e.payload.get("text") if e else None

    def has_tool_result(self, phase: str) -> bool:
        return self._entry("tool_result", phase) is not None

    def has_reasoning(self, phase: str) -> bool:
        return self.reasoning(phase) is not None

    def completed_tools(self) -> list[str]:
        return [e.phase for e in self.entries if e.kind == "tool_result" and e.phase]

    def history(self) -> list[Entry]:
        """Everything accumulated so far, in the order it happened."""
        return list(self.entries)

    # -- rendering into a prompt -------------------------------------------

    def render(self, upto_phase: str | None = None) -> str:
        """The accumulated history as prompt text.

        Everything the agent has seen this run, in order, including what it
        already said. `upto_phase` stops before that phase's own tool result —
        the agent is narrating that result from the artifacts themselves, and
        the history is what came before it.
        """
        lines: list[str] = []
        for e in self.entries:
            if upto_phase and e.kind == "tool_result" and e.phase == upto_phase:
                break
            if e.kind == "carry_in":
                lines.append(self._render_carry_in(e.payload))
            elif e.kind == "tool_result":
                lines.append(
                    f"### Step {e.seq} — {e.phase} returned\n"
                    + json.dumps(e.payload, indent=2, ensure_ascii=False)
                )
            elif e.kind == "reasoning":
                lines.append(f"### Step {e.seq} — you wrote about {e.phase}\n{e.payload.get('text', '')}")
        if not lines:
            return "(nothing yet — this is the first step of the run)"
        return "\n\n".join(lines)

    @staticmethod
    def _render_carry_in(payload: dict) -> str:
        head = "### Step 0 — what this run inherited"
        if payload.get("is_first_run"):
            return (
                f"{head}\nNothing: this is the first run, with no earlier Monday to "
                f"carry recommendations forward from."
            )
        return head + "\n" + json.dumps(payload, indent=2, ensure_ascii=False)
