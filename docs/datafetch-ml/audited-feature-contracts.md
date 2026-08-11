# Audited Loop A feature contracts

Status: current implementation contract

Audited: 2026-08-01

Repository state: the four public selections are represented in this working
tree; public `1w` expands to six internal model routes. Deployment remains an
operator action, and this document makes no live-publication claim.

This document is authoritative for the closed Loop B feature profiles, their
ordered model-value families, the Loop A files they read, and the join rules
used by the integrated runtime. It also distinguishes the aggregate Schwab
option feature row used by directional models from the exact-chain receipt
graph used by the separate strategy model, and from the live account overlay
used only by the UI.

Three contracts must not be conflated:

1. **Loop A persistence** — what a writer calculates and stores.
2. **Semantic registry** — intended provider, version, availability,
   freshness, transform, coverage, and readiness policy.
3. **Integrated Loop B dispatch** — what `ml/rolling_materialization.py`
   actually validates and joins today.

The integrated dispatch is the production enforcement boundary. A stronger
policy in the registry or a specialized helper does not become a runtime
guarantee unless that dispatch calls it.

## Closed runtime profiles

| Runtime profile | `1h` feature set | `4h` feature set | `1d` feature set | `1w` feature set | Role |
| --- | --- | --- | --- | --- | --- |
| `loop-a-all-v1` | `loop-a-all-v1-1h` | `loop-a-all-v1-4h` | `loop-a-all-v1-1d` | `loop-a-all-v1-1w` | Default |
| `production-v1` | `technical-all` | `technical-all-4h` | `technical-all` | `technical-all` | 19-feature compatibility |
| `technical-all-v2` | `technical-all-v2-1h` | `technical-all-v2-4h` | `technical-all-v2-1d` | `technical-all-v2-1w` | 22-feature compatibility |

The four default feature sets are version `1.2.0`.

The weekly column in the profile table applies to aggregate `1w` and internal
`1w-d1` through `1w-d5`. All six use the exact ordered membership of
`loop-a-all-v1-1w`; the route expansion does not discover, add, or remove model
columns.

| Horizon | Model values | Family counts |
| --- | ---: | --- |
| `1h` | 69 | `mr` 13, `bp` 13, `bar` 2, `life` 5, `quote` 1, `opt` 26, `energy` 1, `cme` 8 |
| `4h` | 69 | `mr` 13, `bp` 13, `bar` 2, `life` 5, `quote` 1, `opt` 26, `energy` 1, `cme` 8 |
| `1d` | 139 | `mr` 13, `bp` 13, `bar` 3, `weekly` 3, `life` 5, `fdir` 25, `fund` 13, `ftlife` 17, `quote` 1, `opt` 32, `energy` 1, `macro` 4, `sec` 3, `cme` 6 |
| `1w`, `1w-d1` ... `1w-d5` | 132 each | `mr` 14, `bp` 12, `bar` 3, `weekly` 3, `life` 5, `fdir` 25, `fund` 13, `ftlife` 17, `opt` 29, `macro` 4, `sec` 3, `cme` 4 |

With all public horizons selected, `samples.parquet` contains the ordered union of
143 feature columns. Non-applicable columns are null on that horizon's rows.
Each model still receives only its exact horizon-specific ordered set; repeating
the weekly membership across six row routes does not widen the union.

The three `4h` contracts are horizon-scoped clones of their corresponding
`1h` inventories. In particular, `loop-a-all-v1-4h` has exactly the same
ordered 69 values and family composition as `loop-a-all-v1-1h`. The existing
definitions are not widened in place, so the established `1h`, `1d`, and `1w`
semantic fingerprints remain unchanged.

The `4h` dispatcher maps to the existing canonical `1h` price, technical, and
bar inputs and reuses the `(symbol, "1h")` cache. Market-regime,
breakout-pressure, and bar values retain their exact decision/as-of behavior.
Loop A has no synthetic `4h` fetch, writer, or storage schema.

