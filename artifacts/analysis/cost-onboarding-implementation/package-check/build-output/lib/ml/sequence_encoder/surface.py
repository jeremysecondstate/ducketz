from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from datafetching.bar_timing import bar_end_timestamps
from datafetching.ids import add_readable_id
from ml.sequence_encoder.contracts import (
    SEQUENCE_FEATURE_COLUMNS,
    SEQUENCE_STATE_SCHEMA_VERSION,
)


_OCC_SUFFIX = re.compile(r"(?P<expiry>\d{6})(?P<call_put>[CP])(?P<strike>\d{8})$")
_REQUIRED_OPTION_COLUMNS = {
    "ts_event",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
}


def canonical_stock_hourly_path(datastore_root: Path, symbol: str) -> Path:
    clean = str(symbol).strip().upper()
    path = (
        Path(datastore_root)
        / "stocks"
        / clean
        / "bars"
        / "1h"
        / "databento"
        / "normalized"
        / f"{clean}_source_1825d_1h_ohlcv-1h_1h.parquet"
    )
    if not path.is_file():
        raise FileNotFoundError(
            f"Canonical EQUS.MINI hourly stock history is missing for {clean}: {path}"
        )
    return path


def canonical_opra_hourly_files(
    datastore_root: Path,
    symbol: str,
    *,
    start: object | None = None,
    end: object | None = None,
    maximum_sessions: int | None = None,
) -> tuple[Path, ...]:
    clean = str(symbol).strip().upper()
    root = (
        Path(datastore_root)
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "ohlcv-1h"
        / f"{clean}.OPT"
        / "dates"
    )
    if not root.is_dir():
        raise FileNotFoundError(f"Canonical OPRA hourly history is missing: {root}")
    lower = _optional_utc(start)
    upper = _optional_utc(end)
    files: list[tuple[pd.Timestamp, Path]] = []
    for path in root.glob("*/segments/full-day/normalized.parquet"):
        try:
            session = pd.Timestamp(path.parents[2].name, tz="UTC")
        except ValueError:
            continue
        if lower is not None and session < lower.normalize():
            continue
        if upper is not None and session > upper.normalize():
            continue
        files.append((session, path))
    files.sort(key=lambda pair: pair[0])
    if maximum_sessions is not None:
        if maximum_sessions < 1:
            raise ValueError("maximum_sessions must be positive")
        files = files[-maximum_sessions:]
    return tuple(path for _, path in files)


