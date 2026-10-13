# Scout independent intraday adoption

Jeremy's October 13, 2026 standing direction defines three independent traders. Atlas runs the combined 22-symbol Gameplan trader and Atlas's local eleven-symbol 15-minute trader. Scout runs its own eleven-symbol 15-minute trader. Each trades and logs its own activity with its own local state. No trader checks, verifies, reconciles, combines, or depends on another trader.

Only the two nightly eleven-symbol Gameplans are synthesized. Use one actual account cash/stock balance, publish the same combined Gameplan and Stats on both PCs, and prepare the traders every night under permanent authorization. Atlas may synthesize directly and return the completed result to Scout without an acceptance wait. The human manually starts each trader. See [Independent trading systems](independent-traders.md).

## Research membership without account activation

The research runner uses this PC's installed profile and local watchlist. Gameplan account configuration, activation and peer worker status are not research or 15-minute prerequisites. Keep the local eleven-symbol membership and the existing authorized provider/data bindings.

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

Scout's local 15-minute trader uses its own execution config, orders, fills and lock under `intraday/execution/`. It does not use Atlas's Gameplan activation, a distributed Atlas reservation service, a combined ledger or another worker's compatibility proof. Shared source does not mean shared operating state.

Use the established independent technical sizing recipe and causal model/target contracts. Current actual broker cash/shares bound each own order. Save desired and adjusted quantities, true order IDs, fills and outcomes. Preserve unresolved own orders and the local manual start/stop control.

Nightly preparation is permanently authorized. The human uses Scout's local `Start-15m-Trader.cmd` to begin trading. The same combined nightly Gameplan is displayed on Scout, but only Atlas runs its Gameplan executor.

Optional cross-PC telemetry remains a read-only display aid. Preserve actual producer timestamps and historical receipts, but do not require a remote packet, acknowledgment or delivery verification before any trader runs. Detailed account state, databases, raw data and fitted models remain private. See [current manual guide](intraday-live-readiness.md).
