# Shadow option pricing: Black-Scholes plus a finite-feature GP

Eligibility protocol v2, candidate/lockbox controls, continuous-runtime
operations, rollback, and disaster recovery are specified in
[`option-pricing-operations.md`](option-pricing-operations.md). Existing v1
artifacts described below remain readable but cannot alone establish v2
production eligibility.

This document describes the implemented, independent Pricing runtime. It is a
research and monitoring subsystem. It does not submit orders, change Strategy
rankings, change the default directional feature profile, or authorize any
automated action.

## Research formula and production departure

For each standard option contract, the causal Black-Scholes mean uses

```text
S      completed underlying price at the target boundary
K      strike known from the earlier option definition
r      lagged point-in-time risk-free rate
sigma  implied volatility derived from a strictly earlier option surface
T      ACT/365 time to the 16:00 America/New_York expiration instant
q      lagged dividend yield
```

The observed research target is

```text
normalized residual = (later NBBO midpoint - Black-Scholes price) / S
fair value           = Black-Scholes price + S * predicted residual
```

The thesis implementation describes an exact Bayesian Gaussian process and
MCMC using contemporaneous implied volatility. Production deliberately does
neither. Contemporaneous IV is derived from the price being predicted, so it is
forbidden. The implemented `bsgp` is an RBF finite-feature approximation:
training-only robust scaling, deterministic scikit-learn `Nystroem`, and
`BayesianRidge`. Calibration clusters set posterior scale and weighted 80% and
95% residual quantiles. Component count, gamma grid, selected gamma, random
state, feature order, policies, runtime versions, code checksums, partitions,
and input checksums are in the model manifest.

The same untouched assessment clusters compare four models under one feature
and compute budget: Black-Scholes plus the learned residual (`bsgp`), causal
Black-Scholes alone, a training-weighted constant residual, and the same finite
GP approximation trained on normalized option price (`standard_gp`). The last
model is not an exact GP.

## Six independent runtime owners

| Runtime | Sole responsibility relevant here |
| --- | --- |
| `datafetching.cme_runtime` | CME history, L2 snapshots, and cross-asset context |
| `datafetching.orchestrate` | Equity bars and the rest of Loop A |
| `ml.option_pricing_runtime` | Pricing samples, predictions, evaluations, surfaces, monitoring, models, and its separate pointer |
| `datafetching.options_runtime` | Schwab raw chain, normalized contracts, option-quality surface, receipt, and Options pointer |
| `ml.prediction_runtime` | Directional Loop B and `ml/latest/run.json` |
| `ml.strategy_runtime` | Strategy candidates and `ml/strategy-latest/run.json` |

No runtime starts another runtime. Pricing owns
`.ducketz-option-pricing-runtime.lock`, never calls Schwab, never writes an
Options artifact, and never writes either Loop B or Strategy authority.

## Pre-quote live sequence

Loop A, Pricing, and Options share one `exchange_calendars` XNYS target decision.
An actionable target is an exact completed Databento one-minute bar ending on
`:00`, `:15`, `:30`, or `:45`, strictly after the official regular open and no
later than the official regular close. This makes 09:45 America/New_York the
first normal-session target and includes the official close, including an early
close. Weekends, holidays, DST, and early closes are calendar-owned; premarket and
after-hours bars are unsupported prospective evidence.

For each actionable target, integrated Pricing waits monotonically for Loop A's
immutable all-symbol readiness receipt. The target is prospective only if no
verified Schwab Options receipt already exists for it. A bounded readiness miss
publishes immutable `TARGET_BAR_NOT_READY` once and remains noncreditable forever.
When the calendar supplies no eligible target, Pricing is write-free
`MARKET_CLOSED_IDLE`: it never substitutes the newest older bar, grows the target
chain, or advances research eligibility health as though an opportunity was
lost.

Pricing then selects the latest committed source receipt satisfying both

```text
source_snapshot_for < target_snapshot_for
source_available_at < prediction_created_at
```

It uses the target bar close, earlier contract definitions, and only lagged
rate/dividend/IV evidence. The immutable Pricing receipt is published at the
same `prediction_available_at` recorded in new prediction rows. The independent
Options runtime may then fetch the target chain. If the completed target bar is
not ready before the Pricing phase, that symbol is skipped; the runtime never
backdates a prediction.

