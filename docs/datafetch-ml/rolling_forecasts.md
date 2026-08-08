# Rolling 1h, 4h, 1d, and dynamic remaining-week forecasts

Implementation snapshot: 2026-08-05

Loop B exposes four public selections in canonical order:

- `1h`: direction across the next 60 calendar-selected eligible regular-session
  minutes;
- `4h`: direction across the next 240 calendar-selected eligible
  regular-session minutes;
- `1d`: direction of the next eligible regular session;
- `1w`: one dynamic remaining-week outlook.

Public `1w` expands internally to six ordinary values in the existing `horizon`
column: aggregate `1w` and components `1w-d1` through `1w-d5`. Operators select
only `1w`; they do not list six values. All routes use the same runtime, natural
timestamp joins, timestamped run storage, single-target model pipeline,
prediction schema, and evaluation rules. `1h`, `4h`, and `1d` retain their
existing behavior.

The compatibility consumer file is:

```text
DATASTORE/ml-intelligence/latest/rolling-predictions.parquet
```

## Shared actionability rule

A live prediction is actionable exactly when:

```text
information_available_at <= prediction_created_at < actionable_until
ordinary routes: actionable_until <= target_window_start
remaining-week routes: actionable_until <= target_window_end
```

Equality at `actionable_until` is too late. For `1h`, `4h`, and `1d`, that
deadline equals target entry. For a remaining-week snapshot, aggregate `1w`
and `d1` expire at the first remaining session close; each later component
expires at its own session close. This permits a same-session forecast before
the official close while still rejecting equality at the deadline.

Actionability and visibility are separate after ordinary target entry. When a
fresh forecast cannot be produced, Loop B may carry the latest original
`LIVE`/`CREATED` forecast for a route only from the verified authoritative
receipt chain. The original issuance and promotion must both precede its entry
deadline, its current target contract/serialized specification/exact window and
cost must still match, and the new publication time must be inside the target
window. Current fresh rows supersede prior rows. Orphan, invalid-receipt,
`BACKTEST`, post-entry, incompatible, and expired rows are rejected.

The carried row retains its original `prediction_created_at` and is published
as `TARGET_WINDOW_STARTED` plus `FORECAST_IN_PROGRESS`, with
`automated_action_allowed = false`. It remains visible only until the strict
`target_window_end`; it is never relabeled `ACTIONABLE`. Republishing the same
original row does not create another issuance or increase live-evidence counts.

Each sample also records:

```text
decision_timestamp
information_available_at
target_window_start
target_window_end
actionable_until
label_available_at
```

These timestamps are values, not separate timing or decision identifiers.

## 1h route

| Setting | Rule |
| --- | --- |
| source timeframe | completed canonical `1h` bars; native preferred, complete `1m`-derived continuity fallback |
| target definition | `next-60-eligible-regular-minutes-open-close-v2` |
| information available | source-bar end plus five minutes |
| target start | first calendar candidate strictly after information availability |
| target end | end of the 60th predetermined eligible regular-session minute |
| target prices | adjusted native Databento `1m`; first minute open through final minute close |
| return | first-open-to-final-close simple return, including intervening price gaps |
| label | configured round-trip cost subtracted once; adjusted return strictly greater than zero |
| action deadline | strictly before `target_window_start` |
| label maturity | `target_window_end + 5 minutes` |

The calendar policy is
`session-open-break-resume-plus-full-local-clock-anchor-v1`. Each continuous
regular-session segment contributes its exact start (the official open or a
post-break resume) and each complete exchange-local clock-hour start contained
in that segment. Thus a prior-session decision on ordinary XNAS targets the
next official `09:30 ET` open; its 60-eligible-minute target is `09:30-10:30`, never the
partial `09:30-10:00` opening fragment. Ordinary intraday decisions retain the
safe next full-local-clock anchor.

## 4h route

| Setting | Rule |
| --- | --- |
| target definition | `next-240-eligible-regular-minutes-open-close-v2` |
| source timeframe | completed canonical `1h` bars; native preferred, complete `1m`-derived continuity fallback; no synthetic `4h` bars |
| decision cadence | after every completed eligible `1h` source bar |
| information available | source-bar end plus five minutes |
| target selection | choose the same versioned calendar candidate, then predetermine exactly 240 eligible regular-session minute timestamps before price lookup |
| target start | first selected minute, strictly after information availability |
| target end | end of the 240th selected eligible minute |
| target prices | adjusted native Databento `1m`; first minute open through final minute close |
| return | first-open-to-final-close simple return, including intervening price gaps |
| label | configured round-trip cost subtracted exactly once; positive only when the adjusted return is strictly greater than zero |
| action deadline | `actionable_until = target_window_start` |
| label maturity | `target_window_end + 5 minutes` |

