# September 29 preliminary directional review

Pinned publication: `C:\DATASTORE\ml\nightly-gameplan-runs\20260929T061339.593647Z`.

Native status at review: RUNNING. This checks saved reports and frozen rows; final source/cohort/inference verification is pending.

| Horizon | Saved status | Brier / baseline | Log loss / baseline | Failed quality checks | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250446834 / 0.250000115 | 0.694051777 / 0.693147411 | None | TWST 1h@16:00: 1 |
| 4h | PROMOTED | 0.249875254 / 0.250059450 | 0.692899365 / 0.693266090 | None | TWST 4h@16:00: 24 |
| 1d | PROMOTED | 0.250666791 / 0.250807637 | 0.694485441 / 0.694762794 | None | TWST 1d@D+1: 2 |
| 1w | PROMOTED | 0.252886581 / 0.251353711 | 0.699019540 / 0.695881303 | None | TWST 1w@D+5: 8 |

| Horizon | Current cohort rows | Date range | Boundary admitted / excluded | Further quality exclusions | Conflicting minutes |
| --- | ---: | --- | --- | ---: | --- |
| 1h | 171897 | 2018-05-31 to 2026-09-28 | 172114 / 67314 | 217 | 0 |
| 4h | 42965 | 2018-05-31 to 2026-09-28 | 43031 / 25366 | 66 | 0 |
| 1d | 29712 | 2019-09-18 to 2026-09-28 | 29817 / 55583 | 105 | 0 |
| 1w | 5853 | 2019-09-18 to 2026-09-22 | 5942 / 11116 | 89 | 0 |

Saved policy permits Brier baseline +0.005 and log-loss baseline +0.01; qualification within those tolerances does not establish baseline outperformance.

Horizons strictly beating both training baselines: 4h, 1d. Other groups keep their actual saved qualification status and metric margins.

Per-symbol/route fitted and assessment counts are retained exactly in the JSON, including thin TWST histories. These are observed saved counts, not a new qualification rule.

The saved group-level promotion gate and positive exact fitted support are verified separately. Very small exact-route or symbol assessment counts remain limitations; group promotion does not establish precise per-symbol/per-route accuracy.

No concrete data, fitting or selection defect demonstrated by this bounded report review. Quality failures alone do not justify changing gates or repeatedly selecting on assessment outcomes. Full immutable-source and estimator verification remains pending.

No archive reads, estimator inference, training, model selection, production writes, account/provider calls or orders occurred. Exact reports, support counts, calibration variation and numeric margins are retained in the JSON.
