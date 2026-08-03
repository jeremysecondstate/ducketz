# Parquet ID contract

Implementation snapshot: 2026-08-03

This is the repository-wide persistence rule:

> Every persisted Parquet contains exactly one Duckets-generated identifier
> column. It is named `id`.

The rule applies to raw, normalized, calculated, feature, sample, target,
prediction, evaluation, monitoring, intelligence, and error Parquets.
It also applies to the Loop B strategy candidate and per-strategy audit
artifacts.

## Normative requirements

Every persisted Parquet must satisfy all of the following:

1. `id` appears exactly once.
2. `id` is a string column and is the first physical field.
3. Every non-empty row has a non-null, non-blank `id`.
4. `id` is unique inside that Parquet.
5. Declared calculated and Loop B grains assemble `id` from their exact natural
   columns. Provider-ingestion files prefer the minimum usual natural columns,
   adaptively add readable event values for real provider collisions, and use a
   deterministic file-local row fallback only when no safe value combination
   exists.
6. Timestamp components use readable UTC ISO-8601 text.
7. `id` is not a SHA value, digest, UUID, random token, content address, or
   registry key.
8. No other Duckets-generated identity-shaped column is allowed. This includes
   names ending in `*_id` or `*_ids` and names containing the reserved identity
   terms `hash`, `digest`, `fingerprint`, `checksum`, `sha1`, `sha224`,
   `sha256`, `sha384`, `sha512`, `receipt`, `lineage`, `identity`,
   `content_address`, `uuid`, or `guid`. Provider-native fields with those names
   are preserved only in raw provider evidence and are not Duckets identities.
9. Joins use `id` only when two files have the same exact row grain. Otherwise
   they use the readable natural columns shared by both files.
10. Loop control-plane fields are never persisted in a Parquet. Generations,
    `WRITING`/`COMPLETE` state, leases, acknowledgements, rejections, and route
    handoff metadata belong only in their small JSON control artifact.

Readable value columns are not identities merely because they describe policy,
specification, publication, or version state. Fields such as
`provider_policy_version`, `availability_rule_version`, `calculation_version`,
`schema_version`, `model_version`, and `published_at` are allowed. They explain
what logic produced a row or when information became available; they do not
point to another record.

Natural-key scope is file-local and each writer declares its exact key. Paths
can supply context that is constant for the whole file. For example, a
normalized bar under `stocks/NVDA/bars/1h/databento/normalized` uses
`timestamp`, while newer append-immutable feature families repeat their
symbol/provider context in the key so independently written partitions retain
an explicit grain.

## Readable ID recipes

Current writers use these recipes:

| Current writer or artifact | Declared natural columns | Example `id` |
| --- | --- | --- |
| normalized bar | `timestamp` | `2026-07-29T18:00:00Z` |
| legacy market-regime or breakout-pressure technical row | `timestamp` | `2026-07-29T18:00:00Z` |
| bar-shape or weekly-context technical row | `symbol`, `provider`, `timeframe`, `bar_timestamp` | `NVDA\|databento\|1d\|2026-07-29T20:00:00Z` |
| legacy fundamental-direction row | `period_end_date`; add `fiscal_period` only when needed for uniqueness | `2026-06-30T00:00:00Z` |
| point-in-time fundamental row | `symbol`, `period_type`, `period_end_date`, `available_at` | `NVDA\|quarterly\|2026-06-30T00:00:00Z\|2026-07-28T20:05:00Z` |
| legacy lifecycle signal row | `timestamp` | `2026-07-29T20:00:00Z` |
| technical-lifecycle row | `symbol`, `timestamp`, `available_at`, `calculation_version`, `provider_policy_version` | `NVDA\|2026-07-29T20:00:00Z\|2026-07-29T20:05:00Z\|1.0.0\|databento-canonical-only-v1` |
| Schwab raw option snapshot or option-quality feature row | `symbol`, `snapshot_for`, `available_at` | `NVDA\|2026-07-29T20:00:00Z\|2026-07-29T20:01:00Z` |
| Schwab normalized option contract | `symbol`, `snapshot_for`, `available_at`, `contract_symbol` | `NVDA\|2026-07-29T20:00:00Z\|2026-07-29T20:01:00Z\|NVDA  260821C00150000` |
| Schwab quote-liquidity row | `symbol`, `available_at` | `NVDA\|2026-07-29T20:01:00Z` |
| CME cross-asset context row | `context_name`, `window_end`, `calculation_version` | `continuous-cross-asset-1h\|2026-07-29T20:00:00Z\|1.0.0` |
| FRED vintage row | `series_name`, `observation_date`, `realtime_start` | `CPIAUCSL\|2026-06-01\|2026-07-01` |
| macro release-context row | `context_name`, `available_at`, `calculation_version` | `fred-release-context\|2026-07-29T20:05:00Z\|1.0.0` |
| SEC filing-event row | `symbol`, `filing_accepted_at`, `event_type`, `available_at` | `NVDA\|2026-07-29T20:00:00Z\|offering\|2026-07-29T20:05:00Z` |
| FMP energy-context row | `canonical_instrument`, `provider_instrument`, `available_at`, `calculation_version` | `WTI\|CLUSD\|2026-07-29T20:05:00Z\|1.0.0` |
| Databento MBP event, raw or normalized | usual key: `symbol`, `ts_event`, `sequence`, `action`, `side`, `depth`, `price`; add `size`, `flags`, or another readable event value only for a real collision | `NQ.v.0\|2026-07-29T17:01:22.486829957Z\|359962893\|T\|A\|0\|27620.5` |
| ML sample | `symbol`, `horizon`, `decision_timestamp` | `NVDA\|1d\|2026-07-29T20:05:00Z` |
| prediction | `symbol`, `horizon`, `decision_timestamp`, `prediction_created_at` | `NVDA\|1d\|2026-07-29T20:05:00Z\|2026-07-29T21:00:00Z` |
| evaluation | same four columns as its prediction | `NVDA\|1d\|2026-07-29T20:05:00Z\|2026-07-29T21:00:00Z` |
| monitoring value | `metric_name`, `scope_type`, `scope_value`, `monitored_at` | `mean_log_loss\|global\|all\|2026-07-29T21:00:00Z` |
| current intelligence | `symbol`, `horizon`, `decision_timestamp` | `NVDA\|1d\|2026-07-29T20:05:00Z` |
| strategy candidate | `symbol`, `horizon`, `decision_timestamp`, `candidate_key` | `GOOG\|1d\|2026-08-01T15:00:00Z\|long_call\\\|w1\\\|front=2026-09-18\\\|back=none` |
| strategy audit | `symbol`, `horizon`, `decision_timestamp`, `strategy_name` | `GOOG\|1d\|2026-08-01T15:00:00Z\|long_call` |

The same recipes apply to `4h`, for example:

```text
GOOG|4h|2026-07-29T18:05:00Z
GOOG|4h|2026-07-29T18:05:00Z|2026-07-29T18:10:00Z
```

Because `horizon` is a natural-key component, those rows cannot collide with
`GOOG|1h|...`, `GOOG|1d|...`, or `GOOG|1w|...`. The same rule distinguishes
aggregate `1w` from `1w-d1`, `1w-d2`, `1w-d3`, `1w-d4`, and `1w-d5`; no lead,
component, snapshot, or weekly issuance identifier is needed.

The separator is `|`. A literal backslash or pipe in a source value is escaped,
so the result remains readable without losing the original value.

For a strategy candidate, `candidate_key` is itself readable:
`<strategy>|w<width>|front=<YYYY-MM-DD>|back=<YYYY-MM-DD-or-none>`.
Its internal separators appear as `\|` inside the final `id`, as shown above.
The exact contract graph in `legs_json` is not part of the ID. It is an
ordinary value used to reproduce analytics and draft an order; treating it as
a hidden content identity is forbidden.

