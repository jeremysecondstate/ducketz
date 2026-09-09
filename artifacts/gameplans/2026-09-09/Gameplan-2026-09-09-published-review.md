# September 9, 2026 Gameplan — published review

**Published and verified · Seven stocks · 168 forecasts · 168 stock-only option placeholders · Zero overnight orders**

Action day: Wednesday, September 9, **04:00–17:00 Pacific**. Published Sep 08 21:05 Pacific; enrichment completed Sep 08 21:05 Pacific. Original deadline: September 9 at 04:00 Pacific.

This review is derived from immutable publication **20260909T040455.436642Z**. Forecasts below come from its frozen table. This is the current review copy for this discussion; the earlier blocked draft remains separately preserved. Generated 2026-09-09T04:26:03.799596+00:00.

## What to focus on

- **Hourly, four-hour and weekly direction models passed promotion. The daily direction model remains research-only.** Of 133 scheduled entry windows, 126 have promoted directional models and seven daily entries do not.
- **No promoted entry has a published probability at or above the configured 55% long-entry threshold.** This is a forecast screen, not a live account or order preflight.
- **All four learned sizing models fitted, but none qualified.** Their checks are separate from directional promotion. The configured fixed-budget policy does not require learned sizing promotion; it still requires promoted directional forecasts and all existing live controls.
- Five rows per stock are non-entry context by design: one opening-gap research row and four later daily outlooks.

## Why the daily model remains research-only

The daily model completed fitting and produced varying probabilities. It passed sample-count, directional-information and calibration-error checks. On 976 unseen assessment rows across 51 decision clusters, it failed both requirements to beat a constant probability estimated from training and selection data. Lower Brier score and log loss are better.

| Daily assessment measure | Model | Simple baseline |
| --- | --- | --- |
| Brier score | 0.253475 | 0.249813 |
| Log loss | 0.700755 | 0.692773 |
| Direction accuracy | 51.74% | 54.71% |

This is the recorded reason for withholding promotion. The report does not establish why predictive performance was weaker. It is not a training crash or a missing-symbol-history result. Unchanged retraining would not establish new evidence.

## Direction model assessments

| Horizon | Status | Train / select / calibrate / assess rows | Brier: model / baseline | Log loss: model / baseline | Accuracy |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 16306 / 2588 / 3153 / 3699 | 0.230750 / 0.235597 | 0.653406 / 0.664060 | 62.10% |
| 4h | PROMOTED | 4417 / 734 / 898 / 1046 | 0.246321 / 0.247408 | 0.685767 / 0.687956 | 55.54% |
| 1d | RESEARCH_NOT_PROMOTED | 4210 / 718 / 898 / 976 | 0.253475 / 0.249813 | 0.700755 / 0.692773 | 51.74% |
| 1w | PROMOTED | 850 / 126 / 163 / 193 | 0.249592 / 0.260987 | 0.692342 / 0.715529 | 51.81% |

These held-out model results are separate from cumulative scores on previously published Gameplans. Promotion does not guarantee a profitable future trade.

## Independent sizing assessments

| Horizon | Fit status | Fitted / qualified scopes | Failed quality checks |
| --- | --- | --- | --- |
| 1h | FITTED | 91 / 0 | Return-prediction error exceeds baseline. |
| 4h | FITTED | 52 / 0 | Return-prediction error exceeds baseline. |
| 1d | FITTED | 7 / 0 | Return and adverse-return errors exceed baselines; probability output is constant. |
| 1w | FITTED | 28 / 0 | Return and adverse-return errors, and log loss, exceed baselines. |

All four report `HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED`. No configured symbol lacks admitted sizing targets in this run. Fitting a model and qualifying it for use are separate outcomes.

## Frozen windows

All times below are **America/Los_Angeles (Pacific)**. Actual start/end fields are authoritative. The four-hour 16:00 route spans the close and ends at 07:00 on the next exchange session. The weekly target ends on the fifth exchange session, Tuesday, September 15, at 17:00.

