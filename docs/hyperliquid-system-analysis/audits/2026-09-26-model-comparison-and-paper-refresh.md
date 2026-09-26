# Model comparison and fresh Paper mirror

User-authorized archive, bounded training comparison and fresh simulated Paper
mirror on **2026-09-26**. The comparison rejected both proposed training changes;
the new run retains the current model recipe and qualified-only policy. This is
a fresh observation period, not a claim that resetting improves performance.

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
`C:/DATASTORE/hyperliquid/_experiments/20260926T103559Z-qualified-refresh/model-comparison`.
The `REPORT.md` there explains exact dates, per-market scores and reproduction.

## Implemented capability and retained settings

Added optional `max_train_rows` to model settings and runtime configuration.
It trims only the oldest fitting rows after boundary purging, leaving calibration
and assessment intact. It defaults to `None`; production remains **uncapped**.
Reports now distinguish window omissions from purged boundary rows. See the
[model guide](../../hyperliquid-models.md#optional-fitting-window-cap-not-enabled).

The new run retains 192 calibration rows, 288 assessment rows, minimum 1,000
fitting rows, horizon four 15-minute bars and retraining every 900 seconds.
Research/unavailable forecasts remain excluded from signal allocation; hold,
stop, cooldown and exposure-limit behavior are unchanged. Policy ID remains
`60b7f962240a0857`. No Powder execution or real-account mutation occurred.

## New opening mirror

Fresh public account reads established the seed at
**2026-09-26T10:35:59.867633343Z (03:35:59 PT)**:

| Account | Opening marked equity |
| --- | ---: |
| Alex | $5,948.040062 |
| Jeremy | $6,101.803833 |
| Clear Pond | $30,041.621143 |
| Pool | $42,091.465039 |

All nine inherited positions were captured, including the two passive spot
dust balances. Account reads are sequential rather than atomic; marking the
inherited perps at the seed quotes differs from their source account summaries
by about +$0.03 for Alex and -$0.02 for Jeremy. This is a current inventory/cash
mirror, not replication of exchange liquidation/margin behavior or open orders.
Paper evolves independently after seeding and records subsequent simulated
adjustments and fees against this opening baseline.

New model artifacts were fitted before starting Paper; old pointers and model
histories remain in the archive. Initial ETH/HYPE/ZEC forecasts qualified;
BTC was Research. These are timestamped observations, not guaranteed future
statuses. Process and ledger verification is recorded in
`C:/DATASTORE/hyperliquid/_operations/training-comparison-reset-verification.json`.

## Verification

Verified continuous data PID 57004, new model PID 56520 and Paper PID 70028,
including exact module/config commands and parent creation order. Model and
Paper venv launchers and hidden command parents are outside Windows jobs and
descend from WMI rather than Codex. Their actual interpreter children belong
to Windows jobs; that alone does not imply Codex ownership. No competing
runtime or Powder owner was present.

Committed observations advanced from 10:36:32.329354 to 10:38:07.823759 UTC,
cycles 6 to 9, preserving the new seed and baseline. All 17 projected sources
were fresh, without runtime/slot errors or warnings. The first seven fills
comprised one BTC exposure-cap reduction, two Alex stop losses and four
Qualified signal rebalances. Fees were $28.196468; P/L at 10:38:07 was
-$39.980102. These initial costs remain visible rather than being reset away.
The Research BTC forecast did not cause a signal trade; independent exposure
limits caused its reduction.

The model/configuration suites passed **141 tests**, and model runtime/artifacts,
forecast reader, Paper runtime/seed/ledger suites passed **218 tests**: **359
distinct relevant tests**. Existing configuration remains unchanged. The offline
comparison also asserted aligned assessment rows, mature labels and partition
boundaries. Operations Watch uses the new seed under contract v3; the old seed
is historical and must never be restored as a recovery target.
