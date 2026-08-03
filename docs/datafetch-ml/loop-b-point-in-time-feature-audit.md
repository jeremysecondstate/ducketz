# Loop B point-in-time feature audit

Status: current implementation audit

Audited: 2026-08-01

Repository state: four public horizon selections are present in this working
tree; public `1w` expands to six internal model routes. Deployment status is
recorded in the live-coverage section.

This document records what Loop B actually reads, joins, models, and
publishes. It distinguishes implemented runtime enforcement from policies
that exist only in the semantic registry or specialized helper loaders.
`audited-feature-contracts.md` is the detailed feature allowlist and source
contract. `ml_prediction_runtime.md` is authoritative for model execution and
publication.

The implementation portion was re-audited from repository code and tests on
2026-08-01. The live-coverage section preserves the explicitly dated read-only
PC-datastore observation; this update did not start, stop, or modify either
loop and did not reinterpret that historical run as current deployment proof.

## Executive findings

1. The default closed profile is `loop-a-all-v1`, version `1.2.0`, not the
   19-column `technical-all` compatibility profile.
2. The active model sets contain 69 features for `1h`, 69 for `4h`, 139 for
   `1d`, and 132 for each of the six weekly routes. The ordered `4h` inventory
   exactly clones `1h`; selecting all public horizons still produces a physical
   `samples.parquet` schema with the ordered union of 143 feature columns.
3. The weekly family creates six historical candidates at every eligible daily
   decision: aggregate `1w` targets Day 1 open through Day 5 close, and
   `1w-d1` through `1w-d5` target each session's open-to-close return. LIVE
   issuance is restricted to the final eligible exchange-week decision.
4. Bar shape, weekly context, and SEC events use specialized point-in-time
   loaders. Most other added families use generic backward as-of joins.
5. Generic joins enforce availability and freshness where configured, but
   they do not enforce all registry metadata, specialized family quality
   gates, or duplicate-failure policies.
6. A feature marked `ACTIVE` in `loop-a-all-v1` is selected for modeling; it
   does not mean live coverage passed the candidate readiness policy.
7. Under the default logistic/Platt configuration, aggregate `1w` alone uses
   an L1 logistic fit (`C=0.3`, `liblinear`, `max_iter=5000`, `tol=1e-5`) and a
   Platt fit with `C=0.1`. Before Platt prediction, its raw probability is
   clipped to the calibration partition's observed raw-probability range. The
   five weekly component routes retain the ordinary logistic and calibration
   parameters.
8. Loop B requires every configured symbol/horizon route. One materialization
   failure or a route without predictions aborts the cycle before publication.
   The last successful authoritative `ml/latest/run.json` pointer remains in
   place; compatibility mirrors do not establish a new current run.
9. Every Loop B cycle also runs `schwab-spreads-v1` strategy analytics after
   directional prediction. That stage reads the immutable normalized Schwab
   contract, option-surface, and stock-quote receipt histories directly. It
   creates a point-in-time market state and exact-mechanics scenario prior for
   every constructible candidate, then uses causal GOOG outcomes for nonlinear
   probability/expected-return fitting when the 252/63/63 partitions exist. It
   never uses a closed-lockbox row, and current Schwab holdings are added only
   by the UI after publication; they are not historical model features.

## Current route and feature inventory

| Horizon | Feature set | Version | Model values | Family counts |
| --- | --- | --- | ---: | --- |
| `1h` | `loop-a-all-v1-1h` | `1.2.0` | 69 | `mr` 13, `bp` 13, `bar` 2, `life` 5, `quote` 1, `opt` 26, `energy` 1, `cme` 8 |
| `4h` | `loop-a-all-v1-4h` | `1.2.0` | 69 | `mr` 13, `bp` 13, `bar` 2, `life` 5, `quote` 1, `opt` 26, `energy` 1, `cme` 8 |
| `1d` | `loop-a-all-v1-1d` | `1.2.0` | 139 | `mr` 13, `bp` 13, `bar` 3, `weekly` 3, `life` 5, `fdir` 25, `fund` 13, `ftlife` 17, `quote` 1, `opt` 32, `energy` 1, `macro` 4, `sec` 3, `cme` 6 |
| `1w`, `1w-d1` ... `1w-d5` | `loop-a-all-v1-1w` | `1.2.0` | 132 each | `mr` 14, `bp` 12, `bar` 3, `weekly` 3, `life` 5, `fdir` 25, `fund` 13, `ftlife` 17, `opt` 29, `macro` 4, `sec` 3, `cme` 4 |

