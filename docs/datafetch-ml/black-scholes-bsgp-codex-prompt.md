# Codex implementation prompt: Black-Scholes-integrated Gaussian-process pricing loop

Work in `C:\dev\ducketz`. Build the production-grade, shadow-only Black-Scholes-integrated Gaussian-process option-pricing subsystem described below. Do not stop after producing an architecture note or partial scaffold: inspect the current repository, implement the coherent vertical slice, add tests and documentation, and run the relevant verification. Preserve unrelated user changes.

The local source material is:

- `I:\Shared drives\SECONDSTATE\DUMPER\Black-Scholes-IGPM-Blueprint.md`
- `I:\Shared drives\SECONDSTATE\DUMPER\Black-Scholes-IGPM.md`

Read both files before editing. The thesis calls the method **BSGP** (Black-Scholes-integrated Gaussian process), even though the local filenames say IGPM. Use `bsgp` in code and policy names; mention the filename alias only in documentation.

## Outcome

Add a sixth independent Duckets runtime, `ml.option_pricing_runtime`, that:

1. reads completed, checksum-verified option and market-data evidence;
2. computes a causal European Black-Scholes baseline for American equity options;
3. learns the normalized residual `observed option price - Black-Scholes price` with a scalable Gaussian-process approximation;
4. publishes contract-level fair values, calibrated uncertainty, arbitrage diagnostics, compact surface features, evaluations, and monitoring through an immutable run/manifest/receipt/pointer contract;
5. proves prospective live evidence by committing predictions before the matching option quotes exist;
6. can use normalized historical OPRA evidence for offline training and assessment without ever counting backfill as live evidence;
7. leaves Loop A, Options, directional Loop B, Strategy rankings, the UI, and Schwab order behavior unchanged when Pricing is absent, invalid, disabled, or fails;
8. remains shadow-only: it must never authorize a trade or set `automated_action_allowed=True`.

The build must include a guarded OPRA cost-estimation/import path, but **must not download paid Databento data during implementation or tests**. It must not restart supervisors, alter a live datastore, submit a Schwab order, or expose credentials.

## Repository contracts to preserve

Start by reading these current implementations and their tests rather than inventing a parallel style:

- `README.md`
- `datafetching/options_runtime.py`
- `options/snapshot.py`
- `options/features.py`
- `options/publication.py`
- `datafetching/decision_time.py`
- `datafetching/runtime_lock.py`
- `datafetching/ids.py`
- `ml/artifacts.py`
- `ml/parquet_contracts.py`
- `ml/model_runtime.py`
- `ml/current_publication.py`
- `ml/strategy_runtime.py`
- `ml/strategy_publication.py`
- `ml/strategy_selection/runtime.py`
- `ml/strategy_selection/model.py`
- `ml/strategy_selection/chain.py`
- `docs/datafetch-ml/independent-runtime-orchestration.md`
- `docs/datafetch-ml/options-strategy-selection.md`
- `docs/datafetch-ml/parquet-id-contract.md`
- the corresponding tests under `tests/`

Honor Duckets Law: every new file, symbol, setting, artifact, and dependency must be used by the completed vertical slice. Do not add placeholders, generic frameworks, speculative adapters, or dead feature flags.

The existing ownership boundaries are non-negotiable:

- Options owns Schwab chain acquisition and the observed raw/normalized/surface snapshot receipt.
- Pricing reads verified receipts. It never calls Schwab and never writes an Options-owned artifact.
- Loop A does not run Pricing.
- Pricing does not start or wait on another runtime.
- Directional Loop B does not fit Pricing models.
- Strategy may consume a verified Pricing publication only in explicit shadow mode and must retain its current mechanics-based behavior as fallback.
- Every writer has its own datastore-rooted crash-released operating-system lock.

## Correct interpretation of the research

Implement the thesis idea, not its empirical claims:

```text
f(x) = BS(x) + delta(x)
delta(x) ~ GP(0, k(x, x'))
x = (S, K, r, sigma, T, q)
```

