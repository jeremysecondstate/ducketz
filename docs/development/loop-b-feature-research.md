# Isolated Loop B OHLCV feature research

`ml.loop_b_research_features` supplies four pure OHLCV measurements.
`ml.loop_b_research` compares a fresh baseline with three candidate families and
one combined candidate in an explicitly supplied, private development cohort.
Neither module is wired into production, the feature registry, default models,
canonical predictions, task registration or publication helpers. Candidate
`FeatureSpec` entries remain `IMPLEMENTED_BUT_QUARANTINED`.

## Fixed feature definitions

The semantic version is `native-ohlcv-research-v1`. All four values are
dimensionless and apply to hourly or daily native bars. Daily measurements may
support daily and weekly target routes; the saved target definition determines
the experiment, not a horizon's display name.

| Family / column | Formula | Hypothesis and overlap |
| --- | --- | --- |
| activity: `rx_activity__slot_range_log_ratio` | `log(q / median(previous comparable q))`, where `q=(high-low)/close`; previous 20 scheduled sessions, at least 10 valid native comparators | Unusual range relative to the same session slot may distinguish ordinary seasonal movement from unusual activity. Existing breakout logic already normalizes volume by session slot, so another volume ratio is omitted. |
| path: `rx_path__downside_variation_share_10` | `sum(min(r,0)^2)/sum(r^2)`, for 10 close-to-close log returns | Measures downside concentration of realized variation, beyond overall volatility and signed momentum. |
| path: `rx_path__movement_concentration_10` | `sum(abs(r)^2)/sum(abs(r))^2`, for the same 10 returns | Distinguishes movement spread across bars from a small number of shocks; ranges from 0.1 to 1 when defined. |
| interaction: `rx_interaction__return_persistence_10` | `sum(r)*abs(mean(sign(r)))`, for the same 10 returns | A signed move with persistent return signs may behave differently from oscillating movement. Cumulative return overlaps existing momentum; unsigned persistence supplies the explicit interaction. |

These are underlying measurements, not a combined bullishness score. No
calendar/target-window context features are admitted in this initial design.
Target/future/label/outcome feature names remain prohibited by the harness.

## Source and availability contract

Call `calculate_research_ohlcv_features(bars, symbol=..., timeframe="1h"|"1d")`.
Inputs are `timestamp` (or `bar_timestamp`), OHLCV, and explicit boolean
`is_native`. Optional `bar_complete` also requires boolean evidence. Unknown,
derived, carried-price and incomplete bars never contribute. Provenance must
come from reviewed source metadata; absence of a derived flag does not prove a
bar native. The caller must review splits and other price adjustments before
calculation. Uniform price rebasing is invariant; an unadjusted split within a
window is not. Do not infer corporate actions using future price observations.

Hourly timestamps denote interval starts. A value becomes available one hour
after that timestamp plus five minutes. Daily timestamps are UTC-midnight
session-date labels, available at the official XNYS session close plus five
minutes. Supplied bar-end timestamps are checked against this policy. The
caller must join the exact source bar and require `available_at <= decision_at`;
there is no stale as-of carry. Inputs with invalid OHLC ordering, nonpositive
prices, nonfinite values or negative volume are ineligible. A native zero-volume
bar is not automatically classified as carried.

The pinned `exchange_calendars` package supplies XNYS holidays, early closes,
and DST. Hourly activity comparators match New York clock slot, session
composition (including the hour crossing the regular open), and regular-session
duration. Normal sessions support whole-hour slots from 04:00 through 19:00
Eastern. Short sessions conservatively use only hours wholly within the regular
session. Daily comparators match regular-session duration. Both use only earlier
sessions and never fall back to another slot or session type. Early-close
activity is usually missing because ten comparable short sessions do not occur
within the last twenty sessions.

Hourly path windows require eleven consecutive scheduled full regular hours
(normally starts 10:00 through 15:00 Eastern); daily paths require eleven
consecutive exchange sessions. Scheduled overnight/weekend/holiday closures are
skipped. A missing scheduled native bar breaks the window. Across-session
returns include the overnight move; there is no inference about movement inside
a bar. Extended-session hourly rows have no path measurement. No synthetic rows
or forward/backward price filling are used. Insufficient warm-up and zero
denominators yield missing values. The complete contract is returned by
`semantic_contract()`.

## Cohort construction is an explicit caller responsibility

