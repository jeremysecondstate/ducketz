# Loop-native BSGP production implementation report

Date: 2026-08-11

This report records the implemented code path, validation evidence, measured
fixture timings, the read-only production-history audit, and the operator
cutover procedure. No live process was stopped or restarted, no file under
`C:\DATASTORE` was changed, no order was submitted, and no paid provider request
was made while producing this report.

## Outcome

The Options Strategies Predictive Score path now consumes exact-contract
Black-Scholes/BSGP evidence before Strategy training and scoring. The persisted
`decision_score` remains:

```text
P(strategy net profit after executable spreads and fees > 0 over the horizon)
```

It is stored in `[0, 1]`; the UI displays the persisted value multiplied by 100.
Expected Return remains a separate payoff-magnitude estimate. Candidate rank is
computed only after the final probability is available, persisted, and read by
the UI without recomputation.

The implementation is complete and testable, but it is not yet cut over. The
existing live immutable history contains no causal GP training rows, so the
honest initial production behavior after cutover is Black-Scholes fallback until
new causal observations accumulate.

## Implemented architecture

The runtime dependency chain is now:

```text
all-symbol Databento 1m persistence
    -> atomic exact-target readiness receipt
    -> active Pricing inference and atomic publication
    -> Schwab option-chain capture, with bounded optional Pricing wait
    -> Loop A slow enrichment continues independently
    -> Loop B publication
    -> exact-leg Strategy pricing enrichment
    -> calibrated profitable-outcome probability and rerank
    -> Options Strategies UI
```

The readiness callback is issued immediately after the required 1-minute bars
are persisted. It does not wait for 1-second/history compaction, derived
timeframes, technicals, FMP, FRED, SEC, or other slow work. Pricing handles each
readiness target idempotently. In required-readiness mode, an early missing
receipt is retryable and does not publish an empty terminal artifact. A symbol
failure is isolated from the remaining symbols. Options waits briefly for the
matching Pricing target pointer, but captures the option chain on timeout or
Pricing failure so the next observation is not lost.

Pricing, Options, Loop B, and Strategy retain verified atomic generations,
receipts, manifests, and pointers. A reader validates the generation before
exposing it. Strategy deduplicates natural targets before opening Parquet,
caches verified immutable outcome inputs, and reuses a compatible model rather
than rematerializing the full history for every publication.

## Contract-level quantitative path

For each option contract, causal dividend-adjusted Black-Scholes supplies the
mean function. The finite-feature GP learns normalized market discrepancy:

```text
residual_target = (observed option value - Black-Scholes value) / underlying
fair_value_raw = Black-Scholes value + underlying * shrunken_residual_mean
fair_uncertainty = underlying * calibrated_residual_standard_deviation
```

The raw mean and interval surfaces are then projected into the existing
American-option constraints: nonnegative price, American lower and upper
bounds, strike monotonicity, and strike convexity. If a compatible GP artifact
is unavailable, out of support, or too sparse, residual mean is exactly zero,
residual shrinkage is zero, and the interval widens around the Black-Scholes
mean. Black-Scholes inference does not depend on research eligibility gates.

The pooled model remains separate by CALL and PUT across all configured
symbols. Each model uses robust scaling, RBF Nystroem features, Bayesian Ridge,
session-balanced weights, chronological session partitions, predictive-interval
calibration, and support-sensitive residual shrinkage. `FiniteBasisGP` exposes
same-model posterior joint covariance. Candidate persistence deliberately uses
a conservative L1 interval bound across option legs:

```text
candidate_pricing_uncertainty = sum(abs(quantity * multiplier) * leg_stddev)
```

This bound does not invent independence or CALL/PUT cross-model correlation.

## Exact-leg pricing evidence

A price prediction must match all of symbol, target timestamp, contract symbol,
call/put, expiration date, strike, and multiplier. Both prediction creation and
atomic publication must be strictly before the executable quote. The target and
the effective pricing input age must each satisfy the 20-minute contract policy.
Offline replay evidence is accepted only for historical fitting; live scoring
requires `evidence_lane=LIVE`.

