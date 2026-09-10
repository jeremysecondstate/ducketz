# Gameplan results · 2026-09-09

**Preview — final results await the completed-session data fetch.** All times are Pacific.

The saved price estimates below are compared with actual market prices at the same clock. The ranges were planning estimates; the observations are market prices, not broker fills or trading P/L.

Original forecast: [2026-09-09 Gameplan](C:/DATASTORE/ml/nightly-gameplan-runs/20260909T060404.450224Z/forecasts.parquet).

[Original Gameplan with prices and quantities](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260909T071232.777557Z/Gameplan.md).

**0 evaluated · 56 still pending · 112 waiting for price data.**

Direction results compare the saved Bullish/Bearish call with the actual price move. Neutral forecasts have no directional score. Future and missing outcomes are excluded from accuracy.

| Stock | Correct / scored approved calls | Direction accuracy | Mean absolute price error | Prices within range |
| --- | --- | --- | --- | --- |
| AAPL | — | — | — | — |
| AMZN | — | — | — | — |
| COST | — | — | — | — |
| GOOG | — | — | — | — |
| MU | — | — | — | — |
| NVDA | — | — | — | — |
| SNDK | — | — | — | — |

## AAPL

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $315.61–$316.89 | $316.25 | — | — | — | — | Waiting for data |
| 05:00 | $315.50–$316.78 | $316.14 | — | — | — | — | Waiting for data |
| 06:00 | $315.50–$316.78 | $316.14 | — | — | — | — | Waiting for data |
| 07:00 | $316.13–$317.40 | $316.77 | — | — | — | — | Waiting for data |
| 08:00 | $316.34–$317.61 | $316.98 | — | — | — | — | Waiting for data |
| 09:00 | $316.37–$317.64 | $317.01 | — | — | — | — | Waiting for data |
| 10:00 | $316.76–$318.04 | $317.40 | — | — | — | — | Waiting for data |
| 11:00 | $316.80–$318.08 | $317.44 | — | — | — | — | Waiting for data |
| 12:00 | $316.24–$317.52 | $316.88 | — | — | — | — | Waiting for data |
| 13:00 | $316.19–$317.47 | $316.83 | — | — | — | — | Waiting for data |
| 14:00 | $315.96–$317.24 | $316.60 | — | — | — | — | Waiting for data |
| 15:00 | $316.20–$317.47 | $316.83 | — | — | — | — | Waiting for data |
| 16:00 | $316.18–$317.46 | $316.82 | — | — | — | — | Waiting for data |
| 17:00 | $316.25–$317.53 | $316.89 | — | — | — | — | Pending |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | BEARISH | 36.10% | Approved | — | — | — | Waiting for data |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | BEARISH | 37.00% | Approved | — | — | — | Waiting for data |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | BEARISH | 40.22% | Approved | — | — | — | Waiting for data |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | BEARISH | 38.96% | Approved | — | — | — | Waiting for data |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | BEARISH | 36.68% | Approved | — | — | — | Waiting for data |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | BEARISH | 35.21% | Approved | — | — | — | Waiting for data |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | BEARISH | 32.78% | Approved | — | — | — | Waiting for data |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | BEARISH | 30.90% | Approved | — | — | — | Waiting for data |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | BEARISH | 35.13% | Approved | — | — | — | Waiting for data |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | BEARISH | 27.29% | Approved | — | — | — | Waiting for data |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | BEARISH | 26.93% | Approved | — | — | — | Waiting for data |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | BEARISH | 27.38% | Approved | — | — | — | Waiting for data |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | BEARISH | 27.87% | Approved | — | — | — | Pending target end |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | BEARISH | 44.55% | Approved | $316.34 | — | — | Waiting for data |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | BEARISH | 44.88% | Approved | — | — | — | Waiting for data |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | BEARISH | 45.26% | Approved | — | — | — | Waiting for data |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | BEARISH | 44.39% | Approved | — | — | — | Waiting for data |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | BEARISH | 45.31% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | NO_EDGE | 52.57% | Approved | — | — | — | Pending target end |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | NO_EDGE | 50.78% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 50.23% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | BULLISH | 55.04% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | BULLISH | 54.49% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | BEARISH | 45.37% | Approved | — | — | — | Pending target end |