The CLI exposes closed profile names only. It does not accept an arbitrary
feature set or column list. Public `--horizons 1w` requests all six weekly
routes automatically; operators do not list internal values manually.

## Default model-value allowlist

The definitions in `ml/feature_registry.py` are executable authority. The
lists below make their current composition visible without requiring a code
walk.

### Current technical values

Market regime:

```text
mr__trend_atr
mr__momentum_risk_adjusted
mr__range_position
mr__volume_score
mr__volatility_ratio
mr__technical_score
mr__regime_strength
mr__confidence_score
mr__atr_percent
mr__technical_score_change_5
mr__trend_score
mr__momentum_score
mr__range_score
```

`1w` additionally includes `mr__bars_since_regime_change`; `4h` uses the same
market-regime inventory as `1h`.

Breakout pressure:

```text
bp__compression_score
bp__range_contraction_score
bp__direction_score
bp__upside_pressure_score
bp__downside_pressure_score
bp__breakout_magnitude_atr
bp__volume_participation_score
bp__breakout_readiness_score
bp__breakout_strength_score
bp__setup_quality
bp__confidence_score
bp__boundary_proximity_score
```

`1h` and `4h` additionally include `bp__bars_since_state_change`; `1d`
additionally includes `bp__readiness_change_5`.

These values come from market-regime `1.2.0` and breakout-pressure `1.1.0`.
Both require `FULL` calculation mode, canonical Databento completed bars, and
the exact bar-end-plus-five-minute decision boundary.

### Bar and weekly values

```text
bar__overnight_gap_atr
bar__intrabar_range_atr
bar__close_location

weekly__technical_score
weekly__technical_score_change_5
weekly__breakout_readiness_score
```

`1h` and `4h` omit `bar__overnight_gap_atr` and all weekly values. `1d` and
`1w` include all six.

### Technical lifecycle values

All horizons include:

```text
life__technical_consensus_score
life__technical_consensus_change_5d
life__long_term_technical_score
life__technical_term_spread
life__timing_score
```

This family is the canonical Databento technical-only lifecycle. It is
distinct from the legacy fundamental-technical lifecycle below.

### Legacy fundamental-direction values

`1d` and `1w` include 25 values:

```text
fdir__fundamental_score
fdir__fundamental_confidence
fdir__earnings_momentum_score
fdir__cash_conversion_score
fdir__accrual_quality_score
fdir__balance_sheet_score
fdir__tax_quality_score
fdir__investment_dilution_score
fdir__component_agreement
fdir__component_coverage
fdir__metric_coverage
fdir__revenue_growth
fdir__operating_income_growth
fdir__net_income_growth
fdir__cfo_growth
fdir__receivables_growth_minus_revenue_growth
fdir__inventory_growth_minus_cost_of_revenue_growth
fdir__cfo_growth_minus_net_income_growth
fdir__debt_growth_minus_cfo_growth
fdir__operating_margin
fdir__free_cash_flow_margin
fdir__cfo_to_net_income
fdir__cash_to_debt
fdir__current_ratio
fdir__effective_tax_rate
```

This is the older `fundamental-direction` artifact. Its current Loop B
availability column is `effective_from`.

### Point-in-time fundamental values

`1d` and `1w` include:

```text
fund__revenue_growth_yoy
fund__operating_margin
fund__operating_margin_change_yoy
fund__free_cash_flow_margin
fund__cfo_to_net_income
fund__cash_to_debt
fund__current_ratio
fund__diluted_share_growth_yoy
fund__stock_comp_to_revenue
fund__net_issuance_to_market_cap
fund__buyback_yield
fund__roic
fund__fcf_yield
```

The current Loop A call does not provide a causal market-cap series.
`fund__net_issuance_to_market_cap`, `fund__buyback_yield`, and
`fund__fcf_yield` therefore remain missing in normal live output.

### Legacy fundamental-technical lifecycle values

`1d` and `1w` include:

```text
ftlife__technical_consensus_score
ftlife__technical_timeframe_coverage
ftlife__technical_consensus_confidence
ftlife__short_term_technical_score
ftlife__long_term_technical_score
ftlife__technical_term_spread
ftlife__technical_consensus_change_5d
ftlife__timing_score
ftlife__fundamental_score
ftlife__fundamental_confidence
ftlife__fundamental_change_1q
ftlife__fundamental_acceleration
ftlife__lifecycle_confidence
ftlife__fundamental_technical_spread
ftlife__agreement_strength
ftlife__divergence_strength
ftlife__setup_quality
```

Loop A builds this legacy signal from legacy `fundamental-direction`, not from
the new `point-in-time-fundamentals` artifact.

### Quote and option values

Quote liquidity:

```text
quote__relative_bid_ask_spread
```

It applies to `1h`, `4h`, and `1d`.

The `1d` option set contains the complete current 32-value option allowlist:

```text
opt__iv_minus_realized
opt__put25d_minus_call25d_iv
opt__front_minus_back_iv
opt__atm_move_richness
opt__log_call_put_oi_ratio
opt__log_call_put_volume_ratio
opt__open_interest_concentration
opt__relative_spread
opt__realized_volatility_20d
opt__realized_expected_absolute_move_atm_horizon
opt__atm_implied_volatility
opt__atm_straddle_implied_move
opt__atm_straddle_move_excess
opt__atm_relative_bid_ask_spread
opt__front_atm_implied_volatility
opt__back_atm_implied_volatility
opt__put_25d_implied_volatility
opt__call_25d_implied_volatility
opt__smile_curvature
opt__volume_to_open_interest
opt__put_call_parity_residual
opt__atm_put_call_parity_residual
opt__quote_coverage
opt__quote_time_coverage
opt__iv_coverage
opt__greeks_coverage
opt__open_interest_coverage
opt__intrinsic_value_violation_rate
opt__contract_count
opt__expiration_count
opt__atm_days_to_expiration
opt__quote_staleness_seconds
```

The `1h` and `4h` sets each have the same 26 of these. They omit:

```text
opt__front_minus_back_iv
opt__atm_move_richness
opt__log_call_put_oi_ratio
opt__open_interest_concentration
opt__front_atm_implied_volatility
opt__back_atm_implied_volatility
```

The `1w` set has 29. It omits:

```text
opt__log_call_put_volume_ratio
opt__relative_spread
opt__atm_relative_bid_ask_spread
```

Coverage, contract/expiration counts, days-to-expiration, staleness, and parity
residuals are `MODEL_VALUE`s in `loop-a-all-v1`; they are not audit-only
controls. Explicit controls such as `surface_quality_pass`,
`quote_cutoff_pass`, schema/calculation versions, timestamps, and source
lineage are excluded.

### Shared context and event values

Energy, on `1h`, `4h`, and `1d`:

```text
energy__wti_or_proxy_return
```

FRED macro, on `1d` and `1w`:

```text
macro__fed_funds_level
macro__cpi_yoy
macro__unemployment_change
macro__gdp_yoy
```

SEC, on `1d` and `1w`:

```text
sec__dilution_event
sec__offering_size_to_market_cap
sec__filing_event_impulse
```

CME `1h` and `4h`:

```text
cme__nq_return_1h
cme__es_return_1h
cme__small_cap_breadth
cme__tech_breadth
cme__gold_return
cme__crude_return
cme__relative_spread
cme__book_imbalance
```

`1d` omits `cme__relative_spread` and `cme__book_imbalance`. `1w` retains only
small-cap breadth, tech breadth, gold return, and crude return.

## Loop A persistence contract

This table describes what the current writers produce. “Append/versioned”
means the natural key retains later receipts or calculation versions.
“Atomic replacement” means a file is safely replaced as a unit but is not an
immutable append log.

