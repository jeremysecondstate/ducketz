# Market data and feature publication

Last verified 2026-09-25; code/config authority.

The data layer publishes completed perpetual candles and their causal feature
snapshots. Its coordinator owns market-worker lifetimes; models and paper run
as independent consumers. This is a structural reference; command syntax,
feature descriptions and operator examples remain in the
[component guide](../../hyperliquid-data-pipeline.md).

## Source map

| Responsibility | Implementation |
| --- | --- |
| Shared market membership and validation | [Config](../../../configs/hyperliquid-markets.json), [loader](../../../ml/hyperliquid_coordinator_config.py) |
| Worker ownership and concurrency | [Coordinator](../../../ml/hyperliquid_coordinator.py) |
| Candle wake/retry loop | [Data loop](../../../ml/hyperliquid_data_loop.py) |
| Fetch, merge, feature build and publication | [Pipeline](../../../ml/hyperliquid_data_pipeline.py) |
| Public candle normalization | [Candle adapter](../../../datafetching/hyperliquid_candles.py) |
| Shared causal calculations | [Features](../../../technicals/hyperliquid_features.py) |

The verified config has BTC, ETH, HYPE and ZEC with `15m` candles. One process
creates a worker thread per configured symbol. All workers execute the same
code with separate data, retry state and calculation instances. A bounded
semaphore permits two complete pipeline updates at once; a delayed market
does not force every other market onto its retry schedule.

## Cadence and configuration

Each worker first catches up, then targets five seconds after UTC candle close:
`:00:05`, `:15:05`, `:30:05`, `:45:05` for the current interval. These are
planned wake times, not guarantees that the exchange has published a candle.
The cutoff is recaptured after a worker obtains a coordinator slot, so a queued
job crossing a boundary requests the newly appropriate cutoff.

The coordinator checks configuration on its approximately one-second tick.
Only changes to `symbols` are accepted live. Changes to interval, datastore,
concurrency, endpoint or timing settings require a restart. Invalid JSON or
unsupported live changes expose `config_error`; the accepted configuration
continues. A valid added symbol starts its initial update. Removal requests
graceful worker shutdown and retains its history; an in-progress build may
finish before the worker stops.

Market-loop and publication ownership are distinct:

| Artifact | Meaning |
| --- | --- |
| `_coordinator/.coordinator.lock` | One coordinator for the selected datastore |
| `<coin>/15m/.loop.lock` | One continuous loop owns that market/interval |
| `<coin>/15m/.update.lock` | One snapshot writer, including manual one-shot updates |
| Coordinator `stop.request` | Stop this coordinator and the workers it owns |
| Market `stop.request` | Stop that market loop |

An externally owned single-market loop is reported as `external_worker`; the
coordinator neither overwrites its status nor stops it. It may take ownership
after the external loop releases its lock. A deliberately stopped managed
market remains stopped until removed/re-added or coordinator restart. A
bounded `--max-cycles` run treats unresolved ownership conflict as failure.
These lifecycle behaviors do not register an operating-system schedule.

## Input and publication contract

The public `candleSnapshot` endpoint supplies a rolling history window of up
to 5,000 candles. Stored older history is retained as the window moves; the
code does not claim to recover candles already outside the API window.
Only completed candles enter normalized history. Existing datasets reject
mixing market identity, interval or endpoint, and reject a replay cutoff that
precedes their already stored completed data.

The usual refresh fetches overlapping recent candles, or an earlier recoverable
gap where needed. Merge uses timestamps and replaces matching candles with
the newest observation. It does not fill missing prices. If candles, feature
revision and requested label horizons are unchanged, the completed snapshot
is reused. Otherwise the pipeline rebuilds the shared features across retained
history and writes a new immutable run directory.

| Committed data artifact | Contract |
| --- | --- |
| `runs/<run_id>/ohlcv.parquet` | Deduplicated closed candles, market identity and timestamps |
| `features.parquet` | Same row basis with feature columns; no target labels |
| `labels.parquet` | Separate exact-time future returns and strict-up labels |
| `feature_catalog.json` | Ordered feature definitions, revision, provenance and exclusions |
| `summary.json` | Coverage, gaps, feature availability, paths and timings |
| `<coin>/15m/latest.json` | Atomically replaced only after completed run files exist |

Readers pin the selected run once and read its files together. A failure before
pointer replacement leaves the previous completed snapshot selected; partial
or unreferenced run directories are not a new publication. Writes are complete
Parquet snapshots, not byte-level append to existing Parquets. Automatic run
retention/cleanup is not implemented, so storage grows with repeated builds.

## Causal features and labels

The present feature catalog has 38 columns, including shared standard
primitives and 20 custom adaptations. This is a versioned recipe, not a claim
that every legacy BERA indicator was carried over. Per-build cached
intermediates avoid redundant work; there is no persisted incremental rolling
state between builds and no sharing of numerical history across coins.

Warmup and undefined ratios remain missing. Candle gaps restart rolling and
recursive history. Trailing calculations avoid future backfill and full-history
scaling. Downstream model preprocessing is fitted on its own fitting partition.
Feature-definition changes must advance the feature schema/revision so an
unchanged-candle refresh cannot reuse an incompatible snapshot.

Default label horizons are 1, 4 and 16 candles. A target exists only when its
exact future timestamp is observed; unknown outcomes stay null. The data
pipeline's `target_up_*` uses a strictly higher future close. The model layer
instead derives **not-down** from `future_return_* >= 0`, so an unchanged
future price has different class semantics. See [Models](models.md).

## Recovery and truthful status

Missing publication or a failed attempt retries with exponential delay,
starting at 15 seconds and capped at 120 seconds in the verified config.
The loop catches up after downtime instead of queuing overlapping timer jobs.
It reconciles the available history window every 96 cycles; failed/retry
cycles mean this is not an exact daily wall-clock schedule. Coverage reports
separate recoverable and unrecoverable gaps rather than inventing candles.

Inspect `<coin>/15m/loop_status.json` and `loop_events.jsonl` alongside the
coordinator's status/events. A fresh heartbeat does not imply fresh candle
publication. Useful checks include expected/last close, lag intervals, latest
run ID, missing latest features, retry reason and actual process ownership.
Per-loop source ages should remain visible even if a coordinator is healthy.

Timing fields separate `fetch_and_normalize`, `feature_build`, file writing,
and total work before publication. Outer `last_cycle_timing.total_seconds`
also includes coordinator queue wait and publication. Do not call that entire
duration feature work or sum overlapping workers to obtain wall time.
An unchanged check has its own measurement; older logs with absent durations
remain unknown. See the [timing contract](../../hyperliquid-timings.md).

## Dependencies and change review

The model runtime consumes completed snapshots and can continue using an
existing compatible model while a new data job is pending. A paper quote/risk
cycle is not gated on completion of an entire four-symbol data batch, although
forecast and quote freshness checks still govern its decisions. There is no
model/P&L feedback that changes the fetch schedule automatically.

Relevant regression coverage is in the
[pipeline](../../../tests/test_hyperliquid_data_pipeline.py),
[catch-up](../../../tests/test_hyperliquid_catchup.py),
[data-loop](../../../tests/test_hyperliquid_data_loop.py),
[coordinator](../../../tests/test_hyperliquid_coordinator.py), and
[feature](../../../tests/test_hyperliquid_features.py) tests.
When modifying publication or membership semantics, update this page and the
[inventory](../LOOP_INVENTORY.md) together; current status belongs in dated
evidence, not a permanent “running” assertion here.
