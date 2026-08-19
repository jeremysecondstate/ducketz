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

**Startup/bootstrap boundary:** Pricing is a computation and publication owner,
not a Databento history owner. On an empty datastore it waits for verified Loop
A, rates, and option evidence. The included Standard-plan OPRA bootstrap and
Options-owned continuation create that historical evidence; Pricing reads only
receipt-verified partitions and never initiates or expands a provider request.

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Exact equity-bar readiness | Loop A | target-scoped readiness directory, manifest, receipt, all-symbol bar rows | target, per-symbol close and decision clock, evidence files/checksums, `ready_at`, Loop A generation | Exact target and complete requested symbol set; wait is bounded; prediction time cannot exceed target +1,200 seconds | Required in production | **Confirmed.** `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1134`, `ml/option_pricing_runtime.py:1186` |
| Earlier committed option chains and later outcomes | Options Capture | verified OPRA/Schwab option snapshot receipt/pointer chain and Parquet contracts | symbol/contract, call/put, expiration, strike, multiplier, bid/ask/mid, IV, quote/snapshot/receipt clocks, provider, evidence lane, quality and `fallback_used` | Source chain must be strictly causal for prediction; outcome evidence must become available after prediction; canonical selection prefers OPRA and retains Schwab as explicit fallback/comparison | Required for rows; absence produces per-symbol non-ready/failed status, not invented quotes | **Confirmed.** `ml/option_pricing_runtime.py:660`, `ml/option_pricing/causal.py:1019`, `options/README.md:20`, `ml/parquet_contracts.py:350` |
| Canonical historical OPRA | One-time bootstrap plus Options-owned catch-up | verified `market-data/databento/opra/OPRA.PILLAR` definition and CBBO partitions | provider identity, exact request bounds, contract definitions, pretarget quotes, raw/normalized checksums and event/availability clocks | Only receipt-verified nonempty partitions; definitions are joined point-in-time and quotes cannot follow their target; no provider request occurs in Pricing or its worker | Optional offline replay/model evidence; never fabricated when absent | **Confirmed.** `datafetching/databento_opra_history.py`, `ml/option_pricing/opra_materialization.py` |
| Point-in-time interest rates | Loop A current FRED and Daily ALFRED history | rate observation loader | date/rate, source event/receipt and `available_at`; FEDFUNDS is percentage points converted to a decimal rate | Live construction requires the latest causal FRED/ALFRED observation and disables option-provider/FMP-curve fallback | Required semantic input; missing evidence makes the route unavailable | **Confirmed.** `ml/option_pricing/causal.py:264`, `ml/option_pricing/causal.py:446`, `ml/option_pricing/causal.py:468`, `ml/option_pricing/rates.py:361` |
| Dividend evidence and contract definitions | Loop A/provider history and local option evidence | point-in-time dividend/contract loaders | cash dividends, ex-date, present value/equivalent yield, confidence; exercise style, settlement, multiplier | Only declarations available by the decision boundary enter live carry; offline fallbacks remain separately labeled | Required inputs; missing live declarations resolve under the explicit zero-known-dividend confidence lane | **Confirmed.** `ml/parquet_contracts.py:405`, `ml/parquet_contracts.py:411`, `ml/option_pricing/policies.py:11` |
| Prior Pricing prediction/publication chain | This loop | `ml/option-pricing-target-outcomes/`, `ml/option-pricing-runs/`, current pointers | prior LIVE rows, publication clocks, receipt lineage, model/surface status | All reachable generations and receipts are verified; target publications are immutable/idempotent | Optional on bootstrap; mandatory to trust when present | **Confirmed.** `ml/option_pricing/target_outcome.py:121`, `ml/option_pricing/publication.py:92`, `ml/option_pricing/consumers.py:306` |
| Prior local residual-model generation | Owned worker | loop-native materialization/model generation and receipt | 128-component Nyström RBF Bayesian-ridge normalized-residual model; trained-through, expiry, route support, posterior uncertainty | Must be receipt-verified, trained earlier and unexpired; stale/missing/unsupported model produces an explicit Black–Scholes sidecar fallback | Optional for residual lift; baseline point estimate remains available | **Confirmed.** `ml/option_pricing/policies.py:21`, `ml/option_pricing/policies.py:128`, `ml/option_pricing_runtime.py:1143` |

### Six-input evidence contract

