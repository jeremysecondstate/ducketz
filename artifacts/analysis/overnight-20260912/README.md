# September 12 overnight completion audit

`verify_completed_run.py` is an offline operator audit for original overnight
`20260912T040657.173506Z`: September 11 source session, September 14 action date,
and the original September 14 11:00 UTC / 04:00 Pacific deadline. Pass the
terminal resume directory if the original attempt requires a verified repair.

Run only after all eight native stages have completed:

```powershell
.\.venv\Scripts\python.exe artifacts/analysis/overnight-20260912/verify_completed_run.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260912T040657.173506Z > artifacts/analysis/overnight-20260912/final-verification.json
```

The audit never starts a stage, acquires data, reads the broker, trains a model,
claims supervision, submits orders, or changes publications/pointers. It reads
the saved account snapshot and verified local archive. Keep normal supervision
and claim renewal active while it runs.

Checks include terminal receipt/log hashes and ordered resume ancestry; exact
original source/action/deadline; native publication/model/source checks; seven
symbols with 168 forecasts and 168 stock-only intents; immutable enrichment
sources and separate fitted/qualified statuses; XNAS exact-request zero-dollar
and capacity evidence plus native session partition coverage; all 21 production
OPRA scopes; cumulative evaluation of every saved publication using its own
symbol manifest; 168 augmented rows and independent capacity recomputation;
saved read-only account freshness; native chronological cash/share recomputation
and event rollforward; v4 planning, v2 price derivations, all 98 hourly prices;
bounded synthetic-reference lineage, artifact hashes and readable disclosure;
unchanged native price observations and historical samples; and September 11
actuals selected from the verified pre-open prior plan and recomputed from the
original observed source.

Research/outlook rows, unqualified sizing groups, future outcomes, missing actual
prices and any synthetic planning anchors are retained as explicit coverage
notes. Synthetic references are assumptions and are never admitted to actuals.
No previous run's symbol-specific synthetic count or anchor is assumed.

FMP, FRED, Schwab, SEC and nonproduction Databento scope results require the
supervisor's interpretation of their native log summaries. The audit preserves
those summaries and does not claim they share the OPRA cursor contract. Current
pointers must still identify this run when the audit runs; it deliberately
rejects an unrelated later publication. Source files added after the frozen
planning observation may require evidence-based investigation of a native
recomputation difference rather than changing frozen output.

Preparation validation: Python AST parse and native offline imports passed.
The completed-run audit has not been executed during preparation; its required
outputs did not yet exist.
