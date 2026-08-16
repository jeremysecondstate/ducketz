# System functionality

## System-wide operating model

**Confirmed:** Ducketz is a set of seven single-writer, independently scheduled processes that exchange immutable data products and checksum-bound authority pointers through the datastore. The processes are not a single in-memory pipeline and are not coordinated by a central scheduler. `docs/datafetch-ml/current_start_command:3`, `docs/datafetch-ml/current_start_command:5`

There are three authoritative prediction endpoints:

1. **Option-pricing predictions:** Active Pricing’s exact-target publication (`ml/option-pricing-target-latest/run.json`) is the fast authority used by the Pricing-to-Options barrier; its full append-only generation (`ml/option-pricing-latest/run.json`) supplies compact surfaces, evaluations, and historical consumers. `ml/option_pricing_runtime.py:1305`, `ml/option_pricing/publication.py:79`
2. **Directional horizon predictions:** Directional Loop B’s immutable run is selected by `ml/latest/run.json`; compatibility `latest` files are mirrors, not the generation boundary. `ml/runtime_pipeline.py:859`, `ml/runtime_pipeline.py:943`
3. **Options-strategy predictions:** Strategy’s immutable run is selected by `ml/strategy-latest/run.json`; candidate `decision_score` is the profitable-outcome probability for fitted rows and the explicit scenario prior for fallback rows. `ml/strategy_publication.py:36`, `ml/strategy_publication.py:159`

## End-to-end causal flow

### 1. Provider ingestion

**Confirmed:** provider calls are confined to ingestion owners:

- CME/L2 calls Databento for continuous futures OHLCV, BBO, and MBP-10 ranges. `datafetching/cme_runtime.py:98`, `datafetching/cme_runtime.py:479`
- Loop A calls Databento equity history, FMP, current FRED, Schwab stock data, and SEC lanes for the production watchlist. It explicitly runs CME and option-chain ownership externally in the production command. `datafetching/main.py:23`, `docs/datafetch-ml/current_start_command:52`, `docs/datafetch-ml/current_start_command:56`
- Daily ALFRED calls the FRED/ALFRED API for `FEDFUNDS`, `CPIAUCSL`, `UNRATE`, and `GDP`. `datafetching/fred_vintage_import.py:37`, `datafetching/fred_alfred_runtime.py:69`
- Options Capture owns prospective provider-neutral option snapshots. Its default production CLI constructs one concrete, scoped Databento live adapter for `OPRA.PILLAR` definitions and `cbbo-1s`, validates provider/dataset/schema/symbol/target, definition activation, and all definition/quote clocks, and falls back to a separately labeled Schwab request only for bounded transient per-target unavailability. Missing credentials or transport startup aborts before recurrence; integrity failures do not trigger fallback. `options/databento_live.py:33`, `options/databento_live.py:139`, `datafetching/options_runtime.py:369`, `datafetching/options_runtime.py:384`, `datafetching/options_runtime.py:408`, `datafetching/options_runtime.py:650`, `datafetching/options_runtime.py:706`, `datafetching/options_runtime.py:720`
- Historical OPRA synchronization is a resumable Standard-subscription maintenance path. `ml.option_pricing_opra` discovers provider bounds, refuses nonzero-cost scope, preflights capacity, and publishes provider-native DBN plus normalized Parquet partitions. `datafetching.options_history` is the one-time per-parent bootstrap with an entitlement-clamped six-month cap and a separate one-month `cmbp-1` cap. Options Capture only advances completed cursors with a three-day overlap; Pricing and Strategy consume only checksum-verified local partitions.
- Active Pricing, its owned worker, Directional Loop B, and Strategy consume local evidence. The worker explicitly records zero external provider requests. `ml/option_pricing_loop_native_worker.py:126`

**Confirmed scope:** the checked-in production watchlist is `AAPL AMZN GOOG MU NVDA SNDK`; CALL and PUT create twelve Pricing routes, while `SPY` is research-only. `docs/datafetch-ml/current_start_command:16`