The sample file stores the union of the selected horizons' columns. A column
that does not apply to a row's horizon remains null. Each horizon model
receives only its exact ordered feature set.

`production-v1` selects `technical-all-4h` for `4h`, a horizon-scoped clone of
the 19-feature `technical-all` compatibility contract. `technical-all-v2`
selects the 22-feature `technical-all-v2-4h` clone. The CLI accepts only closed
profiles and does not expose an arbitrary feature-list override.

## Decision and target timing

| Horizon | Decision grid | Context available at the decision | Target |
| --- | --- | --- | --- |
| `1h` | Completed canonical hourly bar plus five minutes | Exact hourly technical/bar values plus causally available as-of families | First native-minute open through final native-minute close of the next 60 calendar-selected eligible regular-session minutes |
| `4h` | Every completed eligible canonical hourly bar plus five minutes | The same ordered hourly technical/as-of inventory and freshness policy as `1h` | First native-minute open through final native-minute close of the next 240 calendar-selected eligible regular-session minutes |
| `1d` | Completed canonical daily session plus five minutes | Exact daily technical/bar values, previous completed weekly context, and causally available as-of families | Next eligible session open-to-close |
| `1w` | Historical candidates after every eligible daily session plus five minutes; LIVE only after the final exchange-week session | Daily technical/bar values and the previous completed exchange week | Day 1 official open through Day 5 official close |
| `1w-d1` ... `1w-d5` | Same historical and LIVE decision rules as aggregate `1w` | Exact same ordered 132-column weekly inventory | Each corresponding eligible session's official open-to-close |

Weekly context is calculated from canonical Databento daily sessions. It
becomes available five minutes after the actual last eligible XNYS close of a
completed week, including holiday-shortened weeks. It is joined backward to
subsequent daily decisions.

The exchange calendar selects the next five eligible sessions, including
holidays, early closes, weekends, and DST. Each component matures at its own
close plus five minutes; aggregate `1w` matures after Day 5 close plus five
minutes. All six routes use separate single-target models and subtract the
configured round-trip cost once. Once a complete LIVE bundle is promoted, its
probabilities, model versions, prediction timestamp, and target windows are
reused exactly from verified receipt-chain history throughout its target
period. Retired next-session `1w` models and predictions are incompatible.

The exact revised target definitions are
`next-60-eligible-regular-minutes-open-close-v2` for `1h` and
`next-240-eligible-regular-minutes-open-close-v2` for `4h`. The versioned
calendar policy,
`session-open-break-resume-plus-full-local-clock-anchor-v1`, fixes each target
before price lookup. Each continuous regular-session segment contributes its
exact start (official session open or post-break resume) and every
exchange-local clock-hour start whose complete hour is contained in that
segment. The first candidate strictly after information availability wins;
equality is too late.

From that fixed start, the calendar accumulates exactly 60 or 240 eligible
regular-session native one-minute intervals. Breaks and closures pause the
eligible-time clock, while the endpoint return includes intervening price
gaps. The target price contract is canonical adjusted native Databento `1m`,
`canonical-adjusted-native-1m-interval-open-v1`: the first selected minute's
open and final selected minute's close form the return. Every predetermined
minute must exist; a missing first, middle, or final constituent leaves the
window unchanged and the label incomplete. No later minute is substituted.