## AMZN

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $256.41–$257.45 | $256.93 | — | — | — | — | Waiting for data |
| 05:00 | $256.35–$257.39 | $256.87 | — | — | — | — | Waiting for data |
| 06:00 | $256.36–$257.39 | $256.87 | — | — | — | — | Waiting for data |
| 07:00 | $255.46–$256.50 | $255.98 | — | — | — | — | Waiting for data |
| 08:00 | $255.74–$256.78 | $256.26 | — | — | — | — | Waiting for data |
| 09:00 | $255.81–$256.84 | $256.33 | — | — | — | — | Waiting for data |
| 10:00 | $255.66–$256.69 | $256.18 | — | — | — | — | Waiting for data |
| 11:00 | $255.65–$256.69 | $256.17 | — | — | — | — | Waiting for data |
| 12:00 | $255.23–$256.26 | $255.75 | — | — | — | — | Waiting for data |
| 13:00 | $255.58–$256.61 | $256.10 | — | — | — | — | Waiting for data |
| 14:00 | $255.36–$256.40 | $255.88 | — | — | — | — | Waiting for data |
| 15:00 | $255.61–$256.65 | $256.13 | — | — | — | — | Waiting for data |
| 16:00 | $255.56–$256.60 | $256.08 | — | — | — | — | Waiting for data |
| 17:00 | $255.64–$256.68 | $256.16 | — | — | — | — | Pending |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | BEARISH | 38.46% | Approved | — | — | — | Waiting for data |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | BEARISH | 36.81% | Approved | — | — | — | Waiting for data |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | BEARISH | 42.22% | Approved | — | — | — | Waiting for data |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | BEARISH | 42.97% | Approved | — | — | — | Waiting for data |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | BEARISH | 42.91% | Approved | — | — | — | Waiting for data |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | BEARISH | 39.03% | Approved | — | — | — | Waiting for data |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | BEARISH | 37.94% | Approved | — | — | — | Waiting for data |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | BEARISH | 36.44% | Approved | — | — | — | Waiting for data |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | BEARISH | 41.28% | Approved | — | — | — | Waiting for data |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | BEARISH | 34.04% | Approved | — | — | — | Waiting for data |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | BEARISH | 32.77% | Approved | — | — | — | Waiting for data |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | BEARISH | 34.92% | Approved | — | — | — | Waiting for data |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | BEARISH | 35.83% | Approved | — | — | — | Pending target end |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | BULLISH | 57.34% | Approved | $256.61 | — | — | Waiting for data |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | BEARISH | 45.23% | Approved | — | — | — | Waiting for data |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | BEARISH | 44.76% | Approved | — | — | — | Waiting for data |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | BEARISH | 44.60% | Approved | — | — | — | Waiting for data |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | BEARISH | 45.32% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | NO_EDGE | 52.03% | Approved | — | — | — | Pending target end |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | NO_EDGE | 50.24% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 49.69% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | BULLISH | 54.51% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 53.95% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | BEARISH | 43.35% | Approved | — | — | — | Pending target end |

