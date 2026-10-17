"""Receipt-time market context for the independent 15-minute trader.

Imports have no I/O. Providers are constructed only by ``collect``/``start``.
All provider responses stay private under DATASTORE; an unavailable input is a
missing feature, never a made-up price, zero volume, or a forecast veto.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
from ml.market_gap_evidence import MARKERS as NATIVE_OBSERVATION_MARKERS, _native as _native_metadata

from .market_input_research import (
    SOURCE_FIELDS as RESEARCH_FIELDS, SOURCE_TTL_SECONDS as RESEARCH_TTL_SECONDS,
    SOURCE_REFRESH_SECONDS as RESEARCH_REFRESH_SECONDS, research_specs, extract_context,
    empty_macro_fallback_spec,
)
from .market_input_schwab_stream import (
    ADDITIONAL_SOURCE_FIELDS as SCHWAB_STREAM_FIELDS,
    ADDITIONAL_SOURCE_TTL_SECONDS as SCHWAB_STREAM_TTL_SECONDS,
)

VERSION = "intraday-receipt-market-inputs-v2"
UTC = timezone.utc
MAX_JOIN_RECEIPTS = 200000
DEFAULT_FUTURES = ("ES.c.0", "NQ.c.0", "CL.c.0", "GC.c.0", "ZN.c.0")
# Stable model columns, independent of endpoint success or current data values.
SOURCE_FIELDS = {
    "schwab_quote": ("spread_relative", "quote_imbalance", "last", "volume", "change_percent"),
    "schwab_minutes": ("return", "range_relative", "volume", "observed_bars"),
    "schwab_options": ("iv", "delta", "gamma", "theta", "vega", "put_call_volume", "put_call_open_interest", "spread_relative", "contracts"),
    "fmp_quote": ("price", "volume", "change_percent", "market_cap"),
    "fmp_5min": ("return", "range_relative", "volume", "observed_bars"),
    "fmp_profile": ("beta", "market_cap", "dividend", "average_volume"),
    "fmp_ratios": ("pe", "pb", "debt_equity", "current_ratio", "roe"),
    "fmp_income": ("revenue", "net_income", "eps"),
    "fmp_balance": ("cash", "total_debt", "equity"),
    "fmp_cashflow": ("operating_cashflow", "free_cashflow"),
    "fmp_earnings": ("days_to_event", "eps_actual", "eps_estimated"),
    "fmp_dividends": ("days_to_event", "dividend"),
    "fmp_splits": ("days_to_event", "ratio"),
    "fmp_analysts": ("target_consensus", "analysts"),
    "fmp_news": ("items",),
    "db_equity": ("spread_relative", "quote_imbalance", "trade_volume", "trade_count", "trade_imbalance", "vwap", "observed_bars"),
    "db_options": ("spread_relative", "quote_imbalance", "trade_volume", "trade_count", "put_call_volume", "contracts", "open_interest"),
}
STREAM_FUTURE_FIELDS = ("spread_relative", "depth_imbalance", "trade_volume", "trade_count", "trade_imbalance", "vwap", "cancel_add_ratio", "open_interest", "settlement")
for _root in ("es", "nq", "rty", "cl", "gc", "zn"):
    SOURCE_FIELDS[f"db_cme_{_root}"] = STREAM_FUTURE_FIELDS
SOURCE_FIELDS.update(RESEARCH_FIELDS)
SOURCE_FIELDS.update(SCHWAB_STREAM_FIELDS)
FEATURE_NAMES = tuple(f"market_{source}_{field}" for source, fields in SOURCE_FIELDS.items() for field in fields)
MODEL_FEATURE_NAMES = tuple(name for feature in FEATURE_NAMES for name in (feature, feature + "__available"))
SOURCE_TTL_SECONDS = {source: (172800 if source.startswith("fmp_") and source not in {"fmp_quote", "fmp_5min", "fmp_news"} else 1800) for source in SOURCE_FIELDS}
SOURCE_TTL_SECONDS["fmp_news"] = 7200
SOURCE_TTL_SECONDS.update(RESEARCH_TTL_SECONDS)
SOURCE_TTL_SECONDS.update(SCHWAB_STREAM_TTL_SECONDS)


class MarketInputReadUnavailable(RuntimeError):
    """Expected optional-store bounds/integrity failure with a safe reason code."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Market input timestamps must include a timezone")
    return value.astimezone(UTC)


def _iso(value: Any) -> str:
    return _utc(value).isoformat()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _ratio(a: Any, b: Any) -> float | None:
    a, b = _number(a), _number(b)
    return a / b if a is not None and b is not None and b != 0 else None


