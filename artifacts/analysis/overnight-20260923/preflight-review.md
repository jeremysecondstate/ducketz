# September 23 bounded verifier preflight

Prepared 2026-09-23T04:11:33Z for native run `20260923T040814.422433Z`, source September 22, action September 23, original deadline September 23 11:00 UTC / 04:00 Pacific. Root retains the supervision claim. This work made no provider/broker call, pipeline control, claim action, production edit, model fit, archive rescan or completion audit.

## Prepared helpers

Both audit helpers are copies of yesterday's successful verifiers. Only the current run/date/deadline defaults, introductory date and YG output-directory guard changed. All source, model, ownership, availability and qualification checks remain intact. Both passed AST syntax parsing only; neither audit ran.

- `verify_completed_run.py` keeps its early COMPLETE report-and-receipt guard before heavy imports. It verifies native ancestry and original eight-stage completion, logs/receipts, current Gameplan and source, all 33 OPRA scopes, XNAS acquisition, enrichment fit versus qualification, original safe account ownership, null `planning_override`, source-bound YG trade rows, deterministic cash/share conservation, native price and synthetic-reference lineage, cumulative evaluation and actuals.
- `verify_yg_completion.py` keeps its early terminal-COMPLETE guard for normal final verification. Its separate publication-only mode cannot establish native completion. It reproduces saved model scores and v2 gates without fitting, with development-only family/calibration selection, fixed logistic grid, weekly shrinkage, purged chronology, exact symbol/route support and target-matched retained champions. Faithfully reported research status remains a coverage note, never a manufactured promotion.
- Production has eleven symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST, IONQ. Expected current counts remain 264 forecasts, 264 stock-only intents, 264 augmented rows and 33 OPRA cursors. Historical checks use each frozen publication's own universe.

Hashes and exact textual changes are recorded in `verifier-preflight.json`; the source helpers and all historical artifacts remain unchanged.

## Contract findings

The observed new native report explicitly records `raw-price-direction-v1` / YG, independent stock-only preparation, `xnas-itch-archive-v1`, the full eight-stage order, no deadline exception and zero overnight orders. The verifiers retain the exact pinned source/action dates and root attempt even if a terminal resume is required.

Current implementation and the dated contract updates record `stock-direction-50-v2`, signal-driven accumulation and same-horizon bearish sales without scheduled expiry, and sparse-session planning-reference v2 / price-path v3 with at most 240 after-hours minutes and complete source acquisition. Older recurring excerpts describe earlier 54/46, expiry and 15-minute policies. The verifiers preserve the saved current native contracts; they do not alter any policy. Actuals and training continue to require original observed prices within five minutes, and unavailable planning references cannot invent trades, ending cash or ending holdings.

There is an internal documentation discrepancy: `NIGHTLY_GAMEPLAN.md` lines 829-837 document v2 Brier <= baseline + 0.005 and log loss <= baseline + 0.01, while lines 844-846 claim strict baseline wins. `ml/gameplan_promotion.py` explicitly implements and numerically validates versioned v2 tolerances for new publications and strict v1 for historical publications. The current user contract also explicitly approves v2. The audit recomputes each saved v2 gate and reports actual scores, baseline differences, probability variation, ECE and sample checks; it does not reinterpret qualification as baseline outperformance or weaken the existing implementation.

The September 18 all-promoted deployment revision is explicitly date/source bound in the contract. The ordinary September 23 audit does not generalize that dated activation gate. Unqualified new groups require evidence-based diagnosis; no unchanged retry or assessment-driven candidate selection is justified.

The concurrent `ml/gameplan_trade_planning.py` diff adds `_planning_snapshot` for a stale local reservation override. The original `snapshot.ownership.safe_for_planning` gate remains mandatory before price calculation; safe snapshots return unchanged with null override. The copied verifier still requires the original saved snapshot to be safe and the override to be null. No production diff was changed.

## Predictable tail attention

- Yesterday completed all eight stages and both final audits. Weekly directional output remained `RESEARCH_NOT_PROMOTED`; its eight newly matured targets, fixed partitions and narrow development winner were investigated with no concrete fitting/data/selection defect. Today's model results need their own evidence; the prior status is no reason to retry unchanged training. Learned sizing was qualified only for part of hourly scope, separately from directional and manual-policy behavior.
- Yesterday's stale zero-fill end-of-session reservations required the previously authorized native evidence reconciliation. Today's saved session ended `FINISHED_WITH_ERRORS`; its sole final-cycle error is `EXECUTION_WINDOW_CLOSED_AFTER_BROKER_CAPTURE` / `OUTSIDE_US_EQUITY_ACTIONABLE_SESSION` at the closing boundary. A local read-only ledger check found one remaining WORKING SELL38 reservation. Root's separate ownership agent is handling the exact current case; this preflight made no broker call, inferred no cancellation and changed no ledger state. Preserve the original ownership gate and fresh snapshot requirement.
- Yesterday's planning prices were fully available but included explicitly synthetic CROX/PATH/TWST anchors, including TWST's 208-minute gap. Today's observed close and source coverage must determine today's availability and disclosure; no prior price may be copied.
- Yesterday's actuals covered September 21, for which the pre-opening trade plan was absent. Today's actuals target September 22, which does have a verified frozen trade plan. The existing comparison code selects and validates that original plan; it must not inherit yesterday's missing-estimate assumption or rebuild historical estimates.
- The retained optional Pricing source and raw CME ESU6/NQU6 omissions were known provider limitations yesterday. Preserve source gates and assess new native evidence without treating a repeated unchanged advisory as a new defect. Complete OPRA acquisition does not imply a qualified optional Pricing model or an observed bar at every clock.

## Final invocation after native COMPLETE

Use the terminal attempt path if the root run resumes, retaining the original ID and deadline:

```powershell
$terminalRun = 'C:/DATASTORE/ml/overnight-runs/20260923T040814.422433Z'
& ./.venv/Scripts/python.exe -B artifacts/analysis/overnight-20260923/verify_completed_run.py --overnight-run $terminalRun --expected-original-run 20260923T040814.422433Z --expected-source-date 2026-09-22 --expected-action-date 2026-09-23 --expected-deadline 2026-09-23T11:00:00Z > artifacts/analysis/overnight-20260923/final-verification.json
$fullAuditExit = $LASTEXITCODE
& ./.venv/Scripts/python.exe -B artifacts/analysis/overnight-20260923/verify_yg_completion.py --overnight-run $terminalRun --expected-original-run 20260923T040814.422433Z --expected-action-date 2026-09-23 --expected-deadline 2026-09-23T11:00:00Z --output C:/dev/ducketz/artifacts/analysis/overnight-20260923/yg-final-verification.json
$ygAuditExit = $LASTEXITCODE
```

Root must continue renewing supervision during final audits. Other old monitoring/provider/reconciliation helpers pin dates, output paths or specific account evidence and cannot be reused unchanged.
