# Active option pricing: Black-Scholes plus a finite-feature GP

Eligibility protocol v2 remains the readable three-symbol/OPRA legacy policy.
The separate Loop-native causal policy is v3 and covers all ten configured
symbols and both option sides without making paid OPRA a prerequisite.
Candidate/lockbox controls, continuous-runtime operations, rollback, and
disaster recovery are specified in
[`option-pricing-operations.md`](option-pricing-operations.md). Existing v1
artifacts described below remain readable but cannot alone establish v2
production eligibility.

This document describes the implemented, independent Pricing runtime. Pricing
is now an active input to Options Strategy profitable-outcome probabilities and
post-score ranking. It still does not submit orders or authorize automated
action. Eligibility and quality reports remain monitoring outputs; they are not
gates in front of a valid Black-Scholes fallback.

## Loop-native causal BSGP lane (policy v3)

The active fair-value path starts from fail-safe Black-Scholes. The immutable
`pricing-bsgp-shadow.parquet` transport name is retained for compatibility, but
its finite-basis GP residual correction is now consumed by Strategy:

```text
fair value = causal Black-Scholes value + S * predicted normalized residual
```

The natural Schwab snapshot key is `(symbol, snapshot_for)`. Materialization
selects the earliest valid committed receipt for that key, retains every later
publication as duplicate lineage, and gives each
`(symbol, target_snapshot_for, call_put)` surface bounded total weight. It never
treats contract rows or duplicate publications as independent sessions.
Conflicting semantic duplicates, path escape, schema drift, checksum changes,
or unverifiable clocks fail closed.

Two pooled models are maintained, one for CALL and one for PUT, over exactly
`NVDA GOOG MU AAPL MSFT AMZN META TSLA CAT SNDK`. Features implement the six
research concepts using only causal transformations: source underlying price,
log-moneyness, verified rate/discount, lagged source-chain IV, time to expiry,
and verified carry/forward. Target midpoint and target IV are labels or later
diagnostics only; neither can enter a feature. Current-revised macro history is
not historical evidence. The Loop-native lane currently requires a causal
provider-rate/carry receipt and disables the older weak source-chain American
parity fallback.

The fast Pricing phase verifies and loads only a model generation with
`published_at < prediction_created_at`. It then publishes unchanged baseline
rows plus `pricing-bsgp-shadow.parquet` before the Options receipt. Model fitting
does not run on this path. After publication, a locked, bounded local worker may
materialize already committed Schwab outcomes and publish a new immutable model
generation for a later cycle. The worker has no provider client and makes no
network request. A same-cycle outcome can therefore never train the generation
used for that cycle.

Shadow statuses are explicit: `BSGP_SHADOW_READY`,
`BASELINE_FALLBACK_NO_MODEL`, `BASELINE_FALLBACK_STALE_MODEL`,
`BASELINE_FALLBACK_OUT_OF_SUPPORT`,
`BASELINE_FALLBACK_INPUT_UNAVAILABLE`, and
`BASELINE_FALLBACK_UNCALIBRATED`. Every fallback copies the causal
Black-Scholes value and publishes a conservative nonzero uncertainty interval;
sparse route support shrinks the residual toward zero while widening toward
that interval. It does not delay Pricing or Options. The sidecar records
raw and constrained point estimates and 80/95 percent intervals, posterior
standard deviation, support and shrinkage diagnostics, model-generation
lineage, projection magnitudes, and `automated_action_allowed=false`.

Existing Schwab history can contribute only to the explicitly labeled
`OFFLINE_SCHWAB_BOOTSTRAP` lane when its real receipt, request, quote, underlying,
rate/carry, and contract clocks prove the reconstruction. It never increments a
prospective count and no backdated prediction receipt is manufactured.
Prospective evidence continues to require the immutable Pricing publication to
precede the exact later Options receipt and remains subject to twenty distinct
sessions for every one of the twenty symbol/side routes.

Legacy Pricing generations are not silently upgraded. A generation without a
preserved input inventory, with an input checksum that no longer verifies, or
without one exact completed-bar proof is reported and excluded from v3 source
samples. Path escapes, malformed inventories, output tampering, and conflicting
causal samples still terminate materialization. This permits later valid v3
generations to mature without granting credit to unverifiable older lineage.

The paper's SPY May-June 2019 result and one-week-ahead example are not assumed
to transfer to ten equities in 2026. This implementation deliberately replaces
contemporaneous IV with lagged source IV, uses chronological session-blocked
partitions, equal surface weighting, explicit interval calibration, and future
Loop evaluation. Millions of cross-sectional contract rows are not represented
as millions of independent observations. An American approximation may be
reported later as a comparator, but it does not replace Black-Scholes as the
settled mean in this version.

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
state, feature order, policies, runtime versions, chronological partitions,
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

No runtime starts another market-data, trading, or decision runtime. Pricing
owns `.ducketz-option-pricing-runtime.lock`, never calls Schwab, never writes an
Options artifact, and never writes either Loop B or Strategy authority. After
the fast target publication it may launch its own locked local-only
Loop-native materialization/training helper; that helper reads committed
artifacts and cannot delay the target publication.

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
publishes no target artifact: the target remains retryable until the pricing
input-freshness deadline. Legacy empty `TARGET_BAR_NOT_READY` artifacts are
never reused as current evidence.
When the calendar supplies no eligible target, Pricing is write-free
`MARKET_CLOSED_IDLE`: it never substitutes the newest older bar, grows the target
chain, or advances research eligibility health as though an opportunity was
lost.

Pricing orders committed source receipts newest-first and selects the newest
one that satisfies both causal clocks and yields at least one contract under
the unchanged source-quality contract:

```text
source_snapshot_for < target_snapshot_for
source_available_at < prediction_created_at
```