def _clean(value: Any) -> Any:
    """JSON-safe provider payload, rejecting credential-shaped keys recursively."""
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items() if not re.search(r"api.?key|access.?token|refresh.?token|authorization|password|secret", str(k), re.I)}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return re.sub(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)=([^&\s\"<>]+)",r"\1=[REDACTED]",value)
    if value is None or isinstance(value, (int, bool)):
        return value
    return str(value)


def _bytes(payload: Any) -> bytes:
    return json.dumps(_clean(payload), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _write_immutable(path: Path, raw: bytes) -> None:
    """Publish only complete bytes; interruptions cannot poison the hash path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise RuntimeError("Immutable market input object differs")
    finally:
        temporary.unlink(missing_ok=True)


def _symbols(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(str(x).strip().upper() for x in values))
    if not result or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", x) for x in result):
        raise ValueError("Configured equity symbols required")
    return result


class MarketInputStore:
    """Immutable content-addressed responses and a small indexed receipt catalog."""
    def __init__(self, datastore_root: str | Path):
        self.root = Path(datastore_root).resolve() / "intraday" / "market-inputs"
        self.database = self.root / "receipts.sqlite3"
        self._lock = threading.RLock()
        self._verified = OrderedDict()
        self.producer_runtime = None

    def capture_producer(self):
        """Freeze the loaded implementation and non-secret acquisition rules."""
        import sys
        from ml.market_provenance import capture_runtime
        names = [__name__, "ml.intraday.market_input_research", "ml.intraday.market_input_stream",
                 "ml.intraday.market_input_schwab_stream", "ml.intraday.provider_recovery",
                 "ml.market_gap_evidence",
                 "datafetching.schwab_fetch", "app.services.fmp_corporate_data"]
        loaded = capture_runtime(names, object_root=self.root / "source-objects")
        stream = sys.modules.get("ml.intraday.market_input_stream")
        rules = {"version": VERSION, "source_fields": SOURCE_FIELDS, "ttl_seconds": SOURCE_TTL_SECONDS,
                 "live_schemas": getattr(stream, "SCHEMAS", None),
                 "native_observation_markers": NATIVE_OBSERVATION_MARKERS,
                 "synthetic_constituents_rule": "absent_or_numeric_zero_excluding_boolean",
                 "scope": "loaded Python code and allowlisted rules; no credentials or environment dump"}
        value = {"runtime": loaded, "rules": rules}
        raw = _bytes(value)
        path = self.root / "producers" / (hashlib.sha256(raw).hexdigest()+".json")
        _write_immutable(path, raw)
        self.producer_runtime = {"path": str(path.relative_to(self.root)), "sha256": path.stem,
                                 "captured_at": loaded.get("captured_at")}
        return dict(self.producer_runtime)

    def persist_raw(self, payload: Any, received_at: Any) -> dict:
        """Durably retain the response before a feature extractor can fail."""
        received = _utc(received_at)
        raw = _bytes(payload)
        identity = hashlib.sha256(raw).hexdigest()
        target = self.root / "raw" / received.strftime("%Y/%m/%d") / (identity + ".json")
        with self._lock:
            _write_immutable(target, raw)
        return {"sha256": identity, "path": str(target.relative_to(self.root)), "bytes": len(raw)}

    def append(self, *, symbol: str, source: str, started_at: Any, received_at: Any,
               payload: Any, features: Mapping[str, Any], status: str = "ok",
               request: Mapping[str, Any] | None = None, event_at: Any = None) -> dict:
        started, received = _utc(started_at), _utc(received_at)
        if received < started:
            raise ValueError("Receipt precedes request start")
        if source not in SOURCE_FIELDS:
            raise ValueError("Unknown market input source")
        numeric = {name: _number(features.get(name)) for name in SOURCE_FIELDS[source]}
        # A future event cannot become evidence solely because a bad clock says
        # it was already received. Preserve it, but expose no model values.
        if event_at is not None and _utc(event_at) > received:
            status, numeric = "future_event", {name: None for name in numeric}
        if status != "ok":
            numeric = {name: None for name in numeric}
        payload_ref = self.persist_raw(payload, received)
        if self.producer_runtime is None:
            self.capture_producer()
        value = {"version": VERSION, "symbol": symbol, "source": source,
                 "request_started_at": _iso(started), "received_at": _iso(received),
                 "event_at": _iso(event_at) if event_at is not None else None,
                 "status": status, "request": dict(request or {}), "features": numeric,
                 "payload_ref": payload_ref, "producer_runtime": dict(self.producer_runtime)}
        raw = _bytes(value)
        identity = hashlib.sha256(raw).hexdigest()
        target = self.root / "objects" / received.strftime("%Y/%m/%d") / (identity + ".json")
        with self._lock:
            _write_immutable(target, raw)
            with closing(sqlite3.connect(self.database, timeout=5)) as db:
                db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL, status TEXT NOT NULL, features TEXT NOT NULL, object_path TEXT NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS receipt_symbol_time ON receipts(symbol,received_at)")
                db.execute("CREATE INDEX IF NOT EXISTS receipt_symbol_source_time ON receipts(symbol,source,received_at)")
                db.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?,?)", (identity, symbol, source, _iso(received), status, json.dumps(numeric, allow_nan=False), str(target.relative_to(self.root))))
                db.commit()
        return value | {"id": identity, "object_path": str(target)}

    def _verify_receipt(self, item, symbol, source):
        stamp,identity,status,features,relative = item
        path = (self.root/relative).resolve()
        if not path.is_relative_to(self.root) or path.name != identity+".json":
            raise ValueError("Invalid market input object identity/path")
        info = path.stat()
        key = (identity,info.st_mtime_ns,info.st_size)
        if key not in self._verified:
            if info.st_size > 1024*1024:
                raise ValueError("Receipt envelope exceeds compact 1 MiB bound")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != identity:
                raise ValueError("Market input receipt object hash mismatch")
            self._verified[key] = json.loads(raw)
            if len(self._verified)>4096:
                self._verified.popitem(last=False)
        recorded = self._verified[key]
        if (recorded["symbol"] != symbol or recorded["source"] != source or recorded["received_at"] != _iso(stamp)
                or recorded["status"] != status or recorded["features"] != features):
            raise ValueError("Market input catalog differs from immutable receipt")
        return recorded

    def join(self, symbol: str, decision_times: Sequence[Any], *, cutoff: Any) -> dict:
        decisions = [min(_utc(value), _utc(cutoff)) for value in decision_times]
        if not decisions:
            return {"feature_names": list(MODEL_FEATURE_NAMES), "rows": [], "provenance": []}
        by_source: dict[str, list] = {s: [] for s in SOURCE_FIELDS}
        if self.database.exists():
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                db.execute("PRAGMA query_only=ON")
                rows = db.execute("SELECT id,source,received_at,status,features,object_path FROM receipts WHERE symbol=? AND received_at>=? AND received_at<=? ORDER BY received_at DESC,id DESC LIMIT ?", (symbol, _iso(min(decisions) - timedelta(seconds=max(SOURCE_TTL_SECONDS.values()))), _iso(max(decisions)), MAX_JOIN_RECEIPTS+1)).fetchall()
            if len(rows) > MAX_JOIN_RECEIPTS:
                raise MarketInputReadUnavailable("catalog_query_limit")
            for identity, source, received, status, features, path in reversed(rows):
                if source in by_source:
                    try:
                        decoded=json.loads(features)
                        if not isinstance(decoded,dict):
                            raise ValueError("Expected catalog feature mapping")
                        by_source[source].append((_utc(received), identity, status, decoded, path))
                    except (ValueError,TypeError):
                        raise MarketInputReadUnavailable("catalog_integrity_unavailable") from None
        clocks = {s: [v[0] for v in values] for s, values in by_source.items()}
        conflicting_clocks = {source:set() for source in by_source}
        for source, values in by_source.items():
            same_clock = {}
            for item in values:
                previous = same_clock.setdefault(item[0], item[2:4])
                if previous != item[2:4]:
                    conflicting_clocks[source].add(item[0])
        matrix, provenance = [], []
        for decision in decisions:
            row, used = [], {}
            for source, fields in SOURCE_FIELDS.items():
                index = bisect_right(clocks[source], decision) - 1
                item = by_source[source][index] if index >= 0 else None
                eligible = item is not None and item[2] == "ok" and (decision - item[0]).total_seconds() <= SOURCE_TTL_SECONDS[source]
                verification = "not_selected"
                if item:
                    try:
                        self._verify_receipt(item,symbol,source)
                        verification = "receipt_hash_verified"
                    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):
                        eligible, verification = False,"receipt_integrity_unavailable"
                    if item[0] in conflicting_clocks[source]:
                        eligible,verification=False,"conflicting_same_receipt_time"
                if item:
                    used[source] = {"id": item[1], "received_at": _iso(item[0]), "status": item[2], "eligible": eligible, "object_path": item[4],"verification":verification}
                for field in fields:
                    number = _number(item[3].get(field)) if eligible else None
                    row.extend((number if number is not None else 0.0, float(number is not None)))
            matrix.append(row)
            provenance.append({"decision_at": _iso(decision), "sources": used})
        return {"feature_names": list(MODEL_FEATURE_NAMES), "rows": matrix, "provenance": provenance}

    def latest_receipt(self, symbol: str, source: str) -> datetime | None:
        if not self.database.exists():
            return None
        with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro",uri=True,timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT received_at FROM receipts WHERE symbol=? AND source=? ORDER BY received_at DESC LIMIT 1",(symbol,source)).fetchone()
        return _utc(row[0]) if row else None


def read_response(market_input_root: str | Path, object_path: str | Path) -> dict:
    """Read one envelope/raw payload, verifying both immutable byte identities."""
    root = Path(market_input_root).resolve()
    path = Path(object_path)
    path = (path if path.is_absolute() else root/path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Market input path escapes root")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != path.stem:
        raise ValueError("Market input envelope hash mismatch")
    envelope = json.loads(raw)
    ref = envelope["payload_ref"]
    payload_path = (root/ref["path"]).resolve()
    if not payload_path.is_relative_to(root):
        raise ValueError("Market payload path escapes root")
    data = payload_path.read_bytes()
    if len(data)!=ref["bytes"] or hashlib.sha256(data).hexdigest()!=ref["sha256"]:
        raise ValueError("Market payload hash mismatch")
    return envelope | {"payload":json.loads(data)}


def augment_snapshot(snapshot: Mapping[str, Any], store: MarketInputStore, *, symbol: str,
                     inference_at: Any, cutoff: Any, historical=None) -> dict:
    """Append fixed columns to a copy; never rewrite existing frozen forecasts."""
    result = copy.deepcopy(dict(snapshot))
    if any(name.startswith("market_") for name in result["feature_names"]):
        raise ValueError("Market context already attached")
    decisions = [_utc(value) + timedelta(minutes=15) for value in result["reference_at"]]
    if len(result["X"]) != len(decisions) or len(result["inference_X"]) != 1:
        raise ValueError("Market context and price feature rows differ")
    cutoff=_utc(cutoff)
    requested=[*decisions,_utc(inference_at)]
    read_status={"status":"ok"}
    try:
        joined = store.join(symbol, requested, cutoff=cutoff)
    except (OSError,sqlite3.Error,MarketInputReadUnavailable) as exc:
        # Optional context failures must not replace valid price observations or
        # suppress core forecasts. Preserve missingness plus a safe reason;
        # malformed snapshot/code errors are deliberately outside this catch.
        read_status={"status":"unavailable","reason":getattr(exc,"reason","store_read_unavailable"),"error_type":type(exc).__name__}
        joined={"feature_names":list(MODEL_FEATURE_NAMES),
                "rows":[[0.0]*len(MODEL_FEATURE_NAMES) for _ in requested],
                "provenance":[{"decision_at":_iso(min(value,cutoff)),"sources":{},"read_status":dict(read_status)} for value in requested]}
    reconstruction = None
    reconstructed, binding = None, {}
    try:
        if historical is None:
            from .coverage import load_data_policy
            binding = load_data_policy(store.root.parent.parent).get("historical_market_inputs") or {}
            if binding:
                from .historical_market_inputs import HistoricalMarketInputs, VERSION as HISTORY_VERSION
                if binding.get("version") != HISTORY_VERSION:
                    raise ValueError("Unknown historical market input binding")
                path = Path(binding["path"])
                if path.stem != binding["sha256"]:
                    raise ValueError("Historical binding identity mismatch")
                historical = HistoricalMarketInputs(store.root.parent.parent, path)
        if historical is not None:
            reconstructed = historical.join(decisions, cutoff=cutoff, purpose="retrospective_training")
            if reconstructed["feature_names"] != joined["feature_names"] or len(reconstructed["rows"]) != len(decisions):
                raise ValueError("Historical market columns or rows differ")
    except (OSError, ValueError, KeyError) as exc:
        from .historical_market_inputs import VERSION as HISTORY_VERSION
        reconstructed = None
        reconstruction = {"version": HISTORY_VERSION, "requested_binding": binding,
            "read_status": {"status": "unavailable", "error_type": type(exc).__name__},
            "inference_uses_reconstruction": False, "training_rows_added": 0,
            "assessment_kind": "historical_context_unavailable; genuine_price_training_continues"}
    if reconstructed is not None:
        counts = {name: 0 for name in joined["feature_names"] if not name.endswith("__available")}
        for index, (current, old) in enumerate(zip(joined["rows"][:-1], reconstructed["rows"])):
            used = []
            for column in range(0, len(current), 2):
                if current[column+1] == 0 and old[column+1] == 1:
                    current[column:column+2] = old[column:column+2]
                    name = joined["feature_names"][column]
                    counts[name] += 1
                    used.append(name)
            if used:
                joined["provenance"][index]["historical_reconstruction"] = {
                    **reconstructed["provenance"][index], "used_features": used}
        reconstruction = {"version": reconstructed["version"], "artifact": reconstructed["artifact"],
            "receipt_rule": reconstructed["receipt_rule"], "training_nonmissing_rows_by_feature": counts,
            "assessment_kind": "retrospective_reconstruction_not_contemporaneous_replay",
            "inference_uses_reconstruction": False}
    result["X"] = [list(row) + extra for row, extra in zip(result["X"], joined["rows"][:-1])]
    result["inference_X"] = [list(result["inference_X"][0]) + joined["rows"][-1]]
    result["feature_names"] = list(result["feature_names"]) + joined["feature_names"]
    eligible_receipts = [_utc(v["received_at"]) for v in joined["provenance"][-1]["sources"].values() if v["eligible"]]
    if eligible_receipts and "inference" in result:
        old = result["inference"].get("information_available_at")
        result["inference"]["information_available_at"] = _iso(max([*eligible_receipts,*([_utc(old)] if old else [])]))
    support = {name:sum(row[index+1] for row in joined["rows"][:-1]) for index,name in enumerate(joined["feature_names"]) if not name.endswith("__available")}
    result["market_inputs"] = {"version": VERSION, "feature_names": joined["feature_names"], "cutoff": _iso(cutoff),
        "training": joined["provenance"][:-1], "inference": joined["provenance"][-1],
        "missing_encoding": "0 only with available=0; not an observed market value",
        "historical_decision": "reference_open + 15 minutes", "receipt_rule": "received_at <= decision_at <= training_cutoff",
        "ttl_seconds": SOURCE_TTL_SECONDS,"training_nonmissing_rows_by_feature":support,
        "training_rows":len(decisions),"support_is_not_qualification":True,"read_status":read_status}
    if reconstruction:
        result["market_inputs"]["historical_reconstruction"] = reconstruction
    from .data import FEATURE_VERSION
    from .feature_support import VERSION as SUPPORT_VERSION
    price_version=result.get("manifest",{}).get("feature_version") or FEATURE_VERSION
    result["manifest"] = dict(result.get("manifest", {})) | {"market_input_version": VERSION, "market_input_columns": len(MODEL_FEATURE_NAMES),
        "feature_contract":{"price":price_version,"market_inputs":VERSION,"market_support":SUPPORT_VERSION}}
    if reconstruction:
        result["manifest"]["feature_contract"]["historical_market_inputs"] = reconstruction["version"]
    return result


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        if not payload:
            return []
        return _rows(payload.get("data", payload.get("historical", [payload])))
    return []


def _native_observation(row: Any) -> bool:
    """Present native markers must be explicit boolean false before projection."""
    values = dict(row) if isinstance(row, Mapping) else {
        key:getattr(row,key) for key in (*NATIVE_OBSERVATION_MARKERS,"synthetic_constituents") if hasattr(row,key)}
    return _native_metadata(values)


def _bar_features(rows: list[dict], *, timestamp_key: str, receipt: datetime, timezone_name: str = "UTC") -> dict:
    from zoneinfo import ZoneInfo
    valid = []
    for row in rows:
        if not _native_observation(row):
            continue
        try:
            stamp = row[timestamp_key]
            if isinstance(stamp, (int, float)):
                stamp = datetime.fromtimestamp(stamp / 1000, UTC)
            else:
                stamp = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=ZoneInfo(timezone_name))
            # The returned current partial candle is retained raw, not admitted.
            duration = timedelta(minutes=5 if timestamp_key == "date" else 1)
            if stamp + duration > receipt or stamp < receipt - timedelta(minutes=30):
                continue
            values = [_number(row.get(k)) for k in ("open", "high", "low", "close", "volume")]
            if any(v is None for v in values) or min(values[:4]) <= 0 or values[4] < 0:
                continue
            op, high, low, close, volume = values
            if high < max(op, close, low) or low > min(op, close):
                continue
            valid.append((stamp, op, high, low, close, volume))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not valid:
        return {}
    # Duplicate timestamps are latest response revision, not extra volume.
    valid = sorted({r[0]: r for r in valid}.values())
    return {"return": valid[-1][4] / valid[0][1] - 1,
            "range_relative": (max(r[2] for r in valid) - min(r[3] for r in valid)) / valid[0][1],
            "volume": sum(r[5] for r in valid), "observed_bars": len(valid)}


def _quote_features(row: Mapping[str, Any]) -> dict:
    if not _native_observation(row):
        return {}
    bid, ask = _number(row.get("bidPrice")), _number(row.get("askPrice"))
    bs, az = _number(row.get("bidSize")), _number(row.get("askSize"))
    return {"spread_relative": (ask-bid)/((ask+bid)/2) if bid and ask and ask >= bid else None,
            "quote_imbalance": (bs-az)/(bs+az) if bs is not None and az is not None and min(bs,az) >= 0 and bs+az > 0 else None,
            "last": row.get("lastPrice"), "volume": row.get("totalVolume"), "change_percent": row.get("netPercentChange")}


def _provider_quote_time(source, payload):
    row = payload if source == "schwab_quote" else (_rows(payload) or [{}])[0]
    keys = ("quoteTime","quoteTimeInLong","tradeTime","tradeTimeInLong") if source == "schwab_quote" else ("timestamp",)
    for key in keys:
        value = _number(row.get(key))
        if value is not None and value > 0:
            try:
                return datetime.fromtimestamp(value/(1000 if value>1e11 else 1),UTC)
            except (ValueError,OverflowError,OSError):
                continue
    return None


def _quote_status(source,payload,received):
    row = payload if source == "schwab_quote" else (_rows(payload) or [{}])[0]
    if not _native_observation(payload) or not _native_observation(row):
        return "invalid_native_marker",None
    event = _provider_quote_time(source,payload)
    if event is None:
        return "unverified_event_time",None
    if event > received:
        return "future_event",event
    if received-event > timedelta(seconds=SOURCE_TTL_SECONDS[source]):
        return "stale_event",event
    return "ok",event


def _option_features(payload: Mapping[str, Any], received: datetime | None = None) -> dict:
    if not _native_observation(payload):
        return {}
    calls, puts = [], []
    for key, output in (("callExpDateMap", calls), ("putExpDateMap", puts)):
        for strikes in (payload.get(key) or {}).values():
            for contracts in strikes.values():
                output.extend(x for x in contracts if isinstance(x, dict) and _native_observation(x))
    if received is not None:
        def timely(row):
            status,_ = _quote_status("schwab_quote",row,received)
            return status=="ok"
        calls,puts = [r for r in calls if timely(r)],[r for r in puts if timely(r)]
    contracts = calls + puts
    if not contracts:
        return {}
    def mean(field):
        values = [_number(x.get(field)) for x in contracts]
        values = [x for x in values if x is not None and abs(x) < 1e8 and x != -999]
        if field == "delta":
            values = [x for x in values if -1 <= x <= 1]
        if field in {"gamma","vega","volatility"}:
            values = [x for x in values if x >= 0]
        return sum(values)/len(values) if values else None
    def total(rows, field):
        values = [_number(x.get(field)) for x in rows]
        return sum(x for x in values if x is not None) if any(x is not None for x in values) else None
    spreads = []
    for row in contracts:
        bid, ask = _number(row.get("bid")), _number(row.get("ask"))
        if bid is not None and ask is not None and 0 <= bid <= ask and ask+bid > 0:
            spreads.append((ask-bid)/((ask+bid)/2))
    return {"iv": mean("volatility"), **{x: mean(x) for x in ("delta", "gamma", "theta", "vega")},
            "put_call_volume": _ratio(total(puts,"totalVolume"),total(calls,"totalVolume")),
            "put_call_open_interest": _ratio(total(puts,"openInterest"),total(calls,"openInterest")),
            "spread_relative": sum(spreads)/len(spreads) if spreads else None, "contracts": len(contracts)}


def fmp_specs(symbol: str, now: datetime) -> tuple[tuple[str, str, dict], ...]:
    """Premium endpoints only; each response retains explicit entitlement errors."""
    from zoneinfo import ZoneInfo
    today = _utc(now).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    base = {"symbol": symbol}
    return (
        ("fmp_quote", "quote", base),
        ("fmp_5min", "historical-chart/5min", base | {"from": today, "to": today, "nonadjusted": "true", "extended": "true"}),
        ("fmp_profile", "profile", base),
        ("fmp_ratios", "ratios-ttm", base),
        ("fmp_income", "income-statement", base | {"period":"quarter", "limit":4}),
        ("fmp_balance", "balance-sheet-statement", base | {"period":"quarter", "limit":4}),
        ("fmp_cashflow", "cash-flow-statement", base | {"period":"quarter", "limit":4}),
        ("fmp_earnings", "earnings", base | {"limit":12}),
        ("fmp_dividends", "dividends", base | {"limit":12}),
        ("fmp_splits", "splits", base | {"limit":12}),
        ("fmp_analysts", "price-target-consensus", base),
        ("fmp_news", "news/stock", {"symbols":symbol,"limit":20}),
    )


def _fmp_features(source: str, payload: Any, now: datetime) -> dict:
    if not _native_observation(payload):
        return {}
    rows = [row for row in _rows(payload) if _native_observation(row)]
    if not rows:
        return {}
    row = rows[0]
    mappings = {
        "fmp_quote": {"price":"price","volume":"volume","change_percent":"changePercentage","market_cap":"marketCap"},
        "fmp_profile": {"beta":"beta","market_cap":"marketCap","dividend":"lastDividend","average_volume":"averageVolume"},
        "fmp_ratios": {"pe":"priceToEarningsRatioTTM","pb":"priceToBookRatioTTM","debt_equity":"debtToEquityRatioTTM","current_ratio":"currentRatioTTM","roe":"returnOnEquityTTM"},
        "fmp_income": {"revenue":"revenue","net_income":"netIncome","eps":"eps"},
        "fmp_balance": {"cash":"cashAndCashEquivalents","total_debt":"totalDebt","equity":"totalStockholdersEquity"},
        "fmp_cashflow": {"operating_cashflow":"operatingCashFlow","free_cashflow":"freeCashFlow"},
        "fmp_analysts": {"target_consensus":"targetConsensus","analysts":"numberOfAnalysts"},
    }
    if source in mappings:
        return {key: row.get(field) for key, field in mappings[source].items()}
    if source == "fmp_5min":
        return _bar_features(rows,timestamp_key="date",receipt=now,timezone_name="America/New_York")
    if source == "fmp_news":
        return {"items":len(rows)}
    future = []
    for row in rows:
        try:
            day = datetime.fromisoformat(str(row["date"])[:10]).date()
            future.append(((day-now.date()).days,row))
        except (ValueError,KeyError):
            continue
    if not future:
        return {}
    upcoming = [x for x in future if x[0] >= 0]
    days, row = min(upcoming or future, key=lambda x:abs(x[0]))
    return {"days_to_event":days,"eps_actual":row.get("epsActual"),"eps_estimated":row.get("epsEstimated"),
            "dividend":row.get("dividend"),"ratio":_ratio(row.get("numerator"),row.get("denominator"))}


def _requested_identity(source, payload, symbol):
    """Check supplied provider identity before projection; absent is request-bound."""
    if symbol == "*":
        return True
    if isinstance(payload, Mapping) and payload.get("symbol", symbol) != symbol:
        return False
    if source == "schwab_options":
        if not isinstance(payload, Mapping) or payload.get("underlyingSymbol", symbol) != symbol:
            return False
        for name in ("callExpDateMap", "putExpDateMap"):
            for strikes in (payload.get(name) or {}).values():
                for contracts in strikes.values():
                    for row in contracts:
                        if row.get("underlyingSymbol", symbol) != symbol:
                            return False
                        if "symbol" in row and not re.fullmatch(re.escape(symbol)+r"\s*\d{6}[CP]\d{8}", str(row["symbol"])):
                            return False
        return True
    rows = payload.get("candles", []) if source == "schwab_minutes" and isinstance(payload, Mapping) else _rows(payload)
    return all(row.get("symbol", symbol) == symbol for row in rows)


class MarketInputCollector:
    """One explicit owner: bounded REST snapshots and three owned live feeds.

    ``collect`` is blocking and intended for the existing acquisition worker.
    Separate provider failures are recorded and do not suppress other providers.
    No account/order endpoint is used. Configuration never requests live XNAS
    depth, FMP one-minute bars, or an all-symbol OPRA subscription.
    """
    def __init__(self, datastore_root: str | Path, symbols: Sequence[str], *,
                 clock: Callable[[], datetime] | None = None,
                 schwab: Any = None, fmp: Any = None, streams: Any = None,
                 schwab_stream: Any = None,
                 futures: Sequence[str] | None = None, request_budget_seconds: float = 120):
        self.store = MarketInputStore(datastore_root)
        self.symbols = _symbols(symbols)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.schwab, self.fmp, self.streams = schwab, fmp, streams
        self.schwab_stream = schwab_stream
        from app.services.databento_cme_context import _configured_symbols
        configured = _configured_symbols(os.getenv("DATABENTO_CME_CONTEXT_SYMBOLS", ""))
        self.futures = tuple(futures or configured or DEFAULT_FUTURES)
        self._collect_lock = threading.Lock()
        self._fmp_last_request = 0.0
        self._closed = False
        self._recovery_stop = threading.Event()
        self._retry_lock = threading.RLock()
        from .provider_recovery import RetryLedger
        self._retry_ledger = RetryLedger(self.store.root/"provider-retry-state.json", clock=self.clock)
        self._retry_jobs_path = self.store.root/"provider-retry-jobs.json"
        self._retry_jobs, self._retry_operations = {}, {}
        if self._retry_jobs_path.exists():
            try:
                loaded = json.loads(self._retry_jobs_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and len(loaded) <= 2000:
                    self._retry_jobs = loaded
            except (OSError,ValueError,TypeError):
                pass
        if not 0 < request_budget_seconds <= 120:
            raise ValueError("Request launch budget must be between 0 and 120 seconds")
        self.request_budget_seconds = float(request_budget_seconds)
        self._deadline = 0.0
        self._deferred = []
        self._symbol_cursor = 0
        self._corporate_cursor = 0
        self._fmp_cursor = 0
        self._technical_cursor = 0
        self._pending_macro = {}
        self._pending_macro_parent = None
        self._pending_macro_depth = 0
        self._state_path = self.store.root/"collector-state.json"
        if self._state_path.exists():
            try:
                state=json.loads(self._state_path.read_text())
                if state.get("symbols")==list(self.symbols):
                    self._symbol_cursor=int(state.get("schwab_cursor",0))%(2*len(self.symbols))
                    self._fmp_cursor=int(state.get("fmp_fast_cursor",state.get("fmp_cursor",0)))%(2*len(self.symbols))
                    self._corporate_cursor=max(0,int(state.get("fmp_corporate_cursor",0)))
                    self._technical_cursor=max(0,int(state.get("fmp_technical_cursor",0)))
                    pending=state.get("pending_macro_fallback")
                    if isinstance(pending,dict) and pending.get("name")=="GDP" and set(pending)=={"name","from","to"}:
                        start=datetime.fromisoformat(pending["from"])
                        end=datetime.fromisoformat(pending["to"])
                        if (end-start).days==89:
                            self._pending_macro=dict(pending)
                            self._pending_macro_depth=min(2,max(1,int(state.get("pending_macro_depth",1))))
                            parent=state.get("pending_macro_empty_receipt_id")
                            if isinstance(parent,str) and re.fullmatch(r"[a-f0-9]{64}",parent):
                                self._pending_macro_parent=parent
            except (OSError,ValueError,TypeError):
                pass

    def start(self) -> dict:
        if self._closed:
            raise RuntimeError("Market collector is closed")
        if self.streams is None:
            from .market_input_stream import DatabentoMarketStreams
            self.streams = DatabentoMarketStreams(self.store.root, self.symbols, self.futures, clock=self.clock)
        result = {}
        for name, stream in (("databento", self.streams), ("schwab", self.schwab_stream)):
            try:
                if name == "schwab" and stream is None:
                    from .market_input_schwab_stream import SchwabMarketStream
                    # Schwab's active-root aliases are distinct from Databento
                    # continuous selectors. Stream records retain native IDs.
                    native_futures = []
                    for value in self.futures:
                        match = re.fullmatch(r"(ES|NQ|RTY|CL|GC|ZN|6E)\.[cv]\.\d+", value)
                        if match:
                            native_futures.append("/" + match[1])
                        elif re.fullmatch(r"/(ES|NQ|RTY|CL|GC|ZN|6E)(?:[FGHJKMNQUVXZ]\d{1,4})?", value):
                            native_futures.append(value)
                    stream = self.schwab_stream = SchwabMarketStream(
                        self.store.root, self.symbols, futures=native_futures, clock=self.clock)
                result[name] = stream.start()
            except Exception as exc:
                result[name] = {"status": "unavailable", "error_type": type(exc).__name__}
        return result

    def close(self) -> None:
        self._closed = True
        self._recovery_stop.set()
        for stream in (self.streams, self.schwab_stream):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception as exc:
                # Optional capture must never derail the manual stop path.
                self.close_error_type = type(exc).__name__

    def _save_retry_jobs(self):
        with self._retry_lock:
            self._retry_jobs_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._retry_jobs_path.with_suffix(".tmp")
            with temporary.open("wb") as handle:
                handle.write(_bytes(self._retry_jobs)); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary,self._retry_jobs_path)

    def _fmp_request(self, endpoint, params):
        from .provider_recovery import fmp_request
        from app.services.fmp_corporate_data import FmpCorporateDataProvider
        if self.fmp is None:
            self.fmp = FmpCorporateDataProvider(timeout_seconds=10)
        if isinstance(self.fmp,FmpCorporateDataProvider):
            return fmp_request(self.fmp,endpoint,params)
        return self.fmp._get_json(endpoint,params)  # Injected offline adapter.

    def _rebuild_retry(self, job):
        symbol,source,request = job["symbol"],job["source"],job["request"]
        if symbol not in (*self.symbols,"*"):
            raise ValueError("Retry symbol is not configured")
        if source.startswith("fmp_"):
            params,endpoint = request["params"],request["endpoint"]
            allowed = {entry[1] for entry in fmp_specs(self.symbols[0],_utc(self.clock()))}
            allowed.update(entry[1] for entry in research_specs(self.symbols[0],_utc(self.clock()),include_global=True))
            if endpoint not in allowed:
                raise ValueError("Retry endpoint is outside configured market-data sources")
            operation = lambda:self._fmp_request(endpoint,params)
            extractor = (lambda p,n:extract_context(source,p,n,symbol=symbol,request_params=params)) if job["context"] else (lambda p,n:_fmp_features(source,p,n))
        else:
            if self.schwab is None:
                from datafetching.schwab_fetch import DataFetchingSchwabSession
                self.schwab = DataFetchingSchwabSession()
            if source == "schwab_minutes":
                operation = lambda:self.schwab.get_price_history(symbol,period_type="day",period=None,frequency_type="minute",frequency=1,need_extended_hours_data=True,start_datetime=_utc(request["start"]),end_datetime=_utc(request["end"]))
                extractor = lambda p,n:_bar_features(p.get("candles",[]),timestamp_key="datetime",receipt=n) if _native_observation(p) else {}
            elif source == "schwab_options":
                operation = lambda:self.schwab.get_option_chain_snapshot(symbol,as_of=_utc(self.clock()),strike_count=request["strike_count"],horizon_days=request["horizon_days"])
                extractor = lambda p,n:_option_features(p,n)
            elif source == "schwab_quote" and symbol=="*":
                operation,extractor=self._schwab_quote_payload,lambda p,n:{}
            else:
                raise ValueError("Unsupported market-data retry source")
        return operation,extractor

    def recover_pending(self, *, budget_seconds=20, max_jobs=8) -> dict | None:
        """Called by the same acquisition worker, never by forecast inference.

        Retry due temporary failures independently of ordinary source TTL. Each
        pass is bounded; pending descriptors survive manual shutdown/restart.
        No stream or new background owner is started here.
        """
        if not 0 < budget_seconds <= 120 or not 1 <= max_jobs <= 100:
            raise ValueError("Explicit bounded recovery budget required")
        if self._closed or not self._collect_lock.acquire(blocking=False):
            return None
        records,errors = [],[]
        previous_deadline = self._deadline
        try:
            self._deadline = time.monotonic()+budget_seconds
            due=[key for key in self._retry_ledger.due_keys() if key in self._retry_jobs][:max_jobs]
            if not due:
                return None
            self.store.capture_producer()
            for key in due:
                if self._closed or time.monotonic() >= self._deadline:
                    break
                job=self._retry_jobs.get(key)
                if not job:
                    continue
                try:
                    operation,extractor=self._retry_operations.get(key) or self._rebuild_retry(job)
                    receipt=self._capture(job["symbol"],job["source"],job["request"],operation,extractor,context=job["context"])
                    records.append(receipt)
                    if job["source"]=="schwab_quote" and job["symbol"]=="*":
                        records.extend(self._retain_schwab_quotes(receipt))
                    elif job["symbol"]=="*":
                        original=read_response(self.store.root,receipt["object_path"])
                        for equity in self.symbols:
                            records.append(self.store.append(symbol=equity,source=receipt["source"],started_at=receipt["request_started_at"],received_at=receipt["received_at"],payload=original["payload"],features=receipt["features"],status=receipt["status"],request={**receipt["request"],"global_receipt_id":receipt["id"]},event_at=receipt.get("event_at")))
                except Exception as exc:
                    from .provider_recovery import safe_failure
                    errors.append({"key":key,**safe_failure(exc,self.clock())})
            if not records and not errors:
                return None
            return {"status":"completed","receipts":records,"errors":errors,
                    "pending_jobs":len(self._retry_jobs),"completed_at":_iso(self.clock())}
        finally:
            self._deadline=previous_deadline
            self._collect_lock.release()

    def _capture(self, symbol, source, request, operation, extractor, *, context=False,
                 response_receipt=None, _attempt=None) -> dict:
        if response_receipt is None and _attempt is None:
            # Stable key: retries do not create a new job on each clock tick.
            key=source+":"+symbol
            with self._retry_lock:
                previous=self._retry_jobs.get(key,{})
                self._retry_jobs[key]={**previous,"symbol":symbol,"source":source,"request":request,"context":context}
                self._retry_operations[key]=(operation,extractor)
                self._save_retry_jobs()
            retained=[]
            def retain(event):
                retained.append(self._capture(symbol,source,request,operation,extractor,context=context,_attempt=event))
                with self._retry_lock:
                    self._retry_jobs[key]["last_receipt"]=retained[-1]
                    self._save_retry_jobs()
            # One request per pass; the worker revisits pending jobs after>=2s.
            result=self._retry_ledger.run(key,operation,retain,stop_event=self._recovery_stop,max_attempts=1,
                deadline=_utc(self.clock())+timedelta(seconds=max(0,self._deadline-time.monotonic())) if self._deadline else None,
                secrets=(getattr(self.fmp,"api_key",None),))
            if result["status"]=="ok":
                with self._retry_lock:
                    self._retry_jobs.pop(key,None);self._retry_operations.pop(key,None);self._save_retry_jobs()
            if retained:
                return retained[-1]
            previous=self._retry_jobs.get(key,{}).get("last_receipt")
            if previous:
                return previous
            # No request was made (closed/not-yet-due/permanent rejection).
            return self.store.append(symbol=symbol,source=source,started_at=self.clock(),received_at=self.clock(),payload={"recovery_status":result["status"],"error":result.get("error")},features={},status="unavailable",request={**request,"request_performed":False})
        started = self.clock()
        event_at = None
        try:
            if _attempt is not None:
                started,received=_utc(_attempt["started_at"]),_utc(_attempt["received_at"])
                request={**request,"attempt_number":_attempt["attempt_number"]}
                if _attempt["status"]!="ok":
                    return self.store.append(symbol=symbol,source=source,started_at=started,received_at=received,
                        payload={"error":_attempt["error"]},features={},status="unavailable",request=request)
                payload=_attempt["payload"]
            elif response_receipt is None:
                payload = operation()
                received = self.clock()
            else:
                original = read_response(self.store.root, response_receipt["object_path"])
                payload = original["payload"]
                started, received = _utc(original["request_started_at"]), _utc(original["received_at"])
                request = {**request, "shared_response_receipt_id": response_receipt["id"]}
                if original["status"] == "unavailable":
                    return self.store.append(symbol=symbol,source=source,started_at=started,
                        received_at=received,payload=payload,features={},status="unavailable",request=request)
        except Exception as exc:
            # Exceptions may embed URLs with API keys or OAuth payloads.
            from .provider_recovery import safe_failure
            received, payload, features, status = self.clock(), {"error":safe_failure(exc,self.clock(),secrets=(getattr(self.fmp,"api_key",None),))}, {}, "unavailable"
        else:
            # Persist the original market response even when extraction fails.
            # Neither an exception string nor a later derivation replaces it.
            self.store.persist_raw(payload, received)
            try:
                if not _requested_identity(source, payload, symbol):
                    request = {**request, "returned_identity_mismatch": True}
                    raise ValueError("Returned market identity differs from requested scope")
                result = extractor(copy.deepcopy(payload), _utc(received))
                if context:
                    features, status = result["features"], result["status"]
                    event_at = result.get("event_at")
                    request = {**request, "extraction_evidence": result.get("evidence", {})}
                else:
                    features = result
                    status = "ok" if any(_number(x) is not None for x in features.values()) else "empty"
                    if source == "fmp_quote" and status == "ok":
                        status,event_at = _quote_status(source,payload,_utc(received))
            except Exception as exc:
                features, status = {}, "extraction_unavailable"
                request = {**request, "extraction_error_type": type(exc).__name__}
        return self.store.append(symbol=symbol,source=source,started_at=started,received_at=received,payload=payload,features=features,status=status,request=request,event_at=event_at)

    def _may_request(self, symbol, source) -> bool:
        if self._closed or time.monotonic() >= self._deadline:
            self._deferred.append({"symbol":symbol,"source":source,"reason":"cycle_request_budget"})
            return False
        return True

    def _schwab_quote_payload(self):
        from datafetching.schwab_fetch import DataFetchingSchwabSession
        if not isinstance(self.schwab,DataFetchingSchwabSession):
            return self.schwab.get_equity_quotes(self.symbols)
        import requests
        from app.services.schwab import MARKETDATA_BASE_URL
        if self._closed:
            raise InterruptedError("Market-data owner stopped")
        response=requests.get(MARKETDATA_BASE_URL+"/quotes",headers=self.schwab._headers(),
            params={"symbols":",".join(self.symbols),"fields":"quote"},timeout=10)
        response.raise_for_status()
        return response.json()

    def _retain_schwab_quotes(self,receipt):
        original=read_response(self.store.root,receipt["object_path"])
        payload,received=original["payload"],_utc(receipt["received_at"])
        rows=[]
        for symbol in self.symbols:
            raw=payload.get(symbol,{}) if isinstance(payload,Mapping) else {}
            quote=raw.get("quote",raw) if isinstance(raw,Mapping) else {}
            if isinstance(raw,Mapping) and "quote" in raw:
                quote={**quote,"quote_realtime":raw.get("realtime"),"quote_type":raw.get("quoteType")}
            status,event_at,features="unavailable",None,{}
            if receipt["status"]!="unavailable":
                try:
                    if not isinstance(raw,Mapping) or not isinstance(quote,Mapping):
                        raise TypeError("Malformed native quote record")
                    if not _requested_identity("schwab_quote",raw,symbol) or not _requested_identity("schwab_quote",quote,symbol):
                        raise ValueError("Returned quote identity differs from requested scope")
                    status,event_at=_quote_status("schwab_quote",quote,received) if quote else ("empty",None)
                    features=_quote_features(quote) if _native_observation(raw) else {}
                    trade_time=_number(quote.get("tradeTime",quote.get("tradeTimeInLong")))
                    if trade_time is not None:
                        stamp=datetime.fromtimestamp(trade_time/(1000 if trade_time>1e11 else 1),UTC)
                        if stamp>received or received-stamp>timedelta(minutes=30):features["last"]=None
                except (TypeError,ValueError,OverflowError,OSError):
                    status,features="extraction_unavailable",{}
            rows.append(self.store.append(symbol=symbol,source="schwab_quote",started_at=receipt["request_started_at"],received_at=received,
                payload=raw if receipt["status"]!="unavailable" else payload,features=features,status=status,event_at=event_at,
                request={"endpoint":"quotes","symbols":list(self.symbols),"batch_receipt_id":receipt["id"],
                    "adapter_batch_payload_ref":receipt["payload_ref"],"raw_response_retained":True}))
        return rows

    def _schwab_collect(self) -> list[dict]:
        if self._closed:
            return []
        if self.schwab is None:
            from datafetching.schwab_fetch import DataFetchingSchwabSession
            self.schwab = DataFetchingSchwabSession()
        records = []
        if self._closed:
            return []
        batch=self._capture("*","schwab_quote",{"endpoint":"quotes","symbols":list(self.symbols)},
            self._schwab_quote_payload,lambda p,n:{})
        records.extend(self._retain_schwab_quotes(batch))
        # Rotate after a bounded cycle so slow symbols never starve later ones.
        jobs=[(symbol,source) for source in ("schwab_minutes","schwab_options") for symbol in self.symbols]
        ordered = jobs[self._symbol_cursor:]+jobs[:self._symbol_cursor]
        for index,(symbol,source) in enumerate(ordered):
            now = _utc(self.clock())
            if not self._may_request(symbol,source):
                self._deferred.extend({"symbol":s,"source":src,"reason":"cycle_request_budget"} for s,src in ordered[index+1:])
                break
            if source=="schwab_minutes":
                records.append(self._capture(symbol,source,{"endpoint":"pricehistory","frequency":1,"start":_iso(now-timedelta(minutes=30)),"end":_iso(now)},
                    lambda s=symbol,n=now: self.schwab.get_price_history(s,period_type="day",period=None,frequency_type="minute",frequency=1,need_extended_hours_data=True,start_datetime=n-timedelta(minutes=30),end_datetime=n),
                    lambda p,n:_bar_features(p.get("candles",[]),timestamp_key="datetime",receipt=n) if _native_observation(p) else {}))
            else:
                records.append(self._capture(symbol,source,{"endpoint":"chains","strike_count":40,"horizon_days":45},
                    lambda s=symbol:self.schwab.get_option_chain_snapshot(s,as_of=_utc(self.clock()),strike_count=40,horizon_days=45),lambda p,n:_option_features(p,n)))
            self._symbol_cursor = (self._symbol_cursor+1)%len(jobs)
        return records

    def _fmp_collect(self) -> list[dict]:
        if self.fmp is None:
            from app.services.fmp_corporate_data import FmpCorporateDataProvider
            self.fmp = FmpCorporateDataProvider(timeout_seconds=10)
        now = _utc(self.clock())
        base = {symbol: fmp_specs(symbol, now) for symbol in self.symbols}
        research = [(symbol, *job) for symbol in self.symbols
                    for job in research_specs(symbol, now)]
        global_jobs = [("*", *job) for job in research_specs(self.symbols[0], now, include_global=True)
                       if job[0].startswith("fmp_macro_")]
        fast = [(symbol, *base[symbol][i]) for i in range(2) for symbol in self.symbols]
        technical = [job for job in research if job[1].startswith("fmp_technical_")]
        corporate = [(symbol, *base[symbol][i]) for i in range(2, len(base[self.symbols[0]]))
                     for symbol in self.symbols]
        corporate += [job for job in research if not job[1].startswith("fmp_technical_")]
        corporate += global_jobs
        # Separate rotating lanes prevent large research inventories from starving
        # current prices. Slow sources retain real receipt times, never backdates.
        lanes = (("fast", fast, "_fmp_cursor", .50),
                 ("technical", technical, "_technical_cursor", .25),
                 ("corporate", corporate, "_corporate_cursor", .0))
        records = []
        response_cache = {}

        def retain_global(receipt):
            original = read_response(self.store.root, receipt["object_path"])
            for equity in self.symbols:
                records.append(self.store.append(symbol=equity, source=receipt["source"],
                    started_at=receipt["request_started_at"], received_at=receipt["received_at"],
                    payload=original["payload"], features=receipt["features"], status=receipt["status"],
                    request={**receipt["request"], "global_receipt_id": receipt["id"]},
                    event_at=receipt.get("event_at")))
            return original["payload"]

        def pending_macro():
            if not self._pending_macro or not self._may_request("*","fmp_macro_gdp"):
                return
            params=dict(self._pending_macro)
            delay=.1-(time.monotonic()-self._fmp_last_request)
            if delay>0:time.sleep(delay)
            self._fmp_last_request=time.monotonic()
            receipt=self._capture("*","fmp_macro_gdp",{"endpoint":"economic-indicators","params":params,
                "recovery":"one preceding90-day slice after actual empty latest-period response",
                "empty_receipt_id":self._pending_macro_parent},
                lambda:self._fmp_request("economic-indicators",params),
                lambda payload,stamp:extract_context("fmp_macro_gdp",payload,stamp,symbol="*",request_params=params),context=True)
            records.append(receipt);payload=retain_global(receipt)
            self._pending_macro={}
            self._pending_macro_parent=None
            from .market_input_research import GDP_LOOKBACK_WINDOWS
            if self._pending_macro_depth < GDP_LOOKBACK_WINDOWS-1:
                fallback=empty_macro_fallback_spec("fmp_macro_gdp",params,payload)
                if fallback is not None:
                    self._pending_macro=dict(fallback[2]);self._pending_macro_parent=receipt["id"]
                    self._pending_macro_depth+=1
                    pending_macro()

        for lane, scheduled, cursor_name, reserved_fraction in lanes:
            if lane=="corporate":
                pending_macro()
            if not scheduled:
                continue
            cursor = getattr(self, cursor_name) % len(scheduled)
            ordered = scheduled[cursor:] + scheduled[:cursor]
            lane_deadline = self._deadline - self.request_budget_seconds * reserved_fraction
            for index, (symbol, source, endpoint, params) in enumerate(ordered):
                recent = self.store.latest_receipt(symbol, source)
                refresh = RESEARCH_REFRESH_SECONDS.get(source, 900 if source == "fmp_news" else 21600)
                pending = source+":"+symbol in self._retry_jobs
                if lane != "fast" and not pending and recent and (_utc(self.clock()) - recent).total_seconds() < refresh:
                    setattr(self, cursor_name, (getattr(self, cursor_name) + 1) % len(scheduled))
                    continue
                if time.monotonic() >= lane_deadline:
                    self._deferred.extend({"symbol": sym, "source": src, "reason": lane + "_lane_budget"}
                                          for sym, src, _, _ in ordered[index:])
                    break
                if not self._may_request(symbol, source):
                    self._deferred.extend({"symbol": sym, "source": src, "reason": "cycle_request_budget"}
                                          for sym, src, _, _ in ordered[index + 1:])
                    break
                delay = .1 - (time.monotonic() - self._fmp_last_request)
                if delay > 0:
                    time.sleep(delay)
                self._fmp_last_request = time.monotonic()
                is_research = source in RESEARCH_FIELDS
                extractor = (lambda payload, stamp, src=source, sym=symbol, par=params:
                             extract_context(src, payload, stamp, symbol=sym, request_params=par)) if is_research else (
                             lambda payload, stamp, src=source: _fmp_features(src, payload, stamp))
                response_key = (endpoint, json.dumps(params, sort_keys=True))
                receipt = self._capture(symbol, source, {"endpoint": endpoint, "params": params},
                    lambda e=endpoint, p=params: self._fmp_request(e, p), extractor, context=is_research,
                    response_receipt=response_cache.get(response_key))
                response_cache[response_key] = receipt
                records.append(receipt)
                if symbol == "*":
                    # One actual global request; all equities reference those same
                    # raw bytes and clocks. Never fetch the macro series per equity.
                    payload=retain_global(receipt)
                    fallback=empty_macro_fallback_spec(source,params,payload)
                    if fallback is not None:
                        self._pending_macro=dict(fallback[2])
                        self._pending_macro_parent=receipt["id"]
                        self._pending_macro_depth=1
                        pending_macro()
                setattr(self, cursor_name, (getattr(self, cursor_name) + 1) % len(scheduled))
        return records

    def collect(self, reference_at: Any) -> dict:
        if self._closed:
            raise RuntimeError("Market collector is closed")
        with self._collect_lock:
            self.store.capture_producer()
            started = self.clock()
            self._deadline = time.monotonic()+self.request_budget_seconds
            self._deferred = []
            records, errors = [], []
            # Only two bounded workers; each provider preserves its request order.
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix="intraday-input") as pool:
                futures = [pool.submit(self._schwab_collect),pool.submit(self._fmp_collect)]
                for task in futures:
                    try:
                        records.extend(task.result())
                    except Exception as exc:
                        errors.append(type(exc).__name__)
            for stream in (self.streams, self.schwab_stream):
                if stream is None:
                    continue
                try:
                    items = stream.snapshot()
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    continue
                for item in items:
                    source, payload = item["source"], item["payload"]
                    targets = ([item["symbol"]] if item.get("symbol") in self.symbols else
                               list(self.symbols) if item.get("symbol") is None else [])
                    for symbol in targets:
                        records.append(self.store.append(symbol=symbol,source=source,started_at=item["started_at"],received_at=item["received_at"],payload=payload,features=item["features"],status=item["status"],request=item.get("request"),event_at=item.get("event_at")))
            manifest = {"version":VERSION,"reference_at":_iso(reference_at),"started_at":_iso(started),"completed_at":_iso(self.clock()),
                        "symbols":list(self.symbols),"receipts":[{k:r[k] for k in ("id","symbol","source","received_at","status","object_path")} for r in records],"errors":errors,
                        "request_launch_budget_seconds":self.request_budget_seconds,"fmp_fast_lane_budget_seconds":self.request_budget_seconds*.50,"fmp_technical_lane_deadline_seconds":self.request_budget_seconds*.75,"deferred":self._deferred,"refresh_seconds":{"quotes_bars_options":900,"corporate":21600,"news":900,"research_by_source":RESEARCH_REFRESH_SECONDS},
                        "scope":"Configured equities; bounded REST chains plus continuously recorded subscribed Schwab streams and full returned identity chains; configured futures roots; finite FMP research/technical/macro endpoints. Raw omitted contracts/pages remain explicit; not a claim of all provider products or market-wide coverage"}
            from ml.market_provenance import capture_runtime
            manifest["runtime_provenance"] = capture_runtime([
                __name__, "ml.intraday.market_input_research", "ml.intraday.market_input_schwab_stream",
                "ml.intraday.market_input_stream", "ml.intraday.provider_recovery"], object_root=self.store.root / "source-objects")
            raw = _bytes(manifest)
            target = self.store.root / "cycles" / (hashlib.sha256(raw).hexdigest()+".json")
            _write_immutable(target, raw)
            state={"symbols":list(self.symbols),"schwab_cursor":self._symbol_cursor,"fmp_fast_cursor":self._fmp_cursor,"fmp_corporate_cursor":self._corporate_cursor,"fmp_technical_cursor":self._technical_cursor,"pending_macro_fallback":self._pending_macro,"pending_macro_depth":self._pending_macro_depth,"pending_macro_empty_receipt_id":self._pending_macro_parent,"completed_at":manifest["completed_at"]}
            state_temp=self._state_path.with_suffix(".tmp")
            state_temp.write_bytes(_bytes(state))
            os.replace(state_temp,self._state_path)
            return manifest | {"manifest_path":str(target)}