## COST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $907.49–$911.13 | $909.31 | — | — | — | — | Waiting for data |
| 05:00 | $906.77–$910.42 | $908.60 | — | — | — | — | Waiting for data |
| 06:00 | $907.04–$910.68 | $908.86 | — | — | — | — | Waiting for data |
| 07:00 | $907.02–$910.67 | $908.85 | — | — | — | — | Waiting for data |
| 08:00 | $907.65–$911.29 | $909.47 | — | — | — | — | Waiting for data |
| 09:00 | $907.01–$910.65 | $908.83 | — | — | — | — | Waiting for data |
| 10:00 | $907.84–$911.49 | $909.66 | — | — | — | — | Waiting for data |
| 11:00 | $905.96–$909.60 | $907.78 | — | — | — | — | Waiting for data |
| 12:00 | $906.41–$910.06 | $908.24 | — | — | — | — | Waiting for data |
| 13:00 | $905.83–$909.47 | $907.65 | — | — | — | — | Waiting for data |
| 14:00 | $903.78–$907.42 | $905.60 | — | — | — | — | Waiting for data |
| 15:00 | $902.91–$906.54 | $904.72 | — | — | — | — | Waiting for data |
| 16:00 | $904.98–$908.62 | $906.80 | — | — | — | — | Waiting for data |
| 17:00 | $903.54–$907.17 | $905.36 | — | — | — | — | Pending |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | BEARISH | 32.96% | Approved | — | — | — | Waiting for data |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | BEARISH | 28.77% | Approved | — | — | — | Waiting for data |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | BEARISH | 33.35% | Approved | — | — | — | Waiting for data |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | BEARISH | 39.58% | Approved | — | — | — | Waiting for data |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | BEARISH | 30.52% | Approved | — | — | — | Waiting for data |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | BEARISH | 32.45% | Approved | — | — | — | Waiting for data |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | BEARISH | 29.68% | Approved | — | — | — | Waiting for data |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | BEARISH | 28.36% | Approved | — | — | — | Waiting for data |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | BEARISH | 27.74% | Approved | — | — | — | Waiting for data |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | BEARISH | 24.27% | Approved | — | — | — | Waiting for data |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | BEARISH | 21.69% | Approved | — | — | — | Waiting for data |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | BEARISH | 21.56% | Approved | — | — | — | Waiting for data |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | BEARISH | 22.21% | Approved | — | — | — | Pending target end |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | BEARISH | 28.02% | Approved | $909.24 | — | — | Waiting for data |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | BEARISH | 44.65% | Approved | — | — | — | Waiting for data |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | BEARISH | 44.84% | Approved | — | — | — | Waiting for data |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | BEARISH | 43.93% | Approved | — | — | — | Waiting for data |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | BEARISH | 45.49% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | NO_EDGE | 52.84% | Approved | — | — | — | Pending target end |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | NO_EDGE | 51.05% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 50.50% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | BULLISH | 55.31% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | BULLISH | 54.75% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | BEARISH | 42.38% | Approved | — | — | — | Pending target end |

## GOOG

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $334.33–$335.68 | $335.00 | — | — | — | — | Waiting for data |
| 05:00 | $334.16–$335.51 | $334.84 | — | — | — | — | Waiting for data |
| 06:00 | $334.24–$335.59 | $334.91 | — | — | — | — | Waiting for data |
| 07:00 | $333.75–$335.10 | $334.42 | — | — | — | — | Waiting for data |
| 08:00 | $334.28–$335.63 | $334.96 | — | — | — | — | Waiting for data |
| 09:00 | $333.97–$335.32 | $334.65 | — | — | — | — | Waiting for data |
| 10:00 | $333.93–$335.28 | $334.61 | — | — | — | — | Waiting for data |
| 11:00 | $334.12–$335.47 | $334.80 | — | — | — | — | Waiting for data |
| 12:00 | $333.83–$335.17 | $334.50 | — | — | — | — | Waiting for data |
| 13:00 | $334.37–$335.72 | $335.05 | — | — | — | — | Waiting for data |
| 14:00 | $334.22–$335.57 | $334.89 | — | — | — | — | Waiting for data |
| 15:00 | $334.52–$335.87 | $335.19 | — | — | — | — | Waiting for data |
| 16:00 | $334.23–$335.57 | $334.90 | — | — | — | — | Waiting for data |
| 17:00 | $334.52–$335.87 | $335.20 | — | — | — | — | Pending |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | BEARISH | 33.13% | Approved | — | — | — | Waiting for data |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | BEARISH | 31.93% | Approved | — | — | — | Waiting for data |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | BEARISH | 36.24% | Approved | — | — | — | Waiting for data |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | BEARISH | 36.49% | Approved | — | — | — | Waiting for data |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | BEARISH | 35.46% | Approved | — | — | — | Waiting for data |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | BEARISH | 32.54% | Approved | — | — | — | Waiting for data |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | BEARISH | 32.71% | Approved | — | — | — | Waiting for data |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | BEARISH | 31.22% | Approved | — | — | — | Waiting for data |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | BEARISH | 32.92% | Approved | — | — | — | Waiting for data |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | BEARISH | 26.53% | Approved | — | — | — | Waiting for data |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | BEARISH | 22.86% | Approved | — | — | — | Waiting for data |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | BEARISH | 26.55% | Approved | — | — | — | Waiting for data |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | BEARISH | 25.96% | Approved | — | — | — | Pending target end |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | BEARISH | 44.98% | Approved | $335.28 | — | — | Waiting for data |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | BEARISH | 45.07% | Approved | — | — | — | Waiting for data |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | BEARISH | 44.77% | Approved | — | — | — | Waiting for data |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | BEARISH | 45.11% | Approved | — | — | — | Waiting for data |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | BEARISH | 45.46% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | NO_EDGE | 51.13% | Approved | — | — | — | Pending target end |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | NO_EDGE | 49.34% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 48.79% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | NO_EDGE | 53.62% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 53.06% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | BEARISH | 43.58% | Approved | — | — | — | Pending target end |