| Route | Start, Pacific | End, Pacific | Purpose |
| --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 | Sep 09 05:00 | Scheduled entry window |
| 1h@05:00 | Sep 09 05:00 | Sep 09 06:00 | Scheduled entry window |
| 1h@06:00 | Sep 09 06:00 | Sep 09 07:00 | Scheduled entry window |
| 1h@07:00 | Sep 09 07:00 | Sep 09 08:00 | Scheduled entry window |
| 1h@08:00 | Sep 09 08:00 | Sep 09 09:00 | Scheduled entry window |
| 1h@09:00 | Sep 09 09:00 | Sep 09 10:00 | Scheduled entry window |
| 1h@10:00 | Sep 09 10:00 | Sep 09 11:00 | Scheduled entry window |
| 1h@11:00 | Sep 09 11:00 | Sep 09 12:00 | Scheduled entry window |
| 1h@12:00 | Sep 09 12:00 | Sep 09 13:00 | Scheduled entry window |
| 1h@13:00 | Sep 09 13:00 | Sep 09 14:00 | Scheduled entry window |
| 1h@14:00 | Sep 09 14:00 | Sep 09 15:00 | Scheduled entry window |
| 1h@15:00 | Sep 09 15:00 | Sep 09 16:00 | Scheduled entry window |
| 1h@16:00 | Sep 09 16:00 | Sep 09 17:00 | Scheduled entry window |
| 1h@gap | Sep 08 17:00 | Sep 09 04:00 | Opening-gap research; no entry |
| 4h@04:00 | Sep 09 04:00 | Sep 09 08:00 | Scheduled entry window |
| 4h@08:00 | Sep 09 08:00 | Sep 09 12:00 | Scheduled entry window |
| 4h@12:00 | Sep 09 12:00 | Sep 09 16:00 | Scheduled entry window |
| 4h@16:00 | Sep 09 16:00 | Sep 10 07:00 | Scheduled entry window |
| 1d@D+1 | Sep 09 04:00 | Sep 09 17:00 | Scheduled entry window |
| 1d@D+2 | Sep 10 04:00 | Sep 10 17:00 | Later-session outlook; no entry |
| 1d@D+3 | Sep 11 04:00 | Sep 11 17:00 | Later-session outlook; no entry |
| 1d@D+4 | Sep 14 04:00 | Sep 14 17:00 | Later-session outlook; no entry |
| 1d@D+5 | Sep 15 04:00 | Sep 15 17:00 | Later-session outlook; no entry |
| 1w@D+5 | Sep 09 04:00 | Sep 15 17:00 | Scheduled entry window |

## Forecasts by stock

Raw and published probabilities both estimate an upward move under the saved stock target contract. Values are rounded to two decimal percentage points; source Parquet retains full precision. PROMOTED model status does not turn a non-entry context row into an entry. Each stock has exactly 24 rows.

### AAPL

| Route | Start → end, Pacific | Raw P(up) | Published P(up) | Direction | Model / purpose |
| --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | 36.10% | 36.10% | BEARISH | Promoted / entry window |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | 37.00% | 37.00% | BEARISH | Promoted / entry window |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | 40.22% | 40.22% | BEARISH | Promoted / entry window |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | 38.96% | 38.96% | BEARISH | Promoted / entry window |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | 36.68% | 36.68% | BEARISH | Promoted / entry window |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | 35.21% | 35.21% | BEARISH | Promoted / entry window |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | 32.78% | 32.78% | BEARISH | Promoted / entry window |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | 30.90% | 30.90% | BEARISH | Promoted / entry window |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | 35.13% | 35.13% | BEARISH | Promoted / entry window |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | 27.29% | 27.29% | BEARISH | Promoted / entry window |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | 26.93% | 26.93% | BEARISH | Promoted / entry window |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | 27.38% | 27.38% | BEARISH | Promoted / entry window |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | 27.87% | 27.87% | BEARISH | Promoted / entry window |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | 44.55% | 44.55% | BEARISH | Promoted / gap context |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | 41.04% | 44.88% | BEARISH | Promoted / entry window |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | 45.00% | 45.26% | NO_EDGE | Promoted / entry window |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | 36.15% | 44.39% | BEARISH | Promoted / entry window |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | 45.54% | 45.31% | NO_EDGE | Promoted / entry window |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | 61.80% | 61.80% | BULLISH | Research / entry window |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | 61.21% | 61.21% | BULLISH | Research / outlook |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | 57.37% | 57.37% | BULLISH | Research / outlook |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | 60.81% | 60.81% | BULLISH | Research / outlook |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | 63.26% | 63.26% | BULLISH | Research / outlook |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | 64.84% | 45.37% | NO_EDGE | Promoted / entry window |

### AMZN

