# Active Pricing / logical Loop 3

## Identity

- Canonical name: Active Pricing runtime
- Logical aliases or numbering: logical Loop 3; startup owner 4
- Runtime entry point: `python -m ml.option_pricing_runtime`
- Owning package: `ml.option_pricing`
- Classification: Independent production loop
- Scheduling mechanism: recurring quarter-hour supervisor plus an asynchronously launched, one-shot local model worker
- Cadence and phase: every 15 minutes at UTC phase +1 minute; the target is the calendar-owned eligible quarter-hour
- Lock or single-writer mechanism: `.ducketz-option-pricing-runtime.lock`; the owned worker separately uses `.ducketz-loop-native-bsgp-worker.lock`
- Primary code evidence: **Confirmed.** `ml/option_pricing_runtime.py:1480`, `ml/option_pricing_runtime.py:1501`, `ml/option_pricing_runtime.py:1560`, `ml/option_pricing_runtime.py:1564`, `ml/option_pricing_loop_native_worker.py:50`

## Purpose

**Confirmed:** Pricing is the option-valuation authority. Its fast path waits for exact Loop A bar readiness, selects causally earlier option evidence and rates, publishes a constrained Black–Scholes baseline point estimate, and publishes the implemented finite-basis residual value/uncertainty as a one-to-one sidecar for active Strategy and eligibility consumers. After that target authority exists, it reconciles later observations, evaluates prior predictions, constructs compact surfaces, updates monitoring/research reports, and publishes a full generation pointer. `ml/option_pricing_runtime.py:327`, `ml/option_pricing_runtime.py:360`, `ml/option_pricing_runtime.py:660`, `ml/option_pricing_runtime.py:707`, `ml/option_pricing/strategy_shadow.py:263`

**Confirmed `BLACK-SCHOLES-OP` implementation:** the reference model sets `f(x)=BS(x)+delta(x)` over `(S,K,r,sigma,t,d)`. Ducketz implements that same mean-plus-residual structure with six semantic inputs and predictive uncertainty, but replaces the reference's exact GP/MCMC with a production-bounded 128-component Nyström RBF map and Bayesian-ridge posterior. The exact-GP SPY implementation is separate and research-only. `docs/edu/BLACK-SCHOLES-OP.md:327`, `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/policies.py:21`, `ml/option_pricing/model.py:68`, `ml/option_pricing/research_benchmark.py:34`

