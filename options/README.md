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

`options/providers.py` remains the injected adapter protocol. The production
implementation is `options/databento_live.py`: Options Capture constructs one
live client, subscribes once to point-in-time definitions and once to
`OPRA.PILLAR` `cbbo-1s` for the six `<SYMBOL>.OPT` parents, and serves every
target from bounded in-memory buffers. The CLI defaults to
`--provider-mode opra-canonical`; it loads `DATABENTO_API_KEY` through the
repository environment convention and exits before the recurring cycle if the
credential, SDK construction, or subscriptions are unavailable. The key is
passed directly to the SDK, is not copied into adapter fields, and is absent
from diagnostics, receipts, persisted failures, and sanitized exceptions.
Tests use fake clients/adapters and never make provider requests.

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
An outcome comes from the earliest later committed snapshot containing a valid
exact-contract NBBO whose conservative quote event and receipt availability are
strictly after prediction availability. Being later than the market target alone
is insufficient, and the later snapshot is never relabeled as the earlier target.

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
- point-in-time definition event, provider-receipt/effective, activation, and local-receipt timestamps;
- exercise style and settlement/reference attributes when available;
- source files/checksums plus schema and policy versions.

OPRA definition rows use `ts_recv` as the provider-received causal selection
clock and retain local receipt/availability separately; the selected definition
must be received by Databento no later than the target and locally visible by
publication. Its contract activation must also be known and no later than the
target, and its market-event clock cannot follow its provider-receipt clock.
Contract type, strike, expiration,
multiplier, exercise style, settlement, and standardization must be explicit in
the definition or its valid six-character option CFI; ambiguous definitions are
ineligible. OPRA L1 uses the final valid, noncrossed BBO strictly before that
target. A `cbbo-1s` record timestamp is the interval end, so live normalization
records both that provider interval end and a conservative market-event time one
second earlier. Because OPRA does not provide the equity spot, live pricing
binds the surface to the exact receipt-visible Loop A close for the target.

Optional OPRA statistics/open interest, volume, status/halts, and trade/TBBO
fields retain their own availability clocks. Trades/TBBO are execution evidence,
not fair-value labels. `cmbp-1` remains research-only.

## Rate and dividend authorities

Live Pricing requires a causally available FRED/ALFRED rate observation and
does not substitute the option provider's rate or an FMP curve. Historical and
offline materialization may retain already-fetched FMP Treasury comparisons
under:

```text
pools/rates/treasury-curve/fmp/<receipt>/
```

The live resolver selects only observations whose event, receipt, and
availability clocks precede the target boundary; percentage-point FEDFUNDS is
converted to a decimal continuous rate. Missing causal FRED/ALFRED evidence makes
the live route unavailable rather than inventing or borrowing a provider rate.

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

## Historical OPRA synchronization

The canonical command validates provider metadata against the checked-in
Standard-plan authority, rejects any range outside the configured included
scope, performs a storage-capacity preflight, and synchronizes every included
schema over the full OPRA universe by default:

```powershell
python -m ml.option_pricing_opra --datastore-target pc
```

Resumable maintenance batches can use `--schemas`, `--start`, `--end`,
`--symbols`, and `--max-partitions`; these flags never expand provider-confirmed
bounds. Provider-native DBN, normalized Parquet, immutable manifests, and receipts
are stored below:

```text
market-data/databento-opra/OPRA.PILLAR/
  schema=<schema>/date=<YYYY-MM-DD>/bucket=<scope>[-segment-<UTC-range>]/
    provider.dbn.zst
    normalized.parquet
    manifest.json
    receipt.json
```

Run the resumable bootstrap once for each new parent-symbol set:

```powershell
python -m datafetching.options_history `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt
```

The bootstrap synchronizes each symbol independently as `<SYMBOL>.OPT` with a
schema-specific initial window: every `*-1s` schema uses 5 days, every `*-1m`
schema 100 days, `ohlcv-1h` 1,825 days, and `ohlcv-1d` 2,555 days.
`definition` retains 13 calendar years; the remaining non-interval Standard
schemas use one month. Every request
must remain inside its configured included entitlement; an explicit range
outside it is rejected rather than silently shortened.
Dense `cmbp-1` and `cbbo-1s` days are split into deterministic intraday
partitions so every normalized Parquet remains inside the exact duplicate-check
bound. The command is idempotent and resumes from checksum-verified partitions.

Verified completion publishes an `options-opra-symbol-history-v5` cursor with
the exact lookback policy, requested start, and completed-through boundary. The
reader retains narrow v4 compatibility only when the old cursor’s policy exactly
matches the former schema-specific bootstrap policy; all new writes are v5.
For daily bars and definitions, that legacy-only v4 policy was 5,000 days; v5
writes use the current schema-specific policy.

The optional one-shot `datafetching.databento_cold_start` command can populate
the same canonical OPRA partitions while creating separate CME/US-equity cold
archives. It uses the OPRA history sync lock and writes the same v5 cursor
handoff after verification, but it never takes the Options snapshot-writer lock
or publishes a snapshot/readiness/model pointer. It is maintenance, not an
eighth production loop.

Options Capture does not perform a missing-symbol bootstrap. Once a bootstrap
cursor exists, its owned daily maintenance uses frequency-specific overlaps:
1 day for `ohlcv-1s`, 2 days for `ohlcv-1m`, 5 days for `ohlcv-1h`, and 10 days
for `ohlcv-1d`; other schemas retain 3 days. It fetches only missing verified
partitions. The overlap is a request safety window,
not a rolling three-month redownload; immutable partition verification and exact
natural-key checks make it safe to repeat. One symbol's capacity or provider
failure does not block the other symbols. Prospective L1 continues to default to `cbbo-1s`;
Strategy and Active Pricing read verified historical `cbbo-1m` first, use
`cbbo-1s` when it is the only verified CBBO schema, and keep Schwab explicitly
labeled as fallback/broker evidence.

## Prospective OPRA startup and fallback

The production Options command is:

```powershell
python -m datafetching.options_runtime `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --provider-mode opra-canonical `
  --interval-minutes 15 `
  --phase-offset-minutes 6 `
  --pricing-barrier-timeout-seconds 45 `
  --bar-readiness-mode required
```

OPRA is attempted first for the calendar target and carries independent
definition, quote, provider-receipt, and local-receipt clocks, so capture does
not wait for Loop A readiness. Loop 3 still requires exact Loop A authority
before using that evidence. A bounded transient unavailability may fall through
to a new, separately labeled Schwab request; if Loop A is late, the broker
response remains in the existing pending quarantine. Identity, schema, clock,
definition, quote, duplicate, or corruption failures fail that target closed
and do not cross into fallback. A target already committed under either
provider makes no second provider request. The explicit
`--provider-mode schwab-only-compatibility` mode disables OPRA and is not the
production command.

The live receiver, callbacks, reconnect bookkeeping, bounded buffers, and daily
incremental historical catch-up are owned by the Options Capture process and close with it.
They are not an eighth loop.

## Supplied Standard-plan evidence

**Confirmed by the current entitlement authority at
`docs/databento-plan/databento_standard_plan_data_access.md`:** the displayed Standard OPRA
plan advertises live OPRA data without a separate license fee, 18 exchanges,
approximately 1.6 million symbols, 13+ years of L0 history, about one year of L1
history, and definitions, OHLCV, statistics, status, CMBP-1, TCBBO, CBBO, and
trades. The displayed use table permits personal display on up to two devices
and personal non-display use; commercial display, distribution, white-label,
and dedicated-service rights are not shown as included. This is entitlement
evidence only, not proof that this repository's key is currently active or that
any live capture has succeeded. The authority records its source evidence and
the exact included-history interpretation used by the executable guards.

The implementation was checked against installed `databento` SDK 0.81.0 and
Databento's official [OPRA.PILLAR dataset](https://databento.com/docs/venues-and-datasets/opra-pillar),
[BBO schema](https://databento.com/docs/schemas-and-data-formats/bbo), and
[instrument-definition schema](https://databento.com/docs/schemas-and-data-formats/instrument-definitions), and
[live client/reconnect](https://databento.com/docs/api-reference-live/client/add-reconnect-callback)
documentation. Those sources establish `cbbo-1s` availability and that sampled
BBO `ts_recv` is the interval-end clock; the conservative one-second subtraction
in normalized rows is an implementation choice that preserves “strictly before
target” semantics rather than pretending the interval-end is a quote event. The
definition contract also establishes `ts_recv` as Databento receipt,
`contract_multiplier` as an unscaled integer, and fixed-point scaling only for
fields such as `unit_of_measure_qty` and `strike_price`.

## Pricing model and rollout

The fair-value residual model is the **128-component Nyström RBF residual model
with Bayesian ridge posterior**, with Black-Scholes as its mean. It is not an
exact GP. Historical identifiers containing `bsgp` are compatibility aliases
for immutable artifacts only. Prediction, sidecar, and evaluation artifacts
retain the residual explicitly in both normalized and dollar units.

Liquidity weights use causal spread, staleness, volume/open interest, and quote
quality, then normalize each target surface to equal total weight. The separate
SPY exact-GP benchmark is bounded and research-only.

Eligibility requires six offline OPRA months, the SPY benchmark, 20 independent
prospective OPRA sessions for every one of the 12 routes, calibration and
comparator evidence, no-arbitrage/liquidity and operational gates, provider
disagreement, shadow strategy/execution results, a closed lockbox, and separate
operator authorization. `automated_action_allowed=false` remains in force.
