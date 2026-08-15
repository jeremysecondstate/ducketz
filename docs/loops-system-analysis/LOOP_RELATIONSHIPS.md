# Loop relationships

## Relationship conventions

The catalog includes only direct artifact/control exchanges or an explicit phase contract. Two loops reading the same datastore without consuming one another’s artifact is not a relationship. `D` = data, `C` = readiness/control, `T` = timing, `M` = model, `F` = fallback, and `H` = historical-only/asynchronous feedback.

## Direct relationship catalog

### R1 — CME/L2 → Directional Loop B

- **Status:** Confirmed.
- **Type:** D; optional-by-freshness but active in registered horizon profiles.
- **Exchange:** `pools/cme/features/cross-asset-context/databento/1h.parquet`; values `nq_return`, `es_return`, RTY-minus-ES, NQ-minus-ES, gold/crude returns, relative spread, book imbalance, quality/completeness and availability clocks.
- **Availability:** common completed 60-minute window; no future evidence; maximum 15-minute source staleness for context construction and horizon-specific feature freshness.
- **Consumer behavior:** joins by causal availability; stale/quality-failed values become unavailable. If no usable source exists, affected routes may fail, but no stale substitution is authorized.
- **Producer evidence:** `datafetching/cme_cross_asset_context.py:24`, `datafetching/cme_cross_asset_context.py:174`, `datafetching/cme_cross_asset_context.py:277`
- **Consumer evidence:** `ml/rolling_materialization.py:782`, `ml/datasets/families.py:517`, `ml/feature_registry.py:499`

### R2 — Loop A → Active Pricing

- **Status:** Confirmed.
- **Type:** D + C.
- **Exchange:** exact all-symbol bar-readiness receipt/row checksums and each target close; completed Databento bars; prospective current-FRED rate context when available.
- **Availability:** exact eligible quarter-hour; readiness must become authoritative within the configured 30-second production wait and no later than the 1,200-second causal window.
- **Consumer behavior:** bounded wait/retry; deadline causes a write-free skip and leaves prior Pricing authority unchanged.
- **Producer evidence:** `datafetching/orchestrate.py:292`, `datafetching/bar_readiness.py:120`, `datafetching/fred_vintages.py:600`
- **Consumer evidence:** `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1181`, `ml/option_pricing/rates.py:361`, `ml/option_pricing_runtime.py:1713`

### R3 — Loop A → Options Capture

- **Status:** Confirmed.
- **Type:** D + C.
- **Exchange:** exact bar-readiness decision clock/close for commit authority; latest complete Loop A time as the regime-data cutoff; persisted daily equity bars for realized volatility.
- **Availability:** exact target readiness is required for production commit. A missing receipt permits only checksum-sealed pending capture; closed-market discovery can reconstruct exact clocks from persisted bars.
- **Consumer behavior:** commit, pending/quarantine, later reconcile, or terminal expiry; it does not fabricate readiness.
- **Producer evidence:** `datafetching/bar_readiness.py:50`, `datafetching/loop_a_cycle.py:153`, `datafetching/orchestrate.py:326`
- **Consumer evidence:** `datafetching/options_runtime.py:266`, `datafetching/options_runtime.py:335`, `datafetching/options_runtime.py:454`, `options/features.py:335`

### R4 — Loop A → Directional Loop B

- **Status:** Confirmed.
- **Type:** D + C + shared single-reader/writer lock.
- **Exchange:** `.ducketz-loop-a-complete.json` and its `finished_at` causal cutoff; normalized equity bars/quotes, technicals, fundamentals, signals, current nonhistorical contexts, energy and SEC evidence.
- **Availability:** Loop B acquires the shared datastore-cycle lock and requires a complete Loop A cycle; `input_available_at` is the complete cycle’s finish time.
- **Consumer behavior:** missing/noncomplete cycle fails cleanly; failed feature routes may be partial under the production default, while shared-authority corruption aborts the run.
- **Producer evidence:** `datafetching/loop_a_cycle.py:127`, `datafetching/loop_a_cycle.py:198`, `datafetching/orchestrate.py:389`
- **Consumer evidence:** `ml/prediction_runtime.py:209`, `ml/prediction_runtime.py:218`, `ml/rolling_materialization.py:272`

### R5 — Loop A → Strategy

