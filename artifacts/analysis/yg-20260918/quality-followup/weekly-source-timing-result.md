# Fixed weekly source-timing development comparison

Preregistered 2026-09-18T07:54:29.927902+00:00; completed 2026-09-18T07:54:33.169122+00:00.

Train 1,561 rows; selection 174 rows. Calibration and final assessment were not loaded or scored. Original chronology, model hyperparameters, admitted features and target labels are retained; exactly two source timing features were added. No shrinkage combination was tested.

Best source-timing candidate: **regularized-logistic-c0.001**, development log loss **0.709806570467**; original winner hist-gradient-mlp-0.50, **0.703697924502**. Change **+0.006108645965**.

| Candidate | Selection log loss | Brier | Probability min–max |
|---|---:|---:|---|
| hist-gradient | 0.726658418971 | 0.262799147249 | 0.162062–0.908678 |
| mlp | 0.719689496886 | 0.262051089984 | 0.360607–0.804345 |
| hist-gradient-mlp-0.25 | 0.715090033789 | 0.258933778355 | 0.259418–0.840685 |
| hist-gradient-mlp-0.50 | 0.710609552601 | 0.257520645847 | 0.356774–0.785089 |
| hist-gradient-mlp-0.75 | 0.712167704504 | 0.258559749723 | 0.373873–0.769595 |
| regularized-logistic-c0.001 | 0.709806570467 | 0.258169947397 | 0.525580–0.644379 |
| regularized-logistic-c0.01 | 0.714347096052 | 0.260120257428 | 0.358186–0.728345 |
| regularized-logistic-c0.1 | 0.736338807609 | 0.269388631886 | 0.086117–0.830773 |
| regularized-logistic-c1 | 0.773601250273 | 0.275800068743 | 0.000986–0.886855 |

One independent fixed development experiment. Any selection improvement is not evidence of final-assessment success. No post-hoc source/shrinkage combination or candidate expansion was performed.

At the frozen current source, every configured symbol has source__hours_after_regular_close=4 and source__hours_until_action_start=11. The same formula uses saved historical/current timestamps and requires no fetch or prices. All feature availability evidence and prediction distributions are in the JSON; selection predictions are retained in the Parquet.
