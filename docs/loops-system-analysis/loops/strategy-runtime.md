# Strategy runtime

## Identity

- Canonical name: Strategy runtime
- Logical aliases or numbering: Strategy; startup owner 7; “Loop 6” appears only in the obsolete SVG
- Runtime entry point: `python -m ml.strategy_runtime`
- Owning package: `ml.strategy_selection`
- Classification: Independent production loop
- Scheduling mechanism: recurring supervisor that reacts to the current verified Loop B and option-snapshot heads
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
| Exact option chain history | Options Capture | verified Schwab snapshot receipt histories, contracts and option-quality surfaces | target/availability; contract/leg symbol, call/put, strike, expiration, multiplier, bid/ask, Greeks, OI/volume/spread/staleness; quality state | live entry receipt is after source bar, available by Strategy cutoff and before target start; historical entry uses earliest eligible receipt and exit uses earliest bounded post-target receipt | Required per candidate route | **Confirmed.** `ml/strategy_selection/runtime.py:109`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:383`, `ml/strategy_selection/runtime.py:396` |
| Stock quote-liquidity history | Loop A | Schwab `quote-liquidity` Parquets | bid/ask, `available_at`, quote quality/policy/schema for stock legs and risk | entry/exit quote must lie within route cutoff; malformed schema/policy/duplicate natural keys fail validation | Conditional on strategy construction/stock legs | **Confirmed.** `ml/strategy_selection/chain.py:151`, `ml/strategy_selection/chain.py:258`, `ml/strategy_selection/chain.py:391` |
| Pricing evidence catalog | Active Pricing | verified target sidecars and current/historical contract predictions/surfaces | per-leg BSGP or Black–Scholes fair value/intervals, availability, generation age/support/shrinkage; candidate edge, conservative edge, coverage, uncertainty, favorable probability; `Active`, `Black-Scholes fallback`, `Delayed`, `Unavailable` | target sidecar status `BSGP_SHADOW_READY` maps to `pricing_source=BSGP`; complete residual fallback maps to `BLACK_SCHOLES`; loaded only through Strategy creation time and attached before scoring; live active mode rejects offline replay | Required for fitted model in active mode; explicit scenario fallback otherwise | **Confirmed.** `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_runtime.py:552` |
| Historical candidate outcomes | Derived by this loop from Loop B plus exact entry/exit evidence | in-memory materialization from immutable source files | realized net profit after leg cash flows/fees, return on risk, binary `profitable` = strictly positive; direction probability/context at the historical decision | only complete Loop B labels and exact causal entry/exit receipts; optional upper bound excludes lockbox starts; active-mode training admits only complete Pricing coverage | Required to fit/reuse; optional for scenario-prior publication | **Confirmed.** `ml/strategy_selection/runtime.py:126`, `ml/strategy_selection/runtime.py:351`, `ml/strategy_selection/runtime.py:479`, `ml/strategy_selection/runtime.py:149` |
| Prior compatible Strategy model | This loop | `ml/strategy-models/<horizon>/market-state-strategy-outcome/<generation>/` | classifier, expected-return regressor, calibrator; exact numeric/categorical features, source inventory/policy/partitions/evaluation | reuse only on exact expected configuration/input compatibility; otherwise refit; assessment is never calibration and real lockbox is not used | Optional | **Confirmed.** `ml/strategy_selection/model.py:276`, `ml/strategy_selection/model.py:300`, `ml/strategy_selection/model.py:316`, `ml/strategy_selection/model.py:372` |

## Processing and decisions

1. **Confirmed:** read and verify the current Loop B authority; fail if its samples or predictions are absent. Capture its immutable pointer record and causal cutoff. `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:83`, `ml/strategy_runtime.py:91`
2. **Confirmed:** load exact option-chain histories and the Pricing catalog only through the Strategy run clock. Validate Loop B inputs and assert forbidden lockbox starts are excluded. `ml/strategy_selection/runtime.py:83`, `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:109`
3. **Confirmed:** for each horizon, reconstruct candidate outcomes from earliest causal Schwab entry BBO and eligible exit BBO, attach Pricing before scoring, and keep the target `profitable` binary. Active-mode fitting filters to receipt-proven `ACTIVE` BSGP/Black–Scholes rows with effectively 100% leg coverage; offline replay cannot satisfy live active scoring. `ml/strategy_selection/runtime.py:139`, `ml/strategy_selection/runtime.py:149`, `ml/strategy_selection/runtime.py:383`, `ml/strategy_selection/runtime.py:458`, `ml/option_pricing/strategy_shadow.py:309`
4. **Confirmed:** partition by decision/target clusters into at least 252 training, 63 calibration and 63 assessment decisions, purging boundary overlap. Train/reuse a histogram-gradient classifier for profitability and regressor for return residual; fit Platt on calibration only, or identity if it has one class. `ml/strategy_selection/contracts.py:102`, `ml/strategy_selection/model.py:210`, `ml/strategy_selection/model.py:330`, `ml/strategy_selection/model.py:360`
5. **Confirmed:** for each canonical LIVE forecast, select one exact entry chain before target start, construct policy-allowed spread candidates, attach Loop B context and active Pricing evidence, infer market state using calibrated `probability_up`, and form a scenario prior. `ml/strategy_selection/runtime.py:225`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:267`, `ml/strategy_selection/runtime.py:296`
6. **Confirmed:** when a model and eligible active Pricing exist, output calibrated profitable-outcome probability and bounded expected return/profit. Ineligible/no-model rows retain an explicit Pricing-scenario score basis; mixing is reranked deterministically. `ml/strategy_selection/runtime.py:306`, `ml/strategy_selection/runtime.py:311`, `ml/strategy_selection/model.py:442`, `ml/strategy_selection/model.py:466`
7. **Confirmed:** validate probability bounds, score-basis/pricing-source coherence, one decision per route, unique candidate keys and complete ranks; then write candidate/audit/report/model files, manifest lineage, receipt and atomic current pointer. `ml/strategy_runtime.py:478`, `ml/strategy_runtime.py:527`, `ml/strategy_runtime.py:598`, `ml/strategy_runtime.py:217`, `ml/strategy_runtime.py:253`
8. **Confirmed:** the supervisor skips work only when the Loop B pointer, pricing mode and option-snapshot heads are unchanged. Historical outcome caching is an owned, process-memory LRU keyed by immutable evidence, not another loop. `ml/strategy_runtime.py:414`, `ml/strategy_selection/runtime.py:520`, `ml/strategy_selection/runtime.py:607`

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
- **Confirmed:** Options Capture supplies exact contract, entry and exit evidence. `ml/strategy_selection/runtime.py:111`, `ml/strategy_selection/runtime.py:240`
- **Confirmed:** Active Pricing supplies the implemented Black–Scholes-plus-residual sidecar and verified historical catalog before fitted probability scoring. Ready residual rows are labeled `BSGP`, baseline/fallback rows `BLACK_SCHOLES`, and absent/ineligible coverage retains an explicit scenario fallback. `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_selection/runtime.py:288`
- **Confirmed:** Loop A stock quote-liquidity is a shared-artifact data input, without a direct readiness barrier. `ml/strategy_selection/chain.py:151`, `ml/strategy_runtime.py:209`

