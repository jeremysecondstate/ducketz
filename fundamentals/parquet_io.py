from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from datafetching.ids import (
    add_readable_id,
    minimum_unique_key,
    without_internal_identity_columns,
)
from datafetching.layout import safe_token

STATEMENT_KEYS = {
    "quarterly": (
        "income_statement_quarterly",
        "balance_sheet_statement_quarterly",
        "cash_flow_statement_quarterly",
    ),
    "annual": (
        "income_statement_annual",
        "balance_sheet_statement_annual",
        "cash_flow_statement_annual",
    ),
}
LEGACY_ANNUAL_KEYS = (
    "income_statement",
    "balance_sheet_statement",
    "cash_flow_statement",
)


def discover_statement_frames(
    datastore_root: Path,
    *,
    symbol: str,
    period_type: str,
    source: str = "fmp",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, tuple[Path, ...]]:
    cadence = period_type.strip().lower()
    if cadence not in STATEMENT_KEYS:
        raise ValueError("period_type must be quarterly or annual")
    keys = STATEMENT_KEYS[cadence]
    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    for index, key in enumerate(keys):
        frame, found = _read_dataset(datastore_root, symbol=symbol, source=source, dataset_key=key)
        if frame.empty and cadence == "annual":
            frame, found = _read_dataset(
                datastore_root,
                symbol=symbol,
                source=source,
                dataset_key=LEGACY_ANNUAL_KEYS[index],
            )
        frames.append(frame)
        files.extend(found)
    return frames[0], frames[1], frames[2], tuple(dict.fromkeys(files))


def write_fundamental_parquet(
    datastore_root: Path,
    *,
    symbol: str,
    period_type: str,
    source: str,
    frame: pd.DataFrame,
    source_files: Iterable[Path],
) -> Path:
    cadence = period_type.strip().lower()
    folder = (
        datastore_root
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "fundamentals"
        / "fundamental-direction"
        / safe_token(source)
    )
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_token(cadence)}.parquet"

    values = without_internal_identity_columns(frame)
    natural_keys = minimum_unique_key(
        values,
        (
            ("period_end_date",),
            ("period_end_date", "fiscal_period"),
        ),
    )
    output = add_readable_id(values, key_columns=natural_keys)
    output["input_source_file_count"] = len(tuple(source_files))
    if path.is_file():
        existing = pd.read_parquet(path)
        if _stable_equal(existing, output):
            return path

    temporary = path.with_suffix(".tmp.parquet")
    output.to_parquet(temporary, index=False)
    temporary.replace(path)
    return path


def load_fundamental_parquet(
    fundamentals_root: Path,
    *,
    period_type: str = "quarterly",
    source: str = "fmp",
) -> pd.DataFrame:
    path = fundamentals_root / "fundamental-direction" / safe_token(source) / f"{safe_token(period_type)}.parquet"
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_parquet(path)


def _read_dataset(
    datastore_root: Path,
    *,
    symbol: str,
    source: str,
    dataset_key: str,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    folder = (
        datastore_root
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "corporate"
        / safe_token(dataset_key)
        / safe_token(source)
        / "normalized"
    )
    paths = tuple(sorted(folder.glob("*.parquet"))) if folder.is_dir() else ()
    if not paths:
        return pd.DataFrame(), ()
    frames = [pd.read_parquet(path) for path in paths]
    return pd.concat(frames, ignore_index=True, sort=False), paths


def _stable_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    volatile = {"calculated_at", "latest_source_fetch"}
    columns = list(dict.fromkeys([*left.columns, *right.columns]))
    left_stable = left.reindex(columns=columns).drop(columns=list(volatile & set(columns)))
    right_stable = right.reindex(columns=columns).drop(columns=list(volatile & set(columns)))
    try:
        pd.testing.assert_frame_equal(
            left_stable.reset_index(drop=True),
            right_stable.reset_index(drop=True),
            check_dtype=False,
            check_like=False,
        )
    except AssertionError:
        return False
    return True
