# Read-only forensic report

The audit is complete. I accessed both the repository and datastore, made no changes, and confirmed the premise: all 22 columns are null in every one of the 92,052 published sample rows.

This is not one generic “insufficient data” problem. Four different production failures are involved:

- Pricing features were calculated, but Loop B read one old publication and rejected every row as unavailable, stale, or low quality. Three uncertainty metrics were already null upstream.
- Macro data was fetched, but Loop B used a single current FRED receipt instead of historical vintages.
- SEC events existed, but low-quality events poisoned grouped joins; the offering ratio also had no market-cap denominator.
- CME data was fetched, but hourly derivation never completed. Loop B fell back to an old snapshot that was unavailable or stale for every decision.

## Independent verification

### Sample artifact

`C:\DATASTORE\ml\runs\20260812T182857.767187Z\samples.parquet` contains:

- 92,052 rows
- 182 columns
- One Parquet row group
- All audited columns encoded as Arrow `double`/pandas `float64`

The run was published at `2026-08-12T18:28:57.767187Z`, with causal input cutoff `2026-08-12T18:28:57.614484Z`, seven symbols, nine horizons, and no route exceptions. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\manifest.json — $.run_timestamp; $.configuration.causal_input_cutoff; $.configuration.symbols; $.configuration.horizons; $.configuration.route_errors`.

Here, `1w*` means `1w` plus `1w-d1` through `1w-d5`.

| #   | Column                                      | Type   | Non-null | Null   | Null %   | Actually selected by models |
| ---:| ------------------------------------------- | ------ | --------:| ------:| --------:| --------------------------- |
| 1   | `opx__causal_coverage`                      | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 2   | `opx__median_normalized_residual`           | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 3   | `opx__median_predictive_standard_deviation` | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 4   | `opx__median_model_edge_in_half_spreads`    | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 5   | `opx__positive_edge_fraction`               | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 6   | `opx__negative_edge_fraction`               | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 7   | `opx__raw_arbitrage_violation_rate`         | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 8   | `opx__constrained_arbitrage_violation_rate` | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 9   | `opx__interval_80_coverage`                 | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 10  | `opx__interval_95_coverage`                 | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 11  | `opx__median_relative_bid_ask_spread`       | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d, 1w*`           |
| 12  | `macro__fed_funds_level`                    | double | 0        | 92,052 | 100.000% | `1d, 1w*`                   |
| 13  | `macro__cpi_yoy`                            | double | 0        | 92,052 | 100.000% | `1d, 1w*`                   |
| 14  | `macro__unemployment_change`                | double | 0        | 92,052 | 100.000% | `1d, 1w*`                   |
| 15  | `macro__gdp_yoy`                            | double | 0        | 92,052 | 100.000% | `1d, 1w*`                   |
| 16  | `sec__dilution_event`                       | double | 0        | 92,052 | 100.000% | `1d, 1w*`                   |
| 17  | `sec__offering_size_to_market_cap`          | double | 0        | 92,052 | 100.000% | `1d, 1w*`                   |
| 18  | `sec__filing_event_impulse`                 | double | 0        | 92,052 | 100.000% | `1d, 1w*`                   |
| 19  | `cme__nq_return_1h`                         | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d`                |
| 20  | `cme__es_return_1h`                         | double | 0        | 92,052 | 100.000% | `1h, 4h, 1d`                |
| 21  | `cme__relative_spread`                      | double | 0        | 92,052 | 100.000% | `1h, 4h`                    |
| 22  | `cme__book_imbalance`                       | double | 0        | 92,052 | 100.000% | `1h, 4h`                    |

Source for every count: `C:\DATASTORE\ml\runs\20260812T182857.767187Z\samples.parquet — named column: 0 non-null of 92,052 rows`. All 22 also appear in `C:\DATASTORE\ml\runs\20260812T182857.767187Z\manifest.json — $.feature_columns`.

### Schema presence versus actual model selection

The run creates one union of every selected horizon’s features, assigns every feature Arrow `double`, projects that union, and writes it to `samples.parquet`. Therefore, presence in the Parquet schema does not mean every horizon used the column. `ml/runtime_pipeline.py:L319-L328 — run_runtime_pipeline`; `ml/parquet_contracts.py:L620-L632 — sample_schema`; `ml/runtime_pipeline.py:L1986-L1998 — _feature_columns`; `ml/runtime_pipeline.py:L2083-L2092 — _project_samples`; `ml/runtime_pipeline.py:L616-L624 — run_runtime_pipeline`.

I independently inspected `$.feature_columns` in these nine model manifests:

- `C:\DATASTORE\ml\models\1h\logistic-1h\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\4h\logistic-4h\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\1d\logistic-1d\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\1w\logistic-1w\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\1w-d1\logistic-1w-d1\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\1w-d2\logistic-1w-d2\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\1w-d3\logistic-1w-d3\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\1w-d4\logistic-1w-d4\20260812T182857.767187Z\manifest.json`
- `C:\DATASTORE\ml\models\1w-d5\logistic-1w-d5\20260812T182857.767187Z\manifest.json`

The weekly offset routes reuse the `1w` feature contract. `ml/horizons.py:L30-L34 — feature_contract_horizon`.

### Null preprocessing

For every audited metric selected by a model:

1. Non-finite values become null.
2. The numeric branch uses median imputation and preserves entirely empty columns.
3. A separate branch creates a missingness indicator for every numeric feature. `ml/models/registry.py:L167-L198 — _replace_non_finite; _linear_preprocessor`; `ml/models/registry.py:L199-L215 — _linear_preprocessor`.

Inspection of the matching nine `model.joblib` files established that each selected audited metric had:

- Fitted imputer statistic `0.0`
- Numeric input therefore replaced by constant zero
- Missingness indicator present and constant one

Artifact locations: the matching `model.joblib` beside each model manifest above — `estimator.named_steps.preprocess.named_transformers_.numeric.named_steps.impute.statistics_` and `estimator.named_steps.preprocess.named_transformers_.missing.named_steps.indicator.features_`.

The base registry labels these families as insufficient coverage or needing point-in-time history, but the active-profile helper forcibly changes applicable features to `ACTIVE`. `ml/feature_registry.py:L75-L117 — _candidate_feature`; `ml/feature_registry.py:L954-L968 — _active_features_for_horizon`; `ml/feature_registry.py:L1166-L1188 — active-v2 feature sets`.

## Metric-by-metric audit

### 1. `opx__causal_coverage`

- **Meaning:** Within an option-surface bucket, this is the fraction of contract predictions whose status is `AVAILABLE` or `CREATED`. It is not Loop B’s historical join coverage.
- **Why the model might care:** High values could indicate that the option surface was broadly usable rather than being based on a few successful contracts.
- **Original data source:** Schwab Market Data `/chains`; relevant raw fields include symbol, strike, expiration, underlying price, bid/ask, implied volatility, rates, quote clocks, and contract status. Raw and normalized snapshots are expected under `stocks\<symbol>\options\...`; aggregate results are stored in `ml\option-pricing-runs\<run>\pricing-surfaces.parquet`. `app/services/schwab.py:L26-L29 — MARKETDATA_BASE_URL`; `datafetching/schwab_fetch.py:L96-L114 — get_option_chain_snapshot`; `options/snapshot.py:L219-L230 — normalize_schwab_option_chain`; `options/snapshot.py:L248-L264 — normalize_schwab_option_chain`.
- **Fetching path:** `datafetching/options_runtime.py:L339-L386 — run_options_cycle` fetches and commits Schwab chains; `options/snapshot.py:L128-L150 — _persist_schwab_option_snapshot` selects paths and receipt clocks; `options/snapshot.py:L171-L187 — _persist_schwab_option_snapshot` publishes the immutable snapshot. Pricing itself consumes committed evidence and reports zero external-provider calls. `ml/option_pricing_loop_native_worker.py:L95-L104 — run_loop_native_worker_once`.
- **Calculation:** For each symbol/target/call-or-put/expiry/moneyness bucket, `valid_predictions / contracts`. Loop B then takes a contract-count-weighted average of bucket values for each symbol and target. Unit: fraction `[0,1]`; higher is more complete. `ml/option_pricing/reporting.py:L90-L110 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L115-L128 — build_pricing_surfaces`; `ml/option_pricing/consumers.py:L223-L256 — read_verified_compact_pricing_features`.
- **Path into Loop B:** Schwab snapshot → pricing predictions → `pricing-surfaces.parquet` → verified pointer/receipt reader → symbol/target backward-as-of join → union sample projection → `samples.parquet`. Availability must be receipt-verified and no later than the decision; freshness is 2h/4h/2d/8d. `ml/feature_registry.py:L873-L896 — _option_pricing_shadow`; `ml/option_pricing/consumers.py:L49-L89 — read_verified_compact_pricing_features`; `ml/rolling_materialization.py:L648-L704 — _attach_loop_a_features`; `ml/rolling_materialization.py:L900-L912 — _join_symbol_values`; `ml/datasets/point_in_time.py:L600-L625 — _finalize_join`.
- **Audited evidence:** Sample: 0 non-null, 92,052 null. The selected legacy surface had 110/110 non-null values. The run’s 63-route pricing audit reported all pricing values missing in all 100,333 pre-lockbox route rows: 100,293 `NO_PRIOR_PUBLICATION`, 32 `STALE`, and 8 `QUALITY_REJECTED`. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\samples.parquet — opx__causal_coverage`; `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — causal_coverage: 110 non-null of 110`; run manifest — `$.configuration.pricing_evidence.routes`.
- **Why it is null:** The calculation succeeded. The first loss is the point-in-time join. Loop B selected only the publication dated `2026-08-11T20:06:12Z`, containing Aug 10 targets. Daily/weekly samples ended at Aug 11 20:05—one minute before publication. Later intraday decisions saw the surface, but its market target was already beyond the 2h/4h limit. A newer v2 run published before Loop B’s cutoff existed, but the authority selected by Loop B remained on the old v1 generation. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\publication.json — $.published_at; $.run_path`; `C:\DATASTORE\ml\option-pricing-runs\20260812T172704.806740Z\publication.json — $.published_at`.
- **Model effect today:** Selected by all nine horizons. It was passed as an all-null column, numerically replaced by zero, and accompanied by a constant-one missingness indicator. It was not excluded.
- **Potential fixes:** Immediate: prevent the current pricing pointer from moving to an older generation and restore the newest verified v2 authority. Durable: build an append-only, receipt-verified history across every pricing generation; the current reader selects one generation from the current chain rather than materializing history. Backfill: reconstruct surface history from immutable publication receipts. `ml/option_pricing/publication.py:L206-L243 — read_option_pricing_publication_at`; `docs/audits/loops-production-audit-081226-12pm/recommendations.md:L14-L21`.
- **How to verify the fix:** Rerun Loop B and require `opx__causal_coverage` to become non-null for decisions after each surface’s verified first availability, while remaining null before publication and after freshness expiry. Inspect the new `samples.parquet` column and `manifest.json — $.configuration.pricing_evidence.routes.*.join_status_counts`.
- **Confidence:** `Proven` — immutable surface values, publication clocks, route statuses, and sample output establish the failure. The identity of the process that left or restored the pointer on v1 remains unresolved.

### 2. `opx__median_normalized_residual`

- **Meaning:** The median difference between the observed option midpoint and its Black-Scholes price, divided by the underlying stock price. Positive means the market option was richer than the Black-Scholes baseline.
- **Why the model might care:** It could capture persistent option-market richness or cheapness associated with expectations not represented by the baseline formula.
- **Original data source:** Schwab `/chains` supplies the underlying, option bid/ask, strike, expiry, implied volatility, rate, yield, and quote timestamps. The expected artifacts are immutable Schwab snapshots, `pricing-predictions.parquet`, `pricing-evaluations.parquet`, and `pricing-surfaces.parquet`. `datafetching/schwab_fetch.py:L96-L114 — get_option_chain_snapshot`; `options/snapshot.py:L305-L340 — normalize_schwab_option_chain`.
- **Fetching path:** Options runtime obtains and persists the chain; pricing later evaluates predicted contracts against observed bid/ask evidence. `datafetching/options_runtime.py:L339-L386 — run_options_cycle`; `options/snapshot.py:L171-L187 — _persist_schwab_option_snapshot`; `ml/option_pricing/reporting.py:L41-L67 — build_pricing_surfaces`.
- **Calculation:** `observed_normalized_residual = (observed_midpoint − Black-Scholes price) / underlying price`; the bucket median is calculated, then bucket medians are contract-count weighted for Loop B. Dimensionless; snapshot-specific; requires a valid observed bid, ask, underlying, and Black-Scholes value. `ml/option_pricing/causal.py:L907-L920 — _evaluation_row`; `ml/option_pricing/reporting.py:L127-L134 — build_pricing_surfaces`; `ml/option_pricing/consumers.py:L223-L256 — read_verified_compact_pricing_features`.
- **Path into Loop B:** Committed Schwab chain → pricing prediction/evaluation → compact surface → verified publication reader → freshness- and quality-checked symbol as-of join → sample projection. `ml/option_pricing/consumers.py:L154-L194 — read_verified_compact_pricing_features`; `ml/rolling_materialization.py:L648-L704 — _attach_loop_a_features`; `ml/runtime_pipeline.py:L319-L328 — run_runtime_pipeline`.
- **Audited evidence:** Sample: 0/92,052 non-null. The selected surface had 56/110 non-null bucket medians; its evaluations had 2,559/5,309 residuals. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\samples.parquet — opx__median_normalized_residual`; `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — median_normalized_residual: 56 non-null of 110`; corresponding `pricing-evaluations.parquet — observed_normalized_residual: 2,559 non-null of 5,309`.
- **Why it is null:** Usable upstream values existed. They first disappear at the Loop B point-in-time eligibility mask for the same publication-time, target-freshness, and six low-quality surface reasons described by the route audit. This was not an empty fetch, formula `NaN` across all rows, column mismatch, or caught provider exception.
- **Model effect today:** Selected by every horizon, imputed to numeric zero, and represented by a constant-one missingness indicator.
- **Potential fixes:** Immediate: use a monotonic newest verified authority. Durable: join an append-only history of every verified surface rather than one generation. Backfill: retain earlier evaluated surfaces and their original receipt clocks.
- **How to verify the fix:** Confirm non-null values only where an evaluated surface was already published and fresh; independently recompute one row from `observed_bid`, `observed_ask`, `black_scholes_price`, and `underlying_price`, then compare it with the new sample value after bucket weighting.
- **Confidence:** `Proven` — the immutable selected surface contains values and the immutable route audit identifies the downstream exclusions.