Calculated and Loop B writers reject incomplete or non-unique declared natural
keys. Immutable append writers also reject conflicts with persisted rows; full
current-rebuild writers atomically replace the complete output. Provider writers
do not fail a successful fetch merely because an undocumented provider shape
does not match a local ID recipe: they extend the readable key and finally use a
deterministic file-local ordinal. Databento MBP responses are deduplicated only
when provider rows are exact duplicates, before Duckets adds fetch metadata.
Distinct book events remain separate even when they share timestamps and venue
sequence numbers. `NO CURRENT ROWS` records use a separate `*_status`
latest-snapshot Parquet, so an empty provider window cannot change previously
persisted events.

## Before and after: normalized bar

The old wide form mixed data with several descriptions of the same row:

```text
Before
------
observation_id
instrument_id
source_snapshot_id
lineage_hash
timestamp
open
high
low
close
volume
provider
timeframe
```

The normalized file now stores one readable row key and the actual values:

```text
After
-----
id          string
timestamp   timestamp[ns, UTC]
open        float64
high        float64
low         float64
close       float64
volume      float64
```

Example:

```text
id = 2026-07-29T18:00:00Z
```

Symbol, provider, timeframe, and normalized scope are already readable from the
path. Provider-native Databento fields remain in raw data and are not copied into
this normalized schema.

## Before and after: technical metrics

The old form assigned identities to the observation, feature collection, source,
and calculation:

```text
Before
------
observation_id
feature_set_id
source_snapshot_id
calculation_receipt_id
timestamp
atr_14
trend_score
volatility_ratio
breakout_readiness_score
compression_score
```

The calculated row now keeps one ID and ordinary value columns:

```text
After
-----
id
timestamp
bar_end_timestamp
symbol
provider
timeframe
calculation
calculation_version
atr_14
trend_score
volatility_ratio
breakout_readiness_score
compression_score
...other calculated values and readable statuses
```

Example:

```text
id = 2026-07-29T18:00:00Z
```

## Before and after: ML sample

The old sample shape carried separate identities for nearly every concept:

```text
Before
------
sample_id
observation_id
instrument_id
feature_set_id
sample_set_id
source_snapshot_id
source_publication_id
target_definition_id
horizon_specification_id
split_plan_id
decision_timestamp
feature values
target values
```

The current sample base is:

```text
After
-----
id
symbol
venue
currency
provider
timeframe
exchange_calendar
exchange_session
horizon
bar_timestamp
bar_end_timestamp
decision_timestamp
information_available_at
target_window_start
target_window_end
actionable_until
label_available_at
target_definition_version
target_specification
target_open
target_close
forward_raw_return
forward_cost_adjusted_return
target_cost_adjusted_positive
label_status
label_exclusion_reason
previous_period_direction
assumed_round_trip_cost
...explicit feature value columns
```

`feature_available_at` exists only while Loop B assembles and validates a
sample, then is deliberately discarded because it is contractually equal to
the decision timestamp. `feature_computed_at` and a row-level `materialized_at`
are not carried into the sample frame at all; computation timing remains source
metadata and the run timestamp remains canonical in the JSON manifest. Loop A's cycle generation and
`WRITING`/`COMPLETE`/`FAILED` state likewise remain exclusively in the atomic
JSON control file and never become Parquet columns.

Example:

```text
id = NVDA|1d|2026-07-29T20:05:00Z
```

Features are named for their values, such as `mr__trend_atr` and
`bp__compression_score`. The readable `horizon` column states which target
window is in use.

## Before and after: prediction

The old prediction shape carried multiple interchangeable keys and run
receipts:

```text
Before
------
prediction_publication_id
canonical_decision_id
sample_id
model_id
model_publication_id
feature_set_id
target_definition_id
runtime_config_id
symbol
horizon
decision_timestamp
prediction_created_at
raw_probability
calibrated_probability
```

The explicit current prediction schema is:

```text
After
-----
id
symbol
provider
horizon
decision_timestamp
information_available_at
target_window_start
target_window_end
actionable_until
target_definition_version
target_specification
prediction_created_at
model_name
model_version
calibration_method
prediction_mode
prediction_status
assumed_round_trip_cost
raw_probability
calibrated_probability
```

