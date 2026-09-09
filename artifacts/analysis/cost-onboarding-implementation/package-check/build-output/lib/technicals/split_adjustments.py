from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from datafetching.layout import safe_token

_FETCH_TIMESTAMP_PATTERN = re.compile(r"(\d{8}T\d{6}(?:\.\d+)?Z)$")
PRICE_COLUMNS = ("open", "high", "low", "close")
EXTREME_DISCONTINUITY_RATIO = 3.0
RATIO_TOLERANCE = 1.35


@dataclass(frozen=True)
class SplitEvent:
    ex_date: pd.Timestamp
    numerator: float
    denominator: float
    source_file: Path

    @property
    def split_ratio(self) -> float:
        return self.numerator / self.denominator

    @property
    def price_factor(self) -> float:
        return self.denominator / self.numerator

    @property
    def volume_factor(self) -> float:
        return self.numerator / self.denominator

    def as_metadata(self, *, action: str) -> dict[str, object]:
        return {
            "ex_date": self.ex_date.date().isoformat(),
            "numerator": self.numerator,
            "denominator": self.denominator,
            "split_ratio": self.split_ratio,
            "price_factor": self.price_factor,
            "volume_factor": self.volume_factor,
            "action": action,
            "source_file": str(self.source_file),
        }


@dataclass(frozen=True)
class SplitAdjustmentResult:
    frame: pd.DataFrame
    status: str
    applied_events: tuple[dict[str, object], ...]
    reflected_events: tuple[dict[str, object], ...]

    @property
    def event_count(self) -> int:
        return len(self.applied_events) + len(self.reflected_events)

    @property
    def metadata_json(self) -> str:
        return json.dumps(
            [*self.applied_events, *self.reflected_events],
            sort_keys=True,
            default=str,
        )


def discover_split_events(datastore_root: Path, *, symbol: str) -> tuple[SplitEvent, ...]:
    """Load the newest FMP stock-split rows and return validated split events."""
    clean_symbol = symbol.strip().upper()
    modern_folder = (
        datastore_root
        / "stocks"
        / safe_token(clean_symbol)
        / "corporate"
        / "stock_splits"
        / "fmp"
        / "normalized"
    )
    legacy_folder = datastore_root / "normalized" / "fmp" / "corporate"

    paths = sorted(modern_folder.glob("*.parquet"), key=_file_sort_key) if modern_folder.is_dir() else []
    if not paths and legacy_folder.is_dir():
        paths = sorted(
            legacy_folder.glob(f"{safe_token(clean_symbol)}_stock_splits_*.parquet"),
            key=_file_sort_key,
        )
    if not paths:
        return ()

    frames: list[pd.DataFrame] = []
    for file_order, path in enumerate(paths):
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:
            raise RuntimeError(f"Could not read split parquet {path}: {exc}") from exc
        if frame.empty:
            continue
        frame = frame.copy()
        frame["_source_file"] = str(path)
        frame["_file_order"] = file_order
        frames.append(frame)

    if not frames:
        return ()

    combined = pd.concat(frames, ignore_index=True, sort=False)
    required = ("date", "numerator", "denominator")
    missing = [column for column in required if column not in combined.columns]
    if missing:
        raise ValueError(
            "FMP stock-split parquet is missing columns: " + ", ".join(missing)
        )

    if "symbol" in combined.columns:
        combined = combined.loc[
            combined["symbol"].astype(str).str.strip().str.upper().eq(clean_symbol)
        ].copy()
    elif "fmp_symbol" in combined.columns:
        combined = combined.loc[
            combined["fmp_symbol"].astype(str).str.strip().str.upper().eq(clean_symbol)
        ].copy()

    combined["date"] = pd.to_datetime(combined["date"], utc=True, errors="coerce")
    combined["numerator"] = pd.to_numeric(combined["numerator"], errors="coerce")
    combined["denominator"] = pd.to_numeric(combined["denominator"], errors="coerce")
    combined = combined.dropna(subset=["date", "numerator", "denominator"])
    combined = combined.loc[
        combined["numerator"].gt(0)
        & combined["denominator"].gt(0)
        & combined["numerator"].ne(combined["denominator"])
    ]
    combined = (
        combined.sort_values(["date", "_file_order"])
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
    )

    return tuple(
        SplitEvent(
            ex_date=row["date"],
            numerator=float(row["numerator"]),
            denominator=float(row["denominator"]),
            source_file=Path(str(row["_source_file"])),
        )
        for row in combined.to_dict(orient="records")
    )