### 3. `opx__median_predictive_standard_deviation`

- **Meaning:** The median model-estimated uncertainty of option fair values, in option-price dollars per share. Larger values mean less certainty.
- **Why the model might care:** Pricing uncertainty could distinguish stable option signals from noisy ones.
- **Original data source:** The prediction’s causal option inputs come from Schwab. A fitted residual model is expected to use receipt-proven Databento OPRA training history; the selected run had no fitted model and used a Black-Scholes baseline. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\option-pricing-model-reports.json — $.model_reports; $.models_trained; $.models_reused`.
- **Fetching path:** Schwab chains are committed by options runtime. The separate pricing worker consumes persisted evidence without making provider requests. `datafetching/options_runtime.py:L339-L386 — run_options_cycle`; `ml/option_pricing_loop_native_worker.py:L29-L52 — run_loop_native_worker_once`; `ml/option_pricing_loop_native_worker.py:L95-L104 — run_loop_native_worker_once`.
- **Calculation:** Current code multiplies normalized residual standard deviation by the underlying price, then takes the median per surface bucket. `σ_option = σ_normalized × underlying`. `ml/option_pricing/prediction.py:L147-L175 — predict_option_values`; `ml/option_pricing/reporting.py:L127-L134 — build_pricing_surfaces`.
- **Path into Loop B:** Prediction uncertainty → compact surface median → verified reader and weighted aggregation → point-in-time join → sample projection. `ml/option_pricing/consumers.py:L207-L212 — read_verified_compact_pricing_features`; `ml/option_pricing/consumers.py:L223-L256 — read_verified_compact_pricing_features`; `ml/rolling_materialization.py:L648-L704 — _attach_loop_a_features`.
- **Audited evidence:** Sample: 0/92,052. The selected run had 0/5,309 predictive standard deviations and 0/110 surface medians. A newer v2 surface had 126/236 non-null medians, but Loop B did not select that authority. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-predictions.parquet — predictive_standard_deviation: 0 non-null of 5,309`; corresponding surface — 0/110; `C:\DATASTORE\ml\option-pricing-runs\20260812T172704.806740Z\pricing-surfaces.parquet — median_predictive_standard_deviation: 126 non-null of 236`.
- **Why it is null:** Its earliest null occurs inside the selected pricing prediction artifact, before Loop B. That generation was baseline-only. The historical code active before commit `5b27880cc9bd7fa21a8db92e7e3c1f7841f44323` explicitly filled fallback uncertainty with `NaN` and emitted `None`; the selected pricing run predates that later commit. `8a2d229c69d48f80c02ddd7f67ce6bb23cf4524f:ml/option_pricing/prediction.py:L89-L115 — predict_option_values`; same historical file `L149-L172`. Current code now supplies finite fallback uncertainty. `ml/option_pricing/prediction.py:L80-L107 — predict_option_values`.
- **Model effect today:** Selected by all horizons, then imputed to zero with a constant-one missingness indicator.
- **Potential fixes:** Immediate: keep this metric excluded until Loop B reads interval-bearing v2 surfaces. Durable: version and preserve whether uncertainty came from a fitted residual model or the fallback. Backfill: regenerate only where immutable causal inputs and the historical uncertainty policy can be reproduced; do not invent uncertainty for old v1 predictions.
- **How to verify the fix:** Check `pricing-predictions.parquet — predictive_standard_deviation`, then the surface median, then `samples.parquet — opx__median_predictive_standard_deviation`. All three must become finite, with the sample still subject to publication and freshness rules.
- **Confidence:** `Proven` — the earliest null is present in the immutable prediction artifact, and historical code explains it exactly.

### 4. `opx__median_model_edge_in_half_spreads`

