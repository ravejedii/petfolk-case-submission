# Assessment data

`INPUTS/` contains the four assessment CSVs exactly as provided. The two files whose
download names include ` (1)` are resolved to canonical table names by the pipeline.
These raw files are read-only.

`TRANSLATION/` contains the accepted, corrected working copies and their manifest.
Every metric, score, and conclusion downstream is computed from that working set,
while the original inputs remain unchanged.

To rebuild the two-Monday demonstration from scratch:

```bash
.venv/bin/python -m pipeline.validate --as-of 2026-04-27 --accept all
.venv/bin/python -m pipeline.run --as-of 2026-04-27 --fresh-ledger
.venv/bin/python -m pipeline.validate --as-of 2026-05-04 --accept all
.venv/bin/python -m pipeline.run --as-of 2026-05-04
```

Digests, logs, ledger entries, and other generated artifacts are written to
`OUTPUTS/`, which is ignored by Git so every reviewer can observe a fresh run.