| Input | Producer/source; concrete artifact and fields | Units | Event, receipt, availability, and freshness | Missing-value behavior | Training and live usage | Tests | Correction status |
|---|---|---|---|---|---|---|---|
| `S` | Loop A exact target readiness; per-symbol completed-bar `close`, bar timestamp/provider/timeframe, manifest/receipt and `ready_at` | USD per underlying share | The selected one-minute bar end/decision timestamp must equal the target; its stored bar timestamp must resolve exactly one row. Receipt/`ready_at` must precede construction and target age must remain inside 1,200 s | Missing/nonpositive/mismatched target close rejects that symbol/target; no option-chain underlying substitute | Same semantic column `underlying_price` scales the normalized residual in training and is the exact Loop A close in live inference | **Confirmed.** `tests/test_option_pricing_loop_native_bsgp.py:258`, `tests/test_option_pricing_loop_native_bsgp.py:430` | Already correct; preserved exact Loop A authority. `datafetching/decision_time.py:467`, `datafetching/decision_time.py:496`, `ml/option_pricing/causal.py:192`, `ml/option_pricing/causal.py:225`, `ml/option_pricing/causal.py:400` |
| `K` plus contract semantics | Loop 4 committed `contracts.parquet`; effective OPRA definition fields `strike`, `call_put`, `expiration_date`, `multiplier`, `exercise_style`, `settlement_type`, `standard_contract`, `definition_effective_at`, `definition_activation_at`, CFI and definition clocks | USD per underlying share; multiplier shares; categorical contract semantics | Definition provider receipt/effective and contract-activation clocks must be no later than the market target; the definition event cannot follow provider receipt; local definition receipt must be no later than snapshot publication, and the committed source must be visible before prediction. Source quote must be fresh (≤1,200 s) and strictly pretarget; the later outcome must match the same semantic contract | Ambiguous/inactive/not-yet-active/nonstandard, wrong multiplier, unsupported style/settlement, changed semantics, or missing contract rejects the row | Definition columns determine call/put Black–Scholes and shape groups in both training and live rows; observed target price is never a feature | **Confirmed.** `tests/test_databento_opra_live.py:278`, `tests/test_databento_opra_live.py:318`, `tests/test_option_pricing_core.py:198` | Corrected: live definitions now use provider-receipt selection, activation eligibility, separate local availability, and validated CFI semantics; ambiguous contracts fail closed. `options/databento_live.py:333`, `options/databento_live.py:425`, `options/snapshot.py:297` |
| `r` | Daily ALFRED/current FRED rate receipt; `risk_free_rate`, source/event/receipt/`available_at` and policy identity | Decimal continuously compounded annual rate | Observation availability must be strictly before the source/decision boundary; maturity resolution is target-to-expiration causal | Missing or invalid live FRED/ALFRED evidence yields `RATE_UNAVAILABLE`; provider and FMP-curve substitution are disabled live | Same `risk_free_rate` feature enters Black–Scholes and the residual design matrix for training/live inference | **Confirmed.** `tests/test_option_pricing_loop_native_bsgp.py:474`, `tests/test_option_pricing_loop_native_bsgp.py:499` | Corrected: live path now requires FRED/ALFRED rather than preferring FMP/provider rate fields. `ml/option_pricing/causal.py:264`, `ml/option_pricing/causal.py:450`, `ml/option_pricing/policies.py:10` |
| `σ` | Earlier committed OPRA-preferred/Schwab-fallback surface; source bid/ask, source underlying, source definition, source quote/snapshot/receipt clocks; field `lagged_implied_volatility` | Decimal annualized volatility | Source snapshot and quote are strictly earlier than target and receipt-visible; interpolation stays inside earlier strike/tenor support with ≤1,200 s source staleness | Failed IV solve or interpolation/extrapolation need yields `VOLATILITY_UNAVAILABLE` | Earlier-price IV is used in training and live inference; target/later option price is used only as an outcome after prediction publication | **Confirmed.** `tests/test_option_pricing_core.py:178`, `tests/test_option_pricing_loop_native_bsgp.py:235`, `tests/test_option_pricing_loop_native_bsgp.py:250` | Already correct; later-cycle outcome reconciliation was completed without weakening the no-same-target-price rule. `ml/option_pricing/causal.py:524`, `ml/option_pricing/causal.py:542`, `ml/option_pricing/causal.py:963` |
| `t` | Derived locally from target plus effective definition `expiration_date`; field `target_years_to_expiration` | ACT/365 calendar years | Target is the event boundary; expiry maps date precision to 16:00 America/New_York with DST; calculation occurs at prediction creation/availability | Nonpositive, <7-day, >120-day, or invalid expiry rejects the row | Same deterministic transformation is stored and used for training/live inference | **Confirmed.** `tests/test_option_pricing_core.py:121` | Already correct. `ml/option_pricing/black_scholes.py:187`, `ml/option_pricing/black_scholes.py:201`, `ml/option_pricing/policies.py:63` |
| `d`/`q` | Loop A/FMP point-in-time cash-dividend history; declaration date, ex-date, cash amount, source receipt/`source_available_at`, computed `known_dividend_pv`, `equivalent_dividend_yield` and confidence | Cash USD/share and equivalent continuous annual decimal yield | Receipt must be available by decision; a declaration date must be strictly before that UTC day; only ex-dates in `(decision, expiration]` enter PV | No causally known declaration resolves to zero under `ZERO_NO_KNOWN_DIVIDEND`; invalid known evidence rejects the row | Live uses declarations only; offline research/materialization may retain an explicitly labeled earlier-chain parity comparator. The resulting `dividend_yield` enters both Black–Scholes and the residual design matrix | **Confirmed.** `tests/test_option_pricing_core.py:43`, `tests/test_option_pricing_loop_native_bsgp.py:481` | Corrected: live construction always disables option-price-derived carry, while the offline lane remains versioned. `ml/option_pricing_runtime.py:1191`, `ml/option_pricing/dividends.py:184`, `ml/option_pricing/dividends.py:264`, `ml/option_pricing/causal.py:452` |

