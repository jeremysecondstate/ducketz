# Ducketz provider-neutral option evidence

The option layer persists point-in-time OPRA and Schwab evidence behind one
normalized, immutable publication contract.

## Universe

`ml/universe.py` is the only production option-universe authority:

```text
AAPL AMZN GOOG MU NVDA SNDK
```

CALL and PUT are derived for each symbol, so readiness and eligibility require
exactly 12 routes. `SPY` is declared separately as a research benchmark and is
never part of Loop A, Loop B, Strategy, production readiness, or those routes.

## Provider roles

- `databento-opra` is canonical market evidence for historical training,
  prospective fair-value inputs/outcomes, uncertainty calibration, and model
  quality.
- `schwab` is broker enrichment, explicit causal fallback, provider
  disagreement, execution/fill validation, and execution-model evidence.

Schwab rows are never labeled as OPRA. Every normalized row and downstream
report carries provider, evidence lane, and `fallback_used`.

`options/providers.py` is the injected adapter boundary for prospective OPRA.
Tests use fake adapters and never make provider requests. A live adapter and
credentials are an explicit rollout dependency, not an implicit fallback.

## Clocks

The schema keeps these concepts separate:

- market target (`target_snapshot_for`);
- source quote event;
- source evidence availability;
- prediction creation;
- prediction publication/availability;
- outcome quote event;
- outcome evidence availability;
- provider ingestion/local receipt.

The target is aligned to the newest completed Databento one-minute bar ending
on `:00`, `:15`, `:30`, or `:45`. Derived bars do not move this clock. Strict
consumers use receipt availability, not a provider event timestamp, for causal
visibility.

The source quote must be the last valid NBBO strictly before prediction cutoff.
An outcome must be the first valid exact-contract NBBO for which both its quote
event and evidence availability are strictly after prediction availability.
Being later than the market target alone is insufficient.

Offline OPRA replay uses a versioned 60-second emulated prediction latency and
stores present-day import receipt separately. It is permanently ineligible for
prospective counts.

## Immutable storage and identity

The natural key is:

```text
(provider, symbol, target_snapshot_for)
```

```text
DATASTORE/stocks/<SYMBOL>/options/
|-- snapshots/
|   |-- databento-opra/<target_ns>/
|   |   |-- raw.parquet
|   |   |-- contracts.parquet
|   |   |-- option-quality.parquet
|   |   |-- manifest.json
|   |   `-- receipt.json
|   `-- schwab/<target_ns>/...
`-- latest/
    |-- databento-opra.json
    `-- schwab.json
```

Publication is staged, checksummed, verified, and atomically committed. An
identical retry returns the existing earliest verified publication without a
provider call. Divergent content for an existing natural key fails closed and
is never overwritten.

Successfully committed closed-market targets are reused across discovery
intervals. Legacy Schwab v1 paths and monthly mirrors remain readable:

```text
chains/schwab/{raw,normalized}/YYYY-MM.parquet
features/option-quality/schwab/YYYY-MM.parquet
```

Legacy duplicates remain immutable. Canonical readers choose the earliest
verified receipt, diagnose duplicates/conflicts, and prevent repeated evidence
from increasing weight or session counts.

## Normalized option snapshot v2

At minimum, normalized contracts contain:

- provider, dataset, underlying symbol, and contract symbol;
- quote event, target, first availability, and provider receipt;
- bid/ask/midpoint and bid/ask sizes;
- trade price/size when available;
- strike, expiration, CALL/PUT, multiplier, and standard/mini/adjusted flags;
- OPRA publisher/venue lineage;
- quote staleness and quality status;
- point-in-time definition timestamp;
- exercise style and settlement/reference attributes when available;
- source files/checksums plus schema and policy versions.

OPRA definition rows must be effective no later than the target. OPRA L1 uses
the final valid BBO strictly before that target. Because OPRA does not itself
provide the equity spot, live pricing binds the surface to the exact
receipt-visible Loop A close for the source target. Unsupported or ambiguous
contract reference is excluded or explicitly stratified.

Optional OPRA statistics/open interest, volume, status/halts, and trade/TBBO
fields retain their own availability clocks. Trades/TBBO are execution evidence,
not fair-value labels. `cmbp-1` remains research-only.

## Rate and dividend authorities

Already-fetched FMP Treasury responses are published under:

```text
pools/rates/treasury-curve/fmp/<receipt>/
```

Resolution selects only a fully available causal curve, defaults to the prior
fully available business-day curve during an XNYS session, interpolates log
discount factors, and derives a continuous maturity-matched rate. ALFRED/FRED is
validation and explicit fallback; broker/provider rate fields are comparison or
last resort.

Already-fetched FMP dividend histories are published under:

```text
stocks/<SYMBOL>/corporate-actions/dividends/fmp/<receipt>/
```

Only knowable declarations with ex-dates in `(as_of, expiration]` are used. The
model input is computed from known cash-dividend PV:

```text
q = -ln((S - PV(dividends)) / S) / T
```

FMP's supplied yield is not used directly and future declarations are excluded.

## Historical OPRA planning

The default dry run plans six calendar months, four declared intraday XNYS
targets per eligible session, the six production parent symbols (`<SYMBOL>.OPT`),
point-in-time definitions, and `cbbo-1m`. Request/cluster counts are derived
from actual sessions, targets, and symbols. It reports date coverage, request
count, estimated billable bytes/cost, expanded storage, capacity, and resumable
receipts without calling `get_range`:

```powershell
python -m ml.option_pricing_opra --datastore-target pc
python -m ml.option_pricing_opra --datastore-target pc --research-benchmark
```

Prospective L1 defaults to `cbbo-1s`. Paid execution additionally requires all
of `--execute`, an explicit `--max-cost-usd`, sufficient capacity, and an exact
operator-approved `opra-paid-execution-authorization-v1` record. Never run the
paid phase as a migration or test.

## Pricing model and rollout

The fair-value residual model is the **128-component Nyström RBF residual model
with Bayesian ridge posterior**, with Black-Scholes as its mean. It is not an
exact GP. Historical identifiers containing `bsgp` are compatibility aliases
for immutable artifacts only.

Liquidity weights use causal spread, staleness, volume/open interest, and quote
quality, then normalize each target surface to equal total weight. The separate
SPY exact-GP benchmark is bounded and research-only.

Eligibility requires six offline OPRA months, the SPY benchmark, 20 independent
prospective OPRA sessions for every one of the 12 routes, calibration and
comparator evidence, no-arbitrage/liquidity and operational gates, provider
disagreement, shadow strategy/execution results, a closed lockbox, and separate
operator authorization. `automated_action_allowed=false` remains in force.