- **Status:** Confirmed.
- **Type:** D; shared artifact without direct control coordination.
- **Exchange:** Schwab stock quote-liquidity Parquets (`symbol`, bid, ask, `available_at`, quality policy/status) used for entry/exit stock legs and candidate risk/quality.
- **Availability:** quotes are bounded by Strategy’s run/entry/exit cutoffs and validated for schema, natural key, and quality policy.
- **Consumer behavior:** missing stock quote returns `None`; option-only candidates can still be considered, while stock-dependent quality/risk evidence degrades or prevents affected constructions.
- **Producer evidence:** `datafetching/main.py:257`, `datafetching/parquet_store.py:121`
- **Consumer evidence:** `ml/strategy_selection/chain.py:151`, `ml/strategy_selection/chain.py:258`, `ml/strategy_selection/chain.py:391`

### R6 — Daily ALFRED → Active Pricing

- **Status:** Confirmed.
- **Type:** D + M.
- **Exchange:** point-in-time `FEDFUNDS` observations from `pools/macro/features/alfred-release-context/fred/*.parquet`; used as causal risk-free fallback input by fast target construction and by the owned residual-model worker.
- **Availability:** `fed_funds_available_at` must be strictly before the pricing decision/source boundary; rate is percentage points converted to decimal. Current-revised history is not eligible historical evidence.
- **Consumer behavior:** prefers a verified FMP Treasury curve if separately present; otherwise uses the latest eligible ALFRED/FRED observation; no invented rate is allowed.
- **Producer evidence:** `datafetching/fred_vintages.py:344`, `datafetching/fred_vintages.py:364`, `datafetching/fred_alfred_readiness.py:185`
- **Consumer evidence:** `ml/option_pricing/rates.py:361`, `ml/option_pricing/rates.py:397`, `ml/option_pricing/rates.py:236`, `ml/option_pricing_loop_native_worker.py:54`

### R7 — Daily ALFRED → Directional Loop B

- **Status:** Confirmed.
- **Type:** D + C.
- **Exchange:** checksum-bound readiness authorization, immutable vintages, and derived macro context for `macro__fed_funds_level`, `macro__cpi_yoy`, `macro__unemployment_change`, and `macro__gdp_yoy`, each with its own clock.
- **Availability:** active only for daily and weekly contracts; minimum 95% causal coverage, zero lookahead violations, and verified importer lineage are required.
- **Consumer behavior:** missing/invalid readiness or corrupt evidence is a shared contract failure and aborts the whole prospective Loop B publication; stale individual values become missing by their own freshness.
- **Producer evidence:** `datafetching/fred_alfred_readiness.py:185`, `datafetching/fred_alfred_readiness.py:234`, `datafetching/fred_alfred_readiness.py:667`
- **Consumer evidence:** `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:757`, `ml/rolling_materialization.py:322`, `ml/datasets/families.py:785`

### R8 — Directional Loop B → Daily ALFRED

- **Status:** Confirmed; asynchronous historical feedback, not a same-cycle barrier.
- **Type:** M + H.
- **Exchange:** current authoritative `samples.parquet` decision grid for `1d`, `1w`, and weekly component horizons. It determines the earliest required ALFRED observation/realtime bounds and readiness coverage checks.
- **Availability:** a current verified Loop B publication with eligible decisions is required to derive backfill/incremental scope.
- **Consumer behavior:** missing horizons/decisions fails plan derivation; the documented one-time backfill bootstraps the cycle before daily updates.
- **Producer evidence:** `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:794`
- **Consumer evidence:** `datafetching/fred_alfred_readiness.py:400`, `datafetching/fred_alfred_readiness.py:405`, `datafetching/fred_alfred_readiness.py:420`, `docs/datafetch-ml/current_start_command:21`

### R9 — Active Pricing → Options Capture

- **Status:** Confirmed.
- **Type:** C + F.
- **Exchange:** exact target-outcome receipt/pointer, terminal status, published time, prediction-row count, run path and receipt checksum embedded as `pricing_barrier` metadata in the Options receipt.
- **Availability:** authority must be verified and published before the Options request to receive prospective credit.
- **Consumer behavior:** blocks for at most 45 seconds in production; `MISSING`, `TIMED_OUT`, or a verified no-prediction/failure outcome does not prevent capture, but cannot receive causal Pricing-before-request credit.
- **Producer evidence:** `ml/option_pricing/target_outcome.py:93`, `ml/option_pricing_runtime.py:1305`
- **Consumer evidence:** `datafetching/pricing_barrier.py:77`, `datafetching/pricing_barrier.py:38`, `datafetching/options_runtime.py:250`

