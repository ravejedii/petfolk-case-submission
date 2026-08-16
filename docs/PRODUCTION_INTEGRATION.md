# Production integration — action plans, approval, and system memory

The case artifact proves the decision loop on the supplied assessment data. It does **not** pretend that Petfolk needs a second action-plan system.

In Petfolk's current operating model, each clinic already has an Operating Plan Tracker in Google Sheets. Business Partners and Partner Doctors maintain the plan, the Regional Partner is the approval step, and the operating team updates progress, status, and completion there. In production, that workflow should stay intact.

## Production flow

1. **Detect and verify the signal.** The deterministic pipeline decides what deserves attention and builds the evidence packet.
2. **Draft the action plan.** AI proposes the business area, accountable owner, goal/outcome, action steps, expected KPI movement, and check-by date. At this point it is a recommendation, not a commitment.
3. **Regional Partner gate.** The Regional Partner can approve, edit, or decline the recommendation. A declined recommendation is logged but never becomes an active commitment.
4. **Write the approved plan to the existing tracker.** The clinic's Operating Plan Tracker remains the operational system of record.
5. **Append the decision to machine memory.** The recommendation ledger stores the approved payload, decision, evidence references, owner, target, check-by date, and lineage so the next run can reason across Mondays without overwriting history.
6. **Read human execution evidence.** Status, progress updates, and actual completion remain human-reported operating facts. The system never infers that work happened merely because the KPI moved.
7. **Measure the outcome independently.** The next run re-computes the KPI from operating data and compares it with the approved plan's expected movement.

That keeps two questions separate:

- **Was the work carried out?** Human-attested from the operating workflow.
- **Did the metric move?** Deterministically measured from source data.

The separation matters. A metric improving does not prove the intervention caused it, and a flat metric does not prove the team failed to execute.

## What the case artifact does vs. production

| Case artifact | Production integration |
|---|---|
| Generates a recommendation and stores it in the local ledger | Generates a **draft** recommendation, then routes it through Regional Partner approval |
| Local ledger is the durable memory used by the next run | Google Sheets remains the operational system of record; the ledger becomes the audit + machine-memory layer |
| Execution defaults to `unknown` unless explicitly attested in the demo | Execution status is read from the existing plan workflow and remains human-owned |
| KPI outcome is re-checked from the supplied CSVs | KPI outcome is re-checked from the production operating-data sources |

No Google Sheets write-back is simulated in this take-home. Faking an integration would make the prototype look more complete while making its evidence weaker. The point of the artifact is to prove the attention, recommendation, verification, and closed-loop logic; production wiring should preserve Petfolk's existing approval process rather than route around it.

## Exam-room transcripts

Ambient exam-room transcripts can add useful context later, but they should not enter the V1 deterministic priority score. A safer sequence is:

**signal surfaced → retrieve relevant transcript evidence → form a richer action hypothesis → verify/ground the generated claim → Regional Partner approval**

That lets transcript evidence improve the recommendation without allowing an LLM interpretation of clinical text to silently decide which clinic deserves leadership attention.