**Confirmed OPRA implementation status:** the codebase has distinct historical and prospective OPRA implementations. Historical execution remains separately authorized maintenance. Prospective live construction, injection, startup failure, six-parent scope, strict pretarget BBO and activated-definition selection, reconnect/buffer behavior, OPRA-first capture, labeled fallback, and committed-target idempotence are isolated-test covered. Actual provider connectivity and datastore population remain Unknown because neither was inspected or invoked. `tests/test_databento_opra_live.py:171`, `tests/test_databento_opra_live.py:217`, `tests/test_databento_opra_live.py:278`, `tests/test_databento_opra_live.py:358`, `tests/test_databento_opra_live.py:392`, `tests/test_databento_opra_live.py:540`, `tests/test_databento_opra_live.py:622`, `tests/test_option_publication.py:387`, `tests/test_option_pricing_opra.py:116`

### 2. Normalization and persistence

**Confirmed:** Loop A’s `ParquetStore` performs canonical idempotent upserts and replaces a temporary file atomically. It persists raw, normalized, error, and calculated datasets by provider/category/symbol rather than publishing a single monolith. `datafetching/parquet_store.py:106`, `datafetching/parquet_store.py:290`, `datafetching/parquet_store.py:530`

**Confirmed:** CME history is partitioned by day for OHLCV and hour for BBO/MBP, with adaptive event natural keys. A cursor is advanced only after all exact ranges in that schema pass persistence. `datafetching/cme_history.py:207`, `datafetching/cme_history.py:256`, `datafetching/cme_runtime.py:535`, `datafetching/cme_history.py:654`

**Confirmed:** ALFRED seals the raw response, normalized vintage Parquet, manifest, and receipt before merging stable vintage identities into immutable yearly feature partitions. It derives macro context only when all four series exist. `datafetching/fred_vintage_import.py:195`, `datafetching/fred_vintage_import.py:267`, `datafetching/fred_vintage_import.py:273`

**Confirmed:** Options commits one immutable natural target keyed by `(provider, symbol, target_snapshot_for)`. An identical retry reuses the earliest verified receipt; divergent content fails closed. `options/publication.py:29`, `options/publication.py:105`, `options/publication.py:139`

### 3. Bar readiness and cycle completion

Loop A has two different authority boundaries:

- **Fast exact-bar readiness. Confirmed.** Immediately after the Databento lane completes, Loop A freezes an all-symbol quarter-hour target containing each symbol’s exact completed one-minute bar timestamp, close, provider, timeframe, source path, and row checksum. It publishes a manifest/receipt directory, then an atomic latest pointer. `datafetching/orchestrate.py:267`, `datafetching/orchestrate.py:292`, `datafetching/bar_readiness.py:82`, `datafetching/bar_readiness.py:150`
- **Full-cycle completion. Confirmed.** Loop A begins `WRITING`; after all fetch, fundamental, technical, and signal stages it publishes `COMPLETE` only if the failure count is zero, otherwise `FAILED`. Only a complete cycle advances `.ducketz-loop-a-complete.json`. `datafetching/loop_a_cycle.py:75`, `datafetching/loop_a_cycle.py:118`, `datafetching/loop_a_cycle.py:127`

These boundaries intentionally decouple time-sensitive Pricing/Options work from slower features. A readiness publication failure does not stop the remainder of Loop A; Pricing decides whether its bounded deadline is missed. `datafetching/orchestrate.py:312`

### 4. Fundamental, technical, macro, signal, CME, and option features

**Confirmed:** Loop A owns provider-normalized equity bars and quotes, point-in-time fundamentals, technical calculations, and cross-domain signals. It invokes the calculated stages after fetch and counts failures into the terminal cycle state. `datafetching/orchestrate.py:326`, `datafetching/orchestrate.py:389`, `datafetching/orchestrate.py:409`, `datafetching/orchestrate.py:429`

