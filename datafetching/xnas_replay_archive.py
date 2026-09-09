"""Immutable, verified XNAS minute replay for completed stock action sessions.

Live replay has a separate archive and receipt contract. It never advances a
Historical cursor or represents a whole UTC day as delivered. Every load checks
the retained native delivery and reproduces the exact normalized action window.
The caller owns supervision and the exact zero-dollar acquisition preflight.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import uuid4

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from datafetching.databento_xnas_replay import (
    DATASET, MODE, capture_replay, validate_replay_delivery,
)
from ml.artifacts import file_checksum


PARTITION_VERSION = "databento-xnas-stock-session-replay-partition-v1"
RECEIPT_VERSION = "databento-xnas-stock-session-replay-receipt-v1"
SCHEMA = "ohlcv-1m"
_COLUMNS = ("timestamp", "symbol", "open", "high", "low", "close", "volume")
_PAYLOAD_NAMES = {"raw": "provider.dbn", "normalized": "normalized.parquet",
                  "delivery": "delivery.json"}
_MIN_FREE_SPACE = 5 * 1024**3


class XnasReplayArchiveError(RuntimeError):
    """A replay publication cannot prove its source, scope or integrity."""


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def _timestamp(value: object) -> pd.Timestamp:
    try:
        if value is None:
            raise ValueError("missing timestamp")
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            raise ValueError("missing timestamp")
        return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    except (ValueError, TypeError, OverflowError) as exc:
        raise XnasReplayArchiveError("Replay archive timestamp is invalid") from exc


def _symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,5}", symbol):
        raise XnasReplayArchiveError("Replay requires one explicit uppercase stock symbol")
    return symbol


def _session(session: str) -> pd.Timestamp:
    if not isinstance(session, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", session):
        raise XnasReplayArchiveError("Replay requires an exact exchange-session date")
    try:
        day = pd.Timestamp(session)
        calendar = xcals.get_calendar("XNYS", start=day - pd.Timedelta(days=10),
                                      end=day + pd.Timedelta(days=10))
        if not calendar.is_session(day):
            raise ValueError("not an XNYS session")
        return day
    except (TypeError, ValueError, OverflowError) as exc:
        raise XnasReplayArchiveError("Replay date must be an actual XNYS exchange session") from exc


def session_bounds(session: str, observed_at: object | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The completed XNYS date's exact [04:00, 17:00) Pacific action window."""
    local = _session(session).tz_localize("America/Los_Angeles")
    start = (local + pd.Timedelta(hours=4)).tz_convert("UTC")
    end = (local + pd.Timedelta(hours=17)).tz_convert("UTC")
    now = _utc_now() if observed_at is None else _timestamp(observed_at)
    if end > now:
        raise XnasReplayArchiveError("Replay requires a completed 17:00 Pacific action session")
    return start, end


def _archive_root(root: Path) -> Path:
    return Path(root).resolve() / "market-data/databento/stock-session-replay" / DATASET / SCHEMA


def partition_directory(root: Path, symbol: str, session: str) -> Path:
    _session(session)
    return _archive_root(root) / _symbol(symbol) / session


def _request(symbol: str, session: str, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    scope = {"dataset": DATASET, "schema": SCHEMA, "symbols": [symbol],
             "stype_in": "raw_symbol", "start": start.isoformat(), "end": end.isoformat(),
             "session": session}
    identity = json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**scope, "request_id": hashlib.sha256(identity).hexdigest()}


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        raise XnasReplayArchiveError(f"Replay metadata is unreadable: {path}") from exc


def _write_json(path: Path, value: Mapping) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")


def _payload(path: Path) -> dict:
    return {"path": path.name, "size_bytes": path.stat().st_size,
            "checksum_sha256": file_checksum(path)}


