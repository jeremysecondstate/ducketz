# Loop A

## Identity

- Canonical name: Loop A
- Logical aliases or numbering: startup owner 2; fetching/orchestration loop
- Runtime entry point: `python -m datafetching.orchestrate`
- Owning package: `datafetching`
- Classification: Independent production loop
- Scheduling mechanism: execute immediately, then sleep to the next 15-minute UTC boundary and apply a 20-second pre-start pause
- Cadence and phase: 15 minutes; effective recurring start about boundary +20 seconds
- Lock or single-writer mechanism: `.ducketz-orchestration.lock` plus `.ducketz-loop-a-cycle.lock` OS lock shared with Directional Loop B
- Primary code evidence: **Confirmed.** `datafetching/orchestrate.py:38`, `datafetching/orchestrate.py:169`, `datafetching/orchestrate.py:172`, `datafetching/orchestrate.py:243`, `datafetching/loop_a_cycle.py:199`

## Purpose

**Confirmed:** Loop A is the equity/provider and calculated-feature owner. It ingests the six-symbol watchlist across Databento, FMP, current FRED, Schwab, and SEC; builds fundamental, technical, and signal products; publishes an early exact-bar readiness barrier for Pricing/Options; and later publishes a complete-cycle boundary for Directional Loop B. `docs/datafetch-ml/current_start_command:50`, `docs/datafetch-ml/current_start_command:55`, `datafetching/orchestrate.py:326`, `datafetching/orchestrate.py:389`

**Confirmed non-ownership:** in production it does not own CME/L2, option-chain capture, ALFRED-vintage history, option-pricing inference, directional fitting/scoring, or Strategy. Inline CME/Options modes are compatibility paths and default to `external`. `datafetching/orchestrate.py:90`, `datafetching/orchestrate.py:96`

**Startup/bootstrap boundary:** Loop A's operational equity dataset is
`EQUS.MINI`, whose Historical endpoint is current during the session and whose
Live endpoint is entitled. Each native schema overlaps from its own latest
stored operational timestamp under `C:\DATASTORE\stocks`. The separate
eight-year `XNAS.ITCH` tree under
`C:\DATASTORE\market-data\databento\us-equities` remains cold provider
provenance. `materialize_equity_archive_baseline` is used only when archive and
operational dataset identities match; otherwise the recurring path explicitly
skips that bridge instead of timestamp-merging venue-specific XNAS rows with
consolidated EQUS rows. The 2026-08-19 switch is checksum-receipted under
`C:\DATASTORE\catalog\migrations`. `datafetching/databento_archive.py:213`,
`datafetching/databento_fetch.py:544`,
`datafetching/equity_dataset_migration.py`

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Production symbols | checked-in watchlist or explicit CLI override | `datafetching/watchlist.txt` / CLI | uppercase equity symbols; production scope AAPL, AMZN, GOOG, MU, NVDA, SNDK | resolved once before supervisor starts | Required, nonempty | **Confirmed.** `docs/datafetch-ml/current_start_command:16`, `datafetching/orchestrate.py:133` |
| Equity OHLCV | Databento `EQUS.MINI` | provider specs for native/resampled analysis bars | OHLCV, timeframe, bar/event/end/receipt clocks, adjustment/source identity | Databento is forced first; exact completed native one-minute bars govern readiness; prematurely ended transport responses use the bounded transient-retry policy | Required for readiness and directional targets; other timeframes depend on profile | **Confirmed.** `datafetching/orchestrate.py:268`, `app/services/databento_retry.py:14`, `app/services/market_fetch_specs.py:92`, `app/services/market_fetch_specs.py:121`, `datafetching/decision_time.py:347` |
| Cold US-equity archive | One-shot Databento archive coordinator | provenance under `market-data/databento/us-equities/XNAS.ITCH`; operational EQUS targets under `stocks` | source receipts/checksums and independent operational migration receipts | different dataset identities remain separate; a bridge is eligible only when identities match | Not a recurring Loop A input while operational dataset is `EQUS.MINI` | **Confirmed.** `datafetching/databento_archive.py:213`, `datafetching/databento_fetch.py:544`, `datafetching/equity_dataset_migration.py:537` |
| Corporate/fundamental/macro/commodity metadata | FMP | stable provider routes | statements, ratios, market cap, filing metadata, energy/commodity context and provider timestamps | point-in-time statement/publication/receipt rules; current values cannot be presumed historical | Required only for configured feature families; lane failures count toward cycle failure | **Confirmed.** `datafetching/main.py:14`, `datafetching/orchestrate.py:389`, `ml/feature_registry.py:659` |
| Current FRED observations | FRED CSV lane | GDP/CPIAUCSL/UNRATE/FEDFUNDS normalized Parquets | current/revised values, observation and fetch/availability clocks | prospective-rate/monitoring only; not ALFRED historical Loop B evidence | One eligible source for Pricing's required causal live FRED/ALFRED rate; never historical macro authority | **Confirmed.** `datafetching/fred_fetch.py:64`, `ml/option_pricing/causal.py:264`, `docs/datafetch-ml/current_start_command:39` |
| Equity quotes | Schwab | batched quote endpoint; price-history remains an explicit compatibility/manual mode | bid/ask/mid, quote event/receipt, spread, quote-quality flags | receipt-bound, quote staleness and crossed/locked quality | Required for active quote/liquidity; recurring Loop A does not refresh Schwab OHLCV | **Confirmed.** `datafetching/schwab_fetch.py:113`, `datafetching/orchestrate.py:243`, `ml/datasets/families.py:383` |
| Filing document text/events | SEC, with FMP metadata | normalized corporate/SEC paths | accepted/receipt/extraction clocks; dilution/offering/filing impulse and size ratio | `available_at` cannot precede any component clock | Optional by horizon/profile; failures count | **Confirmed.** `datafetching/main.py:21`, `ml/datasets/families.py:1051`, `ml/datasets/families.py:1108` |
| Prior canonical Parquets | Loop A prior cycles | datastore provider/category/symbol paths | stable natural keys and IDs, current-revised versions, `available_at` | idempotent continuation/upsert; exact temporal keys normalized to UTC | Optional bootstrap state; required for incremental continuity when present | **Confirmed.** `datafetching/parquet_store.py:106`, `datafetching/parquet_store.py:468`, `datafetching/parquet_store.py:1006` |