def materialize_hourly_surface_states(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    information_cutoff: object,
    start: object | None = None,
    maximum_sessions_per_symbol: int | None = None,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    """Aggregate changing OPRA contracts into a fixed causal hourly surface.

    The output has one row per underlying/hour.  Multiple OPRA publisher rows
    for the same contract/hour are consolidated before cross-sectional
    statistics are calculated, so duplicated venue evidence cannot masquerade
    as additional contracts or independent outcomes.
    """

    cutoff = _utc(information_cutoff, "information_cutoff")
    selected = tuple(
        dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    )
    if not selected:
        raise ValueError("At least one symbol is required")
    frames: list[pd.DataFrame] = []
    inputs: list[Path] = []
    for symbol in selected:
        stock_path = canonical_stock_hourly_path(datastore_root, symbol)
        stock = _load_stock_hourly(stock_path, cutoff=cutoff, start=start)
        option_files = canonical_opra_hourly_files(
            datastore_root,
            symbol,
            start=start,
            end=cutoff,
            maximum_sessions=maximum_sessions_per_symbol,
        )
        inputs.extend((stock_path, *option_files))
        states = _materialize_symbol_states(
            symbol,
            stock=stock,
            option_files=option_files,
            cutoff=cutoff,
        )
        if not states.empty:
            frames.append(states)
    if not frames:
        return _empty_state_frame(), tuple(dict.fromkeys(inputs))
    output = pd.concat(frames, ignore_index=True, sort=False)
    output = output.sort_values(["symbol", "bar_timestamp"]).reset_index(drop=True)
    output["stock_log_return_1h"] = output.groupby("symbol", sort=False)[
        "underlying_close"
    ].transform(lambda values: np.log(values / values.shift(1)))
    prior = output.groupby("symbol", sort=False)["bar_timestamp"].shift(1)
    output["minutes_since_prior_state"] = (
        output["bar_timestamp"] - prior
    ).dt.total_seconds().div(60.0)
    output["schema_version"] = SEQUENCE_STATE_SCHEMA_VERSION
    output = add_readable_id(
        output,
        key_columns=("symbol", "bar_timestamp"),
    )
    ordered = (
        "id",
        "symbol",
        "bar_timestamp",
        "information_available_at",
        "underlying_close",
        "source_contract_count",
        "source_raw_row_count",
        *SEQUENCE_FEATURE_COLUMNS,
        "schema_version",
    )
    return output.loc[:, ordered], tuple(dict.fromkeys(inputs))


def loop_b_supervised_labels(
    samples: pd.DataFrame,
    *,
    horizons: Iterable[str] = ("1h", "4h", "1d", "1w"),
) -> pd.DataFrame:
    """Return exact mature Loop B labels without changing horizon semantics."""

    required = (
        "symbol",
        "horizon",
        "decision_timestamp",
        "information_available_at",
        "bar_end_timestamp",
        "target_window_start",
        "target_window_end",
        "label_available_at",
        "label_status",
        "target_cost_adjusted_positive",
        "forward_cost_adjusted_return",
    )
    missing = sorted(set(required).difference(samples.columns))
    if missing:
        raise ValueError("Loop B samples are missing: " + ", ".join(missing))
    selected_horizons = {str(value) for value in horizons}
    output = samples.loc[
        samples["horizon"].astype("string").isin(selected_horizons)
        & samples["label_status"].astype("string").eq("COMPLETE"),
        list(required),
    ].copy()
    for column in (
        "decision_timestamp",
        "information_available_at",
        "bar_end_timestamp",
        "target_window_start",
        "target_window_end",
        "label_available_at",
    ):
        output[column] = pd.to_datetime(output[column], utc=True, errors="coerce")
    output["target_cost_adjusted_positive"] = pd.to_numeric(
        output["target_cost_adjusted_positive"], errors="coerce"
    )
    output["forward_cost_adjusted_return"] = pd.to_numeric(
        output["forward_cost_adjusted_return"], errors="coerce"
    )
    output = output.dropna(
        subset=[
            "symbol",
            "horizon",
            "decision_timestamp",
            "bar_end_timestamp",
            "target_window_start",
            "target_window_end",
            "label_available_at",
            "target_cost_adjusted_positive",
            "forward_cost_adjusted_return",
        ]
    )
    output = output.loc[
        output["label_available_at"].ge(output["target_window_end"])
        & output["decision_timestamp"].ge(output["information_available_at"])
    ]
    base_keys = ["symbol", "horizon", "decision_timestamp"]
    one_hour = output["horizon"].astype("string").eq("1h")
    if output.loc[one_hour].duplicated(
        [*base_keys, "target_window_start"], keep=False
    ).any() or output.loc[~one_hour].duplicated(base_keys, keep=False).any():
        raise ValueError("Loop B mature labels are not unique by target route")
    output["decision_cluster_size"] = output.groupby(
        ["horizon", "target_window_start", "target_window_end"],
        sort=False,
    )["symbol"].transform("size")
    output["decision_weight"] = 1.0 / output["decision_cluster_size"].astype(float)
    return output.sort_values(
        ["target_window_start", "horizon", "symbol"]
    ).reset_index(drop=True)


def attach_sequence_sample_windows(
    samples: pd.DataFrame,
    routes: pd.DataFrame,
) -> pd.DataFrame:
    """Attach exact 1h target windows while retaining legacy route grain."""

    base_keys = ["symbol", "horizon", "decision_timestamp"]
    one_hour_keys = [*base_keys, "target_window_start"]
    sample_columns = [
        *one_hour_keys,
        "information_available_at",
        "bar_end_timestamp",
        "target_window_end",
    ]
    missing_routes = sorted(set(one_hour_keys).difference(routes.columns))
    missing_samples = sorted(set(sample_columns).difference(samples.columns))
    if missing_routes:
        raise ValueError("Sequence routes are missing: " + ", ".join(missing_routes))
    if missing_samples:
        raise ValueError("Sequence samples are missing: " + ", ".join(missing_samples))

    clean_routes = routes.loc[:, one_hour_keys].copy()
    clean_samples = samples.loc[:, sample_columns].copy()
    for frame in (clean_routes, clean_samples):
        frame["decision_timestamp"] = pd.to_datetime(
            frame["decision_timestamp"], utc=True, errors="coerce"
        )
        frame["target_window_start"] = pd.to_datetime(
            frame["target_window_start"], utc=True, errors="coerce"
        )
    pieces: list[pd.DataFrame] = []
    requested_count = 0
    for is_one_hour, keys in ((True, one_hour_keys), (False, base_keys)):
        route_mask = clean_routes["horizon"].astype("string").eq("1h").fillna(False)
        sample_mask = (
            clean_samples["horizon"].astype("string").eq("1h").fillna(False)
        )
        if not is_one_hour:
            route_mask = ~route_mask
            sample_mask = ~sample_mask
        requested = clean_routes.loc[route_mask, keys].dropna().drop_duplicates()
        if requested.empty:
            continue
        source = clean_samples.loc[sample_mask, sample_columns].drop_duplicates(keys)
        joined = requested.merge(source, on=keys, how="inner", validate="one_to_one")
        if len(joined) != len(requested):
            raise ValueError("LIVE Loop B routes do not map to exact sequence samples")
        pieces.append(joined)
        requested_count += len(requested)
    if not pieces:
        return clean_samples.iloc[0:0].copy()
    output = pd.concat(pieces, ignore_index=True, sort=False)
    if len(output) != requested_count:
        raise ValueError("LIVE Loop B routes do not map to exact sequence samples")
    return output


def _load_stock_hourly(
    path: Path,
    *,
    cutoff: pd.Timestamp,
    start: object | None,
) -> pd.DataFrame:
    stock = pd.read_parquet(
        path,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    stock["timestamp"] = pd.to_datetime(stock["timestamp"], utc=True, errors="coerce")
    stock["information_available_at"] = bar_end_timestamps(
        stock["timestamp"], "1h"
    )
    lower = _optional_utc(start)
    stock = stock.loc[
        stock["timestamp"].notna()
        & stock["information_available_at"].le(cutoff)
    ].copy()
    if lower is not None:
        stock = stock.loc[stock["timestamp"].ge(lower)]
    for column in ("open", "high", "low", "close", "volume"):
        stock[column] = pd.to_numeric(stock[column], errors="coerce")
    stock = stock.dropna(subset=["timestamp", "close"])
    stock = stock.loc[stock["close"].gt(0.0)]
    if stock["timestamp"].duplicated().any():
        raise ValueError(f"Canonical stock hourly rows are duplicated: {path}")
    return stock.set_index("timestamp", drop=False).sort_index()


def _materialize_symbol_states(
    symbol: str,
    *,
    stock: pd.DataFrame,
    option_files: Sequence[Path],
    cutoff: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for path in option_files:
        options = pd.read_parquet(path)
        missing = sorted(_REQUIRED_OPTION_COLUMNS.difference(options.columns))
        if missing:
            raise ValueError(f"OPRA hourly file is missing {missing}: {path}")
        options["ts_event"] = pd.to_datetime(
            options["ts_event"], utc=True, errors="coerce"
        )
        options = options.dropna(subset=["ts_event", "symbol"])
        for timestamp, raw_group in options.groupby("ts_event", sort=True):
            bar_timestamp = _utc(timestamp, "OPRA ts_event")
            available_at = bar_timestamp + pd.Timedelta(hours=1)
            if available_at > cutoff or bar_timestamp not in stock.index:
                continue
            stock_row = stock.loc[bar_timestamp]
            if isinstance(stock_row, pd.DataFrame):
                raise ValueError(
                    f"Canonical stock row is duplicated for {symbol} {bar_timestamp}"
                )
            spot = _finite(stock_row["close"])
            if spot is None or spot <= 0.0:
                continue
            consolidated = _consolidate_contracts(raw_group)
            aggregate = _surface_features(
                consolidated,
                spot=spot,
                bar_timestamp=bar_timestamp,
                raw_row_count=len(raw_group),
            )
            stock_open = _finite(stock_row["open"])
            stock_high = _finite(stock_row["high"])
            stock_low = _finite(stock_row["low"])
            stock_volume = _finite(stock_row["volume"])
            aggregate.update(
                {
                    "symbol": symbol,
                    "bar_timestamp": bar_timestamp,
                    "information_available_at": max(
                        available_at,
                        _utc(stock_row["information_available_at"], "stock availability"),
                    ),
                    "underlying_close": spot,
                    "stock_log_return_1h": np.nan,
                    "stock_intrabar_range": (
                        (stock_high - stock_low) / spot
                        if stock_high is not None and stock_low is not None
                        else np.nan
                    ),
                    "stock_body_return": (
                        math.log(spot / stock_open)
                        if stock_open is not None and stock_open > 0.0
                        else np.nan
                    ),
                    "stock_log_volume": math.log1p(max(stock_volume or 0.0, 0.0)),
                    "minutes_since_prior_state": np.nan,
                    "session_progress": _session_progress(bar_timestamp),
                }
            )
            rows.append(aggregate)
    return pd.DataFrame(rows)


def _consolidate_contracts(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    for column in ("open", "high", "low", "close", "volume"):
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    prepared["volume"] = prepared["volume"].fillna(0.0).clip(lower=0.0)
    records: list[dict[str, object]] = []
    for symbol, group in prepared.groupby("symbol", sort=False):
        weights = group["volume"].to_numpy(dtype=float)
        closes = group["close"].to_numpy(dtype=float)
        valid_close = np.isfinite(closes) & (closes >= 0.0)
        if valid_close.any() and weights[valid_close].sum() > 0.0:
            close = float(np.average(closes[valid_close], weights=weights[valid_close]))
        elif valid_close.any():
            close = float(np.median(closes[valid_close]))
        else:
            close = np.nan
        records.append(
            {
                "symbol": str(symbol),
                "open": _nanmedian(group["open"]),
                "high": _nanmax(group["high"]),
                "low": _nanmin(group["low"]),
                "close": close,
                "volume": float(group["volume"].sum()),
            }
        )
    return pd.DataFrame(records)


def _surface_features(
    contracts: pd.DataFrame,
    *,
    spot: float,
    bar_timestamp: pd.Timestamp,
    raw_row_count: int,
) -> dict[str, object]:
    parsed = contracts["symbol"].map(_parse_occ_symbol)
    valid_parse = parsed.notna()
    working = contracts.loc[valid_parse].copy()
    parsed_values = [value for value in parsed.loc[valid_parse] if value is not None]
    if parsed_values:
        working[["expiration", "call_put", "strike"]] = pd.DataFrame(
            parsed_values,
            index=working.index,
        )
    else:
        working["expiration"] = pd.NaT
        working["call_put"] = pd.NA
        working["strike"] = np.nan
    working["close"] = pd.to_numeric(working["close"], errors="coerce")
    working["high"] = pd.to_numeric(working["high"], errors="coerce")
    working["low"] = pd.to_numeric(working["low"], errors="coerce")
    working["volume"] = pd.to_numeric(working["volume"], errors="coerce").fillna(0.0)
    working["price_to_spot"] = working["close"] / spot
    working["intrabar_range"] = (
        (working["high"] - working["low"])
        / working["close"].replace(0.0, np.nan)
    )
    working["dte"] = (
        pd.to_datetime(working["expiration"], utc=True, errors="coerce").dt.normalize()
        - bar_timestamp.normalize()
    ).dt.days.astype(float)
    working["absolute_log_moneyness"] = np.abs(np.log(working["strike"] / spot))
    signed = np.where(
        working["call_put"].eq("C"),
        np.log(spot / working["strike"]),
        np.log(working["strike"] / spot),
    )
    working["moneyness_bucket"] = np.select(
        (signed > 0.02, signed < -0.02),
        ("itm", "otm"),
        default="atm",
    )
    working["dte_bucket"] = np.select(
        (working["dte"].le(14.0), working["dte"].le(45.0)),
        ("short", "medium"),
        default="long",
    )
    valid_price = working["close"].gt(0.0) & np.isfinite(working["close"])
    valid = working.loc[valid_price & working["dte"].ge(0.0)].copy()
    total_contracts = len(contracts)
    call_mask = valid["call_put"].eq("C")
    total_volume = float(valid["volume"].sum())
    volume_denominator = total_volume if total_volume > 0.0 else float(max(len(valid), 1))
    weights = (
        valid["volume"].astype(float)
        if total_volume > 0.0
        else pd.Series(1.0, index=valid.index, dtype=float)
    )
    call_volume = float(weights.loc[call_mask].sum())
    put_volume = float(weights.loc[~call_mask].sum())
    result: dict[str, object] = {
        "source_contract_count": total_contracts,
        "source_raw_row_count": int(raw_row_count),
        "option_log_contract_count": math.log1p(total_contracts),
        "option_log_volume": math.log1p(max(total_volume, 0.0)),
        "option_call_contract_fraction": float(call_mask.mean()) if len(valid) else np.nan,
        "option_call_volume_fraction": call_volume / max(call_volume + put_volume, 1.0),
        "option_put_call_volume_log_ratio": math.log1p(put_volume) - math.log1p(call_volume),
        "option_median_price_to_spot": _nanmedian(valid["price_to_spot"]),
        "option_price_to_spot_iqr": _iqr(valid["price_to_spot"]),
        "option_median_intrabar_range": _nanmedian(valid["intrabar_range"]),
        "option_missing_fraction": 1.0 - (len(valid) / max(total_contracts, 1)),
        "option_log_raw_row_count": math.log1p(max(int(raw_row_count), 0)),
        "option_median_dte": _nanmedian(valid["dte"]),
        "option_median_absolute_log_moneyness": _nanmedian(
            valid["absolute_log_moneyness"]
        ),
    }
    for call_put, prefix in (("C", "call"), ("P", "put")):
        for bucket in ("itm", "atm", "otm"):
            mask = valid["call_put"].eq(call_put) & valid["moneyness_bucket"].eq(bucket)
            bucket_weight = float(weights.loc[mask].sum())
            result[f"{prefix}_{bucket}_volume_fraction"] = (
                bucket_weight / volume_denominator
            )
            result[f"{prefix}_{bucket}_price_to_spot"] = _weighted_mean(
                valid.loc[mask, "price_to_spot"],
                weights.loc[mask],
            )
    for bucket in ("short", "medium", "long"):
        mask = valid["dte_bucket"].eq(bucket)
        result[f"{bucket}_dte_volume_fraction"] = (
            float(weights.loc[mask].sum()) / volume_denominator
        )
    return result


def _parse_occ_symbol(value: object) -> tuple[pd.Timestamp, str, float] | None:
    text = str(value or "").strip().replace(" ", "")
    match = _OCC_SUFFIX.search(text)
    if match is None:
        return None
    try:
        expiration = pd.to_datetime(
            match.group("expiry"), format="%y%m%d", utc=True, errors="raise"
        )
        strike = int(match.group("strike")) / 1000.0
    except (TypeError, ValueError):
        return None
    if strike <= 0.0:
        return None
    return expiration, match.group("call_put"), strike


def _session_progress(timestamp: pd.Timestamp) -> float:
    local = timestamp.tz_convert("America/New_York")
    minutes = (local.hour * 60 + local.minute) - (9 * 60 + 30)
    return float(np.clip(minutes / 390.0, 0.0, 1.0))


def _weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    clean_values = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    clean_weights = pd.to_numeric(weights, errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(clean_values) & np.isfinite(clean_weights) & (clean_weights >= 0.0)
    if not valid.any():
        return float("nan")
    if clean_weights[valid].sum() <= 0.0:
        return float(np.mean(clean_values[valid]))
    return float(np.average(clean_values[valid], weights=clean_weights[valid]))


def _nanmedian(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    valid = numeric[np.isfinite(numeric)]
    return float(np.median(valid)) if valid.size else float("nan")


def _nanmin(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    valid = numeric[np.isfinite(numeric)]
    return float(np.min(valid)) if valid.size else float("nan")


def _nanmax(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    valid = numeric[np.isfinite(numeric)]
    return float(np.max(valid)) if valid.size else float("nan")


def _iqr(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    valid = numeric[np.isfinite(numeric)]
    if not valid.size:
        return float("nan")
    return float(np.quantile(valid, 0.75) - np.quantile(valid, 0.25))


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"{label} must be a valid timestamp")
    return pd.Timestamp(timestamp)


def _optional_utc(value: object | None) -> pd.Timestamp | None:
    return None if value is None else _utc(value, "timestamp")


def _empty_state_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=(
            "id",
            "symbol",
            "bar_timestamp",
            "information_available_at",
            "underlying_close",
            "source_contract_count",
            "source_raw_row_count",
            *SEQUENCE_FEATURE_COLUMNS,
            "schema_version",
        )
    )


__all__ = [
    "attach_sequence_sample_windows",
    "canonical_opra_hourly_files",
    "canonical_stock_hourly_path",
    "loop_b_supervised_labels",
    "materialize_hourly_surface_states",
]
