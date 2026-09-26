# September 25 sizing assessment review

All four horizons fitted, no qualified scopes. Saved report/code review demonstrates assessment quality failures, not a concrete fitting, convergence or development-selection defect. These failures do not justify assessment tuning or unchanged retries.

Reviewed 2026-09-25T06:26:42.922152+00:00. Source-bound run `C:\DATASTORE\ml\stock-trader-model-runs\20260925T061654.534550Z`; pinned publication `ml/nightly-gameplan-runs/20260925T060654.631426Z`.

| Horizon | Fitted / diagnostic scopes | Admitted sizing rows | Missing optional inputs excluded | Failed unchanged quality checks |
|---|---:|---:|---:|---|
| 1h | 143 / 143 | 92,922 | 72,749 | return_mse_below_baseline |
| 4h | 78 / 79 | 25,048 | 17,846 | return_mse_below_baseline |
| 1d | 11 / 11 | 3,784 | 2,141 | brier_below_baseline, log_loss_below_baseline, return_mse_below_baseline |
| 1w | 39 / 40 | 3,758 | 2,081 | brier_below_baseline, log_loss_below_baseline, return_mse_below_baseline, adverse_mse_at_most_baseline |

The sizing gates require Brier and log loss strictly below baseline, ECE <= 0.15, return MSE strictly below baseline, downside MSE <= baseline, varying probabilities and at least 10 assessment decision clusters. Every group has 63 assessment clusters. These are separate from directional promotion tolerances.

| Horizon | Brier / baseline | Log loss / baseline | Return MSE / baseline | Downside MSE / baseline |
|---|---:|---:|---:|---:|
| 1h | 0.232080411 / 0.236658827 | 0.65618209 / 0.666222514 | 0.000115749389 / 0.000115586287 | 4.46285361e-05 / 4.82877296e-05 |
| 4h | 0.24747256 / 0.248304617 | 0.688099028 / 0.689752751 | 0.000564684987 / 0.000561142509 | 0.000193399227 / 0.000217653423 |
| 1d | 0.259528499 / 0.249073692 | 0.716056232 / 0.691294162 | 0.00175755886 / 0.0016510935 | 0.000471239621 / 0.00050146957 |
| 1w | 0.279832252 / 0.254456445 | 0.772197191 / 0.702133945 | 0.010447394 / 0.0081959816 | 0.00290451027 / 0.00287096975 |

All reported iterative probability/downside optimizers converged with finite objectives. Expected-return/allocation heads use closed-form ridge fits and have no iterative convergence flag. Fixed 1/5/20 penalties are development minima for their respective head objectives. All four choose identity calibration by later purged development log loss; assessment is explicitly excluded from selection.

Archive optional-input admission validates target/source/price evidence first, then excludes only archive rows missing all eight operational market observations. Partial, malformed or infinite market inputs still fail closed. This does not exclude the corresponding directional training examples or alter price/quality gates. Counts and identity hashes match report/model; exact row reconstruction remains the full audit’s responsibility.

Scope coverage remains separate: 260 diagnostic scopes fail horizon quality, 11 also lack sufficient pooled symbol/route evidence, and 2 are unseen exact-duration scopes. The policy needs at least one exact fitted target cluster and 20 pooled symbol/route clusters plus horizon quality. In particular TWST daily sizing has only one fitted cluster and weekly only six pooled; no qualification was invented.

Verified receipt/manifest/report/model hashes, canonical model/source fingerprints, all six source-file byte hashes (publication receipt, manifest and four cohorts), native successful stage/log binding, report/model selection and admission equality, strict gate arithmetic, chronological partition separation and all scope status/reason arithmetic. No inference, fit, account/provider call or production mutation.

Full source reconstruction, admission-row recomputation and independent saved-model assessment inference remain delegated to the full completion audit. Zero orders are recorded. These learned sizing statuses do not change the selected Gameplan policy.
