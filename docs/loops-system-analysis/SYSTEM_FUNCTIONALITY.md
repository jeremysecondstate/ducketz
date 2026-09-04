# System functionality

## System-wide operating model

**Confirmed:** Ducketz is a set of eight single-writer, independently scheduled processes that exchange immutable data products and checksum-bound authority pointers through the datastore. The processes are not a single in-memory pipeline and are not coordinated by a central scheduler. `docs/datafetch-ml/current_start_command:3`, `docs/datafetch-ml/current_start_command:5`

The checked-in deployment path audits exact Win32 launcher/worker pairs and
worker-owned locks before acting, starts only a completely missing owner, and
uses resolved paths, explicit working directory, unbuffered Python, redirected
`logs\ducketz` streams, and hidden windows. Commands come from the guardian's
closed allowlist and Options retains `--skip-historical-catchup`. A partial,
duplicate, foreign-lock, or command-drift state is not repaired by creating a
second owner. `docs/datafetch-ml/start_all_loops.ps1:18`,
`ml/system_guardian.py:81`

There are three authoritative prediction endpoints:

1. **Option-pricing predictions:** Active Pricing’s exact-target publication (`ml/option-pricing-target-latest/run.json`) is the fast authority used by the Pricing-to-Options barrier; its full append-only generation (`ml/option-pricing-latest/run.json`) supplies compact surfaces, evaluations, and historical consumers. `ml/option_pricing_runtime.py:1306`, `ml/option_pricing/publication.py:79`
2. **Directional horizon predictions:** Directional Loop B’s immutable run is selected by `ml/latest/run.json`; compatibility `latest` files are mirrors, not the generation boundary. `ml/runtime_pipeline.py:859`, `ml/runtime_pipeline.py:943`
3. **Options-strategy predictions:** Strategy’s immutable run is selected by `ml/strategy-latest/run.json`; candidate `decision_score` is a calibrated profitable-outcome probability only for fitted rows. Heuristic rows keep `decision_score` and both probability fields null and publish separate `scenario_coverage_score`. `ml/strategy_publication.py`, `ml/strategy_runtime.py`

## End-to-end causal flow

### 1. Provider ingestion

**Confirmed:** recurring provider calls are confined to ingestion owners;
explicit historical maintenance commands are listed separately and terminate:

- CME/L2 calls Databento for continuous futures OHLCV, BBO, and MBP-10 ranges. Current short-window BBO/MBP collection and strict publication precede at most one older recovery chunk per affected schema, so a deep cursor gap cannot redefine current authority. `datafetching/cme_runtime.py:104`, `datafetching/cme_runtime.py:157`, `datafetching/cme_runtime.py:296`
- Loop A calls Databento `EQUS.MINI` equity history, FMP, current FRED, Schwab stock quotes, and SEC lanes for the production watchlist. It explicitly runs CME and option-chain ownership externally in the production command. The shared Databento retry policy treats a prematurely ended response as transient while leaving authentication, entitlement, validation, and readiness-integrity failures fail-closed. `datafetching/main.py:23`, `app/services/databento_retry.py:14`, `app/services/databento_retry.py:24`, `tests/test_databento_retry.py:6`
- Daily ALFRED calls the FRED/ALFRED API for `FEDFUNDS`, `CPIAUCSL`, `UNRATE`, and `GDP`. `datafetching/fred_vintage_import.py:37`, `datafetching/fred_alfred_runtime.py:69`
- Options Capture owns prospective provider-neutral option snapshots. Its default production CLI constructs one concrete, scoped Databento live adapter for `OPRA.PILLAR` definitions and `cbbo-1s`. Dense callback replay yields cooperatively, and target selection waits for the requested symbol's watermark before validating the final pretarget BBO. Bounded `OptionProviderUnavailable`, including `OPRA_TARGET_WATERMARK_UNAVAILABLE`, can enter a separately labeled Schwab request; missing startup configuration aborts before recurrence and identity/integrity failures do not trigger fallback. `options/databento_live.py:34`, `options/databento_live.py:244`, `options/databento_live.py:280`, `datafetching/options_runtime.py:430`, `datafetching/options_runtime.py:454`
- Historical OPRA synchronization has three one-shot command boundaries. `datafetching.options_history` performs the normal prediction-focused bootstrap independently for each parent symbol and schema: `ohlcv-1s` uses 10 days, `cbbo-1s` 1 day, `cbbo-1m` 20 days, `ohlcv-1m` and `definition` 100 days, `ohlcv-1h` 1,825 days, `ohlcv-1d` 2,555 days, and other default non-interval schemas one month. Research-only `cmbp-1` remains explicitly selectable but is omitted by default. `datafetching.databento_cold_start` is the optional all-dataset bootstrap whose execution requires `--confirm-download`; its OPRA scopes use canonical storage and publish a v5 history-cursor handoff. Its CME and US-equity products remain provider-provenance namespaces: the different-dataset `XNAS.ITCH` equity archive stays cold while Loop A uses current `EQUS.MINI` operational bars, and verified CME scope seeds missing runtime boundaries and historical context fingerprints. The all-dataset default similarly defers historical `mbp-1`, retains one day of prediction-consumed `mbp-10`, and validates included scope and storage capacity. `ml.option_pricing_opra` is the separate full-universe or custom-scope administrative synchronizer. Options Capture then runs at most one catch-up per UTC date for valid cursors, only after attempting the latency-sensitive prospective cycle. `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/databento_archive.py:213`, `datafetching/equity_dataset_migration.py`, `datafetching/databento_archive.py:539`, `datafetching/options_runtime.py`, `ml/option_pricing_opra.py`
- Active Pricing, its owned worker, Directional Loop B, and Strategy consume local evidence. The worker explicitly records zero external provider requests. `ml/option_pricing_loop_native_worker.py:126`

