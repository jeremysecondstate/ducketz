"""Reviewed first-gate failure proof for an immutable UNKNOWN Scout attempt.

This retrospective repair never changes a journal or rearms a send. A complete
trace from one specifically reviewed sender proves that its order transport was
not entered. A separate private receipt may remove that attempt's block on
distinct future forecasts, which still require fresh capacity and manual gates.
Hashes preserve evidence; the repair caller must authenticate the local capture
and verify that the original process ended before creating a receipt.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from .prediction import ContractError, instant
from .scout_journal import _safe_path
from .scout_protocol import canonical_sha256

VERSION = "scout-reviewed-first-gate-clearance-v1"
COMMITMENT = "no_order_transport_entered"
MAX_BYTES = 3 * 1024 * 1024
# Source identities, not private account/request identities. Expanding this
# contract requires reviewing the whole sender, callback and diagnostic path.
SUPPORTED_SOURCE = {
    "ml/intraday/scout_client.py": "99afce435afd32ef27d148d0c46872ac4b1d9a5f6f4044ec567abf0db5bb4025",
    "ml/intraday/scout_broker.py": "e2627a1ec521105822520322961fee72e97b34d6684267dfa959ab3ade2400a8",
    "ml/intraday/scout_local_execution.py": "822d5a844607d699b7259d2926df2985cf4f53d7331f9d878f7f2d1a936e5c90",
    "ml/intraday/scout_journal.py": "a9da4c9b5735bf9f446f0449be451c3a4c82cb5674802fef5151728053c6cead",
    "ml/intraday/local_forecast.py": "00190bb360ae27503bfe19ad0fd0c20d9cf44ee0f0fd36a23128861267673df7",
    "ml/intraday/live_session.py": "217d36bb2bc475ef0901ec0c8aceddce18d3b3cc4d42e18c9df7087328b21046",
    "app/services/schwab.py": "76d3d9adea02030e9b419aa032be882a6dcd3b2eb1438e613da6bc1da9258308",
    "app/services/schwab_stock_orders.py": "9989209f1431b02cb43943cb8923d9463bfabd20cb8af6808445d3fb22104261",
    "app/services/schwab_order_fields.py": "74ce11f6bad9e88601f6681cd19d62161659d0d1beecff4e36518ccd63421321",
    "ml/stock_trader/session.py": "b7af001f9dc4bed9677cf66cd881e637b1f4d5c8a536dd3bb934b17d59019784",
}
EXPECTED_FRAMES = [
    {"file": file, "function": function, "line": line} for file, function, line in (
        ("scout_client.py", "submit", 186),
        ("scout_local_execution.py", "<lambda>", 387),
        ("scout_broker.py", "send", 182),
        ("scout_broker.py", "gate", 177),
        ("scout_local_execution.py", "local_authorization", 379),
        ("local_forecast.py", "_saved_forecast", 98),
    )
]


# This second retrospective profile is the complete sender used by the
# observed first-gate freshness rejection. It does not generalize arbitrary
# ContractError failures or permit clearance while the original process lives.
EXPIRED_GATE_SOURCE = {
    "ml/intraday/scout_client.py": "99afce435afd32ef27d148d0c46872ac4b1d9a5f6f4044ec567abf0db5bb4025",
    "ml/intraday/scout_broker.py": "e2627a1ec521105822520322961fee72e97b34d6684267dfa959ab3ade2400a8",
    "ml/intraday/scout_local_execution.py": "5f9a683d6de4e46e28887b1dad58e851a45ef41d03be924db1a38c8be032e7b2",
    "ml/intraday/scout_journal.py": "a9da4c9b5735bf9f446f0449be451c3a4c82cb5674802fef5151728053c6cead",
    "ml/intraday/local_forecast.py": "29af291cb125382ff05dcbbbc1e2cb0a7508ce01639dd0a21feb9a7b8daf5cb2",
    "ml/intraday/live_session.py": "bdcb29f43e3356a9a0c5fc1764b9fcef5288cb60fb7872c6c786bcc9ef97a06d",
    "app/services/schwab.py": "76d3d9adea02030e9b419aa032be882a6dcd3b2eb1438e613da6bc1da9258308",
    "app/services/schwab_stock_orders.py": "9989209f1431b02cb43943cb8923d9463bfabd20cb8af6808445d3fb22104261",
    "app/services/schwab_order_fields.py": "74ce11f6bad9e88601f6681cd19d62161659d0d1beecff4e36518ccd63421321",
    "ml/stock_trader/session.py": "b7af001f9dc4bed9677cf66cd881e637b1f4d5c8a536dd3bb934b17d59019784"
}
EXPIRED_GATE_FRAMES = [
    {"file": file, "function": function, "line": line} for file, function, line in (
        ("scout_client.py", "submit", 186),
        ("scout_local_execution.py", "<lambda>", 399),
        ("scout_broker.py", "send", 182),
        ("scout_broker.py", "gate", 177),
        ("scout_local_execution.py", "local_authorization", 388),
    )
]

def _require(condition, message):
    if not condition:
        raise ContractError("Pretransport clearance: " + message)


def _keys(value, expected, message):
    _require(type(value) is dict and set(value) == set(expected), message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path):
    path = _safe_path(Path(path))
    _require(path.stat().st_size <= MAX_BYTES, "evidence exceeds size limit")
    return path.read_bytes()


def _audit(books, journal, record):
    """One read-only transaction audits full submission and lifecycle chains."""
    _require(type(record) is dict and record.get("state") == "UNKNOWN"
             and record.get("broker_order_id") is None, "exact UNKNOWN attempt required")
    request = record["request"]
    _require(request["request_id"].startswith("scout-local:")
             and request["account_fingerprint"] == books.account_fingerprint
             and journal.binding["account_fingerprint"] == books.account_fingerprint
             and journal.binding["session_id"] == request["session_id"], "account/session binding differs")
    with closing(sqlite3.connect(_safe_path(journal.path).as_uri() + "?mode=ro",
                                 uri=True, timeout=0)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        control, records, events, _, lifecycle = journal._audit(db, include_lifecycle=True)
        _require(records.get(request["request_id"]) == record, "original attempt changed")
        _require(request["request_id"] not in lifecycle[1]
                 and all(e["request_id"] != request["request_id"] for e in lifecycle[2].values()),
                 "original attempt has lifecycle evidence")
        own = [e for e in events.values() if e["event"]["request_id"] == request["request_id"]]
    _require([e["event"]["kind"] for e in own] == ["PREPARED", "GRANTED", "SENDING", "UNKNOWN"],
             "exact single claim and UNKNOWN history required")
    return control["journal_id"], own


def _source(value, error_type):
    _require(error_type in {"OperationalError", "ContractError"}, "unsupported reviewed error")
    supported = SUPPORTED_SOURCE if error_type == "OperationalError" else EXPIRED_GATE_SOURCE
    _keys(value, {"manifest_text", "manifest_sha256", "files"}, "source proof fields differ")
    _require(type(value["manifest_text"]) is str, "manifest bytes missing")
    raw = value["manifest_text"].encode("utf-8")
    _require(_sha(raw) == value["manifest_sha256"], "manifest bytes changed")
    manifest = json.loads(raw)
    _require(manifest.get("schema") == "scout-isolated-live-runtime-v1"
             and type(manifest.get("files")) is dict, "unsupported original manifest")
    _keys(value["files"], supported, "complete reviewed dependencies required")
    for name, digest in supported.items():
        contents = value["files"][name]
        _require(type(contents) is str and _sha(contents.encode("utf-8")) == digest
                 and manifest["files"].get(name) == digest, "unsupported or changed original source: " + name)
    return manifest


def _event(row, *, session, kind):
    _keys(row, {"id", "cycle_id", "symbol", "recorded_at", "kind", "payload"}, "event row fields differ")
    _require(type(row["id"]) is int and row["id"] > 0 and row["cycle_id"] == session
             and row["kind"] == kind and type(row["payload"]) is dict, "event identity differs")
    return instant(row["recorded_at"])


def _proof(evidence, record, *, journal_id, events, as_of):
    _keys(evidence, {"version", "original_record", "original_record_sha256", "journal_id", "journal_events",
        "execution_event", "execution_event_sha256", "stop_event", "stop_event_sha256", "process_end", "source",
        "runtime_observation"},
        "proof fields differ")
    _require(evidence["version"] == VERSION and evidence["original_record"] == record
             and evidence["original_record_sha256"] == canonical_sha256(record)
             and evidence["journal_id"] == journal_id and evidence["journal_events"] == events,
             "original record or single-claim history differs")
    error_type = evidence["execution_event"]["payload"]["evidence"]["error_type"]
    manifest = _source(evidence["source"], error_type)
    request, permit = record["request"], record["grant"]
    session, rid = request["session_id"], request["request_id"]
    row = evidence["execution_event"]
    at = _event(row, session=session, kind="execution_step")
    _require(canonical_sha256(row) == evidence["execution_event_sha256"], "execution event changed")
    payload = row["payload"]
    _require(row["symbol"] == request["symbol"] and payload.get("symbol") == request["symbol"]
             and payload.get("forecast_id") == request["forecast_id"]
             and payload.get("kind") == "execution_disposition"
             and payload.get("disposition") == "submission_recovery_required"
             and payload.get("submission_status") == "UNKNOWN"
             and payload.get("submitted_quantity") is None and payload.get("broker_order_id") is None,
             "execution disposition does not bind original UNKNOWN")
    detail = payload["evidence"]
    _keys(detail, {"mode", "error_type", "local_state", "request_id", "retry_after_seconds", "submission_failure"},
          "execution diagnostic fields differ")
    _require(detail["mode"] == "scout-local-broker-account" and detail["request_id"] == rid
             and detail["local_state"] == "UNKNOWN" and detail["error_type"] == error_type,
             "diagnostic request differs")
    failure = detail["submission_failure"]
    _keys(failure, {"stage", "original"}, "submission diagnostics incomplete")
    _require(failure["stage"] == "send", "failure was not the reviewed sender call")
    original = failure["original"]
    _keys(original, {"chain", "chain_truncated"}, "exception-chain fields differ")
    _require(original["chain_truncated"] is False and type(original["chain"]) is list
             and len(original["chain"]) == 1, "complete single exception required")
    error = original["chain"][0]
    if error_type == "OperationalError":
        _keys(error, {"type", "message", "sqlite_errorcode", "frames"}, "exception fields differ")
        _require(error["type"] == "OperationalError" and error["message"] == "database is locked"
                 and type(error["sqlite_errorcode"]) is int and error["sqlite_errorcode"] == 261
                 and error["frames"] == EXPECTED_FRAMES, "not the reviewed initial-gate SQLite failure")
    else:
        _keys(error, {"type", "message", "frames"}, "exception fields differ")
        _require(error["type"] == "ContractError"
                 and error["message"] == "Exception text withheld; inspect error type and stack locations"
                 and error["frames"] == EXPIRED_GATE_FRAMES,
                 "not the reviewed initial-gate freshness rejection")
    _require(instant(request["issued_at"]) <= instant(events[-2]["event"]["recorded_at"])
             <= instant(events[-1]["event"]["recorded_at"]) <= at
             and instant(payload["recorded_at"]) <= at
             and instant(request["valid_until"]) <= as_of and instant(permit["expires_at"]) <= as_of,
             "request/claim/failure/expiry clocks differ")
    stopped = evidence["stop_event"]
    stopped_at = _event(stopped, session=session, kind="manual_session_stopped")
    _require(canonical_sha256(stopped) == evidence["stop_event_sha256"] and stopped["symbol"] is None
             and stopped["id"] > row["id"] and at <= stopped_at <= as_of
             and stopped["payload"] == {"pending_order_identity_preserved": True,
                 "stop_reason": "manual_stop", "worker_errors": []}, "complete clean original stop required")
    ended = evidence["process_end"]
    _keys(ended, {"session_id", "pid", "process_created_at_epoch", "verified_absent_at", "original_manifest_sha256"},
          "original process-end attestation fields differ")
    _require(ended["session_id"] == session and type(ended["pid"]) is int and ended["pid"] > 0
             and type(ended["process_created_at_epoch"]) in (int, float)
             and math.isfinite(ended["process_created_at_epoch"])
             and 0 < ended["process_created_at_epoch"] < at.timestamp()
             and stopped_at <= instant(ended["verified_absent_at"]) <= as_of
             and ended["original_manifest_sha256"] == evidence["source"]["manifest_sha256"],
             "original process is not proven ended for this source/session")
    observed = evidence["runtime_observation"]
    _keys(observed, {"at", "session_id", "process"}, "original runtime observation fields differ")
    process = observed["process"]
    _keys(process, {"verified", "pid", "created_at_epoch", "module", "binding", "entrypoint_kind",
        "launcher_path", "launcher_sha256", "manifest_path", "manifest_sha256", "source_root",
        "source_files_verified", "recipe_sha256"}, "original verified process fields differ")
    _require(observed["session_id"] == session and at <= instant(observed["at"]) <= stopped_at
             and process["verified"] is True and process["binding"] == "exact_birth"
             and process["module"] == "ml.intraday.live_session"
             and process["entrypoint_kind"] == "reviewed_isolated_live_launcher"
             and process["pid"] == ended["pid"]
             and process["created_at_epoch"] == ended["process_created_at_epoch"]
             and process["manifest_sha256"] == evidence["source"]["manifest_sha256"]
             and process["source_root"] == manifest.get("source_root")
             and type(process["source_files_verified"]) is int
             and process["source_files_verified"] == len(manifest["files"]),
             "original loaded-source observation differs from ended process")


def _path(books, record):
    return _safe_path(Path(books.root) / "pretransport-clearances" /
                      (canonical_sha256({"request_id": record["request"]["request_id"]}) + ".json"))


def _write_once(path, raw):
    _safe_path(path)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _require(_read(path) == raw, "immutable evidence already differs")


def load_clearance(books, journal, record, *, as_of):
    """Missing receipt returns None; any existing invalid proof fails closed."""
    path = _path(books, record)
    if not path.exists():
        return None
    try:
        journal_id, events = _audit(books, journal, record)
        envelope = json.loads(_read(path))
        _keys(envelope, {"payload", "sha256"}, "receipt envelope differs")
        payload = envelope["payload"]
        _keys(payload, {"version", "created_at", "review_reference", "evidence_sha256",
                       "original_record_sha256", "journal_id", "historical_execution", "commitment"},
              "receipt payload differs")
        _require(canonical_sha256(payload) == envelope["sha256"] and payload["version"] == VERSION
                 and payload["historical_execution"] == "UNKNOWN" and payload["commitment"] == COMMITMENT
                 and payload["journal_id"] == journal_id
                 and payload["original_record_sha256"] == canonical_sha256(record)
                 and type(payload["review_reference"]) is str and bool(payload["review_reference"].strip()),
                 "receipt identity or explicit review differs")
        created = instant(payload["created_at"])
        _require(created <= instant(as_of), "receipt is from the future")
        digest = payload["evidence_sha256"]
        _require(type(digest) is str and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
                 "invalid evidence digest")
        raw = _read(path.parent / (digest + ".proof.json"))
        _require(_sha(raw) == digest, "proof bytes changed")
        _proof(json.loads(raw), record, journal_id=journal_id, events=events, as_of=created)
        return dict(path=str(path), sha256=envelope["sha256"], created_at=payload["created_at"],
                    historical_execution="UNKNOWN", commitment=COMMITMENT)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("Pretransport clearance: invalid or unavailable proof") from exc


def create_clearance(books, journal, request_id, *, evidence_path, repository, review_reference, as_of):
    """Install reviewed saved proof only; never call a broker or change history."""
    _require(type(review_reference) is str and bool(review_reference.strip()), "explicit review reference required")
    now = instant(as_of)
    raw = _read(evidence_path)
    evidence = json.loads(raw)
    record = evidence["original_record"]
    _require(record["request"]["request_id"] == request_id, "requested attempt differs")
    journal_id, events = _audit(books, journal, record)
    _proof(evidence, record, journal_id=journal_id, events=events, as_of=now)
    for name, contents in evidence["source"]["files"].items():
        _require(_sha(_read(Path(repository) / name)) == _sha(contents.encode("utf-8")), "reviewed original source changed: " + name)
    path = _path(books, record)
    with books.locked():
        books.inspect()
        journal_id, events = _audit(books, journal, record)
        _proof(evidence, record, journal_id=journal_id, events=events, as_of=now)
        if path.exists():
            return load_clearance(books, journal, record, as_of=now)
        path.parent.mkdir(exist_ok=True)
        digest = _sha(raw)
        _write_once(path.parent / (digest + ".proof.json"), raw)
        payload = dict(version=VERSION, created_at=now.isoformat(), review_reference=review_reference,
            evidence_sha256=digest, original_record_sha256=canonical_sha256(record), journal_id=journal_id,
            historical_execution="UNKNOWN", commitment=COMMITMENT)
        envelope = dict(payload=payload, sha256=canonical_sha256(payload))
        _write_once(path, json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return load_clearance(books, journal, record, as_of=now)
