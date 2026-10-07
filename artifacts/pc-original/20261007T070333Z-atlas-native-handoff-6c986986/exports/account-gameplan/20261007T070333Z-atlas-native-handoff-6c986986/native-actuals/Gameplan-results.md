# Yung Gameplan (YG) results · 2026-10-06

Tomorrow's Gameplan for **2026-10-07** is prepared. All times are Pacific.

The saved price estimates below are compared with actual market prices at the same clock. The ranges were planning estimates; the observations are market prices, not broker fills or trading P/L.

Original forecast: [2026-10-06 Gameplan](C:/DATASTORE/ml/nightly-gameplan-runs/20261006T074936.188782Z/forecasts.parquet).

[Original Gameplan with prices and quantities](C:/DATASTORE/ml/gameplan-trade-plan-runs/20261006T080331.435750Z/Gameplan.md).

**164 evaluated · 66 still pending · 34 missing eligible price observations.**

Direction results compare the saved Bullish/Bearish call with the actual price move. Neutral forecasts have no directional score. Future and missing outcomes are excluded from accuracy.

YG probability scores use a strictly positive raw price return. Cost-adjusted outcomes remain separate.

| Stock | Correct / scored approved calls | Direction accuracy | Mean absolute price error | Prices within range |
| --- | --- | --- | --- | --- |
| AAPL | 7 / 17 | 41.18% | 0.16% | 9 / 14 |
| AMZN | 11 / 17 | 64.71% | 1.33% | 0 / 14 |
| COST | 3 / 13 | 23.08% | 0.90% | 2 / 11 |
| CROX | 6 / 7 | 85.71% | 1.26% | 0 / 7 |
| GOOG | 7 / 17 | 41.18% | 0.32% | 3 / 14 |
| IONQ | 5 / 14 | 35.71% | 1.66% | 0 / 14 |
| MU | 4 / 17 | 23.53% | 0.98% | 6 / 14 |
| NVDA | 8 / 17 | 47.06% | 0.32% | 5 / 14 |
| PATH | 3 / 8 | 37.50% | 0.52% | 2 / 7 |
| SNDK | 8 / 17 | 47.06% | 2.27% | 0 / 14 |
| TWST | 7 / 12 | 58.33% | 10.63% | 0 / 11 |

## AAPL

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $332.06–$333.41 | $332.74 | $332.73 | 04:00 | −$0.01 | -0.00% | Inside |
| 05:00 | $332.04–$333.38 | $332.71 | $332.93 | 05:00 | +$0.22 | +0.07% | Inside |
| 06:00 | $332.09–$333.43 | $332.76 | $332.48 | 06:00 | −$0.28 | -0.08% | Inside |
| 07:00 | $332.42–$333.76 | $333.09 | $331.47 | 07:00 | −$1.62 | -0.49% | Outside |
| 08:00 | $332.48–$333.82 | $333.15 | $333.06 | 08:00 | −$0.08 | -0.03% | Inside |
| 09:00 | $332.65–$333.99 | $333.32 | $333.04 | 09:00 | −$0.28 | -0.08% | Inside |
| 10:00 | $332.65–$333.99 | $333.32 | $332.53 | 10:00 | −$0.79 | -0.24% | Outside |
| 11:00 | $332.78–$334.13 | $333.45 | $332.60 | 11:00 | −$0.85 | -0.26% | Outside |
| 12:00 | $332.69–$334.03 | $333.36 | $333.13 | 12:00 | −$0.23 | -0.07% | Inside |
| 13:00 | $332.62–$333.96 | $333.29 | $333.66 | 13:00 | +$0.37 | +0.11% | Inside |
| 14:00 | $332.33–$333.67 | $333.00 | $333.63 | 14:00 | +$0.63 | +0.19% | Inside |
| 15:00 | $332.35–$333.69 | $333.02 | $333.86 | 15:00 | +$0.84 | +0.25% | Outside |
| 16:00 | $332.60–$333.94 | $333.27 | $333.83 | 16:01 | +$0.56 | +0.17% | Inside |
| 17:00 | $332.44–$333.79 | $333.12 | $333.99 | 17:00 | +$0.87 | +0.26% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BULLISH | 52.24% | Approved | $332.73 | $332.92 | +0.06% | Correct |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BULLISH | 50.26% | Approved | $332.93 | $332.46 | -0.14% | Incorrect |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BULLISH | 50.26% | Approved | $332.48 | $331.49 | -0.30% | Incorrect |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 49.32% | Approved | $331.47 | $333.06 | +0.48% | Incorrect |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 50.74% | Approved | $333.06 | $333.02 | -0.01% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BULLISH | 50.84% | Approved | $333.04 | $332.53 | -0.15% | Incorrect |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BULLISH | 50.84% | Approved | $332.53 | $332.59 | +0.02% | Correct |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 52.12% | Approved | $332.60 | $333.10 | +0.15% | Correct |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BULLISH | 50.08% | Approved | $333.13 | $333.66 | +0.16% | Correct |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 42.72% | Approved | $333.66 | $333.60 | -0.02% | Correct |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 43.63% | Approved | $333.63 | $333.90 | +0.08% | Incorrect |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BULLISH | 50.85% | Approved | $333.86 | $333.85 | -0.00% | Incorrect |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 50.85% | Approved | $333.83 | $333.99 | +0.05% | Correct |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 57.89% | Approved | $332.81 | $332.73 | -0.02% | Incorrect |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 48.98% | Approved | $332.73 | $333.06 | +0.10% | Incorrect |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BULLISH | 53.37% | Approved | $333.06 | $333.10 | +0.01% | Correct |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 48.96% | Approved | $333.13 | $333.85 | +0.22% | Incorrect |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 54.99% | Approved | $333.83 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 55.71% | Research | $332.73 | $333.99 | +0.38% | Correct |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 61.76% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 56.55% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 49.28% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 61.80% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 57.04% | Approved | $332.73 | — | — | Pending target end |