| Family | Representative path | Current writer contract |
| --- | --- | --- |
| Market regime | `stocks/<S>/technicals/market-regime/databento/<TF>.parquet` | Calculation `1.2.0`; completed adjusted bars; atomic file output; no explicit `available_at` |
| Breakout pressure | `stocks/<S>/technicals/breakout-pressure/databento/<TF>.parquet` | Calculation `1.1.0`; completed adjusted bars; atomic file output; no explicit `available_at` |
| Bar shape | `stocks/<S>/technicals/bar-shape/databento/<TF>.parquet` | Calculation `1.0.0`; explicit completed-bar availability; atomic replacement keyed by symbol/provider/timeframe/bar timestamp |
| Weekly context | `stocks/<S>/technicals/weekly-context/databento/1w.parquet` | Calculation `1.0.0`; canonical daily exchange sessions; explicit completed-week availability; atomic replacement |
| Technical lifecycle | `stocks/<S>/signals/technical-lifecycle/consensus/daily.parquet` | Calculation `1.0.0`; canonical Databento daily inputs; append/versioned by timestamp, availability, calculation, and provider policy |
| Fundamental direction | `stocks/<S>/fundamentals/fundamental-direction/fmp/<period>.parquet` | Legacy calculated history with `effective_from`; atomic rewritten file |
| Point-in-time fundamentals | `stocks/<S>/fundamentals/point-in-time/fmp/<period>.parquet` | Calculation `1.0.0`; append/versioned by symbol, period type/end, and availability |
| Fundamental-technical lifecycle | `stocks/<S>/signals/fundamental-technical-lifecycle/consensus/daily.parquet` | Legacy calculation `1.0.0`; consumes legacy fundamental direction; atomic rewritten file |
| Quote liquidity | `stocks/<S>/quotes/features/quote-liquidity/schwab/YYYY-MM.parquet` | Receipt-time rows keyed by symbol and availability; crossed, locked, and nonpositive quotes fail, live-session stale quotes fail, and closed-session stale quotes persist with `quote_quality_pass=false` |
| Option quality | `stocks/<S>/options/features/option-quality/schwab/YYYY-MM.parquet` | Calculation `1.2.0`, schema `option-surface-v2`; append/versioned by symbol, intended snapshot, and receipt availability |
| Energy context | `pools/macro/features/energy-context/fmp/quote.parquet` | Direct WTI or explicit proxy; return is suppressed across instrument-chain changes |
| SEC events | `stocks/<S>/corporate/sec-events/sec/YYYY.parquet` | Versioned extraction keyed by symbol, filing acceptance, event type, and availability |
| CME context | `pools/cme/features/cross-asset-context/databento/1h.parquet` | Exact common NQ/ES/RTY/GC/CL hourly window plus BBO/MBP evidence; written only when the context passes |
| Normalized FRED | `pools/macro/<GROUP>/<SERIES>/fred/normalized/*.parquet` | Current-revised CSV histories with local `fetched_at`; not an ALFRED vintage history |

Option `snapshot_for` is the latest completed Databento one-minute bar ending
on a wall-clock quarter hour. Options run in the provider stage before the
current cycle's technical calculations, so realized-volatility evidence uses
an already persisted prior-cycle market-regime result or remains missing on a
clean first cycle.

### Two Schwab option feature surfaces

The same immutable Loop A receipt family serves two distinct model contracts:

1. Directional `loop-a-all-v1` joins the one-row option-quality artifact and
   projects only the explicit `opt__*` values listed above. Its integrated
   dispatch is the generic backward-as-of path with the documented freshness
   limits.
2. `schwab-spreads-v1` strategy analytics reads the normalized exact contract
   history, its matching option-quality surface receipt, and the stock BBO
   receipt history directly. It does not infer strikes, expirations, contracts,
   bid/ask values, or Greeks from the aggregate `opt__*` row.

The strategy receipt contracts are:

| Input | Natural receipt grain | Required version evidence |
| --- | --- | --- |
| `stocks/<S>/options/chains/schwab/normalized/YYYY-MM.parquet` | `symbol, snapshot_for, available_at, contract_symbol` | option-chain schema `1.1.0` |
| `stocks/<S>/options/features/option-quality/schwab/YYYY-MM.parquet` | `symbol, snapshot_for, available_at` | calculation `1.2.0`, schema `option-surface-v2`, policy `schwab-option-surface-quality-v1` |
| `stocks/<S>/quotes/features/quote-liquidity/schwab/YYYY-MM.parquet` | `symbol, available_at` | schema `quote-liquidity-v1`, policy `schwab-quote-quality-v1` |

For strategy entry, `snapshot_for` must be at or after the Loop B completed
`bar_end_timestamp` and no later than the causal cutoff. `available_at` must
be at or after `information_available_at` and no later than the earlier of the
completed Loop A cycle `finished_at` or one nanosecond before
`target_window_start`.
Historical construction uses the earliest eligible entry surface; the current
pass uses the latest. Contracts are selected only from the chosen exact
`symbol, snapshot_for, available_at` surface key. Historical strategy outcomes
use the earliest eligible future receipt at or after the fixed target end,
enforce exact contract-symbol continuity, and cannot cross the real-lockbox
boundary. Surface quality, quote validity, liquidity, stock-quote quality, and
quote age remain explicit model/output measurements; a numerically
constructible standard 100-multiplier candidate is not suppressed solely
because a diagnostic is false.

After the required directional route has produced its separate calibrated up
probability, `point-in-time-market-state-v1` combines that value with this same
causal entry surface and audited sample context. Expected absolute move comes
from `realized_expected_absolute_move_atm_horizon` or
`atm_straddle_implied_move`, scaled to the candidate holding window, with
`mr__atr_percent` as fallback. Expected realized volatility comes from the
surface's `realized_volatility_20d` or its already-audited `opt__` projection.
Trend persistence and mean-reversion tendency summarize audited `mr__` and
`bp__` values; normalized direction entropy supplies uncertainty. These are
point-in-time market descriptions, not future strategy outcomes.

This strategy path does not change the four closed directional feature sets or
their version. Active `loop-a-all-v1` remains `1.2.0` for `1h`, `4h`, `1d`,
and weekly routes.

The active Loop A FRED lane writes a current FEDFUNDS receipt to the
release-context path using the local fetch time. This is live coverage only for
later decisions. The repository also contains true FRED vintage derivation
code, but Loop A does not fetch that archive; do not treat the current-receipt
row as historical coverage.

Loop A files are atomically written one at a time. Cross-loop consistency comes
from a datastore cycle lock rather than extra Parquet fields: Loop A marks the
small `.duckets-loop-a-cycle.json` state `WRITING`, holds the operating-system
lock for the complete write cycle, and publishes `COMPLETE` before releasing
it. Loop B holds the same lock for its complete read/model/publication cycle.
No readiness lease, decision handoff, or acknowledgement is involved.

## Integrated Loop B wiring

Loop B builds a decision grid, attaches current technical values, and then
dispatches additional families in the order below.

