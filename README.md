# Duckets

## Duckets Law

Every file, class, function, method, constant, setting, dependency, and UI
element must have a reason to exist today.

Code is only allowed if it is directly used by the current application:

- no placeholder modules;
- no speculative abstractions;
- no unused helpers;
- no copied legacy code;
- no “we might need this later.”

Before committing, every new symbol must answer:

1. What uses this today?
2. What breaks if this is deleted?
3. Is this simpler than the alternative?

If the answer is unclear, delete it.

## Independent runtimes

Duckets has five long-running processes connected through causal Parquet and
atomic receipt contracts.

- CME owns complete CME OHLCV/BBO/MBP history, five-minute L2 snapshots, and
  hourly cross-asset features.
- Options owns Schwab chain acquisition, exact normalized contracts, and
  option-quality surfaces.
- Loop A owns equity/provider ingestion plus fundamentals, technicals, and
  signals. Its default external modes never fetch CME or option chains.
- Directional Loop B publishes samples, predictions, evaluations, monitoring,
  and intelligence without running strategy selection.
- Strategy consumes an already-published Loop B run and publishes through its
  own immutable run and pointer.

No runtime starts another runtime, and writer locks prevent compatibility modes
from targeting the same owned artifacts as an external process.

Run one CME and Options cycle:

```powershell
python -m datafetching.cme_runtime --datastore C:\data\ducketz --once
python -m datafetching.options_runtime --datastore C:\data\ducketz --symbols NVDA --once
```

Run one Loop A cycle:

```powershell
python -m datafetching.orchestrate --datastore C:\data\ducketz --symbols NVDA --once
```

Run one Loop B cycle:

```powershell
python -m ml.prediction_runtime --datastore C:\data\ducketz --symbols NVDA GOOG MU --provider databento --horizons 1h 4h 1d 1w --once
```

Run Strategy after Loop B has published:

```powershell
python -m ml.strategy_runtime --datastore C:\data\ducketz --once
```

See
[`docs/datafetch-ml/independent-runtime-orchestration.md`](docs/datafetch-ml/independent-runtime-orchestration.md)
for ownership, startup order, cadences, causal cutoffs, recovery, stopping, and
inline compatibility modes.

During the versioned-generation migration, keep Loop A read-only and place all
Loop B artifacts under a separate root:

```powershell
python -m ml.prediction_runtime --loop-a-root C:\data\ducketz-data --loop-a-format generation --loop-a-namespace loop-a-shadow --loop-b-output-root C:\data\ducketz-loop-b-shadow --feature-profile loop-a-generation-v2 --symbols NVDA GOOG MU --once
```

The generation reader validates pointer/manifest structure eagerly, then
validates each selected route's immutable objects before that route is used.
Corruption in an unselected or unrelated symbol route cannot suppress ready
routes. A failed route retains its last successful forecast as explicitly stale
only when the complete runtime contract still matches; otherwise it publishes
`ROUTE_UNAVAILABLE`. See
[`docs/datafetch-ml/loop-b-generation-migration.md`](docs/datafetch-ml/loop-b-generation-migration.md).

`python -m ml.migration_report equivalence ...` creates a verified
shadow-versus-legacy JSON report and refuses to compare different feature
contracts. `python -m ml.migration_report timing ...` idempotently collects
verified 20-symbol publication timing from the authoritative receipt chain; its
default gate requires 60 unique schedule-slot/generation samples bound to the
exact producer gate schema, and fixture/unclassified evidence cannot return a
production pass.

The explicit `loop-a-generation-v2` profile consumes the new producer's
versioned calculated technical datasets without treating them as equivalent to
the default `loop-a-all-v1` feature contract. Daily/weekly v2 routes currently
remain unavailable when the producer omits their required breakout-pressure
family. The default integrated profile and the legacy `production-v1` and
`technical-all-v2` profiles remain available through `--feature-profile`; see
the migration guide for the contract boundary and blockers.

The `1h` and `4h` routes use completed canonical `1h` bars as their feature and
decision source. Native Databento hours are preferred, with a full-constituent
`1m`-derived hour filling provider publication lag. Their v2 targets use exact
adjusted native Databento `1m` constituents. The exchange calendar selects the official session open,
post-break resume, or next safe full-local-clock anchor before price lookup,
then accumulates 60 or 240 eligible regular-session minutes. Closed periods
pause accumulation while intervening price gaps remain in the return. Loop A
does not fetch or persist synthetic `4h` bars.

The public `1w` selection is a dynamic **remaining-week outlook**. Internally
the existing `1w` route is the aggregate from the next eligible session's open
through the final eligible close of that exchange week, while `1w-d1` through
`1w-d5` supply the available per-session open-to-close routes. Only the Day 1
prefix that remains in the same exchange week is published as the current
snapshot. Thus a Monday-close decision publishes a Tuesday-through-Friday
aggregate plus `d1` through `d4`; Wednesday close publishes Thursday-Friday
plus `d1` and `d2`. The exchange calendar handles holidays, early closes,
weekends, and DST, and every route remains eligible until its applicable
session-close deadline.

