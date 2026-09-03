# Daily ALFRED runtime

## Identity

- Canonical name: Daily ALFRED runtime
- Logical aliases or numbering: startup owner 3; FRED/ALFRED daily owner
- Runtime entry point: `python -m datafetching.fred_alfred_runtime`
- Owning package: `datafetching`
- Classification: Independent production loop
- Scheduling mechanism: run immediately, then schedule the next configured UTC-hour boundary; successful import at most once per UTC date
- Cadence and phase: daily, production `--utc-hour 7`
- Lock or single-writer mechanism: `.ducketz-fred-alfred-import.lock` through stale-owner-aware exclusive lock
- Primary code evidence: **Confirmed.** `datafetching/fred_alfred_runtime.py:47`, `datafetching/fred_alfred_runtime.py:127`, `datafetching/fred_alfred_runtime.py:150`, `datafetching/fred_alfred_runtime.py:156`

## Purpose

**Confirmed:** this loop incrementally maintains the historical point-in-time macro authority needed by Directional Loop B and Pricing. It fetches exact ALFRED realtime/vintage intervals for four series, seals provider evidence, persists immutable vintage identities and derived macro release context, proves causal coverage against the current Loop B decision grid, and publishes separate readiness and daily-completion pointers. `datafetching/fred_alfred_runtime.py:69`, `datafetching/fred_vintage_import.py:147`, `datafetching/fred_alfred_readiness.py:185`

**Confirmed non-ownership:** it is not part of Loop A’s 15-minute cycle and does not publish directional, option-price, or strategy predictions. `docs/datafetch-ml/current_start_command:64`, `docs/datafetch-ml/current_start_command:67`

**Startup/bootstrap boundary:** this owner does not self-initialize from an
empty datastore. A base/earlier-profile Loop B decision grid must exist before
the one-time complete ALFRED backfill can derive causal bounds and publish its
separate readiness receipt. Only then does the daily owner continue with
bounded overlap under the existing ALFRED lock; the backfill is maintenance,
not another loop.

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| ALFRED series/vintage API | FRED/ALFRED | `series/vintagedates` and observation endpoints | series `FEDFUNDS`, `CPIAUCSL`, `UNRATE`, `GDP`; observation date; realtime start/end; value/unit/frequency | provider date-precision realtime start becomes available conservatively at next Chicago midnight; bounded retry/pagination | All four required for complete context/readiness | **Confirmed.** `datafetching/fred_vintage_import.py:37`, `datafetching/fred_vintage_import.py:415`, `datafetching/fred_vintage_import.py:630`, `datafetching/fred_vintages.py:767` |
| Prior canonical ALFRED vintages | This loop / one-time bootstrap | `pools/macro/macro-vintages/fred/<series>/*.parquet` | natural key `(series_name, observation_date, realtime_start, realtime_end)`, revision identity, release/fetch/available clocks, value | first local receipt retained for stable provider vintage; changed identity fails closed | Required for incremental mode; bootstrap backfill required first | **Confirmed.** `datafetching/fred_vintages.py:24`, `datafetching/fred_vintages.py:40`, `datafetching/fred_alfred_readiness.py:145`, `datafetching/fred_vintages.py:707` |
| Current Loop B decision grid | Directional Loop B | authoritative `samples.parquet` resolved through `ml/latest/run.json` | symbol, horizon, decision timestamp; horizons `1d`, `1w`, `1w-d1`…`1w-d5` | used to derive earliest required observation/realtime bounds and test causal coverage; symbol multiplicity is collapsed for shared macro evidence | Required for plan/readiness; one-time backfill bootstraps before daily loop | **Confirmed.** `datafetching/fred_alfred_readiness.py:400`, `datafetching/fred_alfred_readiness.py:405`, `datafetching/fred_alfred_readiness.py:418` |
| Prior daily runtime pointer | This loop | daily ALFRED latest pointer under `ml` authority | last successful UTC date, receipt path and checksum | if already complete for current UTC date, returns `ALREADY_COMPLETE_TODAY` without provider work | Optional on first run; authoritative if present | **Confirmed.** `datafetching/fred_alfred_runtime.py:58`, `datafetching/fred_alfred_runtime.py:63`, `datafetching/fred_alfred_runtime.py:198` |

## Processing and decisions

