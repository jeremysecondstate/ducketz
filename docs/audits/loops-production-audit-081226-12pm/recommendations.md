# Recommendations

## 1. Prediction accuracy and precision

### Gate enriched feature profiles by causal coverage

- **Current evidence:** The active profile assembles technical, fundamental, lifecycle, quote, option, pricing, energy, macro, SEC, and CME families (`ml/rolling_materialization.py:415-818`). In the immutable audited datastore run `ml/runs/20260812T182857.767187Z`, the 92,052-row sample matrix had all `opx__`, `macro__`, and `sec__` values null; CME, option, and quote families were more than 99.8% null. The current preprocessing path imputes missing features before a single model is fit per horizon (`ml/preprocessing.py:25-103`; `ml/runtime_pipeline.py:379-440`).
- **Recommendation:** Maintain a causally available baseline feature profile and separate enriched profiles whose families are admitted per horizon only after predefined training/assessment coverage. Include missingness indicators only where their incremental value survives the same temporal validation.
- **Why it should improve accuracy/precision:** It prevents almost-empty families from changing preprocessing/model geometry while allowing genuinely populated CME/options/pricing features to contribute when enough point-in-time history exists.
- **How to validate it:** Compare baseline, current active profile, and coverage-gated enriched profiles in identical rolling-origin folds, with the admission rule computed from each training fold only.
- **Primary metric(s):** Out-of-sample Brier score, log loss, precision at the actionable probability threshold, calibration error, and causal feature coverage.

### Build an append-only verified pricing-surface history for Loop B

- **Current evidence:** Loop B selects one Pricing publication through `ml/option-pricing-latest/run.json` and reads that generation's `pricing-surfaces.parquet` (`ml/option_pricing/consumers.py:49-89`). It then joins that small current surface across historical decision rows (`ml/rolling_materialization.py:648-707`). The audited Loop B sample artifact consequently had 100% null `opx__` model values even though immutable older Pricing generations exist.
- **Recommendation:** Materialize a receipt-verified, append-only compact surface history from every authoritative Pricing generation, preserving `target_snapshot_for`, first availability, source receipt checksum, schema/policy version, and supersession rules. Have Loop B read the history as of its causal cutoff instead of only the current generation.
- **Why it should improve accuracy/precision:** It supplies historical option-pricing features to training and assessment rows under the same availability contract used live, enabling the model to estimate their signal rather than learning from imputed nulls.
- **How to validate it:** Backfill only from immutable receipts, rerun rolling-origin evaluation, and compare both coverage and predictive metrics against the baseline/current-pointer join.
- **Primary metric(s):** Non-null `opx__` coverage by horizon, Brier score, log loss, precision at threshold, and calibration error.

### Select model and calibrator per horizon with nested temporal validation

- **Current evidence:** Production fixes every horizon to logistic regression and Platt calibration (`docs/datafetch-ml/current_start_command:98-109`; `ml/runtime_pipeline.py:379-440`), while the registry supports multiple model families/calibration paths (`ml/models/registry.py:1-119`; `ml/calibration.py:1-170`). On `ml/runs/20260812T182857.767187Z`, aggregate assessment Brier scores ranged from `0.242455` to `0.260854`; seven of nine horizon accuracies were below 0.50.
- **Recommendation:** For each horizon, select among the already supported regularized logistic/tree families and `none`/Platt/isotonic calibration through nested rolling-origin validation. Lock the selected specification before the final untouched assessment window and require improvement over the horizon's empirical-prior baseline.
- **Why it should improve accuracy/precision:** Horizon-specific nonlinearities and calibration sample sizes differ; temporal selection can exploit those differences without choosing on the final assessment period.
- **How to validate it:** Use purged walk-forward outer folds and training-only inner folds, compare against the current fixed route and empirical-prior predictor, then confirm on a later untouched period.
- **Primary metric(s):** Brier skill score versus empirical prior, log loss, precision/recall at the action threshold, expected calibration error, and fold-to-fold dispersion.

### Accumulate receipt-proven Strategy outcomes before fitting the profitability model

