# Directional Loop B

## Identity

- Canonical name: Directional Loop B
- Logical aliases or numbering: Loop B; startup owner 6
- Runtime entry point: `python -m ml.prediction_runtime`
- Owning package: `ml`
- Classification: Independent production loop
- Scheduling mechanism: recurring supervisor gated by the shared Loop A datastore-cycle lock
- Cadence and phase: every 30 minutes at UTC phase +5 minutes in the production command; one classified-transient retry; immediate startup recovery at 35 minutes of verified authority age
- Lock or single-writer mechanism: `.duckets-ml-prediction-runtime.lock` plus Loop A’s shared datastore-cycle lock during each run
- Primary code evidence: **Confirmed.** `ml/prediction_runtime.py:26`, `ml/prediction_runtime.py:76`, `ml/prediction_runtime.py:189`, `ml/prediction_runtime.py:192`, `ml/prediction_runtime.py:209`

## Purpose

**Confirmed:** Loop B is the authoritative directional-horizon prediction loop. Under the shared lock it snapshots the current Loop A record only after that record is `COMPLETE`, materializes causal rolling samples and targets for intraday, daily and weekly routes, activates feature families through their contracts/gates, partitions observations chronologically, fits or reuses a calibrated classifier, scores BACKTEST and eligible LIVE rows, reconciles matured predictions, produces monitoring/intelligence views, and atomically promotes one immutable run. `datafetching/loop_a_cycle.py:136`, `ml/prediction_runtime.py:218`, `ml/runtime_pipeline.py:329`, `ml/runtime_pipeline.py:464`, `ml/runtime_pipeline.py:480`, `ml/runtime_pipeline.py:876`

**Confirmed non-ownership:** it does not acquire provider data, publish option chains or option fair values, choose final options strategies, or write to the closed lockbox. Strategy is explicitly an independent authority. `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:838`

**Startup/bootstrap boundary:** Loop B is a reader/computation owner. It starts
only from verified Loop A and other causal feature authorities and never treats
a cold-start cursor itself as production evidence. CME may seed a compatible
missing boundary and fingerprint verified archive history into its operational
context. Loop A instead continues current `EQUS.MINI` while the differently
identified `XNAS.ITCH` archive remains separate provenance. Loop B consumes
only the resulting operational products under its normal causal feature
contracts. On a brand-new datastore a base/earlier-profile Loop
B generation is also the decision-grid prerequisite for the one-time ALFRED
backfill; the v3 macro profile begins only after ALFRED readiness is verified.

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Complete current Loop A cycle and market/fundamental feature evidence | Loop A | current `.ducketz-loop-a-cycle.json` plus normalized bars/quotes, technicals, fundamentals, signals, energy/SEC/current context artifacts | current `COMPLETE` cycle’s `finished_at`; OHLCV/adjustments; technical returns/trends/volatility/momentum; valuation/financial statement; signal/regime and current provider context fields | shared lock prevents reading through a write; `finished_at` is the causal input cutoff; a newer `WRITING`/`FAILED` current record is not replaced with `.ducketz-loop-a-complete.json`; each feature uses its own `available_at`/freshness contract | Required control boundary and core features | **Confirmed.** `datafetching/loop_a_cycle.py:136`, `ml/prediction_runtime.py:209`, `ml/prediction_runtime.py:218`, `ml/rolling_materialization.py:128`, `ml/rolling_materialization.py:272` |
| Point-in-time macro readiness and features | Daily ALFRED | readiness pointer/receipt plus immutable vintages and `alfred-release-context` | `macro__fed_funds_level`, `macro__cpi_yoy`, `macro__unemployment_change`, `macro__gdp_yoy`; observation/vintage/release/availability clocks and quality | daily/weekly only; verified importer lineage, no lookahead and at least 95% coverage; each value then obeys horizon freshness | Required shared contract for active daily/weekly macro family | **Confirmed.** `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:757`, `datafetching/fred_alfred_readiness.py:185` |
| CME cross-asset context | CME/L2 runtime | `pools/cme/features/cross-asset-context/databento/1h.parquet` | `cme__` NQ/ES/RTY/gold/crude returns, breadth, relative spreads/book imbalance and quality/availability | latest causal completed common window; future/stale values are ineligible | Conditional feature family | **Confirmed.** `ml/rolling_materialization.py:796`, `datafetching/cme_cross_asset_context.py:196` |
| Option-quality features | Options Capture | verified snapshot histories and `option-quality.parquet` | `opt__` implied move, IV-realized spread, term/skew/smile, OI/volume, parity, quote coverage/staleness and surface quality | snapshot/receipt must be available by decision and pass feature-specific freshness; missing values retain explicit semantics | Conditional feature family | **Confirmed.** `ml/rolling_materialization.py:614`, `options/features.py:214` |
| Compact option-pricing surfaces | Active Pricing | verified `pricing-surfaces.parquet` generation chain | `opx__` causal coverage, median normalized residual/uncertainty/edge, positive/negative edge fractions, interval coverage, spread/staleness and quality status | only receipt-proven, first-available, fresh and quality-admitted surfaces; explicit unavailability permits baseline feature profile, corruption fails closed | Optional gated family | **Confirmed.** `ml/rolling_materialization.py:663`, `ml/option_pricing/consumers.py:30`, `ml/option_pricing/consumers.py:478` |
| Prior verified Loop B runs/models | This loop | `ml/runs/`, model generations, publication receipts and `ml/latest/run.json` | prior LIVE forecasts/target starts, model input fingerprints, training cutoff, calibrator/support, weekly frozen issuance | prior rows must be receipt-proven and still actionable; model reuse requires exact compatibility; matured LIVE targets are excluded from offline partition overlap | Optional on bootstrap; authoritative when present | **Confirmed.** `ml/runtime_pipeline.py:367`, `ml/runtime_pipeline.py:373`, `ml/model_runtime.py:372`, `ml/model_runtime.py:625` |

