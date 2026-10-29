"""Reuse retained EQUS minutes in canonical history without losing receipt time.

The Parquet writer still owns native keys and revisions. This small index stores
only value digests and immutable response references, never a second price table.
Call writers under the existing datastore mutex. Readers never create the index.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from io import StringIO
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pandas as pd

from .acquisition_objects import ResponseObjects, journal_payload_bytes
from .prediction import ContractError

SOURCE = "databento:EQUS.MINI:ohlcv-1m"
FIELDS = ("open", "high", "low", "close", "volume")
INDEX = Path("intraday/source-objects/minute-receipts.sqlite3")
REQUEST = "source_100d_1m_ohlcv-1m_1m"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc(value):
    value = pd.Timestamp(value)
    if value.tzinfo is None:
        raise ContractError("Canonical receipt timestamps must be timezone aware")
    return value.tz_convert("UTC")


def value_digest(row):
    """Identity includes the exact provider, opening time and numeric values."""
    stamp = _utc(row.get("opened_at", row.get("timestamp", row.get("ts_event"))))
    values = [float(row[key]) for key in FIELDS]
    return hashlib.sha256(_json([SOURCE, stamp.isoformat(), *values]).encode()).hexdigest()


def _connect(root):
    path = Path(root) / INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS response_refs(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS revisions(symbol TEXT,opened_ns INTEGER,revision INTEGER,value_sha TEXT,available_ns INTEGER,last_seen_ns INTEGER,response_id TEXT,PRIMARY KEY(symbol,opened_ns,revision))")
    db.execute("CREATE TABLE IF NOT EXISTS journal_cursors(journal TEXT,symbol TEXT,last_rowid INTEGER,PRIMARY KEY(journal,symbol))")
    db.commit()
    return db


def _head(db, symbol, opened):
    return db.execute("SELECT revision,value_sha,available_ns,last_seen_ns,response_id FROM revisions WHERE symbol=? AND opened_ns=? ORDER BY revision DESC LIMIT 1",
                      (symbol, _utc(opened).value)).fetchone()


def _record(db, symbol, row, received, ref):
    stamp, received = _utc(row.get("opened_at", row.get("timestamp"))), _utc(received)
    if received < stamp + pd.Timedelta(minutes=1):
        raise ContractError("Receipt precedes completed source minute")
    digest, previous = value_digest(row), _head(db, symbol, stamp)
    if previous and received.value < previous[3]:
        return False
    db.execute("INSERT OR IGNORE INTO response_refs VALUES(?,?)", (ref["object_sha256"], _json(ref)))
    if previous and previous[1] == digest:
        # Repeated unchanged observations do not reset first known availability.
        db.execute("UPDATE revisions SET last_seen_ns=? WHERE symbol=? AND opened_ns=? AND revision=?",
                   (received.value, symbol, stamp.value, previous[0]))
    else:
        revision = previous[0] + 1 if previous else 1
        db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?)",
                   (symbol, stamp.value, revision, digest, received.value, received.value, ref["object_sha256"]))
    return True


def overlay_receipts(root, symbol, frame):
    """Apply actual clocks only to exact current values with retained evidence.

    A changed indexed value cannot silently reuse an older receipt. A -> B -> A
    selects the new A revision, not the first A receipt. Unindexed legacy rows
    retain their explicit historical assumption.
    """
    path = Path(root) / INDEX
    if not path.is_file() or frame.empty:
        return frame, 0
    frame = frame.copy()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT r.opened_ns,r.value_sha,r.available_ns,p.payload FROM revisions r LEFT JOIN response_refs p ON p.id=r.response_id WHERE r.symbol=? AND r.opened_ns>=? AND r.opened_ns<=? ORDER BY r.revision",
            (symbol.upper(), _utc(frame.opened_at.min()).value, _utc(frame.opened_at.max()).value)).fetchall()
    matches = {stamp: (digest, available, ref) for stamp, digest, available, ref in rows}
    objects = ResponseObjects(Path(root) / "intraday/source-objects/responses")
    verified = {}
    count = 0
    selected = frame.loc[frame.opened_at.isin(pd.to_datetime(list(matches), unit="ns", utc=True))]
    for index, row in selected.iterrows():
        match = matches.get(_utc(row.opened_at).value)
        if match is None:
            continue
        digest, available, reference = match
        if reference is None:
            raise ContractError("Canonical timing index is missing its response reference")
        if digest != value_digest(row):
            raise ContractError("Canonical minute differs from its latest indexed receipt; finish or repair the source upsert")
        if reference not in verified:
            original = json.loads(objects.restore(json.loads(reference)))
            if original.get("source_id") != SOURCE or original.get("symbol", "").upper() != symbol.upper():
                raise ContractError("Canonical receipt has a different source identity")
            verified[reference] = (_utc(original["request_ended_at"]).value,
                                   {value_digest(item) for item in original["rows"]})
        receipt_time, values = verified[reference]
        if receipt_time != available or digest not in values:
            raise ContractError("Canonical timing index differs from its immutable original response")
        frame.at[index, "available_at"] = pd.Timestamp(available, unit="ns", tz="UTC")
        frame.at[index, "availability_kind"] = "actual"
        frame.at[index, "evidence_json"] = _json({"canonical_receipt": json.loads(reference),
            "source_id": SOURCE, "availability_basis": "first_receipt_of_current_value_revision"})
        count += 1
    return frame, count


def _canonical_rows(root, symbol):
    path = Path(root) / f"stocks/{symbol}/bars/1m/databento/normalized/{symbol}_{REQUEST}.parquet"
    if not path.is_file():
        return {}
    frame = pd.read_parquet(path, columns=["timestamp", *FIELDS])
    return {_utc(row["timestamp"]).value: value_digest(row) for row in frame.to_dict("records")}


def _native_frame(response):
    from .data import _require_native_observations
    raw = pd.read_json(StringIO(_json(response.native_response)), orient="table")
    _require_native_observations(raw)
    if not {"ts_event", *FIELDS} <= set(raw) or len(raw) != len(response.rows):
        raise ContractError("Retained native response differs from minute projection")
    if "symbol" in raw and set(raw.symbol.astype(str).str.upper()) != {response.symbol.upper()}:
        raise ContractError("Retained native response has another symbol")
    stamps = [_utc(value) for value in raw.ts_event]
    if len(set(stamps)) != len(stamps):
        raise ContractError("Retained native response contains duplicate source minutes")
    projected = [_utc(row["opened_at"]) for row in response.rows]
    if len(set(projected)) != len(projected) or set(projected) != set(stamps):
        raise ContractError("Retained minute projection changes native timestamp identities")
    native = {stamp.value: value_digest(row) for stamp, row in zip(stamps, raw.to_dict("records"))}
    for row in response.rows:
        stamp = _utc(row["opened_at"])
        if (stamp != stamp.floor("min") or stamp < _utc(response.interval_open)
                or stamp + pd.Timedelta(minutes=1) > _utc(response.interval_close)
                or native.get(stamp.value) != value_digest(row)):
            raise ContractError("Retained native values or exact interval differ from projection")
    raw["ts_event"] = pd.DatetimeIndex(stamps)
    return raw


def ingest_saved_attempts(store, symbols, *, batch_size=512):
    """Incrementally reuse original daytime responses before nighttime fetch.

    Bootstrap considers only the canonical tail overlap; old canonical history
    is never copied or retrospectively assigned invented receipt clocks. Once a
    cursor exists, late-arriving attempts can fill older missing minutes too.
    Unknown conflicting legacy revisions stay untouched for provider overlap to
    resolve. Each bounded batch uses one ordinary raw/normalized upsert.
    """
    from app.models.market_data import MarketBar
    from app.services.market_fetch_specs import databento_analysis_source_specs
    from datafetching.databento_fetch import _persist_native_results
    from .data import IntervalResponse
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("Positive bounded journal batch size required")
    root = Path(store.root_dir)
    journals = [root / "intraday/system-1a/acquisition.sqlite3", root / "intraday/acquisition.sqlite3"]
    journals = [path for path in journals if path.is_file()]
    if not journals:
        return {"attempts": 0, "minute_rows": 0, "legacy_conflicts_preserved": 0}
    totals = {"attempts": 0, "minute_rows": 0, "legacy_conflicts_preserved": 0}
    spec = next(s for s in databento_analysis_source_specs() if s.schema == "ohlcv-1m")
    objects = ResponseObjects(root / "intraday/source-objects/responses")
    with closing(_connect(root)) as index:
        for symbol in symbols:
            symbol = symbol.upper()
            canonical = _canonical_rows(root, symbol)
            bootstrap = pd.Timestamp(max(canonical), unit="ns", tz="UTC") - pd.Timedelta(hours=1) if canonical else None
            for journal in journals:
                key = str(journal.resolve())
                cursor = index.execute("SELECT last_rowid FROM journal_cursors WHERE journal=? AND symbol=?", (key, symbol)).fetchone()
                after = cursor[0] if cursor else 0
                with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    while True:
                        records = source.execute("SELECT rowid,payload,slot FROM attempts WHERE rowid>? AND json_extract(slot,'$[0]')=? AND json_extract(slot,'$[1]')=? ORDER BY rowid LIMIT ?",
                            (after, symbol, SOURCE, batch_size)).fetchall()
                        if not records:
                            break
                        pending, raw_parts = {}, []
                        for rowid, payload, slot in records:
                            after = rowid
                            if cursor is None and bootstrap is not None and _utc(json.loads(slot)[3]) < bootstrap:
                                continue
                            original = journal_payload_bytes(journal, payload)
                            response = IntervalResponse(**json.loads(original)).validate()
                            identity = [response.symbol, response.source_id,
                                _utc(response.interval_open).isoformat(), _utc(response.interval_close).isoformat()]
                            if identity != json.loads(slot) or response.symbol.upper() != symbol or response.source_id != SOURCE:
                                raise ContractError("Retained response identity differs from its original journal slot")
                            if (response.status != "success" or response.source_dataset != "EQUS.MINI"
                                    or not response.covered() or not response.rows or response.native_response is None):
                                continue
                            raw = _native_frame(response)
                            ref = objects.preserve(original)
                            selected = set()
                            for row in response.rows:
                                stamp, digest = _utc(row["opened_at"]), value_digest(row)
                                head = _head(index, symbol, stamp)
                                if stamp.value in canonical and canonical[stamp.value] != digest and head is None:
                                    totals["legacy_conflicts_preserved"] += 1
                                    continue
                                if not _record(index, symbol, row, response.request_ended_at, ref):
                                    continue
                                canonical[stamp.value] = digest
                                pending[stamp.value] = MarketBar(symbol, "databento", "1m", stamp.to_pydatetime(), **{k: float(row[k]) for k in FIELDS})
                                selected.add(stamp)
                            if selected:
                                part = raw.loc[raw.ts_event.isin(selected)].copy()
                                part["range_start"], part["range_end"] = response.interval_open, response.interval_close
                                raw_parts.append(part)
                            totals["attempts"] += 1
                        # Commit timing evidence first. If interrupted, only an
                        # exact matching canonical value can use it; retry safely
                        # completes upserts before advancing the journal cursor.
                        index.commit()
                        if pending:
                            raw = pd.concat(raw_parts, ignore_index=True).drop_duplicates("ts_event", keep="last")
                            available = SimpleNamespace(start=min(_utc(v) for v in raw.range_start).to_pydatetime(),
                                end=max(_utc(v) for v in raw.range_end).to_pydatetime())
                            persisted = _persist_native_results(symbol, store, provider=SimpleNamespace(dataset="EQUS.MINI"),
                                profile="retained_intraday_continuation", observed_at=datetime.now(timezone.utc),
                                native_results=[(spec, list(pending.values()), raw, available, None)], run_derived=False)
                            if persisted.error_files:
                                raise ContractError("Retained source persistence returned errors; journal cursor remains resumable")
                            saved = _canonical_rows(root, symbol)
                            raw_path = root / f"stocks/{symbol}/bars/1m/databento/raw/{symbol}_{REQUEST}_raw.parquet"
                            saved_raw = pd.read_parquet(raw_path, columns=["ts_event", *FIELDS])
                            raw_values = {_utc(row["ts_event"]).value: value_digest(row) for row in saved_raw.to_dict("records")}
                            if any(saved.get(stamp) != canonical[stamp] or raw_values.get(stamp) != canonical[stamp]
                                   for stamp in pending):
                                raise ContractError("Canonical raw/normalized readback differs; journal cursor remains resumable")
                            totals["minute_rows"] += len(pending)
                        index.execute("INSERT INTO journal_cursors VALUES(?,?,?) ON CONFLICT(journal,symbol) DO UPDATE SET last_rowid=excluded.last_rowid", (key, symbol, after))
                        index.commit()
    return totals


def record_native_refresh(root, symbol, native_results, *, started_at, received_at):
    """Index actual new nightly receipts before their existing canonical upsert."""
    from .data import _native_response_snapshot
    objects = ResponseObjects(Path(root) / "intraday/source-objects/responses")
    for spec, bars, raw, available, error in native_results:
        if error is not None or raw is None or not bars:
            continue
        if spec.schema != "ohlcv-1m" or spec.frequency != "1m" or available is None:
            raise ContractError("Canonical receipt indexing requires native EQUS minutes")
        rows = [dict(opened_at=_utc(bar.timestamp).isoformat(), **{key: getattr(bar, key) for key in FIELDS}) for bar in bars]
        response = dict(symbol=symbol, source_id=SOURCE, source_dataset="EQUS.MINI",
            request_started_at=_utc(started_at).isoformat(), request_ended_at=_utc(received_at).isoformat(),
            interval_open=_utc(available.start).isoformat(), interval_close=_utc(available.end).isoformat(),
            status="success", rows=rows, native_response=_native_response_snapshot(raw))
        ref = objects.preserve(_json(response))
        with closing(_connect(root)) as db, db:
            for row in rows:
                if _utc(row["opened_at"]) + pd.Timedelta(minutes=1) <= _utc(received_at):
                    if not _record(db, symbol.upper(), row, received_at, ref):
                        raise ContractError("Nightly receipt precedes the recorded source revision; canonical write withheld")


def reuse_before_refresh(store, symbols):
    """An unusable retained response must not prevent ordinary continuation.

    Keep each failed cursor resumable and report its error explicitly. Provider
    continuation may repair its canonical values; the history loader still
    rejects unresolved source/timing mismatches. Other symbols continue normally.
    """
    result = {"attempts": 0, "minute_rows": 0, "legacy_conflicts_preserved": 0,
              "errors": {}, "metrics_scope": "successful_symbol_reuse_calls"}
    for symbol in symbols:
        try:
            reused = ingest_saved_attempts(store, (symbol,))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
            # This offline path has no provider exception requiring a response
            # body in logs. Symbol and exception class locate the retained error
            # without echoing original source payloads or credential-like text.
            result["errors"][symbol] = {"status": "error", "failure_type": type(exc).__name__,
                "phase": "retained_intraday_reuse", "failed_batch_cursor": "not_advanced",
                "ordinary_provider_continuation": "continues"}
            continue
        for key in ("attempts", "minute_rows", "legacy_conflicts_preserved"):
            result[key] += reused[key]
    result["status"] = "partial" if result["errors"] else "complete"
    return result
