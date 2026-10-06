# Yung Gameplan (YG) results · 2026-10-05

Tomorrow's Gameplan for **2026-10-06** is prepared. All times are Pacific.

The saved price estimates below are compared with actual market prices at the same clock. The ranges were planning estimates; the observations are market prices, not broker fills or trading P/L.

Original forecast: [2026-10-05 Gameplan](C:/DATASTORE/ml/nightly-gameplan-runs/20261003T102959.715605Z/forecasts.parquet).

[Original Gameplan with prices and quantities](C:/DATASTORE/ml/gameplan-trade-plan-runs/20261003T122252.130218Z/Gameplan.md).

**162 evaluated · 66 still pending · 36 missing eligible price observations.**

Direction results compare the saved Bullish/Bearish call with the actual price move. Neutral forecasts have no directional score. Future and missing outcomes are excluded from accuracy.

YG probability scores use a strictly positive raw price return. Cost-adjusted outcomes remain separate.

| Stock | Correct / scored approved calls | Direction accuracy | Mean absolute price error | Prices within range |
| --- | --- | --- | --- | --- |
| AAPL | 7 / 18 | 38.89% | 0.23% | 7 / 14 |
| AMZN | 12 / 18 | 66.67% | 0.31% | 6 / 14 |
| COST | 6 / 13 | 46.15% | 0.29% | 4 / 12 |
| CROX | 6 / 7 | 85.71% | 0.94% | 2 / 7 |
| GOOG | 10 / 18 | 55.56% | 0.70% | 2 / 14 |
| IONQ | 11 / 14 | 78.57% | 1.59% | 2 / 12 |
| MU | 11 / 18 | 61.11% | 0.71% | 2 / 14 |
| NVDA | 11 / 18 | 61.11% | 1.31% | 2 / 14 |
| PATH | 3 / 9 | 33.33% | 0.96% | 1 / 9 |
| SNDK | 14 / 18 | 77.78% | 0.92% | 4 / 14 |
| TWST | 8 / 11 | 72.73% | 5.88% | 0 / 10 |

## AAPL

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $332.79–$334.13 | $333.46 | $333.07 | 04:00 | −$0.39 | -0.12% | Inside |
| 05:00 | $332.77–$334.11 | $333.44 | $332.90 | 05:00 | −$0.54 | -0.16% | Inside |
| 06:00 | $332.81–$334.16 | $333.48 | $331.84 | 06:00 | −$1.64 | -0.49% | Outside |
| 07:00 | $333.14–$334.48 | $333.81 | $333.87 | 07:00 | +$0.06 | +0.02% | Inside |
| 08:00 | $333.22–$334.57 | $333.90 | $333.55 | 08:00 | −$0.35 | -0.11% | Inside |
| 09:00 | $333.39–$334.73 | $334.06 | $333.58 | 09:00 | −$0.48 | -0.14% | Inside |
| 10:00 | $333.37–$334.71 | $334.04 | $334.63 | 10:00 | +$0.59 | +0.18% | Inside |
| 11:00 | $333.52–$334.87 | $334.20 | $332.65 | 11:00 | −$1.55 | -0.46% | Outside |
| 12:00 | $333.41–$334.76 | $334.08 | $333.74 | 12:00 | −$0.34 | -0.10% | Inside |
| 13:00 | $333.34–$334.69 | $334.02 | $333.15 | 13:00 | −$0.87 | -0.26% | Outside |
| 14:00 | $333.09–$334.43 | $333.76 | $332.89 | 14:03 | −$0.87 | -0.26% | Outside |
| 15:00 | $333.09–$334.44 | $333.76 | $332.92 | 15:00 | −$0.84 | -0.25% | Outside |
| 16:00 | $333.33–$334.68 | $334.01 | $332.90 | 16:02 | −$1.11 | -0.33% | Outside |
| 17:00 | $333.18–$334.53 | $333.86 | $332.81 | 16:59 | −$1.05 | -0.31% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BULLISH | 55.06% | Approved | $333.07 | $332.88 | -0.06% | Incorrect |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BULLISH | 51.72% | Approved | $332.90 | $331.77 | -0.34% | Incorrect |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 51.72% | Approved | $331.84 | $333.92 | +0.63% | Correct |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 50.87% | Approved | $333.87 | $333.54 | -0.10% | Incorrect |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 52.74% | Approved | $333.55 | $333.57 | +0.01% | Correct |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 52.24% | Approved | $333.58 | $334.60 | +0.31% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 52.24% | Approved | $334.63 | $332.65 | -0.59% | Incorrect |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 52.36% | Approved | $332.65 | $333.71 | +0.32% | Correct |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BULLISH | 51.66% | Approved | $333.74 | $333.13 | -0.18% | Incorrect |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BEARISH | 48.05% | Approved | $333.15 | $332.92 | -0.07% | Correct |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 41.42% | Approved | $332.89 | $332.89 | +0.00% | Incorrect |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 51.95% | Approved | $332.92 | $332.90 | -0.01% | Incorrect |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 51.95% | Approved | $332.90 | $332.81 | -0.03% | Incorrect |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 50.90% | Approved | $333.53 | $333.07 | -0.14% | Incorrect |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BULLISH | 50.96% | Approved | $333.07 | $333.54 | +0.14% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 52.46% | Approved | $333.55 | $333.71 | +0.05% | Correct |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 51.15% | Approved | $333.74 | $332.90 | -0.25% | Incorrect |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 52.36% | Approved | $332.90 | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 58.17% | Approved | $333.07 | $332.81 | -0.08% | Incorrect |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 55.88% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 53.47% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 52.86% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 54.46% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 55.28% | Approved | $333.07 | — | — | Pending target end |