**Confirmed:** CME/L2 derives hourly `cme__` context from 60 exact common one-minute observations across NQ, ES, RTY, GC, and CL plus recent BBO/MBP data. Values include futures returns, breadth spreads, relative spread, and book imbalance; future, incomplete, saturated, or stale evidence is rejected. `datafetching/cme_cross_asset_context.py:68`, `datafetching/cme_cross_asset_context.py:103`, `datafetching/cme_cross_asset_context.py:168`, `datafetching/cme_cross_asset_context.py:181`

**Confirmed:** Daily ALFRED derives `macro__fed_funds_level`, CPI year-over-year, unemployment change, and GDP year-over-year. Each value retains its own component availability clock. Current/revised FRED history is explicitly rejected as historical evidence. `datafetching/fred_vintages.py:46`, `datafetching/fred_vintages.py:324`, `datafetching/fred_vintages.py:351`, `datafetching/fred_vintages.py:75`

**Confirmed:** Options Capture derives option-surface values such as IV-minus-realized volatility, term and skew differences, move richness, liquidity/spread measures, coverage ratios, and quote staleness. Its `surface_quality_pass` requires causal timing and minimum quote, timestamp, IV, Greeks, and open-interest coverage. `options/features.py:169`, `options/features.py:186`, `options/features.py:199`, `options/features.py:250`

**Confirmed:** Directional Loop B joins implemented feature families using `available_at`, per-component clocks, freshness, natural-key uniqueness, and quality status. The production v3 profile activates verified ALFRED macros only for daily/weekly contracts and includes `opt__`, `opx__`, quote, CME, fundamental, technical, lifecycle, SEC, and energy families where registered. `ml/feature_registry.py:1039`, `ml/feature_registry.py:1051`, `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:782`

### 5. Option-pricing inputs and predictions

**Confirmed:** Active Pricing selects an actionable XNYS quarter-hour target, rejects future or replayed targets, and permits delayed work only inside the 1,200-second causal source window. `ml/option_pricing_runtime.py:251`, `ml/option_pricing_runtime.py:255`, `ml/option_pricing_runtime.py:258`, `ml/option_pricing_runtime.py:284`

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

**Confirmed:** Options Capture first reconciles pending evidence, selects the calendar-owned target (or the latest eligible target during closed-market discovery), reuses committed/pending identities without another provider call, waits at most 45 seconds in production for Pricing, then checks exact Loop A readiness. `datafetching/options_runtime.py:108`, `datafetching/options_runtime.py:117`, `datafetching/options_runtime.py:169`, `datafetching/options_runtime.py:250`, `docs/datafetch-ml/current_start_command:115`

The owned prospective OPRA adapter is attempted first for the calendar target, independently of Loop A readiness because its definitions and BBO carry their own causal clocks. It selects provider-received definitions whose contract activation is no later than the target and the final valid consolidated BBO strictly before the target from its shared buffer, then commits under `databento-opra`; Loop 3 still requires exact Loop A authority before pricing. A bounded availability failure records a credential-free code and uses separately labeled Schwab fallback; definition/quote identity or integrity failure rejects the target. Both lanes retain distinct target, activation, market-event, provider-receipt/send, local-receipt, request, prediction, and publication clocks. `options/databento_live.py:268`, `options/snapshot.py:122`, `datafetching/options_runtime.py:369`, `datafetching/options_runtime.py:384`, `datafetching/options_runtime.py:408`, `datafetching/options_runtime.py:428`, `datafetching/options_runtime.py:437`

If OPRA is transiently unavailable and Loop A readiness is absent, the runtime makes the permitted fallback Schwab request only after durably claiming it, checksum-seals the response under `options/pending-captures/schwab`, and later reconciles it without refetching. The explicit compatibility mode follows the same Schwab pending contract. A response/readiness/reconciliation outside 1,200 seconds becomes an explicit terminal expiry; no clock is backdated. `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:452`, `datafetching/options_runtime.py:477`, `datafetching/options_runtime.py:490`, `options/pending_capture.py:118`, `options/pending_capture.py:264`, `options/pending_capture.py:450`

