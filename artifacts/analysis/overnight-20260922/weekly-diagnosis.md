# Weekly model diagnosis

Reviewed 2026-09-22T05:57:02.107512+00:00. Publication: `C:\DATASTORE\ml\nightly-gameplan-runs\20260922T054940.677673Z`; prior comparison: `C:\DATASTORE\ml\nightly-gameplan-runs\20260919T061628.240412Z`.

The weekly model correctly remains `RESEARCH_NOT_PROMOTED`. Its assessment Brier score is 0.261371535 versus the training-rate baseline 0.253543516 (excess 0.007828020; allowed 0.005). Log loss is 0.717420132 versus 0.700350235 (excess 0.017069898; allowed 0.01). The two limits are missed by 0.002828020 and 0.007069898, respectively. Calibration information, ECE and sample-count checks pass; the preserved v2 arithmetic is consistent.

All 2,274 earlier weekly cohort rows remain identical in every common column, with no removals. Eight newly matured September 15 to September 22 targets increase the cohort to 2,282. All eight have genuine observed endpoint gaps at most five minutes; IONQ is exactly at that boundary. No native observations, labels or old feature values changed. Seven added returns are positive and COST is negative. The other three symbols did not acquire an admitted target; missing boundaries remain excluded.

The train partition is unchanged at 1,561 rows and 629 decision clusters. Fixed rolling partitions move selection from 173 to 176 rows (six enter and three leave), calibration from 203 to 197, and assessment from 248 to 255 (eight enter and one leaves). Assessment still has 63 clusters. Exact fitted symbol/route support and source-cutoff/label chronology pass.

The predeclared 36-candidate development search now selects `hist-gradient-mlp-0.25-prior-shrinkage-w0.75`: development log loss 0.6989911433 versus 0.6990062525 for the prior winner's family, a margin of 0.0000151092. The native formula uses 75% tree plus 25% MLP, then retains 75% of that probability and shrinks 25% toward the fitted prior. Both selection and final fitting complete without fitting warnings. The fixed C grid remains 0.001/0.01/0.1/1 and shrinkage grid remains 0.25/0.5/0.75/1.

Identity calibration wins on the separate purged calibration development split (log loss 0.745036999 versus Platt 0.765132582). The saved procedure reads final assessment only after these choices and the final fit. Its varying assessment probabilities range from 0.327149184 to 0.785183842. The model choice is sensitive to a narrow development margin, which is a limitation; it is not evidence of an implementation error.

No concrete data, fitting, selection or gate defect was identified. An unchanged retry or switching to the runner-up because of this assessment is not justified. Weekly execution remains research gated. No models were fitted, relabeled or selected again, and no code or trading controls changed. Exact evidence is in `weekly-diagnosis.json`, `weekly-cohort-delta.json` and `model-review.json`; full saved-estimator score reproduction belongs to root's final verifier.