On an ordinary XNAS session, a decision using only prior-session information
can therefore target the official 09:30 Eastern open: `1h` ends at 10:30 and
`4h` ends at 13:30. The UTC start is 14:30 in EST and 13:30 in EDT, and the
ordinary Pacific display is 06:30. The 09:30–10:00 fragment is not called a
one-hour target. Ordinary intraday decisions retain full local-clock starts
(for example, 11:05 Eastern selects 12:00), and late targets can accumulate
across breaks, early closes, overnight closures, weekends, holidays, and DST
transitions.

Materialization maps both intraday routes to completed native `1h` source bars
for features and `previous_period_direction`, while loading native `1m` only
for targets. `4h` shares the existing `(symbol, "1h")` source cache with `1h`;
the target caches are shared by symbol as well. There is no Loop A `4h`
provider fetch or synthetic `4h` bar write, and future target minute prices
never enter model features.

## What the integrated runtime actually enforces

The following table describes the production dispatch in
`ml/rolling_materialization.py`. “Generic” means the family is passed through
the shared symbol or shared-context backward as-of path, not its stricter
specialized helper.

| Family | Production source and availability clock | Production join | Freshness in the integrated path | Important runtime behavior |
| --- | --- | --- | --- | --- |
| `mr`, `bp` | Databento market-regime `1.2.0` and breakout-pressure `1.1.0`; availability reconstructed as completed bar end + 5 minutes | Specialized exact technical assembly | Exact decision | Exact timing, version, calculation mode, and price-adjustment basis are enforced |
| `bar` | Bar-shape `1.0.0`, explicit `available_at` | Specialized exact loader | Exact decision | Completed-bar and source-contract validation is enforced |
| `weekly` | Weekly-context `1.0.0`, explicit `available_at` | Specialized backward as-of | 8 calendar days | Calendar, calculation, completion, and availability checks are enforced |
| `life` | Technical-lifecycle daily file, explicit `available_at` | Generic symbol as-of | 2 days for `1h`/`4h`/`1d`; 8 days for `1w` | The generic path does not enforce `constituent_complete` |
| `fdir` | Legacy FMP fundamental-direction partitions, `effective_from` | Generic symbol as-of | None | Later values are not joined before `effective_from`; no operational 120/400-day carry limit |
| `fund` | FMP point-in-time fundamental partitions, `available_at` | Generic symbol as-of | None | Amendments can be represented, but no operational 120/400-day carry limit |
| `ftlife` | Legacy fundamental-technical-lifecycle daily file | Generic symbol as-of using persisted `timestamp` | 2 days for `1d`; 8 days for `1w` | It does not join on `generated_at`; rebuilding a historical row later is not represented by a later availability clock |
| `quote` | Schwab quote-liquidity receipt, `available_at` | Generic symbol as-of | 5 minutes for `1h`/`4h`; 1 day for `1d` | Closed-session stale receipts persist with failed quality so the source history remains readable; the generic read path revalidates physical quote evidence and declared quality |
| `opt` | Schwab option-quality receipt, `available_at` | Generic symbol as-of | 2 hours for `1h`/`4h`; 1 day for `1d`; 3 days for `1w` | Production does not call the specialized option loader's surface-quality and cutoff gates |
| `energy` | FMP energy-context, `available_at` | Generic shared as-of | 30 minutes for `1h`/`4h`; 1 day for `1d` | Production does not independently enforce instrument-chain controls |
| `macro` | Four normalized current-revised FRED histories; availability is the maximum selected `fetched_at` | One derived current snapshot, then generic shared as-of | 120 days for both `1d` and `1w` | The active path does not use FRED vintage/release artifacts |
| `sec` | Versioned SEC events, `available_at` | Specialized event loader | First eligible decision impulse | Acceptance/receipt/extraction timing and one-decision impulse semantics are enforced |
| `cme` | Derived CME context when present; otherwise an in-memory context from normalized OHLCV/BBO/MBP | Generic shared as-of | 15 minutes for `1h`/`4h`; 1 day for `1d`; 3 days for `1w` | The normalized-source fallback does not provide every gate encoded by the derived context writer |

For every family selected by a route, every requested symbol must have its
required source partitions. Shared families must have their required shared
files. A family must contain at least one numeric value somewhere in the
combined source. Per-row feature values may still be missing.

