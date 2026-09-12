# Option Pricing feature quarantine is nonblocking

Read-only audit completed September 12 at 05:13 UTC. Loop B generation
`20260912T045017.509319Z` completed at 05:10:57 UTC with nine models trained,
63 fresh live rows, 4,504 total prediction rows and no route errors. Its native
manifest and output hashes, publication-to-manifest checksum and historical
Pricing source were verified. Full route-level evidence is saved in
[option-pricing-quarantine.json](C:/dev/ducketz/artifacts/analysis/overnight-20260912/option-pricing-quarantine.json).

The warning correctly excludes the optional `opx` feature family. Every selected
Pricing subfamily must cover at least 80% of each route's rows, have at least
80% fresh joined rows and cover at least 20 distinct surface targets. All 63
symbol/horizon routes fail:

| Subfamily | Maximum fresh complete fraction across routes | Maximum distinct usable targets |
|---|---:|---:|
| Constraints | 1.4663% | 1 |
| Fair value, edge, liquidity | 0.6105% | 1 |
| Uncertainty, interval calibration | 0% | 0 |

The verified Pricing history contains 72 compact Schwab symbol/target rows
across 17 August 20 target clocks, latest 19:45 UTC. All are bound to the August
20 20:15:01.002032 UTC publication; the causal reader does not backdate their
availability. Only the original six symbols have surfaces; COST has none.
Most materialized rows consequently have `NO_PRIOR_PUBLICATION` or `STALE`
joins. The join freshness limits remain two hours for 1h, four hours for 4h,
two days for 1d and eight days for weekly routes. Missing predictive uncertainty
and interval calibration remain explicit even where other fields join.

This is incomplete/stale optional feature evidence, not a checksum failure or
a new provider error. The native gate selects `loop-a-all-v1-1h`,
`loop-a-all-v1-4h`, `loop-a-all-v3-1d` and `loop-a-all-v3-1w`; it explicitly
checks that these preserve every requested non-Pricing feature. The stock-only
workflow can continue with those contracts, subject to all mandatory downstream
stock history, model, publication and planning gates. Nothing here qualifies
Pricing models or authorizes options execution, and no new Pricing training,
provider query, repair or retry was performed.

Source evidence:

- [Loop B native manifest, configuration.pricing_evidence](C:/DATASTORE/ml/runs/20260912T045017.509319Z/manifest.json).
- [Loop B publication receipt](C:/DATASTORE/ml/runs/20260912T045017.509319Z/publication.json).
- [Verified Pricing publication pointer](C:/DATASTORE/ml/option-pricing-latest/run.json).
- [Recorded gate and exact baseline-selection implementation](C:/dev/ducketz/ml/runtime_pipeline.py:2274).

The old Pricing health file is dated August 20 and is not treated as a current
incident. Original gate fractions use 229,571 materialized rows; the published
220,713-row sample file excludes closed lockbox rows and is not substituted for
that denominator.
