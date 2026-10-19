"""Bounded real-price recovery and retained cross-provider gap evidence.

Network clients are constructed only by recover(). Native one/five-minute bars
remain native; this module neither edits canonical bars nor expands coarse bars.
The caller chooses its explicitly versioned recovered-population policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from ml.market_gap_evidence import assess_interval_gap, _time, _native

VERSION = "native-provider-gap-recovery-v1"


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _retain(root, folder, value):
    raw = _bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / folder / (digest + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending-" + uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise RuntimeError("Immutable recovery evidence differs")
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(path), "sha256": digest}


def _valid_row(row, timestamp, seconds, received):
    stamp = _time(timestamp)
    values = [row.get(k) for k in ("open", "high", "low", "close", "volume")]
    if (not _native(row) or any(isinstance(v, bool) for v in values)
            or any(not math.isfinite(float(v)) for v in values)):
        raise ValueError("Non-native or invalid source bar")
    op, hi, lo, cl, vol = map(float, values)
    if min(op, hi, lo, cl) <= 0 or hi < max(op, cl, lo) or lo > min(op, cl, hi) or vol < 0:
        raise ValueError("Invalid native OHLCV")
    if stamp.timestamp() % seconds:
        raise ValueError("Unaligned native source interval")
    # An in-progress provider bar is retained raw but cannot support a candle.
    if stamp + timedelta(seconds=seconds) > received:
        return None
    return {"timestamp": stamp.isoformat(), "open": op, "high": hi,
            "low": lo, "close": cl, "volume": vol}


def _native_identity(value, symbol):
    # Validate before projection: dropped metadata must never turn a marked or
    # foreign-symbol response into a genuine observation for the request.
    if not _native(value):
        raise ValueError("Non-native or malformed source record")
    if "symbol" in value and value["symbol"] != symbol:
        raise ValueError("Native response belongs to another symbol")


def _parse(provider, payload, received, *, symbol):
    seconds = 300 if provider == "fmp" else 60
    if provider == "schwab":
        if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
            raise ValueError("Schwab response has no native candle array")
        _native_identity(payload, symbol)
        rows = payload["candles"]
    elif provider == "fmp":
        if not isinstance(payload, list):
            raise ValueError("FMP response is not a native chart array")
        rows = payload
    else:
        rows = payload
    if not isinstance(rows, (list, tuple)) or len(rows) > 2000:
        raise ValueError("Gap response exceeds bounded row scope")
    result = []
    for row in rows:
        _native_identity(row, symbol)
        if provider == "schwab":
            stamp = datetime.fromtimestamp(float(row["datetime"]) / 1000, timezone.utc)
        elif provider == "fmp":
            raw = str(row["date"])
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw):
                raise ValueError("Unknown FMP chart timezone convention")
            stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
        else:
            stamp = row["opened_at"]
        parsed = _valid_row(row, stamp, seconds, received)
        if parsed is not None:
            result.append(parsed)
    return result


def _context(rows, start, end, seconds, received):
    # Genuine positive-volume observations, not zero-volume placeholders, anchor
    # the bracketing prices. Their actual receipt is part of every proof.
    native = [r for r in rows if r["volume"] > 0]
    before = [r for r in native if _time(r["timestamp"]) + timedelta(seconds=seconds) <= start]
    after = [r for r in native if _time(r["timestamp"]) >= end]
    result = {}
    if before:
        row = max(before, key=lambda r: r["timestamp"])
        result["before"] = {"timestamp": (_time(row["timestamp"]) + timedelta(seconds=seconds)).isoformat(),
                            "received_at": received.isoformat(), "price": row["close"]}
    if after:
        row = min(after, key=lambda r: r["timestamp"])
        result["after"] = {"timestamp": row["timestamp"], "received_at": received.isoformat(), "price": row["open"]}
    return result


class _Stop:
    def __init__(self, stopped):
        self.stopped = stopped

    def is_set(self):
        return bool(self.stopped())

    def wait(self, seconds):
        import time
        due = time.monotonic() + seconds
        while not self.is_set() and time.monotonic() < due:
            time.sleep(max(0, min(.1, due - time.monotonic())))
        return self.is_set()


class GapRecovery:
    def __init__(self, root, *, clock=None, stopped=None, schwab=None, fmp=None, retry_ledger=None):
        self.datastore_root = Path(root).resolve()
        self.root = self.datastore_root / "intraday/gap-recovery"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stopped = stopped or (lambda: False)
        self.schwab, self.fmp = schwab, fmp
        self.ledger = retry_ledger

    def _request(self, provider, symbol, start, end):
        from .provider_recovery import RetryLedger, request_key, fmp_request
        if self.ledger is None:
            self.ledger = RetryLedger(self.root / "retry-ledger.json", clock=self.clock)
        request_start, request_end = start - timedelta(minutes=5), min(end + timedelta(minutes=5), _time(self.clock()))
        if provider == "schwab":
            if self.schwab is None:
                from datafetching.schwab_fetch import DataFetchingSchwabSession
                self.schwab = DataFetchingSchwabSession()
            params = {"symbol": symbol, "start": request_start.isoformat(), "end": request_end.isoformat(),
                      "frequency_type": "minute", "frequency": 1, "extended": True}
            request = lambda: self.schwab.get_price_history(symbol, period_type="day", period=None,
                frequency_type="minute", frequency=1, need_extended_hours_data=True,
                start_datetime=request_start, end_datetime=request_end)
            secrets = ()
        else:
            if self.fmp is None:
                from app.services.fmp_corporate_data import FmpCorporateDataProvider
                self.fmp = FmpCorporateDataProvider(timeout_seconds=15)
            day = start.astimezone(ZoneInfo("America/New_York")).date()
            params = {"symbol": symbol, "from": day.isoformat(), "to": day.isoformat(),
                      "nonadjusted": "true", "extended": "true"}
            request_start = datetime.combine(day, datetime.min.time(), ZoneInfo("America/New_York")).astimezone(timezone.utc)
            request_end = request_start + timedelta(days=1)
            request = lambda: fmp_request(self.fmp, "historical-chart/5min", params)
            secrets = (getattr(self.fmp, "api_key", ""),)
        key = request_key(provider, symbol, "native-gap", params)
        reference = []
        def retain(event):
            # Only allowlisted price payloads reach durable evidence. Provider
            # errors have already been sanitized by the shared retry adapter.
            if event["status"] == "ok":
                encoded = _bytes(event["payload"])
                if any(secret and str(secret).encode() in encoded for secret in secrets):
                    raise ValueError("Credential-bearing native response rejected")
            reference.append(_retain(self.root, "responses", {"version": VERSION, "provider": provider,
                "symbol": symbol, "request": params, **event}))
        result = self.ledger.run(key, request, retain, stop_event=_Stop(self.stopped), max_attempts=2,
                                 deadline=_time(self.clock()) + timedelta(seconds=30), secrets=secrets)
        source, dataset, seconds = (("schwab:pricehistory:1m", "SCHWAB.PRICEHISTORY", 60)
            if provider == "schwab" else ("fmp:historical-chart:5min", "FMP.HISTORICAL_CHART_5MIN", 300))
        if result["status"] != "ok":
            return None, {"provider": provider, "status": result["status"], "next_retry_at": result.get("next_retry_at"),
                          "references": reference, "error": result.get("error")}
        received = _time(result["received_at"])
        identity = reference[-1]
        try:
            rows = _parse(provider, result["payload"], received, symbol=symbol)
            context = _context(rows, start, end, seconds, received)
            rows = [r for r in rows if start <= _time(r["timestamp"]) < end]
        except (ValueError, TypeError, KeyError, OverflowError):
            return None, {"provider": provider, "status": "malformed_native_response", "references": reference}
        item = {"provider": provider, "symbol": symbol, "source": source, "dataset": dataset,
                "receipt_id": identity["sha256"], "payload_sha256": hashlib.sha256(_bytes(result["payload"])).hexdigest(),
                "status": "ok", "response_complete": True, "request_start": request_start.isoformat(),
                "request_end": request_end.isoformat(), "received_at": received.isoformat(),
                "native_interval_seconds": seconds, "records": rows, "price_context": context,
                "coverage_scope": "provider-specific observations; market-wide coverage not asserted"}
        return item, {"provider": provider, "status": "ok", "references": reference}

    def _primary(self, symbol, start, end, primary):
        attempts = [r for r in primary.get("evidence", ()) if isinstance(r, dict)
                    and r.get("symbol") == symbol and r.get("source_id", "").startswith("databento:")
                    and _time(r["interval_open"]) == start and _time(r["interval_close"]) == end
                    and _time(r["request_ended_at"]) <= _time(self.clock())]
        if not attempts:
            return None
        attempt = max(attempts, key=lambda r: _time(r["request_ended_at"]))
        if attempt.get("status") != "success" or not attempt.get("coverage_open") or not attempt.get("coverage_close"):
            return None
        if not _time(attempt["coverage_open"]) <= start < end <= _time(attempt["coverage_close"]):
            return None
        surrounding = []
        database = self.datastore_root / "intraday/acquisition.sqlite3"
        if database.is_file():
            # Exact indexed slots only; no schema mutation or table/history scan.
            from contextlib import closing
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                indexed = any(any(column[2] == "slot" for column in db.execute('PRAGMA index_info("' + index[1].replace('"', '""') + '")'))
                              for index in db.execute("PRAGMA index_list(attempts)"))
                for op in (start-timedelta(minutes=15), end):
                    if not indexed:
                        break  # Context unavailable beats scanning a live journal.
                    slot = json.dumps([symbol, attempt["source_id"], op.isoformat(), (op+timedelta(minutes=15)).isoformat()], separators=(",", ":"))
                    row = db.execute("SELECT payload FROM attempts WHERE slot=? ORDER BY rowid DESC LIMIT 1", (slot,)).fetchone()
                    if row:
                        item = json.loads(row[0])
                        if item.get("status") == "success" and _time(item["request_ended_at"]) <= _time(self.clock()):
                            surrounding.append(item)
        received = max(_time(r["request_ended_at"]) for r in [attempt, *surrounding])
        raw = {"attempt": attempt, "context_attempts": surrounding,
               "evidence_kind": "retained normalized AcquisitionJournal responses; not original transport bytes"}
        reference = _retain(self.root, "primary", raw)
        rows = _parse("databento", attempt.get("rows", ()), _time(attempt["request_ended_at"]), symbol=symbol)
        extra = [r for a in surrounding for r in _parse("databento", a.get("rows", ()), _time(a["request_ended_at"]), symbol=symbol)]
        return {"provider": "databento", "symbol": symbol, "source": attempt["source_id"],
                "dataset": attempt["source_dataset"], "receipt_id": reference["sha256"],
                "payload_sha256": reference["sha256"], "status": "ok", "response_complete": True,
                "request_start": start.isoformat(), "request_end": end.isoformat(), "received_at": received.isoformat(),
                "native_interval_seconds": 60, "records": rows, "price_context": _context(extra, start, end, 60, received),
                "coverage_scope": "selected Databento dataset; no market-wide completeness claim"}

    def recover(self, symbol, opened_at, previous, primary_result):
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
            raise ValueError("Explicit equity symbol required")
        start = _time(opened_at); end = start + timedelta(minutes=15)
        if start.timestamp() % 900 or end > _time(self.clock()):
            raise ValueError("Recovery requires an aligned completed quarter")
        if self.stopped():
            return {"status": "stopped", "assessment": None}
        evidence, requests = [], []
        primary = self._primary(symbol, start, end, primary_result)
        if primary is not None:
            evidence.append(primary)
        for provider in ("schwab", "fmp"):
            if self.stopped():
                return {"status": "stopped", "assessment": None}
            item, receipt = self._request(provider, symbol, start, end)
            requests.append(receipt)
            if item is not None:
                evidence.append(item)
        previous = dict(previous or {})
        anchor_ref = _retain(self.root, "anchors", previous)
        native_last = previous.get("native_last_opened_at")
        # A quarter close is not evidence of a last-minute trade. Without the
        # actual last native event, leave anchor validity false for derived carry.
        timestamp = (_time(native_last) + timedelta(seconds=int(previous.get("native_interval_seconds", 60)))).isoformat() if native_last else previous.get("opened_at", start.isoformat())
        try:
            price = float(previous.get("close"))
            price_valid = math.isfinite(price) and price > 0
        except (ValueError, TypeError):
            price, price_valid = 1., False
        anchor = {"price": price if price_valid else 1., "timestamp": timestamp,
                  "received_at": previous.get("available_at", end.isoformat()),
                  "kind": "observed_trade_close" if price_valid and native_last and previous.get("synthetic") is False else "unverified_prior_close",
                  "source": previous.get("source_id", "unavailable"), "receipt_id": anchor_ref["sha256"],
                  "payload_sha256": anchor_ref["sha256"], "is_synthetic": previous.get("synthetic", True)}
        assessment = assess_interval_gap(symbol, start, end, evidence=evidence, anchor=anchor, as_of=self.clock())
        proof = _retain(self.root, "assessments", {"version": VERSION, "assessment": assessment,
                        "evidence": evidence, "requests": requests, "anchor_reference": anchor_ref})
        pending = any(r["status"] in {"retry_pending", "deferred"} for r in requests)
        return {"status": "recovered" if assessment["observed_candidates"] or assessment["derived_allowed"] else "retry" if pending else "unavailable",
                "assessment": assessment, "proof_path": proof["path"], "proof_sha256": proof["sha256"],
                "retry_after_seconds": 2 if pending else 0, "requests": requests}