### R10 — Options Capture → Active Pricing

- **Status:** Confirmed.
- **Type:** D + M.
- **Exchange:** earlier committed canonical OPRA/explicit-Schwab-fallback chains for contract definitions, lagged IV, source BBO, residual samples and model fitting; later chains reconcile prior predictions into dollar/normalized error and interval coverage. Provider/evidence lane and `fallback_used` remain explicit.
- **Availability:** source quote/evidence must predate prediction; outcome quote/evidence must be strictly later. Natural targets and receipt chains are checksum-verified.
- **Consumer behavior:** missing/stale source produces explicit route failure/baseline absence; missing later outcome leaves evaluation pending; OPRA is preferred when available and Schwab is labeled fallback.
- **Producer evidence:** `options/publication.py:105`, `options/publication.py:402`, `options/snapshot.py:201`
- **Consumer evidence:** `ml/option_pricing/causal.py:107`, `ml/option_pricing_runtime.py:660`, `ml/option_pricing_runtime.py:675`

### R11 — Active Pricing → Directional Loop B

- **Status:** Confirmed.
- **Type:** D + F.
- **Exchange:** append-only compact `opx__` surfaces: coverage, residual, uncertainty, edge fractions, arbitrage rates, interval coverage and spread, plus provider/generation/availability provenance.
- **Availability:** every reachable Pricing generation is verified; Loop B selects the newest generation for a natural symbol/target whose first availability is no later than its causal cutoff. Freshness is 2 h/4 h/2 d/8 d by public horizon.
- **Consumer behavior:** unavailable/stale/uncovered data becomes audited null and triggers the non-Pricing baseline feature set; corrupt authority aborts publication.
- **Producer evidence:** `ml/option_pricing_runtime.py:707`, `ml/option_pricing/publication.py:36`, `ml/option_pricing/consumers.py:306`
- **Consumer evidence:** `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:676`, `ml/runtime_pipeline.py:432`

### R12 — Options Capture → Directional Loop B

- **Status:** Confirmed.
- **Type:** D.
- **Exchange:** committed `opt__` option-quality surface values and quality/timing clocks.
- **Availability:** only receipt-committed surfaces at or before the Loop B input cutoff; family-specific freshness and `surface_quality_pass` apply.
- **Consumer behavior:** exact committed history is preferred; legacy feature files are a compatibility fallback only when no committed snapshots exist. Missing/invalid family data can fail affected routes.
- **Producer evidence:** `options/features.py:217`, `options/features.py:285`, `options/publication.py:402`
- **Consumer evidence:** `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:626`, `ml/datasets/families.py:263`

### R13 — Directional Loop B → Strategy

- **Status:** Confirmed.
- **Type:** D + M + C.
- **Exchange:** authoritative Loop B source record/receipt, redacted samples, LIVE calibrated directional probabilities, target windows, feature context and causal input cutoff.
- **Availability:** Strategy reads one verified current pointer; predictions must match exactly one sample and precede entry.
- **Consumer behavior:** no valid current Loop B run, samples, or predictions fails the Strategy cycle; unchanged Loop B plus unchanged option heads/pricing mode is skipped.
- **Producer evidence:** `ml/runtime_pipeline.py:704`, `ml/runtime_pipeline.py:876`
- **Consumer evidence:** `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:81`, `ml/strategy_runtime.py:125`, `ml/strategy_runtime.py:414`

### R14 — Options Capture → Strategy

