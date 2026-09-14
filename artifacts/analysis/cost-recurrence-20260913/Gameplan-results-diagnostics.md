# Offline diagnostic preview; original publication preserved

# Gameplan results · 2026-09-11

Tomorrow's Gameplan for **2026-09-14** is prepared. All times are Pacific.

The saved price estimates below are compared with actual market prices at the same clock. The ranges were planning estimates; the observations are market prices, not broker fills or trading P/L.

Original forecast: [2026-09-11 Gameplan](C:/DATASTORE/ml/nightly-gameplan-runs/20260911T051120.445494Z/forecasts.parquet).

[Original Gameplan with prices and quantities](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260911T061456.678966Z/Gameplan.md).

**119 evaluated · 42 still pending · 7 missing eligible price observations.**

Direction results compare the saved Bullish/Bearish call with the actual price move. Neutral forecasts have no directional score. Future and missing outcomes are excluded from accuracy.

| Stock | Correct / scored approved calls | Direction accuracy | Mean absolute price error | Prices within range |
| --- | --- | --- | --- | --- |
| AAPL | 7 / 17 | 41.18% | 1.64% | 2 / 14 |
| AMZN | 6 / 17 | 35.29% | 1.95% | 0 / 14 |
| COST | 3 / 9 | 33.33% | 0.33% | 3 / 11 |
| GOOG | 8 / 17 | 47.06% | 2.12% | 0 / 14 |
| MU | 8 / 16 | 50.00% | 0.45% | 9 / 14 |
| NVDA | 9 / 13 | 69.23% | 0.64% | 2 / 14 |
| SNDK | 8 / 16 | 50.00% | 2.94% | 0 / 14 |

## AAPL

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $325.04–$326.35 | $325.69 | $326.19 | 04:00 | +$0.50 | +0.15% | Inside |
| 05:00 | $324.84–$326.16 | $325.50 | $326.67 | 05:00 | +$1.17 | +0.36% | Outside |
| 06:00 | $324.92–$326.23 | $325.57 | $326.07 | 06:00 | +$0.50 | +0.15% | Inside |
| 07:00 | $325.58–$326.90 | $326.24 | $332.36 | 07:00 | +$6.12 | +1.88% | Outside |
| 08:00 | $325.80–$327.12 | $326.46 | $335.02 | 08:00 | +$8.56 | +2.62% | Outside |
| 09:00 | $325.91–$327.23 | $326.57 | $333.92 | 09:00 | +$7.35 | +2.25% | Outside |
| 10:00 | $326.34–$327.66 | $327.00 | $334.74 | 10:00 | +$7.74 | +2.37% | Outside |
| 11:00 | $326.35–$327.66 | $327.01 | $333.40 | 11:00 | +$6.39 | +1.95% | Outside |
| 12:00 | $325.70–$327.02 | $326.36 | $332.52 | 12:00 | +$6.16 | +1.89% | Outside |
| 13:00 | $325.67–$326.99 | $326.33 | $332.24 | 13:00 | +$5.91 | +1.81% | Outside |
| 14:00 | $325.49–$326.81 | $326.15 | $332.69 | 14:00 | +$6.54 | +2.01% | Outside |
| 15:00 | $325.83–$327.15 | $326.49 | $332.49 | 15:00 | +$6.00 | +1.84% | Outside |
| 16:00 | $325.85–$327.16 | $326.51 | $332.58 | 16:02 | +$6.07 | +1.86% | Outside |
| 17:00 | $325.99–$327.31 | $326.65 | $332.58 | 17:00 | +$5.93 | +1.82% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 11 04:00 → Sep 11 05:00 | BEARISH | 37.77% | Approved | $326.19 | $326.67 | +0.15% | Incorrect |
| 1h@05:00 | Sep 11 05:00 → Sep 11 06:00 | BEARISH | 36.17% | Approved | $326.67 | $326.19 | -0.15% | Correct |
| 1h@06:00 | Sep 11 06:00 → Sep 11 07:00 | BEARISH | 41.75% | Approved | $326.07 | $332.40 | +1.94% | Incorrect |
| 1h@07:00 | Sep 11 07:00 → Sep 11 08:00 | BEARISH | 41.48% | Approved | $332.36 | $334.98 | +0.79% | Incorrect |
| 1h@08:00 | Sep 11 08:00 → Sep 11 09:00 | BEARISH | 37.39% | Approved | $335.02 | $333.92 | -0.33% | Correct |
| 1h@09:00 | Sep 11 09:00 → Sep 11 10:00 | BEARISH | 38.18% | Approved | $333.92 | $334.75 | +0.25% | Incorrect |
| 1h@10:00 | Sep 11 10:00 → Sep 11 11:00 | BEARISH | 35.30% | Approved | $334.74 | $333.38 | -0.41% | Correct |
| 1h@11:00 | Sep 11 11:00 → Sep 11 12:00 | BEARISH | 35.63% | Approved | $333.40 | $332.50 | -0.27% | Correct |
| 1h@12:00 | Sep 11 12:00 → Sep 11 13:00 | BEARISH | 34.48% | Approved | $332.52 | $332.23 | -0.09% | Correct |
| 1h@13:00 | Sep 11 13:00 → Sep 11 14:00 | BEARISH | 26.86% | Approved | $332.24 | $332.67 | +0.13% | Incorrect |
| 1h@14:00 | Sep 11 14:00 → Sep 11 15:00 | BEARISH | 23.63% | Approved | $332.69 | $332.50 | -0.06% | Correct |
| 1h@15:00 | Sep 11 15:00 → Sep 11 16:00 | BEARISH | 23.32% | Approved | $332.49 | $332.56 | +0.02% | Incorrect |
| 1h@16:00 | Sep 11 16:00 → Sep 11 17:00 | BEARISH | 23.83% | Approved | $332.58 | $332.58 | +0.00% | Incorrect |
| 1h@gap | Sep 10 17:00 → Sep 11 04:00 | BEARISH | 43.09% | Approved | $325.79 | $326.19 | +0.12% | Incorrect |
| 4h@04:00 | Sep 11 04:00 → Sep 11 08:00 | BEARISH | 44.83% | Approved | $326.19 | $334.98 | +2.69% | Incorrect |
| 4h@08:00 | Sep 11 08:00 → Sep 11 12:00 | BEARISH | 44.78% | Approved | $335.02 | $332.50 | -0.75% | Correct |
| 4h@12:00 | Sep 11 12:00 → Sep 11 16:00 | BEARISH | 44.48% | Approved | $332.52 | $332.56 | +0.01% | Incorrect |
| 4h@16:00 | Sep 11 16:00 → Sep 14 07:00 | BEARISH | 45.24% | Approved | $332.58 | — | — | Pending target end |
| 1d@D+1 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 49.51% | Approved | $326.19 | $332.58 | +1.96% | No directional call |
| 1d@D+2 | Sep 14 04:00 → Sep 14 17:00 | BULLISH | 54.35% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 53.80% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 16 04:00 → Sep 16 17:00 | NO_EDGE | 51.93% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 17 04:00 → Sep 17 17:00 | NO_EDGE | 49.87% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 11 04:00 → Sep 17 17:00 | BEARISH | 45.33% | Approved | $326.19 | — | — | Pending target end |

