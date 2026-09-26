# Hyperliquid market data and shared features

For the maintained cross-system map, runtime inventory and operating procedures,
see [Hyperliquid system analysis](hyperliquid-system-analysis/README.md).

For persistent stage/cycle timings and a Parquet performance export, see
[Hyperliquid operation timing](hyperliquid-timings.md).

This standalone research pipeline fetches public Hyperliquid perpetual candles,
calculates a shared set of causal OHLCV features, and writes Parquets under
`C:/DATASTORE/hyperliquid`. It does not submit orders, transfer funds, train
models, or register a scheduled task.

## Run

From `C:/dev/ducketz` in PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_data_pipeline --coin BTC --interval 15m --benchmark
```

Repeat the command without `--benchmark` to fetch new candles. The public info
URL follows `HYPERLIQUID_INFO_URL` in the existing `.env`; account keys are not
used. `--info-url` can override that URL. Existing histories cannot be mixed
between endpoints. Defaults use the latest 5,000 candles permitted by the
Hyperliquid candle API; at 15 minutes that is approximately 52 days, potentially
one fewer completed candle while the current candle is forming. The pipeline
retains older local history as it grows. It does not claim to fetch exchange
history outside the API's rolling window.

Other options:

- `--output-root PATH`: use a different datastore directory.
- `--interval 5m`: build a separate five-minute dataset.
- `--refresh-history`: refetch the available history window to reconcile older
  revisions, preserving local candles outside that window.
- `--rebuild`: recompute features even if fetched candles are unchanged.
- `--horizons 1 4 16`: forward-label horizons in candle counts; defaults are
  15 minutes, one hour, and four hours for 15-minute input.

## Multiple symbols with one coordinator

Start the shared coordinator from `C:/dev/ducketz` in PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_coordinator
```

The default configuration is `configs/hyperliquid-markets.json` in the project.
An explicit `--config PATH` accepts an absolute path or a path relative to the
current working directory. A relative `output_root` inside the JSON is resolved
against that configuration file's directory. The supplied configuration covers
four perpetual markets:

```json
{
  "version": 1,
  "symbols": ["BTC", "ETH", "HYPE", "ZEC"],
  "interval": "15m",
  "output_root": "C:/DATASTORE/hyperliquid",
  "max_parallel_updates": 2,
  "close_delay_seconds": 5,
  "retry_seconds": 15,
  "max_retry_seconds": 120,
  "repair_every_cycles": 96
}
```

The coordinator runs one process with a worker thread per symbol. Each worker
uses the same fetching, calculation, and publication code, with its own timer,
retry state, history, and calculation cache. At most two complete pipeline
updates run simultaneously with this configuration. A delayed candle or failed
request for one market does not put the other markets on that market's retry
schedule. Workers waiting for an update slot use the current time when their
fetch actually begins.

To onboard another supported perpetual symbol, add it to `symbols` and save the
JSON file. The coordinator checks for changes every second and starts that
symbol's initial fetch immediately. Existing histories are reused and caught
up. No Python source or calculation folders need copying. Removing a symbol
requests a graceful stop for its managed worker and preserves its saved data.
An update already in progress can finish before that worker stops.
The symbols list must remain nonempty; use the coordinator's stop command to
stop all markets.

Only changes to `symbols` apply while the coordinator is running. Changes to
the interval, datastore, concurrency, or timing settings require a coordinator
restart. Invalid JSON or unsupported live setting changes are reported as
`config_error`; the last accepted configuration and its workers keep running.

