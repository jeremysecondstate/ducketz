# Strategy runtime

## Identity

- Canonical name: Strategy runtime
- Logical aliases or numbering: Strategy; startup owner 7
- Runtime entry point: `python -m ml.strategy_runtime`
- Owning package: `ml.strategy_selection`
- Classification: Independent production loop
- Scheduling mechanism: recurring supervisor that reacts to the current verified Loop B, pricing mode, and per-symbol OPRA and Schwab option-snapshot heads
- Cadence and phase: every 15 minutes at UTC phase +10 minutes
- Lock or single-writer mechanism: `.ducketz-strategy-runtime.lock`
- Primary code evidence: **Confirmed.** `ml/strategy_runtime.py:316`, `ml/strategy_runtime.py:327`, `ml/strategy_runtime.py:358`, `ml/strategy_runtime.py:361`

## Purpose

**Confirmed:** Strategy converts authoritative directional forecasts into ranked, options-aware candidate predictions. It binds one verified Loop B run, selects exact causal option/stock evidence, constructs allowed spread candidates, attaches point-in-time contract pricing, reconstructs historical candidate outcomes for chronological model fitting/reuse, estimates the probability of strictly positive net profit and expected return on risk, ranks candidates, and publishes a separate immutable authority. `ml/strategy_runtime.py:63`, `ml/strategy_selection/runtime.py:139`, `ml/strategy_selection/runtime.py:267`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/model.py:424`

**Confirmed non-ownership:** it does not publish directional forecasts, option chains or option fair-value authority; it consumes them. It publishes predictions/ranks only, and the candidate contract does not authorize automated actions or orders. `ml/strategy_runtime.py:71`, `ml/strategy_publication.py:159`

**Startup/bootstrap boundary:** Strategy is a reader/computation owner and has
no Databento bootstrap path. It requires verified Loop B, Pricing, and option
evidence, preserves each upstream pointer/receipt and causal cutoff, and cannot
promote an empty archive, configured provider, or unverified partition into
strategy authority.

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Current Loop B authority | Directional Loop B | verified `ml/latest/run.json`, run receipt/manifest, `samples.parquet`, `predictions.parquet` | symbol/horizon/decision/target/action clocks; registered context features; `LIVE` calibrated direction probability; Loop B causal cutoff/config/symbol scope | current pointer and receipt must verify; Strategy reads only configured symbols and canonical LIVE rows; source generation is immutable in Strategy receipt | Required | **Confirmed.** `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:81`, `ml/strategy_runtime.py:91`, `ml/strategy_selection/runtime.py:222` |
| Exact option chain history | Options Capture prospective receipts; bounded offline materialization | verified point-in-time OPRA snapshots with verified Schwab snapshots as fallback; historical OPRA is an explicit offline-only source | target/availability; provider/fallback; contract symbol, call/put, strike, expiration, multiplier, bid/ask and available quality fields | live loader selects immutable prospective receipts first with OPRA priority per target, bounds all evidence by the Strategy cutoff, and disables historical OPRA replay; offline workflows may opt into replay | Required per candidate route | **Confirmed.** `ml/strategy_selection/chain.py`, `ml/strategy_selection/runtime.py` |
| Stock quote-liquidity history | Loop A | Schwab `quote-liquidity` Parquets | bid/ask, `available_at`, quote quality/policy/schema for stock legs and risk | entry/exit quote must lie within route cutoff; malformed schema/policy/duplicate natural keys fail validation | Conditional on strategy construction/stock legs | **Confirmed.** `ml/strategy_selection/chain.py:151`, `ml/strategy_selection/chain.py:258`, `ml/strategy_selection/chain.py:391` |
| Pricing evidence catalog | Active Pricing | verified target sidecars and current/historical contract predictions/surfaces | per-leg BSGP or Black–Scholes fair value/intervals, availability, generation age/support/shrinkage; candidate edge, conservative edge, coverage, uncertainty; `Active`, `Black-Scholes fallback`, `Delayed`, `Unavailable` | exact symbol/target/contract/call-put/expiry/strike/multiplier and causal clock matching; target sidecar status `BSGP_SHADOW_READY` maps to `BSGP`, complete residual fallback maps to `BLACK_SCHOLES`; live scoring rejects offline replay | Required for fitted model; separate Scenario Coverage otherwise | **Confirmed.** `ml/option_pricing/strategy_shadow.py`, `ml/strategy_selection/runtime.py` |
| Historical candidate outcomes | Derived by this loop from Loop B plus exact entry/exit evidence | append-only `ml/strategy-outcomes/<horizon>/<content-sha256>/` artifacts plus a bounded in-process cache | realized net profit after observed leg BBO cash flows/fees, return on risk, binary `profitable` = strictly positive; direction context at the historical decision | only complete Loop B labels and exact causal entry/exit receipts; optional upper bound excludes lockbox starts; training admits only complete quality-passing Pricing coverage; manifest/receipt/checksums verify reuse | Required to fit/reuse; optional for Scenario Coverage publication | **Confirmed.** `ml/strategy_selection/runtime.py`, `ml/strategy_selection/outcome_store.py` |
| Prior compatible Strategy model | This loop | `ml/strategy-models/<horizon>/market-state-strategy-outcome/<generation>/` | classifier, expected-return regressor, Platt calibrator; exact features, cohort fingerprint, policy/partitions/evaluation | reuse only when the actual train/calibration/assessment fingerprint and configuration match; both classes are mandatory in training and calibration; assessment is never calibration and real lockbox is excluded | Optional | **Confirmed.** `ml/strategy_selection/model.py` |

## Processing and decisions

1. **Confirmed:** read and verify the current Loop B authority; fail if its samples or predictions are absent. Capture its immutable pointer record and causal cutoff. `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:83`, `ml/strategy_runtime.py:91`
2. **Confirmed:** load exact option-chain histories and the Pricing catalog only through the Strategy run clock. Validate Loop B inputs and assert forbidden lockbox starts are excluded. `ml/strategy_selection/runtime.py:83`, `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:109`
3. **Confirmed:** for each horizon, reconstruct candidate outcomes from the earliest causal provider-neutral entry BBO and eligible exit BBO, attach Pricing before scoring, keep the target `profitable` binary, and persist the result under its immutable evidence hash. Historical OPRA replay remains opt-in for offline materialization and is disabled in the recurring live selection path. `ml/strategy_selection/runtime.py`, `ml/strategy_selection/chain.py`, `ml/strategy_selection/outcome_store.py`
4. **Confirmed:** partition by decision/target clusters into at least 252 training, 63 calibration and 63 assessment decisions, purging boundary overlap. Train/reuse a histogram-gradient classifier for profitability and regressor for return residual; fit Platt on calibration only. A one-class calibration partition is explicitly unavailable and cannot become an identity-calibrated score. `ml/strategy_selection/contracts.py`, `ml/strategy_selection/model.py`
5. **Confirmed:** for each canonical LIVE forecast, select one exact entry chain before target start, construct policy-allowed spread candidates, attach Loop B context and exact Pricing evidence, infer market state using calibrated `probability_up`, and compute a non-probabilistic local scenario-grid coverage. `ml/strategy_selection/runtime.py`, `ml/strategy_selection/market_state.py`
6. **Confirmed:** when a fitted calibrated model and eligible active Pricing exist, output calibrated profitable-outcome probability and bounded expected return/profit. Ineligible/no-model rows keep all probability fields null and retain only `scenario_coverage_score`; mixing is reranked deterministically by authority tier. `ml/strategy_selection/runtime.py`, `ml/strategy_selection/model.py`
7. **Confirmed:** validate probability bounds, score-basis/pricing-source coherence, one decision per route, unique candidate keys and complete ranks; then write candidate/audit/report/model files, manifest lineage, receipt and atomic current pointer. `ml/strategy_runtime.py:478`, `ml/strategy_runtime.py:527`, `ml/strategy_runtime.py:598`, `ml/strategy_runtime.py:217`, `ml/strategy_runtime.py:253`
8. **Confirmed:** the supervisor skips work only when the Loop B pointer, pricing mode, and both prospective OPRA and Schwab per-symbol snapshot heads are unchanged. Historical outcomes are content-addressed, receipt-verified and reusable across restarts; the in-process LRU is only a faster front cache. Live cycles disable the multi-billion-row historical OPRA replay. `ml/strategy_runtime.py`, `options/publication.py`, `ml/strategy_selection/runtime.py`, `ml/strategy_selection/outcome_store.py`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Strategy candidate predictions/ranks | UI/read-only consumers | `ml/strategy-runs/<generation>/strategy-candidates.parquet` | strategy/legs/risk/capital/Greeks/liquidity; direction probability; fitted raw/calibrated probability or null; separate Scenario Coverage; expected net profit/return; Pricing status/source/reason/quality; score basis and rank | fitted score equals calibrated probability, requires full exact-leg quality-passing Pricing and matches its source; heuristic rows have null probability fields; one decision per route and unique ranks | **Confirmed.** `ml/parquet_contracts.py`, `ml/strategy_runtime.py` |
| Construction audit | UI/research | `strategy-audit.parquet` | strategy family/name, approval/authorization/construction status, candidate count and reason | schema-bound and receipt-covered; failed routes are explicit rather than silently absent | **Confirmed.** `ml/parquet_contracts.py:327`, `ml/strategy_runtime.py:153` |
| Model/evidence reports and copied artifacts | later Strategy runs/research | `strategy-model-reports.json` and `model-artifacts/` | trained/reused counts, chronological offline evaluation, Pricing coverage/status, source generation mapping and candidate contract | copies only model artifacts referenced by this run; manifest checksums bind outputs/inputs | **Confirmed.** `ml/strategy_runtime.py:176`, `ml/strategy_runtime.py:181`, `ml/strategy_runtime.py:217` |
| Strategy publication authority | UI/readers | `ml/strategy-latest/run.json` plus run `publication.json` | current immutable path/timestamps, manifest/receipt checksums, exact source Loop B record and candidate contract | receipt validates manifest and source Loop B; atomic pointer must match receipt and contract exactly | **Confirmed.** `ml/strategy_publication.py:36`, `ml/strategy_publication.py:40`, `ml/strategy_publication.py:118`, `ml/strategy_publication.py:152` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop B is mandatory and supplies the authoritative route/sample/probability context. `ml/strategy_runtime.py:71`, `ml/strategy_runtime.py:125`
- **Confirmed:** canonical historical OPRA supplies point-in-time definitions/BBO first; Options Capture supplies prospective snapshots and Schwab fallback history. `ml/strategy_selection/chain.py`, `ml/strategy_selection/runtime.py`
- **Confirmed:** Active Pricing supplies the implemented Black–Scholes-plus-residual sidecar and verified historical catalog before fitted probability scoring. Ready residual rows are labeled `BSGP`, baseline/fallback rows `BLACK_SCHOLES`, and absent/ineligible coverage retains only non-probabilistic Scenario Coverage. `ml/option_pricing/strategy_shadow.py`, `ml/strategy_selection/runtime.py`
- **Confirmed:** Loop A stock quote-liquidity is a shared-artifact data input, without a direct readiness barrier. `ml/strategy_selection/chain.py:151`, `ml/strategy_runtime.py:209`

### Downstream

**Confirmed:** no other production loop consumes Strategy outputs. The located consumers are read-only UI/data access. `app/ui/options_strategy_data.py:628`

### Timing and control relationships

**Confirmed:** Strategy’s +10 phase follows Loop B +5 and Options +6, but it does not wait on a named Options barrier; it reads the current verified Loop B and evidence available by its own creation clock. Its unchanged-work test uses the Loop B pointer, pricing mode, and both prospective provider heads. `ml/strategy_runtime.py`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | None | Strategy consumes directional probabilities after Loop B publication and has no write/control path back into Loop B. |
| Option-pricing predictions | None | Strategy consumes verified fair values/surfaces; its candidate score does not alter Pricing authority. |
| Options-strategy predictions | Direct | LIVE direction + exact chain/stock evidence + contract pricing + observed historical outcome model → calibrated profitable-outcome probability when available; otherwise separately typed Scenario Coverage → rank and Strategy authority. `ml/strategy_selection/runtime.py`, `ml/strategy_selection/model.py`, `ml/strategy_publication.py` |

**Roll-up classification: Options.** It directly owns options-strategy predictions and contributes to neither upstream prediction family.

## Failure and degradation behavior

- `.ducketz-strategy-runtime.lock` rejects a second owner and may reclaim one
  dead recorded PID. A cycle failure is caught by the supervisor; no incomplete
  run is promoted and the prior Strategy pointer remains current.
- Missing/corrupt Loop B authority or absent source samples/predictions fails the
  cycle. Missing entry/exit chain or stock evidence is route-audited and skips
  affected constructions/outcomes rather than synthesizing them.
- Missing full active Pricing coverage or a compatible fitted model keeps an
  explicit `SCENARIO_COVERAGE_HEURISTIC` value. It leaves raw, calibrated, and
  decision probabilities null and the UI marks the row research-only.
- Receipt, schema, rank/coherence, or source-lineage validation failure prevents
  `ml/strategy-latest/run.json` from advancing.


## Accuracy and efficiency relevance

- Exact causal entry/exit receipts, point-in-time definitions, Loop B target
  clocks and pre-score Pricing attachment prevent future-chain and pricing
  leakage.
- Decision-cluster chronological partitions keep training, calibration and
  assessment separate; real lockbox targets are excluded from fitting and
  runtime evaluation.
- Content-addressed outcome reuse, exact cohort fingerprints, compatible model
  reuse and the in-process LRU reduce repeat work without re-reading the full
  historical OPRA corpus every live cycle. Their existence does not prove
  profitability or calibration quality; use current model/evaluation reports.


## Conflicts, gaps, and uncertainty

- The supervisor wake key covers prospective OPRA and Schwab heads. Historical
  partition-only updates remain outside that latency-sensitive key and are read
  on the next Loop B, pricing-mode, or prospective-receipt change.
- The manifest checksums every `selection.source_files` input, and the summarized
  `option_snapshot_receipts` lineage covers both prospective provider trees.
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
