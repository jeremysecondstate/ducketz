# Pricing gate review - September 21 preparation

Reviewed **2026-09-19T06:07:20.051678+00:00**. **Known Pricing quarantine; unchanged compact authority.** All **51** compact surface/manifest/receipt SHA-256 bindings match the prior verified audit (10,208,882 bytes total), and the current pointer still binds the August 20 publication `ml/option-pricing-runs/20260820T200404.173684Z`.

The unchanged source retains **72 natural rows / 17 distinct targets**, latest target **August 20 19:45 UTC** and latest publication **August 20 20:15:01 UTC**. COST, CROX, IONQ, PATH and TWST remain absent. Predictive standard deviation and interval-calibration columns remain absent under the unchanged verified source. These characteristics come from the prior normalization whose exact source files were reverified; normalization and historical input scans were not repeated.

The current native log materializes **352,057 rows** for eleven symbols, then quarantines the same **99 exact symbol/horizon routes across nine groups**. Fallback contracts remain `loop-a-all-v1-1h`, `loop-a-all-v1-4h`, `loop-a-all-v3-1d`, and `loop-a-all-v3-1w`. The current native helper explicitly checks exact preservation of requested non-Pricing features before selecting these contracts.

Native requirements remain **80% complete, 80% fresh joined and 20 distinct usable targets**. The current causal cutoff is **2026-09-19T04:29:32.689904Z**. Historical joins can be fresh at their original decision clocks; the saved August 20 authority is stale at today's cutoff.

Current publication-to-manifest binding verifies. Current saved gate fractions:

| Subfamily | Maximum complete | Maximum fresh joined | Maximum targets | Passing routes |
|---|---:|---:|---:|---:|
| constraints | 1.4451% | 1.4451% | 1 | 0/99 |
| edge | 0.6068% | 0.6068% | 1 | 0/99 |
| fair_value | 0.6068% | 0.6068% | 1 | 0/99 |
| interval_calibration | 0.0000% | 0.0000% | 0 | 0/99 |
| liquidity | 0.6068% | 0.6068% | 1 | 0/99 |
| uncertainty | 0.0000% | 0.0000% | 0 | 0/99 |

The current manifest confirms all 99 exact route identities, matching native admission decisions, unchanged thresholds, matching causal cutoff and **zero route errors**; its publication-to-manifest checksum binding verifies.

**No fresh source defect or justified focused repair was identified.** Preserve the native gate and reduced contracts; no retry, acquisition or retraining is indicated. Current OPRA completion is a separate source-maintenance result and does not create a new Pricing model publication.

Evidence: [structured review](C:/dev/ducketz/artifacts/analysis/overnight-20260919/pricing-gate-review.json), [current native log](C:/DATASTORE/ml/overnight-runs/20260919T040748.657031Z/loop_b_directional_generation.log), [prior audit](C:/dev/ducketz/artifacts/analysis/overnight-20260918/pricing-gate-review.json). Only the two requested evidence artifacts were written.