**Confirmed scope:** the checked-in production watchlist is `AAPL AMZN GOOG MU NVDA SNDK`; CALL and PUT create twelve Pricing routes, while `SPY` is research-only. `docs/datafetch-ml/current_start_command:16`

**Confirmed OPRA implementation status:** historical bootstrap/catch-up and prospective live capture are distinct. The eight-owner launcher starts only the prospective Options owner; it does not bootstrap a missing symbol. A configured `opra-canonical` adapter is therefore not proof of historical acquisition. Current population must be established from nonempty `normalized.parquet` files, matching manifests/receipts/checksums, `health/current.json`, and consumer-usage records beneath `C:\DATASTORE\market-data\databento\opra\OPRA.PILLAR`.

### 2. Normalization and persistence

**Confirmed:** Loop A’s `ParquetStore` performs canonical idempotent upserts and replaces a temporary file atomically. It persists raw, normalized, error, and calculated datasets by provider/category/symbol rather than publishing a single monolith. `datafetching/parquet_store.py:106`, `datafetching/parquet_store.py:290`, `datafetching/parquet_store.py:530`

**Confirmed equity dataset boundary:** Loop A's operational dataset is current
`EQUS.MINI`; the cold archive dataset is `XNAS.ITCH`. Native requests overlap
each schema from the latest same-dataset operational timestamp under `stocks`.
`materialize_equity_archive_baseline` remains available only for matching
dataset identities; the recurring wrapper skips a different-dataset archive so
venue-specific and consolidated rows cannot be silently timestamp-merged.
`market-data/databento/us-equities` remains archive provenance, and the
2026-08-19 operational switch is checksum-receipted under `catalog/migrations`.
`datafetching/databento_archive.py:213`, `datafetching/databento_fetch.py:544`,
`datafetching/equity_dataset_migration.py`

**Confirmed:** CME history is partitioned by day for OHLCV and hour for BBO/MBP, with adaptive event natural keys. A cursor is advanced only after all exact ranges in that schema pass persistence. When a deep MBP gap exists, the runtime first fetches exact five-second current MBP and five-minute BBO windows, requires every configured symbol/stream fresh, and then checkpoints at most one older recovery chunk. `datafetching/cme_history.py:207`, `datafetching/cme_runtime.py:157`, `datafetching/cme_runtime.py:202`, `datafetching/cme_runtime.py:296`

**Confirmed CME archive bridge:** when an owned runtime cursor is absent,
verified archive scope supplies the exact-spec historical boundary for live
continuation. Cross-asset context fingerprints the archive inventory, combines
archive and ongoing persisted rows, appends unseen historical/current common
windows, and checksum-binds the lineage. Archive code never owns the CME live
lock, cursor, L2 pointer, or publication. `datafetching/databento_archive.py:539`,
`datafetching/cme_runtime.py:476`,
`datafetching/cme_cross_asset_context.py:250`

**Confirmed:** the supporting five-minute L2 generation is immutable beneath `pools/cme/snapshots/l2/databento/5m/<target_ns>/`; its current pointer is exactly `pools/cme/snapshots/l2/databento/5m/latest.json`. Event time is bounded by the snapshot boundary, local fetch availability is separately bounded by `available_not_after`, configured symbols override a stale cursor inventory, and strict publication requires all expected BBO/MBP rows `FRESH`. No production-loop consumer of that pointer was found. It remains a CME-owned current-state artifact and is not Loop A readiness. `datafetching/cme_history.py:288`, `datafetching/cme_history.py:484`, `datafetching/cme_history.py:532`, `datafetching/cme_history.py:577`

**Confirmed:** ALFRED seals the raw response, normalized vintage Parquet, manifest, and receipt before merging stable vintage identities into immutable yearly feature partitions. It derives macro context only when all four series exist. `datafetching/fred_vintage_import.py:195`, `datafetching/fred_vintage_import.py:267`, `datafetching/fred_vintage_import.py:273`

**Confirmed bootstrap ordering:** the ALFRED backfill planner derives its bounds from an authoritative Loop B `samples.parquet`. A new datastore must therefore create a base/earlier-profile Loop B sample grid before running the one-time backfill and enabling the v3 macro profile. Daily ALFRED updates are valid only after that backfill/readiness authority exists. `datafetching/fred_alfred_readiness.py:400`, `docs/datafetch-ml/fred-alfred-causal-ingestion.md`

**Confirmed:** Options commits one immutable natural target keyed by `(provider, symbol, target_snapshot_for)`. An identical retry reuses the earliest verified receipt; divergent content fails closed. `options/publication.py:29`, `options/publication.py:105`, `options/publication.py:139`

**Confirmed:** historical OPRA partitions use `<schema>/<parent-symbol>/dates/<UTC-date>/segments/<full-day-or-UTC-range>/` and contain distinct `provider.dbn.zst`, `normalized.parquet`, `manifest.json`, and `receipt.json` artifacts. Publication is atomic; duplicate natural keys, empty Parquet, timestamp escape, or checksum/schema mismatch prevents authority. `datafetching/databento_opra_history.py`

### 3. Bar readiness and cycle completion

Loop A has two different authority boundaries:

- **Fast exact-bar readiness. Confirmed.** Immediately after the Databento lane completes, Loop A freezes an all-symbol quarter-hour target containing each symbol’s exact completed one-minute bar timestamp, close, provider, timeframe, source path, and row checksum. Only an actionable XNYS target can publish; a closed/non-actionable cycle explicitly keeps `target=NONE`. Missing exact rows may enter provider-aware bounded recovery, but corrupt/contradictory readiness never enters network retry. It publishes a manifest/receipt directory, then an atomic latest pointer. `datafetching/orchestrate.py:277`, `datafetching/orchestrate.py:300`, `datafetching/orchestrate.py:344`, `datafetching/bar_readiness.py:82`
- **Full-cycle completion. Confirmed.** Loop A begins `WRITING`; after all fetch, fundamental, technical, and signal stages it publishes `COMPLETE` only if the blocking failure count is zero, otherwise `FAILED`. Production quote-only Schwab capture is best-effort directional enrichment: its errors remain persisted/logged but do not block a Databento-backed generation. Explicit inline Schwab options/history modes remain blocking. Only a complete cycle advances `.ducketz-loop-a-complete.json`. `datafetching/orchestrate.py`, `datafetching/loop_a_cycle.py:75`, `datafetching/loop_a_cycle.py:118`, `datafetching/loop_a_cycle.py:127`

