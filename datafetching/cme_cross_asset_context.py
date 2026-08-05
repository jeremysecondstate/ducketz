from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from datafetching.calculated_features import write_immutable_feature_partition
from datafetching.cme_history import cme_normalized_event_paths

CME_CONTEXT_NAME = "continuous-cross-asset-1h"
CME_CONTEXT_CALCULATION = "cross-asset-context"
CME_CONTEXT_CALCULATION_VERSION = "1.0.0"
CME_CONTEXT_SCHEMA_VERSION = "cme-cross-asset-v1"
CME_CONTINUOUS_ROLL_POLICY_VERSION = "databento-continuous-v0-roll-v1"
CME_CONTEXT_TIMEFRAME = "1h"
CME_CONTEXT_ROOTS = ("NQ", "ES", "RTY", "GC", "CL")
CME_CONTEXT_MAX_STALENESS = pd.Timedelta(minutes=15)
CME_CONTEXT_MAX_CLOCK_SKEW = pd.Timedelta(seconds=5)
CME_BOOK_LOOKBACK = pd.Timedelta(minutes=15)

CME_CONTEXT_COLUMNS = (
    "context_name",
    "window_start",
    "window_end",
    "observed_at",
    "fetched_at",
    "available_at",
    "calculation",
    "calculation_version",
    "schema_version",
    "roll_policy_version",
    "nq_return",
    "es_return",
    "rty_minus_es_return",
    "nq_minus_es_return",
    "gold_return",
    "crude_return",
    "relative_spread",
    "book_imbalance",
    "constituent_complete",
    "source_stale",
)
CME_CONTEXT_NATURAL_KEY = (
    "context_name",
    "window_end",
    "calculation_version",
)

_CME_SOURCE_DATASETS = {
    "ohlcv": "cme_context_ohlcv-1m",
    "bbo": "cme_context_bbo-1m",
    "mbp": "cme_context_mbp-10",
}
_ROOT_PATTERN = re.compile(r"^(RTY|NQ|ES|GC|CL)(?:$|[.\-_/])", re.IGNORECASE)


class CmeCrossAssetNotReady(ValueError):
    """Raised when persisted CME history has no unseen complete common window."""


class CmeCrossAssetQualityError(ValueError):
    """Raised when a candidate CME context window fails a hard quality gate."""


