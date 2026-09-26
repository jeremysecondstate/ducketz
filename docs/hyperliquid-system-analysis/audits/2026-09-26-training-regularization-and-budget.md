# Training alternatives rejected before the fresh Paper experiment

Two bounded offline experiments on September 26, 2026 tested three training
alternatives. None improved the predeclared aggregate probability losses, so
the production model recipe and `configs/hyperliquid-models.json` remain
unchanged. This records the model-selection decision; the subsequent Paper
policy changes and fresh mirror are documented separately.

| Recipe | Selection mean Brier | Selection mean log loss | Qualified cells |
| --- | ---: | ---: | ---: |
| Current | 0.249828293621 | 0.692814886161 | 6/12 |
| Stronger regularization across all four estimators | 0.251599797100 | 0.696577925346 | 5/12 |
| Regularized logistic/tree ensemble, omit MLP | 0.251589408784 | 0.696689054898 | 5/12 |
| Current estimators, only raise MLP max_iter 100 to 300 | 0.250059843427 | 0.693286711670 | 6/12 |

Lower losses are better. Candidates were fixed before fitting and each had to
improve both mean losses without a market mean regression above 5%. All failed
selection, so none was substituted after observing later results. The final
single-parameter test was motivated by persistent MLP convergence warnings;
raising the budget left warnings in 11/12 fits and did not improve the losses.

Four markets shared immutable input copies ending at 2026-09-26 15:30 UTC and
selection cutoffs 16, 12 and 8 days earlier. Each assessment used the same 288
decision rows, with future labels masked beyond the cutoff and strict
horizon-maturity purging between fitting, calibration and assessment. Across
folds, assessment decisions and their final outcome times do not overlap.

Later baseline evaluation at cutoffs 4 and 0 days earlier averaged Brier
0.252052890490 and log loss 0.697369884054, qualifying in 1/8 market/fold cells.
Those losses lagged the prior and neutral baselines. Since rejected candidates
were not fitted there, this does not establish superiority against them.

The first trial performed 44 ensemble fits in 41.5184 seconds of measured
training/evaluation time. The second copied and hash-verified the 20 baseline
reports from the first trial, then made 12 new candidate fits in 23.1575 seconds.
These are 56 new fits total, not 76. Independent score/hash/alignment checks
passed. No source/configuration, live `_models`, Paper, account or execution
state was changed by the research harnesses.

The histories overlap earlier model research; the second trial also reused all
baseline folds observed in the first. They are small retrospective probability
comparisons, **not globally untouched tests** or execution backtests. One-hour
outcomes overlap within each fold, markets are related, and costs/funding are
outside these scores. The user-excluded Paper sample was not read or used.

Full protocols, pinned inputs, source snapshots, predictions, reports and
verification evidence are preserved at:

- `C:/DATASTORE/hyperliquid/_model_research/20260926T154000Z-regularization-comparison/REPORT.md`
- `C:/DATASTORE/hyperliquid/_model_research/20260926T155000Z-mlp-budget-comparison/REPORT.md`

Retained production training settings: four 15-minute bars per forecast,
uncapped fitting, minimum 1,000 fit rows, 192 calibration rows, 288 assessment
rows, seed 42, two numerical threads, and 900-second retraining. Fresh training
after the operational reset uses this retained recipe; changing the execution
policy does not turn a model's Qualified label into evidence of profitability.
