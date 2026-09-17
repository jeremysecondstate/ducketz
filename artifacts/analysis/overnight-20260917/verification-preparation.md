# September 17 final-verification preparation

Prepared at 2026-09-17T04:09:54Z. This preparation did not run the full audit, inspect native archives, call providers or a broker, acquire supervision, or change production code, data or pointers.

Copied [the prior verifier](C:/dev/ducketz/artifacts/analysis/overnight-20260916/verify_completed_run.py) into [tonight's helper](C:/dev/ducketz/artifacts/analysis/overnight-20260917/verify_completed_run.py). Its defaults now bind source September 16, action September 17, original native run `20260917T040722.433471Z`, and the ordinary `2026-09-17T11:00:00Z` deadline. Complete receipt/report preflight and all eleven original audit sections remain. Historical publications retain their own saved universes; the current watchlist contains eleven symbols, implying 264 forecasts, 264 stock-only intents, and 33 production OPRA scopes.

The September 16 [holding contract](C:/dev/ducketz/docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md) and latest automation memory require an adjustment: new trade plans use `project_direction_trades(..., signal_driven=True)`. The prior helper defaulted to legacy expiry/promotion behavior and would falsely reject a correctly produced plan. The new helper explicitly selects signal-driven recomputation, requires the saved holding-policy identity and its readable explanation, and independently rolls each horizon's available/reserved holdings forward. It rejects expiry sales, non-bearish sales, duplicate forecast events, mismatched forecast clocks, and sales borrowing other horizons' or reserved shares. Repeated bullish events accumulate; unpromoted manual instructions are compatible with separately verified, unchanged research model assessment.

The explicit price-unavailability branch remains supported without fabricated events or ending balances. Sparse-session references retain their manifest-bound 240-minute approved planning policy, exact observed intraday samples, native archive fingerprint, and original observation timestamps. The recent historical-reference-veto repair is naturally covered by deterministic recomputation through current native builders; no obsolete symbol-wide veto was added.

Validation was deliberately lightweight: CLI help and AST syntax succeeded. Three synthetic fixtures (successive accumulation then bearish sale, unpromoted weekly ownership protected from an hourly sale, and existing allocations plus reserved shares) passed the new independent audit. Three mutations (expiry sale, duplicate forecast, and excess sale) were rejected. These fixtures did not access production artifacts or external services.

After native COMPLETE, run from `C:/dev/ducketz`, substituting the terminal completed attempt if a resume was required:

```powershell
& C:/dev/ducketz/.venv/Scripts/python.exe -B C:/dev/ducketz/artifacts/analysis/overnight-20260917/verify_completed_run.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260917T040722.433471Z --expected-original-run 20260917T040722.433471Z --expected-source-date 2026-09-16 --expected-action-date 2026-09-17 --expected-deadline 2026-09-17T11:00:00Z > C:/dev/ducketz/artifacts/analysis/overnight-20260917/final-verification.json
$LASTEXITCODE
```

Exit 0 means every selected check passed; explicit coverage notes still need reporting. Exit 1 means a verification failure. Exit 2 means native report/receipt were not both COMPLETE and the archive audit did not start. `--only-fetch-log` is a narrower final diagnostic and is never a substitute for the full audit. Supervision renewal remains the root owner's responsibility while the full local archive audit runs.

Remaining limits: no conclusion about tonight's results exists yet. The helper expects current pointers to still select the just-completed publication and native fresh planning snapshot; an independent later publication or informational refresh requires investigation, not relaxation. FMP/FRED/Schwab/SEC and nonproduction Databento outcomes still require the supervisor to assess the exact log summaries surfaced by the fetch check. Native model/price missingness and failed model assessments remain visible coverage notes rather than invented passes or prices. Paused daytime supervision and the disabled Windows launcher remain unchanged.
