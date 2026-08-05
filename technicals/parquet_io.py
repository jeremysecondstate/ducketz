from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from datafetching.bar_schema import (
    legacy_bar_completion_mask,
    normalized_bar_file_sort_key,
    read_normalized_bar_parquet,
)
from datafetching.bar_timing import annotate_bar_timing
from datafetching.ids import add_readable_id, without_internal_identity_columns
from datafetching.layout import safe_token
from fundamentals.join import attach_fundamental_context
from technicals.calculations.bar_shape import (
    CALCULATION_NAME as BAR_SHAPE_CALCULATION,
)
from technicals.calculations.weekly_context import (
    CALCULATION_NAME as WEEKLY_CONTEXT_CALCULATION,
    OUTPUT_TIMEFRAME as WEEKLY_CONTEXT_TIMEFRAME,
)
from technicals.split_adjustments import apply_split_adjustments, discover_split_events

_DAILY_SESSION_CALENDAR = "XNYS"
_DAILY_SESSION_CLEANUP_VERSION = "xnys-exact-non-session-duplicate-v1"
_PRICE_EVIDENCE_COLUMNS = ("open", "high", "low", "close", "volume")
_BAR_CONTEXT_COLUMNS = (
    "bar_end_timestamp",
    "bar_complete",
    "bar_is_current",
    "bar_complete_as_of",
    "bar_duration_seconds",
    "bar_timing_version",
    "session_type",
    "session_date",
    "session_minute",
    "session_progress",
)
_NEW_CALCULATION_NATURAL_KEY = (
    "symbol",
    "provider",
    "timeframe",
    "bar_timestamp",
)
_BAR_SHAPE_OUTPUT_COLUMNS = (
    "id",
    "symbol",
    "provider",
    "timeframe",
    "bar_timestamp",
    "bar_end_timestamp",
    "bar_complete",
    "available_at",
    "calculation",
    "calculation_version",
    "price_adjustment_status",
    "split_event_count",
    "overnight_gap_atr",
    "intrabar_range_atr",
    "close_location",
)
_WEEKLY_CONTEXT_OUTPUT_COLUMNS = (
    "id",
    "symbol",
    "provider",
    "timeframe",
    "source_timeframe",
    "exchange_calendar",
    "week_start_session",
    "week_end_session",
    "bar_timestamp",
    "bar_end_timestamp",
    "bar_complete",
    "available_at",
    "calculation",
    "calculation_version",
    "market_regime_calculation_version",
    "breakout_pressure_calculation_version",
    "availability_rule_version",
    "price_adjustment_status",
    "split_event_count",
    "constituent_session_count",
    "constituent_complete",
    "technical_score",
    "technical_score_change_5",
    "breakout_readiness_score",
)


@dataclass(frozen=True)
class BarDataset:
    provider: str
    timeframe: str
    symbol: str
    frame: pd.DataFrame
    source_files: tuple[Path, ...]
    adjustment_status: str
    split_event_count: int
    split_events_json: str
    incomplete_bar_count: int = 0
    excluded_non_session_duplicate_count: int = 0
    daily_session_cleanup_version: str = "not-applicable"