A later Pricing cycle scores a prediction only against a verified receipt with
the exact target and semantic contract, a receipt later than prediction
creation/publication, and a normalized option quote strictly later than both.
A stale pre-prediction quote remains an explicit unavailable evaluation and
does not increment prospective evidence. Repeated cycles retain the earliest
valid prediction once per symbol, target, and contract.

## Evidence, bounds, and model contracts

Pilot contracts are standard, non-mini, non-adjusted 100-share calls and puts,
7–120 calendar days from expiration, with `abs(log(K/S)) <= 0.25`. A source BBO
must be finite, uncrossed, positive, recent, and have a causal quote timestamp.
Missing rate, dividend, or interpolation support makes a row unavailable;
there is no silent zero or extrapolation.

European dividend-adjusted Black-Scholes supplies the mean. Published American
point bounds use intrinsic/European lower bounds and spot-or-strike upper
bounds. A deterministic weighted constrained optimization projects each
call/put expiration surface to point bounds, strike monotonicity, and convexity.
Raw values, constrained values, separate raw/constrained violations, and the
correction magnitude are all retained. One failed surface is unavailable; it
does not erase another route.

The independent evidence unit is a complete `target_snapshot_for` cluster.
Every cluster receives equal total fit and primary evaluation weight regardless
of contract count. Production-eligible model fitting requires chronological
252/63/63 train/calibration/assessment clusters, then 126 later real clusters
recorded only as `CLOSED_UNTOUCHED_UNSCORED`, plus a six-month span. Label
availability is purged across every boundary. The lockbox contributes only
redacted counts and time bounds; it is not predicted, scored, or used for model
selection.

Model reuse validates the manifest and `model.joblib` checksum before loading.
Any route, feature, target, timing, rate, dividend, volatility, expiration,
contract, weighting, kernel, uncertainty, bounds, projection, partition,
input, implementation, Python, or package mismatch causes a refit.

## Datastore authority

```text
DATASTORE/ml/
|-- option-pricing-models/<SYMBOL>/<call-or-put>/black-scholes-rbf-residual/<UTC>/
|   |-- model.joblib
|   `-- manifest.json
|-- option-pricing-runs/<UTC>/
|   |-- pricing-samples.parquet
|   |-- pricing-predictions.parquet
|   |-- pricing-evaluations.parquet
|   |-- pricing-surfaces.parquet
|   |-- pricing-monitoring.parquet
|   |-- option-pricing-model-reports.json
|   |-- model-artifacts/...
|   |-- manifest.json
|   `-- publication.json
`-- option-pricing-latest/run.json
```

All five Parquets have an exact Arrow schema and begin with one readable
Duckets `id`. Control-plane checksums and receipt lineage remain JSON-only.
Pricing writes files into an unpublished staging directory, verifies the
manifest and schemas, renames the completed run, writes its receipt, and then
atomically replaces its own pointer. Each receipt embeds the prior verified
Pricing record. Orphans are invisible; path escapes, broken chains, missing
files, schema drift, and checksum mismatches fail closed.

`pricing-surfaces.parquet` is the compact consumer boundary. It contains
coverage, residual, uncertainty, edge-in-half-spreads, raw/constrained shape
violations, matured interval coverage, liquidity, staleness, and explicit
surface quality by symbol, target, call/put, moneyness, and expiration bucket.

## Historical OPRA estimate and import safety

The separate command uses Databento `OPRA.PILLAR`, `definition`, and
consolidated `cbbo-1m`. It never runs inside the Options runtime. The default is
metadata/cost estimation only:

```powershell
python -m ml.option_pricing_opra --datastore-target pc `
  --symbols NVDA GOOG MU `
  --market-times 10:00 11:30 13:30 15:00
