# Locked YG quality-repair saved-model verification

Run `C:\DATASTORE\ml\nightly-gameplan-runs\20260918T080016.713533Z`; verified 2026-09-18T08:08:19.111886+00:00.

Artifact checks pass: **True**. All four directional groups and exact forecast histories pass: **True**.

This is a rerun against already-seen assessment observations after a development-selected repair, not a fresh untouched external test. The repair was locked before its new assessment results.

| Group | Selected family | Status | Failed promotion checks | Reproduced-score max error |
|---|---|---|---|---:|
| 1h | regularized-logistic-c0.1 | PROMOTED | None | 0.0 |
| 4h | hist-gradient-mlp-0.50 | PROMOTED | None | 0.0 |
| 1d | regularized-logistic-c0.001 | PROMOTED | None | 0.0 |
| 1w | hist-gradient-mlp-0.25-prior-shrinkage-w0.5 | PROMOTED | None | 0.0 |

The separate JSON verifies raw-return labels, preserved cost labels, all four C grids, information-retaining development calibration, exact fitted symbol/route support, and the weekly wrapper’s fixed candidate set, selected weight and train-only/final-fit priors. Source timing was not integrated. OG and the first YG remain immutable.

## Separate optional enrichment

Run `C:\DATASTORE\ml\stock-trader-model-runs\20260918T080230.016116Z`. All fitted scopes qualified: **False**.

| Horizon | Fitted | Fitted scopes | Qualified scopes |
|---|---|---:|---:|
| 1h | True | 143 | 0 |
| 4h | True | 78 | 0 |
| 1d | True | 11 | 0 |
| 1w | True | 41 | 0 |

Independent enrichment intentionally rebuilds net-return>0 labels; it does not relabel its profitability head as YG raw direction.

Saved-model inference and arithmetic only; no candidate refitting or tuning.

Current manual policy consumes saved instructions independently of promotion; legacy fixed/qualified policies retain their gates. This audit places no orders.