Equality at the selected start is too late. Breaks and closures pause eligible
minute accumulation, while the return still includes the price gap from one
eligible minute to the next. The calendar owns early closes, breaks, overnight
closures, weekends, holidays, and DST; fixed-duration UTC arithmetic does not.

Every one of the predetermined 60 or 240 eligible native-minute records must
exist.
Missing the first, a middle, or the final constituent keeps the original
window and leaves the label incomplete; no later price is substituted. The
completed canonical `1h` source bar supplies `previous_period_direction`, while
the native `1m` target prices and returns never enter model features.

## 1d route

| Setting | Rule |
| --- | --- |
| source timeframe | completed `1d` bars |
| information available | official session close plus processing delay |
| target start | next eligible session open |
| target end | next eligible session close |
| return | next-session open-to-close simple return |
| label | cost-adjusted return strictly greater than zero |
| calendar | regular exchange sessions |
| action deadline | strictly before next eligible session open |

Weekends and exchange holidays are skipped by the calendar rather than handled
with fixed clock arithmetic.

## Dynamic remaining-week family

| Setting | Rule |
| --- | --- |
| source timeframe | completed `1d` bars |
| historical training cadence | one candidate after every completed eligible daily decision |
| weekly context | most recently completed exchange week, joined backward as-of by its actual availability |
| internal horizons | aggregate `1w`; `1w-d1` through `1w-d5` |
| aggregate target version | `dynamic-remaining-week-aggregate-open-close-v2` |
| component target versions | `dynamic-remaining-week-d1-open-close-v2` through `dynamic-remaining-week-d5-open-close-v2` |
| daily targets | each selected eligible session's official open through official close |
| aggregate target | Day 1 official open through the final eligible close of Day 1's exchange week |
| return | simple open-to-close return for each route |
| label | configured round-trip cost subtracted once; adjusted return strictly greater than zero |
| calendar | next five eligible exchange sessions are resolved; LIVE publication keeps the Day 1 prefix in one exchange week |
| LIVE issuance | from the latest completed daily decision plus processing delay |
| action deadline | aggregate `1w` and `d1` expire at the first remaining session close; later components expire at their own closes |

Every eligible completed daily decision produces six historical training
candidates. `1w-d1` through `1w-d5` use the first through fifth future eligible
sessions. Aggregate `1w` uses the first target session's open and the final
eligible close in that target's exchange week, so its historical target is
also dynamic. These are six independent single-target models; no predicted day
is recursively fed into another.

The calendar supplies actual sessions rather than assuming Monday through
Friday. After Friday, July 31, 2026, Day 1 through Day 5 are August 3 through
August 7 and aggregate `1w` spans Monday's official open through Friday's
official close. After Monday, August 3 closes, the next snapshot instead spans
Tuesday through Friday and publishes `1w` plus `d1` through `d4`. After
Wednesday closes it spans Thursday through Friday and publishes `1w`, `d1`,
and `d2`. A holiday shortens the prefix rather than pushing the aggregate into
the following exchange week. Early-close and UTC offsets come from the same
exchange schedule.

The components mature independently at their respective close plus the
existing processing delay. Aggregate `1w` matures after its dynamic final
close plus that delay. Until maturity, target values remain null and evidence
is pending.

Each symbol's latest usable completed daily decision can issue a LIVE
remaining-week snapshot while its first target session has not closed. The
aggregate and matching `d1` prefix are selected per symbol, so symbols first
fetched on Tuesday and Wednesday can legitimately publish different snapshot
shapes in one run. Prior exact rows may be reused for the same decision, but
receipt history and a complete six-route bundle are not prerequisites. When no
usable snapshot exists, that symbol's weekly LIVE rows are omitted until a
newer daily decision is available.

## Features

The default profile is `loop-a-all-v1`. It selects four active, ordered feature
inventories registered as version `1.2.0`; every internal weekly route uses the
same existing 132-column weekly inventory:

| Horizon | Feature set | Ordered model columns | Family counts |
| --- | --- | ---: | --- |
| `1h` | `loop-a-all-v1-1h` | 69 | market regime (`mr`) 13; breakout pressure (`bp`) 13; bar shape 2; technical lifecycle 5; quote 1; options 26; energy 1; CME 8 |
| `4h` | `loop-a-all-v1-4h` | 69 | market regime (`mr`) 13; breakout pressure (`bp`) 13; bar shape 2; technical lifecycle 5; quote 1; options 26; energy 1; CME 8 |
| `1d` | `loop-a-all-v1-1d` | 139 | `mr` 13; `bp` 13; bar shape 3; weekly context 3; technical lifecycle 5; fundamental direction 25; point-in-time fundamentals 13; fundamental-technical lifecycle 17; quote 1; options 32; energy 1; macro 4; SEC 3; CME 6 |
| `1w`, `1w-d1` ... `1w-d5` | `loop-a-all-v1-1w` | 132 | `mr` 14; `bp` 12; bar shape 3; weekly context 3; technical lifecycle 5; fundamental direction 25; point-in-time fundamentals 13; fundamental-technical lifecycle 17; options 29; macro 4; SEC 3; CME 4 |

