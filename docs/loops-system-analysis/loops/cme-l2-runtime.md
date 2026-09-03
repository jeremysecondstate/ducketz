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
- Primary code evidence: **Confirmed.** `datafetching/cme_runtime.py:40`, `datafetching/cme_runtime.py:104`, `datafetching/cme_runtime.py:434`, `datafetching/cme_history.py:288`

## Purpose

**Confirmed:** this loop independently acquires high-frequency continuous-futures evidence, preserves event history and successful query positions, publishes a strict causally bounded current L2 snapshot, and derives hourly cross-asset features for Directional Loop B. It exists outside Loop A so CME cadence, high-volume MBP recovery, and writer ownership do not delay the equity/provider cycle. Deep recovery is explicitly separated from current authority: exact current BBO/MBP windows publish first only when every configured stream is fresh, then at most one older recovery chunk is checkpointed. `datafetching/cme_runtime.py:104`, `datafetching/cme_runtime.py:157`, `datafetching/cme_runtime.py:296`, `docs/datafetch-ml/current_start_command:52`

**Confirmed non-ownership:** it does not fetch equity Loop A data, build horizon targets, fit models, publish option valuations, capture equity option chains, or publish strategy ranks.

**Startup/bootstrap boundary:** the cold archive remains separate provider
provenance, but it is no longer isolated from runtime continuation. When an
owned runtime cursor is absent, `cme_archive_cursor_for_spec` verifies the
matching archive scope and supplies a historical boundary for the first live
query. Cross-asset materialization fingerprints archive inventories, combines
verified archive rows with ongoing persisted runtime rows, appends unseen
historical common windows, and records lineage before continuing current
windows. The archive never owns the runtime lock, live cursor, L2 pointer, or
publication authority. `datafetching/databento_archive.py:539`,
`datafetching/cme_runtime.py:476`,
`datafetching/cme_cross_asset_context.py:250`

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| CME continuous-futures OHLCV | Databento Historical API | provider `fetch_cme_context_exact`; schema `ohlcv-1m` | NQ, ES, RTY, GC, CL one-minute open/high/low/close/volume plus provider event/receive and local receipt clocks | Exact query ranges; 120-second overlap; daily partitions; common context needs 60 identical minute timestamps | Required for hourly context; individual fetch failures isolated by schema | **Confirmed.** `datafetching/cme_runtime.py:40`, `datafetching/cme_runtime.py:50`, `datafetching/cme_runtime.py:104`, `datafetching/cme_runtime.py:589`, `datafetching/cme_cross_asset_context.py:83` |
| CME BBO | Databento Historical API | schema `bbo-1m` | best bid/ask state, configured symbols, event/receive/local-fetch timestamps | normal 15-second cadence; strict current lane fetches the exact last five minutes and accepts BBO rows no older than 300 seconds; event time is bounded by the snapshot while local availability has a distinct cutoff | Required for relative-spread context and complete current L2; otherwise strict publication skips | **Confirmed.** `datafetching/cme_runtime.py:202`, `datafetching/cme_history.py:288`, `datafetching/cme_history.py:392` |
| CME MBP-10 | Databento Historical API | schema `mbp-10` | depth price/size, side, action, sequence, configured instrument/symbol and event clocks | normal 5-second cadence and 2-second overlap; strict current lane fetches the exact last five seconds and accepts rows no older than 60 seconds; a deep gap checkpoints at most one older chunk after current collection | Required for book imbalance/current book and complete current L2; otherwise source fetch or derived stage fails independently | **Confirmed.** `datafetching/cme_runtime.py:157`, `datafetching/cme_runtime.py:202`, `datafetching/cme_runtime.py:296`, `datafetching/cme_history.py:392` |
| Successful query cursor | This loop’s prior cycle | `pools/cme/runtime/cursors/<group>__<schema>.json` | dataset/group/schema/symbols, `queried_through`, `successful_at`, optional `last_event_at`, row count | Read before query planning; only advanced after all chunks in that schema complete | Optional on first run; required if present and must match path identity/schema | **Confirmed.** `datafetching/cme_history.py:81`, `datafetching/cme_history.py:114`, `datafetching/cme_runtime.py:428`, `datafetching/cme_runtime.py:535` |
| Previously persisted event partitions | This loop’s prior cycles | `pools/cme/events/databento/<group>/<schema>/{raw,normalized}/.../events.parquet` | exact event rows keyed adaptively by symbol/instrument, event time, sequence, action, side, depth and price | Idempotent overlap upsert; day partition for OHLCV, hour for book/quote events | Optional on bootstrap; reused for overlap, hourly derivation and L2 | **Confirmed.** `datafetching/cme_history.py:173`, `datafetching/cme_history.py:207`, `datafetching/cme_history.py:256`, `datafetching/cme_history.py:654` |
| Verified CME archive inventory | One-shot Databento archive coordinator | `market-data/databento/cme/<dataset>` plus readable scope manifests/receipts | dataset, schema/group/symbol bounds, event clocks, normalized rows, source inventory checksums/fingerprints | eligible only after scope and checksums verify; seeds a missing runtime query boundary and historical context, but cannot advance a live cursor or pointer itself | Optional on empty runtime state; included when verified | **Confirmed.** `datafetching/databento_archive.py:539`, `datafetching/cme_runtime.py:612`, `datafetching/cme_cross_asset_context.py:250` |

