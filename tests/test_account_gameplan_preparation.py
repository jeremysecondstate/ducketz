import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from ml.account_gameplan.config import CONFIG, VERSION as CONFIG_VERSION, load_account_config
from ml.account_gameplan.preparation import run_preparation, register_inputs
from ml.account_gameplan.sources import export_source_bundle
from ml.artifacts import file_checksum
from test_account_gameplan_sources import DAY, native_case, save
from test_account_gameplan_config import write_config
from test_gameplan_cash_ledger import snapshot

NOW = "2026-10-05T06:00:00Z"
DEADLINE = "2026-10-05T11:00:00Z"


def activate(root):
    value = json.loads((root / CONFIG).read_text())
    config = load_account_config(root)
    path = root / "state/account-gameplan/cutovers/verified.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, {"schema_version": CONFIG_VERSION, "status": "VERIFIED", "binding_sha256": config.fingerprint,
        "machine_id": config.machine_id, "coordinator_id": config.coordinator_id,
        "peer_execution_fenced": True, "peer_fence_receipt_sha256": "b" * 64,
        "migration_manifest_sha256": "c" * 64, "fresh_union_reconciliation_sha256": "d" * 64,
        "installed_source_commit": "e" * 40, "orders_placed": 0})
    value["activation"].update(status="ACTIVE", receipt_path=path.relative_to(root).as_posix(),
                               receipt_sha256=file_checksum(path))
    save(root / CONFIG, value)
    return load_account_config(root)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    local = native_case(tmp_path, monkeypatch, producer="pc-original", symbol="AAPL")
    peer = native_case(tmp_path, monkeypatch, producer="pc-new", symbol="MU")
    write_config(local.root)
    source_hash = file_checksum(local.source / "receipt.json")
    source = export_source_bundle(local.root, gameplan_run=local.source, trade_plan_run=local.trade,
        destination=local.root / f"ml/account-gameplan-source-runs/{source_hash}", producer_id=local.producer,
        expected_symbols=[local.symbol])
    peer_bundle = export_source_bundle(peer.root, gameplan_run=peer.source, trade_plan_run=peer.trade,
        destination=peer.root / "portable", producer_id=peer.producer, expected_symbols=[peer.symbol])
    peer_destination = local.root / "ml/account-gameplan-source-imports/peer"
    shutil.copytree(peer_bundle.path, peer_destination)
    records = [{"producer_id": bundle.metadata["producer_id"], "bundle_path": path.relative_to(local.root).as_posix(),
                "manifest_sha256": bundle.manifest_sha256, "symbols": bundle.metadata["symbols"]}
               for bundle, path in ((source, source.path), (peer_bundle, peer_destination))]
    return local, records


def run(case, **kwargs):
    config = load_account_config(case.root)
    return run_preparation(case.root, gameplan_run=case.source, trade_plan_run=case.trade,
        deadline=kwargs.pop("deadline", DEADLINE), expected_config=config.fingerprint,
        clock=kwargs.pop("clock", lambda: NOW), **kwargs)


def capture(root, config, *, observed_at):
    state = snapshot(config.symbols, cash=1000, equity=10000)
    return {**state, "observed_at": observed_at, "account_fingerprint": config.account_fingerprint}


def test_preparing_exports_only_and_reuses_verified_immutable_source(prepared):
    case, _ = prepared
    def forbidden(*a, **k): pytest.fail("PREPARING must not capture or wait")
    first = run(case, sleeper=forbidden, snapshot_capture=forbidden)
    second = run(case, sleeper=forbidden, snapshot_capture=forbidden)
    assert first == second and first["status"] == "SOURCE_READY" and first["selection_changed"] is False
    assert (case.root / first["source_ready_receipt"]).is_file()
    assert not (case.root / "ml/account-gameplan-latest/run.json").exists()


def test_forecast_producer_never_waits_for_coordinator_or_captures(tmp_path, monkeypatch):
    case = native_case(tmp_path, monkeypatch, producer="pc-new", symbol="MU")
    write_config(case.root, machine="pc-new", status="ACTIVE")
    def forbidden(*a, **k): pytest.fail("Producer must not capture or wait")
    assert run(case, sleeper=forbidden, snapshot_capture=forbidden)["status"] == "SOURCE_READY"


