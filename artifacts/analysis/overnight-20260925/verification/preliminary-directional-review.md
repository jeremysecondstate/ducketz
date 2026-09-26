# September 25 preliminary directional review

Pinned publication: `C:\DATASTORE\ml\nightly-gameplan-runs\20260925T060654.631426Z`.

Native status at review: RUNNING. This checks saved reports and frozen rows; final source/cohort/inference verification is pending.

| Horizon | Saved status | Brier / baseline | Log loss / baseline | Failed quality checks | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250755552 / 0.250000010 | 0.694678409 / 0.693147200 | None | TWST 1h@16:00: 1 |
| 4h | PROMOTED | 0.249674883 / 0.250062913 | 0.692497652 / 0.693273017 | None | TWST 4h@16:00: 24 |
| 1d | PROMOTED | 0.250584667 / 0.250757893 | 0.694320630 / 0.694663326 | None | TWST 1d@D+1: 2 |
| 1w | PROMOTED | 0.253966530 / 0.252409482 | 0.701197402 / 0.698003635 | None | TWST 1w@D+5: 8 |

| Horizon | Current cohort rows | Date range | Boundary admitted / excluded | Further quality exclusions | Conflicting minutes |
| --- | ---: | --- | --- | ---: | --- |
| 1h | 171641 | 2018-05-31 to 2026-09-24 | 171858 / 67262 | 217 | 0 |
| 4h | 42894 | 2018-05-31 to 2026-09-24 | 42960 / 25349 | 66 | 0 |
| 1d | 29642 | 2019-09-18 to 2026-09-24 | 29747 / 55543 | 105 | 0 |
| 1w | 5839 | 2019-09-18 to 2026-09-18 | 5928 / 11108 | 89 | 0 |

Saved policy permits Brier baseline +0.005 and log-loss baseline +0.01; qualification within those tolerances does not establish baseline outperformance.

Horizons strictly beating both training baselines: 4h, 1d. The other promoted horizons pass the saved tolerances without beating both baselines.

TWST fitted support: hourly 10,895 total, with only 1 row on route 1h@16:00; four-hour 1,982 total, minimum 24 on 4h@16:00; daily 10 total, 2 per route, with 10 assessment rows total; weekly 8 fitted and 2 assessment rows. These are observed saved counts, not a new qualification rule.

The saved group-level promotion gate and positive exact fitted support are verified separately. Very small exact-route or symbol assessment counts remain limitations; group promotion does not establish precise per-symbol/per-route accuracy.

No concrete data, fitting or selection defect demonstrated by this bounded report review. Quality failures alone do not justify changing gates or repeatedly selecting on assessment outcomes. Full immutable-source and estimator verification remains pending.

No archive reads, estimator inference, training, model selection, production writes, account/provider calls or orders occurred. Exact reports, support counts, calibration variation and numeric margins are retained in the JSON.