### Downstream

**Confirmed:** no other production loop consumes Strategy outputs. The located consumers are read-only UI/data access. `app/ui/options_strategy_data.py:628`

### Timing and control relationships

**Confirmed:** Strategy’s +10 phase follows Loop B +5 and Options +6, but it does not wait on a named Options barrier; it reads the current verified Loop B and the option heads available by its own creation clock. If neither changes, it skips. `docs/datafetch-ml/current_start_command:139`, `docs/datafetch-ml/current_start_command:150`, `ml/strategy_runtime.py:120`, `ml/strategy_runtime.py:414`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | None | Strategy consumes directional probabilities after Loop B publication and has no write/control path back into Loop B. |
| Option-pricing predictions | None | Strategy consumes verified fair values/surfaces; its candidate score does not alter Pricing authority. |
| Options-strategy predictions | Direct | LIVE direction + exact chain/stock evidence + contract pricing + historical outcome model/scenario prior → profitable-outcome probability, expected return/profit and rank → Strategy authority. `ml/strategy_selection/runtime.py:225`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/model.py:442`, `ml/strategy_publication.py:40` |

**Roll-up classification: Options.** It directly owns options-strategy predictions and contributes to neither upstream prediction family.

## Failure and degradation behavior

- **Confirmed:** missing/corrupt current Loop B authority or missing required Loop B files prevents publication. `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:83`
- **Confirmed:** absent/invalid chain history or exact entry receipt skips the affected route and emits an audit reason; other routes continue. `ml/strategy_selection/runtime.py:116`, `ml/strategy_selection/runtime.py:230`, `ml/strategy_selection/runtime.py:247`
- **Confirmed:** insufficient complete historical outcomes, one-class training, or partition/model failure produces `MODEL_NOT_FIT`; live candidates can still publish under the explicit scenario-prior contract. `ml/strategy_selection/runtime.py:168`, `ml/strategy_selection/runtime.py:193`, `ml/strategy_selection/runtime.py:306`
- **Confirmed:** unavailable/delayed/incomplete active Pricing prevents fitted scoring for affected candidates, but they retain an explicit Pricing-scenario fallback and user-facing Pricing status. `ml/strategy_selection/runtime.py:310`, `ml/strategy_runtime.py:552`, `ml/strategy_runtime.py:566`
- **Confirmed:** schema, probability, score-basis or rank inconsistency fails validation before authoritative publication; an unreadable/mismatched pointer/receipt fails closed on read. `ml/strategy_runtime.py:478`, `ml/strategy_publication.py:85`, `ml/strategy_publication.py:134`

## Accuracy and efficiency relevance

- Leakage/target integrity: exact entry/exit receipt selection, no future chain/quote, completed labels only, boundary purging, lockbox exclusion and Pricing-before-probability ordering. `ml/strategy_selection/runtime.py:84`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/model.py:226`, `ml/strategy_runtime.py:474`
- Prediction/calibration quality: candidate/market/Pricing/context feature families, profitability classifier, expected-return residual regressor, Platt calibration, chronological assessment and explicit fallback score basis. `ml/strategy_selection/model.py:53`, `ml/strategy_selection/model.py:330`, `ml/strategy_selection/model.py:364`, `ml/strategy_runtime.py:468`
- Efficiency: compatible model reuse, skip-on-unchanged Loop B/option heads, process-memory historical-outcome LRU and exact source inventories. `ml/strategy_runtime.py:414`, `ml/strategy_selection/model.py:316`, `ml/strategy_selection/runtime.py:607`
- Computation/storage: historical candidate reconstruction can dominate a run; output copies only referenced model artifacts and immutable source lineage. `ml/strategy_selection/runtime.py:351`, `ml/strategy_runtime.py:176`

