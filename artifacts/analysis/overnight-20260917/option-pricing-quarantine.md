# Optional Pricing quarantine — September 17 preparation

Audited **2026-09-17T05:45:36.599980+00:00**. The native quarantine remains the known retained August 20 Pricing limitation on **99 symbol/horizon routes**, across all 11 symbols and nine groups. No new source corruption or focused repair was identified.

The authority is still `ml/option-pricing-runs/20260820T200404.173684Z`, published August 20 20:15:01.002032 UTC. The complete 17-generation pointer/receipt/manifest chain and each compact surface binding were checked. All **51 compact surface/manifest/receipt paths and SHA256 hashes** match the September 16 audit. Other historical Pricing outputs and input archives were not rehashed. Native compact normalization yields the same **72 natural rows / 17 distinct targets**. The latest target is **August 20 19:45 UTC** and latest receipt-bounded availability **August 20 20:15:01.002032 UTC**.

No Pricing row is fresh at the current causal cutoff, **2026-09-17T04:27:00.270375Z**, for 1h, 4h, 1d or 1w. COST, CROX, IONQ, PATH and TWST have no Pricing surfaces. Predictive standard deviation and 80%/95% interval calibration are absent throughout this compact evidence.

The current Loop B publication manifest became available during the audit. Its publication-to-manifest checksum binding verifies and its own saved gate records the following current route statistics; prior-day fractions were not reused:

| Pricing subfamily | Maximum complete fraction | Maximum fresh joined fraction | Maximum usable targets | Passing routes |
|---|---:|---:|---:|---:|
| constraints | 1.4535% | 1.4535% | 1 | 0/99 |
| edge | 0.6083% | 0.6083% | 1 | 0/99 |
| fair value | 0.6083% | 0.6083% | 1 | 0/99 |
| liquidity | 0.6083% | 0.6083% | 1 | 0/99 |
| interval calibration | 0.0000% | 0.0000% | 0 | 0/99 |
| uncertainty | 0.0000% | 0.0000% | 0 | 0/99 |

The unchanged thresholds are **80% complete, 80% fresh joined and 20 distinct usable targets**. Historical joins can be fresh at their original decision clocks even when all Pricing source rows are stale at today's cutoff. The current native gate uses its original materialized route rows; this audit did not rebuild those 350,913 samples or substitute published training rows as a denominator.

The selected reduced contracts remain `loop-a-all-v1-1h`, `loop-a-all-v1-4h`, `loop-a-all-v3-1d`, and `loop-a-all-v3-1w` for weekly/day groups. Calling the native gate-selection helper confirmed exact preservation of every requested non-Pricing feature across all nine groups.

This optional Pricing qualification is separate from tonight's successfully maintained **33 OPRA production scopes**. Fresh native option schemas do not manufacture new qualified Pricing surfaces. Preserve the gate and native reduced contracts; no repair, retry, retraining or new provider acquisition is justified merely to remove this quarantine.

Evidence: [JSON audit](C:/dev/ducketz/artifacts/analysis/overnight-20260917/option-pricing-quarantine.json), [current native manifest](C:/DATASTORE/ml/runs/20260917T052042.075600Z/manifest.json), [Loop B log](C:/DATASTORE/ml/overnight-runs/20260917T040722.433471Z/loop_b_directional_generation.log), [prior audit](C:/dev/ducketz/artifacts/analysis/overnight-20260916/option-pricing-quarantine.json).