These boundaries intentionally decouple time-sensitive Pricing/Options work from slower features. A readiness publication failure does not stop the remainder of Loop A; Pricing decides whether its bounded deadline is missed. `datafetching/orchestrate.py:312`

### 4. Fundamental, technical, macro, signal, CME, and option features

**Confirmed:** Loop A owns provider-normalized equity bars and quotes, point-in-time fundamentals, technical calculations, and cross-domain signals. It invokes the calculated stages after fetch and counts failures into the terminal cycle state. `datafetching/orchestrate.py:326`, `datafetching/orchestrate.py:389`, `datafetching/orchestrate.py:409`, `datafetching/orchestrate.py:429`

**Confirmed:** CME/L2 derives hourly `cme__` context from 60 exact common one-minute observations across NQ, ES, RTY, GC, and CL plus recent BBO/MBP data. Values include futures returns, breadth spreads, relative spread, and book imbalance; future, incomplete, saturated, or stale evidence is rejected. `datafetching/cme_cross_asset_context.py:83`, `datafetching/cme_cross_asset_context.py:183`, `datafetching/cme_cross_asset_context.py:196`, `datafetching/cme_cross_asset_context.py:1007`, `datafetching/cme_cross_asset_context.py:1016`

**Confirmed:** Daily ALFRED derives `macro__fed_funds_level`, CPI year-over-year, unemployment change, and GDP year-over-year. Each value retains its own component availability clock. Current/revised FRED history is explicitly rejected as historical evidence. `datafetching/fred_vintages.py:46`, `datafetching/fred_vintages.py:324`, `datafetching/fred_vintages.py:351`, `datafetching/fred_vintages.py:75`

**Confirmed:** Options Capture derives option-surface values such as IV-minus-realized volatility, term and skew differences, move richness, liquidity/spread measures, coverage ratios, and quote staleness. Its `surface_quality_pass` requires causal timing and minimum quote, timestamp, IV, Greeks, and open-interest coverage. `options/features.py:169`, `options/features.py:186`, `options/features.py:199`, `options/features.py:250`

**Confirmed:** Directional Loop B joins implemented feature families using `available_at`, per-component clocks, freshness, natural-key uniqueness, and quality status. The production v3 profile activates verified ALFRED macros only for daily/weekly contracts and includes `opt__`, `opx__`, quote, CME, fundamental, technical, lifecycle, SEC, and energy families where registered. `ml/feature_registry.py:1039`, `ml/feature_registry.py:1051`, `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:796`

### 5. Option-pricing inputs and predictions

**Confirmed:** Active Pricing selects an actionable XNYS quarter-hour target, rejects future or replayed targets, and permits delayed work only inside the 1,200-second causal source window. When no target is calendar-eligible, it makes no target pointer and does not backdate a prior boundary; monitoring classifies that state as benign `INFO`, not missing production work. `ml/option_pricing_runtime.py:251`, `ml/system_monitor.py:830`

Its target-time inputs are:

- the exact all-symbol Loop A readiness and underlying close;
- earlier committed OPRA-preferred or Schwab-fallback option chains providing lagged IV and contract evidence;
- a strictly causal FRED/ALFRED rate observation for live inference; option-provider and FMP-curve fallback are disabled on this path;
- dividend and contract-definition evidence bounded by availability;
- a prior, receipt-verified loop-native residual model if it existed before prediction and remains fresh/in-support. `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1181`, `ml/option_pricing/causal.py:107`, `ml/option_pricing/rates.py:170`, `ml/option_pricing/rates.py:236`, `ml/option_pricing_runtime.py:313`

**Confirmed and important:** authoritative fast baseline rows are constructed with `models={}`, so their point estimate is constrained causal Black–Scholes and fitted-model uncertainty fields remain null. A one-to-one sidecar carries the Nyström-RBF/Bayesian-ridge residual value and calibrated uncertainty when its prior model is valid, or explicit Black–Scholes fallback intervals/status when the model is missing, stale, unsupported, or fails inference. Strategy's active pricing catalog can consume ready sidecar rows as `BSGP` and fallback rows as `BLACK_SCHOLES`; this deliberate authority split is not an unimplemented model. `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/prediction.py:98`, `ml/option_pricing_runtime.py:1232`, `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`

The six semantic model inputs are underlying price, strike, risk-free rate, lagged implied volatility, target years to expiration, and dividend yield. Eligible contracts are 7–120 days to expiry, within absolute log-moneyness 0.25, multiplier 100. `ml/option_pricing/policies.py:42`, `ml/option_pricing/policies.py:61`

#### Alignment with `BLACK-SCHOLES-OP.md`