def test_active_coordinator_waits_for_transport_then_captures_one_fresh_union_snapshot(prepared):
    case, records = prepared
    config = activate(case.root)
    waits, captures = [], []
    def transport(seconds):
        waits.append(seconds)
        register_inputs(case.root, action_date=DAY, sources=records, expected_config=config.fingerprint)
    def observed(*args, **kwargs):
        captures.append(kwargs["observed_at"])
        return capture(*args, **kwargs)
    result = run(case, sleeper=transport, snapshot_capture=observed)
    assert waits == [30] and captures == [NOW.replace("Z", "+00:00")]
    assert result["status"] == "COMPLETE" and result["selection_changed"] is True
    latest = json.loads((case.root / "ml/account-gameplan-latest/run.json").read_text())
    assert latest["config_sha256"] == config.fingerprint and latest["producer_id"] == "pc-original"
    assert latest == json.loads((case.root / f"ml/account-gameplan-by-date/{DAY}/run.json").read_text())
    before = (case.root / "ml/account-gameplan-latest/run.json").read_bytes()
    again = run(case, sleeper=lambda _: pytest.fail("no wait on resume"),
                snapshot_capture=lambda *a, **k: pytest.fail("completed selection must not recapture"))
    assert again["current"] == result["current"] and again["selection_changed"] is False
    assert (case.root / "ml/account-gameplan-latest/run.json").read_bytes() == before


def test_missing_peer_reaches_original_deadline_and_keeps_local_export(prepared):
    case, _ = prepared
    activate(case.root)
    current = [pd.Timestamp("2026-10-05T10:59:50Z")]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        current[0] += pd.Timedelta(seconds=seconds)
    with pytest.raises(ValueError, match="DEADLINE_PASSED"):
        run(case, clock=lambda: current[0], sleeper=sleep,
            snapshot_capture=lambda *a, **k: pytest.fail("no peer: no snapshot"))
    assert waits == [10]
    assert list((case.root / f"state/account-gameplan/outgoing/{DAY}").glob("*.json"))
    assert not (case.root / "ml/account-gameplan-latest/run.json").exists()


@pytest.mark.parametrize("failure", ["wrong_date", "hash", "overlap", "escape", "config"])
def test_registry_rejects_unverified_or_mismatched_transport(prepared, failure):
    case, records = prepared
    config = activate(case.root)
    if failure == "hash": records[1]["manifest_sha256"] = "0" * 64
    elif failure == "overlap": records[1]["symbols"] = ["AAPL"]
    elif failure == "escape": records[1]["bundle_path"] = "../../outside"
    with pytest.raises((ValueError, FileNotFoundError)):
        register_inputs(case.root, action_date="2026-10-06" if failure == "wrong_date" else DAY,
            sources=records, expected_config="0" * 64 if failure == "config" else config.fingerprint)
    assert not (case.root / f"state/account-gameplan/inputs/{DAY}.json").exists()


def test_active_without_cutover_and_wrong_original_deadline_cannot_publish(prepared):
    case, _ = prepared
    write_config(case.root, status="ACTIVE")
    with pytest.raises((ValueError, TypeError)):
        run(case)
    with pytest.raises(ValueError, match="original"):
        run(case, deadline="2026-10-05T12:00:00Z")


@pytest.mark.parametrize("failure", ["account", "config_during_capture", "deadline_during_capture"])
def test_failed_fresh_capture_cannot_select_or_change_an_existing_pointer(prepared, failure):
    case, records = prepared
    config = activate(case.root)
    register_inputs(case.root, action_date=DAY, sources=records, expected_config=config.fingerprint)
    now = [NOW]
    def bad_capture(root, binding, *, observed_at):
        result = capture(root, binding, observed_at=observed_at)
        if failure == "account": result["account_fingerprint"] = "0" * 64
        elif failure == "config_during_capture":
            value = json.loads((root / CONFIG).read_text())
            value["activation"]["status"] = "PREPARING"
            save(root / CONFIG, value)
        else: now[0] = DEADLINE
        return result
    with pytest.raises(ValueError):
        run(case, clock=lambda: now[0], snapshot_capture=bad_capture)
    assert not (case.root / "ml/account-gameplan-latest/run.json").exists()


