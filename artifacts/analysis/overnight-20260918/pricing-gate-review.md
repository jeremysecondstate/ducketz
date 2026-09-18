# Pricing gate review — September 18 preparation

Reviewed **2026-09-18T05:45:09.133172+00:00**. The current Loop B log and verified publication manifest quarantine the same **99 symbol/horizon routes** across all 11 configured symbols and nine groups. The Pricing authority still points to `ml/option-pricing-runs/20260820T200404.173684Z`, published August 20 20:15:01.002032 UTC. The pointer is unchanged from the September 17 review, and **all 51 previously audited compact surface/manifest/receipt hashes match**.

The unchanged compact evidence retains its previously verified **72 natural rows / 17 distinct targets**, latest target August 20 19:45 UTC, and absent predictive standard deviation and interval-calibration fields. COST, CROX, IONQ, PATH and TWST remain absent. This review did not rerun normalization or rehash unrelated historical inputs.

Tonight's publication-to-manifest hash binding verifies. Its own saved native gate gives these current fractions, without rebuilding materialized samples or reusing prior-day fractions:

| Pricing subfamily | Maximum complete | Maximum fresh joined | Maximum targets | Passing routes |
|---|---:|---:|---:|---:|
| constraints | 1.4493% | 1.4493% | 1 | 0/99 |
| edge | 0.6075% | 0.6075% | 1 | 0/99 |
| fair_value | 0.6075% | 0.6075% | 1 | 0/99 |
| interval_calibration | 0.0000% | 0.0000% | 0 | 0/99 |
| liquidity | 0.6075% | 0.6075% | 1 | 0/99 |
| uncertainty | 0.0000% | 0.0000% | 0 | 0/99 |

The native gate still requires **80% complete, 80% fresh joined and 20 distinct usable targets**. Historical joins may qualify as fresh at their original decision clocks; the retained Pricing evidence is stale at the current causal cutoff, **2026-09-18T04:27:28.741812+00:00**.

Current native outcomes use the same reduced feature contracts: `loop-a-all-v1-1h`, `loop-a-all-v1-4h`, `loop-a-all-v3-1d`, and `loop-a-all-v3-1w`. The native selection helper checks exact preservation of non-Pricing features. The publication reports no route errors.

**No new defect or justified focused repair was identified.** Preserve the gate and reduced contracts; no retry, acquisition or retraining is indicated by this known sparse/stale Pricing limitation.

Evidence: [structured review](C:/dev/ducketz/artifacts/analysis/overnight-20260918/pricing-gate-review.json), [current native manifest](C:/DATASTORE/ml/runs/20260918T051928.441440Z/manifest.json), [current Loop B log](C:/DATASTORE/ml/overnight-runs/20260918T040804.048018Z/loop_b_directional_generation.log), [prior verified audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/option-pricing-quarantine.json). Only these evidence artifacts were written; no provider/broker calls, ownership actions or production changes occurred.