| Family | Loader used by `loop-a-all-v1` | Availability used | Freshness used | Runtime gates actually applied |
| --- | --- | --- | --- | --- |
| `mr`, `bp` | Strict technical assembler | Reconstructed bar end + 5 minutes | Exact | Calculation/version/mode, canonical source timing, price-adjustment and split basis |
| `bar` | Specialized bar-shape loader | `available_at` | Exact | Schema/calculation/completion/timing checks |
| `weekly` | Specialized weekly loader | `available_at` | 8 days | Schema/calculation/completion/calendar/timing checks |
| `life` | Generic symbol as-of | `available_at` | 2 days (`1h`,`4h`,`1d`), 8 days (`1w`) | Required columns and some numeric data; no integrated `constituent_complete` gate |
| `fdir` | Generic symbol as-of | `effective_from` | None | Required columns and some numeric data |
| `fund` | Generic symbol as-of | `available_at` | None | Required columns and some numeric data |
| `ftlife` | Generic symbol as-of | `timestamp` | 2 days (`1d`), 8 days (`1w`) | Required columns and some numeric data |
| `quote` | Generic symbol as-of | `available_at` | 5 minutes (`1h`,`4h`), 1 day (`1d`) | Required columns and some numeric data; relies on Loop A writer quality |
| `opt` | Generic symbol as-of | `available_at` | 2 hours (`1h`,`4h`), 1 day (`1d`), 3 days (`1w`) | Required columns and some numeric data; specialized surface gates are not called |
| `energy` | Generic shared as-of | `available_at` | 30 minutes (`1h`,`4h`), 1 day (`1d`) | Required columns and some numeric data; chain controls are not revalidated |
| `macro` | Current context derivation + generic shared as-of | Max selected normalized `fetched_at` | 120 days (`1d`,`1w`) | Four source files and required lag values; no release/vintage reconstruction |
| `sec` | Specialized SEC event loader | `available_at` | First eligible decision | Event timing, coverage boundary, extraction state, and impulse behavior |
| `cme` | Generic shared as-of | Derived `available_at`, or normalized fallback receipt | 15 minutes (`1h`,`4h`), 1 day (`1d`), 3 days (`1w`) | Uses derived file when present; otherwise derives one current context from normalized inputs |

Every configured symbol must have the required symbol-scoped source
partitions. Each required family must expose all selected source columns and
at least one populated numeric value somewhere in its combined source. This is
a family-level availability check, not a promise that every sample row or
feature is populated.

The generic joins:

- reject future availability through backward as-of semantics;
- never backfill before a family's first available row;
- apply only the freshness value passed by the integrated dispatch;
- sort deterministically and keep the last same-availability row; and
- leave a row missing after freshness expiry.

They do not automatically validate calculation/schema versions, upstream
quality flags, every natural-key component, or every registry policy.

### Current macro behavior

The macro route reads current-revised normalized histories for:

```text
FEDFUNDS
CPIAUCSL
UNRATE
GDP
```

It selects the latest observation and required lag from each history,
calculates the four model values, and creates one shared context row whose
availability is the maximum `fetched_at` across the selected inputs. Older
decision rows before that receipt remain missing. This is causal with respect
to the local receipt, but it is not a historical release-vintage
reconstruction and can contain revisions unavailable at the original
observation date.

### Current CME fallback

If a derived cross-asset-context Parquet exists, Loop B uses it. Otherwise it
reads the current normalized CME OHLCV, BBO, and MBP inputs and derives one
context from their latest common completed full hour. The fallback is useful
for continuity, but it must not be described as equivalent to a durable,
historically versioned derived-context series.

## Missing values and model projection

Missing values are allowed when:

- a family was not yet available at the decision;
- the latest row exceeded its configured freshness;
- an upstream calculation could not causally compute the value;
- an indicator was still initializing; or
- the feature applies only to another horizon.

The join layer creates audit columns such as family availability, age,
staleness, and join status in memory. The final sample projection persists the
base sample fields and selected model values only; those family audit columns
are not in `samples.parquet`. Assembly-only feature availability is validated
in memory and discarded when it duplicates the decision timestamp. Computation
timing stays with the source artifact, and materialization timing stays in the
run manifest rather than being repeated on every sample row.

Only `MODEL_VALUE` features enter the model. Explicit audit controls, IDs,
timestamps, status strings, calculation/schema versions, quality flags, and
lineage fields are excluded. The option evidence fields specifically listed
in the allowlist are model values despite their audit-like names.

Preprocessing is fitted on training data only:

- semantic per-feature transforms run first;
- `log1p-capped-training-v2` uses a 99.75th-percentile upper cap before
  `log1p`;
- logistic models use median imputation, 0.25th/99.75th-percentile clipping,
  robust scaling, and missing indicators;