## MU

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,000.82–$1,004.84 | $1,002.83 | — | — | — | — | Waiting for data |
| 05:00 | $1,003.27–$1,007.30 | $1,005.29 | — | — | — | — | Waiting for data |
| 06:00 | $1,004.59–$1,008.63 | $1,006.61 | — | — | — | — | Waiting for data |
| 07:00 | $1,001.82–$1,005.84 | $1,003.83 | — | — | — | — | Waiting for data |
| 08:00 | $1,000.85–$1,004.87 | $1,002.86 | — | — | — | — | Waiting for data |
| 09:00 | $1,000.96–$1,004.98 | $1,002.97 | — | — | — | — | Waiting for data |
| 10:00 | $1,003.39–$1,007.42 | $1,005.41 | — | — | — | — | Waiting for data |
| 11:00 | $1,005.99–$1,010.03 | $1,008.01 | — | — | — | — | Waiting for data |
| 12:00 | $1,001.48–$1,005.50 | $1,003.49 | — | — | — | — | Waiting for data |
| 13:00 | $1,005.15–$1,009.19 | $1,007.17 | — | — | — | — | Waiting for data |
| 14:00 | $1,004.14–$1,008.17 | $1,006.15 | — | — | — | — | Waiting for data |
| 15:00 | $1,003.74–$1,007.77 | $1,005.75 | — | — | — | — | Waiting for data |
| 16:00 | $1,002.94–$1,006.97 | $1,004.96 | — | — | — | — | Waiting for data |
| 17:00 | $1,002.98–$1,007.01 | $1,005.00 | — | — | — | — | Pending |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | NO_EDGE | 50.25% | Approved | — | — | — | Waiting for data |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | NO_EDGE | 48.07% | Approved | — | — | — | Waiting for data |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | NO_EDGE | 51.51% | Approved | — | — | — | Waiting for data |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | NO_EDGE | 52.55% | Approved | — | — | — | Waiting for data |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | NO_EDGE | 50.22% | Approved | — | — | — | Waiting for data |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | NO_EDGE | 48.67% | Approved | — | — | — | Waiting for data |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | BEARISH | 43.71% | Approved | — | — | — | Waiting for data |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | NO_EDGE | 47.64% | Approved | — | — | — | Waiting for data |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | NO_EDGE | 48.23% | Approved | — | — | — | Waiting for data |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | NO_EDGE | 46.18% | Approved | — | — | — | Waiting for data |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | BEARISH | 44.84% | Approved | — | — | — | Waiting for data |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | BEARISH | 44.27% | Approved | — | — | — | Waiting for data |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | NO_EDGE | 47.24% | Approved | — | — | — | Pending target end |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | BULLISH | 58.22% | Approved | $1,001.20 | — | — | Waiting for data |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | NO_EDGE | 46.18% | Approved | — | — | — | Waiting for data |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | NO_EDGE | 46.39% | Approved | — | — | — | Waiting for data |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | BEARISH | 45.99% | Approved | — | — | — | Waiting for data |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | NO_EDGE | 46.03% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | NO_EDGE | 50.32% | Approved | — | — | — | Pending target end |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | NO_EDGE | 48.52% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 47.97% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | NO_EDGE | 52.80% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 52.24% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | BEARISH | 44.88% | Approved | — | — | — | Pending target end |

