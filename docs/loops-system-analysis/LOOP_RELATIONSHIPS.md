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
- **Availability:** exact eligible quarter-hour; Loop A polls Databento Historical schema availability and may refetch the exact minute for at most 420 seconds. Pricing waits at most 480 seconds, and both remain inside the independent 1,200-second causal window.
- **Consumer behavior:** bounded wait/retry; deadline causes a write-free skip and leaves prior Pricing authority unchanged.
- **Producer evidence:** `datafetching/orchestrate.py:292`, `datafetching/bar_readiness.py:120`, `datafetching/fred_vintages.py:600`
- **Consumer evidence:** `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1181`, `ml/option_pricing/rates.py:361`, `ml/option_pricing_runtime.py:1713`

### R3 — Loop A → Options Capture

- **Status:** Confirmed.
- **Type:** D + C.
- **Exchange:** exact bar-readiness decision clock/close for downstream pricing and Schwab normalization; latest complete Loop A time as the regime-data cutoff; persisted daily equity bars for realized volatility.
- **Availability:** prospective OPRA capture has independent provider/local clocks and can commit before Loop A readiness. Schwab publication requires exact target readiness; a missing receipt permits only checksum-sealed Schwab pending capture, and closed-market discovery can reconstruct exact clocks from persisted bars.
- **Consumer behavior:** OPRA commit, or Schwab commit/pending/reconcile/terminal expiry; it does not fabricate readiness.
- **Producer evidence:** `datafetching/bar_readiness.py:50`, `datafetching/loop_a_cycle.py:153`, `datafetching/orchestrate.py:326`
- **Consumer evidence:** `datafetching/options_runtime.py:266`, `datafetching/options_runtime.py:335`, `datafetching/options_runtime.py:454`, `options/features.py:335`

### R4 — Loop A → Directional Loop B

- **Status:** Confirmed.
- **Type:** D + C + shared single-reader/writer lock.
- **Exchange:** the current `.ducketz-loop-a-cycle.json` record and its `finished_at` causal cutoff; normalized equity bars/quotes, technicals, fundamentals, signals, current nonhistorical contexts, energy and SEC evidence. `.ducketz-loop-a-complete.json` is retained for independent readers such as Options, not substituted by B.
- **Availability:** Loop B acquires the shared datastore-cycle lock and requires the current Loop A cycle to be `COMPLETE`; `input_available_at` is that cycle’s finish time. A newer `WRITING` or `FAILED` record aborts the B attempt even if a prior latest-complete record remains.
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
- **Exchange:** point-in-time `FEDFUNDS` observations from `pools/macro/features/alfred-release-context/fred/*.parquet`; used as the required causal live risk-free input by fast target construction and by the owned residual-model worker.
- **Availability:** `fed_funds_available_at` must be strictly before the pricing decision/source boundary; rate is percentage points converted to decimal. Current-revised history is not eligible historical evidence.
- **Consumer behavior:** live construction requires the latest eligible ALFRED/FRED observation and disables both option-provider rate fallback and FMP-curve substitution. Offline/general materialization may retain the separately verified curve hierarchy; no invented rate is allowed. `ml/option_pricing/causal.py:264`, `ml/option_pricing/causal.py:450`, `ml/option_pricing/causal.py:468`
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
- **Consumer behavior:** missing horizons/decisions fails plan derivation. A new datastore must first publish an authoritative base/earlier-profile Loop B sample grid; the documented one-time ALFRED backfill can then derive and authorize v3 macro coverage before daily updates begin.
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
- **Exchange:** earlier committed canonical OPRA/explicit-Schwab-fallback chains for contract definitions, lagged IV, source BBO, residual samples and model fitting; the earliest eligible later-target chain reconciles a prior prediction into dollar/normalized error and interval coverage. Provider/evidence lane and `fallback_used` remain explicit.
- **Availability:** source quote/evidence must predate prediction. A prospective outcome must come from a committed snapshot target at or after the prediction target, with exact contract quote and receipt after prediction availability; the later target and availability are retained, never backdated. Natural targets and receipt chains are checksum-verified.
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
- **Consumer behavior:** no valid current Loop B run, samples, or predictions fails the Strategy cycle. The skip fingerprint compares the Loop B pointer, pricing mode, and both prospective OPRA and Schwab per-symbol snapshot heads; a new receipt from either provider wakes the cycle.
- **Producer evidence:** `ml/runtime_pipeline.py:704`, `ml/runtime_pipeline.py:876`
- **Consumer evidence:** `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:81`, `ml/strategy_runtime.py:125`, `ml/strategy_runtime.py:414`

### R14 — Options Capture → Strategy