- **Status:** Confirmed.
- **Type:** D + M.
- **Exchange:** exact entry/exit Schwab chain receipts, contract BBOs, option-quality surface, quote availability, IV/Greeks/open interest/volume, and observed option outcomes.
- **Availability:** entry must be known after information availability and before target start; exit is the earliest receipt after target end within a horizon-specific delay and before any lockbox boundary.
- **Consumer behavior:** missing chain/entry/exit evidence is audited and the affected candidate/outcome is skipped rather than synthesized.
- **Producer evidence:** `options/publication.py:406`, `options/snapshot.py:499`
- **Consumer evidence:** `ml/strategy_selection/chain.py:97`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:395`

### R15 — Active Pricing → Strategy

- **Status:** Confirmed.
- **Type:** D + M + F.
- **Exchange:** exact-contract baseline predictions, one-to-one residual sidecars and verified generation history; `BSGP_SHADOW_READY` becomes Strategy source `BSGP`, while complete residual fallback becomes `BLACK_SCHOLES`. Per leg, Strategy receives fair-value edge, conservative edge, uncertainty, favorable probability, model age and residual shrinkage.
- **Availability:** Pricing evidence must be receipt-verified and available before the candidate probability; live scoring disallows offline replay.
- **Consumer behavior:** full active leg coverage admits fitted Strategy scoring; missing/delayed coverage keeps the explicit scenario-prior fallback.
- **Producer evidence:** `ml/option_pricing/target_outcome.py:93`, `ml/option_pricing/publication.py:83`, `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`
- **Consumer evidence:** `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/runtime.py:310`, `ml/strategy_selection/runtime.py:637`

### R16 — Directional Loop B → Options Capture (phase only)

- **Status:** Documented only as a relative timing relationship; no direct data/control dependency.
- **Type:** T.
- **Exchange:** none. Startup says Options starts after Loop B’s +5 information clock; code schedules B at +5 and Options at +6, but Options never reads the Loop B pointer.
- **Consumer behavior:** none; Options proceeds independently based on Loop A, Pricing barrier, pending state, and provider evidence.
- **Evidence:** `docs/datafetch-ml/current_start_command:95`, `docs/datafetch-ml/current_start_command:141`, `datafetching/options_runtime.py:584`

## Owned-worker relationships

**Confirmed:** Active Pricing launches the one-shot loop-native worker after fast target publication without blocking Options. The worker reads ALFRED/FRED rates and committed OPRA/Schwab history, publishes a local materialization/model/status under its own lock, and makes no provider request. A later Pricing cycle may load that prior model; Strategy may also read loop-native evidence as a fallback/offline replay catalog, but live Strategy disallows offline replay. `ml/option_pricing_runtime.py:418`, `ml/option_pricing_loop_native_worker.py:38`, `ml/option_pricing_loop_native_worker.py:126`, `ml/option_pricing_runtime.py:313`, `ml/strategy_selection/runtime.py:96`, `ml/strategy_selection/runtime.py:293`

## OPRA boundaries that are not loop-to-loop relationships

- **Confirmed prospective boundary:** `OptionMarketDataAdapter` is an injected `databento-opra`/`OPRA.PILLAR`/`cbbo-1s` interface inside Options Capture. It does not supervise itself and therefore adds no inventory row or dependency-matrix owner. When supplied, Options validates it, persists OPRA under its own provider identity, and uses labeled Schwab fallback on failure. The numbered CLI does not construct a concrete adapter. `options/providers.py:25`, `datafetching/options_runtime.py:362`, `datafetching/options_runtime.py:404`, `datafetching/options_runtime.py:663`
- **Confirmed historical boundary:** `ml.option_pricing_opra` is an explicitly authorized, resumable maintenance importer for `OPRA.PILLAR` definitions/`cbbo-1m`, not startup. Active Pricing and its worker materialize only verified local import receipts; the worker records zero provider requests. `ml/option_pricing_opra.py:35`, `ml/option_pricing/opra.py:1120`, `ml/option_pricing_runtime.py:553`, `ml/option_pricing_loop_native_worker.py:58`, `ml/option_pricing_loop_native_worker.py:126`
- **Confirmed model relationship, not process coordination:** the resulting residual architecture implements the reference `f(x)=BS(x)+delta(x)` pattern with six inputs, but uses a bounded Nyström/Bayesian-ridge posterior in production and retains an exact-GP SPY path only for research. `docs/edu/BLACK-SCHOLES-OP.md:327`, `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/model.py:68`, `ml/option_pricing/research_benchmark.py:34`

## Dependency matrix

Rows are producers; columns are consumers. Empty cells mean no direct exchange. `T(doc)` is phase-only and does not create causal contribution.

| Producer ↓ / Consumer → | CME/L2 | Loop A | Daily ALFRED | Active Pricing | Options Capture | Directional B | Strategy |
|---|---|---|---|---|---|---|---|
| CME/L2 | — |  |  |  |  | D: `cme__` context |  |
| Loop A |  | — |  | D+C: bar receipt, close, rates | D+C: readiness, bars/regime | D+C: complete cycle and feature data | D: stock BBO |
| Daily ALFRED |  |  | — | D+M: causal FEDFUNDS |  | D+C: vintage macros/readiness |  |
| Active Pricing |  |  |  | — | C+F: target barrier | D+F: `opx__` surfaces | D+M+F: exact leg pricing |
| Options Capture |  |  |  | D+M: lagged chains/outcomes | — | D: `opt__` surfaces | D+M: entry/exit chains/outcomes |
| Directional B |  |  | M+H: decision grid |  | T(doc): +5 before +6 | — | D+M+C: samples/probabilities |
| Strategy |  |  |  |  |  |  | — |

## Fan-in, fan-out, and critical path

- **Confirmed fan-in — Active Pricing:** Loop A exact readiness, Daily ALFRED/current-FRED rates, earlier Options snapshots, and an optional prior owned-worker model. `ml/option_pricing_runtime.py:309`, `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1181`
- **Confirmed fan-in — Directional Loop B:** complete Loop A authority plus Loop A feature files, CME context, ALFRED readiness/vintages, Options surfaces, and Pricing compact history. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:782`
- **Confirmed fan-in — Strategy:** Loop B publication, Options chain/stock BBO history, and Pricing catalog. `ml/strategy_runtime.py:74`, `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:109`
- **Confirmed fan-out — Loop A:** exact readiness to Pricing/Options, complete-cycle/features to B, and stock BBO to Strategy. `datafetching/orchestrate.py:292`, `datafetching/loop_a_cycle.py:127`, `ml/strategy_selection/chain.py:151`
- **Confirmed fan-out — Options Capture:** supplies future Pricing samples/evaluations, `opt__` features to B, and exact strategy chains/outcomes. `ml/option_pricing_runtime.py:660`, `ml/rolling_materialization.py:614`, `ml/strategy_selection/runtime.py:109`
- **Confirmed fan-out — Active Pricing:** target barrier to Options, compact `opx__` features to B, and leg pricing to Strategy. `datafetching/pricing_barrier.py:100`, `ml/option_pricing/consumers.py:306`, `ml/option_pricing/strategy_shadow.py:74`