- tree models use median imputation and missing indicators; and
- an all-missing required column is retained rather than silently removing it.

The manifest records preprocessing policy
`training-quantiles-0.25-99.75-v1`, including the training-only fit rule,
numeric clipping bounds, and semantic cap mapping. A prior-policy artifact
does not pass model-reuse compatibility.

## Strategy-model feature projection

The exact-chain strategy model has its own feature contract and does not widen
the directional `FeatureSet`. Its fixed numeric candidate values cover
underlying/expiration geometry, leg and width counts, entry debit/credit,
profit/loss/capital measurements, aggregate Greeks, spread/open-interest/volume
measurements, normalized debit/loss ratios, quote age, and the four surface,
option-quote, liquidity, and stock-quote quality booleans. It also includes the
five persisted market-state values and the scenario prior's profit probability
and expected return. Its categorical values are:

```text
strategy_name
strategy_family
risk_form
expiration_structure
stock_requirement
cash_requirement
```

During historical outcome construction, Loop B also attaches
`previous_period_direction` and every sample column containing `__` to the
working candidate frame. The fitted strategy matrix then selects only numeric
context under this explicit prefix allowlist:

```text
technical__  bar__   weekly__  life__   fdir__  fund__  ftlife__
quote__      opt__   energy__  macro__  sec__   cme__
mr__         bp__
```

This means applicable audited bar, weekly, lifecycle, fundamental, quote,
option, energy, macro, SEC, and CME values can supply point-in-time strategy
context. Compatibility-profile `technical__*` values can also enter. The
default directional `mr__*` and `bp__*` values enter as audited context without
changing their membership in the directional `loop-a-all-v1` contracts. No
arbitrary numeric column is discovered for strategy fitting.

The two nonlinear strategy estimators fit preprocessing on training only:
median imputation, 0.25th/99.75th-percentile clipping, robust scaling, missing
indicators, and an unknown-safe one-hot encoder. The classifier predicts
profitable outcome. The regressor predicts return-on-risk residual relative to
the point-in-time Greek/BBO scenario prior. Platt calibration is fit only on the
following calibration clusters, with equal total weight per
`target_window_start`; it calibrates only the classifier and does not refit
preprocessing. Assessment and the real lockbox contribute no fit statistics.

Only the candidate schema's exact-chain, five market-state, prior, and fitted
model measurements are persisted to `strategy-candidates.parquet`. The broader
attached directional sample context is recorded through model
compatibility/input inventory rather than copied as extra candidate columns.

## Live portfolio facts are not model features

The Options Strategies UI loads current Schwab account facts after it resolves
the immutable candidate publication. It derives applicable shares, absolute
option-position count, working option-order count, and available funds. Policy
`current-schwab-position-fit-v2` produces only feasibility/exposure text;
it cannot change Predictive Score or the persisted market rank. These values
can legitimately change after Loop B publishes.

No account holding, buying-power value, working order, UI portfolio-fit value,
or order-draft value is joined to a historical sample, supplied to the
strategy estimator or calibrator, or written back to either strategy Parquet.
This boundary prevents a mutable present-day account snapshot from being
silently described as a point-in-time ML feature. The exact scoring and order
rules are in
[Loop B options-strategy selection](options-strategy-selection.md).

## Registry readiness versus production activation

Candidate sets such as `option-candidate-v1`, `macro-candidate-v1`, and
`fundamental-candidate-v1` remain
`IMPLEMENTED_BUT_QUARANTINED`. Their readiness policies describe the evidence
that would be required to promote those candidate sets on their own.

At the same time, `_active_features_for_horizon` creates active copies of
applicable candidate features for `loop-a-all-v1`. For `bar`, `weekly`,
`macro`, and `cme`, it also relaxes selected required-version metadata.

Therefore:

- candidate-set quarantine does not block the default integrated profile;
- `ACTIVE` means selected by that closed profile, not coverage-qualified;
- the readiness evaluator is available to audit tooling and tests but is not
  invoked by `ml.prediction_runtime`; and
