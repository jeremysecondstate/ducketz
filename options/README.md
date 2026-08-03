# Duckets option snapshots and features

The option layer turns Schwab option-chain responses into point-in-time contract
evidence and a compact deterministic feature row.

## Decision timestamp

Option data have two clocks:

- `available_at` and `fetched_at`: when Duckets received the Schwab response;
- `timestamp` and `decision_timestamp`: the newest completed Databento one-minute
  bar end that lands on `:00`, `:15`, `:30`, or `:45`.

The clock reads only:

```text
DATASTORE/stocks/<SYMBOL>/bars/1m/databento/normalized/*.parquet
```

Derived 5m, 10m, 15m, and 30m Parquets are not consulted:

```text
1m timestamp:          10:14:00
1m bar_end_timestamp:  10:15:00
option timestamp:      10:15:00
decision_timeframe:    1m
```

A later `10:15:00 → 10:16:00` row does not move the option snapshot clock. The
next eligible boundary is `10:30:00`.

`decision_bar_timestamp`, `decision_provider`, `decision_timeframe`, and
`decision_source_file` describe the readable source context. They are not IDs.
Strict point-in-time consumers must enforce `available_at <= decision_time`
because the option response arrives after its alignment boundary.

If a sparse session has no one-minute row ending on the newest quarter-hour, the
clock remains at the most recent qualifying boundary.

## Storage and readable IDs

Monthly partitions bound file size while preserving appendable history:

```text
DATASTORE/stocks/<SYMBOL>/options/
├── chains/schwab/raw/YYYY-MM.parquet
├── chains/schwab/normalized/YYYY-MM.parquet
└── features/option-quality/schwab/YYYY-MM.parquet
```

Every file has one Duckets-generated `id`:

| Parquet | Natural ID recipe |
| --- | --- |
| raw response | `timestamp` |
| normalized contracts | `timestamp\|contract_symbol` |
| option-quality features | `timestamp` |

No contract, snapshot, feature-set, source, or calculation ID is generated.
`contract_symbol` remains a readable provider value and is not renamed.

Writes upsert on those same natural columns and atomically replace the monthly
file.

## Contract evidence

The normalized chain retains prices, sizes, volume, open interest, implied
volatility, Greeks, intrinsic/time value, contract metadata, provider quote and
trade times, underlying price, rate/dividend inputs, relative spread, and quote
staleness.

## Option-quality features

The feature set is transparent and model-independent:

```text
relative_bid_ask_spread
atm_relative_bid_ask_spread
atm_straddle_implied_move
realized_expected_absolute_move_atm_horizon
atm_straddle_move_excess
atm_straddle_move_richness
iv_minus_realized_volatility
front_iv_minus_back_iv
put_25d_iv_minus_call_25d_iv
smile_curvature
open_interest_concentration
volume_to_open_interest
call_put_volume_ratio
call_put_open_interest_ratio
put_call_parity_residual
atm_put_call_parity_residual
intrinsic_value_violation
intrinsic_value_violation_rate
quote_staleness_seconds
```

### ATM straddle move richness

`atm_straddle_move_richness` compares the executable ATM straddle ask, as a
fraction of underlying price, with recent expected absolute movement over the
same horizon:

```text
realized_expected_absolute_move_atm_horizon
    = realized_volatility_20d
    × sqrt(2 / pi)
    × sqrt(atm_days_to_expiration / 365)

atm_straddle_move_richness
    = atm_straddle_implied_move
    / realized_expected_absolute_move_atm_horizon
```

The `sqrt(2 / pi)` factor converts a zero-drift normal standard deviation into
an expected absolute move.

- above `1.0`: the ATM straddle ask is rich relative to recent movement;
- near `1.0`: the priced move is near the recent baseline;
- below `1.0`: the ATM straddle ask is cheap relative to recent movement.

`atm_straddle_move_excess` expresses the same comparison as a difference.
Coverage columns prevent downstream models from treating absent provider values
as valid zeros.

Twenty-day realized volatility is calculated only from split-adjusted daily
market-regime rows whose bar-end timestamp is no later than the option decision
timestamp. Dependent features remain null when that prerequisite is unavailable.

## Fetch scope

The Schwab lane requests calls and puts, 80 strikes around the underlying,
underlying quote context, and expirations through 120 calendar days.
