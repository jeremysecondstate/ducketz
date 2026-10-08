"""Saved evidence reporting uses fixtures, never live accounts or trading."""
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys

import pandas as pd
import pytest

from ml import nightly_handoff
from ml.account_gameplan.config import CONFIG, VERSION as ACCOUNT_VERSION, load_account_config
from ml.artifacts import file_checksum
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
from tests.test_nightly_handoff import handoff_inputs
from tests.test_nightly_synthesis import specification as synthesis_inputs
from tests.test_joint_capital_plan import NOW
from tools import gameplan_execution_readiness as module


@pytest.fixture
def prepared(handoff_inputs):
    spec = handoff_inputs
    nightly_handoff.run_handoff(spec, now=NOW)
    return spec, Path(spec["datastore_root"])


def ledger(root, symbols=None, *, pending=False, blocked=False):
    account = load_account_config(root)
    selected = symbols or account.symbols
    path = root / module.LEDGER
    HorizonLedger(path, account.account_fingerprint)
    evidence = PortfolioEvidence("private-native-id", account.account_fingerprint, NOW,
        dict.fromkeys(selected, 987654), dict.fromkeys(selected, "765432.10"),
        dict.fromkeys(selected, "876543.21"), "b" * 64)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
            (evidence.snapshot_id, NOW, json.dumps(asdict(evidence)), 1, "[]", "{}"))
        if blocked:
            db.execute("INSERT INTO blocks VALUES (?,?)", (selected[0], "private-block-details"))
        if pending:
            db.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,?)",
                ("private-allocation", account.account_fingerprint, selected[0], "1d", "private-forecast", NOW,
                 (pd.Timestamp(NOW) + pd.Timedelta(days=1)).isoformat(), "ACTIVE"))
            db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("private-order", "private-allocation", "BUY", 999, "12345.67", 0, "UNKNOWN", None,
                 "private-idempotency", "private-batch", "{}", None))
    return path


def activate(root):
    value = json.loads((root / CONFIG).read_text())
    binding = value["activation"]["binding_sha256"]
    path = root / "state/account-gameplan/cutovers/fixture.json"
    path.parent.mkdir(parents=True)
    receipt = {"schema_version": ACCOUNT_VERSION, "status": "VERIFIED", "binding_sha256": binding,
        "machine_id": "pc-original", "coordinator_id": "pc-original", "peer_execution_fenced": True,
        "peer_fence_receipt_sha256": "f" * 64, "migration_manifest_sha256": "c" * 64,
        "fresh_union_reconciliation_sha256": "d" * 64, "installed_source_commit": "e" * 40, "orders_placed": 0}
    path.write_text(json.dumps(receipt))
    value["activation"].update(status="ACTIVE", receipt_path=path.relative_to(root).as_posix(), receipt_sha256=file_checksum(path))
    (root / CONFIG).write_text(json.dumps(value))


def inspect(prepared, **kwargs):
    spec, root = prepared
    return module.inspect_readiness(root, spec["action_date"], now=NOW, **kwargs)


def test_preparing_reports_plan_success_and_missing_native_ledger_separately(prepared):
    result = inspect(prepared)
    assert result["plan_ready"] and not result["execution_setup_ready"]
    assert result["blockers"] == ["ACCOUNT_CUTOVER_PREPARING", "NATIVE_LEDGER_MISSING"]
    assert result["handoff"]["completion_id"] == prepared[0]["completion_id"]
    assert result["current_broker_reconciliation_performed"] is False and result["broker_ready"] is None
    assert result["manual_start_required"] and not result["execution_authorized"]


def test_preparing_reports_local_partition_incomplete_and_never_exports_private_values(prepared, monkeypatch):
    spec, root = prepared
    source = ledger(root, ["AAPL"])
    monkeypatch.setattr(HorizonLedger, "__init__", lambda *a, **k: pytest.fail("Do not open the live native ledger"))
    monkeypatch.setattr("tools.nightly_account_snapshot.capture_snapshot", lambda *a, **k: pytest.fail("No broker snapshot"))
    monkeypatch.setattr("ml.nightly_workflow.source_identity", lambda *a, **k: pytest.fail("Completed handoff is frozen evidence"))
    originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = inspect(prepared)
    assert "NATIVE_LEDGER_UNION_INCOMPLETE" in result["blockers"]
    assert result["native_ledger"]["saved_baseline_at"] == pd.Timestamp(NOW).isoformat()
    serialized = json.dumps(result)
    for forbidden in ("987654", "765432.10", "876543.21", "private-native-id", load_account_config(root).account_fingerprint):
        assert forbidden not in serialized
    assert all(p.read_bytes() == content for p, content in originals.items())
    assert set(originals) == {p for p in root.rglob("*") if p.is_file()}
    assert source.is_file()