## AMZN

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $251.04–$252.06 | $251.55 | $253.76 | 04:00 | +$2.21 | +0.88% | Outside |
| 05:00 | $251.04–$252.06 | $251.55 | $253.41 | 05:00 | +$1.86 | +0.74% | Outside |
| 06:00 | $251.04–$252.06 | $251.55 | $254.84 | 06:00 | +$3.29 | +1.31% | Outside |
| 07:00 | $250.17–$251.18 | $250.67 | $256.16 | 07:00 | +$5.49 | +2.19% | Outside |
| 08:00 | $250.47–$251.48 | $250.97 | $254.94 | 08:00 | +$3.97 | +1.58% | Outside |
| 09:00 | $250.51–$251.52 | $251.01 | $255.71 | 09:00 | +$4.70 | +1.87% | Outside |
| 10:00 | $250.59–$251.61 | $251.10 | $255.76 | 10:00 | +$4.66 | +1.86% | Outside |
| 11:00 | $250.46–$251.47 | $250.96 | $256.86 | 11:00 | +$5.90 | +2.35% | Outside |
| 12:00 | $250.01–$251.03 | $250.52 | $257.15 | 12:00 | +$6.63 | +2.65% | Outside |
| 13:00 | $250.28–$251.29 | $250.79 | $256.78 | 13:00 | +$5.99 | +2.39% | Outside |
| 14:00 | $250.09–$251.10 | $250.60 | $256.58 | 14:00 | +$5.98 | +2.39% | Outside |
| 15:00 | $250.31–$251.33 | $250.82 | $256.75 | 15:01 | +$5.93 | +2.36% | Outside |
| 16:00 | $250.27–$251.28 | $250.77 | $256.65 | 16:03 | +$5.88 | +2.34% | Outside |
| 17:00 | $250.34–$251.35 | $250.85 | $256.70 | 17:00 | +$5.85 | +2.33% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 11 04:00 → Sep 11 05:00 | BEARISH | 35.36% | Approved | $253.76 | $253.35 | -0.16% | Correct |
| 1h@05:00 | Sep 11 05:00 → Sep 11 06:00 | BEARISH | 36.06% | Approved | $253.41 | $254.82 | +0.56% | Incorrect |
| 1h@06:00 | Sep 11 06:00 → Sep 11 07:00 | BEARISH | 40.61% | Approved | $254.84 | $256.20 | +0.53% | Incorrect |
| 1h@07:00 | Sep 11 07:00 → Sep 11 08:00 | BEARISH | 41.29% | Approved | $256.16 | $254.93 | -0.48% | Correct |
| 1h@08:00 | Sep 11 08:00 → Sep 11 09:00 | BEARISH | 38.19% | Approved | $254.94 | $255.71 | +0.30% | Incorrect |
| 1h@09:00 | Sep 11 09:00 → Sep 11 10:00 | BEARISH | 36.79% | Approved | $255.71 | $255.76 | +0.02% | Incorrect |
| 1h@10:00 | Sep 11 10:00 → Sep 11 11:00 | BEARISH | 33.55% | Approved | $255.76 | $256.84 | +0.42% | Incorrect |
| 1h@11:00 | Sep 11 11:00 → Sep 11 12:00 | BEARISH | 30.41% | Approved | $256.86 | $257.14 | +0.11% | Incorrect |
| 1h@12:00 | Sep 11 12:00 → Sep 11 13:00 | BEARISH | 31.91% | Approved | $257.15 | $256.81 | -0.13% | Correct |
| 1h@13:00 | Sep 11 13:00 → Sep 11 14:00 | BEARISH | 21.17% | Approved | $256.78 | $256.51 | -0.11% | Correct |
| 1h@14:00 | Sep 11 14:00 → Sep 11 15:00 | BEARISH | 16.56% | Approved | $256.58 | $256.78 | +0.08% | Incorrect |
| 1h@15:00 | Sep 11 15:00 → Sep 11 16:00 | BEARISH | 16.31% | Approved | $256.75 | $256.62 | -0.05% | Correct |
| 1h@16:00 | Sep 11 16:00 → Sep 11 17:00 | BEARISH | 17.24% | Approved | $256.65 | $256.70 | +0.02% | Incorrect |
| 1h@gap | Sep 10 17:00 → Sep 11 04:00 | BEARISH | 42.63% | Approved | $251.29 | $253.76 | +0.98% | Incorrect |
| 4h@04:00 | Sep 11 04:00 → Sep 11 08:00 | BEARISH | 44.99% | Approved | $253.76 | $254.93 | +0.46% | Incorrect |
| 4h@08:00 | Sep 11 08:00 → Sep 11 12:00 | BEARISH | 44.79% | Approved | $254.94 | $257.14 | +0.86% | Incorrect |
| 4h@12:00 | Sep 11 12:00 → Sep 11 16:00 | BEARISH | 44.05% | Approved | $257.15 | $256.62 | -0.21% | Correct |
| 4h@16:00 | Sep 11 16:00 → Sep 14 07:00 | BEARISH | 45.49% | Approved | $256.65 | — | — | Pending target end |
| 1d@D+1 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 50.05% | Approved | $253.76 | $256.70 | +1.16% | No directional call |
| 1d@D+2 | Sep 14 04:00 → Sep 14 17:00 | BULLISH | 54.89% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 15 04:00 → Sep 15 17:00 | BULLISH | 54.34% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 16 04:00 → Sep 16 17:00 | NO_EDGE | 52.48% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 17 04:00 → Sep 17 17:00 | NO_EDGE | 50.42% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 11 04:00 → Sep 17 17:00 | BEARISH | 45.64% | Approved | $253.76 | — | — | Pending target end |