### 7. Horizon samples and targets

**Confirmed:** Loop B materializes public horizons `1h`, `4h`, `1d`, and `1w`, plus internal weekly components `1w-d1` through `1w-d5`. `ml/horizons.py:9`, `ml/horizons.py:18`

- `1h` and `4h` require every calendar-selected native one-minute target record for the next 60/240 eligible regular-session minutes; missing minutes do not shift the window. `ml/horizons.py:121`, `ml/horizons.py:171`
- `1d` is next eligible session open-to-close. `ml/horizons.py:221`
- `1w` is the remaining next-target exchange week, with component routes for each of the next five eligible sessions. `ml/horizons.py:240`, `ml/horizons.py:281`

For every route, the label is complete only after the target end plus processing delay and only when target prices are complete. The target is `1` when simple forward return minus the configured round-trip cost is strictly positive; otherwise `0`; immature or incomplete labels remain null with an explicit status/reason. `ml/rolling_samples.py:272`, `ml/rolling_samples.py:282`, `ml/rolling_samples.py:288`, `ml/rolling_samples.py:319`

### 8. Model fitting, calibration, reuse, and scoring

**Confirmed:** Directional Loop B partitions completed target clusters chronologically into at least 252 training, 63 calibration, 63 assessment, and 126 sealed lockbox clusters; overlapping target windows are purged at boundaries, and the lockbox values are not returned to fitting or evaluation. `ml/model_runtime.py:57`, `ml/model_runtime.py:200`, `ml/model_runtime.py:255`, `ml/model_runtime.py:301`

It fits the configured logistic model on training only, fits Platt calibration on the calibration window only, evaluates on assessment, and reuses a model only when its complete configuration and input-file inventory match and its checksum verifies. `ml/model_runtime.py:458`, `ml/model_runtime.py:466`, `ml/model_runtime.py:482`, `ml/model_runtime.py:625`

**Confirmed:** Pricing’s residual model uses a 128-component Nyström RBF approximation and Bayesian ridge, selects gamma on chronological calibration evidence, calibrates predictive intervals separately, and reports comparison against Black–Scholes on assessment. It is not an exact Gaussian process. `ml/option_pricing/policies.py:103`, `ml/option_pricing/model.py:847`, `ml/option_pricing/model.py:895`, `ml/option_pricing/model.py:902`

**Confirmed:** Strategy constructs historical candidate outcomes from exact entry and exit option receipts and stock BBOs, partitions decision clusters chronologically, fits a profitable-outcome classifier and expected-return residual model on training, fits Platt on calibration, and evaluates/reuses by full evidence fingerprint. `ml/strategy_selection/runtime.py:351`, `ml/strategy_selection/model.py:194`, `ml/strategy_selection/model.py:330`, `ml/strategy_selection/model.py:352`

### 9. Horizon prediction publication

**Confirmed:** Loop B scores assessment rows as `BACKTEST` and causally actionable latest rows as `LIVE`, carries still-active verified prior LIVE forecasts once, evaluates only exact matching target/cost/contracts, and rejects post-entry predictions. `ml/runtime_pipeline.py:493`, `ml/runtime_pipeline.py:503`, `ml/runtime_pipeline.py:612`, `ml/runtime_pipeline.py:3318`

Before promotion, actual publication time must remain strictly before the actionable deadline. If it does not, the new run fails and the prior pointer is unchanged. A complete immutable run includes samples, predictions, evaluations, monitoring, intelligence, and a manifest; its durable receipt is verified before the sole atomic pointer replacement. `ml/runtime_pipeline.py:590`, `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:794`, `ml/runtime_pipeline.py:943`