def test_wal_metadata_is_included_without_touching_live_database_or_sidecars(prepared):
    _, root = prepared
    path = ledger(root)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO blocks VALUES (?,?)", ("AAPL", "private-wal-reason"))
        db.commit()
        originals = {Path(str(path) + suffix): Path(str(path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        result = inspect(prepared)
        assert "NATIVE_LEDGER_BLOCKED" in result["blockers"]
        assert all(candidate.read_bytes() == original for candidate, original in originals.items())


def test_complete_setup_still_requires_manual_start_and_runtime_broker_reconciliation(prepared, monkeypatch):
    _, root = prepared
    ledger(root)
    activate(root)
    monkeypatch.setattr("ml.stock_trader.gameplan_execution.execution_preflight", lambda *a, **k: {"status": "READY"})
    result = inspect(prepared)
    assert result["plan_ready"] and result["execution_setup_ready"] and result["saved_execution_instructions_ready"]
    assert result["account_activation"] == "ACTIVE" and result["blockers"] == []
    assert result["manual_start_required"] and result["broker_ready"] is None
    assert result["current_broker_reconciliation_performed"] is False


def test_native_blocks_pending_and_saved_baseline_are_actionable_without_reconciliation(prepared):
    _, root = prepared
    ledger(root, pending=True, blocked=True)
    result = inspect(prepared)
    assert "NATIVE_LEDGER_BLOCKED" in result["blockers"]
    assert "NATIVE_PENDING_RESERVATIONS_PREVENT_CUTOVER" in result["blockers"]
    assert result["native_ledger"]["pending_reservation_count"] == 1
    assert "private-order" not in json.dumps(result)


@pytest.mark.parametrize("damage", ["receipt", "plan", "day"])
def test_completed_handoff_must_match_requested_day_and_original_bytes(prepared, damage):
    spec, root = prepared
    if damage == "receipt":
        path = Path(spec["state_root"]) / spec["completion_id"] / "receipt.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "plan":
        pin = json.loads((root / "ml/joint-gameplan-by-date" / spec["action_date"] / "run.json").read_text())
        path = root / pin["run_path"] / "joint-plan.json"
        path.write_text("{}")
    else:
        spec["action_date"] = "2026-09-10"
    result = inspect(prepared)
    assert not result["plan_ready"] and not result["execution_setup_ready"]
    assert result["handoff"]["reason"] in {"JOINT_HANDOFF_MISSING", "JOINT_HANDOFF_INVALID"}


def session(root, day):
    saved = {"schema_version": "independent-stock-session-status-v1", "action_date": day,
        "execute": True, "sizing_policy": GAMEPLAN_SIZING_POLICY, "status": "SLEEPING_UNTIL_OPEN",
        "pid": 12345, "started_at": NOW, "heartbeat_at": NOW}
    path = root / module.SESSION
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved))
    lock = root / module.SESSION_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("process=independent-stock-session\npid=12345\nstarted_at=" + NOW + "\ntoken=" + "a"*32 + "\n")
    return saved, path


def probe(pid):
    assert pid == 12345
    return {"alive": True, "created_at": pd.Timestamp(NOW) - pd.Timedelta(seconds=3),
            "executable": getattr(sys, "_base_executable", None) or sys.executable,
            "argv": [sys.executable, "-u", "-m", "ml.gameplan_stock_trader", "--datastore-target", "pc",
                     "--execute", "--target-horizon", "all", "--sizing-policy", GAMEPLAN_SIZING_POLICY,
                     "--run-session", "--wait-for-open"]}


def test_same_date_manual_session_is_authoritative_diagnostic_not_an_execution_gate(prepared):
    spec, root = prepared
    session(root, spec["action_date"])
    result = inspect(prepared, process_probe=probe)
    assert result["manual_session"]["running"] and not result["manual_start_required"]
    assert not result["execution_setup_ready"] and result["blockers"][0] == "ACCOUNT_CUTOVER_PREPARING"


def test_session_inspection_samples_its_own_current_time_after_artifact_checks(prepared, monkeypatch):
    spec, root = prepared
    saved, path = session(root, spec["action_date"])
    later = pd.Timestamp(NOW) + pd.Timedelta(seconds=5)
    saved["heartbeat_at"] = later.isoformat()
    path.write_text(json.dumps(saved))
    stamps = iter((pd.Timestamp(NOW), later))
    monkeypatch.setattr(module, "utc_timestamp", lambda *args: next(stamps))
    result = module.inspect_readiness(root, spec["action_date"], process_probe=probe)
    assert result["manual_session"]["running"]
    assert result["manual_session"]["observed_at"] == later.isoformat()


@pytest.mark.parametrize("damage", ["wrong_day", "stale", "pid_reused", "wrong_command", "wrong_image", "wrong_lock"])
def test_old_or_unrelated_process_cannot_prove_manual_session(prepared, damage):
    spec, root = prepared
    saved, path = session(root, spec["action_date"])
    observed = probe(12345)
    if damage == "wrong_day": saved["action_date"] = "2026-09-08"
    elif damage == "stale": saved["heartbeat_at"] = "2026-09-09T09:00:00Z"
    elif damage == "pid_reused": observed["created_at"] = pd.Timestamp(NOW) + pd.Timedelta(seconds=1)
    elif damage == "wrong_image": observed["executable"] = str(root / "unrelated.exe")
    elif damage == "wrong_lock": (root / module.SESSION_LOCK).write_text("process=not-the-session\n")
    else: observed["argv"] = ["python", "-m", "unrelated"]
    path.write_text(json.dumps(saved))
    result = inspect(prepared, process_probe=lambda _: observed)
    assert not result["manual_session"]["running"] and result["manual_start_required"]


def test_active_receipt_and_local_deployment_remain_required(prepared, monkeypatch):
    _, root = prepared
    ledger(root)
    activate(root)
    monkeypatch.setattr("ml.stock_trader.gameplan_execution.execution_preflight", lambda *a, **k: {"status": "NOT_READY"})
    assert "SAVED_EXECUTION_INSTRUCTIONS_NOT_READY" in inspect(prepared)["blockers"]
    (root / "state/account-gameplan/cutovers/fixture.json").write_text("{}")
    assert "CUTOVER_RECEIPT_INVALID" in inspect(prepared)["blockers"]


def test_handoff_pointer_change_during_later_inspection_is_not_ready(prepared, monkeypatch):
    spec, root = prepared
    pointer = root / module.POINTERS / spec["action_date"] / "run.json"
    original = module._ledger
    def changes(*args):
        result = original(*args)
        pointer.write_bytes(pointer.read_bytes() + b" ")
        return result
    monkeypatch.setattr(module, "_ledger", changes)
    result = inspect(prepared)
    assert not result["plan_ready"] and "JOINT_HANDOFF_INVALID" in result["blockers"]


@pytest.mark.parametrize("damage", ["negative_holdings", "mismatched_timestamp", "ready_with_reasons", "ownership_exceeds_holdings"])
def test_damaged_native_metadata_cannot_report_setup_ready(prepared, damage, monkeypatch):
    _, root = prepared
    path = ledger(root)
    activate(root)
    monkeypatch.setattr("ml.stock_trader.gameplan_execution.execution_preflight", lambda *a, **k: {"status": "READY"})
    with sqlite3.connect(path) as db:
        payload = json.loads(db.execute("SELECT payload FROM snapshots").fetchone()[0])
        if damage == "negative_holdings":
            payload["held_shares"]["AAPL"] = -1
        elif damage == "mismatched_timestamp":
            payload["observed_at"] = "2026-09-08T10:00:00Z"
        elif damage == "ready_with_reasons":
            db.execute("UPDATE snapshots SET reasons=?", ('["private-native-error"]',))
        else:
            from types import SimpleNamespace
            allocation = SimpleNamespace(account_fingerprint=load_account_config(root).account_fingerprint,
                symbol="AAPL", horizon="1d", status="ACTIVE", filled_shares=9999999, reserved_sell_shares=0,
                reserved_buy_shares=0, target_start=NOW, target_end=(pd.Timestamp(NOW) + pd.Timedelta(days=1)).isoformat())
            monkeypatch.setattr(HorizonLedger, "_snapshot", lambda _: SimpleNamespace(allocations=[allocation]))
        db.execute("UPDATE snapshots SET payload=?", (json.dumps(payload),))
    result = inspect(prepared)
    assert "NATIVE_LEDGER_INVALID" in result["blockers"] and not result["execution_setup_ready"]


def test_cli_writes_only_explicit_local_report_namespace(prepared, capsys):
    spec, root = prepared
    output = root / "state/gameplan-execution-readiness/status.json"
    assert module.main(["--datastore-root", str(root), "--action-date", spec["action_date"], "--output", str(output)]) == 1
    assert json.loads(output.read_text())["schema_version"] == module.VERSION
    assert json.loads(capsys.readouterr().out)["plan_ready"]
    original = (root / CONFIG).read_bytes()
    with pytest.raises(ValueError, match="Report output"):
        module.main(["--datastore-root", str(root), "--action-date", spec["action_date"], "--output", str(root / CONFIG)])
    assert (root / CONFIG).read_bytes() == original
