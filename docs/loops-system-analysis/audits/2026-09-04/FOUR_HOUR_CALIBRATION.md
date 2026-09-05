# Four-hour forecasts showing 50%

The September 4 Rolling Forecasts display faithfully showed the frozen Gameplan.
All 24 four-hour probabilities were **exactly 0.5**. The values were not a display
default or a rounding effect. Their underlying raw model probabilities varied
from approximately 0.3002 to 0.6500.

## Cause

The fitted four-hour predictor did not retain a supported positive ranking on
its separate calibration partition. Reconstructing that partition from the saved
source features and historical market bars reproduced all four partition sizes:
979 training, 220 selection, 208 calibration, and 184 assessment rows.

Unconstrained Platt calibration fitted a negative slope (-0.1587537744). The
existing nondecreasing policy correctly prevented calibration from reversing the
meaning of the model's upward probability. It used a zero-slope base-rate fallback.
The calibration partition had exactly 50% positive outcomes, so every calibrated
prediction became 50%.

The first confirmed defect was in promotion: the four existing checks allowed this constant
fallback to pass. Assessment Brier score was 0.25 versus a training-base-rate
baseline of 0.2453087; the allowed 0.01 tolerance accepted it. Log loss and the
calibration-error checks also passed. None checked that calibrated output retained
any variation.

## Correction

- Future promotion requires both outcome classes and varying calibrated output
  on the calibration and assessment partitions. Flat or unsupported output
  remains `RESEARCH_NOT_PROMOTED`; calibration mathematics is unchanged.
- Each future model report records diagnostic status, constrained slope, base
  rate, and probability ranges. A `FIT_WARNING` reaches overnight supervision
  immediately after calibration. The operating contract treats it as a quality
  result to inspect and report, not a reason to repeat an unchanged training run.
- Rolling Forecasts detects the flat calibration in both the first immutable
  Gameplan and future publications. Per the user's display preference, it uses
  valid saved raw scores for those groups. The grid contains only percentages;
  the complete raw/calibrated values and display source are in Debug Details.

The first Gameplan and its original promotion status remain saved unchanged for
later evaluation. This correction does not manufacture a better four-hour model;
the next overnight fit must independently earn promotion. This audit does not
establish the predictive quality of the other horizons.

## Verification

- 158 focused tests passed, covering forecast display, calibration/promotion,
  Gameplan evaluation and consumers, training progress, and supervision. Four
  existing joblib/NumPy deprecation warnings were reported.
- The real saved four-hour model fails the new information check. Its original
  50% calibrated outputs remain preserved in the frozen Gameplan.
- A 1900×1050 offline UI capture using the real Gameplan was visually checked:
  `artifacts/validation/rolling-forecasts-flat-calibration.png`.
- Python compilation and `git diff --check` passed. No broker commands ran.

Evidence: [diagnostic output](four-hour-calibration-evidence.json),
[reproduction script](diagnose_four_hour_calibration.py), and
[test output](four-hour-calibration-tests.txt).

Reopen the running Duckets app to load the updated UI code. Its Refresh button
reloads saved data, not Python modules. Future overnight Python processes load
the new promotion guard automatically.

## Follow-up: which saved four-hour window is displayed?

The September 4 evening display already selected `4h@16:00`: the forecast for
**Friday, September 4, noon through 4 p.m. PDT**. The route suffix denotes the
window's end. Read-only checks at Friday evening, Saturday, and Sunday all chose
that same last completed window from the September 4 plan.

The original scores were different; the shared calibration made them identical:

| Symbol | Saved raw score (uncalibrated) | Saved calibrated probability |
|---|---:|---:|
| AAPL | 43.9% | 50.0% |
| AMZN | 43.1% | 50.0% |
| GOOG | 30.0% | 50.0% |
| MU | 47.3% | 50.0% |
| NVDA | 48.6% | 50.0% |
| SNDK | 55.7% | 50.0% |

The final display uses each raw score above as the cell's single percentage,
with the usual red/green/neutral color. It removes the extra fallback text,
secondary percentages, calibration banner, and the taller four-hour heading.
The saved window remains available in expanded cards and Debug Details. Those
cards use the same displayed probabilities as the grid.

This is a display choice: raw scores do not overwrite published calibrated
values, change trading inputs, bypass model promotion, or replace evaluation
inputs. No forecasts were regenerated. A single 50% forecast in an otherwise
varying group continues to use its published value. Missing, non-finite, or
out-of-range raw scores cannot replace a valid published value.