def discover_bar_datasets(
    datastore_root: Path,
    *,
    symbol: str,
    providers: Iterable[str],
    timeframes: set[str] | None = None,
) -> tuple[BarDataset, ...]:
    """Load, consolidate, finalize, and split-adjust OHLCV by provider/timeframe."""
    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Symbol is required.")

    datasets: list[BarDataset] = []
    selected_timeframes = {
        value.strip().lower() for value in timeframes or set() if value.strip()
    }
    split_events = discover_split_events(datastore_root, symbol=clean_symbol)

    for provider in providers:
        clean_provider = provider.strip().lower()
        path_entries = _bar_path_entries(
            datastore_root,
            symbol=clean_symbol,
            provider=clean_provider,
        )
        grouped_frames: dict[str, list[pd.DataFrame]] = {}
        grouped_files: dict[str, list[Path]] = {}

        for file_order, (path, folder_timeframe) in enumerate(path_entries):
            # Canonical-layout files already declare their timeframe in the
            # directory path.  Apply the requested filter before reading the
            # Parquet so a horizon-scoped consumer does not deserialize every
            # unrelated bar history only to discard it below.
            if (
                folder_timeframe
                and selected_timeframes
                and folder_timeframe.strip().lower() not in selected_timeframes
            ):
                continue
            try:
                if folder_timeframe:
                    frame, _physical_schema = read_normalized_bar_parquet(
                        path,
                        include_legacy_completion=True,
                        include_ids=False,
                    )
                else:
                    frame = pd.read_parquet(path)
            except Exception as exc:
                raise RuntimeError(f"Could not read bar parquet {path}: {exc}") from exc
            if frame.empty:
                continue

            if "symbol" in frame.columns:
                frame = frame.loc[
                    frame["symbol"].astype(str).str.strip().str.upper().eq(clean_symbol)
                ].copy()
            if frame.empty:
                continue

            if folder_timeframe:
                frame["_canonical_timeframe"] = folder_timeframe
            else:
                frame["_canonical_timeframe"] = _canonical_timeframe(
                    frame, clean_provider
                )
            frame["_source_file"] = str(path)
            frame["_file_order"] = file_order

            for timeframe, timeframe_frame in frame.groupby(
                "_canonical_timeframe", dropna=False
            ):
                canonical = str(timeframe).strip().lower()
                if not canonical or canonical == "nan":
                    continue
                if selected_timeframes and canonical not in selected_timeframes:
                    continue
                grouped_frames.setdefault(canonical, []).append(timeframe_frame)
                grouped_files.setdefault(canonical, []).append(path)

        for timeframe, frames in sorted(grouped_frames.items()):
            combined = pd.concat(frames, ignore_index=True, sort=False)
            (
                normalized,
                incomplete_count,
                excluded_non_session_duplicate_count,
            ) = _normalize_bar_frame(combined, timeframe=timeframe)
            if normalized.empty:
                continue
            adjustment = apply_split_adjustments(
                normalized,
                events=split_events,
                provider=clean_provider,
                timeframe=timeframe,
            )
            datasets.append(
                BarDataset(
                    provider=clean_provider,
                    timeframe=timeframe,
                    symbol=clean_symbol,
                    frame=adjustment.frame,
                    source_files=tuple(dict.fromkeys(grouped_files[timeframe])),
                    adjustment_status=adjustment.status,
                    split_event_count=adjustment.event_count,
                    split_events_json=adjustment.metadata_json,
                    incomplete_bar_count=incomplete_count,
                    excluded_non_session_duplicate_count=(
                        excluded_non_session_duplicate_count
                    ),
                    daily_session_cleanup_version=(
                        _DAILY_SESSION_CLEANUP_VERSION
                        if timeframe == "1d"
                        else "not-applicable"
                    ),
                )
            )

    return tuple(datasets)


def write_technical_parquet(
    output_root: Path,
    *,
    calculation: str,
    dataset: BarDataset,
    frame: pd.DataFrame,
) -> Path:
    """Atomically replace the current technical output for one provider/timeframe."""
    if calculation in {BAR_SHAPE_CALCULATION, WEEKLY_CONTEXT_CALCULATION}:
        return _write_semantic_technical_parquet(
            output_root,
            calculation=calculation,
            dataset=dataset,
            frame=frame,
        )

    folder = output_root / safe_token(calculation) / safe_token(dataset.provider)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_token(dataset.timeframe)}.parquet"
    temporary = path.with_suffix(".tmp.parquet")

    output = _attach_bar_context(frame.copy(), dataset.frame)
    if "technical_score" in output.columns:
        output = attach_fundamental_context(
            output,
            fundamentals_root=output_root.parent / "fundamentals",
        )
    output["input_source_file_count"] = len(dataset.source_files)
    output["input_bar_count"] = len(dataset.frame)
    output["input_complete_bar_count"] = len(dataset.frame)
    output["input_incomplete_bar_count"] = dataset.incomplete_bar_count
    output["input_excluded_non_session_duplicate_count"] = (
        dataset.excluded_non_session_duplicate_count
    )
    output["input_total_bar_count"] = (
        len(dataset.frame)
        + dataset.incomplete_bar_count
        + dataset.excluded_non_session_duplicate_count
    )
    output["daily_session_cleanup_version"] = dataset.daily_session_cleanup_version
    output["price_adjustment_status"] = dataset.adjustment_status
    output["split_event_count"] = dataset.split_event_count
    output["split_events_json"] = dataset.split_events_json
    output = without_internal_identity_columns(output)
    output = add_readable_id(output, key_columns=("timestamp",))
    output.to_parquet(temporary, index=False)
    temporary.replace(path)
    return path


