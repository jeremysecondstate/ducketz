"""Explicit multi-provider candles; primary observations and estimates stay distinct.

Pure transformations of retained evidence. No provider, datastore or model calls.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import numpy as np
import pandas as pd

from .coverage import verified_source_observation
from .prediction import ContractError, STEP
from ml.market_gap_evidence import POLICY, MARKERS, derive_gap_carry

POPULATION_CONTRACT = "intraday-multi-provider-recovery-v1"
SOURCE_ID = "multi:databento-schwab-fmp:15m"
SOURCE_DATASET = "MULTI_PROVIDER"
PRICE_FIELDS = ("open", "high", "low", "close", "volume")
MAX_SAVED_CANDLE_BYTES = 4 * 1024 * 1024
MAX_CATALOG_ROWS = 100000
MAX_LOAD_BYTES = 128 * 1024 * 1024


def _utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ContractError("Recovery clocks must be timezone aware")
    return stamp.tz_convert("UTC")


def _default(value):
    if isinstance(value, (datetime, pd.Timestamp)):
        return _utc(value).isoformat()
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        return value.item()
    raise TypeError(type(value).__name__)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_default, allow_nan=False)


def _plain(value):
    return json.loads(_json(value))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _session(session, opened_at):
    from .data import SessionBounds
    if isinstance(session, Mapping):
        session = SessionBounds(str(session["session_id"]), session["session_open"], session["session_close"])
    if not session.contains(opened_at) or _utc(opened_at) != _utc(opened_at).floor("15min"):
        raise ContractError("Recovery candle is outside its exact session quarter")
    return {"session_id": str(session.session_id), "session_open": _utc(session.opened_at).isoformat(),
            "session_close": _utc(session.closed_at).isoformat()}


def _wrapper(row, evidence):
    row = _plain(row)
    row.update(source_id=SOURCE_ID, source_dataset=SOURCE_DATASET,
               population_contract=POPULATION_CONTRACT, evidence_json=_json(evidence))
    return row


def wrap_primary(candle):
    """Retain a genuine primary candle unchanged inside a new-contract wrapper."""
    if candle.get("population_contract") == POPULATION_CONTRACT:
        if not verified_recovery_candle(candle):
            raise ContractError("Invalid existing recovered population candle")
        return _plain(candle)
    if not verified_source_observation(candle):
        raise ContractError("Only a verified genuine primary candle can be wrapped")
    original = _plain(candle)
    _session(original, original["opened_at"])
    row = dict(original, native_source_id=original["source_id"], native_source_dataset=original["source_dataset"],
               native_interval_seconds=60, observation_kind="primary_observed")
    return _wrapper(row, {"aggregation": POPULATION_CONTRACT, "kind": "primary_observed",
        "native_primary": original, "native_primary_sha256": _digest(original),
        "absence_policy": "original_verified_primary_observations_preserved"})


def _assessment_valid(assessment):
    if not isinstance(assessment, Mapping) or assessment.get("policy") != POLICY:
        return False
    body = {key: value for key, value in assessment.items() if key != "assessment_sha256"}
    if _digest(body) != assessment.get("assessment_sha256"):
        return False
    start, end, decision = map(_utc, (assessment["start"], assessment["end"], assessment["as_of"]))
    return bool(assessment.get("symbol")) and start == start.floor("15min") and end == start+STEP and end <= decision


def _candidate(candidate, assessment):
    """Revalidate whole native bars and receipt timing before aggregation."""
    provider = candidate.get("provider")
    seconds = candidate.get("native_interval_seconds")
    if (provider, seconds) not in {("schwab", 60), ("fmp", 300)}:
        return None
    start, end, as_of = map(_utc, (assessment["start"], assessment["end"], assessment["as_of"]))
    received = _utc(candidate["received_at"])
    if not end <= received <= as_of:
        return None
    references = assessment.get("source_references", [])
    if not any(all(candidate.get(key) == ref.get(key) for key in
               ("source", "dataset", "receipt_id", "payload_sha256", "received_at", "native_interval_seconds"))
               and ref.get("canonical_provider") == provider
               and _utc(ref["request_start"]) <= start and end <= _utc(ref["request_end"])
               for ref in references):
        return None
    native = candidate.get("records")
    if not isinstance(native, list) or not 1 <= len(native) <= 10000:
        return None
    distinct = {}
    for record in native:
        if any(key in record and record[key] is not False for key in (*MARKERS, "synthetic_open", "synthetic_close")):
            return None
        if "symbol" in record and record["symbol"] != assessment["symbol"]:
            return None
        if "synthetic_constituents" in record and (isinstance(record["synthetic_constituents"], bool)
                or record["synthetic_constituents"] != 0):
            return None
        clock = _utc(record["timestamp"])
        if not start <= clock < end or clock.value % (int(seconds)*1_000_000_000) or clock+pd.Timedelta(seconds=seconds) > end:
            return None
        if any(isinstance(record[key], bool) for key in PRICE_FIELDS):
            return None
        values = tuple(float(record[key]) for key in PRICE_FIELDS)
        op, high, low, close, volume = values
        if not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or volume <= 0 or high < max(op, close, low) or low > min(op, close):
            return None
        if clock in distinct and distinct[clock] != values:
            return None
        distinct[clock] = values
    rows = [(clock, values) for clock, values in sorted(distinct.items())]
    if len(rows) > 900 // seconds:
        return None
    aggregate = dict(zip(PRICE_FIELDS, (rows[0][1][0], max(v[1] for _, v in rows), min(v[2] for _, v in rows),
                                       rows[-1][1][3], sum(v[4] for _, v in rows))))
    return {"candidate": _plain(candidate), "aggregate": aggregate, "first": rows[0][0].isoformat(),
            "last": rows[-1][0].isoformat(), "observed": len(rows), "seconds": int(seconds),
            "received_at": received.isoformat()}


def recovery_candle(assessment, session):
    """Return an observed recovery, an explicitly derived estimate, or None."""
    try:
        if not _assessment_valid(assessment):
            return None
        session_fields = _session(session, assessment["start"])
        candidates = []
        for item in assessment.get("observed_candidates", []):
            valid = _candidate(item, assessment)
            if valid is not None:
                candidates.append(valid)
        chosen = None
        for provider in ("schwab", "fmp"):
            matching = [c for c in candidates if c["candidate"]["provider"] == provider]
            if not matching:
                continue
            latest = max(_utc(c["received_at"]) for c in matching)
            latest_rows = [c for c in matching if _utc(c["received_at"]) == latest]
            # Equal-time conflicting revisions cannot be selected by file order.
            if len({_json({k: c[k] for k in ("aggregate", "first", "last", "observed", "seconds")}) for c in latest_rows}) != 1:
                continue
            chosen = min(latest_rows, key=lambda c: (c["candidate"]["payload_sha256"], c["candidate"]["receipt_id"]))
            break
        common = {"symbol": assessment["symbol"], "opened_at": assessment["start"], "interval_seconds": 900,
                  **session_fields, "availability_kind": "actual", "no_trade": False}
        if chosen is not None:
            candidate = chosen["candidate"]
            row = dict(common, **chosen["aggregate"], available_at=chosen["received_at"], synthetic=False,
                synthetic_open=False, synthetic_close=False, synthetic_constituents=0,
                observed_constituents=chosen["observed"], source_empty_constituents=900//chosen["seconds"]-chosen["observed"],
                native_interval_seconds=chosen["seconds"], native_first_opened_at=chosen["first"], native_last_opened_at=chosen["last"],
                native_source_id=candidate["source"], native_source_dataset=candidate["dataset"], observation_kind="alternate_provider_observed")
            return _wrapper(row, {"aggregation": POPULATION_CONTRACT, "kind": "alternate_provider_observed",
                "assessment": assessment, "session": session_fields,
                "selected_candidate": candidate, "selected_candidate_sha256": _digest(candidate),
                "preference": "Schwab native 1-minute then FMP native 5-minute; latest eligible receipt; no source mixing",
                "absence_policy": "missing_native_bars_unavailable_not_filled"})
        if assessment.get("observed_candidates"):
            return None
        derived = derive_gap_carry(assessment)
        row = dict(common, **{key: derived[key] for key in PRICE_FIELDS}, available_at=derived["available_at"], synthetic=True,
            synthetic_open=True, synthetic_close=True, synthetic_constituents=1, observed_constituents=0,
            source_empty_constituents=0, native_interval_seconds=900, native_first_opened_at=None, native_last_opened_at=None,
            native_source_id="derived:"+POLICY, native_source_dataset="CROSS_PROVIDER_CORROBORATION",
            observation_kind="corroborated_price_carry_estimate")
        return _wrapper(row, {"aggregation": POPULATION_CONTRACT, "kind": "corroborated_price_carry_estimate",
            "assessment": assessment, "session": session_fields, "derived": derived, "derived_sha256": _digest(derived),
            "market_wide_no_trade_verified": False})
    except (KeyError, TypeError, ValueError, OverflowError, ContractError):
        return None


def verified_recovery_candle(candle):
    """Verify frozen source evidence; never reinterpret an older contract."""
    try:
        if (candle.get("population_contract") != POPULATION_CONTRACT or candle.get("source_id") != SOURCE_ID
                or candle.get("source_dataset") != SOURCE_DATASET):
            return False
        if any(not isinstance(candle.get(key), (bool, np.bool_)) for key in
               ("synthetic", "no_trade", "synthetic_open", "synthetic_close")):
            return False
        for key in (*PRICE_FIELDS, "observed_constituents", "synthetic_constituents", "source_empty_constituents"):
            number = candle.get(key)
            if isinstance(number, (bool, np.bool_)) or not isinstance(number, (int, float, np.integer, np.floating)) or not math.isfinite(float(number)):
                return False
        evidence = candle["evidence_json"]
        evidence = evidence if isinstance(evidence, Mapping) else json.loads(evidence)
        if evidence.get("aggregation") != POPULATION_CONTRACT:
            return False
        if evidence.get("kind") == "primary_observed":
            original = evidence["native_primary"]
            if _digest(original) != evidence["native_primary_sha256"]:
                return False
            # wrap_primary performs the complete native observation validation.
            # Repeating it here doubles the same per-minute proof work.
            expected = wrap_primary(original)
        else:
            expected = recovery_candle(evidence["assessment"], evidence["session"])
            if expected is None:
                return False
        # Compare all contract-owned fields, including exact retained evidence.
        # Callers may add storage/evaluation identifiers without changing meaning.
        actual = {key: candle.get(key) for key in expected}
        # Pandas can represent an intentionally absent native clock as NaT/NaN
        # after mixing derived and observed rows. Only these two nullable fields
        # may normalize, and only when the frozen evidence expects absence.
        for key in ("native_first_opened_at", "native_last_opened_at"):
            if expected.get(key) is None and pd.isna(actual.get(key)):
                actual[key] = None
        if isinstance(actual.get("evidence_json"), Mapping):
            actual["evidence_json"] = _json(actual["evidence_json"])
        for key in ("opened_at", "available_at", "session_open", "session_close"):
            actual[key], expected[key] = _utc(actual[key]).isoformat(), _utc(expected[key]).isoformat()
        return _plain(actual) == _plain(expected)
    except (KeyError, TypeError, ValueError, OverflowError, ContractError):
        return False


def save_recovery_candle(datastore_root, candle):
    """Append non-primary evidence; no canonical data or older object is edited."""
    if not verified_recovery_candle(candle):
        raise ContractError("Cannot save invalid recovered candle")
    if candle["observation_kind"] == "primary_observed":
        return None
    value = dict(candle)
    if value["observation_kind"] == "corroborated_price_carry_estimate":
        for key in ("native_first_opened_at", "native_last_opened_at"):
            if pd.isna(value.get(key)):
                value[key] = None
    value = _plain(value)
    for key in ("opened_at", "available_at", "session_open", "session_close"):
        value[key] = _utc(value[key]).isoformat()
    data = _json(value).encode()
    if len(data) > MAX_SAVED_CANDLE_BYTES:
        raise ContractError("Recovered candle exceeds compact evidence bound")
    identity = hashlib.sha256(data).hexdigest()
    root = Path(datastore_root).resolve()/"intraday/gap-recovery"
    objects = root/"candles"
    objects.mkdir(parents=True, exist_ok=True)
    path = objects/(identity+".json")
    temporary = objects/(identity+".pending-"+uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ContractError("Immutable recovered candle identity differs")
    finally:
        temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(root/"candles.sqlite3", timeout=3)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS candles(sha256 TEXT PRIMARY KEY,symbol TEXT NOT NULL,opened_at TEXT NOT NULL,available_at TEXT NOT NULL,kind TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS candles_symbol_time ON candles(symbol,opened_at,available_at)")
        record = (identity, value["symbol"], value["opened_at"], value["available_at"], value["observation_kind"])
        existing = db.execute("SELECT sha256,symbol,opened_at,available_at,kind FROM candles WHERE sha256=?", (identity,)).fetchone()
        if existing is not None and existing != record:
            raise ContractError("Immutable recovered catalog entry differs")
        db.execute("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?)", record)
    return {"path": str(path), "sha256": identity, "bytes": len(data), "catalog": str(root/"candles.sqlite3")}


def load_recovery_candles(datastore_root, symbol, *, start=None, end=None, cutoff=None):
    """Read bounded indexed evidence, selecting newest eligible revision per slot.

    Pass cutoff for historical replay: later revisions must not hide earlier
    eligible evidence. No files or database are created on this read path.
    """
    root = Path(datastore_root).resolve()/"intraday/gap-recovery"
    database = root/"candles.sqlite3"
    if not database.is_file():
        return []
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
        raise ContractError("Explicit recovered-candle symbol required")
    clauses, params = ["symbol=?"], [symbol]
    for field, op, value in (("opened_at", ">=", start), ("opened_at", "<", end), ("available_at", "<=", cutoff)):
        if value is not None:
            clauses.append(field+op+"?"); params.append(_utc(value).isoformat())
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=1)) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(candles)")]
        if columns != ["sha256", "symbol", "opened_at", "available_at", "kind"]:
            raise ContractError("Recovered catalog schema differs")
        indices = db.execute("PRAGMA index_list(candles)").fetchall()
        if not any([row[2] for row in db.execute('PRAGMA index_info("'+str(index[1]).replace('"','""')+'")')][:3]
                   == ["symbol", "opened_at", "available_at"] for index in indices):
            raise ContractError("Recovered catalog requires symbol/time index")
        records = db.execute("SELECT sha256,symbol,opened_at,available_at,kind FROM candles WHERE "+" AND ".join(clauses)+
                             " ORDER BY opened_at,available_at,sha256 LIMIT ?", (*params, MAX_CATALOG_ROWS+1)).fetchall()
    if len(records) > MAX_CATALOG_ROWS:
        raise ContractError("Recovered catalog exceeds bounded per-symbol selection")
    latest, total_bytes = {}, 0
    for digest, saved_symbol, opened, available, kind in records:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ContractError("Recovered catalog object identity malformed")
        path = root/"candles"/(digest+".json")
        size = path.stat().st_size
        total_bytes += size
        if size > MAX_SAVED_CANDLE_BYTES or total_bytes > MAX_LOAD_BYTES:
            raise ContractError("Recovered objects exceed bounded read size")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ContractError("Recovered candle object checksum differs")
        candle = json.loads(data)
        if (not verified_recovery_candle(candle) or candle["observation_kind"] == "primary_observed"
                or (candle["symbol"], candle["opened_at"], candle["available_at"], candle["observation_kind"]) !=
                   (saved_symbol, opened, available, kind)):
            raise ContractError("Recovered catalog and frozen evidence differ")
        old = latest.get(opened)
        if old is not None and old["available_at"] == available:
            comparable = (*PRICE_FIELDS, "synthetic", "observation_kind", "native_interval_seconds", "native_source_id")
            if any(old[key] != candle[key] for key in comparable):
                raise ContractError("Equal-time recovered revisions conflict")
        latest[opened] = candle
    return list(latest.values())