def calculate_cme_cross_asset_context(
    ohlcv: pd.DataFrame,
    bbo: pd.DataFrame,
    mbp: pd.DataFrame,
    *,
    calculated_at: object | None = None,
    excluded_window_ends: Iterable[object] = (),
) -> pd.DataFrame:
    """Calculate exact one-hour continuous-futures context from persisted rows.

    Every return leg uses the same 60 one-minute timestamps. BBO and MBP
    observations must come from all five roots inside that same hour, and the
    newest observation for each root must be no more than 15 minutes old at
    calculation time. The MBP request is rejected in full when Databento reports
    request-limit saturation.

    Crude uses ``sign(delta) * log1p(abs(delta) / max(abs(start), eps))``.
    Unlike a price ratio, this definition remains finite when the continuous
    series crosses zero.
    """

    completed = _utc_timestamp(
        calculated_at if calculated_at is not None else pd.Timestamp.now(tz="UTC"),
        field="calculated_at",
    )
    bars = _prepare_ohlcv(ohlcv)
    quotes = _prepare_events(bbo, label="BBO")
    book = _prepare_events(mbp, label="MBP")
    _reject_limit_saturated_book(book)

    excluded = {
        timestamp
        for value in excluded_window_ends
        if (timestamp := _optional_utc_timestamp(value)) is not None
    }
    windows = _complete_common_ohlcv_windows(bars)
    if not windows:
        raise CmeCrossAssetNotReady(
            "CME cross-asset context requires 60 exact common one-minute bars "
            "for NQ, ES, RTY, GC, and CL"
        )

    future_windows = [
        start for start in windows if start + pd.Timedelta(hours=1) > completed
    ]
    eligible = [
        start
        for start in windows
        if start + pd.Timedelta(hours=1) <= completed
        and start + pd.Timedelta(hours=1) not in excluded
    ]
    if not eligible:
        if future_windows and not excluded:
            raise CmeCrossAssetQualityError(
                "CME context rejects future-ending windows"
            )
        raise CmeCrossAssetNotReady(
            "CME cross-asset context has no unseen completed common window"
        )

    records: list[dict[str, object]] = []
    failures: list[str] = []
    for window_start in eligible:
        window_end = window_start + pd.Timedelta(hours=1)
        by_root = windows[window_start]
        try:
            spread, bbo_times = _relative_spread(
                quotes,
                window_start=window_start,
                window_end=window_end,
                calculated_at=completed,
            )
            imbalance, mbp_times = _book_imbalance(
                book,
                window_start=window_start,
                window_end=window_end,
                calculated_at=completed,
            )
            return_values = {
                root: _window_return(root, by_root[root])
                for root in CME_CONTEXT_ROOTS
            }
            ohlcv_times = _source_times(
                pd.concat(list(by_root.values()), ignore_index=True, sort=False)
            )
            event_times = [
                *ohlcv_times["events"],
                *bbo_times["events"],
                *mbp_times["events"],
            ]
            receive_times = [
                *ohlcv_times["receives"],
                *bbo_times["receives"],
                *mbp_times["receives"],
            ]
            receipt_times = [
                *ohlcv_times["receipts"],
                *bbo_times["receipts"],
                *mbp_times["receipts"],
            ]
            _reject_future_evidence(
                [*event_times, *receive_times, *receipt_times],
                calculated_at=completed,
            )
            observed_at = window_end
            fetched_at = max(receipt_times)
            available_at = max(
                [completed, *event_times, *receive_times, *receipt_times]
            )
        except CmeCrossAssetQualityError as exc:
            failures.append(f"{window_end.isoformat()}: {exc}")
            continue

        records.append(
            {
                "context_name": CME_CONTEXT_NAME,
                "window_start": window_start,
                "window_end": window_end,
                "observed_at": observed_at,
                "fetched_at": fetched_at,
                "available_at": available_at,
                "calculation": CME_CONTEXT_CALCULATION,
                "calculation_version": CME_CONTEXT_CALCULATION_VERSION,
                "schema_version": CME_CONTEXT_SCHEMA_VERSION,
                "roll_policy_version": CME_CONTINUOUS_ROLL_POLICY_VERSION,
                "nq_return": return_values["NQ"],
                "es_return": return_values["ES"],
                "rty_minus_es_return": (
                    return_values["RTY"] - return_values["ES"]
                ),
                "nq_minus_es_return": (
                    return_values["NQ"] - return_values["ES"]
                ),
                "gold_return": return_values["GC"],
                "crude_return": return_values["CL"],
                "relative_spread": spread,
                "book_imbalance": imbalance,
                "constituent_complete": True,
                "source_stale": False,
            }
        )

    if not records:
        detail = failures[-1] if failures else "no eligible common book window"
        raise CmeCrossAssetQualityError(
            "CME cross-asset context rejected every candidate window: " + detail
        )
    return pd.DataFrame(records, columns=CME_CONTEXT_COLUMNS).sort_values(
        "window_end",
        kind="stable",
    ).reset_index(drop=True)


def persist_cme_cross_asset_context(
    datastore_root: Path,
    frame: pd.DataFrame,
) -> Path:
    """Persist the shared, quarantined one-hour calculated partition."""

    return write_immutable_feature_partition(
        cme_cross_asset_context_path(datastore_root),
        frame,
        columns=CME_CONTEXT_COLUMNS,
        natural_key=CME_CONTEXT_NATURAL_KEY,
    )