- **Meaning:** The median distance between modeled fair value and the observed option midpoint, measured in half bid/ask spreads. Positive means fair value is above market.
- **Why the model might care:** It could quantify whether the option market implies upward or downward information after accounting for trading friction.
- **Original data source:** Schwab option bid/ask and underlying data plus the pricing system’s constrained fair value. Expected outputs are prediction, evaluation, and surface Parquets. `options/snapshot.py:L305-L340 — normalize_schwab_option_chain`; `ml/option_pricing/causal.py:L970-L995 — _evaluation_row`.
- **Fetching path:** Schwab `/chains` → immutable snapshot → pricing evaluation; no external call occurs in the pricing worker. `datafetching/schwab_fetch.py:L96-L114 — get_option_chain_snapshot`; `options/snapshot.py:L171-L187 — _persist_schwab_option_snapshot`.
- **Calculation:** `(constrained fair value − observed midpoint) / ((ask − bid)/2)`, only for complete evaluations with a positive spread; median per bucket, then contract-count-weighted across buckets. Dimensionless. `ml/option_pricing/causal.py:L988-L995 — _evaluation_row`; `ml/option_pricing/reporting.py:L127-L136 — build_pricing_surfaces`.
- **Path into Loop B:** Evaluations → compact surface → verified generation → symbol/target as-of join → sample. Registry freshness is 2h, 4h, 2 days, or 8 days. `ml/feature_registry.py:L873-L896 — _option_pricing_shadow`; `ml/rolling_materialization.py:L683-L704 — _attach_loop_a_features`.
- **Audited evidence:** Sample: 0/92,052. Selected surface: 55/110 non-null; evaluations: 2,400/5,309 non-null. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — median_model_edge_in_half_spreads: 55 non-null of 110`; corresponding evaluations — `model_edge_in_half_spreads: 2,400 non-null of 5,309`.
- **Why it is null:** The edge existed upstream and first disappears at the Loop B join because the only selected publication was too late for daily/weekly decisions and too old by target time for later intraday decisions; eight rows also failed surface quality.
- **Model effect today:** Selected everywhere, numeric zero after imputation, plus a constant missingness flag.
- **Potential fixes:** Immediate: monotonic pointer and newest v2 authority. Durable: append-only surface history with first availability and target time. Backfill: all immutable complete evaluations, preserving their original bid/ask and publication clocks.
- **How to verify the fix:** Recalculate a contract’s half-spread edge, verify the bucket median and weighted symbol value, and require a non-null sample only at a causally eligible decision.
- **Confidence:** `Proven`.

### 5. `opx__positive_edge_fraction`

- **Meaning:** Among contracts with a calculable edge, the fraction whose modeled fair value is above the observed midpoint.
- **Why the model might care:** A broad concentration of positive option edges could be more informative than one unusually priced contract.
- **Original data source:** Schwab option bid/ask plus constrained pricing fair values; persisted through option snapshots, evaluations, and pricing surfaces. `datafetching/schwab_fetch.py:L96-L114 — get_option_chain_snapshot`; `options/snapshot.py:L305-L340 — normalize_schwab_option_chain`.
- **Fetching path:** Options runtime fetches and commits Schwab chains; pricing reads the committed data and constructs evaluations. `datafetching/options_runtime.py:L339-L386 — run_options_cycle`; `ml/option_pricing/reporting.py:L41-L67 — build_pricing_surfaces`.
- **Calculation:** `count(edge > 0) / count(non-null edge)` in each bucket, then contract-count-weighted across buckets. Unit `[0,1]`; zero edges are neither positive nor negative. `ml/option_pricing/reporting.py:L103-L110 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L127-L138 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L681-L688 — _mean_bool; _fraction`.
- **Path into Loop B:** Pricing surface → verified current generation → weighted symbol/target row → freshness/quality join → sample projection. `ml/option_pricing/consumers.py:L214-L271 — read_verified_compact_pricing_features`; `ml/rolling_materialization.py:L648-L704 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052; selected surface 55/110 non-null. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — positive_edge_fraction: 55 non-null of 110`.
- **Why it is null:** It was calculated for half the selected surface buckets, then every usable value was excluded at the historical as-of join. Authentication, empty fetching, and schema mismatch are disproved by the verified nonempty surface and `opx__source_status=VERIFIED`.
- **Model effect today:** Selected by all nine models, zero-imputed with a missingness indicator.
- **Potential fixes:** Immediate: repair pricing authority monotonicity. Durable: join all historical verified generations. Backfill: immutable edge-bearing evaluations.
- **How to verify the fix:** For a fresh post-publication decision, confirm the sample fraction equals the contract-count-weighted surface result and lies in `[0,1]`.
- **Confidence:** `Proven`.

### 6. `opx__negative_edge_fraction`

- **Meaning:** Among contracts with a valid edge, the fraction whose modeled fair value is below the observed midpoint.
- **Why the model might care:** A broad concentration of negative option edges might indicate comparatively expensive options or downside information.
- **Original data source:** Schwab option bid/ask and constrained fair values, persisted through immutable option snapshots and pricing artifacts. `options/snapshot.py:L305-L340 — normalize_schwab_option_chain`; `ml/option_pricing/causal.py:L988-L995 — _evaluation_row`.
- **Fetching path:** Schwab `/chains` is fetched by options runtime; the pricing worker uses persisted data. `datafetching/schwab_fetch.py:L96-L114 — get_option_chain_snapshot`; `datafetching/options_runtime.py:L339-L386 — run_options_cycle`.
- **Calculation:** `count(edge < 0) / count(non-null edge)` per bucket, followed by contract-count weighting. Range `[0,1]`; zero edges are excluded from both signs. `ml/option_pricing/reporting.py:L127-L138 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L686-L688 — _fraction`.
- **Path into Loop B:** Evaluation → compact surface → verified publication → point-in-time symbol join → sample. `ml/option_pricing/consumers.py:L214-L271 — read_verified_compact_pricing_features`; `ml/datasets/point_in_time.py:L600-L625 — _finalize_join`.
- **Audited evidence:** Sample 0/92,052; selected surface 55/110 non-null. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — negative_edge_fraction: 55 non-null of 110`.
- **Why it is null:** The selected surface had values, but none survived the publication, target-freshness, and quality eligibility checks.
- **Model effect today:** All horizons selected it; all received numeric zero and a constant-one missingness indicator.
- **Potential fixes:** Immediate pointer repair; durable append-only history; historical backfill from verified evaluation receipts.
- **How to verify the fix:** Check that positive and negative fractions are independently reproduced from non-null edges and become populated only for eligible decisions.
- **Confidence:** `Proven`.

### 7. `opx__raw_arbitrage_violation_rate`

- **Meaning:** The fraction of raw modeled contracts that violate at least one option-shape rule: price bounds, the expected direction across strikes, or convexity.
- **Why the model might care:** A high raw violation rate could indicate unstable pricing inputs or a model extrapolating poorly.
- **Original data source:** Schwab option surfaces plus raw model fair values and theoretical price bounds. Expected persisted artifact: `pricing-predictions.parquet`, summarized into `pricing-surfaces.parquet`. `options/snapshot.py:L305-L340 — normalize_schwab_option_chain`.
- **Fetching path:** Options runtime supplies the chain; the pricing projection layer creates violation flags. `datafetching/options_runtime.py:L339-L386 — run_options_cycle`; `ml/option_pricing/prediction.py:L597-L628 — _project_surface`.
- **Calculation:** A contract is flagged if it breaches bounds, call/put strike monotonicity, or convexity; the metric is the mean of those flags per bucket, then contract-count weighted. Range `[0,1]`. `ml/option_pricing/constraints.py:L41-L73 — shape_violations`; `ml/option_pricing/reporting.py:L90-L103 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L127-L138 — build_pricing_surfaces`.
- **Path into Loop B:** Raw violation flags → surface rate → verified reader → point-in-time join → sample. `ml/option_pricing/consumers.py:L207-L212 — read_verified_compact_pricing_features`; `ml/rolling_materialization.py:L648-L704 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052; selected surface 110/110 non-null. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — raw_arbitrage_violation_rate: 110 non-null of 110`.
- **Why it is null:** The calculation was complete for every bucket. The first loss is entirely downstream at the Loop B causal/freshness/quality join.
- **Model effect today:** Selected everywhere, zero-imputed, constant missingness indicator.
- **Potential fixes:** Immediate newest verified authority; durable multi-generation history; backfill raw projection diagnostics from immutable pricing runs.
- **How to verify the fix:** Confirm a fresh sample value equals the contract-count-weighted rate from its verified surface and lies in `[0,1]`.
- **Confidence:** `Proven`.

### 8. `opx__constrained_arbitrage_violation_rate`

- **Meaning:** The fraction of contracts still violating shape rules after fair values have been projected into valid option bounds and strike relationships.
- **Why the model might care:** A nonzero result could identify failed or unreliable constraint enforcement.
- **Original data source:** Same Schwab-derived pricing surface as the raw metric, after deterministic constraint projection. `options/snapshot.py:L305-L340 — normalize_schwab_option_chain`.
- **Fetching path:** Options runtime commits the source chain; pricing applies its local projection without another provider request. `datafetching/options_runtime.py:L339-L386 — run_options_cycle`; `ml/option_pricing_loop_native_worker.py:L95-L104 — run_loop_native_worker_once`.
- **Calculation:** The projection minimizes weighted squared movement from raw prices while enforcing bounds, monotonicity, and convexity; the post-projection violation flags are averaged. `ml/option_pricing/constraints.py:L107-L139 — project_surface_values`; `ml/option_pricing/constraints.py:L146-L166 — project_surface_values`; `ml/option_pricing/reporting.py:L95-L110 — build_pricing_surfaces`.
- **Path into Loop B:** Projection diagnostics → surface rate → weighted verified feature → historical as-of join → sample. `ml/option_pricing/consumers.py:L223-L256 — read_verified_compact_pricing_features`; `ml/rolling_materialization.py:L683-L704 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052; selected surface 110/110 non-null, all equal to zero. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — constrained_arbitrage_violation_rate: 110 non-null of 110; value 0.0 in all rows`.
- **Why it is null:** A valid upstream zero is distinct from a missing value. Every zero was discarded by the downstream join; the imputer then generated a different zero with no provenance.
- **Model effect today:** Selected everywhere; the model receives zero plus missingness one. Consequently it cannot distinguish a genuine zero-violation surface from a missing surface except through the indicator.
- **Potential fixes:** Immediate authority repair; durable surface history; backfill constraint diagnostics with provenance.
- **How to verify the fix:** A genuinely joined zero must have `opx__join_status=JOINED` and missingness false; an imputed zero must remain marked missing.
- **Confidence:** `Proven`.