Directional outputs carry raw and calibrated probabilities in `[0,1]`, target window and availability clocks, model/calibration identity, mode/status, and cost contract. Evaluation outputs carry exact observed labels, log loss, Brier score, and correctness; monitoring adds calibration gap, ROC AUC, coverage, and model-reuse rate. `ml/parquet_contracts.py:131`, `ml/parquet_contracts.py:156`, `ml/runtime_pipeline.py:3409`, `ml/runtime_pipeline.py:3460`, `ml/runtime_pipeline.py:3633`

### 10. Strategy construction, prediction, ranking, publication, and UI

**Confirmed:** Strategy reads one verified current Loop B run and uses its causal input cutoff, samples, and directional probabilities. For each LIVE prediction it selects an OPRA-first, provider-neutral point-in-time chain receipt between information availability and target entry, constructs registered candidates, attaches exact-contract Pricing evidence before market-state/model scoring, and uses a calibrated fitted model only when active Pricing covers every leg. Schwab remains an explicitly labeled broker/underlying-quote fallback. `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:91`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:267`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/runtime.py:310`

When the fitted model is unavailable or full Pricing coverage is absent, the candidate remains explicitly `PRICING_SCENARIO_FALLBACK`; the raw scenario probability becomes the decision score and calibrated probability remains null. Fitted rows use calibrated profitable-outcome probability as the decision score, then rank by probability, expected return on risk, and stable candidate key. `ml/strategy_runtime.py:527`, `ml/strategy_runtime.py:565`, `ml/strategy_selection/model.py:442`, `ml/strategy_selection/model.py:466`

Strategy writes candidates, audit, model reports and copied model artifacts, then binds the manifest, Loop B source record, option receipts, and stock BBO lineage into an atomic publication receipt/pointer. `ml/strategy_runtime.py:163`, `ml/strategy_runtime.py:181`, `ml/strategy_runtime.py:217`, `ml/strategy_publication.py:40`

**Confirmed:** the Rolling Forecast UI resolves the authoritative Loop B pointer; the Options Strategy UI resolves the Strategy pointer and validates candidate schema/policy. Neither is a writer or an automated-action owner. `app/ui/rolling_forecast_data.py:539`, `app/ui/options_strategy_data.py:628`, `app/ui/rolling_forecast_data.py:408`

## Scheduling and ordinary phase order

| Loop | Implemented production cadence/phase | Same-cycle relationship | Status |
|---|---|---|---|
| CME/L2 | 5 s MBP, 15 s BBO, 60 s OHLCV with +0/+2/+1 s phases | Independent rolling context | **Confirmed.** `datafetching/cme_runtime.py:37`, `datafetching/cme_runtime.py:42` |
| Loop A | 15-minute boundary then +20 s after each completed recurring cycle | Readiness precedes slower Loop A work | **Confirmed.** `datafetching/orchestrate.py:199`, `datafetching/orchestrate.py:210`, `datafetching/orchestrate.py:267` |
| Active Pricing | 15 minutes, +1 | Waits up to 30 s in the startup command for exact Loop A readiness | **Confirmed.** `docs/datafetch-ml/current_start_command:89`, `docs/datafetch-ml/current_start_command:92` |
| Directional Loop B | 15 minutes, +5 | Reads only a complete Loop A cycle | **Confirmed.** `docs/datafetch-ml/current_start_command:141`, `ml/prediction_runtime.py:209` |
| Options Capture | 15 minutes, +6 | Bounded 45 s Pricing barrier; documented after B’s +5 clock but no B artifact read | **Confirmed timing; documented-only B association.** `docs/datafetch-ml/current_start_command:95`, `docs/datafetch-ml/current_start_command:113` |
| Strategy | 15 minutes, +10 | Reads current Loop B and current option heads; skips unchanged tuple | **Confirmed.** `docs/datafetch-ml/current_start_command:152`, `ml/strategy_runtime.py:414` |
| Daily ALFRED | at most once per date; 07:00 UTC next boundary | Asynchronous historical authority, not a quarter-hour phase | **Confirmed.** `datafetching/fred_alfred_runtime.py:53`, `docs/datafetch-ml/current_start_command:67` |