The generic symbol join:

- parses the configured availability column as UTC;
- filters to requested symbols;
- sorts by `symbol`, availability, and configured tie-breakers;
- keeps the last row for duplicate `symbol,available_at`;
- performs a backward as-of join with `available_at <= decision_timestamp`;
- applies the configured freshness duration; and
- leaves unavailable or stale row values missing.

The generic shared join does the same by `available_at` without a symbol.
Consequently, do not infer that every same-availability conflict fails closed:
the integrated generic path deterministically keeps the last sorted row.

## Registry contract versus runtime dispatch

The registry carries provider, source-grain, required-version, availability,
freshness, missing-value, coverage, readiness, and transform metadata. It also
defines quarantined candidate sets for each added family.

`loop-a-all-v1` constructs active copies of applicable candidate features.
This has three consequences:

1. candidate feature sets can remain `IMPLEMENTED_BUT_QUARANTINED` while the
   same values are active inside the integrated default profile;
2. `ml.readiness.evaluate_feature_readiness` is available for audit tooling
   and tests but is not called by the prediction runtime; and
3. registry policy text is not proof that the production loader enforces that
   policy.

The production guarantees are the intersection of the selected registry
allowlist and the loader behavior documented in the preceding table.

## Point-in-time status by family

### Strongest current paths

- Current market-regime and breakout-pressure values are tied to exact
  completed bars, exact calculation versions/modes, and a five-minute delay.
- Bar-shape values use the same exact-decision boundary.
- Weekly context is built from completed exchange sessions and cannot be used
  until the completed week's final close plus five minutes.
- SEC values use filing acceptance, document receipt, and extraction timing;
  the event impulse is confined to the first eligible decision.
- Point-in-time FMP fundamentals persist later publication/amendment versions
  with readable availability keys.

### Material caveats

- The active macro path uses the latest revised values fetched now. It creates
  one current context row and cannot recreate the vintage known at older
  decisions. The repository's FRED vintage writer exists, but live Loop A does
  not feed it.
- The legacy fundamental-technical lifecycle path uses its `timestamp` as
  availability. That is conservative only if the historical file itself was
  generated and retained causally.
- Fundamental-direction and point-in-time fundamental values have no freshness
  limit in the integrated dispatch, despite stricter freshness policies in the
  semantic registry and specialized loaders.
- Option evidence fields such as coverage, counts, days-to-expiration, quote
  staleness, and parity residuals are ordinary `MODEL_VALUE` features in the
  default profile. They are not merely audit controls. Explicit controls such
  as `surface_quality_pass`, cutoff flags, schema versions, and availability
  timestamps are excluded from the model matrix.
- The integrated option, quote, lifecycle, energy, and CME generic joins do not
  independently reapply all upstream quality controls.
- Loop A and Loop B retain independent supervisor locks and share one
  crash-released datastore cycle lock. Loop A holds it for a complete write
  cycle and Loop B holds it for a complete read/model/publication cycle, so a
  Loop B run cannot mix two Loop A cycles. The cycle state lives in a small JSON
  file and adds no Parquet columns. Loop B inventories every file it reads.

These caveats are not a claim of observed leakage in every row. They identify
where the implementation does not yet prove the stronger contract encoded in
the registry or specialized helpers.

## Exact-chain strategy lineage and timing

The directional model's `opt__*` columns and the options-strategy model do not
consume the same physical view of the chain. Directional materialization joins
the one-row-per-surface option-quality feature artifact described above. The
strategy stage instead opens the immutable receipt histories needed to recover
the exact observed legs:

| Strategy input | Immutable path | Receipt grain and enforced version |
| --- | --- | --- |
| Normalized Schwab contracts | `stocks/<S>/options/chains/schwab/normalized/YYYY-MM.parquet` | `symbol, snapshot_for, available_at, contract_symbol`; option-chain schema `1.1.0` |
| Schwab option-surface diagnostics | `stocks/<S>/options/features/option-quality/schwab/YYYY-MM.parquet` | `symbol, snapshot_for, available_at`; calculation `1.2.0`, schema `option-surface-v2`, quality policy `schwab-option-surface-quality-v1` |
| Schwab stock BBO receipts | `stocks/<S>/quotes/features/quote-liquidity/schwab/YYYY-MM.parquet` | `symbol, available_at`; schema `quote-liquidity-v1`, quality policy `schwab-quote-quality-v1` |