## AMZN

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $251.18–$252.20 | $251.69 | $250.94 | 04:00 | −$0.75 | -0.30% | Outside |
| 05:00 | $251.31–$252.32 | $251.82 | $251.25 | 05:00 | −$0.57 | -0.23% | Outside |
| 06:00 | $251.26–$252.27 | $251.76 | $251.55 | 06:00 | −$0.21 | -0.08% | Inside |
| 07:00 | $250.68–$251.70 | $251.19 | $252.05 | 07:00 | +$0.86 | +0.34% | Outside |
| 08:00 | $250.71–$251.73 | $251.22 | $251.80 | 08:00 | +$0.58 | +0.23% | Outside |
| 09:00 | $250.89–$251.90 | $251.39 | $252.10 | 09:00 | +$0.71 | +0.28% | Outside |
| 10:00 | $250.80–$251.81 | $251.30 | $252.28 | 10:00 | +$0.98 | +0.39% | Outside |
| 11:00 | $250.82–$251.84 | $251.33 | $253.77 | 11:00 | +$2.44 | +0.97% | Outside |
| 12:00 | $250.81–$251.82 | $251.31 | $253.68 | 12:00 | +$2.37 | +0.94% | Outside |
| 13:00 | $250.95–$251.97 | $251.46 | $251.56 | 13:00 | +$0.10 | +0.04% | Inside |
| 14:00 | $250.84–$251.86 | $251.35 | $251.60 | 14:00 | +$0.25 | +0.10% | Inside |
| 15:00 | $250.82–$251.83 | $251.32 | $251.67 | 15:00 | +$0.35 | +0.14% | Inside |
| 16:00 | $250.94–$251.95 | $251.45 | $251.86 | 16:00 | +$0.41 | +0.16% | Inside |
| 17:00 | $251.06–$252.08 | $251.57 | $251.88 | 16:59 | +$0.31 | +0.12% | Inside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BULLISH | 53.56% | Approved | $250.94 | $251.28 | +0.14% | Correct |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BULLISH | 51.82% | Approved | $251.25 | $251.55 | +0.12% | Correct |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 51.82% | Approved | $251.55 | $252.09 | +0.21% | Correct |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 51.19% | Approved | $252.05 | $251.78 | -0.11% | Incorrect |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 52.93% | Approved | $251.80 | $252.10 | +0.12% | Correct |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 52.23% | Approved | $252.10 | $252.26 | +0.06% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 52.23% | Approved | $252.28 | $253.77 | +0.59% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 52.35% | Approved | $253.77 | $253.67 | -0.04% | Incorrect |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BULLISH | 51.78% | Approved | $253.68 | $251.52 | -0.85% | Incorrect |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BULLISH | 50.54% | Approved | $251.56 | $251.61 | +0.02% | Correct |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 45.64% | Approved | $251.60 | $251.66 | +0.02% | Incorrect |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 52.08% | Approved | $251.67 | $251.86 | +0.08% | Correct |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 52.08% | Approved | $251.86 | $251.88 | +0.01% | Correct |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 52.56% | Approved | $251.37 | $250.94 | -0.17% | Incorrect |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BULLISH | 50.49% | Approved | $250.94 | $251.78 | +0.34% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 51.99% | Approved | $251.80 | $253.67 | +0.74% | Correct |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 51.15% | Approved | $253.68 | $251.86 | -0.72% | Incorrect |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 51.89% | Approved | $251.86 | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 57.67% | Approved | $250.94 | $251.88 | +0.37% | Correct |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 55.37% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 52.95% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 52.35% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 53.95% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 53.54% | Approved | $250.94 | — | — | Pending target end |

