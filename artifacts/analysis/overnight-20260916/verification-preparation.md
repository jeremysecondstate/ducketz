# September 16 final verification helper

Prepared for native original attempt `20260916T040757.217937Z`, September 15 source session, September 16 action session, and unchanged deadline `2026-09-16T11:00:00Z` (04:00 Pacific). The production watchlist currently contains eleven symbols. This document is preparation evidence, not a final verification result.

Run only after the native terminal attempt reports `COMPLETE` and has its completed receipt:

```powershell
.\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260916/verify_completed_run.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z | Out-File -FilePath artifacts/analysis/overnight-20260916/final-verification.json -Encoding utf8
```

If recovery produced a terminal resume, pass that terminal attempt to `--overnight-run`; keep the original-run, source/action dates and deadline defaults unchanged. The helper validates the full original ancestry and completed stage union. A small JSON-only preflight refuses unfinished attempts with exit code 2 before archive/model imports or expensive checks. Successful completion exits 0, audit failures exit 1. Observe Python's exit status as well as the saved JSON result.

The helper is adapted from the reconciled September 15 verifier. Its eleven sections cover native receipts/log hashes and ancestry, current source-bound Gameplan, OPRA coverage, XNAS history/cost/capacity, fetch scope logs, enrichment qualification, saved trade plan and cash/share reconstruction, price path reconstruction, synthetic-reference lineage, cumulative evaluation, and prior-session actuals. It reads the current production watchlist for the new publication and each historical publication's own universe for cumulative evaluation. Eleven current symbols imply 264 forecasts, intents and augmented rows, 154 price points, and 33 OPRA scopes.

Preserved lessons:

- OPRA log completeness uses the exact symbol/schema union of native Historical and Live replay successes. Historical-only completion lines are not required; mismatched/duplicate/missing scopes still fail. Native cursor verification separately checks source evidence.
- New direction checks use the current approved `stock-direction-50-v2`. Historical forecasts retain their saved labels and universes.
- Sparse v3 planning uses the approved 240-minute same-session after-hours limit; v2 references retain their 15-minute limit. Native observations, model targets and actuals receive no synthetic rows.
- Explicit `UNAVAILABLE_PRICE_REFERENCES` is valid only when native reconstruction produces the same unavailable points and no fabricated chronological trades, ending cash or ending holdings.
- Actuals recompute frozen forecasts and saved pre-open estimates, including native boundary diagnostics. Pending and missing observations remain explicit; observed market prices are not fills.
- The helper preserves exact native summaries for FMP/FRED/Schwab/SEC and other fetch scopes. Their warnings need the supervisor's separate evidence-based review; the OPRA cursor contract does not establish other providers' completeness or archive-wide validity.

Preparation validation used AST parsing and CLI `--help` only. No archive verification, provider/account request, pipeline, claim, schedule, publication, model fitting, production edit or order action was performed. Run result availability and any fresh policy/source changes must be assessed at completion. The previous day's unavailable IONQ/PATH price paths and research-only sizing groups are context, not assumptions about tonight's result.