For quantity `q` and multiplier `m`, candidate edge is the sum of:

```text
long:  q * m * (fair_value - executable_ask)
short: q * m * (executable_bid - fair_value)
```

The conservative 95% edge is the sum of:

```text
long:  q * m * (fair_value_95_lower - executable_ask)
short: q * m * (executable_bid - fair_value_95_upper)
```

The pre-score candidate record includes `pricing_leg_coverage`,
`pricing_candidate_edge`, `pricing_conservative_edge`,
`pricing_edge_to_friction`, `pricing_uncertainty`,
`pricing_probability_favorable`, `pricing_relative_edge`,
`pricing_model_age_seconds`, `pricing_residual_shrinkage`, and
`pricing_source`. Stock legs contribute no option-pricing edge. Incomplete,
invalid, future, or stale evidence makes only the affected candidate
`Delayed`/`Unavailable`; it is not reused as current evidence.

## Predictive Score formula and score bases

The same pricing columns are attached before both historical model fitting and
live scoring. They are part of the Strategy numeric feature contract alongside
market state, liquidity, Greeks, and the pricing-informed scenario prior. The
horizon-specific HistGradientBoosting profitable-outcome classifier is trained
on the unchanged observed target `net_profit > 0` after spreads and fees, using
chronological/session-blocked partitions and Platt calibration.

For a compatible fitted model:

```text
decision_score = PlattCalibrator(
    HistGradientBoostingClassifier.predict_proba(
        market + strategy + scenario prior + liquidity + Greeks + pricing
    )[:, 1]
)
```

No post-model Black-Scholes bonus or hand-selected weighting is applied.

When no compatible fitted Strategy model exists, the fallback is still a
probability rather than a bonus. For each of 258 weighted directional/magnitude
scenarios:

```text
base_profit = delta*dS + 0.5*gamma*dS^2 + theta*holding_days - friction
profit = payoff_bounds(base_profit + pricing_candidate_edge)
scenario_probability = NormalCDF(profit / pricing_uncertainty)
decision_score = sum(scenario_weight * scenario_probability)
```

If pricing uncertainty is zero, the strict indicator `profit > 0` replaces the
normal CDF. The output is checked finite and clipped only to the probability
domain.

Persisted/UI score bases are:

| Persisted basis | Required path | UI text |
| --- | --- | --- |
| `BSGP_CALIBRATED_MODEL` | BSGP pricing plus compatible calibrated Strategy model | `BSGP + Strategy ML` |
| `BLACK_SCHOLES_CALIBRATED_MODEL` | Black-Scholes fallback evidence plus compatible calibrated Strategy model | `Black-Scholes + ML` |
| `PRICING_SCENARIO_FALLBACK` | No compatible Strategy model; pricing-informed scenario probability | `Pricing Scenario` |

The UI reads persisted `decision_score` and `candidate_rank`. It does not
recalculate either field. The table layout and order ticket remain unchanged;
only the existing Score Basis width was adjusted so all three labels are
visible.

## Intentional version migrations

| Contract | New version |
| --- | --- |
| Pricing policy | `black-scholes-rbf-residual-v2` |
| Pricing schema | `option-pricing-v2` |
| Pricing uncertainty | `cluster-weighted-80-95-bs-fallback-v2` |
| Loop-native model | `loop-native-pooled-bsgp-active-v3` |
| Loop-native surface schema/profile | `loop-native-bsgp-active-v2` |
| Directional feature profile | `loop-a-all-bsgp-active-v2` |
| Strategy candidate policy | `schwab-exact-chain-pricing-candidates-v4` |
| Strategy market state | `point-in-time-market-state-pricing-v2` |
| Strategy prior | `pricing-greek-bbo-scenario-prior-v3` |
| Strategy model | `pricing-market-state-hgb-platt-return-v5` |
| Strategy ranking | `post-pricing-probability-first-ranking-v4` |
| Strategy candidate schema | `strategy-candidate-v3` |
| Strategy publication/pointer | `strategy-publication-v3` / `strategy-pointer-v3` |