### 9. `opx__interval_80_coverage`

- **Meaning:** Among completed evaluations, the fraction whose observed option midpoint fell inside the model’s constrained 80% prediction interval.
- **Why the model might care:** It could reveal whether pricing uncertainty was realistically calibrated.
- **Original data source:** Schwab observed bid/ask plus prediction interval bounds from a fitted residual model or current fallback uncertainty. Persisted in prediction/evaluation/surface artifacts. `ml/option_pricing/causal.py:L929-L936 — covered`; `ml/option_pricing/causal.py:L970-L995 — _evaluation_row`.
- **Fetching path:** Schwab chains are fetched and committed; interval evaluation is local. `datafetching/schwab_fetch.py:L96-L114 — get_option_chain_snapshot`; `options/snapshot.py:L171-L187 — _persist_schwab_option_snapshot`.
- **Calculation:** `count(observed midpoint inside constrained 80% bounds) / count(completed evaluated rows with bounds)`. Only `EVALUATED` or `COMPLETE` rows are considered. Range `[0,1]`. `ml/option_pricing/reporting.py:L103-L110 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L139-L147 — build_pricing_surfaces`.
- **Path into Loop B:** Prediction bounds → covered flag → surface coverage → verified consumer → point-in-time join → sample. `ml/option_pricing/consumers.py:L214-L271 — read_verified_compact_pricing_features`; `ml/rolling_materialization.py:L648-L704 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052. Selected predictions had no interval bounds; evaluations had 0/5,309 coverage flags; surfaces had 0/110 coverage values. Even the newer v2 surface had 0/236. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-evaluations.parquet — interval_80_covered: 0 non-null of 5,309`; corresponding surface — 0/110; `C:\DATASTORE\ml\option-pricing-runs\20260812T172704.806740Z\pricing-surfaces.parquet — interval_80_coverage: 0 non-null of 236`.
- **Why it is null:** Earliest failure is upstream: the selected legacy predictions had no uncertainty bounds, so coverage could not be evaluated. Later, Loop B also rejected every surface row. This is not merely a join-history problem.
- **Model effect today:** Selected by every horizon, zero-imputed with constant missingness one.
- **Potential fixes:** Immediate: exclude it until interval-bearing predictions have matured. Durable: persist prediction intervals at creation and immutable observed outcomes later. Backfill: only from predictions that genuinely had intervals when issued; never reconstruct an interval after observing the outcome.
- **How to verify the fix:** Require non-null prediction bounds first, then non-null `interval_80_covered` after maturity, then a surface rate and causally joined sample. Observed coverage should be assessed against the stated 80% level.
- **Confidence:** `Proven`.

### 10. `opx__interval_95_coverage`