**Confirmed non-ownership:** it does not obtain live option chains from providers, declare Loop A bars ready, build directional labels, rank strategies, or make automated trading decisions. The owned worker performs no external provider requests and is not an eighth loop. `ml/option_pricing_loop_native_worker.py:126`, `ml/option_pricing_loop_native_worker.py:141`

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Exact equity-bar readiness | Loop A | target-scoped readiness directory, manifest, receipt, all-symbol bar rows | target, per-symbol close and decision clock, evidence files/checksums, `ready_at`, Loop A generation | Exact target and complete requested symbol set; wait is bounded; prediction time cannot exceed target +1,200 seconds | Required in production | **Confirmed.** `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1134`, `ml/option_pricing_runtime.py:1186` |
| Earlier committed option chains and later outcomes | Options Capture | verified OPRA/Schwab option snapshot receipt/pointer chain and Parquet contracts | symbol/contract, call/put, expiration, strike, multiplier, bid/ask/mid, IV, quote/snapshot/receipt clocks, provider, evidence lane, quality and `fallback_used` | Source chain must be strictly causal for prediction; outcome evidence must become available after prediction; canonical selection prefers OPRA and retains Schwab as explicit fallback/comparison | Required for rows; absence produces per-symbol non-ready/failed status, not invented quotes | **Confirmed.** `ml/option_pricing_runtime.py:660`, `ml/option_pricing/causal.py:1019`, `options/README.md:20`, `ml/parquet_contracts.py:350` |
| Point-in-time interest rates | Loop A current FRED and Daily ALFRED history; optional FMP curve artifact | rate observation loader | tenor/date/rate, source and `available_at`; FEDFUNDS is percentage points converted to a decimal rate | Prefer causal FMP Treasury curve; otherwise latest causal ALFRED/FRED observation strictly available by boundary | Required semantic input; layered source fallback | **Confirmed.** `ml/option_pricing/rates.py:236`, `ml/option_pricing/rates.py:361`, `ml/option_pricing/rates.py:397` |
| Dividend evidence and contract definitions | Loop A/provider history and local option evidence | point-in-time dividend/contract loaders | cash dividends, ex-date, present value/equivalent yield, confidence; exercise style, settlement, multiplier | Only records available by the decision boundary; explicit fallback is recorded | Required inputs with explicit fallback lanes | **Confirmed.** `ml/parquet_contracts.py:405`, `ml/parquet_contracts.py:411`, `ml/option_pricing/policies.py:11` |
| Prior Pricing prediction/publication chain | This loop | `ml/option-pricing-target-outcomes/`, `ml/option-pricing-runs/`, current pointers | prior LIVE rows, publication clocks, receipt lineage, model/surface status | All reachable generations and receipts are verified; target publications are immutable/idempotent | Optional on bootstrap; mandatory to trust when present | **Confirmed.** `ml/option_pricing/target_outcome.py:121`, `ml/option_pricing/publication.py:92`, `ml/option_pricing/consumers.py:306` |
| Prior local residual-model generation | Owned worker | loop-native materialization/model generation and receipt | 128-component Nyström RBF Bayesian-ridge normalized-residual model; trained-through, expiry, route support, posterior uncertainty | Must be receipt-verified, trained earlier and unexpired; stale/missing/unsupported model produces an explicit Black–Scholes sidecar fallback | Optional for residual lift; baseline point estimate remains available | **Confirmed.** `ml/option_pricing/policies.py:21`, `ml/option_pricing/policies.py:128`, `ml/option_pricing_runtime.py:1143` |

## Processing and decisions