## Conflicts, gaps, and uncertainty

- **Confirmed alias/history, not conflict:** Strategy is startup owner 7. “Loop 6” belongs to the obsolete six-owner SVG and is not used as current inventory evidence. `docs/datafetch-ml/current_start_command:145`, `ml/strategy_runtime.py:361`
- **Confirmed active implementation:** `--pricing-mode active` is the production command, and implementation consumes ready BSGP sidecar rows or explicit Black–Scholes fallback before model scoring. Candidate-level scenario fallback remains a deliberate degradation contract, not evidence that the pricing setup is absent. `docs/datafetch-ml/current_start_command:150`, `ml/option_pricing/strategy_shadow.py:298`, `ml/strategy_runtime.py:566`, `tests/test_option_pricing_shadow_consumers.py:1064`
- **Unknown:** static analysis cannot establish current fitted-model coverage, current candidate count, empirical calibration/profitability, or whether all configured strategies have sufficient live chain evidence.
- **Unknown:** no production-loop consumer of Strategy authority was found; UI consumption is confirmed, deployment/use beyond that is not.
- **Confidence:** High for direct inputs, scoring/publication contract and fallback behavior; Medium for live model coverage and predictive performance.

## Evidence index

- `ml/strategy_runtime.py:63`
- `ml/strategy_runtime.py:125`
- `ml/strategy_runtime.py:217`
- `ml/strategy_selection/runtime.py:68`
- `ml/strategy_selection/runtime.py:139`
- `ml/strategy_selection/runtime.py:240`
- `ml/strategy_selection/model.py:276`
- `ml/strategy_selection/model.py:424`
- `tests/test_ml_strategy_selection.py:343`
- `tests/test_ml_strategy_selection.py:891`
- `tests/test_option_pricing_shadow_consumers.py:1058`