The compatibility profiles are:

- `production-v1`, which selects the legacy 19-column `technical-all` set for
  `1h`, `1d`, and `1w` and its ordered horizon-scoped clone
  `technical-all-4h` for `4h`;
- `technical-all-v2`, which selects the horizon-specific
  `technical-all-v2-1h`, `technical-all-v2-4h`,
  `technical-all-v2-1d`, and `technical-all-v2-1w` sets.

The public runtime accepts only those closed profiles. It does not infer
features from every numeric field or accept an arbitrary feature-set name.

The `1h` and `4h` routes use the same `1h` technical inputs and source cache;
Loop A does not store a `4h` bar. The `1d` and `1w` routes use `1d` technical
inputs. Core market-regime and breakout-pressure values are aligned
exactly by symbol, provider, timeframe, bar timestamp, exchange session, and
adjusted-price basis. The registry validates their calculation names, versions,
modes, and completed-bar timing. Loop B also compares each current technical
file's `price_adjustment_status` and `split_event_count` with the adjusted bars;
a missing or different value rejects the route.

`loop-a-all-v1-4h` contains exactly the same ordered 69 model values and family
composition as `loop-a-all-v1-1h`, but its definitions are scoped to `4h`
rather than widening the existing set. Its market-regime, breakout-pressure,
and bar values keep the current exact decision/as-of behavior. Freshness is two
days for technical lifecycle, five minutes for quote, two hours for options,
30 minutes for energy, and 15 minutes for CME. The existing `1h`, `1d`, and
`1w` semantic fingerprints therefore remain unchanged.

For additional families, the operational wiring is deliberately explicit:

- bar-shape, weekly-context, and SEC use specialized family loaders;
- technical lifecycle, fundamental direction, point-in-time fundamentals,
  fundamental-technical lifecycle, quote liquidity, and option quality use the
  generic symbol-scoped backward-as-of adapter;
- energy, FRED macro, and CME cross-asset context use the generic shared-context
  backward-as-of adapter.

The generic adapters enforce `available_at <= decision_timestamp`, apply
configured freshness where present, and keep the last same-availability row
after deterministic ordering. Stricter family helper APIs in the repository do
not all gate this runtime path.

Every family applicable to the selected profile must have its required non-empty
Parquet source for every requested symbol and its required columns. Families
routed through the generic adapters must also have at least one populated
numeric family value. Missing physical input makes the route non-ready, which
aborts the whole runtime cycle. A present source can still be missing for an
individual decision because there is no prior publication, the latest value is
stale, or a specialized loader rejects that row's quality. Those per-row feature
values remain null and are handled by training-fitted imputation and missing
indicators; Loop B does not substitute a source from another horizon.

FRED macro is current-receipt context, not historical vintage reconstruction.
Loop B derives one row from normalized FEDFUNDS, CPIAUCSL, UNRATE, and GDP
observation histories and uses the maximum selected `fetched_at` as
`available_at`. Decisions before that receipt have null macro features.

`samples.parquet` uses the ordered union of the active features for all selected
horizons so the run has one Arrow schema. Rows have nulls for columns that do not
belong to their horizon. Each horizon model nevertheless projects only its exact
ordered 69-, 69-, 139-, or 132-column subset; the six weekly models project the
same 132 columns without discovering or adding feature values. The union is
never passed wholesale to an estimator.

## Targets

For each route:

```text
forward_raw_return = target_close / target_open - 1

forward_cost_adjusted_return
    = forward_raw_return - assumed_round_trip_cost

target_cost_adjusted_positive
    = 1 when forward_cost_adjusted_return > 0, otherwise 0
```

The default round-trip cost is `0.001`. Change it with
`--round-trip-cost`. The readable `assumed_round_trip_cost` value is persisted
on samples, predictions, and evaluations so reconciliation can verify that the
prediction and observed target used the same configuration.

Before label maturity, target prices, returns, and the binary target remain null.
The row stays in `samples.parquet` with `label_status = INCOMPLETE_LABEL`, making
the future window visible without leaking its outcome.

## Readable row grains

Samples and current intelligence use:

```text
id = symbol|horizon|decision_timestamp
```

Predictions and evaluations can contain more than one forecast creation time for
one decision, so they use:

```text
id = symbol|horizon|decision_timestamp|prediction_created_at
```

All elements remain ordinary readable columns. The six weekly horizon strings
and existing timestamps distinguish the rows; there is no `lead_index`,
component ID, weekly issuance ID, or separate ID for symbols, horizons, target
definitions, feature sets, models, decisions, or forecast snapshots. The weekly
change adds no Parquet file type or schema column.