1. **Confirmed:** derive the only calendar-eligible quarter-hour target; reject replayed or future targets and targets beyond the 1,200-second causal source window. `ml/option_pricing_runtime.py:251`, `ml/option_pricing_runtime.py:255`, `ml/option_pricing_runtime.py:282`
2. **Confirmed:** reuse an existing immutable target outcome unless it is a legacy retryable `TARGET_BAR_NOT_READY`; otherwise wait for exact all-symbol readiness. Missing readiness remains retryable and publishes nothing. `ml/option_pricing_runtime.py:1062`, `ml/option_pricing_runtime.py:1080`, `ml/option_pricing_runtime.py:1128`
3. **Confirmed:** per symbol, build rows for standard 100-share contracts with 7–120 days to expiry and absolute log-moneyness at most 0.25, using earlier chain/volatility, causal rate/dividend inputs and the Loop A close. `ml/option_pricing/policies.py:61`, `ml/option_pricing_runtime.py:1181`
4. **Confirmed:** the authoritative baseline predictions deliberately pass `models={}`: residual mean is zero and fitted uncertainty/interval fields are null. A verified prior model separately prices `BS + normalized residual × underlying`, calibrates uncertainty, and shape-projects the surface into a keyed sidecar; a missing model creates explicit fallback intervals. Every sidecar row keeps `automated_action_allowed=false`. `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/prediction.py:98`, `ml/option_pricing/prediction.py:110`, `ml/option_pricing/prediction.py:250`, `ml/option_pricing/target_outcome.py:165`
5. **Confirmed:** publish one immutable target authority containing samples, predictions or terminal status, shadow sidecar, manifest and receipt; atomically advance its pointer after files are complete. `ml/option_pricing_runtime.py:1305`, `ml/option_pricing/target_outcome.py:192`, `ml/option_pricing/target_outcome.py:238`
6. **Confirmed:** launch the owned worker after the fast publication without blocking Pricing or Options. The worker materializes verified imported historical OPRA first, combines it with causal Schwab fallback history, trains locally at most once per refresh window, and records provider row counts/status; it has no recurring supervisor and makes zero provider requests. `ml/option_pricing_runtime.py:418`, `ml/option_pricing_loop_native_worker.py:38`, `ml/option_pricing_loop_native_worker.py:58`, `ml/option_pricing_loop_native_worker.py:118`, `ml/option_pricing_loop_native_worker.py:126`
7. **Confirmed:** reconcile receipt-proven predictions with later quotes, form chronological train/calibration/assessment/closed-lockbox partitions, compute evaluations/surfaces/monitoring, and publish a verified full generation. Lockbox target values are not reported and automated action remains disabled. `ml/option_pricing_runtime.py:466`, `ml/option_pricing/policies.py:82`, `ml/option_pricing_runtime.py:2233`, `ml/option_pricing_runtime.py:2235`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Target-scoped Pricing authority | Options Capture; this loop | `ml/option-pricing-target-outcomes/<target-created>/` and `ml/option-pricing-target-latest/run.json` | per-symbol `READY`, `TARGET_ALREADY_OBSERVED`, `PRICING_FAILED` or terminal state; baseline LIVE constrained Black–Scholes point values, with fitted uncertainty null; readiness proof and publication clock | Immutable natural target; exact symbol scope; predictions must be LIVE; files then receipt/pointer; transient missing readiness normally creates no artifact | **Confirmed.** `ml/option_pricing/target_outcome.py:89`, `ml/option_pricing/prediction.py:98`, `ml/option_pricing/target_outcome.py:151`, `ml/option_pricing_runtime.py:1271` |
| Residual-model sidecar | Strategy active pricing; research/eligibility; this loop | `pricing-bsgp-shadow.parquet` beside target outcome | `BSGP_SHADOW_READY` residual fair value, posterior standard deviation, raw/constrained 80/95% intervals, support distance/shrinkage, model lineage, projection violations/status; explicit Black–Scholes fallback rows otherwise | One-to-one natural keys with baseline; cannot replace the fast baseline file or authorize automated action, but Strategy canonicalizes ready rows to `pricing_source=BSGP` and fallback rows to `BLACK_SCHOLES` | **Confirmed.** `ml/parquet_contracts.py:496`, `ml/option_pricing/target_outcome.py:165`, `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298` |
| Full Pricing generation | Directional Loop B, Strategy, UI/readers, this loop | `ml/option-pricing-runs/<generation>/` and `ml/option-pricing-latest/run.json` | samples; contract predictions; realized evaluations including dollar/normalized errors and interval coverage; compact surfaces; monitoring; reports/models | Required files are schema/checksum verified; receipt links previous authority; pointer cannot regress and orphan adoption needs explicit recovery | **Confirmed.** `ml/option_pricing/publication.py:36`, `ml/option_pricing/publication.py:83`, `ml/option_pricing/publication.py:107`, `ml/option_pricing_runtime.py:2271` |
| Compact pricing surfaces | Directional Loop B; Strategy pricing catalog | `pricing-surfaces.parquet` in current verified generation | surface buckets by call/put, expiry and moneyness; causal coverage, median residual/uncertainty/edge, interval coverage, quote quality, `PASS`/`REJECT`, first availability and `fresh_until` | Consumer may use only receipt-proven, causal, fresh, quality-passing surfaces; each row identifies generation/supersession | **Confirmed.** `ml/parquet_contracts.py:619`, `ml/parquet_contracts.py:661`, `ml/option_pricing_runtime.py:2306`, `ml/option_pricing/consumers.py:478` |
| Owned-worker materialization/model/status | Fast shadow path in later Pricing targets | loop-native materialization/model generations; `ml/option-pricing-loop-native-worker/latest.json` | OPRA/Schwab row counts, precedence, generation/status, cutoff, no-provider/no-action attestations | Separate lock; minimum refresh; receipt-required model publication; failure records baseline fallback and does not retract fast authority | **Confirmed.** `ml/option_pricing_loop_native_worker.py:65`, `ml/option_pricing_loop_native_worker.py:79`, `ml/option_pricing_loop_native_worker.py:102`, `ml/option_pricing_loop_native_worker.py:209` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop A supplies exact readiness/close and current causal provider context. `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1187`
- **Confirmed:** Daily ALFRED supplies point-in-time FEDFUNDS rate fallback. `ml/option_pricing/rates.py:361`, `datafetching/fred_vintages.py:364`
- **Confirmed:** Options Capture supplies prior chains and later observed quotes/outcomes. `ml/option_pricing/consumers.py:372`, `ml/option_pricing_runtime.py:466`