## COST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $917.68–$921.36 | $919.52 | $917.66 | 04:00 | −$1.86 | -0.20% | Outside |
| 05:00 | $917.60–$921.29 | $919.45 | — | — | — | — | No observed price within five minutes |
| 06:00 | $917.72–$921.40 | $919.56 | $919.16 | 06:03 | −$0.40 | -0.04% | Inside |
| 07:00 | $917.46–$921.15 | $919.30 | $918.14 | 07:00 | −$1.16 | -0.13% | Inside |
| 08:00 | $917.54–$921.23 | $919.39 | $921.01 | 08:00 | +$1.62 | +0.18% | Inside |
| 09:00 | $917.84–$921.52 | $919.68 | $922.37 | 09:00 | +$2.69 | +0.29% | Outside |
| 10:00 | $918.06–$921.75 | $919.91 | $923.17 | 10:00 | +$3.26 | +0.35% | Outside |
| 11:00 | $917.63–$921.32 | $919.47 | $923.51 | 11:01 | +$4.04 | +0.44% | Outside |
| 12:00 | $917.51–$921.20 | $919.36 | $920.74 | 12:00 | +$1.38 | +0.15% | Inside |
| 13:00 | $917.41–$921.10 | $919.25 | $923.52 | 13:00 | +$4.27 | +0.46% | Outside |
| 14:00 | $917.45–$921.14 | $919.30 | — | — | — | — | No observed price within five minutes |
| 15:00 | $917.24–$920.92 | $919.08 | $922.89 | 15:00 | +$3.81 | +0.41% | Outside |
| 16:00 | $916.83–$920.51 | $918.67 | $922.93 | 16:02 | +$4.26 | +0.46% | Outside |
| 17:00 | $917.43–$921.12 | $919.27 | $922.04 | 16:58 | +$2.77 | +0.30% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BULLISH | 55.54% | Approved | $917.66 | — | — | End: No observed price within five minutes |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BULLISH | 52.43% | Approved | — | $918.77 | — | Start: No observed price within five minutes |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 52.43% | Approved | $919.16 | $918.12 | -0.11% | Incorrect |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BEARISH | 49.20% | Approved | $918.14 | $921.16 | +0.33% | Incorrect |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 51.10% | Approved | $921.01 | $922.29 | +0.14% | Correct |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 50.77% | Approved | $922.37 | $923.17 | +0.09% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 50.77% | Approved | $923.17 | $923.21 | +0.00% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 50.89% | Approved | $923.51 | $920.83 | -0.29% | Incorrect |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BEARISH | 49.94% | Approved | $920.74 | $923.72 | +0.32% | Incorrect |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BEARISH | 45.41% | Approved | $923.52 | $923.00 | -0.06% | Correct |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 39.36% | Approved | — | $922.66 | — | Start: No observed price within five minutes |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 50.15% | Approved | $922.89 | — | — | End: No observed price within five minutes |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 50.90% | Approved | $922.93 | $922.04 | -0.10% | Incorrect |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 50.27% | Approved | $919.55 | $917.66 | -0.21% | Incorrect |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BULLISH | 50.01% | Approved | $917.66 | $921.16 | +0.38% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 50.80% | Approved | $921.01 | $920.83 | -0.02% | Incorrect |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 50.13% | Approved | $920.74 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 52.05% | Approved | $922.93 | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 52.92% | Approved | $917.66 | $922.04 | +0.48% | Correct |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 50.58% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 48.15% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 47.55% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 49.15% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 54.21% | Approved | $917.66 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 05:00 | No observed price within five minutes (nearest Oct 05 05:30; 30 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price within five minutes (nearest Oct 05 14:25; 25 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Oct 05 04:47; 13 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 05 05:30; 30 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Oct 05 14:25; 25 minutes away); source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 05 15:49; 11 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 05 15:49; 11 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## CROX

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $117.89–$118.37 | $118.13 | — | — | — | — | No observed price within five minutes |
| 05:00 | $118.02–$118.51 | $118.26 | — | — | — | — | No observed price within five minutes |
| 06:00 | $117.92–$118.41 | $118.17 | — | — | — | — | No observed price within five minutes |
| 07:00 | $117.82–$118.30 | $118.06 | $118.02 | 07:00 | −$0.04 | -0.03% | Inside |
| 08:00 | $117.88–$118.36 | $118.12 | $118.06 | 08:00 | −$0.06 | -0.05% | Inside |
| 09:00 | $117.75–$118.23 | $117.99 | $119.32 | 09:00 | +$1.33 | +1.13% | Outside |
| 10:00 | $117.74–$118.22 | $117.98 | $119.92 | 10:00 | +$1.94 | +1.64% | Outside |
| 11:00 | $117.60–$118.09 | $117.85 | $120.00 | 11:00 | +$2.15 | +1.82% | Outside |
| 12:00 | $117.52–$118.00 | $117.76 | $119.47 | 12:00 | +$1.71 | +1.45% | Outside |
| 13:00 | $117.43–$117.91 | $117.67 | $118.19 | 13:00 | +$0.52 | +0.44% | Outside |
| 14:00 | $117.41–$117.89 | $117.65 | — | — | — | — | No observed price on the required side of the clock |
| 15:00 | $117.03–$117.51 | $117.27 | — | — | — | — | No observed price on the required side of the clock |
| 16:00 | $118.22–$118.70 | $118.46 | — | — | — | — | No observed price on the required side of the clock |
| 17:00 | $117.54–$118.02 | $117.78 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BULLISH | 51.93% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BULLISH | 50.68% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 50.68% | Approved | — | $117.95 | — | Start: No observed price within five minutes |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 50.72% | Approved | $118.02 | $118.05 | +0.03% | Correct |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 53.27% | Approved | $118.06 | $119.32 | +1.07% | Correct |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 51.50% | Approved | $119.32 | $119.98 | +0.55% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 51.50% | Approved | $119.92 | $119.98 | +0.05% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 51.62% | Approved | $120.00 | $119.44 | -0.47% | Incorrect |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BEARISH | 49.44% | Approved | $119.47 | $118.22 | -1.05% | Correct |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BEARISH | 49.51% | Approved | $118.19 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BULLISH | 51.10% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BEARISH | 49.18% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 51.36% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 56.87% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BEARISH | 49.58% | Approved | — | $118.05 | — | Start: No observed price within five minutes |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 54.85% | Approved | $118.06 | $119.44 | +1.17% | Correct |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 50.61% | Approved | $119.47 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 51.88% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 57.05% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 54.74% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 52.32% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 51.72% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 53.32% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 55.04% | Approved | — | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 04:00 | No observed price within five minutes (nearest Oct 05 06:30; 150 minutes away); source request covers the full tolerance window |
| Price 05:00 | No observed price within five minutes (nearest Oct 05 06:30; 90 minutes away); source request covers the full tolerance window |
| Price 06:00 | No observed price within five minutes (nearest Oct 05 06:30; 30 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 15:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 16:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 05 13:01; 239 minutes away); source request covers the full tolerance window |
| 1h@04:00 start | No observed price within five minutes (nearest Oct 05 06:30; 150 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Oct 02 13:01; 3839 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 05 06:30; 90 minutes away); source request covers the full tolerance window |
| 1h@05:00 end | No observed price within five minutes (nearest Oct 02 13:01; 3899 minutes away); source request covers the full tolerance window |
| 1h@06:00 start | No observed price within five minutes (nearest Oct 05 06:30; 30 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 05 13:01; 59 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 05 13:01; 119 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 05 13:01; 179 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 05 13:01; 239 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 02 13:01; 239 minutes away); source request covers the full tolerance window |
| 1h@gap end | No observed price within five minutes (nearest Oct 05 06:30; 150 minutes away); source request covers the full tolerance window |
| 4h@04:00 start | No observed price within five minutes (nearest Oct 05 06:30; 150 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 05 13:01; 179 minutes away); source request covers the full tolerance window |
| 1d@D+1 start | No observed price within five minutes (nearest Oct 05 06:30; 150 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 05 13:01; 239 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## GOOG

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $339.61–$340.98 | $340.30 | $339.84 | 04:00 | −$0.46 | -0.14% | Inside |
| 05:00 | $339.52–$340.89 | $340.21 | $339.92 | 05:00 | −$0.29 | -0.09% | Inside |
| 06:00 | $339.61–$340.98 | $340.30 | $339.23 | 06:00 | −$1.07 | -0.31% | Outside |
| 07:00 | $338.94–$340.31 | $339.62 | $341.14 | 07:00 | +$1.52 | +0.45% | Outside |
| 08:00 | $339.17–$340.54 | $339.86 | $340.86 | 08:00 | +$1.00 | +0.29% | Outside |
| 09:00 | $339.36–$340.73 | $340.04 | $341.73 | 09:00 | +$1.69 | +0.50% | Outside |
| 10:00 | $339.38–$340.75 | $340.07 | $341.94 | 10:00 | +$1.87 | +0.55% | Outside |
| 11:00 | $339.28–$340.65 | $339.96 | $344.20 | 11:00 | +$4.24 | +1.25% | Outside |
| 12:00 | $339.15–$340.52 | $339.84 | $344.42 | 12:00 | +$4.58 | +1.35% | Outside |
| 13:00 | $339.36–$340.73 | $340.04 | $343.83 | 13:00 | +$3.79 | +1.11% | Outside |
| 14:00 | $339.31–$340.68 | $340.00 | $343.44 | 14:00 | +$3.44 | +1.01% | Outside |
| 15:00 | $339.40–$340.77 | $340.09 | $343.60 | 15:00 | +$3.51 | +1.03% | Outside |
| 16:00 | $339.87–$341.24 | $340.55 | $343.35 | 16:01 | +$2.80 | +0.82% | Outside |
| 17:00 | $339.50–$340.87 | $340.19 | $343.43 | 17:00 | +$3.24 | +0.95% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BULLISH | 53.69% | Approved | $339.84 | $339.89 | +0.01% | Correct |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BULLISH | 51.77% | Approved | $339.92 | $339.25 | -0.20% | Incorrect |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 51.77% | Approved | $339.23 | $341.26 | +0.60% | Correct |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 50.92% | Approved | $341.14 | $340.83 | -0.09% | Incorrect |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 52.88% | Approved | $340.86 | $341.70 | +0.25% | Correct |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 52.18% | Approved | $341.73 | $341.93 | +0.06% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 52.18% | Approved | $341.94 | $344.20 | +0.66% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 52.30% | Approved | $344.20 | $344.42 | +0.06% | Correct |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BULLISH | 51.59% | Approved | $344.42 | $343.89 | -0.15% | Incorrect |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BULLISH | 50.35% | Approved | $343.83 | $343.45 | -0.11% | Incorrect |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 46.92% | Approved | $343.44 | $343.57 | +0.04% | Incorrect |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 51.89% | Approved | $343.60 | $343.40 | -0.06% | Incorrect |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 51.89% | Approved | $343.35 | $343.43 | +0.02% | Correct |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 51.02% | Approved | $340.00 | $339.84 | -0.05% | Incorrect |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BULLISH | 50.45% | Approved | $339.84 | $340.83 | +0.29% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 51.56% | Approved | $340.86 | $344.42 | +1.04% | Correct |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 50.57% | Approved | $344.42 | $343.40 | -0.30% | Incorrect |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 52.05% | Approved | $343.35 | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 57.90% | Approved | $339.84 | $343.43 | +1.06% | Correct |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 55.60% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 53.19% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 52.58% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 54.18% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 55.39% | Approved | $339.84 | — | — | Pending target end |

## IONQ

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $43.82–$44.00 | $43.91 | $43.96 | 04:00 | +$0.05 | +0.11% | Inside |
| 05:00 | $43.85–$44.04 | $43.94 | $43.87 | 05:01 | −$0.07 | -0.16% | Inside |
| 06:00 | $43.87–$44.06 | $43.96 | $43.76 | 06:00 | −$0.20 | -0.45% | Outside |
| 07:00 | $43.73–$43.91 | $43.82 | $42.98 | 07:00 | −$0.84 | -1.92% | Outside |
| 08:00 | $43.70–$43.88 | $43.79 | $42.61 | 08:00 | −$1.18 | -2.69% | Outside |
| 09:00 | $43.71–$43.90 | $43.81 | $43.04 | 09:00 | −$0.77 | -1.76% | Outside |
| 10:00 | $43.64–$43.82 | $43.73 | $42.71 | 10:00 | −$1.02 | -2.33% | Outside |
| 11:00 | $43.65–$43.83 | $43.74 | $42.52 | 11:00 | −$1.22 | -2.79% | Outside |
| 12:00 | $43.66–$43.85 | $43.76 | $43.22 | 12:00 | −$0.54 | -1.23% | Outside |
| 13:00 | $43.63–$43.81 | $43.72 | $43.00 | 13:00 | −$0.72 | -1.65% | Outside |
| 14:00 | $43.69–$43.87 | $43.78 | $42.92 | 14:02 | −$0.86 | -1.96% | Outside |
| 15:00 | $43.65–$43.83 | $43.74 | $42.88 | 15:02 | −$0.86 | -1.97% | Outside |
| 16:00 | $43.77–$43.96 | $43.86 | — | — | — | — | No observed price within five minutes |
| 17:00 | $43.70–$43.88 | $43.79 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BEARISH | 46.41% | Approved | $43.96 | $43.88 | -0.18% | Correct |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BEARISH | 47.41% | Approved | $43.87 | $43.72 | -0.34% | Correct |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BEARISH | 47.41% | Approved | $43.76 | $43.03 | -1.67% | Correct |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BEARISH | 48.64% | Approved | $42.98 | $42.60 | -0.88% | Correct |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BEARISH | 49.58% | Approved | $42.61 | $43.05 | +1.03% | Incorrect |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BEARISH | 49.07% | Approved | $43.04 | $42.72 | -0.74% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BEARISH | 49.07% | Approved | $42.71 | $42.51 | -0.47% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BEARISH | 49.36% | Approved | $42.52 | $43.23 | +1.67% | Incorrect |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BEARISH | 48.74% | Approved | $43.22 | $42.99 | -0.53% | Correct |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BEARISH | 45.33% | Approved | $43.00 | $42.95 | -0.12% | Correct |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 42.22% | Approved | $42.92 | $42.88 | -0.09% | Correct |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BEARISH | 49.28% | Approved | $42.88 | — | — | End: No observed price within five minutes |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BEARISH | 49.28% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 56.48% | Approved | $43.91 | $43.96 | +0.11% | Correct |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BEARISH | 47.95% | Approved | $43.96 | $42.60 | -3.09% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BEARISH | 49.66% | Approved | $42.61 | $43.23 | +1.46% | Incorrect |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BEARISH | 49.67% | Approved | $43.22 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BEARISH | 48.00% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 53.68% | Approved | $43.96 | — | — | End: No observed price within five minutes |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 51.35% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 48.91% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 48.31% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 49.91% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 55.30% | Approved | $43.96 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 16:00 | No observed price within five minutes (nearest Oct 05 16:08; 8 minutes away); source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 05 16:51; 9 minutes away); source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 05 15:54; 6 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Oct 05 16:08; 8 minutes away); source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 05 16:51; 9 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 05 15:54; 6 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 05 16:51; 9 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## MU

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,068.31–$1,072.60 | $1,070.45 | $1,072.64 | 04:00 | +$2.19 | +0.20% | Outside |
| 05:00 | $1,069.14–$1,073.43 | $1,071.28 | $1,070.01 | 05:00 | −$1.27 | -0.12% | Inside |
| 06:00 | $1,069.10–$1,073.40 | $1,071.25 | $1,070.82 | 06:00 | −$0.43 | -0.04% | Inside |
| 07:00 | $1,068.34–$1,072.63 | $1,070.49 | $1,059.39 | 07:00 | −$11.10 | -1.04% | Outside |
| 08:00 | $1,068.78–$1,073.07 | $1,070.92 | $1,068.66 | 08:00 | −$2.26 | -0.21% | Outside |
| 09:00 | $1,068.98–$1,073.28 | $1,071.13 | $1,063.10 | 09:00 | −$8.03 | -0.75% | Outside |
| 10:00 | $1,069.42–$1,073.71 | $1,071.57 | $1,066.99 | 10:00 | −$4.58 | -0.43% | Outside |
| 11:00 | $1,070.10–$1,074.39 | $1,072.25 | $1,062.02 | 11:00 | −$10.23 | -0.95% | Outside |
| 12:00 | $1,069.35–$1,073.65 | $1,071.50 | $1,058.57 | 12:00 | −$12.93 | -1.21% | Outside |
| 13:00 | $1,070.93–$1,075.23 | $1,073.08 | $1,063.96 | 13:00 | −$9.12 | -0.85% | Outside |
| 14:00 | $1,072.22–$1,076.53 | $1,074.37 | $1,063.70 | 14:01 | −$10.67 | -0.99% | Outside |
| 15:00 | $1,072.05–$1,076.36 | $1,074.21 | $1,062.88 | 15:00 | −$11.33 | -1.05% | Outside |
| 16:00 | $1,074.40–$1,078.72 | $1,076.56 | $1,063.69 | 16:00 | −$12.87 | -1.20% | Outside |
| 17:00 | $1,071.60–$1,075.91 | $1,073.75 | $1,064.35 | 17:00 | −$9.40 | -0.88% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BULLISH | 52.57% | Approved | $1,072.64 | $1,070.25 | -0.22% | Incorrect |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BULLISH | 51.02% | Approved | $1,070.01 | $1,070.75 | +0.07% | Correct |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 51.02% | Approved | $1,070.82 | $1,059.62 | -1.05% | Incorrect |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 51.29% | Approved | $1,059.39 | $1,068.56 | +0.87% | Correct |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 54.88% | Approved | $1,068.66 | $1,063.10 | -0.52% | Incorrect |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 52.08% | Approved | $1,063.10 | $1,067.00 | +0.37% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 52.08% | Approved | $1,066.99 | $1,062.06 | -0.46% | Incorrect |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 52.20% | Approved | $1,062.02 | $1,058.64 | -0.32% | Incorrect |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BULLISH | 51.48% | Approved | $1,058.57 | $1,064.00 | +0.51% | Correct |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BEARISH | 49.59% | Approved | $1,063.96 | $1,063.78 | -0.02% | Correct |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 49.46% | Approved | $1,063.70 | $1,062.72 | -0.09% | Correct |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 51.93% | Approved | $1,062.88 | $1,063.70 | +0.08% | Correct |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 51.93% | Approved | $1,063.69 | $1,064.35 | +0.06% | Correct |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 58.81% | Approved | $1,069.15 | $1,072.64 | +0.33% | Correct |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BEARISH | 49.78% | Approved | $1,072.64 | $1,068.56 | -0.38% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 53.43% | Approved | $1,068.66 | $1,058.64 | -0.94% | Incorrect |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 50.81% | Approved | $1,058.57 | $1,063.70 | +0.48% | Correct |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 51.88% | Approved | $1,063.69 | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 53.06% | Approved | $1,072.64 | $1,064.35 | -0.77% | Incorrect |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 50.73% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 48.29% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 47.69% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 49.29% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 55.63% | Approved | $1,072.64 | — | — | Pending target end |