## Model partitions

Routes are materialized per symbol and horizon, but fitting is pooled. Loop B
concatenates all configured symbols for one horizon and fits or reuses one
horizon-wide model. The model then produces assessment predictions and the
latest eligible live prediction separately for each symbol. There is no
per-symbol estimator.

Only complete targets enter partitioning. The partition unit is a target
cluster: all samples with one distinct `target_window_start`. Loop B orders
these clusters chronologically and divides them into training, calibration,
assessment, and a latest closed lockbox. An intraday route can have multiple
clusters in one calendar day. The horizon-specific defaults are:

| Horizon | Minimum training clusters | Calibration clusters | Assessment clusters | Closed lockbox clusters |
| --- | ---: | ---: | ---: | ---: |
| `1h` | 252 | 63 | 63 | 126 |
| `4h` | 252 | 63 | 63 | 126 |
| `1d` | 252 | 63 | 63 | 126 |
| `1w` | 252 | 63 | 63 | 126 |
| `1w-d1` through `1w-d5` | 252 | 63 | 63 | 126 |

For every route, target windows that reach a later partition boundary are
purged at every transition: training to calibration, calibration to assessment,
and assessment to lockbox. This is especially material for aggregate `1w`,
whose rolling remaining-week windows overlap. Loop B selects enough earlier
clusters for the configured training, calibration, and assessment requirements
to survive purging; if that is impossible, readiness fails clearly rather than
reducing a requirement. Training, calibration, assessment, and lockbox sample
`id` values must be disjoint.

Assessment rows are not used to fit the estimator or calibrator; they are held
out for offline evaluation and backtest predictions.

Verified prior `LIVE` target starts are excluded from offline partitioning.
They can therefore mature as out-of-sample live evidence without becoming a
later run's lockbox. The latest remaining completed clusters form the
model-time lockbox, newer than the assessment slice. Its target column is never
coerced or read, and its rows are omitted from persisted `samples.parquet`,
never predicted, and never scored. Model JSON records only
`CLOSED_UNTOUCHED_UNSCORED`, row and target-cluster counts, and the first/last
`target_window_start` bounds; it contains no lockbox targets or performance
values.

Configure the counts with:

```text
--minimum-train-clusters
--calibration-clusters
--assessment-clusters
--lockbox-clusters
```

Each option overrides that count for every selected horizon. Omit an option to
retain the horizon-specific value above.

## Models and probabilities

The default route model is logistic regression with Platt calibration. The
runtime can also request LightGBM or XGBoost, and can select no calibration or
isotonic calibration.

| Route | Default model | Predicted outcome | Primary Python implementation |
| --- | --- | --- | --- |
| `1h` | Logistic regression + Platt calibration | Cost-adjusted return over the next 60 eligible regular-session minutes is positive | Model execution: `ml/runtime_pipeline.py`; fit/reuse: `ml/model_runtime.py`; estimator: `ml/models/registry.py`; calibration: `ml/calibration.py`; target: `ml/horizons.py`, `ml/rolling_samples.py` |
| `4h` | Logistic regression + Platt calibration | Cost-adjusted return over the next 240 eligible regular-session minutes is positive | Model execution: `ml/runtime_pipeline.py`; fit/reuse: `ml/model_runtime.py`; estimator: `ml/models/registry.py`; calibration: `ml/calibration.py`; target: `ml/horizons.py`, `ml/rolling_samples.py` |
| `1d` | Logistic regression + Platt calibration | Next eligible session's cost-adjusted open-to-close return is positive | Model execution: `ml/runtime_pipeline.py`; fit/reuse: `ml/model_runtime.py`; estimator: `ml/models/registry.py`; calibration: `ml/calibration.py`; target: `ml/horizons.py`, `ml/rolling_samples.py` |
| `1w` | L1 logistic regression + support-bounded Platt calibration | Cost-adjusted return from Day 1 open through the final close of Day 1's exchange week is positive | Model execution and remaining-week issuance: `ml/runtime_pipeline.py`; fit/reuse and aggregate override: `ml/model_runtime.py`; estimator: `ml/models/registry.py`; calibration: `ml/calibration.py`; target: `ml/horizons.py`, `ml/rolling_samples.py` |
| `1w-d1` ... `1w-d5` | Five separate logistic regression + Platt calibration models | The corresponding eligible session's cost-adjusted open-to-close return is positive | Model execution and remaining-week issuance: `ml/runtime_pipeline.py`; fit/reuse: `ml/model_runtime.py`; estimator: `ml/models/registry.py`; calibration: `ml/calibration.py`; targets: `ml/horizons.py`, `ml/rolling_samples.py` |

