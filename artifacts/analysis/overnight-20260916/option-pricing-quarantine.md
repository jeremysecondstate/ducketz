# Current optional Pricing quarantine

Read-only audit at **2026-09-16 05:59:18 UTC** confirms the current native exclusion on **99 symbol/horizon routes**, across all eleven symbols and nine horizon groups. No new source corruption or actionable defect was identified.

The native verified compact Pricing reader succeeds at this run's causal cutoff, **04:29:29.523354 UTC**. Its **72 rows / 17 distinct targets** and all source paths/hashes are unchanged from the September 15 audit. Latest target remains **August 20 19:45 UTC**, latest availability **August 20 20:15:01.002032 UTC**. No row is fresh at the current cutoff for 1h, 4h, 1d or 1w. COST, CROX, PATH, TWST and IONQ have no Pricing surfaces. Predictive standard deviation and 80%/95% interval calibration are missing throughout the compact source.

The **current** publication manifest became available during this audit; its publication-to-manifest checksum binding passes. Its saved gate supplies the following current route statistics (not copied from the prior audit):

| Pricing subfamily | Maximum complete fraction | Maximum fresh joined fraction | Maximum usable targets | Passing routes |
|---|---:|---:|---:|---:|
| constraints | 1.4577% | 1.4577% | 1 | 0/99 |
| edge | 0.6090% | 0.6090% | 1 | 0/99 |
| fair value | 0.6090% | 0.6090% | 1 | 0/99 |
| liquidity | 0.6090% | 0.6090% | 1 | 0/99 |
| interval calibration | 0% | 0% | 0 | 0/99 |
| uncertainty | 0% | 0% | 0 | 0/99 |

All fail the unchanged **80% complete, 80% fresh joined and 20 distinct usable-target** thresholds. Historical joined rows may be fresh at their original decision clocks even though all source rows are stale at today's cutoff. The native gate is calculated over original materialized route rows; later published training samples are not substituted for its denominator.

The selected reduced feature contracts are `loop-a-all-v1-1h`, `loop-a-all-v1-4h`, `loop-a-all-v3-1d` and `loop-a-all-v3-1w` (including weekly day groups). Calling the native contract-selection helper verifies exact preservation of every requested non-Pricing feature for all nine groups.

This is the known optional Pricing coverage/freshness/uncertainty limitation, separate from tonight's successful 33-scope OPRA production history. Preserve the gate and the native reduced contracts; no retry, retraining, new provider request or production repair is justified merely to remove the quarantine. This audit only read compact Pricing history, source hashes, native logs and the saved control-plane gate; it did not rematerialize samples, train models or rehash the full current Loop B payload.

Evidence: [current detailed audit](C:/dev/ducketz/artifacts/analysis/overnight-20260916/option-pricing-quarantine.json), [current native manifest](C:/DATASTORE/ml/runs/20260916T053415.692855Z/manifest.json), [native log](C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z/loop_b_directional_generation.log), [prior-day audit](C:/dev/ducketz/artifacts/analysis/overnight-20260915/option-pricing-quarantine.md).