def materialize_cme_cross_asset_context(
    datastore_root: Path,
    *,
    calculated_at: object | None = None,
) -> Path | None:
    """Read already-persisted Databento rows and append unseen context windows."""

    root = Path(datastore_root)
    output_path = cme_cross_asset_context_path(root)
    excluded: Sequence[object] = ()
    if output_path.is_file():
        existing = pd.read_parquet(
            output_path,
            columns=["context_name", "window_end", "calculation_version"],
        )
        matching = existing.loc[
            existing["context_name"].eq(CME_CONTEXT_NAME)
            & existing["calculation_version"].eq(
                CME_CONTEXT_CALCULATION_VERSION
            )
        ]
        excluded = tuple(matching["window_end"])

    try:
        sources = {
            name: _read_persisted_source(root, dataset)
            for name, dataset in _CME_SOURCE_DATASETS.items()
        }
        frame = calculate_cme_cross_asset_context(
            sources["ohlcv"],
            sources["bbo"],
            sources["mbp"],
            calculated_at=calculated_at,
            excluded_window_ends=excluded,
        )
    except CmeCrossAssetNotReady:
        return None
    if frame.empty:
        return None
    return persist_cme_cross_asset_context(root, frame)


def cme_cross_asset_context_path(datastore_root: Path) -> Path:
    return (
        Path(datastore_root)
        / "pools"
        / "cme"
        / "features"
        / CME_CONTEXT_CALCULATION
        / "databento"
        / f"{CME_CONTEXT_TIMEFRAME}.parquet"
    )


def _read_persisted_source(root: Path, dataset: str) -> pd.DataFrame:
    schema = dataset.removeprefix("cme_context_")
    partitioned = cme_normalized_event_paths(
        root,
        group_key="context",
        schema=schema,
    )
    if partitioned:
        return pd.concat(
            [pd.read_parquet(path) for path in partitioned],
            ignore_index=True,
            sort=False,
        )
    folder = (
        root
        / "pools"
        / "cme"
        / "CME_CONTEXT"
        / dataset
        / "databento"
        / "normalized"
    )
    paths = tuple(
        path
        for path in sorted(folder.glob("*.parquet"))
        if not path.stem.endswith("_status")
    )
    if not paths:
        raise CmeCrossAssetNotReady(
            f"Persisted CME source is not ready: {folder}"
        )
    return pd.concat(
        [pd.read_parquet(path) for path in paths],
        ignore_index=True,
        sort=False,
    )