## Processing and decisions

1. **Confirmed:** acquire the standalone runtime lock, wait for phase, acquire the shared Loop A lock, then require the current Loop A cycle record to be `COMPLETE` and pass its finish time as `input_available_at`. `ml/prediction_runtime.py:189`, `ml/prediction_runtime.py:209`, `ml/prediction_runtime.py:218`
2. **Confirmed:** materialize each symbol/horizon route at one input cutoff. Within the materializer, source/inventory caches are per invocation; it joins technical/fundamental/macro/signal/CME/option/Pricing families by point-in-time availability. `ml/rolling_materialization.py:128`, `ml/rolling_materialization.py:179`, `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:796`
3. **Confirmed:** construct horizon targets according to explicit exchange-calendar rules: next 60 or 180 eligible one-minute records, next-session open/close, or the dynamic remaining-week aggregate and its contiguous eligible-session prefix. The `1h` decision uses the latest completed regular or available bounded US extended-hours source bar; `4h` remains regular-session bounded. A missing predetermined constituent makes the label incomplete; cost-adjusted return is positive only after one configured round trip. `ml/calendars.py:527`, `ml/horizons.py:121`, `ml/horizons.py:173`, `ml/horizons.py:242`, `ml/horizons.py:283`, `ml/rolling_samples.py:270`
4. **Confirmed:** require active feature-set contracts. If Pricing features are configured but fail coverage/freshness, quarantine that family and fit the implemented baseline feature set instead; shared-authority corruption is not treated as ordinary missingness. `ml/runtime_pipeline.py:424`, `ml/runtime_pipeline.py:432`, `ml/runtime_pipeline.py:455`, `ml/rolling_materialization.py:322`
5. **Confirmed:** split target clusters chronologically into training, calibration, assessment and sealed lockbox; exclude target starts already used by compatible prospective LIVE forecasts. Intraday routes use the 160/40/40/80 policy supported by the bounded 100-calendar-day native-minute input, while daily/weekly routes retain 252/63/63/126. Fit or reuse an exact-compatible model and calibrator, never using the assessment or lockbox to train. `app/services/market_fetch_specs.py:96`, `ml/runtime_pipeline.py:464`, `ml/model_runtime.py:76`, `ml/model_runtime.py:141`, `ml/model_runtime.py:372`, `ml/model_runtime.py:458`
6. **Confirmed:** score assessment rows as `BACKTEST` and causally eligible current rows as `LIVE`; weekly forecasts use a frozen remaining-week issuance contract. Intelligence may classify an omitted weekly suffix slot as `NOT_APPLICABLE_TO_REMAINING_WEEK` and `OPERATIONALLY_CURRENT` only when exactly one coherent created-LIVE bundle for that symbol proves the aggregate-plus-contiguous-component prefix, common issuance, valid calendar geometry/deadlines/models, and bounded probabilities. Missing, malformed, or ambiguous proof remains fail-closed and stale. `ml/runtime_pipeline.py:3762`, `ml/runtime_pipeline.py:3819`, `ml/runtime_pipeline.py:4005`, `tests/test_ml_weekly_context_model_runtime.py:361`
7. **Confirmed:** enforce the real publication deadline strictly before the earliest live `actionable_until`; carry only still-active receipt-proven prior forecasts. If all predictions are empty, fail without promotion. `ml/runtime_pipeline.py:590`, `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:612`, `ml/runtime_pipeline.py:654`
8. **Confirmed:** redact closed-lockbox sample outcomes, reconcile only visible matured targets into evaluation rows, write five schema-bound outputs and a manifest, then publish receipt before atomically advancing the sole authority pointer. Compatibility mirrors are not authoritative. `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:717`, `ml/runtime_pipeline.py:794`, `ml/runtime_pipeline.py:931`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Rolling samples | Strategy; Daily ALFRED coverage planner; this loop | `ml/runs/<generation>/samples.parquet` | symbol/horizon/decision and availability clocks; target window/action deadline; grouped features; open/close/raw and cost-adjusted returns; binary target; `COMPLETE`/incomplete label status/reason | Closed-lockbox target values are redacted; schema and manifest bound to the immutable run | **Confirmed.** `ml/parquet_contracts.py:98`, `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:699` |
| Directional predictions | Strategy and UI/readers; this loop | `ml/runs/<generation>/predictions.parquet` | `raw_probability` and `calibrated_probability` in [0,1], model/version/calibration, `LIVE` or `BACKTEST`, target/action clocks and status | LIVE must publish before action deadline; fresh and carried rows are distinguished; authoritative only via valid run receipt/current pointer | **Confirmed.** `ml/parquet_contracts.py:131`, `ml/runtime_pipeline.py:494`, `ml/runtime_pipeline.py:503`, `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:2450` |
| Evaluation and monitoring | model-reuse gate/research/UI | `evaluations.parquet`, `monitoring.parquet` | observed binary target/returns, raw/calibrated log loss and Brier score, 0.5 accuracy; status/metric/reference/evidence window | only matured causally visible targets; receipt-bound run; sealed targets absent | **Confirmed.** `ml/parquet_contracts.py:156`, `ml/parquet_contracts.py:187`, `ml/runtime_pipeline.py:717`, `ml/runtime_pipeline.py:729` |
| Intelligence view | read-only UI/consumers | `intelligence.parquet` and compatibility mirror `ml-intelligence/latest/rolling-predictions.parquet` | probability up/down, actionability/operational/model/live/intelligence status, clocks, model identity; intentional weekly suffix N/A versus genuine stale absence | immutable-run file is authoritative; a suffix N/A requires one coherent per-symbol LIVE prefix and carries no probability; latest mirror is compatibility-only | **Confirmed.** `ml/parquet_contracts.py:224`, `ml/runtime_pipeline.py:3762`, `ml/runtime_pipeline.py:4005` |
| Manifest, receipt and current pointer | Strategy; UI/readers; Daily ALFRED reader | `ml/runs/<generation>/manifest.json`, publication receipt, `ml/latest/run.json` | source checksum inventory, exact config/features/models/routes/errors, causal cutoff, publication counts, prior authority lineage | files and manifest complete first; receipt verified before atomic pointer; pointer is the sole generation boundary | **Confirmed.** `ml/runtime_pipeline.py:801`, `ml/runtime_pipeline.py:846`, `ml/runtime_pipeline.py:859`, `ml/runtime_pipeline.py:943` |
| Compatible model generations | later Loop B cycles | model artifact directories/receipts | estimator, calibrator, feature set, target definition, source fingerprint, trained-through/partitions and assessment metrics | reused only after schema/specification/source compatibility checks; otherwise refit chronologically | **Confirmed.** `ml/model_runtime.py:372`, `ml/model_runtime.py:437`, `ml/model_runtime.py:625` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop A supplies the hard complete-cycle control boundary and most feature evidence. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:272`
- **Confirmed:** ALFRED supplies verified point-in-time macro readiness/features. `ml/rolling_materialization.py:740`
- **Confirmed:** CME supplies cross-asset context; Options supplies option-quality features; Pricing supplies gated surfaces. `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:796`

### Downstream

- **Confirmed:** Strategy consumes this run’s samples, LIVE probabilities, configuration and cutoff. `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:82`, `ml/strategy_runtime.py:91`
- **Confirmed:** Daily ALFRED reads the current sample decision grid to scope planned series/decision coverage; this is asynchronous historical-coverage feedback, not a same-cycle barrier. `datafetching/fred_alfred_readiness.py:400`, `datafetching/fred_alfred_readiness.py:420`

### Timing and control relationships

**Confirmed:** Loop B runs at the 30-minute `:05`/`:35` phase and waits on Loop A’s shared lock/complete cycle, not on CME, ALFRED’s daily scheduler, Pricing or Options. Its strict action deadline is a publication boundary independent of wall-clock phase. Options is phase +6, but no B → Options artifact exchange exists. `ml/prediction_runtime.py`, `ml/runtime_pipeline.py:603`, `docs/datafetch-ml/current_start_command`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Direct | point-in-time sample/target → chronological fit/reuse and calibration → LIVE raw/calibrated probability → atomic Loop B authority. `ml/runtime_pipeline.py:464`, `ml/runtime_pipeline.py:480`, `ml/runtime_pipeline.py:509`, `ml/runtime_pipeline.py:876` |
| Option-pricing predictions | None | Pricing does not read Loop B samples, probabilities, models or pointers; phase proximity is not a causal input. |
| Options-strategy predictions | Indirect | authoritative Loop B samples and probability-up → exact-chain candidate context → fitted calibrated profitable-outcome score when available, otherwise non-probabilistic Scenario Coverage → Strategy ranking. `ml/strategy_runtime.py`, `ml/strategy_selection/runtime.py` |

**Roll-up classification: Both.** It directly owns horizon predictions and indirectly drives strategy predictions; it has no path into contract pricing.

## Failure and degradation behavior

- `.duckets-ml-prediction-runtime.lock` rejects a second supervisor and has no
  stale-PID recovery. The shared OS datastore-cycle lock prevents a complete B
  read from overlapping Loop A mutation and releases when either process exits.
- Missing, `WRITING`, or `FAILED` current Loop A cycle state aborts the attempt.
  B does not silently fall back to `.ducketz-loop-a-complete.json`.
- Structurally valid missing/stale optional feature values become audited null.
  Pricing-family coverage/freshness failure selects the registered non-Pricing
  baseline. Corrupt Pricing authority or invalid ALFRED readiness is a shared
  contract failure and aborts the entire new publication.
- The production default allows successful routes to publish when some routes
  fail. An empty prediction set, `--require-all-routes`, missed action deadline,
  receipt failure, or promotion failure leaves the prior `ml/latest/run.json`
  authority in place.
- A recurring failure receives at most one retry and only when classified as
  transient. Deadline, pointer/receipt/checksum integrity, deterministic
  contract, and explicitly failed Loop A states do not retry. A newly started
  supervisor performs one immediate recovery cycle only when the last verified
  authority is at least 35 minutes old; corrupt authority fails closed.
- An omitted weekly component is not automatically healthy. Without exactly
  one coherent calendar-valid created-LIVE bundle for its symbol, the route
  remains `OPERATIONALLY_STALE`/`NO_CURRENT_FORECAST`; ambiguous bundles also
  fail closed.


## Accuracy and efficiency relevance

- Leakage/target integrity: one input cutoff, per-source availability/freshness, predetermined calendar constituents, separate label availability, chronological partitions, prospective-target exclusion and sealed lockbox. `ml/runtime_pipeline.py:310`, `ml/horizons.py:145`, `ml/model_runtime.py:141`, `ml/runtime_pipeline.py:695`
- Feature quality/activation: registered active feature sets and explicit Pricing-family quarantine; macro readiness is fail-closed. `ml/runtime_pipeline.py:424`, `ml/runtime_pipeline.py:432`, `ml/rolling_materialization.py:757`
- Calibration/quality: separate calibration partition; assessment reports log loss, Brier, support and base-rate comparisons; prediction rows retain raw and calibrated probabilities. `ml/model_runtime.py:466`, `ml/model_runtime.py:677`, `ml/parquet_contracts.py:151`
- Critical path/computation: per-cycle materialization, per-horizon fit-or-reuse, weekly freeze/carry and atomic staging. Existing caches are limited to one materialization call. `ml/rolling_materialization.py:179`, `ml/runtime_pipeline.py:480`, `ml/runtime_pipeline.py:612`
- Model reuse: exact fingerprint/contract compatibility avoids unnecessary refits without accepting stale or mismatched models. `ml/model_runtime.py:437`, `ml/model_runtime.py:625`

## Conflicts, gaps, and uncertainty

- The executable lock filename is `.duckets-ml-prediction-runtime.lock`
  (without the second `z`). This audit records the current contract and does not
  rename it merely for spelling consistency.
- `ml/latest/*.parquet` and
  `ml-intelligence/latest/rolling-predictions.parquet` are compatibility mirrors;
  only the immutable run receipt plus `ml/latest/run.json` selects authority.
- Current route coverage, admitted feature family, model reuse and realized
  metrics are manifest/evaluation facts, not guaranteed by the v3 command.

## Runtime and evaluation monitoring

**Confirmed deployment contract:** Directional Loop B is a standalone hidden
owner whose worker owns the executable singleton lock
`.duckets-ml-prediction-runtime.lock`. The monitor verifies the exact pair,
lock, immutable run receipt/pointer, freshness, route inventory, Strategy
source lineage, and UI contract independently. A live PID cannot excuse a stale
publication. Freshness uses the receipt's `promoted_at` availability clock,
warns after 35 minutes, and fails after 45 minutes. `ml/system_monitor.py`,
`docs/datafetch-ml/start_all_loops.ps1:18`

Daily monitoring evaluates every public and component route—`1h`, `4h`, `1d`,
`1w`, and `1w-d1` through `1w-d5`—and keeps insufficient mature LIVE labels
separate from measured poor results. Weekly monitoring is not an alias for this
loop's `1w` route: it uses immutable `LIVE` evaluations from the last two
completed XNYS session weeks, requires matching model/target/cost definitions
and at least 30 independent observations per period, and otherwise returns
`INSUFFICIENT_WEEKLY_EVIDENCE` or
`INCOMPATIBLE_WEEKLY_DEFINITIONS`. `ml/system_monitor.py:1375`

**Observed 2026-08-19 22:45:36 UTC:** one Loop B launcher/worker pair, matching
worker lock, active primary logs, current publication, Strategy lineage, and
Rolling Forecast UI contract passed. Immutable run
`ml/runs/20260819T223552.337574Z` passed manifest and publication-receipt
verification. Its 54 intelligence rows were all `OPERATIONALLY_CURRENT`; every
symbol had primary `1h`, `4h`, `1d`, and `1w` coverage and no stale cell.

The decision occurred on Wednesday. For every symbol the one valid frozen LIVE
remaining-week bundle was `1w`, `1w-d1` (Thursday), and `1w-d2` (Friday).
Slots `1w-d3` through `1w-d5` were correctly
`NOT_APPLICABLE_TO_REMAINING_WEEK`, with no synthesized probability, rather
than missing forecasts. A 22:59:29 UTC read-only follow-up verified a newer
current run `ml/runs/20260819T225107.106040Z` with the same 54-row, stale-free
UI contract. Run paths and the weekday-specific prefix are observations, not
fixed architecture.


## Evidence index

- `ml/prediction_runtime.py:189`
- `ml/prediction_runtime.py:192`
- `ml/prediction_runtime.py:209`
- `ml/runtime_pipeline.py:432`
- `ml/runtime_pipeline.py:603`
- `ml/runtime_pipeline.py:654`
- `ml/runtime_pipeline.py:859`
- `ml/runtime_pipeline.py:943`
- `datafetching/loop_a_cycle.py:136`
- `tests/test_ml_prediction_runtime.py:100`
- `tests/test_ml_runtime_pipeline.py:885`