Inspect or stop the coordinator from another terminal:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_coordinator --status
& .\.venv\Scripts\python.exe -m ml.hyperliquid_coordinator --stop
```

These control commands use the default datastore without parsing the config,
so they also work while that file is temporarily invalid. For a custom
datastore, pass `--output-root PATH` to each control command. `Ctrl+C` also
requests a graceful stop. Shutdown stops only this coordinator's workers.
There is no operating-system startup registration; restart the process after
a reboot.

Coordinator state lives below `C:/DATASTORE/hyperliquid/_coordinator/`:

- `coordinator_status.json`: coordinator and worker status, including config
  errors and workers controlled by another process.
- `coordinator_events.jsonl`: coordinator lifecycle and configuration events.
- `.coordinator.lock`: prevents a second coordinator for this datastore.
- `stop.request`: a pending graceful-stop request.

Each market also retains its existing `loop_status.json`, `loop_events.jsonl`,
and lifetime/update locks in its own symbol/interval directory. A separately
running single-symbol loop is reported as externally managed; the coordinator
does not stop it or start a duplicate. After that external loop stops, the
coordinator can take over the market. To hand off an existing BTC loop, use its
single-symbol `--stop` command below and wait for it to stop before starting
the coordinator.

Stopping an individual managed market with its single-symbol `--stop` command
is respected: it stays stopped until that symbol is removed and re-added to
the configuration, or the coordinator is restarted. To stop all managed
markets, use the coordinator's `--stop` command.

For a bounded onboarding or catch-up check, use `--max-cycles 1`. The limit
applies separately to each worker, including failed attempts, and the
coordinator exits when all its workers complete. An ownership conflict is
reported as an error and exits this bounded run instead of waiting
indefinitely.

## Single-symbol continuous updates

Run the continuous loop from `C:/dev/ducketz` in PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_data_loop --coin BTC --interval 15m --output-root C:/DATASTORE/hyperliquid
```

The loop catches up immediately, then wakes five seconds after each UTC candle
close. For 15-minute candles, the normal refresh times are `:00:05`, `:15:05`,
`:30:05`, and `:45:05` each hour. This runs continuously while the process is
alive. It does not install an operating-system startup task, register a Codex
scheduled task, or execute trades.

If a completed candle has not appeared yet, or a request/build fails, the loop
retries with exponential backoff starting at 15 seconds and capped at 120
seconds. A failed refresh preserves the previous completed snapshot. The loop
continues retrying and catches up missed candles rather than creating a queue
of overlapping refresh jobs. Every 96 cycles it also reconciles the available
API history window for older revisions. This is a cycle count, not a fixed
wall-clock daily guarantee. Missing candles outside the API's latest 5,000
candles cannot be recovered through that endpoint; gaps are reported rather
than filled with invented prices.

Loop options:

- `--close-delay-seconds 5`: delay after the UTC candle boundary before fetching.
- `--retry-seconds 15`: initial retry delay.
- `--max-retry-seconds 120`: maximum retry delay.
- `--repair-every-cycles 96`: frequency of reconciliation across the available
  API history window.
- `--max-cycles N`: stop after a bounded number of cycles, useful for checks.

Read the saved loop status from another terminal:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_data_loop --coin BTC --interval 15m --output-root C:/DATASTORE/hyperliquid --status
```

Stop a foreground loop with `Ctrl+C`, or request a graceful stop from another
terminal:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_data_loop --coin BTC --interval 15m --output-root C:/DATASTORE/hyperliquid --stop
```

The selected market/interval directory, here `BTC/15m`, contains
`loop_status.json` for saved status, `loop_events.jsonl` for the event history,
and `stop.request` when a graceful stop has been requested. A per-market
`.loop.lock` prevents duplicate loops; the existing `.update.lock` separately
protects individual publications, including manual one-shot refreshes.

## Files

Each completed run is written below `<symbol>/<interval>/runs/<run_id>/`,
for example `BTC/15m/runs/<run_id>/` or `ETH/15m/runs/<run_id>/`:

| File | Contents |
| --- | --- |
| `ohlcv.parquet` | Closed candles, UTC open/exclusive-close timestamps, market identity, OHLCV and trade count. |
| `features.parquet` | The same candles plus standard primitives and 20 custom BERA adaptations. No future labels. |
| `labels.parquet` | Separate forward returns and nullable binary up labels, aligned by exact future timestamps. |
| `feature_catalog.json` | Formulas, source provenance, changes, and excluded legacy indicators. |
| `summary.json` | Coverage, missing values, feature counts, computation reuse, timings and file paths. |

Each symbol/interval has its own `latest.json`, such as `BTC/15m/latest.json`,
which points to its completed run. It is replaced only after all
run files have been written; readers pin that run's paths. One short-lived writer
lock prevents simultaneous publishers. A failed build leaves the previous
pointer unchanged. These mechanisms do not add external readiness services.