## Processing and decisions

1. **Confirmed:** acquire the supervisor lock, then the shared datastore-cycle lock; publish a new `WRITING` generation with symbol/provider scope. `datafetching/orchestrate.py:169`, `datafetching/orchestrate.py:174`, `datafetching/loop_a_cycle.py:75`
2. **Confirmed:** compute the calendar-owned target decision. Only eligible XNYS regular-session quarter-hour targets can receive readiness; a closed/non-actionable cycle reports `target=NONE` and creates no readiness. `datafetching/orchestrate.py:277`, `datafetching/orchestrate.py:300`, `datafetching/bar_readiness.py:94`
3. **Confirmed:** run Databento `EQUS.MINI` first across the watchlist. Each native request begins at an overlap of the latest same-dataset operational timestamp rather than rebuilding history. The different-dataset XNAS cold archive remains provenance-only. Prematurely ended responses are transient-retry eligible; authentication, entitlement, malformed-request, and validation failures are immediate. `datafetching/databento_fetch.py:544`, `app/services/databento_retry.py:14`, `app/services/databento_retry.py:24`
4. **Confirmed:** the one-minute completion callback attempts all-symbol readiness before unrelated providers and calculated stages. `publish_bar_readiness` resolves the exact completed one-minute bar and close, constructs semantic checksums, writes private manifest/receipt files, renames the immutable directory, verifies it, and updates the pointer. A missing exact row may use provider-aware bounded recovery; contradictory/corrupt readiness does not. Failure leaves Pricing to its own deadline and does not abort the remaining Loop A work. `datafetching/orchestrate.py:300`, `datafetching/orchestrate.py:344`, `datafetching/orchestrate.py:395`, `datafetching/bar_readiness.py:82`
5. **Confirmed:** fetch the remaining provider lanes in batched watchlist form; shared FRED runs once for the first symbol. Normalize/upsert data and errors. `datafetching/main.py:214`, `datafetching/main.py:217`, `datafetching/parquet_store.py:290`
6. **Confirmed:** run fundamental calculations when FMP is configured, then technicals, then cross-domain signals for each symbol. A nonzero stage exit increments failure count. `datafetching/orchestrate.py:389`, `datafetching/orchestrate.py:409`, `datafetching/orchestrate.py:429`
7. **Confirmed:** publish terminal `COMPLETE` only when failure count is zero; otherwise `FAILED`. `.ducketz-loop-a-complete.json` remains the last successful generation. `datafetching/loop_a_cycle.py:118`, `datafetching/loop_a_cycle.py:127`
8. **Confirmed:** release the shared lock, wait for the next boundary, then pause 20 seconds. `datafetching/orchestrate.py:196`, `datafetching/orchestrate.py:209`

