# Scout intraday research adoption

This continues the shared full-retraining implementation from Ducketz PRs
67 and 68, with reviewed parent source
`c3c8faf7f65a79d84f7b0fe4fca055b21577a400`. The ten enabled models, exact
not-down target, immutable forecasts and append-only outcomes retain their
existing definitions. Each PC keeps its own symbols and private state.

## Research membership without account activation

The research runner validates the installed profile's actor, machine and unique
symbols against the local watchlist. When account Gameplan configuration is
absent, the watchlist must be the exact path pinned by that installed profile.
This permits research on Scout without creating account or trading configuration.
If account configuration exists, its machine and participant membership must
also agree. Invalid existing configuration never falls back to research bindings.

## Explicit, separate historical sources

`EQUS.MINI` remains the default canonical source and the existing shadow-provider
binding. Local canonical files require their adjacent native provenance. Missing
minute constituents remain missing unless exact no-trade evidence establishes
otherwise.

The optional `--source-dataset XNAS.ITCH` selects only already-local cold-start
minute archives for historical replay. Every partition must retain its manifest,
receipt, normalized observations and native DBN. The loader verifies dataset,
schema, symbol, request interval, hashes, sizes and row counts, then compares all
normalized OHLCV observations against the native floating-point DBN decode.
Contradictory duplicates and concurrent source mutations are rejected.
Native rows containing unusable prices are recorded as explicit unavailable
observations after native equality is established. They invalidate the affected
aggregate rather than becoming zero-volume or synthetic no-trade evidence.

Example using the PC's existing local paths:

```text
python -B -m ml.intraday.runtime --datastore LOCAL_DATASTORE --profile LOCAL_PROFILE --watchlist LOCAL_WATCHLIST --mode historical_replay --source-dataset XNAS.ITCH --reference EXACT_UTC_REFERENCE --experiment DISTINCT_REPLAY_ID
```

XNAS observations are venue-specific, not consolidated EQUS observations. The
adapter never merges the sources or fills unconfirmed gaps. Historical receipt
times remain assumed from candle closes. The archive selector is rejected for
shadow/watch mode before provider construction; it does not change credentials,
subscriptions or live data bindings.

Every cycle still fully retrains the configured ten classifiers for each symbol
with valid causal inputs, using all compatible history by default. Sparse data,
insufficient warm-up, single-class training and individual model failures remain
explicit. A failed cycle or incomplete ensemble is not a completed 110-fit
benchmark. Per-symbol results publish when that symbol finishes.

Scoring selects observations matching the source identifiers saved with each
forecast. An incompatible history cannot append a missing result or revise an
outcome for a different source. Direct store admission also rejects source
mismatches. Existing forecasts and prior revisions stay immutable.

## Monitoring and operating boundaries

The read-only tab shows persisted results. Remote operations select the newest
cycle by its saved start time, independently of packet order. A receipt or local
refresh cannot make a historical result live or renew a remote producer clock.
Actual cross-PC verification requires an independently produced packet and a
separate receiving-PC receipt; fixture exchanges establish protocol behavior only.
Telemetry retains a separate latest row for each symbol, mode and source dataset.
The UI labels and filters datasets so venue-only replay is distinguishable from
consolidated history. Selecting All explicitly combines those metric cohorts.
New readers accept legacy packets without source provenance as Unrecorded;
peers must adopt the source-aware reader before admitting the new optional paired
source fields, because the older strict reader rejects unknown fields.

Raw history, fitted models, SQLite stores, detailed manifests, private errors,
bindings and account state remain local. Reviewed minimal telemetry uses the
separate artifact publication procedure. Source publication, local installation,
running research processes, main integration and production deployment remain
separate facts.

The historical benchmark and bounded shadow checks place no orders and do not
restart production workers. No live trading schedule or account authority is
changed. Continuous correction discovery, automatic retry of failed model
cycles, session-edge policies and eventual execution behavior remain explicit
follow-up work; existing Gameplan operation is preserved.
