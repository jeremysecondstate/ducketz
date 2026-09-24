# Archive enrichment admission diagnosis

Inspected 2026-09-24 at 06:39 UTC using only the immutable publication
`C:/DATASTORE/ml/nightly-gameplan-runs/20260924T062615.050242Z` and local code.
No model was fitted and no production file was modified during this diagnosis.

The publication's archive contract intentionally retains older rows whose
optional operational inputs are absent. The directional models admit those
rows using verified archive features. The separate enrichment fitter still
requires eight legacy `mr__`/`bp__` inputs on every execution row, causing
`ValueError: Independent enrichment market features must be finite causal observations`.

| Horizon | Execution rows | All eight inputs finite | All eight inputs absent |
|---|---:|---:|---:|
| 1h | 165,549 | 92,800 | 72,749 |
| 4h | 42,858 | 25,012 | 17,846 |
| 1d | 5,917 | 3,776 | 2,141 |
| 1w | 5,831 | 3,750 | 2,081 |

There are **zero partially missing rows and zero infinity cells** across these
market inputs. Every configured symbol retains finite enrichment examples.
Most first finite examples occur in April 2023; SNDK starts in March 2025.
Exact per-symbol dates and counts are in
[enrichment-missing-profile.json](C:/dev/ducketz/artifacts/analysis/overnight-20260924/enrichment-missing-profile.json).

The smallest correction is a training admission rule limited to
`xnas-archive-prior-session-features-v1`: exclude rows lacking required optional
enrichment inputs, retain every finite causal row, and report the exclusions.
Do not impute inputs, rename archive features as legacy features, rewrite the
directional cohorts, remove source checks, or relax assessment gates.

Suggested implementation constraints:

- Validate candidate identities, target clocks, prices, labels and source before
  filtering, so an absence exclusion cannot conceal invalid outcome evidence.
- Preserve strict rejection of malformed/nonfinite supplied values and legacy
  publications with missing required features. Only genuinely absent archive
  optional inputs qualify for the new exclusion rule.
- Store separate counts for nonexecution/context rows and missing optional
  market-input rows; the existing `excluded_context_rows` name must not absorb
  the new reason.
- Bind the admission policy, counts by symbol/feature and excluded natural-key
  digest into each fitted or unfitted horizon and the training report.
- Use identical admission in fitting and `verify_independent_model_sources`,
  and rederive/compare the exclusion report alongside cohort and partition hashes.
- Test archived absent-input exclusion, all finite row retention, unchanged
  legacy rejection, malformed/infinite rejection, invalid target rejection even
  when features are absent, and tampered exclusion evidence rejection.

Relevant code: [admission](C:/dev/ducketz/ml/stock_trader/independent_training.py:48),
[fit report](C:/dev/ducketz/ml/stock_trader/independent_training.py:466),
[source verification](C:/dev/ducketz/ml/stock_trader/independent_training.py:606),
and [unchanged execution input contract](C:/dev/ducketz/ml/stock_trader/market_features.py:11).