### Downstream

- **Confirmed:** Options Capture waits for or records this target outcome as a sequencing proof, but can capture pending evidence if it is absent. `datafetching/pricing_barrier.py:77`, `datafetching/options_runtime.py:349`
- **Confirmed:** Directional Loop B consumes compact verified `opx__` surface features. `ml/rolling_materialization.py:663`
- **Confirmed:** Strategy's active catalog reads the target sidecar and full verified history, maps ready residual rows to `BSGP` and complete fallback rows to `BLACK_SCHOLES`, then attaches exact leg values before scoring; unavailable coverage retains the scenario-prior fallback. `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:288`

### Timing and control relationships

**Confirmed:** intended order is Loop A +00:20, Pricing +1 minute, B +5, Options +6, Strategy +10. Pricing waits only for Loop A; its separate target outcome gives Options causal barrier credit. The worker and research tail are deliberately off the target-authority critical path. `docs/datafetch-ml/current_start_command:55`, `docs/datafetch-ml/current_start_command:88`, `ml/option_pricing_runtime.py:360`, `ml/option_pricing_loop_native_worker.py:141`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | Verified Pricing generation → compact `opx__` surfaces → Loop B samples/features → directional probabilities. `ml/option_pricing/consumers.py:30`, `ml/rolling_materialization.py:663`, `ml/runtime_pipeline.py:493` |
| Option-pricing predictions | Direct | Loop A target plus earlier OPRA/Schwab chain, rate, dividend and volatility → authoritative constrained Black–Scholes baseline point value plus separately published finite-basis residual/fallback sidecar → target and full Pricing authorities. `ml/option_pricing_runtime.py:1181`, `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/target_outcome.py:93` |
| Options-strategy predictions | Indirect | verified ready BSGP or Black–Scholes contract values → exact candidate leg pricing/eligibility → fitted profitable-outcome probability, or unavailable coverage → explicit scenario fallback → rank. `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/runtime.py:311` |

**Roll-up classification: Both.** It directly owns option-pricing predictions and has evidenced indirect paths to horizon and strategy predictions.

## Failure and degradation behavior

- **Confirmed:** missing readiness before the bounded wait is retryable and write-free; after the 1,200-second window the target is causally ineligible. `ml/option_pricing_runtime.py:1128`, `ml/option_pricing_runtime.py:1137`, `ml/option_pricing_runtime.py:1713`
- **Confirmed:** stale/missing loop-native model or inference failure produces an explicit Black–Scholes sidecar fallback with configured wider uncertainty; it cannot break authoritative baseline publication. `ml/option_pricing_runtime.py:1143`, `ml/option_pricing/prediction.py:286`, `ml/option_pricing_runtime.py:1244`
- **Confirmed:** per-symbol input errors become terminal symbol status; mixed valid/failed scope is labeled `MIXED_TERMINAL`. An existing immutable outcome wins on restart. `ml/option_pricing_runtime.py:1203`, `ml/option_pricing_runtime.py:1258`, `ml/option_pricing_runtime.py:1320`
- **Confirmed:** a corrupt pointer/run/receipt fails closed; ordinary publication cannot regress or silently adopt an orphan. `ml/option_pricing/publication.py:67`, `ml/option_pricing/publication.py:107`, `ml/option_pricing/publication.py:120`
- **Confirmed:** Loop B treats an explicitly unavailable Pricing surface as baseline absence, but corruption aborts; Strategy can retain explicit Black-Scholes/scenario fallback status. `ml/option_pricing/consumers.py:63`, `ml/strategy_runtime.py:553`

