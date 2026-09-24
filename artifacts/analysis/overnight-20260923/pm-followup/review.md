# September 23 PM review: model quality and overnight readiness

The previous overnight run completed all eight stages, with 264 forecasts, 264
stock-only intents and 154 planning price points. Its weekly quality failure is
real, but it is not an interrupted training process or incomplete publication.
The active September 23 manual worker was RUNNING at 13:39:42 Pacific with zero
consecutive failures. No trader, control, order, ledger or frozen publication was
changed during this review.

## Probability quality

The saved v2 policy permits Brier score up to the training-rate baseline +0.005
and log loss up to baseline +0.01. Both are probability error scores; lower is
better. This policy does not establish baseline outperformance or profitability.

| Weekly score | Baseline | Maximum allowed | Observed |
| --- | ---: | ---: | ---: |
| Brier | 0.252768096 | 0.257768096 | 0.260511989 |
| Log loss | 0.698791351 | 0.708791351 | 0.714423656 |

Independent inference reproduced the scores. All prior 2,282 cohort rows were
unchanged, with eight valid mature targets added. Fixed development selection,
source timestamps, chronological partitions and calibration verified. The
calibrated probabilities average 41.85% on assessment, whose observed up rate is
52.34%; raw probabilities also fail the quality limits. There is no demonstrated
implementation defect to repair by retrying or replacing calibration after
seeing assessment results. Hourly, four-hour and daily groups pass their saved
tolerances; none of the four groups strictly beats both corresponding baselines.

The selected manual Gameplan reader consumes saved execution instructions without
a separate model-promotion veto. A failed weekly quality check does not therefore
automatically prohibit that policy's weekly instructions. Legacy readers retain
their separate requirements. Documentation was corrected to match the existing
implementation and distinguish strict v1 from tolerant v2.

## Price observations and ownership

The 33 mature missing September 22 outcomes all have complete verified source
request coverage. Independent native DBN readback for the five affected symbols
reproduced normalized prices and all 66 endpoint checks without differences.
There is no identified acquisition, normalization or selector defect. The 66
pending outcomes had future target ends at the review cutoff. Re-downloading
unchanged XNAS.ITCH data cannot create missing venue observations.

Synthetic closing anchors in planning are disclosed estimates only. They are not
used to invent training labels or completed-session actuals. Broader prospective
source coverage is a separate potential improvement: the prior seven-symbol
XNAS.BASIC study improved COST coverage, but has not validated the four newly
onboarded symbols or authorized production adoption.

The prior TWST canceled reservation was already reconciled from broker evidence.
The recurring process gap is that PM/DAY cancellations can arrive seconds after
the 17:00 worker exit. The operating procedure now explicitly requires a
supervised post-close ownership check and the existing native reconciliation path
when necessary, only with the trader absent, exact terminal evidence, a newer
matching-account snapshot, native locks, backup and zero execution budgets. This
is an operator-procedure improvement; no new automatic runtime reconciliation
stage has been implemented. Today's single historical failed cycle was a capture
crossing the 06:25 session boundary; the next executable cycle recovered.

## Time available for history experiments

Measured native stage time was 102.54 minutes: fetch 70.16, upstream Directional
24.05, XNAS history 1.51, evaluation 1.24, Gameplan publication 2.01, enrichment
0.81, trade planning 2.42 and actuals 0.34. The 21:05 to 03:30 planning target
provides 385 minutes; the native hard deadline remains 04:00. One night's timing
does not establish a worst-case runtime. A 03:30 target leaves recovery margin.

The Gameplan fitter has no 120-session history cap; it consumes the eligible
source/target intersection, with separate chronological train, selection,
calibration and assessment partitions and purged overlapping target windows.
The 120-session setting belongs to conditional planning prices. More training
iterations alone do not establish better forecasts.

Longer-history comparisons should preserve the recent development windows and
source/feature semantics, compare a small preregistered set of available training
spans on several purged time-ordered development folds, and measure probability
quality and runtime by horizon. Keep final assessment out of selection. Previously
inspected assessment data cannot become a new independent confirmation; require
subsequent unseen evidence for improvement claims. No fit, history download,
training-policy change or new production publication was run during this review.

### Current history utilization

| Horizon | Admitted rows | Actual train-plus-selection fit | Calibration | Assessment |
| --- | ---: | ---: | ---: | ---: |
| 1h | 65,203 | 56,544 | 3,927 | 4,598 |
| 4h | 16,693 | 14,303 | 1,074 | 1,262 |
| 1d | 11,465 | 8,855 | 1,085 | 1,261 |
| 1w | 2,290 | 1,740 | 203 | 256 |

The remaining 134/54/264/91 rows respectively are purged overlaps. These are
pooled symbol/route rows, not independent sessions. Current source features begin
in April 2023 for ten stocks and March 2025 for SNDK. AAPL, AMZN, COST, GOOG, MU
and NVDA have selected XNAS minute targets only from January 13, 2025; SNDK begins
February 24, 2025. CROX/TWST/PATH/IONQ minute archives begin in 2018/2018/2021/2021,
and their hourly/four-hour cohorts already reach April 2023. Their earlier raw
prices do not yet have the required causal feature rows. TWST retains only 15
daily and eight weekly admitted rows despite its deep raw archive.

The six older stocks each have 252 selected causal feature dates in 2024, or
1,512 feature-sessions, awaiting compatible minute labels. Across April 2023 to
January 12, 2025 there are 2,666 candidate feature-sessions. These are not yet
admitted targets. The concrete next research comparison is current history
versus adding 2024, then the full available feature overlap if supported. First
refresh exact same-source $0/capacity preflights, acquire missing partitions once,
verify native receipts and genuine boundaries, and build an isolated candidate
cohort. Reuse the history incrementally thereafter. No acquisition was performed
or production expansion assumed from a previous cost quote.

## Auxiliary raw futures configuration repair

Saved verified definitions establish that configured ESU6/NQU6 expired on
September 18. The current continuous archive resolves ES/NQ/CL to instrument IDs
10252/261401/301334. One authorized read-only Databento symbology lookup bound
those IDs to ESZ6/NQZ6/CLX6 for September 22–23. The raw contract setting now uses
ESZ6, NQZ6, CLX6 and unchanged GCZ6. All other `.env` bytes were preserved; no
running process was restarted. Native request construction for all three raw
schemas verifies offline, retaining the five continuous roots and raw symbology.
Tonight must still pass its normal acquisition coverage and cost/capacity checks.

This fixes obsolete auxiliary fetch inputs, not weekly model performance. All
four current directional reports admit zero CME/Pricing features. The continuous
context already existed and historical evidence remains unchanged. Exact CLV6
expiry was not independently established; its replacement follows the verified
current instrument mapping rather than an inferred expiry date. See
`cme-current-instrument-symbology.json`, `cme-roll-readiness.json`,
`cme-config-repair.json` and `cme-config-validation.json`.

## Evidence

- `../weekly-diagnosis.md` and `../weekly-diagnosis.json`
- `../completion-summary.md`, `../final-verification.json`
- `../broker-order-status-readonly.json`, `../native-reconciliation.json`
- `C:/DATASTORE/ml/stock-trader-decision-runs/20260923T132500.365344Z/receipt.json`
- `C:/DATASTORE/ml/stock-trader-decision-runs/20260923T133005.097957Z/receipt.json`
- `C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/history-2024-feasibility.md`
- `C:/dev/ducketz/ml/gameplan_promotion.py`
- `C:/dev/ducketz/ml/stock_trader/gameplan_execution.py`
