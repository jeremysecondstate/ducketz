# Yung Gameplan (YG) results · 2026-10-02

Tomorrow's Gameplan for **2026-10-05** is prepared. All times are Pacific.

The saved price estimates below are compared with actual market prices at the same clock. The ranges were planning estimates; the observations are market prices, not broker fills or trading P/L.

Original forecast: [2026-10-02 Gameplan](C:/DATASTORE/ml/nightly-gameplan-runs/20261002T063014.996205Z/forecasts.parquet).

[Original Gameplan with prices and quantities](C:/DATASTORE/ml/gameplan-trade-plan-runs/20261002T064252.251864Z/Gameplan.md).

**156 evaluated · 66 still pending · 42 missing eligible price observations.**

Direction results compare the saved Bullish/Bearish call with the actual price move. Neutral forecasts have no directional score. Future and missing outcomes are excluded from accuracy.

YG probability scores use a strictly positive raw price return. Cost-adjusted outcomes remain separate.

| Stock | Correct / scored approved calls | Direction accuracy | Mean absolute price error | Prices within range |
| --- | --- | --- | --- | --- |
| AAPL | 8 / 18 | 44.44% | 0.46% | 2 / 14 |
| AMZN | 8 / 18 | 44.44% | 0.98% | 1 / 14 |
| COST | 7 / 8 | 87.50% | 0.33% | 2 / 10 |
| CROX | 3 / 7 | 42.86% | 0.42% | 1 / 7 |
| GOOG | 5 / 17 | 29.41% | 1.16% | 1 / 14 |
| IONQ | 8 / 15 | 53.33% | 1.00% | 4 / 12 |
| MU | 12 / 18 | 66.67% | 1.61% | 1 / 14 |
| NVDA | 9 / 18 | 50.00% | 1.32% | 0 / 14 |
| PATH | 9 / 11 | 81.82% | 1.65% | 1 / 9 |
| SNDK | 11 / 18 | 61.11% | 3.29% | 1 / 14 |
| TWST | 4 / 8 | 50.00% | 1.11% | 2 / 8 |

## AAPL

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $330.03–$331.36 | $330.70 | $330.65 | 04:00 | −$0.05 | -0.02% | Inside |
| 05:00 | $329.90–$331.24 | $330.57 | $331.31 | 05:00 | +$0.74 | +0.22% | Outside |
| 06:00 | $330.01–$331.34 | $330.68 | $332.48 | 06:00 | +$1.80 | +0.54% | Outside |
| 07:00 | $330.92–$332.25 | $331.58 | $332.23 | 07:00 | +$0.65 | +0.20% | Inside |
| 08:00 | $330.90–$332.24 | $331.57 | $332.97 | 08:00 | +$1.40 | +0.42% | Outside |
| 09:00 | $331.03–$332.37 | $331.70 | $332.44 | 09:00 | +$0.74 | +0.22% | Outside |
| 10:00 | $331.44–$332.78 | $332.11 | $333.41 | 10:00 | +$1.30 | +0.39% | Outside |
| 11:00 | $331.41–$332.75 | $332.08 | $333.35 | 11:00 | +$1.27 | +0.38% | Outside |
| 12:00 | $330.89–$332.23 | $331.56 | $333.71 | 12:00 | +$2.15 | +0.65% | Outside |
| 13:00 | $330.64–$331.98 | $331.31 | $333.79 | 13:00 | +$2.48 | +0.75% | Outside |
| 14:00 | $330.38–$331.72 | $331.05 | $333.40 | 14:02 | +$2.35 | +0.71% | Outside |
| 15:00 | $330.61–$331.95 | $331.28 | $333.34 | 15:00 | +$2.06 | +0.62% | Outside |
| 16:00 | $330.42–$331.75 | $331.08 | $333.29 | 16:04 | +$2.21 | +0.67% | Outside |
| 17:00 | $330.69–$332.03 | $331.36 | $333.53 | 17:00 | +$2.17 | +0.65% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BEARISH | 49.98% | Approved | $330.65 | $331.40 | +0.23% | Incorrect |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 49.75% | Approved | $331.31 | $332.47 | +0.35% | Incorrect |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 49.75% | Approved | $332.48 | $332.21 | -0.08% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 48.85% | Approved | $332.23 | $332.96 | +0.22% | Incorrect |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BULLISH | 52.48% | Approved | $332.97 | $332.43 | -0.16% | Incorrect |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BULLISH | 50.46% | Approved | $332.44 | $333.36 | +0.28% | Correct |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 50.42% | Approved | $333.41 | $333.32 | -0.03% | Incorrect |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 51.15% | Approved | $333.35 | $333.70 | +0.10% | Correct |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BEARISH | 49.75% | Approved | $333.71 | $333.79 | +0.02% | Incorrect |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 43.20% | Approved | $333.79 | $333.34 | -0.13% | Correct |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 46.39% | Approved | $333.40 | $333.33 | -0.02% | Correct |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 45.94% | Approved | $333.34 | $333.28 | -0.02% | Correct |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 45.82% | Approved | $333.29 | $333.53 | +0.07% | Incorrect |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 58.09% | Approved | $330.78 | $330.65 | -0.04% | Incorrect |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 49.69% | Approved | $330.65 | $332.96 | +0.70% | Incorrect |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 53.79% | Approved | $332.97 | $333.70 | +0.22% | Correct |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BULLISH | 51.60% | Approved | $333.71 | $333.28 | -0.13% | Incorrect |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 56.72% | Approved | $333.29 | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 53.41% | Approved | $330.65 | $333.53 | +0.87% | Correct |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 57.34% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 54.86% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 52.34% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 51.67% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 56.24% | Approved | $330.65 | — | — | Pending target end |

