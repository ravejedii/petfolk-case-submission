# Successor recommendation — the next move after one that did not work

Last week this digest made a recommendation. It has now been re-checked against the real
weeks that followed, and it did not work: either the metric moved the wrong way, or nothing
moved at all. Your job is to write the NEXT MOVE — a different, specific thing to try — so
the leader's Monday ends with an action, never with "rethink the fix".

The reader is a veterinary regional leader (a doctor, not an analyst) reading this on a
Monday morning with about thirty minutes. The recommendation has to survive that meeting:
one accountable person, one concrete thing to do differently, one number that should move,
one date it will be checked.

Run date (as-of): {{AS_OF}} · center: {{CENTER}} · metric: {{METRIC}}
Accountable: {{OWNER}} ({{OWNER_ROLE}}) · new check-by date: {{CHECK_BY}}

{{ESCALATION_INSTRUCTION}}

## Hard rules

1. **Different from what was already tried.** The failed recommendation is in the context
   below. Repeating it — the same verb, the same review, the same ask — is the one answer
   that is definitely wrong. Name a mechanism, not an intention: who does what, when, and
   what changes in the day-to-day as a result.
2. **Never a dead end.** Do not write "rethink", "reconsider", "re-evaluate", "look into",
   or "figure out" as the action. Those are not moves.
3. **Every number you write must appear in the context below** (or be a simple difference
   between two of them). Your answer is machine-checked against that context; one number
   that isn't there gets the whole answer rejected and regenerated.
4. **Falsifiable.** Name {{OWNER}} by name, say what should move and roughly how far, and
   end on the check-by date {{CHECK_BY}} — so next Monday's re-check can call it worked or
   not worked without any argument.
5. **Plain language for a doctor.** Real center names only — never internal IDs (nothing
   like "PCC_006", "DVM_0056", or "AP_003"). Never raw column names (say "staff call-outs",
   never "staff_call_outs"). Never call the data step an "audit".
6. Dates only as they appear in the context (YYYY-MM-DD).

## Context — what was tried, what the numbers did, and who carries it

```json
{{CONTEXT_JSON}}
```
{{RETRY_NOTE}}
## Answer format — exactly three lines, nothing else

NEXT MOVE: <one sentence: {{OWNER}} does this specific different thing at {{CENTER}}>
EXPECTED: <one sentence: what number should move, from where toward where, by {{CHECK_BY}}>
WHY: <one sentence: what was tried, what the metric actually did since, and why this is different>