def _write_semantic_technical_parquet(
    output_root: Path,
    *,
    calculation: str,
    dataset: BarDataset,
    frame: pd.DataFrame,
) -> Path:
    """Write a strict new-family schema with its complete natural identity."""

    output = frame.copy()
    output["price_adjustment_status"] = dataset.adjustment_status
    output["split_event_count"] = dataset.split_event_count
    output = without_internal_identity_columns(output)
    output = add_readable_id(
        output,
        key_columns=_NEW_CALCULATION_NATURAL_KEY,
    )

    if calculation == BAR_SHAPE_CALCULATION:
        columns = _BAR_SHAPE_OUTPUT_COLUMNS
        output_timeframe = dataset.timeframe
    elif calculation == WEEKLY_CONTEXT_CALCULATION:
        columns = _WEEKLY_CONTEXT_OUTPUT_COLUMNS
        output_timeframe = WEEKLY_CONTEXT_TIMEFRAME
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"No semantic technical contract for {calculation}.")

    missing = [column for column in columns if column not in output.columns]
    if missing:
        raise ValueError(
            f"{calculation} output is missing contract columns: "
            + ", ".join(missing)
        )
    output = output.loc[:, columns]

    folder = output_root / safe_token(calculation) / safe_token(dataset.provider)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_token(output_timeframe)}.parquet"
    temporary = path.with_suffix(".tmp.parquet")
    output.to_parquet(temporary, index=False)
    temporary.replace(path)
    return path


def _bar_path_entries(
    datastore_root: Path,
    *,
    symbol: str,
    provider: str,
) -> list[tuple[Path, str | None]]:
    modern_root = datastore_root / "stocks" / safe_token(symbol) / "bars"
    modern: list[tuple[Path, str | None]] = []
    if modern_root.is_dir():
        for timeframe_folder in modern_root.iterdir():
            if not timeframe_folder.is_dir():
                continue
            provider_folder = timeframe_folder / safe_token(provider) / "normalized"
            if not provider_folder.is_dir():
                continue
            modern.extend(
                (path, timeframe_folder.name.lower())
                for path in provider_folder.glob("*.parquet")
            )
    if modern:
        return sorted(
            modern,
            key=lambda item: (
                _schwab_history_file_sort_key(item[0])
                if provider == "schwab"
                else normalized_bar_file_sort_key(item[0])
            ),
        )

    legacy_folder = datastore_root / "normalized" / safe_token(provider) / "bars"
    if not legacy_folder.is_dir():
        return []
    return [
        (path, None)
        for path in sorted(
            legacy_folder.glob(f"{safe_token(symbol)}_*.parquet"),
            key=normalized_bar_file_sort_key,
        )
    ]


def _schwab_history_file_sort_key(path: Path) -> tuple[int, int, int, str]:
    """Place the widest refreshed Schwab window last for duplicate revisions."""
    layout, snapshot_time, _name = normalized_bar_file_sort_key(path)
    match = re.search(
        r"_(day|month|year|ytd)_(\d+)_(?:minute|daily|weekly|monthly)_",
        path.stem.lower(),
    )
    if match is None:
        coverage_days = 0
    else:
        multiplier = {"day": 1, "month": 31, "year": 366, "ytd": 366}[
            match.group(1)
        ]
        coverage_days = multiplier * int(match.group(2))
    return layout, snapshot_time, coverage_days, path.name


def _normalize_bar_frame(
    frame: pd.DataFrame,
    *,
    timeframe: str,
) -> tuple[pd.DataFrame, int, int]:
    required = ("timestamp", "open", "high", "low", "close")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(
            f"Normalized bar parquet is missing columns: {', '.join(missing)}"
        )

    normalized = frame.copy()
    normalized["timestamp"] = pd.to_datetime(
        normalized["timestamp"], utc=True, errors="coerce"
    )
    for column in _PRICE_EVIDENCE_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = 0.0
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized["volume"] = normalized["volume"].fillna(0.0)

    normalized = (
        normalized.sort_values(["timestamp", "_file_order"])
        .drop_duplicates(subset=["timestamp"], keep="last")
        .dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    legacy_complete = legacy_bar_completion_mask(normalized)
    legacy_incomplete_count = int((~legacy_complete).sum())
    normalized = normalized.loc[legacy_complete].reset_index(drop=True)

    excluded_non_session_duplicate_count = 0
    if timeframe == "1d" and not normalized.empty:
        normalized, excluded_non_session_duplicate_count = (
            _drop_exact_non_session_daily_duplicates(normalized)
        )

    normalized = annotate_bar_timing(normalized, timeframe=timeframe)
    incomplete_count = legacy_incomplete_count + int(
        (~normalized["bar_complete"].fillna(False)).sum()
    )
    normalized = normalized.loc[
        normalized["bar_complete"].fillna(False)
    ].reset_index(drop=True)
    columns = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        *_BAR_CONTEXT_COLUMNS,
    ]
    return (
        normalized.loc[:, columns],
        incomplete_count,
        excluded_non_session_duplicate_count,
    )