1. **Confirmed:** verify the daily pointer; if its successful date equals today, return idempotently. `datafetching/fred_alfred_runtime.py:58`
2. **Confirmed:** derive an incremental plan from existing coverage with a seven-day overlap and a maximum 130-day realtime span; reject a gap and direct the operator to rerun the one-time backfill. `datafetching/fred_alfred_readiness.py:135`, `datafetching/fred_alfred_readiness.py:161`, `datafetching/fred_alfred_readiness.py:169`
3. **Confirmed:** fetch all four series, restore clipped interval starts only when linked to prior sealed evidence, normalize actual vintage identity, and reject current-revised/local-receipt rows as historical evidence. `datafetching/fred_vintage_import.py:174`, `datafetching/fred_vintage_import.py:645`, `datafetching/fred_vintages.py:75`
4. **Confirmed:** seal raw provider responses, `vintages.parquet`, manifest, and receipt under `ml/option-pricing-evidence/fred-alfred-vintages/<run>`. Receipt explicitly records `current_revised_history_used=false`, coverage not yet evaluated, and automated actions disabled. `datafetching/fred_vintage_import.py:195`, `datafetching/fred_vintage_import.py:217`, `datafetching/fred_vintage_import.py:238`
5. **Confirmed:** verify the sealed import, persist immutable yearly vintage partitions, then derive release context only when all series are present. `datafetching/fred_vintage_import.py:260`, `datafetching/fred_vintage_import.py:267`, `datafetching/fred_vintage_import.py:273`
6. **Confirmed:** for every release availability clock, construct the point-in-time latest vintage snapshot and derive FEDFUNDS level, CPI YoY, one-month unemployment change, and GDP YoY with independent component availability. `datafetching/fred_vintages.py:294`, `datafetching/fred_vintages.py:324`, `datafetching/fred_vintages.py:351`
7. **Confirmed:** verify lineage, natural keys, coverage (minimum 0.95 per feature/horizon), freshness, zero lookahead, and no current-revised history; only then publish a distinct Loop B consumption authorization. `datafetching/fred_alfred_readiness.py:185`, `datafetching/fred_alfred_readiness.py:213`, `datafetching/fred_alfred_readiness.py:523`
8. **Confirmed:** publish a daily receipt binding the import and readiness checksums, then atomically advance the daily pointer. `datafetching/fred_alfred_runtime.py:82`, `datafetching/fred_alfred_runtime.py:101`, `datafetching/fred_alfred_runtime.py:264`

The importer’s pagination is internal. The one-time `ml.option_pricing_fred --backfill` is bootstrap/maintenance, not an independent loop.

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Sealed ALFRED import evidence | readiness verifier, audits | `ml/option-pricing-evidence/fred-alfred-vintages/<run>/` | raw responses, normalized vintages, request bounds, availability basis, row/series counts, checksums, automation false | immutable directory; manifest/receipt and output checksums must verify | **Confirmed.** `datafetching/fred_vintage_import.py:195`, `datafetching/fred_vintage_import.py:289` |
| Canonical vintage partitions | readiness; Loop B loader | `pools/macro/macro-vintages/fred/<series>/*.parquet` | series/revision identity, observation/realtime interval, release/fetch/available clocks, value/unit/frequency | immutable natural key; first stable receipt retained; duplicates or revision mutation rejected | **Confirmed.** `datafetching/fred_vintages.py:259`, `datafetching/fred_vintages.py:411`, `datafetching/fred_vintages.py:707` |
| ALFRED macro release context | Directional Loop B; Active Pricing rate loader | `pools/macro/features/alfred-release-context/fred/<year>.parquet` | four macro values and four component availability clocks; `available_at`, availability/calculation/schema versions | immutable natural key `(context_name, available_at, calculation_version)` | **Confirmed.** `datafetching/fred_vintages.py:342`, `datafetching/fred_vintages.py:364` |
| ALFRED readiness report/receipt/pointer | Directional Loop B | ALFRED readiness run under `ml` plus latest pointer | `PASS`, coverage by horizon/feature, lookahead count zero, input inventory/checksums, Loop B authorization true, automation false | published only after verified import, lineage and coverage; pointer is atomic and cutoff-aware reader rejects future readiness | **Confirmed.** `datafetching/fred_alfred_readiness.py:234`, `datafetching/fred_alfred_readiness.py:266`, `datafetching/fred_alfred_readiness.py:300`, `datafetching/fred_alfred_readiness.py:667` |
| Daily runtime receipt/pointer | next daily cycle/operators | daily ALFRED authority under `ml` | UTC run date, status `COMPLETE`, series, plan bounds, import/readiness paths and checksums | one successful receipt per UTC date; pointer verification requires authorization true and both safety flags false | **Confirmed.** `datafetching/fred_alfred_runtime.py:82`, `datafetching/fred_alfred_runtime.py:198`, `datafetching/fred_alfred_runtime.py:223` |

## Direct loop relationships

### Upstream

**Confirmed:** Directional Loop B directly supplies the current decision grid used to derive historical bounds and coverage. This is asynchronous historical feedback, not a same-cycle dependency. `datafetching/fred_alfred_readiness.py:400`

### Downstream

- **Directional Loop B:** consumes only readiness-authorized vintage/context evidence for daily/weekly macros. `ml/rolling_materialization.py:740`
- **Active Pricing:** live construction requires a strictly prior FRED/ALFRED rate observation; its rate loader reads ALFRED release context, and the owned worker uses the same causal FEDFUNDS evidence for residual materialization. Provider/FMP rate substitution is disabled live. `ml/option_pricing/causal.py:264`, `ml/option_pricing/causal.py:450`, `ml/option_pricing/rates.py:369`, `ml/option_pricing_loop_native_worker.py:54`

### Timing and control relationships

**Confirmed:** the daily update is independent of quarter-hour phases. A future Loop B run may use the new readiness only if it was published no later than Loop B’s causal cutoff. `datafetching/fred_alfred_readiness.py:334`

