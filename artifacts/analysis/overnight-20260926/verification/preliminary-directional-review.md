# September 28 preliminary directional review

Pinned publication: `C:\DATASTORE\ml\nightly-gameplan-runs\20260926T061604.318956Z`.

Native status at review: RUNNING. This checks saved reports and frozen rows; final source/cohort/inference verification is pending.

| Horizon | Saved status | Brier / baseline | Log loss / baseline | Failed quality checks | Smallest exact fitted route |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 0.250356280 / 0.250000080 | 0.693873413 / 0.693147340 | None | TWST 1h@16:00: 1 |
| 4h | PROMOTED | 0.249714674 / 0.250034616 | 0.692576762 / 0.693216420 | None | TWST 4h@16:00: 24 |
| 1d | PROMOTED | 0.250458645 / 0.250767641 | 0.694069015 / 0.694682815 | None | TWST 1d@D+1: 2 |
| 1w | PROMOTED | 0.253310454 / 0.251549081 | 0.699878679 / 0.696275053 | None | TWST 1w@D+5: 8 |

| Horizon | Current cohort rows | Date range | Boundary admitted / excluded | Further quality exclusions | Conflicting minutes |
| --- | ---: | --- | --- | ---: | --- |
| 1h | 171767 | 2018-05-31 to 2026-09-25 | 171984 / 67290 | 217 | 0 |
| 4h | 42928 | 2018-05-31 to 2026-09-25 | 42994 / 25359 | 66 | 0 |
| 1d | 29677 | 2019-09-18 to 2026-09-25 | 29782 / 55563 | 105 | 0 |
| 1w | 5846 | 2019-09-18 to 2026-09-21 | 5935 / 11112 | 89 | 0 |

Saved policy permits Brier baseline +0.005 and log-loss baseline +0.01; qualification within those tolerances does not establish baseline outperformance.

Horizons strictly beating both training baselines: 4h, 1d. Other groups keep their actual saved qualification status and metric margins.

Per-symbol/route fitted and assessment counts are retained exactly in the JSON, including thin TWST histories. These are observed saved counts, not a new qualification rule.

The saved group-level promotion gate and positive exact fitted support are verified separately. Very small exact-route or symbol assessment counts remain limitations; group promotion does not establish precise per-symbol/per-route accuracy.

No concrete data, fitting or selection defect demonstrated by this bounded report review. Quality failures alone do not justify changing gates or repeatedly selecting on assessment outcomes. Full immutable-source and estimator verification remains pending.

No archive reads, estimator inference, training, model selection, production writes, account/provider calls or orders occurred. Exact reports, support counts, calibration variation and numeric margins are retained in the JSON.
