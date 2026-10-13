# Scout intraday source and research adoption

Scout uses the shared exact-target ensemble and its own eleven-symbol profile,
watchlist, DATASTORE, broker/provider bindings and logs. The current manual
execution path is [independent broker-local execution](scout-local-15m-manual-start.md).
Gameplan account configuration, Atlas enrollment, shared reservation services
and other-trader state are not required for that path.

Every night is already authorized by Jeremy's October 13 standing instruction:
Atlas produces its Gameplan, Scout produces its Gameplan, Scout synthesizes the
two plans, and Scout sends the result to Atlas. Delivery completes the handoff.
No further human permission, peer acceptance, third account snapshot or
cross-trader verification/reconciliation is an operating prerequisite. Each of
Gameplan, Atlas 15m and Scout 15m executes independently and keeps its own logs.
The first trader start remains Jeremy's manual action. Source publication and
private-data procedures continue separately from this operating authority.

Reviewed source adoption follows the installed exact-byte procedure and preserves
private originals. Historical/research/shadow commands remain distinct from the
human's real trader start and from actual live results.

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

The approved initial sizing contract replaces the older hourly reference:
`0.015 * current equity / fresh BUY ask or SELL bid * own-symbol TA score`,
nearest-rounded half-up, score in `[0,1)` and rounded equality allowed. The
[nine-term starter](../15m-trading-system/INTRADAY_15M_SYSTEM_DESIGN.md#3-approved-independent-technical-sizing)
uses original causal forecast candles and preserves full local recipe,
contribution, availability and hash evidence. It has no Gameplan due-order or
horizon/budget/reserve-buffer dependency. Desired size stays immutable; actual
uncommitted cash/eligible shares separately clip it to a smaller quantity,
including zero, without discarding the forecast or fabricating fills/P&L.

Each 15m trader executes through its own broker and manages its own accepted IDs,
fills and cancellation history. The local journal prevents duplicate effects;
other traders' state and locks are not prerequisites. Scout is not shadow-only,
and a distributed reservation protocol is not outstanding startup work. The
human starts the local trader manually using its existing command.

The read-only tab shows persisted results. Remote operations select the newest
cycle by its saved start time, independently of packet order. A receipt or local
refresh cannot make a historical result live or renew a remote producer clock.
Actual cross-PC verification requires an independently produced packet and a
separate receiving-PC receipt; fixture exchanges establish protocol behavior only.
Atlas's authentic receipt of Scout's packet at
`2026-10-12T12:37:14.556354+00:00` and its own packet publication at artifact
commit `61ec8dbc4171ba31a2884e5644eb94d62944d4e6` are prior exchange evidence in
[issue 18](https://github.com/jeremysecondstate/atlas-scout/issues/18#issuecomment-6124501809).
Scout's independent receipt of that Atlas packet remains outstanding; no new
receipt is claimed for this source change. Its 22 Atlas rows are historical
replay source cohorts, not fresh shadow/live activity. Continuous live telemetry
delivery is not installed.
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

Historical/research/shadow commands place no orders and do not restart
production workers. The separate explicit Atlas `Start-15m-Trader.cmd` can place
real orders after runtime gates; it was not launched for this development.
No live schedule, local binding or Scout authority is changed by source adoption.
Continuous correction discovery, model/recipe/lookback research, special
opening/final-information targets remain
follow-up work; existing Gameplan operation and manual controls are preserved.