Loop A assigns `snapshot_for` from the causal Databento decision clock and
assigns `available_at` from the local Schwab receipt time. The strategy entry
reader requires `snapshot_for` to be at or after the sample
`bar_end_timestamp` and no later than the causal cutoff. It also requires
`available_at` to be at or after `information_available_at` and no later than
the earlier of the completed Loop A cycle `finished_at` or one nanosecond before
`target_window_start`. For a current candidate it selects the latest eligible
surface; historical candidate and outcome construction selects the earliest
eligible entry surface. This bounded interval reconciles the Loop B decision
clock with the Loop A receipt clock without lookahead. Contracts are then
matched to the selected exact `symbol, snapshot_for, available_at` surface;
contracts from another receipt are never substituted.

Candidate construction uses the exact Schwab contract symbol, expiration,
strike, call/put type, bid, ask, multiplier, Greeks, open interest, volume, and
quote age from that receipt. It admits only standard, non-mini contracts with
a 100-share multiplier and a numerically usable, non-crossed BBO. Surface
quality, quote validity, route liquidity, stock-quote quality, and quote age
are retained as measurements. A numerically constructible row is not erased
merely because one of those diagnostics fails. A strategy containing stock
also requires an exact numerically usable Schwab stock BBO inside the same
information-to-entry interval.

Market-state policy `point-in-time-market-state-v1` runs only after the
directional route succeeds. Current candidates use that route's exact matching
calibrated up probability; historical candidates use a matching causal
prediction when supplied and otherwise use neutral 0.5/0.5 scenario sign
weights rather than reconstructing a forecast from the future label. The
remaining measurements must be available by the candidate's causal entry
boundary. Expected absolute move comes from the selected surface's
`realized_expected_absolute_move_atm_horizon` or
`atm_straddle_implied_move`, scaled to the decision holding interval; audited
`mr__atr_percent` is the fallback. Expected realized volatility comes from the
same surface or its already-joined `opt__` context. Audited `mr__` and `bp__`
values supply trend-persistence and mean-reversion summaries, and direction
entropy supplies uncertainty. None of these inputs comes from an exit receipt
or a current Schwab account snapshot.

Prior policy `greek-bbo-scenario-prior-v1` evaluates deterministic up/down
scenarios with the exact candidate's delta, gamma, theta, holding time, BBO
spread, modeled fees, and payoff bounds. It fills raw profit probability,
expected net profit, expected return on risk, score, and rank even before an
empirical model exists. The calibrated probability remains null in that state;
the row is labeled `MARKET_STATE_PRIOR`, while the route model report records
`MODEL_NOT_FIT`. This prevents a mechanics prior from being misrepresented as
GOOG calibration.

For historical labels, the reader chooses the earliest future option surface
whose `snapshot_for` and `available_at` are at or after the fixed
`target_window_end`, subject to the route's maximum delay: two hours for `1h`,
six hours for `4h`, two days for `1d` and each `1w-dN`, and four days for
aggregate `1w`. It requires the same exact option contract symbols at exit and
uses future stock BBO receipts for stock legs. Long legs enter at ask and exit
at bid; short legs enter at bid and exit at ask; the modeled fee is $0.65 per
option contract at both entry and exit. Missing receipts, contracts, or usable
BBOs remain unavailable outcomes rather than being theoretically priced or
imputed.

Only complete observed-BBO pseudo-outcomes enter the strategy partitions.
Every row sharing one `target_window_start` stays in one chronological cluster;
the model uses at least 252 training clusters, then 63 Platt-calibration
clusters, then 63 assessment clusters. Calibration fits only on calibration,
and assessment influences neither the estimator nor the calibrator. The
directional runtime first removes its 126-cluster real lockbox from the sample
view and also passes every forbidden lockbox start to the strategy stage. A
matching start is a hard error. Historical exit selection is capped strictly
before the earliest lockbox boundary, so an earlier candidate cannot receive a
label from inside the closed period.

