# CME/L2 runtime

## Identity

- Canonical name: CME/L2 runtime
- Logical aliases or numbering: startup owner 1; independent CME/L2 owner
- Runtime entry point: `python -m datafetching.cme_runtime`
- Owning package: `datafetching`
- Classification: Independent production loop
- Scheduling mechanism: one supervisor computes the next due slot per schema
- Cadence and phase: `mbp-10` every 5 seconds at +0 seconds; `bbo-1m` every 15 seconds at +2 seconds; `ohlcv-1m` every 60 seconds at +1 second
- Lock or single-writer mechanism: `.ducketz-cme-writer.lock` via the stale-owner-aware exclusive runtime lock
- Primary code evidence: **Confirmed.** `datafetching/cme_runtime.py:37`, `datafetching/cme_runtime.py:355`, `datafetching/cme_runtime.py:382`, `datafetching/cme_history.py:22`

## Purpose

**Confirmed:** this loop independently acquires high-frequency continuous-futures evidence, preserves event history and successful query positions, publishes a causally bounded current L2 snapshot, and derives hourly cross-asset features for Directional Loop B. It exists outside Loop A so CME cadence, high-volume MBP recovery, and writer ownership do not delay the equity/provider cycle. `datafetching/cme_runtime.py:98`, `datafetching/cme_runtime.py:171`, `datafetching/cme_runtime.py:192`, `docs/datafetch-ml/current_start_command:42`

**Confirmed non-ownership:** it does not fetch equity Loop A data, build horizon targets, fit models, publish option valuations, capture equity option chains, or publish strategy ranks.

**Startup/bootstrap boundary:** on an empty datastore this recurring owner
self-initializes its own bounded runtime history (30 days for OHLCV and at most
three days for non-OHLCV schemas) and then continues from its verified owned
cursors. The optional 5,000-day CME cold-start archive is included Standard-plan
maintenance evidence in a distinct namespace; it does not replace or authorize
the CME runtime cursor, L2 snapshot, or writer lock.

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| CME continuous-futures OHLCV | Databento Historical API | provider `fetch_cme_context_exact`; schema `ohlcv-1m` | NQ, ES, RTY, GC, CL one-minute open/high/low/close/volume plus provider event/receive and local receipt clocks | Exact query ranges; 120-second overlap; daily partitions; common context needs 60 identical minute timestamps | Required for hourly context; individual fetch failures isolated by schema | **Confirmed.** `datafetching/cme_runtime.py:37`, `datafetching/cme_runtime.py:47`, `datafetching/cme_runtime.py:479`, `datafetching/cme_cross_asset_context.py:103` |
| CME BBO | Databento Historical API | schema `bbo-1m` | best bid/ask state, symbols, event/receive/receipt timestamps | 15-second cadence/overlap; hourly partitions; newest observation for all five roots must be no more than 15 minutes old for context | Required for relative-spread context and BBO portion of L2; otherwise derived stage skips/fails | **Confirmed.** `datafetching/cme_runtime.py:39`, `datafetching/cme_cross_asset_context.py:78`, `datafetching/cme_history.py:366` |
| CME MBP-10 | Databento Historical API | schema `mbp-10` | depth price/size, side, action, sequence, instrument/symbol and event clocks | 5-second cadence; 2-second overlap; five-minute chunks; 250,000-row cap; saturated ranges split before persistence | Required for book imbalance/current book; otherwise source fetch or derived stage fails independently | **Confirmed.** `datafetching/cme_runtime.py:40`, `datafetching/cme_runtime.py:55`, `datafetching/cme_runtime.py:57`, `datafetching/cme_runtime.py:492` |
| Successful query cursor | This loop’s prior cycle | `pools/cme/runtime/cursors/<group>__<schema>.json` | dataset/group/schema/symbols, `queried_through`, `successful_at`, optional `last_event_at`, row count | Read before query planning; only advanced after all chunks in that schema complete | Optional on first run; required if present and must match path identity/schema | **Confirmed.** `datafetching/cme_history.py:81`, `datafetching/cme_history.py:114`, `datafetching/cme_runtime.py:428`, `datafetching/cme_runtime.py:535` |
| Previously persisted event partitions | This loop’s prior cycles | `pools/cme/events/databento/<group>/<schema>/{raw,normalized}/.../events.parquet` | exact event rows keyed adaptively by symbol/instrument, event time, sequence, action, side, depth and price | Idempotent overlap upsert; day partition for OHLCV, hour for book/quote events | Optional on bootstrap; reused for overlap, hourly derivation and L2 | **Confirmed.** `datafetching/cme_history.py:173`, `datafetching/cme_history.py:207`, `datafetching/cme_history.py:256`, `datafetching/cme_history.py:654` |

