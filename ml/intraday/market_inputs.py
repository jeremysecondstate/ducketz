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
import time
from typing import Any, Callable, Mapping, Sequence

VERSION = "intraday-receipt-market-inputs-v1"
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
FEATURE_NAMES = tuple(f"market_{source}_{field}" for source, fields in SOURCE_FIELDS.items() for field in fields)
MODEL_FEATURE_NAMES = tuple(name for feature in FEATURE_NAMES for name in (feature, feature + "__available"))
SOURCE_TTL_SECONDS = {source: (172800 if source.startswith("fmp_") and source not in {"fmp_quote", "fmp_5min", "fmp_news"} else 1800) for source in SOURCE_FIELDS}
SOURCE_TTL_SECONDS["fmp_news"] = 7200
NATIVE_OBSERVATION_MARKERS = ("synthetic", "is_synthetic", "no_trade", "is_no_trade", "source_empty", "is_source_empty")


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
        payload_raw = _bytes(payload)
        payload_id = hashlib.sha256(payload_raw).hexdigest()
        payload_target = self.root / "raw" / received.strftime("%Y/%m/%d") / (payload_id+".json")
        value = {"version": VERSION, "symbol": symbol, "source": source,
                 "request_started_at": _iso(started), "received_at": _iso(received),
                 "event_at": _iso(event_at) if event_at is not None else None,
                 "status": status, "request": dict(request or {}), "features": numeric,
                 "payload_ref": {"sha256":payload_id,"path":str(payload_target.relative_to(self.root)),"bytes":len(payload_raw)}}
        raw = _bytes(value)
        identity = hashlib.sha256(raw).hexdigest()
        target = self.root / "objects" / received.strftime("%Y/%m/%d") / (identity + ".json")
        with self._lock:
            for path,content in ((payload_target,payload_raw),(target,raw)):
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with path.open("xb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                except FileExistsError:
                    if path.read_bytes() != content:
                        raise RuntimeError("Immutable market input object differs")
            with closing(sqlite3.connect(self.database, timeout=5)) as db:
                db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL, status TEXT NOT NULL, features TEXT NOT NULL, object_path TEXT NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS receipt_symbol_time ON receipts(symbol,received_at)")
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
                     inference_at: Any, cutoff: Any) -> dict:
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
    from .data import FEATURE_VERSION
    from .feature_support import VERSION as SUPPORT_VERSION
    price_version=result.get("manifest",{}).get("feature_version") or FEATURE_VERSION
    result["manifest"] = dict(result.get("manifest", {})) | {"market_input_version": VERSION, "market_input_columns": len(MODEL_FEATURE_NAMES),
        "feature_contract":{"price":price_version,"market_inputs":VERSION,"market_support":SUPPORT_VERSION}}
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
    return all((row.get(key, False) if isinstance(row, Mapping) else getattr(row, key, False)) is False
               for key in NATIVE_OBSERVATION_MARKERS)


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
                 futures: Sequence[str] | None = None, request_budget_seconds: float = 120):
        self.store = MarketInputStore(datastore_root)
        self.symbols = _symbols(symbols)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.schwab, self.fmp, self.streams = schwab, fmp, streams
        from app.services.databento_cme_context import _configured_symbols
        configured = _configured_symbols(os.getenv("DATABENTO_CME_CONTEXT_SYMBOLS", ""))
        self.futures = tuple(futures or configured or DEFAULT_FUTURES)
        self._collect_lock = threading.Lock()
        self._fmp_last_request = 0.0
        self._closed = False
        if not 0 < request_budget_seconds <= 120:
            raise ValueError("Request launch budget must be between 0 and 120 seconds")
        self.request_budget_seconds = float(request_budget_seconds)
        self._deadline = 0.0
        self._deferred = []
        self._symbol_cursor = 0
        self._corporate_cursor = 0
        self._fmp_cursor = 0
        self._state_path = self.store.root/"collector-state.json"
        if self._state_path.exists():
            try:
                state=json.loads(self._state_path.read_text())
                if state.get("symbols")==list(self.symbols):
                    self._symbol_cursor=int(state.get("schwab_cursor",0))%(2*len(self.symbols))
                    self._fmp_cursor=int(state.get("fmp_fast_cursor",state.get("fmp_cursor",0)))%(2*len(self.symbols))
                    self._corporate_cursor=int(state.get("fmp_corporate_cursor",0))%(len(self.symbols)*(len(fmp_specs(self.symbols[0],_utc(self.clock())))-2))
            except (OSError,ValueError,TypeError):
                pass

    def start(self) -> dict:
        if self.streams is None:
            from .market_input_stream import DatabentoMarketStreams
            self.streams = DatabentoMarketStreams(self.store.root, self.symbols, self.futures, clock=self.clock)
        return self.streams.start()

    def close(self) -> None:
        self._closed = True
        if self.streams is not None:
            try:
                self.streams.close()
            except Exception as exc:
                # Optional capture must never derail the manual stop path.
                self.close_error_type = type(exc).__name__

    def _capture(self, symbol, source, request, operation, extractor) -> dict:
        started = self.clock()
        event_at = None
        try:
            payload = operation()
            received = self.clock()
            features = extractor(payload, _utc(received))
            status = "ok" if any(_number(x) is not None for x in features.values()) else "empty"
            if source == "fmp_quote" and status == "ok":
                status,event_at = _quote_status(source,payload,_utc(received))
        except Exception as exc:
            # Exceptions may embed URLs with API keys or OAuth payloads.
            code = getattr(getattr(exc,"response",None),"status_code",None)
            if code is None:
                match = re.search(r"\bHTTP (\d{3})\b",str(exc))
                code = int(match.group(1)) if match else None
            received, payload, features, status = self.clock(), {"error_type": type(exc).__name__,"http_status":code,"category":"entitlement_or_permission" if code in {402,403} else "authentication" if code==401 else "request_unavailable"}, {}, "unavailable"
        return self.store.append(symbol=symbol,source=source,started_at=started,received_at=received,payload=payload,features=features,status=status,request=request,event_at=event_at)

    def _may_request(self, symbol, source) -> bool:
        if self._closed or time.monotonic() >= self._deadline:
            self._deferred.append({"symbol":symbol,"source":source,"reason":"cycle_request_budget"})
            return False
        return True

    def _schwab_collect(self) -> list[dict]:
        if self._closed:
            return []
        if self.schwab is None:
            from datafetching.schwab_fetch import DataFetchingSchwabSession
            self.schwab = DataFetchingSchwabSession()
        records = []
        started = self.clock()
        try:
            if self._closed:
                return []
            quotes = self.schwab.get_equity_quotes(self.symbols)
            received = self.clock()
            for symbol in self.symbols:
                quote = quotes.get(symbol, {})
                status,event_at = _quote_status("schwab_quote",quote,_utc(received)) if quote else ("empty",None)
                features = _quote_features(quote)
                trade_time = _number(quote.get("tradeTime",quote.get("tradeTimeInLong")))
                if trade_time is not None:
                    try:
                        trade_time = datetime.fromtimestamp(trade_time/(1000 if trade_time>1e11 else 1),UTC)
                        if trade_time > _utc(received) or _utc(received)-trade_time > timedelta(minutes=30):
                            features["last"] = None
                    except (ValueError,OverflowError,OSError):
                        features["last"] = None
                records.append(self.store.append(symbol=symbol,source="schwab_quote",started_at=started,received_at=received,payload=quote,features=features,status=status,event_at=event_at,request={"endpoint":"quotes","symbols":list(self.symbols)}))
        except Exception as exc:
            for symbol in self.symbols:
                records.append(self.store.append(symbol=symbol,source="schwab_quote",started_at=started,received_at=self.clock(),payload={"error_type":type(exc).__name__},features={},status="unavailable"))
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
        records = []
        by_symbol = {}
        for symbol in self.symbols:
            by_symbol[symbol]=fmp_specs(symbol,_utc(self.clock()))
        # Refresh quotes/bars every collection independently of the corporate
        # cursor. Reserve one quarter of the budget for slower corporate work.
        fast=[(symbol,*by_symbol[symbol][i]) for i in range(2) for symbol in self.symbols]
        corporate=[(symbol,*by_symbol[symbol][i]) for i in range(2,len(next(iter(by_symbol.values())))) for symbol in self.symbols]
        fast_deadline=self._deadline-self.request_budget_seconds*.25
        for lane,scheduled,cursor in (("fast",fast,self._fmp_cursor),("corporate",corporate,self._corporate_cursor)):
            ordered=scheduled[cursor:]+scheduled[:cursor]
            for index,(symbol,source,endpoint,params) in enumerate(ordered):
                recent = self.store.latest_receipt(symbol,source)
                refresh = 900 if source == "fmp_news" else 21600
                if lane=="corporate" and recent and (_utc(self.clock())-recent).total_seconds() < refresh:
                    self._corporate_cursor=(self._corporate_cursor+1)%len(scheduled)
                    continue
                if lane=="fast" and time.monotonic()>=fast_deadline:
                    self._deferred.extend({"symbol":s,"source":src,"reason":"fast_lane_budget"} for s,src,_,_ in ordered[index:])
                    break
                if not self._may_request(symbol,source):
                    self._deferred.extend({"symbol":s,"source":src,"reason":"cycle_request_budget"} for s,src,_,_ in ordered[index+1:])
                    break
                # <=600 requests/minute, below the stated Premium 750/minute.
                delay = 0.1 - (time.monotonic()-self._fmp_last_request)
                if delay > 0:
                    time.sleep(delay)
                self._fmp_last_request = time.monotonic()
                records.append(self._capture(symbol,source,{"endpoint":endpoint,"params":params},
                    lambda e=endpoint,p=params:self.fmp._get_json(e,p),
                    lambda payload,now,s=source:_fmp_features(s,payload,now)))
                if lane=="fast":self._fmp_cursor=(self._fmp_cursor+1)%len(scheduled)
                else:self._corporate_cursor=(self._corporate_cursor+1)%len(scheduled)
        return records

    def collect(self, reference_at: Any) -> dict:
        if self._closed:
            raise RuntimeError("Market collector is closed")
        with self._collect_lock:
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
            if self.streams is not None:
                for item in self.streams.snapshot():
                    source, payload = item["source"], item["payload"]
                    targets = [item["symbol"]] if item.get("symbol") in self.symbols else list(self.symbols)
                    for symbol in targets:
                        records.append(self.store.append(symbol=symbol,source=source,started_at=item["started_at"],received_at=item["received_at"],payload=payload,features=item["features"],status=item["status"],request=item.get("request")))
            manifest = {"version":VERSION,"reference_at":_iso(reference_at),"started_at":_iso(started),"completed_at":_iso(self.clock()),
                        "symbols":list(self.symbols),"receipts":[{k:r[k] for k in ("id","symbol","source","received_at","status","object_path")} for r in records],"errors":errors,
                        "request_launch_budget_seconds":self.request_budget_seconds,"fmp_fast_lane_budget_seconds":self.request_budget_seconds*.75,"deferred":self._deferred,"refresh_seconds":{"quotes_bars_options":900,"corporate":21600,"news":900},
                        "scope":"Configured equities, 40 strikes/45-day Schwab chain, configured CME contexts; no claim every provider endpoint or contract is ingested"}
            raw = _bytes(manifest)
            target = self.store.root / "cycles" / (hashlib.sha256(raw).hexdigest()+".json")
            target.parent.mkdir(parents=True,exist_ok=True)
            try:
                with target.open("xb") as out:
                    out.write(raw)
            except FileExistsError:
                pass
            state={"symbols":list(self.symbols),"schwab_cursor":self._symbol_cursor,"fmp_fast_cursor":self._fmp_cursor,"fmp_corporate_cursor":self._corporate_cursor,"completed_at":manifest["completed_at"]}
            state_temp=self._state_path.with_suffix(".tmp")
            state_temp.write_bytes(_bytes(state))
            os.replace(state_temp,self._state_path)
            return manifest | {"manifest_path":str(target)}