The one-shot provider/fundamental/technical/signal CLIs are owned stages, not independent production loops. `tests/test_loop_a_orchestration.py:19`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Exact bar readiness | Active Pricing; Options Capture | `loop-a/bar-readiness/<target_ns>/{readiness.json,receipt.json}` plus latest pointer | target, `ready_at`, Loop A generation, symbol scope; per symbol exact bar timestamp, provider, timeframe, source file, positive close, row checksum | immutable all-symbol target; checksum verified; late/future/corrupt receipts fail closed | **Confirmed.** `datafetching/bar_readiness.py:34`, `datafetching/bar_readiness.py:82`, `datafetching/bar_readiness.py:181` |
| Current cycle state | Directional Loop B; Loop A/operator | `.ducketz-loop-a-cycle.json` | generation, `WRITING`/`COMPLETE`/`FAILED`, start/finish, symbols/providers, failure count | atomically replaced for current state; B requires this exact current record to be `COMPLETE` | **Confirmed.** `datafetching/loop_a_cycle.py:15`, `datafetching/loop_a_cycle.py:136` |
| Latest complete-cycle authority | Options regime cutoff; independent readers | `.ducketz-loop-a-complete.json` | last zero-failure generation and `finished_at` causal input cutoff | advances only on `COMPLETE`; failed/current writing cycles do not replace it | **Confirmed.** `datafetching/loop_a_cycle.py:127`, `datafetching/loop_a_cycle.py:153`, `datafetching/options_runtime.py:399` |
| Provider raw/normalized Parquets | Directional Loop B; Pricing/Options/Strategy where relevant; calculations | provider/category/symbol trees under datastore | OHLCV, quotes, statements, FRED current values, SEC records, timestamps, natural IDs, status/error rows | canonical idempotent upsert; temporary file atomic replace | **Confirmed.** `datafetching/parquet_store.py:106`, `datafetching/parquet_store.py:530` |
| Conditional equity archive lineage | Operators and later same-dataset archive materialization | operational `.archive-lineage.json` beside a materialized Loop A target | archive dataset/live dataset, selected source paths and SHA-256 values, target path, row bounds/counts, creation time | source checksums and dataset identity must reverify; current XNAS/EQUS mismatch produces no materialization edge | **Confirmed.** `datafetching/databento_archive.py:213`, `datafetching/databento_fetch.py:544` |
| Calculated fundamental, technical, signal, energy, quote and SEC feature products | Directional Loop B; stock quote subset also Strategy | symbol/pool calculated feature paths | registered feature values, calculation/schema versions, component `available_at`, quality and lifecycle status | written by owned calculation stages; complete-cycle pointer states whether the whole generation succeeded | **Confirmed.** `datafetching/orchestrate.py:389`, `ml/feature_registry.py:545`, `ml/feature_registry.py:626`, `ml/feature_registry.py:848` |
| Prospective current FEDFUNDS rate context | Active Pricing rate loader | `pools/macro/features/prospective-release-context/fred/*.parquet` when bridge exists | FEDFUNDS level and local receipt availability; other macro values null | valid only for decisions after its actual receipt; never rewritten as historical | **Confirmed.** `datafetching/fred_vintages.py:456`, `datafetching/fred_vintages.py:541`, `ml/option_pricing/rates.py:369` |

## Direct loop relationships

### Upstream

**Confirmed:** no production loop supplies a direct Loop A input. Its upstreams are external providers and its own prior canonical state. CME and Options are independent despite compatibility switches.

### Downstream