## Processing and decisions

1. **Confirmed:** discover requested Databento CME specs and enforce maximum concurrency of one or two. `datafetching/cme_runtime.py:114`, `datafetching/cme_runtime.py:126`
2. **Confirmed:** for each schema, read its cursor, build bounded overlapping chunks, and call the provider with persistent bounded retry. `datafetching/cme_runtime.py:428`, `datafetching/cme_runtime.py:433`, `datafetching/cme_runtime.py:479`
3. **Confirmed:** if a response hits its record limit, split the exact request and do not persist/advance past the missing range. `datafetching/cme_runtime.py:492`
4. **Confirmed:** persist normalized and raw events into bounded partitions, deduplicating by stable event identity rather than volatile fetch metadata. `datafetching/cme_history.py:207`, `datafetching/cme_history.py:654`
5. **Confirmed:** after every planned range for a schema succeeds, atomically publish the cursor. Schema failures are recorded and do not prevent other schemas from running. `datafetching/cme_runtime.py:168`, `datafetching/cme_runtime.py:535`
6. **Confirmed:** derive every unseen completed common one-hour window. It requires the same 60 OHLCV minutes for all roots, recent BBO/MBP for all roots, rejects limit saturation/future evidence, and records `available_at` as the maximum relevant clock. `datafetching/cme_cross_asset_context.py:76`, `datafetching/cme_cross_asset_context.py:96`, `datafetching/cme_cross_asset_context.py:168`, `datafetching/cme_cross_asset_context.py:174`
7. **Confirmed:** publish the latest five-minute L2 state using only events causally available by the boundary; classify MBP/MBO older than 60 seconds and BBO older than 300 seconds as `STALE`. `datafetching/cme_history.py:288`, `datafetching/cme_history.py:336`, `datafetching/cme_history.py:366`

No additional owned worker exists. Query splitting is an internal queue, not a production loop.

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Raw and normalized CME event history | This loop’s derivations; possible read-only/research consumers | `pools/cme/events/databento/.../events.parquet` | OHLCV/BBO/MBP records; provider event/receive and local receipt clocks; normalized IDs | Bounded immutable/idempotent partitions; saturated parent request is never published as complete | **Confirmed.** `datafetching/cme_history.py:173`, `datafetching/cme_runtime.py:492`, `datafetching/cme_runtime.py:508` |
| Per-schema successful cursor | This loop | `pools/cme/runtime/cursors/*.json` | queried-through boundary, last event, success time and row count | Atomic JSON; only after the schema’s exact ranges succeed | **Confirmed.** `datafetching/cme_history.py:131`, `datafetching/cme_history.py:162`, `datafetching/cme_runtime.py:535` |
| Hourly cross-asset context | Directional Loop B; indirectly Strategy | `pools/cme/features/cross-asset-context/databento/1h.parquet` | NQ/ES/gold/crude returns; small-cap and tech breadth; relative spread; book imbalance; completeness/staleness and availability | Immutable natural key `(context_name, window_end, calculation_version)`; only complete causal windows | **Confirmed.** `datafetching/cme_cross_asset_context.py:24`, `datafetching/cme_cross_asset_context.py:181`, `datafetching/cme_cross_asset_context.py:221`, `datafetching/cme_cross_asset_context.py:277` |
| Five-minute L2 snapshot, manifest, receipt and pointer | No production-loop consumer located; supporting/current-state artifact | `pools/cme/snapshots/l2/databento/5m/<target_ns>/`; pointer `pools/cme/snapshots/l2/databento/5m/latest.json` | latest causal book/BBO rows; event/receipt ages; `FRESH`/`STALE`; cursor lineage | Immutable target directory and checksum receipt, then atomic pointer; exact existing target is reused | **Confirmed.** `datafetching/cme_history.py:295`, `datafetching/cme_history.py:306`, `datafetching/cme_history.py:418`, `datafetching/cme_history.py:437` |
| Failure records | Operators/diagnostics | datastore error authority | group/schema or derived-stage error and time | Failure is recorded per schema/stage; successful lanes continue | **Confirmed.** `datafetching/cme_runtime.py:168`, `datafetching/cme_runtime.py:189` |

