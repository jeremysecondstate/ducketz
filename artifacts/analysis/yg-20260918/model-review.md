# YG September 18 saved-model verification

Verified 2026-09-18T07:31:50.033423+00:00; publication `C:\DATASTORE\ml\nightly-gameplan-runs\20260918T072555.813034Z`.

Artifact checks: **PASS**. Original OG publication and every manifest-bound output remain unchanged: **True**.

YG predicts strictly positive raw observed price return. Cost-adjusted-positive labels remain separate evidence. The authorized C grid is 0.001, 0.01, 0.1, 1 for 1h/4h/1d and 1 for 1w; selection uses development log loss. OG is frozen comparison evidence only and cannot serve as a retained YG champion.

| Horizon | Selected family | Directional status | Brier / baseline | Log loss / baseline | Score reproduction max error |
|---|---|---|---|---|---|
| 1h | regularized-logistic-c0.1 | RESEARCH_NOT_PROMOTED | 0.249980909006 / 0.249985067061 | 0.693108998087 / 0.693117315296 | 0.0 |
| 4h | hist-gradient-mlp-0.50 | PROMOTED | 0.251086859562 / 0.249995920585 | 0.695324930840 / 0.693139021721 | 0.0 |
| 1d | regularized-logistic-c0.001 | PROMOTED | 0.252020432748 / 0.250784560197 | 0.697256534773 / 0.694716716872 | 0.0 |
| 1w | hist-gradient-mlp-0.50 | RESEARCH_NOT_PROMOTED | 0.268145925815 / 0.255816301107 | 0.731704738871 / 0.704938537066 | 0.0 |

1h failed promotion checks: calibration_retains_directional_information.

1h flat-calibration diagnosis: The fixed development policy permits the constrained Platt base-rate boundary as a calibration candidate; it wins saved development log loss, then fails the separate directional-information promotion gate. Analytical derivatives and saved-model predictions support the flat result. This is a valid weak-model outcome, not a detected fit/source/label defect; replacing it after reading assessment would change the established selection policy.
Its development fit contains 1886 rows at positive rate 0.5; the constant development prediction 0.5 reproduces saved Platt log loss 0.693147180560, below identity 0.695178480082. Full calibration reproduces its base rate 0.495564005070. Nonnegative slope-boundary derivatives: development 0.000410252136025, full calibration 0.00069578957723. No candidate was fitted.

1w failed promotion checks: brier_within_baseline_tolerance, log_loss_within_baseline_tolerance.

Qualification uses each publication’s recorded policy and is distinct from beating the baseline. The JSON retains exact sample counts, calibration ranges, causal/source checks, and raw-versus-cost label disagreements.

decision_timestamp is a prior-session feature clock. Checks retain native target-window purging and also require preceding labels before the next post-close source cutoff; it is not the live dispatch clock.

Assessment and calibrated outputs are recomputed from saved models only; discarded development candidates are not refitted. Their recorded selection metrics and source code determine selection verification.

## Separate independent enrichment

Run `C:\DATASTORE\ml\stock-trader-model-runs\20260918T072819.180746Z`; Independent enrichment intentionally rebuilds net-return>0 labels; it does not relabel its profitability head as YG raw direction.

| Horizon | Fitted | Fitted scopes | Qualified scopes |
|---|---|---:|---:|
| 1h | True | 143 | 0 |
| 4h | True | 78 | 0 |
| 1d | True | 11 | 0 |
| 1w | True | 41 | 0 |

Current manual Gameplan execution consumes saved instructions independently of promotion. Legacy fixed/qualified policies retain their gates. No execution is performed or authorized by this audit.

Audit wrote only analysis artifacts in its own YG directory; the prior OG audit was preserved.