Writers, manifests, readers, UI compatibility checks, Parquet contracts, and
tests were updated together. Old accumulated compact surfaces cannot become
fresh when republished: target/event time and first availability are both
preserved and both participate in freshness validation.

## Historical bootstrap result

The implementation provides an immutable `OFFLINE_SCHWAB_BOOTSTRAP` replay lane.
For every observation, an earlier source snapshot must exist, the emulated
prediction must precede the later observed quote and receipt, and all underlying,
rate, dividend, and volatility inputs must have been known at the emulated
decision time. Cross-fit BSGP is applied only to assessment sessions after every
training/calibration label used by that model was available. Earlier or
unsupported replay observations remain exact Black-Scholes baselines.

A read-only audit of `C:\DATASTORE` on 2026-08-11 found 4,604 immutable Schwab
snapshot receipts across the ten symbols (460 or 461 per symbol). The current
causal materialization evaluated 528 offline source/target pairs and accepted
zero: 448 target requests were not after a possible emulated prediction, and 80
targets were outside the regular session. Therefore no real historical row was
promoted, no receipt was fabricated, and no real-market BSGP or Strategy
calibration metric can yet be reported. This is the correct causal result, not a
failed-open condition; production starts with the Black-Scholes path and wider
uncertainty.

The deterministic replay integration fixture produced 180 observations: 120
chronologically earlier Black-Scholes baseline observations and 60 held-out
assessment observations eligible for causal cross-fit BSGP. This proves the
lane and guards work, but is not production evidence.

## Model and calibration measurements

The reproducible structural benchmark used 4,800 deterministic training rows
and 14,000 inference rows across all ten symbols. It is a numerical-capacity and
local-latency benchmark, not market-performance evidence.

| Nystroem components | Assessment normalized RMSE | Fit + gamma selection | 10-symbol inference p95 | Result |
| ---: | ---: | ---: | ---: | --- |
| 128 | 0.000701768 | 0.4434 s | 0.0339 s | retained |
| 256 | 0.000701806 | 0.7196 s | 0.0598 s | accepted but larger/slower |

Both configurations met the contained benchmark limits of normalized RMSE at
most 0.01 and inference p95 below 30 seconds. The production policy retains the
smallest passing configuration, 128 components.

The temporary Strategy smoke fixture had only two assessment decisions and 304
assessment rows. Its raw classifier metrics were Brier 0.137345, log loss
0.430336, AUC 0.895182, and accuracy 0.789474. Platt-calibrated metrics were
Brier 0.191312, log loss 0.561560, AUC 0.895182, and accuracy 0.631579. Expected
Return MAE was 2.243640 and RMSE was 13.508622. Calibration worsened on this tiny
synthetic assessment set, so these figures validate artifact plumbing only and
must not be interpreted as an accepted production calibration or profitability
claim.

A controlled two-candidate pricing fixture changed only valid pricing evidence.
Favoring candidate A produced scores approximately `1.0` and `2.22e-16`, ranks
A=1/B=2; favoring candidate B reversed both persisted scores and ranks. This
directly verifies that pricing evidence can affect Predictive Score and ranking.

## Timing measurements

All measurements were local fixture/temp-datastore runs on the current machine.
They made no provider calls.

| Path | Measurement | Acceptance |
| --- | ---: | ---: |
| all-ten-symbol readiness publication | 0.2051 s | <= 5 s |
| Pricing target authority, 14,000 contracts, five-run p95 | 4.5606 s | <= 30 s |
| Pricing full fixture end-to-end, five-run p95 | 12.1385 s | <= 30 s |
| matching Pricing publication to Options capture start | 1.15 s | <= 5 s |
| full Strategy temporary rebuild | 33.5108 s | <= 300 s |
| incremental Strategy publication | 3.2323 s | <= 60 s |

