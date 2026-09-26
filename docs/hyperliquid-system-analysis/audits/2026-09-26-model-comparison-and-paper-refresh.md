# Model comparison and excluded Paper sample

User-authorized archive, bounded training comparison and fresh simulated Paper
mirror on **2026-09-26**. The comparison rejected both proposed training changes.

**Excluded Paper sample, deletion completed:** the user subsequently directed
that the run seeded at **2026-09-26T10:35:59.867633343Z** be deleted without an
archive or analytical use. Automatic approval review initially blocked recursive
deletion; the user then completed it. The temporary cleanup directory
`C:/DATASTORE/hyperliquid/_pending_deletion/20260926T103559Z-excluded-paper` was
verified absent at **2026-09-26 11:36:32 UTC**. It was not an analytical archive.
The sample remains permanently excluded from analysis and recovery. Its opening
balances, performance observations and run-specific
deployment evidence are excluded from this audit. This is a user-chosen
evaluation exclusion, not evidence that every Hold was defective. The separate
earlier archive and offline model comparison below remain valid historical
records; neither belongs to the excluded sample.

## Preserved previous run

Gracefully stopped Paper and models and verified their owners exited. The data
coordinator continued. Operations Watch was paused and an `in_progress`
maintenance record prevented recovery during the intentional transition.

Archive: `C:/DATASTORE/hyperliquid/_paper_archives/20260926T102933Z-qualified-15m-v1`.
It contains the entire `_paper` and `_models` trees, pre-change source/config
snapshots, final summary, previous maintenance record and a SHA-256 manifest
covering 317 files / 161,599,704 bytes. Read-only ledger integrity checks passed.

| Final ledger observation, 10:29:07.740913 UTC | Amount |
| --- | ---: |
| Opening equity | $42,071.690760 |
| Ending equity | $42,026.232231 |
| P/L since opening | -$45.458528 |
| Simulated fees | $32.874026 |
| Fills / virtual transfers | 20 / 0 |

The first seven opening adjustments incurred $26.908484 in fees, 81.9% of total
fees, plus $11.722682 execution-price cost relative to opening marks. That
includes spread, simulated slippage and possible within-snapshot movement.
An inherited position can immediately breach the unchanged historical-entry
stop or exposure rules; resetting does not avoid those reductions or costs.

At the last observation retaining all opening-market marks, 10:00:28.999461 UTC,
holding opening positions returned +$1.311821 excluding funding. Paper returned
+$9.609698 before fees/funding and -$21.472190 after fees, excluding funding.
The after-fee difference was -$22.784011 at those matched marks. Later snapshots
lacked a closed position's mark, so this is not a benchmark at archive time.
Separate UI screenshots do not isolate forecasting performance or a synchronized
return difference.

## Training comparison

The evaluation fixed its recipes and selection rule before fitting. Four markets
used immutable inputs ending at 10:30 UTC, with historical cutoffs 12, 9 and 6
days earlier for selection. Cutoffs 3 days earlier and at the tip were reserved
for later evaluation. All recipes used the same 288 assessment decision rows
per market/fold, strict horizon purging and labels masked beyond each cutoff.
Qualification thresholds, estimators and random seed remained fixed.

| Recipe | Mean selection Brier | Mean selection log loss | Qualified cells |
| --- | ---: | ---: | ---: |
| Current: uncapped fit, 192 calibration rows | 0.251411 | 0.696036 | 6/12 |
| Last 2,016 fit rows, 96 calibration rows | 0.253735 | 0.701321 | 7/12 |
| Last 2,016 fit rows, 192 calibration rows | 0.253123 | 0.701345 | 7/12 |

Lower losses are better. Neither alternative improved both mean scores, so
neither passed the predeclared selection rule. More Qualified cells did not
mean better aggregate probability estimates. No candidate was substituted
after viewing later results. Baseline-only later evaluation averaged Brier
0.252670 and log loss 0.698732 across eight market/fold cells; this does not
establish superiority over alternatives that were not evaluated on those folds.

There were 44 ensemble fits, about 40.62 seconds of measured fitting/evaluation
time. All retained the existing MLP 100-iteration convergence warning. Mean
scores for the current recipe also lagged constant baselines in this sample.
The experiment is small, retrospective and uses related markets and overlapping
one-hour outcome windows. It is neither evidence of a reliable edge nor an
execution backtest after costs.

Full reports, protocol, code, all per-fold predictions, environment versions,
hashes and pinned input copies are preserved under
`C:/DATASTORE/hyperliquid/_model_research/20260926T103000Z-window-comparison`.
The `REPORT.md` there explains exact dates, per-market scores and reproduction.

## Implemented capability and retained settings

Added optional `max_train_rows` to model settings and runtime configuration.
It trims only the oldest fitting rows after boundary purging, leaving calibration
and assessment intact. It defaults to `None`; production remains **uncapped**.
Reports now distinguish window omissions from purged boundary rows. See the
[model guide](../../hyperliquid-models.md#optional-fitting-window-cap-not-enabled).

The retained model configuration uses 192 calibration rows, 288 assessment rows,
minimum 1,000 fitting rows, horizon four 15-minute bars and retraining every 900
seconds. The comparison did not select a training-window cap or a calibration
change. No Powder execution or real-account mutation occurred during this work.

The subsequent Paper reset supersedes this audit's former opening-mirror and
deployment sections. The excluded ledger must not be restored or incorporated
into forward strategy evaluation. Replacement-run evidence is maintained
separately from this historical model comparison.

## Verification

The model/configuration suites passed **141 tests**, and model runtime/artifacts,
forecast reader, Paper runtime/seed/ledger suites passed **218 tests**: **359
distinct relevant tests** at that implementation checkpoint. The offline
comparison also asserted aligned assessment rows, mature labels and partition
boundaries. These software and offline checks remain evidence independently of
the excluded Paper sample; they do not establish its trading performance.