Example:

```text
id = NVDA|1d|2026-07-29T20:05:00Z|2026-07-29T21:00:00Z
```

`model_name`, `model_version`, `calibration_method`, and `prediction_mode` say
what produced the value. `model_version` is the readable UTC timestamp name of
the trained model artifact directory; it is version evidence, not an opaque
model identity or join key.

## Before and after: evaluation

The old evaluation shape propagated the prediction keys and added policy,
route, unit, and outcome identifiers.

The current evaluation schema is:

```text
id
symbol
provider
horizon
decision_timestamp
target_window_start
target_window_end
prediction_created_at
evaluated_at
model_name
model_version
prediction_mode
evaluation_status
target_definition_version
target_specification
assumed_round_trip_cost
observed_target
observed_forward_raw_return
observed_forward_cost_adjusted_return
raw_probability
calibrated_probability
raw_log_loss
log_loss
raw_brier_score
brier_score
prediction_correct_0_5
```

The prediction and evaluation share their four-column natural grain. A target
candidate is selected by `symbol`, `horizon`, and `decision_timestamp`, but
scoring also requires exact target-definition version and serialized
specification, exact target-window timestamps, and matching
`assumed_round_trip_cost`. A live prediction created at or after target entry is
not scoreable. Unscored rows remain readable as `PENDING`,
`TARGET_CONTRACT_MISMATCH`, `TARGET_WINDOW_MISMATCH`,
`CONFIGURATION_MISMATCH`, `INVALID_PREDICTION`, or `POST_ENTRY_PREDICTION`.

Repeated eligible live snapshots remain distinct evaluation rows. Monitoring
and live evidence canonicalize them to the earliest eligible
`prediction_created_at` per
`symbol`, `horizon`, and `decision_timestamp`.
A completed LIVE forecast also requires a complete verified run manifest and
output inventory; when the run declares the transactional publication
contract, its `publication.json` receipt must be valid and bound to that
manifest, and the run must be reachable through the authoritative pointer's
publication chain.

The dynamic weekly path reuses the exact origin prediction rows for the same
daily decision. Because `prediction_created_at` remains unchanged at that
natural grain, deduplication keeps one prediction/evaluation event and one
possible evidence contribution per internal weekly route. A newer completed
daily decision has a different natural key and may publish a shorter aggregate
plus Day 1 prefix. Every reused origin must be reachable through the verified
receipt chain and must have committed before its applicable session-close
deadline.

## Strategy candidate and audit schemas

`ml.parquet_contracts.STRATEGY_CANDIDATE_SCHEMA` has this exact physical order:

```text
id:string
symbol:string
horizon:string
decision_timestamp:timestamp[ns, UTC]
information_available_at:timestamp[ns, UTC]
target_window_start:timestamp[ns, UTC]
target_window_end:timestamp[ns, UTC]
entry_available_at:timestamp[ns, UTC]
strategy_name:string
strategy_display_name:string
strategy_family:string
candidate_key:string
account_approval:string
authorization_status:string
construction_status:string
risk_form:string
expiration_structure:string
stock_requirement:string
cash_requirement:string
lifecycle:bool
front_expiration:timestamp[ns, UTC]
back_expiration:timestamp[ns, UTC]
front_days_to_expiration:float64
back_days_to_expiration:float64
target_elapsed_hours:float64
width_steps:int64
leg_count:int64
legs_json:string
underlying_price:float64
entry_cash_flow:float64
entry_fees:float64
entry_net_credit:float64
entry_net_debit:float64
max_profit:float64
max_loss:float64
capital_required:float64
risk_calculation_status:string
net_delta:float64
net_gamma:float64
net_theta:float64
net_vega:float64
mean_relative_spread:float64
max_relative_spread:float64
minimum_open_interest:float64
total_volume:float64
entry_debit_to_underlying:float64
max_loss_to_underlying:float64
net_delta_per_share:float64
surface_quality_pass:bool
all_option_quotes_valid:bool
liquidity_policy_pass:bool
stock_quote_quality_pass:bool
maximum_quote_staleness_seconds:float64
quality_observations_json:string
market_expected_absolute_move:float64
market_expected_realized_volatility:float64
market_uncertainty:float64
market_trend_persistence:float64
market_mean_reversion_tendency:float64
raw_profit_probability:float64
calibrated_profit_probability:float64
direction_probability_up:float64
direction_alignment:float64
expected_net_profit:float64
expected_return_on_risk:float64
decision_score:float64
candidate_rank:int64
model_version:string
model_status:string
registry_version:string
candidate_policy_version:string
model_policy_version:string
ranking_policy_version:string
```

