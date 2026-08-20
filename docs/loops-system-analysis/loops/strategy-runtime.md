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
- Primary code evidence: **Confirmed.** `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:331`, `ml/strategy_runtime.py:429`, `ml/strategy_publication.py:41`

## Purpose

**Confirmed:** Strategy converts authoritative directional forecasts into ranked, options-aware candidate predictions. It binds the exact verified current Loop B pointer record and its checksums, selects exact causal option/stock evidence, constructs allowed spread candidates, attaches point-in-time contract pricing, reconstructs historical candidate outcomes for chronological model fitting/reuse, estimates the probability of strictly positive net profit and expected return on risk, ranks candidates, and publishes a separate immutable authority. `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:235`, `ml/strategy_publication.py:41`, `ml/strategy_selection/runtime.py:81`

**Confirmed non-ownership:** it does not publish directional forecasts, option chains or option fair-value authority; it consumes them. It publishes predictions/ranks only, and the candidate contract does not authorize automated actions or orders. `ml/strategy_runtime.py:71`, `ml/strategy_publication.py:159`

**Startup/bootstrap boundary:** Strategy is a reader/computation owner and has
no Databento bootstrap path. It requires verified Loop B, Pricing, and option
evidence, preserves each upstream pointer/receipt and causal cutoff, and cannot
promote an empty archive, configured provider, or unverified partition into
strategy authority. Canonical OPRA replay and the content-addressed Strategy
OPRA cache are offline historical-evidence accelerators; neither is eligible as
a prospective live receipt or live Pricing authority.

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Current Loop B authority | Directional Loop B | verified `ml/latest/run.json`, run receipt/manifest, `samples.parquet`, `predictions.parquet` | complete current pointer record with manifest/receipt checksums; symbol/horizon/decision/target/action clocks; `LIVE` calibrated direction probability; Loop B causal cutoff/config/symbol scope | current pointer and receipt must verify; Strategy captures that exact record, reads only configured symbols and actual LIVE rows, and embeds the immutable source in its manifest and receipt | Required | **Confirmed.** `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:235`, `ml/strategy_publication.py:41` |
| Exact option chain history | Options Capture prospective receipts; bounded offline materialization/cache | verified point-in-time prospective OPRA snapshots with verified Schwab snapshots as fallback; canonical historical replay and the receipt/checksum-bound Strategy OPRA cache are explicit offline sources | target/availability; provider/fallback; contract symbol, call/put, strike, expiration, multiplier, bid/ask and available quality fields | recurring live loader selects immutable prospective receipts first with OPRA priority per natural target, bounds them by the Strategy cutoff, and passes `allow_historical_opra_replay=False`; offline outcome/training workflows may use replay/cache when prospective history is absent | Required per live candidate route; replay/cache eligible only for offline history | **Confirmed.** `ml/strategy_selection/chain.py:111`, `ml/strategy_selection/runtime.py:167`, `ml/strategy_selection/opra_cache.py` |
| Stock quote-liquidity history | Loop A | Schwab `quote-liquidity` Parquets | bid/ask, `available_at`, quote quality/policy/schema for stock legs and risk | entry/exit quote must lie within route cutoff; malformed schema/policy/duplicate natural keys fail validation | Conditional on strategy construction/stock legs | **Confirmed.** `ml/strategy_selection/chain.py:151`, `ml/strategy_selection/chain.py:258`, `ml/strategy_selection/chain.py:391` |
| Pricing evidence catalog | Active Pricing | verified target sidecars and current/historical contract predictions/surfaces | per-leg BSGP or Black–Scholes fair value/intervals, availability, generation age/support/shrinkage; candidate edge, conservative edge, coverage, uncertainty; `Active`, `Black-Scholes fallback`, `Delayed`, `Unavailable` | exact symbol/target/contract/call-put/expiry/strike/multiplier and causal clock matching; target sidecar status `BSGP_SHADOW_READY` maps to `BSGP`, complete residual fallback maps to `BLACK_SCHOLES`; live scoring rejects offline replay | Required for fitted model; separate Scenario Coverage otherwise | **Confirmed.** `ml/option_pricing/strategy_shadow.py`, `ml/strategy_selection/runtime.py` |
| Historical candidate outcomes | Derived by this loop from Loop B plus exact entry/exit evidence | append-only `ml/strategy-outcomes/<horizon>/<content-sha256>/` artifacts plus a bounded in-process cache | realized net profit after observed leg BBO cash flows/fees, return on risk, binary `profitable` = strictly positive; direction context at the historical decision | only complete Loop B labels and exact causal entry/exit receipts; optional upper bound excludes lockbox starts; training admits only complete quality-passing Pricing coverage; manifest/receipt/checksums verify reuse | Required to fit/reuse; optional for Scenario Coverage publication | **Confirmed.** `ml/strategy_selection/runtime.py`, `ml/strategy_selection/outcome_store.py` |
| Prior compatible Strategy model | This loop | `ml/strategy-models/<horizon>/market-state-strategy-outcome/<generation>/` | classifier, expected-return regressor, Platt calibrator; exact features, cohort fingerprint, policy/partitions/evaluation | reuse only when the actual train/calibration/assessment fingerprint and configuration match; both classes are mandatory in training and calibration; assessment is never calibration and real lockbox is excluded | Optional | **Confirmed.** `ml/strategy_selection/model.py` |

