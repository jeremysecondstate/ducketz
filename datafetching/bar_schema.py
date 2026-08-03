from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from datafetching.ids import add_readable_id

NORMALIZED_BAR_VALUE_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
NORMALIZED_BAR_COLUMNS = (
    "id",
    *NORMALIZED_BAR_VALUE_COLUMNS,
)
NORMALIZED_BAR_PRICE_COLUMNS = NORMALIZED_BAR_VALUE_COLUMNS[1:]
LEGACY_BAR_COMPLETION_COLUMNS = ("bar_complete", "bar_is_current")
NORMALIZED_BAR_ARROW_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("timestamp", pa.timestamp("ns", tz="UTC"), nullable=True),
        pa.field("open", pa.float64(), nullable=True),
        pa.field("high", pa.float64(), nullable=True),
        pa.field("low", pa.float64(), nullable=True),
        pa.field("close", pa.float64(), nullable=True),
        pa.field("volume", pa.float64(), nullable=True),
    ]
)
_REQUIRED_NORMALIZED_BAR_COLUMNS = NORMALIZED_BAR_VALUE_COLUMNS[:-1]
_FETCH_TIMESTAMP_PATTERN = re.compile(r"_(\d{8}T\d{6}(?:\.\d+)?Z)$")


def project_normalized_bar_frame(
    frame: pd.DataFrame,
    *,
    keep_legacy_completion: bool = False,
) -> pd.DataFrame:
    """Return the canonical readable-ID plus OHLCV storage frame."""

    missing = [
        column
        for column in _REQUIRED_NORMALIZED_BAR_COLUMNS
        if column not in frame.columns
    ]
    if missing:
        raise ValueError(
            "Normalized bar data is missing columns: " + ", ".join(missing)
        )

    output = frame.copy()
    if "volume" not in output.columns:
        output["volume"] = 0.0
    output["timestamp"] = pd.to_datetime(
        output["timestamp"], utc=True, errors="coerce"
    ).astype("datetime64[ns, UTC]")
    for column in NORMALIZED_BAR_PRICE_COLUMNS:
        output[column] = pd.to_numeric(output[column], errors="coerce").astype(
            "float64"
        )
    output = add_readable_id(output, key_columns=("timestamp",))

    columns = list(NORMALIZED_BAR_COLUMNS)
    if keep_legacy_completion:
        columns.extend(
            column
            for column in LEGACY_BAR_COMPLETION_COLUMNS
            if column in output.columns
        )
    return output.loc[:, columns]


def read_normalized_bar_parquet(
    path: Path,
    *,
    include_legacy_completion: bool = False,
) -> tuple[pd.DataFrame, pa.Schema]:
    """Read canonical ID/OHLCV columns and return the physical source schema."""

    physical_schema = pq.read_schema(path)
    available = set(physical_schema.names)
    missing = [
        column
        for column in _REQUIRED_NORMALIZED_BAR_COLUMNS
        if column not in available
    ]
    if missing:
        raise ValueError(
            f"Normalized bar parquet {path} is missing columns: "
            + ", ".join(missing)
        )

    selected = [
        column for column in NORMALIZED_BAR_COLUMNS if column in available
    ]
    if include_legacy_completion:
        selected.extend(
            column
            for column in LEGACY_BAR_COMPLETION_COLUMNS
            if column in available
        )
    frame = pd.read_parquet(path, columns=selected)
    return (
        project_normalized_bar_frame(
            frame,
            keep_legacy_completion=include_legacy_completion,
        ),
        physical_schema,
    )


def read_bar_timestamp_and_completion(
    path: Path,
) -> tuple[pd.DataFrame, pa.Schema]:
    """Read timestamp plus optional legacy completion flags from a bar Parquet."""

    physical_schema = pq.read_schema(path)
    available = set(physical_schema.names)
    if "timestamp" not in available:
        raise ValueError(f"Normalized bar parquet {path} is missing timestamp")
    selected = ["timestamp"]
    selected.extend(
        column
        for column in LEGACY_BAR_COMPLETION_COLUMNS
        if column in available
    )
    frame = pd.read_parquet(path, columns=selected)
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"], utc=True, errors="coerce"
    ).astype("datetime64[ns, UTC]")
    return frame, physical_schema


def legacy_bar_completion_mask(frame: pd.DataFrame) -> pd.Series:
    """Exclude rows carrying an explicit legacy incomplete/current marker."""

    eligible = pd.Series(True, index=frame.index, dtype=bool)
    if "bar_complete" in frame.columns:
        eligible &= ~_explicit_boolean(frame["bar_complete"], expected=False)
    if "bar_is_current" in frame.columns:
        eligible &= ~_explicit_boolean(frame["bar_is_current"], expected=True)
    return eligible


def normalized_bar_schema_is_canonical(schema: pa.Schema) -> bool:
    """Return whether an Arrow schema exactly matches normalized bar storage."""

    if schema.names != NORMALIZED_BAR_ARROW_SCHEMA.names:
        return False
    return all(
        schema.field(column).type == NORMALIZED_BAR_ARROW_SCHEMA.field(column).type
        for column in NORMALIZED_BAR_COLUMNS
    )


def write_normalized_bar_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Write one normalized bar frame with fixed Arrow names, order, and dtypes."""

    output = project_normalized_bar_frame(frame)
    table = pa.Table.from_pandas(
        output,
        schema=NORMALIZED_BAR_ARROW_SCHEMA,
        preserve_index=False,
        safe=True,
    )
    pq.write_table(table, path)


def normalized_bar_canonical_path(path: Path) -> Path:
    """Return the stable path represented by a legacy timestamp-suffixed path."""

    stem = _FETCH_TIMESTAMP_PATTERN.sub("", path.stem)
    return path.with_name(stem + path.suffix)


def normalized_bar_file_sort_key(path: Path) -> tuple[int, int, str]:
    """Order legacy snapshots oldest-first and the stable canonical file last."""

    match = _FETCH_TIMESTAMP_PATTERN.search(path.stem)
    if match is None:
        return (1, 0, path.name)
    timestamp = pd.to_datetime(match.group(1), utc=True, errors="coerce")
    value = -1 if pd.isna(timestamp) else int(timestamp.value)
    return (0, value, path.name)


def _explicit_boolean(series: pd.Series, *, expected: bool) -> pd.Series:
    strings = series.astype("string").str.strip().str.lower()
    numeric = pd.to_numeric(series, errors="coerce")
    if expected:
        recognized = strings.isin({"true", "1", "yes"}) | numeric.eq(1)
    else:
        recognized = strings.isin({"false", "0", "no"}) | numeric.eq(0)
    return series.notna() & recognized.fillna(False)