Every row predicts the binary `target_cost_adjusted_positive` column. The
continuous `forward_raw_return` and `forward_cost_adjusted_return` values are
persisted for evaluation but are not the fitted target. The implementation
files are shared factories and runtime functions rather than one Python model
module per route. The model-family and calibration CLI options can replace the
defaults above for all selected routes; if a calibration partition has one
class, the effective calibration method is `none`.

The aggregate `1w` route has a conditional logistic-family override. When its
model family is logistic, it uses an L1 fit with `C=0.3`, `liblinear`,
`max_iter=5000`, and `tol=1e-5`. When that same route requests Platt calibration,
the Platt fit uses `C=0.1`; the fitted calibrator stores the chronological
calibration partition's minimum and maximum raw probabilities and clips later
raw probabilities to that support before calibration. This prevents the Platt
mapping from extrapolating outside its fit support. The five component routes
`1w-d1` through `1w-d5` retain the ordinary logistic and calibration parameters,
and non-logistic aggregate models do not receive a logistic override.

Each model first projects its horizon's exact ordered registry columns and
applies the registered semantic transforms. A
`log1p-capped-training-v2` feature is capped at its training-fitted 99.75th
percentile before `log1p`. For the logistic numeric branch, the training
partition fits median imputation, 0.25th/99.75th-percentile clipping, robust
scaling, and missing indicators. Registered categorical columns are one-hot
encoded. Tree-family numeric branches use median imputation plus missing
indicators, with one-hot encoding for categorical columns. No preprocessing
statistic or clipping bound is learned from calibration, assessment, lockbox,
or live rows.

Model names are readable, for example:

```text
logistic-1h
logistic-4h
logistic-1d
logistic-1w
logistic-1w-d1
...
logistic-1w-d5
```

Each horizon stores its model under:

```text
DATASTORE/ml/models/<horizon>/<model-name>/<trained UTC timestamp>/
```

Loop B reuses the latest model only when its manifest matches the current
feature, target, configuration, input, and training boundaries. It otherwise
fits a new timestamped model. No separate model-selection daemon or model
registry is involved.

Compatibility includes preprocessing policy
`training-quantiles-0.25-99.75-v1`, its exact numeric bounds, and the semantic
training-cap mapping. An artifact fitted under the former 0.5th/99.5th policy
is therefore not reusable.

The recorded estimator parameters and, when present, aggregate calibration
parameters are part of that compatibility comparison. Consequently a logistic
aggregate artifact trained before the L1/Platt-support policy is not reusable;
the component routes are not invalidated merely by the aggregate-only change.

For both `1h` and `4h`, compatibility includes the complete readable horizon
specification, the respective v2 target version, native-`1m` target-price
source and constituent policy, calendar policy, five-minute processing delay,
one-time round-trip-cost convention, and numeric assumed cost. This migration
invalidates existing `1h` and `4h` reuse and prospective evidence without
changing `1d` compatibility.

For every internal weekly horizon, compatibility includes the readable horizon
specification, target-definition version, and numeric assumed round-trip cost.
The new aggregate definition cannot reuse a model trained for the retired
weekly-context next-session target. The five component models are separate
ordinary horizon models. There is no multi-output wrapper, compatibility
fallback, or legacy target adapter.

Reuse also requires the recorded Python implementation and major/minor version,
plus NumPy, pandas, PyArrow, scikit-learn, joblib, exchange-calendars, LightGBM,
and XGBoost package versions, to match before `model.joblib` is loaded.

The model manifest records held-out assessment metrics for raw and calibrated
probabilities: log loss, Brier score, 0.5-threshold accuracy, and ROC AUC. It
also records constant training-base-rate and calibration-base-rate comparisons,
prior-period-direction accuracy when available, and whether calibrated log loss
beats each base-rate baseline. The manifest explicitly records that assessment
was used for neither training nor calibration.

Every newly fitted model also records the calibration partition's raw-probability
minimum and maximum, how many assessment probabilities fall below, above, or
anywhere outside that support, and whether the fitted calibrator clips to the
observed range. The clipping flag is true when the default logistic/Platt
aggregate `1w` route successfully fits the support-bounded calibrator, and false
when no such calibrator was fitted.

The manifest's separate lockbox block contains only its closed status and
row/target-cluster counts and `target_window_start` bounds, never lockbox
targets, predictions, scores, or metrics.

Predictions retain:

```text
model_name
model_version
calibration_method
prediction_mode
prediction_status
prediction_created_at
assumed_round_trip_cost
raw_probability
calibrated_probability
```

`model_version` is the readable trained UTC timestamp directory of the model
artifact used to make the row.

Every newly issued `LIVE` prediction is created strictly before
`target_window_start`. During a weekly target period, current runs carry forward
the exact verified origin rows rather than creating later prediction events.
The current rolling view maps calibrated probability to `probability_up` and
stores `probability_down = 1 - probability_up`.