- **Current evidence:** `ml/strategy-runs/20260812T184000.082911Z/strategy-model-reports.json` records zero trained/reused models and zero complete observed-BBO outcomes for every horizon; entry receipt unavailability accounts for the rejected history. All 5,600 published candidates use `model_status=PRICING_SCENARIO`. The code requires committed entry and exit option evidence and at least 378 decision clusters (`ml/strategy_selection/runtime.py:104-220`; `ml/strategy_selection/model.py:28-73`).
- **Recommendation:** Persist an incremental, receipt-keyed outcome table as committed chain history accumulates, and train the profitability classifier only after the existing cluster and entry/exit evidence requirements are satisfied. Retain the current scenario route as the baseline until that point.
- **Why it should improve accuracy/precision:** A classifier trained on actual spread BBO entry/exit outcomes can estimate realized profitable-outcome probability; the current scenario status is not a fitted probability model.
- **How to validate it:** Freeze the first eligible training cohort, score only later receipt-complete decisions, and compare ranked candidates with the existing pricing-scenario ranking.
- **Primary metric(s):** Precision among top-ranked candidates, realized net-profit hit rate, Brier score, log loss, and calibration by probability decile.

### Use point-in-time liquidity costs in directional labels

- **Current evidence:** Production applies one fixed `0.001` round-trip cost to every symbol, horizon, and timestamp (`docs/datafetch-ml/current_start_command:102-109`), while Loop A already persists causal bid/ask spread and quote-quality observations (`datafetching/quote_liquidity.py:91-197`). Target construction subtracts the configured constant (`ml/horizons.py:198-245`; `ml/rolling_samples.py:240-330`).
- **Recommendation:** Define a new versioned target specification whose training-only cost estimate is derived from point-in-time quote spread/liquidity by symbol and decision time, with the fixed 10 bps value retained as a no-quote fallback.
- **Why it should improve accuracy/precision:** Labels would distinguish gross directional moves from moves that clear the observed execution-cost hurdle, aligning the predicted class with actionable profitability.
- **How to validate it:** Build both label versions from identical causal rows, use the same rolling folds/models, and evaluate future net-of-observed-spread outcomes without retuning on the final period.
- **Primary metric(s):** Precision for positive net returns, net-return hit rate, Brier score, log loss, and turnover-adjusted return.

## 2. Whole-system speed and efficiency

### Remove the unconditional 20-second Loop A phase delay

- **Current bottleneck/evidence:** After waiting for each 15-minute boundary, Loop A sleeps an additional 20 seconds before beginning the cycle (`datafetching/orchestrate.py:199-211`). Pricing targets `+1`, Loop B `+5`, Options `+6`, and Strategy `+10` (`docs/datafetch-ml/current_start_command:58-121`).
- **Recommendation:** Start the Loop A fast Databento lane at the boundary and make any provider-specific settlement allowance part of that lane's completed-bar clock, rather than a process-wide fixed delay.
- **System-wide benefit:** Exact bar readiness and the complete-cycle authority become available up to 20 seconds earlier, advancing both the Pricing/Options coordination path and the Loop A -> Loop B -> Strategy critical path.
- **How to measure the improvement:** Compare boundary-to-bar-readiness, boundary-to-Loop-A-complete, boundary-to-Loop-B-pointer, and boundary-to-Strategy-pointer distributions before and after.
- **Primary metric(s):** p50/p95 target-to-publication latency for bar readiness, Loop B, and Strategy.

### Publish the compact Pricing surface on the fast target path

- **Current bottleneck/evidence:** Active Pricing publishes the fast target outcome first, then runs eligibility, materialization/model, evaluation, monitoring, and report preparation before writing `pricing-surfaces.parquet` and advancing `ml/option-pricing-latest/run.json` (`ml/option_pricing_runtime.py:316-390`, `ml/option_pricing_runtime.py:417-883`). Loop B at `+5` consumes the latter authority, not the fast target outcome (`ml/rolling_materialization.py:648-707`).
- **Recommendation:** Produce and receipt-publish the compact v2 surface from the already computed target-causal rows immediately after the target outcome. Keep cumulative research/model/evaluation generation on the existing tail/worker path and link both publications by target and checksum.
- **System-wide benefit:** Loop B can consume current-boundary pricing features without waiting for Pricing's research tail, which also advances downstream Strategy and prevents repeated consumption of the previous surface.
- **How to measure the improvement:** Trace target timestamp through target outcome, compact surface, Loop B pointer, and Strategy pointer for the same boundary.
- **Primary metric(s):** target-to-compact-surface p50/p95, percentage of Loop B cycles using same-boundary pricing, target-to-Strategy latency.

