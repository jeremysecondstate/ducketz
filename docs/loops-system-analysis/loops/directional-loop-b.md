# Directional Loop B

## Identity

- Canonical name: Directional Loop B
- Logical aliases or numbering: Loop B; startup owner 6
- Runtime entry point: `python -m ml.prediction_runtime`
- Owning package: `ml`
- Classification: Independent production loop
- Scheduling mechanism: recurring supervisor gated by the shared Loop A datastore-cycle lock
- Cadence and phase: every 15 minutes at UTC phase +5 minutes in the production command
- Lock or single-writer mechanism: `.duckets-ml-prediction-runtime.lock` plus Loop A’s shared datastore-cycle lock during each run
- Primary code evidence: **Confirmed.** `ml/prediction_runtime.py:26`, `ml/prediction_runtime.py:76`, `ml/prediction_runtime.py:189`, `ml/prediction_runtime.py:192`, `ml/prediction_runtime.py:209`

## Purpose

**Confirmed:** Loop B is the authoritative directional-horizon prediction loop. Under the shared lock it snapshots the current Loop A record only after that record is `COMPLETE`, materializes causal rolling samples and targets for intraday, daily and weekly routes, activates feature families through their contracts/gates, partitions observations chronologically, fits or reuses a calibrated classifier, scores BACKTEST and eligible LIVE rows, reconciles matured predictions, produces monitoring/intelligence views, and atomically promotes one immutable run. `datafetching/loop_a_cycle.py:136`, `ml/prediction_runtime.py:218`, `ml/runtime_pipeline.py:329`, `ml/runtime_pipeline.py:464`, `ml/runtime_pipeline.py:480`, `ml/runtime_pipeline.py:876`

**Confirmed non-ownership:** it does not acquire provider data, publish option chains or option fair values, choose final options strategies, or write to the closed lockbox. Strategy is explicitly an independent authority. `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:838`

**Startup/bootstrap boundary:** Loop B is a reader/computation owner. It starts
only from verified Loop A and other causal feature authorities and never uses a
cold-start archive cursor as production evidence. On a brand-new datastore a
base/earlier-profile Loop B generation is also the decision-grid prerequisite
for the one-time ALFRED backfill; the v3 macro profile begins only after ALFRED
readiness is verified.

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Complete current Loop A cycle and market/fundamental feature evidence | Loop A | current `.ducketz-loop-a-cycle.json` plus normalized bars/quotes, technicals, fundamentals, signals, energy/SEC/current context artifacts | current `COMPLETE` cycle’s `finished_at`; OHLCV/adjustments; technical returns/trends/volatility/momentum; valuation/financial statement; signal/regime and current provider context fields | shared lock prevents reading through a write; `finished_at` is the causal input cutoff; a newer `WRITING`/`FAILED` current record is not replaced with `.ducketz-loop-a-complete.json`; each feature uses its own `available_at`/freshness contract | Required control boundary and core features | **Confirmed.** `datafetching/loop_a_cycle.py:136`, `ml/prediction_runtime.py:209`, `ml/prediction_runtime.py:218`, `ml/rolling_materialization.py:128`, `ml/rolling_materialization.py:272` |
| Point-in-time macro readiness and features | Daily ALFRED | readiness pointer/receipt plus immutable vintages and `alfred-release-context` | `macro__fed_funds_level`, `macro__cpi_yoy`, `macro__unemployment_change`, `macro__gdp_yoy`; observation/vintage/release/availability clocks and quality | daily/weekly only; verified importer lineage, no lookahead and at least 95% coverage; each value then obeys horizon freshness | Required shared contract for active daily/weekly macro family | **Confirmed.** `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:757`, `datafetching/fred_alfred_readiness.py:185` |
| CME cross-asset context | CME/L2 runtime | `pools/cme/features/cross-asset-context/databento/1h.parquet` | `cme__` NQ/ES/RTY/gold/crude returns, breadth, relative spreads/book imbalance and quality/availability | latest causal completed common window; future/stale values are ineligible | Conditional feature family | **Confirmed.** `ml/rolling_materialization.py:782`, `datafetching/cme_cross_asset_context.py:174` |
| Option-quality features | Options Capture | verified snapshot histories and `option-quality.parquet` | `opt__` implied move, IV-realized spread, term/skew/smile, OI/volume, parity, quote coverage/staleness and surface quality | snapshot/receipt must be available by decision and pass feature-specific freshness; missing values retain explicit semantics | Conditional feature family | **Confirmed.** `ml/rolling_materialization.py:614`, `options/features.py:214` |
| Compact option-pricing surfaces | Active Pricing | verified `pricing-surfaces.parquet` generation chain | `opx__` causal coverage, median normalized residual/uncertainty/edge, positive/negative edge fractions, interval coverage, spread/staleness and quality status | only receipt-proven, first-available, fresh and quality-admitted surfaces; explicit unavailability permits baseline feature profile, corruption fails closed | Optional gated family | **Confirmed.** `ml/rolling_materialization.py:663`, `ml/option_pricing/consumers.py:30`, `ml/option_pricing/consumers.py:478` |
| Prior verified Loop B runs/models | This loop | `ml/runs/`, model generations, publication receipts and `ml/latest/run.json` | prior LIVE forecasts/target starts, model input fingerprints, training cutoff, calibrator/support, weekly frozen issuance | prior rows must be receipt-proven and still actionable; model reuse requires exact compatibility; matured LIVE targets are excluded from offline partition overlap | Optional on bootstrap; authoritative when present | **Confirmed.** `ml/runtime_pipeline.py:367`, `ml/runtime_pipeline.py:373`, `ml/model_runtime.py:372`, `ml/model_runtime.py:625` |