## COST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $900.58–$904.20 | $902.39 | $904.67 | 04:00 | +$2.28 | +0.25% | Outside |
| 05:00 | $900.19–$903.81 | $902.00 | $905.25 | 05:01 | +$3.25 | +0.36% | Outside |
| 06:00 | $900.26–$903.88 | $902.07 | $906.88 | 06:00 | +$4.81 | +0.53% | Outside |
| 07:00 | $900.00–$903.62 | $901.81 | $903.65 | 07:00 | +$1.84 | +0.20% | Outside |
| 08:00 | $900.01–$903.63 | $901.82 | $902.92 | 08:00 | +$1.10 | +0.12% | Inside |
| 09:00 | $899.98–$903.60 | $901.79 | $903.35 | 09:00 | +$1.56 | +0.17% | Inside |
| 10:00 | $900.81–$904.43 | $902.62 | $903.87 | 10:00 | +$1.25 | +0.14% | Inside |
| 11:00 | $898.69–$902.30 | $900.49 | $904.10 | 11:00 | +$3.61 | +0.40% | Outside |
| 12:00 | $899.11–$902.72 | $900.91 | $903.41 | 12:01 | +$2.50 | +0.28% | Outside |
| 13:00 | $898.81–$902.43 | $900.62 | $904.46 | 13:00 | +$3.84 | +0.43% | Outside |
| 14:00 | $896.79–$900.39 | $898.59 | — | — | — | — | No observed price within five minutes |
| 15:00 | $895.92–$899.52 | $897.72 | — | — | — | — | No observed price within five minutes |
| 16:00 | $898.65–$902.26 | $900.46 | — | — | — | — | No observed price within five minutes |
| 17:00 | $896.07–$899.67 | $897.87 | $904.99 | 17:00 | +$7.12 | +0.79% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 11 04:00 → Sep 11 05:00 | BEARISH | 42.47% | Approved | $904.67 | — | — | End: No observed price within five minutes |
| 1h@05:00 | Sep 11 05:00 → Sep 11 06:00 | BEARISH | 39.30% | Approved | $905.25 | $906.75 | +0.17% | Incorrect |
| 1h@06:00 | Sep 11 06:00 → Sep 11 07:00 | BEARISH | 42.95% | Approved | $906.88 | $903.65 | -0.36% | Correct |
| 1h@07:00 | Sep 11 07:00 → Sep 11 08:00 | NO_EDGE | 49.07% | Approved | $903.65 | $902.89 | -0.08% | No directional call |
| 1h@08:00 | Sep 11 08:00 → Sep 11 09:00 | BEARISH | 39.74% | Approved | $902.92 | $903.37 | +0.05% | Incorrect |
| 1h@09:00 | Sep 11 09:00 → Sep 11 10:00 | BEARISH | 42.88% | Approved | $903.35 | $904.00 | +0.07% | Incorrect |
| 1h@10:00 | Sep 11 10:00 → Sep 11 11:00 | BEARISH | 39.61% | Approved | $903.87 | $904.02 | +0.02% | Incorrect |
| 1h@11:00 | Sep 11 11:00 → Sep 11 12:00 | BEARISH | 38.27% | Approved | $904.10 | $903.56 | -0.06% | Correct |
| 1h@12:00 | Sep 11 12:00 → Sep 11 13:00 | BEARISH | 37.62% | Approved | $903.41 | $904.75 | +0.15% | Incorrect |
| 1h@13:00 | Sep 11 13:00 → Sep 11 14:00 | BEARISH | 31.17% | Approved | $904.46 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Sep 11 14:00 → Sep 11 15:00 | BEARISH | 26.30% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@15:00 | Sep 11 15:00 → Sep 11 16:00 | BEARISH | 26.19% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@16:00 | Sep 11 16:00 → Sep 11 17:00 | BEARISH | 27.36% | Approved | — | $904.99 | — | Start: No observed price within five minutes |
| 1h@gap | Sep 10 17:00 → Sep 11 04:00 | BEARISH | 38.81% | Approved | — | $904.67 | — | Start: No observed price within five minutes |
| 4h@04:00 | Sep 11 04:00 → Sep 11 08:00 | BEARISH | 44.33% | Approved | $904.67 | $902.89 | -0.20% | Correct |
| 4h@08:00 | Sep 11 08:00 → Sep 11 12:00 | BEARISH | 44.44% | Approved | $902.92 | $903.56 | +0.07% | Incorrect |
| 4h@12:00 | Sep 11 12:00 → Sep 11 16:00 | BEARISH | 43.77% | Approved | $903.41 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Sep 11 16:00 → Sep 14 07:00 | BEARISH | 45.49% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 49.04% | Approved | $904.67 | $904.99 | +0.04% | No directional call |
| 1d@D+2 | Sep 14 04:00 → Sep 14 17:00 | NO_EDGE | 53.88% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 53.34% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 16 04:00 → Sep 16 17:00 | NO_EDGE | 51.47% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 17 04:00 → Sep 17 17:00 | NO_EDGE | 49.40% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 11 04:00 → Sep 17 17:00 | BEARISH | 44.03% | Approved | $904.67 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 14:00 | No observed price within five minutes (nearest Sep 11 14:08; 8 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Sep 11 15:14; 14 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price within five minutes (nearest Sep 11 16:59; 59 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Sep 11 04:44; 16 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Sep 11 13:46; 14 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Sep 11 14:08; 8 minutes away); source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Sep 11 14:52; 8 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Sep 11 15:14; 14 minutes away); source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Sep 11 15:38; 22 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Sep 11 16:59; 59 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Sep 10 16:47; 13 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Sep 11 15:38; 22 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## GOOG

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $327.99–$329.31 | $328.65 | $332.01 | 04:00 | +$3.36 | +1.02% | Outside |
| 05:00 | $327.83–$329.15 | $328.49 | $332.08 | 05:00 | +$3.59 | +1.09% | Outside |
| 06:00 | $327.90–$329.22 | $328.56 | $333.15 | 06:00 | +$4.59 | +1.40% | Outside |
| 07:00 | $327.41–$328.73 | $328.07 | $335.89 | 07:00 | +$7.82 | +2.38% | Outside |
| 08:00 | $327.94–$329.27 | $328.60 | $337.32 | 08:00 | +$8.72 | +2.65% | Outside |
| 09:00 | $327.63–$328.96 | $328.30 | $338.44 | 09:00 | +$10.14 | +3.09% | Outside |
| 10:00 | $327.78–$329.11 | $328.45 | $337.66 | 10:00 | +$9.21 | +2.80% | Outside |
| 11:00 | $327.89–$329.22 | $328.55 | $337.06 | 11:00 | +$8.51 | +2.59% | Outside |
| 12:00 | $327.75–$329.08 | $328.42 | $335.86 | 12:00 | +$7.44 | +2.27% | Outside |
| 13:00 | $328.08–$329.40 | $328.74 | $335.45 | 13:00 | +$6.71 | +2.04% | Outside |
| 14:00 | $327.98–$329.30 | $328.64 | $335.76 | 14:02 | +$7.12 | +2.17% | Outside |
| 15:00 | $328.17–$329.50 | $328.83 | $335.70 | 15:03 | +$6.87 | +2.09% | Outside |
| 16:00 | $327.92–$329.25 | $328.59 | $335.39 | 16:00 | +$6.80 | +2.07% | Outside |
| 17:00 | $328.18–$329.50 | $328.84 | $335.47 | 17:00 | +$6.63 | +2.02% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 11 04:00 → Sep 11 05:00 | BEARISH | 34.24% | Approved | $332.01 | $332.08 | +0.02% | Incorrect |
| 1h@05:00 | Sep 11 05:00 → Sep 11 06:00 | BEARISH | 35.04% | Approved | $332.08 | $333.16 | +0.33% | Incorrect |
| 1h@06:00 | Sep 11 06:00 → Sep 11 07:00 | BEARISH | 38.03% | Approved | $333.15 | $335.92 | +0.83% | Incorrect |
| 1h@07:00 | Sep 11 07:00 → Sep 11 08:00 | BEARISH | 38.55% | Approved | $335.89 | $337.32 | +0.43% | Incorrect |
| 1h@08:00 | Sep 11 08:00 → Sep 11 09:00 | BEARISH | 34.19% | Approved | $337.32 | $338.49 | +0.35% | Incorrect |
| 1h@09:00 | Sep 11 09:00 → Sep 11 10:00 | BEARISH | 32.46% | Approved | $338.44 | $337.65 | -0.23% | Correct |
| 1h@10:00 | Sep 11 10:00 → Sep 11 11:00 | BEARISH | 29.37% | Approved | $337.66 | $337.08 | -0.17% | Correct |
| 1h@11:00 | Sep 11 11:00 → Sep 11 12:00 | BEARISH | 26.89% | Approved | $337.06 | $335.85 | -0.36% | Correct |
| 1h@12:00 | Sep 11 12:00 → Sep 11 13:00 | BEARISH | 28.10% | Approved | $335.86 | $335.49 | -0.11% | Correct |
| 1h@13:00 | Sep 11 13:00 → Sep 11 14:00 | BEARISH | 18.94% | Approved | $335.45 | $335.83 | +0.11% | Incorrect |
| 1h@14:00 | Sep 11 14:00 → Sep 11 15:00 | BEARISH | 16.19% | Approved | $335.76 | $335.70 | -0.02% | Correct |
| 1h@15:00 | Sep 11 15:00 → Sep 11 16:00 | BEARISH | 16.46% | Approved | $335.70 | $335.40 | -0.09% | Correct |
| 1h@16:00 | Sep 11 16:00 → Sep 11 17:00 | BEARISH | 16.27% | Approved | $335.39 | $335.47 | +0.02% | Incorrect |
| 1h@gap | Sep 10 17:00 → Sep 11 04:00 | BEARISH | 40.96% | Approved | $328.92 | $332.01 | +0.94% | Incorrect |
| 4h@04:00 | Sep 11 04:00 → Sep 11 08:00 | BEARISH | 45.13% | Approved | $332.01 | $337.32 | +1.60% | Incorrect |
| 4h@08:00 | Sep 11 08:00 → Sep 11 12:00 | BEARISH | 44.84% | Approved | $337.32 | $335.85 | -0.44% | Correct |
| 4h@12:00 | Sep 11 12:00 → Sep 11 16:00 | BEARISH | 44.48% | Approved | $335.86 | $335.40 | -0.14% | Correct |
| 4h@16:00 | Sep 11 16:00 → Sep 14 07:00 | BEARISH | 45.96% | Approved | $335.39 | — | — | Pending target end |
| 1d@D+1 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 51.28% | Approved | $332.01 | $335.47 | +1.04% | No directional call |
| 1d@D+2 | Sep 14 04:00 → Sep 14 17:00 | BULLISH | 56.09% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 15 04:00 → Sep 15 17:00 | BULLISH | 55.55% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 16 04:00 → Sep 16 17:00 | NO_EDGE | 53.70% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 17 04:00 → Sep 17 17:00 | NO_EDGE | 51.64% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 11 04:00 → Sep 17 17:00 | NO_EDGE | 46.69% | Approved | $332.01 | — | — | Pending target end |