def _prepare_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    required = ("timestamp", "open", "high", "low", "close", "fetched_at")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise CmeCrossAssetNotReady(
            "Persisted CME OHLCV is missing columns: " + ", ".join(missing)
        )
    output = _continuous_roots(frame)
    output["timestamp"] = pd.to_datetime(
        output["timestamp"], utc=True, errors="coerce"
    )
    output["fetched_at"] = pd.to_datetime(
        output["fetched_at"], utc=True, errors="coerce"
    )
    for column in ("open", "high", "low", "close"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    if "timeframe" in output.columns:
        output = output.loc[
            output["timeframe"].astype(str).str.strip().str.lower().eq("1m")
        ]
    if output.empty:
        raise CmeCrossAssetNotReady(
            "Persisted CME OHLCV contains no continuous one-minute rows"
        )
    if output[[*required, "_root"]].isna().any(axis=None):
        raise CmeCrossAssetQualityError(
            "CME OHLCV contains missing timestamps, receipts, or OHLC values"
        )
    if output["high"].lt(output["low"]).any():
        raise CmeCrossAssetQualityError("CME OHLCV contains high below low")
    if output.duplicated(["_root", "timestamp"], keep=False).any():
        raise CmeCrossAssetQualityError(
            "CME OHLCV contains duplicate continuous root-minute rows"
        )
    return output.sort_values(["timestamp", "_root"], kind="stable").reset_index(
        drop=True
    )


def _prepare_events(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if "timestamp" not in frame.columns or "fetched_at" not in frame.columns:
        raise CmeCrossAssetNotReady(
            f"Persisted CME {label} requires timestamp and fetched_at"
        )
    output = _continuous_roots(frame)
    output["timestamp"] = pd.to_datetime(
        output["timestamp"], utc=True, errors="coerce"
    )
    output["fetched_at"] = pd.to_datetime(
        output["fetched_at"], utc=True, errors="coerce"
    )
    for column in _receive_columns(output):
        output[column] = pd.to_datetime(
            output[column], utc=True, errors="coerce"
        )
    if output.empty:
        raise CmeCrossAssetNotReady(
            f"Persisted CME {label} contains no continuous rows"
        )
    if output[["timestamp", "fetched_at", "_root"]].isna().any(axis=None):
        raise CmeCrossAssetQualityError(
            f"CME {label} contains missing event or receipt evidence"
        )
    return output.sort_values(["timestamp", "_root"], kind="stable").reset_index(
        drop=True
    )


def _continuous_roots(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    if "provider_stype_in" in output.columns:
        output = output.loc[
            output["provider_stype_in"]
            .astype(str)
            .str.strip()
            .str.lower()
            .eq("continuous")
        ]
    symbol_column = next(
        (
            column
            for column in ("provider_symbol", "symbol", "raw_symbol")
            if column in output.columns
        ),
        None,
    )
    if symbol_column is None:
        raise CmeCrossAssetNotReady(
            "Persisted CME rows do not identify their continuous symbol"
        )
    output["_root"] = output[symbol_column].map(_continuous_root)
    return output.loc[output["_root"].isin(CME_CONTEXT_ROOTS)].copy()


def _continuous_root(value: object) -> str | None:
    match = _ROOT_PATTERN.match(str(value or "").strip().upper())
    return match.group(1).upper() if match else None


def _complete_common_ohlcv_windows(
    bars: pd.DataFrame,
) -> dict[pd.Timestamp, dict[str, pd.DataFrame]]:
    complete_by_root: dict[str, dict[pd.Timestamp, pd.DataFrame]] = {}
    for root in CME_CONTEXT_ROOTS:
        root_bars = bars.loc[bars["_root"].eq(root)].copy()
        complete: dict[pd.Timestamp, pd.DataFrame] = {}
        for window_start, group in root_bars.groupby(
            root_bars["timestamp"].dt.floor("1h"),
            sort=True,
        ):
            ordered = group.sort_values("timestamp", kind="stable")
            expected = pd.date_range(
                window_start,
                periods=60,
                freq="1min",
                tz="UTC",
            )
            actual = pd.DatetimeIndex(ordered["timestamp"])
            if len(ordered) == 60 and actual.equals(expected):
                complete[pd.Timestamp(window_start)] = ordered
        complete_by_root[root] = complete

    common = set.intersection(
        *(set(complete_by_root[root]) for root in CME_CONTEXT_ROOTS)
    )
    return {
        start: {
            root: complete_by_root[root][start] for root in CME_CONTEXT_ROOTS
        }
        for start in sorted(common)
    }


def _window_return(root: str, bars: pd.DataFrame) -> float:
    start = _finite_number(bars.iloc[0]["open"], field=f"{root} window open")
    end = _finite_number(bars.iloc[-1]["close"], field=f"{root} window close")
    if root == "CL":
        return _signed_log_return(start, end)
    if start <= 0 or end <= 0:
        raise CmeCrossAssetQualityError(
            f"{root} log return requires positive endpoint prices"
        )
    return math.log(end / start)


def _signed_log_return(start: float, end: float) -> float:
    delta = end - start
    if delta == 0:
        return 0.0
    scale = max(abs(start), 1e-12)
    return math.copysign(math.log1p(abs(delta) / scale), delta)


def _relative_spread(
    quotes: pd.DataFrame,
    *,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    calculated_at: pd.Timestamp,
) -> tuple[float, dict[str, list[pd.Timestamp]]]:
    bid_column = _first_column(
        quotes,
        ("bid_px_00", "bid_price", "bid_px", "bid"),
    )
    ask_column = _first_column(
        quotes,
        ("ask_px_00", "ask_price", "ask_px", "ask"),
    )
    if bid_column is None or ask_column is None:
        raise CmeCrossAssetQualityError(
            "CME BBO rows do not contain bid and ask prices"
        )
    window = quotes.loc[
        quotes["timestamp"].ge(window_start)
        & quotes["timestamp"].lt(window_end)
    ].copy()
    if window.empty:
        raise CmeCrossAssetQualityError(
            "CME BBO has no rows inside the return window"
        )

    selected: list[pd.DataFrame] = []
    spreads: list[float] = []
    for root in CME_CONTEXT_ROOTS:
        root_rows = window.loc[window["_root"].eq(root)].sort_values(
            "timestamp",
            kind="stable",
        )
        if root_rows.empty:
            raise CmeCrossAssetQualityError(
                f"CME BBO is missing {root} in the common window"
            )
        row = root_rows.tail(1)
        _reject_stale(
            pd.Timestamp(row["timestamp"].iloc[0]),
            calculated_at=calculated_at,
            label=f"CME BBO {root}",
        )
        bid = _finite_number(row[bid_column].iloc[0], field=f"{root} bid")
        ask = _finite_number(row[ask_column].iloc[0], field=f"{root} ask")
        if bid <= 0 or ask <= 0 or ask <= bid:
            raise CmeCrossAssetQualityError(
                f"CME BBO {root} is non-positive, crossed, or locked"
            )
        midpoint = (bid + ask) / 2.0
        spreads.append((ask - bid) / midpoint)
        selected.append(row)
    evidence = pd.concat(selected, ignore_index=True, sort=False)
    return sum(spreads) / len(spreads), _source_times(
        evidence,
        require_receive=True,
    )


def _book_imbalance(
    book: pd.DataFrame,
    *,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    calculated_at: pd.Timestamp,
) -> tuple[float, dict[str, list[pd.Timestamp]]]:
    lookback_start = max(window_start, window_end - CME_BOOK_LOOKBACK)
    window = book.loc[
        book["timestamp"].ge(lookback_start)
        & book["timestamp"].lt(window_end)
    ].copy()
    if window.empty:
        raise CmeCrossAssetQualityError(
            "CME MBP has no rows inside the fixed book lookback"
        )

    bid_size_columns, ask_size_columns = _wide_size_columns(window)
    selected: list[pd.DataFrame] = []
    imbalances: list[float] = []
    for root in CME_CONTEXT_ROOTS:
        root_rows = window.loc[window["_root"].eq(root)].sort_values(
            "timestamp",
            kind="stable",
        )
        if root_rows.empty:
            raise CmeCrossAssetQualityError(
                f"CME MBP is missing {root} in the common window"
            )
        newest = pd.Timestamp(root_rows["timestamp"].iloc[-1])
        _reject_stale(
            newest,
            calculated_at=calculated_at,
            label=f"CME MBP {root}",
        )

        if bid_size_columns and ask_size_columns:
            snapshot = root_rows.tail(1)
            bid_size = _numeric_sum(snapshot, bid_size_columns)
            ask_size = _numeric_sum(snapshot, ask_size_columns)
            evidence = snapshot
        else:
            side_column = _first_column(root_rows, ("side", "book_side"))
            size_column = _first_column(
                root_rows,
                ("size", "quantity", "qty"),
            )
            if side_column is None or size_column is None:
                raise CmeCrossAssetQualityError(
                    "CME MBP rows do not contain side and size"
                )
            evidence = root_rows
            if "depth" in evidence.columns:
                depth = pd.to_numeric(evidence["depth"], errors="coerce")
                evidence = evidence.loc[depth.between(0, 9, inclusive="both")]
            sides = evidence[side_column].astype(str).str.strip().str.upper()
            sizes = pd.to_numeric(evidence[size_column], errors="coerce")
            bid_size = float(sizes.loc[sides.isin({"B", "BID"})].sum())
            ask_size = float(sizes.loc[sides.isin({"A", "ASK"})].sum())

        total = bid_size + ask_size
        if (
            not math.isfinite(bid_size)
            or not math.isfinite(ask_size)
            or bid_size < 0
            or ask_size < 0
            or total <= 0
        ):
            raise CmeCrossAssetQualityError(
                f"CME MBP {root} has incomplete bid/ask size"
            )
        if bid_size == 0 or ask_size == 0:
            raise CmeCrossAssetQualityError(
                f"CME MBP {root} is missing one side of the book"
            )
        imbalances.append((bid_size - ask_size) / total)
        selected.append(evidence)

    evidence = pd.concat(selected, ignore_index=True, sort=False)
    return sum(imbalances) / len(imbalances), _source_times(
        evidence,
        require_receive=True,
    )


def _wide_size_columns(
    frame: pd.DataFrame,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    bid = tuple(
        column
        for column in frame.columns
        if re.fullmatch(r"bid_(?:sz|size)_?0?\d", str(column).lower())
    )
    ask = tuple(
        column
        for column in frame.columns
        if re.fullmatch(r"ask_(?:sz|size)_?0?\d", str(column).lower())
    )
    return bid, ask


def _numeric_sum(frame: pd.DataFrame, columns: Sequence[str]) -> float:
    values = frame.loc[:, list(columns)].apply(
        pd.to_numeric,
        errors="coerce",
    )
    return float(values.sum(axis=1, min_count=1).iloc[-1])


def _reject_limit_saturated_book(book: pd.DataFrame) -> None:
    if "request_limit_saturated" not in book.columns:
        return
    if _truthy(book["request_limit_saturated"]).any():
        raise CmeCrossAssetQualityError(
            "CME MBP request was limit-saturated"
        )


def _reject_stale(
    event_at: pd.Timestamp,
    *,
    calculated_at: pd.Timestamp,
    label: str,
) -> None:
    age = calculated_at - event_at
    if age < -CME_CONTEXT_MAX_CLOCK_SKEW:
        raise CmeCrossAssetQualityError(f"{label} event is in the future")
    if age > CME_CONTEXT_MAX_STALENESS:
        raise CmeCrossAssetQualityError(
            f"{label} is stale by {age}; maximum is "
            f"{CME_CONTEXT_MAX_STALENESS}"
        )


def _reject_future_evidence(
    timestamps: Sequence[pd.Timestamp],
    *,
    calculated_at: pd.Timestamp,
) -> None:
    if not timestamps:
        raise CmeCrossAssetQualityError(
            "CME context has no complete event/receipt evidence"
        )
    if max(timestamps) > calculated_at + CME_CONTEXT_MAX_CLOCK_SKEW:
        raise CmeCrossAssetQualityError(
            "CME context contains evidence after calculation completion"
        )


def _source_times(
    frame: pd.DataFrame,
    *,
    require_receive: bool = False,
) -> dict[str, list[pd.Timestamp]]:
    events = _timestamps(frame, ("timestamp", "ts_event", "databento_ts_event"))
    receives = _timestamps(frame, _receive_columns(frame))
    receipts = _timestamps(frame, ("fetched_at",))
    if not events or not receipts:
        raise CmeCrossAssetQualityError(
            "CME source rows require event and receipt timestamps"
        )
    if require_receive and not receives:
        raise CmeCrossAssetQualityError(
            "CME book rows require provider receive timestamps"
        )
    return {
        "events": events,
        "receives": receives,
        "receipts": receipts,
    }


def _timestamps(
    frame: pd.DataFrame,
    columns: Sequence[str],
) -> list[pd.Timestamp]:
    values: list[pd.Timestamp] = []
    for column in columns:
        if column not in frame.columns:
            continue
        parsed = pd.to_datetime(frame[column], utc=True, errors="coerce")
        values.extend(pd.Timestamp(value) for value in parsed.dropna())
    return values


def _receive_columns(frame: pd.DataFrame) -> tuple[str, ...]:
    return tuple(
        column
        for column in ("ts_recv", "databento_ts_recv")
        if column in frame.columns
    )


def _first_column(
    frame: pd.DataFrame,
    candidates: Sequence[str],
) -> str | None:
    lookup = {str(column).strip().lower(): str(column) for column in frame.columns}
    return next((lookup[candidate] for candidate in candidates if candidate in lookup), None)


def _truthy(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip().str.lower()
    numeric = pd.to_numeric(series, errors="coerce")
    return (text.isin({"true", "yes", "y", "1"}) | numeric.eq(1)).fillna(False)


def _finite_number(value: object, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CmeCrossAssetQualityError(
            f"CME context requires numeric {field}"
        ) from exc
    if not math.isfinite(number):
        raise CmeCrossAssetQualityError(
            f"CME context requires finite {field}"
        )
    return number


def _utc_timestamp(value: object, *, field: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"CME context requires a valid {field}")
    return pd.Timestamp(parsed)


def _optional_utc_timestamp(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)