## Processing and decisions

1. **Confirmed:** discover requested Databento CME specs and enforce maximum concurrency of one or two. `datafetching/cme_runtime.py:104`
2. **Confirmed:** detect an MBP cursor gap larger than the bounded budget. For every configured BBO/MBP stream, construct an exact current window ending on the latest completed common five-minute boundary: five seconds for MBP/MBO and five minutes for BBO. `datafetching/cme_runtime.py:157`, `datafetching/cme_runtime.py:184`, `datafetching/cme_runtime.py:202`
3. **Confirmed:** collect those current windows without advancing historical cursors, then publish only if all configured expected symbols exist and every row is fresh. Configured symbols are authoritative over an older cursor symbol list. `datafetching/cme_runtime.py:218`, `datafetching/cme_history.py:532`, `datafetching/cme_history.py:577`
4. **Confirmed:** for each normal/recovery schema, read its owned cursor; if absent, derive a verified archive boundary for that exact spec. Build bounded overlapping chunks, but when a deep recovery range exists process at most one older chunk in the cycle. `datafetching/cme_runtime.py:296`, `datafetching/databento_archive.py:539`
5. **Confirmed:** if a response hits its record limit, split the exact request and do not persist or advance past the missing range. Persist successful normalized/raw events with stable event identities, and publish a cursor only after that schema's exact processed ranges succeed. `datafetching/cme_runtime.py:760`, `datafetching/cme_history.py:207`, `datafetching/cme_history.py:654`
6. **Confirmed:** fingerprint verified archive inventories, combine archive and already-persisted runtime rows, materialize every unseen historical and current completed common one-hour window, and checksum-bind the lineage. Each window requires the same 60 OHLCV minutes for all roots plus eligible BBO/MBP, rejects saturation/future evidence, and records `available_at` as the maximum relevant clock. `datafetching/cme_cross_asset_context.py:250`, `datafetching/cme_cross_asset_context.py:296`, `datafetching/cme_cross_asset_context.py:370`
7. **Confirmed:** publish the five-minute L2 state with event timestamps no later than the snapshot boundary and fetched timestamps no later than a separate `available_not_after` cutoff. Strict mode rejects any missing configured stream or `STALE` row rather than advancing the pointer. `datafetching/cme_history.py:288`, `datafetching/cme_history.py:484`, `datafetching/cme_history.py:532`

No additional owned worker exists. Query splitting is an internal queue, not a production loop.

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Raw and normalized CME event history | This loop’s derivations; possible read-only/research consumers | `pools/cme/events/databento/.../events.parquet` | OHLCV/BBO/MBP records; provider event/receive and local receipt clocks; normalized IDs | Bounded immutable/idempotent partitions; saturated parent request is never published as complete | **Confirmed.** `datafetching/cme_history.py:173`, `datafetching/cme_runtime.py:492`, `datafetching/cme_runtime.py:508` |
| Per-schema successful cursor | This loop | `pools/cme/runtime/cursors/*.json` | queried-through boundary, last event, success time and row count | Atomic JSON; only after the schema’s exact ranges succeed | **Confirmed.** `datafetching/cme_history.py:131`, `datafetching/cme_history.py:162`, `datafetching/cme_runtime.py:535` |
| Hourly cross-asset context | Directional Loop B; indirectly Strategy | `pools/cme/features/cross-asset-context/databento/1h.parquet` | NQ/ES/gold/crude returns; small-cap and tech breadth; relative spread; book imbalance; completeness/staleness and availability | Immutable natural key `(context_name, window_end, calculation_version)`; only complete causal windows | **Confirmed.** `datafetching/cme_cross_asset_context.py:24`, `datafetching/cme_cross_asset_context.py:196`, `datafetching/cme_cross_asset_context.py:221`, `datafetching/cme_cross_asset_context.py:250` |
| Five-minute L2 snapshot, manifest, receipt and pointer | No production-loop consumer located; supporting/current-state artifact | `pools/cme/snapshots/l2/databento/5m/<target_ns>/`; pointer `pools/cme/snapshots/l2/databento/5m/latest.json` | latest causal book/BBO rows; event/receipt ages; `FRESH`/`STALE`; configured-symbol and cursor lineage | Immutable target directory and checksum receipt, then atomic pointer; strict mode reuses an exact target only if every configured stream is still present and fresh | **Confirmed.** `datafetching/cme_history.py:288`, `datafetching/cme_history.py:315`, `datafetching/cme_history.py:419`, `datafetching/cme_history.py:532` |
| Failure records | Operators/diagnostics | datastore error authority | group/schema or derived-stage error and time | Failure is recorded per schema/stage; successful lanes continue | **Confirmed.** `datafetching/cme_runtime.py:168`, `datafetching/cme_runtime.py:189` |