## MU

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $970.46–$974.36 | $972.41 | $985.43 | 04:00 | +$13.02 | +1.34% | Outside |
| 05:00 | $973.25–$977.17 | $975.21 | $988.00 | 05:00 | +$12.79 | +1.31% | Outside |
| 06:00 | $974.69–$978.61 | $976.65 | $994.55 | 06:00 | +$17.90 | +1.83% | Outside |
| 07:00 | $972.96–$976.87 | $974.91 | $975.71 | 07:00 | +$0.80 | +0.08% | Inside |
| 08:00 | $970.78–$974.68 | $972.73 | $973.68 | 08:00 | +$0.95 | +0.10% | Inside |
| 09:00 | $971.17–$975.07 | $973.12 | $979.29 | 09:00 | +$6.17 | +0.63% | Outside |
| 10:00 | $973.53–$977.44 | $975.48 | $976.71 | 10:00 | +$1.23 | +0.13% | Inside |
| 11:00 | $976.31–$980.24 | $978.28 | $977.17 | 11:00 | −$1.11 | -0.11% | Inside |
| 12:00 | $971.44–$975.34 | $973.39 | $974.76 | 12:00 | +$1.37 | +0.14% | Inside |
| 13:00 | $975.23–$979.15 | $977.19 | $974.97 | 13:00 | −$2.22 | -0.23% | Outside |
| 14:00 | $974.25–$978.16 | $976.21 | $974.84 | 14:00 | −$1.37 | -0.14% | Inside |
| 15:00 | $973.86–$977.77 | $975.82 | $974.16 | 15:00 | −$1.66 | -0.17% | Inside |
| 16:00 | $973.09–$977.00 | $975.05 | $974.30 | 16:01 | −$0.75 | -0.08% | Inside |
| 17:00 | $973.13–$977.04 | $975.09 | $975.39 | 17:00 | +$0.30 | +0.03% | Inside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 11 04:00 → Sep 11 05:00 | BEARISH | 35.46% | Approved | $985.43 | $988.00 | +0.26% | Incorrect |
| 1h@05:00 | Sep 11 05:00 → Sep 11 06:00 | BEARISH | 37.87% | Approved | $988.00 | $994.25 | +0.63% | Incorrect |
| 1h@06:00 | Sep 11 06:00 → Sep 11 07:00 | BEARISH | 42.15% | Approved | $994.55 | $975.71 | -1.89% | Correct |
| 1h@07:00 | Sep 11 07:00 → Sep 11 08:00 | BEARISH | 43.96% | Approved | $975.71 | $973.78 | -0.20% | Correct |
| 1h@08:00 | Sep 11 08:00 → Sep 11 09:00 | BEARISH | 42.03% | Approved | $973.68 | $979.35 | +0.58% | Incorrect |
| 1h@09:00 | Sep 11 09:00 → Sep 11 10:00 | BEARISH | 39.40% | Approved | $979.29 | $976.59 | -0.28% | Correct |
| 1h@10:00 | Sep 11 10:00 → Sep 11 11:00 | BEARISH | 39.36% | Approved | $976.71 | $977.01 | +0.03% | Incorrect |
| 1h@11:00 | Sep 11 11:00 → Sep 11 12:00 | BEARISH | 36.89% | Approved | $977.17 | $974.68 | -0.26% | Correct |
| 1h@12:00 | Sep 11 12:00 → Sep 11 13:00 | BEARISH | 41.66% | Approved | $974.76 | $974.94 | +0.02% | Incorrect |
| 1h@13:00 | Sep 11 13:00 → Sep 11 14:00 | BEARISH | 29.45% | Approved | $974.97 | $974.86 | -0.01% | Correct |
| 1h@14:00 | Sep 11 14:00 → Sep 11 15:00 | BEARISH | 27.67% | Approved | $974.84 | $974.28 | -0.06% | Correct |
| 1h@15:00 | Sep 11 15:00 → Sep 11 16:00 | BEARISH | 26.99% | Approved | $974.16 | $974.50 | +0.03% | Incorrect |
| 1h@16:00 | Sep 11 16:00 → Sep 11 17:00 | BEARISH | 26.90% | Approved | $974.30 | $975.39 | +0.11% | Incorrect |
| 1h@gap | Sep 10 17:00 → Sep 11 04:00 | NO_EDGE | 53.35% | Approved | $971.40 | $985.43 | +1.44% | No directional call |
| 4h@04:00 | Sep 11 04:00 → Sep 11 08:00 | BEARISH | 45.32% | Approved | $985.43 | $973.78 | -1.18% | Correct |
| 4h@08:00 | Sep 11 08:00 → Sep 11 12:00 | BEARISH | 45.57% | Approved | $973.68 | $974.68 | +0.10% | Incorrect |
| 4h@12:00 | Sep 11 12:00 → Sep 11 16:00 | BEARISH | 45.54% | Approved | $974.76 | $974.50 | -0.03% | Correct |
| 4h@16:00 | Sep 11 16:00 → Sep 14 07:00 | NO_EDGE | 46.47% | Approved | $974.30 | — | — | Pending target end |
| 1d@D+1 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 49.70% | Approved | $985.43 | $975.39 | -1.02% | No directional call |
| 1d@D+2 | Sep 14 04:00 → Sep 14 17:00 | BULLISH | 54.54% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 54.00% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 16 04:00 → Sep 16 17:00 | NO_EDGE | 52.13% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 17 04:00 → Sep 17 17:00 | NO_EDGE | 50.07% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 11 04:00 → Sep 17 17:00 | NO_EDGE | 46.28% | Approved | $985.43 | — | — | Pending target end |