**Inferred:** under normal latency the ordinary quarter-hour flow is Loop A bar readiness at about `+00:20`, Pricing at `+01`, Loop B at `+05`, Options at `+06`, Strategy at `+10`. The exact completion order is not guaranteed: readiness and pricing barriers are bounded, each process has its own clock, and no central orchestrator enforces this sequence. `datafetching/bar_readiness.py:245`, `datafetching/pricing_barrier.py:77`

## Clocks, causality, and authority boundaries

| Clock/boundary | Meaning | Enforcement |
|---|---|---|
| `decision_timestamp` / `target_snapshot_for` | Calendar-owned information boundary, not receipt time | Must be an eligible XNYS quarter-hour for Pricing/Options; exact completed bars only. `datafetching/bar_readiness.py:94`, `datafetching/bar_readiness.py:97` |
| event/quote timestamp | Provider’s market-event time | Option OPRA quotes must be strictly before target; CME future evidence is rejected. `options/snapshot.py:183`, `datafetching/cme_cross_asset_context.py:168` |
| `available_at` / `first_available_at` | Earliest causal consumer availability | Family joins use it and reject component clocks after it; Pricing history selection is bounded by it. `ml/datasets/families.py:1577`, `ml/option_pricing/consumers.py:372` |
| local receipt/publication time | When immutable evidence actually became consumable | Receipts cannot predate input availability; barrier credit requires authority before Options request. `options/publication.py:195`, `datafetching/pricing_barrier.py:40` |
| label/outcome availability | Earliest time a target or strategy result may be known | Loop B label requires target end + delay; Pricing reconciliation requires post-prediction option evidence; Strategy exit receipts follow target end. `ml/rolling_samples.py:272`, `ml/option_pricing/causal.py:909`, `ml/strategy_selection/runtime.py:395` |
| closed lockbox | Sealed final-evidence interval | Model partitions report metadata only; target values are redacted/unread. `ml/model_runtime.py:745`, `ml/option_pricing_runtime.py:2229` |

## Locks and single-writer rules

**Confirmed:** CME, ALFRED, Pricing, Options, and Strategy use the shared exclusive runtime-lock helper, which creates an `O_EXCL` lock, reclaims it once only when its recorded PID is dead, and removes it on normal exit. `datafetching/runtime_lock.py:12`, `datafetching/runtime_lock.py:20`, `datafetching/runtime_lock.py:37`

**Confirmed:** Loop A and Directional Loop B use their own `O_EXCL` supervisor locks without stale-PID recovery; they additionally share an OS file lock around Loop A writes and complete Loop B reads. The OS lock releases on process exit even though the marker file persists. `datafetching/orchestrate.py:496`, `ml/prediction_runtime.py:317`, `datafetching/loop_a_cycle.py:198`

**Confirmed:** the Pricing child has a separate collision lock but cannot move target authority directly; it publishes only future-consumable materialization/model/status artifacts. `ml/option_pricing_loop_native_worker.py:50`, `ml/option_pricing_loop_native_worker.py:87`

## Failure and degradation propagation

