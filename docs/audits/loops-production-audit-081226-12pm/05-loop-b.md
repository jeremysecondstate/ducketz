# Directional Loop B (owner 5)

Audited baseline commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

OPRA-first implementation update: 2026-08-14

Production entrypoint: `python -m ml.prediction_runtime` at the 15-minute `+5`
phase. Loop B remains owner 5; the Active Pricing and Options Capture changes do
not renumber or alter the other owners.

## Inputs and preserved joins

Loop B still waits for the completed Loop A authority and causally joins bars,
technicals, fundamentals, lifecycle signals, quote liquidity, energy, SEC, CME,
and ALFRED vintage macro data. The working ALFRED GDP, CPI, unemployment, and
FEDFUNDS coverage is preserved. SEC and derived CME activation/diagnostics are
unchanged by this update.

Options Capture supplies verified provider-neutral `opt__` quality evidence.
Active Pricing supplies append-only `opx__` history. Loop B no longer treats one
selected current Pricing generation as the complete evidence set: it verifies
the reachable receipt chain, reads all eligible generations, deduplicates by the
declared natural surface key, and chooses the newest generation whose first
availability is no later than the decision cutoff.

## Pricing feature contract

The joined columns are:

- `opx__causal_coverage`
- `opx__median_normalized_residual`
- `opx__median_predictive_standard_deviation`
- `opx__median_model_edge_in_half_spreads`
- `opx__positive_edge_fraction`
- `opx__negative_edge_fraction`
- `opx__raw_arbitrage_violation_rate`
- `opx__constrained_arbitrage_violation_rate`
- `opx__interval_80_coverage`
- `opx__interval_95_coverage`
- `opx__median_relative_bid_ask_spread`

A publication is invisible before verified first availability, after freshness
expiry, after quality rejection, or when its receipt/checksum fails. A joined
zero is preserved as observed data; it is not converted to missing. Uncertainty
stays null until a supported verified model supplies it. Interval coverage stays
null until at least 20 genuinely published out-of-sample outcomes have matured
after their prediction availability; hindsight replay cannot populate it.

## Family activation gates

`option-pricing-loop-b-family-coverage-freshness-gate-v1` independently gates:

- fair value;
- uncertainty;
- edge;
- constraints;
- interval calibration;
- liquidity.

Each family requires at least 80% populated coverage, 80% fresh verified joins,
and 20 distinct target surfaces. A column's presence alone cannot activate
training. Until the selected `opx__` profile passes every required family gate,
downstream fitting fails closed while audited nulls remain distinguishable from
real zeros.

## Publication and non-activation

Loop B continues to publish one immutable run under `ml/runs/<timestamp>/` and
advances `ml/latest/run.json` atomically. Strategy consumes that verified run.
No OPRA offline row can be converted into a prospective Loop B observation, and
no automated trading permission is created by the availability of these
features.
