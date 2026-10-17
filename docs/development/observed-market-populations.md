# Observed market populations and prospective lineage

## Source absence and observed quarters

`intraday-verified-source-observations-v1` aggregates genuine native one-minute
observations within a covered fifteen-minute interval. Its open and close are
the first and last **observed source prices**, with their actual timestamps
retained. They are not assertions of trades at the interval boundary. The source
identifier, successful request coverage and native observation evidence remain
attached. A selected-venue feed is never relabelled consolidated market data.

One to fifteen observed native minutes can form a nonempty quarter. No omitted
minute is filled and no volume is invented. An entirely source-empty interval
is unavailable. For a sparse live response, two stable covered receipts at least
four seconds apart are required; this is evidence of source stability, not proof
that the whole market had no trades. Final source-empty collection is recorded
without a candle and without a zero-return outcome.

Legacy carry-forward requires explicit independent complete-market no-trade
evidence. Successful requests, advertised provider ranges, a no-trade omission
string, repeated emptiness and a prior close cannot supply that evidence. Current
Mini adapters do not manufacture it. Historical saved artifacts are retained;
the new population rejects their unsupported synthetic bars.

The private `DATASTORE/intraday/data-policy.json` binds a local installation to
this population and optionally enables receipt-time market context. An absent
binding preserves the existing population choice. Model candidate specifications,
lookbacks, calibration and qualification thresholds remain separate local choices.

## Training, scoring and sizing

Training uses causally available features and mature labels with exact target
intervals. The target remains the third named interval's close relative to the
reference interval's open. Missing endpoints do not become labels. Supervision
and the live runner use the same selected population and feature definitions.
Successful fitting is not qualification. Retained specifications assessed under
older populations or features are labelled as not yet assessed in the new context.

Forecasts freeze their population, feature contract, actual arrays, candle
evidence and synthetic policy. Outcomes cannot silently cross population
contracts. Technical sizing filters by the forecast's frozen policy before
indicator warmup; excluding a reference does not permit a substitute reference.
Actual source-empty targets are explicitly unscorable instead of being retried
forever or priced from a previous candle.

See [receipt-time market inputs](intraday-market-inputs.md) for the acquisition
suite, immutable storage, causal joins, limits and missing-value masks. During a
manual session, its bounded collection worker advances every fifteen minutes
independently of forecast computation. Each prediction refits using newly mature
eligible data. Corporate context has a slower refresh cadence. Historical
receipt availability is never manufactured from a newly fetched fiscal date.

## Gameplan source recovery

`observed-multi-source-stock-v1` tries native XNAS one-minute observations, raw
Mini one-minute observations, raw Schwab one-minute observations, then raw Schwab
five-minute observations. Both endpoints of a target must be supplied by the
same selected source. Existing endpoint distance limits remain. Provider-native
identities and immutable source hashes are saved, and the selected feed remains
visible in target and forecast evidence. Normalized files without their required
source evidence are not silently trusted.

`observed-multi-source-stock-v2` preserves that ordered four-source contract and
adds immutable provider-native FMP five-minute history as the fifth candidate.
V1 continues to mean exactly its original four sources. FMP bars use New York
interval-open timestamps, positive native volume and same-source endpoint pairs
within the existing tolerance; no intermediate minute is reconstructed. The
normal nightly fetch requests seven inclusive New York calendar dates with
overlap and retained revisions. Failures are recorded without blocking other
nightly work. See [native FMP receipts](fmp-native-five-minute-receipts.md).

Successful acquisition alone no longer licenses the older long carry-forward
completion. Unavailable endpoints remain unavailable. Split/discontinuity rules
continue to apply, while XNAS-specific exclusions apply to XNAS observations.
Planning histories and anchors use one source per symbol; actuals use compatible
same-source endpoint pairs. Historical artifacts retain their frozen definitions.

Model reports expose per-symbol and per-route class and decision support for
admitted, fitted, calibration and assessment populations. A pooled promotion is
labelled pooled; it does not imply separate qualification for each constituent
symbol or route. No new cross-trader operating gate is introduced. Independent
Gameplans and fifteen-minute traders retain their own state and manual controls.

## Evidence that can and cannot be established

New receipts preserve actual payloads and receipt clocks, input arrays, model
specifications and content-addressed source objects. Runtime evidence fingerprints
enumerated **loaded Python function and method code**, separately compares it to
current disk source, and records mismatches. It does not attest every global,
native extension or configuration value. A disk commit alone cannot identify a
running process's loaded code.

Old missing receipt clocks and missing runtime fingerprints cannot be recovered
retrospectively. Historical reconstruction remains labelled reconstruction.
Newly added market inputs need prospective history before their usefulness can
be assessed. Neither broader source coverage nor additional features guarantees
model quality; unchanged qualification criteria must still be evaluated.

## Offline checks

Use repository pytest fixtures for `test_intraday_verified_source`,
`test_intraday_market_inputs`, `test_intraday_repair_integration`,
`test_market_runtime_provenance`, `test_gameplan_observed_recovery`, and
`test_observed_source_workflow`, together with affected existing data, runtime,
supervision, sizing, completion and evaluation tests. These checks must not call
providers, brokers, fit production models, or start traders. A separately invoked
actual preparation run is operational data work and must retain its own evidence.