- **Meaning:** The fraction of completed option evaluations whose observed midpoint fell inside the constrained 95% prediction interval.
- **Why the model might care:** It could identify overconfident or unusually uncertain option-pricing regimes.
- **Original data source:** Schwab observed bid/ask and pricing-system 95% interval bounds, persisted in predictions, evaluations, and surfaces. `ml/option_pricing/causal.py:L929-L936 — covered`; `ml/option_pricing/prediction.py:L162-L180 — predict_option_values`.
- **Fetching path:** Options runtime fetches Schwab chains; pricing evaluates locally from committed evidence. `datafetching/options_runtime.py:L339-L386 — run_options_cycle`; `options/snapshot.py:L171-L187 — _persist_schwab_option_snapshot`.
- **Calculation:** Completed evaluated observations inside the constrained 95% lower/upper bounds divided by completed observations having valid bounds. Range `[0,1]`. `ml/option_pricing/reporting.py:L103-L110 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L139-L150 — build_pricing_surfaces`.
- **Path into Loop B:** Interval bounds → evaluation flag → bucket rate → verified weighted surface → as-of join → sample.
- **Audited evidence:** Sample 0/92,052; selected evaluation flags 0/5,309; selected surface 0/110; newer v2 surface 0/236. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-evaluations.parquet — interval_95_covered`; corresponding surface — `interval_95_coverage`; newer v2 surface — same column.
- **Why it is null:** It first becomes null in legacy predictions because no uncertainty was produced. The all-failing historical join is a second downstream loss.
- **Model effect today:** Selected everywhere, numeric zero after imputation, constant missingness indicator.
- **Potential fixes:** Immediate exclusion; durable prospective interval/outcome accumulation; backfill only where original interval bounds are receipt-proven.
- **How to verify the fix:** Follow one immutable prediction from 95% bounds through a later observed midpoint, coverage flag, surface aggregation, and causally eligible sample.
- **Confidence:** `Proven`.

### 11. `opx__median_relative_bid_ask_spread`

- **Meaning:** The median option bid/ask spread divided by the underlying stock price. It is not divided by the option midpoint.
- **Why the model might care:** It could represent option-market liquidity and the cost of acting on apparent pricing signals.
- **Original data source:** Schwab option bid, ask, and underlying price, stored in normalized option snapshots and later evaluation/surface artifacts. `options/snapshot.py:L248-L264 — normalize_schwab_option_chain`; `options/snapshot.py:L305-L340 — normalize_schwab_option_chain`.
- **Fetching path:** Schwab `/chains` → options runtime → immutable snapshot → pricing evaluation. `datafetching/schwab_fetch.py:L96-L114 — get_option_chain_snapshot`; `datafetching/options_runtime.py:L339-L386 — run_options_cycle`.
- **Calculation:** `(ask − bid) / underlying price`, median per bucket, then contract-count-weighted across buckets. Dimensionless and nonnegative; requires a valid bid, ask, and nonzero underlying. `ml/option_pricing/reporting.py:L103-L106 — build_pricing_surfaces`; `ml/option_pricing/reporting.py:L149-L153 — build_pricing_surfaces`.
- **Path into Loop B:** Evaluation spread → surface median → verified publication → freshness/quality join → sample.
- **Audited evidence:** Sample 0/92,052; selected surface 56/110 non-null. `C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z\pricing-surfaces.parquet — median_relative_bid_ask_spread: 56 non-null of 110`.
- **Why it is null:** The spread existed upstream and first disappeared at the historical join because the one selected generation was not both available and fresh for any published sample.
- **Model effect today:** Selected everywhere, imputed to zero and given a constant missingness flag.
- **Potential fixes:** Immediate authority repair; durable append-only surface history; historical backfill of receipt-proven bid/ask evaluations.
- **How to verify the fix:** Recalculate `(ask-bid)/underlying` for a contract, check aggregation, and verify a non-null sample only after publication and before expiry.
- **Confidence:** `Proven`.

### 12. `macro__fed_funds_level`

- **Meaning:** The latest effective federal-funds rate level, expressed in percentage points; for example, `5.25` means 5.25%.
- **Why the model might care:** Interest-rate levels can affect discount rates, financing conditions, and market risk appetite.
- **Original data source:** FRED graph CSV, series `FEDFUNDS`, sourced by FRED from the Federal Reserve. Raw fields are observation date and value; current normalized output is `pools\macro\FEDERALFUNDS\FEDFUNDS\fred\normalized\FEDERALFUNDS_FEDFUNDS.parquet`. `datafetching/fred_fetch.py:L15 — FRED_CSV_BASE_URL`; `datafetching/fred_fetch.py:L92-L100 — FRED_SERIES`; `ml/rolling_materialization.py:L1041-L1058 — _fred_source_paths`.
- **Fetching path:** `fetch_series` calls the public CSV endpoint without an API key, parses numeric observations, stamps every row with one local `fetched_at`, and `save_macro_rows` writes normalized Parquet. `datafetching/fred_fetch.py:L193-L215 — fetch_series`; `datafetching/fred_fetch.py:L218-L258 — _rows_from_csv`; `datafetching/fred_fetch.py:L161-L188 — fetch`.
- **Calculation:** Select the latest `FEDFUNDS` observation unchanged. The whole four-metric context receives `available_at = max(fetched_at)` across all selected FRED rows. No economic lookback for this metric. `ml/rolling_materialization.py:L1085-L1125 — _derive_current_fred_context`.
- **Path into Loop B:** Four current normalized FRED files → one latest context row → shared backward-as-of join → sample. The registry says 45-day freshness, but the active rolling materializer actually applies 120 days to all four macro metrics. `ml/feature_registry.py:L778-L807 — _macro; MACRO_FEATURES`; `ml/rolling_materialization.py:L725-L742 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052. The run manifest lists all four current FRED files as inputs; the FEDFUNDS input had SHA-256 `fe2a...` and modification time Aug 12 02:16Z. All daily/weekly sample decisions end Aug 11 20:05Z. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\manifest.json — $.input_files[path=pools\macro\FEDERALFUNDS\...\FEDERALFUNDS_FEDFUNDS.parquet]`; sample — `horizon; decision_timestamp`.
- **Why it is null:** The provider was called and a source file existed. The earliest strongly supported loss is the as-of join: Loop B constructed only one current receipt after every selected daily/weekly decision, so every historical row had no prior publication. The exact input file has since been overwritten and no longer matches the manifest checksum, so its original row-level `fetched_at` cannot now be proven.
- **Model effect today:** Selected for `1d` and all weekly routes; excluded from `1h`/`4h`. Where selected, it was zero-imputed with a missingness indicator.
- **Potential fixes:** Immediate: remove it from active models until vintage coverage exists. Durable: use immutable FRED/ALFRED vintages and feature-specific availability clocks through `load_macro_features`, rather than the one-row current-context function. Backfill: `FEDFUNDS` real-time vintages across the training span. `datafetching/fred_vintages.py:L208-L240 — persist_fred_vintages`; `ml/datasets/families.py:L777-L830 — load_macro_features`.
- **How to verify the fix:** A new sample should show the rate only on or after its documented release/receipt, continue through the allowed freshness period, and remain null before release. Compare against `pools\macro-vintages\FEDFUNDS\fred\*.parquet`.
- **Confidence:** `Strongly supported` — code, input inventory, and decision clocks agree; confidence would become `Proven` with the original hash-matching normalized file or a persisted macro join-status audit.

### 13. `macro__cpi_yoy`

- **Meaning:** The latest CPI level divided by the most recent observation dated at least 12 months earlier, minus one. A value of `0.03` means roughly 3% year-over-year inflation.
- **Why the model might care:** Inflation changes can influence monetary-policy expectations, valuation, and sector leadership.
- **Original data source:** FRED `CPIAUCSL`, from the Bureau of Labor Statistics; date and seasonally adjusted CPI index value. Expected current path: `pools\macro\CPI\CPIAUCSL\fred\normalized\CPI_CPIAUCSL.parquet`. `datafetching/fred_fetch.py:L74-L81 — FRED_SERIES`; `ml/rolling_materialization.py:L1041-L1058 — _fred_source_paths`.
- **Fetching path:** Public FRED graph CSV → parsed rows with common `fetched_at` → normalized Parquet. `datafetching/fred_fetch.py:L193-L215 — fetch_series`; `datafetching/fred_fetch.py:L218-L258 — _rows_from_csv`; `datafetching/parquet_store.py:L199-L219 — save_macro_rows`.
- **Calculation:** `latest CPI / latest CPI dated ≤ latest date − 12 months − 1`. Dimensionless fraction; positive means CPI rose. Availability is the maximum receipt time across every observation used for all four metrics. `ml/rolling_materialization.py:L1095-L1128 — _derive_current_fred_context`.
- **Path into Loop B:** Current CPI plus the other three current series → one shared context row → 120-day as-of join → sample. Registry intent is 45 days for CPI, but production join is 120 days. `ml/feature_registry.py:L799-L807 — MACRO_FEATURES`; `ml/rolling_materialization.py:L725-L742 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052. The CPI input manifest entry had SHA `987c...` and Aug 12 17:05Z modification time, after the last daily/weekly decision. The current file has since changed checksum. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\manifest.json — $.input_files[path=pools\macro\CPI\CPIAUCSL\fred\normalized\CPI_CPIAUCSL.parquet]`.
- **Why it is null:** The fetch did not fail; Loop B selected only the latest revised history and stamped the whole derived context with its current receipt. Historical decisions therefore had no prior row. Historical publication generations were never materialized.
- **Model effect today:** Selected in `1d` and weekly models, excluded intraday; selected routes get zero plus a missingness indicator.
- **Potential fixes:** Immediate exclusion. Durable: import ALFRED real-time intervals for `CPIAUCSL`, persist immutable vintages, and use the feature-level vintage loader. Backfill: all original CPI releases and revisions required by the sample span. `datafetching/fred_vintage_import.py:L29-L39 — supported series`; `datafetching/fred_vintage_import.py:L394-L459 — _collect_import`.
- **How to verify the fix:** Reproduce the ratio from two causally available vintage rows, then confirm non-null samples only after the later release. The value must not change retroactively when FRED revises history.
- **Confidence:** `Strongly supported` — original hash-matching row clocks are the missing evidence needed for `Proven`.

### 14. `macro__unemployment_change`

- **Meaning:** The latest unemployment rate minus the latest observation dated at least one month earlier. Unit: percentage points; positive means unemployment increased.
- **Why the model might care:** A rising or falling unemployment rate can affect growth expectations and policy expectations.
- **Original data source:** FRED `UNRATE`, from the Bureau of Labor Statistics; observation date and seasonally adjusted percentage. Expected path: `pools\macro\UNEMPLOYMENTRATE\UNRATE\fred\normalized\UNEMPLOYMENTRATE_UNRATE.parquet`. `datafetching/fred_fetch.py:L83-L90 — FRED_SERIES`; `ml/rolling_materialization.py:L1041-L1058 — _fred_source_paths`.
- **Fetching path:** FRED public CSV → numeric rows with local receipt time → normalized Parquet. `datafetching/fred_fetch.py:L193-L215 — fetch_series`; `datafetching/fred_fetch.py:L218-L258 — _rows_from_csv`.
- **Calculation:** `latest UNRATE − latest UNRATE dated ≤ latest date − 1 month`. Availability is shared across the four-series context. `ml/rolling_materialization.py:L1112-L1132 — _derive_current_fred_context`.
- **Path into Loop B:** Four current source files → one derived row → shared as-of join with production’s 120-day limit → sample. Registry declares 45 days. `ml/feature_registry.py:L799-L807 — MACRO_FEATURES`; `ml/rolling_materialization.py:L725-L742 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052. Manifest input SHA `4482...`, modification Aug 12 02:16Z; current file no longer matches. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\manifest.json — corresponding UNRATE $.input_files entry`.
- **Why it is null:** Current-receipt materialization placed the only context row after all selected decisions. The historical observation dates do not make those values historically available; local receipt time governs.
- **Model effect today:** Selected daily/weekly only; imputed zero plus missingness one.
- **Potential fixes:** Immediate exclusion. Durable ALFRED `UNRATE` vintages with per-release clocks. Backfill all real-time intervals over the training period.
- **How to verify the fix:** For a known release, calculate the percentage-point change from the correct two vintages and confirm the first non-null sample occurs after that release.
- **Confidence:** `Strongly supported`.

### 15. `macro__gdp_yoy`

- **Meaning:** The latest GDP level divided by the latest observation dated at least 12 months earlier, minus one. The result is a fraction, not a percentage-point value.
- **Why the model might care:** GDP growth can affect earnings expectations and broad risk appetite.
- **Original data source:** FRED `GDP`, sourced from the Bureau of Economic Analysis, in billions of dollars at a seasonally adjusted annual rate. Expected path: `pools\macro\GDP\GDP\fred\normalized\GDP_GDP.parquet`. `datafetching/fred_fetch.py:L64-L72 — FRED_SERIES`; `ml/rolling_materialization.py:L1041-L1058 — _fred_source_paths`.
- **Fetching path:** FRED graph CSV → parsed observations → current normalized Parquet. `datafetching/fred_fetch.py:L193-L215 — fetch_series`; `datafetching/parquet_store.py:L199-L219 — save_macro_rows`.
- **Calculation:** `latest GDP / latest GDP dated ≤ latest date − 12 months − 1`; positive means GDP increased. `ml/rolling_materialization.py:L1118-L1135 — _derive_current_fred_context`.
- **Path into Loop B:** Current four-series context → shared as-of join → sample. GDP’s registry limit is 120 days, which matches the production family-wide limit. `ml/feature_registry.py:L799-L807 — MACRO_FEATURES`; `ml/rolling_materialization.py:L725-L742 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052. The GDP input entry had SHA `c0e...` and Aug 12 02:16Z modification time; it has since been overwritten. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\manifest.json — corresponding GDP $.input_files entry`.
- **Why it is null:** Loop B had one current receipt after all model-selected decisions, not a historical release series. The calculation was intended, but no prior publication could be joined.
- **Model effect today:** Selected daily/weekly; excluded intraday; selected routes received zero plus missingness one.
- **Potential fixes:** Immediate exclusion. Durable: ALFRED `GDP` real-time vintages and feature-specific clocks. Backfill every advance/second/third estimate and revision needed to reproduce what was known at each decision.
- **How to verify the fix:** Recompute from two contemporaneously available GDP vintages and ensure later revisions do not alter earlier samples.
- **Confidence:** `Strongly supported`.

### 16. `sec__dilution_event`

- **Meaning:** A one-time indicator equal to one when a supported filing describes a potentially dilutive instrument or offering; otherwise zero. It is exposed only at the first eligible model decision.
- **Why the model might care:** New share supply or convertible instruments can affect expected price pressure and risk.
- **Original data source:** FMP stable endpoint `sec-filings-search/symbol` discovers filings; the system then downloads SEC filing text from `finalLink`/document URL. Raw fields include symbol, accepted date, form, accession, URL, filing text, event type, and evidence quality. `app/services/fmp_corporate_data.py:L12-L17 — configuration`; `app/services/sec_capital_structure.py:L160-L181 — _fetch_fmp_filings`; `app/services/sec_capital_structure.py:L183-L194 — _fetch_text`.
- **Fetching path:** `datafetching/sec_fetch.py:L27-L56 — fetch` obtains the index; `datafetching/sec_fetch.py:L84-L101 — fetch` downloads and extracts text; `datafetching/sec_fetch.py:L119-L134 — fetch` persists text and events. Expected event artifact: `stocks\<symbol>\corporate\sec-events\sec\<year>.parquet`. `datafetching/sec_events.py:L171-L208 — persist_sec_events`.
- **Calculation:** One if extraction quality passes, evidence is supported, and event type contains “dilut” or matches warrants, convertibles, preferred stock, at-the-market offering, or securities offering. Otherwise zero. Availability is the maximum of acceptance, document receipt, extraction completion, and any denominator clock. `datafetching/sec_events.py:L86-L128 — normalize_sec_event_rows`; `datafetching/sec_events.py:L129-L165 — normalize_sec_event_rows`.
- **Path into Loop B:** Immutable SEC event partitions → clock validation → aggregation to first eligible decision → symbol as-of join with `_sec_valid_until` → sample. `ml/rolling_materialization.py:L744-L761 — _attach_loop_a_features`; `ml/datasets/families.py:L989-L1055 — load_sec_event_features`; `ml/datasets/families.py:L1080-L1108 — load_sec_event_features`.
- **Audited evidence:** Sample 0/92,052. The 12 exact SEC inputs in the run manifest still match their hashes and contain 277 rows: 54 ones and 223 zeros; 110 quality-pass and 167 quality-fail. Read-only replay produced, for `1d`, 4,212 `NO_PRIOR_PUBLICATION` and 7 `QUALITY_REJECTED`; analogous weekly routes were entirely one of those two statuses. `C:\DATASTORE\ml\runs\20260812T182857.767187Z\manifest.json — 12 $.input_files entries containing \corporate\sec-events\sec\`; those exact Parquets — `dilution_event; extraction_quality_pass`.
- **Why it is null:** All symbols have a 185-day decision gap from Feb 6 to Aug 11, 2026. Events received Aug 2–10 accumulated into the next available decision. The aggregation takes the maximum event value but requires every grouped event’s quality flag to be true; at least one low-quality event therefore invalidated the whole group. `ml/datasets/families.py:L1350-L1380 — _aggregate_sec_event_snapshots`; `ml/datasets/families.py:L1423-L1441 — _aggregate_sec_groups`; sample — `symbol; horizon; decision_timestamp`.
- **Model effect today:** Selected for `1d` and weekly models; excluded intraday. Selected models received zero plus a missingness indicator.
- **Potential fixes:** Immediate: exclude quality-failed events before aggregating qualified values. Durable: preserve per-accession event identity and map each qualified event to exactly one decision without allowing unrelated failures to poison it. Backfill: restore the missing decision/bar timeline and replay the immutable Aug 2–10 receipts; do not backdate to filing acceptance.
- **How to verify the fix:** Count qualified SEC events, map each to exactly one first eligible decision, and require corresponding non-null `sec__dilution_event` rows. Sparse nulls elsewhere are expected.
- **Confidence:** `Proven` — exact hash-matching events and a replay of the production join reproduce all-null output.

### 17. `sec__offering_size_to_market_cap`

- **Meaning:** Offering size in dollars divided by the company’s market capitalization, producing a dimensionless fraction.
- **Why the model might care:** A large offering relative to company size may matter more than the same dollar amount for a much larger company.
- **Original data source:** Filing text supplies an extracted offering amount; a separate point-in-time market-cap value and its availability timestamp are required. The SEC/FMP discovery and text path is the same as above. `app/services/sec_capital_structure.py:L160-L194 — _fetch_fmp_filings; _fetch_text`.
- **Fetching path:** SEC fetch downloads and scans the filing, then calls `normalize_sec_event_rows` without supplying `market_cap`, `shares_outstanding`, or `denominator_available_at`. `datafetching/sec_fetch.py:L84-L101 — fetch`; `datafetching/sec_events.py:L53-L83 — normalize_sec_event_rows`.
- **Calculation:** `offering_size / market_cap`, only when a causal denominator and denominator clock are supplied. The system refuses to invent a denominator. `datafetching/sec_events.py:L70-L83 — normalize_sec_event_rows`; `datafetching/sec_events.py:L125-L164 — normalize_sec_event_rows`.
- **Path into Loop B:** SEC event partition → first-eligible aggregation → quality-gated symbol join → sample. `ml/datasets/families.py:L1056-L1108 — load_sec_event_features`; `ml/datasets/families.py:L1306-L1397 — _aggregate_sec_event_snapshots`.
- **Audited evidence:** Sample 0/92,052. All 277 exact SEC source rows already have null `offering_size_to_market_cap`, null normalized amount, and null denominator timestamp. Ten rows contain an extracted offering size, but none has market-cap evidence. `C:\DATASTORE\stocks\<audited-symbol>\corporate\sec-events\sec\*.parquet — offering_size_to_market_cap: 0 non-null of 277; denominator_available_at: 0 non-null of 277; offering_size: 10 non-null of 277`, with exact paths and hashes enumerated in the run manifest.
- **Why it is null:** The earliest failure is the calculation stage: the producer never supplied a point-in-time market-cap denominator. The later group-quality failure would also reject the rows, but it is not the root cause for this metric.
- **Model effect today:** Selected daily/weekly, zero-imputed, plus missingness one; excluded intraday.
- **Potential fixes:** Immediate: keep it excluded. Durable: join a receipt-proven point-in-time market cap before SEC normalization and pass both value and availability clock. Backfill: reconstruct market cap from immutable price and shares-outstanding evidence available at each extraction time.
- **How to verify the fix:** For an offering with a known amount, require non-null `market_cap`, `denominator_available_at ≤ available_at`, a reproducible ratio, and exactly one causally eligible sample row.
- **Confidence:** `Proven`.

### 18. `sec__filing_event_impulse`

- **Meaning:** A one-time one/zero flag indicating whether a filing contained any supported, quality-passing capital-structure event, regardless of whether it was dilutive.
- **Why the model might care:** The mere arrival of a material financing or instrument event could affect short-term uncertainty or direction.
- **Original data source:** FMP filing discovery plus SEC filing text; raw inputs are acceptance, accession, document URL, form, extracted event type/state, and evidence quality. `app/services/sec_capital_structure.py:L160-L194 — _fetch_fmp_filings; _fetch_text`; `app/services/sec_capital_structure.py:L200-L224 — _scan_filing_text`.
- **Fetching path:** `datafetching/sec_fetch.py:L27-L56 — fetch`; `datafetching/sec_fetch.py:L84-L101 — fetch`; event persistence at `datafetching/sec_events.py:L171-L208 — persist_sec_events`.
- **Calculation:** One if extraction quality passes and evidence state is neither unrelated nor insufficient; otherwise zero. It is valid only for the first eligible model decision. `datafetching/sec_events.py:L100-L114 — normalize_sec_event_rows`; `datafetching/sec_events.py:L160-L165 — normalize_sec_event_rows`; `ml/feature_registry.py:L626-L649 — _sec`.
- **Path into Loop B:** SEC events → first-decision aggregation → quality mask → backward-as-of join → sample.
- **Audited evidence:** Sample 0/92,052. Exact upstream rows contain 83 ones and 194 zeros. The same replay produced only `NO_PRIOR_PUBLICATION` or `QUALITY_REJECTED` for every selected decision. `C:\DATASTORE\stocks\<audited-symbol>\corporate\sec-events\sec\*.parquet — filing_event_impulse: 277 non-null, including 83 ones`; exact files in run manifest.
- **Why it is null:** The long sample decision gap caused many events to collapse onto one later decision. The group’s quality is computed with `all()`, so any bad extraction made all event values in that group ineligible.
- **Model effect today:** Selected daily/weekly; zero-imputed with missingness one; excluded intraday.
- **Potential fixes:** Immediate: aggregate only qualified events. Durable: preserve event/accession identity and first-decision assignment before cross-event grouping. Backfill the missing decision timeline and replay actual local receipt clocks.
- **How to verify the fix:** Every quality-passing supported event should create one and only one non-null impulse row; unrelated and low-quality events should create none and should not suppress unrelated valid events.
- **Confidence:** `Proven`.

### 19. `cme__nq_return_1h`

- **Meaning:** The natural-log return of Nasdaq-100 futures over one exact common hour. Positive means NQ rose during the hour.
- **Why the model might care:** Recent technology-futures movement could provide broad market context for individual equities.
- **Original data source:** Databento `GLBX.MDP3`, continuous `NQ.v.0`, alongside `ES.v.0`, `RTY.v.0`, `GC.v.0`, and `CL.v.0`; schemas `ohlcv-1m`, `bbo-1m`, and `mbp-10`. Return fields are timestamp, open, and close; clocks include event, receive, and local receipt time. The three exact audited normalized inputs record these provider identifiers.
- **Fetching path:** `DatabentoCmeContextProvider` reads API/dataset/schema/symbol configuration and calls `client.timeseries.get_range`. The older Loop A path persisted canonical normalized snapshots; the independent runtime now persists daily/hourly immutable event partitions. `app/services/databento_cme_context.py:L77-L123 — DatabentoCmeContextProvider`; `app/services/databento_cme_context.py:L177-L198 — fetch_cme_context_exact`; `app/services/databento_cme_context.py:L246-L249 — _fetch_frame`; `datafetching/databento_fetch.py:L1037-L1080 — _fetch_cme_unlocked`; `datafetching/cme_history.py:L248-L275 — persist_cme_event_history`.
- **Calculation:** Requires 60 exact one-minute timestamps shared by all five roots. `ln(last close / first open)` for NQ. The intended full producer also requires usable BBO and MBP evidence for all five roots. `datafetching/cme_cross_asset_context.py:L68-L87 — calculate_cme_cross_asset_context`; `datafetching/cme_cross_asset_context.py:L426-L457 — _complete_common_ohlcv_windows`; `datafetching/cme_cross_asset_context.py:L460-L469 — _window_return`.
- **Path into Loop B:** Preferred path is immutable events → `pools\cme\features\cross-asset-context\databento\1h.parquet` → shared as-of join. Because that derived file was absent, Loop B fell back to three old canonical normalized snapshots and calculated one latest row. `datafetching/cme_cross_asset_context.py:L221-L285 — persistence functions`; `ml/rolling_materialization.py:L763-L806 — _attach_loop_a_features`; `ml/rolling_materialization.py:L1141-L1174 — _cme_normalized_source_paths`.
- **Audited evidence:** Sample 0/92,052. Exact OHLCV input: 28,486 rows through Aug 5 11:59Z; BBO: 15,399 through 11:58:59Z; MBP: 326,079 through 04:06Z. The fallback calculation produced NQ return `0.001806` for 11:00–12:00, available at 12:08:58Z. Replay: `1h` 31,235 `NO_PRIOR` + 28 `STALE`; `4h` 31,214 + 42; `1d` 4,212 + 7. `C:\DATASTORE\pools\cme\CME_CONTEXT\cme_context_ohlcv-1m\databento\normalized\CME_CONTEXT_cme_context_ohlcv-1m.parquet`; corresponding BBO and MBP paths; all three hashes match the run manifest.
- **Why it is null:** The return calculated successfully in the fallback. It first disappears at the as-of join: decisions before 12:08:58 had no prior publication, and later decisions exceeded 15 minutes (`1h`/`4h`) or one day (`1d`). `ml/datasets/families.py:L123-L128 — CME_FRESHNESS`; `ml/datasets/point_in_time.py:L600-L625 — _finalize_join`.
- **Model effect today:** Selected for `1h`, `4h`, and `1d`; excluded weekly. Selected models received zero plus missingness one.
- **Potential fixes:** Immediate: make the independent CME owner publish a fresh derived hourly artifact. Durable: derive incrementally as each complete hour becomes available rather than waiting for every backlog schema to finish. Backfill: two years of exact five-root immutable windows.
- **How to verify the fix:** `pools\cme\features\cross-asset-context\databento\1h.parquet` should exist and advance hourly; the sample return should equal `ln(close/open)` and have `cme__join_status=JOINED`.
- **Confidence:** `Proven` — exact inputs, fallback output, and production-join replay reproduce the null.

### 20. `cme__es_return_1h`

- **Meaning:** The natural-log return of S&P 500 futures over the same exact common hour. Positive means ES rose.
- **Why the model might care:** It could provide broad equity-market direction separate from stock-specific movement.
- **Original data source:** Databento `GLBX.MDP3`, continuous `ES.v.0`, using `ohlcv-1m` open/close and the common five-root clock contract. Exact provider fields and identifiers are present in the audited CME Parquets.
- **Fetching path:** Databento provider → `timeseries.get_range` → canonical normalized files and newer immutable event partitions. `app/services/databento_cme_context.py:L139-L149 — specs`; `app/services/databento_cme_context.py:L177-L198 — fetch_cme_context_exact`; `datafetching/cme_runtime.py:L474-L513 — _collect_schema_history`.
- **Calculation:** `ln(ES last close / ES first open)` across 60 exact minutes shared by all roots. `datafetching/cme_cross_asset_context.py:L426-L469 — _complete_common_ohlcv_windows; _window_return`.
- **Path into Loop B:** Intended hourly derived history; audited fallback from old normalized snapshots; shared freshness join; sample projection. `ml/rolling_materialization.py:L763-L806 — _attach_loop_a_features`; `ml/rolling_materialization.py:L1181-L1245 — _derive_current_cme_context`.
- **Audited evidence:** Sample 0/92,052. Fallback ES value was `0.000064`, with the same 12:08:58Z availability and the same replay statuses as NQ.
- **Why it is null:** A finite return existed but every sample decision was before availability or beyond the applicable freshness limit.
- **Model effect today:** Selected in `1h`, `4h`, `1d`; zero-imputed with a missingness flag; excluded weekly.
- **Potential fixes:** Immediate current hourly publication; durable incremental derivation; immutable two-year backfill.
- **How to verify the fix:** Recalculate the ES log return from the derived hour and confirm a causally joined non-null sample.
- **Confidence:** `Proven`.

### 21. `cme__relative_spread`

- **Meaning:** The average relative bid/ask spread across NQ, ES, RTY, gold, and crude futures. Larger values mean wider, less liquid markets.
- **Why the model might care:** Broad futures-market illiquidity could accompany volatility or weak price discovery.
- **Original data source:** Databento `bbo-1m` for continuous `NQ.v.0`, `ES.v.0`, `RTY.v.0`, `GC.v.0`, and `CL.v.0`; raw fields include `bid_px_00`, `ask_px_00`, timestamps, receive clocks, and receipt clocks. Audited path: `C:\DATASTORE\pools\cme\CME_CONTEXT\cme_context_bbo-1m\databento\normalized\CME_CONTEXT_cme_context_bbo-1m.parquet`.
- **Fetching path:** Databento `timeseries.get_range` through `DatabentoCmeContextProvider`; canonical persistence in `datafetching/databento_fetch.py:L1037-L1080 — _fetch_cme_unlocked`; immutable history in `datafetching/cme_history.py:L248-L275 — persist_cme_event_history`.
- **Calculation:** For each root, select its latest BBO inside the common hour and calculate `(ask−bid)/((ask+bid)/2)`; average the five results. Dimensionless and nonnegative; all roots must be present and uncrossed. `datafetching/cme_cross_asset_context.py:L480-L507 — _relative_spread`; `datafetching/cme_cross_asset_context.py:L508-L538 — _relative_spread`.
- **Path into Loop B:** BBO event history → hourly context, or audited canonical fallback → shared as-of join → sample. `ml/rolling_materialization.py:L1246-L1270 — _derive_current_cme_context`; `ml/rolling_materialization.py:L799-L806 — _attach_loop_a_features`.
- **Audited evidence:** Sample 0/92,052. Fallback calculation produced `0.000093`; exact BBO input had 15,399 rows through Aug 5 11:58:59Z. Replay for selected horizons: `1h` 31,235 no-prior + 28 stale; `4h` 31,214 + 42.
- **Why it is null:** The spread calculated successfully. It first disappeared at the join because the lone fallback context was unavailable before 12:08:58 and more than 15 minutes old afterward.
- **Model effect today:** Selected for `1h` and `4h`; excluded from `1d` and weekly models. Selected routes received zero plus missingness one.
- **Potential fixes:** Immediate fresh derived context. Durable per-hour, five-root BBO rollups independent of MBP backlog completion. Backfill exact BBO windows from immutable partitions.
- **How to verify the fix:** Compare a derived row against the average of five latest valid BBO spreads, then require non-null intraday samples within 15 minutes.
- **Confidence:** `Proven`.

### 22. `cme__book_imbalance`

- **Meaning:** The average order-book size imbalance across the five futures roots: `(bid size − ask size)/(bid size + ask size)`. Positive is bid-heavy, negative is ask-heavy; range `[-1,1]`.
- **Why the model might care:** Broad order-book pressure could provide a short-lived measure of market demand or supply.
- **Original data source:** Databento `mbp-10`, continuous NQ/ES/RTY/GC/CL. Raw fields are ten levels of bid and ask size, or side/depth/size rows, plus event/receive/receipt clocks. Audited path: `C:\DATASTORE\pools\cme\CME_CONTEXT\cme_context_mbp-10\databento\normalized\CME_CONTEXT_cme_context_mbp-10.parquet`.
- **Fetching path:** Databento exact-range requests are split when the 250,000-row cap is reached, then persisted as hourly event partitions. `datafetching/cme_runtime.py:L474-L513 — _collect_schema_history`; `datafetching/cme_runtime.py:L558-L575 — _split_saturated_request`; `datafetching/cme_history.py:L248-L275 — persist_cme_event_history`.
- **Calculation:** Within the last 15 minutes of the common hour, take the newest book for each root, sum levels 0–9 on each side, calculate the imbalance, then average five roots. All roots and both sides are prerequisites. `datafetching/cme_cross_asset_context.py:L541-L576 — _book_imbalance`; `datafetching/cme_cross_asset_context.py:L577-L623 — _book_imbalance`.
- **Path into Loop B:** MBP history should feed the derived hourly context. Because no derived context existed, Loop B’s fallback tried the old canonical MBP file and returned `None`, then passed that row to the shared as-of join. `ml/rolling_materialization.py:L1273-L1310 — _derive_current_cme_context`; `ml/rolling_materialization.py:L1313-L1335 — _derive_current_cme_context`.
- **Audited evidence:** Sample 0/92,052. The exact MBP file had 326,079 rows but ended at Aug 5 04:06Z, almost eight hours before the 11:00–12:00 fallback window; therefore the fallback row’s `book_imbalance` was null. Runtime logs show successful high-volume MBP requests and repeated cap splitting, but the log ends while still collecting and contains no hourly-derivation stage. `C:\DATASTORE\runtime-logs\loop-native-restart-20260811T160228.2069648Z\cme.stdout.log:L280-L324`; corresponding `cme.stderr.log — 0 bytes`.
- **Why it is null:** The earliest failure is the fallback calculation: there was no MBP row in the required final 15 minutes, so it returned `None`. Every decision would also have failed the later availability/freshness join. The provider was called and authentication worked; unfinished/backlogged materialization, not an empty provider response, caused the loss.
- **Model effect today:** Selected for `1h` and `4h`; excluded otherwise. Selected models received zero plus missingness one.
- **Potential fixes:** Immediate: complete or bypass the MBP backlog and publish the next fully valid hourly context. Durable: create bounded incremental MBP rollups and derive completed hours without waiting for the entire schema backlog. Backfill verified MBP partitions, preserving cap-saturation and cursor receipts.
- **How to verify the fix:** Require five current roots, both book sides, ten-level sums, a finite value in `[-1,1]`, a new derived hourly row, and non-null intraday sample values inside 15 minutes.
- **Confidence:** `Proven` — exact timestamps establish the calculation failure; whether the runtime was externally stopped is unresolved.

## Shared root causes

| Family                      | Metrics       | Proven/shared cause                                                                                                                                     |
| --------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pricing publication/history | 1, 2, 4–8, 11 | Values existed in the selected surface, but Loop B read one old generation and every historical join was no-prior, stale, or quality-rejected.          |
| Pricing uncertainty         | 3, 9, 10      | The selected legacy predictions were baseline-only and already lacked standard deviation and interval bounds; the historical join was a second failure. |
| Macro                       | 12–15         | Loop B derived one current four-series FRED receipt rather than historical point-in-time vintages. Exact original rows were later overwritten.          |
| SEC event grouping          | 16, 18        | A 185-day decision gap collected multiple events into one decision, and `quality=all()` let any weak event reject every grouped value.                  |
| SEC denominator             | 17            | The normalizer was never supplied a causal market-cap value or clock.                                                                                   |
| CME stale fallback          | 19–21         | No derived hourly artifact existed; the fallback had one old row, which every decision saw as unavailable or stale.                                     |
| CME missing MBP             | 22            | MBP ended before the required 15-minute book window, so the fallback calculation itself returned null.                                                  |

The evidence rules out a universal authentication failure, empty provider response, column-name mismatch, timezone mismatch, or silently caught route exception. The run manifest has `route_errors={}`, exact SEC/CME sources are nonempty and hash-valid, pricing status was `VERIFIED`, and CME logs show successful provider requests.

## Ranked repair plan

1. **Coverage-gate the enriched families immediately.** Keep the 22 columns out of active horizon models until each family meets an explicit causal-coverage gate. This is low effort and prevents constant imputed columns from changing model geometry.

2. **Make pricing authority monotonic and append-only.** Prevent older generations from becoming current, recover the existing v2 generation, and materialize all verified pricing surfaces into historical first-availability order.

3. **Decouple CME derivation from backlog completion.** Publish completed hourly contexts incrementally from immutable OHLCV/BBO/MBP partitions; do not wait for an entire high-volume MBP backlog.

4. **Replace current FRED history with genuine vintages.** Import ALFRED real-time intervals for all four series and wire the existing feature-level vintage loader into Loop B.

5. **Repair SEC event eligibility and denominators.** Aggregate only quality-passing events and add a causal market-cap join before calculating offering ratios.

6. **Backfill, rerun Loop B, and retrain.** Do not activate any family merely because it becomes non-null; require out-of-time incremental value per horizon.

## Backfill requirements

- **Pricing:** Reconstruct a compact history from every immutable `ml\option-pricing-runs\<generation>\publication.json`, manifest, prediction/evaluation artifact, and surface. Preserve first availability, target time, checksum, schema/policy version, and supersession. Do not infer unavailable v1 uncertainty.

- **Macro:** Import ALFRED vintages for `FEDFUNDS`, `CPIAUCSL`, `UNRATE`, and `GDP`. Current FRED graph CSV files are revised histories and cannot reconstruct what was known historically. `datafetching/fred_vintages.py:L69-L104 — normalize_fred_vintage_rows`.

- **SEC:** Replay immutable accession/document/extraction receipts. Reconstruct missing decision/bar dates. A filing accepted earlier must not be backdated before its actual local document receipt and extraction.

- **CME:** Use Databento immutable event partitions and cursors to reconstruct exact common one-hour windows. Re-fetch verified gaps and reject any cap-saturated request until its split children are complete.

## Activation criteria

These are recommended gates, not claims about existing policy:

- **Pricing:** At least 80% causally joined sample coverage per intended horizon over the required training span, with separate minimum matured counts for residual, edge, and interval metrics. Interval coverage should be near its nominal 80%/95% range before those two metrics are considered.

- **Macro:** At least 95% coverage of decisions that fall within the feature’s post-release freshness window, with immutable vintage identity and zero retroactive revisions to frozen samples.

- **SEC:** Do not use raw sample non-null percentage as the gate because these are intentionally sparse impulses. Require at least 99% filing-index surveillance coverage, complete accession/receipt/extraction identity, exactly one eligible decision per qualified event, and high denominator coverage among supported offerings.

- **CME:** At least 95% complete, nonstale eligible windows per selected horizon; all five roots present; no accepted cap-saturated source; and approximately two years of reproducible history, matching the registry intent.

- **Predictive-value gate for every family and horizon:** Compare the causal baseline against the baseline-plus-family model in rolling-origin folds. Require consistent improvement in out-of-time Brier score/log loss or precision at the action threshold, no material calibration degradation, and confirmation on a later untouched period. Test missingness indicators as candidate features rather than admitting them automatically.

## Read-only verification commands

Run these from `C:\dev\ducketz`.

### Reproduce sample null counts

```powershell
@'
import pyarrow.parquet as pq