def test_final_deadline_guard_cannot_activate_dated_execution_selection(prepared, monkeypatch):
    from ml.account_gameplan import preparation
    case, records = prepared
    config = activate(case.root)
    register_inputs(case.root, action_date=DAY, sources=records, expected_config=config.fingerprint)
    now = [NOW]
    original = preparation.os.replace
    def changed_after_ui(source, destination):
        original(source, destination)
        now[0] = DEADLINE
    monkeypatch.setattr(preparation.os, "replace", changed_after_ui)
    with pytest.raises(ValueError, match="DEADLINE_PASSED"):
        run(case, clock=lambda: now[0], snapshot_capture=capture)
    assert (case.root / "ml/account-gameplan-latest/run.json").exists()
    assert not (case.root / f"ml/account-gameplan-by-date/{DAY}/run.json").exists()


def test_interrupted_ui_selection_reuses_exact_plan_without_second_snapshot(prepared, monkeypatch):
    from ml.account_gameplan import preparation
    case, records = prepared
    config = activate(case.root)
    register_inputs(case.root, action_date=DAY, sources=records, expected_config=config.fingerprint)
    original = preparation._immutable
    def interrupted(path, value):
        if "account-gameplan-by-date" in path.parts:
            raise OSError("Fixture interrupted before final source selection")
        return original(path, value)
    monkeypatch.setattr(preparation, "_immutable", interrupted)
    with pytest.raises(OSError):
        run(case, snapshot_capture=capture)
    current = (case.root / "ml/account-gameplan-latest/run.json").read_bytes()
    monkeypatch.setattr(preparation, "_immutable", original)
    result = run(case, snapshot_capture=lambda *a, **k: pytest.fail("Do not recapture or rebuild the completed plan"))
    assert result["selection_changed"] is True
    assert (case.root / "ml/account-gameplan-latest/run.json").read_bytes() == current
    assert (case.root / f"ml/account-gameplan-by-date/{DAY}/run.json").is_file()


@pytest.mark.parametrize("record_kind", ["outgoing", "dated"])
@pytest.mark.parametrize("failure", ["deadline", "config", "foreign"])
def test_post_commit_guard_invalidates_only_exact_new_record(prepared, monkeypatch, record_kind, failure):
    from ml.account_gameplan import preparation
    case, records = prepared
    config = activate(case.root) if record_kind == "dated" else load_account_config(case.root)
    if record_kind == "dated":
        register_inputs(case.root, action_date=DAY, sources=records, expected_config=config.fingerprint)
    now, committed = [NOW], []
    original = preparation._immutable
    foreign = b'{"status":"INDEPENDENT_WRITER"}'
    def commit_then_change(path, value):
        result = original(path, value)
        selected = ("outgoing" in path.parts if record_kind == "outgoing" else "account-gameplan-by-date" in path.parts)
        if selected and value.get("status") in {"SOURCE_READY", "SELECTED"}:
            committed.append(path)
            if failure == "config":
                current = json.loads((case.root / CONFIG).read_text())
                current["activation"]["status"] = "PREPARING" if record_kind == "dated" else "ACTIVE"
                save(case.root / CONFIG, current)
            else:
                now[0] = DEADLINE
                if failure == "foreign":
                    path.write_bytes(foreign)
        return result
    monkeypatch.setattr(preparation, "_immutable", commit_then_change)
    with pytest.raises(ValueError):
        run(case, clock=lambda: now[0], snapshot_capture=capture)
    assert len(committed) == 1
    path = committed[0]
    if failure == "foreign":
        assert path.read_bytes() == foreign
    else:
        assert json.loads(path.read_text())["status"] == "FAILED"
    failures = list(path.parent.glob(f"{path.stem}-failed-*.json"))
    assert len(failures) == 1
    evidence = json.loads(failures[0].read_text())
    assert evidence["record"]["status"] == ("SOURCE_READY" if record_kind == "outgoing" else "SELECTED")