## NVDA

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $234.20–$235.14 | $234.67 | $235.41 | 04:00 | +$0.74 | +0.32% | Outside |
| 05:00 | $234.42–$235.37 | $234.90 | $235.12 | 05:00 | +$0.22 | +0.09% | Inside |
| 06:00 | $234.42–$235.37 | $234.90 | $235.37 | 06:00 | +$0.47 | +0.20% | Inside |
| 07:00 | $234.24–$235.19 | $234.72 | $237.26 | 07:00 | +$2.54 | +1.08% | Outside |
| 08:00 | $234.41–$235.36 | $234.88 | $237.29 | 08:00 | +$2.41 | +1.03% | Outside |
| 09:00 | $234.19–$235.14 | $234.66 | $236.81 | 09:00 | +$2.15 | +0.92% | Outside |
| 10:00 | $234.16–$235.11 | $234.63 | $236.84 | 10:00 | +$2.21 | +0.94% | Outside |
| 11:00 | $234.18–$235.13 | $234.66 | $237.58 | 11:00 | +$2.92 | +1.24% | Outside |
| 12:00 | $234.31–$235.26 | $234.79 | $239.42 | 12:00 | +$4.63 | +1.97% | Outside |
| 13:00 | $234.35–$235.30 | $234.83 | $239.10 | 13:00 | +$4.27 | +1.82% | Outside |
| 14:00 | $234.23–$235.17 | $234.70 | $239.54 | 14:00 | +$4.84 | +2.06% | Outside |
| 15:00 | $234.02–$234.97 | $234.49 | $239.50 | 15:00 | +$5.01 | +2.14% | Outside |
| 16:00 | $234.17–$235.12 | $234.64 | $239.53 | 16:01 | +$4.89 | +2.08% | Outside |
| 17:00 | $233.82–$234.77 | $234.30 | $240.19 | 17:00 | +$5.89 | +2.51% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BULLISH | 56.95% | Approved | $235.41 | $235.07 | -0.14% | Incorrect |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BULLISH | 50.31% | Approved | $235.12 | $235.40 | +0.12% | Correct |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 50.31% | Approved | $235.37 | $237.28 | +0.81% | Correct |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BEARISH | 49.67% | Approved | $237.26 | $237.29 | +0.01% | Incorrect |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 51.84% | Approved | $237.29 | $236.80 | -0.21% | Incorrect |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 51.14% | Approved | $236.81 | $236.84 | +0.01% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 51.14% | Approved | $236.84 | $237.59 | +0.31% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 50.96% | Approved | $237.58 | $239.41 | +0.77% | Correct |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BULLISH | 50.42% | Approved | $239.42 | $239.08 | -0.14% | Incorrect |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BEARISH | 49.61% | Approved | $239.10 | $239.54 | +0.18% | Incorrect |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 44.70% | Approved | $239.54 | $239.51 | -0.01% | Correct |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 50.64% | Approved | $239.50 | $239.53 | +0.01% | Correct |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 50.64% | Approved | $239.53 | $240.19 | +0.28% | Correct |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BEARISH | 49.79% | Approved | $234.23 | $235.41 | +0.50% | Incorrect |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BULLISH | 50.24% | Approved | $235.41 | $237.29 | +0.80% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 51.60% | Approved | $237.29 | $239.41 | +0.89% | Correct |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BEARISH | 49.33% | Approved | $239.42 | $239.53 | +0.05% | Incorrect |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 51.65% | Approved | $239.53 | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 55.99% | Approved | $235.41 | $240.19 | +2.03% | Correct |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 53.67% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 51.24% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 50.63% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 52.24% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 54.51% | Approved | $235.41 | — | — | Pending target end |