## Direct loop relationships

### Upstream

**Confirmed:** no other production loop is upstream. Databento is an external provider; this loop’s own cursor/history is state, not another loop.

### Downstream

**Confirmed:** Directional Loop B directly reads the hourly context and joins the `cme__` family by causal availability/freshness. Strategy receives the same values only indirectly because Loop B copies context into its samples/candidates. `ml/rolling_materialization.py:796`, `ml/rolling_materialization.py:846`, `ml/datasets/families.py:517`, `ml/strategy_selection/model.py:132`

### Timing and control relationships

**Confirmed:** CME runs independently at sub-minute schema phases. There is no readiness barrier between it and Loop A or Loop B; Loop B consumes the latest causally eligible context at its own cutoff. `datafetching/cme_runtime.py:40`, `datafetching/cme_runtime.py:45`, `ml/rolling_materialization.py:796`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | CME events → hourly causal context → Loop B `cme__` features → calibrated directional probability. `datafetching/cme_cross_asset_context.py:196`, `ml/rolling_materialization.py:796`, `ml/runtime_pipeline.py:480` |
| Option-pricing predictions | None | No Pricing input reader for CME event/context/L2 artifacts was found. |
| Options-strategy predictions | Indirect | CME context → Loop B samples/predictions → Strategy context features and profitable-outcome score. `ml/strategy_runtime.py:125`, `ml/strategy_selection/model.py:132` |

**Roll-up classification: Both.** This follows evidenced paths to horizon and strategy outputs; it does not imply a CME path to contract pricing.

## Failure and degradation behavior

- A second owner is rejected by `.ducketz-cme-writer.lock`; the shared helper
  may reclaim the lock once only when its recorded PID is dead.
- A provider or persistence failure is recorded per group/schema. Other due
  schemas can continue, but the failed schema cursor does not advance until all
  exact chunks for that query range verify.
- A deep historical gap cannot block or dilute current L2 authority. The exact
  short current lane must pass configured-symbol completeness and strict
  freshness first; only one older recovery chunk is eligible afterward.
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

## Runtime and datastore observation

**Confirmed deployment contract:** CME is one of the eight independently
scheduled owners. Hidden launch uses the guardian allowlist, resolved paths,
redirected monitor-visible logs, and the worker-owned
`.ducketz-cme-writer.lock`; no archive command becomes another owner.
`docs/datafetch-ml/start_all_loops.ps1:18`, `ml/system_guardian.py:81`

**Observed 2026-08-19 22:45:36 UTC:** the hourly monitor found one CME
launcher/worker pair, matching live lock, and active primary logs. The current
immutable L2 authority
`pools/cme/snapshots/l2/databento/5m/1787145000000000000` passed its manifest,
receipt, and output checksum contract. Its 18 observed rows were exclusively
`GLBX.MDP3` BBO/MBP evidence, all `FRESH`, with complete configured stream
coverage. `GCZ6` was the gold contract observed in that publication; dated
contract symbols are runtime selection evidence, not evergreen architecture.
The 22:59:29 UTC read-only follow-up verified the same current authority and
continued owner health.


## Evidence index

- `datafetching/cme_runtime.py:40`
- `datafetching/cme_runtime.py:356`
- `datafetching/cme_runtime.py:382`
- `datafetching/cme_runtime.py:535`
- `datafetching/cme_history.py:131`
- `datafetching/cme_history.py:295`
- `datafetching/cme_cross_asset_context.py:196`
- `ml/rolling_materialization.py:796`
