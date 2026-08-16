from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

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
from options.publication import committed_option_snapshots
from datafetching.databento_opra_history import (
    iter_verified_partitions,
    record_consumer_usage,
)
from ml.option_pricing.opra import (
    normalize_cbbo_records,
    normalize_definition_records,
)


_SNAPSHOT_KEY = ("symbol", "snapshot_for", "available_at")


@dataclass(frozen=True)
class OptionChainHistory:
    symbol: str
    contracts: pd.DataFrame
    surfaces: pd.DataFrame
    quotes: pd.DataFrame
    source_files: tuple[Path, ...]
    provider: str = "schwab"
    contract_lookup: Mapping[
        tuple[str, pd.Timestamp, pd.Timestamp], pd.DataFrame
    ] = field(default_factory=dict, compare=False, repr=False)
    surface_available_ns: tuple[int, ...] = field(
        default=(), compare=False, repr=False
    )
    quote_available_ns: tuple[int, ...] = field(
        default=(), compare=False, repr=False
    )

    def __post_init__(self) -> None:
        surfaces = self.surfaces.sort_values(
            ["available_at", "snapshot_for"], kind="mergesort"
        ).reset_index(drop=True)
        contracts = self.contracts.sort_values(
            ["snapshot_for", "available_at", "expiration_date", "strike", "call_put"],
            kind="mergesort",
        ).reset_index(drop=True)
        quotes = (
            self.quotes.sort_values("available_at", kind="mergesort").reset_index(
                drop=True
            )
            if not self.quotes.empty
            else self.quotes
        )
        lookup = {
            (
                str(key[0]).strip().upper(),
                pd.Timestamp(key[1]),
                pd.Timestamp(key[2]),
            ): group.reset_index(drop=True)
            for key, group in contracts.groupby(
                list(_SNAPSHOT_KEY), sort=False, dropna=False
            )
        }
        object.__setattr__(self, "surfaces", surfaces)
        object.__setattr__(self, "contracts", contracts)
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "contract_lookup", lookup)
        object.__setattr__(
            self,
            "surface_available_ns",
            tuple(int(pd.Timestamp(value).value) for value in surfaces["available_at"]),
        )
        object.__setattr__(
            self,
            "quote_available_ns",
            tuple(int(pd.Timestamp(value).value) for value in quotes["available_at"])
            if not quotes.empty
            else (),
        )


@dataclass(frozen=True)
class ChainReceipt:
    contracts: pd.DataFrame
    surface: pd.Series

    @property
    def available_at(self) -> pd.Timestamp:
        return pd.Timestamp(self.surface["available_at"])


def load_option_chain_history(
    datastore_root: Path,
    *,
    symbol: str,
    available_not_after: object | None = None,
) -> OptionChainHistory:
    root = Path(datastore_root)
    clean_symbol = str(symbol).strip().upper()
    opra = _load_opra_chain_history(
        root,
        symbol=clean_symbol,
        available_not_after=available_not_after,
    )
    if opra is not None:
        return opra
    stock_root = root / "stocks" / clean_symbol
    all_committed = committed_option_snapshots(
        root,
        symbol=clean_symbol,
    )
    committed = committed_option_snapshots(
        root,
        symbol=clean_symbol,
        available_not_after=available_not_after,
    )
    committed = _deduplicate_natural_targets(committed)
    if all_committed:
        # Monthly immutable-history views are referenced by many target receipts.
        # Open each physical Parquet once; receipt files remain fully preserved in
        # lineage below.
        contract_paths = tuple(
            dict.fromkeys(snapshot.contracts_path for snapshot in committed)
        )
        surface_paths = tuple(
            dict.fromkeys(snapshot.features_path for snapshot in committed)
        )
        receipt_paths = tuple(snapshot.receipt_path for snapshot in committed)
    else:
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
        receipt_paths = ()
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
    if available_not_after is not None and not quotes.empty:
        cutoff = _utc(available_not_after)
        quote_available = pd.to_datetime(
            quotes["available_at"], utc=True, errors="coerce"
        )
        quotes = quotes.loc[quote_available.le(cutoff)].copy()
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
    return OptionChainHistory(
        symbol=clean_symbol,
        provider="schwab",
        contracts=contracts,
        surfaces=surfaces,
        quotes=quotes,
        source_files=tuple(
            (*contract_paths, *surface_paths, *receipt_paths, *quote_paths)
        ),
    )


