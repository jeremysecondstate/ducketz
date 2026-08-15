from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from datafetching.decision_time import completed_bar_clock_for_target
def test_obsolete_no_trade_proposal_remains_retired() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "datafetch-ml"
        / "opra-underlying-no-trade-proposal-v2.json"
    )
    assert not path.exists()


def test_active_completed_bar_contract_still_rejects_prior_minute_substitution(
    tmp_path: Path,
) -> None:
    path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "bars"
        / "1m"
        / "databento"
        / "normalized"
        / "source.parquet"
    )
    path.parent.mkdir(parents=True)
    _bars(["2026-04-10T18:58:00Z"]).to_parquet(path, index=False)

    with pytest.raises(FileNotFoundError, match="Exact completed"):
        completed_bar_clock_for_target(
            tmp_path,
            symbol="GOOG",
            target_snapshot_for="2026-04-10T19:00:00Z",
            as_of="2026-04-10T19:00:00Z",
        )

    _bars(
        ["2026-04-10T18:58:00Z", "2026-04-10T18:59:00Z"]
    ).to_parquet(path, index=False)
    exact = completed_bar_clock_for_target(
        tmp_path,
        symbol="GOOG",
        target_snapshot_for="2026-04-10T19:00:00Z",
        as_of="2026-04-10T19:00:00Z",
    )
    assert exact.bar_timestamp == pd.Timestamp("2026-04-10T18:59:00Z")
    assert exact.decision_timestamp == pd.Timestamp("2026-04-10T19:00:00Z")


def _bars(timestamps: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": "GOOG",
            "source": "databento",
            "timeframe": "1m",
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000.0,
            "provider_timeframe": "1m",
            "canonical_timeframe": "1m",
            "request_key": "source_1000d_1m_ohlcv-1m_1m",
            "provider_dataset": "EQUS.MINI",
            "bar_complete": True,
            "session_type": "REGULAR",
        }
    )
