# September 23 weekly model diagnosis

The weekly model correctly remains `RESEARCH_NOT_PROMOTED`. Saved-estimator inference exactly reproduces its calibrated Brier **0.2605119888203955** against baseline **0.2527680960166469**, and log loss **0.714423656466066** against **0.6987913505921706**. Excesses are 0.007743892804 and 0.015632305874, exceeding the saved v2 limits by **0.002743892804** and **0.005632305874**. Calibration-information, ECE and assessment-sample checks pass; neither quality failure was relabeled.

Publication is `C:/DATASTORE/ml/nightly-gameplan-runs/20260923T054513.490247Z`; prior comparison is the verified September 22 publication `20260922T054940.677673Z`. Review times, hashes, raw and calibrated score reproduction, fitting log evidence and full numerical details are in `weekly-diagnosis.json` and `weekly-cohort-delta.json`.

## Data and selection evidence

All **2,282** prior weekly rows remain exactly unchanged across all **196** common non-key columns, with no removed rows or changed column set. Eight newly matured September 16 to September 22 targets increase the cohort to **2,290**. Their price endpoint gaps are zero except COST's four-minute closing gap; all meet the native five-minute rule. Seven returns are positive and COST is negative. CROX, PATH and TWST did not obtain another admitted weekly target; their missing observations were not invented.

The fixed rolling partitions move train 1,561 to 1,569 rows, selection 176 to 171, calibration 197 to 203 and assessment 255 to 256. Assessment retains 63 decision clusters. The new mature rows enter assessment while older boundaries shift train/selection/calibration membership; this is the existing chronological partition rule, not assessment-guided repartitioning. Source-cutoff chronology, mature labels, native XNAS prices, exact fitted symbol/route support and admitted features verify.

The predeclared **36-candidate** development search retains `hist-gradient-mlp-0.25-prior-shrinkage-w0.75`. Its development log loss **0.7003231844274078** beats the runner-up **0.7012571536971829** by 0.000933969270. The saved estimator correctly combines 75% tree with 25% MLP, then retains 75% of that signal and adds 25% of the TRAIN-plus-selection prior. The C grid remains 0.001/0.01/0.1/1 and shrinkage grid 0.25/0.5/0.75/1. All nine weekly fitting steps completed with zero fitting warnings; the separate assessment warning faithfully reports the two failed quality checks.

## Calibration change and limitation

The shifted purged calibration development split now chooses eligible **Platt**: validation log loss **0.7316521493417615** versus identity **0.7344238390938871**. Yesterday identity won. The current split has 69 fitting and 100 validation rows, with 34 intervening rows purged; the diagnostic independently reproduces the split and identity score without fitting a candidate. Final assessment is absent from this selection procedure.

Full calibration refitting on its 203 rows yields a positive slope **0.0274186273**, intercept -0.3353993125 and varying assessment probabilities **0.4119476517–0.4243667024**. This is strongly compressed toward the calibration positive rate of **41.87%**, compared with **52.34%** in assessment. It passes the unchanged nonconstant/orientation criteria but remains a material model limitation; all eleven current weekly probabilities are below 0.50. Raw pre-calibration assessment also fails both quality tolerances (Brier 0.2622609875, log loss 0.7192285136). Switching back to identity after inspecting this assessment would neither repair a demonstrated defect nor justify model selection.

No concrete data, fitting, calibration-selection or implementation defect was identified. An unchanged retry, threshold change or selection based on these assessment outcomes is not justified. No model was fitted or relabeled, no raw data or production code changed, and no provider/broker call occurred.

## Other groups and learned sizing

The 1h, 4h and 1d directional groups pass their saved v2 tolerances, producing **253 promoted forecast rows**; the **11 weekly rows** retain research status. None of the four directional groups strictly beats both training-rate baselines. Every published route has exact fitted support, but the minimum remains only two rows for TWST 1h@15:00, 18 for TWST 4h@16:00, one for TWST 1d@D+1 and six for TWST 1w@D+5. Aggregate promotion does not establish each route's accuracy.

The explicit matching enrichment run `C:/DATASTORE/ml/stock-trader-model-runs/20260923T054714.752473Z` passed source/cohort and native model validation. All four models fitted. Qualified/fitted scopes are **1h 137/143**, **4h 0/78**, **1d 0/11**, **1w 0/41**. The six unqualified hourly scopes are CROX 16:00 and TWST 04:00, 05:00, 14:00, 15:00, 16:00, each with `INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE`. Learned sizing remains separate from directional promotion and the selected manual policy. Both model-review invocations exited zero with no evidence errors; details are in `model-review.md`, `model-review.json` and `model-metric-summary.json`.