The first 14,000-contract profile took 43.9699 seconds because Python
`tracemalloc` instrumented every allocation in the hot path. Replacing it with
native process peak-RSS measurement reduced the target-authority p95 to 4.5606
seconds while retaining the memory guard. Continuous live backlog behavior was
not measured because restarting or shadowing the running production owners was
outside the authorized safety envelope; it remains a cutover monitoring item.

## Validation performed

- Full suite: `855 passed`, `3,582 warnings`, 167.13 seconds. Warnings are the
  existing joblib/NumPy and exchange-calendars deprecations.
- Focused schema/atomic verification: 30 tests passed in 5.65 seconds.
- Python bytecode compilation completed for `app`, `datafetching`, `ml`,
  `benchmarks`, and `tests`.
- `git diff --check` reported no whitespace errors (only Windows line-ending
  notices).
- All six documented runtime modules returned successful `--help` output and
  the documented flags were verified.
- Temporary-datastore smoke: 10 historical observations, 160 live candidate
  rows, full rebuild and incremental reuse both completed.
- Tests cover BS plus residual identity, exact BS fallback, future-data guards,
  surface first-availability freshness, exact semantic contract matching,
  long/short signs, multi-leg L1 uncertainty, pre-model pricing columns,
  score/rank sensitivity, stored/UI probability bounds, all score-basis labels,
  symbol isolation, capture-on-Pricing-delay, atomic generation visibility,
  stale-pricing rejection, and unchanged exact-leg order-ticket safety.

The real `OptionsStrategiesTab` was rendered with fixtures and visually checked
against `I:\Shared drives\SECONDSTATE\DUMPER\OPT-STRATS-BLACK-SCHOLES.jpg`.
The validation image is
`artifacts/validation/options-strategies-bsgp.png`. Rank, Predictive Score, and
all three Score Basis labels are visible in the existing table, and the order
workflow is unchanged.

## Files changed

Runtime and orchestration:

- `datafetching/databento_fetch.py`, `datafetching/main.py`,
  `datafetching/orchestrate.py`
- `ml/option_pricing_runtime.py`, `ml/strategy_runtime.py`,
  `ml/strategy_publication.py`, `ml/rolling_materialization.py`

Pricing and Strategy implementation:

- `ml/option_pricing/consumers.py`, `model.py`, `policies.py`, `prediction.py`,
  `publication.py`, `reporting.py`, `shadow_model.py`, `strategy_outcomes.py`,
  `strategy_shadow.py`, and `target_outcome.py`
- `ml/strategy_selection/chain.py`, `contracts.py`, `market_state.py`,
  `model.py`, and `runtime.py`
- `ml/feature_registry.py`, `ml/horizons.py`, `ml/parquet_contracts.py`, and
  `ml/option_pricing_admin.py`

UI:

- `app/ui/options_strategy_data.py`, `app/ui/options_strategies.py`

Tests and reproducible validation:

- `tests/test_loop_a_batch_fetching.py`, `test_loop_a_integration.py`,
  `test_market_cycle_coordination.py`, `test_ml_runtime_pipeline.py`,
  `test_ml_strategy_selection.py`, `test_option_pricing_core.py`,
  `test_option_pricing_loop_native_bsgp.py`,
  `test_option_pricing_shadow_consumers.py`, `test_options_strategy_ui.py`, and
  `test_pricing_options_sequencing.py`
- `benchmarks/benchmark_bsgp_components.py`
- `benchmarks/render_options_strategy_fixture.py`
- `artifacts/validation/options-strategies-bsgp.png`

Documentation:

- `docs/datafetch-ml/current_start_command`, `current_prediction_command`,
  `duckets_datafetching_ml_orchestration_map.md`,
  `independent-runtime-orchestration.md`, `ml_prediction_runtime.md`,
  `option-pricing-shadow.md`, `options-strategy-selection.md`,
  `parquet-id-contract.md`, `rolling_forecasts.md`, and this report.

## Operator-approved migration and cutover

Do not run these steps while the existing owners are active. The cutover was not
executed as part of this implementation.