- **Active Pricing:** exact readiness/close and prospective rate context. `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1186`
- **Options Capture:** exact readiness/decision clocks, last complete regime cutoff, daily bars for realized volatility. `datafetching/options_runtime.py:266`, `datafetching/options_runtime.py:335`, `options/features.py:335`
- **Directional Loop B:** complete-cycle control barrier and all registered Loop A families. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:272`
- **Strategy:** stock quote-liquidity history used at entry/exit. `ml/strategy_selection/chain.py:151`

### Timing and control relationships

**Confirmed:** Pricing can consume readiness before Loop A completes, whereas Loop B cannot read until the full cycle finishes and the shared lock transfers. Options can commit causally clocked OPRA without readiness or quarantine a single Schwab response pending readiness. `datafetching/orchestrate.py:267`, `ml/prediction_runtime.py:209`, `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:452`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | Loop A complete generation + equity/fundamental/technical/signal data → Loop B feature/sample construction → directional probability. `datafetching/loop_a_cycle.py:127`, `ml/rolling_materialization.py:272`, `ml/runtime_pipeline.py:480` |
| Option-pricing predictions | Indirect | exact bar readiness/underlying close and causal rate context → Active Pricing Black–Scholes/residual inputs → contract values. `datafetching/bar_readiness.py:120`, `ml/option_pricing_runtime.py:1181` |
| Options-strategy predictions | Indirect | Loop A → Loop B probabilities/context and stock BBO → Strategy candidate/outcome model and rank. `ml/strategy_runtime.py:125`, `ml/strategy_selection/chain.py:151` |

**Roll-up classification: Both.**

## Failure and degradation behavior

- `.ducketz-orchestration.lock` rejects a second Loop A supervisor. It is a
  separate `O_EXCL` implementation without stale-PID recovery; the shared
  `.ducketz-loop-a-cycle.lock` is OS-held and releases when its owner exits even
  though the marker file remains.
- Bar-readiness publication is attempted only from the completed Databento
  all-symbol fast lane. A publish failure is reported and later Loop A stages
  may continue, but no readiness receipt is synthesized.
- Premature stream termination is a bounded transient retry condition. Provider
  identity, entitlement, malformed request, validation, or readiness-integrity
  failures remain fail-closed and are not relabeled as transport recovery.
- Any provider/calculation failure increments the cycle failure count and makes
  the current `.ducketz-loop-a-cycle.json` `FAILED`. Only a zero-failure cycle
  advances `.ducketz-loop-a-complete.json`.
- Independent readers may retain the prior latest-complete record while a new
  cycle is writing or failed. Directional Loop B deliberately requires the
  current cycle to be `COMPLETE` under the shared lock, so a current failed
  cycle aborts that B attempt rather than silently substituting older inputs.


## Accuracy and efficiency relevance

- Exact target identity, all-symbol scope, row semantic checksums and receipt
  verification protect the close used by Pricing and Options.
- Publishing readiness immediately after the 1-minute Databento lane removes
  slower FMP/FRED/SEC/calculation work from Pricing’s critical path; full-cycle
  completion still protects Loop B’s wider feature read.
- CME and option-chain work remain external in production, preventing their
  provider cadence and writer locks from extending Loop A ownership.


## Conflicts, gaps, and uncertainty

- `--cme-mode inline` and `--options-mode inline` remain executable
  compatibility paths, but the supported production command selects
  `external`. They are not additional owners and must not be used to absorb the
  CME or Options runtimes.
- The readiness and complete-cycle authorities are intentionally different.
  Readiness does not claim the rest of Loop A succeeded; complete-cycle state
  does not retroactively manufacture a missed readiness receipt.

## Runtime and datastore observation

**Confirmed deployment contract:** the checked-in launcher and guardian use the
closed canonical command, hidden window, explicit work directory, redirected
logs, and one launcher/worker pair whose worker owns
`.ducketz-orchestration.lock`. Existing valid ownership is audited and retained;
command drift alone does not authorize a second owner or an improvised restart.
`docs/datafetch-ml/start_all_loops.ps1:18`, `ml/system_guardian.py:81`

**Observed 2026-08-19 22:45:36 UTC:** one Loop A pair, its live worker lock,
primary logs, and exact six-symbol readiness for target
`2026-08-19T20:00:00Z` passed. The checksum-valid receipt is
`loop-a/bar-readiness/1787169600000000000/receipt.json`. The monitor correctly
reported an active later `WRITING` generation while retaining
`20260819T223020.010102Z-pid5516` as `last_complete_generation`; that generation
finished at 22:35:52 UTC with zero failures and all six symbols. Active cycle
state is liveness evidence, not permission to substitute it for completed
authority.

The observed raw one-minute files for AAPL, AMZN, GOOG, MU, NVDA, and SNDK each
contained only `provider_dataset=EQUS.MINI` and
`source_schema=ohlcv-1m`. Schwab remained quote/chain/broker evidence, not
canonical operational OHLCV, and the cold `XNAS.ITCH` archive remained
separate. At 22:59:29 UTC the read-only monitor had verified a newer complete
Loop A generation, `20260819T224520.001083Z-pid5516`, with the same zero-failure
contract.


## Evidence index

- `datafetching/orchestrate.py:84`
- `datafetching/orchestrate.py:169`
- `datafetching/orchestrate.py:267`
- `datafetching/orchestrate.py:389`
- `datafetching/loop_a_cycle.py:15`
- `datafetching/loop_a_cycle.py:127`
- `datafetching/loop_a_cycle.py:199`
- `datafetching/bar_readiness.py:82`
- `tests/test_independent_loop_isolation.py:17`
- `tests/test_loop_a_cycle.py:36`