## NVDA

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $217.66–$218.55 | $218.11 | $220.15 | 04:00 | +$2.04 | +0.94% | Outside |
| 05:00 | $217.75–$218.63 | $218.19 | $219.66 | 05:00 | +$1.47 | +0.67% | Outside |
| 06:00 | $217.90–$218.78 | $218.34 | $221.28 | 06:00 | +$2.94 | +1.35% | Outside |
| 07:00 | $217.98–$218.86 | $218.42 | $221.62 | 07:00 | +$3.20 | +1.47% | Outside |
| 08:00 | $217.92–$218.80 | $218.36 | $219.47 | 08:00 | +$1.11 | +0.51% | Outside |
| 09:00 | $217.37–$218.25 | $217.81 | $219.68 | 09:00 | +$1.87 | +0.86% | Outside |
| 10:00 | $217.30–$218.18 | $217.74 | $219.11 | 10:00 | +$1.37 | +0.63% | Outside |
| 11:00 | $217.25–$218.13 | $217.69 | $219.14 | 11:00 | +$1.45 | +0.67% | Outside |
| 12:00 | $217.17–$218.05 | $217.61 | $219.34 | 12:00 | +$1.73 | +0.80% | Outside |
| 13:00 | $217.82–$218.70 | $218.26 | $218.17 | 13:00 | −$0.09 | -0.04% | Inside |
| 14:00 | $217.53–$218.42 | $217.97 | $218.39 | 14:00 | +$0.42 | +0.19% | Inside |
| 15:00 | $217.43–$218.31 | $217.87 | $218.35 | 15:03 | +$0.48 | +0.22% | Outside |
| 16:00 | $217.16–$218.04 | $217.60 | $218.22 | 16:00 | +$0.62 | +0.28% | Outside |
| 17:00 | $217.15–$218.03 | $217.59 | $218.19 | 16:55 | +$0.60 | +0.28% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 11 04:00 → Sep 11 05:00 | BEARISH | 40.46% | Approved | $220.15 | $219.59 | -0.25% | Correct |
| 1h@05:00 | Sep 11 05:00 → Sep 11 06:00 | BEARISH | 41.46% | Approved | $219.66 | $221.30 | +0.75% | Incorrect |
| 1h@06:00 | Sep 11 06:00 → Sep 11 07:00 | NO_EDGE | 46.07% | Approved | $221.28 | $221.61 | +0.15% | No directional call |
| 1h@07:00 | Sep 11 07:00 → Sep 11 08:00 | NO_EDGE | 47.37% | Approved | $221.62 | $219.47 | -0.97% | No directional call |
| 1h@08:00 | Sep 11 08:00 → Sep 11 09:00 | NO_EDGE | 46.22% | Approved | $219.47 | $219.70 | +0.10% | No directional call |
| 1h@09:00 | Sep 11 09:00 → Sep 11 10:00 | BEARISH | 42.44% | Approved | $219.68 | $219.11 | -0.26% | Correct |
| 1h@10:00 | Sep 11 10:00 → Sep 11 11:00 | BEARISH | 40.59% | Approved | $219.11 | $219.15 | +0.02% | Incorrect |
| 1h@11:00 | Sep 11 11:00 → Sep 11 12:00 | BEARISH | 35.47% | Approved | $219.14 | $219.34 | +0.09% | Incorrect |
| 1h@12:00 | Sep 11 12:00 → Sep 11 13:00 | BEARISH | 39.20% | Approved | $219.34 | $218.18 | -0.53% | Correct |
| 1h@13:00 | Sep 11 13:00 → Sep 11 14:00 | BEARISH | 28.10% | Approved | $218.17 | $218.38 | +0.10% | Incorrect |
| 1h@14:00 | Sep 11 14:00 → Sep 11 15:00 | BEARISH | 21.95% | Approved | $218.39 | $218.37 | -0.01% | Correct |
| 1h@15:00 | Sep 11 15:00 → Sep 11 16:00 | BEARISH | 22.12% | Approved | $218.35 | $218.22 | -0.06% | Correct |
| 1h@16:00 | Sep 11 16:00 → Sep 11 17:00 | BEARISH | 21.48% | Approved | $218.22 | $218.19 | -0.01% | Correct |
| 1h@gap | Sep 10 17:00 → Sep 11 04:00 | NO_EDGE | 52.22% | Approved | $217.79 | $220.15 | +1.08% | No directional call |
| 4h@04:00 | Sep 11 04:00 → Sep 11 08:00 | BEARISH | 44.75% | Approved | $220.15 | $219.47 | -0.31% | Correct |
| 4h@08:00 | Sep 11 08:00 → Sep 11 12:00 | BEARISH | 44.75% | Approved | $219.47 | $219.34 | -0.06% | Correct |
| 4h@12:00 | Sep 11 12:00 → Sep 11 16:00 | BEARISH | 44.36% | Approved | $219.34 | $218.22 | -0.51% | Correct |
| 4h@16:00 | Sep 11 16:00 → Sep 14 07:00 | BEARISH | 45.82% | Approved | $218.22 | — | — | Pending target end |
| 1d@D+1 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 47.78% | Approved | $220.15 | $218.19 | -0.89% | No directional call |
| 1d@D+2 | Sep 14 04:00 → Sep 14 17:00 | NO_EDGE | 52.63% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 52.08% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 16 04:00 → Sep 16 17:00 | NO_EDGE | 50.21% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 17 04:00 → Sep 17 17:00 | NO_EDGE | 48.14% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 11 04:00 → Sep 17 17:00 | BEARISH | 44.54% | Approved | $220.15 | — | — | Pending target end |