## Direct loop relationships

### Upstream

**Confirmed:** no other production loop is upstream. Databento is an external provider; this loop’s own cursor/history is state, not another loop.

### Downstream

**Confirmed:** Directional Loop B directly reads the hourly context and joins the `cme__` family by causal availability/freshness. Strategy receives the same values only indirectly because Loop B copies context into its samples/candidates. `ml/rolling_materialization.py:782`, `ml/datasets/families.py:517`, `ml/strategy_selection/model.py:118`

### Timing and control relationships

**Confirmed:** CME runs independently at sub-minute schema phases. There is no readiness barrier between it and Loop A or Loop B; Loop B consumes the latest causally eligible context at its own cutoff. `datafetching/cme_runtime.py:641`, `ml/rolling_materialization.py:782`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | CME events → hourly causal context → Loop B `cme__` features → calibrated directional probability. `datafetching/cme_cross_asset_context.py:181`, `ml/rolling_materialization.py:782`, `ml/runtime_pipeline.py:480` |
| Option-pricing predictions | None | No Pricing input reader for CME event/context/L2 artifacts was found. |
| Options-strategy predictions | Indirect | CME context → Loop B samples/predictions → Strategy context features and profitable-outcome score. `ml/strategy_runtime.py:125`, `ml/strategy_selection/model.py:118` |

**Roll-up classification: Both.** This follows evidenced paths to horizon and strategy outputs; it does not imply a CME path to contract pricing.

## Failure and degradation behavior

- A second owner is rejected by `.ducketz-cme-writer.lock`; the shared helper
  may reclaim the lock once only when its recorded PID is dead.
- A provider or persistence failure is recorded per group/schema. Other due
  schemas can continue, but the failed schema cursor does not advance until all
  exact chunks for that query range verify.
- Saturated multi-symbol requests are split rather than published as complete.
  Derived hourly-context or L2 failure leaves the prior verified artifact and
  pointer authoritative.
- Missing/stale MBP/BBO evidence produces explicit quality/staleness state; it
  is never restamped to make a causal window fresh.


## Accuracy and efficiency relevance

- Exact query cursors, adaptive natural keys, event/receipt clocks and common
  completed windows prevent duplicate or future CME evidence from entering
  Loop B.
- The production `--max-concurrency 1` command bounds provider pressure;
  schema-specific 5/15/60-second phases keep high-frequency work independent of
  Loop A.
- Empirical directional lift is not established by code presence; current
  `cme__` coverage, quality and model reports remain operational evidence.


## Conflicts, gaps, and uncertainty

- No executable production consumer of the five-minute L2 `latest.json` pointer
  was found. It is intentionally retained because the CME owner still publishes
  and verifies it; this audit does not treat it as Loop A readiness or delete it.
- CME dataset/symbol scope is externally configured. Repository defaults and
  code establish validation/ownership, not current entitlement or population.


## Evidence index

- `datafetching/cme_runtime.py:37`
- `datafetching/cme_runtime.py:355`
- `datafetching/cme_runtime.py:382`
- `datafetching/cme_runtime.py:535`
- `datafetching/cme_history.py:131`
- `datafetching/cme_history.py:295`
- `datafetching/cme_cross_asset_context.py:181`
- `ml/rolling_materialization.py:782`