## Processing and decisions

1. **Confirmed:** read and verify the current Loop B authority; fail if its samples or predictions are absent. Capture its complete immutable current pointer record and causal cutoff for exact manifest/receipt binding. `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:81`, `ml/strategy_runtime.py:235`
2. **Confirmed:** load exact option-chain histories and the Pricing catalog only through the Strategy run clock. Validate Loop B inputs and assert forbidden lockbox starts are excluded. `ml/strategy_selection/runtime.py:83`, `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:109`
3. **Confirmed:** for each horizon, reconstruct candidate outcomes from the earliest causal provider-neutral entry BBO and eligible exit BBO, attach Pricing before scoring, keep the target `profitable` binary, and persist the result under its immutable evidence hash. Prospective receipts are preferred. Receipt/checksum-verified canonical replay or its Strategy OPRA cache may fill offline historical evidence, but the recurring live entry loader and live Pricing attachment both forbid offline replay. `ml/strategy_selection/runtime.py:167`, `ml/strategy_selection/chain.py:111`, `ml/strategy_selection/runtime.py:425`, `ml/strategy_selection/outcome_store.py`
4. **Confirmed:** partition by decision/target clusters into at least 252 training, 63 calibration and 63 assessment decisions, purging boundary overlap. Train/reuse a histogram-gradient classifier for profitability and regressor for return residual; fit Platt on calibration only. A one-class calibration partition is explicitly unavailable and cannot become an identity-calibrated score. `ml/strategy_selection/contracts.py`, `ml/strategy_selection/model.py`
5. **Confirmed:** for each canonical LIVE forecast, select one exact entry chain before target start, construct policy-allowed spread candidates, attach Loop B context and exact Pricing evidence, infer market state using calibrated `probability_up`, and compute a non-probabilistic local scenario-grid coverage. `ml/strategy_selection/runtime.py`, `ml/strategy_selection/market_state.py`
6. **Confirmed:** when a fitted calibrated model and eligible active Pricing exist, output calibrated profitable-outcome probability and bounded expected return/profit. Ineligible/no-model rows keep all probability fields null and retain only `scenario_coverage_score`; mixing is reranked deterministically by authority tier. `ml/strategy_selection/runtime.py`, `ml/strategy_selection/model.py`
7. **Confirmed:** validate probability bounds, score-basis/pricing-source coherence, one decision per route, unique candidate keys and complete ranks; then write candidate/audit/report/model files, exact source lineage, receipt and atomic current pointer. `ml/strategy_runtime.py:146`, `ml/strategy_runtime.py:163`, `ml/strategy_runtime.py:221`, `ml/strategy_runtime.py:256`, `ml/strategy_publication.py:41`
8. **Confirmed:** the supervisor skips work only when the Strategy source record exactly equals the current Loop B pointer and the pricing mode plus both prospective OPRA and Schwab per-symbol snapshot heads are unchanged. Historical outcomes are content-addressed, receipt-verified and reusable across restarts; the in-process LRU is only a faster front cache. Live cycles disable historical OPRA replay. `ml/strategy_runtime.py:429`, `ml/strategy_selection/runtime.py:167`, `ml/strategy_selection/outcome_store.py`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Strategy candidate predictions/ranks | UI/read-only consumers | `ml/strategy-runs/<generation>/strategy-candidates.parquet` | strategy/legs/risk/capital/Greeks/liquidity; direction probability; fitted raw/calibrated probability or null; separate Scenario Coverage; expected net profit/return; Pricing status/source/reason/quality; score basis and rank | fitted score equals calibrated probability, requires full exact-leg quality-passing Pricing and matches its source; heuristic rows have null probability fields; one decision per route and unique ranks | **Confirmed.** `ml/parquet_contracts.py`, `ml/strategy_runtime.py` |
| Construction audit | UI/research | `strategy-audit.parquet` | strategy family/name, approval/authorization/construction status, candidate count and reason | schema-bound and receipt-covered; failed routes are explicit rather than silently absent | **Confirmed.** `ml/parquet_contracts.py:327`, `ml/strategy_runtime.py:153` |
| Model/evidence reports and copied artifacts | later Strategy runs/research | `strategy-model-reports.json` and `model-artifacts/` | trained/reused counts, chronological offline evaluation, Pricing coverage/status, source generation mapping and candidate contract | copies only model artifacts referenced by this run; manifest checksums bind outputs/inputs | **Confirmed.** `ml/strategy_runtime.py:176`, `ml/strategy_runtime.py:181`, `ml/strategy_runtime.py:217` |
| Strategy publication authority | UI/readers | `ml/strategy-latest/run.json` plus run `publication.json` | current immutable path/timestamps, manifest/receipt checksums, exact source Loop B record and candidate contract | receipt validates manifest and exact source Loop B record; atomic pointer must match receipt and contract exactly | **Confirmed.** `ml/strategy_publication.py:37`, `ml/strategy_publication.py:41`, `ml/strategy_publication.py:86` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop B is mandatory and supplies the authoritative route/sample/probability context. `ml/strategy_runtime.py:71`, `ml/strategy_runtime.py:125`
- **Confirmed:** Options Capture prospective receipts are live authority, with OPRA priority and labeled Schwab fallback per natural target. Canonical historical OPRA and the Strategy cache supply point-in-time definitions/BBO only to eligible offline history/outcome workflows; recurring live entry and live Pricing scoring explicitly disable that evidence. `ml/strategy_selection/chain.py:111`, `ml/strategy_selection/runtime.py:167`
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
- A verified but non-current Loop B source is not silently treated as the same
  authority. The monitor compares checksums and current-record identity, while
  the next Strategy cycle's unchanged-work test requires exact pointer equality.


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

