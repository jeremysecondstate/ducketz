# Session-aware and derived bars

Duckets treats a provider bar timestamp as the opening time of the interval. A
bar is eligible for normalized storage and calculations only after its interval
has ended.

This matters at every polling cadence: a provider may expose the candle that is
still forming when a Loop A cycle begins.

## Databento storage invariant

Raw and normalized Databento files have different responsibilities:

```text
raw/databento        provider-shaped evidence, possibly including an active bar
normalized/databento completed bars only
```

Every normalized write filters against the fetch cycle's captured observation
time. A cleanup pass also rechecks the current normalized file and removes any
incomplete row.

The normalized Arrow schema is:

```text
id         string, non-null
timestamp  timestamp[ns, UTC]
open       float64
high       float64
low        float64
close      float64
volume     float64
```

`id` is the readable UTC `timestamp`. It is the only Duckets-generated
identifier column. The path supplies symbol, provider, timeframe, scope, and
request context.

Databento `instrument_id` and `publisher_id` may remain in raw provider data
because Databento supplied them. They are not copied into normalized, technical,
signal, feature, target, prediction, or evaluation Parquets.

The technical loader recalculates completion defensively and excludes any
still-forming candle before split adjustment or a registered calculation. This
is a second safety boundary in addition to normalized storage.

## Timing columns in calculated outputs

Technical outputs include readable timing and session values:

```text
id
timestamp
bar_end_timestamp
bar_complete
bar_is_current
bar_complete_as_of
bar_duration_seconds
bar_timing_version
session_type
session_date
session_minute
session_progress
```

Their `id` is also the row `timestamp`. No separate observation, snapshot,
session, or timing identifier is created.

`session_type` is calculated in `America/New_York`, including daylight-saving
changes:

```text
PREMARKET     04:00–09:30 ET
REGULAR       09:30–16:00 ET
AFTER_HOURS   16:00–20:00 ET
OVERNIGHT     other weekday intraday times
CLOSED        weekends
MULTI_SESSION daily, weekly, and monthly bars
```

`input_incomplete_bar_count` records how many provider rows were intentionally
excluded by the calculation-time check.

## Databento derived intervals

The Databento lane derives `5m`, `10m`, `15m`, and `30m` intervals from completed
one-minute bars. Aggregation:

1. receives finalized normalized-source candidates;
2. independently discards an incomplete one-minute bar;
3. groups by Eastern market date and session type;
4. aggregates OHLCV without crossing session boundaries;
5. emits the larger bar only after that interval has closed;
6. writes `id, timestamp, open, high, low, close, volume`.

Outputs use the normal stock layout:

```text
DATASTORE/stocks/<SYMBOL>/bars/5m/databento/normalized/*.parquet
DATASTORE/stocks/<SYMBOL>/bars/10m/databento/normalized/*.parquet
DATASTORE/stocks/<SYMBOL>/bars/15m/databento/normalized/*.parquet
DATASTORE/stocks/<SYMBOL>/bars/30m/databento/normalized/*.parquet
```

This lane complements Schwab data. Provider-aware calculations can compare both
when they are available, while Databento-derived intervals still provide
completed higher-timeframe coverage when Schwab intraday history is unavailable.