`ml.parquet_contracts.STRATEGY_AUDIT_SCHEMA` has this exact physical order:

```text
id:string
symbol:string
horizon:string
decision_timestamp:timestamp[ns, UTC]
strategy_name:string
strategy_display_name:string
strategy_family:string
account_approval:string
authorization_status:string
construction_status:string
candidate_count:int64
reason:string
registry_version:string
candidate_policy_version:string
```

All fields are nullable at the Arrow layer so an exact empty schema and
explicitly unavailable measurements can be published. Prior-ranked rows carry
the five market-state fields when their causal inputs are available and fill
raw probability, expected profit/return, score, and rank; calibrated probability
remains null until a compatible GOOG strategy model has been fitted and
calibrated. The readable-ID
validator still requires every `id` in a non-empty file to be present, unique,
and non-opaque. `write_parquet_with_schema` rejects extra fields, fills omitted
declared fields with null, coerces them to the exact Arrow types, and writes
them in the order above. `verify_parquet_schema` requires exact field order and
types before the Options Strategies reader adapts any row.

`candidate_key`, provider contract symbols inside `legs_json`, model versions,
and policy versions are readable data values. None is an additional identity
column. In particular, `legs_json` can reproduce the exact expiration, strike,
type, quantity, BBO, multiplier, Greeks, liquidity, and receipt graph, but its
serialized content is not hashed, duplicated as an ID, or used to choose a
publication.

## Other explicit ML schemas

Monitoring rows contain:

```text
id
monitored_at
category
metric_name
scope_type
scope_value
status
observed_value
reference_value
unit
evidence_row_count
window_start
window_end
details
```

Monitoring uses `scope_type = global` for all-route values, `horizon` for one
configured horizon, `live_horizon` for pooled matured live-only performance,
and `symbol_horizon` for route-specific `completed_live_forecasts`.
The route scope value is `SYMBOL|horizon`; pooled values use `all`, `1h`,
`4h`, `1d`, `1w`, or one of the five weekly component horizons as appropriate.
`reference_value` records a comparison threshold such as the minimum completed
live decisions, the `0.05` calibration-gap warning boundary, or the `0.5`
chance-level ROC AUC. These are ordinary monitoring values and do not create
additional identifiers.

Current intelligence rows contain:

```text
id
symbol
horizon
decision_timestamp
forecast_created_at
information_available_at
target_window_start
target_window_end
actionable_until
target_definition_version
probability_up
probability_down
actionability_status
operational_status
model_evidence_status
live_evidence_status
intelligence_status
model_name
completed_decision_count
minimum_live_decision_count
automated_action_allowed
limitations
schema_version
```

`ml/parquet_contracts.py` defines and enforces these physical Arrow schemas.

The remaining-week outlook continues to use only `samples.parquet`,
`predictions.parquet`, `evaluations.parquet`, `monitoring.parquet`, and
`intelligence.parquet`. It adds no Parquet column, dataset type, state file,
pointer, acknowledgement, or coordination record.

The separate Options Strategies screen reads `strategy-candidates.parquet`.
When the default predictable path is requested and `ml/latest/run.json` exists,
the reader resolves that artifact through the authoritative pointer and the
verified immutable run manifest. It does not select a directory still being
written or treat the `ml/latest` mirror as a generation boundary. The fresh
Schwab account snapshot and `current-schwab-position-fit-v1` overlay are not
written back to Parquet.