### Critical-path interpretation

- **Directional horizon publication:** Loop A complete authority is mandatory. Valid ALFRED authority is mandatory for the active v3 daily/weekly profile and invalid shared ALFRED/Pricing authority aborts the materialization. Missing valid Pricing rows alone is noncritical because the baseline feature set is explicit. CME and Options can lag while existing evidence remains within freshness; absent/invalid required family sources can fail affected routes. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:322`, `ml/runtime_pipeline.py:455`
- **Option-pricing target publication:** exact Loop A readiness and valid causal contract/rate/option inputs are critical for each target/route. The fitted residual model is not critical because the Black–Scholes point estimate and explicit sidecar fallback remain supported. Ready residual values can still affect Strategy through the separately verified sidecar. `ml/option_pricing_runtime.py:1128`, `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/strategy_shadow.py:298`
- **Options capture:** neither Pricing success nor immediate Loop A readiness is critical to making the single allowed request; they control prospective credit and commit-versus-pending authority. `datafetching/pricing_barrier.py:124`, `datafetching/options_runtime.py:349`
- **Options-strategy predictions:** a verified Loop B run and an eligible entry option-chain receipt are critical. Active Pricing and a fitted Strategy model improve/authorize calibrated fitted scoring but can degrade to explicit scenario prior. `ml/strategy_runtime.py:83`, `ml/strategy_selection/runtime.py:247`, `ml/strategy_selection/runtime.py:306`

## Explicit non-relationships

- **Confirmed:** CME/L2 and Loop A both use Databento but do not coordinate or consume each other; external CME ownership is deliberately isolated. `datafetching/orchestrate.py:90`, `tests/test_independent_loop_isolation.py:17`
- **Confirmed:** Options does not consume Loop B despite the +5/+6 phase ordering. Its implemented upstreams are Loop A readiness, Pricing barrier, pending state, and providers. `datafetching/options_runtime.py:250`, `datafetching/options_runtime.py:266`
- **Confirmed:** Strategy is not an inline stage of Loop B; Loop B’s manifest declares independent strategy authority, and tests assert directional publication does not wait for Strategy. `ml/runtime_pipeline.py:838`, `tests/test_ml_runtime_pipeline.py:454`