def _load_opra_chain_history(
    datastore_root: Path,
    *,
    symbol: str,
    available_not_after: object | None,
) -> OptionChainHistory | None:
    root = Path(datastore_root).resolve()
    definition_frames: list[pd.DataFrame] = []
    cbbo_frames: dict[str, list[pd.DataFrame]] = {"cbbo-1m": [], "cbbo-1s": []}
    files: dict[str, list[Path]] = {"definition": [], "cbbo-1m": [], "cbbo-1s": []}
    metadata_files: list[Path] = []
    for verified in iter_verified_partitions(
        root, schemas=("definition", "cbbo-1m", "cbbo-1s")
    ):
        manifest = verified["manifest"]
        schema = str(manifest["schema"])
        directory = Path(verified["directory"])
        path = directory / str(manifest["normalized"]["path"])
        raw = pd.read_parquet(path)
        if schema == "definition":
            normalized = normalize_definition_records(raw)
            normalized = normalized.loc[
                normalized["symbol"].astype("string").str.upper().eq(symbol)
            ]
            if not normalized.empty:
                definition_frames.append(normalized)
                files[schema].append(path)
        else:
            normalized = normalize_cbbo_records(raw)
            normalized = normalized.loc[
                normalized["contract_symbol"].astype("string").map(_occ_underlying).eq(symbol)
            ]
            if available_not_after is not None:
                normalized = normalized.loc[
                    pd.to_datetime(normalized["quote_timestamp"], utc=True, errors="coerce")
                    .le(_utc(available_not_after))
                ]
            if not normalized.empty:
                cbbo_frames[schema].append(normalized)
                files[schema].append(path)
        metadata_files.extend((directory / "manifest.json", directory / "receipt.json"))
    cbbo_schema = "cbbo-1m" if cbbo_frames["cbbo-1m"] else "cbbo-1s"
    if not definition_frames or not cbbo_frames[cbbo_schema]:
        return None
    definitions = (
        pd.concat(definition_frames, ignore_index=True, sort=False)
        .sort_values(["definition_effective_at", "contract_symbol"], kind="mergesort")
        .drop_duplicates(["contract_symbol", "definition_effective_at"], keep="last")
    )
    cbbo = (
        pd.concat(cbbo_frames[cbbo_schema], ignore_index=True, sort=False)
        .sort_values(["quote_timestamp", "contract_symbol"], kind="mergesort")
        .drop_duplicates(["contract_symbol", "quote_timestamp"], keep="last")
    )
    merged = pd.merge_asof(
        cbbo.sort_values(["quote_timestamp", "contract_symbol"], kind="mergesort"),
        definitions.sort_values(
            ["definition_effective_at", "contract_symbol"], kind="mergesort"
        ),
        left_on="quote_timestamp",
        right_on="definition_effective_at",
        by="contract_symbol",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.loc[
        merged["expiration_date"].notna()
        & merged["call_put"].isin(("call", "put"))
        & merged["strike"].notna()
    ].copy()
    if merged.empty:
        return None
    snapshot_for = pd.to_datetime(merged["quote_timestamp"], utc=True, errors="coerce")
    contracts = pd.DataFrame(
        {
            "symbol": symbol,
            "snapshot_for": snapshot_for,
            "available_at": snapshot_for,
            "contract_symbol": merged["contract_symbol"].astype("string"),
            "call_put": merged["call_put"].astype("string").str.upper(),
            "expiration_date": merged["expiration_date"],
            "strike": pd.to_numeric(merged["strike"], errors="coerce"),
            "underlying_price": float("nan"),
            "bid": pd.to_numeric(merged["bid"], errors="coerce"),
            "ask": pd.to_numeric(merged["ask"], errors="coerce"),
            "open_interest": float("nan"),
            "volume": float("nan"),
            "delta": float("nan"),
            "gamma": float("nan"),
            "theta": float("nan"),
            "vega": float("nan"),
            "multiplier": pd.to_numeric(merged["multiplier"], errors="coerce"),
            "mini": False,
            "non_standard": ~merged["standard_contract"].fillna(False).astype(bool),
            "quote_valid": merged["ask"].ge(merged["bid"]) & merged["ask"].gt(0),
            "relative_bid_ask_spread": (
                (merged["ask"] - merged["bid"])
                / ((merged["ask"] + merged["bid"]) / 2.0).replace(0, pd.NA)
            ),
            "quote_staleness_seconds": 0.0,
            "source_provider": "databento-opra",
            "fallback_used": False,
            "schema_version": OPTION_CHAIN_SCHEMA_VERSION,
        }
    )
    surfaces = (
        contracts.groupby(["symbol", "snapshot_for", "available_at"], as_index=False)
        .agg(surface_quality_pass=("quote_valid", "any"))
        .assign(
            source_provider="databento-opra",
            fallback_used=False,
            surface_quality_policy_version=OPTION_SURFACE_QUALITY_POLICY_VERSION,
            calculation_version=OPTION_FEATURE_VERSION,
            schema_version=OPTION_FEATURE_SCHEMA_VERSION,
        )
    )
    quote_paths = tuple(
        sorted(
            (
                root
                / "stocks"
                / symbol
                / "quotes"
                / "features"
                / "quote-liquidity"
                / "schwab"
            ).glob("*.parquet")
        )
    )
    quotes = _read_many(quote_paths) if quote_paths else pd.DataFrame()
    if available_not_after is not None and not quotes.empty:
        quotes = quotes.loc[
            pd.to_datetime(quotes["available_at"], utc=True, errors="coerce").le(
                _utc(available_not_after)
            )
        ].copy()
    if not quotes.empty:
        _validate_quotes(quotes, symbol=symbol)
    source_files = tuple(
        dict.fromkeys((*files["definition"], *files[cbbo_schema], *metadata_files, *quote_paths))
    )
    record_consumer_usage(
        root,
        consumer="options-strategy-history",
        schemas=("definition", cbbo_schema),
        rows=len(contracts),
        source_files=source_files,
    )
    return OptionChainHistory(
        symbol=symbol,
        provider="databento-opra",
        contracts=contracts,
        surfaces=surfaces,
        quotes=quotes,
        source_files=source_files,
    )


def entry_chain_receipt(
    history: OptionChainHistory,
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
    eligible = _surface_available_slice(history, information, cutoff)
    eligible = eligible.loc[
        eligible["snapshot_for"].ge(minimum_snapshot)
        & eligible["snapshot_for"].le(cutoff)
    ]
    if eligible.empty:
        return None
    if receipt_choice not in {"earliest", "latest"}:
        raise ValueError("receipt_choice must be earliest or latest")
    surface = eligible.iloc[0 if receipt_choice == "earliest" else -1]
    return _receipt_for_surface(history, surface)


def exit_chain_receipt(
    history: OptionChainHistory,
    *,
    target_window_end: object,
    maximum_delay: pd.Timedelta,
    strictly_before: object | None = None,
) -> ChainReceipt | None:
    target_end = _utc(target_window_end)
    upper = target_end + maximum_delay
    if strictly_before is not None:
        upper = min(upper, _utc(strictly_before) - pd.Timedelta(nanoseconds=1))
    eligible = _surface_available_slice(history, target_end, upper)
    eligible = eligible.loc[
        eligible["snapshot_for"].ge(target_end)
        & eligible["snapshot_for"].le(upper)
    ]
    if eligible.empty:
        return None
    surface = eligible.iloc[0]
    return _receipt_for_surface(history, surface)


def entry_stock_quote(
    history: OptionChainHistory,
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
    eligible = _quote_available_slice(history, information, cutoff)
    if eligible.empty:
        return None
    if receipt_choice not in {"earliest", "latest"}:
        raise ValueError("receipt_choice must be earliest or latest")
    return eligible.iloc[0 if receipt_choice == "earliest" else -1]


def exit_stock_quote(
    history: OptionChainHistory,
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
    eligible = _quote_available_slice(history, target_end, upper)
    if eligible.empty:
        return None
    return eligible.iloc[0]


def _receipt_for_surface(
    history: OptionChainHistory,
    surface: pd.Series,
) -> ChainReceipt:
    key = (
        str(surface["symbol"]).strip().upper(),
        pd.Timestamp(surface["snapshot_for"]),
        pd.Timestamp(surface["available_at"]),
    )
    contracts = history.contract_lookup.get(key, pd.DataFrame()).copy()
    if contracts.empty:
        raise ValueError("Option-quality receipt has no exact normalized contracts")
    return ChainReceipt(contracts=contracts.reset_index(drop=True), surface=surface)


def _surface_available_slice(
    history: OptionChainHistory,
    lower: pd.Timestamp,
    upper: pd.Timestamp,
) -> pd.DataFrame:
    start = bisect_left(history.surface_available_ns, int(lower.value))
    stop = bisect_right(history.surface_available_ns, int(upper.value))
    return history.surfaces.iloc[start:stop]


def _quote_available_slice(
    history: OptionChainHistory,
    lower: pd.Timestamp,
    upper: pd.Timestamp,
) -> pd.DataFrame:
    start = bisect_left(history.quote_available_ns, int(lower.value))
    stop = bisect_right(history.quote_available_ns, int(upper.value))
    return history.quotes.iloc[start:stop]


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
    frames = [_read_verified_immutable_parquet(Path(path)) for path in paths]
    return pd.concat(frames, ignore_index=True, sort=False)


def _deduplicate_natural_targets(snapshots: Iterable[object]) -> tuple[object, ...]:
    selected: dict[tuple[str, pd.Timestamp], object] = {}
    for snapshot in snapshots:
        key = (
            str(getattr(snapshot, "symbol")).strip().upper(),
            pd.Timestamp(getattr(snapshot, "snapshot_for")),
        )
        previous = selected.get(key)
        candidate_order = (
            pd.Timestamp(
                getattr(snapshot, "receipt_published_at", None)
                or getattr(snapshot, "available_at")
            ),
            Path(getattr(snapshot, "directory")).as_posix(),
        )
        if previous is None:
            selected[key] = snapshot
            continue
        previous_order = (
            pd.Timestamp(
                getattr(previous, "receipt_published_at", None)
                or getattr(previous, "available_at")
            ),
            Path(getattr(previous, "directory")).as_posix(),
        )
        if candidate_order < previous_order:
            selected[key] = snapshot
    return tuple(
        sorted(
            selected.values(),
            key=lambda snapshot: (
                pd.Timestamp(getattr(snapshot, "snapshot_for")),
                pd.Timestamp(getattr(snapshot, "available_at")),
            ),
        )
    )


def _read_verified_immutable_parquet(path: Path) -> pd.DataFrame:
    resolved = path.resolve()
    stat = resolved.stat()
    return _cached_parquet(
        str(resolved), int(stat.st_size), int(stat.st_mtime_ns)
    ).copy()


@lru_cache(maxsize=2_048)
def _cached_parquet(path: str, size: int, modified_ns: int) -> pd.DataFrame:
    resolved = Path(path)
    stat = resolved.stat()
    if stat.st_size != size or stat.st_mtime_ns != modified_ns:
        raise RuntimeError("Immutable Strategy history changed during verified read")
    return pd.read_parquet(resolved)


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


def _occ_underlying(value: object) -> str:
    import re

    match = re.match(r"^([A-Z.]{1,6})\s*\d{6}[CP]", str(value).strip().upper())
    return match.group(1) if match else ""


__all__ = [
    "ChainReceipt",
    "OptionChainHistory",
    "entry_chain_receipt",
    "entry_stock_quote",
    "exit_chain_receipt",
    "exit_stock_quote",
    "load_option_chain_history",
]