```

The command resolves at least six historical months and exactly the required 504
chronological clusters per symbol with `exchange_calendars`, including holidays,
early closes, and DST. It prints each exact dataset, schema, symbology, symbol
list, UTC window, estimated cost, billable bytes, storage preflight, and ceiling.
Definitions are estimated in whole-day windows. After a separately approved
definition phase is present, eligible standard raw symbols are filtered
point-in-time and CBBO is estimated in bounded ten-minute windows. The CBBO plan
must cover every scheduled target with both same-expiry/strike CALL and PUT raw
symbols, an exact completed underlying bar, and strictly prior rate evidence.

Paid access is impossible without `--execute`, an explicit `--max-cost-usd`, and
an operator-approved `--authorization-record` generated from the exact estimate.
The record binds every request, per-request and aggregate estimates, billable
bytes, eligibility-policy hash, storage requirement, one-attempt limit, and both
spend and datastore-write authorization. Every proposed request first calls
`metadata.get_cost`; a changed plan, incomplete authorization, insufficient
disk, or phase exceeding the aggregate ceiling aborts before `get_range`.
Approved DBN responses stream directly to bounded `.dbn.zst` files in immutable
`ml/option-pricing-evidence/opra/<UTC>/` evidence. A verified matching import is
resumed instead of downloaded again. Definition and CBBO are intentionally
separate resumable phases so the eligible raw-symbol request set and its full
cost are known before that paid phase starts.

Raw integer prices are explicitly divided by Databento's `1e9` fixed-point
scale. Definition expiration dates retain date precision and use the documented
equity-option expiration-time policy during materialization. `cbbo-1m`
`ts_recv` is the interval end. Source selection is backward-only within the
configured staleness allowance; target selection is forward-only. Missing
intervals remain missing. Every OPRA row is `OFFLINE` and can never count toward
prospective evidence.

No paid OPRA request was executed as part of this implementation.

## Shadow consumers and fallback

Directional Loop B's default remains `loop-a-all-v1`. The explicit
`loop-a-all-bsgp-shadow-v1` profile adds `opx__` compact-surface features:

```powershell
python -m ml.prediction_runtime --datastore-target pc `
  --symbols NVDA GOOG MU --horizons 1h 4h 1d 1w `
  --feature-profile loop-a-all-bsgp-shadow-v1 --once
```

The join first verifies the reachable Pricing receipt chain, then uses only a
publication and row with availability no later than the directional decision
cutoff. Missing, late, incompatible, empty, or tampered evidence makes this
explicit optional route unavailable. It never causes the default profile to
read Pricing or substitute a current value.

Strategy defaults to `--pricing-mode off`. `--pricing-mode shadow` matches each
option leg to an exact target and semantic contract and requires the Pricing
receipt before that leg's quote. It records leg coverage, missing reasons,
long `fair - ask`, short `bid - fair`, quantity/multiplier-weighted candidate
edge, spread-and-fee friction, edge-to-friction, conservative summed interval
uncertainty, and comparison to the existing scenario expected profit. These
columns are diagnostics only: `decision_score`, `candidate_rank`, `legs_json`,
UI ordering, order drafting, and submission behavior are unchanged.

## Run commands

Run one bounded Pricing cycle before the Options `+2` phase:

```powershell
python -m ml.option_pricing_runtime `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --interval-minutes 15 `
  --phase-offset-minutes 1 `
  --bar-readiness-mode required `
  --bar-readiness-timeout-seconds 45 `
  --once
```

Enable Strategy diagnostics explicitly:

```powershell
python -m ml.strategy_runtime --datastore-target pc `
  --pricing-mode shadow --once
```

These are operating examples, not a deployment record. Installing this code,
starting or restarting supervisors, approving OPRA spend, and freezing an
operational model remain explicit operator actions.

## Ten-part evidence gate and limitations

The JSON report exposes ten separate statuses for lineage/timing/schema,
partitions/lockbox, three comparator wins, interval calibration, constrained
shape/projection magnitude, edge after spread and bucket monotonicity, Strategy
shadow improvement, and 60 prospective predictions over at least 20 sessions
per symbol. Any unproven or failed item keeps
`gate_status=NOT_PRODUCTION_ELIGIBLE`. `automated_action_allowed` is always
false, even if fixture tests pass.

Important limitations:

- An NBBO midpoint is not a fill.
- Adverse-BBO Strategy pseudo-outcomes are not broker executions.
- American early-exercise effects enter only through historical residual
  evidence; the analytic mean is European Black-Scholes.
- Provider IV/Greeks at the target are never model inputs.
- No real-data calibration, profitability, production readiness, or deployment
  claim exists yet.
- No paid OPRA download, model freeze, closed-lockbox score, or receipt-proven
  prospective collection was authorized in this build.
