# Options Capture (logical Loop 4)

Audited baseline commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

OPRA-first implementation update: 2026-08-14

Production entrypoint: `python -m datafetching.options_runtime` at the
15-minute `+6` phase. The owner number and coordination relationship with
Active Pricing are unchanged.

## Scope and provider contract

The runtime validates the exact six-symbol production universe from
`ml/universe.py`; CALL and PUT routes are derived, yielding 12 routes. `SPY` is
not accepted by this owner.

Options Capture is provider-neutral. The required provider roles are:

- `databento-opra`: canonical L1 market evidence. Point-in-time definitions
  and the last valid NBBO strictly before the target are required.
- `schwab`: broker-chain enrichment, explicit causal fallback, disagreement
  measurement, and execution/fill validation.

`options/providers.py` defines the injected, mockable OPRA adapter. The runtime
never fabricates OPRA and never relabels Schwab as OPRA. When no live OPRA
adapter/credentials are configured, the runtime records Schwab fallback
explicitly; wiring and operating a live adapter remains an operator rollout
step.

## Immutable identity and storage

The natural publication key is:

```text
(provider, symbol, target_snapshot_for)
```

Provider-specific authorities are:

```text
stocks/<SYMBOL>/options/snapshots/databento-opra/<target_ns>/
stocks/<SYMBOL>/options/snapshots/schwab/<target_ns>/
stocks/<SYMBOL>/options/latest/databento-opra.json
stocks/<SYMBOL>/options/latest/schwab.json
```

Each generation contains raw, normalized contracts, option quality, manifest,
and receipt files with checksums. Publication is staged, verified, and moved
atomically before its pointer changes.

An identical retry returns the earliest verified generation and does not call
the provider again. Divergent content for the same natural key fails closed and
is never overwritten. Existing Schwab v1 generations and monthly mirrors remain
readable. Legacy duplicates remain immutable, but readers canonicalize them to
the earliest verified receipt and emit duplicate/conflict diagnostics so they
cannot multiply training weight or prospective sessions.

This identity also fixes closed-market discovery: once the natural target is
committed, later discovery intervals reuse it rather than fetching and
publishing the same market target again.

## Normalized v2 contract

The shared normalized schema includes provider/dataset, underlying and contract
symbols, quote event and target times, first availability/receipt, BBO and
sizes, optional trade price/size, strike/expiration/CALL-or-PUT, multiplier and
standard/mini/adjusted flags, publisher/venue lineage, staleness and quality,
definition-as-of, exercise/settlement attributes, source files/checksums,
schema/policy versions, evidence lane, and fallback status.

OPRA normalization binds definitions effective no later than the target and
selects each contract's final valid BBO strictly before the target. A causal
Loop A source-target close supplies the equity spot when OPRA itself does not.
Ambiguous contract definitions stay marked and are excluded from ordinary
pricing eligibility.

## Coordination and outputs

Options Capture waits briefly for the exact Active Pricing target receipt and
embeds that verified barrier when available. It also verifies exact Loop A bar
readiness. Existing Schwab pending-capture behavior remains checksum-sealed and
backward compatible; an unavailable target is never backdated.

Verified snapshots feed:

- Active Pricing source surfaces, outcome reconciliation, and provider
  disagreement;
- Loop B `opt__` quality features;
- Strategy broker enrichment and entry/exit execution evidence.

No paid Databento request, broker request, or automated action is performed by
tests or migration tools. All repair/migration exercises must target temporary
fixtures or an explicit datastore copy.