## NVDA

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $225.09–$226.00 | $225.54 | — | — | — | — | Waiting for data |
| 05:00 | $225.21–$226.12 | $225.67 | — | — | — | — | Waiting for data |
| 06:00 | $225.32–$226.23 | $225.78 | — | — | — | — | Waiting for data |
| 07:00 | $225.40–$226.32 | $225.86 | — | — | — | — | Waiting for data |
| 08:00 | $225.34–$226.25 | $225.80 | — | — | — | — | Waiting for data |
| 09:00 | $224.81–$225.72 | $225.26 | — | — | — | — | Waiting for data |
| 10:00 | $224.75–$225.66 | $225.21 | — | — | — | — | Waiting for data |
| 11:00 | $224.70–$225.61 | $225.15 | — | — | — | — | Waiting for data |
| 12:00 | $224.57–$225.48 | $225.02 | — | — | — | — | Waiting for data |
| 13:00 | $225.24–$226.16 | $225.70 | — | — | — | — | Waiting for data |
| 14:00 | $224.94–$225.86 | $225.40 | — | — | — | — | Waiting for data |
| 15:00 | $224.84–$225.75 | $225.29 | — | — | — | — | Waiting for data |
| 16:00 | $224.56–$225.47 | $225.01 | — | — | — | — | Waiting for data |
| 17:00 | $224.55–$225.46 | $225.00 | — | — | — | — | Pending |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | BEARISH | 45.97% | Approved | — | — | — | Waiting for data |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | BEARISH | 45.10% | Approved | — | — | — | Waiting for data |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | NO_EDGE | 49.11% | Approved | — | — | — | Waiting for data |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | NO_EDGE | 47.95% | Approved | — | — | — | Waiting for data |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | NO_EDGE | 46.89% | Approved | — | — | — | Waiting for data |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | BEARISH | 43.99% | Approved | — | — | — | Waiting for data |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | BEARISH | 42.01% | Approved | — | — | — | Waiting for data |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | BEARISH | 41.00% | Approved | — | — | — | Waiting for data |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | NO_EDGE | 47.01% | Approved | — | — | — | Waiting for data |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | BEARISH | 39.35% | Approved | — | — | — | Waiting for data |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | BEARISH | 37.91% | Approved | — | — | — | Waiting for data |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | BEARISH | 41.10% | Approved | — | — | — | Waiting for data |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | BEARISH | 38.77% | Approved | — | — | — | Pending target end |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | BULLISH | 63.17% | Approved | $225.21 | — | — | Waiting for data |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | BEARISH | 45.28% | Approved | — | — | — | Waiting for data |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | BEARISH | 45.67% | Approved | — | — | — | Waiting for data |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | BEARISH | 45.17% | Approved | — | — | — | Waiting for data |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | BEARISH | 45.58% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | NO_EDGE | 50.31% | Approved | — | — | — | Pending target end |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | NO_EDGE | 48.52% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 47.97% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | NO_EDGE | 52.80% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 52.24% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | BEARISH | 43.46% | Approved | — | — | — | Pending target end |

