// What one Monday inherited from the Monday before it, and what happened when
// that memory met the new week's evidence.
//
// This is the client's ONE source of truth for the loop's shape, the same way
// phases.js is the one source for phase names. The digest page, the run console
// and the ledger section all read it, so a reviewer cannot be shown "2 carried
// forward" on one surface and a differently-derived number on another.
//
// Nothing here computes an outcome. Every value is either read verbatim from
// `digest.carry_in` — the run's own Step 0, recorded by
// `pipeline/runctx.py:build_carry_in()` before the ledger step touched anything
// — or is a partition of the ledger rows the pipeline already produced. The one
// judgement it makes is which bucket a row belongs in, and that follows from
// rec_id membership alone.

export function mondayShort(iso) {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

// The three buckets a reviewer has to be able to tell apart, plus the facts
// about the memory itself.
//
//   rechecked  opened by an earlier Monday and graded against this week's data
//   created    genuinely new this Monday
//   stillOpen  carried forward but not graded this run (not due, no new data)
//
// `carryIn` may be absent — a digest written before Step 0 was recorded in it.
// The buckets still resolve from each row's `created_week`, and `known` says
// the memory facts (which Monday, how many corrections) are unavailable rather
// than letting the UI invent them.
export function summarizeMemory({ carryIn, ledger, asOf } = {}) {
  const changed = (ledger && ledger.changed) || [];
  const created = (ledger && ledger.created) || [];
  const open = (ledger && ledger.open) || [];

  const changedIds = new Set(changed.map((r) => r.rec_id));
  const createdIds = new Set(created.map((r) => r.rec_id));

  // `ledger.created` is a creation receipt — it carries the ask, not the
  // lifecycle fields a card renders. The same row appears in `ledger.open`
  // fully populated, so prefer that copy and keep the receipt as the fallback.
  const openById = new Map(open.map((r) => [r.rec_id, r]));
  const newThisRun = created.map((r) => openById.get(r.rec_id) || r);

  const carriedIds = new Set(
    carryIn && Array.isArray(carryIn.open_recommendations)
      ? carryIn.open_recommendations.map((r) => r.rec_id)
      : [...changed, ...open]
          .filter((r) => r.created_week && asOf && r.created_week < asOf)
          .map((r) => r.rec_id)
  );

  const newData = (carryIn && carryIn.new_data) || null;
  const num = (v) => (typeof v === "number" ? v : null);

  return {
    // Are the memory facts below readable at all, or only the buckets?
    known: Boolean(carryIn),
    isFirstRun: carryIn ? Boolean(carryIn.is_first_run) : carriedIds.size === 0,
    previousRun: (carryIn && carryIn.previous_run) || null,
    carriedCount: carryIn
      ? num(carryIn.open_recommendation_count) ?? carriedIds.size
      : carriedIds.size,
    dueCount:
      carryIn && Array.isArray(carryIn.due_this_week)
        ? carryIn.due_this_week.length
        : null,
    correctionsCarried: num(newData && newData.corrections_carried),
    correctionsNew: num(newData && newData.corrections_new),
    rowsUnderStandingCorrections: num(newData && newData.rows_under_standing_corrections),
    providerRowsUnderStandingCorrections: num(
      newData && newData.provider_rows_under_standing_corrections
    ),
    panelReadThrough: (newData && newData.panel_read_through) || null,

    rechecked: changed.filter((r) => carriedIds.has(r.rec_id)),
    created: newThisRun,
    stillOpen: open.filter(
      (r) => !createdIds.has(r.rec_id) && !changedIds.has(r.rec_id)
    ),
  };
}

// What the re-check found, counted the way the ledger keeps it: what the
// NUMBERS did and whether a human confirmed the WORK happened are two separate
// tallies, and they are never folded into one. A metric moving the right way is
// not evidence that anyone acted.
export function recheckCounts(rechecked) {
  const rows = rechecked || [];
  const count = (field, value) => rows.filter((r) => r[field] === value).length;
  return {
    total: rows.length,
    working: count("outcome", "working"),
    notWorking: count("outcome", "not_working"),
    flat: count("outcome", "flat"),
    unverifiable: count("outcome", "unverifiable"),
    executionDone: count("execution", "done"),
    executionNotDone: count("execution", "not_done"),
    executionUnknown: count("execution", "unknown"),
  };
}

// "1 moved the right way, 1 moved the wrong way" — the outcome half only.
// Returns "" when nothing was re-checked, so a caller can skip the clause.
export function outcomeClause(counts) {
  const parts = [];
  if (counts.working) parts.push(`${counts.working} moved the right way`);
  if (counts.notWorking) parts.push(`${counts.notWorking} moved the wrong way`);
  if (counts.flat) parts.push(`${counts.flat} did not move`);
  if (counts.unverifiable) parts.push(`${counts.unverifiable} could not be measured`);
  return parts.join(", ");
}

// The execution half, always stated separately from the outcome half.
export function executionClause(counts) {
  const parts = [];
  if (counts.executionDone) parts.push(`${counts.executionDone} confirmed carried out`);
  if (counts.executionNotDone) parts.push(`${counts.executionNotDone} confirmed not carried out`);
  if (counts.executionUnknown)
    parts.push(
      counts.executionUnknown === counts.total
        ? "none confirmed as carried out"
        : `${counts.executionUnknown} not confirmed either way`
    );
  return parts.join(", ");
}
