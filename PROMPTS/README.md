# PROMPTS/ — the runtime prompt library (V1)

**This folder is a code path, not documentation.** The pipeline loads these `.md` files at
runtime, substitutes the `{{PLACEHOLDER}}` blocks, and sends the result to whichever LLM tier
is available (claude-cli → api → openai → template; the ladder lives in `pipeline/verdicts.py`
and every other step reuses it). Editing a prompt changes what the system does on the next
run; no Python changes required. The `template` tier runs no prompt at all — it builds its
sentences deterministically from the same facts, which is why the repo still works for a
reviewer with no model access.

| File | Used by | What it asks for | Placeholders |
|---|---|---|---|
| `claimed-vs-verified.md` | `pipeline/verdicts.py` | The "Claimed vs. Verified" ruling on one action plan: bucket, one plain-English sentence, one recommended action | `{{PLAN_FACTS_JSON}}`, `{{REPORTED_STATUS}}`, `{{RETRY_NOTE}}` |
| `phase-note.md` | `pipeline/narrate.py` | ONE completed pipeline phase narrated to a non-engineer leader — a conversational lead line, then bullets of what was DECIDED or FLAGGED (center + metric + numbers), from that phase's artifacts plus trimmed prior-phase context | `{{AS_OF}}`, `{{LATEST_WEEK}}`, `{{LEADER}}`, `{{PHASE}}`, `{{PHASE_TITLE}}`, `{{ARTIFACTS}}`, `{{PRIOR_CONTEXT}}`, `{{RETRY_NOTE}}` |
| `answer.md` | `pipeline/ask.py` | One grounded answer to the leader's question about this run — with citations, or a specific refusal naming the missing data | `{{AS_OF}}`, `{{LATEST_WEEK}}`, `{{LEADER}}`, `{{QUESTION}}`, `{{ARTIFACTS}}`, `{{RETRY_NOTE}}` |
| `next-move.md` | `pipeline/successor.py` | The next move after a recommendation failed or was ignored: a different mechanism, an accountable owner, the number that should move, a new check-by date | `{{AS_OF}}`, `{{CENTER}}`, `{{METRIC}}`, `{{OWNER}}`, `{{OWNER_ROLE}}`, `{{CHECK_BY}}`, `{{CONTEXT_JSON}}`, `{{ESCALATION_INSTRUCTION}}`, `{{RETRY_NOTE}}` |

`{{RETRY_NOTE}}` is how a failed check comes back to the model: the harness's complaint is
pasted into the next attempt, so a regeneration is told exactly what to fix.

Two invariants every prompt in this library obeys:

- **Prompts never define scoring or arithmetic.** All formulas, windows, weights, and
  thresholds live in code and are documented in `docs/SCORING.md`. Prompts receive computed
  facts as input and produce language and judgment.
- **No number a prompt produces reaches a leader unchecked.** Every LLM output passes through
  the harness (`pipeline/harness.py`): every figure in it must exist in the facts that were
  pasted into the prompt, and a deterministic rule table bounds the conclusions. Failures
  regenerate (max 2 retries), then fail the run loudly — or fall back to the deterministic
  text where a silent phase would be worse.

  **Where the language check runs, precisely.** Narration (`narrate.py`) and successor
  recommendations (`successor.py`) additionally enforce leader-facing *language* — no internal
  IDs, no raw metric ids, no process narration — and successors add a rule check rejecting
  restatements and dead-end verbs. Verdicts and asks are **number- and reasoning-checked but
  not language-checked**; there, the ban on writing `PCC_011` or `recheck_compliance_pct` is
  instructed in the prompt (`claimed-vs-verified.md` R2/R3) rather than enforced in code. No committed
  artifact has ever leaked one, but that is a prompt holding the line, not a guard — and
  closing that gap by reusing `narrate.check_leader_language` is the first item on the V2 list
  below.

## V2 roadmap (documented decision)

V1 deliberately keeps the judgment instructions — including the verdict decision table — in
these prompt files, because prompts are the fastest thing to iterate on while the digest's
voice is being tuned. Prompts are also the weakest thing to guarantee, which is the whole
argument for moving off them.

1. **Enforce leader language everywhere a model writes**, not just in narration and
   successors. `narrate.check_leader_language` already exists; wiring it into
   `harness.validate_verdict` and `ask.py` turns two prompt instructions into two guards.
2. **Move logic out of prompts and into code.** The decision table already exists in code
   (`pipeline/harness.py`) as the enforcement layer, so V2 shrinks the prompts to pure
   language-generation ("here is the verdict and the facts; write the sentence") and the
   prompt library stops being able to change an outcome at all.
