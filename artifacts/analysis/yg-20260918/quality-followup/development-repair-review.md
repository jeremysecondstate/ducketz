# Locked development-only quality repair

Source: immutable YG `20260918T072555.813034Z`. All production model comparisons here used train, selection or calibration development data. Assessment labels and features were excluded by Parquet predicate; only saved clock geometry was read to recover the existing partition boundary. Price observations, source identity, labels, partitions, promotion thresholds and trading controls are unchanged.

## Hourly calibration eligibility

The original development selector preferred constrained Platt log loss **0.6931471806** over identity **0.6951784801**, even though Platt produced a constant 0.5 and had its nondecreasing constraint active. Such a map cannot satisfy the existing promotion requirement for varying probabilities.

The raw-direction-only repair excludes development candidates that are constant or orientation constrained before ranking their development log loss. The final full-calibration refit must also retain information. If it does not, only an already eligible identity candidate can replace it; an ineligible identity fallback raises an error. Historical cost-target selection remains unchanged.

For the saved hourly model, identity retains a full-calibration probability span of **0.2032711613**, versus **0** for the old Platt map. This fixes a selection/eligibility inconsistency; it does not claim improved held-out accuracy.

## Weekly bounded candidates

The existing daily/intraday logistic grid `{0.001, 0.01, 0.1, 1}` was extended to raw-direction weekly candidates. Its development log losses were **0.70891105**, **0.71271535**, **0.73627103**, and **0.77337513**. None beat the existing 50/50 HGB/MLP blend (**0.70369792**), so this change alone would not alter the selected weekly model.

A separately preregistered, uniform shrinkage comparison used `w × model_probability + (1−w) × training_positive_rate` for fixed weights `{0.25, 0.5, 0.75, 1}` across all nine existing candidate families. No zero or arbitrarily small weights were considered. Source-clock feature changes were studied separately and were not combined with shrinkage.

The selected standalone shrinkage candidate is the HGB/MLP blend with 25% neural contribution and shrinkage weight **0.5**. Selection log loss is **0.7011636448**, versus **0.7036979245** originally. Selection Brier is **0.2537161150**, versus **0.2541386349**. Its probabilities span **0.43714–0.70953** (standard deviation **0.05659**). Selection direction accuracy is **50%**; an accuracy improvement is not claimed. The constant TRAIN base-rate benchmark has log loss **0.7117097215** and Brier **0.2591357095**.

The native policy selects among all 36 fixed candidates using selection log loss, preferring weight 1 on an exact tie. Selection uses only the purged TRAIN prior. Final refitting uses the TRAIN-plus-selection prior, frozen into a stable serialized estimator wrapper. Calibration and final assessment remain separate and unchanged in authority. The separate source-clock feature proposal lost on development and was not adopted.

## Verification

Focused tests cover the calibration edge cases, fixed-grid values, numerical probability mixing, unchanged identity behavior, invalid values, fresh-process joblib loading, full-fit prior and candidate provenance, assessment-label perturbation independence, and exact serialized forecast reproduction. The full-fit fixture selects a nontrivial shrinkage weight. Existing native model, champion and price-boundary tests also pass. Production fitting and activation remain with the supervising owner.

Evidence: `development-evidence.json`, `weekly-shrinkage-preregistration.json`, `weekly-shrinkage-development.json`, and `weekly-shrinkage-selection-predictions.parquet` in this directory. No assessment-driven candidate adjustment is authorized by these results.