## AMZN

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $248.74–$249.75 | $249.25 | $249.67 | 04:00 | +$0.42 | +0.17% | Inside |
| 05:00 | $248.65–$249.66 | $249.16 | $249.67 | 05:00 | +$0.51 | +0.20% | Outside |
| 06:00 | $248.63–$249.63 | $249.13 | $251.85 | 06:00 | +$2.72 | +1.09% | Outside |
| 07:00 | $247.74–$248.75 | $248.24 | $251.61 | 07:00 | +$3.37 | +1.36% | Outside |
| 08:00 | $247.88–$248.88 | $248.38 | $251.46 | 08:00 | +$3.08 | +1.24% | Outside |
| 09:00 | $248.09–$249.09 | $248.59 | $250.56 | 09:00 | +$1.97 | +0.79% | Outside |
| 10:00 | $247.85–$248.85 | $248.35 | $251.05 | 10:00 | +$2.70 | +1.09% | Outside |
| 11:00 | $247.77–$248.77 | $248.27 | $250.90 | 11:00 | +$2.62 | +1.06% | Outside |
| 12:00 | $247.67–$248.67 | $248.17 | $250.47 | 12:00 | +$2.30 | +0.93% | Outside |
| 13:00 | $247.73–$248.73 | $248.23 | $251.53 | 13:00 | +$3.30 | +1.33% | Outside |
| 14:00 | $247.62–$248.62 | $248.12 | $250.81 | 14:01 | +$2.69 | +1.08% | Outside |
| 15:00 | $247.85–$248.85 | $248.35 | $250.94 | 15:02 | +$2.59 | +1.04% | Outside |
| 16:00 | $247.73–$248.73 | $248.23 | $251.15 | 16:01 | +$2.92 | +1.18% | Outside |
| 17:00 | $247.83–$248.83 | $248.33 | $251.37 | 17:00 | +$3.04 | +1.22% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BEARISH | 49.09% | Approved | $249.67 | $249.68 | +0.00% | Incorrect |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 49.69% | Approved | $249.67 | $251.82 | +0.86% | Incorrect |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 49.69% | Approved | $251.85 | $251.64 | -0.08% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 48.79% | Approved | $251.61 | $251.44 | -0.07% | Correct |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BEARISH | 49.95% | Approved | $251.46 | $250.57 | -0.35% | Correct |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BULLISH | 50.45% | Approved | $250.56 | $251.03 | +0.19% | Correct |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 50.41% | Approved | $251.05 | $250.89 | -0.06% | Incorrect |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 51.87% | Approved | $250.90 | $250.46 | -0.17% | Incorrect |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BULLISH | 50.06% | Approved | $250.47 | $251.53 | +0.42% | Correct |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 43.72% | Approved | $251.53 | $250.82 | -0.28% | Correct |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 45.89% | Approved | $250.81 | $250.91 | +0.04% | Incorrect |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 47.69% | Approved | $250.94 | $251.13 | +0.08% | Incorrect |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 47.58% | Approved | $251.15 | $251.37 | +0.09% | Incorrect |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 55.96% | Approved | $248.90 | $249.67 | +0.31% | Correct |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 49.92% | Approved | $249.67 | $251.44 | +0.71% | Incorrect |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 54.51% | Approved | $251.46 | $250.46 | -0.40% | Incorrect |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 48.31% | Approved | $250.47 | $251.13 | +0.26% | Incorrect |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 56.71% | Approved | $251.15 | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 54.63% | Approved | $249.67 | $251.37 | +0.68% | Correct |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 58.54% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 56.08% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 53.57% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 52.90% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 55.88% | Approved | $249.67 | — | — | Pending target end |