Repeated runs request a small overlapping candle window and replace matching
timestamps with the latest API values. Unchanged data reuses the completed
snapshot. Changed data triggers a full shared-feature rebuild in this first
version. The computation cache is per build: persistent incremental EMA/window
state is not implemented yet. New feature definitions must increment
`FEATURE_SCHEMA_VERSION` so a refresh cannot reuse old feature outputs.

New candles are logically appended by merging on candle timestamps and
deduplicating, preserving all accumulated raw history. Each changed publication
writes complete Parquet snapshots; it does not append bytes to an existing
Parquet file. All 38 features are recomputed using the shared calculations.
These builds were inexpensive in the initial experiment, but runtime grows with
the accumulated history. Completed run directories are currently retained;
automatic snapshot retention or cleanup is not implemented.

## Shared feature layer

`technicals/hyperliquid_features.py` computes intermediates once per input and
parameter set. Features reuse Series from this per-build cache, and the final
DataFrame is assembled together rather than repeatedly adding and deleting
temporary columns. Rolling OLS, polynomial smoothing, histograms, and FFTs use
batched NumPy calculations. No BERA executable module is imported.

All symbols share this implementation. A BTC calculation and an ETH calculation
use separate `SharedCalculations` instances, so their values and cached
intermediates cannot be reused across markets by accident. Sharing formulas
removes source duplication; sharing repeated intermediates within each build
removes redundant computation. Simple formulas can remain functions.

Twenty custom adaptations are included: LWMF, TDINDI, VAA, ADI, DIRPRESH, VWTMI,
PVAM, APDI, IVTS, DIRECTINS, PAINDEX, DVWA, PDPF, DYPIM, PMVF, EPDII, CVEI,
SMDI, ETSA, and ROC. Their columns end in `_v2`, because they are new feature
definitions, not numerically identical ports. The catalog documents every
adaptation. Standard columns include log returns, EMAs, volatility, RSI, ATR,
Bollinger measures, volume ratio, stochastic position, and momentum.

The other 30 active BERA indicators are explicitly catalogued as excluded from
this first set. They require separate decisions about causal wavelet/Hilbert/STL
transforms, full-history statistical fits, model refit schedules, or external
order-book dependencies. No feature is silently substituted under its old name.

Full-history scaling and backward filling have been removed. Centered smoothers
use trailing polynomial endpoint estimates. Warmup values and undefined ratios
remain null; they are not fabricated zeros. Each feature is available at the
close of its input candle. Candle gaps restart rolling/recursive feature history;
missing prices are never synthesized. Use your selected features' actual
lookbacks when choosing training rows. Fit any downstream imputer/scaler only on
the training partition of each evaluation fold.

Forward outcomes are stored separately to avoid accidental feature leakage.
`target_up_4bar`, for example, is 1 when the close exactly four intervals later
exceeds the current close and 0 otherwise (including an unchanged close).
Unknown future outcomes stay null. These labels are research conveniences, not
a selected trading strategy or evidence of profitability.

## Performance and checks

`--benchmark` runs three cached and three uncached builds, alternating order,
and checks their outputs match. It reports medians and speedup for this same
implemented feature set. This is not a comparison against all 50 original BERA
indicators or against the old 20-minute run. Per-computation timings include
dependencies and should not be summed as wall-clock time.

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_hyperliquid_market_data.py tests/test_hyperliquid_features.py tests/test_hyperliquid_data_pipeline.py tests/test_hyperliquid_catchup.py tests/test_hyperliquid_data_loop.py tests/test_hyperliquid_coordinator.py tests/test_hyperliquid_coordinator_config.py -q
```

Tests cover completed candle boundaries, retries, revisions and duplicates,
future/prefix invariance, cached/uncached equality, known feature values, flat
and zero-volume cases, gap resets, label maturity, file roundtrips, and retaining
the previous publication after failure. Continuous-loop checks also cover
publication delays, capped retries, downtime catch-up, repairable and expired
gaps, boundary-crossing refreshes, singleton ownership, and graceful stop/restart.
Coordinator checks cover configuration validation, live symbol changes,
independent workers, bounded concurrency, queued cancellation, external worker
ownership, and graceful shutdown.