With sufficient evidence, `market-state-hgb-platt-return-v3` fits a nonlinear
profitable-outcome classifier and a nonlinear return-on-risk residual regressor.
The strategy model can use exact-chain and market-state measurements,
`previous_period_direction`, and eligible numeric point-in-time sample context
selected by its explicit prefix allowlist, including audited `mr__` and `bp__`
families. The persisted candidate artifact contains the exact-chain, five
market-state, prior, and fitted-model measurements defined by its own schema;
it does not copy the entire directional sample matrix. Platt calibration is fit
only on the classifier's calibration slice. The return regressor is fit only on
training, and assessment is evidence only.

Current account facts have a separate clock and owner. The Options Strategies
screen fetches a fresh Schwab account snapshot when it loads and derives
shares, option-position counts, working option-order counts, and available
funds. Policy `current-schwab-position-fit-v1` applies that snapshot only to
display-time portfolio fit and overall rank. Those mutable account facts never
enter historical sample assembly, strategy fitting, calibration, assessment,
or either immutable strategy Parquet. See
[Loop B options-strategy selection](options-strategy-selection.md) for the
complete strategy, UI, and order contract.

## Model and persisted-column boundary

Family joins create audit fields in memory, including family availability,
age, staleness, and join status. `_project_samples` persists only the base
sample contract and the selected model-value columns. Family join-audit
columns are not retained in `samples.parquet`. The assembly-only
`feature_available_at` field is also discarded because it duplicates the
validated decision timestamp. Feature-computation timing remains source
metadata, and materialization timing remains run-manifest metadata rather than
being copied onto every sample row.

Missing values are expected before first availability, after freshness
expiry, during indicator initialization, and when an upstream field is not
causally computable. The model pipeline does not drop a required feature merely
because it is all-missing in a training slice.

- Capped-log transform `log1p-capped-training-v2` applies a training-fitted
  99.75th-percentile upper cap before `log1p`.
- Logistic preprocessing applies semantic transforms, training-only median
  imputation, 0.25th/99.75th-percentile training clipping, robust scaling, and
  missing indicators.
- Tree preprocessing applies semantic transforms, training-only median
  imputation, and missing indicators.

Model compatibility records policy `training-quantiles-0.25-99.75-v1`, its
exact numeric bounds, and the semantic cap mapping. Models created under the
former 0.5th/99.5th policy cannot be reused.

The aggregate weekly override is conditional, not a general weekly-family
policy. When `model_family=logistic`, aggregate `1w` uses the L1 estimator
parameters above. When that route also requests Platt calibration, the fitted
calibrator stores the minimum and maximum raw probabilities observed on the
chronological calibration partition and clips later raw probabilities to that
support before applying the logit/Platt mapping. Other horizons, including
`1w-d1` through `1w-d5`, do not receive either override. If the calibration
partition has one target class, the effective calibrator is still `none`.

The ordered semantic feature metadata and its fingerprint are written to the
model manifest. Changes to order, semantics, inputs, row counts, training
boundary, target/cost configuration, estimator parameters, aggregate
calibration parameters, runtime/package compatibility, or model checksum
prevent unsafe model reuse. Every new model manifest also records calibration
raw-probability support and the count of assessment probabilities below,
above, and anywhere outside that support; the clipping flag is true only when
the fitted calibrator actually carries support bounds.

The `4h` feature definitions are horizon-scoped clones, not widened `1h`
definitions. The ordered `1h`, `1d`, and 132-column weekly feature inventories
remain unchanged; `4h` retains its own feature fingerprint and model
compatibility path. The revised `1h` and weekly targets are independently
target-contract changes, not feature-contract changes.

