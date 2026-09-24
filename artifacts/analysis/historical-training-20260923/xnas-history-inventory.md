# All-symbol XNAS.ITCH historical inventory

Audited 2026-09-23T22:39:23.741997+00:00. **VERIFIED_INVENTORY_WITH_EXPLICIT_SCOPE_LIMITS**.

Verified **195/195 normalized hashes** and **195/195 raw hashes**, with **0 raw files deferred**. Manifest/receipt bindings, native request headers, Parquet row counts and observed ranges were checked. Full native record normalization replay was not performed.

| Schema | Symbols | Windows | Rows including overlapping windows | Raw bytes | Normalized bytes |
|---|---:|---:|---:|---:|---:|
| ohlcv-1d | 11 | 11 | 17,587 | 645,566 | 755,559 |
| ohlcv-1h | 11 | 11 | 203,953 | 5,521,487 | 6,476,352 |
| ohlcv-1m | 11 | 162 | 5,449,294 | 97,164,407 | 112,142,647 |
| ohlcv-1s | 11 | 11 | 16,443,370 | 189,987,625 | 267,701,476 |

Only the existing eleven production symbols were found. No symbols were selected or activated. Requested history ranges and duplicate-window row totals must not be mistaken for distinct training examples.

## Second-bar endpoint evidence

| Symbol | First observed second | Last observed second | Observed minute buckets | Absent in native minute archive | Exact OHLCV matches / overlap |
|---|---|---|---:|---:|---:|
| AAPL | 2026-08-05 08:00:00+00:00 | 2026-08-14 23:59:45+00:00 | 6,971 | 0 | 6,971 / 6,971 |
| AMZN | 2026-08-05 08:00:00+00:00 | 2026-08-14 23:59:52+00:00 | 6,822 | 0 | 6,822 / 6,822 |
| COST | 2026-08-05 08:01:49+00:00 | 2026-08-14 23:51:39+00:00 | 3,755 | 0 | 3,755 / 3,755 |
| CROX | 2018-05-01 13:30:00+00:00 | 2026-09-11 21:36:51+00:00 | 802,632 | 0 | 802,632 / 802,632 |
| GOOG | 2026-08-05 08:00:00+00:00 | 2026-08-14 23:58:53+00:00 | 6,210 | 0 | 6,210 / 6,210 |
| IONQ | 2021-10-01 08:03:58+00:00 | 2026-09-11 23:59:40+00:00 | 770,842 | 0 | 770,842 / 770,842 |
| MU | 2026-08-05 08:00:00+00:00 | 2026-08-14 23:59:54+00:00 | 7,652 | 0 | 7,652 / 7,652 |
| NVDA | 2026-08-05 08:00:00+00:00 | 2026-08-14 23:59:11+00:00 | 7,242 | 0 | 7,242 / 7,242 |
| PATH | 2021-04-21 16:26:00+00:00 | 2026-09-11 23:57:50+00:00 | 598,660 | 0 | 598,660 / 598,660 |
| SNDK | 2026-08-05 08:00:04+00:00 | 2026-08-14 23:59:56+00:00 | 7,559 | 0 | 7,559 / 7,559 |
| TWST | 2018-10-31 15:48:59+00:00 | 2026-09-11 23:11:43+00:00 | 623,989 | 0 | 623,989 / 623,989 |

Minute buckets above are audit comparisons, not new price artifacts. The first observed second supplies its genuine open time; the last observed second supplies a close available one second later. Never promote these times to a minute boundary or assume absent seconds had no trades. Native minute observations remain their own source evidence.

The seven original symbols have only August 5–14 second bars; CROX/IONQ/PATH/TWST have longer second history ending September 11. Therefore saved second history cannot fill September 22 actuals. It can support prospective historical feature/label admission where exact source, timestamp, target and chronological checks succeed.

Daily and hourly archives can contribute causal features at their actual availability times. They cannot supply missing intraday target endpoints. No training, model assessment, performance claim, production mutation or provider request was made.

Issues: **0**. Full per-window checks and raw verification scope: [JSON](C:/dev/ducketz/artifacts/analysis/historical-training-20260923/xnas-history-inventory.json).

## Native quality exclusions

- COST ohlcv-1h: 1 undefined/nonpositive OHLC observations; exact timestamps retained in JSON. These rows must be excluded from eligible inputs, without changing source evidence.
- IONQ ohlcv-1m: 3 undefined/nonpositive OHLC observations; exact timestamps retained in JSON. These rows must be excluded from eligible inputs, without changing source evidence.
- PATH ohlcv-1m: 1 undefined/nonpositive OHLC observations; exact timestamps retained in JSON. These rows must be excluded from eligible inputs, without changing source evidence.
- IONQ ohlcv-1s: 5 undefined/nonpositive OHLC observations; exact timestamps retained in JSON. These rows must be excluded from eligible inputs, without changing source evidence.
- PATH ohlcv-1s: 1 undefined/nonpositive OHLC observations; exact timestamps retained in JSON. These rows must be excluded from eligible inputs, without changing source evidence.

The seconds comparison conservatively excludes any minute bucket containing an invalid second. Overlap discrepancies are reported, not repaired or used to overwrite native minute data.