## PATH

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $13.08–$13.14 | $13.11 | — | — | — | — | No observed price within five minutes |
| 05:00 | $13.08–$13.14 | $13.11 | — | — | — | — | No observed price within five minutes |
| 06:00 | $13.08–$13.14 | $13.11 | $13.15 | 06:00 | +$0.04 | +0.31% | Outside |
| 07:00 | $13.05–$13.12 | $13.08 | $13.01 | 07:00 | −$0.07 | -0.57% | Outside |
| 08:00 | $13.06–$13.12 | $13.09 | $12.89 | 08:00 | −$0.20 | -1.53% | Outside |
| 09:00 | $13.06–$13.13 | $13.10 | $12.89 | 09:00 | −$0.21 | -1.56% | Outside |
| 10:00 | $13.05–$13.12 | $13.09 | $12.88 | 10:00 | −$0.21 | -1.60% | Outside |
| 11:00 | $13.04–$13.11 | $13.07 | $12.87 | 11:00 | −$0.21 | -1.57% | Outside |
| 12:00 | $13.04–$13.10 | $13.07 | $13.05 | 12:00 | −$0.02 | -0.15% | Inside |
| 13:00 | $13.05–$13.11 | $13.08 | $13.15 | 13:04 | +$0.07 | +0.54% | Outside |
| 14:00 | $13.05–$13.11 | $13.08 | $13.18 | 14:00 | +$0.10 | +0.76% | Outside |
| 15:00 | $13.02–$13.08 | $13.05 | — | — | — | — | No observed price within five minutes |
| 16:00 | $13.05–$13.11 | $13.08 | — | — | — | — | No observed price within five minutes |
| 17:00 | $13.05–$13.11 | $13.08 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BEARISH | 45.84% | Approved | — | $13.13 | — | Start: No observed price within five minutes |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BEARISH | 49.58% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BULLISH | 50.17% | Approved | $13.15 | $13.03 | -0.91% | Incorrect |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 51.33% | Approved | $13.01 | $12.90 | -0.81% | Incorrect |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BEARISH | 49.85% | Approved | $12.89 | $12.89 | +0.04% | Incorrect |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 51.37% | Approved | $12.89 | $12.88 | -0.08% | Incorrect |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BEARISH | 49.34% | Approved | $12.88 | $12.87 | -0.12% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BEARISH | 49.46% | Approved | $12.87 | $13.05 | +1.44% | Incorrect |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BEARISH | 49.55% | Approved | $13.05 | $13.16 | +0.88% | Incorrect |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BEARISH | 45.29% | Approved | $13.15 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 40.58% | Approved | $13.18 | — | — | End: No observed price within five minutes |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 50.19% | Approved | — | $13.18 | — | Start: No observed price within five minutes |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BEARISH | 49.92% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 57.86% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BEARISH | 49.09% | Approved | — | $12.90 | — | Start: No observed price within five minutes |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 54.61% | Approved | $12.89 | $13.05 | +1.24% | Correct |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 50.13% | Approved | $13.05 | $13.18 | +1.00% | Correct |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 51.63% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BEARISH | 49.68% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 47.34% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 44.92% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 44.32% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BEARISH | 45.91% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 53.82% | Approved | — | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 04:00 | No observed price within five minutes (nearest Oct 05 04:22; 22 minutes away); source request covers the full tolerance window |
| Price 05:00 | No observed price within five minutes (nearest Oct 05 05:12; 12 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 05 15:07; 7 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price within five minutes (nearest Oct 05 16:06; 6 minutes away); source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 05 16:31; 29 minutes away); source request covers the full tolerance window |
| 1h@04:00 start | No observed price within five minutes (nearest Oct 05 04:22; 22 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 05 05:12; 12 minutes away); source request covers the full tolerance window |
| 1h@05:00 end | No observed price within five minutes (nearest Oct 05 05:44; 16 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 05 13:50; 10 minutes away); source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 05 14:53; 7 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 05 15:07; 7 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Oct 05 16:06; 6 minutes away); source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 05 16:31; 29 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 02 16:33; 27 minutes away); source request covers the full tolerance window |
| 1h@gap end | No observed price within five minutes (nearest Oct 05 04:22; 22 minutes away); source request covers the full tolerance window |
| 4h@04:00 start | No observed price within five minutes (nearest Oct 05 04:22; 22 minutes away); source request covers the full tolerance window |
| 1d@D+1 start | No observed price within five minutes (nearest Oct 05 04:22; 22 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 05 16:31; 29 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## SNDK

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,721.61–$1,728.53 | $1,725.07 | $1,725.19 | 04:00 | +$0.12 | +0.01% | Inside |
| 05:00 | $1,721.74–$1,728.65 | $1,725.19 | $1,725.81 | 05:00 | +$0.62 | +0.04% | Inside |
| 06:00 | $1,724.51–$1,731.43 | $1,727.97 | $1,726.00 | 06:00 | −$1.97 | -0.11% | Inside |
| 07:00 | $1,720.90–$1,727.80 | $1,724.35 | $1,718.91 | 07:00 | −$5.44 | -0.32% | Outside |
| 08:00 | $1,720.14–$1,727.05 | $1,723.59 | $1,725.70 | 08:00 | +$2.11 | +0.12% | Inside |
| 09:00 | $1,719.87–$1,726.78 | $1,723.33 | $1,708.80 | 09:00 | −$14.53 | -0.84% | Outside |
| 10:00 | $1,723.81–$1,730.73 | $1,727.27 | $1,710.25 | 10:00 | −$17.02 | -0.99% | Outside |
| 11:00 | $1,726.08–$1,733.01 | $1,729.54 | $1,714.71 | 11:00 | −$14.83 | -0.86% | Outside |
| 12:00 | $1,723.83–$1,730.75 | $1,727.29 | $1,690.60 | 12:00 | −$36.69 | -2.12% | Outside |
| 13:00 | $1,725.38–$1,732.30 | $1,728.84 | $1,704.20 | 13:00 | −$24.64 | -1.43% | Outside |
| 14:00 | $1,725.65–$1,732.57 | $1,729.11 | $1,704.64 | 14:02 | −$24.47 | -1.42% | Outside |
| 15:00 | $1,729.29–$1,736.23 | $1,732.76 | $1,702.80 | 15:01 | −$29.96 | -1.73% | Outside |
| 16:00 | $1,725.56–$1,732.48 | $1,729.02 | $1,703.70 | 16:01 | −$25.32 | -1.46% | Outside |
| 17:00 | $1,728.35–$1,735.28 | $1,731.82 | $1,705.96 | 17:00 | −$25.86 | -1.49% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BEARISH | 43.72% | Approved | $1,725.19 | $1,724.90 | -0.02% | Correct |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BEARISH | 49.72% | Approved | $1,725.81 | $1,725.34 | -0.03% | Correct |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BEARISH | 49.72% | Approved | $1,726.00 | $1,718.90 | -0.41% | Correct |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 50.37% | Approved | $1,718.91 | $1,725.01 | +0.35% | Correct |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 53.13% | Approved | $1,725.70 | $1,709.00 | -0.97% | Incorrect |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 50.79% | Approved | $1,708.80 | $1,710.50 | +0.10% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 50.79% | Approved | $1,710.25 | $1,715.15 | +0.29% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 50.91% | Approved | $1,714.71 | $1,690.52 | -1.41% | Incorrect |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BULLISH | 50.44% | Approved | $1,690.60 | $1,704.72 | +0.84% | Correct |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BULLISH | 50.86% | Approved | $1,704.20 | $1,704.65 | +0.03% | Correct |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 49.34% | Approved | $1,704.64 | $1,702.51 | -0.12% | Correct |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 50.98% | Approved | $1,702.80 | $1,703.62 | +0.05% | Correct |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 50.98% | Approved | $1,703.70 | $1,705.96 | +0.13% | Correct |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 59.38% | Approved | $1,717.50 | $1,725.19 | +0.45% | Correct |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BEARISH | 49.42% | Approved | $1,725.19 | $1,725.01 | -0.01% | Correct |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 50.02% | Approved | $1,725.70 | $1,690.52 | -2.04% | Incorrect |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 50.57% | Approved | $1,690.60 | $1,703.62 | +0.77% | Correct |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BEARISH | 49.04% | Approved | $1,703.70 | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 54.43% | Approved | $1,725.19 | $1,705.96 | -1.11% | Incorrect |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 52.10% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.66% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 49.06% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 50.66% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 55.37% | Approved | $1,725.19 | — | — | Pending target end |

## TWST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $189.51–$190.28 | $189.90 | $193.40 | 04:00 | +$3.50 | +1.84% | Outside |
| 05:00 | $189.99–$190.76 | $190.37 | $192.11 | 05:04 | +$1.74 | +0.91% | Outside |
| 06:00 | $190.17–$190.95 | $190.56 | — | — | — | — | No observed price within five minutes |
| 07:00 | $188.90–$189.66 | $189.28 | $201.93 | 07:00 | +$12.65 | +6.68% | Outside |
| 08:00 | $189.19–$189.95 | $189.57 | $200.81 | 08:00 | +$11.24 | +5.93% | Outside |
| 09:00 | $189.21–$189.97 | $189.59 | $200.00 | 09:00 | +$10.41 | +5.49% | Outside |
| 10:00 | $189.23–$190.00 | $189.61 | $200.44 | 10:00 | +$10.83 | +5.71% | Outside |
| 11:00 | $189.49–$190.26 | $189.87 | $203.35 | 11:00 | +$13.48 | +7.10% | Outside |
| 12:00 | $189.06–$189.83 | $189.45 | $204.90 | 12:00 | +$15.45 | +8.16% | Outside |
| 13:00 | $189.14–$189.91 | $189.53 | $205.00 | 13:00 | +$15.47 | +8.16% | Outside |
| 14:00 | $189.00–$189.77 | $189.39 | $206.00 | 14:00 | +$16.61 | +8.77% | Outside |
| 15:00 | $190.22–$190.99 | $190.60 | — | — | — | — | No observed price within five minutes |
| 16:00 | $189.95–$190.72 | $190.34 | — | — | — | — | No observed price within five minutes |
| 17:00 | $189.35–$190.12 | $189.74 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 05 04:00 → Oct 05 05:00 | BEARISH | 49.92% | Approved | $193.40 | $192.21 | -0.62% | Correct |
| 1h@05:00 | Oct 05 05:00 → Oct 05 06:00 | BEARISH | 49.33% | Approved | $192.11 | — | — | End: No observed price within five minutes |
| 1h@06:00 | Oct 05 06:00 → Oct 05 07:00 | BEARISH | 49.33% | Approved | — | $202.02 | — | Start: No observed price within five minutes |
| 1h@07:00 | Oct 05 07:00 → Oct 05 08:00 | BULLISH | 50.32% | Approved | $201.93 | $200.99 | -0.47% | Incorrect |
| 1h@08:00 | Oct 05 08:00 → Oct 05 09:00 | BULLISH | 50.20% | Approved | $200.81 | $199.89 | -0.46% | Incorrect |
| 1h@09:00 | Oct 05 09:00 → Oct 05 10:00 | BULLISH | 50.88% | Approved | $200.00 | $200.57 | +0.28% | Correct |
| 1h@10:00 | Oct 05 10:00 → Oct 05 11:00 | BULLISH | 50.88% | Approved | $200.44 | $203.20 | +1.38% | Correct |
| 1h@11:00 | Oct 05 11:00 → Oct 05 12:00 | BULLISH | 51.00% | Approved | $203.35 | $204.78 | +0.70% | Correct |
| 1h@12:00 | Oct 05 12:00 → Oct 05 13:00 | BULLISH | 50.42% | Approved | $204.90 | $204.98 | +0.04% | Correct |
| 1h@13:00 | Oct 05 13:00 → Oct 05 14:00 | BULLISH | 51.51% | Approved | $205.00 | $207.00 | +0.98% | Correct |
| 1h@14:00 | Oct 05 14:00 → Oct 05 15:00 | BEARISH | 48.87% | Approved | $206.00 | — | — | End: No observed price within five minutes |
| 1h@15:00 | Oct 05 15:00 → Oct 05 16:00 | BULLISH | 50.88% | Approved | — | $206.50 | — | Start: No observed price within five minutes |
| 1h@16:00 | Oct 05 16:00 → Oct 05 17:00 | BULLISH | 50.88% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@gap | Oct 02 17:00 → Oct 05 04:00 | BULLISH | 54.19% | Approved | — | $193.40 | — | Start: No observed price within five minutes |
| 4h@04:00 | Oct 05 04:00 → Oct 05 08:00 | BEARISH | 49.42% | Approved | $193.40 | $200.99 | +3.92% | Incorrect |
| 4h@08:00 | Oct 05 08:00 → Oct 05 12:00 | BULLISH | 54.15% | Approved | $200.81 | $204.78 | +1.97% | Correct |
| 4h@12:00 | Oct 05 12:00 → Oct 05 16:00 | BULLISH | 50.10% | Approved | $204.90 | $206.50 | +0.78% | Correct |
| 4h@16:00 | Oct 05 16:00 → Oct 06 07:00 | BULLISH | 51.73% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 54.55% | Approved | $193.40 | — | — | End: No observed price within five minutes |
| 1d@D+2 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 52.22% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.79% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 49.18% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 09 04:00 → Oct 09 17:00 | BULLISH | 50.79% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 05 04:00 → Oct 09 17:00 | BULLISH | 55.39% | Approved | $193.40 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 06:00 | No observed price within five minutes (nearest Oct 05 06:08; 8 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 05 15:12; 12 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price within five minutes (nearest Oct 05 16:09; 9 minutes away); source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 05 16:54; 6 minutes away); source request covers the full tolerance window |
| 1h@05:00 end | No observed price within five minutes (nearest Oct 05 05:46; 14 minutes away); source request covers the full tolerance window |
| 1h@06:00 start | No observed price within five minutes (nearest Oct 05 06:08; 8 minutes away); source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 05 14:17; 43 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 05 15:12; 12 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Oct 05 16:09; 9 minutes away); source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 05 16:54; 6 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 02 15:37; 83 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 05 16:54; 6 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

Actual prices use the Gameplan's own stock dataset and the existing five-minute boundary tolerance. The 17:00 price is a completed minute's closing price; earlier hourly clocks use opening prices. No missing prices are filled. Longer forecasts continue in the cumulative saved-Gameplan evaluation.

The machine-readable results retain the model's own frozen probability target and Brier score, plus separate raw-price and cost-adjusted outcomes. These are not broker fills or realized profit.
