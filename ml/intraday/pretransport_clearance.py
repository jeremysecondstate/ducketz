"""Reviewed pre-POST failure proof for an immutable UNKNOWN Scout attempt.

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

from ml.stock_trader.session import stock_execution_window

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

# The native Schwab callback is directly before its sole requests.post call.
# This profile accepts only the observed exact source and complete callback
# stack; opaque broker implementations and all post/response failures remain
# unknown. The original clean-stop/process-end requirements also still apply.
NATIVE_CALLBACK_SOURCE = {
    "ml/intraday/scout_client.py": "99afce435afd32ef27d148d0c46872ac4b1d9a5f6f4044ec567abf0db5bb4025",
    "ml/intraday/scout_broker.py": "50b88b9c8a716ec632b952a2d27809c73d37fa6151a0dde226610554fab119d8",
    "ml/intraday/scout_local_execution.py": "ec82ef8ef4561264d501293e8b468b3df48b29bd2831b63c51b584f6cc8890bd",
    "ml/intraday/scout_journal.py": "a9da4c9b5735bf9f446f0449be451c3a4c82cb5674802fef5151728053c6cead",
    "ml/intraday/local_forecast.py": "29af291cb125382ff05dcbbbc1e2cb0a7508ce01639dd0a21feb9a7b8daf5cb2",
    "ml/intraday/live_session.py": "29ccec5ecab25b80085372afe4e2943487f1a5cc387c5ad3430d8c9435d7f731",
    "app/services/schwab.py": "76d3d9adea02030e9b419aa032be882a6dcd3b2eb1438e613da6bc1da9258308",
    "app/services/schwab_stock_orders.py": "9989209f1431b02cb43943cb8923d9463bfabd20cb8af6808445d3fb22104261",
    "app/services/schwab_order_fields.py": "74ce11f6bad9e88601f6681cd19d62161659d0d1beecff4e36518ccd63421321",
    "ml/stock_trader/session.py": "b7af001f9dc4bed9677cf66cd881e637b1f4d5c8a536dd3bb934b17d59019784",
    "ml/intraday/pretransport_clearance.py": "8cd6f25ddd73902e1bf30f453208c1acca5be00d9722be034a4a59089982df50",
    "ml/intraday/initial_gate_clearance.py": "72c9debccea3a1494b97ed8724812762b6b1f1c7524e4119792c941b4dffd214",
}
NATIVE_CALLBACK_FRAMES = [
    {"file": file, "function": function, "line": line} for file, function, line in (
        ("scout_client.py", "submit", 186),
        ("scout_local_execution.py", "send", 404),
        ("scout_broker.py", "send", 203),
        ("schwab.py", "submit_prepared_order", 690),
        ("scout_broker.py", "gate", 189),
        ("scout_local_execution.py", "local_authorization", 391),
    )
]

# The observed regular-to-PM transition refusal occurred in the adapter's
# first local clock/window check, before preparation or any order transport.
# This is a separate frozen historical sender profile, not a generic
# ScoutBrokerError clearance. Clean stop, process-end and expiry still apply.
CLOSED_WINDOW_SOURCE = {
    "ml/intraday/scout_client.py": "99afce435afd32ef27d148d0c46872ac4b1d9a5f6f4044ec567abf0db5bb4025",
    "ml/intraday/scout_broker.py": "706b666a4c891d1e9dccfb6dea6c85080796d15766ba7e3b207c74e6a4288be3",
    "ml/intraday/scout_local_execution.py": "4e18b853f4523889bfd43cd73cd08371c083ed163aaa661e827e7471da200206",
    "ml/intraday/scout_journal.py": "a9da4c9b5735bf9f446f0449be451c3a4c82cb5674802fef5151728053c6cead",
    "ml/intraday/local_forecast.py": "786c0c11e1a1bcdfb50b0b36541a92d6b6288220b9b8b11fc6168532934f9b6b",
    "ml/intraday/live_session.py": "67ad2c35f0f0c104fe36084e4931b1334da9d783e4c4fc58e633fd2caa952c14",
    "app/services/schwab.py": "41a3cf8b3e5e412083b864afbdafb7056b44a9390051aaca087c4e271a704951",
    "app/services/schwab_stock_orders.py": "9989209f1431b02cb43943cb8923d9463bfabd20cb8af6808445d3fb22104261",
    "app/services/schwab_order_fields.py": "74ce11f6bad9e88601f6681cd19d62161659d0d1beecff4e36518ccd63421321",
    "ml/stock_trader/session.py": "b7af001f9dc4bed9677cf66cd881e637b1f4d5c8a536dd3bb934b17d59019784",
    "ml/calendars.py": "c87adbd5527daa93c84490903611cb20a7e57cc6355e3c77b63b67c880663dcc",
    "ml/stock_trader/contracts.py": "9b20941167702594ac6dfbd6a8215ce5a58d65e3f9c552cb79f9681fb5cb37ae",
    "ml/intraday/pretransport_clearance.py": "18afc1b1f688f664da95853012c3e3c1eef551aec847a3728a79b7c37f9100cb",
    "ml/intraday/initial_gate_clearance.py": "2ff22f507cf98c88f055c5a7a149488616b43f8bbc053dab5794282225e2f503",
}
CLOSED_WINDOW_FRAMES = [
    {"file": file, "function": function, "line": line} for file, function, line in (
        ("scout_client.py", "submit", 186),
        ("scout_local_execution.py", "send", 517),
        ("scout_broker.py", "send", 182),
        ("scout_broker.py", "_current", 148),
    )
]

# Separate frozen historical profile. It cannot manufacture a synchronous
# client witness, clear a live original session, or broaden legacy VERSION.
HISTORICAL_CLIENT_FINAL_GATE_VERSION = "scout-reviewed-client-final-gate-history-v1"
HISTORICAL_CLIENT_FINAL_GATE_LAUNCHER = "1d544928f99cacef1af6cb3adc194121e73122b9a646b633b861d0f072db5b47"
HISTORICAL_CLIENT_FINAL_GATE_MANIFEST = "88af48af7ce1b3b2bc1e50951ba627bebd046e0801f672a97fcf2b1250665bf7"
HISTORICAL_CLIENT_FINAL_GATE_SOURCE = {
    "ml/intraday/scout_client.py": "99afce435afd32ef27d148d0c46872ac4b1d9a5f6f4044ec567abf0db5bb4025",
    "ml/intraday/scout_broker.py": "82b4ae2625ed5b112427861a147a428c9a98b0dfff15ef1001b320cecd14101d",
    "ml/intraday/scout_local_execution.py": "62b221ccaf536b1bf3e7d9379783ea5a4c0b905c77a590fee1e64d3852275e96",
    "ml/intraday/scout_journal.py": "a9da4c9b5735bf9f446f0449be451c3a4c82cb5674802fef5151728053c6cead",
    "ml/intraday/local_forecast.py": "38191f6f3f08cf8dca66fd8b90c6659d4806d22caa8a4dc09d631876d1dffe33",
    "ml/intraday/live_session.py": "29235e868957a1ad037a1a6254f4a8e999ba94e94101a85f04c979d25bf95712",
    "app/services/schwab.py": "41a3cf8b3e5e412083b864afbdafb7056b44a9390051aaca087c4e271a704951",
    "app/services/schwab_stock_orders.py": "9989209f1431b02cb43943cb8923d9463bfabd20cb8af6808445d3fb22104261",
    "app/services/schwab_order_fields.py": "74ce11f6bad9e88601f6681cd19d62161659d0d1beecff4e36518ccd63421321",
    "ml/stock_trader/session.py": "b7af001f9dc4bed9677cf66cd881e637b1f4d5c8a536dd3bb934b17d59019784",
    "ml/intraday/initial_gate_clearance.py": "2ff22f507cf98c88f055c5a7a149488616b43f8bbc053dab5794282225e2f503",
    "ml/intraday/pretransport_clearance.py": "a1c23dfb00f0d68ee91c2cfbbd72b2dd85a22f23d1122f63d81c4a2fa1ca3c61"
}
HISTORICAL_CLIENT_FINAL_GATE_FRAMES = [
    {"file": file, "function": function, "line": line} for file, function, line in (
        ("scout_client.py", "submit", 184),
        ("scout_client.py", "_gate", 155),
        ("scout_local_execution.py", "local_authorization", 565),
        ("local_forecast.py", "_saved_forecast", 182),
        ("local_forecast.py", "_saved_forecast_once", 139),
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


def _source(value, error_type, frames):
    _require(error_type in {"OperationalError", "ContractError", "ScoutBrokerError"}, "unsupported reviewed error")
    if error_type == "OperationalError":
        _require(frames == EXPECTED_FRAMES, "not the reviewed initial-gate SQLite failure")
        supported = SUPPORTED_SOURCE
    elif error_type == "ScoutBrokerError":
        _require(frames == CLOSED_WINDOW_FRAMES, "not the reviewed initial closed-window refusal")
        supported = CLOSED_WINDOW_SOURCE
    elif frames == NATIVE_CALLBACK_FRAMES:
        supported = NATIVE_CALLBACK_SOURCE
    else:
        _require(frames == EXPIRED_GATE_FRAMES, "not the reviewed freshness rejection")
        supported = EXPIRED_GATE_SOURCE
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


def _historical_client_final_gate_source(value):
    """Accept only the full reviewed original source and exact manifest bytes."""
    _keys(value, {"manifest_text", "manifest_sha256", "files"}, "source proof fields differ")
    _require(type(value["manifest_text"]) is str, "manifest bytes missing")
    raw = value["manifest_text"].encode("utf-8")
    _require(value["manifest_sha256"] == HISTORICAL_CLIENT_FINAL_GATE_MANIFEST
             and _sha(raw) == HISTORICAL_CLIENT_FINAL_GATE_MANIFEST,
             "not the reviewed historical client final-gate manifest")
    manifest = json.loads(raw)
    _require(manifest.get("schema") == "scout-isolated-live-runtime-v1"
             and type(manifest.get("files")) is dict, "unsupported original manifest")
    _keys(value["files"], HISTORICAL_CLIENT_FINAL_GATE_SOURCE,
          "complete reviewed historical client final-gate dependencies required")
    for name, digest in HISTORICAL_CLIENT_FINAL_GATE_SOURCE.items():
        contents = value["files"][name]
        _require(type(contents) is str and _sha(contents.encode("utf-8")) == digest
                 and manifest["files"].get(name) == digest,
                 "unsupported or changed original source: " + name)
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
    manifest = _source(evidence["source"], error_type, error.get("frames"))
    if error_type == "OperationalError":
        _keys(error, {"type", "message", "sqlite_errorcode", "frames"}, "exception fields differ")
        _require(error["type"] == "OperationalError" and error["message"] == "database is locked"
                 and type(error["sqlite_errorcode"]) is int and error["sqlite_errorcode"] == 261
                 and error["frames"] == EXPECTED_FRAMES, "not the reviewed initial-gate SQLite failure")
    elif error_type == "ScoutBrokerError":
        _keys(error, {"type", "message", "frames"}, "exception fields differ")
        _require(error["type"] == "ScoutBrokerError"
                 and error["message"] == "Exception text withheld; inspect error type and stack locations"
                 and error["frames"] == CLOSED_WINDOW_FRAMES,
                 "not the reviewed initial closed-window refusal")
        # The exact original line raises before preparation/transport. Also
        # reconstruct CLOSED across the retained claim-to-failure interval;
        # an open session, different clock refusal or transition-crossing
        # observation cannot borrow this narrower historical profile.
        stamps = [instant(events[-2]["event"]["recorded_at"]),
                  instant(events[-1]["event"]["recorded_at"]),
                  instant(payload["recorded_at"]), at]
        _require(stamps == sorted(stamps)
                 and instant(request["issued_at"]) <= stamps[0]
                 and at < instant(request["valid_until"])
                 and instant(permit["issued_at"]) <= stamps[0]
                 and at < instant(permit["expires_at"]), "closed-window refusal clocks differ")
        for stamp in stamps:
            window = stock_execution_window(stamp)
            _require(window.executable is False and window.time_in_force is None
                     and window.mode == "CLOSED"
                     and window.reason == "OUTSIDE_US_EQUITY_ACTIONABLE_SESSION",
                     "original submission window was not deterministically CLOSED")
    else:
        _keys(error, {"type", "message", "frames"}, "exception fields differ")
        _require(error["type"] == "ContractError"
                 and error["message"] == "Exception text withheld; inspect error type and stack locations"
                 and error["frames"] in [EXPIRED_GATE_FRAMES, NATIVE_CALLBACK_FRAMES],
                 "not the reviewed freshness rejection")
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


def _historical_client_final_gate_proof(evidence, record, *, journal_id, events, as_of):
    _keys(evidence, {"version", "original_record", "original_record_sha256", "journal_id", "journal_events",
        "execution_event", "execution_event_sha256", "stop_event", "stop_event_sha256", "process_end", "source",
        "runtime_observation"},
        "proof fields differ")
    _require(evidence["version"] == HISTORICAL_CLIENT_FINAL_GATE_VERSION and evidence["original_record"] == record
             and evidence["original_record_sha256"] == canonical_sha256(record)
             and evidence["journal_id"] == journal_id and evidence["journal_events"] == events,
             "original record or single-claim history differs")
    error_type = evidence["execution_event"]["payload"]["evidence"]["error_type"]
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
    _require(failure["stage"] == "final_gate", "failure was not the reviewed client final gate")
    original = failure["original"]
    _keys(original, {"chain", "chain_truncated"}, "exception-chain fields differ")
    _require(original["chain_truncated"] is False and type(original["chain"]) is list
             and len(original["chain"]) == 1, "complete single exception required")
    error = original["chain"][0]
    manifest = _historical_client_final_gate_source(evidence["source"])
    _keys(error, {"type", "message", "sqlite_errorcode", "frames"}, "exception fields differ")
    _require(error_type == error["type"] == "OperationalError"
             and error["message"] == "database is locked"
             and type(error["sqlite_errorcode"]) is int and error["sqlite_errorcode"] == 261
             and error["frames"] == HISTORICAL_CLIENT_FINAL_GATE_FRAMES,
             "not the reviewed historical client final-gate SQLite failure")
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
             and process["launcher_sha256"] == HISTORICAL_CLIENT_FINAL_GATE_LAUNCHER
             and process["recipe_sha256"] == manifest.get("recipe_sha256")             and process["manifest_sha256"] == evidence["source"]["manifest_sha256"]
             and process["source_root"] == manifest.get("source_root")
             and type(process["source_files_verified"]) is int
             and process["source_files_verified"] == len(manifest["files"]),
             "original loaded-source observation differs from ended process")



# Separate original FAILED-session profile. It never converts a failed stop
# into a clean stop, changes manual authority or rearms a consumed send token.
FAILED_TERMINAL_CLIENT_FINAL_GATE_VERSION = "scout-reviewed-failed-terminal-client-final-gate-history-v1"
FAILED_TERMINAL_CLIENT_FINAL_GATE_LAUNCHER = '1d544928f99cacef1af6cb3adc194121e73122b9a646b633b861d0f072db5b47'
FAILED_TERMINAL_CLIENT_FINAL_GATE_MANIFEST = '88af48af7ce1b3b2bc1e50951ba627bebd046e0801f672a97fcf2b1250665bf7'
FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE = {'ml/intraday/scout_client.py': '99afce435afd32ef27d148d0c46872ac4b1d9a5f6f4044ec567abf0db5bb4025',
 'ml/intraday/scout_broker.py': '82b4ae2625ed5b112427861a147a428c9a98b0dfff15ef1001b320cecd14101d',
 'ml/intraday/scout_local_execution.py': '62b221ccaf536b1bf3e7d9379783ea5a4c0b905c77a590fee1e64d3852275e96',
 'ml/intraday/scout_journal.py': 'a9da4c9b5735bf9f446f0449be451c3a4c82cb5674802fef5151728053c6cead',
 'ml/intraday/local_forecast.py': '38191f6f3f08cf8dca66fd8b90c6659d4806d22caa8a4dc09d631876d1dffe33',
 'ml/intraday/live_session.py': '29235e868957a1ad037a1a6254f4a8e999ba94e94101a85f04c979d25bf95712',
 'app/services/schwab.py': '41a3cf8b3e5e412083b864afbdafb7056b44a9390051aaca087c4e271a704951',
 'app/services/schwab_stock_orders.py': '9989209f1431b02cb43943cb8923d9463bfabd20cb8af6808445d3fb22104261',
 'app/services/schwab_order_fields.py': '74ce11f6bad9e88601f6681cd19d62161659d0d1beecff4e36518ccd63421321',
 'ml/stock_trader/session.py': 'b7af001f9dc4bed9677cf66cd881e637b1f4d5c8a536dd3bb934b17d59019784',
 'ml/intraday/initial_gate_clearance.py': '2ff22f507cf98c88f055c5a7a149488616b43f8bbc053dab5794282225e2f503',
 'ml/intraday/pretransport_clearance.py': 'a1c23dfb00f0d68ee91c2cfbbd72b2dd85a22f23d1122f63d81c4a2fa1ca3c61',
 'ml/intraday/market_collection.py': 'c7d67d74dc7a060b92001d96a4e3a3fa30ff92ddfc601bc7e169beaab467f938',
 'ml/intraday/runtime.py': 'd1a5d4e246e18334b0a1025553a22e8864f6661d96f658eb1975cef6365342f2'}
FAILED_TERMINAL_CLIENT_FINAL_GATE_FRAMES = [{'file': 'live_session.py', 'function': 'context_work', 'line': 678},
 {'file': 'market_collection.py', 'function': 'run', 'line': 446},
 {'file': 'market_collection.py', 'function': 'collect', 'line': 438},
 {'file': 'market_collection.py', 'function': 'step', 'line': 480},
 {'file': '_api.py', 'function': '__enter__', 'line': 967},
 {'file': '_api.py', 'function': 'acquire', 'line': 1075},
 {'file': '_api.py', 'function': '_poll_until_acquired', 'line': 1235}]
FAILED_TERMINAL_EXECUTION_CONTROL_FRAMES = [{'file': 'live_session.py', 'function': 'run_manual', 'line': 719},
 {'file': 'scout_local_execution.py', 'function': 'step', 'line': 421},
 {'file': 'scout_local_execution.py', 'function': '_step', 'line': 438},
 {'file': 'scout_local_execution.py', 'function': '_capture', 'line': 375},
 {'file': 'scout_local_execution.py', 'function': '_controls', 'line': 86}]


def _failed_terminal_client_final_gate_source(value):
    """Accept only the full reviewed original source and exact manifest bytes."""
    _keys(value, {"manifest_text", "manifest_sha256", "files"}, "source proof fields differ")
    _require(type(value["manifest_text"]) is str, "manifest bytes missing")
    raw = value["manifest_text"].encode("utf-8")
    _require(value["manifest_sha256"] == FAILED_TERMINAL_CLIENT_FINAL_GATE_MANIFEST
             and _sha(raw) == FAILED_TERMINAL_CLIENT_FINAL_GATE_MANIFEST,
             "not the reviewed failed-terminal client final-gate manifest")
    manifest = json.loads(raw)
    _require(manifest.get("schema") == "scout-isolated-live-runtime-v1"
             and type(manifest.get("files")) is dict, "unsupported original manifest")
    _keys(value["files"], FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE,
          "complete reviewed failed-terminal client final-gate dependencies required")
    for name, digest in FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE.items():
        contents = value["files"][name]
        _require(type(contents) is str and _sha(contents.encode("utf-8")) == digest
                 and manifest["files"].get(name) == digest,
                 "unsupported or changed original source: " + name)
    return manifest

def _failed_terminal_client_final_gate_proof(evidence, record, *, journal_id, events, as_of):
    _keys(evidence, {"version", "original_record", "original_record_sha256", "journal_id", "journal_events",
        "execution_event", "execution_event_sha256", "stop_event", "stop_event_sha256", "process_end", "source",
        "runtime_observation", "terminal_events", "terminal_events_sha256",
        "terminal_checkpoint", "terminal_status_text", "terminal_status_sha256"},
        "proof fields differ")
    _require(evidence["version"] == FAILED_TERMINAL_CLIENT_FINAL_GATE_VERSION and evidence["original_record"] == record
             and evidence["original_record_sha256"] == canonical_sha256(record)
             and evidence["journal_id"] == journal_id and evidence["journal_events"] == events,
             "original record or single-claim history differs")
    error_type = evidence["execution_event"]["payload"]["evidence"]["error_type"]
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
    _require(failure["stage"] == "final_gate", "failure was not the reviewed client final gate")
    original = failure["original"]
    _keys(original, {"chain", "chain_truncated"}, "exception-chain fields differ")
    _require(original["chain_truncated"] is False and type(original["chain"]) is list
             and len(original["chain"]) == 1, "complete single exception required")
    error = original["chain"][0]
    manifest = _failed_terminal_client_final_gate_source(evidence["source"])
    _keys(error, {"type", "message", "sqlite_errorcode", "frames"}, "exception fields differ")
    _require(error_type == error["type"] == "OperationalError"
             and error["message"] == "database is locked"
             and type(error["sqlite_errorcode"]) is int and error["sqlite_errorcode"] == 261
             and error["frames"] == HISTORICAL_CLIENT_FINAL_GATE_FRAMES,
             "not the reviewed historical client final-gate SQLite failure")
    _require(instant(request["issued_at"]) <= instant(events[-2]["event"]["recorded_at"])
             <= instant(events[-1]["event"]["recorded_at"]) <= at
             and instant(payload["recorded_at"]) <= at
             and instant(request["valid_until"]) <= as_of and instant(permit["expires_at"]) <= as_of,
             "request/claim/failure/expiry clocks differ")
    # Authenticate the complete reviewed terminal interval. Later or omitted
    # operations cannot borrow this narrower ended-process contract.
    terminal = evidence["terminal_events"]
    kinds = ["market_context_worker_stopped", "target_scoring_retry", "local_execution_profile",
             "execution_retry", "forecast_worker_stopped", "manual_session_stopped"]
    _require(type(terminal) is list and len(terminal) == len(kinds)
             and canonical_sha256({"events": terminal}) == evidence["terminal_events_sha256"],
             "complete terminal interval differs")
    stamps = [_event(item, session=session, kind=kind) for item, kind in zip(terminal, kinds)]
    ids = [item["id"] for item in terminal]
    _require(ids == sorted(set(ids)) and stamps == sorted(stamps)
             and row["id"] < ids[0] and at < stamps[0] <= stamps[-1] <= as_of,
             "terminal interval chronology differs")
    fatal, score_retry, profile, refused, drained, stopped = terminal
    fatal_at, stopped_at = stamps[0], stamps[-1]
    diagnostic = {"schema_version": "intraday-symbol-exception-v1", "chain_truncated": False,
        "exception_chain": [{"type": "Timeout", "relation": "raised", "frames_truncated": False,
            "frames": FAILED_TERMINAL_CLIENT_FINAL_GATE_FRAMES}]}
    worker_error = {"worker": "market_context", "error_type": "Timeout",
        "message": "Inspect private collection status", "diagnostics": diagnostic}
    _require(fatal["symbol"] is None
             and fatal["payload"] == {"error_type": "Timeout", "diagnostics": diagnostic}
             and fatal["payload"]["diagnostics"]["chain_truncated"] is False
             and fatal["payload"]["diagnostics"]["exception_chain"][0]["frames_truncated"] is False,
             "not the reviewed fatal market-context timeout")
    _require(drained["symbol"] is None
             and drained["payload"] == {"error_type": "InterruptedError",
                 "message": "Manual session stopped; acquisition recovery retained"},
             "not the reviewed forecast drain interruption")
    _require(stopped == evidence["stop_event"]
             and canonical_sha256(stopped) == evidence["stop_event_sha256"] and stopped["symbol"] is None
             and stopped["payload"] == {"pending_order_identity_preserved": True,
                 "stop_reason": "worker_failure", "worker_errors": [worker_error]}
             and stopped["payload"]["pending_order_identity_preserved"] is True,
             "complete reviewed failed original terminal required")
    _keys(score_retry["payload"], {"error_type", "forecast_id", "message"}, "scoring retry fields differ")
    _require(type(score_retry["symbol"]) is str and bool(score_retry["symbol"])
             and score_retry["payload"]["error_type"] == "ContractError"
             and score_retry["payload"]["message"] == "History reference lies outside this feed's latched eligible day"
             and type(score_retry["payload"]["forecast_id"]) is str
             and len(score_retry["payload"]["forecast_id"]) == 64
             and all(c in "0123456789abcdef" for c in score_retry["payload"]["forecast_id"]),
             "not the reviewed scoring retry")
    control_diagnostic = {"schema_version": "intraday-symbol-exception-v1", "chain_truncated": False,
        "exception_chain": [{"type": "ContractError", "relation": "raised", "frames_truncated": False,
            "frames": FAILED_TERMINAL_EXECUTION_CONTROL_FRAMES}]}
    detail = refused["payload"]
    _keys(detail, {"diagnostics", "error_type", "forecast_id", "message", "retry_after_seconds"},
          "manual-control refusal fields differ")
    _require(type(refused["symbol"]) is str and bool(refused["symbol"])
             and detail["error_type"] == "ContractError" and detail["diagnostics"] == control_diagnostic
             and detail["diagnostics"]["chain_truncated"] is False
             and detail["diagnostics"]["exception_chain"][0]["frames_truncated"] is False
             and detail["message"] == "Current manual Scout session authority required"
             and type(detail["retry_after_seconds"]) in (int, float) and detail["retry_after_seconds"] == 3
             and type(detail["forecast_id"]) is str and len(detail["forecast_id"]) == 64
             and all(c in "0123456789abcdef" for c in detail["forecast_id"]),
             "not the reviewed pre-capture manual-control refusal")
    prof = profile["payload"]
    _keys(prof, {"context_caches", "cpu_scope", "error_type", "forecast_id", "manual_session_id",
        "observer_errors", "phases", "process_cpu_scope", "schema_version", "status", "symbol", "target_close", "worker"},
        "terminal execution profile fields differ")
    _require(profile["symbol"] == refused["symbol"] == prof["symbol"]
             and prof["forecast_id"] == detail["forecast_id"] and prof["manual_session_id"] == session
             and prof["schema_version"] == "intraday-local-execution-profile-v1"
             and prof["status"] == "error" and prof["error_type"] == "ContractError"
             and prof["context_caches"] == [] and prof["observer_errors"] == []
             and prof["cpu_scope"] == "current_thread" and prof["process_cpu_scope"] == "all_process_threads"
             and instant(prof["target_close"]) > stopped_at, "terminal execution profile differs")
    _keys(prof["worker"], {"native_thread_id", "thread_name"}, "terminal execution worker differs")
    _require(prof["worker"]["thread_name"] == "MainThread"
             and type(prof["worker"]["native_thread_id"]) is int and prof["worker"]["native_thread_id"] > 0,
             "terminal execution worker differs")
    phases = prof["phases"]
    expected_phases = [("snapshot_validation", "success"), ("chronology_check", "success"),
                       ("initial_capture", "error"), ("total_step", "error")]
    _require(type(phases) is list and len(phases) == len(expected_phases), "terminal execution phases differ")
    for phase, (name, state) in zip(phases, expected_phases):
        fields = {"phase", "process_cpu_seconds", "status", "thread_cpu_seconds", "wall_seconds"}
        _keys(phase, fields | ({"error_type"} if state == "error" else set()), "terminal execution phase fields differ")
        _require(phase["phase"] == name and phase["status"] == state
                 and (state != "error" or phase["error_type"] == "ContractError")
                 and all(type(phase[key]) in (int, float) and math.isfinite(phase[key]) and phase[key] >= 0
                         for key in ("process_cpu_seconds", "thread_cpu_seconds", "wall_seconds")),
                 "terminal execution phase differs")
    _require(type(evidence["terminal_status_text"]) is str
             and _sha(evidence["terminal_status_text"].encode("utf-8")) == evidence["terminal_status_sha256"],
             "terminal status bytes changed")
    status = json.loads(evidence["terminal_status_text"])
    _keys(status, {"automatic_stop_at", "broker_orders_automatically_cancelled", "evidence_paths",
        "heartbeat_at", "manual_start", "pc", "pid", "process_create_time", "schema_version",
        "scout_admission", "session_id", "started_at", "status", "stop_reason", "wake_at",
        "worker_errors", "worker_threads"}, "terminal status fields differ")
    _require(status["status"] == "FAILED" and status["stop_reason"] == "worker_failure"
             and status["worker_errors"] == [worker_error]
             and status["manual_start"] is True and status["pc"] == "Scout"
             and status["broker_orders_automatically_cancelled"] is False
             and status["schema_version"] == "manual-scout-15m-session-v1"
             and status["scout_admission"] == "broker-local" and status["session_id"] == session
             and fatal_at <= instant(status["heartbeat_at"]) <= stopped_at
             and instant(status["started_at"]) <= instant(request["issued_at"])
             and instant(status["wake_at"]) <= instant(status["started_at"])
             and instant(status["automatic_stop_at"]) > stopped_at,
             "terminal FAILED status does not match reviewed failure")
    threads = status["worker_threads"]
    roles = {"forecast": "intraday-manual-forecast", "scoring": "intraday-manual-scoring",
             "minute_data": "intraday-manual-minute-data", "market_context": "intraday-manual-market-context"}
    _require(type(threads) is list and len(threads) == len(roles), "terminal worker identity differs")
    for thread in threads:
        _keys(thread, {"native_thread_id", "role", "thread_name"}, "terminal worker fields differ")
        _require(thread["role"] in roles and thread["thread_name"] == roles[thread["role"]]
                 and type(thread["native_thread_id"]) is int and thread["native_thread_id"] > 0,
                 "terminal worker identity differs")
    _require({thread["role"] for thread in threads} == set(roles)
             and len({thread["native_thread_id"] for thread in threads}) == len(roles),
             "terminal worker identities repeat")
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
    _require(status["pid"] == ended["pid"] and type(status["pid"]) is int
             and status["process_create_time"] == ended["process_created_at_epoch"]
             and type(status["process_create_time"]) in (int, float)
             and ended["process_created_at_epoch"] <= instant(status["started_at"]).timestamp(),
             "terminal status and ended process differ")
    checkpoint = evidence["terminal_checkpoint"]
    _keys(checkpoint, {"session_id", "observed_at", "query_only", "last_own_event_id",
        "terminal_event_count", "terminal_events_sha256"}, "terminal checkpoint fields differ")
    _require(checkpoint["session_id"] == session and checkpoint["query_only"] is True
             and type(checkpoint["last_own_event_id"]) is int and checkpoint["last_own_event_id"] == stopped["id"]
             and type(checkpoint["terminal_event_count"]) is int and checkpoint["terminal_event_count"] == len(terminal)
             and checkpoint["terminal_events_sha256"] == evidence["terminal_events_sha256"]
             and instant(ended["verified_absent_at"]) <= instant(checkpoint["observed_at"]) <= as_of,
             "terminal checkpoint does not seal ended original session")
    observed = evidence["runtime_observation"]
    _keys(observed, {"at", "session_id", "process"}, "original runtime observation fields differ")
    process = observed["process"]
    _keys(process, {"verified", "pid", "created_at_epoch", "module", "binding", "entrypoint_kind",
        "launcher_path", "launcher_sha256", "manifest_path", "manifest_sha256", "source_root",
        "source_files_verified", "recipe_sha256"}, "original verified process fields differ")
    _require(observed["session_id"] == session and at <= instant(observed["at"]) <= fatal_at
             and process["verified"] is True and process["binding"] == "exact_birth"
             and process["module"] == "ml.intraday.live_session"
             and process["entrypoint_kind"] == "reviewed_isolated_live_launcher"
             and process["pid"] == ended["pid"]
             and process["created_at_epoch"] == ended["process_created_at_epoch"]
             and process["launcher_sha256"] == FAILED_TERMINAL_CLIENT_FINAL_GATE_LAUNCHER
             and process["recipe_sha256"] == manifest.get("recipe_sha256")             and process["manifest_sha256"] == evidence["source"]["manifest_sha256"]
             and process["source_root"] == manifest.get("source_root")
             and type(process["source_files_verified"]) is int
             and process["source_files_verified"] == len(manifest["files"]),
             "original loaded-source observation differs from ended process")

def _validate_proof(evidence, record, *, journal_id, events, as_of, version=None):
    """Versions are separate contracts; never reinterpret historical diagnostics."""
    _require(type(evidence) is dict, "proof must be an object")
    if version is not None:
        _require(evidence.get("version") == version, "receipt and proof versions differ")
    if evidence.get("version") == VERSION:
        return _proof(evidence, record, journal_id=journal_id, events=events, as_of=as_of)
    if evidence.get("version") == HISTORICAL_CLIENT_FINAL_GATE_VERSION:
        return _historical_client_final_gate_proof(evidence, record,
            journal_id=journal_id, events=events, as_of=as_of)
    _require(evidence.get("version") == FAILED_TERMINAL_CLIENT_FINAL_GATE_VERSION,
             "unsupported proof version")
    return _failed_terminal_client_final_gate_proof(evidence, record,
        journal_id=journal_id, events=events, as_of=as_of)


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
        _require(canonical_sha256(payload) == envelope["sha256"] and payload["version"] in {VERSION, HISTORICAL_CLIENT_FINAL_GATE_VERSION, FAILED_TERMINAL_CLIENT_FINAL_GATE_VERSION}
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
        _validate_proof(json.loads(raw), record, journal_id=journal_id, events=events, as_of=created,
                        version=payload["version"])
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
    _validate_proof(evidence, record, journal_id=journal_id, events=events, as_of=now)
    for name, contents in evidence["source"]["files"].items():
        _require(_sha(_read(Path(repository) / name)) == _sha(contents.encode("utf-8")), "reviewed original source changed: " + name)
    path = _path(books, record)
    with books.locked():
        books.inspect()
        journal_id, events = _audit(books, journal, record)
        _validate_proof(evidence, record, journal_id=journal_id, events=events, as_of=now)
        if path.exists():
            return load_clearance(books, journal, record, as_of=now)
        path.parent.mkdir(exist_ok=True)
        digest = _sha(raw)
        _write_once(path.parent / (digest + ".proof.json"), raw)
        payload = dict(version=evidence["version"], created_at=now.isoformat(), review_reference=review_reference,
            evidence_sha256=digest, original_record_sha256=canonical_sha256(record), journal_id=journal_id,
            historical_execution="UNKNOWN", commitment=COMMITMENT)
        envelope = dict(payload=payload, sha256=canonical_sha256(payload))
        _write_once(path, json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return load_clearance(books, journal, record, as_of=now)