Raw scores preserve the model's distinctions but may overstate or understate
outcome frequencies. Calibration aims to align those two, and must be assessed
on separate data. A flat constrained fit is not itself an arithmetic error or
proof that raw scores are better. See the [scikit-learn calibration
documentation](https://scikit-learn.org/stable/modules/calibration.html).

Final display verification: 120 UI tests passed, covering the retained last
window, display source, complements, colors, normal calibration, and invalid raw
scores. The real Gameplan's clean grid was visually checked at 1900×1050 in
`artifacts/validation/rolling-forecasts-clean-raw-scores.png`. Python compilation
and the diff whitespace check passed.

## Deeper diagnosis: distinct inputs were erased, and some labels were wrong

The user's concern was correct: the calibration erased distinctions among six
different raw scores. This was an explicit constant mapping. A fitted slope of
-0.1587537744 became zero under the orientation constraint, and the calibration
cohort's 104 positive outcomes out of 208 supplied its 50% intercept. Explaining
that mathematical behavior did not diagnose why the underlying fit failed.

The subsequent audit reconstructed the saved model, class orientation, source
features, raw price returns, target labels, and four chronological partitions.
It found no duplicate natural keys, conflicting duplicate price timestamps,
class inversion, or arithmetic mismatch in the reconstructed returns. However,
the **price observations did not necessarily match the stated target windows**:

| Daytime four-hour window, PT | Candidate labels | Boundary more than five minutes away |
|---|---:|---:|
| 04:00–08:00 | 400 | 135 |
| 08:00–12:00 | 400 | 0 |
| 12:00–16:00 | 400 | 274 |
| Total | 1,200 | 409 |

The builder used the first and last available bar anywhere inside the window.
For AAPL on August 18, its noon–4 p.m. target used a last bar at 12:59 p.m., so
the observed closing price ended at 1 p.m.—three hours before the stated endpoint.
The same missing coverage exists in the locally saved EQUS.MINI raw provider
records. This audit does not establish whether every gap was caused by sparse
venue trades, an incomplete request, or an upstream data issue. It does establish
that the label builder silently accepted stale observations as fixed-window data.

Opening-gap targets had the same boundary problem: 261 of 395 candidates missed
the prior 17:00 close or current 04:00 open by more than five minutes. Overall,
670 of 1,595 candidate four-hour-group labels fail the new boundary policy.

### Implemented repair

- Target contract v2 records the actual opening/closing observation timestamps
  and their distance from the intended boundaries. Both must be within five
  minutes, and prices must be positive. This is an explicit data-quality policy,
  not a threshold selected to optimize the assessment results.
- Misaligned returns are excluded from intraday fitting and new evaluation.
  No prices are forward-filled across a long gap. The shared implementation
  also corrects the same defect in one-hour labels and opening-gap labels.
- Cumulative evaluation retains a mature forecast awaiting valid data and
  retries it later. Previously saved forecasts and completed evaluations are
  preserved. Historical reconstruction scripts have an explicit audit-only
  opt-out; active training and evaluation enforce the policy.
- Training reports include exclusion counts by route and bounded examples,
  raw/calibrated proper scores, and calibration's change in those scores.
  Exclusions emit `FIT_WARNING` before fitting; failed assessment reports its
  evidence immediately for overnight supervision.
- Promotion now requires beating the training-base-rate baseline on both Brier
  score and log loss. The old allowances could promote a varying model that
  performed worse than this simple baseline. No trading gate was relaxed.

### Controlled refit and its limits

The replay kept the original chronological boundaries, feature list, model
families, random seeds, and hyperparameters fixed. It filtered invalid labels
inside each existing cohort rather than moving dates to get a better result.
Retained rows: 571 train, 122 model selection, 131 calibration, 100 assessment.
The final predictor was refit on train plus selection, as in production.

The corrected calibration has positive slope **0.0861464226**, and the replayed
September 4 outputs range from **49.6644% to 51.8398%**. It no longer maps every
score to 50%. These are experimental outputs; they were not published over the
first Gameplan.

The later assessment still does not establish a useful model. Lower scores are
better in both columns below; every row uses the same 100 valid assessment labels:

| Predictor | Brier score | Log loss |
|---|---:|---:|
| Original saved predictor, raw | 0.241094 | 0.674212 |
| Original saved predictor, calibrated | 0.250000 | 0.693147 |
| Refit predictor, raw | 0.248619 | 0.688780 |
| Refit predictor, calibrated | 0.252003 | 0.697156 |
| Constant from refit training plus selection | 0.243995 | 0.681117 |

The refit's calibration is worse than its raw scores on this assessment, and
both are worse than its training-base-rate baseline. The repaired promotion
gate therefore correctly leaves this experiment `RESEARCH_NOT_PROMOTED`.
This is a confirmed repair of mislabeled training data and diagnostic reporting,
**not a claim that the four-hour model now has demonstrated predictive skill**.
The model sees only a small number of independent sessions, so forcing varied
output or selecting a winner after viewing assessment results is not a valid fix.

On the original full 184-row assessment, raw Brier score was 0.251767 versus
0.250000 for the flat map and 0.245309 for the training-base-rate baseline.
Unconstrained reversed Platt and isotonic calibration also failed to beat that
baseline. Thus removing the orientation constraint alone is not supported by
the recorded experiment. Training performance was much stronger than later
calibration/assessment performance; scores from the final estimator on its
selection rows are in-sample because that estimator was refit on those rows.

Reproduction:

- [Original model and label investigation](investigate_four_hour_model.py)
- [Controlled boundary repair replay](refit_boundary_aligned_four_hour.py)
- [Original model diagnostic results](../../../../artifacts/validation/four-hour-root-cause/diagnosis.json)
- [Final controlled comparison](../../../../artifacts/validation/four-hour-root-cause/boundary-corrected-v2/comparison.json)

Both scripts write research artifacts only under the workspace. The final replay
verifies that the first Gameplan's checksummed outputs remain unchanged. The
Rolling Forecasts grid continues to show the saved raw values as requested,
with detailed provenance in Debug Details.

Final deeper-diagnosis verification: **182 tests passed**, including interval
boundary failures, valid sparse windows, opening-gap coverage, retained pending
evaluation, raw/calibrated scoring, promotion, supervision, trader consumption,
and UI display. Four existing joblib/NumPy deprecation warnings remain. Both
reproduction scripts completed, Python compilation passed, and `git diff --check`
passed. See [regression output](../../../../artifacts/validation/four-hour-root-cause/regression-tests.txt).
