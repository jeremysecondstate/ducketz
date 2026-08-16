# Strategy runtime

## Identity

- Canonical name: Strategy runtime
- Logical aliases or numbering: Strategy; startup owner 7
- Runtime entry point: `python -m ml.strategy_runtime`
- Owning package: `ml.strategy_selection`
- Classification: Independent production loop
- Scheduling mechanism: recurring supervisor that reacts to the current verified Loop B, pricing mode, and per-symbol Schwab option-snapshot heads
- Cadence and phase: every 15 minutes at UTC phase +10 minutes
- Lock or single-writer mechanism: `.ducketz-strategy-runtime.lock`
- Primary code evidence: **Confirmed.** `ml/strategy_runtime.py:316`, `ml/strategy_runtime.py:327`, `ml/strategy_runtime.py:358`, `ml/strategy_runtime.py:361`

## Purpose

**Confirmed:** Strategy converts authoritative directional forecasts into ranked, options-aware candidate predictions. It binds one verified Loop B run, selects exact causal option/stock evidence, constructs allowed spread candidates, attaches point-in-time contract pricing, reconstructs historical candidate outcomes for chronological model fitting/reuse, estimates the probability of strictly positive net profit and expected return on risk, ranks candidates, and publishes a separate immutable authority. `ml/strategy_runtime.py:63`, `ml/strategy_selection/runtime.py:139`, `ml/strategy_selection/runtime.py:267`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/model.py:424`

**Confirmed non-ownership:** it does not publish directional forecasts, option chains or option fair-value authority; it consumes them. It publishes predictions/ranks only, and the candidate contract does not authorize automated actions or orders. `ml/strategy_runtime.py:71`, `ml/strategy_publication.py:159`

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Current Loop B authority | Directional Loop B | verified `ml/latest/run.json`, run receipt/manifest, `samples.parquet`, `predictions.parquet` | symbol/horizon/decision/target/action clocks; registered context features; `LIVE` calibrated direction probability; Loop B causal cutoff/config/symbol scope | current pointer and receipt must verify; Strategy reads only configured symbols and canonical LIVE rows; source generation is immutable in Strategy receipt | Required | **Confirmed.** `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:81`, `ml/strategy_runtime.py:91`, `ml/strategy_selection/runtime.py:222` |
| Exact option chain history | Historical OPRA storage and Options Capture | verified point-in-time OPRA definitions plus `cbbo-1m`/`cbbo-1s`, with verified Schwab snapshots as fallback | target/availability; provider/fallback; contract symbol, call/put, strike, expiration, multiplier, bid/ask and available quality fields | loader tries eligible OPRA first; definitions are joined backward as of each quote; all evidence is bounded by the Strategy cutoff; Schwab is used only when no usable OPRA chain exists | Required per candidate route | **Confirmed.** `ml/strategy_selection/chain.py`, `ml/strategy_selection/runtime.py` |
| Stock quote-liquidity history | Loop A | Schwab `quote-liquidity` Parquets | bid/ask, `available_at`, quote quality/policy/schema for stock legs and risk | entry/exit quote must lie within route cutoff; malformed schema/policy/duplicate natural keys fail validation | Conditional on strategy construction/stock legs | **Confirmed.** `ml/strategy_selection/chain.py:151`, `ml/strategy_selection/chain.py:258`, `ml/strategy_selection/chain.py:391` |
| Pricing evidence catalog | Active Pricing | verified target sidecars and current/historical contract predictions/surfaces | per-leg BSGP or Black–Scholes fair value/intervals, availability, generation age/support/shrinkage; candidate edge, conservative edge, coverage, uncertainty, favorable probability; `Active`, `Black-Scholes fallback`, `Delayed`, `Unavailable` | target sidecar status `BSGP_SHADOW_READY` maps to `pricing_source=BSGP`; complete residual fallback maps to `BLACK_SCHOLES`; loaded only through Strategy creation time and attached before scoring; live active mode rejects offline replay | Required for fitted model in active mode; explicit scenario fallback otherwise | **Confirmed.** `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_runtime.py:552` |
| Historical candidate outcomes | Derived by this loop from Loop B plus exact entry/exit evidence | in-memory materialization from immutable source files | realized net profit after leg cash flows/fees, return on risk, binary `profitable` = strictly positive; direction probability/context at the historical decision | only complete Loop B labels and exact causal entry/exit receipts; optional upper bound excludes lockbox starts; active-mode training admits only complete Pricing coverage | Required to fit/reuse; optional for scenario-prior publication | **Confirmed.** `ml/strategy_selection/runtime.py:126`, `ml/strategy_selection/runtime.py:351`, `ml/strategy_selection/runtime.py:479`, `ml/strategy_selection/runtime.py:149` |
| Prior compatible Strategy model | This loop | `ml/strategy-models/<horizon>/market-state-strategy-outcome/<generation>/` | classifier, expected-return regressor, calibrator; exact numeric/categorical features, source inventory/policy/partitions/evaluation | reuse only on exact expected configuration/input compatibility; otherwise refit; assessment is never calibration and real lockbox is not used | Optional | **Confirmed.** `ml/strategy_selection/model.py:276`, `ml/strategy_selection/model.py:300`, `ml/strategy_selection/model.py:316`, `ml/strategy_selection/model.py:372` |

## Processing and decisions

1. **Confirmed:** read and verify the current Loop B authority; fail if its samples or predictions are absent. Capture its immutable pointer record and causal cutoff. `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:83`, `ml/strategy_runtime.py:91`
2. **Confirmed:** load exact option-chain histories and the Pricing catalog only through the Strategy run clock. Validate Loop B inputs and assert forbidden lockbox starts are excluded. `ml/strategy_selection/runtime.py:83`, `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:109`
3. **Confirmed:** for each horizon, reconstruct candidate outcomes from the earliest causal OPRA-first provider-neutral entry BBO and eligible exit BBO, attach Pricing before scoring, and keep the target `profitable` binary. Historical OPRA replay may support historical outcomes, but it cannot be attached as prospective live pricing; live active scoring still requires eligible point-in-time evidence. `ml/strategy_selection/runtime.py`, `ml/strategy_selection/chain.py`, `ml/option_pricing/strategy_shadow.py`
4. **Confirmed:** partition by decision/target clusters into at least 252 training, 63 calibration and 63 assessment decisions, purging boundary overlap. Train/reuse a histogram-gradient classifier for profitability and regressor for return residual; fit Platt on calibration only, or identity if it has one class. `ml/strategy_selection/contracts.py:102`, `ml/strategy_selection/model.py:210`, `ml/strategy_selection/model.py:330`, `ml/strategy_selection/model.py:360`
5. **Confirmed:** for each canonical LIVE forecast, select one exact entry chain before target start, construct policy-allowed spread candidates, attach Loop B context and active Pricing evidence, infer market state using calibrated `probability_up`, and form a scenario prior. `ml/strategy_selection/runtime.py:225`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:267`, `ml/strategy_selection/runtime.py:296`
6. **Confirmed:** when a model and eligible active Pricing exist, output calibrated profitable-outcome probability and bounded expected return/profit. Ineligible/no-model rows retain an explicit Pricing-scenario score basis; mixing is reranked deterministically. `ml/strategy_selection/runtime.py:306`, `ml/strategy_selection/runtime.py:311`, `ml/strategy_selection/model.py:442`, `ml/strategy_selection/model.py:466`
7. **Confirmed:** validate probability bounds, score-basis/pricing-source coherence, one decision per route, unique candidate keys and complete ranks; then write candidate/audit/report/model files, manifest lineage, receipt and atomic current pointer. `ml/strategy_runtime.py:478`, `ml/strategy_runtime.py:527`, `ml/strategy_runtime.py:598`, `ml/strategy_runtime.py:217`, `ml/strategy_runtime.py:253`
8. **Confirmed:** the supervisor skips work only when the Loop B pointer, pricing mode and per-symbol Schwab option-snapshot heads are unchanged. The helper calls the snapshot-pointer path with its default `provider="schwab"`; OPRA historical-partition changes alone are not a wake key. Historical outcome caching is an owned, process-memory LRU keyed by immutable evidence, not another loop. `ml/strategy_runtime.py:283`, `ml/strategy_runtime.py:414`, `options/publication.py:75`, `ml/strategy_selection/runtime.py:520`, `ml/strategy_selection/runtime.py:607`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Strategy candidate predictions/ranks | UI/read-only consumers | `ml/strategy-runs/<generation>/strategy-candidates.parquet` | strategy/legs/risk/capital/Greeks/liquidity; direction probability; raw/calibrated profitable-outcome probability; expected net profit/return on risk; Pricing status/edge/uncertainty; score basis; consecutive rank | finite probabilities in [0,1]; fitted score equals calibrated probability and matches Pricing source; scenario score equals raw prior; one decision per route and unique ranks | **Confirmed.** `ml/parquet_contracts.py:234`, `ml/strategy_runtime.py:463`, `ml/strategy_runtime.py:511`, `ml/strategy_runtime.py:598` |
| Construction audit | UI/research | `strategy-audit.parquet` | strategy family/name, approval/authorization/construction status, candidate count and reason | schema-bound and receipt-covered; failed routes are explicit rather than silently absent | **Confirmed.** `ml/parquet_contracts.py:327`, `ml/strategy_runtime.py:153` |
| Model/evidence reports and copied artifacts | later Strategy runs/research | `strategy-model-reports.json` and `model-artifacts/` | trained/reused counts, chronological offline evaluation, Pricing coverage/status, source generation mapping and candidate contract | copies only model artifacts referenced by this run; manifest checksums bind outputs/inputs | **Confirmed.** `ml/strategy_runtime.py:176`, `ml/strategy_runtime.py:181`, `ml/strategy_runtime.py:217` |
| Strategy publication authority | UI/readers | `ml/strategy-latest/run.json` plus run `publication.json` | current immutable path/timestamps, manifest/receipt checksums, exact source Loop B record and candidate contract | receipt validates manifest and source Loop B; atomic pointer must match receipt and contract exactly | **Confirmed.** `ml/strategy_publication.py:36`, `ml/strategy_publication.py:40`, `ml/strategy_publication.py:118`, `ml/strategy_publication.py:152` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop B is mandatory and supplies the authoritative route/sample/probability context. `ml/strategy_runtime.py:71`, `ml/strategy_runtime.py:125`
- **Confirmed:** canonical historical OPRA supplies point-in-time definitions/BBO first; Options Capture supplies prospective snapshots and Schwab fallback history. `ml/strategy_selection/chain.py`, `ml/strategy_selection/runtime.py`
- **Confirmed:** Active Pricing supplies the implemented Black–Scholes-plus-residual sidecar and verified historical catalog before fitted probability scoring. Ready residual rows are labeled `BSGP`, baseline/fallback rows `BLACK_SCHOLES`, and absent/ineligible coverage retains an explicit scenario fallback. `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:288`
- **Confirmed:** Loop A stock quote-liquidity is a shared-artifact data input, without a direct readiness barrier. `ml/strategy_selection/chain.py:151`, `ml/strategy_runtime.py:209`