**Confirmed bootstrap cycle:** the daily incremental planner requires existing four-series history and Loop B decisions; startup therefore mandates a one-time backfill before the owner. `datafetching/fred_alfred_readiness.py:149`, `docs/datafetch-ml/current_start_command:21`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | ALFRED vintages/readiness → daily/weekly `macro__` feature joins → Loop B model → calibrated horizon probabilities. `ml/rolling_materialization.py:740`, `ml/runtime_pipeline.py:480` |
| Option-pricing predictions | Indirect | ALFRED FEDFUNDS context → causal decimal risk-free rate → Pricing Black–Scholes/residual features → target contract values. `ml/option_pricing/rates.py:361`, `ml/option_pricing/rates.py:236`, `ml/option_pricing_runtime.py:1189` |
| Options-strategy predictions | Indirect | macro features influence Loop B probability/context; Pricing rate influences leg fair values; both feed Strategy candidate scoring. `ml/strategy_selection/model.py:130`, `ml/strategy_selection/runtime.py:288` |

**Roll-up classification: Both.**

## Failure and degradation behavior

- Missing `FRED_API_KEY`, a conflicting
  `.ducketz-fred-alfred-import.lock`, provider/import failure, or readiness
  verification failure makes the daily attempt fail without advancing its
  verified receipt/readiness pointers.
- The lock is shared with `ml.option_pricing_fred --backfill` and can reclaim a
  dead recorded PID once. This prevents daily maintenance and the one-time
  backfill from writing the same vintage authority concurrently.
- Incremental planning requires all four series, the authoritative Loop B
  decision grid, and a bounded overlap. A gap outside that safe overlap fails
  closed and requires the complete backfill rather than widening history
  silently.
- The supervisor runs immediately at process start, then at most once per UTC
  date. Repeated same-day wakeups do not manufacture another daily authority.


## Accuracy and efficiency relevance

- Leakage prevention: conservative date-only availability, immutable revisions, no current-revised history, explicit causal coverage/lookahead verification. `datafetching/fred_vintages.py:767`, `datafetching/fred_alfred_readiness.py:494`
- Feature quality: independent component clocks and series-specific freshness/lag bounds. `datafetching/fred_vintages.py:351`, `datafetching/fred_alfred_readiness.py:111`
- Target/model integrity: plan is derived from actual Loop B decision grids and registered horizons. `datafetching/fred_alfred_readiness.py:400`
- Provider volume/efficiency: once-daily idempotence, bounded overlap, pagination and reuse of prior vintages. `datafetching/fred_alfred_runtime.py:53`, `datafetching/fred_alfred_readiness.py:135`
- Storage I/O: yearly immutable feature partitions and stable-identity replay suppression. `datafetching/fred_vintages.py:383`, `datafetching/fred_vintages.py:707`

## Conflicts, gaps, and uncertainty

- The one-time ALFRED backfill cannot bootstrap from an empty datastore because
  its request/readiness bounds come from an existing authoritative Loop B
  `samples.parquet`. A base/earlier-profile Loop B generation is therefore an
  explicit setup prerequisite before the v3 macro profile starts.
- Repository code establishes the four-series contract and readiness checks,
  but not current API entitlement, actual datastore coverage, or current
  ≥95% readiness. Those remain receipt/health facts.

## Runtime and monitoring observation

**Confirmed deployment contract:** Daily ALFRED is one of the eight independent
hidden owners, even though its work is at most once per UTC date. The canonical
launcher/guardian command is unbuffered, uses an explicit working directory and
redirected monitor-visible logs, and requires one launcher/worker pair whose
worker owns `.ducketz-fred-alfred-import.lock`. Existing valid ownership is not
duplicated. `docs/datafetch-ml/start_all_loops.ps1:18`,
`ml/system_guardian.py:81`

Hourly monitoring verifies process/lock/log state and the current daily
pointer. Daily and weekly layers additionally verify full vintage/importer
lineage, minimum coverage, and lookahead guards; a freshness failure is not
silenced by a live process. `ml/system_monitor.py:164`

**Observed 2026-08-19 22:45:36 UTC:** exactly one ALFRED launcher/worker pair,
its matching worker lock, active primary logs, and its current publication
contract passed. The current checksum-valid daily receipt was
`ml/fred-alfred-runtime/20260819T070003.938741Z/receipt.json`. The 22:59:29 UTC
read-only follow-up verified the same daily authority. These timestamped facts
do not guarantee the next provider update or future API entitlement.


## Evidence index

- `datafetching/fred_alfred_runtime.py:47`
- `datafetching/fred_vintage_import.py:147`
- `datafetching/fred_vintages.py:24`
- `datafetching/fred_vintages.py:294`
- `datafetching/fred_alfred_readiness.py:135`
- `datafetching/fred_alfred_readiness.py:185`
- `ml/option_pricing/rates.py:361`
- `ml/rolling_materialization.py:740`
- `tests/test_fred_alfred_causal_pipeline.py:116`
- `tests/test_fred_alfred_causal_pipeline.py:442`
