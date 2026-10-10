"""First-use configuration fixtures never contact providers or touch accounting."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from ml.account_gameplan.config import CONFIG, VERSION, digest, load_account_config, verify_cutover
from tools import gameplan_first_use as first_use


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


@pytest.fixture
def first_use_account(tmp_path, monkeypatch):
    repo, root = tmp_path / "repo", tmp_path / "datastore"
    account = {"schema_version": VERSION, "machine_id": "pc-original", "coordinator_id": "pc-original",
        "participants": {"pc-original": ["COST"], "pc-new": ["MSFT"]}, "account_fingerprint": "a" * 64}
    account["activation"] = {"status": "PREPARING", "binding_sha256": digest(account)}
    write(root / CONFIG, account)
    bound = load_account_config(root)
    marker = repo / "scratch/nightly-workflow/first-use.json"
    declaration = {"schema_version": "gameplan-first-use-declaration-v1", "operation_id": "original-first-use-operation",
        "account_binding_sha256": bound.fingerprint, "account_fingerprint": bound.account_fingerprint,
        "executor": "Atlas", "basis": "LOCAL_HUMAN_ATLAS_SOLE_EXECUTOR", "peer_history_expected": False,
        "manual_start_only": True}
    write(marker, declaration)
    # These deliberately are not SQLite files: this route must not inspect,
    # reconstruct, migrate, or make reconciliation claims about native history.
    from tools.nightly_ownership import LEDGER
    from ml.stock_trader import runtime as trader_runtime, state as trader_state
    ledger = root / LEDGER
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger_group = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(ledger) + suffix)
        path.write_bytes(("native-original-history" + suffix).encode())
        ledger_group[path] = path.read_bytes()
    monkeypatch.setattr(first_use, "_repository", lambda: repo)
    def forbid_capture(*args, **kwargs):
        pytest.fail("First-use setup must not capture an account")
    monkeypatch.setattr(trader_state, "capture_portfolio_state", forbid_capture)
    monkeypatch.setattr(trader_runtime, "_capture_portfolio_state_with_retry", forbid_capture)
    return {"repo": repo, "root": root, "marker": marker, "declaration": declaration,
        "original": (root / CONFIG).read_bytes(), "ledger_group": ledger_group}


def assert_original_accounting(case):
    assert all(path.read_bytes() == raw for path, raw in case["ledger_group"].items())


def saved_receipt(case):
    account = load_account_config(case["root"])
    path = case["root"] / account.activation["receipt_path"]
    return path, path.read_bytes(), verify_cutover(case["root"], account)


def test_first_use_inspection_and_manual_setup_do_not_capture_or_touch_native_history(first_use_account):
    case = first_use_account
    before = {path: path.read_bytes() for path in case["repo"].parent.rglob("*") if path.is_file()}
    checked = first_use.inspect_first_use(case["root"])
    assert checked["ready"] and checked["status"] == "READY_FOR_MANUAL_START"
    assert checked["startup_mode"] == "ATLAS_SOLE_EXECUTOR"
    assert checked["broker_reconciliation_pending"] and not checked["activation_changed"]
    assert {path: path.read_bytes() for path in case["repo"].parent.rglob("*") if path.is_file()} == before
    result = first_use.initialize_first_use(case["root"])
    assert result["ready"] and result["status"] == "ACCOUNT_READY" and result["activation_changed"]
    assert result["broker_read_performed"] is False
    active = json.loads((case["root"] / CONFIG).read_bytes())
    original = json.loads(case["original"])
    assert {key: value for key, value in active.items() if key != "activation"} == {
        key: value for key, value in original.items() if key != "activation"}
    _, _, receipt = saved_receipt(case)
    assert receipt["schema_version"] == "account-gameplan-first-use-v1"
    assert receipt["operation_id"] == case["declaration"]["operation_id"]
    assert_original_accounting(case)


def test_first_use_missing_local_direction_never_activates(first_use_account):
    case = first_use_account
    case["marker"].unlink()
    assert first_use.inspect_first_use(case["root"]) is None
    with pytest.raises((ValueError, FileNotFoundError)):
        first_use.initialize_first_use(case["root"])
    assert (case["root"] / CONFIG).read_bytes() == case["original"]
    assert_original_accounting(case)


@pytest.mark.parametrize("field,value", [("account_binding_sha256", "b" * 64),
    ("account_fingerprint", "c" * 64), ("peer_history_expected", True)])
def test_first_use_rejects_another_binding_or_existing_peer_history(first_use_account, field, value):
    case = first_use_account
    declaration = deepcopy(case["declaration"])
    declaration[field] = value
    write(case["marker"], declaration)
    checked = first_use.inspect_first_use(case["root"])
    assert not checked["ready"]
    with pytest.raises(ValueError):
        first_use.initialize_first_use(case["root"])
    assert (case["root"] / CONFIG).read_bytes() == case["original"]
    assert_original_accounting(case)


def test_first_use_rechecks_direction_at_activation_boundary(first_use_account):
    case = first_use_account
    def mutate(point):
        if point == "before_activation":
            changed = {**case["declaration"], "account_binding_sha256": "d" * 64}
            write(case["marker"], changed)
    with pytest.raises(ValueError):
        first_use.initialize_first_use(case["root"], checkpoint=mutate)
    assert (case["root"] / CONFIG).read_bytes() == case["original"]
    assert_original_accounting(case)


@pytest.mark.parametrize("point", ["before_activation", "after_activation"])
def test_first_use_interruption_retries_same_receipt_without_native_replay(first_use_account, point):
    case = first_use_account
    def fail(observed):
        if observed == point:
            raise RuntimeError("offline first-use interruption")
    with pytest.raises(RuntimeError, match="offline first-use"):
        first_use.initialize_first_use(case["root"], checkpoint=fail)
    retained = {path: path.read_bytes() for path in (case["root"] / "state/account-gameplan/cutovers").glob("*.json")}
    assert retained, "Write the immutable receipt before the activation commit"
    result = first_use.initialize_first_use(case["root"])
    assert result["ready"] and result["broker_read_performed"] is False
    assert result["activation_changed"] is (point == "before_activation")
    assert all(path.read_bytes() == raw for path, raw in retained.items())
    assert set((case["root"] / "state/account-gameplan/cutovers").glob("*.json")) == set(retained)
    saved_receipt(case)
    assert_original_accounting(case)


def test_first_use_active_retry_preserves_exact_config_and_receipt(first_use_account):
    case = first_use_account
    first_use.initialize_first_use(case["root"])
    original = (case["root"] / CONFIG).read_bytes()
    path, receipt, _ = saved_receipt(case)
    result = first_use.initialize_first_use(case["root"])
    assert result["ready"] and not result["activation_changed"] and not result["broker_read_performed"]
    assert (case["root"] / CONFIG).read_bytes() == original and path.read_bytes() == receipt
    assert_original_accounting(case)


@pytest.mark.parametrize("owner", ["another_setup", "running_worker"])
def test_first_use_cannot_activate_while_another_owner_holds_its_lock(first_use_account, owner):
    from filelock import FileLock, Timeout
    from datafetching.runtime_lock import exclusive_runtime_lock
    case = first_use_account
    if owner == "another_setup":
        lock_path = case["root"] / first_use.LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(lock_path), timeout=0)
    else:
        lock = exclusive_runtime_lock(case["root"] / "locks/independent-stock-session.lock", process_name="fixture-worker")
    with lock:
        with pytest.raises(Timeout if owner == "another_setup" else RuntimeError,
                           match=None if owner == "another_setup" else "owns these artifacts"):
            first_use.initialize_first_use(case["root"])
    assert (case["root"] / CONFIG).read_bytes() == case["original"]
    assert not (case["root"] / "state/account-gameplan/cutovers").exists()
    assert_original_accounting(case)


def test_first_use_does_not_replace_an_existing_verified_history_cutover(first_use_account):
    case = first_use_account
    bound = load_account_config(case["root"])
    receipt = {"schema_version": VERSION, "status": "VERIFIED", "binding_sha256": bound.fingerprint,
        "machine_id": "pc-original", "coordinator_id": "pc-original", "peer_execution_fenced": True,
        "peer_fence_receipt_sha256": "b" * 64, "migration_manifest_sha256": "c" * 64,
        "fresh_union_reconciliation_sha256": "d" * 64, "installed_source_commit": "e" * 40, "orders_placed": 0}
    path = case["root"] / "state/account-gameplan/cutovers/existing.json"
    write(path, receipt)
    account = json.loads(case["original"])
    account["activation"].update(status="ACTIVE", receipt_path=path.relative_to(case["root"]).as_posix(),
        receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    write(case["root"] / CONFIG, account)
    original = (case["root"] / CONFIG).read_bytes()
    result = first_use.initialize_first_use(case["root"])
    assert result["ready"] and not result["activation_changed"] and not result["broker_read_performed"]
    assert (case["root"] / CONFIG).read_bytes() == original
    assert saved_receipt(case)[2] == receipt
    assert_original_accounting(case)
