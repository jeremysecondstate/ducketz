# September 16 weekly directional assessment

The weekly model correctly remains **RESEARCH_NOT_PROMOTED**. No concrete source, cohort, fitting, selection or promotion defect was found that justifies an immediate repair/retrain. The change from yesterday is explained by the rolling calibration sample and its development-only selector; substituting yesterday's calibrator after seeing today's assessment would not be justified. No fitting, provider call, retry, source mutation or order action was performed.

Evidence: [machine-readable diagnosis](C:/dev/ducketz/artifacts/analysis/overnight-20260916/weekly-model-diagnosis.json), [read-only audit script](C:/dev/ducketz/artifacts/analysis/overnight-20260916/weekly_model_diagnosis.py), [current model report](C:/DATASTORE/ml/nightly-gameplan-runs/20260916T060149.932707Z/model-reports.json), [prior model report](C:/DATASTORE/ml/nightly-gameplan-runs/20260915T054345.154531Z/model-reports.json). Both reports, weekly cohorts, weekly models and forecasts matched their immutable manifest hashes; receipt-to-manifest hashes matched.

| Directional group | Selected family | Current assessment status |
| --- | --- | --- |
| 1h | regularized-logistic-c1 | PROMOTED |
| 4h | hist-gradient-mlp-0.75 | PROMOTED |
| 1d | regularized-logistic-c0.001 | PROMOTED |
| 1w | hist-gradient-mlp-0.50 | RESEARCH_NOT_PROMOTED |

The weekly Brier score is **0.2680305834** against baseline **0.2551710829**, an excess **0.0128595004** versus allowed 0.005. Log loss is **0.7326357996** against **0.7036270534**, an excess **0.0290087462** versus allowed 0.01. The other three weekly checks pass: 63 assessment decision clusters, retained probability variation, and calibration error 0.1132176335 below 0.15. The rejected assessment contains 239 rows. Passing the other checks does not override the two failed proper-score gates.

## What changed

The saved weekly cohort grew from 2,243 to 2,250 rows: seven newly matured September 9–15 weekly observations were appended. Every cell of all 2,243 common rows is unchanged; no rows disappeared, no historical features or labels changed, and no conflicting native minutes were admitted. Both cohorts retain exact XNAS.ITCH identity, observed five-minute endpoint alignment, and labels equal to observed return above the recorded round-trip cost. Source sparsity remains explicit: tonight admitted 2,250 of 8,979 candidates and excluded 6,729 through the unchanged endpoint rule. CROX has no weekly assessment rows and TWST has two; these are support limitations, not acquisition-repair evidence.

The native rolling partitions recompute exactly from the saved cohort. Training is unchanged at 1,554 rows. Selection changes 166→173, calibration 204→198 and assessment 233→239. All partitions have both classes. The 50/50 tree/MLP blend wins the fixed candidate development comparison on both nights; tonight its log loss is 0.6926128847, better than the 25% blend at 0.6949999474 and logistic at 0.7582938634. Assessment is not used for this selection.

Calibration's purged fitting subset loses six positive rows from the June 10 action session, reducing fitting support from 69 rows (26 positive/43 negative) to 63 (20 positive/43 negative). The later validation subset is exactly the same 97 row identities and data. Its native comparison changes from yesterday's Platt win (0.7186668919 versus identity 0.7431445183) to today's identity win (**0.7411467752 versus Platt 0.7522803937**). The selected calibrator is therefore correctly identity. Raw held-out log loss actually improves from 0.7450797501 yesterday to 0.7326357996 today; yesterday's promotion depended on the Platt calibration that its then-current development evidence selected. This is concrete calibration-selection sensitivity, not a proven implementation fault.

The outer partition purge uses next target-window start. Seven training and eight selection labels finish after the next partition's earliest feature decision timestamp, but before its target start. No calibration labels overlap the current assessment's earliest feature decision. Feature timestamps precede nightly fitting, so this observation alone does not establish leakage; the recorded chronology was not changed. The inner calibrator purge separately uses validation decision timestamp. Exact affected rows and partition ranges are retained in the JSON.

## Why no older champion was retained

[Champion retention](C:/dev/ducketz/ml/gameplan_champions.py:19) explicitly uses `latest-compatible-promoted-same-action-date-v1`; its filter at line 36 requires the same action date. Yesterday's promoted model belongs to September 15 and cannot replace September 16. Date mismatch rejection has a [dedicated regression test](C:/dev/ducketz/tests/test_gameplan_champions.py:113). Expanding this policy after today's failure would be a policy change, not a correction to the implemented fallback.

## Execution implications under the current contract

All eleven weekly rows have `execution_eligible=true`, the flag identifying a scheduled entry window. Their model status remains research; five are bullish (AAPL, GOOG, MU, NVDA, SNDK), six bearish. The promoted 1h/4h/1d groups provide the other 198 entry windows, all bearish, and there are 55 non-entry context rows.

The selected **manual Gameplan policy does consume the saved weekly instructions**. The September 14 contract at the top of [INDEPENDENT_STOCK_HORIZONS.md](C:/dev/ducketz/docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md:3) expressly places research validation in publication. [Runtime dispatch](C:/dev/ducketz/ml/stock_trader/independent_runtime.py:190) selects `execution_ready_plan`; [the signal reader](C:/dev/ducketz/ml/stock_trader/independent_signals.py:56) delegates to [gameplan_execution.py](C:/dev/ducketz/ml/stock_trader/gameplan_execution.py:22), which uses saved action date, symbol, execution flag, probabilities and holding windows without filtering `model_status`. A pure read-only opening-clock inspection returned 44 instructions, including all eleven weekly rows. It did not invoke the runtime, broker, ledger or an order. Actual controls, cash, shares, quotes, ownership, active allocations and per-forecast records still determine whether an order can occur. Do not describe the research label as an automatic manual-policy execution block.

The legacy fixed policy follows the validated reader and filters research rows at [independent_signals.py:130](C:/dev/ducketz/ml/stock_trader/independent_signals.py:130). The informational projection also marks them `MODEL_NOT_PROMOTED` at [gameplan_cash_ledger.py:166](C:/dev/ducketz/ml/gameplan_cash_ledger.py:166). This difference is disclosed here without introducing a new execution gate or changing policy.

All four enrichment groups are fitted, separately from direction. Current qualified scope counts are 137/143 hourly, 0/78 four-hour, 0/11 daily and 0/41 weekly. Manual and fixed horizon quantities do not consume learned enrichment qualification. [Enrichment evidence](C:/DATASTORE/ml/stock-trader-model-runs/20260916T060336.201575Z/training-report.json).

Preserve the research result and immutable publication. There is no evidence-backed immediate corrective candidate. A future calibration-stability study could be preregistered on development data and assessed on untouched later outcomes; no such study or tuning was run here.