def _normalize(raw_path: Path, *, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    import databento as db

    store = None
    try:
        store = db.DBNStore.from_file(raw_path)
        frame = store.to_df(schema=SCHEMA, map_symbols=True, price_type="float")
        if not isinstance(frame, pd.DataFrame):
            raise XnasReplayArchiveError("Replay DBN normalization did not produce a frame")
        if frame.index.name == "ts_event":
            frame = frame.reset_index()
        if "ts_event" not in frame or not set(_COLUMNS[1:]).issubset(frame.columns):
            raise XnasReplayArchiveError("Replay DBN lacks required symbol/minute OHLCV fields")
        frame = frame.rename(columns={"ts_event": "timestamp"}).loc[:, list(_COLUMNS)].copy()
    except XnasReplayArchiveError:
        raise
    except Exception as exc:
        raise XnasReplayArchiveError(f"Replay DBN normalization failed: {type(exc).__name__}") from exc
    finally:
        if store is not None:
            store.reader.close()
    if frame.empty or frame.symbol.isna().any() or set(frame.symbol.astype(str)) != {symbol}:
        raise XnasReplayArchiveError("Replay DBN is empty or contains an unresolved/different symbol")
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, errors="coerce").astype("datetime64[ns, UTC]")
    if frame.timestamp.isna().any() or not frame.timestamp.eq(frame.timestamp.dt.floor("min")).all():
        raise XnasReplayArchiveError("Replay DBN has malformed minute timestamps")
    frame["symbol"] = frame.symbol.astype(str)
    values = ["open", "high", "low", "close", "volume"]
    for key in values:
        frame[key] = pd.to_numeric(frame[key], errors="coerce")
    if (not np.isfinite(frame[values].to_numpy(dtype=float)).all()
            or frame[["open", "high", "low", "close"]].le(0).any().any()
            or frame.volume.lt(0).any() or frame.volume.ne(np.floor(frame.volume)).any()
            or frame.high.lt(frame[["open", "low", "close"]].max(axis=1)).any()
            or frame.low.gt(frame[["open", "high", "close"]].min(axis=1)).any()):
        raise XnasReplayArchiveError("Replay DBN has malformed OHLCV observations")
    for key in ("open", "high", "low", "close"):
        frame[key] = frame[key].astype("float64")
    frame["volume"] = frame.volume.astype("uint64")
    # The native stream runs through connection time. Coverage is only the
    # requested action window; bars outside it must never enter target labels.
    frame = frame.loc[frame.timestamp.ge(start) & frame.timestamp.lt(end)].copy()
    if frame.empty:
        raise XnasReplayArchiveError("Replay contains no observations in the action window")
    frame = frame.drop_duplicates(list(_COLUMNS))
    if frame.duplicated(["symbol", "timestamp"]).any():
        raise XnasReplayArchiveError("Replay contains conflicting minute observations")
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _verify(directory: Path, *, root: Path, staging: bool = False) -> dict:
    directory, root = Path(directory).resolve(), Path(root).resolve()
    archive = _archive_root(root)
    if not directory.is_relative_to(archive) or (".staging" in directory.relative_to(archive).parts and not staging):
        raise XnasReplayArchiveError("Replay partition escapes its published archive")
    manifest_path, receipt_path = directory / "manifest.json", directory / "receipt.json"
    manifest, receipt = _json(manifest_path), _json(receipt_path)
    symbol, session = _symbol(manifest.get("symbol")), manifest.get("session")
    start, end = session_bounds(session)
    request = _request(symbol, session, start, end)
    if not staging and directory != partition_directory(root, symbol, session).resolve():
        raise XnasReplayArchiveError("Replay partition identity differs from its directory")
    if (manifest.get("schema_version") != PARTITION_VERSION
            or manifest.get("dataset") != DATASET or manifest.get("schema") != SCHEMA
            or manifest.get("mode") != MODE or manifest.get("request") != request
            or manifest.get("partition_start") != start.isoformat()
            or manifest.get("partition_end") != end.isoformat()
            or manifest.get("coverage") != {"basis": "VERIFIED_EXCHANGE_SESSION_ACTION_WINDOW",
                "calendar": "XNYS", "timezone": "America/Los_Angeles", "local_start": "04:00", "local_end": "17:00"}
            or receipt.get("schema_version") != RECEIPT_VERSION
            or receipt.get("request_id") != request["request_id"]
            or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
            or receipt.get("published_at") != manifest.get("published_at")):
        raise XnasReplayArchiveError("Replay receipt does not verify its exact source/session manifest")
    _timestamp(manifest.get("published_at"))
    paths = {}
    for key, filename in _PAYLOAD_NAMES.items():
        evidence = manifest.get(key)
        path = (directory / filename).resolve()
        if (not isinstance(evidence, dict) or evidence.get("path") != filename
                or not path.is_relative_to(directory) or not path.is_file()
                or path.stat().st_size < 1 or path.stat().st_size != evidence.get("size_bytes")
                or file_checksum(path) != evidence.get("checksum_sha256")
                or receipt.get(f"{key}_checksum_sha256") != evidence.get("checksum_sha256")):
            raise XnasReplayArchiveError(f"Replay {key} checksum/size does not verify")
        paths[key] = path
    delivery = _json(paths["delivery"])
    if (delivery.get("source_request_start") != start.isoformat()
            or delivery.get("requested_start") != start.isoformat()
            or delivery.get("requested_end") != end.isoformat()):
        raise XnasReplayArchiveError("Replay delivery shortened or changed the exact action window")
    try:
        validate_replay_delivery(delivery, request, paths["raw"])
        expected = _normalize(paths["raw"], symbol=symbol, start=start, end=end)
        actual = pd.read_parquet(paths["normalized"])
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    except XnasReplayArchiveError:
        raise
    except Exception as exc:
        raise XnasReplayArchiveError(f"Replay source/normalized verification failed: {type(exc).__name__}") from exc
    normalized = manifest["normalized"]
    if (normalized.get("row_count") != len(actual) or normalized.get("timestamp_column") != "timestamp"
            or normalized.get("first_timestamp") != actual.timestamp.min().isoformat()
            or normalized.get("last_timestamp") != actual.timestamp.max().isoformat()):
        raise XnasReplayArchiveError("Replay normalized coverage differs from its manifest")
    return {"directory": directory, "manifest": manifest, "receipt": receipt,
            "delivery": delivery, "frame": actual, "symbol": symbol, "session": session,
            "manifest_path": manifest_path, "receipt_path": receipt_path,
            "raw_path": paths["raw"], "normalized_path": paths["normalized"],
            "source_files": (paths["raw"], paths["normalized"], paths["delivery"], manifest_path, receipt_path)}


