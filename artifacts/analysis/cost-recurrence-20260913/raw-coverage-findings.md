# COST native price coverage investigation

Generated 2026-09-13 UTC. All source inspection was read-only. No provider request,
source modification, new synthetic observation, forecast/model mutation, publication,
pointer change, or order action was performed.

The recent missing actuals originate in sparse observations in the returned
XNAS.ITCH OHLCV1m source. They do not come from COST being omitted from the universe,
a local normalization error, or an unfinished latest download. All seven symbols use
the same source reader and five-minute endpoint rule.

The native verified loader accepted all source partitions. For all 28 recent
partitions (four overlapping requests per symbol covering September 4–11), decoding
the original DBN yields exactly the same timestamp, symbol, OHLC, volume, and
identifier values as the normalized Parquet. Recent DBN metadata has the requested
source/range, complete symbol mapping, no partial or not-found symbol list, and no
recorded provider warnings. This establishes local fidelity to the provider's
returned data. It does **not** prove that no trading occurred on the market during
unobserved minutes, or independently rule out provider-side omissions.

## September 11 density

Minutes represented by actual source bars, using Pacific time:

| Symbol | 04:00–06:30 / 150 | 06:30–13:00 / 390 | 13:00–17:00 / 240 |
|---|---:|---:|---:|
| AAPL | 146 | 390 | 165 |
| AMZN | 134 | 390 | 145 |
| GOOG | 132 | 390 | 109 |
| MU | 150 | 390 | 218 |
| NVDA | 147 | 390 | 157 |
| SNDK | 149 | 390 | 201 |
| COST | 31 | 386 | 20 |

Across September 8, 9, 10, and 11, COST misses 1, 3, 2, and 3 of the 14 hourly
actual-price points (04:00–16:00 observed opens, 17:00 observed close). The other six
symbols miss none on those four sessions. COST's after-hours bar counts over those
sessions are 90, 40, 37, and 20 out of 240 possible minutes. This is recurring source
sparsity, especially outside regular hours; there is no observed special COST code
branch in the price loading or endpoint selection.

## The seven September 11 missing forecast outcomes

| Frozen forecast | Missing endpoint | Nearest permitted-direction source observation | Gap |
|---|---|---|---:|
| 1h@04:00 | 05:00 finish | 04:44 close | 16 min |
| 1h@13:00 | 14:00 finish | 13:46 close | 14 min |
| 1h@14:00 | 14:00 start / 15:00 finish | 14:08 open / 14:52 close | 8 / 8 min |
| 1h@15:00 | 15:00 start / 16:00 finish | 15:14 open / 15:38 close | 14 / 22 min |
| 1h@16:00 | 16:00 start | 16:59 open | 59 min |
| 4h@12:00 | 16:00 finish | 15:38 close | 22 min |
| 1h@gap | September 10 17:00 start close | September 10 16:47 close | 13 min |

The final row is the previous close already completed synthetically for the
planning anchor under the explicit planning-only policy. That assumption correctly
does not become an actual outcome. The other six outcomes fail different current
session boundaries. September 11's final 17:00 close is present and timely.

## Evidence

- Native COST latest partition:
  `C:/DATASTORE/market-data/databento/us-equities/XNAS.ITCH/ohlcv-1m/COST/windows/2026-09-09_to_2026-09-12/`.
- Frozen review:
  `C:/DATASTORE/ml/gameplan-actuals-review-runs/20260912T051439.995563Z/`.
- `raw-coverage.json` records raw and normalized SHA-256 values, exact equality
  checks, native metadata, density, and nearest source observations across all seven.
- `raw-coverage-recent-prices.parquet` contains 28,988 original deduplicated source
  rows for September 4–11, with no fabricated or repriced observations.
- `raw-coverage-source-inventory.json` copies the full verified source inventory from
  the frozen review, matching the source successfully accepted by the native loader.
- `raw-coverage-hourly-summary.csv`, `raw-coverage-density.csv`, and
  `raw-coverage-boundaries.csv` provide compact comparisons.
- `raw-coverage-cost-forecast-results.json` and `raw-coverage-cost-price-results.json`
  are read-only extracts of the frozen actuals review.

The accompanying script reproduces the audit from the verified native source. The
five-minute actuals policy must retain these missing outcomes until a separately
verified actual source observation becomes available. Blindly downloading the same
completed partitions, treating 59-minute-late prices as hourly actuals, or using the
planning synthetic bars cannot repair the missing evidence.
