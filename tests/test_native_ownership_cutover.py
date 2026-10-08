"""All capture, operating files and ledgers here are local temporary fixtures."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.config import CONFIG, VERSION as ACCOUNT_VERSION, digest, load_account_config, verify_cutover
from ml.account_gameplan.migration import pin_ledger_files
from tools import native_ownership_cutover as adapter
from tools import native_ownership_exchange
from test_account_gameplan_cutover import NOW, setup as migration_fixture


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(adapter._bytes(value))
    return {"path": str(path), "file_sha256": adapter.file_checksum(path)}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    args, sources = migration_fixture(tmp_path)
    repo = tmp_path / "repo"
    root = tmp_path / "datastore"
    state_root = repo / "scratch/native-operation"
    operation = "native-test-operation"
    local = state_root / operation
    local.mkdir(parents=True)
    candidate = local / "migration-candidate"
    shutil.copytree(args["candidate_directory"], candidate)
    ledger = root / adapter.LEDGER
    ledger.parent.mkdir(parents=True)
    for suffix, pin in adapter._group(Path(sources[0]["path"])).items():
        if pin is not None:
            shutil.copyfile(Path(sources[0]["path"] + suffix), Path(str(ledger) + suffix))
    account_doc = {"schema_version": ACCOUNT_VERSION, "machine_id": "pc-original", "coordinator_id": "pc-original",
                   "participants": args["expected_participants"], "account_fingerprint": args["expected_account_fingerprint"]}
    account_doc["activation"] = {"status": "PREPARING", "binding_sha256": digest(account_doc)}
    write(root / CONFIG, account_doc)
    original = (root / CONFIG).read_bytes()
    native = {"actor": "Atlas", "operation_id": operation, "cutover_action_date": "2026-10-05",
              "datastore_root": str(root), "state_root": str(state_root)}
    native_selection = write(repo / "scratch/native-config.json", native)
    write(local / "export/manifest.json", {"source_pins": pin_ledger_files(ledger)})
    review = {"packets": {"Atlas": "a" * 64, "Scout": "b" * 64},
              "files": {path.relative_to(candidate).as_posix(): adapter.file_checksum(path)
                        for path in candidate.rglob("*") if path.is_file()}}
    write(local / "migration-review.json", review)
    write(local / "binding.json", {"config": native, "account_binding_sha256": load_account_config(root).fingerprint,
          "frozen_inputs_sha256": {"operation_config": native_selection["file_sha256"]}})
    human = {"schema_version": adapter.HUMAN_VERSION, "operation_id": operation, "action_date": "2026-10-05",
             "basis": "DIRECT_ATLAS_LOCAL_HUMAN_INSTRUCTION", "executor": "Atlas", "manual_start_only": True,
             "scout_will_not_execute": True, "authorize_read_only_capture": True,
             "authorize_native_installation": True, "authorize_activation": True}
    human_selection = write(repo / "scratch/human-authority.json", human)
    exchange_root = repo / "scratch/exchange"
    write(repo / "scratch/nightly-workflow/exchange-config.json", {"state_root": str(exchange_root)})
    plan = repo / "scratch/terminal-handoff.json"
    write(plan, {"completion_id": "original-completion", "status": "COMPLETE"})
    calls = []
    def handoff(spec, config, account, watched):
        calls.append("handoff")
        adapter._pin(plan, adapter.file_checksum(plan), watched)
        return {"session": exchange_root / "sessions/2026-10-05", "exchange_lock": exchange_root / "exchange.lock",
                "handoff_completion_id": "original-completion"}
    implementation = {"source_identity": {"commit": "c" * 40, "source_sha256": "d" * 64},
                      "tools": {name: "e" * 64 for name in adapter.TOOLS}}
    for relative in adapter.CONTROL_PATHS:
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text("CONFIRM_ACTIVE_TRADING=TRUE\n")
    def runtime(root):
        return {"scheduled_task": {"task_path": "\\", "task_name": "Ducketz Independent Stock Session",
                "enabled": False, "state": "Disabled"}, "manual_control_files": {
                path: adapter.file_checksum(root / path) for path in adapter.CONTROL_PATHS}}
    monkeypatch.setattr(adapter, "_repository", lambda: repo)
    monkeypatch.setattr(adapter, "_load_native", lambda path: adapter._json(path))
    monkeypatch.setattr(adapter, "implementation", lambda: implementation)
    monkeypatch.setattr(adapter, "runtime_bindings", runtime)
    monkeypatch.setattr(adapter, "_completed_handoff", handoff)
    monkeypatch.setattr(native_ownership_exchange, "_binding_inputs", lambda config: {})
    spec = {"schema_version": adapter.VERSION, "operation_id": operation, "action_date": "2026-10-05",
            "native_exchange_config": native_selection, "account_config_sha256": adapter.file_checksum(root / CONFIG),
            "candidate_manifest_sha256": adapter.file_checksum(candidate / "manifest.json"),
            "migration_review_sha256": adapter.file_checksum(local / "migration-review.json"),
            "human_authorization": human_selection, "implementation": implementation,
            "nightly": dict.fromkeys(adapter.NIGHTLY, "f" * 64), "runtime_bindings": runtime(root)}
    def capture(account, clock):
        assert calls[-1] == "handoff"
        calls.append("capture")
        return args["broker_snapshot"]
    return dict(spec=spec, root=root, local=local, work=local / "activation", ledger=ledger, candidate=candidate,
                original=original, native=native, args=args, calls=calls, capture=capture, clock=args["clock"],
                plan=plan, implementation=implementation)


def run(fixture, **kwargs):
    return adapter.apply(fixture["spec"], clock=fixture["clock"], capture=fixture["capture"], **kwargs)


def journal(fixture):
    return adapter._json(fixture["work"] / "journal.json")


def assert_preparing(fixture):
    assert (fixture["root"] / CONFIG).read_bytes() == fixture["original"]
    assert load_account_config(fixture["root"]).activation["status"] == "PREPARING"


def test_check_is_readonly_and_matching_handoff_precedes_capture(fixture):
    before = {path: path.read_bytes() for path in fixture["local"].rglob("*") if path.is_file()}
    checked = adapter.preflight(fixture["spec"], clock=fixture["clock"])
    assert checked["handoff_completion_id"] == "original-completion"
    assert fixture["calls"] == ["handoff"]
    assert not fixture["work"].exists()
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert_preparing(fixture)


def test_apply_uses_one_capture_preserves_native_ids_and_config_bindings_then_terminal_retry(fixture):
    before = adapter._read_tables(fixture["ledger"])
    plan = fixture["plan"].read_bytes()
    controls = adapter.runtime_bindings(fixture["root"])
    result = run(fixture)
    assert result["status"] == "ACTIVE_CUTOVER_VERIFIED" and result["activation_changed"]
    assert result["broker_read_performed"] and not result["trader_started"] and result["orders_placed"] == 0
    assert not result["nightly_session_rebound"] and result["manual_start_required"]
    assert fixture["calls"] == ["handoff", "capture"]
    after = adapter._read_tables(fixture["ledger"])
    for name in ("allocations", "reservations", "fills"):
        assert all(row in after[name] for row in before[name])
    last = json.loads(after["snapshots"][-1]["payload"])
    assert last["symbol_budgets"] == {"COST": "0", "MSFT": "0"}
    assert after["blocks"] == []
    active = json.loads((fixture["root"] / CONFIG).read_bytes())
    original = json.loads(fixture["original"])
    assert {key: value for key, value in active.items() if key != "activation"} == {
        key: value for key, value in original.items() if key != "activation"}
    receipt = verify_cutover(fixture["root"], load_account_config(fixture["root"]))
    assert receipt["peer_fence_receipt_sha256"] == fixture["spec"]["human_authorization"]["file_sha256"]
    saved = journal(fixture)
    assert saved["human_evidence_kind"] == "HUMAN_ATTESTED_SOLE_EXECUTOR"
    assert "installed_database_sha256" not in saved and saved["candidate_database_sha256"]
    assert saved["installed_group"] and saved["phase"] == "COMPLETE"
    raw_original = fixture["work"] / saved["attempt"] / "original/holdings.sqlite3"
    assert adapter._group(raw_original) == saved["original_group"]
    assert fixture["plan"].read_bytes() == plan and adapter.runtime_bindings(fixture["root"]) == controls
    retry = run(fixture)
    assert not retry["activation_changed"] and not retry["broker_read_performed"]
    assert fixture["calls"] == ["handoff", "capture"]
    assert not (fixture["root"] / "locks/independent-stock-session.lock").exists()


@pytest.mark.parametrize("failure", ["manifest", "review", "source", "human", "runtime", "implementation", "native_binding", "handoff", "deadline"])
def test_precondition_failures_never_capture_or_change_operating_state(fixture, monkeypatch, failure):
    spec = fixture["spec"]
    if failure == "manifest":
        (fixture["candidate"] / "manifest.json").write_bytes(b"{}")
    elif failure == "review":
        (fixture["local"] / "migration-review.json").write_bytes(b"{}")
    elif failure == "source":
        with sqlite3.connect(fixture["ledger"]) as db:
            db.execute("INSERT INTO blocks VALUES ('COST','external')")
    elif failure == "human":
        path = Path(spec["human_authorization"]["path"])
        value = adapter._json(path)
        value["scout_will_not_execute"] = False
        spec["human_authorization"] = write(path, value)
    elif failure == "runtime":
        (fixture["root"] / adapter.CONTROL_PATHS[0]).write_text("CONFIRM_ACTIVE_TRADING=FALSE\n")
    elif failure == "implementation":
        monkeypatch.setattr(adapter, "implementation", lambda: {})
    elif failure == "native_binding":
        (fixture["local"] / "binding.json").write_bytes(b"{}")
    elif failure == "handoff":
        def not_complete(*args):
            raise ValueError("CUTOVER_MATCHING_HANDOFF_NOT_COMPLETE")
        monkeypatch.setattr(adapter, "_completed_handoff", not_complete)
    elif failure == "deadline":
        fixture["clock"] = lambda: datetime.fromisoformat("2026-10-05T11:00:00+00:00")
    with pytest.raises(ValueError):
        run(fixture)
    assert "capture" not in fixture["calls"]
    assert_preparing(fixture)


@pytest.mark.parametrize("point", ["before_install", "before_sqlite_commit", "sqlite_committed", "ledger_installed", "config_activated"])
def test_failures_preserve_originals_and_never_blindly_restore_committed_union(fixture, point):
    old = adapter._read_tables(fixture["ledger"])
    def fail(name):
        if name == point:
            raise RuntimeError("offline injected failure")
    with pytest.raises(RuntimeError, match="offline injected"):
        run(fixture, checkpoint=fail)
    assert_preparing(fixture)
    committed = point in {"sqlite_committed", "ledger_installed", "config_activated"}
    saved = journal(fixture)
    expected = "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED" if committed else "ORIGINALS_PRESERVED_RETRY_REQUIRED"
    assert saved["phase"] == expected
    raw_original = fixture["work"] / saved["attempt"] / "original/holdings.sqlite3"
    assert adapter._group(raw_original) == saved["original_group"]
    assert (adapter._read_tables(fixture["ledger"]) == old) is (not committed)
    if committed:
        result = run(fixture)
        assert result["status"] == expected and not result["broker_read_performed"]
        assert fixture["calls"].count("capture") == 1


@pytest.mark.parametrize("change", ["candidate", "receipt", "implementation", "freshness", "deadline", "control"])
def test_post_capture_changes_are_rechecked_before_activation(fixture, monkeypatch, change):
    now = [fixture["clock"]()]
    fixture["clock"] = lambda: now[0]
    def mutate(point):
        if point != "ledger_installed":
            return
        attempt = fixture["work"] / journal(fixture)["attempt"]
        if change == "candidate":
            with (attempt / "reconciled/holdings.sqlite3").open("ab") as handle:
                handle.write(b"modified")
        elif change == "receipt":
            path = next((fixture["root"] / "state/account-gameplan/cutovers").glob("*.json"))
            path.write_bytes(b"{}")
        elif change == "implementation":
            monkeypatch.setattr(adapter, "implementation", lambda: {})
        elif change == "freshness":
            now[0] += timedelta(seconds=61)
        elif change == "deadline":
            now[0] = datetime.fromisoformat("2026-10-05T11:00:00+00:00")
        elif change == "control":
            (fixture["root"] / adapter.CONTROL_PATHS[0]).write_text("CONFIRM_ACTIVE_TRADING=FALSE\n")
    with pytest.raises(ValueError):
        run(fixture, checkpoint=mutate)
    assert_preparing(fixture)
    assert journal(fixture)["phase"] == "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED"


def test_freshness_crossing_after_own_active_write_restores_only_config(fixture):
    now = [fixture["clock"]()]
    fixture["clock"] = lambda: now[0]
    def mutate(point):
        if point == "config_activated":
            now[0] += timedelta(seconds=61)
    with pytest.raises(ValueError, match="EXPIRED"):
        run(fixture, checkpoint=mutate)
    assert_preparing(fixture)
    assert journal(fixture)["phase"] == "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED"


def test_deadline_crossing_during_last_runtime_guard_never_captures(fixture, monkeypatch):
    now = [fixture["clock"]()]
    fixture["clock"] = lambda: now[0]
    runtime = adapter.runtime_bindings
    calls = 0
    def slow_runtime(root):
        nonlocal calls
        calls += 1
        result = runtime(root)
        if calls == 2:  # First is preflight; second immediately precedes capture.
            now[0] = datetime.fromisoformat("2026-10-05T11:00:00+00:00")
        return result
    monkeypatch.setattr(adapter, "runtime_bindings", slow_runtime)
    before = adapter._read_tables(fixture["ledger"])
    with pytest.raises(ValueError, match="OPENING_DEADLINE_PASSED"):
        run(fixture)
    assert calls == 2 and "capture" not in fixture["calls"]
    assert adapter._read_tables(fixture["ledger"]) == before
    assert_preparing(fixture)


@pytest.mark.parametrize("crossing", ["session", "identity", "portfolio", "verify", None])
def test_production_capture_deadline_stops_subsequent_provider_reads(fixture, monkeypatch, crossing):
    from app.services import schwab
    from ml.stock_trader import state
    now = [fixture["clock"]()]
    calls = []
    def tick(stage):
        calls.append(stage)
        if stage == crossing:
            now[0] = datetime.fromisoformat("2026-10-05T11:00:00+00:00")
    account = load_account_config(fixture["root"])
    class FakeReadOnlySession:
        def __init__(self):
            tick("session")
        def stable_account_fingerprint(self):
            tick("identity")
            return account.account_fingerprint
        def verify_read_snapshot(self, expected):
            assert expected == fixture["args"]["broker_snapshot"].broker_identity_fingerprint
            tick("verify")
    def portfolio(session, **kwargs):
        assert kwargs["symbols"] == account.symbols
        assert kwargs["include_order_identities"] and kwargs["literal_cash_only"]
        assert kwargs["use_actual_quote_timestamps"] and kwargs["parallel"]
        tick("portfolio")
        return fixture["args"]["broker_snapshot"]
    monkeypatch.setattr(schwab, "SchwabSession", FakeReadOnlySession)
    monkeypatch.setattr(state, "capture_portfolio_state", portfolio)
    call = lambda: adapter._capture(account, lambda: now[0],
        deadline_guard=lambda: adapter._deadline(fixture["spec"]["action_date"], lambda: now[0]))
    if crossing is None:
        assert call() == fixture["args"]["broker_snapshot"]
        assert calls == ["session", "identity", "portfolio", "verify", "identity"]
    else:
        with pytest.raises(ValueError, match="OPENING_DEADLINE_PASSED"):
            call()
        stages = ["session", "identity", "portfolio", "verify"]
        assert calls == stages[:stages.index(crossing) + 1]
    assert_preparing(fixture)


def test_foreign_active_config_is_never_overwritten_and_retains_native_exclusion(fixture):
    foreign = None
    def mutate(point):
        nonlocal foreign
        if point == "config_activated":
            path = fixture["root"] / CONFIG
            value = adapter._json(path)
            value["activation"]["receipt_sha256"] = "0" * 64
            foreign = adapter._bytes(value)
            path.write_bytes(foreign)
    with pytest.raises(ValueError):
        run(fixture, checkpoint=mutate)
    assert (fixture["root"] / CONFIG).read_bytes() == foreign
    for name in ("independent-stock-session.lock", "stock-trader-hourly.lock"):
        path = fixture["root"] / "locks" / name
        assert b"recovery-required" in path.read_bytes() and b"pid=" not in path.read_bytes()
        with pytest.raises(RuntimeError):
            with exclusive_runtime_lock(path, process_name="offline-refusal-check"):
                pytest.fail("ambiguous activation lock must not be auto-recovered")


def test_existing_native_lock_is_not_recovered_even_if_pid_is_dead(fixture):
    path = fixture["root"] / "locks/independent-stock-session.lock"
    path.parent.mkdir(parents=True)
    original = b"process=prior-reviewed-operation\npid=999999999\n"
    path.write_bytes(original)
    with pytest.raises(FileExistsError):
        run(fixture)
    assert path.read_bytes() == original and "capture" not in fixture["calls"]
    assert_preparing(fixture)


@pytest.mark.parametrize("alter", ["traversal", "absolute", "number", "operation", "phase"])
def test_recovery_journal_cannot_select_foreign_paths(fixture, alter):
    work = fixture["work"]
    work.mkdir()
    (work / "attempt-0001").mkdir()
    record = {"schema_version": adapter.VERSION, "operation_id": fixture["spec"]["operation_id"],
              "number": 1, "attempt": "attempt-0001", "phase": "PREPARED"}
    if alter == "traversal": record["attempt"] = "../outside"
    elif alter == "absolute": record["attempt"] = str(fixture["root"])
    elif alter == "number": record["number"] = True
    elif alter == "operation": record["operation_id"] = "another-operation"
    elif alter == "phase": record["phase"] = "READY"
    write(work / "journal.json", record)
    with pytest.raises(ValueError, match="JOURNAL"):
        run(fixture)
    assert "capture" not in fixture["calls"]
    assert_preparing(fixture)


def test_disposable_reads_leave_raw_wal_shm_and_no_wal_originals_untouched(fixture):
    path = fixture["ledger"]
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO blocks VALUES ('COST','reader-fixture')")
        writer.commit()
        before = adapter._group(path)
        assert before["-wal"] and before["-shm"]
        tables = adapter._read_tables(path)
        assert tables["blocks"] == [{"symbol": "COST", "reason": "reader-fixture"}]
        assert adapter._group(path) == before
    before = adapter._group(path)
    adapter._read_tables(path)
    assert adapter._group(path) == before


def test_sqlite_transaction_coexists_with_old_wal_reader_without_path_replacement(fixture):
    adapter.reconcile_migration_candidate(**fixture["args"])
    candidate = fixture["args"]["destination_directory"] / "holdings.sqlite3"
    path = fixture["ledger"]
    before = adapter._read_tables(path)
    with closing(sqlite3.connect(path)) as reader:
        reader.execute("PRAGMA journal_mode=WAL")
        reader.execute("BEGIN")
        old_count = reader.execute("SELECT count(*) FROM allocations").fetchone()[0]
        identity = (path.stat().st_dev, path.stat().st_ino)
        expected = adapter._install_rows(path, candidate, before=before, guard=lambda: None, checkpoint=lambda _: None)
        assert (path.stat().st_dev, path.stat().st_ino) == identity
        assert reader.execute("SELECT count(*) FROM allocations").fetchone()[0] == old_count
        assert len(expected["allocations"]) > old_count
        reader.rollback()
        assert reader.execute("SELECT count(*) FROM allocations").fetchone()[0] == len(expected["allocations"])


def test_sqlite_busy_delete_journal_reader_leaves_original_rows(fixture):
    adapter.reconcile_migration_candidate(**fixture["args"])
    candidate = fixture["args"]["destination_directory"] / "holdings.sqlite3"
    path = fixture["ledger"]
    before = adapter._read_tables(path)
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM allocations").fetchone()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            adapter._install_rows(path, candidate, before=before, guard=lambda: None, checkpoint=lambda _: None)
        reader.rollback()
    assert adapter._read_tables(path) == before


def test_native_cleanup_never_removes_same_bytes_at_foreign_file_identity(fixture):
    path = fixture["root"] / "locks/independent-stock-session.lock"
    with adapter._native_lock(path, fixture["root"], fixture["work"], {"active_config_sha256": None}):
        original_identity = path.stat().st_ino
        replacement = path.with_suffix(".foreign")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        assert path.stat().st_ino != original_identity
    assert path.exists()


def test_unreviewed_source_revision_cannot_be_inferred_from_current_files(fixture):
    write(fixture["local"] / "source-revisions/unreviewed.json", {"effective_binding": {}})
    with pytest.raises(ValueError, match="revision"):
        run(fixture)
    assert "capture" not in fixture["calls"]
    assert_preparing(fixture)


def test_pending_orders_cannot_reach_install(fixture):
    fixture["capture"] = lambda account, clock: replace(fixture["args"]["broker_snapshot"], working_order_count=1)
    before = adapter._read_tables(fixture["ledger"])
    with pytest.raises(ValueError):
        run(fixture)
    assert adapter._read_tables(fixture["ledger"]) == before
    assert_preparing(fixture)


def test_cli_does_not_emit_private_provider_errors(fixture, monkeypatch, capsys):
    path = fixture["local"] / "spec-input.json"
    write(path, fixture["spec"])
    def fail(*args):
        raise ValueError("private-account-123456 cash=99442.50 raw-provider-body")
    monkeypatch.setattr(adapter, "preflight", fail)
    assert adapter.main(["--spec", str(path), "--check"]) == 2
    text = capsys.readouterr().out
    assert "private-account" not in text and "99442" not in text and "raw-provider" not in text
    assert json.loads(text)["reason"] == "CUTOVER_REVIEW_OR_RECOVERY_REQUIRED"


def test_atomic_config_rechecks_preimage_after_temporary_fsync(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_bytes(b"original")
    real_fsync = adapter.os.fsync
    def change_during_flush(descriptor):
        real_fsync(descriptor)
        path.write_bytes(b"foreign")
    monkeypatch.setattr(adapter.os, "fsync", change_during_flush)
    with pytest.raises(ValueError, match="PREIMAGE_CHANGED"):
        adapter._atomic(path, b"active", expected_preimage=b"original")
    assert path.read_bytes() == b"foreign"
    assert list(tmp_path.iterdir()) == [path]


@pytest.fixture
def completed_session(tmp_path, monkeypatch):
    """Actual frozen worker artifacts, synthesis, handoff and transport caches."""
    from ml import nightly_workflow as workflow
    from tests import test_nightly_joint_readiness as artifacts
    from tools import nightly_exchange as exchange
    original_configuration = workflow._verify_configuration_binding
    original_symbols = workflow._verify_symbol_binding
    original_json = artifacts._json
    def fixture_json(path, value):
        if path.name == "local-profile.json":
            watch = Path(value["checkout"]) / "datafetching/watchlist.local.txt"
            watch.parent.mkdir(parents=True, exist_ok=True)
            watch.write_text("\n".join(value["symbols"]) + "\n")
            value = {**value, "symbol_profile_path": str(watch)}
        original_json(path, value)
    monkeypatch.setattr(artifacts, "_json", fixture_json)
    native, state, accepted_receipt, handoff_spec = artifacts._prepare(tmp_path, monkeypatch, "atlas")
    monkeypatch.setattr(workflow, "_verify_configuration_binding", original_configuration)
    monkeypatch.setattr(workflow, "_verify_symbol_binding", original_symbols)
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    checkout = Path(native["repository"])
    root = Path(native["datastore"])
    monkeypatch.setattr(adapter, "_repository", lambda: checkout)
    profile = checkout / "scratch/cross-pc/local-profile.json"
    active = checkout / "scratch/cross-pc/active.json"
    write(active, {"fixture": "no-installation-execution"})
    native.update(schema_version=workflow.VERSION, state_root=str(checkout / "scratch/native"),
                  local_profile=str(profile), coordination_active=str(active),
                  reviewer={"model": "offline-fixture", "reasoning_effort": "high"}, peer_communication_enabled=False)
    workflow_path = checkout / "scratch/nightly-workflow/config.json"
    write(workflow_path, native)
    state["configuration_binding"] = workflow._configuration_binding(native)
    state["symbol_binding"] = workflow._symbol_binding(native)
    state_path = Path(native["state_root"]) / "runs" / artifacts.DAY / "state.json"
    write(state_path, state)
    account = load_account_config(root)
    config = {"schema_version": exchange.VERSION, "actor": "Atlas", "workflow_config": str(workflow_path),
              "local_profile": str(profile), "coordination_active": str(active), "account_config": str(root / CONFIG),
              "exchange_root": str(tmp_path / "CODEXSTORE/ducketz-nightly-exchange/v1"),
              "state_root": str(checkout / "scratch/exchange"), "account_scope_sha256": account.account_fingerprint,
              "owners": {actor: owner["symbols"] for actor, owner in handoff_spec["owners"].items()},
              "private_exchange_authorized": True}
    config_path = checkout / "scratch/nightly-workflow/exchange-config.json"
    write(config_path, config)
    # Actual publishers produce the exact self-publication receive cache layout.
    preparations = {}
    for actor in ("atlas", "scout"):
        producer = {**config, "actor": actor.title()}
        owner = handoff_spec["owners"][actor]
        preparations[actor] = exchange._publish(producer, artifacts.DAY, artifacts.REVIEW, "preparation", {
            "plan.json": Path(owner["plan_package"]["path"]), "stats.json": Path(owner["stats_package"]["path"])})
    joint = exchange._publish({**config, "actor": "Scout"}, artifacts.DAY, artifacts.REVIEW, "joint", {
        "joint-plan.json": Path(handoff_spec["joint_plan"]["path"]),
        "scout-receipt.json": Path(handoff_spec["scout_receipt"]["path"]),
        **{actor + "-" + name + ".json": preparation["files"][name + ".json"]
           for actor, preparation in preparations.items() for name in ("plan", "stats")}},
        {**{actor + "_preparation": value["digest"] for actor, value in preparations.items()}, "snapshot": "1" * 64})
    accepted = exchange._publish(config, artifacts.DAY, artifacts.REVIEW, "accepted", {
        "atlas-receipt.json": Path(accepted_receipt["receipt_path"])}, {"joint": joint["digest"]})
    session = Path(config["state_root"]) / "sessions" / artifacts.DAY
    write(session / "handoff-selection.json", {"spec": handoff_spec, "inputs": {"joint": joint["digest"]}})
    for name in ("nightly_exchange.py", "nightly_account_snapshot.py", "nightly_ownership.py"):
        path = checkout / "tools" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(exchange.__file__).with_name(name), path)
    write(session / "binding.json", exchange._binding(config))
    write(session / "status.json", {"status": "COMPLETE", "actor": "Atlas", "action_date": artifacts.DAY,
        "joint_ready": True, "ui_ready": True, "joint_packet_sha256": joint["digest"],
        "accepted_packet_sha256": accepted["digest"]})
    spec = {"action_date": artifacts.DAY, "nightly": {
        "exchange_config_sha256": adapter.file_checksum(config_path),
        "session_binding_sha256": adapter.file_checksum(session / "binding.json"),
        "session_status_sha256": adapter.file_checksum(session / "status.json"),
        "handoff_selection_sha256": adapter.file_checksum(session / "handoff-selection.json"),
        "local_preparation_sha256": adapter.file_checksum(state_path)}}
    return spec, {"datastore_root": str(root)}, account, session, state


def test_actual_completed_handoff_verifier_reads_both_cached_preparations_and_saved_native_outputs(completed_session, tmp_path):
    spec, native, account, session, state = completed_session
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    watched = {}
    verified = adapter._completed_handoff(spec, native, account, watched)
    assert verified["handoff_completion_id"] == "fixture-atlas-completion"
    for actor, kind in (("atlas", "preparation"), ("scout", "preparation"), ("scout", "joint"), ("atlas", "accepted")):
        assert str(session / "selections" / (actor + "-" + kind + ".json")) in watched
    assert str(Path(state["steps"]["model_review"]["output"]["proposal"])) in watched
    assert all(path.read_bytes() == value for path, value in before.items())
    assert set(path for path in tmp_path.rglob("*") if path.is_file()) == set(before)


@pytest.mark.parametrize("damage", ["peer_preparation", "local_preparation", "accepted_packet", "native_output", "frozen_adapter", "binding"])
def test_actual_completed_session_damage_is_rejected(completed_session, damage):
    spec, native, account, session, state = completed_session
    if damage in {"peer_preparation", "local_preparation", "accepted_packet"}:
        actor, kind = {"peer_preparation": ("scout", "preparation"), "local_preparation": ("atlas", "preparation"),
                       "accepted_packet": ("atlas", "accepted")}[damage]
        selected = adapter._json(session / "selections" / (actor + "-" + kind + ".json"))
        path = session / "cache" / actor / kind / selected["content_sha256"] / "packet.json"
    elif damage == "native_output":
        path = Path(state["steps"]["verify_display"]["output"]["source_gameplan_run"]) / "receipt.json"
    elif damage == "frozen_adapter":
        path = adapter._repository() / "tools/nightly_account_snapshot.py"
    else:
        path = session / "binding.json"
    path.write_bytes(path.read_bytes() + b" changed")
    with pytest.raises((ValueError, RuntimeError)):
        adapter._completed_handoff(spec, native, account, {})


def manual_inputs(fixture, monkeypatch, *, stamp="2026-10-05T12:00:00+00:00"):
    """Populate the fixed selectors used by the manual launcher entry point."""
    repo = adapter._repository()
    config_path = repo / "scratch/nightly-workflow/native-ownership-exchange-config.json"
    write(config_path, fixture["native"])
    workflow_path = repo / "scratch/nightly-workflow/config.json"
    preparation_root = repo / "scratch/preparation"
    write(workflow_path, {"state_root": str(preparation_root)})
    exchange_path = repo / "scratch/nightly-workflow/exchange-config.json"
    exchange = adapter._json(exchange_path)
    exchange["workflow_config"] = str(workflow_path)
    write(exchange_path, exchange)
    session = Path(exchange["state_root"]) / "sessions/2026-10-05"
    for name in ("binding.json", "status.json", "handoff-selection.json"):
        write(session / name, {"fixture": "pinned completed session"})
    write(preparation_root / "runs/2026-10-05/state.json", {"fixture": "pinned completed preparation"})
    fixture["clock"] = lambda: datetime.fromisoformat(stamp)
    snapshot = fixture["args"]["broker_snapshot"]
    fixture["args"]["broker_snapshot"] = replace(snapshot, observed_at=stamp,
        quotes={symbol: replace(quote, observed_at=stamp) for symbol, quote in snapshot.quotes.items()})
    from tools import gameplan_execution_readiness
    monkeypatch.setattr(gameplan_execution_readiness, "_handoff", lambda *args: ({"ready": True}, {
        str(fixture["plan"]): adapter.file_checksum(fixture["plan"])}))
    monkeypatch.setattr(adapter, "runtime_bindings", lambda *args: pytest.fail("Manual Start must not require a task-disabled approval gate"))
    return adapter._manual_spec(fixture["root"], clock=fixture["clock"], invoke=True)[0]


@pytest.mark.parametrize("stamp", ["2026-10-05T12:00:00+00:00", "2026-10-06T17:00:00+00:00"])
def test_manual_start_automatically_installs_after_open_or_later_day_without_approval_file(fixture, monkeypatch, stamp):
    manual_inputs(fixture, monkeypatch, stamp=stamp)
    Path(fixture["spec"]["human_authorization"]["path"]).unlink()
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    monkeypatch.setattr(adapter, "reconcile_migration_candidate", lambda **kwargs: pytest.fail("Manual Start must not call the dated legacy route"))
    before_plan = fixture["plan"].read_bytes()
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
                                                 clock=fixture["clock"], capture=fixture["capture"])
    assert result["ready"] and result["status"] == "ACCOUNT_READY" and result["activation_changed"]
    assert result["broker_read_performed"] and fixture["calls"] == ["handoff", "capture"]
    assert not result["trader_started"] and result["orders_placed"] == 0
    work = fixture["local"] / "activation/manual-starts/2026-10-05"
    saved = adapter._json(work / "spec.json")
    assert saved["action_date"] == saved["migration_action_date"] == "2026-10-05"
    assert "human_authorization" not in saved
    assert saved["manual_invocation"]["trigger"] == "EXPLICIT_MANUAL_START"
    assert saved["manual_invocation"]["requested_at"] == datetime.fromisoformat(stamp).isoformat()
    report = adapter._json(work / "attempt-0001/reconciled/report.json")
    assert report["reconciliation_mode"] == "MANUAL_STARTUP" and report["cutover_action_date"] == "2026-10-05"
    assert report["reconciled_at"] == datetime.fromisoformat(stamp).isoformat()
    assert fixture["plan"].read_bytes() == before_plan
    again = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
        clock=fixture["clock"], capture=lambda *args: pytest.fail("ACTIVE must not remerge or capture"))
    assert again == {"status": "ACCOUNT_READY", "ready": True, "activation_changed": False, "broker_read_performed": False}


def test_manual_command_without_first_use_direction_keeps_required_history_gate(fixture, monkeypatch):
    manual_inputs(fixture, monkeypatch)
    (fixture["candidate"] / "manifest.json").unlink()
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
        capture=lambda *args: pytest.fail("Missing peer history must not call broker"))
    assert result["status"] == "ACCOUNT_NOT_READY" and not result["ready"]
    assert result["reason"] == "SCOUT_OWNERSHIP_HISTORY_PENDING"
    assert "capture" not in fixture["calls"]
    assert_preparing(fixture)


def test_manual_first_use_needs_no_scout_packet_union_or_prelaunch_capture(fixture, monkeypatch, tmp_path):
    manual_inputs(fixture, monkeypatch)
    candidate = fixture["candidate"].resolve()
    assert candidate.is_relative_to(tmp_path.resolve())
    shutil.rmtree(candidate)
    (fixture["local"] / "migration-review.json").unlink()
    assert not (fixture["local"] / "scout-selection.json").exists()
    from tools import gameplan_first_use
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    monkeypatch.setattr(gameplan_first_use, "_repository", adapter._repository)
    account = load_account_config(fixture["root"])
    write(adapter._repository() / "scratch/nightly-workflow/first-use.json", {
        "schema_version": "gameplan-first-use-declaration-v1", "operation_id": fixture["native"]["operation_id"],
        "account_binding_sha256": account.fingerprint, "account_fingerprint": account.account_fingerprint,
        "executor": "Atlas", "basis": "LOCAL_HUMAN_ATLAS_SOLE_EXECUTOR", "peer_history_expected": False,
        "manual_start_only": True})
    before = adapter._group(fixture["ledger"])
    plan = fixture["plan"].read_bytes()
    monkeypatch.setattr(adapter, "apply", lambda *a, **k: pytest.fail("First use must not install a union"))
    checked = adapter.inspect_staged_startup(fixture["root"], action_date="2026-10-05", now=fixture["clock"]())
    assert checked["ready"] and checked["status"] == "READY_FOR_MANUAL_START"
    assert checked["startup_mode"] == "ATLAS_SOLE_EXECUTOR"
    assert checked["broker_reconciliation_pending"] and not checked["activation_changed"]
    assert_preparing(fixture)
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
        clock=fixture["clock"], capture=lambda *a: pytest.fail("Normal worker owns account capture"))
    assert result["ready"] and result["status"] == "ACCOUNT_READY" and result["activation_changed"]
    assert result["broker_read_performed"] is False
    assert adapter._group(fixture["ledger"]) == before and fixture["plan"].read_bytes() == plan
    assert "capture" not in fixture["calls"] and not candidate.exists()
    assert not (fixture["local"] / "activation").exists()
    assert verify_cutover(fixture["root"], load_account_config(fixture["root"]))["schema_version"] == "account-gameplan-first-use-v1"


def test_readonly_staged_inspection_is_ready_and_cannot_apply_or_capture(fixture, monkeypatch):
    manual_inputs(fixture, monkeypatch)
    monkeypatch.setattr(adapter, "_capture", lambda *args, **kwargs: pytest.fail("No readiness capture"))
    before = {path: path.read_bytes() for path in fixture["local"].rglob("*") if path.is_file()}
    result = adapter.inspect_staged_startup(fixture["root"], action_date="2026-10-05", now=fixture["clock"]())
    assert result["ready"] and result["status"] == "READY_FOR_MANUAL_START" and result["broker_reconciliation_pending"]
    spec, _ = adapter._manual_spec(fixture["root"], clock=fixture["clock"], invoke=False)
    with pytest.raises(ValueError, match="MANUAL_START_INVOCATION_REQUIRED"):
        adapter.apply(spec, clock=fixture["clock"])
    assert all(path.read_bytes() == value for path, value in before.items())
    assert set(path for path in fixture["local"].rglob("*") if path.is_file()) == set(before)
    assert_preparing(fixture)


@pytest.mark.parametrize("fail_at", ["sqlite_committed", "ledger_installed", "config_activated"])
def test_same_manual_start_retries_committed_union_with_fresh_evidence_and_preserves_all_ids(fixture, monkeypatch, fail_at):
    spec = manual_inputs(fixture, monkeypatch)
    def fail(point):
        if point == fail_at:
            raise RuntimeError("injected manual startup interruption")
    with pytest.raises(RuntimeError, match="interruption"):
        adapter.apply(spec, clock=fixture["clock"], capture=fixture["capture"], checkpoint=fail)
    assert_preparing(fixture)
    committed = adapter._read_tables(fixture["ledger"])
    work = fixture["local"] / "activation/manual-starts/2026-10-05"
    first_spec = (work / "spec.json").read_bytes()
    assert adapter._json(work / "journal.json")["phase"] == "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED"
    # A new real observation is required, without changing the original date.
    manual_inputs(fixture, monkeypatch, stamp="2026-10-05T12:01:00+00:00")
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
                                                 clock=fixture["clock"], capture=fixture["capture"])
    assert result["ready"] and result["activation_changed"]
    after = adapter._read_tables(fixture["ledger"])
    for name, rows in committed.items():
        assert all(row in after[name] for row in rows)
    assert after["snapshots"][:-1] == committed["snapshots"]
    assert len(after["snapshots"]) == len(committed["snapshots"]) + 1
    assert fixture["calls"].count("capture") == 2
    assert (work / "spec.json").read_bytes() == first_spec
    assert len(list((work / "specifications").glob("*.json"))) == 2
    saved = adapter._json(work / "journal.json")
    assert saved["phase"] == "COMPLETE" and saved["number"] == 2
    report = adapter._json(work / "attempt-0002/reconciled/report.json")
    assert report["reconciliation_mode"] == "MANUAL_STARTUP_CONTINUATION"


def test_manual_staged_corrupt_union_reports_blocked_instead_of_throwing(fixture, monkeypatch):
    manual_inputs(fixture, monkeypatch)
    monkeypatch.setattr(adapter, "_pending_continuation", lambda *args: (_ for _ in ()).throw(sqlite3.DatabaseError("private native failure")))
    result = adapter.inspect_staged_startup(fixture["root"], action_date="2026-10-05", now=fixture["clock"]())
    assert result["status"] == "INVENTORY_SETUP_PENDING" and not result["ready"]
    assert result["reason"] == "NATIVE_OWNERSHIP_VALIDATION_FAILED" and "private native" not in json.dumps(result)


def test_manual_continuation_holding_drift_has_specific_reason():
    assert adapter._startup_reason(ValueError("Union native continuation did not become ready: private-native-reason")) == "OWNERSHIP_DIFFERS_FROM_CURRENT_ACCOUNT"


def interrupted_manual(fixture, monkeypatch):
    spec = manual_inputs(fixture, monkeypatch)
    def fail(point):
        if point == "sqlite_committed":
            raise RuntimeError("offline interruption")
    with pytest.raises(RuntimeError, match="interruption"):
        adapter.apply(spec, clock=fixture["clock"], capture=fixture["capture"], checkpoint=fail)
    return fixture["local"] / "activation/manual-starts/2026-10-05"


def test_manual_retry_archives_only_its_dead_crash_locks_and_completes(fixture, monkeypatch):
    work = interrupted_manual(fixture, monkeypatch)
    raw = ("process=native-ownership-cutover\npid=12345\nstarted_at=2026-10-05T12:00:00+00:00\n"
           "token=fixture\noperation=" + fixture["native"]["operation_id"] + "\nwork=" + str(work) + "\n").encode()
    paths = [fixture["root"] / "locks" / name for name in ("independent-stock-session.lock", "stock-trader-hourly.lock")]
    for path in paths:
        path.write_bytes(raw)
    monkeypatch.setattr(adapter, "_pid_is_running", lambda pid: False if pid == 12345 else pytest.fail("Unexpected PID check"))
    manual_inputs(fixture, monkeypatch, stamp="2026-10-05T12:01:00+00:00")
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
        clock=fixture["clock"], capture=fixture["capture"])
    assert result["ready"] and fixture["calls"].count("capture") == 2
    archive = work / "recovered-locks" / adapter._sha(raw)
    assert (archive / "owner.txt").read_bytes() == raw
    for path in paths:
        assert not path.exists()
        receipt = adapter._json(archive / (path.name + ".json"))
        assert receipt["owner_sha256"] == adapter._sha(raw) and receipt["state"] == "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("owner", ["live", "foreign", "pidless", "changed_native"])
def test_manual_retry_preserves_unproven_crash_locks_without_capture(fixture, monkeypatch, owner):
    work = interrupted_manual(fixture, monkeypatch)
    raw = ("process=native-ownership-cutover\npid=12345\noperation=" + fixture["native"]["operation_id"] +
           "\nwork=" + str(work) + "\n").encode()
    if owner == "foreign":
        raw = raw.replace(b"operation=native-test-operation", b"operation=another-operation")
    elif owner == "pidless":
        raw = raw.replace(b"pid=12345\n", b"")
    elif owner == "changed_native":
        with sqlite3.connect(fixture["ledger"]) as db:
            db.execute("INSERT INTO blocks VALUES ('COST','external')")
    path = fixture["root"] / "locks/independent-stock-session.lock"
    path.write_bytes(raw)
    monkeypatch.setattr(adapter, "_pid_is_running", lambda pid: owner == "live")
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
        clock=fixture["clock"], capture=lambda *args: pytest.fail("Unsafe lock recovery must not capture"))
    assert not result["ready"] and path.read_bytes() == raw
    assert not (work / "recovered-locks").exists()
    assert_preparing(fixture)


def test_manual_continuation_unexpected_file_rejects_before_capture(fixture, monkeypatch):
    work = interrupted_manual(fixture, monkeypatch)
    (work / "attempt-0001/reconciled/INVALID").write_text("unexpected fixture")
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
        clock=fixture["clock"], capture=lambda *args: pytest.fail("Invalid prior union must not capture"))
    assert not result["ready"] and fixture["calls"].count("capture") == 1
    assert_preparing(fixture)


def test_manual_continuation_retry_before_second_commit_keeps_last_installed_union(fixture, monkeypatch):
    work = interrupted_manual(fixture, monkeypatch)
    installed = adapter._read_tables(fixture["ledger"])
    spec = manual_inputs(fixture, monkeypatch, stamp="2026-10-05T12:01:00+00:00")
    def fail(point):
        if point == "before_sqlite_commit":
            raise RuntimeError("offline second interruption")
    with pytest.raises(RuntimeError, match="second interruption"):
        adapter.apply(spec, clock=fixture["clock"], capture=fixture["capture"], checkpoint=fail)
    assert adapter._read_tables(fixture["ledger"]) == installed
    second = adapter._json(work / "journal.json")
    assert second["phase"] == "ORIGINALS_PRESERVED_RETRY_REQUIRED"
    assert Path(second["continuation"]["previous_directory"]) == work / "attempt-0001/reconciled"
    manual_inputs(fixture, monkeypatch, stamp="2026-10-05T12:02:00+00:00")
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    result = adapter.ensure_gameplan_account_ready(fixture["root"], sizing_policy=GAMEPLAN_SIZING_POLICY,
        clock=fixture["clock"], capture=fixture["capture"])
    assert result["ready"] and adapter._json(work / "journal.json")["number"] == 3
    assert adapter._read_tables(fixture["ledger"])["snapshots"][:-1] == installed["snapshots"]