The reusable harness does not discover DATASTORE, follow moving pointers,
reconstruct target prices, open legacy fitted models or read sealed files.
Supply a reviewed dataframe with unique `sample_id`, `symbol`, timezone-aware
`decision_at`, `target_start`, `target_end`, binary `y`, and preferably the exact
`label_available_at`. Supply only eligible nonsealed outcomes, an explicit
frozen development boundary, excluded IDs, source/input hashes, and provenance.
Do not construct eligibility by repartitioning already redacted samples.

Where historical membership needs reconstruction, reconcile saved boundaries,
training counts/cutoffs and assessment identities using metadata only. Record
that a model-name/version directory is not a cryptographically pinned historical
manifest and that reconciliation is not an original saved training-ID receipt.
Freeze new exact research IDs. For shared feature selection across routes,
consider overlaps with other routes' protected outcomes; a common earlier
protected-period boundary can be necessary even when a row is training for its
own route. Never move the frozen boundary to reclaim protected rows.

## Bounded matched comparison

`run_experiment` accepts the baseline's admitted columns and saved semantic
transform mapping, three optional family lists, and an explicit
`PrivateExperimentStore`. The default design uses a half-history initial
training region followed by three expanding chronological folds. Target starts
are grouped across symbols and decisions. A whole group is purged unless its
target outcome and label availability precede the earliest validation decision.
Validation end groups are also purged to prevent overlapping target outcomes
from straddling successive validation folds.

All variants use the same eligible rows, folds, binary target and fixed C=1
logistic family. This is a matched research baseline, not reproduction of an
original production fit. Semantic transforms, finite-value admission, duplicate
and constant checks, median imputation, quantile clipping and standard scaling
are fitted separately using each training fold only. No fitted calibration or
hyperparameter search is performed. Finite availability controls column
admission, not row selection based on validation coverage. Baseline and
candidate probabilities are compared on all matched validation rows.

The primary metric is target-cluster-averaged log loss; Brier score, fixed-bin
reliability, per-fold, per-symbol and per-month results are also recorded.
Training and scoring weights avoid counting repeated decisions/symbols as
independent target starts. Paired moving blocks of five observed target dates
provide approximate temporal uncertainty. The predeclared multiplicity budget
must cover all horizon/family comparisons, not just a favorable slice.
Autocorrelation beyond the block length, common market exposures and overlapping
weekly outcomes remain limitations; confidence intervals are not proof of
independence.

The default acceptance rule requires three successful folds, at least forty
out-of-sample target dates, an absolute log-loss improvement of 0.005, a corrected
upper paired log-loss bound below zero, no aggregate Brier deterioration, at
least two improving folds and improvement in more than half the symbols.
Even a passing result means only retain for further research. Short or failed
comparisons are inconclusive. Coverage is reported separately, including empty,
constant and exactly redundant columns; coverage alone is not predictive value.

## Private outputs and reproducibility

`PrivateExperimentStore` creates a new directory beneath an explicitly supplied
existing `scratch`, `research` or `experiments` parent. It rejects path escapes,
symlink traversal, overwrites, pointer-like output names and supplied forbidden
production roots. Always pass canonical data roots as `forbidden_roots`.
It is a guardrail, not a substitute for reviewing an adapter's input selection.

Before fitting, the harness saves configuration, exact cohort and exclusion IDs,
fold train/validation/purge receipts, feature allowlists, source/input hashes and
cohort bytes. Each attempted fit gets a started/finished record. Fitted numeric
preprocessor state and logistic coefficients, probabilities, coverage and
results stay in that directory. Inputs and source bytes are rechecked afterward;
changes invalidate the experiment. Failed attempts remain visible. A changed
design requires a new frozen run and an explicit explanation, not overwriting
the old evidence.

Keep adapters containing machine paths, sample selections, raw inputs, models,
predictions, reports and detailed experiment evidence private. Only this reusable
implementation, synthetic offline tests and portable design documentation belong
in shared source publication. Production adoption requires a separate request,
reviewed semantic registration and runtime integration, additional untouched
validation and calibration design, and an explicit promotion decision.

## Offline checks

Run the two synthetic suites with the project's Python:

```text
python -B -m pytest tests/test_loop_b_research_features.py tests/test_loop_b_research.py -q -p no:cacheprovider
```

They exercise causal prefix invariance, future mutation, warm-up, sparse/native
provenance, session/DST/holiday boundaries, production semantic quarantine,
purged folds, train-only preprocessing, matched model attempts, uncertainty,
exact receipts and output containment. They do not access providers, brokers,
production data, fitted production objects or task registration.