p = r"C:\DATASTORE\ml\runs\20260812T182857.767187Z\samples.parquet"
names = """
opx__causal_coverage
opx__median_normalized_residual
opx__median_predictive_standard_deviation
opx__median_model_edge_in_half_spreads
opx__positive_edge_fraction
opx__negative_edge_fraction
opx__raw_arbitrage_violation_rate
opx__constrained_arbitrage_violation_rate
opx__interval_80_coverage
opx__interval_95_coverage
opx__median_relative_bid_ask_spread
macro__fed_funds_level
macro__cpi_yoy
macro__unemployment_change
macro__gdp_yoy
sec__dilution_event
sec__offering_size_to_market_cap
sec__filing_event_impulse
cme__nq_return_1h
cme__es_return_1h
cme__relative_spread
cme__book_imbalance
""".split()

t = pq.read_table(p, columns=names)
print("rows:", len(t))
for name in names:
    col = t[name]
    print(name, col.type, "non-null", len(col)-col.null_count,
          "null", col.null_count,
          "null_pct", 100*col.null_count/len(col))
'@ | .\.venv\Scripts\python.exe -
```

### Reproduce model selections

```powershell
@'
import json
from pathlib import Path

root = Path(r"C:\DATASTORE\ml\models")
run = "20260812T182857.767187Z"
prefixes = ("opx__", "macro__", "sec__", "cme__")

