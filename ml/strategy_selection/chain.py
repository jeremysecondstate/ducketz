from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from datafetching.quote_liquidity import (
    QUOTE_LIQUIDITY_QUALITY_POLICY_VERSION,
    QUOTE_LIQUIDITY_SCHEMA_VERSION,
)
from options.features import (
    OPTION_FEATURE_SCHEMA_VERSION,
    OPTION_FEATURE_VERSION,
    OPTION_SURFACE_QUALITY_POLICY_VERSION,
)
from options.snapshot import OPTION_CHAIN_SCHEMA_VERSION


_SNAPSHOT_KEY = ("symbol", "snapshot_for", "available_at")


@dataclass(frozen=True)
class SchwabChainHistory:
    symbol: str
    contracts: pd.DataFrame
    surfaces: pd.DataFrame
    quotes: pd.DataFrame
    source_files: tuple[Path, ...]


@dataclass(frozen=True)
class ChainReceipt:
    contracts: pd.DataFrame
    surface: pd.Series

    @property
    def available_at(self) -> pd.Timestamp:
        return pd.Timestamp(self.surface["available_at"])


def load_schwab_chain_history(
    datastore_root: Path,
    *,
    symbol: str,
) -> SchwabChainHistory:
    root = Path(datastore_root)
    clean_symbol = str(symbol).strip().upper()
    stock_root = root / "stocks" / clean_symbol
    contract_paths = tuple(
        sorted(
            (
                stock_root
                / "options"
                / "chains"
                / "schwab"
                / "normalized"
            ).glob("*.parquet")
        )
    )
    surface_paths = tuple(
        sorted(
            (
                stock_root
                / "options"
                / "features"
                / "option-quality"
                / "schwab"
            ).glob("*.parquet")
        )
    )
    quote_paths = tuple(
        sorted(
            (
                stock_root
                / "quotes"
                / "features"
                / "quote-liquidity"
                / "schwab"
            ).glob("*.parquet")
        )
    )
    if not contract_paths:
        raise FileNotFoundError(
            f"No immutable Schwab normalized option-chain receipts exist for {clean_symbol}"
        )
    if not surface_paths:
        raise FileNotFoundError(
            f"No immutable Schwab option-quality receipts exist for {clean_symbol}"
        )

    contracts = _read_many(contract_paths)
    surfaces = _read_many(surface_paths)
    quotes = _read_many(quote_paths) if quote_paths else pd.DataFrame()
    _validate_contracts(contracts, symbol=clean_symbol)
    _validate_surfaces(surfaces, symbol=clean_symbol)
    if not quotes.empty:
        _validate_quotes(quotes, symbol=clean_symbol)

    surface_keys = surfaces.loc[
        :, [*_SNAPSHOT_KEY, "surface_quality_pass"]
    ].drop_duplicates(list(_SNAPSHOT_KEY), keep="last")
    contracts = contracts.merge(
        surface_keys.rename(
            columns={"surface_quality_pass": "__surface_quality"}
        ),
        on=list(_SNAPSHOT_KEY),
        how="left",
        validate="many_to_one",
    )
    contracts["__surface_quality"] = (
        contracts["__surface_quality"].fillna(False).astype(bool)
    )
    return SchwabChainHistory(
        symbol=clean_symbol,
        contracts=contracts.sort_values(
            ["snapshot_for", "available_at", "expiration_date", "strike", "call_put"],
            kind="mergesort",
        ).reset_index(drop=True),
        surfaces=surfaces.sort_values(
            ["snapshot_for", "available_at"], kind="mergesort"
        ).reset_index(drop=True),
        quotes=quotes.sort_values("available_at", kind="mergesort").reset_index(
            drop=True
        )
        if not quotes.empty
        else quotes,
        source_files=tuple((*contract_paths, *surface_paths, *quote_paths)),
    )