| Route | Start → end, Pacific | Raw P(up) | Published P(up) | Direction | Model / purpose |
| --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | 38.46% | 38.46% | BEARISH | Promoted / entry window |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | 36.81% | 36.81% | BEARISH | Promoted / entry window |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | 42.22% | 42.22% | BEARISH | Promoted / entry window |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | 42.97% | 42.97% | BEARISH | Promoted / entry window |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | 42.91% | 42.91% | BEARISH | Promoted / entry window |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | 39.03% | 39.03% | BEARISH | Promoted / entry window |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | 37.94% | 37.94% | BEARISH | Promoted / entry window |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | 36.44% | 36.44% | BEARISH | Promoted / entry window |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | 41.28% | 41.28% | BEARISH | Promoted / entry window |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | 34.04% | 34.04% | BEARISH | Promoted / entry window |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | 32.77% | 32.77% | BEARISH | Promoted / entry window |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | 34.92% | 34.92% | BEARISH | Promoted / entry window |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | 35.83% | 35.83% | BEARISH | Promoted / entry window |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | 57.34% | 57.34% | BULLISH | Promoted / gap context |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | 44.65% | 45.23% | NO_EDGE | Promoted / entry window |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | 39.83% | 44.76% | BEARISH | Promoted / entry window |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | 38.23% | 44.60% | BEARISH | Promoted / entry window |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | 45.64% | 45.32% | NO_EDGE | Promoted / entry window |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | 51.27% | 51.27% | NO_EDGE | Research / entry window |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | 54.55% | 54.55% | NO_EDGE | Research / outlook |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | 50.24% | 50.24% | NO_EDGE | Research / outlook |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | 51.01% | 51.01% | NO_EDGE | Research / outlook |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | 49.92% | 49.92% | NO_EDGE | Research / outlook |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | 53.36% | 43.35% | BEARISH | Promoted / entry window |

### GOOG

| Route | Start → end, Pacific | Raw P(up) | Published P(up) | Direction | Model / purpose |
| --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | 33.13% | 33.13% | BEARISH | Promoted / entry window |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | 31.93% | 31.93% | BEARISH | Promoted / entry window |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | 36.24% | 36.24% | BEARISH | Promoted / entry window |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | 36.49% | 36.49% | BEARISH | Promoted / entry window |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | 35.46% | 35.46% | BEARISH | Promoted / entry window |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | 32.54% | 32.54% | BEARISH | Promoted / entry window |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | 32.71% | 32.71% | BEARISH | Promoted / entry window |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | 31.22% | 31.22% | BEARISH | Promoted / entry window |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | 32.92% | 32.92% | BEARISH | Promoted / entry window |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | 26.53% | 26.53% | BEARISH | Promoted / entry window |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | 22.86% | 22.86% | BEARISH | Promoted / entry window |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | 26.55% | 26.55% | BEARISH | Promoted / entry window |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | 25.96% | 25.96% | BEARISH | Promoted / entry window |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | 44.98% | 44.98% | BEARISH | Promoted / gap context |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | 43.04% | 45.07% | NO_EDGE | Promoted / entry window |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | 39.93% | 44.77% | BEARISH | Promoted / entry window |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | 43.39% | 45.11% | NO_EDGE | Promoted / entry window |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | 47.05% | 45.46% | NO_EDGE | Promoted / entry window |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | 53.86% | 53.86% | NO_EDGE | Research / entry window |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | 53.51% | 53.51% | NO_EDGE | Research / outlook |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | 54.10% | 54.10% | NO_EDGE | Research / outlook |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | 57.32% | 57.32% | BULLISH | Research / outlook |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | 55.62% | 55.62% | BULLISH | Research / outlook |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | 54.71% | 43.58% | BEARISH | Promoted / entry window |

### MU

| Route | Start → end, Pacific | Raw P(up) | Published P(up) | Direction | Model / purpose |
| --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | 50.25% | 50.25% | NO_EDGE | Promoted / entry window |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | 48.07% | 48.07% | NO_EDGE | Promoted / entry window |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | 51.51% | 51.51% | NO_EDGE | Promoted / entry window |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | 52.55% | 52.55% | NO_EDGE | Promoted / entry window |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | 50.22% | 50.22% | NO_EDGE | Promoted / entry window |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | 48.67% | 48.67% | NO_EDGE | Promoted / entry window |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | 43.71% | 43.71% | BEARISH | Promoted / entry window |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | 47.64% | 47.64% | NO_EDGE | Promoted / entry window |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | 48.23% | 48.23% | NO_EDGE | Promoted / entry window |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | 46.18% | 46.18% | NO_EDGE | Promoted / entry window |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | 44.84% | 44.84% | BEARISH | Promoted / entry window |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | 44.27% | 44.27% | BEARISH | Promoted / entry window |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | 47.24% | 47.24% | NO_EDGE | Promoted / entry window |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | 58.22% | 58.22% | BULLISH | Promoted / gap context |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | 54.65% | 46.18% | NO_EDGE | Promoted / entry window |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | 56.84% | 46.39% | NO_EDGE | Promoted / entry window |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | 52.72% | 45.99% | NO_EDGE | Promoted / entry window |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | 53.12% | 46.03% | NO_EDGE | Promoted / entry window |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | 53.66% | 53.66% | NO_EDGE | Research / entry window |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | 54.55% | 54.55% | NO_EDGE | Research / outlook |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | 53.38% | 53.38% | NO_EDGE | Research / outlook |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | 67.88% | 67.88% | BULLISH | Research / outlook |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | 64.54% | 64.54% | BULLISH | Research / outlook |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | 62.19% | 44.88% | BEARISH | Promoted / entry window |