- **Confirmed methodology match:** the reference specifies option price as `f(x)=BS(x)+delta(x)` over six inputs `(S,K,r,sigma,t,d)` and treats the discrepancy as a learned stochastic residual with predictive uncertainty. Ducketz uses the same six semantic inputs, learns the normalized residual left by the causal Black–Scholes price, publishes the correction in normalized and dollar units, adds it back to the structural mean, and calibrates residual uncertainty. `docs/edu/BLACK-SCHOLES-OP.md:327`, `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/policies.py:42`, `ml/option_pricing/prediction.py:110`, `ml/option_pricing/prediction.py:172`, `ml/option_pricing/model.py:144`
- **Confirmed production adaptation:** Ducketz does not run the thesis's exact cubic GP/MCMC procedure on the production path. It uses a 128-component Nyström RBF map plus Bayesian ridge posterior, chronological gamma selection, interval calibration, liquidity weighting, and no-arbitrage surface projection. Historical `BSGP` names are compatibility aliases. `docs/edu/BLACK-SCHOLES-OP.md:472`, `docs/edu/BLACK-SCHOLES-OP.md:483`, `ml/option_pricing/model.py:68`, `ml/option_pricing/model.py:847`, `ml/option_pricing/policies.py:123`
- **Confirmed bounded research bridge:** an exact-GP SPY comparison exists separately as a bounded, research-only benchmark and cannot establish production accuracy or enter the six-symbol production universe. `ml/option_pricing/research_benchmark.py:27`, `ml/option_pricing/research_benchmark.py:34`, `ml/option_pricing/research_benchmark.py:178`, `options/README.md:14`
- **Documented only as external research evidence:** the thesis's 2019 SPY sample and reported errors do not prove Ducketz performance on current symbols, OPRA evidence, clocks, or policies. Ducketz's own chronological assessment/prospective gates remain authoritative for model admission. `docs/edu/BLACK-SCHOLES-OP.md:517`, `docs/edu/BLACK-SCHOLES-OP.md:548`, `ml/option_pricing/eligibility.py:646`

### 6. Option snapshot capture

**Confirmed:** Options Capture first reconciles pending evidence, selects the calendar-owned target (or the latest eligible target during closed-market discovery), reuses committed/pending identities without another provider call, waits at most 45 seconds in production for Pricing, then checks exact Loop A readiness. `datafetching/options_runtime.py:108`, `datafetching/options_runtime.py:117`, `datafetching/options_runtime.py:169`, `datafetching/options_runtime.py:250`, `docs/datafetch-ml/current_start_command:161`

The owned prospective OPRA adapter is attempted first for the calendar target, independently of Loop A readiness because its definitions and BBO carry their own causal clocks. Dense replay callbacks periodically yield to the runtime. Before selection, the adapter waits for that symbol's quote watermark to reach the target; only then can it select provider-received definitions active by the target and the final valid consolidated BBO strictly before it. A bounded watermark/transport availability failure records a credential-free code and uses separately labeled Schwab fallback; definition/quote identity or integrity failure rejects the target. Both lanes retain distinct target, activation, market-event, provider-receipt/send, local-receipt, request, prediction, and publication clocks. `options/databento_live.py:244`, `options/databento_live.py:280`, `datafetching/options_runtime.py:430`, `datafetching/options_runtime.py:454`, `options/snapshot.py:122`

If OPRA is transiently unavailable and Loop A readiness is absent, the runtime makes the permitted fallback Schwab request only after durably claiming it, checksum-seals the response under `options/pending-captures/schwab`, and later reconciles it without refetching. The explicit compatibility mode follows the same Schwab pending contract. A response/readiness/reconciliation outside 1,200 seconds becomes an explicit terminal expiry; no clock is backdated. `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:452`, `datafetching/options_runtime.py:477`, `datafetching/options_runtime.py:490`, `options/pending_capture.py:118`, `options/pending_capture.py:264`, `options/pending_capture.py:450`

### 7. Horizon samples and targets

**Confirmed:** Loop B materializes public horizons `1h`, `4h`, `1d`, and `1w`, plus internal weekly route slots `1w-d1` through `1w-d5`. The LIVE remaining-week publication is a calendar-correct contiguous prefix: aggregate `1w`, Day 1, then only the eligible sessions still remaining in that exchange week. `ml/horizons.py:9`, `ml/horizons.py:242`, `ml/horizons.py:283`

- `1h` and `4h` both use the latest completed regular or available standard US extended-hours source hour from 04:00--20:00 Eastern. Source context is deliberately broader than Schwab order authority. Their broker-actionable target minutes are PRE 07:00--09:25, REGULAR 09:30--16:00, and POST 16:05--20:00 Eastern; the two five-minute transitions, holidays, and closures pause accumulation. Early-close days stay core-only. `ml/calendars.py`, `ml/horizons.py`
- `1h` selects the next segment open or eligible exchange-local clock-hour start. A 60-minute window may pause across a closed transition gap, so the 09:00 Eastern / 06:00 Pacific PRE target remains distinct from the 09:30 / 06:30 opening target. `4h` selects the explicit 07:30, 11:30, 15:30, or 19:30 Eastern checkpoint (04:30, 08:30, 12:30, or 16:30 Pacific) and retains its versioned 180-eligible-minute target. The 16:30 Pacific target crosses the overnight closure and normally ends at 06:35 Pacific the next eligible day. `ml/calendars.py`, `ml/horizons.py`
- Databento OHLCV is trade-bearing and omits empty minutes. Each selected target minute therefore uses its native open/close when present or the latest strictly prior native close as a causal no-trade mark. Future fill is forbidden, and the label remains incomplete unless collection coverage proves the entire target window has elapsed. `ml/rolling_samples.py`
- Official `REGULAR` retains its exchange meaning. Options Capture, Active Pricing, Strategy, daily horizons, and weekly horizons are not broadened by the stock PRE/POST policy.
- `1d` is next eligible session open-to-close. `ml/horizons.py:221`
- `1w` is the remaining next-target exchange week. In intelligence, an omitted suffix slot is `NOT_APPLICABLE_TO_REMAINING_WEEK` and `OPERATIONALLY_CURRENT` only when exactly one coherent per-symbol created-LIVE bundle proves its contiguous calendar prefix, common issuance, valid geometry/deadlines/model fields, and bounded probability. Missing, malformed, or ambiguous bundles remain fail-closed as `NO_CURRENT_FORECAST`/`OPERATIONALLY_STALE`. `ml/runtime_pipeline.py:3762`, `ml/runtime_pipeline.py:4005`, `tests/test_ml_weekly_context_model_runtime.py:361`

