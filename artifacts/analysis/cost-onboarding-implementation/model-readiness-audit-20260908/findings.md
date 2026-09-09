# Independent stock model readiness audit — 8 September 2026

The four forecast models in `C:/DATASTORE/ml/nightly-gameplan-runs/20260908T065643.819336Z` correctly remain unpromoted. The saved assessment is reproducible. Missing equity target history and unstable calibration explain the observed failures; the completed OPRA cursors do not resolve either problem. A separate confirmed serialization defect affects the portability of CLI-created blended estimators, not the saved scores.

This audit read existing local data and predicted with the existing estimators. It made zero fits, provider calls, broker calls, or changes to immutable artifacts. The executable reconstruction and detailed evidence are `audit_saved_models.py` and `evidence.json` in this directory. All eight source files used in reconstruction, including the source samples and seven native minute-bar files, matched their immutable manifest SHA-256 values. Every partition row count and every saved raw and calibrated assessment metric matched within numerical tolerance of 1e-10. The audit records the exact code hashes used because other work is extending the source interfaces concurrently.

## Observed quality, with no retrospective candidate selection

Lower Brier and log loss are better. The prior is the positive rate of the fitted train-plus-selection cohort. The raw column is diagnostic evidence already saved by the original run, not a newly selected replacement.

| Group | Assessment rows / decision clusters | Calibrated Brier / log loss | Raw Brier / log loss | Training-prior Brier / log loss | Calibration positive rate → assessment positive rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1h | 570 / 15 | 0.242008 / 0.677110 | 0.237810 / 0.668224 | 0.240597 / 0.674240 | 44.74% → 40.00% |
| 4h | 129 / 15 | 0.257584 / 0.708329 | 0.233950 / 0.658936 | 0.238854 / 0.670787 | 52.55% → 36.43% |
| 1d | 64 / 11 | 0.306163 / 0.807707 | 0.215800 / 0.620211 | 0.218021 / 0.628370 | 61.54% → 29.69% |
| 1w | 20 / 10 | 0.337395 / 0.908242 | 0.250948 / 0.693141 | 0.251054 / 0.695260 | 80.00% → 50.00% |

Every calibrated group loses to its training-prior baseline on both proper scores. The 4h calibrator's nondecreasing constraint is active and its slope is zero, producing the constant probability 0.5255474453. Its directional-information check correctly fails. Daily and weekly calibrated mean probabilities are 0.611406 and 0.796021; their assessment class rates are 0.296875 and 0.5. Their expected calibration errors, 0.314531 and 0.296021, exceed the existing 0.15 limit. Hourly retains variation and passes the calibration-error limit, but still loses to its prior on both proper scores.

The fixed Platt fit uses regularization C=0.1. It shrinks the logit slope toward zero, while its intercept can follow the small calibration cohort's class balance. Weekly calibration has only ten rows and six decision timestamps, with eight positives. Daily calibration has 52 route rows but only 13 distinct symbol/target windows: D+1 through D+5 forecasts from different prior dates repeatedly observe the same eventual session return. This is a legitimate forecast panel, but those 52 rows are not 52 independent realized returns. Assessment daily rows similarly represent only 18 distinct symbol/target windows. Weekly windows overlap in time. More independent support, and reporting uncertainty at the realized-window/session level, should precede any calibration redesign.

This evidence does not establish that raw forecasts are acceptable probabilities. Selecting the identity calibrator now because it won on the already observed assessment would consume that assessment as development data. No calibrator, threshold, gate, or model parameter was changed by this audit.

## Equity support is materially incomplete

The independent target builder correctly requires an observed positive entry and exit price within five minutes of the requested clock boundaries. It does not substitute regular-session returns for 04:00–17:00 returns. The pinned native files provide these admitted target counts:

| Group | Admitted / candidate rows | COST admitted / fitted rows | Critical support gap |
| --- | ---: | ---: | --- |
| 1h | 4,354 / 54,460 | 474 / 347 | COST has no 04:00, 05:00, 14:00, 15:00, or 16:00 target history. |
| 4h | 1,015 / 15,553 | 77 / 56 | COST history supports only the 08:00 route. |
| 1d | 662 / 19,422 | 0 / 0 | COST and GOOG have no admitted daily targets. AAPL's two fitted rows are D+2 and D+3 only; D+1 has none. |
| 1w | 125 / 3,883 | 0 / 0 | COST and GOOG have no admitted weekly targets. AAPL has four admitted rows but none in fitting. |

