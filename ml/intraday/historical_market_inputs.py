"""Bounded saved-CME reconstruction for retrospective model training only.

No provider imports, acquisition, import-time I/O, or fake historical receipts.
Only an explicit materialization writes a new content-addressed compact object.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

VERSION = "intraday-cme-reconstructed-market-time-v1"
STEP_NS = 900_000_000_000
MAX_AGE_SECONDS = 900
ROOTS = ("ES", "NQ", "RTY", "CL", "GC", "ZN")
MARKERS = ("synthetic", "is_synthetic", "is_derived", "no_trade", "is_no_trade",
           "source_empty", "is_source_empty", "synthetic_open", "synthetic_close")
BOOK_COLUMNS = tuple(f"{side}_{field}_{level:02d}" for level in range(10)
                     for side in ("bid", "ask") for field in ("px", "sz"))
COLUMNS = ("ts_event", "ts_recv", "rtype", "publisher_id", "instrument_id", "sequence", "flags",
           "symbol", "provider_symbol", "provider_stype_in", "provider_schema", "cme_context_group",
           "synthetic_constituents", *MARKERS, *BOOK_COLUMNS)
DEFAULT_BOUNDS = {"max_input_bytes": 1342177280, "max_rows": 32000000,
                  "max_files": 1500, "max_output_bytes": 33554432, "max_days": 180}


def _stamp(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Historical market clocks must be explicit timezone timestamps")
    return result.tz_convert("UTC")


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _read_json(path, bound=1048576):
    path = Path(path)
    if path.stat().st_size > bound:
        raise ValueError("Historical source manifest exceeds compact bound")
    return json.loads(path.read_bytes())


def _safe_path(root, path):
    result = Path(path).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Historical source escapes datastore")
    return result


def _native_value(value):
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return _stamp(value).isoformat()
    return value


def _features(native):
    levels = []
    for i in range(10):
        try:
            bid, ask, bs, az = (float(native[f"{s}_{f}_{i:02d}"]) for s, f in
                                (("bid", "px"), ("ask", "px"), ("bid", "sz"), ("ask", "sz")))
        except (TypeError, ValueError, KeyError):
            continue
        if all(math.isfinite(x) for x in (bid, ask, bs, az)) and 0 < bid <= ask < 9e9 and 0 <= bs < 4294967295 and 0 <= az < 4294967295:
            levels.append((i, bid, ask, bs, az))
    top = next((row for row in levels if row[0] == 0), None)
    total = sum(row[3] + row[4] for row in levels)
    return {"spread_relative": (top[2]-top[1])/((top[1]+top[2])/2) if top else None,
            "depth_imbalance": sum(row[3]-row[4] for row in levels)/total if total > 0 else None}


def discover_sources(datastore_root, *, start, end, roots=ROOTS, bounds=None):
    """Read compact manifests/Parquet footers; never launch a provider client."""
    root = Path(datastore_root).resolve()
    lower, upper = _stamp(start), _stamp(end)
    limits = DEFAULT_BOUNDS | dict(bounds or {})
    if upper <= lower or upper-lower > pd.Timedelta(days=limits["max_days"]):
        raise ValueError("Historical request exceeds explicit time bound")
    roots = tuple(str(r).upper() for r in roots)
    if not roots or any(r not in ROOTS for r in roots):
        raise ValueError("Unknown configured CME root")
    sources = []
    archive = root / "market-data/databento/cme/GLBX.MDP3/mbp-10"
    for manifest_path in sorted(archive.glob("*/windows/*/manifest.json")):
        manifest = _read_json(manifest_path)
        request = manifest.get("request", {})
        symbols = request.get("symbol_scope", [])
        match = re.fullmatch(r"(ES|NQ|RTY|CL|GC|ZN)\.[cv]\.0", str(symbols[0])) if len(symbols) == 1 else None
        if not match or match[1] not in roots or request.get("stype_in") != "continuous" or request.get("dataset") != "GLBX.MDP3" or request.get("schema") != "mbp-10":
            continue
        normalized = manifest["normalized"]
        if _stamp(normalized["latest_timestamp"]) < lower or _stamp(normalized["earliest_timestamp"]) > upper:
            continue
        receipt_path = manifest_path.with_name("receipt.json")
        receipt = _read_json(receipt_path)
        manifest_sha = _hash(manifest_path)
        if receipt.get("manifest_checksum_sha256") != manifest_sha:
            raise ValueError("Historical archive manifest receipt mismatch")
        path = _safe_path(root, manifest_path.parent / normalized["path"])
        sources.append({"path": str(path), "kind": "native_archive", "root": match[1],
                        "sha256_expected": normalized["checksum_sha256"], "manifest_sha256": manifest_sha,
                        "manifest_path": str(manifest_path), "receipt_path": str(receipt_path),
                        "receipt_sha256": _hash(receipt_path), "published_at": receipt.get("published_at"),
                        "request_id": receipt.get("request_id"), "native_selector": symbols[0]})
    snapshots = root / "pools/cme/snapshots/l2/databento/5m"
    for manifest_path in sorted(snapshots.glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        stamp = _stamp(manifest["snapshot_for"])
        # Snapshot may contain older genuine book events. Only inspect bounded
        # snapshot dates; never relabel its stale selected event as snapshot time.
        if stamp < lower or stamp > upper:
            continue
        path = _safe_path(root, manifest_path.with_name("snapshot.parquet"))
        sources.append({"path": str(path), "kind": "saved_snapshot", "root": None,
                        "sha256_expected": manifest["snapshot_checksum_sha256"],
                        "manifest_sha256": _hash(manifest_path), "manifest_path": str(manifest_path),
                        "published_at": manifest.get("available_at"), "snapshot_for": stamp.isoformat()})
    total_bytes = total_rows = 0
    for source in sources:
        path = Path(source["path"])
        source["bytes"] = path.stat().st_size
        source["rows"] = pq.ParquetFile(path).metadata.num_rows
        total_bytes += source["bytes"]
        total_rows += source["rows"]
    if len(sources) > limits["max_files"] or total_bytes > limits["max_input_bytes"] or total_rows > limits["max_rows"]:
        raise ValueError("Historical source inventory exceeds explicit resource bounds")
    return {"sources": sources, "input_bytes": total_bytes, "input_rows": total_rows,
            "roots": roots, "start": lower.isoformat(), "end": upper.isoformat(), "bounds": limits}


def _candidates(frame, source, lower_ns, upper_ns, roots, counts):
    if frame.empty:
        return []
    frame = frame.copy()
    valid = pd.Series(True, index=frame.index)
    for marker in MARKERS:
        if marker in frame:
            valid &= frame[marker].map(lambda x: isinstance(x, (bool, np.bool_)) and not x)
    if "synthetic_constituents" in frame:
        valid &= frame.synthetic_constituents.map(lambda x: not isinstance(x, (bool, np.bool_))
            and isinstance(x, (int, float, np.integer, np.floating)) and math.isfinite(float(x)) and x == 0)
    flags = pd.to_numeric(frame.get("flags", pd.Series(0, index=frame.index)), errors="coerce")
    valid &= flags.notna() & flags.fillna(4).astype("int64").map(lambda x: not x & 4)
    valid &= pd.to_numeric(frame.get("rtype"), errors="coerce").eq(10)
    event = pd.to_datetime(frame.get("ts_event"), utc=True, errors="coerce")
    received = pd.to_datetime(frame.get("ts_recv"), utc=True, errors="coerce")
    valid &= event.notna() & received.notna() & event.le(received)
    clock = received.astype("datetime64[ns, UTC]").astype("int64")
    valid &= clock.ge(lower_ns) & clock.le(upper_ns)
    for field in ("instrument_id", "publisher_id", "sequence"):
        numeric = pd.to_numeric(frame.get(field), errors="coerce")
        valid &= numeric.notna() & numeric.ge(0) & numeric.mod(1).eq(0)
    if source["kind"] == "native_archive":
        frame["_root"] = source["root"]
    else:
        valid &= frame.get("provider_schema", pd.Series("", index=frame.index)).eq("mbp-10")
        valid &= frame.get("provider_stype_in", pd.Series("", index=frame.index)).eq("continuous")
        valid &= frame.get("cme_context_group", pd.Series("", index=frame.index)).eq("context")
        frame["_root"] = frame.get("provider_symbol", pd.Series("", index=frame.index)).str.extract(r"^(ES|NQ|RTY|CL|GC|ZN)\.[cv]\.0$", expand=False)
    valid &= frame["_root"].isin(roots)
    counts["rows_read"] += len(frame)
    counts["rejected_or_outside_bounds"] += int((~valid).sum())
    frame = frame.loc[valid].copy()
    if frame.empty:
        return []
    frame["_clock"] = clock.loc[valid]
    frame["_decision"] = ((frame["_clock"] + STEP_NS - 1) // STEP_NS) * STEP_NS
    frame = frame.loc[frame["_decision"].le(upper_ns)]
    if frame.empty:
        return []
    # Only latest clocks per quarter/root can affect an end-of-reference input.
    maxima = frame.groupby(["_root", "_decision"])["_clock"].transform("max")
    frame = frame.loc[frame["_clock"].eq(maxima)]
    candidates = []
    for _, row in frame.iterrows():
        native = {name: _native_value(row[name]) for name in COLUMNS if name in row and name not in MARKERS}
        candidates.append({"root": row["_root"], "decision_ns": int(row["_decision"]),
                           "clock_ns": int(row["_clock"]), "sequence": int(row["sequence"]),
                           "native": native, "source_sha256": source["sha256_expected"],
                           "source_row_index": int(row["_row_index"]), "source_kind": source["kind"]})
    return candidates


def materialize_cme_history(datastore_root, *, start, end, roots=ROOTS, output_root=None, bounds=None):
    """Reduce bounded existing native books to immutable quarter-end features."""
    inventory = discover_sources(datastore_root, start=start, end=end, roots=roots, bounds=bounds)
    lower, upper = _stamp(start).value, _stamp(end).value
    counts, selected = Counter(), {}
    for source in inventory["sources"]:
        path = Path(source["path"])
        before = (path.stat().st_size, path.stat().st_mtime_ns)
        if _hash(path) != source["sha256_expected"]:
            raise ValueError("Historical native file differs from saved checksum")
        pf = pq.ParquetFile(path)
        columns = [name for name in COLUMNS if name in pf.schema_arrow.names]
        required = {"ts_event", "ts_recv", "rtype", "publisher_id", "instrument_id", "sequence"}
        if not required.issubset(columns):
            raise ValueError("Historical native source lacks identity/clock columns")
        offset = 0
        for batch in pf.iter_batches(batch_size=65536, columns=columns, use_threads=False):
            # Native archive pandas metadata may nominate ts_recv as its index.
            # Keep projected physical clock columns; do not silently lose them.
            frame = batch.to_pandas(ignore_metadata=True)
            frame["_row_index"] = np.arange(offset, offset+len(frame))
            offset += len(frame)
            for row in _candidates(frame, source, lower, upper, inventory["roots"], counts):
                key = (row["root"], row["decision_ns"])
                previous = selected.get(key)
                rank = (row["clock_ns"], row["sequence"])
                old_rank = (previous["clock_ns"], previous["sequence"]) if previous else (-1, -1)
                different_contract = (previous is not None and row["clock_ns"] == previous["clock_ns"]
                    and (row["native"]["publisher_id"], row["native"]["instrument_id"]) !=
                        (previous["native"]["publisher_id"], previous["native"]["instrument_id"]))
                if rank > old_rank:
                    row["conflict"] = different_contract or (previous is not None and row["clock_ns"] == previous["clock_ns"] and previous.get("ambiguous_contract", False))
                    row["ambiguous_contract"] = row["conflict"]
                    selected[key] = row
                elif different_contract:
                    previous["conflict"] = previous["ambiguous_contract"] = True
                    counts["conflicting_latest_records"] += 1
                elif rank == old_rank:
                    # Duplicate saved snapshots are harmless only when native
                    # identity and book bytes agree; never pick a conflict by file order.
                    if row["native"] != previous["native"]:
                        previous["conflict"] = True
                        counts["conflicting_latest_records"] += 1
                    else:
                        counts["duplicate_latest_records"] += 1
        if before != (path.stat().st_size, path.stat().st_mtime_ns):
            raise ValueError("Historical source changed during materialization")
    rows = []
    for _, row in sorted(selected.items()):
        if row["conflict"]:
            counts["conflicting_decisions_excluded"] += 1
            continue
        features = _features(row["native"])
        if not any(value is not None for value in features.values()):
            counts["invalid_books_excluded"] += 1
            continue
        value = {"source": "db_cme_"+row["root"].lower(), "decision_at": pd.Timestamp(row["decision_ns"], tz="UTC").isoformat(),
                 "market_observed_at": pd.Timestamp(row["clock_ns"], tz="UTC").isoformat(),
                 "native": row["native"], "features": features,
                 "source_sha256": row["source_sha256"], "source_row_index": row["source_row_index"], "source_kind": row["source_kind"]}
        value["id"] = hashlib.sha256(_bytes(value)).hexdigest()
        rows.append(value)
    result = {"version": VERSION, "materialized_at": datetime.now(timezone.utc).isoformat(),
              "temporal_contract": "reconstructed market-time; event<=provider_receive<=reference close; not contemporaneous local receipts",
              "purpose": "retrospective_training_and_validation_only", "max_age_seconds": MAX_AGE_SECONDS,
              "inventory": inventory, "counts": dict(counts), "rows": rows}
    data = _bytes(result)
    if len(data) > inventory["bounds"]["max_output_bytes"]:
        raise ValueError("Historical compact result exceeds output bound")
    destination = Path(output_root) if output_root is not None else Path(datastore_root)/"intraday/market-inputs/historical-cme"
    destination.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(data).hexdigest()
    path = destination/(identity+".json")
    with path.open("xb") as handle:
        handle.write(data)
    return {"path": str(path.resolve()), "sha256": identity, "bytes": len(data), "rows": len(rows),
            "version": VERSION, "counts": dict(counts), "input_bytes": inventory["input_bytes"], "input_rows": inventory["input_rows"]}


class HistoricalMarketInputs:
    """Immutable, explicit artifact reader; never auto-materializes or fetches."""
    def __init__(self, datastore_root, artifact_path=None):
        self.root = Path(datastore_root).resolve()
        self.artifact_path = Path(artifact_path).resolve() if artifact_path else None
        self._artifact = None
        self._lookup = {}
        if self.artifact_path is not None:
            if self.artifact_path.stat().st_size > DEFAULT_BOUNDS["max_output_bytes"]:
                raise ValueError("Historical compact artifact exceeds bound")
            data = self.artifact_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != self.artifact_path.stem:
                raise ValueError("Historical artifact hash mismatch")
            self._artifact = json.loads(data)
            if self._artifact["version"] != VERSION:
                raise ValueError("Historical context version mismatch")
            for row in self._artifact["rows"]:
                identity = row["id"]
                if hashlib.sha256(_bytes({k: v for k, v in row.items() if k != "id"})).hexdigest() != identity:
                    raise ValueError("Historical selected-row hash mismatch")
                if row["features"] != _features(row["native"]):
                    raise ValueError("Historical feature differs from frozen native book")
                event, clock = _stamp(row["native"]["ts_event"]), _stamp(row["native"]["ts_recv"])
                decision = _stamp(row["decision_at"])
                if event > clock or clock != _stamp(row["market_observed_at"]) or decision.value != ((clock.value+STEP_NS-1)//STEP_NS)*STEP_NS:
                    raise ValueError("Historical reconstructed clocks are inconsistent")
                key = (row["source"], _stamp(row["decision_at"]).value)
                if key in self._lookup:
                    raise ValueError("Historical artifact has duplicate decisions")
                self._lookup[key] = row

    def join(self, decision_times, *, cutoff, purpose="retrospective_training"):
        if purpose not in {"retrospective_training", "retrospective_validation"}:
            raise ValueError("Reconstructed context cannot represent contemporaneous evaluation or inference")
        from .market_inputs import MODEL_FEATURE_NAMES
        names = list(MODEL_FEATURE_NAMES)
        columns = {name: i for i, name in enumerate(names)}
        cutoff = _stamp(cutoff)
        rows, provenance = [], []
        for value in decision_times:
            decision = _stamp(value)
            extra, sources = [0.0]*len(names), {}
            if decision <= cutoff:
                for root in ROOTS:
                    source = "db_cme_"+root.lower()
                    item = self._lookup.get((source, decision.value))
                    if item is None:
                        continue
                    age = (decision-_stamp(item["market_observed_at"])).total_seconds()
                    if not 0 <= age <= MAX_AGE_SECONDS:
                        continue
                    for field, number in item["features"].items():
                        name = f"market_{source}_{field}"
                        if number is not None and name in columns and math.isfinite(float(number)):
                            index = columns[name]
                            extra[index:index+2] = [float(number), 1.0]
                    sources[source] = {"id": item["id"], "eligible": True, "market_observed_at": item["market_observed_at"],
                                       "age_seconds": age, "source_sha256": item["source_sha256"],
                                       "native_instrument_id": item["native"]["instrument_id"], "reconstructed": True}
            rows.append(extra)
            provenance.append({"decision_at": decision.isoformat(), "sources": sources, "purpose": purpose})
        return {"feature_names": names, "rows": rows, "provenance": provenance, "version": VERSION,
                "artifact": {"path": str(self.artifact_path), "sha256": self.artifact_path.stem,
                             "materialized_at": self._artifact["materialized_at"]} if self._artifact else None,
                "receipt_rule": "not a local receipt; exact reference-close historical reconstruction; no forward fill across decision slots"}