For every route, the label is complete only after the target end plus processing delay and only when target prices are complete. The target is `1` when simple forward return minus the configured round-trip cost is strictly positive; otherwise `0`; immature or incomplete labels remain null with an explicit status/reason. `ml/rolling_samples.py:272`, `ml/rolling_samples.py:282`, `ml/rolling_samples.py:288`, `ml/rolling_samples.py:319`

### 8. Model fitting, calibration, reuse, and scoring

**Confirmed:** Directional Loop B partitions completed target clusters chronologically and purges overlapping windows at every boundary. The `1h`/`4h` routes use 160 training, 40 calibration, 40 assessment, and 80 sealed lockbox clusters, matching the bounded 100-calendar-day minute-input contract; daily and weekly routes retain 252/63/63/126. Lockbox values are never returned to fitting or evaluation. `app/services/market_fetch_specs.py:96`, `ml/model_runtime.py:57`, `ml/model_runtime.py:200`, `ml/model_runtime.py:255`, `ml/model_runtime.py:301`

It fits the configured logistic model on training only, fits Platt calibration on the calibration window only, evaluates on assessment, and reuses a model only when its complete configuration and input-file inventory match and its checksum verifies. `ml/model_runtime.py:458`, `ml/model_runtime.py:466`, `ml/model_runtime.py:482`, `ml/model_runtime.py:625`

**Confirmed:** Pricing’s residual model uses a 128-component Nyström RBF approximation and Bayesian ridge, selects gamma on chronological calibration evidence, calibrates predictive intervals separately, and reports comparison against Black–Scholes on assessment. It is not an exact Gaussian process. `ml/option_pricing/policies.py:103`, `ml/option_pricing/model.py:847`, `ml/option_pricing/model.py:895`, `ml/option_pricing/model.py:902`

**Confirmed:** Strategy constructs historical candidate outcomes from exact entry and exit option receipts and stock BBOs, partitions decision clusters chronologically, fits a profitable-outcome classifier and expected-return residual model on training, fits Platt on calibration, and evaluates/reuses by full evidence fingerprint. `ml/strategy_selection/runtime.py:425`, `ml/strategy_selection/model.py:138`, `ml/strategy_selection/model.py:277`, `ml/strategy_selection/model.py:331`, `ml/strategy_selection/model.py:365`

### 9. Horizon prediction publication

**Confirmed:** Loop B scores assessment rows as `BACKTEST` and causally actionable latest rows as `LIVE`, carries still-active verified prior LIVE forecasts once, evaluates only exact matching target/cost/contracts, and rejects post-entry predictions. `ml/runtime_pipeline.py:493`, `ml/runtime_pipeline.py:503`, `ml/runtime_pipeline.py:612`, `ml/runtime_pipeline.py:3318`

Before promotion, actual publication time must remain strictly before the actionable deadline. If it does not, the new run fails and the prior pointer is unchanged. A complete immutable run includes samples, predictions, evaluations, monitoring, intelligence, and a manifest; its durable receipt is verified before the sole atomic pointer replacement. `ml/runtime_pipeline.py:590`, `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:794`, `ml/runtime_pipeline.py:943`

Directional outputs carry raw and calibrated probabilities in `[0,1]`, target window and availability clocks, model/calibration identity, mode/status, and cost contract. Evaluation outputs carry exact observed labels, log loss, Brier score, and correctness; monitoring adds calibration gap, ROC AUC, coverage, and model-reuse rate. `ml/parquet_contracts.py:131`, `ml/parquet_contracts.py:156`, `ml/runtime_pipeline.py:3409`, `ml/runtime_pipeline.py:3460`, `ml/runtime_pipeline.py:3633`

### 10. Strategy construction, prediction, ranking, publication, and UI

**Confirmed:** Strategy reads the verified current Loop B run, captures the complete current pointer record, and binds that exact record and its checksums into the Strategy manifest and publication receipt. It then uses the causal input cutoff, samples, and directional probabilities. Immutable prospective snapshots are selected first with OPRA priority per natural target. Canonical historical OPRA replay and the Strategy cache are eligible fallbacks only for offline history/outcome/model construction; recurring live entry and live Pricing attachment reject offline evidence. For each actual LIVE prediction it requires a point-in-time prospective chain receipt between information availability and target entry, constructs registered candidates, attaches exact-contract live-eligible Pricing evidence before scoring, and uses a calibrated fitted model only when active Pricing covers every leg. `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:235`, `ml/strategy_publication.py:41`, `ml/strategy_selection/chain.py:111`, `ml/strategy_selection/runtime.py:167`

When the fitted model is unavailable or full quality-passing Pricing coverage is absent, the candidate remains `SCENARIO_COVERAGE_HEURISTIC`. Its local scenario-grid pass fraction is stored only in `scenario_coverage_score`; raw, calibrated, and decision probabilities remain null. Fitted rows use calibrated profitable-outcome probability as the decision score. Calibrated candidates rank ahead of heuristic candidates, with deterministic within-tier ranking. `ml/strategy_runtime.py`, `ml/strategy_selection/market_state.py`, `ml/strategy_selection/runtime.py`

Strategy writes candidates, audit, model reports and copied model artifacts, then binds the manifest, exact Loop B source record, option receipts, and stock BBO lineage into an atomic publication receipt/pointer. The unchanged-work test likewise requires exact current Loop B record equality plus unchanged pricing mode and provider heads. `ml/strategy_runtime.py:163`, `ml/strategy_runtime.py:221`, `ml/strategy_runtime.py:256`, `ml/strategy_runtime.py:429`