The original loader selected `stocks/<symbol>/bars/1m/databento/normalized/*_ohlcv-1m_1m.parquet`. A separate source-coverage effort is inspecting explicit same-source XNAS archives. This audit has not counted or assumed coverage from that new interface. The correct next data experiment is to measure all seven symbols and every execution route against genuine observed boundaries using that explicit source, keeping model parameters and existing gates fixed. Sparse native boundary observations are a data limitation, not evidence that the five-minute tolerance is erroneous.

There is also a concrete readiness-reporting limitation: the publisher changes status to `RESEARCH_NO_TARGET_HISTORY` only when the symbol has zero fitted rows for the entire horizon. It already reports per-route support, but does not use that support to block a route with no fitted symbol/route examples. Thus a future group-wide promotion alone would not prove that COST 04:00 or AAPL daily D+1 has supporting history. The source experiment should report symbol-plus-route fitted and assessment support explicitly. No unsupported route should be described as ready merely because other routes for that symbol were fitted. This limitation does not explain the current score failures because every group already remains unpromoted.

## Feature and temporal alignment checks

The source selector admits 3,897 prior-session rows with information available before each action session's 04:00 opening. The target builder uses those causal frozen features consistently across the next-session route grid. Within the checksum-pinned reconstruction, all 12 adjacent partition boundaries have zero left-partition target labels arriving at or after the next partition's earliest decision timestamp. The purged splits therefore have no demonstrated cross-partition label leakage in this run. Reconstruction of the saved scores also rules out a target/index alignment discrepancy in these inputs.

Of 154 manifest features, exactly 126 are entirely null in every group's training cohort. The 28 surviving columns are technical `mr__`, `bp__`, and `bar__` features. Daily and weekly admit 27 because `bp__confidence_score` is constant in those training rows. The omitted set contains fundamental, lifecycle, macro, CME, quote, option, and other contextual features. The model's numeric-support filter is correctly excluding all-null columns. Completing OPRA history does not automatically populate those frozen overnight source rows; enrichment must be joined causally before fitting and independently assessed. Including an available future or publication-time value in historical rows would be an error. The separate enrichment effort should inspect source availability timestamps before any join.

No sample/feature misalignment, retrospective label use across these partitions, or incorrectly calculated saved metric was demonstrated. Pooling correlated outlook rows and using a small regularized Platt cohort are research design choices; changing them requires a preregistered independent evaluation, rather than describing an observed loss as a coding bug.

## Confirmed portability defect and narrow correction

Ordinary `joblib.load` in a fresh interpreter fails on the saved blended models with `AttributeError: Can't get attribute '_ProbabilityBlend' on <module '__main__'>`. The publication command defined the blend class in the module executed as `__main__`. The 1h, 1d, and 1w saved blends therefore depend on that transient module identity. The read-only audit imported the same class as an explicit compatibility alias solely to inspect those original files.

The approved correction is a stable `ml.gameplan_estimators.ProbabilityBlend` class, with unchanged arithmetic, imported into the CLI publisher as `_ProbabilityBlend`. A new regression test serializes through the CLI-style alias and loads in an independent interpreter without any `__main__` alias, covering all three existing blend weights. The original immutable files remain unchanged. This correction makes regenerated estimators portable; it neither repairs nor overrides their quality metrics.

## Preregisterable next experiments

First freeze a source-coverage experiment: exact XNAS source contract and checksums, boundary policy, date range, symbols and routes, unchanged model parameters, existing chronology and gates. Before looking at its assessment scores, report target support for train, selection, calibration, and assessment separately, including distinct realized windows and per-symbol/per-route counts. If coverage remains inadequate, report that result rather than widening the tolerance or generating prices. Because the current assessment has already been inspected, repeated development against its dates cannot establish fresh out-of-sample quality.

If adequate genuine source coverage still leaves calibration unstable, preregister a separate calibration experiment with a final untouched chronological cohort. Use only earlier development data for chronological inner folds, with purges based on complete label availability. Fix the candidate set, fold boundaries, sample weighting, proper-score criterion, minimum independent support, and tie-breaking before evaluating candidates. Select and fit calibration using those inner development folds only, freeze the resulting choice, and score the final cohort once. The candidate set may compare existing Platt with a shrinkage/identity reference, but this audit does not choose one. Preserve the existing promotion requirements; lack of support or another failure remains research-only. The current exposed assessment must not be relabeled as untouched.