## SNDK

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,747.78–$1,754.80 | $1,751.29 | — | — | — | — | Waiting for data |
| 05:00 | $1,744.82–$1,751.82 | $1,748.32 | — | — | — | — | Waiting for data |
| 06:00 | $1,753.28–$1,760.32 | $1,756.80 | — | — | — | — | Waiting for data |
| 07:00 | $1,745.95–$1,752.96 | $1,749.46 | — | — | — | — | Waiting for data |
| 08:00 | $1,757.69–$1,764.74 | $1,761.22 | — | — | — | — | Waiting for data |
| 09:00 | $1,758.99–$1,766.05 | $1,762.52 | — | — | — | — | Waiting for data |
| 10:00 | $1,762.15–$1,769.22 | $1,765.69 | — | — | — | — | Waiting for data |
| 11:00 | $1,760.07–$1,767.13 | $1,763.60 | — | — | — | — | Waiting for data |
| 12:00 | $1,756.95–$1,764.00 | $1,760.48 | — | — | — | — | Waiting for data |
| 13:00 | $1,752.88–$1,759.91 | $1,756.39 | — | — | — | — | Waiting for data |
| 14:00 | $1,752.84–$1,759.88 | $1,756.36 | — | — | — | — | Waiting for data |
| 15:00 | $1,747.66–$1,754.68 | $1,751.17 | — | — | — | — | Waiting for data |
| 16:00 | $1,753.68–$1,760.72 | $1,757.20 | — | — | — | — | Waiting for data |
| 17:00 | $1,751.19–$1,758.22 | $1,754.70 | — | — | — | — | Pending |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09 04:00 → Sep 09 05:00 | NO_EDGE | 52.87% | Approved | — | — | — | Waiting for data |
| 1h@05:00 | Sep 09 05:00 → Sep 09 06:00 | NO_EDGE | 49.40% | Approved | — | — | — | Waiting for data |
| 1h@06:00 | Sep 09 06:00 → Sep 09 07:00 | NO_EDGE | 53.68% | Approved | — | — | — | Waiting for data |
| 1h@07:00 | Sep 09 07:00 → Sep 09 08:00 | BULLISH | 54.71% | Approved | — | — | — | Waiting for data |
| 1h@08:00 | Sep 09 08:00 → Sep 09 09:00 | NO_EDGE | 52.16% | Approved | — | — | — | Waiting for data |
| 1h@09:00 | Sep 09 09:00 → Sep 09 10:00 | NO_EDGE | 51.63% | Approved | — | — | — | Waiting for data |
| 1h@10:00 | Sep 09 10:00 → Sep 09 11:00 | NO_EDGE | 46.78% | Approved | — | — | — | Waiting for data |
| 1h@11:00 | Sep 09 11:00 → Sep 09 12:00 | NO_EDGE | 51.41% | Approved | — | — | — | Waiting for data |
| 1h@12:00 | Sep 09 12:00 → Sep 09 13:00 | NO_EDGE | 52.50% | Approved | — | — | — | Waiting for data |
| 1h@13:00 | Sep 09 13:00 → Sep 09 14:00 | NO_EDGE | 52.00% | Approved | — | — | — | Waiting for data |
| 1h@14:00 | Sep 09 14:00 → Sep 09 15:00 | NO_EDGE | 47.56% | Approved | — | — | — | Waiting for data |
| 1h@15:00 | Sep 09 15:00 → Sep 09 16:00 | NO_EDGE | 48.82% | Approved | — | — | — | Waiting for data |
| 1h@16:00 | Sep 09 16:00 → Sep 09 17:00 | NO_EDGE | 51.61% | Approved | — | — | — | Pending target end |
| 1h@gap | Sep 08 17:00 → Sep 09 04:00 | BULLISH | 54.77% | Approved | $1,743.19 | — | — | Waiting for data |
| 4h@04:00 | Sep 09 04:00 → Sep 09 08:00 | BEARISH | 45.98% | Approved | — | — | — | Waiting for data |
| 4h@08:00 | Sep 09 08:00 → Sep 09 12:00 | BEARISH | 45.62% | Approved | — | — | — | Waiting for data |
| 4h@12:00 | Sep 09 12:00 → Sep 09 16:00 | BEARISH | 45.80% | Approved | — | — | — | Waiting for data |
| 4h@16:00 | Sep 09 16:00 → Sep 10 07:00 | BEARISH | 45.69% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 09 04:00 → Sep 09 17:00 | NO_EDGE | 50.93% | Approved | — | — | — | Pending target end |
| 1d@D+2 | Sep 10 04:00 → Sep 10 17:00 | NO_EDGE | 49.14% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 48.59% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 14 04:00 → Sep 14 17:00 | NO_EDGE | 53.42% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 52.86% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 09 04:00 → Sep 15 17:00 | NO_EDGE | 47.31% | Approved | — | — | — | Pending target end |

Actual prices use the Gameplan's own stock dataset and the existing five-minute boundary tolerance. The 17:00 price is a completed minute's closing price; earlier hourly clocks use opening prices. No missing prices are filled. Longer forecasts continue in the cumulative saved-Gameplan evaluation.

The machine-readable results also retain the model's cost-adjusted target and Brier score, separately from the raw-price direction results above.