For both `1h` and `4h`, compatibility now contains the complete readable
horizon specification, v2 target-definition version, native-`1m` target-price
provider/timeframe/source version and constituent rule, calendar-policy version
and definition, processing delay, and the convention that the configured
round-trip cost is subtracted exactly once with a strict-positive class.
Changing one horizon's block invalidates reuse only for that horizon. Migration
to intraday v2 therefore requires new `1h` and `4h` model artifacts, while `1d`
reuse remains unaffected. The separate new aggregate and Day 1 through Day 5
weekly target versions independently invalidate retired next-session `1w`
artifacts without changing the 132-column feature membership.

Partition purging now follows actual target geometry instead of the horizon
label. Crossing windows are purged at every partition boundary. Equality is
also purged conservatively because an ending label and the next window share a
target-price endpoint and the ending label is not yet available before that
boundary. No live model was retrained and no supervisor was restarted during
this audit update; an operator must deploy, train the affected intraday and six
weekly routes, and restart Loop B before the new forecasts can be observed.

## Live read-only coverage snapshot

The inspected successful run was:

```text
ml/runs/20260730T111412.702785Z
```

Its manifest selected Databento, symbols `GOOG`, `MU`, and `NVDA`, the three
then-deployed horizons (`1h`, `1d`, `1w`), `loop-a-all-v1`, logistic models,
Platt calibration, and a `0.001` round-trip cost.

| Artifact observation | Value |
| --- | ---: |
| Sample rows | 19,635 |
| Sample columns | 172 |
| Base columns | 29 |
| Union feature columns | 143 |
| `1h` rows | 14,907 |
| `1d` rows | 2,364 |
| `1w` rows | 2,364 |
| Complete labels | 19,623 |
| Incomplete labels | 12 |
| Prediction rows | 612 |
| BACKTEST predictions | 603 |
| LIVE predictions | 9 |

Feature presence in that one run:

| Horizon | Active columns | Any populated value | Entirely missing |
| --- | ---: | ---: | ---: |
| `1h` | 69 | 28 | 41 |
| `1d` | 139 | 74 | 65 |
| `1w` | 132 | 74 | 58 |

This is a dated coverage observation, not a promotion decision or stable
contract. It demonstrates why `ACTIVE` must not be read as
“coverage-qualified.”

This observed run predates both the `4h` implementation and the frozen weekly
target family, and it also predates the strategy artifacts described above.
Its `1w` rows use the retired next-session definition and are not compatible
with aggregate `1w` or `1w-d1` through `1w-d5`. It is not evidence of live
`4h` predictions, a frozen weekly snapshot, a 27-row all-horizon publication,
or strategy-model performance. Those are separate operational facts and were
not inferred from this historical run.

The implementation and repository evidence for strategy analytics are scoped
to GOOG. The receipt architecture can load another symbol, but this dated
directional run's NVDA and MU rows do not establish transferable strategy
calibration, ranking quality, or economic performance.

The same run used normalized-source fallback for CME because a derived
cross-asset-context Parquet was not present. No matured prior live decisions
were available for `live_horizon` monitoring rows.

## Publication and failure boundary

Loop B first materializes all requested routes. If any route is not `READY`,
the runtime raises before creating a run publication. During modeling, any
model or prediction exception aborts the cycle, even when a BACKTEST row could
otherwise be computed. Each horizon's prediction frames are added only after
that horizon completes successfully, so a failing route cannot contribute a
partial horizon publication.

A successfully published immutable run contains:

```text
samples.parquet
predictions.parquet
evaluations.parquet
monitoring.parquet
intelligence.parquet
strategy-candidates.parquet
strategy-audit.parquet
manifest.json
publication.json
```

Promotion then refreshes the predictable `ml/latest` Parquet mirrors and the
`ml-intelligence/latest/rolling-predictions.parquet` compatibility mirror. The
mirror name is not a timestamped-run artifact and is not publication authority.

The immutable run artifacts and verified receipt are complete before Loop B
atomically commits the single authoritative `ml/latest/run.json` pointer.
Predictable Parquets under `ml/latest` and the UI path are compatibility
mirrors, not publication authority. Persisted samples omit closed-lockbox rows.
A later failure can leave an unpromoted timestamped working directory,
including one with a complete manifest but no valid `publication.json`; it is
not a published run. The authoritative pointer and UI continue to expose the
previous successful publication.

