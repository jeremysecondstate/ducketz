"""Read-only cross-resolution checks for source-bound XNAS archive history.

Observed seconds are independent consistency evidence, never extra training
examples or replacements for the existing native minute target-price source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import databento as db
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from datafetching.databento_archive import discover_archive_partitions
from datafetching.databento_cold_start import _verify_generic_partition


SECOND_MINUTE_CHECK_CONTRACT = "xnas-archive-second-minute-consistency-v1"
_DATASET = "XNAS.ITCH"
_VALUES = ("open", "high", "low", "close", "volume")
_COLUMNS = ("timestamp", *_VALUES)
_MINUTE = pd.Timedelta(minutes=1)
_SECOND = pd.Timedelta(seconds=1)
_BATCH_ROWS = 131_072


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Archive consistency clocks must be timezone aware")
    return stamp.tz_convert("UTC")


def _request_clock(value) -> pd.Timestamp:
    # Native acquisition requests use UTC dates, rather than observation clocks.
    return pd.to_datetime(value, utc=True, errors="raise")


def _iso(value):
    return None if pd.isna(value) else value.isoformat()


def _range(frame, column="timestamp"):
    return {"first": _iso(frame[column].min()), "last": _iso(frame[column].max())}


def _normalize(frame: pd.DataFrame, *, interval: pd.Timedelta) -> pd.DataFrame:
    """Validate real observations, retaining wholly undefined native prices."""
    if not set(_COLUMNS).issubset(frame):
        raise ValueError("Archive consistency payload lacks observed OHLCV")
    out = frame.loc[:, list(_COLUMNS)].copy()
    if isinstance(out.timestamp.dtype, pd.DatetimeTZDtype):
        out["timestamp"] = out.timestamp.dt.tz_convert("UTC")
    else:
        out["timestamp"] = pd.to_datetime([_utc(v) for v in out.timestamp], utc=True)
    if out.timestamp.isna().any() or out.timestamp.ne(out.timestamp.dt.floor(interval)).any():
        raise ValueError("Archive observation is not at its native interval start")
    for name in _VALUES:
        out[name] = pd.to_numeric(out[name], errors="raise")
    missing = out[list(_VALUES[:-1])].isna()
    undefined = missing.all(axis=1)
    if (missing.any(axis=1) & ~undefined).any():
        raise ValueError("Partially undefined native OHLC")
    observed = out.loc[~undefined, list(_VALUES[:-1])]
    if not np.isfinite(observed.to_numpy(dtype=float)).all() or observed.le(0).any(axis=None):
        raise ValueError("Invalid observed native OHLC")
    if (out.high.lt(out[["open", "close", "low"]].max(axis=1))
            | out.low.gt(out[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Inconsistent observed native OHLC")
    if (not np.isfinite(out.volume.to_numpy(dtype=float)).all() or out.volume.lt(0).any()
            or out.volume.mod(1).ne(0).any()):
        raise ValueError("Invalid observed native volume")
    # Integer accumulation is exact and cannot wrap for any possible minute.
    limit = np.iinfo(np.int64).max // (60 if interval == _SECOND else 1)
    if out.volume.gt(limit).any():
        raise ValueError("Native volume exceeds exact aggregation capacity")
    out["volume"] = out.volume.astype("int64")
    out["undefined"] = undefined
    out = out.drop_duplicates(list(_COLUMNS))
    if out.timestamp.duplicated().any():
        raise ValueError("Conflicting same-clock native OHLCV")
    return out.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _coverage(ranges):
    merged = []
    for left, right in sorted((_utc(a), _utc(b)) for a, b in ranges):
        if left >= right:
            raise ValueError("Invalid second archive request interval")
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _aggregate(chunks: Iterable[pd.DataFrame], *, available_at):
    pieces, tail = [], pd.DataFrame()
    rows = future = undefined = 0
    examples = []
    first = last = previous = None

    def append(frame):
        if frame.empty:
            return
        pieces.append(frame.groupby(frame.timestamp.dt.floor("min"), sort=True).agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"),
            close=("close", "last"), volume=("volume", "sum"),
            observed_seconds=("timestamp", "size"), undefined_seconds=("undefined", "sum"),
            first_observed_second=("timestamp", "first"), last_observed_second=("timestamp", "last")))

    for frame in chunks:
        frame = _normalize(frame, interval=_SECOND)
        if frame.empty:
            continue
        if previous is not None and frame.timestamp.iloc[0] <= previous:
            raise ValueError("Second archive chunks overlap or are out of order")
        previous = frame.timestamp.iloc[-1]
        complete = frame.timestamp.add(_SECOND).le(available_at)
        future += int((~complete).sum())
        frame = frame.loc[complete]
        if frame.empty:
            continue
        rows += len(frame)
        first = frame.timestamp.iloc[0] if first is None else first
        last = frame.timestamp.iloc[-1]
        undefined += int(frame.undefined.sum())
        examples.extend(frame.loc[frame.undefined, "timestamp"].head(max(0, 10-len(examples))).map(_iso))
        frame = pd.concat([tail, frame], ignore_index=True) if not tail.empty else frame
        clock = frame.timestamp.dt.floor("min")
        is_tail = clock.eq(clock.iloc[-1])
        tail = frame.loc[is_tail]
        append(frame.loc[~is_tail])
    append(tail)
    columns = ["timestamp", *_VALUES, "observed_seconds", "undefined_seconds",
               "first_observed_second", "last_observed_second"]
    aggregate = pd.concat(pieces).reset_index() if pieces else pd.DataFrame(columns=columns)
    if aggregate.timestamp.duplicated().any():
        raise ValueError("Second aggregation produced duplicate minute identity")
    return aggregate, {"second_rows": rows, "second_rows_after_cutoff": future,
                       "second_range": {"first": _iso(first), "last": _iso(last)},
                       "undefined_second_rows": undefined, "undefined_second_examples": examples}


def _compare(aggregate, minutes, *, coverage, available_at, counts):
    native = _normalize(minutes, interval=_MINUTE)
    future = native.timestamp.add(_MINUTE).gt(available_at)
    future_count = int(future.sum())
    native = native.loc[~future]
    undefined_native = native.loc[native.undefined]
    native_valid = native.loc[~native.undefined]
    covered = pd.Series(False, index=aggregate.index)
    for left, right in _coverage(coverage):
        covered |= aggregate.timestamp.ge(left) & aggregate.timestamp.add(_MINUTE).le(right)
    completed = aggregate.timestamp.add(_MINUTE).le(available_at)
    defined = aggregate.undefined_seconds.eq(0)
    eligible = aggregate.loc[covered & completed & defined]
    common = eligible.merge(native_valid, on="timestamp", how="inner", suffixes=("_seconds", "_minute"),
                            validate="one_to_one")
    matches = {key: int(common[key+"_seconds"].eq(common[key+"_minute"]).sum()) for key in _VALUES}
    conflicts = pd.Series(False, index=common.index)
    for key in _VALUES:
        conflicts |= common[key+"_seconds"].ne(common[key+"_minute"])
    if conflicts.any():
        clocks = ", ".join(common.loc[conflicts, "timestamp"].head(5).map(_iso))
        raise ValueError(f"Second/minute native OHLCV conflict at {clocks}; exact field matches: {matches}")
    only_seconds = eligible.loc[~eligible.timestamp.isin(native.timestamp)]
    undefined_counterpart = eligible.timestamp.isin(undefined_native.timestamp)
    examples = []
    for row in only_seconds.head(10).itertuples():
        examples.append({"minute": _iso(row.timestamp), "open_observed_at": _iso(row.first_observed_second),
                         "close_observed_at": _iso(row.last_observed_second + _SECOND)})
    return {**counts, "status": "VERIFIED_OVERLAP" if len(common) else "NO_ELIGIBLE_OVERLAP",
            "minute_rows": len(native), "minute_range": _range(native),
            "minute_rows_after_cutoff": future_count, "undefined_minute_rows": len(undefined_native),
            "undefined_minute_examples": undefined_native.timestamp.head(10).map(_iso).tolist(),
            "second_minute_buckets": len(aggregate), "eligible_second_minute_buckets": len(eligible),
            "second_minutes_outside_complete_request": int((~covered).sum()),
            "second_minutes_after_cutoff": int((~completed).sum()),
            "second_minutes_with_undefined_observation": int((~defined).sum()),
            "overlap_minutes": len(common), "exact_ohlcv_overlap_minutes": len(common),
            "exact_field_matches": matches, "only_seconds_minutes_unused": len(only_seconds),
            "only_seconds_examples": examples,
            "second_minutes_with_undefined_native_counterpart": int(undefined_counterpart.sum()),
            "native_minutes_without_eligible_seconds": int((~native_valid.timestamp.isin(eligible.timestamp)).sum()),
            "first_second_at_minute_start": int(eligible.first_observed_second.eq(eligible.timestamp).sum()),
            "last_second_completes_at_minute_end": int(eligible.last_observed_second.add(_SECOND).eq(eligible.timestamp.add(_MINUTE)).sum()),
            "minutes_with_60_observed_seconds": int(eligible.observed_seconds.eq(60).sum()),
            "synthetic_rows": 0, "added_training_rows": 0}


def compare_second_minute_overlap(seconds: pd.DataFrame, minutes: pd.DataFrame, *, coverage,
                                  available_at) -> dict:
    """Pure exact comparison; all timestamps are native bar-start observations.

    Request intervals establish acquisition coverage, not an assumption that
    absent seconds contained no trades. Sparse buckets preserve actual clocks.
    """
    cutoff = _utc(available_at)
    aggregate, counts = _aggregate((seconds,), available_at=cutoff)
    return _compare(aggregate, minutes, coverage=coverage, available_at=cutoff, counts=counts)


def _read_batches(partition):
    timestamp = partition.manifest["normalized"]["timestamp_column"]
    parquet = pq.ParquetFile(partition.normalized_path)
    if timestamp != "ts_event" or not {timestamp, "symbol", *_VALUES}.issubset(parquet.schema_arrow.names):
        raise ValueError("Native OHLCV archive lacks ts_event/symbol/OHLCV")
    start, end = (_request_clock(partition.request[key]) for key in ("start", "end"))
    for batch in parquet.iter_batches(batch_size=_BATCH_ROWS, columns=[timestamp, "symbol", *_VALUES]):
        frame = batch.to_pandas()
        if timestamp not in frame and frame.index.name == timestamp:
            frame = frame.reset_index()
        if frame.symbol.isna().any() or not frame.symbol.astype(str).eq(partition.request["symbol_scope"][0]).all():
            raise ValueError("Native OHLCV archive contains another symbol")
        frame = frame.rename(columns={timestamp: "timestamp"})
        if (frame.timestamp.isna().any() or frame.timestamp.lt(start).any() or frame.timestamp.ge(end).any()
                or not frame.timestamp.is_monotonic_increasing):
            raise ValueError("Native OHLCV observation escaped its request or ordering")
        yield frame.loc[:, list(_COLUMNS)]


def _second_chunks(partitions):
    ranges = [(_request_clock(p.request["start"]), _request_clock(p.request["end"])) for p in partitions]
    overlapping = any(left < max(b for _, b in ranges[:i]) for i, (left, _) in enumerate(ranges) if i)
    if overlapping:
        # Overlapping acquisitions need exact same-clock deduplication before
        # aggregation. Ordinary disjoint archives retain bounded batch reads.
        frames = [frame for part in partitions for frame in _read_batches(part)]
        yield _normalize(pd.concat(frames, ignore_index=True), interval=_SECOND)
    else:
        for partition in partitions:
            yield from _read_batches(partition)


def _verify_partition(partition, root, *, available_at):
    request = partition.request
    schema, symbol = request.get("schema"), request["symbol_scope"][0]
    directory = partition.directory.resolve()
    expected = (root / "market-data/databento/us-equities" / _DATASET / schema / symbol).resolve()
    raw = (directory / str(partition.manifest["raw"]["path"])).resolve()
    if (request.get("dataset") != _DATASET or request.get("stype_in") != "raw_symbol"
            or not directory.is_relative_to(expected) or not raw.is_relative_to(directory)
            or not partition.normalized_path.resolve().is_relative_to(directory)):
        raise ValueError("Native archive request or payload path identity differs")
    _verify_generic_partition(directory, request)
    if (partition.receipt.get("raw_checksum_sha256") != partition.manifest["raw"]["checksum_sha256"]
            or partition.receipt.get("published_at") != partition.manifest.get("published_at")
            or _utc(partition.manifest["published_at"]) > available_at):
        raise ValueError("Native archive receipt/raw publication binding differs or is after cutoff")
    store = db.DBNStore.from_file(raw)
    try:
        meta = store.metadata
        start, end = (_request_clock(request[key]) for key in ("start", "end"))
        if (start >= end or str(meta.dataset) != _DATASET or str(meta.schema) != schema
                or str(meta.stype_in) != "raw_symbol" or list(meta.symbols) != [symbol]
                or meta.start != start.value or meta.end != end.value or meta.not_found):
            raise ValueError("Native DBN header does not match the archive request")
        notes = {"native_partial_symbology": list(meta.partial), "native_not_found": list(meta.not_found)}
    finally:
        store.reader.close()
    return raw, {"schema": schema, "symbol": symbol, "manifest_path": str(partition.manifest_path),
                 "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                 "rows": int(partition.manifest["normalized"]["row_count"]), **notes,
                 "provider_warnings": partition.manifest.get("provider_warnings", [])}


def verify_second_minute_overlap(root: Path, *, symbols: Sequence[str], available_at):
    """Verify saved 1s and 1m evidence and return a report plus bound source files.

    Uses the native cold-archive verifier for both payloads and Parquet metadata;
    DBN headers are independently checked without replaying the native records.
    The caller controls the existing production symbol universe and publication.
    """
    root, cutoff = Path(root).resolve(), _utc(available_at)
    clean = tuple(str(s).strip().upper() for s in symbols)
    if not clean or len(set(clean)) != len(clean) or any(not s for s in clean):
        raise ValueError("Archive consistency requires a unique configured universe")
    by_symbol = {symbol: {"ohlcv-1s": [], "ohlcv-1m": []} for symbol in clean}
    files, evidence = [], []
    for schema in ("ohlcv-1s", "ohlcv-1m"):
        for partition in discover_archive_partitions(root, market="us-equities", dataset=_DATASET, schema=schema):
            symbol = partition.request["symbol_scope"][0]
            if symbol not in by_symbol:
                continue
            raw, note = _verify_partition(partition, root, available_at=cutoff)
            files.extend((*partition.source_files, raw))
            evidence.append(note)
            by_symbol[symbol][schema].append(partition)
    reports = {}
    for symbol, schemas in by_symbol.items():
        seconds, minutes = schemas["ohlcv-1s"], schemas["ohlcv-1m"]
        if not seconds or not minutes:
            raise ValueError(f"Native 1s/1m archive is missing: {symbol}")
        aggregate, counts = _aggregate(_second_chunks(seconds), available_at=cutoff)
        frames = [frame for partition in minutes for frame in _read_batches(partition)]
        native = pd.concat(frames, ignore_index=True)
        ranges = [(_request_clock(p.request["start"]), _request_clock(p.request["end"])) for p in seconds]
        reports[symbol] = _compare(aggregate, native, coverage=ranges, available_at=cutoff, counts=counts)
        reports[symbol]["second_partitions"] = len(seconds)
        reports[symbol]["minute_partitions"] = len(minutes)
    report = {"schema_version": SECOND_MINUTE_CHECK_CONTRACT, "status": "VERIFIED",
              "dataset": _DATASET, "available_at": cutoff.isoformat(), "symbols": list(clean),
              "native_archive_partitions_verified": len(evidence), "by_symbol": reports,
              "partitions": evidence, "raw_record_replay": "NOT_PERFORMED",
              "verification": "Native archive payload hashes and normalized metadata; exact native DBN request headers",
              "interpretation": "1s validates native 1m overlap only; nonoverlap unused; no missing-second or boundary-price assumptions",
              "synthetic_rows": 0, "added_training_rows": 0}
    return report, tuple(dict.fromkeys(path.resolve() for path in files))
