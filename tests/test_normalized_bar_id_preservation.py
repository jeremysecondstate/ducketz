from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models.market_data import MarketBar
import datafetching.bar_schema as bar_schema
from datafetching.bar_schema import (
    NORMALIZED_BAR_VALUE_COLUMNS,
    read_normalized_bar_parquet,
    write_normalized_bar_parquet,
)
from datafetching.continuation import (
    latest_normalized_bar_timestamp,
    normalized_bar_path,
)
from datafetching.parquet_store import ParquetStore
from technicals.parquet_io import discover_bar_datasets


REQUEST_KEY = "source_1000d_1m_ohlcv-1m_1m"


def test_canonical_ids_survive_continuation_overlap_and_revision(
    tmp_path: Path,
) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    frame = _frame(("2026-08-05T10:00:00Z", "2026-08-05T10:01:00Z"))
    frame.insert(0, "id", ("stored-bar-zero", "stored-bar-one"))
    write_normalized_bar_parquet(frame, path)

    store = ParquetStore(tmp_path)
    store.save_bars(
        "databento",
        "GOOG",
        "1m",
        (
            _bar("2026-08-05T10:01:00Z", close=101.5, volume=1_001.0),
            _bar("2026-08-05T10:02:00Z", close=102.0),
        ),
        request_key=REQUEST_KEY,
        as_of=pd.Timestamp("2026-08-05T10:04:00Z"),
    )
    continued = pd.read_parquet(path).set_index("timestamp")
    assert continued.loc[pd.Timestamp("2026-08-05T10:00:00Z"), "id"] == "stored-bar-zero"
    assert continued.loc[pd.Timestamp("2026-08-05T10:01:00Z"), "id"] == "stored-bar-one"

    before_replay = path.read_bytes()
    replay = store.save_bars(
        "databento",
        "GOOG",
        "1m",
        (
            _bar("2026-08-05T10:01:00Z", close=101.5, volume=1_001.0),
            _bar("2026-08-05T10:02:00Z", close=102.0),
        ),
        request_key=REQUEST_KEY,
        as_of=pd.Timestamp("2026-08-05T10:04:00Z"),
    )
    assert replay is None
    assert path.read_bytes() == before_replay

    store.save_bars(
        "databento",
        "GOOG",
        "1m",
        (_bar("2026-08-05T10:01:00Z", close=101.75),),
        request_key=REQUEST_KEY,
        as_of=pd.Timestamp("2026-08-05T10:04:00Z"),
    )
    revised = pd.read_parquet(path).set_index("timestamp")
    assert revised.loc[pd.Timestamp("2026-08-05T10:01:00Z"), "id"] == "stored-bar-one"
    assert revised.loc[pd.Timestamp("2026-08-05T10:01:00Z"), "close"] == 101.75


def test_legacy_missing_and_invalid_ids_are_repaired_without_replacing_valid_ids(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.parquet"
    _frame(("2026-08-05T10:00:00Z",)).to_parquet(missing_path, index=False)
    missing, _ = read_normalized_bar_parquet(missing_path)
    assert missing["id"].tolist() == ["2026-08-05T10:00:00Z"]

    invalid_path = tmp_path / "invalid.parquet"
    invalid = _frame(
        (
            "2026-08-05T10:00:00Z",
            "2026-08-05T10:01:00Z",
            "2026-08-05T10:02:00Z",
        )
    )
    invalid.insert(0, "id", ("valid-stored-id", "", "a" * 32))
    invalid.to_parquet(invalid_path, index=False)
    repaired, _ = read_normalized_bar_parquet(invalid_path)
    assert repaired["id"].tolist() == [
        "valid-stored-id",
        "2026-08-05T10:01:00Z",
        "2026-08-05T10:02:00Z",
    ]


def test_analytical_reads_and_loop_b_discovery_do_not_generate_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    _frame(("2026-08-05T10:00:00Z", "2026-08-05T10:01:00Z")).to_parquet(
        path,
        index=False,
    )

    def unexpected_id_generation(*_args: object, **_kwargs: object) -> pd.DataFrame:
        raise AssertionError("analytical read generated normalized-bar IDs")

    monkeypatch.setattr(bar_schema, "add_readable_id", unexpected_id_generation)
    analytical, _ = read_normalized_bar_parquet(path, include_ids=False)
    assert tuple(analytical.columns) == NORMALIZED_BAR_VALUE_COLUMNS
    datasets = discover_bar_datasets(
        tmp_path,
        symbol="GOOG",
        providers=("databento",),
        timeframes={"1m"},
    )
    assert len(datasets) == 1
    assert "id" not in datasets[0].frame.columns


def test_continuation_uses_only_maximum_stored_timestamp_even_without_ids(
    tmp_path: Path,
) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    _frame(
        (
            "2026-08-05T10:02:00Z",
            "2026-08-05T10:00:00Z",
            "2026-08-05T10:01:00Z",
        )
    ).to_parquet(path, index=False)

    latest = latest_normalized_bar_timestamp(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key=REQUEST_KEY,
    )
    assert pd.Timestamp(latest) == pd.Timestamp("2026-08-05T10:02:00Z")


def _path(root: Path) -> Path:
    return normalized_bar_path(
        root,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key=REQUEST_KEY,
    )


def _frame(timestamps: tuple[str, ...]) -> pd.DataFrame:
    count = len(timestamps)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": [100.0 + index for index in range(count)],
            "high": [101.0 + index for index in range(count)],
            "low": [99.0 + index for index in range(count)],
            "close": [100.5 + index for index in range(count)],
            "volume": [1_000.0 + index for index in range(count)],
        }
    )


def _bar(timestamp: str, *, close: float, volume: float = 1_000.0) -> MarketBar:
    return MarketBar(
        symbol="GOOG",
        source="databento",
        timeframe="1m",
        timestamp=pd.Timestamp(timestamp).to_pydatetime(),
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.5,
        close=close,
        volume=volume,
    )