def entry_chain_receipt(
    history: SchwabChainHistory,
    *,
    minimum_snapshot_for: object,
    information_available_at: object,
    target_window_start: object,
    known_at: object,
    receipt_choice: str = "latest",
) -> ChainReceipt | None:
    minimum_snapshot = _utc(minimum_snapshot_for)
    information = _utc(information_available_at)
    target_start = _utc(target_window_start)
    cutoff = min(_utc(known_at), target_start - pd.Timedelta(nanoseconds=1))
    eligible = history.surfaces.loc[
        history.surfaces["snapshot_for"].ge(minimum_snapshot)
        & history.surfaces["snapshot_for"].le(cutoff)
        & history.surfaces["available_at"].ge(information)
        & history.surfaces["available_at"].le(cutoff)
    ]
    if eligible.empty:
        return None
    if receipt_choice not in {"earliest", "latest"}:
        raise ValueError("receipt_choice must be earliest or latest")
    ordered = eligible.sort_values(
        ["available_at", "snapshot_for"], kind="mergesort"
    )
    surface = ordered.iloc[0 if receipt_choice == "earliest" else -1]
    return _receipt_for_surface(history, surface)


def exit_chain_receipt(
    history: SchwabChainHistory,
    *,
    target_window_end: object,
    maximum_delay: pd.Timedelta,
    strictly_before: object | None = None,
) -> ChainReceipt | None:
    target_end = _utc(target_window_end)
    upper = target_end + maximum_delay
    if strictly_before is not None:
        upper = min(upper, _utc(strictly_before) - pd.Timedelta(nanoseconds=1))
    eligible = history.surfaces.loc[
        history.surfaces["snapshot_for"].ge(target_end)
        & history.surfaces["available_at"].ge(target_end)
        & history.surfaces["available_at"].le(upper)
        & history.surfaces["snapshot_for"].le(upper)
    ]
    if eligible.empty:
        return None
    surface = eligible.sort_values(
        ["available_at", "snapshot_for"], kind="mergesort"
    ).iloc[0]
    return _receipt_for_surface(history, surface)


def entry_stock_quote(
    history: SchwabChainHistory,
    *,
    information_available_at: object,
    target_window_start: object,
    known_at: object,
    receipt_choice: str = "latest",
) -> pd.Series | None:
    if history.quotes.empty:
        return None
    information = _utc(information_available_at)
    target_start = _utc(target_window_start)
    cutoff = min(_utc(known_at), target_start - pd.Timedelta(nanoseconds=1))
    eligible = history.quotes.loc[
        history.quotes["available_at"].ge(information)
        & history.quotes["available_at"].le(cutoff)
    ]
    if eligible.empty:
        return None
    if receipt_choice not in {"earliest", "latest"}:
        raise ValueError("receipt_choice must be earliest or latest")
    ordered = eligible.sort_values("available_at", kind="mergesort")
    return ordered.iloc[0 if receipt_choice == "earliest" else -1]


def exit_stock_quote(
    history: SchwabChainHistory,
    *,
    target_window_end: object,
    maximum_delay: pd.Timedelta,
    strictly_before: object | None = None,
) -> pd.Series | None:
    if history.quotes.empty:
        return None
    target_end = _utc(target_window_end)
    upper = target_end + maximum_delay
    if strictly_before is not None:
        upper = min(upper, _utc(strictly_before) - pd.Timedelta(nanoseconds=1))
    eligible = history.quotes.loc[
        history.quotes["available_at"].ge(target_end)
        & history.quotes["available_at"].le(upper)
    ]
    if eligible.empty:
        return None
    return eligible.sort_values("available_at", kind="mergesort").iloc[0]


def _receipt_for_surface(
    history: SchwabChainHistory,
    surface: pd.Series,
) -> ChainReceipt:
    mask = pd.Series(True, index=history.contracts.index)
    for column in _SNAPSHOT_KEY:
        mask &= history.contracts[column].eq(surface[column])
    contracts = history.contracts.loc[mask].copy()
    if contracts.empty:
        raise ValueError("Option-quality receipt has no exact normalized contracts")
    return ChainReceipt(contracts=contracts.reset_index(drop=True), surface=surface)