The completed run manifest serializes the full `1h`, `4h`, and six weekly
horizon specifications for audit alongside the selected horizons and
configuration.

## Relationship to options-strategy ranking

Directional forecasting and options-strategy prediction are two models with
different targets and artifacts:

| Value | Meaning | Persisted owner |
| --- | --- | --- |
| `calibrated_probability` / `probability_up` | Probability that the route's underlying cost-adjusted directional target is positive | `predictions.parquet` and `intelligence.parquet` |
| `market_expected_absolute_move`, `market_expected_realized_volatility`, `market_uncertainty`, `market_trend_persistence`, `market_mean_reversion_tendency` | Point-in-time market-state context shared with each exact candidate | `strategy-candidates.parquet` |
| `raw_profit_probability` | Scenario-prior probability before a model is fit, or fitted classifier probability once sufficient history exists | `strategy-candidates.parquet` |
| `calibrated_profit_probability` | Empirically calibrated probability that one exact candidate has positive observed-BBO net profit after modeled option fees; null for prior-only rows | `strategy-candidates.parquet` |
| `decision_score` | Primary profitable-outcome probability: calibrated for fitted rows, scenario prior for fallback rows | `strategy-candidates.parquet` |
| `score_basis` | `CALIBRATED_MODEL` or `SCENARIO_PRIOR`; identifies how `decision_score` was produced | `strategy-candidates.parquet` |
| `expected_return_on_risk` | Separate payoff-magnitude estimate; secondary ranking key only | `strategy-candidates.parquet` |
| portfolio fit | Freshly calculated feasibility/exposure description with no score or rank influence | Options Strategies UI only |

The strategy stage runs after all required directional predictions succeed.
`point-in-time-market-state-v1` combines the matching directional probability
with causal exact-surface and audited route context to describe direction,
expected move, expected realized volatility, uncertainty, trend persistence,
and mean-reversion tendency. `greek-bbo-scenario-prior-v2` uses that state with
each candidate's exact Greeks, holding time, BBO spread, fees, and payoff bounds
to produce an immediate raw probability and expected return. Direction therefore
influences the candidate through its market-state distribution, not through a
fixed bonus added after calibration.

Therefore a 70% directional up probability is not a 70% profitable-outcome
probability for a call, spread, hedge, or volatility structure. The directional
value is persisted separately in `direction_probability_up` for audit, and
`direction_alignment` is only a diagnostic measurement. A prior-only row is
explicitly uncalibrated. Once enough causal GOOG history exists, the strategy
classifier produces its own raw profitable-outcome probability, Platt maps that
value using only the calibration partition, and a separate nonlinear regressor
learns expected-return residual relative to the scenario prior.

The strategy model has its own exact-chain and market-state feature set, causal
future-BBO outcomes, route model hierarchy, weighted Platt calibrator, and
chronological 252 training / 63 calibration / 63 assessment cluster
requirements. The real
126-cluster directional lockbox is removed before the strategy stage and its
target starts are passed as forbidden values. Strategy fitting, calibration,
assessment, ranking, or outcomes can never reopen it. None of this changes the
directional model's 252/63/63/126 defaults, target definitions, feature-set
version, or prediction artifacts.

The **Rolling Forecasts** screen continues to present the underlying
directional values above. The sibling **Options Strategies** screen reads the
separate candidate artifact, applies current portfolio fit at display time,
and owns user-controlled ticketing. See
[Loop B options-strategy selection](options-strategy-selection.md).

## Evaluation

Loop B scans prior timestamped runs for live predictions, but accepts only runs
whose manifests and output inventories verify. Receipt-era rows must also be
reachable through the authoritative pointer's verified publication chain.
Incomplete, damaged, unmanifested, unpromoted, and orphan-receipt directories
are excluded. Eligible prior live rows are combined with the current run and
deduplicated at the natural prediction grain before a target candidate is
selected on:

```text
symbol, horizon, decision_timestamp
```

Because `horizon` is part of every reconciliation and prediction key, a `4h`
decision cannot collide with an otherwise identical `1h`, `1d`, or weekly
timestamp, and the six weekly rows cannot collide with one another.

A prediction receives scores only when that natural key resolves to a complete
target, its target-definition version and serialized specification match, both
target-window timestamps match, and `assumed_round_trip_cost` matches. A `LIVE`
prediction must also have valid available information and satisfy its strict
deadline rule. Ordinary routes require `prediction_created_at <
actionable_until <= target_window_start`; remaining-week routes require
`prediction_created_at < actionable_until <= target_window_end`. Equality at
the action deadline is post-deadline and cannot be live evidence.

`evaluation_status` explains every unscored row:

| Status | Meaning |
| --- | --- |
| `INVALID_PREDICTION` | Prediction status, mode, timestamps, or information timing is invalid. |
| `PENDING` | The natural key has no complete observed target yet. |
| `TARGET_CONTRACT_MISMATCH` | Target-definition version or serialized specification does not match. |
| `TARGET_WINDOW_MISMATCH` | Target start or end does not match. |
| `CONFIGURATION_MISMATCH` | Assumed round-trip cost does not match. |
| `POST_ENTRY_PREDICTION` | The live snapshot was created at or after its action deadline. |
| `EVALUATED` | Natural key, contract, target window, cost, and timing are eligible. |

An evaluated row contains:

```text
assumed_round_trip_cost
observed_target
observed_forward_raw_return
observed_forward_cost_adjusted_return
raw_log_loss
log_loss
raw_brier_score
brier_score
prediction_correct_0_5
evaluated_at
```

Mismatch and post-entry rows retain their readable status but have null observed
values and scores. There is no post-entry fallback.

## Monitoring and live evidence

`monitoring.parquet` contains three global coverage/model values:
`prediction_rows`, `evaluated_predictions`, and `model_reuse_rate`.

Evaluated performance is reported at the `global` scope and separately for each
`horizon`:

```text
mean_raw_log_loss
mean_log_loss
mean_raw_brier_score
mean_brier_score
accuracy_at_0_5
observed_positive_rate
mean_calibrated_probability
calibration_gap
roc_auc
```

These scopes include evaluated offline assessment rows. If matured live
predictions exist, the same metrics are also written at the `live_horizon`
scope using only live rows. When multiple eligible live snapshots exist for one
symbol/horizon/decision, the earliest eligible `prediction_created_at` is
canonical for
live performance.

Each observed symbol/horizon route has a `completed_live_forecasts` monitoring
row with `scope_type = symbol_horizon` and
`scope_value = symbol|horizon`. A **completed live forecast** is an `EVALUATED`
prediction whose mode is genuinely prospective `LIVE`. It therefore has a
complete matured label, matching target contract, matching predetermined target
window, matching cost, valid information timing, and
`prediction_created_at < actionable_until`; ordinary deadlines cannot exceed
target entry and remaining-week deadlines cannot exceed target close. If its run declares the
transactional publication contract, that run must also have a valid
`publication.json` receipt bound to its verified manifest and be reachable
through the authoritative pointer's publication chain.

The count deduplicates on:

```text
symbol, horizon, decision_timestamp
```

The earliest eligible `prediction_created_at` is canonical, so repeated
publications of one natural decision count once. Including both symbol and
horizon prevents route collisions. The runtime recovers candidates only from
prior run directories whose manifests and complete output inventories verify.
An unpromoted working run can have a complete manifest or an orphaned prepared
receipt, but it is not reachable through the publication chain and can never
become live evidence.

`BACKTEST`, pending, post-entry, target-contract mismatch,
target-window mismatch, cost mismatch, invalid prediction, invalid-manifest,
and closed-lockbox rows never increase this count. Historical rolling samples
and offline assessment rows are not prospective live evidence. A `4h` row
cannot mature until its full target and processing delay have elapsed.

The current intelligence row uses exactly the same route-specific count, status,
and threshold. Horizon-pooled `live_horizon` rows remain performance summaries;
they are not presented as a symbol card's evidence count.

| Horizon | Live-evidence threshold |
| --- | ---: |
| `1h` | 60 |
| `4h` | 60 |
| `1d` | 30 |
| `1w` | 30 |
| `1w-d1` through `1w-d5` | 30 each |

The deliberate initial `4h` threshold is 60 because decisions occur after
eligible hourly source bars.

A route reports `NO_COMPLETED_DECISIONS` at zero,
`INSUFFICIENT_LIVE_EVIDENCE` below its threshold, and
`LIVE_EVIDENCE_AVAILABLE` at or above the threshold.

Current `one-id-v2` intelligence rows persist both
`completed_decision_count` and `minimum_live_decision_count`. The card and its
accessible status use:

```text
Live evidence: Awaiting first completed forecast (0 of N)
Live evidence: X of N completed forecasts
```

The second form applies whenever the count is positive, including at and above
the threshold.

`calibration_gap` uses `0.05` as its warning reference. `roc_auc` uses `0.5` as
the chance reference. These values and live-evidence statuses support research
and risk analysis; they do not enable automated action.

For `1h`, `4h`, and `1d`, a route is `OPERATIONALLY_CURRENT` while it has either
an actionable fresh prediction or a verified, explicitly non-actionable carried
forecast whose target is in progress. A ready non-weekly route with neither is
`OPERATIONALLY_STALE`. A verified
weekly route remains `OPERATIONALLY_CURRENT` while its coherent
remaining-week snapshot is published. A newer completed daily decision
supersedes the prior snapshot with a shorter target set.