## Historical OPRA and model-maturity evidence

**Confirmed causal boundary:** canonical OPRA replay publishes immutable
samples, predictions, evaluations, manifest, receipt, and checksums. Legacy
emulated creation clocks at or before their target are repaired to a causal
post-target creation clock and recorded as requested clock corrections. The
Strategy OPRA cache is content-addressed from verified source files and binds
its surfaces/contracts and receipt checksums. These products accelerate offline
outcome construction; prospective receipts remain preferred, and live scoring
cannot substitute either product for real-time entry or Pricing evidence.
`ml/option_pricing_opra_replay.py:224`,
`ml/option_pricing_opra_replay.py:382`,
`ml/strategy_selection/runtime.py:167`

**Historical observation at 2026-08-19 11:15 UTC:** the then-current canonical replay described 68
targets and 394,296 complete evaluation rows, recorded four corrected target
clocks, and reported zero replay materialization errors. Its published output
checksums and receipt-to-manifest checksum verified. The current Strategy OPRA
cache contained approximately 270,035 contracts and 48 surfaces from 860
source files; its output and receipt checksums also verified. The source archive
can advance after a replay, so these measurements do not claim that a prior
replay fingerprint equals the forever-current archive.

**Observed model maturity at 2026-08-19 22:40 UTC:** immutable Strategy run
`ml/strategy-runs/20260819T224000.073641Z` passed manifest, publication, and
source-lineage checksum verification. It contained 4,800 candidates and 1,440
audit rows. All candidates were `SCENARIO_COVERAGE_HEURISTIC`; all nine route
reports were `MODEL_NOT_FIT`; and zero models were trained or reused. This is
insufficient causal/model/Pricing maturity, not measured poor calibrated
performance. Calibrated Probability correctly remained null; Scenario Coverage
was not copied into any probability field.

## Runtime and monitoring observation

**Observed 2026-08-19 22:45:36 UTC:** one Strategy launcher/worker pair, its
worker-owned singleton lock, primary logs, immutable publication, and Options
Strategy UI contract passed. Run `ml/strategy-runs/20260819T224000.073641Z`
was bound to exact current Loop B run `ml/runs/20260819T223552.337574Z`, including
matching source manifest and receipt checksums. The earlier lineage warning was
fully resolved. At 22:59:29 UTC the read-only monitor verified newer Strategy
run `ml/strategy-runs/20260819T225500.058671Z` bound to newer current Loop B run
`ml/runs/20260819T225107.106040Z`; exact lineage remained healthy. Run paths are
timestamped evidence, not fixed authority names.


## Evidence index

- `ml/strategy_runtime.py:74`
- `ml/strategy_runtime.py:120`
- `ml/strategy_runtime.py:203`
- `ml/strategy_runtime.py:217`
- `ml/strategy_runtime.py:283`
- `ml/strategy_runtime.py:358`
- `ml/strategy_runtime.py:361`
- `ml/strategy_runtime.py:414`
- `ml/strategy_publication.py:41`
- `ml/strategy_selection/runtime.py:240`
- `options/publication.py:75`
- `tests/test_ml_runtime_pipeline.py:454`
- `tests/test_ml_runtime_pipeline.py:606`