### NVDA

| Route | Start → end, Pacific | Raw P(up) | Published P(up) | Direction | Model / purpose |
| --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | 45.97% | 45.97% | NO_EDGE | Promoted / entry window |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | 45.10% | 45.10% | NO_EDGE | Promoted / entry window |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | 49.11% | 49.11% | NO_EDGE | Promoted / entry window |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | 47.95% | 47.95% | NO_EDGE | Promoted / entry window |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | 46.89% | 46.89% | NO_EDGE | Promoted / entry window |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | 43.99% | 43.99% | BEARISH | Promoted / entry window |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | 42.01% | 42.01% | BEARISH | Promoted / entry window |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | 41.00% | 41.00% | BEARISH | Promoted / entry window |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | 47.01% | 47.01% | NO_EDGE | Promoted / entry window |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | 39.35% | 39.35% | BEARISH | Promoted / entry window |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | 37.91% | 37.91% | BEARISH | Promoted / entry window |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | 41.10% | 41.10% | BEARISH | Promoted / entry window |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | 38.77% | 38.77% | BEARISH | Promoted / entry window |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | 63.17% | 63.17% | BULLISH | Promoted / gap context |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | 45.21% | 45.28% | NO_EDGE | Promoted / entry window |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | 49.30% | 45.67% | NO_EDGE | Promoted / entry window |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | 44.11% | 45.17% | NO_EDGE | Promoted / entry window |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | 48.32% | 45.58% | NO_EDGE | Promoted / entry window |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | 45.11% | 45.11% | NO_EDGE | Research / entry window |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | 40.29% | 40.29% | BEARISH | Research / outlook |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | 39.01% | 39.01% | BEARISH | Research / outlook |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | 59.30% | 59.30% | BULLISH | Research / outlook |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | 56.08% | 56.08% | BULLISH | Research / outlook |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | 54.04% | 43.46% | BEARISH | Promoted / entry window |

### SNDK

| Route | Start → end, Pacific | Raw P(up) | Published P(up) | Direction | Model / purpose |
| --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | 52.87% | 52.87% | NO_EDGE | Promoted / entry window |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | 49.40% | 49.40% | NO_EDGE | Promoted / entry window |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | 53.68% | 53.68% | NO_EDGE | Promoted / entry window |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | 54.71% | 54.71% | NO_EDGE | Promoted / entry window |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | 52.16% | 52.16% | NO_EDGE | Promoted / entry window |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | 51.63% | 51.63% | NO_EDGE | Promoted / entry window |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | 46.78% | 46.78% | NO_EDGE | Promoted / entry window |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | 51.41% | 51.41% | NO_EDGE | Promoted / entry window |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | 52.50% | 52.50% | NO_EDGE | Promoted / entry window |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | 52.00% | 52.00% | NO_EDGE | Promoted / entry window |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | 47.56% | 47.56% | NO_EDGE | Promoted / entry window |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | 48.82% | 48.82% | NO_EDGE | Promoted / entry window |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | 51.61% | 51.61% | NO_EDGE | Promoted / entry window |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | 54.77% | 54.77% | NO_EDGE | Promoted / gap context |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | 52.59% | 45.98% | NO_EDGE | Promoted / entry window |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | 48.82% | 45.62% | NO_EDGE | Promoted / entry window |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | 50.70% | 45.80% | NO_EDGE | Promoted / entry window |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | 49.52% | 45.69% | NO_EDGE | Promoted / entry window |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | 47.92% | 47.92% | NO_EDGE | Research / entry window |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | 47.30% | 47.30% | NO_EDGE | Research / outlook |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | 49.25% | 49.25% | NO_EDGE | Research / outlook |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | 59.38% | 59.38% | BULLISH | Research / outlook |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | 52.39% | 52.39% | NO_EDGE | Research / outlook |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | 74.40% | 47.31% | NO_EDGE | Promoted / entry window |

### COST