- a live source can be sparsely populated while its columns remain active.

Any future decision to make readiness a production gate must be implemented in
the runtime; editing registry descriptions alone is insufficient.

## Semantic compatibility and model reuse

Each `FeatureSet` is an ordered model-input contract. The model manifest stores
its name, version, semantic definitions, canonical metadata, and fingerprint.
A feature definition includes its family/source column, dtype, horizons,
provider/source grain, expected versions, availability rule, freshness,
missing policy, transform, coverage policy, readiness policy, activation
state, and value role.

Model reuse additionally validates the horizon and target/cost configuration,
ordered feature fingerprint, input paths/sizes/modification times, row counts,
training-through boundary, Python/package compatibility, artifact checksum,
and model configuration before deserialization. A mismatch retrains.

For `1h` and `4h`, that compatibility metadata contains the complete readable
v2 specification (`next-60-eligible-regular-minutes-open-close-v2` or
`next-240-eligible-regular-minutes-open-close-v2`), adjusted native-`1m`
target-price/constituent policy, calendar policy, processing delay, one-time
cost convention, and assumed round-trip cost. These horizon-scoped blocks
invalidate incompatible `1h` and `4h` reuse without forcing unrelated `1d` or
`1w` retraining.

Each internal weekly model also records its readable target specification,
new target-definition version, and cost. Aggregate `1w` therefore cannot reuse
the retired weekly-context next-session model, while `1w-d1` through `1w-d5`
remain separate ordinary model routes. This target versioning does not change
the existing 132-column weekly membership. There is no multi-output model,
legacy target fallback, automatic feature discovery, or automatic reduction of
partition requirements.

Estimator and calibration policy are separate from feature membership. When
the aggregate `1w` route uses the logistic family, its model configuration is
L1 with `C=0.3`, `l1_ratio=1.0`, `liblinear`, `max_iter=5000`, and `tol=1e-5`.
When that route also uses Platt calibration, the calibrator configuration uses
`C=0.1` and clips raw probabilities to the calibration partition's observed
minimum/maximum before applying the Platt mapping. These parameter maps are
part of model-manifest compatibility. They apply neither to `1w-d1` through
`1w-d5` nor to a non-logistic aggregate estimator, and they do not create a
new feature-set version. The offline evaluation block records calibration
support and assessment probabilities outside it for every newly fitted model.

Changing feature order, membership, transforms, timing, or roles requires a
new versioned feature set. It must not be silently folded into
`loop-a-all-v1` version `1.2.0`.

## Known contract gaps

The current implementation is honest about these remaining differences:

1. FRED vintage/release persistence exists in code but is not fed by a true
   archive importer. Loop A supplies only a current-revised receipt snapshot;
   its FEDFUNDS bridge is valid for future Pricing targets, not backtests.
2. Legacy fundamental-technical lifecycle joins on `timestamp`, not an
   immutable calculation receipt. Historical rebuild timing is not preserved
   in the integrated join.
3. The integrated fundamental joins omit the registry's 120-day quarterly and
   400-day annual freshness limits.
4. Generic option, quote, lifecycle, energy, and CME joins do not reapply all
   specialized quality gates.
5. Same-availability duplicates in generic joins are kept-last rather than
   universally failing closed.
6. Join-audit columns are not persisted with final samples.
7. Loop A does not commit all of its Parquets as one datastore-generation
   transaction. Cross-loop consistency instead comes from the shared
   crash-released cycle lock plus the atomic `WRITING`/`COMPLETE`/`FAILED` JSON
   state: Loop B holds the same lock while reading and publishing, so it cannot
   mix adjacent Loop A cycles, but no generation field is copied into Parquet.

See `loop-b-point-in-time-feature-audit.md` for the dated evidence and
remediation priorities, `rolling_forecasts.md` for horizon timing,
`ml_prediction_runtime.md` for training/publication, and
`parquet-id-contract.md` for natural-key rules.
