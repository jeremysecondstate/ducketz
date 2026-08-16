from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from datafetching.databento_history_policy import INTERVAL_LOOKBACK_DAYS


@dataclass(frozen=True)
class SchwabPriceHistorySpec:
    key: str
    period_type: str
    period: int
    frequency_type: str
    frequency: int
    need_extended_hours_data: bool = False


@dataclass(frozen=True)
class DatabentoAnalysisSourceSpec:
    key: str
    schema: str
    frequency: str
    lookback: timedelta


@dataclass(frozen=True)
class DatabentoAnalysisBarSpec:
    key: str
    source_key: str
    output_frequency: str
    aggregation_method: str


def schwab_price_history_specs() -> tuple[SchwabPriceHistorySpec, ...]:
    specs: list[SchwabPriceHistorySpec] = []

    for period in (1, 2, 3, 4, 5, 10):
        for frequency in (1, 5, 10, 15, 30):
            specs.append(
                SchwabPriceHistorySpec(
                    key=f"day_{period}_minute_{frequency}",
                    period_type="day",
                    period=period,
                    frequency_type="minute",
                    frequency=frequency,
                    need_extended_hours_data=True,
                )
            )

    for period in (1, 2, 3, 6):
        for frequency_type in ("daily", "weekly"):
            specs.append(
                SchwabPriceHistorySpec(
                    key=f"month_{period}_{frequency_type}_1",
                    period_type="month",
                    period=period,
                    frequency_type=frequency_type,
                    frequency=1,
                )
            )

    for period in (1, 2, 3, 5, 10, 15, 20):
        for frequency_type in ("daily", "weekly", "monthly"):
            specs.append(
                SchwabPriceHistorySpec(
                    key=f"year_{period}_{frequency_type}_1",
                    period_type="year",
                    period=period,
                    frequency_type=frequency_type,
                    frequency=1,
                )
            )

    for frequency_type in ("daily", "weekly"):
        specs.append(
            SchwabPriceHistorySpec(
                key=f"ytd_1_{frequency_type}_1",
                period_type="ytd",
                period=1,
                frequency_type=frequency_type,
                frequency=1,
            )
        )

    return tuple(specs)


def databento_analysis_source_specs() -> tuple[DatabentoAnalysisSourceSpec, ...]:
    return (
        DatabentoAnalysisSourceSpec(
            key="source_5d_1s",
            schema="ohlcv-1s",
            frequency="1s",
            lookback=timedelta(days=INTERVAL_LOOKBACK_DAYS["1s"]),
        ),
        DatabentoAnalysisSourceSpec(
            key="source_100d_1m",
            schema="ohlcv-1m",
            frequency="1m",
            lookback=timedelta(days=INTERVAL_LOOKBACK_DAYS["1m"]),
        ),
        DatabentoAnalysisSourceSpec(
            key="source_1825d_1h",
            schema="ohlcv-1h",
            frequency="1h",
            lookback=timedelta(days=INTERVAL_LOOKBACK_DAYS["1h"]),
        ),
        DatabentoAnalysisSourceSpec(
            key="source_2555d_1d",
            schema="ohlcv-1d",
            frequency="1d",
            lookback=timedelta(days=INTERVAL_LOOKBACK_DAYS["1d"]),
        ),
    )


def databento_analysis_bar_specs() -> tuple[DatabentoAnalysisBarSpec, ...]:
    return (
        DatabentoAnalysisBarSpec("analysis_5d_1s", "source_5d_1s", "1s", "native"),
        DatabentoAnalysisBarSpec("analysis_5d_5s", "source_5d_1s", "5s", "resampled_from_1s"),
        DatabentoAnalysisBarSpec("analysis_5d_15s", "source_5d_1s", "15s", "resampled_from_1s"),
        DatabentoAnalysisBarSpec("analysis_5d_30s", "source_5d_1s", "30s", "resampled_from_1s"),
        DatabentoAnalysisBarSpec("analysis_100d_1m", "source_100d_1m", "1m", "native"),
        DatabentoAnalysisBarSpec("analysis_100d_5m", "source_100d_1m", "5m", "resampled_from_1m"),
        DatabentoAnalysisBarSpec("analysis_100d_15m", "source_100d_1m", "15m", "resampled_from_1m"),
        DatabentoAnalysisBarSpec("analysis_100d_30m", "source_100d_1m", "30m", "resampled_from_1m"),
        DatabentoAnalysisBarSpec("analysis_1825d_1h", "source_1825d_1h", "1h", "native"),
        DatabentoAnalysisBarSpec("analysis_1825d_2h", "source_1825d_1h", "2h", "resampled_from_1h"),
        DatabentoAnalysisBarSpec("analysis_1825d_4h", "source_1825d_1h", "4h", "resampled_from_1h"),
        DatabentoAnalysisBarSpec("analysis_2555d_1d", "source_2555d_1d", "1d", "native"),
        DatabentoAnalysisBarSpec("analysis_2555d_1w", "source_2555d_1d", "1w", "resampled_from_1d"),
        DatabentoAnalysisBarSpec("analysis_2555d_1mo", "source_2555d_1d", "1mo", "resampled_from_1d"),
    )