1. In the six existing runtime terminals, stop downstream first with Ctrl+C:
   Strategy, Loop B, Options, Pricing, Loop A, then CME. Wait for each process to
   exit. Do not delete lock files and do not start a second owner.
2. Verify that no old owner is still running:

   ```powershell
   Get-CimInstance Win32_Process |
     Where-Object { $_.CommandLine -match 'ml\.strategy_runtime|ml\.prediction_runtime|datafetching\.options_runtime|ml\.option_pricing_runtime|datafetching\.orchestrate|datafetching\.cme_runtime' } |
     Select-Object ProcessId, CommandLine
   ```

3. Deploy this working tree. No in-place Parquet rewrite is required. New
   schema generations are written side-by-side and become visible only through
   their atomic pointers.
4. In every new PowerShell terminal, run:

   ```powershell
   cd C:\dev\ducketz
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\.venv\Scripts\Activate.ps1
   ```

5. If the datastore predates the point-in-time rate bridge, run once:

   ```powershell
   python -m ml.option_pricing_admin --datastore-target pc capture-current-rate
   ```

6. Start the six owners in this exact order, one command per terminal:

   ```powershell
   python -m datafetching.cme_runtime --datastore-target pc --max-concurrency 1

   python -m datafetching.orchestrate --datastore-target pc --watchlist datafetching\watchlist.txt --providers databento fmp fred schwab sec --cme-mode external --options-mode external --interval-minutes 15

   python -m ml.option_pricing_runtime --datastore-target pc --watchlist datafetching\watchlist.txt --interval-minutes 15 --phase-offset-minutes 1 --bar-readiness-mode required --bar-readiness-timeout-seconds 30

   python -m datafetching.options_runtime --datastore-target pc --watchlist datafetching\watchlist.txt --interval-minutes 15 --phase-offset-minutes 1 --pricing-barrier-timeout-seconds 45 --bar-readiness-mode required

   python -m ml.prediction_runtime --datastore-target pc --watchlist datafetching\watchlist.txt --provider databento --horizons 1h 4h 1d 1w --feature-profile loop-a-all-bsgp-active-v2 --model-family logistic --calibration platt --round-trip-cost 0.001 --interval-minutes 15 --phase-offset-minutes 5

   python -m ml.strategy_runtime --datastore-target pc --interval-minutes 15 --phase-offset-minutes 10 --pricing-mode active
   ```

7. Confirm, in order, a new all-symbol readiness receipt, Pricing target pointer,
   option snapshot receipt (with Pricing lineage when available), Loop B pointer,
   and Strategy v3 pointer. Then open the existing Options Strategies screen and
   verify Rank, Predictive Score, and Score Basis against the atomic Strategy
   generation. `python -m ml.option_pricing_admin --datastore-target pc status`
   is diagnostic only and cannot block Black-Scholes fallback.

The single authoritative maintained startup command document is
`docs/datafetch-ml/current_start_command`.

## Remaining factual limitations

- The cutover and live-process restart were not executed.
- The production datastore has zero causally eligible historical BSGP rows at
  this time. Initial production scoring will therefore use Black-Scholes plus a
  compatible Strategy model where one can be trained, otherwise Pricing
  Scenario fallback.
- No real-market BSGP accuracy, Strategy calibration, profitability, or economic
  value claim is supported yet. The reported model metrics are deterministic
  fixtures and are labeled as such.
- Same CALL/PUT-model covariance is available through the GP API, but a dense
  covariance matrix is not persisted in the candidate schema. Candidate
  aggregation uses the documented conservative L1 interval bound.
- Black-Scholes fallback uncertainty currently uses the explicit normalized
  policy floor; live residual coverage should be monitored and recalibrated as
  causal labels accumulate.
- Sustained no-backlog behavior and live p95 latency remain to be verified after
  operator cutover without making paid requests.
- Research eligibility and quality reports remain available for monitoring and
  promotion research, but are no longer dependencies of valid Black-Scholes
  production inference or the live decision path.