## Processing and decisions

1. **Confirmed:** acquire the standalone runtime lock, wait for phase, acquire the shared Loop A lock, then require the current Loop A cycle record to be `COMPLETE` and pass its finish time as `input_available_at`. `ml/prediction_runtime.py:189`, `ml/prediction_runtime.py:209`, `ml/prediction_runtime.py:218`
2. **Confirmed:** materialize each symbol/horizon route at one input cutoff. Within the materializer, source/inventory caches are per invocation; it joins technical/fundamental/macro/signal/CME/option/Pricing families by point-in-time availability. `ml/rolling_materialization.py:128`, `ml/rolling_materialization.py:176`, `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:782`
3. **Confirmed:** construct horizon targets according to explicit exchange-calendar rules: next 60 or 240 eligible one-minute records, next-session open/close, or remaining-week/component endpoints. A missing predetermined constituent makes the label incomplete; cost-adjusted return is positive only after one configured round trip. `ml/horizons.py:121`, `ml/horizons.py:171`, `ml/horizons.py:221`, `ml/horizons.py:240`, `ml/rolling_samples.py:266`
4. **Confirmed:** require active feature-set contracts. If Pricing features are configured but fail coverage/freshness, quarantine that family and fit the implemented baseline feature set instead; shared-authority corruption is not treated as ordinary missingness. `ml/runtime_pipeline.py:424`, `ml/runtime_pipeline.py:432`, `ml/runtime_pipeline.py:455`, `ml/rolling_materialization.py:322`
5. **Confirmed:** split target clusters chronologically into training, calibration, assessment and sealed lockbox; exclude target starts already used by compatible prospective LIVE forecasts. Fit or reuse an exact-compatible model and calibrator, never using the assessment or lockbox to train. `ml/runtime_pipeline.py:464`, `ml/model_runtime.py:141`, `ml/model_runtime.py:372`, `ml/model_runtime.py:458`
6. **Confirmed:** score assessment rows as `BACKTEST` and causally eligible current rows as `LIVE`; weekly forecasts use a frozen remaining-week issuance contract. Raw and calibrated probabilities are constrained to probability semantics. `ml/runtime_pipeline.py:493`, `ml/runtime_pipeline.py:502`, `ml/runtime_pipeline.py:539`, `ml/parquet_contracts.py:151`
7. **Confirmed:** enforce the real publication deadline strictly before the earliest live `actionable_until`; carry only still-active receipt-proven prior forecasts. If all predictions are empty, fail without promotion. `ml/runtime_pipeline.py:590`, `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:612`, `ml/runtime_pipeline.py:654`
8. **Confirmed:** redact closed-lockbox sample outcomes, reconcile only visible matured targets into evaluation rows, write five schema-bound outputs and a manifest, then publish receipt before atomically advancing the sole authority pointer. Compatibility mirrors are not authoritative. `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:717`, `ml/runtime_pipeline.py:794`, `ml/runtime_pipeline.py:931`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Rolling samples | Strategy; Daily ALFRED coverage planner; this loop | `ml/runs/<generation>/samples.parquet` | symbol/horizon/decision and availability clocks; target window/action deadline; grouped features; open/close/raw and cost-adjusted returns; binary target; `COMPLETE`/incomplete label status/reason | Closed-lockbox target values are redacted; schema and manifest bound to the immutable run | **Confirmed.** `ml/parquet_contracts.py:98`, `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:699` |
| Directional predictions | Strategy and UI/readers; this loop | `ml/runs/<generation>/predictions.parquet` | `raw_probability` and `calibrated_probability` in [0,1], model/version/calibration, `LIVE` or `BACKTEST`, target/action clocks and status | LIVE must publish before action deadline; fresh and carried rows are distinguished; authoritative only via valid run receipt/current pointer | **Confirmed.** `ml/parquet_contracts.py:131`, `ml/runtime_pipeline.py:493`, `ml/runtime_pipeline.py:603` |
| Evaluation and monitoring | model-reuse gate/research/UI | `evaluations.parquet`, `monitoring.parquet` | observed binary target/returns, raw/calibrated log loss and Brier score, 0.5 accuracy; status/metric/reference/evidence window | only matured causally visible targets; receipt-bound run; sealed targets absent | **Confirmed.** `ml/parquet_contracts.py:156`, `ml/parquet_contracts.py:187`, `ml/runtime_pipeline.py:717`, `ml/runtime_pipeline.py:729` |
| Intelligence view | read-only UI/consumers | `intelligence.parquet` and compatibility mirror `ml-intelligence/latest/rolling-predictions.parquet` | probability up/down, actionability/operational/model/live/intelligence status, clocks, model identity | immutable-run file is authoritative; latest mirror is explicitly compatibility-only | **Confirmed.** `ml/parquet_contracts.py:206`, `ml/runtime_pipeline.py:738`, `ml/runtime_pipeline.py:943` |
| Manifest, receipt and current pointer | Strategy; UI/readers; Daily ALFRED reader | `ml/runs/<generation>/manifest.json`, publication receipt, `ml/latest/run.json` | source checksum inventory, exact config/features/models/routes/errors, causal cutoff, publication counts, prior authority lineage | files and manifest complete first; receipt verified before atomic pointer; pointer is the sole generation boundary | **Confirmed.** `ml/runtime_pipeline.py:801`, `ml/runtime_pipeline.py:846`, `ml/runtime_pipeline.py:859`, `ml/runtime_pipeline.py:943` |
| Compatible model generations | later Loop B cycles | model artifact directories/receipts | estimator, calibrator, feature set, target definition, source fingerprint, trained-through/partitions and assessment metrics | reused only after schema/specification/source compatibility checks; otherwise refit chronologically | **Confirmed.** `ml/model_runtime.py:372`, `ml/model_runtime.py:437`, `ml/model_runtime.py:625` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop A supplies the hard complete-cycle control boundary and most feature evidence. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:272`
- **Confirmed:** ALFRED supplies verified point-in-time macro readiness/features. `ml/rolling_materialization.py:740`
- **Confirmed:** CME supplies cross-asset context; Options supplies option-quality features; Pricing supplies gated surfaces. `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:782`

### Downstream

- **Confirmed:** Strategy consumes this run’s samples, LIVE probabilities, configuration and cutoff. `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:82`, `ml/strategy_runtime.py:91`
- **Confirmed:** Daily ALFRED reads the current sample decision grid to scope planned series/decision coverage; this is asynchronous historical-coverage feedback, not a same-cycle barrier. `datafetching/fred_alfred_readiness.py:400`, `datafetching/fred_alfred_readiness.py:420`

### Timing and control relationships

**Confirmed:** Loop B is phase +5 and waits on Loop A’s shared lock/complete cycle, not on CME, ALFRED’s daily scheduler, Pricing or Options. Its strict action deadline is a publication boundary independent of wall-clock phase. Options is phase +6, but no B → Options artifact exchange exists. `ml/prediction_runtime.py:78`, `ml/prediction_runtime.py:209`, `ml/runtime_pipeline.py:603`, `docs/datafetch-ml/current_start_command:160`, `docs/datafetch-ml/current_start_command:188`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Direct | point-in-time sample/target → chronological fit/reuse and calibration → LIVE raw/calibrated probability → atomic Loop B authority. `ml/runtime_pipeline.py:464`, `ml/runtime_pipeline.py:480`, `ml/runtime_pipeline.py:509`, `ml/runtime_pipeline.py:876` |
| Option-pricing predictions | None | Pricing does not read Loop B samples, probabilities, models or pointers; phase proximity is not a causal input. |
| Options-strategy predictions | Indirect | authoritative Loop B samples and probability-up → exact-chain candidate context/prior → fitted profitable-outcome score or explicit fallback → Strategy ranking. `ml/strategy_runtime.py:82`, `ml/strategy_selection/runtime.py:299`, `ml/strategy_selection/runtime.py:311` |

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


## Accuracy and efficiency relevance

- Leakage/target integrity: one input cutoff, per-source availability/freshness, predetermined calendar constituents, separate label availability, chronological partitions, prospective-target exclusion and sealed lockbox. `ml/runtime_pipeline.py:310`, `ml/horizons.py:145`, `ml/model_runtime.py:141`, `ml/runtime_pipeline.py:695`
- Feature quality/activation: registered active feature sets and explicit Pricing-family quarantine; macro readiness is fail-closed. `ml/runtime_pipeline.py:424`, `ml/runtime_pipeline.py:432`, `ml/rolling_materialization.py:757`
- Calibration/quality: separate calibration partition; assessment reports log loss, Brier, support and base-rate comparisons; prediction rows retain raw and calibrated probabilities. `ml/model_runtime.py:466`, `ml/model_runtime.py:677`, `ml/parquet_contracts.py:151`
- Critical path/computation: per-cycle materialization, per-horizon fit-or-reuse, weekly freeze/carry and atomic staging. Existing caches are limited to one materialization call. `ml/rolling_materialization.py:176`, `ml/runtime_pipeline.py:480`, `ml/runtime_pipeline.py:612`
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
