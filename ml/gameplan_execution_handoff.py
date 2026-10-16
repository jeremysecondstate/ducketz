"""Private, optional delivery of Atlas's saved Gameplan display to Scout.

Only the display JSON moves. This module never opens a ledger, captures an
account, or operates a trader. Immutable packets retain the original JSON bytes;
their selector may advance independently of the nightly Gameplan selection.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from filelock import FileLock, Timeout

VERSION = "gameplan-execution-display-v1"
MAX_SUMMARY = 8 * 1024 * 1024
MAX_PACKET = 12 * 1024 * 1024
SUMMARY = Path("state/gameplan-execution-reports")
STATE = Path("state/gameplan-execution-display-sync")
DISPLAY = Path("display/gameplan-execution-stats/v1")
REPORT_FIELDS = {"schema_version", "session_date", "timezone", "generated_at", "state", "message",
                 "last_broker_observation_at", "counts", "orders", "coverage", "provenance"}
ORDER_FIELDS = {"order_ref", "allocation_ref", "forecast_ref", "symbol", "side", "requested_quantity",
                "filled_quantity", "filled_quantity_in_session", "average_fill_price", "fill_details_complete",
                "fills", "status", "lifecycle_status", "submission_state", "submitted_at", "last_observed_at",
                "recorded_at", "session_scope", "retry_parent_ref", "retry_child_refs", "cancel_requested_at",
                "timeout_requested_at", "reasons", "is_catchup"}
COVERAGE_TEXT = {"start", "end", "scope", "population", "carryover", "outcomes", "distribution",
                 "completeness", "attribution", "logical_intentions"}
COVERAGE_NUMBERS = {"stale_after_seconds", "excluded_unattributed_reservations", "orders_without_submission_date",
                    "recorded_orders"}
PROVENANCE_FIELDS = {"source", "reader", "private", "broker_requests", "query_snapshot", "read_started_at",
                     "read_completed_at", "source_content_sha256"}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Invalid display digest")
    return value


def _bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _object(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate display field")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite display value")))
    if not isinstance(value, dict):
        raise ValueError("Expected display object")
    return value


def _read(path, limit=MAX_SUMMARY):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Display file exceeds its limit")
    return raw


def _write(path, raw, *, replace=False):
    path = Path(path)
    if path.exists():
        if _read(path, MAX_PACKET) == raw:
            return
        if not replace:
            raise ValueError("Immutable display history changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".display-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or value.startswith(("\\\\", "//")):
        raise ValueError("Display bindings require local absolute paths")
    return Path(value).resolve()


def _default_config_path():
    return Path(__file__).resolve().parents[1] / "scratch/nightly-workflow/exchange-config.json"


def _config(path, datastore):
    config = _object(_read(path, 65536))
    known = {"schema_version", "actor", "workflow_config", "local_profile", "coordination_active", "exchange_root",
             "state_root", "account_scope_sha256", "owners", "private_exchange_authorized", "account_config"}
    if (set(config) - known or config.get("schema_version") != "nightly-exchange-config-v1"
            or config.get("actor") not in {"Atlas", "Scout"} or config.get("private_exchange_authorized") is not True):
        raise ValueError("Invalid private display binding")
    _digest(config["account_scope_sha256"])
    profile = _object(_read(_absolute(config["local_profile"]), 65536))
    checkout = _absolute(profile["checkout"])
    if (profile.get("actor") != config["actor"]
            or profile.get("machine") != {"Atlas": "pc-original", "Scout": "pc-new"}[config["actor"]]
            or _absolute(config["local_profile"]) != checkout / "scratch/cross-pc/local-profile.json"):
        raise ValueError("Display actor differs from local profile")
    workflow = _object(_read(_absolute(config["workflow_config"]), 65536))
    if _absolute(workflow["datastore"]) != datastore:
        return None
    if workflow.get("actor") != config["actor"] or _absolute(workflow["repository"]) != checkout:
        raise ValueError("Display datastore differs from local binding")
    exchange = _absolute(config["exchange_root"])
    parts = [part.casefold() for part in exchange.parts]
    if parts[-2:] != ["ducketz-nightly-exchange", "v1"] or "codexstore" not in parts[:-2]:
        raise ValueError("Display transport must stay in the private CODEXSTORE exchange")
    return config, checkout, exchange


def _report(raw, session):
    # Reuse the existing display-only validator; importing it opens no ledger.
    from app.ui.gameplan_execution_data import _validate, COUNT_FIELDS, VERSION as REPORT_VERSION
    report = _object(raw)
    generated, _ = _validate(report, session)
    if set(report) != REPORT_FIELDS:
        raise ValueError("Display summary contains unknown fields")
    provenance, coverage = report["provenance"], report["coverage"]
    if (set(provenance) - PROVENANCE_FIELDS
            or provenance.get("source") != "state/gameplan-trader/holdings.sqlite3"
            or provenance.get("reader") != REPORT_VERSION or provenance.get("private") is not True
            or provenance.get("broker_requests") is not False
            or any(not isinstance(value, (str, bool, type(None))) for value in provenance.values())):
        raise ValueError("Display provenance contains unsupported fields")
    if "source_content_sha256" in provenance:
        _digest(provenance["source_content_sha256"])
    if set(coverage) - (COVERAGE_TEXT | COVERAGE_NUMBERS | {"reasons", "stale", "stale_order_refs"}):
        raise ValueError("Display coverage contains unknown fields")
    for key, value in coverage.items():
        if key in COVERAGE_TEXT and not isinstance(value, str):
            raise ValueError("Invalid display coverage text")
        if key in COVERAGE_NUMBERS and (type(value) is not int or value < 0):
            raise ValueError("Invalid display coverage count")
        if key == "stale" and type(value) is not bool:
            raise ValueError("Invalid display coverage state")
        if key in {"reasons", "stale_order_refs"} and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)):
            raise ValueError("Invalid display coverage details")
        if key == "stale_order_refs":
            for reference in value:
                _digest(reference)
    if report["counts"] is not None and set(report["counts"]) != set(COUNT_FIELDS):
        raise ValueError("Display totals contain unknown fields")
    for order in report["orders"]:
        if set(order) - ORDER_FIELDS or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", order["symbol"]) is None:
            raise ValueError("Display order contains unknown fields")
        for name in ("order_ref", "allocation_ref", "forecast_ref", "retry_parent_ref"):
            if order.get(name) is not None:
                _digest(order[name])
        for reference in order["retry_child_refs"]:
            _digest(reference)
        for fill in order["fills"]:
            if set(fill) != {"quantity", "price", "executed_at"}:
                raise ValueError("Display fill contains unknown fields")
        for name in ("recorded_at", "cancel_requested_at", "timeout_requested_at"):
            value = order.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError("Invalid display order timestamp")
        for name in ("fill_details_complete", "is_catchup"):
            if name in order and type(order[name]) is not bool:
                raise ValueError("Invalid display order flag")
        if "filled_quantity_in_session" in order and type(order["filled_quantity_in_session"]) not in {int, float}:
            raise ValueError("Invalid display fill quantity")
    return report, generated


def _scan(raw, report, checkout):
    from tools.cross_pc.artifact_publish import _scan_bytes, _secret_values
    secrets = _secret_values(checkout / ".env")
    _scan_bytes(raw, secrets)
    # JSON escaping must not conceal a credential embedded in a text value.
    def strings(value):
        if isinstance(value, str):
            _scan_bytes(value.encode(), secrets)
        elif isinstance(value, dict):
            for child in value.values():
                strings(child)
        elif isinstance(value, list):
            for child in value:
                strings(child)
    strings(report)


def _selected(folder, config, session):
    selector_raw = _read(folder / "selection.json", 4096)
    selector = _object(selector_raw)
    if (set(selector) != {"schema_version", "packet_sha256", "packet_bytes"}
            or selector["schema_version"] != VERSION or type(selector["packet_bytes"]) is not int
            or not 0 < selector["packet_bytes"] <= MAX_PACKET):
        raise ValueError("Invalid display selector")
    digest = _digest(selector["packet_sha256"])
    packet_raw = _read(folder / "packets" / (digest + ".json"), MAX_PACKET)
    if len(packet_raw) != selector["packet_bytes"] or _sha(packet_raw) != digest:
        raise ValueError("Display packet digest differs")
    packet = _object(packet_raw)
    if (set(packet) != {"schema_version", "producer", "recipient", "session_date", "account_scope_sha256",
                       "summary_sha256", "summary_base64", "generated_at"}
            or packet["schema_version"] != VERSION or packet["producer"] != "Atlas" or packet["recipient"] != "Scout"
            or packet["session_date"] != session or packet["account_scope_sha256"] != config["account_scope_sha256"]):
        raise ValueError("Display packet identity differs")
    raw = base64.b64decode(packet["summary_base64"], validate=True)
    if len(raw) > MAX_SUMMARY or _sha(raw) != _digest(packet["summary_sha256"]):
        raise ValueError("Display summary digest differs")
    report, generated = _report(raw, session)
    if packet["generated_at"] != report["generated_at"] or report["counts"] is None:
        raise ValueError("Display packet does not contain saved execution evidence")
    if _read(folder / "selection.json", 4096) != selector_raw:
        raise OSError("Display selection is still syncing")
    return raw, report, generated, digest, packet_raw


def _older(raw, generated, previous_raw, previous_generated):
    if generated < previous_generated:
        return True
    if generated == previous_generated and raw != previous_raw:
        raise ValueError("Different execution display bytes claim the same generation")
    return False


def _publish(root, session, config, checkout, folder, state):
    source = root / SUMMARY / session / "summary.json"
    if not source.is_file():
        return {"status": "NO_SOURCE"}
    raw = _read(source)
    report, generated = _report(raw, session)
    if report["counts"] is None or report["state"] in {"missing", "unreadable"}:
        return {"status": "NO_SOURCE"}
    _scan(raw, report, checkout)
    if (folder / "selection.json").exists():
        old_raw, _, old_generated, digest, _ = _selected(folder, config, session)
        if raw == old_raw:
            return {"status": "UNCHANGED", "summary_sha256": _sha(raw)}
        if _older(raw, generated, old_raw, old_generated):
            return {"status": "STALE_IGNORED"}
    packet = {"schema_version": VERSION, "producer": "Atlas", "recipient": "Scout", "session_date": session,
              "account_scope_sha256": config["account_scope_sha256"], "summary_sha256": _sha(raw),
              "summary_base64": base64.b64encode(raw).decode("ascii"), "generated_at": report["generated_at"]}
    packet_raw = _bytes(packet)
    digest = _sha(packet_raw)
    if _read(source) != raw:
        return {"status": "SOURCE_CHANGED", "error": "Saved execution display changed during export; retry its latest bytes"}
    _write(state / "packets" / (digest + ".json"), packet_raw)
    _write(folder / "packets" / (digest + ".json"), packet_raw)
    _write(folder / "selection.json", _bytes({"schema_version": VERSION, "packet_sha256": digest,
                                             "packet_bytes": len(packet_raw)}), replace=True)
    return {"status": "PUBLISHED", "summary_sha256": _sha(raw)}


def _receive(root, session, config, folder, state):
    if not (folder / "selection.json").is_file():
        return {"status": "NOT_AVAILABLE"}
    raw, report, generated, digest, packet_raw = _selected(folder, config, session)
    destination = root / SUMMARY / session / "summary.json"
    previous = _read(destination) if destination.exists() else None
    receipt_path = state / "receipt.json"
    receipt = _object(_read(receipt_path)) if receipt_path.is_file() else None
    if receipt is not None:
        cached = _read(state / "received" / (_digest(receipt["summary_sha256"]) + ".json"))
        if _sha(cached) != receipt["summary_sha256"]:
            raise ValueError("Imported display cache changed")
        _, cached_generated = _report(cached, session)
        if _older(raw, generated, cached, cached_generated):
            return {"status": "STALE_IGNORED"}
        if previous == raw and receipt["summary_sha256"] == _sha(raw):
            return {"status": "UNCHANGED", "summary_sha256": _sha(raw)}
    if previous is not None and previous != raw:
        try:
            previous_report, previous_generated = _report(previous, session)
        except (ValueError, KeyError, TypeError, UnicodeError):
            previous_report = None
        # A locally generated missing-ledger placeholder has no observations and
        # must not outrank the real Atlas report by its later generation clock.
        if previous_report is not None and previous_report["counts"] is not None:
            if _older(raw, generated, previous, previous_generated):
                return {"status": "STALE_IGNORED"}
        _write(state / "history" / (_sha(previous) + ".json"), previous)
    _write(state / "packets" / (digest + ".json"), packet_raw)
    _write(state / "received" / (_sha(raw) + ".json"), raw)
    _write(destination, raw, replace=True)
    imported = {"schema_version": VERSION, "producer": "Atlas", "recipient": "Scout", "session_date": session,
                "account_scope_sha256": config["account_scope_sha256"], "summary_sha256": _sha(raw),
                "packet_sha256": digest, "generated_at": report["generated_at"],
                "last_broker_observation_at": report["last_broker_observation_at"],
                "imported_at": datetime.now(timezone.utc).isoformat()}
    _write(receipt_path, _bytes(imported), replace=True)
    return {"status": "IMPORTED", "summary_sha256": _sha(raw)}


def sync_execution_summary(datastore_root, session, *, config_path=None, receive_only=False):
    """Best-effort private display exchange; all ordinary failures stay local.

    UI callers use ``receive_only=True``: Atlas then performs no publication.
    A missing binding is a cheap no-op for standalone installs and test roots.
    No returned status is a trading or nightly completion prerequisite.
    """
    state = None
    try:
        path = Path(config_path) if config_path is not None else _default_config_path()
        if not path.is_file():
            return {"status": "NOT_CONFIGURED"}
        if date.fromisoformat(session).isoformat() != session:
            raise ValueError("Invalid display session")
        root = Path(datastore_root).resolve()
        binding = _config(path, root)
        if binding is None:
            return {"status": "NOT_APPLICABLE"}
        config, checkout, exchange = binding
        if receive_only and config["actor"] != "Scout":
            return {"status": "NOT_APPLICABLE"}
        state = root / STATE / session
        state.mkdir(parents=True, exist_ok=True)
        if not exchange.is_dir():
            raise OSError("Private display transport is unavailable")
        folder = exchange / DISPLAY / session / "atlas"
        with FileLock(str(state / "sync.lock"), timeout=0):
            result = (_publish(root, session, config, checkout, folder, state) if config["actor"] == "Atlas"
                      else _receive(root, session, config, folder, state))
    except Timeout:
        result = {"status": "BUSY"}
    except OSError:
        result = {"status": "UNAVAILABLE", "error": "Private display files are unavailable or still syncing"}
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        result = {"status": "INVALID", "error": "Private execution display validation failed"}
    if state is not None:
        _record_status(state, result)
    return result


def _record_status(state, result):
    """Keep a concise local outcome without turning diagnostics into a gate."""
    try:
        path = state / "status.json"
        previous = _object(_read(path, 4096)) if path.is_file() else {}
        # A completed operation followed by identical bytes remains complete;
        # unchanged UI refreshes should not continually rewrite diagnostics.
        if (result.get("status") == "UNCHANGED" and previous.get("summary_sha256") == result.get("summary_sha256")
                and previous.get("status") in {"PUBLISHED", "IMPORTED", "UNCHANGED"}):
            return
        if {key: value for key, value in previous.items() if key != "recorded_at"} == result:
            return
        _write(path, _bytes({**result, "recorded_at": datetime.now(timezone.utc).isoformat()}), replace=True)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        pass