## SNDK

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,680.26–$1,687.00 | $1,683.63 | $1,699.36 | 04:00 | +$15.73 | +0.93% | Outside |
| 05:00 | $1,677.52–$1,684.25 | $1,680.89 | $1,702.80 | 05:00 | +$21.91 | +1.30% | Outside |
| 06:00 | $1,685.70–$1,692.47 | $1,689.09 | $1,723.84 | 06:00 | +$34.75 | +2.06% | Outside |
| 07:00 | $1,678.66–$1,685.40 | $1,682.03 | $1,630.47 | 07:00 | −$51.56 | -3.07% | Outside |
| 08:00 | $1,687.64–$1,694.41 | $1,691.03 | $1,634.63 | 08:00 | −$56.40 | -3.34% | Outside |
| 09:00 | $1,691.19–$1,697.98 | $1,694.59 | $1,640.95 | 09:00 | −$53.64 | -3.17% | Outside |
| 10:00 | $1,693.49–$1,700.29 | $1,696.89 | $1,632.36 | 10:00 | −$64.53 | -3.80% | Outside |
| 11:00 | $1,690.84–$1,697.63 | $1,694.24 | $1,634.24 | 11:00 | −$60.00 | -3.54% | Outside |
| 12:00 | $1,686.52–$1,693.29 | $1,689.91 | $1,632.72 | 12:00 | −$57.19 | -3.38% | Outside |
| 13:00 | $1,682.49–$1,689.24 | $1,685.87 | $1,633.24 | 13:00 | −$52.63 | -3.12% | Outside |
| 14:00 | $1,684.57–$1,691.33 | $1,687.95 | $1,630.24 | 14:00 | −$57.71 | -3.42% | Outside |
| 15:00 | $1,680.30–$1,687.05 | $1,683.67 | $1,629.03 | 15:00 | −$54.64 | -3.25% | Outside |
| 16:00 | $1,686.09–$1,692.86 | $1,689.47 | $1,630.19 | 16:00 | −$59.28 | -3.51% | Outside |
| 17:00 | $1,683.69–$1,690.45 | $1,687.07 | $1,631.20 | 17:00 | −$55.87 | -3.31% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 11 04:00 → Sep 11 05:00 | BEARISH | 36.69% | Approved | $1,699.36 | $1,703.00 | +0.21% | Incorrect |
| 1h@05:00 | Sep 11 05:00 → Sep 11 06:00 | BEARISH | 37.67% | Approved | $1,702.80 | $1,723.47 | +1.21% | Incorrect |
| 1h@06:00 | Sep 11 06:00 → Sep 11 07:00 | BEARISH | 40.56% | Approved | $1,723.84 | $1,630.90 | -5.39% | Correct |
| 1h@07:00 | Sep 11 07:00 → Sep 11 08:00 | BEARISH | 41.82% | Approved | $1,630.47 | $1,635.02 | +0.28% | Incorrect |
| 1h@08:00 | Sep 11 08:00 → Sep 11 09:00 | BEARISH | 39.71% | Approved | $1,634.63 | $1,640.51 | +0.36% | Incorrect |
| 1h@09:00 | Sep 11 09:00 → Sep 11 10:00 | BEARISH | 38.34% | Approved | $1,640.95 | $1,632.88 | -0.49% | Correct |
| 1h@10:00 | Sep 11 10:00 → Sep 11 11:00 | BEARISH | 37.38% | Approved | $1,632.36 | $1,633.81 | +0.09% | Incorrect |
| 1h@11:00 | Sep 11 11:00 → Sep 11 12:00 | BEARISH | 34.01% | Approved | $1,634.24 | $1,632.72 | -0.09% | Correct |
| 1h@12:00 | Sep 11 12:00 → Sep 11 13:00 | BEARISH | 37.46% | Approved | $1,632.72 | $1,633.67 | +0.06% | Incorrect |
| 1h@13:00 | Sep 11 13:00 → Sep 11 14:00 | BEARISH | 29.06% | Approved | $1,633.24 | $1,630.02 | -0.20% | Correct |
| 1h@14:00 | Sep 11 14:00 → Sep 11 15:00 | BEARISH | 25.61% | Approved | $1,630.24 | $1,629.01 | -0.08% | Correct |
| 1h@15:00 | Sep 11 15:00 → Sep 11 16:00 | BEARISH | 25.03% | Approved | $1,629.03 | $1,630.19 | +0.07% | Incorrect |
| 1h@16:00 | Sep 11 16:00 → Sep 11 17:00 | BEARISH | 25.69% | Approved | $1,630.19 | $1,631.20 | +0.06% | Incorrect |
| 1h@gap | Sep 10 17:00 → Sep 11 04:00 | NO_EDGE | 48.07% | Approved | $1,676.00 | $1,699.36 | +1.39% | No directional call |
| 4h@04:00 | Sep 11 04:00 → Sep 11 08:00 | BEARISH | 44.87% | Approved | $1,699.36 | $1,635.02 | -3.79% | Correct |
| 4h@08:00 | Sep 11 08:00 → Sep 11 12:00 | BEARISH | 45.00% | Approved | $1,634.63 | $1,632.72 | -0.12% | Correct |
| 4h@12:00 | Sep 11 12:00 → Sep 11 16:00 | BEARISH | 44.82% | Approved | $1,632.72 | $1,630.19 | -0.15% | Correct |
| 4h@16:00 | Sep 11 16:00 → Sep 14 07:00 | BEARISH | 45.81% | Approved | $1,630.19 | — | — | Pending target end |
| 1d@D+1 | Sep 11 04:00 → Sep 11 17:00 | NO_EDGE | 48.90% | Approved | $1,699.36 | $1,631.20 | -4.01% | No directional call |
| 1d@D+2 | Sep 14 04:00 → Sep 14 17:00 | NO_EDGE | 53.74% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Sep 15 04:00 → Sep 15 17:00 | NO_EDGE | 53.19% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Sep 16 04:00 → Sep 16 17:00 | NO_EDGE | 51.32% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Sep 17 04:00 → Sep 17 17:00 | NO_EDGE | 49.26% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Sep 11 04:00 → Sep 17 17:00 | BEARISH | 45.85% | Approved | $1,699.36 | — | — | Pending target end |

Actual prices use the Gameplan's own stock dataset and the existing five-minute boundary tolerance. The 17:00 price is a completed minute's closing price; earlier hourly clocks use opening prices. No missing prices are filled. Longer forecasts continue in the cumulative saved-Gameplan evaluation.

The machine-readable results also retain the model's cost-adjusted target and Brier score, separately from the raw-price direction results above.