- **Status:** Confirmed.
- **Type:** D + M.
- **Exchange:** OPRA-first provider-neutral entry/exit chain history, point-in-time definitions, contract BBOs, option-quality evidence, and observed option outcomes. Verified Schwab snapshot history remains the explicit fallback when eligible OPRA definitions/BBOs are unavailable; Schwab stock quote-liquidity remains available for stock legs.
- **Availability:** entry must be known after information availability and before target start; exit is the earliest receipt after target end within a horizon-specific delay and before any lockbox boundary.
- **Consumer behavior:** missing chain/entry/exit evidence is audited and the affected candidate/outcome is skipped rather than synthesized.
- **Producer evidence:** `options/publication.py:406`, `options/snapshot.py:499`
- **Consumer evidence:** `ml/strategy_selection/chain.py:97`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:395`

### R15 — Active Pricing → Strategy

- **Status:** Confirmed.
- **Type:** D + M + F.
- **Exchange:** exact-contract baseline predictions, one-to-one residual sidecars and verified generation history; `BSGP_SHADOW_READY` becomes Strategy source `BSGP`, while complete residual fallback becomes `BLACK_SCHOLES`. Per leg, Strategy receives fair-value edge, conservative edge, uncertainty, favorable probability, model age and residual shrinkage.
- **Availability:** Pricing evidence must be receipt-verified and available before the candidate probability; live scoring disallows offline replay.
- **Consumer behavior:** full active leg coverage admits fitted Strategy scoring; missing/delayed coverage keeps a separately typed non-probabilistic scenario-coverage heuristic with all model-probability fields null.
- **Producer evidence:** `ml/option_pricing/target_outcome.py:93`, `ml/option_pricing/publication.py:83`, `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`
- **Consumer evidence:** `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/runtime.py:310`, `ml/strategy_selection/runtime.py:637`

### R16 — Directional Loop B → Options Capture (phase only)

- **Status:** Documented only as a relative timing relationship; no direct data/control dependency.
- **Type:** T.
- **Exchange:** none. Startup says Options starts after Loop B’s +5 information clock; code schedules B at +5 and Options at +6, but Options never reads the Loop B pointer.
- **Consumer behavior:** none; Options proceeds independently based on Loop A, Pricing barrier, pending state, and provider evidence.
- **Evidence:** `docs/datafetch-ml/current_start_command:99`, `docs/datafetch-ml/current_start_command:160`, `docs/datafetch-ml/current_start_command:188`, `datafetching/options_runtime.py:700`, `datafetching/options_runtime.py:821`

## Owned-worker relationships

**Confirmed:** Active Pricing launches the one-shot loop-native worker after fast target publication without blocking Options. The worker reads ALFRED/FRED rates and verified local OPRA-first/Schwab-fallback history, publishes a local materialization/model/status under its own lock, and makes no provider request. A later Pricing cycle may load that prior model; Strategy may also read verified replay evidence, but live fitted scoring does not relabel offline replay as prospective. `ml/option_pricing_runtime.py`, `ml/option_pricing_loop_native_worker.py`, `ml/option_pricing/strategy_shadow.py`

## OPRA boundaries that are not loop-to-loop relationships

- **Confirmed prospective boundary:** `DatabentoOpraLiveAdapter` implements the injected `OptionMarketDataAdapter` protocol inside Options Capture. It owns one scoped, bounded/reconnecting `OPRA.PILLAR` definitions + `cbbo-1s` transport and no separate supervisor. The default production CLI constructs it before entering the recurring loop; missing configuration/startup fails clearly. At a target, only bounded transient unavailability may use labeled Schwab fallback; identity/integrity failures fail closed. `options/databento_live.py:33`, `options/databento_live.py:139`, `datafetching/options_runtime.py:369`, `datafetching/options_runtime.py:384`, `datafetching/options_runtime.py:408`, `datafetching/options_runtime.py:650`, `datafetching/options_runtime.py:706`, `datafetching/options_runtime.py:720`
- **Confirmed historical boundary:** `datafetching.options_history` is the normal one-time prediction-focused per-parent bootstrap. It prioritizes 100 days of definitions, 20 days of `cbbo-1m`, 1 day of `cbbo-1s`, and the bounded OHLCV windows; research-only `cmbp-1` remains explicitly selectable but default-deferred. The optional `datafetching.databento_cold_start` command can populate the same canonical OPRA partitions alongside isolated CME/US-equity archives, where historical `mbp-1` is also default-deferred in favor of prediction-consumed `mbp-10`. The US-equity archive uses `XNAS.ITCH` rather than inheriting Loop A's live `EQUS.MINI` dataset. It rejects schema, provider-range, entitlement, or storage-capacity failure. After each verified OPRA scope it publishes a v5 symbol/schema cursor containing the exact requested start, completion boundary, lookback policy, and bootstrap manifest. Options Capture runs at most one daily catch-up for valid cursors; legacy v4 is accepted only under its exact former schema policy, while current writes are v5. A missing/invalid cursor is reported as bootstrap-required. `ml.option_pricing_opra` is the separate full-universe/custom-scope administrative synchronizer. Active Pricing and Strategy consume only verified local partitions and record consumer use; they do not fetch history. `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/options_runtime.py`, `datafetching/databento_opra_history.py`, `ml/option_pricing_opra.py`, `ml/option_pricing/opra_materialization.py`, `ml/strategy_selection/chain.py`
- **Confirmed cold-start ownership boundary:** the cold-start command holds `.ducketz-databento-cold-start.lock` and, for OPRA only, the canonical history `state/sync.lock`. It never takes the CME, Loop A, or Options snapshot-writer locks and never publishes Loop A readiness, option snapshots, ML/Strategy pointers, or model authority. Its readable CME and US-equity history cursors plan only later one-shot overlap fills; they are not live-loop cursors. `datafetching/databento_cold_start.py`, `tests/test_databento_cold_start.py`
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
- **Confirmed fan-in — Strategy:** Loop B publication, immutable prospective provider-neutral receipts with OPRA priority per target, historical OPRA replay fallback, stock BBO history, and the Pricing catalog. The +10 supervisor observes both prospective provider heads. `ml/strategy_runtime.py`, `ml/strategy_selection/chain.py`, `ml/strategy_selection/runtime.py`
- **Confirmed fan-out — Loop A:** exact readiness to Pricing/Options, complete-cycle/features to B, and stock BBO to Strategy. `datafetching/orchestrate.py:292`, `datafetching/loop_a_cycle.py:127`, `ml/strategy_selection/chain.py:151`
- **Confirmed fan-out — Options Capture:** supplies future Pricing samples/evaluations, `opt__` features to B, and exact strategy chains/outcomes. `ml/option_pricing_runtime.py:660`, `ml/rolling_materialization.py:614`, `ml/strategy_selection/runtime.py:109`
- **Confirmed fan-out — Active Pricing:** target barrier to Options, compact `opx__` features to B, and leg pricing to Strategy. `datafetching/pricing_barrier.py:100`, `ml/option_pricing/consumers.py:306`, `ml/option_pricing/strategy_shadow.py:74`

### Critical-path interpretation

- **Directional horizon publication:** Loop A complete authority is mandatory. Valid ALFRED authority is mandatory for the active v3 daily/weekly profile and invalid shared ALFRED/Pricing authority aborts the materialization. Missing valid Pricing rows alone is noncritical because the baseline feature set is explicit. CME and Options can lag while existing evidence remains within freshness; absent/invalid required family sources can fail affected routes. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:322`, `ml/runtime_pipeline.py:455`
- **Option-pricing target publication:** exact Loop A readiness and valid causal contract/rate/option inputs are critical for each target/route. The fitted residual model is not critical because the Black–Scholes point estimate and explicit sidecar fallback remain supported. Ready residual values can still affect Strategy through the separately verified sidecar. `ml/option_pricing_runtime.py:1128`, `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/strategy_shadow.py:298`
- **Options capture:** Pricing success is not critical to capture. Target-scoped OPRA selection/commit uses its own point-in-time clocks and does not wait for Loop A; exact Loop A readiness remains mandatory for downstream Pricing and Schwab normalization. If OPRA is transiently unavailable while readiness is delayed, the single fallback Schwab request is durably claimed and quarantined pending reconciliation. `datafetching/pricing_barrier.py:124`, `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:369`, `datafetching/options_runtime.py:452`, `options/pending_capture.py:118`
- **Options-strategy predictions:** a verified Loop B run and an eligible entry option-chain receipt are critical. Active Pricing plus a fitted and calibrated Strategy model authorizes probability scoring; otherwise Strategy exposes only separately typed Scenario Coverage. `ml/strategy_runtime.py`, `ml/strategy_selection/runtime.py`

## Explicit non-relationships

- **Confirmed:** CME/L2 and Loop A both use Databento but do not coordinate or consume each other; external CME ownership is deliberately isolated. `datafetching/orchestrate.py:90`, `tests/test_independent_loop_isolation.py:17`
- **Confirmed:** Options does not consume Loop B despite the +5/+6 phase ordering. Its implemented upstreams are Loop A readiness, Pricing barrier, pending state, and providers. `datafetching/options_runtime.py:250`, `datafetching/options_runtime.py:266`
- **Confirmed:** Strategy is not an inline stage of Loop B; Loop B’s manifest declares independent strategy authority, and tests assert directional publication does not wait for Strategy. `ml/runtime_pipeline.py:838`, `tests/test_ml_runtime_pipeline.py:454`