Prior LIVE predictions used for reconciliation are loaded only from verified
complete manifests and output inventories. A run that declares the
transactional publication contract must also have a valid receipt bound to its
manifest and be reachable through the authoritative pointer's linked
publication history. Weekly origins are stricter: only complete six-row bundles
from that verified receipt chain, revalidated against the natural samples and
official exchange calendar, may be reused or counted as evidence; the legacy
history path is excluded. Their target starts are excluded from later offline
training, calibration, assessment, and model-time lockbox partitioning so
genuinely prior-LIVE outcomes can mature only through reconciliation.
Incomplete or corrupt run directories are skipped.

## Remaining remediation priorities

1. Route each active family through its specialized loader, or make the generic
   loader enforce the same version, quality, duplicate, freshness, and lineage
   policy.
2. Feed real FRED release/vintage history and replace the current-revised
   single-snapshot macro path for historical decisions.
3. Give legacy fundamental-technical lifecycle rows an explicit immutable
   availability/version key, then join on it.
4. Enforce the intended fundamental carry limits in the integrated dispatch.
5. Persist sufficient family join-audit evidence to reproduce why a sample
   value was present, stale, or missing.
6. Decide whether option coverage/quality evidence is intentionally predictive
   model input. If so, retain that choice explicitly; if not, publish a new
   versioned feature set rather than silently changing `1.2.0`.
7. Make readiness an explicit deployment gate if candidate coverage policy is
   meant to control the production profile.

## Code and test evidence

Primary implementation anchors:

- `ml/feature_registry.py`: profile composition, semantic roles, versions, and
  candidate-state overrides.
- `ml/rolling_materialization.py`: production family dispatch, required input
  checks, generic join behavior, FRED derivation, and CME fallback.
- `ml/datasets/technical.py`: exact current technical assembly.
- `ml/datasets/families.py`: specialized family loaders and freshness
  constants.
- `ml/datasets/point_in_time.py`: backward as-of primitives and audit fields.
- `ml/runtime_pipeline.py`: route completeness, model execution, and
  all-or-nothing publication.
- `ml/model_runtime.py`: chronological partitioning, aggregate weekly model
  overrides, compatibility matching, and offline evaluation metadata.
- `ml/models/registry.py`: estimator construction and logistic parameter
  handling.
- `ml/calibration.py`: Platt fitting and calibration-support clipping.
- `ml/preprocessing.py`: training-only transforms, clipping, imputation,
  scaling, and missing indicators.
- `ml/strategy_selection/chain.py`: exact receipt lineage and entry/exit clocks.
- `ml/strategy_selection/candidates.py`: exact-leg construction and causal
  observed-BBO pseudo-outcomes.
- `ml/strategy_selection/market_state.py`: causal market-state inference and
  exact-mechanics scenario prior.
- `ml/strategy_selection/model.py`: decision-cluster strategy modeling,
  calibration, assessment, and continuous ranking.
- `app/ui/options_strategy_data.py`: current-account display-time overlay.

Relevant regression coverage includes:

- `tests/test_ml_loop_a_all_family_wiring.py`
- `tests/test_ml_point_in_time_ingestion.py`
- `tests/test_ml_readiness.py`
- `tests/test_ml_semantic_feature_contract.py`
- `tests/test_ml_rolling_horizons.py`
- `tests/test_ml_calibration.py`
- `tests/test_ml_required_feature_preprocessing.py`
- `tests/test_ml_weekly_context_model_runtime.py`
- `tests/test_ml_frozen_weekly_runtime.py`
- `tests/test_ml_runtime_pipeline.py`
- `tests/test_runtime_ui_integration.py`
- `tests/test_ml_strategy_selection.py`
- `tests/test_options_strategy_ui.py`

Test names and assertions are durable evidence; historical pass counts are not
part of this contract.