## Accuracy and efficiency relevance

- Leakage/target integrity: exact target ownership, readiness receipt, earlier-source clocks, later-outcome clocks, immutable target natural key, and sealed lockbox. `ml/option_pricing_runtime.py:1134`, `ml/parquet_contracts.py:394`, `ml/option_pricing_runtime.py:2230`
- Prediction quality: the implemented `BS + residual` structure, six semantic inputs, causal OPRA-first evidence, liquidity/quality weights, constrained no-arbitrage projection, residual uncertainty and interval coverage. `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/policies.py:42`, `ml/option_pricing/model.py:561`, `ml/parquet_contracts.py:469`
- Critical-path latency: baseline target authority precedes research, reconciliation, model refresh and the nonblocking worker. `ml/option_pricing_runtime.py:327`, `ml/option_pricing_runtime.py:360`, `ml/option_pricing_loop_native_worker.py:141`
- Model reuse/provider volume: earlier verified model is reused until expiry; worker refresh is throttled and uses only persisted evidence with zero provider calls. `ml/option_pricing_runtime.py:313`, `ml/option_pricing_loop_native_worker.py:154`, `ml/option_pricing_loop_native_worker.py:199`
- I/O: immutable wide generation plus compact surface; current consumers verify receipt chains before use. `ml/option_pricing_runtime.py:2261`, `ml/option_pricing/consumers.py:306`

## Conflicts, gaps, and uncertainty

- **Confirmed deliberate boundary, not conflict:** “active” describes the implemented residual model/consumer path and eligibility profile; the fast baseline file remains Black–Scholes while the receipt-bound sidecar is actively usable by Strategy when `BSGP_SHADOW_READY`. Tests assert that adding the sidecar cannot mutate the baseline. `ml/option_pricing/policies.py:34`, `ml/option_pricing/strategy_shadow.py:298`, `tests/test_option_pricing_loop_native_bsgp.py:706`, `tests/test_option_pricing_loop_native_bsgp.py:777`
- **Confirmed aliasing:** “logical Loop 3” is the functional name; startup owner 4 reflects Daily ALFRED's insertion as owner 3. The obsolete SVG is not current deployment evidence. `docs/datafetch-ml/current_start_command:61`, `docs/datafetch-ml/current_start_command:72`
- **Confirmed methodology adaptation:** the production model is not the reference thesis's exact GP/MCMC. This is an explicit bounded implementation choice, not missing BSGP setup. `docs/edu/BLACK-SCHOLES-OP.md:472`, `ml/option_pricing/model.py:68`, `options/README.md:173`
- **Unknown operational/empirical state:** static analysis does not establish that historical OPRA acquisition has executed, the residual eligibility gate currently passes, live histories are populated, or residual estimates outperform Black–Scholes prospectively.
- **Confidence:** High for ownership, OPRA/Black–Scholes-residual mechanics, fast authority, worker classification, and direct consumers; Medium for live activation and empirical lift.

## Evidence index

- `ml/option_pricing_runtime.py:182`
- `ml/option_pricing_runtime.py:1036`
- `ml/option_pricing_runtime.py:1220`
- `ml/option_pricing_runtime.py:1305`
- `ml/option_pricing_runtime.py:2239`
- `ml/option_pricing/target_outcome.py:93`
- `ml/option_pricing/publication.py:83`
- `ml/option_pricing_loop_native_worker.py:38`
- `tests/test_pricing_options_sequencing.py:594`
- `tests/test_option_pricing_loop_native_bsgp.py:706`
- `docs/edu/BLACK-SCHOLES-OP.md:441`
- `tests/test_option_pricing_opra.py:313`