def apply_split_adjustments(
    bars: pd.DataFrame,
    *,
    events: tuple[SplitEvent, ...],
    provider: str,
    timeframe: str,
) -> SplitAdjustmentResult:
    """Back-adjust OHLCV only when the observed discontinuity confirms the split."""
    adjusted = bars.copy()
    applied: list[dict[str, object]] = []
    reflected: list[dict[str, object]] = []

    if adjusted.empty:
        return SplitAdjustmentResult(adjusted, "NO_BARS", (), ())

    adjusted["timestamp"] = pd.to_datetime(adjusted["timestamp"], utc=True, errors="coerce")
    adjusted = adjusted.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    relevant_events = tuple(
        event
        for event in events
        if adjusted["timestamp"].lt(event.ex_date).any()
        and adjusted["timestamp"].ge(event.ex_date).any()
    )

    if not relevant_events:
        _raise_for_unexplained_discontinuity(adjusted, provider=provider, timeframe=timeframe)
        return SplitAdjustmentResult(adjusted, "NO_SPLIT_EVENTS_IN_RANGE", (), ())

    for event in relevant_events:
        pre_rows = adjusted.loc[adjusted["timestamp"].lt(event.ex_date)]
        post_rows = adjusted.loc[adjusted["timestamp"].ge(event.ex_date)]
        pre_close = float(pre_rows.iloc[-1]["close"])
        post_open = float(post_rows.iloc[0]["open"])
        if not _positive_finite(pre_close) or not _positive_finite(post_open):
            raise ValueError(
                f"Cannot validate {event.ex_date.date()} split for {provider}/{timeframe}: "
                "boundary prices are not positive finite values."
            )

        observed_ratio = pre_close / post_open
        expected_ratio = event.split_ratio
        if _ratio_matches(observed_ratio, expected_ratio):
            mask = adjusted["timestamp"].lt(event.ex_date)
            for column in PRICE_COLUMNS:
                adjusted.loc[mask, column] = adjusted.loc[mask, column] * event.price_factor
            if "volume" in adjusted.columns:
                adjusted.loc[mask, "volume"] = adjusted.loc[mask, "volume"] * event.volume_factor
            applied.append(event.as_metadata(action="APPLIED"))
            continue

        if _ratio_matches(observed_ratio, 1.0):
            reflected.append(event.as_metadata(action="ALREADY_REFLECTED"))
            continue

        raise ValueError(
            f"Ambiguous split boundary for {provider}/{timeframe} on {event.ex_date.date()}: "
            f"observed pre-close/post-open ratio {observed_ratio:.6g}, "
            f"expected {expected_ratio:.6g} or approximately 1 for already-adjusted data."
        )

    _raise_for_unexplained_discontinuity(
        adjusted,
        provider=provider,
        timeframe=timeframe,
        ignored_dates={event.ex_date.normalize() for event in relevant_events},
    )

    if applied and reflected:
        status = "SPLIT_ADJUSTED_WITH_PREEXISTING_ADJUSTMENTS"
    elif applied:
        status = "SPLIT_ADJUSTED"
    else:
        status = "SPLITS_ALREADY_REFLECTED"
    return SplitAdjustmentResult(
        adjusted,
        status,
        tuple(applied),
        tuple(reflected),
    )


def _raise_for_unexplained_discontinuity(
    frame: pd.DataFrame,
    *,
    provider: str,
    timeframe: str,
    ignored_dates: set[pd.Timestamp] | None = None,
) -> None:
    if len(frame) < 2:
        return
    ignored = ignored_dates or set()
    previous_close = pd.to_numeric(frame["close"], errors="coerce").shift(1)
    current_open = pd.to_numeric(frame["open"], errors="coerce")
    ratio = previous_close / current_open
    dates = frame["timestamp"].dt.normalize()
    extreme = ratio.gt(EXTREME_DISCONTINUITY_RATIO) | ratio.lt(1 / EXTREME_DISCONTINUITY_RATIO)
    extreme = extreme & ~dates.isin(ignored)
    if not extreme.any():
        return

    index = extreme[extreme].index[0]
    event_date = frame.loc[index, "timestamp"].date().isoformat()
    observed = float(ratio.loc[index])
    raise ValueError(
        f"Unexplained {observed:.6g}x price discontinuity in {provider}/{timeframe} "
        f"at {event_date}. Fetch FMP stock_splits data before running technicals."
    )


def _ratio_matches(observed: float, expected: float) -> bool:
    if not _positive_finite(observed) or not _positive_finite(expected):
        return False
    return abs(math.log(observed / expected)) <= math.log(RATIO_TOLERANCE)


def _positive_finite(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _file_sort_key(path: Path) -> tuple[int, str]:
    match = _FETCH_TIMESTAMP_PATTERN.search(path.stem)
    if match is None:
        return 0, path.name
    timestamp = pd.to_datetime(match.group(1), utc=True, errors="coerce")
    if pd.isna(timestamp):
        return 0, path.name
    return int(timestamp.value), path.name
