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

The defect was in promotion: the four existing checks allowed this constant
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
- Rolling Forecasts marks the affected cells and cards **No model signal**. It
  detects this condition in the existing immutable first Gameplan as well as
  future publications, and preserves the actual saved percentages.

The first Gameplan and its original promotion status remain saved unchanged for
later evaluation. This correction does not manufacture a better four-hour model;
the next overnight fit must independently earn promotion. This audit does not
establish the predictive quality of the other horizons.

## Verification

- 158 focused tests passed, covering forecast display, calibration/promotion,
  Gameplan evaluation and consumers, training progress, and supervision. Four
  existing joblib/NumPy deprecation warnings were reported.
- The real saved four-hour model fails the new information check. All six UI
  symbols receive the warning with 50% probabilities preserved.
- A 1900×1050 offline UI capture using the real Gameplan was visually checked:
  `artifacts/validation/rolling-forecasts-flat-calibration.png`.
- Python compilation and `git diff --check` passed. No broker commands ran.

Evidence: [diagnostic output](four-hour-calibration-evidence.json),
[reproduction script](diagnose_four_hour_calibration.py), and
[test output](four-hour-calibration-tests.txt).

Reopen the running Duckets app to load the updated UI code. Its Refresh button
reloads saved data, not Python modules. Future overnight Python processes load
the new promotion guard automatically.