### Incrementally materialize Loop B samples

- **Current bottleneck/evidence:** Every Loop B cycle rematerializes all symbols and nine effective horizons, then projects and partitions the full frame (`ml/runtime_pipeline.py:282-325`, `ml/runtime_pipeline.py:379-440`). The audited immutable run contains 92,052 sample rows; the feature assembler repeatedly resolves/globs and reads each persisted family, although in-cycle caches avoid some duplicate reads (`ml/rolling_materialization.py:415-818`).
- **Recommendation:** Key a persisted materialization cache by source receipt/checksum, feature-profile version, symbol, horizon, and causal cutoff. Reuse unchanged historical joins and append/recompute only decisions affected by new Loop A/CME/Options/Pricing artifacts.
- **System-wide benefit:** It shortens the Loop B critical path to `ml/latest/run.json`, reduces datastore I/O/CPU contention with the other owners, and lets Strategy begin sooner.
- **How to measure the improvement:** Record rows recomputed versus reused, bytes read, CPU time, peak memory, and Loop B pointer latency over matched cycles.
- **Primary metric(s):** p50/p95 Loop B runtime, bytes read per cycle, rows recomputed per new target, CPU-seconds, peak memory.

### Put slow-changing Loop A providers on evidence-driven cadences

- **Current bottleneck/evidence:** The 15-minute Loop A provider list invokes FMP corporate/statements/growth, FMP macro/commodities, FRED GDP/CPI/unemployment/FEDFUNDS, and SEC filing discovery/document scans on every cycle (`docs/datafetch-ml/current_start_command:40-46`; `app/services/fmp_corporate_data.py:46-88`; `app/services/fmp_macro_context.py:239-260`; `datafetching/fred_fetch.py:65-100`; `app/services/sec_capital_structure.py:120-194`). Loop A completion waits for those lanes and then runs per-symbol calculations (`datafetching/orchestrate.py:360-449`).
- **Recommendation:** Keep Databento bars and Schwab quotes on the fast cadence; schedule corporate statements, macro series, and filing scans from their persisted continuation watermark/release cadence, reusing the last verified artifacts between due runs.
- **System-wide benefit:** Loop A complete-cycle publication reaches Loop B earlier with fewer provider calls, downloads, Parquet upserts, and recalculations, reducing contention for Pricing/Options/CME datastore access.
- **How to measure the improvement:** Compare provider request counts, bytes written, Loop A tail duration, and downstream Loop B/Strategy publication times across equivalent market sessions.
- **Primary metric(s):** requests per 15-minute boundary, Loop A p50/p95 completion time, Parquet bytes written, target-to-Strategy latency.

### Persist the Strategy outcome index incrementally

- **Current bottleneck/evidence:** Strategy revisits Loop B history and option snapshot history to construct observed outcomes on each new source/snapshot head (`ml/strategy_runtime.py:63-139`, `ml/strategy_selection/runtime.py:104-220`). The current in-process outcome cache is bounded and process-local (`ml/strategy_selection/runtime.py:57-65`, `ml/strategy_selection/runtime.py:520-570`); the audited report shows tens of thousands of rows reconsidered while zero complete outcomes were produced.
- **Recommendation:** Persist a content-addressed outcome index keyed by Loop B sample identity plus entry/exit option-receipt checksums. On each cycle, evaluate only newly satisfiable identities and reuse immutable completed outcomes.
- **System-wide benefit:** It reduces repeated chain scans and joins at the terminal stage of the Loops pipeline, lowering shared datastore load and shortening target-to-Strategy publication time as history grows.
- **How to measure the improvement:** Compare chain files opened, rows reconsidered, cache reuse, Strategy CPU time, and boundary-to-Strategy-pointer latency.
- **Primary metric(s):** p50/p95 Strategy runtime, option files read per cycle, new-versus-reused outcome rows, CPU-seconds, peak memory.