## AMZN

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $251.69–$252.71 | $252.20 | $252.98 | 04:00 | +$0.78 | +0.31% | Outside |
| 05:00 | $251.82–$252.84 | $252.33 | $253.46 | 05:00 | +$1.13 | +0.45% | Outside |
| 06:00 | $251.77–$252.78 | $252.27 | $253.74 | 06:00 | +$1.47 | +0.58% | Outside |
| 07:00 | $251.21–$252.22 | $251.71 | $253.05 | 07:00 | +$1.34 | +0.53% | Outside |
| 08:00 | $251.23–$252.24 | $251.74 | $255.06 | 08:00 | +$3.31 | +1.32% | Outside |
| 09:00 | $251.41–$252.42 | $251.91 | $255.19 | 09:00 | +$3.28 | +1.30% | Outside |
| 10:00 | $251.32–$252.34 | $251.83 | $255.45 | 10:00 | +$3.62 | +1.44% | Outside |
| 11:00 | $251.34–$252.35 | $251.85 | $255.46 | 11:00 | +$3.61 | +1.43% | Outside |
| 12:00 | $251.33–$252.34 | $251.84 | $255.99 | 12:00 | +$4.15 | +1.65% | Outside |
| 13:00 | $251.50–$252.51 | $252.01 | $256.33 | 13:00 | +$4.32 | +1.71% | Outside |
| 14:00 | $251.36–$252.38 | $251.87 | $256.71 | 14:00 | +$4.84 | +1.92% | Outside |
| 15:00 | $251.37–$252.39 | $251.88 | $256.81 | 15:00 | +$4.93 | +1.96% | Outside |
| 16:00 | $251.56–$252.58 | $252.07 | $257.00 | 16:00 | +$4.93 | +1.96% | Outside |
| 17:00 | $251.59–$252.61 | $252.10 | $257.22 | 17:00 | +$5.12 | +2.03% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BULLISH | 52.15% | Approved | $252.98 | $253.44 | +0.18% | Correct |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BULLISH | 50.49% | Approved | $253.46 | $253.80 | +0.13% | Correct |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BULLISH | 50.49% | Approved | $253.74 | $253.07 | -0.26% | Incorrect |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 49.43% | Approved | $253.05 | $255.05 | +0.79% | Incorrect |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 51.13% | Approved | $255.06 | $255.15 | +0.04% | Correct |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BULLISH | 50.62% | Approved | $255.19 | $255.46 | +0.11% | Correct |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BULLISH | 50.51% | Approved | $255.45 | $255.47 | +0.01% | Correct |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 51.66% | Approved | $255.46 | $255.98 | +0.21% | Correct |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BULLISH | 50.27% | Approved | $255.99 | $256.33 | +0.13% | Correct |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 45.34% | Approved | $256.33 | $256.73 | +0.16% | Incorrect |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 45.46% | Approved | $256.71 | $256.81 | +0.04% | Incorrect |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BULLISH | 50.73% | Approved | $256.81 | $256.99 | +0.07% | Correct |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 50.85% | Approved | $257.00 | $257.22 | +0.09% | Correct |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 54.51% | Approved | $251.88 | $252.98 | +0.44% | Correct |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 49.43% | Approved | $252.98 | $255.05 | +0.82% | Incorrect |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BULLISH | 52.65% | Approved | $255.06 | $255.98 | +0.36% | Correct |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 47.62% | Approved | $255.99 | $256.99 | +0.39% | Incorrect |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 53.46% | Approved | $257.00 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 51.56% | Research | $252.98 | $257.22 | +1.68% | Correct |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.65% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 48.76% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 46.79% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 55.83% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 55.42% | Approved | $252.98 | — | — | Pending target end |