## Run and current storage

A completed runtime iteration writes:

```text
DATASTORE/ml/runs/<UTC timestamp>/
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

Publication is fail-closed across required routes and predictions:

1. every requested symbol/horizon materialization route must be `READY`;
2. after the horizon model loop, every requested symbol/horizon must have at
   least one prediction;
3. public `1w` requires the aggregate and all five components from either one
   new eligible issuance or one complete verified frozen issuance; and
4. the always-running strategy stage must complete its lockbox check and
   produce the exact candidate/audit schemas, even when either is empty; and
5. all seven Parquets and the manifest must be written before current-state
   refresh begins.

Materialization records a route-specific source error for reporting, but any
non-ready required route aborts the cycle before a timestamped run directory is
created. Model errors are caught by horizon so the loop can finish reporting,
but a resulting missing route also aborts the cycle. Success from another
symbol or horizon is not published as a partial current run.

A model or prediction failure can leave a timestamped working directory with
partial artifacts and no completed manifest. A still later failure can leave
all artifacts and a complete manifest but no valid publication receipt. Neither
form is authoritative or eligible for later prediction reconciliation.

After a complete run, Loop B prepares compatibility/convenience mirrors under
`DATASTORE/ml/latest` and at the legacy UI path:

```text
DATASTORE/ml-intelligence/latest/rolling-predictions.parquet
```

The default UI and other official current readers instead follow the single
authoritative `DATASTORE/ml/latest/run.json` commit pointer and resolve
`intelligence.parquet` from that immutable timestamped run. The path above is
a compatibility mirror, not publication authority. An explicitly configured
fixture path remains available for tests.

The mirrors are not an atomic multi-file snapshot and are never used as the
publication commit signal. After all route, prediction, integrity, receipt,
and target-start checks pass, Loop B atomically replaces
the single authoritative `ml/latest/run.json` pointer. That pointer swap is the
current-view commit point; official readers then resolve every artifact from
the immutable run directory. A required-route, prediction, staging, deadline,
receipt, or pointer-commit failure leaves the previous pointer authoritative.

`publication.json` binds the completed manifest checksum and links to the prior
committed receipt-era publication, if any. Later LIVE-evidence recovery accepts
receipt-era runs only when they are reachable through that verified chain. A
failed working directory may have a complete manifest or orphaned prepared
receipt, but it cannot contribute evidence. No failed-source row is exposed
through the authoritative current view. A malformed pointer or broken chain
fails closed rather than falling back to a newest directory.

For three requested symbols and public selections `1h 4h 1d 1w`, a successful
all-horizon publication contains 27 current rows: three non-weekly and six
weekly rows per symbol. This is a deterministic contract consequence, not a
claim that a live 27-row publication has been deployed or observed.

Within a successful run, a non-weekly route can still have assessment/backtest
predictions but neither a fresh actionable row nor an eligible active carry.
That route is published with null probability, `NOT_ACTIONABLE`,
`OPERATIONALLY_STALE`, and a readable limitation.
The UI verifies the exact current `one-id-v2` Arrow schema,
orders the unchanged `1h`, `4h`, and `1d` cards and groups the current weekly
aggregate and Day 1 prefix as one **remaining-week outlook**. That outlook
shows aggregate up/down probability, issuance time, dynamic aggregate bounds,
and each remaining session with its actual weekday, date, UTC/local window,
probability, and pending/completed evidence status.
It exposes route-specific evidence and limitations and never authorizes
automated action. Cards no
longer render a separate model-testing row, separator, or accessible
model-testing announcement. `model_name` and `model_evidence_status` remain in
the persisted intelligence row and Debug details; offline metrics remain in
their model and evaluation artifacts.

Run and model directories are named with UTC timestamps rather than long
generated identifiers. Integrity checksums may appear in JSON manifests but
never in Parquet columns, directory names, or joins.

## Invocation

```powershell
python -m ml.prediction_runtime `
  --datastore C:\data\duckets `
  --symbols NVDA GOOG `
  --provider databento `
  --horizons 1h 4h 1d 1w `
  --model-family logistic `
  --calibration platt `
  --round-trip-cost 0.001 `
  --once
```

Public `--horizons 1w` expands automatically to all six weekly routes. This
invocation leaves the four cluster-count flags unset, so each internal horizon uses
its default partition counts. Loop B never substitutes a model or source from
another horizon: a required missing source or prediction aborts publication,
while a successful route with no current live candidate publishes an explicit
non-actionable state. See
[ml_prediction_runtime.md](ml_prediction_runtime.md) for stage and supervisor
details.

Deploying the code, training operational models, and restarting the Loop B
supervisor remain operator actions. This documentation does not claim a live
weekly snapshot has been published and does not start or restart either loop.