The local thesis used SPY options from only May-June 2019, illustrated week-to-week tests, reported ordinary contract-row MSE, used contemporaneous implied volatility, and used an exact GP/MCMC formulation with cubic scaling. That is research inspiration, not sufficient Duckets evidence. Do not reproduce its data leakage, contract-row pseudo-replication, test reuse, or scalability problem, and do not copy its performance claim into application documentation.

In particular, contemporaneous implied volatility is derived from the option price being predicted. It is therefore forbidden as a feature for that same target quote. The model must use a strictly earlier, causally available implied-volatility surface. A row that cannot prove that lag must be unavailable, not imputed from the outcome.

## The causal prediction clock

Make this timing contract explicit in schemas, manifests, validation, tests, and documentation.

### Live prediction

For each symbol:

1. Resolve the newest completed canonical Databento `1m` bar whose `bar_end_timestamp` is on a `:00`, `:15`, `:30`, or `:45` boundary and whose evidence was available before the Pricing run time.
2. That bar end is `target_snapshot_for` only if the matching Schwab Options receipt has not yet been published. A target with an already-visible Options receipt is too late for a new prospective prediction.
3. Read the most recent earlier committed Schwab Options receipt with `source_available_at < prediction_created_at` and `source_snapshot_for < target_snapshot_for`.
4. Use the target boundary's completed underlying price, contract definitions already known from the earlier receipt, and only lagged rate/dividend/volatility inputs to create predictions.
5. Atomically publish the prediction before the independent Options runtime fetches the target chain. The documented operating phase should put Pricing before the existing Options `+2 minute` phase. If the target bar is not ready in time, skip it; never backdate a prediction.
6. On a later Pricing cycle, reconcile the prediction only to a verified Schwab receipt with the exact `target_snapshot_for`, `receipt.available_at > prediction_created_at`, and the exact contract.
7. A contract counts as prospective only when its normalized `quote_timestamp` is strictly later than `prediction_created_at`. A stale quote that existed before the prediction may be displayed as unavailable evidence but must not be scored or counted.
8. Repeated runs for one symbol/target/contract canonicalize to the earliest valid pre-quote prediction. Duplicate cycles never increase evidence.

Do not weaken `created before outcome` to `model trained before outcome`, `receipt downloaded later`, or `snapshot directory written later`.

### Historical OPRA row

Historical materialization must emulate the same feature/target relationship:

- use a completed underlying bar and a strictly earlier option surface as features;
- select a later `cbbo-1m` observation for the target quote;
- join the point-in-time instrument definition effective at the snapshot;
- record source and target times explicitly;
- never use a look-forward fill;
- label all such rows `prediction_mode=OFFLINE` (or the repository's equivalent explicit value);
- never allow an OPRA backfill row to contribute to prospective/live counts.

Databento documents `cbbo-1m` `ts_recv` as the interval end and notes that no row is printed when no trade or CBBO update occurs. Permit only a small, configured backward staleness window for a source surface and a small forward observation window for the target. Missing records remain missing.

## Pricing input and target contract

Use separate routes for each `(symbol, call_put)` pair. Preserve the six semantic Black-Scholes inputs in the model manifest, even if the fitted preprocessing derives stable forms such as log moneyness:

```text
underlying_price                  S
strike                            K
risk_free_rate                    r
lagged_implied_volatility         sigma
target_years_to_expiration        T
dividend_yield                    q
```

Rules:

- `S` comes from the completed canonical underlying bar at the target boundary, not from the target option quote.
- `K`, call/put, expiration, multiplier, and standard-contract status come from a definition known before prediction.
- `T` is ACT/365 calendar time from `target_snapshot_for` to the documented option-expiration instant. OPRA definition dates have date precision; encode the U.S. equity-option expiration-time policy and version it.
- Prefer a lagged provider rate and dividend yield when present and valid.
- If historical OPRA lacks those fields, use an explicit causal policy: the latest point-in-time Duckets rate observation and a robust lagged effective dividend yield inferred from prior-surface put-call parity. Record source time and policy. Never silently substitute zero.
- If OPRA has no implied volatility, solve it from the **earlier** source midpoint using the lagged rate/dividend inputs and bounded root finding. Reject impossible/nonconvergent rows.
- Interpolate the earlier IV surface causally across log moneyness and time to expiration. Do not extrapolate outside configured support; mark those rows unavailable.
- Standard pilot contracts are non-mini, non-adjusted 100-share calls and puts, 7-120 calendar days to expiration, with `abs(log(K/S)) <= 0.25`, finite uncrossed BBO, positive midpoint, and explicit spread/staleness diagnostics.
- Bid, ask, mark, last, target midpoint, target provider IV, target Greeks, target volume, target open interest, future underlying values, and future surface aggregates are prohibited model features.

The observed target is the later valid NBBO midpoint:

```text
observed_mid = (observed_bid + observed_ask) / 2
black_scholes_price = BS(S, K, r, lagged_sigma, T, q)
normalized_residual = (observed_mid - black_scholes_price) / S
```

The model predicts `normalized_residual`; convert it back to dollars and add it to the Black-Scholes mean. Persist both the raw prediction and the constrained published fair value. Do not train directly on provider theoretical value.

## Black-Scholes and arbitrage behavior

Implement and unit-test dividend-adjusted European call and put Black-Scholes formulas. Handle expiry, near-zero volatility, invalid values, and numerical tails deterministically; do not let NaN/inf silently enter a fitted matrix.

For American equity options, define pointwise bounds:

```text
call lower = max(intrinsic call, European BS call)
call upper = S
put  lower = max(intrinsic put, European BS put)
put  upper = K
```

At each fixed symbol/snapshot/call-put/expiration surface:

- calls must be non-increasing in strike;
- puts must be non-decreasing in strike;
- both must be convex in strike;
- prices and interval endpoints must remain inside their pointwise bounds.

Publish raw values, violation diagnostics, constrained values, and the magnitude of any correction. Use a deterministic weighted least-squares shape projection with explicit tolerances; do not silently relabel a clipped value as the raw model output. A projection failure makes that surface unavailable to consumers while other routes continue.

If the projection implementation imports SciPy directly, declare SciPy as a direct project dependency in the appropriate ML extra; do not rely on it arriving transitively through scikit-learn.

## Scalable BSGP model

Do not use an unbounded exact `GaussianProcessRegressor` over all contracts. The pilot alone can contain tens of thousands of rows and exact GP fitting is cubic.

Implement a deterministic finite-basis approximation to an RBF Gaussian process using existing scikit-learn primitives:

1. training-only semantic preprocessing/robust scaling;
2. `Nystroem(kernel="rbf", ...)` with a fixed policy random state and a bounded component count;
3. `BayesianRidge` on the basis expansion, fitted with inverse snapshot-size weights;
4. predictive standard deviation from the Bayesian linear posterior;
5. calibration-cluster weighted scale/quantile adjustments for 80% and 95% intervals.

Version and document this honestly as an RBF finite-feature GP approximation. Do not call it exact GP or MCMC. Persist the component count, gamma-selection grid, selected gamma, random state, preprocessing contract, feature order, residual definition, price scale, and uncertainty-calibration method.

Fit these comparable models under the same chronological partitions and feature budget:

- `bsgp`: Black-Scholes mean plus learned residual;
- `black_scholes`: zero residual;
- `constant_residual`: training-cluster-weighted constant residual;
- `standard_gp`: the same GP approximation trained on normalized option price without the Black-Scholes mean.

Assessment and monitoring must compare all four. Strategy's existing Greek/BBO scenario prior is a separate candidate-level comparator and must not be mislabeled as a contract-price model.

## Chronological evidence partitions

The independent evidence unit is a complete `target_snapshot_for` cluster, not a contract row. All contracts, calls/puts, expirations, and symbols sharing a target snapshot remain together for chronological boundary purposes. Contract weights within a snapshot must sum to one for primary fit/evaluation summaries so a larger chain does not masquerade as more evidence.

Per production-eligible route require:

- 252 chronological target-snapshot clusters for training;
- the next 63 for hyperparameter and uncertainty calibration;
- the next 63 untouched clusters for assessment;
- 126 later real clusters designated `CLOSED_UNTOUCHED_UNSCORED`;
- at least six calendar months from the first training cluster through the last closed-lockbox cluster.

Purge source/target overlap at every boundary. A training target and all information needed to label it must be strictly available before the next partition starts. Assessment fits nothing. The closed lockbox contributes only redacted counts and time bounds to manifests: no lockbox target values, predictions, scores, bucket metrics, or model selection.

Insufficient routes publish an explicit not-fit/not-ready report without fabricating a model. They may still publish the Black-Scholes baseline if all causal inputs are valid.

After a model is frozen, require at least 60 completed prospective predictions per symbol across at least 20 distinct exchange sessions. Count the earliest valid prediction once per natural target. Offline rows never increment this count.

## Model artifacts and reuse

Use a readable hierarchy such as:

```text
DATASTORE/ml/option-pricing-models/<SYMBOL>/<call-or-put>/black-scholes-rbf-residual/<trained UTC timestamp>/
    model.joblib
    manifest.json
```

Use `latest.json` only as a path pointer. Before loading `model.joblib`, verify its checksum and a complete compatibility manifest. Refit on any mismatch in:

- route and model policy versions;
- ordered semantic/derived feature columns;
- target/residual/price-scale definition;
- source/target timing policy;
- rate, dividend, IV, interpolation, expiration, contract-selection, weighting, kernel, uncertainty, bounds, and projection policies;
- partition boundaries, cluster counts, row counts, training-through timestamp, and immutable input inventory;
- Python implementation checksum/version and relevant NumPy, pandas, PyArrow, SciPy, scikit-learn, and joblib versions.

Never load a model first and validate afterward. Copy every model artifact used for a publication into that immutable run, as Strategy currently does.

## Immutable Pricing publication

Use a separate authority:

```text
DATASTORE/ml/
|-- option-pricing-runs/<UTC timestamp>/
|   |-- pricing-samples.parquet
|   |-- pricing-predictions.parquet
|   |-- pricing-evaluations.parquet
|   |-- pricing-surfaces.parquet
|   |-- pricing-monitoring.parquet
|   |-- option-pricing-model-reports.json
|   |-- model-artifacts/...
|   |-- manifest.json
|   `-- publication.json
`-- option-pricing-latest/run.json
```

The runtime owns `.ducketz-option-pricing-runtime.lock`. Create a timestamped working/run directory, write exact-schema outputs, write and verify the manifest, atomically create the receipt, then atomically replace the authoritative pointer. Interrupted or orphaned directories are invisible. Link each Pricing receipt to the prior verified Pricing publication so prospective recovery can prove reachability. Reject path escapes, tampering, missing files, checksum mismatches, incompatible schemas, and broken publication chains.

One symbol/route failure must not erase valid routes. Record route errors in JSON metadata, publish successful routes, and never reuse stale output under an incompatible contract.

### Parquet identity and grains

Every Parquet begins with exactly one readable Duckets-generated string `id`. Do not add generated `*_id`, receipt, lineage, digest, UUID, or hash columns. Provider-native instrument IDs may exist only in raw provider evidence, not normalized/model outputs. Put control-plane lineage and checksums in JSON.

Use these natural grains unless repository inspection proves a stricter grain is necessary:

```text
pricing sample:
  symbol | target_snapshot_for | contract_symbol

pricing prediction:
  symbol | target_snapshot_for | contract_symbol | prediction_created_at

pricing evaluation:
  symbol | target_snapshot_for | contract_symbol | prediction_created_at

pricing surface:
  symbol | target_snapshot_for | call_put | expiration_bucket | moneyness_bucket

pricing monitoring:
  metric_name | scope_type | scope_value | monitored_at
```

Declare exact Arrow schemas in the repository's schema layer. Include readable timestamps and statuses sufficient to prove source snapshot, source availability, target snapshot, prediction creation, quote observation, evaluation, model/policy version, raw/constrained fair value, Black-Scholes baseline, observed BBO/mid, residual, predictive standard deviation, 80/95 intervals, spread-relative edge, and quality/constraint state. Do not persist duplicate workflow timestamps merely because they were convenient during assembly.

`pricing-surfaces.parquet` is the compact consumer boundary. At minimum report causal coverage, median normalized residual, median uncertainty, median model-vs-market edge in half-spread units, positive/negative edge fractions, raw and constrained arbitrage-violation rates, interval coverage when matured, liquidity/spread summaries, and an explicit surface status by moneyness/expiration bucket.

## Historical OPRA acquisition and normalization

Add a narrow historical OPRA provider/import command; do not put OPRA backfill into `datafetching.options_runtime`.

Use the configured `DATABENTO_API_KEY` without printing it. Use the official `OPRA.PILLAR` dataset, `definition` records, and consolidated `cbbo-1m` records. The official examples support parent symbology such as `NVDA.OPT` with `stype_in="parent"`; definitions expose strike, expiration, and instrument class. Preserve exact SDK fields in raw evidence and normalize prices/timestamps explicitly.

Relevant official references:

- https://databento.com/docs/examples/options/option-spreads
- https://databento.com/docs/examples/options/equity-options-introduction/using-parent-symbology-to-fetch-an-option-chain
- https://databento.com/docs/schemas-and-data-formats/cbbo
- https://databento.com/docs/venues-and-datasets/opra-pillar
- https://databento.com/docs/reference-historical/basics/

The importer must be safe by construction:

- default to metadata/cost estimation only;
- use `Historical.metadata.get_cost` before every proposed request;
- print the exact dataset, schema, symbols, UTC windows, estimated cost, and configured ceiling;
- require an explicit execution flag and `--max-cost-usd` before any paid `get_range`/batch call;
- abort before download if the summed estimate exceeds the ceiling;
- estimate definitions in whole-day windows and CBBO in discrete ten-minute windows because Databento documents estimator caveats outside those units;
- fetch and filter definitions first, then request only eligible raw option symbols and narrow windows;
- stream large approved downloads to bounded files rather than materializing an unbounded response in memory;
- make imports resumable and idempotent through immutable manifests/receipts;
- mock the Databento client in every automated test;
- never execute the paid path as part of this task.

The configurable pilot defaults are NVDA, GOOG, and MU; six historical months; four fixed `America/New_York` market times per eligible session; calls and puts; 7-120 DTE; and the moneyness/quality limits above. Use `exchange_calendars` for holidays, early closes, and DST. Do not hardcode UTC offsets. Expose times and scope as CLI arguments, and record the exact resolved schedule in import metadata.

Normalize OPRA into a provider-specific immutable historical evidence contract that the Pricing materializer can read alongside Schwab without pretending the sources are identical. Preserve provider source in rows and report OPRA-to-Schwab drift separately. Use semantic contract columns (`symbol`, expiration, call/put, strike, multiplier) for cross-provider matching; do not manufacture an opaque cross-provider contract ID.

## Prerequisite Strategy bug fix

Before evaluating any Strategy improvement, fix the existing multi-symbol natural-key failure in `ml.strategy_selection.model.partition_strategy_outcomes`.

The persisted Strategy candidate grain is already:

```text
symbol | horizon | decision_timestamp | candidate_key
```

The current duplicate check uses only `decision_timestamp | candidate_key`, so two symbols with the same timestamp, strategy geometry, and expiration can be falsely treated as one duplicate. Make the validation use the full normalized natural grain (including symbol and horizon), while true duplicates at that grain still fail closed.

Add regression tests proving:

- identical candidate keys at one timestamp for two symbols are accepted;
- an exact duplicate of symbol/horizon/timestamp/candidate is rejected;
- snapshot-cluster partitioning still keeps the shared time together and does not reinterpret contract/candidate rows as independent decisions.

Do this narrowly; do not weaken other Strategy duplicate or causal checks.

## Shadow consumers

Implement consumer integration only as explicit shadow behavior.

### Directional Loop B

Add an explicit feature profile that can point-in-time join verified compact Pricing surface features. Do not add Pricing to the existing default feature profile. The join must require a verified Pricing publication and `published_at`/row availability no later than the directional decision cutoff. Use a clear prefix such as `opx__`, version the feature contract, and preserve route isolation. Missing, late, incompatible, or tampered Pricing evidence makes that optional feature route unavailable; it must never cause the default directional route to read future data or silently substitute current values.

### Strategy

Add an explicit `off|shadow` Pricing mode with `off` preserving current behavior. In shadow mode, join only an exact, pre-quote, verified Pricing prediction for every option leg by semantic contract and target snapshot. Compute and persist diagnostics such as:

```text
long-leg edge  = constrained fair value - executable ask
short-leg edge = executable bid - constrained fair value
candidate edge = signed sum after multiplier and quantity
edge-to-friction = candidate edge / modeled spread-and-fee friction
```

Aggregate uncertainty conservatively unless a tested covariance contract exists. Record coverage and reasons when a leg is missing. Compare the shadow edge with the existing Greek/BBO scenario prior and future observed Strategy pseudo-outcome, but do not change `decision_score`, `candidate_rank`, order construction, UI ordering, or submission behavior in this build. The existing fallback values must be logically unchanged when Pricing is off or unavailable.

Do not add a trade threshold. Do not call Pricing output an arbitrage opportunity or guaranteed profit.

## Assessment and the 10-part gate

Primary pricing metrics are snapshot-weighted: first aggregate contract losses within each complete target snapshot with equal total snapshot weight, then aggregate snapshots chronologically. Report contract-row metrics only as secondary diagnostics.

At minimum report:

- normalized MAE and RMSE using `error / underlying_price`;
- dollar MAE as a secondary metric;
- error relative to half-spread;
- 80% and 95% interval coverage and average width;
- negative/bound/monotonicity/convexity violations before and after projection;
- projection magnitude;
- metrics by symbol, call/put, moneyness bucket, expiration bucket, liquidity bucket, and volatility regime;
- OPRA-versus-Schwab source drift;
- realized outcome by predicted edge bucket, with monotonicity diagnostics;
- Strategy shadow performance after observed bid/ask and configured contract fees versus its existing scenario prior.

Implement a readable gate report with ten independently visible statuses; do not compress failures into a flattering composite score. A route is not production-eligible unless all are proven:

1. source lineage, point-in-time timing, schema, and publication chain pass;
2. 252/63/63 partitions plus the redacted 126-cluster lockbox and six-month span exist;
3. BSGP snapshot-weighted normalized error beats causal Black-Scholes;
4. BSGP beats the constant-residual comparator;
5. BSGP beats the standard-GP comparator under the same budget;
6. 80% and 95% interval coverage are within documented predeclared tolerances without using assessment to tune them;
7. constrained outputs have no material arbitrage violations and projection magnitude stays below a predeclared tolerance;
8. predicted edge remains meaningful relative to spread and improves monotonically across predeclared edge buckets;
9. Strategy shadow results improve on the current Greek/BBO prior after spreads and contract fees without changing live rankings;
10. at least 60 receipt-proven prospective completed predictions per symbol span at least 20 sessions.

Keep the 126-cluster lockbox closed in this implementation. Its presence is a readiness condition, not permission to score it. The gate must remain `NOT_PRODUCTION_ELIGIBLE` while any item lacks evidence, and `automated_action_allowed` must remain false even if fixture tests pass.

## Tests

Add focused deterministic tests covering at least:

- reference Black-Scholes call/put values with dividends;
- expiry, zero-volatility, invalid-input, and numerical-tail behavior;
- American pointwise bounds and call/put strike monotonicity/convexity projection;
- no contemporaneous target quote/IV/Greek entering a feature row;
- strict earlier-surface selection and no look-forward interpolation;
- target quote timestamp strictly after prediction creation;
- stale pre-prediction quotes excluded from evaluation/live counts;
- exact-contract reconciliation and missing-contract status;
- snapshot-cluster weighting and chronological boundary purging;
- 252/63/63/126 separation and lockbox redaction;
- deterministic BSGP/standard-GP fitting, interval calibration, and comparators;
- model-reuse success plus invalidation for every material contract change;
- exact Arrow schemas and one readable `id` per Parquet;
- atomic publication, prior-receipt chaining, interruption recovery, tamper detection, and path-escape rejection;
- route isolation when one symbol fails;
- duplicate-cycle canonicalization;
- OPRA cost-only default, ceiling rejection, resumability, and mocked paid path;
- OPRA price scaling, definition as-of join, expiration date handling, and CBBO interval semantics;
- the Strategy multi-symbol duplicate regression;
- Pricing-off/missing/tampered fallback for Loop B and Strategy;
- no change to candidate rank, UI order, or Schwab order request in shadow mode;
- independent runtime isolation: no runtime starts another runtime or writes another runtime's authority.

Use synthetic/fixture chains with known residual functions so the model tests prove it learns the residual rather than merely checking that code runs. Do not make fixture performance count as real evidence.

Run the focused tests while iterating, then run the full suite. Report exact commands and results. If the full suite exposes an unrelated pre-existing failure, prove that with a focused comparison rather than suppressing it.

## Documentation and operating commands

Add a focused design/operations document under `docs/datafetch-ml/` that explains:

- the thesis formula and the production departures;
- the six-runtime ownership map;
- the pre-quote timing sequence;
- datastore topology and authority;
- OPRA estimate/import safety;
- feature, target, uncertainty, bounds, evidence, model-reuse, and fallback contracts;
- explicit limitations: midpoint is not a fill, adverse BBO pseudo-outcomes are not broker executions, American early exercise is learned only through residual evidence, and no production claim exists yet.

Update existing orchestration documentation and example start commands without claiming deployment. Show Pricing in its own terminal before the existing Options phase, with a documented skip when the current target bar is not ready. Keep deployment and supervisor restart as explicit operator actions.

Provide useful `--help` output and `--once` modes. A representative shadow command should be possible without changing defaults elsewhere, for example:

```powershell
python -m ml.option_pricing_runtime `
  --datastore-target pc `
  --symbols NVDA GOOG MU `
  --interval-minutes 15 `
  --phase-offset-minutes 1 `
  --once
```

The OPRA command must demonstrate estimate-only behavior separately. Do not run an approved/download form in this task.

## Implementation order

Use this order so each step leaves a testable system:

1. audit the current working tree and relevant contracts;
2. fix and test the Strategy full-natural-key duplicate bug;
3. implement and test pricing math, causal surface construction, bounds, schemas, partitions, comparators, BSGP approximation, uncertainty, and model reuse;
4. implement fixture-backed OPRA normalization and the metadata-only cost guard;
5. implement immutable Pricing runtime publication and prospective reconciliation;
6. implement compact surfaces, monitoring, gate reporting, and shadow-only consumers;
7. update documentation and operating examples;
8. run focused and full verification, inspect the diff for Duckets Law, and remove unused code.

## Final response

Lead with what is now working. Summarize:

- files and contracts added or changed;
- the exact causal timing guarantee;
- how BSGP differs from the thesis implementation;
- publication/fallback behavior;
- tests run and results;
- what remains operationally unavailable because no paid OPRA download, model freeze, closed-lockbox scoring, or prospective live collection was authorized.

Do not claim the model is profitable, production-ready, calibrated on real Duckets data, or deployed unless the repository and receipt-proven evidence actually demonstrate that.
