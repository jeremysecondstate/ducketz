"""Read saved Atlas execution prerequisites without enabling or starting trading.

This report separates a completed joint plan, installed execution setup and a
same-date manual session. Saved ownership is never current broker reconciliation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
import uuid

import pandas as pd

from ml.account_gameplan.config import CONFIG, load_account_config, verify_cutover
from ml.artifacts import file_checksum, utc_timestamp
from ml.joint_capital_adoption import POINTERS, read_accepted_joint_plan
from ml.nightly_joint_readiness import FIELDS, VERSION as SELECTION_VERSION, _receipt_identity
from ml.stock_trader.horizon_ledger import HorizonLedger, LEDGER_VERSION, _OPEN
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
from tools.nightly_ownership import LEDGER, _pins, _saved_held


VERSION = "gameplan-execution-readiness-v1"
SESSION = Path("state/independent-stock-trader/session-status.json")
SESSION_LOCK = Path("locks/independent-stock-session.lock")
ACTION = {
    "ACCOUNT_NOT_CONFIGURED": "Install the reviewed Atlas account binding.",
    "ACCOUNT_BINDING_INVALID": "Review the local account binding without changing its identity or universes.",
    "ACCOUNT_NOT_ATLAS_COORDINATOR": "Use Atlas for the human's manual trader start.",
    "ACCOUNT_CUTOVER_PREPARING": "Complete the reviewed native ledger installation and local activation transition.",
    "CUTOVER_RECEIPT_INVALID": "Repair the reviewed local cutover evidence before manual startup.",
    "JOINT_HANDOFF_MISSING": "Complete and verify this action date's Atlas handoff.",
    "JOINT_HANDOFF_INVALID": "Review this action date's pinned handoff and accepted artifact bytes.",
    "NATIVE_LEDGER_MISSING": "Install the reviewed combined native ledger, preserving both original producer ledgers.",
    "NATIVE_LEDGER_INVALID": "Review the native ledger metadata; do not replace missing ownership with zero inventory.",
    "NATIVE_LEDGER_UNION_INCOMPLETE": "Complete the reviewed native ownership migration for both producer universes.",
    "NATIVE_LEDGER_BLOCKED": "Resolve the recorded native ownership blocks through the reviewed transition.",
    "NATIVE_LEDGER_BASELINE_NOT_READY": "Complete native reconciliation; a saved ledger read is not broker reconciliation.",
    "NATIVE_PENDING_RESERVATIONS_PREVENT_CUTOVER": "Resolve native pending reservations before account cutover.",
    "SAVED_EXECUTION_INSTRUCTIONS_NOT_READY": "Review the selected plan's local deployment and saved trading instructions.",
}


def _json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or str(value).startswith(("\\\\", "//")):
        raise ValueError("An explicit local absolute path is required")
    return path.resolve()


def _stamp(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("An aware saved timestamp is required")
    return stamp.tz_convert("UTC")


def _handoff(root, day, account):
    selected = root / "ml/nightly-joint-readiness-by-date" / day / "run.json"
    if not selected.is_file():
        return {"ready": False, "reason": "JOINT_HANDOFF_MISSING"}, {}
    pin = _json(selected)
    if (set(pin) != FIELDS or pin["schema_version"] != SELECTION_VERSION
            or pin["local_actor"] != "atlas" or pin["action_date"] != day):
        raise ValueError("Handoff selection differs")
    receipt_path = _absolute(pin["receipt_path"])
    if file_checksum(receipt_path) != pin["receipt_sha256"]:
        raise ValueError("Handoff receipt changed")
    receipt = _json(receipt_path)
    _receipt_identity(receipt, actor="atlas", action_date=day, review_session=pin["review_session"])
    if receipt["completion_id"] != pin["completion_id"] or _absolute(receipt["receipt_path"]) != receipt_path:
        raise ValueError("Handoff completion differs")
    accepted_pointer = root / POINTERS / day / "run.json"
    accepted_pointer_sha = file_checksum(accepted_pointer)
    accepted = read_accepted_joint_plan(root, day)
    if accepted is None:
        raise ValueError("Accepted plan missing")
    plan, binding, run = accepted
    evidence = receipt["ui_evidence"]
    if (binding["local_actor"] != "atlas" or binding["executor_owner"] != "atlas"
            or binding["account_scope_sha256"] != receipt["account_scope_sha256"]
            or binding["owner_packages"] != receipt["owner_packages"]
            or plan["plan_sha256"] != evidence["plan_sha256"] or _absolute(evidence["plan_run"]) != run):
        raise ValueError("Accepted plan differs from handoff")
    if account is not None and (account.account_fingerprint != binding["account_scope_sha256"]
            or set(account.participants["pc-original"]) != set(binding["owner_universes"]["atlas"])
            or set(account.participants["pc-new"]) != set(binding["owner_universes"]["scout"])):
        raise ValueError("Account differs from handoff")
    stats_run = _absolute(evidence["stats_run"])
    if stats_run.parent != root / "ml/gameplan-actuals-review-runs":
        raise ValueError("Stats path differs")
    paths = (run / "accepted-plan.json", run / "joint-plan.json", stats_run / "receipt.json",
             stats_run / "manifest.json", stats_run / "forecast-results.parquet")
    frozen = {str(path): file_checksum(path) for path in paths}
    if evidence["files"] != frozen:
        raise ValueError("Completed UI bytes changed")
    if (file_checksum(stats_run / "receipt.json") != evidence["stats_receipt_sha256"]
            or file_checksum(receipt_path) != pin["receipt_sha256"] or _json(selected) != pin):
        raise ValueError("Completion evidence changed during verification")
    frozen.update({str(selected): file_checksum(selected), str(receipt_path): pin["receipt_sha256"],
                   str(accepted_pointer): accepted_pointer_sha})
    if any(file_checksum(Path(path)) != expected for path, expected in frozen.items()):
        raise ValueError("Selected handoff changed during inspection")
    return {"ready": True, "status": receipt["status"], "action_date": day,
            "completion_id": pin["completion_id"]}, frozen


def _ledger(root, account):
    path = root / LEDGER
    if not path.is_file():
        return {"available": False, "reason": "NATIVE_LEDGER_MISSING"}
    pins = _pins(path)
    # Even SQLite read-only mode can change source SHM read marks. Copy all
    # sidecars and query only the private temporary copy, including saved WAL.
    with TemporaryDirectory(prefix="gameplan-readiness-") as temporary:
        copied = Path(temporary) / "holdings.sqlite3"
        for suffix, expected in pins.items():
            if expected is not None:
                target = Path(str(copied) + suffix)
                shutil.copyfile(Path(str(path) + suffix), target)
                if file_checksum(target) != expected:
                    raise ValueError("Native ledger changed during copy")
        if _pins(path) != pins:
            raise ValueError("Native ledger changed during copy")
        with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            if metadata.get("version") != LEDGER_VERSION or metadata.get("account") != account.account_fingerprint:
                raise ValueError("Native ledger account or schema differs")
            baseline = db.execute("SELECT observed_at,ready,payload,id,reasons FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
            blocks = db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM reservations WHERE status IN (" +
                                 ",".join("?" for _ in _OPEN) + ")", _OPEN).fetchone()[0]
            if baseline is None:
                result = {"available": True, "union_coverage": False, "saved_baseline_ready": False,
                          "saved_baseline_at": None, "covered_symbol_count": 0}
            else:
                payload = json.loads(baseline[2])
                held = payload["held_shares"]
                if not isinstance(held, dict) or payload.get("account_fingerprint") != account.account_fingerprint:
                    raise ValueError("Native baseline binding differs")
                stamp = _stamp(baseline[0])
                if (_stamp(payload["observed_at"]) != stamp or payload.get("snapshot_id") != baseline[3]
                        or baseline[1] not in (0, 1) or not isinstance(json.loads(baseline[4]), list)
                        or (baseline[1] == 1 and json.loads(baseline[4]) != [])):
                    raise ValueError("Native baseline timestamp differs")
                for quantity in held.values():
                    _saved_held(quantity)
                native = HorizonLedger._snapshot(db)
                totals = dict.fromkeys(account.symbols, 0)
                for allocation in native.allocations:
                    if (allocation.account_fingerprint != account.account_fingerprint or allocation.symbol not in totals
                            or allocation.horizon not in {"1h", "4h", "1d", "1w"}
                            or allocation.status not in {"ACTIVE", "CLOSED"} or allocation.filled_shares < 0
                            or allocation.reserved_sell_shares < 0 or allocation.reserved_buy_shares < 0
                            or allocation.reserved_sell_shares > allocation.filled_shares
                            or _stamp(allocation.target_end) <= _stamp(allocation.target_start)
                            or (allocation.status == "CLOSED" and allocation.filled_shares)):
                        raise ValueError("Native saved ownership is inconsistent")
                    totals[allocation.symbol] += allocation.filled_shares
                if any((symbol not in held and quantity > 0)
                       or (symbol in held and quantity > _saved_held(held[symbol]))
                       for symbol, quantity in totals.items()):
                    raise ValueError("Native ownership exceeds its saved baseline")
                result = {"available": True, "union_coverage": set(held) == set(account.symbols),
                          "saved_baseline_ready": baseline[1] == 1, "saved_baseline_at": stamp.isoformat(),
                          "covered_symbol_count": len(held)}
    if _pins(path) != pins:
        raise ValueError("Native ledger changed during inspection")
    return {**result, "expected_symbol_count": len(account.symbols), "blocked_symbol_count": blocks,
            "pending_reservation_count": pending, "current_broker_reconciliation_performed": False}


def _process_probe(pid):
    import psutil
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            return {"alive": process.is_running() and process.status() != psutil.STATUS_ZOMBIE,
                    "created_at": pd.Timestamp(process.create_time(), unit="s", tz="UTC"),
                    "argv": process.cmdline(), "executable": process.exe()}
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {"alive": None}


def _session(root, day, now, probe):
    path = root / SESSION
    absent = {"running": False, "status": "NO_VERIFIED_SAME_DATE_MANUAL_SESSION"}
    if not path.is_file():
        return absent
    saved = _json(path)
    if (saved.get("schema_version") != "independent-stock-session-status-v1"
            or saved.get("action_date") != day or saved.get("execute") is not True
            or saved.get("sizing_policy") != GAMEPLAN_SIZING_POLICY):
        return {**absent, "status": "SAVED_SESSION_NOT_FOR_REQUESTED_ACTION_DATE_OR_POLICY"}
    status = saved.get("status")
    if status not in {"RUNNING", "SLEEPING_UNTIL_OPEN"}:
        # Do not emit arbitrary saved status text or counters from private files.
        return {**absent, "status": "SAME_DATE_SESSION_STOPPED"}
    stamp, started = _stamp(saved["heartbeat_at"]), _stamp(saved["started_at"])
    if not started <= stamp <= now or (now - stamp).total_seconds() > 90:
        return {**absent, "status": "SAME_DATE_SESSION_HEARTBEAT_STALE"}
    pid = saved.get("pid")
    if type(pid) is not int or pid <= 0:
        return {**absent, "status": "SAME_DATE_SESSION_PROCESS_UNVERIFIED"}
    observed = probe(pid)
    argv = observed.get("argv", [])
    expected = ["-u", "-m", "ml.gameplan_stock_trader", "--datastore-target", "pc", "--execute",
                "--target-horizon", "all", "--sizing-policy", GAMEPLAN_SIZING_POLICY, "--run-session"]
    tail = argv[1:] if isinstance(argv, list) else []
    if tail[len(expected):len(expected)+1] == ["--wait-for-open"]:
        tail = tail[:len(expected)] + tail[len(expected)+1:]
    for flag, key in (("--late-opening-date", "late_opening_date"), ("--resume-quote-run", "resume_quote_run"),
                      ("--resume-quote-symbol", "resume_quote_symbol")):
        if saved.get(key) is not None:
            expected.extend((flag, saved[key]))
    created = _stamp(observed["created_at"]) if observed.get("created_at") is not None else None
    expected_executable = Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()
    if (observed.get("alive") is not True or tail != expected or created is None
            or not argv or Path(argv[0]).resolve() != Path(sys.executable).resolve()
            or not observed.get("executable") or Path(observed["executable"]).resolve() != expected_executable
            or not 0 <= (started - created).total_seconds() <= 60):
        return {**absent, "status": "SAME_DATE_SESSION_PROCESS_UNVERIFIED"}
    lock_path = root / SESSION_LOCK
    if not lock_path.is_file():
        return {**absent, "status": "SAME_DATE_SESSION_LOCK_UNVERIFIED"}
    lock_raw = lock_path.read_text(encoding="utf-8")
    pairs = [line.split("=", 1) for line in lock_raw.splitlines() if line]
    if any(len(pair) != 2 for pair in pairs) or len(pairs) != 4:
        return {**absent, "status": "SAME_DATE_SESSION_LOCK_UNVERIFIED"}
    lock = dict(pairs)
    if (set(lock) != {"process", "pid", "started_at", "token"}
            or lock["process"] != "independent-stock-session" or lock["pid"] != str(pid)
            or re.fullmatch("[0-9a-f]{32}", lock["token"]) is None
            or not started <= _stamp(lock["started_at"]) <= stamp
            or (_stamp(lock["started_at"]) - started).total_seconds() > 60
            or lock_path.read_text(encoding="utf-8") != lock_raw or _json(path) != saved):
        return {**absent, "status": "SAME_DATE_SESSION_LOCK_UNVERIFIED"}
    return {"running": True, "status": status, "action_date": day, "heartbeat_at": stamp.isoformat(),
            "basis": "SAME_DATE_MANUAL_SESSION_AND_MATCHING_PROCESS"}


def inspect_readiness(datastore_root, action_date, *, now=None, process_probe=None):
    """Inspect saved local evidence; never capture accounts, reconcile or trade."""
    root = _absolute(datastore_root)
    day = date.fromisoformat(action_date).isoformat()
    if day != action_date:
        raise ValueError("An exact ISO action date is required")
    observed = utc_timestamp(now)
    blockers = []
    account = None
    config_path = root / CONFIG
    original = config_path.read_bytes() if config_path.is_file() else None
    try:
        account = load_account_config(root)
        if account is None:
            blockers.append("ACCOUNT_NOT_CONFIGURED")
        elif account.machine_id != "pc-original" or account.role != "coordinator":
            blockers.append("ACCOUNT_NOT_ATLAS_COORDINATOR")
        elif account.activation["status"] != "ACTIVE":
            blockers.append("ACCOUNT_CUTOVER_PREPARING")
        else:
            try:
                verify_cutover(root, account)
            except (ValueError, OSError, TypeError, KeyError):
                blockers.append("CUTOVER_RECEIPT_INVALID")
    except (ValueError, OSError, TypeError, KeyError):
        blockers.append("ACCOUNT_BINDING_INVALID")
    try:
        handoff, handoff_pins = _handoff(root, day, account)
    except (ValueError, OSError, TypeError, KeyError):
        handoff = {"ready": False, "reason": "JOINT_HANDOFF_INVALID"}
        handoff_pins = {}
    if not handoff["ready"]:
        blockers.append(handoff["reason"])
    ledger = {"available": False, "reason": "ACCOUNT_BINDING_REQUIRED"}
    if account is not None:
        try:
            ledger = _ledger(root, account)
            if not ledger["available"]:
                blockers.append(ledger["reason"])
            else:
                if not ledger["union_coverage"]:
                    blockers.append("NATIVE_LEDGER_UNION_INCOMPLETE")
                if ledger["blocked_symbol_count"]:
                    blockers.append("NATIVE_LEDGER_BLOCKED")
                if not ledger["saved_baseline_ready"]:
                    blockers.append("NATIVE_LEDGER_BASELINE_NOT_READY")
                if ledger["pending_reservation_count"] and account.activation["status"] != "ACTIVE":
                    blockers.append("NATIVE_PENDING_RESERVATIONS_PREVENT_CUTOVER")
                ledger_observed = observed if now is not None else utc_timestamp()
                if ledger["saved_baseline_at"] and _stamp(ledger["saved_baseline_at"]) > ledger_observed:
                    raise ValueError("Native saved baseline is in the future")
        except (ValueError, OSError, TypeError, KeyError, sqlite3.Error):
            ledger = {"available": False, "reason": "NATIVE_LEDGER_INVALID"}
            blockers.append("NATIVE_LEDGER_INVALID")
    instructions_ready = False
    if account is not None and handoff["ready"] and account.activation["status"] == "ACTIVE" and "CUTOVER_RECEIPT_INVALID" not in blockers:
        from ml.stock_trader.gameplan_execution import execution_preflight
        instructions_ready = execution_preflight(root, action_date=date.fromisoformat(day))["status"] == "READY"
        if not instructions_ready:
            blockers.append("SAVED_EXECUTION_INSTRUCTIONS_NOT_READY")
    try:
        session_observed = observed if now is not None else utc_timestamp()
        session = {**_session(root, day, session_observed, process_probe or _process_probe),
                   "observed_at": session_observed.isoformat()}
    except (ValueError, OSError, TypeError, KeyError):
        session = {"running": False, "status": "SAME_DATE_SESSION_EVIDENCE_INVALID"}
    if (config_path.read_bytes() if config_path.is_file() else None) != original:
        blockers.append("ACCOUNT_BINDING_INVALID")
    try:
        if any(file_checksum(Path(path)) != expected for path, expected in handoff_pins.items()):
            raise ValueError("Completed handoff changed during inspection")
    except (OSError, ValueError):
        handoff = {"ready": False, "reason": "JOINT_HANDOFF_INVALID"}
        blockers.append("JOINT_HANDOFF_INVALID")
    blockers = list(dict.fromkeys(blockers))
    return {"schema_version": VERSION, "action_date": day, "observed_at": observed.isoformat(),
            "status": "EXECUTION_SETUP_READY" if not blockers else "EXECUTION_SETUP_BLOCKED",
            "plan_ready": handoff["ready"], "execution_setup_ready": not blockers,
            "account_activation": account.activation["status"] if account else "UNAVAILABLE",
            "handoff": handoff, "native_ledger": ledger, "saved_execution_instructions_ready": instructions_ready,
            "manual_session": session, "manual_start_required": not session["running"],
            "blockers": blockers, "required_actions": [ACTION[reason] for reason in blockers],
            "current_broker_reconciliation_performed": False, "broker_ready": None,
            "execution_authorized": False, "activation_changed": False, "orders_placed": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore-root", required=True, type=Path)
    parser.add_argument("--action-date", required=True)
    parser.add_argument("--output", type=Path, help="Optional absolute local JSON report path; never an operating control")
    args = parser.parse_args(argv)
    result = inspect_readiness(args.datastore_root, args.action_date)
    if args.output is not None:
        output, root = _absolute(args.output), _absolute(args.datastore_root)
        repository = Path(__file__).resolve().parents[1]
        allowed = (root / "state/gameplan-execution-readiness", repository / "scratch/gameplan-execution-readiness")
        if output.suffix != ".json" or not any(folder in output.parents for folder in allowed):
            raise ValueError("Report output must be JSON under datastore state/gameplan-execution-readiness or repository scratch/gameplan-execution-readiness")
        if output.exists() and _json(output).get("schema_version") != VERSION:
            raise ValueError("Report output cannot replace a different document")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(result, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(result, indent=2))
    return 0 if result["execution_setup_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