| Condition | Implemented propagation | Prediction effect |
|---|---|---|
| Loop A target bar missing by Pricing deadline | Pricing retries the exact target until one monotonic deadline, then skips without changing current authority. `ml/option_pricing_runtime.py:1681`, `ml/option_pricing_runtime.py:1713` | No new option-pricing authority for that target; prior authority remains. |
| Loop A target bar missing for Options | Canonical OPRA can still commit using its own causal clocks. If OPRA is transiently unavailable, the single Schwab fallback is sealed pending and reconciled if readiness arrives inside 1,200 s. `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:452`, `datafetching/options_runtime.py:490`, `options/pending_capture.py:450` | OPRA evidence remains available for later causal Pricing; no pending Schwab feature is usable until committed. |
| Pricing barrier missing/timed out | Barrier returns status rather than raising; Options still captures, but prospective Pricing-before-request credit is false. `datafetching/pricing_barrier.py:121`, `datafetching/pricing_barrier.py:181` | Options continues; later evaluation cannot claim invalid causal sequencing. |
| Pricing model missing/stale/out-of-support | Fast target keeps the Black–Scholes point estimate; the sidecar publishes explicit fallback status and wider fallback uncertainty. `ml/option_pricing_runtime.py:1143`, `ml/option_pricing/prediction.py:289`, `ml/option_pricing_runtime.py:1244` | Option prices remain available at baseline quality; no model-based residual lift. |
| Pricing evidence missing/stale in Loop B | `opx__` becomes audited null, then the family gate uses the versioned non-Pricing baseline. `ml/rolling_materialization.py:676`, `ml/runtime_pipeline.py:455` | Directional publication can continue without Pricing features. |
| Pricing authority corrupt or ALFRED readiness invalid | The shared contract exception is re-raised instead of converted into a partial route. `ml/rolling_materialization.py:322` | Whole new Loop B publication aborts; prior pointer remains. |
| Optional feature is stale but contract-valid | Point-in-time joins null the feature after freshness/quality rules. `ml/datasets/families.py:1026`, `ml/datasets/families.py:1577` | Model preprocessing/family gate determines degradation; no silent stale substitution. |
| Some Loop B routes fail | Production default permits successful routes; an empty prediction set or explicit `require_all_routes` fails the run. `ml/runtime_pipeline.py:633`, `ml/runtime_pipeline.py:654` | Partial current coverage is possible; failed routes are manifest-audited. |
| Loop B publication crosses entry deadline or promotion fails | Transaction aborts/rolls back before pointer replacement. `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:1067` | Prior directional authority remains consumable. |
| Strategy lacks chain history/entry receipt | Route is audit-only and candidate construction skips. `ml/strategy_selection/runtime.py:230`, `ml/strategy_selection/runtime.py:247` | No new candidate predictions for the affected route. |
| Strategy lacks full active Pricing coverage/model | Candidate uses explicit scenario-prior score; fitted calibration is not fabricated. `ml/strategy_selection/runtime.py:306`, `ml/strategy_runtime.py:565` | Strategy output may continue at fallback quality. |
| Runtime crash during staging | Private staging has no pointer/receipt authority; readers remain on the previous verified generation. `datafetching/bar_readiness.py:173`, `ml/option_pricing_runtime.py:2393`, `ml/runtime_pipeline.py:943` | No partial publication becomes authoritative. |

## Activation gates and what code presence does not prove

- **Confirmed:** a feature column is not active merely because it exists; the registry resolves horizon-specific active sets, and Pricing-family admission additionally requires coverage/freshness. `ml/feature_registry.py:1001`, `ml/runtime_pipeline.py:432`
- **Confirmed:** option-pricing research eligibility and quality reports cannot block a valid Black–Scholes target publication. `ml/option_pricing_runtime.py:360`
- **Confirmed:** Strategy's active fitted model admits only candidates with ready BSGP or explicit Black–Scholes evidence and complete leg coverage; live mode rejects offline replay. `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:637`
- **Confirmed:** Loop B's production-selected Pricing family is not admitted merely because `opx__` columns exist; insufficient coverage/freshness selects the exact registered non-Pricing baseline feature contract while preserving the other features. `ml/runtime_pipeline.py:432`, `ml/runtime_pipeline.py:455`, `tests/test_runtime_ui_integration.py:154`
- **Unknown operational state:** local code/tests do not establish live credential entitlement/connectivity, contract-definition completeness in the real stream, historical import completion, prospective data coverage/provider share, gate state, model reuse, realized accuracy, or whether any pointer exists in the production datastore.
