# September 17 directional model review

All four directional groups passed their recorded v2 qualification gates. All 264 frozen rows are PROMOTED, including the 209 entry windows. No concrete source/cohort/fitting/selection defect or justified repair/retrain was found in this bounded offline review. No training, provider/broker call, production mutation, or execution occurred.

Evidence: [machine-readable audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/model-review.json), [current model reports](C:/DATASTORE/ml/nightly-gameplan-runs/20260917T054622.686474Z/model-reports.json), [prior weekly diagnosis](C:/dev/ducketz/artifacts/analysis/overnight-20260916/weekly-model-diagnosis.md). Checked at 2026-09-17T05:51:07.373619+00:00.

| Group | Family | Assessment rows / clusters | Brier / baseline | Log loss / baseline | Calibration error | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1h | regularized-logistic-c1 | 4338 / 63 | 0.2293459011 / 0.2361140256 | 0.6506343908 / 0.6651474151 | 0.0245647781 | PROMOTED |
| 4h | hist-gradient-mlp-0.75 | 1193 / 63 | 0.2482260592 / 0.2480272617 | 0.6896057214 / 0.6891972500 | 0.0189812754 | PROMOTED |
| 1d | regularized-logistic-c0.001 | 1185 / 63 | 0.2490955365 / 0.2495699123 | 0.6914325852 / 0.6922869850 | 0.0462573845 | PROMOTED |
| 1w | hist-gradient-mlp-0.50 | 241 / 63 | 0.2566433066 / 0.2565355863 | 0.7066736982 / 0.7063806384 | 0.0719758562 | PROMOTED |

The policy permits Brier up to baseline +0.005 and log loss up to baseline +0.01; it also requires at least ten decision clusters, calibration error no more than 0.15, and retained probability variation. Four-hour and weekly scores remain slightly worse than their baselines, so their qualification is not baseline outperformance. All four saved estimators reproduce reported held-out scores, baseline scores and calibrated probability ranges without refitting. Saved receipt/manifest/output hashes, chronological partition counts, class labels, exact XNAS source identity and five-minute observed endpoint limits matched. Development winners equal the minimum saved development log loss; assessment was not used to select a model or calibrator.

## Weekly change from the prior plan

The same 50/50 tree/MLP family remains selected. The weekly cohort changed from 2250 to 2259 rows: 9 added, 0 removed, and 0 changed cells across common rows. The calibration development split now chooses Platt: log loss 0.7304438226 versus identity 0.7315446219; the prior plan chose identity (Platt 0.7522803937, identity 0.7411467752). Its purged fit population changes 63 to 70 rows; sample-change counts and population ranges are in the JSON. This is fresh development-based selection, not substitution based on the held-out result.

Weekly assessment Brier is 0.2566433066, versus baseline 0.2565355863; log loss is 0.7066736982, versus 0.7063806384. Excesses are 0.0001077203 and 0.0002930599. The prior excesses 0.0128595004 and 0.0290087462 exceeded the allowed limits. Tonight's calibration retains variation, with held-out probabilities [0.37358268088343066, 0.47180685515762777]; no flat fallback was promoted.

## Fitted support and execution meaning

Every published symbol/route has actual fitted target support. Per-symbol/route admitted, fitted and assessment counts were recomputed from the immutable cohorts and their native partitions. A pooled group pass does not establish each individual symbol had sufficient held-out outcomes; sparse or absent symbol assessment remains disclosed in the JSON.

- 1d symbols with zero assessment rows: CROX.
- 1w symbols with zero assessment rows: CROX.

The manual Gameplan policy consumes saved instructions independently of research model status. All 209 of tonight's entry instructions are bearish. Bullish instructions accumulate within their horizon and bearish instructions sell eligible same-horizon/unallocated shares; target ends do not trigger automatic sales. Actual controls, available cash, eligible holdings, quotes, ownership, allocation limits and per-forecast records still determine execution. No trade or fill is implied by this review. Independent enrichment qualification remains separate from these directional results.