## COST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $914.16–$917.83 | $916.00 | — | — | — | — | No observed price within five minutes |
| 05:00 | $913.98–$917.66 | $915.82 | — | — | — | — | No observed price within five minutes |
| 06:00 | $914.16–$917.84 | $916.00 | $918.57 | 06:03 | +$2.57 | +0.28% | Outside |
| 07:00 | $913.74–$917.41 | $915.58 | $914.77 | 07:00 | −$0.81 | -0.09% | Inside |
| 08:00 | $913.44–$917.11 | $915.28 | $913.09 | 08:00 | −$2.19 | -0.24% | Outside |
| 09:00 | $913.95–$917.62 | $915.78 | $913.48 | 09:00 | −$2.30 | -0.25% | Outside |
| 10:00 | $914.68–$918.36 | $916.52 | $912.25 | 10:00 | −$4.27 | -0.47% | Outside |
| 11:00 | $913.69–$917.37 | $915.53 | $914.74 | 11:00 | −$0.79 | -0.09% | Inside |
| 12:00 | $913.36–$917.03 | $915.19 | $917.53 | 12:00 | +$2.34 | +0.26% | Outside |
| 13:00 | $913.53–$917.21 | $915.37 | $920.97 | 13:00 | +$5.60 | +0.61% | Outside |
| 14:00 | $912.92–$916.59 | $914.75 | $919.50 | 14:03 | +$4.75 | +0.52% | Outside |
| 15:00 | $912.77–$916.44 | $914.61 | — | — | — | — | No observed price within five minutes |
| 16:00 | $913.04–$916.71 | $914.88 | — | — | — | — | No observed price within five minutes |
| 17:00 | $913.20–$916.87 | $915.03 | $919.55 | 17:00 | +$4.52 | +0.49% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BULLISH | 53.95% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 47.67% | Approved | — | $918.50 | — | Start: No observed price within five minutes |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 47.67% | Approved | $918.57 | $914.80 | -0.41% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 46.78% | Approved | $914.77 | $912.76 | -0.22% | Correct |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BULLISH | 51.78% | Approved | $913.09 | $913.47 | +0.04% | Correct |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BULLISH | 51.45% | Approved | $913.48 | $912.11 | -0.15% | Incorrect |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 51.41% | Approved | $912.25 | $914.73 | +0.27% | Correct |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 52.87% | Approved | $914.74 | $917.60 | +0.31% | Correct |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BULLISH | 50.69% | Approved | $917.53 | $920.97 | +0.37% | Correct |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 45.09% | Approved | $920.97 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 47.25% | Approved | $919.50 | — | — | End: No observed price within five minutes |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 48.27% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 46.66% | Approved | — | $919.55 | — | Start: No observed price within five minutes |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 51.12% | Approved | $915.96 | — | — | End: No observed price within five minutes |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BULLISH | 50.37% | Approved | — | $912.76 | — | Start: No observed price within five minutes |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 54.89% | Approved | $913.09 | $917.60 | +0.49% | Correct |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 49.21% | Approved | $917.53 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 57.01% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 50.47% | Approved | — | $919.55 | — | Start: No observed price within five minutes |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 54.44% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 51.93% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.40% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 48.73% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 55.40% | Approved | — | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 04:00 | No observed price within five minutes (nearest Oct 02 04:10; 10 minutes away); source request covers the full tolerance window |
| Price 05:00 | No observed price within five minutes (nearest Oct 02 05:06; 6 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 02 15:23; 23 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price within five minutes (nearest Oct 02 16:06; 6 minutes away); source request covers the full tolerance window |
| 1h@04:00 start | No observed price within five minutes (nearest Oct 02 04:10; 10 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Oct 02 04:52; 8 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 02 05:06; 6 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 02 13:51; 9 minutes away); source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 02 14:42; 18 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 02 15:23; 23 minutes away); source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 02 15:53; 7 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Oct 02 16:06; 6 minutes away); source request covers the full tolerance window |
| 1h@gap end | No observed price within five minutes (nearest Oct 02 04:10; 10 minutes away); source request covers the full tolerance window |
| 4h@04:00 start | No observed price within five minutes (nearest Oct 02 04:10; 10 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 02 15:53; 7 minutes away); source request covers the full tolerance window |
| 1d@D+1 start | No observed price within five minutes (nearest Oct 02 04:10; 10 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## CROX

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $118.13–$118.61 | $118.37 | — | — | — | — | No observed price within five minutes |
| 05:00 | $118.02–$118.50 | $118.26 | — | — | — | — | No observed price within five minutes |
| 06:00 | $117.75–$118.24 | $118.00 | — | — | — | — | No observed price within five minutes |
| 07:00 | $118.22–$118.70 | $118.46 | $118.86 | 07:00 | +$0.40 | +0.34% | Outside |
| 08:00 | $118.18–$118.66 | $118.42 | $119.62 | 08:00 | +$1.20 | +1.01% | Outside |
| 09:00 | $118.47–$118.95 | $118.71 | $119.47 | 09:00 | +$0.76 | +0.64% | Outside |
| 10:00 | $118.37–$118.85 | $118.61 | $119.00 | 10:00 | +$0.39 | +0.33% | Outside |
| 11:00 | $117.96–$118.45 | $118.20 | $118.55 | 11:00 | +$0.35 | +0.30% | Outside |
| 12:00 | $117.94–$118.42 | $118.18 | $118.06 | 12:01 | −$0.12 | -0.10% | Inside |
| 13:00 | $118.11–$118.59 | $118.35 | $118.09 | 13:00 | −$0.26 | -0.22% | Outside |
| 14:00 | $118.41–$118.89 | $118.65 | — | — | — | — | No observed price on the required side of the clock |
| 15:00 | $118.13–$118.61 | $118.37 | — | — | — | — | No observed price on the required side of the clock |
| 16:00 | $118.99–$119.47 | $119.23 | — | — | — | — | No observed price on the required side of the clock |
| 17:00 | $118.25–$118.74 | $118.49 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BULLISH | 52.22% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 44.99% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 45.12% | Approved | — | $118.86 | — | Start: No observed price within five minutes |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 45.80% | Approved | $118.86 | $119.68 | +0.69% | Incorrect |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BEARISH | 49.08% | Approved | $119.62 | $119.39 | -0.19% | Correct |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BEARISH | 49.41% | Approved | $119.47 | $118.97 | -0.42% | Correct |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BEARISH | 49.41% | Approved | $119.00 | $118.56 | -0.37% | Correct |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 50.53% | Approved | $118.55 | $118.07 | -0.40% | Incorrect |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BEARISH | 48.84% | Approved | $118.06 | $118.13 | +0.06% | Incorrect |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 43.70% | Approved | $118.09 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 46.20% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 46.15% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 46.62% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 56.75% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BULLISH | 52.24% | Approved | — | $119.68 | — | Start: No observed price within five minutes |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 56.01% | Approved | $119.62 | $118.07 | -1.30% | Incorrect |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BULLISH | 50.57% | Approved | $118.06 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 57.36% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 50.70% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 54.67% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 52.16% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.63% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 48.96% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 55.81% | Approved | — | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 04:00 | No observed price within five minutes (nearest Oct 02 05:44; 104 minutes away); source request covers the full tolerance window |
| Price 05:00 | No observed price within five minutes (nearest Oct 02 05:44; 44 minutes away); source request covers the full tolerance window |
| Price 06:00 | No observed price within five minutes (nearest Oct 02 06:22; 22 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 15:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 16:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 02 13:01; 239 minutes away); source request covers the full tolerance window |
| 1h@04:00 start | No observed price within five minutes (nearest Oct 02 05:44; 104 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Oct 02 01:37; 203 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 02 05:44; 44 minutes away); source request covers the full tolerance window |
| 1h@05:00 end | No observed price within five minutes (nearest Oct 02 05:47; 13 minutes away); source request covers the full tolerance window |
| 1h@06:00 start | No observed price within five minutes (nearest Oct 02 06:22; 22 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 02 13:01; 59 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@14:00 end | No observed price within five minutes (nearest Oct 02 13:01; 119 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 02 13:01; 179 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 02 13:01; 239 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 01 15:21; 99 minutes away); source request covers the full tolerance window |
| 1h@gap end | No observed price within five minutes (nearest Oct 02 05:44; 104 minutes away); source request covers the full tolerance window |
| 4h@04:00 start | No observed price within five minutes (nearest Oct 02 05:44; 104 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 02 13:01; 179 minutes away); source request covers the full tolerance window |
| 1d@D+1 start | No observed price within five minutes (nearest Oct 02 05:44; 104 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 02 13:01; 239 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## GOOG

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $335.29–$336.65 | $335.97 | $337.20 | 04:00 | +$1.23 | +0.37% | Outside |
| 05:00 | $334.98–$336.33 | $335.66 | $336.25 | 05:00 | +$0.59 | +0.18% | Inside |
| 06:00 | $335.01–$336.36 | $335.69 | $337.97 | 06:00 | +$2.28 | +0.68% | Outside |
| 07:00 | $334.47–$335.83 | $335.15 | $340.91 | 07:00 | +$5.76 | +1.72% | Outside |
| 08:00 | $335.05–$336.41 | $335.73 | $341.52 | 08:00 | +$5.79 | +1.72% | Outside |
| 09:00 | $334.78–$336.14 | $335.46 | $339.35 | 09:00 | +$3.89 | +1.16% | Outside |
| 10:00 | $335.05–$336.40 | $335.72 | $340.95 | 10:00 | +$5.23 | +1.56% | Outside |
| 11:00 | $335.09–$336.44 | $335.76 | $340.77 | 11:00 | +$5.01 | +1.49% | Outside |
| 12:00 | $334.87–$336.22 | $335.54 | $339.76 | 12:00 | +$4.22 | +1.26% | Outside |
| 13:00 | $335.20–$336.56 | $335.88 | $340.41 | 13:00 | +$4.53 | +1.35% | Outside |
| 14:00 | $335.04–$336.39 | $335.71 | $339.98 | 14:00 | +$4.27 | +1.27% | Outside |
| 15:00 | $335.36–$336.71 | $336.04 | $339.97 | 15:01 | +$3.93 | +1.17% | Outside |
| 16:00 | $335.46–$336.82 | $336.14 | $340.02 | 16:04 | +$3.88 | +1.15% | Outside |
| 17:00 | $335.54–$336.90 | $336.22 | $340.00 | 17:00 | +$3.78 | +1.12% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BEARISH | 47.30% | Approved | $337.20 | $336.15 | -0.31% | Correct |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 49.35% | Approved | $336.25 | $338.01 | +0.52% | Incorrect |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 49.48% | Approved | $337.97 | $340.92 | +0.87% | Incorrect |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 49.82% | Approved | $340.91 | $341.50 | +0.17% | Incorrect |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BULLISH | 50.99% | Approved | $341.52 | $339.37 | -0.63% | Incorrect |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BEARISH | 49.79% | Approved | $339.35 | $340.97 | +0.48% | Incorrect |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 50.26% | Approved | $340.95 | $340.71 | -0.07% | Incorrect |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 51.24% | Approved | $340.77 | $339.75 | -0.30% | Incorrect |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BEARISH | 49.92% | Approved | $339.76 | $340.43 | +0.20% | Incorrect |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 44.98% | Approved | $340.41 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BULLISH | 50.62% | Approved | $339.98 | $339.85 | -0.04% | Incorrect |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 47.30% | Approved | $339.97 | $340.00 | +0.01% | Incorrect |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 47.72% | Approved | $340.02 | $340.00 | -0.01% | Correct |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 58.80% | Approved | $336.00 | $337.20 | +0.36% | Correct |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 49.97% | Approved | $337.20 | $341.50 | +1.28% | Incorrect |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 52.59% | Approved | $341.52 | $339.75 | -0.52% | Incorrect |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BULLISH | 51.53% | Approved | $339.76 | $340.00 | +0.07% | Correct |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 56.55% | Approved | $340.02 | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 50.40% | Approved | $337.20 | $340.00 | +0.83% | Correct |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 54.37% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 51.87% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.33% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 48.66% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 55.16% | Approved | $337.20 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 02 13:52; 8 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## IONQ

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $44.02–$44.21 | $44.12 | $44.47 | 04:00 | +$0.35 | +0.79% | Outside |
| 05:00 | $43.96–$44.15 | $44.05 | $44.30 | 05:00 | +$0.25 | +0.57% | Outside |
| 06:00 | $43.87–$44.06 | $43.96 | $44.94 | 06:00 | +$0.98 | +2.23% | Outside |
| 07:00 | $44.13–$44.32 | $44.23 | $44.90 | 07:00 | +$0.67 | +1.51% | Outside |
| 08:00 | $43.93–$44.11 | $44.02 | $44.98 | 08:00 | +$0.96 | +2.18% | Outside |
| 09:00 | $43.93–$44.11 | $44.02 | $44.65 | 09:00 | +$0.63 | +1.43% | Outside |
| 10:00 | $43.91–$44.10 | $44.01 | $44.65 | 10:00 | +$0.64 | +1.45% | Outside |
| 11:00 | $43.97–$44.15 | $44.06 | $44.63 | 11:00 | +$0.57 | +1.31% | Outside |
| 12:00 | $43.91–$44.10 | $44.00 | $43.94 | 12:00 | −$0.06 | -0.14% | Inside |
| 13:00 | $43.76–$43.95 | $43.86 | $43.77 | 13:00 | −$0.09 | -0.21% | Inside |
| 14:00 | $43.82–$44.01 | $43.92 | $43.90 | 14:01 | −$0.02 | -0.05% | Inside |
| 15:00 | $43.86–$44.04 | $43.95 | — | — | — | — | No observed price within five minutes |
| 16:00 | $43.89–$44.08 | $43.99 | — | — | — | — | No observed price within five minutes |
| 17:00 | $43.87–$44.06 | $43.97 | $43.91 | 17:00 | −$0.06 | -0.14% | Inside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BEARISH | 47.78% | Approved | $44.47 | $44.35 | -0.27% | Correct |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 46.70% | Approved | $44.30 | $44.95 | +1.47% | Incorrect |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 46.82% | Approved | $44.94 | $44.88 | -0.14% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 47.98% | Approved | $44.90 | $44.95 | +0.12% | Incorrect |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BEARISH | 49.40% | Approved | $44.98 | $44.66 | -0.71% | Correct |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BEARISH | 48.66% | Approved | $44.65 | $44.65 | -0.01% | Correct |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BEARISH | 48.95% | Approved | $44.65 | $44.64 | -0.02% | Correct |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 51.03% | Approved | $44.63 | $43.95 | -1.53% | Incorrect |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BEARISH | 49.20% | Approved | $43.94 | $43.77 | -0.39% | Correct |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 46.23% | Approved | $43.77 | $43.88 | +0.25% | Incorrect |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 47.23% | Approved | $43.90 | $43.91 | +0.02% | Incorrect |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 46.12% | Approved | — | $43.85 | — | Start: No observed price within five minutes |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 46.00% | Approved | — | $43.91 | — | Start: No observed price within five minutes |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 59.14% | Approved | — | $44.47 | — | Start: No observed price within five minutes |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 42.36% | Approved | $44.47 | $44.95 | +1.09% | Incorrect |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BEARISH | 45.57% | Approved | $44.98 | $43.95 | -2.29% | Correct |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 47.52% | Approved | $43.94 | $43.85 | -0.20% | Correct |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BEARISH | 48.60% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 50.62% | Approved | $44.47 | $43.91 | -1.26% | Incorrect |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 54.58% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 52.08% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 49.54% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 48.88% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 56.58% | Approved | $44.47 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 15:00 | No observed price within five minutes (nearest Oct 02 15:07; 7 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price within five minutes (nearest Oct 02 16:11; 11 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 02 15:07; 7 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Oct 02 16:11; 11 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 01 16:51; 9 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## MU

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,089.82–$1,094.20 | $1,092.01 | $1,104.20 | 04:00 | +$12.19 | +1.12% | Outside |
| 05:00 | $1,092.64–$1,097.02 | $1,094.83 | $1,096.20 | 05:00 | +$1.37 | +0.13% | Inside |
| 06:00 | $1,093.37–$1,097.76 | $1,095.56 | $1,110.85 | 06:00 | +$15.29 | +1.40% | Outside |
| 07:00 | $1,092.97–$1,097.36 | $1,095.16 | $1,088.42 | 07:00 | −$6.74 | -0.62% | Outside |
| 08:00 | $1,090.59–$1,094.97 | $1,092.78 | $1,082.77 | 08:00 | −$10.01 | -0.92% | Outside |
| 09:00 | $1,090.62–$1,095.00 | $1,092.81 | $1,080.73 | 09:00 | −$12.08 | -1.11% | Outside |
| 10:00 | $1,093.80–$1,098.20 | $1,096.00 | $1,078.31 | 10:00 | −$17.69 | -1.61% | Outside |
| 11:00 | $1,096.61–$1,101.02 | $1,098.81 | $1,078.44 | 11:00 | −$20.37 | -1.85% | Outside |
| 12:00 | $1,093.67–$1,098.06 | $1,095.87 | $1,075.70 | 12:00 | −$20.17 | -1.84% | Outside |
| 13:00 | $1,095.16–$1,099.56 | $1,097.36 | $1,074.06 | 13:00 | −$23.30 | -2.12% | Outside |
| 14:00 | $1,094.74–$1,099.14 | $1,096.94 | $1,072.21 | 14:00 | −$24.73 | -2.25% | Outside |
| 15:00 | $1,095.67–$1,100.08 | $1,097.87 | $1,071.47 | 15:00 | −$26.40 | -2.40% | Outside |
| 16:00 | $1,095.42–$1,099.82 | $1,097.62 | $1,069.73 | 16:00 | −$27.89 | -2.54% | Outside |
| 17:00 | $1,096.05–$1,100.45 | $1,098.25 | $1,069.15 | 17:00 | −$29.10 | -2.65% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BULLISH | 53.74% | Approved | $1,104.20 | $1,096.25 | -0.72% | Incorrect |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 48.59% | Approved | $1,096.20 | $1,110.89 | +1.34% | Incorrect |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 48.71% | Approved | $1,110.85 | $1,088.30 | -2.03% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 49.40% | Approved | $1,088.42 | $1,082.63 | -0.53% | Correct |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BULLISH | 50.08% | Approved | $1,082.77 | $1,081.00 | -0.16% | Incorrect |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BULLISH | 50.47% | Approved | $1,080.73 | $1,078.30 | -0.22% | Incorrect |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 50.47% | Approved | $1,078.31 | $1,078.46 | +0.01% | Correct |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 51.28% | Approved | $1,078.44 | $1,075.92 | -0.23% | Incorrect |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BEARISH | 48.74% | Approved | $1,075.70 | $1,074.27 | -0.13% | Correct |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 45.58% | Approved | $1,074.06 | $1,072.30 | -0.16% | Correct |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 44.97% | Approved | $1,072.21 | $1,071.40 | -0.08% | Correct |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 46.87% | Approved | $1,071.47 | $1,069.86 | -0.15% | Correct |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 46.99% | Approved | $1,069.73 | $1,069.15 | -0.05% | Correct |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 53.16% | Approved | $1,090.56 | $1,104.20 | +1.25% | Correct |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 46.22% | Approved | $1,104.20 | $1,082.63 | -1.95% | Correct |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BEARISH | 48.79% | Approved | $1,082.77 | $1,075.92 | -0.63% | Correct |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 46.90% | Approved | $1,075.70 | $1,069.86 | -0.54% | Correct |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 52.74% | Approved | $1,069.73 | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 51.19% | Approved | $1,104.20 | $1,069.15 | -3.17% | Incorrect |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 55.15% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 52.65% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 50.12% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 49.45% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 56.07% | Approved | $1,104.20 | — | — | Pending target end |

## NVDA

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $231.53–$232.47 | $232.00 | $233.56 | 04:00 | +$1.56 | +0.67% | Outside |
| 05:00 | $231.56–$232.50 | $232.03 | $234.83 | 05:00 | +$2.80 | +1.21% | Outside |
| 06:00 | $231.58–$232.52 | $232.05 | $235.89 | 06:00 | +$3.84 | +1.65% | Outside |
| 07:00 | $231.98–$232.92 | $232.45 | $237.35 | 07:00 | +$4.90 | +2.11% | Outside |
| 08:00 | $231.65–$232.58 | $232.11 | $236.57 | 08:00 | +$4.46 | +1.92% | Outside |
| 09:00 | $231.12–$232.06 | $231.59 | $235.58 | 09:00 | +$3.99 | +1.72% | Outside |
| 10:00 | $231.25–$232.19 | $231.72 | $235.24 | 10:00 | +$3.52 | +1.52% | Outside |
| 11:00 | $231.27–$232.20 | $231.73 | $234.69 | 11:00 | +$2.96 | +1.28% | Outside |
| 12:00 | $231.13–$232.06 | $231.59 | $234.55 | 12:00 | +$2.96 | +1.28% | Outside |
| 13:00 | $231.45–$232.38 | $231.92 | $234.00 | 13:00 | +$2.08 | +0.90% | Outside |
| 14:00 | $231.28–$232.21 | $231.74 | $233.92 | 14:01 | +$2.18 | +0.94% | Outside |
| 15:00 | $231.19–$232.13 | $231.66 | $233.90 | 15:01 | +$2.24 | +0.97% | Outside |
| 16:00 | $230.87–$231.80 | $231.34 | $233.88 | 16:00 | +$2.54 | +1.10% | Outside |
| 17:00 | $230.84–$231.77 | $231.30 | $234.23 | 17:00 | +$2.93 | +1.27% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BULLISH | 58.03% | Approved | $233.56 | $234.87 | +0.56% | Correct |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BULLISH | 50.63% | Approved | $234.83 | $235.84 | +0.43% | Correct |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BULLISH | 50.63% | Approved | $235.89 | $237.36 | +0.62% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 49.91% | Approved | $237.35 | $236.56 | -0.33% | Correct |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BULLISH | 52.60% | Approved | $236.57 | $235.57 | -0.42% | Incorrect |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BULLISH | 51.92% | Approved | $235.58 | $235.25 | -0.14% | Incorrect |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 51.78% | Approved | $235.24 | $234.69 | -0.23% | Incorrect |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 53.24% | Approved | $234.69 | $234.55 | -0.06% | Incorrect |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BULLISH | 50.46% | Approved | $234.55 | $234.02 | -0.23% | Incorrect |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 44.67% | Approved | $234.00 | $233.92 | -0.03% | Correct |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 45.18% | Approved | $233.92 | $233.92 | +0.00% | Incorrect |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 48.75% | Approved | $233.90 | $233.88 | -0.01% | Correct |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 48.59% | Approved | $233.88 | $234.23 | +0.15% | Incorrect |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 51.90% | Approved | $231.48 | $233.56 | +0.90% | Correct |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 49.40% | Approved | $233.56 | $236.56 | +1.28% | Incorrect |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 52.79% | Approved | $236.57 | $234.55 | -0.85% | Incorrect |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 49.35% | Approved | $234.55 | $233.88 | -0.29% | Correct |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 55.88% | Approved | $233.88 | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 54.33% | Approved | $233.56 | $234.23 | +0.29% | Correct |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 58.24% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 55.77% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 53.26% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 52.59% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 54.56% | Approved | $233.56 | — | — | Pending target end |

## PATH

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $13.31–$13.37 | $13.34 | $13.37 | 04:01 | +$0.03 | +0.22% | Inside |
| 05:00 | $13.32–$13.38 | $13.35 | — | — | — | — | No observed price within five minutes |
| 06:00 | $13.31–$13.38 | $13.34 | $13.39 | 06:02 | +$0.05 | +0.37% | Outside |
| 07:00 | $13.34–$13.40 | $13.37 | $13.29 | 07:00 | −$0.08 | -0.60% | Outside |
| 08:00 | $13.37–$13.43 | $13.40 | $13.13 | 08:00 | −$0.27 | -1.98% | Outside |
| 09:00 | $13.35–$13.41 | $13.38 | $13.05 | 09:00 | −$0.33 | -2.43% | Outside |
| 10:00 | $13.38–$13.44 | $13.41 | $13.13 | 10:00 | −$0.28 | -2.09% | Outside |
| 11:00 | $13.39–$13.45 | $13.42 | $13.06 | 11:00 | −$0.36 | -2.68% | Outside |
| 12:00 | $13.40–$13.46 | $13.43 | $13.13 | 12:00 | −$0.29 | -2.20% | Outside |
| 13:00 | $13.39–$13.45 | $13.42 | $13.12 | 13:01 | −$0.30 | -2.24% | Outside |
| 14:00 | $13.34–$13.41 | $13.37 | — | — | — | — | No observed price within five minutes |
| 15:00 | $13.26–$13.32 | $13.29 | — | — | — | — | No observed price within five minutes |
| 16:00 | $13.29–$13.35 | $13.32 | — | — | — | — | No observed price within five minutes |
| 17:00 | $13.34–$13.41 | $13.38 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BEARISH | 47.23% | Approved | $13.37 | $13.33 | -0.30% | Correct |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 44.68% | Approved | — | $13.41 | — | Start: No observed price within five minutes |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 48.41% | Approved | $13.39 | $13.30 | -0.67% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 49.09% | Approved | $13.29 | $13.14 | -1.13% | Correct |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BEARISH | 49.67% | Approved | $13.13 | $13.05 | -0.65% | Correct |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BEARISH | 49.20% | Approved | $13.05 | $13.13 | +0.57% | Incorrect |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BEARISH | 49.20% | Approved | $13.13 | $13.05 | -0.57% | Correct |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 50.09% | Approved | $13.06 | $13.13 | +0.57% | Correct |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BEARISH | 49.20% | Approved | $13.13 | $13.10 | -0.27% | Correct |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 45.26% | Approved | $13.12 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 47.74% | Approved | — | $13.10 | — | Start: No observed price within five minutes |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 46.80% | Approved | — | $13.11 | — | Start: No observed price within five minutes |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 47.45% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 53.35% | Approved | — | $13.37 | — | Start: No observed price within five minutes |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 41.98% | Approved | $13.37 | $13.14 | -1.72% | Correct |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BEARISH | 46.80% | Approved | $13.13 | $13.13 | +0.00% | Incorrect |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 46.36% | Approved | $13.13 | $13.11 | -0.19% | Correct |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 51.19% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BEARISH | 47.16% | Approved | $13.37 | — | — | End: No observed price within five minutes |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 51.14% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BEARISH | 48.62% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 46.09% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 45.43% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 54.54% | Approved | $13.37 | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 05:00 | No observed price within five minutes (nearest Oct 02 05:06; 6 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price within five minutes (nearest Oct 02 14:32; 32 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 02 15:54; 54 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price within five minutes (nearest Oct 02 16:29; 29 minutes away); source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 02 16:33; 27 minutes away); source request covers the full tolerance window |
| 1h@05:00 start | No observed price within five minutes (nearest Oct 02 05:06; 6 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 02 13:37; 23 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Oct 02 14:32; 32 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 02 15:54; 54 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price within five minutes (nearest Oct 02 16:29; 29 minutes away); source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 02 16:33; 27 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 01 16:52; 8 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 02 16:33; 27 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

## SNDK

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $1,788.22–$1,795.40 | $1,791.81 | $1,792.81 | 04:00 | +$1.00 | +0.06% | Inside |
| 05:00 | $1,786.02–$1,793.19 | $1,789.60 | $1,763.97 | 05:00 | −$25.63 | -1.43% | Outside |
| 06:00 | $1,790.49–$1,797.68 | $1,794.08 | $1,783.00 | 06:00 | −$11.08 | -0.62% | Outside |
| 07:00 | $1,786.16–$1,793.33 | $1,789.74 | $1,742.22 | 07:00 | −$47.52 | -2.66% | Outside |
| 08:00 | $1,793.90–$1,801.10 | $1,797.50 | $1,730.78 | 08:00 | −$66.72 | -3.71% | Outside |
| 09:00 | $1,795.17–$1,802.38 | $1,798.77 | $1,737.67 | 09:00 | −$61.10 | -3.40% | Outside |
| 10:00 | $1,801.07–$1,808.30 | $1,804.68 | $1,718.93 | 10:00 | −$85.75 | -4.75% | Outside |
| 11:00 | $1,798.23–$1,805.45 | $1,801.84 | $1,722.42 | 11:00 | −$79.42 | -4.41% | Outside |
| 12:00 | $1,793.50–$1,800.70 | $1,797.10 | $1,721.04 | 12:00 | −$76.06 | -4.23% | Outside |
| 13:00 | $1,788.47–$1,795.64 | $1,792.05 | $1,719.99 | 13:00 | −$72.06 | -4.02% | Outside |
| 14:00 | $1,792.12–$1,799.31 | $1,795.72 | $1,718.64 | 14:00 | −$77.08 | -4.29% | Outside |
| 15:00 | $1,786.20–$1,793.37 | $1,789.78 | $1,718.32 | 15:02 | −$71.46 | -3.99% | Outside |
| 16:00 | $1,790.77–$1,797.95 | $1,794.36 | $1,717.45 | 16:00 | −$76.91 | -4.29% | Outside |
| 17:00 | $1,789.54–$1,796.72 | $1,793.13 | $1,717.50 | 17:00 | −$75.63 | -4.22% | Outside |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BEARISH | 49.30% | Approved | $1,792.81 | $1,764.00 | -1.61% | Correct |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 48.53% | Approved | $1,763.97 | $1,784.25 | +1.15% | Incorrect |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 48.66% | Approved | $1,783.00 | $1,742.40 | -2.28% | Correct |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 49.81% | Approved | $1,742.22 | $1,730.06 | -0.70% | Correct |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BULLISH | 51.23% | Approved | $1,730.78 | $1,737.26 | +0.37% | Correct |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BULLISH | 50.76% | Approved | $1,737.67 | $1,719.05 | -1.07% | Incorrect |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 50.76% | Approved | $1,718.93 | $1,722.31 | +0.20% | Correct |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 51.82% | Approved | $1,722.42 | $1,721.00 | -0.08% | Incorrect |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BULLISH | 50.19% | Approved | $1,721.04 | $1,719.35 | -0.10% | Incorrect |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 46.30% | Approved | $1,719.99 | $1,719.25 | -0.04% | Correct |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 46.13% | Approved | $1,718.64 | $1,718.50 | -0.01% | Correct |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 48.05% | Approved | $1,718.32 | $1,717.00 | -0.08% | Correct |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 48.17% | Approved | $1,717.45 | $1,717.50 | +0.00% | Incorrect |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 52.72% | Approved | $1,783.69 | $1,792.81 | +0.51% | Correct |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 46.89% | Approved | $1,792.81 | $1,730.06 | -3.50% | Correct |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 50.67% | Approved | $1,730.78 | $1,721.00 | -0.57% | Incorrect |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 49.83% | Approved | $1,721.04 | $1,717.00 | -0.23% | Correct |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BULLISH | 54.08% | Approved | $1,717.45 | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BULLISH | 52.06% | Approved | $1,792.81 | $1,717.50 | -4.20% | Incorrect |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 56.00% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 53.51% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BULLISH | 50.98% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BULLISH | 50.32% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 55.64% | Approved | $1,792.81 | — | — | Pending target end |

## TWST

### Planned prices vs actual prices

| Time | Saved price range | Saved midpoint | Actual price | Observed at | Actual − midpoint | Difference % | Range result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 04:00 | $187.40–$188.16 | $187.78 | — | — | — | — | No observed price within five minutes |
| 05:00 | $188.61–$189.37 | $188.99 | $189.29 | 05:01 | +$0.30 | +0.16% | Inside |
| 06:00 | $188.01–$188.77 | $188.39 | — | — | — | — | No observed price within five minutes |
| 07:00 | $188.78–$189.54 | $189.16 | $192.81 | 07:00 | +$3.65 | +1.93% | Outside |
| 08:00 | $188.44–$189.21 | $188.83 | $190.00 | 08:00 | +$1.17 | +0.62% | Outside |
| 09:00 | $188.78–$189.54 | $189.16 | $190.71 | 09:00 | +$1.55 | +0.82% | Outside |
| 10:00 | $188.99–$189.76 | $189.38 | $185.45 | 10:00 | −$3.93 | -2.08% | Outside |
| 11:00 | $189.14–$189.90 | $189.52 | $186.00 | 11:00 | −$3.52 | -1.86% | Outside |
| 12:00 | $188.53–$189.30 | $188.91 | $186.55 | 12:00 | −$2.36 | -1.25% | Outside |
| 13:00 | $188.57–$189.33 | $188.95 | $188.67 | 13:00 | −$0.28 | -0.15% | Inside |
| 14:00 | $194.33–$195.11 | $194.72 | — | — | — | — | No observed price within five minutes |
| 15:00 | $196.65–$197.45 | $197.05 | — | — | — | — | No observed price within five minutes |
| 16:00 | $187.86–$188.62 | $188.24 | — | — | — | — | No observed price on the required side of the clock |
| 17:00 | $188.46–$189.22 | $188.84 | — | — | — | — | No observed price within five minutes |

### Forecast outcomes

| Forecast | Window | Saved direction | P(up) | Model | Actual start | Actual end | Price move | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Oct 02 04:00 → Oct 02 05:00 | BEARISH | 49.85% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@05:00 | Oct 02 05:00 → Oct 02 06:00 | BEARISH | 46.03% | Approved | $189.29 | $191.00 | +0.90% | Incorrect |
| 1h@06:00 | Oct 02 06:00 → Oct 02 07:00 | BEARISH | 46.16% | Approved | — | $193.30 | — | Start: No observed price within five minutes |
| 1h@07:00 | Oct 02 07:00 → Oct 02 08:00 | BEARISH | 46.54% | Approved | $192.81 | $190.04 | -1.44% | Correct |
| 1h@08:00 | Oct 02 08:00 → Oct 02 09:00 | BULLISH | 53.53% | Approved | $190.00 | $190.90 | +0.47% | Correct |
| 1h@09:00 | Oct 02 09:00 → Oct 02 10:00 | BULLISH | 50.55% | Approved | $190.71 | $185.48 | -2.74% | Incorrect |
| 1h@10:00 | Oct 02 10:00 → Oct 02 11:00 | BULLISH | 50.55% | Approved | $185.45 | $185.98 | +0.29% | Correct |
| 1h@11:00 | Oct 02 11:00 → Oct 02 12:00 | BULLISH | 51.53% | Approved | $186.00 | $186.69 | +0.37% | Correct |
| 1h@12:00 | Oct 02 12:00 → Oct 02 13:00 | BEARISH | 49.71% | Approved | $186.55 | $188.87 | +1.24% | Incorrect |
| 1h@13:00 | Oct 02 13:00 → Oct 02 14:00 | BEARISH | 46.87% | Approved | $188.67 | — | — | End: No observed price within five minutes |
| 1h@14:00 | Oct 02 14:00 → Oct 02 15:00 | BEARISH | 47.16% | Approved | — | $190.00 | — | Start: No observed price within five minutes |
| 1h@15:00 | Oct 02 15:00 → Oct 02 16:00 | BEARISH | 47.57% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1h@16:00 | Oct 02 16:00 → Oct 02 17:00 | BEARISH | 47.99% | Approved | — | — | — | Start: No observed price on the required side of the clock; End: No observed price within five minutes |
| 1h@gap | Oct 01 17:00 → Oct 02 04:00 | BULLISH | 58.02% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 4h@04:00 | Oct 02 04:00 → Oct 02 08:00 | BEARISH | 47.44% | Approved | — | $190.04 | — | Start: No observed price within five minutes |
| 4h@08:00 | Oct 02 08:00 → Oct 02 12:00 | BULLISH | 57.20% | Approved | $190.00 | $186.69 | -1.74% | Incorrect |
| 4h@12:00 | Oct 02 12:00 → Oct 02 16:00 | BEARISH | 49.48% | Approved | $186.55 | — | — | End: No observed price within five minutes |
| 4h@16:00 | Oct 02 16:00 → Oct 05 07:00 | BEARISH | 46.02% | Approved | — | — | — | Pending target end |
| 1d@D+1 | Oct 02 04:00 → Oct 02 17:00 | BEARISH | 49.48% | Approved | — | — | — | Start: No observed price within five minutes; End: No observed price within five minutes |
| 1d@D+2 | Oct 05 04:00 → Oct 05 17:00 | BULLISH | 53.45% | Approved | — | — | — | Pending target end |
| 1d@D+3 | Oct 06 04:00 → Oct 06 17:00 | BULLISH | 50.94% | Approved | — | — | — | Pending target end |
| 1d@D+4 | Oct 07 04:00 → Oct 07 17:00 | BEARISH | 48.41% | Approved | — | — | — | Pending target end |
| 1d@D+5 | Oct 08 04:00 → Oct 08 17:00 | BEARISH | 47.74% | Approved | — | — | — | Pending target end |
| 1w@D+5 | Oct 02 04:00 → Oct 08 17:00 | BULLISH | 55.88% | Approved | — | — | — | Pending target end |

<details>
<summary>Missing price observation details</summary>

| Boundary | Observed evidence and source coverage |
| --- | --- |
| Price 04:00 | No observed price within five minutes (nearest Oct 02 04:24; 24 minutes away); source request covers the full tolerance window |
| Price 06:00 | No observed price within five minutes (nearest Oct 02 06:09; 9 minutes away); source request covers the full tolerance window |
| Price 14:00 | No observed price within five minutes (nearest Oct 02 14:26; 26 minutes away); source request covers the full tolerance window |
| Price 15:00 | No observed price within five minutes (nearest Oct 02 15:36; 36 minutes away); source request covers the full tolerance window |
| Price 16:00 | No observed price on the required side of the clock; source request covers the full tolerance window |
| Price 17:00 | No observed price within five minutes (nearest Oct 02 15:37; 83 minutes away); source request covers the full tolerance window |
| 1h@04:00 start | No observed price within five minutes (nearest Oct 02 04:24; 24 minutes away); source request covers the full tolerance window |
| 1h@04:00 end | No observed price within five minutes (nearest Oct 02 04:36; 24 minutes away); source request covers the full tolerance window |
| 1h@06:00 start | No observed price within five minutes (nearest Oct 02 06:09; 9 minutes away); source request covers the full tolerance window |
| 1h@13:00 end | No observed price within five minutes (nearest Oct 02 13:38; 22 minutes away); source request covers the full tolerance window |
| 1h@14:00 start | No observed price within five minutes (nearest Oct 02 14:26; 26 minutes away); source request covers the full tolerance window |
| 1h@15:00 start | No observed price within five minutes (nearest Oct 02 15:36; 36 minutes away); source request covers the full tolerance window |
| 1h@15:00 end | No observed price within five minutes (nearest Oct 02 15:37; 23 minutes away); source request covers the full tolerance window |
| 1h@16:00 start | No observed price on the required side of the clock; source request covers the full tolerance window |
| 1h@16:00 end | No observed price within five minutes (nearest Oct 02 15:37; 83 minutes away); source request covers the full tolerance window |
| 1h@gap start | No observed price within five minutes (nearest Oct 01 16:52; 8 minutes away); source request covers the full tolerance window |
| 1h@gap end | No observed price within five minutes (nearest Oct 02 04:24; 24 minutes away); source request covers the full tolerance window |
| 4h@04:00 start | No observed price within five minutes (nearest Oct 02 04:24; 24 minutes away); source request covers the full tolerance window |
| 4h@12:00 end | No observed price within five minutes (nearest Oct 02 15:37; 23 minutes away); source request covers the full tolerance window |
| 1d@D+1 start | No observed price within five minutes (nearest Oct 02 04:24; 24 minutes away); source request covers the full tolerance window |
| 1d@D+1 end | No observed price within five minutes (nearest Oct 02 15:37; 83 minutes away); source request covers the full tolerance window |

Complete source request coverage does not prove that no trades occurred. Missing observations remain unscored; nearby observations are shown only to explain the gap.

</details>

Actual prices use the Gameplan's own stock dataset and the existing five-minute boundary tolerance. The 17:00 price is a completed minute's closing price; earlier hourly clocks use opening prices. No missing prices are filled. Longer forecasts continue in the cumulative saved-Gameplan evaluation.

The machine-readable results retain the model's own frozen probability target and Brier score, plus separate raw-price and cost-adjusted outcomes. These are not broker fills or realized profit.