The feature mappings, point-in-time rules, quarantine gates, and model reuse
contract are documented in
[`docs/datafetch-ml/audited-feature-contracts.md`](docs/datafetch-ml/audited-feature-contracts.md).
The independent versioned Schwab-chain Strategy runtime is documented in
[`docs/datafetch-ml/options-strategy-selection.md`](docs/datafetch-ml/options-strategy-selection.md).
The Duckets **Options Strategies** tab displays those published predictions,
combines them with the current Schwab position, fills exact-contract tickets
from the **Exact legs** column, and submits the displayed single- or multi-leg
order through the existing Schwab order path.

Remove `--once` to run any runtime continuously. Each writer uses a datastore-
rooted ownership lock and stops cleanly on `Ctrl+C`.

For recurring production operation, use
`docs/datafetch-ml/current_start_command` and
`docs/datafetch-ml/current_prediction_command`. Loop A continuously runs its
normal fetch, normalization, fundamental, technical, and signal cycle. It marks
a small datastore state file `WRITING` before the cycle and atomically replaces
it with `COMPLETE` only after a successful cycle.

The legacy layout uses one crash-released operating-system lock to serialize a
complete Loop A write cycle with a complete Loop B read/model/publication cycle.
This prevents Loop B from ingesting a mixture of two Loop A cycles without any
bootstrap, readiness lease, decision-timestamp handoff, acknowledgement, or
rejection state. Loop B runs on its normal UTC phase whenever the latest Loop A
cycle is `COMPLETE`. Non-weekly routes retain their existing rolling behavior.
Weekly issuance is resolved independently for every symbol from that symbol's
newest usable completed daily decision. A symbol first added on Tuesday can
publish Tuesday-through-Friday; one first added on Wednesday can publish
Wednesday-through-Friday in the same Loop B cycle. Prior published rows are an
optional exact-decision reuse source, not an issuance prerequisite. Each
requested route still validates its actual input files and schema. In
generation mode, failures are isolated by symbol and horizon while the atomic
current-output pointer advances only to a complete immutable Loop B run.

If a symbol has no usable remaining-week decision before the applicable close,
Loop B simply omits that symbol's weekly LIVE snapshot for the cycle. Other
symbols and horizons continue. The next completed daily decision can create a
shorter snapshot without bootstrap, backfill, or a legacy target fallback.

## Parquet identity rule

Every persisted Parquet contains exactly one Duckets-generated identifier
column. It is named `id` and is readable. Declared calculated and Loop B grains
use their exact natural columns; provider ingestion adaptively extends a usual
key and can use a deterministic file-local row fallback rather than rejecting
valid fetched evidence. Provider-native IDs, UUIDs, and hashes may remain in raw
provider data but are never used as the Duckets `id`.
Loop-control state is JSON-only. The persisted Loop B sample schema also omits
the redundant `feature_available_at`, `feature_computed_at`, and
`materialized_at` workflow fields.

The complete contract, schema examples, provider-native exceptions, and test
commands are in
[`docs/datafetch-ml/parquet-id-contract.md`](docs/datafetch-ml/parquet-id-contract.md).

## Application entry point

Run the desktop application with:

```text
python -m app.main ui
```

The first tab is the read-only rolling forecast dashboard. It retains the
existing `1h`, `4h`, and `1d` cards and presents `1w` as one grouped
**remaining-week outlook** with the aggregate probability, issuance time,
aggregate window, and dated remaining-session rows. By default it follows
the single authoritative current-view pointer:

```text
DATASTORE/ml/latest/run.json
```

and reads `intelligence.parquet` from that immutable timestamped run. Loop B
also maintains this compatibility mirror:

```text
DATASTORE/ml-intelligence/latest/rolling-predictions.parquet
```

An explicitly configured fixture path remains available for tests. The mirror
is not the receipt-era publication commit signal. Card evidence is scoped to
the exact symbol/horizon route; pooled `live_horizon` rows are model-performance
summaries, not a symbol card's completed-forecast count. A completed LIVE
forecast is prospective, matured, contract/window/cost-compatible, and
recovered from a verified run; a run declaring the transactional publication
contract also requires a valid manifest-bound `publication.json` receipt and
must be reachable through the authoritative pointer's publication chain.

Dashboard setup and behavior are documented in
[`docs/datafetch-ml/rolling_forecast_ui.md`](docs/datafetch-ml/rolling_forecast_ui.md).

Deployment, model training against an operational datastore, and supervisor
restarts remain explicit operator actions. This repository documentation does
not claim that a live dynamic weekly publication has been deployed or observed.