**Observed in the preserved 2026-08-19 22:45:36 UTC proof:** Loop B run
`ml/runs/20260819T223552.337574Z` and Strategy run
`ml/strategy-runs/20260819T224000.073641Z` both passed manifest/publication
checksum verification, and Strategy was bound to that exact current Loop B
record. The Strategy run published 4,800 candidate rows and 1,440 audit rows;
all candidates were explicitly `SCENARIO_COVERAGE_HEURISTIC`, all nine model
reports were `MODEL_NOT_FIT`, and zero models were trained or reused. This is
insufficient causal/model/Pricing maturity, not poor calibrated performance.
Scenario Coverage stays a distinct heuristic fraction and must not fill a
probability column. A 22:59:29 UTC read-only follow-up verified newer Loop B and
Strategy authorities with the same exact-lineage contract.

**Confirmed:** the Rolling Forecast UI resolves the authoritative Loop B pointer; the Options Strategy UI resolves the Strategy pointer and validates candidate schema/policy. Neither is a writer or an automated-action owner. `app/ui/rolling_forecast_data.py:539`, `app/ui/options_strategy_data.py:628`, `app/ui/rolling_forecast_data.py:408`

### 11. Sequence shadow, Loop C Options Strategy paper tracking, and live-stock evaluation

**Confirmed:** the pooled sequence encoder publishes only `SHADOW_ONLY`
evidence. With explicit current observe-only controls, Loop C may select one
exact receipt-bound Strategy candidate during an open XNYS session, but it has
no options broker path and cannot submit an order. Only `1d` and dynamic `1w`
(`Remaining-Week Aggregate`) candidates enter the options-paper lane; `1h` and
`4h` remain ineligible. `ml/sequence_encoder/runtime.py`, `ml/loop_c/runtime.py`

A Loop C `RESEARCH_PROPOSAL` freezes the selected candidate's exact legs and
entry economics, Loops/sequence evidence, quantity, expirations, standard
contract multipliers, maximum potential share obligation, and planned
target-window exit. A stateful lifecycle, missing expiration, or target beyond
the earliest expiration is rejected. This makes expiration/assignment exposure
visible without pretending that a paper position was assigned or exercised.
`ml/loop_c/paper_ledger.py`

The separate Tuesday-through-Saturday daily paper tracker reconstructs all
verified proposals and joins them only to causally mature exact Strategy outcome
receipts. It keeps open, evidence-pending, and mature paper positions separate
from the operator's Schwab Options Strategy history and from actual account P/L.
The Saturday Loop C review consumes this ledger. `ml/loop_c/paper_ledger.py`,
`ml/loop_c/weekly_review.py`

The autonomous stock trader is a separate real-order system. Every LIVE and
SHADOW decision retains its Loop B prediction link; reconciliation adds
sanitized fill quantity, price, and execution time. The weekly audit reports
both forward-window prediction economics and a separate receipt-matched local
FIFO fill lifecycle. The latter is explicitly not Schwab tax-lot or fee-inclusive
account P/L. `ml/stock_trader/reconciliation.py`,
`ml/stock_trader/execution_lifecycle.py`, `ml/stock_trader/audit.py`

## Scheduling and ordinary phase order

| Loop | Implemented production cadence/phase | Same-cycle relationship | Status |
|---|---|---|---|
| CME/L2 | 5 s MBP, 15 s BBO, 60 s OHLCV with +0/+2/+1 s phases | Independent rolling context | **Confirmed.** `datafetching/cme_runtime.py:40`, `datafetching/cme_runtime.py:45` |
| Loop A | 15-minute boundary then +20 s after each completed recurring cycle | Readiness precedes slower Loop A work | **Confirmed.** `datafetching/orchestrate.py:225`, `datafetching/orchestrate.py:236`, `datafetching/orchestrate.py:243` |
| Active Pricing | 15 minutes, +1 | Waits up to 480 s for Loop A's exact target while Loop A performs a provider-availability-gated recovery for at most 420 s | **Confirmed.** `docs/datafetch-ml/current_start_command`, `datafetching/databento_fetch.py`, `datafetching/orchestrate.py` |
| Directional Loop B | 30 minutes, +6 | Reads only a complete Loop A cycle; permits one classified-transient retry and a 35-minute startup freshness recovery | **Confirmed.** `docs/datafetch-ml/current_start_command`, `ml/prediction_runtime.py` |
| Options Capture | 15 minutes, +6 | Bounded 45 s Pricing barrier; documented after B’s +5 clock but no B artifact read | **Confirmed timing; documented-only B association.** `docs/datafetch-ml/current_start_command:99`, `docs/datafetch-ml/current_start_command:160`, `docs/datafetch-ml/current_start_command:161` |
| Strategy | 15 minutes, +10 | Reads current Loop B and evidence; skip fingerprint is Loop B pointer + pricing mode + both OPRA and Schwab per-symbol snapshot heads | **Confirmed.** A new prospective receipt from either provider wakes the otherwise unchanged cycle. `ml/strategy_runtime.py` |
| Strategy-profit training | Daily at 22:00 UTC | Owns the bounded 1d/1w fitted-model training and promotion path; no order authority | **Confirmed.** `docs/datafetch-ml/current_start_command:249` |
| Daily ALFRED | at most once per date; 07:00 UTC next boundary | Asynchronous historical authority, not a quarter-hour phase | **Confirmed.** `datafetching/fred_alfred_runtime.py:53`, `docs/datafetch-ml/current_start_command:72` |

**Inferred:** under normal latency the ordinary quarter-hour flow is Loop A bar readiness at about `+00:20`, Pricing at `+01`, Loop B at `+05`, Options at `+06`, Strategy at `+10`. The exact completion order is not guaranteed: readiness and pricing barriers are bounded, each process has its own clock, and no central orchestrator enforces this sequence. `datafetching/bar_readiness.py:245`, `datafetching/pricing_barrier.py:77`

