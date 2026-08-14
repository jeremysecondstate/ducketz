# Active Pricing (logical Loop 3)

Audited baseline commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

OPRA-first implementation update: 2026-08-14

Production entrypoint: `python -m ml.option_pricing_runtime` at the 15-minute
`+1` phase with exact Loop A readiness required. The owner number and cadence
are unchanged.

## Scope

The single authority in `ml/universe.py` defines exactly six production option
symbols (`AAPL, AMZN, GOOG, MU, NVDA, SNDK`) and derives CALL/PUT routes, so
production acceptance is exactly 12 routes. `SPY` is declared separately and
is accepted only by the bounded research-benchmark path.

The active code is primarily in:

- `ml/universe.py`
- `ml/option_pricing/{causal,dividends,model,opra,opra_materialization}.py`
- `ml/option_pricing/{policies,prediction,publication,rates,reporting}.py`
- `ml/option_pricing/{research_benchmark,schwab_materialization,shadow_model,weighting}.py`
- `ml/option_pricing/{consumers,eligibility,loop_native_eligibility}.py`
- `ml/option_pricing_runtime.py`
- `ml/option_pricing_loop_native_worker.py`
- `ml/option_pricing_admin.py`

## Provider and evidence roles

Fair-value precedence is:

1. `databento-opra` — canonical historical and prospective market evidence.
2. `schwab` — causal fallback when OPRA is unavailable or ineligible.

The persisted lanes are `OFFLINE_OPRA_BACKFILL`, `PROSPECTIVE_OPRA`, and
`PROSPECTIVE_SCHWAB`. Provider and fallback status are stored on every sample,
prediction/evaluation, surface, and report. Offline imports never increment a
prospective-session count. Schwab also supplies broker enrichment, provider
disagreement, execution/fill validation, and the separate execution-model
target; execution labels are not blended into the OPRA-midpoint fair-value
target.

The child worker performs no provider call. It materializes verified OPRA
imports plus provider-neutral prospective snapshot history and then trains or
reuses the local model. Paid OPRA acquisition remains a separately gated
operator action.

## Causal pricing contract

The semantic inputs remain `S, K, r, sigma, T, q`, and the target remains
`(observed_midpoint - BlackScholes) / underlying_price`. Black-Scholes is the
mean/baseline.

Each row preserves distinct clocks for market target, source quote event,
source evidence availability, prediction creation, prediction publication,
outcome quote event, outcome evidence availability, and provider receipt. The
source is the last valid NBBO strictly before the prediction cutoff. The label
is the first exact-semantic-contract NBBO satisfying both:

```text
outcome_quote_timestamp > prediction_available_at
outcome_available_at     > prediction_available_at
```

Offline replay uses the versioned 60-second emulated latency and retains the
present-day import receipt separately. A quote after the target but before
emulated prediction availability is rejected.

## Rates, dividends, and contract reference

The primary rate authority is immutable FMP daily Treasury evidence under
`pools/rates/treasury-curve/fmp/<receipt>/`. Only curves received before the
decision are eligible; during an XNYS session, the previous fully available
business-day curve is the default. Rates are interpolated in log-discount space
and converted to a continuously compounded maturity-matched rate. ALFRED/FRED
is the validation and explicit fallback; provider rate fields are comparison or
last-resort provenance.

Declared FMP dividend history lives under
`stocks/<SYMBOL>/corporate-actions/dividends/fmp/<receipt>/`. Resolution uses
only declarations and receipts knowable at the decision and ex-dates in
`(as_of, expiration]`. It persists dividend PV, equivalent continuous `q`, event
count, next ex-date, availability, and one of `DECLARED_FMP`,
`CAUSAL_RECURRING_ESTIMATE`, `PUT_CALL_PARITY_FALLBACK`, or
`ZERO_NO_KNOWN_DIVIDEND`. FMP's supplied yield is never used directly.

Point-in-time exercise/settlement reference is retained. Ambiguous,
non-standard, mini, adjusted, or otherwise unsupported contracts are excluded
or explicitly stratified; they are not silently treated as ordinary European
contracts.

## Model and weighting

The active residual model is accurately named:

> 128-component Nyström RBF residual model with Bayesian ridge posterior

It is a finite-basis approximation, not an exact Gaussian Process. New policy,
manifest, and model names use “finite-basis”/“Nyström”. Historical Python and
Parquet names containing `bsgp` remain read aliases so immutable v1/v2/v3
artifacts continue to load; those aliases do not assert exact-GP semantics.

Rows receive causal liquidity weights from relative spread, staleness,
volume/open interest, and quote quality. Weights are normalized so every
symbol/target/CALL-or-PUT surface contributes one total unit. Raw components,
missingness, final weight, normalization factor, and policy version are
persisted.

A separate bounded `SPY` exact-GP benchmark implements calls and puts, volume
filtering, chronological partitions, Black-Scholes mean, residual GP,
uncertainty, and comparators. It does not claim to reproduce the paper's
May–June 2019 experiment unless those exact data are supplied.

## Append-only publication and recovery

Pricing v3 publications form an append-only verified receipt chain. Ordinary
publish cannot move the pointer backward, conceal a newer verified generation,
adopt an orphan, or form a broken/cyclic chain. Compact history spans every
reachable generation and records natural surface key, model generation,
provider/lane, target/availability, publication checksum, quality,
supersession, uncertainty, residual, spread, edge, constraints, and coverage.

Read-only diagnosis:

```powershell
python -m ml.option_pricing_admin --datastore-target pc diagnose-publications
```

Recovery is a separate exact-child operation and requires a matching immutable
`option-pricing-orphan-recovery-authorization-v1` record:

```powershell
python -m ml.option_pricing_admin --datastore-target pc recover-orphan `
  --run-directory <verified-child-run> `
  --authorization-record <approved-json>
```

Recovery never silently promotes an orphan and never rolls the pointer back.

## Eligibility and non-activation

Eligibility v4 derives 12 routes. It requires six bounded offline OPRA months,
the separate SPY benchmark, at least 20 independent `PROSPECTIVE_OPRA` sessions
per route, calibrated uncertainty, comparator/no-arbitrage/liquidity gates,
provider disagreement, capacity/latency evidence, fair-value and execution
shadow results, a closed lockbox, and explicit operator authorization.

`automated_action_allowed=false` remains mandatory. This implementation does
not activate automated trading or establish production eligibility.
