# Duckets technicals

`technicals` is a Parquet-in, Parquet-out calculation layer. It does not fetch
provider data or depend on UI code.

## Input and joins

The runner reads normalized OHLCV files from stock and timeframe folders:

```text
DATASTORE/stocks/NVDA/bars/1m/databento/normalized/*.parquet
DATASTORE/stocks/NVDA/bars/1h/databento/normalized/*.parquet
DATASTORE/stocks/NVDA/bars/1d/schwab/normalized/*.parquet
```

Overlapping files are consolidated inside each provider/timeframe bucket by
`timestamp`. Natural timestamps, not observation or sample identifiers, drive
deduplication and joins.

Every technical output has exactly one Duckets-generated identifier:

```text
id = timestamp
```

Metric names such as `atr_14`, `trend_score`, `volatility_ratio`,
`breakout_readiness_score`, and `compression_score` are ordinary value columns.
They do not receive separate IDs.

## Stock-split adjustment

Databento OHLCV is raw market-scale data. Prices before a forward split remain
on the old per-share scale. That is correct for provider evidence, but the split
boundary must not enter a continuous technical calculation unadjusted.

FMP split history is read from:

```text
DATASTORE/stocks/<SYMBOL>/corporate/stock_splits/fmp/normalized/*.parquet
```

Before calculating, the loader:

1. reads split date, numerator, and denominator;
2. compares the observed pre-close/post-open ratio with the declared ratio;
3. multiplies earlier OHLC prices by `denominator / numerator`;
4. multiplies earlier volumes by `numerator / denominator`;
5. skips adjustment when the provider already reflects the split;
6. fails closed when a large unexplained discontinuity remains.

Outputs record readable adjustment status, event count, and event details. Raw
and normalized source Parquets are never rewritten.

## Market regime composite

`market-regime` produces a transparent directional score from 0 to 100:

- 40% ATR-normalized EMA trend;
- 30% volatility-adjusted momentum;
- 20% location inside the trailing range;
- 10% signed volume confirmation.

The output includes intermediate values, confidence, component agreement,
volatility regime, regime strength, and a categorical regime label.

### Adaptive history modes

`FULL` mode requires at least 60 usable bars. It uses ATR 14, EMA 20/50,
20-bar momentum, a 50-bar range, and a 20-bar volume baseline.

`BOOTSTRAP` mode applies when 15–59 usable bars exist. It preserves nominal
ATR-14 and EMA-20/50 formulas while permitting exponentially weighted estimates
after 4, 5, and 6 bars. It uses 5-bar momentum, a 15-bar range, and a 10-bar
volume baseline.

Canonical `atr_14`, `ema_20`, and `ema_50` values are populated in both modes.
`atr_min_periods`, `ema_fast_min_periods`, `ema_slow_min_periods`, and
`regime_mode` make the warm-up state explicit. Generic effective columns expose
the adaptive lookback values.

Bootstrap confidence is capped and rises from 45 toward 70 as history approaches
60 bars. Fixed-lookback mature columns remain null until their named windows are
actually available. Fewer than 15 usable bars fails closed.

One current Parquet is written per provider/timeframe:

```text
DATASTORE/stocks/<SYMBOL>/technicals/market-regime/<provider>/<timeframe>.parquet
```

## Breakout pressure

`breakout-pressure` asks whether price is compressing near a meaningful boundary
and whether a directional breakout has been confirmed. It combines:

- ATR and Bollinger-bandwidth compression against trailing baselines;
- prior-channel contraction;
- proximity to upper or lower channel boundaries;
- channel position, candle location, and volatility-adjusted momentum;
- volume participation;
- breakout magnitude in ATR units.

The channel is shifted by one bar before breakout testing, so the current bar is
not included in the boundary it is attempting to break.

Important value columns include:

```text
compression_score
range_contraction_score
breakout_readiness_score
direction_score
upside_pressure_score
downside_pressure_score
breakout_direction
breakout_magnitude_atr
breakout_strength_score
setup_quality
confidence_score
breakout_state
```

`breakout_state` is one of:

```text
BREAKOUT_UP
BREAKOUT_DOWN
COILED_UP
COILED_DOWN
COILED_NEUTRAL
EXPANDING_UP
EXPANDING_DOWN
NO_SETUP
```

Histories with at least 60 bars use full 20-bar channel and volatility windows.
Histories with 15–59 bars use an explicitly labelled bootstrap mode with
shorter fixed lookbacks, ATR-14 warm-up, and capped confidence.

Outputs are independent from market regime:

```text
DATASTORE/stocks/<SYMBOL>/technicals/breakout-pressure/<provider>/<timeframe>.parquet
```

## Run

```powershell
python -m technicals.main NVDA --datastore-target pc
python -m technicals.main NVDA --datastore-target local
python -m technicals.main NVDA --datastore-target pc --calculations breakout-pressure
python -m technicals.main NVDA --datastore-target local --providers databento --timeframes 1m 1h 1d
```

Custom paths:

```powershell
python -m technicals.main NVDA --datastore D:\custom\input --output-root D:\custom\nvda-technicals
```

Each run atomically replaces the current output for its
calculation/provider/timeframe. Loop A invokes the same calculations unless
`--skip-technicals` is supplied.