## Processing and decisions

1. **Confirmed:** derive the only calendar-eligible quarter-hour target; reject replayed or future targets and targets beyond the 1,200-second causal source window. `ml/option_pricing_runtime.py:251`, `ml/option_pricing_runtime.py:255`, `ml/option_pricing_runtime.py:282`
2. **Confirmed:** reuse an existing immutable target outcome unless it is a legacy retryable `TARGET_BAR_NOT_READY`; otherwise wait for exact all-symbol readiness. Missing readiness remains retryable and publishes nothing. `ml/option_pricing_runtime.py:1062`, `ml/option_pricing_runtime.py:1080`, `ml/option_pricing_runtime.py:1128`
3. **Confirmed:** per symbol, build rows for standard 100-share contracts with 7–120 days to expiry and absolute log-moneyness at most 0.25, using earlier chain/volatility, causal rate/dividend inputs and the Loop A close. `ml/option_pricing/policies.py:61`, `ml/option_pricing_runtime.py:1181`
4. **Confirmed:** the authoritative baseline predictions deliberately pass `models={}`: residual mean is zero and fitted uncertainty/interval fields are null. A verified prior model separately prices `BS + normalized residual × underlying`, publishes that correction explicitly in normalized and dollar fields, calibrates uncertainty, and shape-projects the surface into a keyed sidecar; missing, stale, unsupported, uncalibrated, or failed-model surfaces copy the Black–Scholes point and retain separately projected wider fallback intervals. Every sidecar row keeps `automated_action_allowed=false`. `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/prediction.py:98`, `ml/option_pricing/prediction.py:110`, `ml/option_pricing/prediction.py:172`, `ml/option_pricing/prediction.py:260`, `ml/option_pricing/target_outcome.py:165`
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
- **Confirmed:** Daily ALFRED/current FRED supplies the required point-in-time live rate authority. `ml/option_pricing/causal.py:264`, `ml/option_pricing/rates.py:361`, `datafetching/fred_vintages.py:364`
- **Confirmed:** Options Capture supplies prior chains and later observed quotes/outcomes. `ml/option_pricing/consumers.py:372`, `ml/option_pricing_runtime.py:466`

### Downstream

- **Confirmed:** Options Capture waits for or records this target outcome as a sequencing proof, but can still commit causally clocked OPRA or capture pending Schwab evidence if it is absent. `datafetching/pricing_barrier.py:77`, `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:452`
- **Confirmed:** Directional Loop B consumes compact verified `opx__` surface features. `ml/rolling_materialization.py:663`
- **Confirmed:** Strategy's active catalog reads the target sidecar and verified history, maps ready residual rows to `BSGP` and complete fallback rows to `BLACK_SCHOLES`, then attaches exact leg values before scoring; unavailable coverage retains only separately typed Scenario Coverage. `ml/option_pricing/strategy_shadow.py`, `ml/strategy_selection/runtime.py`