### Downstream

**Confirmed:** no other production loop consumes Strategy outputs. The located consumers are read-only UI/data access. `app/ui/options_strategy_data.py:628`

### Timing and control relationships

**Confirmed:** Strategy’s +10 phase follows Loop B +5 and Options +6, but it does not wait on a named Options barrier; it reads the current verified Loop B and evidence available by its own creation clock. Its unchanged-work test uses the Loop B pointer, pricing mode, and Schwab snapshot heads. `ml/strategy_runtime.py:120`, `ml/strategy_runtime.py:283`, `ml/strategy_runtime.py:414`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | None | Strategy consumes directional probabilities after Loop B publication and has no write/control path back into Loop B. |
| Option-pricing predictions | None | Strategy consumes verified fair values/surfaces; its candidate score does not alter Pricing authority. |
| Options-strategy predictions | Direct | LIVE direction + exact chain/stock evidence + contract pricing + historical outcome model/scenario prior → profitable-outcome probability, expected return/profit and rank → Strategy authority. `ml/strategy_selection/runtime.py:225`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/model.py:442`, `ml/strategy_publication.py:40` |

**Roll-up classification: Options.** It directly owns options-strategy predictions and contributes to neither upstream prediction family.

## Failure and degradation behavior

- `.ducketz-strategy-runtime.lock` rejects a second owner and may reclaim one
  dead recorded PID. A cycle failure is caught by the supervisor; no incomplete
  run is promoted and the prior Strategy pointer remains current.
- Missing/corrupt Loop B authority or absent source samples/predictions fails the
  cycle. Missing entry/exit chain or stock evidence is route-audited and skips
  affected constructions/outcomes rather than synthesizing them.
- Missing full active Pricing coverage or a compatible fitted model keeps an
  explicit `PRICING_SCENARIO_FALLBACK` score. It never fills calibrated fitted
  probabilities with the fallback prior.
- Receipt, schema, rank/coherence, or source-lineage validation failure prevents
  `ml/strategy-latest/run.json` from advancing.


## Accuracy and efficiency relevance

- Exact causal entry/exit receipts, point-in-time definitions, Loop B target
  clocks and pre-score Pricing attachment prevent future-chain and pricing
  leakage.
- Decision-cluster chronological partitions keep training, calibration and
  assessment separate; real lockbox targets are excluded from fitting and
  runtime evaluation.
- Compatible model reuse and the in-process immutable-evidence LRU reduce repeat
  work. Their existence does not prove profitability or calibration quality;
  use current model/evaluation reports.


## Conflicts, gaps, and uncertainty

- Strategy selection is OPRA-first, but the supervisor’s unchanged-work
  fingerprint observes only the default Schwab snapshot heads. A pure OPRA
  historical update will be reread only after Loop B, pricing mode, or a Schwab
  head changes. This is documented as current behavior, not silently broadened
  in this documentation audit.
- The manifest checksums every `selection.source_files` input, but the separately
  summarized `option_snapshot_receipts` lineage field filters specifically for
  `options/snapshots/schwab`. The full manifest remains the broader input
  authority.
- No downstream production loop consumes Strategy output; the located consumer
  is the read-only Options Strategy UI. Current candidate coverage and empirical
  performance remain datastore facts.


## Evidence index

- `ml/strategy_runtime.py:74`
- `ml/strategy_runtime.py:120`
- `ml/strategy_runtime.py:203`
- `ml/strategy_runtime.py:217`
- `ml/strategy_runtime.py:283`
- `ml/strategy_runtime.py:358`
- `ml/strategy_runtime.py:361`
- `ml/strategy_runtime.py:414`
- `ml/strategy_publication.py:40`
- `ml/strategy_selection/runtime.py:240`
- `options/publication.py:75`
- `tests/test_ml_runtime_pipeline.py:454`
- `tests/test_ml_runtime_pipeline.py:606`