The physical `horizon` field is already a string, so aggregate `1w` and
`1w-d1` through `1w-d5` require no schema change. Current intelligence is
`one-id-v2` because the target
contract migration adds `target_definition_version` and route-scoped evidence
adds `minimum_live_decision_count`. A complete three-symbol publication selected with
public `1h 4h 1d 1w` contains 27 rows: three non-weekly plus six weekly rows per
symbol, all with the same `symbol|horizon|decision_timestamp` ID recipe. That is
a schema example, not a deployment claim.

For non-weekly current intelligence, `OPERATIONALLY_CURRENT` requires an
actionable current route. A ready non-weekly route without a current actionable
prediction is `OPERATIONALLY_STALE`. A verified weekly route remains current
while its remaining-week snapshot is published. Aggregate `1w` and `d1` expire
at the first remaining session close; later components expire at their own
session closes.

## Timestamped run and model directories

Run outputs use a creation timestamp:

```text
DATASTORE/ml/runs/
└── 20260729T184512.123456Z/
    ├── samples.parquet
    ├── predictions.parquet
    ├── evaluations.parquet
    ├── monitoring.parquet
    ├── intelligence.parquet
    ├── strategy-candidates.parquet
    ├── strategy-audit.parquet
    ├── manifest.json
    └── publication.json
```

Compatibility/convenience mirrors use predictable paths:

```text
DATASTORE/ml/latest/
├── samples.parquet
├── predictions.parquet
├── evaluations.parquet
├── monitoring.parquet
├── intelligence.parquet
├── strategy-candidates.parquet
├── strategy-audit.parquet
└── run.json
```

Models use:

```text
DATASTORE/ml/models/
└── <horizon>/
    └── <model-name>/
        ├── <trained UTC timestamp>/
        │   ├── model.joblib
        │   └── manifest.json
        └── latest.json
```

Options-strategy models use the parallel readable hierarchy:

```text
DATASTORE/ml/strategy-models/
└── <horizon>/
    └── market-state-strategy-outcome/
        ├── <trained UTC timestamp>/
        │   ├── model.joblib
        │   └── manifest.json
        └── latest.json
```

Directory names never use content digests, generated publication values, or
row IDs. `latest.json` is a readable model-path pointer.
The corresponding model contract is `market-state-hgb-platt-return-v3`; the
market-state and prior policies are `point-in-time-market-state-v1` and
`greek-bbo-scenario-prior-v1`. These version strings are ordinary compatibility
metadata, never identity columns.
`ml/latest/run.json` is the single authoritative current-view commit pointer;
official readers resolve all immutable current artifacts from its run path.
The predictable Parquets shown above, plus
`ml-intelligence/latest/rolling-predictions.parquet`, are mirrors and are not
publication authority. New pointers declare
`current-output-pointer-v1`.

`publication.json` is not a row identity or join key. It is the current-output
transaction receipt, bound to the run manifest checksum and linked to the
preceding committed receipt-era publication, if any, under contract
`current-output-authoritative-pointer-v2`. Loop B prepares and verifies it
before committing the pointer; it becomes publication-valid only through that
reachability. A failed working run may have a complete manifest or an orphaned
prepared receipt, but it is not reachable through the authoritative
publication chain.

## Manifest and checksum constraints

A run manifest may contain:

- run timestamp;
- readable input file paths and basic file metadata;
- readable output filenames, sizes, and integrity checksums;
- model name;
- feature columns;
- target column;
- symbols and horizons;
- configuration values;
- readable route errors.

A model manifest may additionally contain the readable training boundary, row
counts, requested and effective calibration methods, estimator parameters,
optional calibrator parameters, and `model.joblib` integrity metadata. It records
Python implementation and major/minor version plus package versions for NumPy,
pandas, PyArrow, scikit-learn, joblib, exchange-calendars, LightGBM, and XGBoost;
these values are checked before joblib reuse.