A newer receipt whose entire surface is stale or otherwise unusable cannot
poison a later target when an older causal receipt remains valid. Every skipped
receipt consulted by this deterministic fallback is included in the Pricing
lineage; no staleness, moneyness, timing, or quote threshold is relaxed.

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
does not increment prospective evidence. The earliest committed receipt for a
natural `(symbol, snapshot_for)` target is authoritative; a later duplicate can
never rescue a prediction created after that target was already observable.
Repeated cycles retain the earliest valid prediction once per symbol, target,
and contract.

## Evidence, bounds, and model contracts

Pilot contracts are standard, non-mini, non-adjusted 100-share calls and puts,
7–120 calendar days from expiration, with `abs(log(K/S)) <= 0.25`. A source BBO
must be finite, uncrossed, positive, recent, and have a causal quote timestamp.
The Loop-native materializer contract v2 applies the same 1,200-second maximum to the
target receipt-to-quote delay, records the exact target spread and staleness,
and preserves `TARGET_QUOTE_STALE` as an exclusion reason.
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

Loop-native artifacts use new identities rather than reinterpreting those
legacy runs:

```text
DATASTORE/ml/
|-- option-pricing-loop-native-materializations/<UTC>/
|   |-- samples.parquet
|   |-- manifest.json
|   `-- publication.json
|-- option-pricing-loop-native-materialization-latest/run.json
|-- option-pricing-loop-native-models/generations/<UTC>/
|   |-- model.joblib
|   |-- manifest.json
|   `-- publication.json
|-- option-pricing-loop-native-models/latest.json
|-- option-pricing-loop-native-policy-v3/<UTC>/...
|-- option-pricing-loop-native-eligibility-v3/<UTC>/...
`-- option-pricing-target-outcomes/<UTC>/
    |-- pricing-predictions.parquet
    |-- pricing-bsgp-shadow.parquet
    |-- manifest.json
    `-- publication.json
```

Each current pointer is atomic and checksummed. Verification walks the complete
reachable chain, constrains every relative path to the datastore, and checks
the exact schema and content checksum before a model or sidecar is usable. A
model generation is append-only and cannot update in place.

## Optional legacy OPRA benchmark and import safety

Paid OPRA is not a prerequisite of Loop-native policy v3. The guarded v2 path
below remains available only as a separately authorized external benchmark and
preserves all historical evidence identities. Its materializer is not relabeled
as Schwab evidence, and no OPRA purchase or download is implied by v3.

Historical FEDFUNDS coverage is acquired separately with
`python -m ml.option_pricing_fred`. That importer requires a securely configured
`FRED_API_KEY`, explicit provider real-time and observation bounds, and exact
ALFRED `realtime_start`/`realtime_end` interval fields. Date-precision vintage
availability is conservative end-of-day America/Chicago; actual local
acquisition remains `fetched_at`. A successful import remains
`NOT_EVALUATED` for schedule coverage until the OPRA planner proves every
target. Current-revised FRED history is never substituted.

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

## Active consumers and fallback

Historical Strategy bootstrap uses the same exact-leg pricing contract through
an explicitly offline replay lane. Each replay row is sourced from an earlier
verified Schwab snapshot and point-in-time underlying/rate/dividend/volatility
inputs. When the current pooled model generation is tied to that exact immutable
materialization, only its untouched assessment sessions may receive BSGP
cross-fit predictions: every train and calibration availability timestamp must
strictly precede the emulated decision. Earlier or unsupported observations use
the causal Black-Scholes replay with zero learned residual and wider fallback
uncertainty. The evidence lane remains `OFFLINE`; it never increments prospective
counts or claims live availability.

The production directional profile is `loop-a-all-bsgp-active-v2`. Its `opx__`
join verifies the reachable Pricing receipt chain, preserves each surface's
first availability, and expires a value against both first availability and
the underlying market target. Republishing a cumulative artifact cannot make
an old surface fresh. Missing, late, incompatible, empty, or tampered evidence
stays missing; no current value is substituted.

Strategy defaults to `--pricing-mode active`. Before either historical fitting
or live scoring, every option leg must match symbol, target, exact contract
symbol, call/put, expiration, strike, and multiplier. Both prediction creation
and immutable publication must be strictly earlier than the executable quote;
the source surface and target are also subject to the 1,200-second freshness
contract. Stock legs receive no option-pricing edge.

For long legs, edge is `quantity * multiplier * (fair - ask)`; for short legs it
is `quantity * multiplier * (bid - fair)`. The conservative edge uses the 95%
lower bound for longs and 95% upper bound for shorts. Candidate uncertainty is
the conservative sum of absolute leg posterior exposures. This is an interval
bound, not an invented CALL/PUT correlation assumption.

The fields are attached before the scenario prior and before the
horizon-specific classifier. A compatible classifier emits calibrated
`BSGP_CALIBRATED_MODEL` or `BLACK_SCHOLES_CALIBRATED_MODEL`; otherwise the same
pricing distribution is convolved with the executable-spread/fee scenarios and
emits `PRICING_SCENARIO_FALLBACK`. Ranking occurs only after this probability is
final. Order drafting and submission behavior are unchanged.

The one authoritative production command list is
[`current_start_command`](current_start_command). Installing the code and
restarting the six runtime owners remain explicit operator actions.

## Ten-part evidence gate and limitations

The JSON report exposes ten separate statuses for lineage/timing/schema,
partitions/lockbox, three comparator wins, interval calibration, constrained
shape/projection magnitude, edge after spread and bucket monotonicity, Strategy
evidence improvement, and 60 prospective predictions over at least 20 sessions
per symbol. Any unproven or failed item keeps the research gate unproven, but
does not block the production Black-Scholes fallback.
`automated_action_allowed` is always false, even if fixture tests pass.

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
