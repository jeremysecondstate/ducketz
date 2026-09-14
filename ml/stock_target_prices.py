"""Read an explicit, single-dataset equity price source for stock target labels.

This module never acquires data, materializes continuation files, or changes a
production pointer. The cold archive can be selected for a new experiment while
the existing canonical prices and immutable forecasts retain their identity.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from datafetching.databento_archive import discover_archive_partitions
from ml.artifacts import file_checksum


CANONICAL_STOCK_PRICE_SOURCE = "canonical-equity-minute-v1"
XNAS_STOCK_PRICE_SOURCE = "xnas-itch-archive-v1"
STOCK_PRICE_SOURCES = {
    CANONICAL_STOCK_PRICE_SOURCE: "EQUS.MINI",
    XNAS_STOCK_PRICE_SOURCE: "XNAS.ITCH",
}


def stock_price_dataset(source_contract: str) -> str:
    try:
        return STOCK_PRICE_SOURCES[source_contract]
    except KeyError as exc:
        raise ValueError(f"Unsupported stock price source: {source_contract}") from exc


def load_stock_target_prices(
    datastore_root: Path, *, symbols: Sequence[str],
    source_contract: str = CANONICAL_STOCK_PRICE_SOURCE,
) -> tuple[pd.DataFrame, tuple[Path, ...], dict]:
    """Return observed prices, provenance files, and a verified source inventory.

    No fallback crosses datasets. A missing symbol, altered archive receipt or
    payload, invalid source identity, or conflicting minute price fails closed.
    Missing trading minutes remain missing; endpoint quality belongs to the
    existing target builder's directional five-minute observation policy.
    """
    root = Path(datastore_root).resolve()
    dataset = stock_price_dataset(source_contract)
    clean_symbols = tuple(str(value).strip().upper() for value in symbols)
    if not clean_symbols or len(set(clean_symbols)) != len(clean_symbols):
        raise ValueError("Stock price source requires a nonempty unique symbol set")
    if any(not symbol or not symbol.replace(".", "").isalnum() for symbol in clean_symbols):
        raise ValueError("Stock price source symbol is invalid")
    frames, files, partitions = [], [], []
    if source_contract == XNAS_STOCK_PRICE_SOURCE:
        available = discover_archive_partitions(
            root, market="us-equities", dataset=dataset, schema="ohlcv-1m", verify_payload=True,
        )
        for symbol in clean_symbols:
            selected = [item for item in available if item.request["symbol_scope"] == [symbol]]
            if not selected:
                raise RuntimeError(f"Verified {dataset} stock minute archive is missing: {symbol}")
            for item in selected:
                directory = item.directory.resolve()
                if not directory.is_relative_to(root / "market-data/databento/us-equities" / dataset):
                    raise RuntimeError("Stock price archive escapes its dataset root")
                request = item.request
                if request.get("stype_in") != "raw_symbol" or request.get("schema") != "ohlcv-1m":
                    raise RuntimeError("Stock price archive has an incompatible request identity")
                raw = item.manifest.get("raw", {})
                raw_path = (directory / str(raw.get("path", ""))).resolve()
                normalized_path = item.normalized_path.resolve()
                if not raw_path.is_relative_to(directory) or not normalized_path.is_relative_to(directory):
                    raise RuntimeError("Stock price archive payload escapes its partition")
                if (not raw_path.is_file() or raw_path.stat().st_size != raw.get("size_bytes")
                        or file_checksum(raw_path) != raw.get("checksum_sha256")):
                    raise RuntimeError(f"Stock price archive raw payload failed verification: {raw_path}")
                frame = pd.read_parquet(normalized_path)
                timestamp_column = item.manifest["normalized"]["timestamp_column"]
                if timestamp_column not in frame and frame.index.name == timestamp_column:
                    frame = frame.reset_index()
                if "symbol" in frame and set(frame.symbol.astype(str).str.upper()) != {symbol}:
                    raise RuntimeError("Stock price archive contains another symbol")
                frame["timestamp"] = pd.to_datetime(frame[timestamp_column], utc=True, errors="coerce")
                if len(frame) != item.manifest["normalized"]["row_count"]:
                    raise RuntimeError("Stock price archive row count differs from its manifest")
                if (frame.timestamp.isna().any()
                        or frame.timestamp.lt(pd.to_datetime(request["start"], utc=True)).any()
                        or frame.timestamp.ge(pd.to_datetime(request["end"], utc=True)).any()):
                    raise RuntimeError("Stock price archive timestamps escape its requested interval")
                frames.append(frame.loc[:, ["timestamp", "open", "close"]].assign(symbol=symbol))
                files.extend((*item.source_files, raw_path))
                partitions.append({"symbol": symbol, "request_id": request["request_id"],
                    "start": request["start"], "end": request["end"], "rows": len(frame),
                    "manifest_path": str(item.manifest_path), "published_at": item.manifest["published_at"]})
        # Live replay is a delivery method for the same XNAS dataset. Its exact
        # action-session scope and native controls remain separate from the
        # historical archive and are verified before admitting any prices.
        from datafetching.xnas_replay_archive import discover_partitions
        for item in discover_partitions(root, symbols=clean_symbols):
            frame = item["frame"]
            frames.append(frame.loc[:, ["timestamp", "open", "close"]].assign(symbol=item["symbol"]))
            files.extend(item["source_files"])
            replay_manifest = item["manifest"]
            partitions.append({"symbol": item["symbol"], "session": item["session"],
                "delivery_mode": "live-intraday-replay", "rows": len(frame),
                "manifest_path": str(item["manifest_path"]),
                "published_at": replay_manifest["published_at"]})
    else:
        for symbol in clean_symbols:
            folder = root / "stocks" / symbol / "bars/1m/databento/normalized"
            paths = sorted(folder.glob("*_ohlcv-1m_1m.parquet"))
            if not paths:
                raise RuntimeError(f"Canonical stock minute prices are missing: {symbol}")
            for path in paths:
                frame = pd.read_parquet(path)
                if "provider_dataset" not in frame or set(frame.provider_dataset.dropna().astype(str)) != {dataset}:
                    raise RuntimeError(f"Canonical stock price dataset differs from {dataset}: {path}")
                frames.append(frame.loc[:, ["timestamp", "open", "close"]].assign(symbol=symbol))
                files.append(path)
    bars = pd.concat(frames, ignore_index=True)
    bars["timestamp"] = pd.to_datetime(bars.timestamp, utc=True, errors="coerce")
    if bars.timestamp.isna().any():
        raise RuntimeError("Stock price source has invalid minute timestamps")
    bars = bars.drop_duplicates(["symbol", "timestamp", "open", "close"])
    if bars.duplicated(["symbol", "timestamp"]).any():
        raise RuntimeError("Stock price source contains conflicting minute observations")
    # A provider row with both prices undefined contains no price observation.
    # Preserve its verified native evidence and disclose the omitted minute;
    # never fabricate a price or hide a partially invalid observation.
    missing_prices = bars.loc[bars.open.isna() & bars.close.isna(), ["symbol", "timestamp"]]
    bars = bars.loc[~(bars.open.isna() & bars.close.isna())].copy()
    for field in ("open", "close"):
        values = pd.to_numeric(bars[field], errors="raise")
        if pd.api.types.is_bool_dtype(values) or not np.isfinite(values).all() or values.le(0).any():
            raise RuntimeError(f"Stock price source contains invalid observed {field}")
        bars[field] = values
    if set(bars.symbol) != set(clean_symbols):
        raise RuntimeError("Stock price source has a symbol without observed prices")
    bars = bars.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)
    files = tuple(dict.fromkeys(Path(path).resolve() for path in files))
    report = {
        "source_contract": source_contract, "dataset": dataset, "schema": "ohlcv-1m",
        "price_basis": "unadjusted_market_scale", "timestamp_semantics": "minute_interval_open",
        "source_policy": "one_explicit_dataset_no_fill_no_cross_dataset_fallback",
        "missing_price_policy": "omit_only_rows_with_both_prices_undefined_preserve_native_evidence",
        "missing_price_rows_by_symbol": missing_prices.groupby("symbol").size().to_dict(),
        "missing_price_examples": [{"symbol":row.symbol,"timestamp":row.timestamp.isoformat()}
                                   for row in missing_prices.head(50).itertuples()],
        "native_archive_partitions_verified": len(partitions), "partitions": partitions,
        "by_symbol": {symbol: {"rows": len(frame), "first_timestamp": frame.timestamp.min().isoformat(),
                               "last_timestamp": frame.timestamp.max().isoformat()}
                      for symbol, frame in bars.groupby("symbol", sort=True)},
        "files": [{"path": str(path), "bytes": path.stat().st_size, "sha256": file_checksum(path)} for path in files],
    }
    bars.attrs["stock_price_source"] = report
    return bars, files, report


def independent_price_identity(row) -> tuple[str, str]:
    """Old independent publications retain their original canonical EQUS labels."""
    contract = row.get("target_price_source_contract")
    contract = CANONICAL_STOCK_PRICE_SOURCE if pd.isna(contract) else str(contract)
    dataset = row.get("target_price_dataset")
    dataset = stock_price_dataset(contract) if pd.isna(dataset) else str(dataset)
    if stock_price_dataset(contract) != dataset:
        raise RuntimeError("Independent stock target price source and dataset disagree")
    return contract, dataset