## Operational monitoring and guarded recovery

**Confirmed:** `ml.system_monitor` has three nested layers. Hourly verifies the
eight exact process pairs, worker locks, active logs, publications, lineage,
UI contracts, and storage. Daily adds post-close ALFRED, directional, Strategy,
and Pricing/Strategy evaluation for all nine routes, explicitly including `1d`
and `1w`. Weekly contains both baselines and adds a comparison of the last two
completed XNYS session weeks using immutable `LIVE` evaluation evidence only.
It requires identical model/target/cost definitions and at least 30 independent
observations per period; otherwise it reports insufficient or incompatible
weekly evidence, never a manufactured trend. `ml/system_monitor.py:164`,
`ml/system_monitor.py:1375`

Scheduled selection is exchange-calendar-aware. The heartbeat wakes at local
minute 42; at the 2 PM wake an eligible post-close XNYS session selects daily,
or weekly if it is the final eligible session of that exchange week. Holidays
and shortened weeks therefore do not assume Friday. All other wakes select
hourly. `ml/system_monitor.py:1872`

**Confirmed status semantics:** `FAIL` makes the report `UNHEALTHY`, `WARN`
makes it `DEGRADED`, and a report containing only `PASS` plus benign `INFO`
remains `HEALTHY`. In particular, closed/settling market awareness may report
that no Pricing target exists without treating it as stale; it cannot mask a
warning, failure, or stale UI/publication condition. `ml/system_monitor.py:830`,
`ml/system_monitor.py:1767`, `tests/test_system_monitor.py:243`

**Confirmed guardian boundary:** `ml.system_guardian` may repair at most one
closed-allowlist liveness fault per wake. A suspected hang requires two
unchanged observations at least 30 minutes apart; the first writes
`OBSERVING_HANG` and preserves evidence. Duplicate/ambiguous owners, foreign or
unreadable locks, integrity failure, and credential/entitlement/rate/capacity
blockers fail closed. Repairs have a two-hour per-runtime cooldown and write
immutable audit receipts. The guardian never repairs data/model/lineage
quality, backfills history, changes pointers, promotes models, edits code, or
places orders. `ml/system_guardian.py:237`

**Active automation:** `loops-hourly-operations` is the singleton operational
guardian at minute 42, with a fresh chat for every run. It reads a
checksum-verified advisory handoff, runs the compact scheduled guardian exactly
once, parses JSON even on exit 2, and reports selected mode/stage/status plus
every WARN/FAIL. Separate Scheduled tasks own stock-trader checkpoint execution,
daily stock adaptation, the Tuesday-through-Saturday Options Strategy paper
ledger, and the Saturday Loop C/operator review. Those tasks do not share or
advance the hourly guardian handoff and cannot borrow one another's mutation or
broker authority. `ml/scheduler_handoff.py`; see `MONITORING.md`,
`STOCK_TRADER_AUTOMATION.md`, `OPTIONS_STRATEGY_PAPER_AUTOMATION.md`, and
`WEEKLY_REVIEW_AUTOMATION.md` for exact responsibility boundaries.

## Clocks, causality, and authority boundaries

| Clock/boundary | Meaning | Enforcement |
|---|---|---|
| `decision_timestamp` / `target_snapshot_for` | Calendar-owned information boundary, not receipt time | Must be an eligible XNYS quarter-hour for Pricing/Options; exact completed bars only. `datafetching/bar_readiness.py:94`, `datafetching/bar_readiness.py:97` |
| event/quote timestamp | Provider’s market-event time | Option OPRA quotes must be strictly before target; CME future evidence is rejected. `options/snapshot.py:183`, `datafetching/cme_cross_asset_context.py:183` |
| `available_at` / `first_available_at` | Earliest causal consumer availability | Family joins use it and reject component clocks after it; Pricing history selection is bounded by it. `ml/datasets/families.py:1577`, `ml/option_pricing/consumers.py:372` |
| local receipt/publication time | When immutable evidence actually became consumable | Receipts cannot predate input availability; barrier credit requires authority before Options request. `options/publication.py:204`, `datafetching/pricing_barrier.py:40` |
| label/outcome availability | Earliest time a target or strategy result may be known | Loop B label requires target end + delay; Pricing reconciliation requires post-prediction option evidence; Strategy exit receipts follow target end. `ml/rolling_samples.py:272`, `ml/option_pricing/causal.py:909`, `ml/strategy_selection/runtime.py:395` |
| closed lockbox | Sealed final-evidence interval | Model partitions report metadata only; target values are redacted/unread. `ml/model_runtime.py:745`, `ml/option_pricing_runtime.py:2229` |

## Locks and single-writer rules

**Confirmed:** all eight recurring owners use the shared exclusive runtime-lock helper, including Loop A's `.ducketz-orchestration.lock` and Directional Loop B's `.duckets-ml-prediction-runtime.lock`. The helper creates an `O_EXCL` lock, reclaims it once only when its recorded positive PID is positively confirmed dead, and removes it on normal exit only when the owned bytes are unchanged. Live, malformed, zero, or unqueryable ownership remains fail-closed. `datafetching/runtime_lock.py`, `datafetching/orchestrate.py`, `ml/prediction_runtime.py`

**Confirmed:** Loop A and Directional Loop B additionally share the OS-held `.ducketz-loop-a-cycle.lock` around Loop A writes and complete Loop B reads. The OS lock releases on process exit even though its marker file persists. `datafetching/loop_a_cycle.py`

**Confirmed:** the one-shot all-dataset baseline/overlap command uses the same stale-owner-aware helper only for `.ducketz-databento-cold-start.lock` and the canonical OPRA `state/sync.lock`; neither is a production supervisor lock or snapshot/publication authority. `datafetching/databento_cold_start.py`

