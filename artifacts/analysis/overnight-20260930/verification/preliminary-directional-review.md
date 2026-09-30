# September 30 preliminary directional review

Pinned publication: `C:\DATASTORE\ml\nightly-gameplan-runs\20260930T061501.586401Z`.

Native status at review: RUNNING. This checks saved reports and frozen rows; final source/cohort/inference verification is pending.

| Horizon | Saved status | Brier / baseline | Log loss / baseline | Failed quality checks | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250275974 / 0.249999738 | 0.693711590 / 0.693146656 | None | TWST 1h@16:00: 1 |
| 4h | PROMOTED | 0.249928310 / 0.250067329 | 0.693004755 / 0.693281847 | None | TWST 4h@16:00: 24 |
| 1d | PROMOTED | 0.251006402 / 0.250798327 | 0.695167195 / 0.694744139 | None | TWST 1d@D+1: 2 |
| 1w | PROMOTED | 0.252118575 / 0.251934172 | 0.697415401 / 0.697044808 | None | TWST 1w@D+5: 8 |

| Horizon | Current cohort rows | Date range | Boundary admitted / excluded | Further quality exclusions | Conflicting minutes |
| --- | ---: | --- | --- | ---: | --- |
| 1h | 172024 | 2018-05-31 to 2026-09-29 | 172241 / 67341 | 217 | 0 |
| 4h | 43000 | 2018-05-31 to 2026-09-29 | 43066 / 25375 | 66 | 0 |
| 1d | 29757 | 2019-09-18 to 2026-09-29 | 29862 / 55593 | 105 | 0 |
| 1w | 5861 | 2019-09-18 to 2026-09-23 | 5950 / 11119 | 89 | 0 |

Saved policy permits Brier baseline +0.005 and log-loss baseline +0.01; qualification within those tolerances does not establish baseline outperformance.

Horizons strictly beating both training baselines: 4h. Other groups keep their actual saved qualification status and metric margins.

Per-symbol/route fitted and assessment counts are retained exactly in the JSON, including thin TWST histories. These are observed saved counts, not a new qualification rule.

The saved group-level promotion gate and positive exact fitted support are verified separately. Very small exact-route or symbol assessment counts remain limitations; group promotion does not establish precise per-symbol/per-route accuracy.

No concrete data, fitting or selection defect demonstrated by this bounded report review. Quality failures alone do not justify changing gates or repeatedly selecting on assessment outcomes. Full immutable-source and estimator verification remains pending.

No archive reads, estimator inference, training, model selection, production writes, account/provider calls or orders occurred. Exact reports, support counts, calibration variation and numeric margins are retained in the JSON.