## COST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $920.16–$923.86 | $922.01 | $921.98 | 04:00 | −$0.03 | -0.00% | Inside |
| 05:00 | $920.09–$923.79 | $921.94 | — | — | — | — | No observed price within five minutes |
| 06:00 | $920.18–$923.88 | $922.03 | $921.03 | 06:00 | −$1.00 | -0.11% | Inside |
| 07:00 | $919.90–$923.60 | $921.75 | $924.66 | 07:00 | +$2.91 | +0.32% | Outside |
| 08:00 | $920.03–$923.73 | $921.88 | $928.38 | 08:00 | +$6.50 | +0.71% | Outside |
| 09:00 | $920.32–$924.02 | $922.17 | $929.25 | 09:00 | +$7.08 | +0.77% | Outside |
| 10:00 | $920.55–$924.25 | $922.40 | $932.31 | 10:00 | +$9.91 | +1.07% | Outside |
| 11:00 | $920.11–$923.81 | $921.96 | $931.47 | 11:00 | +$9.51 | +1.03% | Outside |
| 12:00 | $920.05–$923.74 | $921.89 | $934.60 | 12:00 | +$12.71 | +1.38% | Outside |
| 13:00 | $919.89–$923.59 | $921.74 | $935.85 | 13:00 | +$14.11 | +1.53% | Outside |
| 14:00 | $919.89–$923.59 | $921.74 | — | — | — | — | No observed price within five minutes |
| 15:00 | $919.72–$923.42 | $921.57 | — | — | — | — | No observed price within five minutes |
| 16:00 | $919.44–$923.13 | $921.29 | $935.28 | 16:00 | +$13.99 | +1.52% | Outside |
| 17:00 | $919.91–$923.61 | $921.76 | $935.18 | 16:58 | +$13.42 | +1.46% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BULLISH | 55.14% | Approved | $921.98 | $921.93 | -0.01% | Incorrect |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BULLISH | 50.33% | Approved | — | $920.70 | — | Start: No observed price within five minutes |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BULLISH | 50.33% | Approved | $921.03 | $924.67 | +0.40% | Correct |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 45.98% | Approved | $924.66 | $928.24 | +0.39% | Incorrect |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BEARISH | 48.96% | Approved | $928.38 | $929.36 | +0.11% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BEARISH | 48.44% | Approved | $929.25 | $932.56 | +0.36% | Incorrect |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BEARISH | 47.84% | Approved | $932.31 | $931.57 | -0.08% | Correct |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BEARISH | 48.99% | Approved | $931.47 | $934.35 | +0.31% | Incorrect |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BEARISH | 47.20% | Approved | $934.60 | $935.76 | +0.12% | Incorrect |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 40.82% | Approved | $935.85 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 42.56% | Approved | — | $934.50 | — | Start: No observed price within five minutes |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BEARISH | 47.62% | Approved | — | $934.54 | — | Start: No observed price within five minutes |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 51.06% | Approved | $935.28 | $935.18 | -0.01% | Incorrect |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 50.53% | Approved | $922.04 | $921.98 | -0.01% | Incorrect |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 47.30% | Approved | $921.98 | $928.24 | +0.68% | Incorrect |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BEARISH | 44.66% | Approved | $928.38 | $934.35 | +0.64% | Incorrect |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 46.98% | Approved | $934.60 | $934.54 | -0.01% | Correct |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 50.84% | Approved | $935.28 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 27.52% | Research | $921.98 | $935.18 | +1.43% | Incorrect |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 26.85% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 42.70% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 43.74% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BEARISH | 43.70% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 54.75% | Approved | $921.98 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 05:00 | No observed price within five minutes (nearest Oct 06 05:06; 6 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price within five minutes (nearest Oct 06 14:17; 17 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 06 15:22; 22 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 06 05:06; 6 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 06 13:49; 11 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Oct 06 14:17; 17 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 06 15:22; 22 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## CROX

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $117.99–$118.47 | $118.23 | — | — | — | — | No observed price within five minutes |
| 05:00 | $118.12–$118.61 | $118.37 | — | — | — | — | No observed price within five minutes |
| 06:00 | $118.02–$118.51 | $118.27 | — | — | — | — | No observed price within five minutes |
| 07:00 | $117.92–$118.40 | $118.16 | $119.33 | 07:00 | +$1.17 | +0.99% | Outside |
| 08:00 | $117.98–$118.46 | $118.22 | $119.09 | 08:02 | +$0.87 | +0.74% | Outside |
| 09:00 | $117.87–$118.35 | $118.11 | $119.34 | 09:00 | +$1.23 | +1.04% | Outside |
| 10:00 | $117.84–$118.33 | $118.08 | $119.57 | 10:00 | +$1.49 | +1.26% | Outside |
| 11:00 | $117.71–$118.19 | $117.95 | $119.26 | 11:00 | +$1.31 | +1.11% | Outside |
| 12:00 | $117.62–$118.11 | $117.87 | $119.72 | 12:00 | +$1.85 | +1.57% | Outside |
| 13:00 | $117.53–$118.01 | $117.77 | $120.22 | 13:00 | +$2.45 | +2.08% | Outside |
| 14:00 | $117.51–$117.99 | $117.75 | — | — | — | — | No observed price within five minutes |
| 15:00 | $117.13–$117.61 | $117.37 | — | — | — | — | No observed price within five minutes |
| 16:00 | $118.32–$118.80 | $118.56 | — | — | — | — | No observed price on the required side of the clock |
| 17:00 | $117.64–$118.12 | $117.88 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BULLISH | 52.72% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BEARISH | 49.10% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BEARISH | 49.10% | Approved | — | $119.49 | — | Start: No observed price within five minutes |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 48.27% | Approved | $119.33 | $118.95 | -0.32% | Correct |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 51.38% | Approved | $119.09 | $119.28 | +0.16% | Correct |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BULLISH | 50.56% | Approved | $119.34 | $119.63 | +0.24% | Correct |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BULLISH | 50.57% | Approved | $119.57 | $119.24 | -0.28% | Incorrect |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 52.08% | Approved | $119.26 | $119.73 | +0.39% | Correct |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BULLISH | 50.14% | Approved | $119.72 | $120.15 | +0.36% | Correct |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 44.05% | Approved | $120.22 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 49.36% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BULLISH | 50.58% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 50.58% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 53.81% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 46.33% | Approved | — | $118.95 | — | Start: No observed price within five minutes |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BULLISH | 55.14% | Approved | $119.09 | $119.73 | +0.54% | Correct |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 48.27% | Approved | $119.72 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 53.96% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 33.35% | Research | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 31.76% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 40.17% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 69.33% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 60.34% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 55.64% | Approved | — | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 04:00 | No observed price within five minutes (nearest Oct 06 05:25; 85 minutes away); source request covers the full tolerance window |
| Price 05:00 | No observed price within five minutes (nearest Oct 06 05:25; 25 minutes away); source request covers the full tolerance window |
| Price 06:00 | No observed price within five minutes (nearest Oct 06 06:30; 30 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price within five minutes (nearest Oct 06 15:11; 71 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 06 15:11; 11 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 06 15:12; 108 minutes away); source request covers the full tolerance window |
| 1h@04:00 start | No observed price within five minutes (nearest Oct 06 05:25; 85 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Oct 05 13:01; 959 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 06 05:25; 25 minutes away); source request covers the full tolerance window |
| 1h@05:00 end | No observed price within five minutes (nearest Oct 06 05:26; 34 minutes away); source request covers the full tolerance window |
| 1h@06:00 start | No observed price within five minutes (nearest Oct 06 06:30; 30 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 06 13:06; 54 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Oct 06 15:11; 71 minutes away); source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 06 13:06; 114 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 06 15:11; 11 minutes away); source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 06 15:12; 48 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 06 15:12; 108 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 05 13:01; 239 minutes away); source request covers the full tolerance window |
| 1h@gap end | No observed price within five minutes (nearest Oct 06 05:25; 85 minutes away); source request covers the full tolerance window |
| 4h@04:00 start | No observed price within five minutes (nearest Oct 06 05:25; 85 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 06 15:12; 48 minutes away); source request covers the full tolerance window |
| 1d@D+1 start | No observed price within five minutes (nearest Oct 06 05:25; 85 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 06 15:12; 108 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## GOOG

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $343.02–$344.41 | $343.71 | $345.17 | 04:00 | +$1.46 | +0.42% | Outside |
| 05:00 | $342.95–$344.33 | $343.64 | $345.26 | 05:00 | +$1.62 | +0.47% | Outside |
| 06:00 | $343.03–$344.42 | $343.73 | $345.02 | 06:00 | +$1.29 | +0.38% | Outside |
| 07:00 | $342.36–$343.74 | $343.05 | $342.41 | 07:00 | −$0.64 | -0.19% | Inside |
| 08:00 | $342.59–$343.98 | $343.28 | $344.34 | 08:00 | +$1.06 | +0.31% | Outside |
| 09:00 | $342.78–$344.17 | $343.47 | $344.25 | 09:00 | +$0.78 | +0.23% | Outside |
| 10:00 | $342.81–$344.19 | $343.50 | $343.75 | 10:00 | +$0.25 | +0.07% | Inside |
| 11:00 | $342.70–$344.08 | $343.39 | $344.17 | 11:00 | +$0.78 | +0.23% | Outside |
| 12:00 | $342.57–$343.96 | $343.26 | $344.99 | 12:00 | +$1.73 | +0.50% | Outside |
| 13:00 | $342.78–$344.16 | $343.47 | $344.62 | 13:00 | +$1.15 | +0.33% | Outside |
| 14:00 | $342.74–$344.12 | $343.43 | $344.80 | 14:01 | +$1.37 | +0.40% | Outside |
| 15:00 | $342.82–$344.21 | $343.52 | $344.80 | 15:05 | +$1.28 | +0.37% | Outside |
| 16:00 | $343.30–$344.68 | $343.99 | $344.68 | 16:01 | +$0.69 | +0.20% | Inside |
| 17:00 | $342.93–$344.31 | $343.62 | $345.06 | 16:58 | +$1.44 | +0.42% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BULLISH | 53.44% | Approved | $345.17 | $345.20 | +0.01% | Correct |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BULLISH | 50.74% | Approved | $345.26 | $345.00 | -0.08% | Incorrect |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BULLISH | 50.74% | Approved | $345.02 | $342.47 | -0.74% | Incorrect |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 49.79% | Approved | $342.41 | $344.32 | +0.56% | Incorrect |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 51.83% | Approved | $344.34 | $344.26 | -0.02% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BULLISH | 51.33% | Approved | $344.25 | $343.75 | -0.15% | Incorrect |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BULLISH | 51.34% | Approved | $343.75 | $344.16 | +0.12% | Correct |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 52.49% | Approved | $344.17 | $344.97 | +0.23% | Correct |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BULLISH | 50.70% | Approved | $344.99 | $344.63 | -0.10% | Incorrect |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 46.38% | Approved | $344.62 | $344.81 | +0.06% | Incorrect |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 46.91% | Approved | $344.80 | $344.75 | -0.01% | Correct |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BULLISH | 51.12% | Approved | $344.80 | $344.74 | -0.02% | Incorrect |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 51.11% | Approved | $344.68 | $345.06 | +0.11% | Correct |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 52.47% | Approved | $343.43 | $345.17 | +0.51% | Correct |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BULLISH | 50.90% | Approved | $345.17 | $344.32 | -0.25% | Incorrect |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BULLISH | 54.95% | Approved | $344.34 | $344.97 | +0.18% | Correct |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BULLISH | 51.68% | Approved | $344.99 | $344.74 | -0.07% | Incorrect |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 52.82% | Approved | $344.68 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 54.87% | Research | $345.17 | $345.06 | -0.03% | Incorrect |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 52.88% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 46.38% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 41.14% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 57.53% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 55.13% | Approved | $345.17 | — | — | Pending target end |

## IONQ

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $42.87–$43.05 | $42.96 | $43.39 | 04:00 | +$0.43 | +1.00% | Outside |
| 05:00 | $42.90–$43.08 | $42.99 | $43.54 | 05:00 | +$0.55 | +1.28% | Outside |
| 06:00 | $42.92–$43.10 | $43.01 | $43.61 | 06:00 | +$0.60 | +1.40% | Outside |
| 07:00 | $42.78–$42.96 | $42.87 | $44.17 | 07:00 | +$1.30 | +3.03% | Outside |
| 08:00 | $42.75–$42.93 | $42.84 | $44.10 | 08:00 | +$1.26 | +2.94% | Outside |
| 09:00 | $42.77–$42.95 | $42.86 | $43.63 | 09:00 | +$0.77 | +1.80% | Outside |
| 10:00 | $42.69–$42.87 | $42.78 | $43.18 | 10:00 | +$0.40 | +0.94% | Outside |
| 11:00 | $42.71–$42.89 | $42.80 | $43.49 | 11:00 | +$0.69 | +1.61% | Outside |
| 12:00 | $42.72–$42.90 | $42.81 | $43.57 | 12:00 | +$0.76 | +1.78% | Outside |
| 13:00 | $42.68–$42.86 | $42.77 | $43.33 | 13:00 | +$0.56 | +1.31% | Outside |
| 14:00 | $42.74–$42.92 | $42.83 | $43.48 | 14:02 | +$0.65 | +1.52% | Outside |
| 15:00 | $42.70–$42.89 | $42.79 | $43.47 | 15:00 | +$0.68 | +1.59% | Outside |
| 16:00 | $42.82–$43.01 | $42.92 | $43.48 | 16:00 | +$0.56 | +1.30% | Outside |
| 17:00 | $42.75–$42.93 | $42.84 | $43.59 | 17:00 | +$0.75 | +1.75% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BEARISH | 43.33% | Approved | $43.39 | $43.52 | +0.30% | Incorrect |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BEARISH | 46.31% | Approved | $43.54 | $43.62 | +0.18% | Incorrect |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BEARISH | 46.84% | Approved | $43.61 | $44.16 | +1.26% | Incorrect |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 48.10% | Approved | $44.17 | $44.09 | -0.18% | Correct |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 50.58% | Approved | $44.10 | $43.62 | -1.09% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BEARISH | 48.61% | Approved | $43.63 | $43.20 | -0.99% | Correct |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BEARISH | 48.61% | Approved | $43.18 | $43.49 | +0.72% | Incorrect |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BEARISH | 49.90% | Approved | $43.49 | $43.57 | +0.18% | Incorrect |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BEARISH | 48.30% | Approved | $43.57 | $43.31 | -0.60% | Correct |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 45.32% | Approved | $43.33 | $43.49 | +0.37% | Incorrect |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 48.74% | Approved | $43.48 | $43.47 | -0.02% | Correct |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BEARISH | 48.91% | Approved | $43.47 | — | — | End: No observed price within five minutes |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BEARISH | 48.91% | Approved | $43.48 | $43.59 | +0.25% | Incorrect |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 54.98% | Approved | — | $43.39 | — | Start: No observed price within five minutes |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 44.33% | Approved | $43.39 | $44.09 | +1.61% | Incorrect |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BEARISH | 48.92% | Approved | $44.10 | $43.57 | -1.20% | Correct |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 47.65% | Approved | $43.57 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BEARISH | 48.16% | Approved | $43.48 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 49.06% | Research | $43.39 | $43.59 | +0.46% | Incorrect |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 50.15% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 46.19% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 38.15% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BEARISH | 48.45% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 56.05% | Approved | $43.39 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 06 15:52; 8 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 05 16:51; 9 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 06 15:52; 8 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## MU

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,063.54–$1,067.81 | $1,065.67 | $1,060.17 | 04:00 | −$5.50 | -0.52% | Outside |
| 05:00 | $1,064.34–$1,068.61 | $1,066.48 | $1,066.17 | 05:00 | −$0.31 | -0.03% | Inside |
| 06:00 | $1,064.30–$1,068.58 | $1,066.44 | $1,067.55 | 06:00 | +$1.11 | +0.10% | Inside |
| 07:00 | $1,063.54–$1,067.82 | $1,065.68 | $1,066.22 | 07:00 | +$0.54 | +0.05% | Inside |
| 08:00 | $1,063.98–$1,068.25 | $1,066.12 | $1,070.45 | 08:00 | +$4.33 | +0.41% | Outside |
| 09:00 | $1,064.18–$1,068.46 | $1,066.32 | $1,065.50 | 09:00 | −$0.82 | -0.08% | Inside |
| 10:00 | $1,064.62–$1,068.89 | $1,066.76 | $1,064.67 | 10:00 | −$2.09 | -0.20% | Inside |
| 11:00 | $1,065.29–$1,069.57 | $1,067.43 | $1,066.34 | 11:00 | −$1.09 | -0.10% | Inside |
| 12:00 | $1,064.55–$1,068.83 | $1,066.69 | $1,057.38 | 12:00 | −$9.31 | -0.87% | Outside |
| 13:00 | $1,066.12–$1,070.40 | $1,068.26 | $1,045.55 | 13:00 | −$22.71 | -2.13% | Outside |
| 14:00 | $1,067.41–$1,071.69 | $1,069.55 | $1,045.49 | 14:00 | −$24.06 | -2.25% | Outside |
| 15:00 | $1,067.22–$1,071.51 | $1,069.36 | $1,046.60 | 15:00 | −$22.76 | -2.13% | Outside |
| 16:00 | $1,069.53–$1,073.82 | $1,071.67 | $1,044.62 | 16:00 | −$27.05 | -2.52% | Outside |
| 17:00 | $1,066.79–$1,071.08 | $1,068.93 | $1,044.44 | 17:00 | −$24.49 | -2.29% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BULLISH | 50.94% | Approved | $1,060.17 | $1,065.95 | +0.55% | Correct |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BEARISH | 49.97% | Approved | $1,066.17 | $1,067.60 | +0.13% | Incorrect |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BEARISH | 49.97% | Approved | $1,067.55 | $1,066.36 | -0.11% | Correct |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 49.18% | Approved | $1,066.22 | $1,070.38 | +0.39% | Incorrect |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 52.18% | Approved | $1,070.45 | $1,065.39 | -0.47% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BULLISH | 50.74% | Approved | $1,065.50 | $1,064.66 | -0.08% | Incorrect |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BULLISH | 50.75% | Approved | $1,064.67 | $1,066.20 | +0.14% | Correct |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 52.34% | Approved | $1,066.34 | $1,057.39 | -0.84% | Incorrect |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BULLISH | 50.04% | Approved | $1,057.38 | $1,045.50 | -1.12% | Incorrect |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 43.56% | Approved | $1,045.55 | $1,045.40 | -0.01% | Correct |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 47.18% | Approved | $1,045.49 | $1,046.50 | +0.10% | Incorrect |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BULLISH | 50.53% | Approved | $1,046.60 | $1,044.60 | -0.19% | Incorrect |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 50.52% | Approved | $1,044.62 | $1,044.44 | -0.02% | Incorrect |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 56.37% | Approved | $1,064.35 | $1,060.17 | -0.39% | Incorrect |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 49.20% | Approved | $1,060.17 | $1,070.38 | +0.96% | Incorrect |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BULLISH | 51.79% | Approved | $1,070.45 | $1,057.39 | -1.22% | Incorrect |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BULLISH | 50.26% | Approved | $1,057.38 | $1,044.60 | -1.21% | Incorrect |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 54.38% | Approved | $1,044.62 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 50.73% | Research | $1,060.17 | $1,044.44 | -1.48% | Incorrect |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 50.93% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 43.83% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 35.14% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 59.75% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 56.10% | Approved | $1,060.17 | — | — | Pending target end |

## NVDA

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $240.16–$241.13 | $240.65 | $240.92 | 04:00 | +$0.27 | +0.11% | Inside |
| 05:00 | $240.40–$241.37 | $240.88 | $241.42 | 05:00 | +$0.54 | +0.22% | Outside |
| 06:00 | $240.40–$241.38 | $240.89 | $241.41 | 06:00 | +$0.52 | +0.22% | Outside |
| 07:00 | $240.24–$241.21 | $240.72 | $242.30 | 07:00 | +$1.58 | +0.66% | Outside |
| 08:00 | $240.38–$241.35 | $240.87 | $242.46 | 08:00 | +$1.59 | +0.66% | Outside |
| 09:00 | $240.18–$241.15 | $240.66 | $241.64 | 09:00 | +$0.98 | +0.41% | Outside |
| 10:00 | $240.12–$241.10 | $240.61 | $240.58 | 10:00 | −$0.03 | -0.01% | Inside |
| 11:00 | $240.15–$241.13 | $240.64 | $239.99 | 11:00 | −$0.65 | -0.27% | Outside |
| 12:00 | $240.30–$241.27 | $240.78 | $240.44 | 12:00 | −$0.34 | -0.14% | Inside |
| 13:00 | $240.31–$241.29 | $240.80 | $239.19 | 13:00 | −$1.61 | -0.67% | Outside |
| 14:00 | $240.19–$241.16 | $240.67 | $239.49 | 14:01 | −$1.18 | -0.49% | Outside |
| 15:00 | $239.98–$240.95 | $240.46 | $239.53 | 15:00 | −$0.93 | -0.39% | Outside |
| 16:00 | $240.13–$241.10 | $240.61 | $240.35 | 16:00 | −$0.26 | -0.11% | Inside |
| 17:00 | $239.77–$240.74 | $240.26 | $239.89 | 17:00 | −$0.37 | -0.15% | Inside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BULLISH | 58.47% | Approved | $240.92 | $241.42 | +0.21% | Correct |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BULLISH | 50.60% | Approved | $241.42 | $241.42 | +0.00% | Incorrect |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BULLISH | 50.96% | Approved | $241.41 | $242.29 | +0.37% | Correct |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BULLISH | 50.20% | Approved | $242.30 | $242.46 | +0.06% | Correct |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 51.60% | Approved | $242.46 | $241.62 | -0.34% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BULLISH | 51.80% | Approved | $241.64 | $240.57 | -0.44% | Incorrect |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BULLISH | 51.50% | Approved | $240.58 | $239.98 | -0.25% | Incorrect |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 51.74% | Approved | $239.99 | $240.44 | +0.19% | Correct |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BULLISH | 51.00% | Approved | $240.44 | $239.18 | -0.53% | Incorrect |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 45.59% | Approved | $239.19 | $239.50 | +0.13% | Incorrect |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 47.19% | Approved | $239.49 | $239.54 | +0.02% | Incorrect |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BULLISH | 51.28% | Approved | $239.53 | $240.34 | +0.34% | Correct |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 51.40% | Approved | $240.35 | $239.89 | -0.19% | Incorrect |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 50.41% | Approved | $240.19 | $240.92 | +0.30% | Correct |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 47.64% | Approved | $240.92 | $242.46 | +0.64% | Incorrect |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BEARISH | 48.73% | Approved | $242.46 | $240.44 | -0.83% | Correct |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 45.38% | Approved | $240.44 | $240.34 | -0.04% | Correct |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BEARISH | 49.72% | Approved | $240.35 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 46.71% | Research | $240.92 | $239.89 | -0.43% | Correct |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.37% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 37.20% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 40.80% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 54.17% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 54.17% | Approved | $240.92 | — | — | Pending target end |

## PATH

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $13.15–$13.21 | $13.18 | — | — | — | — | No observed price within five minutes |
| 05:00 | $13.15–$13.21 | $13.18 | — | — | — | — | No observed price within five minutes |
| 06:00 | $13.15–$13.21 | $13.18 | — | — | — | — | No observed price within five minutes |
| 07:00 | $13.12–$13.19 | $13.15 | $13.24 | 07:00 | +$0.09 | +0.68% | Outside |
| 08:00 | $13.13–$13.19 | $13.16 | $13.16 | 08:00 | +$0.00 | +0.00% | Inside |
| 09:00 | $13.13–$13.20 | $13.17 | $13.18 | 09:00 | +$0.01 | +0.04% | Inside |
| 10:00 | $13.12–$13.19 | $13.16 | $13.10 | 10:00 | −$0.06 | -0.46% | Outside |
| 11:00 | $13.11–$13.17 | $13.14 | $13.03 | 11:00 | −$0.11 | -0.84% | Outside |
| 12:00 | $13.11–$13.17 | $13.14 | $12.97 | 12:00 | −$0.17 | -1.29% | Outside |
| 13:00 | $13.12–$13.18 | $13.15 | $13.11 | 13:00 | −$0.04 | -0.30% | Outside |
| 14:00 | $13.13–$13.19 | $13.16 | — | — | — | — | No observed price within five minutes |
| 15:00 | $13.09–$13.15 | $13.12 | — | — | — | — | No observed price within five minutes |
| 16:00 | $13.12–$13.18 | $13.15 | — | — | — | — | No observed price on the required side of the clock |
| 17:00 | $13.12–$13.19 | $13.16 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BEARISH | 46.63% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BEARISH | 46.67% | Approved | — | $13.32 | — | Start: No observed price within five minutes |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BEARISH | 47.22% | Approved | — | $13.24 | — | Start: No observed price within five minutes |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 48.64% | Approved | $13.24 | $13.16 | -0.60% | Correct |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BEARISH | 49.37% | Approved | $13.16 | $13.17 | +0.08% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BEARISH | 48.73% | Approved | $13.18 | $13.11 | -0.49% | Correct |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BEARISH | 48.73% | Approved | $13.10 | $13.04 | -0.50% | Correct |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 50.00% | Approved | $13.03 | $12.97 | -0.46% | Incorrect |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BEARISH | 48.73% | Approved | $12.97 | $13.12 | +1.16% | Incorrect |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BULLISH | 50.77% | Approved | $13.11 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 44.87% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BEARISH | 48.49% | Approved | — | $13.09 | — | Start: No observed price within five minutes |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BEARISH | 49.39% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 53.04% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 46.23% | Approved | — | $13.16 | — | Start: No observed price within five minutes |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BULLISH | 53.49% | Approved | $13.16 | $12.97 | -1.44% | Incorrect |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 49.52% | Approved | $12.97 | $13.09 | +0.93% | Incorrect |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 52.44% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 35.01% | Research | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 28.50% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 46.08% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 51.76% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 58.24% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 54.79% | Approved | — | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 04:00 | No observed price within five minutes (nearest Oct 06 04:16; 16 minutes away); source request covers the full tolerance window |
| Price 05:00 | No observed price within five minutes (nearest Oct 06 05:11; 11 minutes away); source request covers the full tolerance window |
| Price 06:00 | No observed price within five minutes (nearest Oct 06 06:17; 17 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price within five minutes (nearest Oct 06 15:55; 115 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 06 15:55; 55 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 06 15:56; 64 minutes away); source request covers the full tolerance window |
| 1h@04:00 start | No observed price within five minutes (nearest Oct 06 04:16; 16 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Oct 06 04:53; 7 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 06 05:11; 11 minutes away); source request covers the full tolerance window |
| 1h@06:00 start | No observed price within five minutes (nearest Oct 06 06:17; 17 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 06 13:44; 16 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Oct 06 15:55; 115 minutes away); source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 06 13:44; 76 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 06 15:55; 55 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 06 15:56; 64 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 05 16:31; 29 minutes away); source request covers the full tolerance window |
| 1h@gap end | No observed price within five minutes (nearest Oct 06 04:16; 16 minutes away); source request covers the full tolerance window |
| 4h@04:00 start | No observed price within five minutes (nearest Oct 06 04:16; 16 minutes away); source request covers the full tolerance window |
| 1d@D+1 start | No observed price within five minutes (nearest Oct 06 04:16; 16 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 06 15:56; 64 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## SNDK

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,710.07–$1,716.94 | $1,713.51 | $1,695.60 | 04:00 | −$17.91 | -1.05% | Outside |
| 05:00 | $1,710.18–$1,717.05 | $1,713.61 | $1,707.65 | 05:00 | −$5.96 | -0.35% | Outside |
| 06:00 | $1,712.76–$1,719.63 | $1,716.20 | $1,707.50 | 06:00 | −$8.70 | -0.51% | Outside |
| 07:00 | $1,708.99–$1,715.85 | $1,712.42 | $1,682.73 | 07:00 | −$29.69 | -1.73% | Outside |
| 08:00 | $1,709.26–$1,716.12 | $1,712.69 | $1,679.47 | 08:00 | −$33.22 | -1.94% | Outside |
| 09:00 | $1,707.99–$1,714.85 | $1,711.42 | $1,669.69 | 09:00 | −$41.73 | -2.44% | Outside |
| 10:00 | $1,711.99–$1,718.87 | $1,715.43 | $1,670.41 | 10:00 | −$45.02 | -2.62% | Outside |
| 11:00 | $1,714.45–$1,721.33 | $1,717.89 | $1,675.60 | 11:00 | −$42.29 | -2.46% | Outside |
| 12:00 | $1,712.00–$1,718.87 | $1,715.44 | $1,667.84 | 12:00 | −$47.61 | -2.78% | Outside |
| 13:00 | $1,713.37–$1,720.25 | $1,716.81 | $1,660.46 | 13:00 | −$56.35 | -3.28% | Outside |
| 14:00 | $1,714.02–$1,720.90 | $1,717.46 | $1,663.63 | 14:00 | −$53.83 | -3.13% | Outside |
| 15:00 | $1,716.18–$1,723.07 | $1,719.62 | $1,665.43 | 15:00 | −$54.19 | -3.15% | Outside |
| 16:00 | $1,712.73–$1,719.60 | $1,716.17 | $1,663.50 | 16:00 | −$52.67 | -3.07% | Outside |
| 17:00 | $1,716.24–$1,723.13 | $1,719.69 | $1,663.99 | 17:00 | −$55.70 | -3.24% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BEARISH | 44.76% | Approved | $1,695.60 | $1,706.50 | +0.64% | Incorrect |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BEARISH | 47.89% | Approved | $1,707.65 | $1,707.88 | +0.01% | Incorrect |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BEARISH | 48.42% | Approved | $1,707.50 | $1,682.74 | -1.45% | Correct |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 49.42% | Approved | $1,682.73 | $1,679.22 | -0.21% | Correct |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 50.14% | Approved | $1,679.47 | $1,669.50 | -0.59% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BEARISH | 49.75% | Approved | $1,669.69 | $1,670.17 | +0.03% | Incorrect |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BEARISH | 49.75% | Approved | $1,670.41 | $1,675.83 | +0.32% | Incorrect |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 50.68% | Approved | $1,675.60 | $1,667.73 | -0.47% | Incorrect |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BEARISH | 49.25% | Approved | $1,667.84 | $1,660.26 | -0.45% | Correct |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 45.84% | Approved | $1,660.46 | $1,664.25 | +0.23% | Incorrect |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BULLISH | 50.53% | Approved | $1,663.63 | $1,665.21 | +0.09% | Correct |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BEARISH | 49.91% | Approved | $1,665.43 | $1,663.95 | -0.09% | Correct |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BEARISH | 49.91% | Approved | $1,663.50 | $1,663.99 | +0.03% | Incorrect |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 57.34% | Approved | $1,705.96 | $1,695.60 | -0.61% | Incorrect |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 46.11% | Approved | $1,695.60 | $1,679.22 | -0.97% | Correct |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BEARISH | 49.37% | Approved | $1,679.47 | $1,667.73 | -0.70% | Correct |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 48.05% | Approved | $1,667.84 | $1,663.95 | -0.23% | Correct |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 51.05% | Approved | $1,663.50 | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 63.21% | Research | $1,695.60 | $1,663.99 | -1.86% | Incorrect |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 54.36% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 55.38% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 53.45% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 63.89% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 55.75% | Approved | $1,695.60 | — | — | Pending target end |

## TWST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $205.94–$206.77 | $206.36 | $209.04 | 04:00 | +$2.68 | +1.30% | Outside |
| 05:00 | $206.45–$207.28 | $206.86 | $210.50 | 05:05 | +$3.64 | +1.76% | Outside |
| 06:00 | $206.64–$207.47 | $207.06 | $210.00 | 06:00 | +$2.94 | +1.42% | Outside |
| 07:00 | $205.25–$206.08 | $205.66 | $208.10 | 07:00 | +$2.44 | +1.19% | Outside |
| 08:00 | $205.56–$206.39 | $205.98 | $184.00 | 08:00 | −$21.98 | -10.67% | Outside |
| 09:00 | $205.63–$206.46 | $206.04 | $177.72 | 09:00 | −$28.32 | -13.74% | Outside |
| 10:00 | $205.65–$206.48 | $206.06 | $173.34 | 10:00 | −$32.72 | -15.88% | Outside |
| 11:00 | $205.97–$206.80 | $206.39 | $173.81 | 11:00 | −$32.57 | -15.78% | Outside |
| 12:00 | $205.47–$206.31 | $205.89 | $170.56 | 12:00 | −$35.32 | -17.16% | Outside |
| 13:00 | $205.52–$206.35 | $205.94 | $166.97 | 13:00 | −$38.97 | -18.92% | Outside |
| 14:00 | $205.58–$206.42 | $206.00 | — | — | — | — | No observed price within five minutes |
| 15:00 | $206.68–$207.52 | $207.10 | $167.59 | 15:01 | −$39.51 | -19.08% | Outside |
| 16:00 | $206.39–$207.23 | $206.81 | — | — | — | — | No observed price within five minutes |
| 17:00 | $205.85–$206.69 | $206.27 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 06 04:00 → Oct 06 05:00 | BEARISH | 49.40% | Approved | $209.04 | $211.00 | +0.94% | Incorrect |
| 1h@05:00 | Oct 06 05:00 → Oct 06 06:00 | BEARISH | 45.71% | Approved | $210.50 | $210.25 | -0.12% | Correct |
| 1h@06:00 | Oct 06 06:00 → Oct 06 07:00 | BEARISH | 47.52% | Approved | $210.00 | $208.80 | -0.57% | Correct |
| 1h@07:00 | Oct 06 07:00 → Oct 06 08:00 | BEARISH | 48.38% | Approved | $208.10 | $183.84 | -11.66% | Correct |
| 1h@08:00 | Oct 06 08:00 → Oct 06 09:00 | BULLISH | 50.42% | Approved | $184.00 | $177.63 | -3.46% | Incorrect |
| 1h@09:00 | Oct 06 09:00 → Oct 06 10:00 | BEARISH | 49.94% | Approved | $177.72 | $173.22 | -2.53% | Correct |
| 1h@10:00 | Oct 06 10:00 → Oct 06 11:00 | BULLISH | 50.05% | Approved | $173.34 | $173.69 | +0.20% | Correct |
| 1h@11:00 | Oct 06 11:00 → Oct 06 12:00 | BULLISH | 51.63% | Approved | $173.81 | $170.44 | -1.94% | Incorrect |
| 1h@12:00 | Oct 06 12:00 → Oct 06 13:00 | BEARISH | 49.44% | Approved | $170.56 | $166.88 | -2.16% | Correct |
| 1h@13:00 | Oct 06 13:00 → Oct 06 14:00 | BEARISH | 49.41% | Approved | $166.97 | $167.45 | +0.29% | Incorrect |
| 1h@14:00 | Oct 06 14:00 → Oct 06 15:00 | BEARISH | 46.50% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@15:00 | Oct 06 15:00 → Oct 06 16:00 | BULLISH | 50.10% | Approved | $167.59 | — | — | End: No observed price within five minutes |
| 1h@16:00 | Oct 06 16:00 → Oct 06 17:00 | BULLISH | 50.10% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@gap | Oct 05 17:00 → Oct 06 04:00 | BULLISH | 53.54% | Approved | — | $209.04 | — | Start: No observed price within five minutes |
| 4h@04:00 | Oct 06 04:00 → Oct 06 08:00 | BEARISH | 46.21% | Approved | $209.04 | $183.84 | -12.06% | Correct |
| 4h@08:00 | Oct 06 08:00 → Oct 06 12:00 | BULLISH | 56.52% | Approved | $184.00 | $170.44 | -7.37% | Incorrect |
| 4h@12:00 | Oct 06 12:00 → Oct 06 16:00 | BEARISH | 48.00% | Approved | $170.56 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 06 16:00 → Oct 07 07:00 | BULLISH | 55.05% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 33.87% | Research | $209.04 | — | — | End: No observed price within five minutes |
| 1d@D+2 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 37.49% | Research | — | — | — | Pending target end |
| 1d@D+3 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 53.23% | Research | — | — | — | Pending target end |
| 1d@D+4 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 58.85% | Research | — | — | — | Pending target end |
| 1d@D+5 | Oct 12 04:00 → Oct 12 17:00 | BULLISH | 54.61% | Research | — | — | — | Pending target end |
| 1w@D+5 | Oct 06 04:00 → Oct 12 17:00 | BULLISH | 55.29% | Approved | $209.04 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 14:00 | No observed price within five minutes (nearest Oct 06 14:06; 6 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price within five minutes (nearest Oct 06 16:24; 24 minutes away); source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 06 16:51; 9 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Oct 06 14:06; 6 minutes away); source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 06 14:48; 12 minutes away); source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 06 15:45; 15 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Oct 06 16:24; 24 minutes away); source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 06 16:51; 9 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 05 16:54; 6 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 06 15:45; 15 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 06 16:51; 9 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

Actual prices use the Gameplan's own stock dataset and the existing five-minute boundary tolerance. The 17:00 price is a completed minute's closing price; earlier hourly clocks use opening prices. No missing prices are filled. Longer forecasts continue in the cumulative saved-Gameplan evaluation.

The machine-readable results retain the model's own frozen probability target and Brier score, plus separate raw-price and cost-adjusted outcomes. These are not broker fills or realized profit.