**Confirmed:** the Pricing child has a separate collision lock but cannot move target authority directly; it publishes only future-consumable materialization/model/status artifacts. `ml/option_pricing_loop_native_worker.py:36`, `ml/option_pricing_loop_native_worker.py:48`, `ml/option_pricing_loop_native_worker.py:87`

**Confirmed deployment/repair rule:** the checked-in launcher and guardian both
derive commands from the closed allowlist. New starts are hidden, unbuffered,
resolved-path launches with explicit working directory and stdout/stderr under
`logs\ducketz`. Active legacy `runtime-logs` streams remain discoverable for
monitoring, but are not the future launch destination. The Options command
retains `--skip-historical-catchup`. `docs/datafetch-ml/start_all_loops.ps1:18`,
`ml/system_guardian.py:81`, `ml/system_monitor.py:427`

## Failure and degradation propagation

| Condition | Implemented propagation | Prediction effect |
|---|---|---|
| Loop A target bar missing by Pricing deadline | Pricing retries the exact target until one monotonic deadline, then skips without changing current authority. `ml/option_pricing_runtime.py:1681`, `ml/option_pricing_runtime.py:1713` | No new option-pricing authority for that target; prior authority remains. |
| Loop A target bar missing for Options | Canonical OPRA can still commit using its own causal clocks. If OPRA is transiently unavailable, the single Schwab fallback is sealed pending and reconciled if readiness arrives inside 1,200 s. `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:452`, `datafetching/options_runtime.py:490`, `options/pending_capture.py:450` | OPRA evidence remains available for later causal Pricing; no pending Schwab feature is usable until committed. |
| Pricing barrier missing/timed out | Barrier returns status rather than raising; Options still captures, but prospective Pricing-before-request credit is false. `datafetching/pricing_barrier.py:121`, `datafetching/pricing_barrier.py:181` | Options continues; later evaluation cannot claim invalid causal sequencing. |
| Pricing model missing/stale/out-of-support | Fast target keeps the Black–Scholes point estimate; the sidecar publishes explicit fallback status and wider fallback uncertainty. `ml/option_pricing_runtime.py:1143`, `ml/option_pricing/prediction.py:289`, `ml/option_pricing_runtime.py:1244` | Option prices remain available at baseline quality; no model-based residual lift. |
| Pricing evidence missing/stale in Loop B | `opx__` becomes audited null, then the family gate uses the versioned non-Pricing baseline. `ml/rolling_materialization.py:677`, `ml/runtime_pipeline.py:455` | Directional publication can continue without Pricing features. |
| Pricing authority corrupt or ALFRED readiness invalid | The shared contract exception is re-raised instead of converted into a partial route. `ml/rolling_materialization.py:322` | Whole new Loop B publication aborts; prior pointer remains. |
| Optional feature is stale but contract-valid | Point-in-time joins null the feature after freshness/quality rules. `ml/datasets/families.py:1026`, `ml/datasets/families.py:1577` | Model preprocessing/family gate determines degradation; no silent stale substitution. |
| Some Loop B routes fail | Production default permits successful routes; an empty prediction set or explicit `require_all_routes` fails the run. `ml/runtime_pipeline.py:633`, `ml/runtime_pipeline.py:654` | Partial current coverage is possible; failed routes are manifest-audited. |
| Weekly component suffix is omitted | It is current/calendar-inapplicable only when one coherent created-LIVE per-symbol bundle proves the exact remaining-week prefix. Missing, malformed, or ambiguous proof stays stale. `ml/runtime_pipeline.py:3819`, `ml/runtime_pipeline.py:4005` | UI may show an intentional N/A slot without a probability; genuine absence cannot masquerade as current. |
| Loop B publication crosses entry deadline or promotion fails | Transaction aborts/rolls back before pointer replacement. `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:1067` | Prior directional authority remains consumable. |
| Strategy lacks chain history/entry receipt | Route is audit-only and candidate construction skips. `ml/strategy_selection/runtime.py:230`, `ml/strategy_selection/runtime.py:247` | No new candidate predictions for the affected route. |
| Strategy lacks full active Pricing coverage/model | Candidate uses separately typed Scenario Coverage; all model-probability fields remain null and fitted calibration is not fabricated. `ml/strategy_selection/runtime.py`, `ml/strategy_runtime.py` | Strategy output may continue as research-only fallback. |
| Runtime crash during staging | Private staging has no pointer/receipt authority; readers remain on the previous verified generation. `datafetching/bar_readiness.py:173`, `ml/option_pricing_runtime.py:2393`, `ml/runtime_pipeline.py:943` | No partial publication becomes authoritative. |

## Activation gates and what code presence does not prove

- **Confirmed:** a feature column is not active merely because it exists; the registry resolves horizon-specific active sets, and Pricing-family admission additionally requires coverage/freshness. `ml/feature_registry.py:1001`, `ml/runtime_pipeline.py:432`
- **Confirmed:** option-pricing research eligibility and quality reports cannot block a valid Black–Scholes target publication. `ml/option_pricing_runtime.py:360`
- **Confirmed:** Strategy's active fitted model admits only candidates with ready BSGP or explicit Black–Scholes evidence and complete leg coverage; live mode rejects offline replay. `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:637`
- **Confirmed:** Loop B's production-selected Pricing family is not admitted merely because `opx__` columns exist; insufficient coverage/freshness selects the exact registered non-Pricing baseline feature contract while preserving the other features. `ml/runtime_pipeline.py:432`, `ml/runtime_pipeline.py:455`, `tests/test_runtime_ui_integration.py:154`
- **Operational verification required:** code presence and these docs do not establish current entitlement/connectivity, historical completeness, prospective OPRA/Schwab share, gate state, model reuse, or realized accuracy. Inspect current provider responses, immutable receipts, health totals, consumer-usage records, and prediction/strategy manifests for those claims.