For a logistic aggregate `1w` model, `model_parameters` records the
route-specific L1 policy (`C=0.3`, `l1_ratio=1.0`, `solver=liblinear`,
`max_iter=5000`, and `tol=1e-5`). When that route requests Platt calibration,
`calibration_parameters` records `platt_regularization_c=0.1` and
`clip_to_observed_probability_range=true`. Those JSON values are ordinary
configuration and compatibility evidence, not Parquet identities. The five
`1w-d1` through `1w-d5` component manifests retain the default estimator and
calibration parameters.

The manifest's offline assessment block records its date range and row count,
explicitly states that assessment was used for neither training nor calibration,
and stores raw/calibrated log loss, Brier score, 0.5-threshold accuracy, ROC AUC,
training- and calibration-base-rate comparisons, and prior-period-direction
accuracy when available. Its `calibration_support` block records the calibration
partition's minimum and maximum raw probabilities, assessment counts below,
above, and anywhere outside that range, and whether the fitted calibrator clips
to the observed range. These are JSON evaluation values; they do not add a
Parquet column or row identifier.

Its closed-lockbox block contains only the
`CLOSED_UNTOUCHED_UNSCORED` status, row and target-cluster counts, and the
first/last `target_window_start` bounds. A target cluster is one distinct
`target_window_start` shared by one or more samples. Lockbox targets are never
coerced, read, predicted, or scored, and no lockbox target values or performance
metrics are written to model JSON. Closed-lockbox rows are also omitted from
published `samples.parquet`. Verified prior-LIVE target starts are excluded
from later offline partitions so reconciliation, rather than model-time
assessment or lockbox assignment, matures those outcomes.

Checksums are permitted only to detect an incomplete or corrupted file. A
checksum must never be:

- a Parquet column;
- an `id` value;
- a directory name;
- a join key;
- a model or run name;
- an input to another identity value.

## Provider-native identity fields

Raw provider payloads preserve provider-native `*_id`, UUID, checksum, digest,
and hash fields without a source-specific allowlist. These values are evidence
supplied by an external provider. Duckets does not generate them, does not use
them as its leading `id`, and does not copy them into normalized, calculated,
model, prediction, evaluation, monitoring, or intelligence Parquets.

`FredSeriesSpec.series_id` remains a request-configuration field in production
code, not a persisted Parquet column. FRED Parquets use readable series,
endpoint, and path context.

If a raw provider table itself uses the reserved name `id`, the raw writer
preserves that value as `provider_native_identifier` before creating the
Duckets `id`. This readable name is not an additional `*_id` field.

There are no other Duckets-generated persisted `*_id` or `*_ids` fields.

## Enforcement

The contract is enforced at three levels:

1. shared provider persistence adaptively resolves readable identity while
   explicit calculated and Loop B schemas reject invalid declared grains;
2. explicit Arrow schema builders reject forbidden identity-shaped columns;
3. repository tests inspect every explicit persisted Parquet schema and exercise
   a clean temporary datastore through one Loop A and one Loop B cycle.

The tests also prove:

- readable non-hash IDs;
- natural timestamp joins without observation or sample IDs;
- raw provider-native exceptions;
- Loop A fetching, normalization, fundamentals, technicals, and signals;
- Loop B targets, model fit/reuse, predictions, evaluations, exact-chain
  strategy candidates, and per-strategy audits;
- exact candidate/audit schemas, readable natural keys, and rejection of
  duplicate or opaque identity columns;
- `--once`, process locking, and clean supervisor shutdown.

## Test commands

Commands to run from the repository root:

```powershell
pytest -q

pytest -q `
  tests/test_parquet_id_contract.py `
  tests/test_non_ml_parquet_ids.py `
  tests/test_loop_a_integration.py `
  tests/test_loop_a_orchestration.py `
  tests/test_ml_prediction_runtime.py `
  tests/test_ml_calibration.py `
  tests/test_ml_required_feature_preprocessing.py `
  tests/test_ml_weekly_context_model_runtime.py `
  tests/test_ml_runtime_pipeline.py `
  tests/test_ml_strategy_selection.py `
  tests/test_options_strategy_ui.py
```

Both commands must exit successfully. Pass counts, warning totals, and timing are
intentionally not pinned here because they change as the suite and dependency
versions evolve; review the output from the current checkout.
