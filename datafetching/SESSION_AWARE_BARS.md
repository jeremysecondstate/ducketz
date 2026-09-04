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

## Databento derived intervals and hourly continuity

The Databento lane derives `5m`, `10m`, `15m`, `30m`, and `1h` intervals from
completed one-minute bars. The derived `1h` lane is a continuity fallback for
the provider's sometimes-delayed native hourly publication. When both files
contain the same timestamp, the native hour wins; a complete derived hour fills
only a timestamp the native file has not published yet. Aggregation:

1. receives finalized normalized-source candidates;
2. independently discards an incomplete one-minute bar;
3. keeps `5m`/`10m`/`15m`/`30m` on the strict all-constituent session rule;
4. allows `1h` sparse continuity only when a successful Databento `1m` request
   supplies an explicit selected-range start and end;
5. caps that proof at the fetch observation time and emits only whole XNAS
   trading-date clock hours inside the v6 04:00--20:00 ET source envelope;
6. aggregates every actual trade-bearing minute in a proven hour, or emits a
   flat prior-close/zero-volume bar when that completed hour is truly empty;
7. never fills from a future trade, never emits a partial trailing hour, and
   retains the continuous 09:00--10:00 ET source hour across the open boundary;
8. writes `id, timestamp, open, high, low, close, volume`.

Without explicit provider coverage, including direct library calls and legacy
callers, `1h` retains the strict 60-constituent rule. Holidays, weekends, and
hours outside the XNAS source envelope are never synthesized. On nonstandard
early-close sessions, only full official regular-session hours are eligible.

Outputs use the normal stock layout:

```text
DATASTORE/stocks/<SYMBOL>/bars/5m/databento/normalized/*.parquet
DATASTORE/stocks/<SYMBOL>/bars/10m/databento/normalized/*.parquet
DATASTORE/stocks/<SYMBOL>/bars/15m/databento/normalized/*.parquet
DATASTORE/stocks/<SYMBOL>/bars/30m/databento/normalized/*.parquet
DATASTORE/stocks/<SYMBOL>/bars/1h/databento/normalized/*.parquet
```

This lane complements Schwab data. Provider-aware calculations can compare both
when they are available, while Databento-derived intervals still provide
completed higher-timeframe coverage when Schwab intraday history is unavailable.

## Daily post-close continuity

Databento's native `ohlcv-1d` availability can remain on the prior session until
the next UTC day. After the official XNAS close, the fetch lane therefore builds
the just-completed daily candle from normalized one-minute evidence. It emits a
daily row only when trade-bearing bars reach both regular-session boundaries,
using the exchange calendar for holidays and early closes. Databento intentionally
omits minute intervals with no trades; those gaps do not change daily OHLC or
summed volume. The native daily row wins if it later overlaps the derived
timestamp; the derived file only fills the temporary post-close gap.

A daily provider timestamp is a UTC label for an exchange session, not a literal
24-hour interval. Its completion timestamp is the official session close.