def _drop_exact_non_session_daily_duplicates(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Drop provider duplicate rows that are labeled on closed exchange dates.

    Schwab historical daily responses can rarely repeat a Friday OHLCV row under a
    Saturday timestamp. We remove a closed-date row only when its complete OHLCV
    evidence exactly matches the immediately preceding row. Any distinct closed-date
    row fails closed instead of being silently reassigned to another session.
    """

    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required to validate daily technical bars"
        ) from exc

    labels = (
        frame["timestamp"]
        .dt.tz_convert("UTC")
        .dt.tz_localize(None)
        .dt.normalize()
    )
    calendar = xcals.get_calendar(
        _DAILY_SESSION_CALENDAR,
        start=labels.min() - pd.Timedelta(days=14),
        end=labels.max() + pd.Timedelta(days=14),
    )
    invalid_positions = [
        position
        for position, label in enumerate(labels)
        if label not in calendar.sessions
    ]
    if not invalid_positions:
        return frame, 0

    drop_indexes: list[int] = []
    unresolved_dates: list[str] = []
    for position in invalid_positions:
        label = labels.iloc[position]
        if position == 0:
            unresolved_dates.append(label.date().isoformat())
            continue
        current = frame.iloc[position].loc[list(_PRICE_EVIDENCE_COLUMNS)]
        previous = frame.iloc[position - 1].loc[list(_PRICE_EVIDENCE_COLUMNS)]
        if current.equals(previous):
            drop_indexes.append(frame.index[position])
        else:
            unresolved_dates.append(label.date().isoformat())

    if unresolved_dates:
        raise ValueError(
            "Daily bars contain distinct rows on non-session dates for "
            f"{_DAILY_SESSION_CALENDAR}: {', '.join(sorted(set(unresolved_dates))[:10])}"
        )

    cleaned = frame.drop(index=drop_indexes).reset_index(drop=True)
    return cleaned, len(drop_indexes)


def _attach_bar_context(output: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    if "timestamp" not in output.columns or "timestamp" not in bars.columns:
        return output
    missing_context = [
        column for column in _BAR_CONTEXT_COLUMNS if column not in output.columns
    ]
    if not missing_context:
        return output

    left = output.copy()
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True, errors="coerce")
    context = bars.loc[:, ["timestamp", *missing_context]].copy()
    context["timestamp"] = pd.to_datetime(
        context["timestamp"], utc=True, errors="coerce"
    )
    context = context.drop_duplicates("timestamp", keep="last")
    return left.merge(context, on="timestamp", how="left", validate="one_to_one")


def _canonical_timeframe(frame: pd.DataFrame, provider: str) -> pd.Series:
    if "canonical_timeframe" in frame.columns:
        canonical = frame["canonical_timeframe"].astype(str).str.strip().str.lower()
        if canonical.ne("").any():
            return canonical

    if provider == "schwab" and {
        "provider_frequency_type",
        "provider_frequency",
    }.issubset(frame.columns):
        frequency_type = (
            frame["provider_frequency_type"].astype(str).str.strip().str.lower()
        )
        frequency = (
            pd.to_numeric(frame["provider_frequency"], errors="coerce")
            .fillna(1)
            .astype(int)
        )
        mapped = pd.Series("", index=frame.index, dtype="object")
        mapped.loc[frequency_type.eq("minute")] = (
            frequency.loc[frequency_type.eq("minute")].astype(str) + "m"
        )
        mapped.loc[frequency_type.eq("daily")] = (
            frequency.loc[frequency_type.eq("daily")].astype(str) + "d"
        )
        mapped.loc[frequency_type.eq("weekly")] = (
            frequency.loc[frequency_type.eq("weekly")].astype(str) + "w"
        )
        mapped.loc[frequency_type.eq("monthly")] = (
            frequency.loc[frequency_type.eq("monthly")].astype(str) + "mo"
        )
        if mapped.ne("").any():
            fallback = _timeframe_fallback(frame)
            return mapped.where(mapped.ne(""), fallback)

    if "output_frequency" in frame.columns:
        output = frame["output_frequency"].astype(str).str.strip().str.lower()
        fallback = _timeframe_fallback(frame)
        return output.where(output.ne(""), fallback)

    return _timeframe_fallback(frame)


def _timeframe_fallback(frame: pd.DataFrame) -> pd.Series:
    for column in ("timeframe", "provider_timeframe"):
        if column in frame.columns:
            return frame[column].astype(str).str.strip().str.lower()
    return pd.Series("", index=frame.index, dtype="object")