for h in ("1h","4h","1d","1w","1w-d1","1w-d2","1w-d3","1w-d4","1w-d5"):
    p = root / h / f"logistic-{h}" / run / "manifest.json"
    m = json.loads(p.read_text())
    selected = [x for x in m["feature_columns"] if x.startswith(prefixes)]
    print(h, m["feature_set_name"], len(m["feature_columns"]))
    print(*selected, sep="\n  ")
'@ | .\.venv\Scripts\python.exe -
```

### Inspect pricing evidence and join statuses

```powershell
@'
import json
from collections import Counter
from pathlib import Path
import pandas as pd

run = Path(r"C:\DATASTORE\ml\runs\20260812T182857.767187Z")
m = json.loads((run / "manifest.json").read_text())
routes = m["configuration"]["pricing_evidence"]["routes"]

counts = Counter()
for route in routes.values():
    counts.update(route["join_status_counts"])

print("routes:", len(routes))
print("all missing:", sum(x["all_pricing_values_missing_rows"] for x in routes.values()))
print(counts)

p = Path(r"C:\DATASTORE\ml\option-pricing-runs\20260811T200100.150651Z")
for name in ("pricing-surfaces.parquet",
             "pricing-predictions.parquet",
             "pricing-evaluations.parquet"):
    f = pd.read_parquet(p / name)
    print(name, len(f), f.count().to_dict())
'@ | .\.venv\Scripts\python.exe -
```

### Compare current files with audited manifest hashes

```powershell
@'
import hashlib, json
from pathlib import Path

root = Path(r"C:\DATASTORE")
run = root / "ml/runs/20260812T182857.767187Z"
m = json.loads((run / "manifest.json").read_text())

for item in m["input_files"]:
    path = item["path"]
    if any(x in path for x in ("\\macro\\", "\\sec-events\\", "\\cme\\CME_CONTEXT\\")):
        p = root / path
        current = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "MISSING"
        print(path, "match=", current == item["checksum_sha256"])
'@ | .\.venv\Scripts\python.exe -
```

No code, configuration, pointer, receipt, or datastore artifact was modified.