### Timing and control relationships

**Confirmed:** intended order is Loop A +00:20, Pricing +1 minute, B +5, Options +6, Strategy +10. Pricing waits only for Loop A; its separate target outcome gives Options causal barrier credit. The worker and research tail are deliberately off the target-authority critical path. `docs/datafetch-ml/current_start_command:50`, `docs/datafetch-ml/current_start_command:94`, `ml/option_pricing_runtime.py:360`, `ml/option_pricing_loop_native_worker.py:141`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | Verified Pricing generation → compact `opx__` surfaces → Loop B samples/features → directional probabilities. `ml/option_pricing/consumers.py:30`, `ml/rolling_materialization.py:663`, `ml/runtime_pipeline.py:493` |
| Option-pricing predictions | Direct | Loop A target plus earlier OPRA/Schwab chain, rate, dividend and volatility → authoritative constrained Black–Scholes baseline point value plus separately published finite-basis residual/fallback sidecar → target and full Pricing authorities. `ml/option_pricing_runtime.py:1181`, `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/target_outcome.py:93` |
| Options-strategy predictions | Indirect | verified ready BSGP or Black–Scholes contract values → exact candidate leg pricing/eligibility → fitted calibrated profitable-outcome probability, or unavailable coverage → non-probabilistic Scenario Coverage → rank. `ml/option_pricing/strategy_shadow.py`, `ml/strategy_selection/runtime.py` |

**Roll-up classification: Both.** It directly owns option-pricing predictions and has evidenced indirect paths to horizon and strategy predictions.

## Failure and degradation behavior

- `.ducketz-option-pricing-runtime.lock` rejects a second owner and can reclaim
  one dead recorded PID. The child’s separate lock prevents duplicate local
  materialization/training without granting it target-publication authority.
- Missing exact Loop A readiness remains retryable until one monotonic deadline.
  Expiry returns a write-free skipped result and leaves both current Pricing
  pointers unchanged; no empty readiness or completion evidence is created.
- Older missed boundaries outside the recoverable causal window receive an
  explicit target outcome such as `PRICING_TIMED_OUT`. The newest still-causal
  boundary is retried rather than prematurely marked missed.
- Per-symbol source/input failure can produce a verified mixed terminal target
  while successful symbols publish. Receipt/schema/pointer verification failure
  prevents authority from advancing.
- Once the fast target is published, later reconciliation, monitoring, full
  generation, or owned-worker failure cannot retract it. The prior full
  generation pointer remains authoritative when a replacement fails.
- Missing/stale/out-of-support residual models retain the constrained
  Black–Scholes point value and publish an explicit wider sidecar fallback; no
  fitted uncertainty or residual lift is fabricated.


## Accuracy and efficiency relevance

- Exact target readiness, earlier-chain selection, causal rates/dividends and
  post-prediction outcome clocks protect against same-target and lookahead
  leakage.
- The fast baseline is deliberately on the critical path; local OPRA-first
  materialization/model work is a nonblocking one-shot child with zero provider
  requests and a refresh guard.
- Chronological train/calibration/assessment partitions, a closed lockbox,
  interval coverage and Black–Scholes comparisons are the relevant accuracy
  evidence. The model’s presence alone does not prove lift.


## Conflicts, gaps, and uncertainty

- Historical `BSGP` names are compatibility terminology. The production model
  is the bounded 128-component Nyström/Bayesian-ridge residual, while exact GP
  work is SPY research-only.
- The baseline file and one-to-one residual sidecar intentionally have different
  authority roles. Combining them in prose must not imply that the worker can
  rewrite the fast target or authorize actions.
- Current model readiness, OPRA/Schwab population, route support and empirical
  performance are external datastore facts and remain unknown without current
  receipts and reports.


## Evidence index

- `ml/option_pricing_runtime.py:360`
- `ml/option_pricing_runtime.py:418`
- `ml/option_pricing_runtime.py:1062`
- `ml/option_pricing_runtime.py:1116`
- `ml/option_pricing_runtime.py:1220`
- `ml/option_pricing_runtime.py:1305`
- `ml/option_pricing_runtime.py:1563`
- `ml/option_pricing_runtime.py:1680`
- `ml/option_pricing/target_outcome.py:192`
- `ml/option_pricing_loop_native_worker.py:126`
- `tests/test_pricing_options_sequencing.py:129`
- `tests/test_pricing_options_sequencing.py:256`