| Route | Start → end, Pacific | Raw P(up) | Published P(up) | Direction | Model / purpose |
| --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | 32.96% | 32.96% | BEARISH | Promoted / entry window |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | 28.77% | 28.77% | BEARISH | Promoted / entry window |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | 33.35% | 33.35% | BEARISH | Promoted / entry window |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | 39.58% | 39.58% | BEARISH | Promoted / entry window |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | 30.52% | 30.52% | BEARISH | Promoted / entry window |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | 32.45% | 32.45% | BEARISH | Promoted / entry window |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | 29.68% | 29.68% | BEARISH | Promoted / entry window |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | 28.36% | 28.36% | BEARISH | Promoted / entry window |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | 27.74% | 27.74% | BEARISH | Promoted / entry window |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | 24.27% | 24.27% | BEARISH | Promoted / entry window |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | 21.69% | 21.69% | BEARISH | Promoted / entry window |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | 21.56% | 21.56% | BEARISH | Promoted / entry window |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | 22.21% | 22.21% | BEARISH | Promoted / entry window |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | 28.02% | 28.02% | BEARISH | Promoted / gap context |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | 38.76% | 44.65% | BEARISH | Promoted / entry window |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | 40.65% | 44.84% | BEARISH | Promoted / entry window |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | 31.76% | 43.93% | BEARISH | Promoted / entry window |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | 47.37% | 45.49% | NO_EDGE | Promoted / entry window |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | 21.40% | 21.40% | BEARISH | Research / entry window |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | 31.87% | 31.87% | BEARISH | Research / outlook |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | 18.68% | 18.68% | BEARISH | Research / outlook |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | 38.42% | 38.42% | BEARISH | Research / outlook |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | 20.67% | 20.67% | BEARISH | Research / outlook |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | 47.65% | 42.38% | BEARISH | Promoted / entry window |

## Prior Gameplan evaluation

The pre-publication evaluation covers **1,296 forecasts from prior saved editions**: **823 evaluated**, **174 mature awaiting valid price data**, and **299 not yet mature**. September 9's 168 forecasts were published afterward and are not included in those 1,296 rows. Repeated editions remain distinct records.

Observed direction accuracy: **64.03%**; mean Brier score: **0.227011**. These figures include prior promoted and research forecasts. They are not trading returns or September 9 model accuracy.

| Prior action date | Forecasts | Evaluated | Mature awaiting data | Pending maturity |
| --- | --- | --- | --- | --- |
| 2026-09-04 | 144 | 106 | 14 | 24 |
| 2026-09-08 | 1152 | 717 | 160 | 275 |

The exact five-minute price-boundary tolerance remains enforced. Missing valid outcomes await data; no substitute dataset is used.

## Data, stages and verification

- The resumed workflow retained completed September 8 Loop A fetch and Directional generation. Target history, cumulative evaluation, Gameplan publication and independent sizing completed under the original deadline.
- Target history log records a new XNAS.ITCH OHLCV1m partition for each of seven stocks. The publication identifies `xnas-itch-archive-v1` throughout.
- The publication records all 21 production OPRA symbol/schema cursors current through September 8, using exclusive completed-through September 9.
- All 168 options intents are `NO_TRADE_STOCK_ONLY`, with no option legs, candidate or Strategy source authority. Options Strategy training/generation were intentionally omitted.
- Read-only checks verified the current pointer, publication manifest/receipt and output hashes, per-symbol counts, matching option IDs, exact-route fitted history, original deadline, terminal stage/log hashes, and enrichment report plus its bound cohort hashes.
- Overnight and enrichment evidence records zero orders and disabled broker order authority. No pipeline, forecast, model, trading control or broker state was changed for this review.

The earlier XNAS release blocker is resolved in this completed run. Missing historical outcome boundaries and model-quality limitations remain visible above. This review does not claim a fresh download or independent re-audit of every raw provider partition.

## Source evidence

- [Frozen Gameplan](C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/gameplan.json)
- [Published forecasts](C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/forecasts.parquet)
- [Direction assessments](C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/model-reports.json)
- [Publication receipt](C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/receipt.json)
- [Sizing assessment](C:/DATASTORE/ml/stock-trader-model-runs/20260909T040540.539128Z/training-report.json)
- [Overnight receipt](C:/DATASTORE/ml/overnight-runs/20260909T040352.061858Z/receipt.json)
- [Target history log](C:/DATASTORE/ml/overnight-runs/20260909T040352.061858Z/stock_target_history.log)
- [Cumulative evaluation](C:/DATASTORE/ml/gameplan-evaluation-runs/20260909T040453.621818Z/summary.json)
