"""Verify and read saved Scout display projections without accessing peer stores.

Only explicitly imported, locally pinned archives are discovered. This module
never reads CODEXSTORE, databases, providers, models, or trader controls.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}\Z")
_SOURCE_FILES = {"app/ui/scout_stats_data.py", "app/ui/scout_stats_widgets.py"}
_SNAPSHOT = {"session", "status", "message", "forecasts", "orders", "warnings",
             "prediction_status", "execution_status"}
_PAYLOAD = {"schema_version", "producer", "recipient", "session", "generated_at",
            "completion_id", "source_files", "snapshot", "prediction_summary",
            "execution_summary", "per_symbol"}
_MANIFEST = {"schema_version", "producer", "recipient", "machine", "session", "generated_at",
             "completion_id", "source_files", "files", "authorization", "consumer_interface",
             "limitations", "runtime_implications", "scope", "validation"}
_FORECAST = {"forecast_id", "pc", "symbol", "mode", "target_version", "target_open", "target_close",
             "probability_not_down", "reference_at", "issued_at", "supported_target", "outcome_status",
             "target_not_down", "outcome_observed_at", "target_close_price", "unavailable_reason"}
_ORDER = {"request_id", "forecast_id", "symbol", "side", "status", "submission_state", "quantity",
          "filled_quantity", "issued_at", "reference_at", "observed_at", "submitted_at",
          "broker_order_id", "fills", "note"}
_FILL = {"fill_id", "quantity", "price", "executed_at"}
_STATES = {"read", "empty", "available", "partial", "missing", "unreadable"}


def _require(condition, message):
    if not condition:
        raise ValueError("Scout display export: " + message)


def _fields(value, allowed, required=()):
    _require(isinstance(value, dict) and set(required) <= set(value) <= allowed, "unexpected or missing fields")


def _text(value, *, nullable=False, maximum=4096):
    _require((nullable and value is None) or (isinstance(value, str) and len(value) <= maximum
             and not any(ord(c) < 32 and c not in "\n\t\r" for c in value)), "invalid text")


def _stamp(value, *, nullable=False):
    if nullable and value is None:
        return None
    _text(value, maximum=64)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(result.tzinfo is not None, "timestamp has no timezone")
        return result
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Scout display export: invalid timestamp") from exc


def _day(value):
    try:
        _require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value, "invalid session")
    except (ValueError, TypeError) as exc:
        raise ValueError("Scout display export: invalid session") from exc
    return value


def _number(value, *, nullable=False, probability=False, numeric_text=False):
    if numeric_text and isinstance(value, str):
        _require(len(value) <= 64 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is not None,
                 "invalid numeric text")
        value = float(value)
    try:
        valid = ((nullable and value is None) or
                 (type(value) in {int, float} and math.isfinite(value) and value >= 0
                  and (not probability or value <= 1)))
    except (OverflowError, TypeError):
        valid = False
    _require(valid, "invalid numeric field")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON field")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Scout display export: invalid JSON") from exc


def _read(directory, name, maximum):
    path = directory / name
    _require(not path.is_symlink() and path.resolve().parent == directory.resolve()
             and path.is_file(), "missing file or unsafe path")
    _require(path.stat().st_size <= maximum, "file exceeds display limit")
    raw = path.read_bytes()
    _require(len(raw) <= maximum, "file exceeds display limit")
    return raw


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _rows(snapshot, session):
    from app.ui.scout_stats_data import _session

    _fields(snapshot, _SNAPSHOT, _SNAPSHOT)
    _require(snapshot["session"] == session, "snapshot session differs")
    for name in ("status", "prediction_status", "execution_status"):
        _require(isinstance(snapshot[name], str) and snapshot[name] in _STATES, "invalid availability status")
    _text(snapshot["message"])
    _require(isinstance(snapshot["warnings"], list) and len(snapshot["warnings"]) <= 100, "invalid warnings")
    for warning in snapshot["warnings"]:
        _text(warning)
    forecasts, orders = snapshot["forecasts"], snapshot["orders"]
    _require(isinstance(forecasts, list) and len(forecasts) <= 100000
             and isinstance(orders, list) and len(orders) <= 100000, "invalid row collections")
    identities = set()
    for row in forecasts:
        _fields(row, _FORECAST, {"forecast_id", "pc", "symbol", "mode", "target_version", "reference_at",
                               "issued_at", "target_open", "target_close", "supported_target", "outcome_status"})
        for name in ("forecast_id", "symbol", "target_version"):
            _text(row[name], maximum=200)
        _require(row["forecast_id"] and row["forecast_id"] not in identities, "duplicate or empty forecast identity")
        identities.add(row["forecast_id"])
        _require(row["pc"] == "Scout" and row["mode"] == "live" and type(row["supported_target"]) is bool,
                 "invalid forecast origin or target")
        _require(row["outcome_status"] in ("scored", "pending", "missing", "unscorable", "synthetic"), "invalid outcome status")
        for name in ("reference_at", "issued_at", "target_open", "target_close", "outcome_observed_at"):
            _stamp(row.get(name), nullable=True)
        _require(_session(row["reference_at"]) == session, "forecast belongs to another session")
        _number(row.get("probability_not_down"), nullable=True, probability=True)
        _number(row.get("target_close_price"), nullable=True)
        _require(row.get("target_not_down") is None or type(row["target_not_down"]) is bool, "invalid outcome label")
        _text(row.get("unavailable_reason"), nullable=True)
    identities = set()
    fills = {}
    for row in orders:
        _fields(row, _ORDER, {"request_id", "symbol", "status", "submission_state", "reference_at", "fills"})
        for name in ("request_id", "symbol"):
            _text(row[name], maximum=200)
        _require(row["request_id"] and row["request_id"] not in identities, "duplicate or empty request identity")
        identities.add(row["request_id"])
        for name in ("forecast_id", "broker_order_id", "note"):
            _text(row.get(name), nullable=True)
        _require(row.get("side") in ("BUY", "SELL", "HOLD", None), "invalid order side")
        _require(row["status"] in ("FILLED", "PARTIAL", "WORKING", "CANCELLED", "REJECTED", "UNKNOWN", "NOT_SENT"), "invalid order status")
        _require(row["submission_state"] in ("ACCEPTED", "UNKNOWN", "NOT_SENT", "REJECTED", "PREPARED", "ERROR"), "invalid submission state")
        for name in ("quantity", "filled_quantity"):
            _number(row.get(name), nullable=True)
        for name in ("reference_at", "issued_at", "observed_at", "submitted_at"):
            _stamp(row.get(name), nullable=True)
        _require(_session(row["reference_at"]) == session, "request belongs to another session")
        _require(isinstance(row["fills"], list) and len(row["fills"]) <= 100000, "invalid fills")
        own_fills = set()
        for fill in row["fills"]:
            _fields(fill, _FILL, _FILL)
            _text(fill["fill_id"], maximum=200)
            _require(fill["fill_id"] and fill["fill_id"] not in own_fills, "duplicate or empty fill identity")
            own_fills.add(fill["fill_id"])
            _number(fill["quantity"])
            # The saved journal projection preserves decimal prices as strings.
            _number(fill["price"], nullable=True, numeric_text=True)
            _stamp(fill["executed_at"], nullable=True)
            key = (row.get("broker_order_id") or row["request_id"], fill["fill_id"])
            _require(key not in fills or fills[key] == fill, "conflicting fill identity")
            fills[key] = fill


def verify_export(directory, expected_manifest_sha256=None, expected_snapshot_sha256=None):
    """Verify exact export bytes, identity, display fields and recomputed summaries."""
    directory = Path(directory)
    _require(not _link(directory) and directory.is_dir(), "unsafe archive directory")
    manifest_raw = _read(directory, "manifest.json", 65536)
    if expected_manifest_sha256 is not None:
        _require(_SHA.fullmatch(expected_manifest_sha256) is not None
                 and _hash(manifest_raw) == expected_manifest_sha256, "manifest hash differs")
    manifest = _json(manifest_raw)
    _fields(manifest, _MANIFEST, {"schema_version", "producer", "recipient", "machine", "session",
                                "generated_at", "completion_id", "source_files", "files"})
    _require(manifest["schema_version"] == "scout-15m-display-delivery-v1"
             and manifest["producer"] == "Scout" and manifest["recipient"] == "Atlas"
             and manifest["machine"] == "pc-new", "delivery identity differs")
    _day(manifest["session"])
    _stamp(manifest["generated_at"])
    _require(isinstance(manifest["completion_id"], str) and _ID.fullmatch(manifest["completion_id"]), "invalid completion identity")
    _fields(manifest["source_files"], _SOURCE_FILES, _SOURCE_FILES)
    for digest in manifest["source_files"].values():
        _require(isinstance(digest, str) and _SHA.fullmatch(digest), "invalid source hash")
    _fields(manifest["files"], {"snapshot.json", "report.html"}, {"snapshot.json", "report.html"})
    raw_files = {}
    for name, maximum in (("snapshot.json", 32 * 1024 * 1024), ("report.html", 4 * 1024 * 1024)):
        item = manifest["files"][name]
        _fields(item, {"bytes", "sha256"}, {"bytes", "sha256"})
        _require(type(item["bytes"]) is int and 0 <= item["bytes"] <= maximum
                 and isinstance(item["sha256"], str) and _SHA.fullmatch(item["sha256"]), "invalid file record")
        raw = _read(directory, name, maximum)
        _require(len(raw) == item["bytes"] and _hash(raw) == item["sha256"], "file size or hash differs")
        raw_files[name] = raw
    if expected_snapshot_sha256 is not None:
        _require(_hash(raw_files["snapshot.json"]) == expected_snapshot_sha256, "snapshot hash differs")
    payload = _json(raw_files["snapshot.json"])
    _fields(payload, _PAYLOAD, _PAYLOAD)
    _require(payload["schema_version"] == "scout-15m-display-export-v1", "unsupported snapshot schema")
    for name in ("producer", "recipient", "session", "generated_at", "completion_id", "source_files"):
        _require(payload[name] == manifest[name], "snapshot and manifest identity differ")
    _rows(payload["snapshot"], payload["session"])
    from app.ui.scout_stats_data import execution_summary, prediction_summary
    forecasts, orders = payload["snapshot"]["forecasts"], payload["snapshot"]["orders"]
    _require(payload["prediction_summary"] == prediction_summary(forecasts), "prediction summary differs")
    _require(payload["execution_summary"] == execution_summary(orders), "execution summary differs")
    forecast_groups, order_groups = defaultdict(list), defaultdict(list)
    for row in forecasts:
        forecast_groups[row["symbol"]].append(row)
    for row in orders:
        order_groups[row["symbol"]].append(row)
    symbols = set(forecast_groups) | set(order_groups)
    per_symbol = {symbol: {"prediction": prediction_summary(forecast_groups[symbol]),
                           "execution": execution_summary(order_groups[symbol])} for symbol in symbols}
    _require(payload["per_symbol"] == per_symbol, "per-symbol summary differs")
    return payload


def _archives(datastore_root):
    base = Path(datastore_root).resolve()
    root = base / "intraday/scout/display-exports/v1"
    if not root.is_dir():
        return
    path = base
    for part in ("intraday", "scout", "display-exports", "v1"):
        path = path / part
        _require(not _link(path) and path.resolve().is_relative_to(base), "unsafe archive root")
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        _require(not _link(directory) and directory.resolve().parent == root.resolve(), "unsafe archive directory")
        if not (directory / "receipt.json").is_file():
            continue
        receipt = _json(_read(directory, "receipt.json", 65536))
        _require(isinstance(receipt, dict), "invalid local receipt")
        for name in ("manifest_sha256", "snapshot_sha256"):
            _require(isinstance(receipt.get(name), str) and _SHA.fullmatch(receipt[name]), "invalid local hash pin")
        _require(receipt.get("completion_id") == directory.name, "local completion identity differs")
        _day(receipt.get("session"))
        _stamp(receipt.get("verified_at"))
        yield directory, receipt


def peer_stats_sessions(datastore_root):
    """Discover explicitly imported sessions; loading always rechecks all bytes."""
    return tuple(sorted({receipt["session"] for _, receipt in _archives(datastore_root)}, reverse=True))


def load_peer_stats(datastore_root, session):
    """Return the newest verified export for one session, never combine populations."""
    _day(session)
    candidates = []
    for directory, receipt in _archives(datastore_root):
        if receipt["session"] != session:
            continue
        payload = verify_export(directory, receipt["manifest_sha256"], receipt["snapshot_sha256"])
        _require(payload["session"] == session and payload["completion_id"] == receipt["completion_id"], "local receipt identity differs")
        candidates.append(payload)
    return max(candidates, key=lambda payload: (_stamp(payload["generated_at"]), payload["completion_id"])) if candidates else None
