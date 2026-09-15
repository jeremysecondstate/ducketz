# Current optional Pricing quarantine

Audited 2026-09-15T05:43:54.864259+00:00. No newly actionable source failure identified.

- Native Loop B excluded Pricing features on **99 routes** across all 11 symbols and nine horizons. It selected `loop-a-all-v1-1h`, `loop-a-all-v1-4h`, `loop-a-all-v3-1d` and `loop-a-all-v3-1w`; the native exact preservation check passes for every requested non-Pricing feature contract.
- Pricing source verification succeeds. The **72 compact rows and their source hashes are unchanged** from the September 12 audit. They cover six symbols and only **17 distinct August 20 target clocks**, below the required 20 per route.
- Predictive standard deviation and interval calibration remain entirely missing. No source row is fresh at the current cutoff for any supported horizon. COST, CROX, PATH, TWST and IONQ have no Pricing surfaces.
- Thresholds remain 80% complete rows, 80% fresh joined rows and 20 distinct usable targets. The previous 63 rejected routes have become 99 with the expanded production universe; this is the same optional coverage limitation.
- Current original route fractions are available in the saved manifest. September 12 fractions were not reused as current. The current log records 349,769 materialized rows; this audit did not rematerialize or retrain them.
- This optional Pricing exclusion is separate from tonight's completed 33-scope OPRA production coverage. No provider calls, repairs, retries, training, broker actions, claim operations or production edits occurred.

Detailed [audit](C:/dev/ducketz/artifacts/analysis/overnight-20260915/option-pricing-quarantine.json); [native log](C:/DATASTORE/ml/overnight-runs/20260915T040742.365640Z/loop_b_directional_generation.log); [prior comparison](C:/dev/ducketz/artifacts/analysis/overnight-20260912/option-pricing-quarantine.json).

## Published native qualification evidence

The completed publication has zero route errors and retains the Pricing gate across 349,769 original materialized rows. Its published 335,785-row sample output omits closed lockbox rows and is not substituted for the gate denominator. The publication binds the manifest checksum.

| Pricing subfamily | Maximum complete/fresh fraction | Maximum usable targets | Passing routes |
|---|---:|---:|---:|
| constraints | 1.4620% | 1 | 0/99 |
| edge | 0.6098% | 1 | 0/99 |
| fair_value | 0.6098% | 1 | 0/99 |
| interval_calibration | 0.0000% | 0 | 0/99 |
| liquidity | 0.6098% | 1 | 0/99 |
| uncertainty | 0.0000% | 0 | 0/99 |

Historical joins can contain some fresh rows at their original decision clocks even though every source row is stale at today's cutoff. The all-route exclusion remains expected; the six subfamilies fail the unchanged 80%/20-target rules.