def _validate_contracts(frame: pd.DataFrame, *, symbol: str) -> None:
    required = {
        *_SNAPSHOT_KEY,
        "contract_symbol",
        "call_put",
        "expiration_date",
        "strike",
        "underlying_price",
        "bid",
        "ask",
        "open_interest",
        "volume",
        "delta",
        "gamma",
        "theta",
        "vega",
        "multiplier",
        "mini",
        "non_standard",
        "quote_valid",
        "relative_bid_ask_spread",
        "quote_staleness_seconds",
        "schema_version",
    }
    _require(frame, required, label="normalized option chain")
    _normalize_times(frame, ("snapshot_for", "available_at", "expiration_date"))
    if not frame["symbol"].astype("string").str.upper().eq(symbol).all():
        raise ValueError("Normalized option-chain history contains another symbol")
    if not frame["schema_version"].eq(OPTION_CHAIN_SCHEMA_VERSION).all():
        raise ValueError("Normalized option-chain schema version is incompatible")
    if frame.duplicated([*_SNAPSHOT_KEY, "contract_symbol"]).any():
        raise ValueError("Normalized option-chain history contains duplicate receipt legs")


def _validate_surfaces(frame: pd.DataFrame, *, symbol: str) -> None:
    required = {
        *_SNAPSHOT_KEY,
        "surface_quality_pass",
        "surface_quality_policy_version",
        "calculation_version",
        "schema_version",
    }
    _require(frame, required, label="option-quality surface")
    _normalize_times(frame, ("snapshot_for", "available_at"))
    if not frame["symbol"].astype("string").str.upper().eq(symbol).all():
        raise ValueError("Option-quality history contains another symbol")
    if not frame["schema_version"].eq(OPTION_FEATURE_SCHEMA_VERSION).all():
        raise ValueError("Option-quality schema version is incompatible")
    if not frame["calculation_version"].eq(OPTION_FEATURE_VERSION).all():
        raise ValueError("Option-quality calculation version is incompatible")
    if not frame["surface_quality_policy_version"].eq(
        OPTION_SURFACE_QUALITY_POLICY_VERSION
    ).all():
        raise ValueError("Option-quality policy version is incompatible")
    if frame.duplicated(list(_SNAPSHOT_KEY)).any():
        raise ValueError("Option-quality history contains duplicate receipt keys")


def _validate_quotes(frame: pd.DataFrame, *, symbol: str) -> None:
    required = {
        "symbol",
        "available_at",
        "bid",
        "ask",
        "quote_quality_pass",
        "quality_policy_version",
        "schema_version",
    }
    _require(frame, required, label="Schwab stock quote")
    _normalize_times(frame, ("available_at",))
    if not frame["symbol"].astype("string").str.upper().eq(symbol).all():
        raise ValueError("Schwab stock-quote history contains another symbol")
    if not frame["schema_version"].eq(QUOTE_LIQUIDITY_SCHEMA_VERSION).all():
        raise ValueError("Schwab stock-quote schema version is incompatible")
    if not frame["quality_policy_version"].eq(
        QUOTE_LIQUIDITY_QUALITY_POLICY_VERSION
    ).all():
        raise ValueError("Schwab stock-quote quality policy is incompatible")
    if frame.duplicated(["symbol", "available_at"]).any():
        raise ValueError("Schwab stock-quote history contains duplicate receipts")


def _read_many(paths: Iterable[Path]) -> pd.DataFrame:
    frames = [pd.read_parquet(path) for path in paths]
    return pd.concat(frames, ignore_index=True, sort=False)


def _normalize_times(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    for column in columns:
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
        if frame[column].isna().any():
            raise ValueError(f"Invalid timestamp in {column}")


def _require(frame: pd.DataFrame, required: set[str], *, label: str) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"{label} is missing columns: " + ", ".join(missing))


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Strategy-selection timestamp is invalid")
    return pd.Timestamp(timestamp)


__all__ = [
    "ChainReceipt",
    "SchwabChainHistory",
    "entry_chain_receipt",
    "entry_stock_quote",
    "exit_chain_receipt",
    "exit_stock_quote",
    "load_schwab_chain_history",
]