def verify_partition(directory: Path, *, root: Path) -> dict:
    """Re-read source DBN, delivery controls, hashes and exact normalized rows."""
    return _verify(directory, root=root)


def discover_partitions(root: Path, *, symbols: Sequence[str]) -> list[dict]:
    """Discover only immutable published partitions for the requested symbols."""
    requested = tuple(_symbol(symbol) for symbol in symbols)
    if not requested or len(set(requested)) != len(requested):
        raise XnasReplayArchiveError("Replay discovery requires unique stock symbols")
    found = []
    for symbol in sorted(requested):
        parent = _archive_root(root) / symbol
        if parent.exists():
            for directory in sorted(parent.iterdir()):
                if directory.name == ".staging":
                    continue
                if directory.is_dir():
                    found.append(verify_partition(directory, root=root))
    return found


def publish_session(root: Path, *, symbol: str, session: str, api_key: str,
                    max_bytes: int, timeout_seconds: float = 300,
                    capture_fn: Callable | None = None) -> dict:
    """Capture once and atomically publish; reuse only a fully verified artifact.

    The external supervisor serializes publication. Failed staging and native
    bytes remain available for diagnosis; no historical cursor is touched.
    """
    root, symbol = Path(root).resolve(), _symbol(symbol)
    start, end = session_bounds(session)
    destination = partition_directory(root, symbol, session)
    if destination.exists():
        return verify_partition(destination, root=root)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise XnasReplayArchiveError("Replay capture byte cap must be a positive integer")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 900):
        raise XnasReplayArchiveError("Replay timeout must be finite and at most 900 seconds")
    staging_parent = _archive_root(root) / ".staging" / symbol / session
    staging_parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(staging_parent).free < 3 * max_bytes + _MIN_FREE_SPACE:
        raise XnasReplayArchiveError("Insufficient free space for bounded XNAS replay")
    staging = staging_parent / str(uuid4())
    staging.mkdir()
    raw_path = staging / "provider.dbn"
    request = _request(symbol, session, start, end)
    delivery = (capture_fn or capture_replay)(
        api_key=api_key, schema=SCHEMA, symbols=[symbol], start=start.isoformat(), end=end.isoformat(),
        raw_path=raw_path, max_bytes=max_bytes, timeout_seconds=timeout_seconds,
    )
    _write_json(staging / "delivery.json", delivery)
    if not raw_path.is_file() or not 0 < raw_path.stat().st_size <= max_bytes:
        raise XnasReplayArchiveError("Replay capture did not respect the native byte budget")
    validate_replay_delivery(delivery, request, raw_path)
    frame = _normalize(raw_path, symbol=symbol, start=start, end=end)
    frame.to_parquet(staging / "normalized.parquet", index=False)
    published_at = _utc_now().isoformat()
    manifest = {"schema_version": PARTITION_VERSION, "dataset": DATASET, "schema": SCHEMA,
                "mode": MODE, "symbol": symbol, "session": session, "request": request,
                "partition_start": start.isoformat(), "partition_end": end.isoformat(),
                "published_at": published_at,
                "coverage": {"basis": "VERIFIED_EXCHANGE_SESSION_ACTION_WINDOW",
                    "calendar": "XNYS", "timezone": "America/Los_Angeles", "local_start": "04:00", "local_end": "17:00"},
                **{key: _payload(staging / filename) for key, filename in _PAYLOAD_NAMES.items()}}
    manifest["normalized"].update(row_count=len(frame), timestamp_column="timestamp",
                                  first_timestamp=frame.timestamp.min().isoformat(),
                                  last_timestamp=frame.timestamp.max().isoformat())
    _write_json(staging / "manifest.json", manifest)
    receipt = {"schema_version": RECEIPT_VERSION, "request_id": request["request_id"],
               "published_at": published_at, "manifest_checksum_sha256": file_checksum(staging / "manifest.json"),
               **{f"{key}_checksum_sha256": manifest[key]["checksum_sha256"] for key in _PAYLOAD_NAMES}}
    _write_json(staging / "receipt.json", receipt)
    _verify(staging, root=root, staging=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return verify_partition(destination, root=root)
    # Directory rename on the same filesystem publishes all receipts at once.
    # Never replace an existing destination, including an invalid publication.
    try:
        staging.rename(destination)
    except FileExistsError:
        return verify_partition(destination, root=root)
    return verify_partition(destination, root=root)


__all__ = ["XnasReplayArchiveError", "session_bounds", "partition_directory",
           "publish_session", "verify_partition", "discover_partitions"]
