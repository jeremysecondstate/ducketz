"""Private exchange fixtures use real synthesis/adoption, never account providers."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from tools import nightly_exchange as module
from ml import nightly_synthesis, nightly_handoff
from ml.account_gameplan.config import CONFIG, VERSION as ACCOUNT_VERSION, digest
from ml.artifacts import file_checksum
from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from tests.test_nightly_synthesis import specification as source_inputs
from tests.test_joint_capital_plan import NOW, DAY, SCOPE, snapshot

OWNERSHIP_OBSERVATIONS = module._ownership_observations
ENSURE_RESPONDER = module._ensure_ownership_responder
LOCAL_STATE = module._local_state


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(module._bytes(value))
    return path


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def rendezvous(exchange, monkeypatch):
    from tools import nightly_ownership
    wake(exchange, "scout")
    assert wake(exchange, "atlas")["reason"] == "JOINT_SCOUT"
    configs, natives, _, captures = exchange
    preparations = {actor: module._receive(configs["atlas"], DAY, "2026-09-08", actor, "preparation")
                    for actor in ("atlas", "scout")}
    inputs = {f"{actor}_preparation": selected["digest"] for actor, selected in preparations.items()}
    tick, ledger_reads = [0.0], []
    clock = lambda: pd.Timestamp(NOW) + pd.Timedelta(seconds=tick[0])
    def capture(config, now=None):
        ledger_reads.append(config["actor"])
        return {"actor": config["actor"], "ledger_observed_at": pd.Timestamp(now).isoformat(),
                "held_basis": "SAVED_READY_RECONCILIATION", "saved_baseline_at": "2026-09-08T12:00:00Z"}
    def validate(config, values, observed_at):
        assert [value["actor"] for value in values] == ["Atlas", "Scout"]
        assert all(0 <= (observed_at - pd.Timestamp(value["ledger_observed_at"])).total_seconds() <= 60 for value in values)
    monkeypatch.setattr(nightly_ownership, "capture_ownership", capture)
    monkeypatch.setattr(nightly_ownership, "validate_observations", validate)
    def sleep(seconds):
        tick[0] += seconds
        module._respond_ownership(configs["scout"], natives["scout"], DAY, "2026-09-08", inputs, clock)
    monkeypatch.setattr(module, "sleep", sleep)
    monkeypatch.setattr(module, "monotonic", lambda: tick[0])
    return configs, natives, inputs, tick, clock, ledger_reads


@pytest.fixture
def exchange(source_inputs, tmp_path, monkeypatch):
    configs, states, natives = {}, {}, {}
    # Synthetic wake timestamps are explicit and may repeat. Keep their clock
    # deterministic except tests that deliberately advance capture/read time.
    monkeypatch.setattr(module, "monotonic", lambda: 10)
    monkeypatch.setattr(module, "_ensure_ownership_responder", lambda *args: None)
    monkeypatch.setattr(module, "_ownership_observations", lambda *args: [])
    for actor in ("scout", "atlas"):
        checkout = tmp_path / f"{actor}-checkout"
        root = tmp_path / f"{actor}-data"
        profile = write(checkout / "scratch/cross-pc/local-profile.json", {
            "contract_version": "cross-pc-v2", "actor": actor.title(),
            "machine": "pc-new" if actor == "scout" else "pc-original",
            "checkout": str(checkout), "symbols": source_inputs["owners"][actor]["symbols"]})
        active = write(checkout / "scratch/cross-pc/active.json", {"fixture": "pinned"})
        native = {"actor": actor.title(), "repository": str(checkout), "datastore": str(root),
                  "state_root": str(checkout / "scratch/native"), "local_profile": str(profile), "coordination_active": str(active)}
        native_path = write(checkout / "scratch/workflow.json", native)
        config = {"schema_version": module.VERSION, "actor": actor.title(), "workflow_config": str(native_path),
            "local_profile": str(profile), "coordination_active": str(active),
            "exchange_root": str(tmp_path / "CODEXSTORE/ducketz-nightly-exchange/v1"),
            "state_root": str(checkout / "scratch/exchange"), "account_scope_sha256": SCOPE,
            "owners": {owner: item["symbols"] for owner, item in source_inputs["owners"].items()},
            "private_exchange_authorized": True}
        if actor == "atlas":
            account = {"schema_version": ACCOUNT_VERSION, "machine_id": "pc-original", "coordinator_id": "pc-original",
                "participants": {"pc-original": config["owners"]["atlas"], "pc-new": config["owners"]["scout"]},
                "account_fingerprint": SCOPE}
            account["activation"] = {"status": "PREPARING", "binding_sha256": digest(account)}
            config["account_config"] = str(write(root / CONFIG, account))
        owner = source_inputs["owners"][actor]
        output = {"package": owner["plan_package"]["path"], "stats_package": owner["stats_package"]["path"]}
        output["files"] = {path: file_checksum(Path(path)) for path in output.values()}
        from ml.nightly_exchange_repair import _source_from_files, _inventory
        states[actor] = {"action_date": DAY, "source_identity": _source_from_files("a" * 40, _inventory(checkout)),
                         "steps": {"local_handoff": {"output": output}}}
        configs[actor], natives[actor] = config, native
    monkeypatch.setattr(module.workflow, "load_config", lambda path: json.loads(path.read_text()))
    monkeypatch.setattr(module.workflow, "verify_installation", lambda _: None)
    monkeypatch.setattr(module.workflow, "source_identity", lambda root: _source_from_files("a" * 40, _inventory(root)))
    monkeypatch.setattr(module, "_local_state", lambda config, *_: deepcopy(states[config["actor"].lower()]))
    monkeypatch.setattr(nightly_synthesis, "__file__", str(Path(natives["scout"]["repository"]) / "ml/nightly_synthesis.py"))
    monkeypatch.setattr(nightly_handoff, "__file__", str(Path(natives["atlas"]["repository"]) / "ml/nightly_handoff.py"))
    from tools import nightly_account_snapshot
    captures = []
    def capture(config, now=None, ownership_observations=None):
        assert config["actor"] == "Atlas"
        captures.append(now)
        value = snapshot()
        value["observed_at"] = pd.Timestamp(now).isoformat()
        return value
    monkeypatch.setattr(nightly_account_snapshot, "capture_snapshot", capture)
    return configs, natives, states, captures


def wake(exchange, actor, *, now=NOW, capture=False):
    return module.run_once(exchange[0][actor], now=now, allow_snapshot_refresh=capture)


def prepared(exchange):
    assert wake(exchange, "scout")["reason"] == "PREPARATION_ATLAS"
    assert wake(exchange, "atlas", capture=True)["reason"] == "JOINT_SCOUT"


def synthesized(exchange):
    prepared(exchange)
    assert wake(exchange, "scout")["status"] == "COMPLETE"


def test_two_machine_real_synthesis_handoff_and_repeat_without_changes(exchange):
    configs, natives, _, captures = exchange
    synthesized(exchange)
    accepted = wake(exchange, "atlas")
    assert accepted["status"] == "COMPLETE" and accepted["joint_ready"] and not accepted["peer_verified"]
    confirmed = wake(exchange, "scout")
    assert confirmed["status"] == "COMPLETE" and confirmed["joint_packet_sha256"] == accepted["joint_packet_sha256"]
    for actor, native in natives.items():
        assert set(load_gameplan(Path(native["datastore"])).symbols) == {"AAPL", "ABCL"}
        assert set(load_gameplan_stats(Path(native["datastore"])).symbols) == {"AAPL", "ABCL"}
    private_before = files(Path(configs["scout"]["exchange_root"]))
    source_before = {actor: files(Path(native["datastore"])) for actor, native in natives.items()}
    assert wake(exchange, "atlas", now="2026-09-09T12:00:00Z", capture=True) == accepted
    assert wake(exchange, "scout", now="2026-09-09T12:00:00Z") == confirmed
    assert files(Path(configs["scout"]["exchange_root"])) == private_before
    assert {actor: files(Path(native["datastore"])) for actor, native in natives.items()} == source_before
    assert len(captures) == 0
    assert not accepted["execution_authorized"] and accepted["orders_placed"] == 0
    assert json.loads(Path(configs["atlas"]["account_config"]).read_text())["activation"]["status"] == "PREPARING"


def test_completed_session_uses_retained_evidence_without_stage_or_peer_replay(exchange, monkeypatch):
    synthesized(exchange)
    expected = {"atlas": wake(exchange, "atlas"), "scout": wake(exchange, "scout")}
    local_before = {actor: files(Path(native["datastore"])) for actor, native in exchange[1].items()}
    state_before = {actor: files(Path(config["state_root"])) for actor, config in exchange[0].items()}
    def forbidden(*args, **kwargs):
        pytest.fail("A completed session cannot replay any preparation, capture, synthesis or adoption")
    for name in ("_run", "_local_state", "_publish", "_binding"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(nightly_synthesis, "run_synthesis", forbidden)
    monkeypatch.setattr(nightly_handoff, "run_handoff", forbidden)
    # A synced drive may be unavailable after completion. Retained verified
    # input bytes and the dated local publications establish historical result.
    shared = Path(exchange[0]["atlas"]["exchange_root"])
    shared.rename(shared.with_name("offline-v1"))
    for actor in ("atlas", "scout"):
        assert wake(exchange, actor, now="2026-09-09T23:30:00Z", capture=actor == "atlas") == expected[actor]
    assert {actor: files(Path(native["datastore"])) for actor, native in exchange[1].items()} == local_before
    assert {actor: files(Path(config["state_root"])) for actor, config in exchange[0].items()} == state_before
    assert len(exchange[3]) == 0


def test_damaged_terminal_evidence_never_downgrades_completion_or_replays(exchange, monkeypatch):
    synthesized(exchange)
    wake(exchange, "atlas")
    session = Path(exchange[0]["atlas"]["state_root"]) / "sessions" / DAY
    before = (session / "status.json").read_bytes()
    receipt = next((session / "handoff").glob("*/receipt.json"))
    receipt.write_bytes(receipt.read_bytes() + b" ")
    monkeypatch.setattr(module, "_run", lambda *a, **kw: pytest.fail("Never replay terminal work"))
    for _ in range(2):
        with pytest.raises(ValueError, match="Completed exchange local receipt changed"):
            wake(exchange, "atlas", capture=True)
        assert (session / "status.json").read_bytes() == before
    assert len(exchange[3]) == 0


def test_no_snapshot_capture_even_when_compatibility_flag_present(exchange):
    assert wake(exchange, "atlas", capture=True)["reason"] == "PREPARATION_SCOUT"
    assert exchange[3] == []
    assert wake(exchange, "scout")["status"] == "COMPLETE"
    assert wake(exchange, "atlas")["status"] == "COMPLETE"
    assert exchange[3] == []
    with pytest.raises(ValueError, match="Only Atlas"):
        wake(exchange, "scout", capture=True)


@pytest.mark.parametrize("damage", ["absent", "short"])
def test_partial_preparation_packet_is_pending_and_never_captured(exchange, damage):
    wake(exchange, "scout")
    folder = module._folder(exchange[0]["atlas"], DAY, "scout", "preparation")
    selector = json.loads((folder / "selection.json").read_text())
    payload = folder / "packets" / (selector["content_sha256"] + ".json")
    data = payload.read_bytes()
    if damage == "absent":
        payload.unlink()
    else:
        payload.write_bytes(data[:100])
    assert wake(exchange, "atlas", capture=True)["reason"] == "PACKET_SYNC"
    assert not exchange[3]
    payload.write_bytes(data)
    assert wake(exchange, "atlas", capture=True)["reason"] == "JOINT_SCOUT"


def test_complete_corrupt_packet_is_error_not_fallback(exchange):
    wake(exchange, "scout")
    folder = module._folder(exchange[0]["atlas"], DAY, "scout", "preparation")
    selector = json.loads((folder / "selection.json").read_text())
    payload = folder / "packets" / (selector["content_sha256"] + ".json")
    data = payload.read_bytes()
    payload.write_bytes(b"X" + data[1:])
    with pytest.raises(ValueError, match="Complete selected packet bytes"):
        wake(exchange, "atlas", capture=True)
    assert not exchange[3]


def test_partial_joint_packet_blocks_refresh_even_when_snapshot_stale(exchange):
    synthesized(exchange)
    folder = module._folder(exchange[0]["atlas"], DAY, "scout", "joint")
    selector = json.loads((folder / "selection.json").read_text())
    payload = folder / "packets" / (selector["content_sha256"] + ".json")
    original = payload.read_bytes()
    payload.write_bytes(original[:100])
    assert wake(exchange, "atlas", now="2026-09-09T10:20:00Z", capture=True)["reason"] == "PACKET_SYNC"
    payload.write_bytes(original)
    # Restored peer delivery later in the market session adopts the exact
    # already synthesized result; its old account observation is not recaptured.
    assert wake(exchange, "atlas", now="2026-09-09T21:00:00Z", capture=True)["status"] == "COMPLETE"
    assert len(exchange[3]) == 0




def test_pre_adoption_crash_resumes_exact_frozen_plan_without_freshness_gate(exchange, monkeypatch):
    prepared(exchange)
    original = nightly_synthesis.run_synthesis
    monkeypatch.setattr(nightly_synthesis, "run_synthesis", lambda *a, **kw: (_ for _ in ()).throw(InterruptedError("fixture interruption")))
    with pytest.raises(InterruptedError):
        wake(exchange, "scout")
    frozen = Path(exchange[0]["scout"]["state_root"]) / "sessions" / DAY / "synthesis-selection.json"
    before = frozen.read_bytes()
    monkeypatch.setattr(nightly_synthesis, "run_synthesis", original)
    assert wake(exchange, "scout", now="2026-09-09T10:16:00Z")["status"] == "COMPLETE"
    assert frozen.read_bytes() == before
    assert (Path(exchange[1]["scout"]["datastore"]) / "ml/joint-gameplan-by-date").exists()


def test_partial_ui_adoption_resumes_exact_spec_and_does_not_recapture(exchange, monkeypatch):
    prepared(exchange)
    original = nightly_synthesis._adopt_stats
    def interrupted(root, *args):
        if root == Path(exchange[1]["scout"]["datastore"]):
            raise InterruptedError("fixture stop after local plan adoption")
        return original(root, *args)
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", interrupted)
    with pytest.raises(InterruptedError):
        wake(exchange, "scout")
    frozen = Path(exchange[0]["scout"]["state_root"]) / "sessions" / DAY / "synthesis-selection.json"
    before = frozen.read_bytes()
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", original)
    assert wake(exchange, "scout", now="2026-09-09T11:00:00Z")["status"] == "COMPLETE"
    assert frozen.read_bytes() == before and len(exchange[3]) == 0


def test_changed_export_does_not_replace_selected_preparation(exchange):
    wake(exchange, "scout")
    shared = Path(exchange[0]["scout"]["exchange_root"])
    before = files(shared)
    package = Path(exchange[2]["scout"]["steps"]["local_handoff"]["output"]["package"])
    package.write_bytes(package.read_bytes() + b" ")
    with pytest.raises(ValueError, match="export changed before copying"):
        wake(exchange, "scout")
    assert files(shared) == before


def test_binding_change_is_refused_before_private_exports(exchange):
    wake(exchange, "scout")
    before = files(Path(exchange[0]["scout"]["exchange_root"]))
    exchange[0]["scout"]["account_scope_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="immutable"):
        wake(exchange, "scout")
    assert files(Path(exchange[0]["scout"]["exchange_root"])) == before


def test_scope_and_universe_mismatch_rejected(exchange):
    wake(exchange, "scout")
    config = deepcopy(exchange[0]["atlas"])
    config["account_scope_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="identity, scope"):
        module._receive(config, DAY, "2026-09-08", "scout", "preparation")
    config["account_scope_sha256"] = SCOPE
    config["owners"]["scout"] = ["OTHER"]
    with pytest.raises(ValueError, match="identity, scope"):
        module._receive(config, DAY, "2026-09-08", "scout", "preparation")


















def test_pending_local_preparation_does_not_freeze_setup_bindings(exchange, monkeypatch):
    def unavailable(*args):
        raise module.Pending("LOCAL_PREPARATION")
    monkeypatch.setattr(module, "_local_state", unavailable)
    assert wake(exchange, "scout")["reason"] == "LOCAL_PREPARATION"
    session = Path(exchange[0]["scout"]["state_root"]) / "sessions" / DAY
    assert not (session / "binding.json").exists()
    exchange[0]["scout"]["account_scope_sha256"] = "b" * 64
    assert wake(exchange, "scout")["reason"] == "LOCAL_PREPARATION"
    assert not (session / "binding.json").exists()
    assert not Path(exchange[0]["scout"]["exchange_root"]).exists()




@pytest.mark.parametrize("provenance", ["adapter_sources", "coordination_active_sha256"])
def test_source_provenance_change_preserves_original_binding_and_delivery(exchange, monkeypatch, provenance):
    prepared(exchange)
    originals = {Path(config["state_root"]) / "sessions" / DAY / "binding.json":
                 (Path(config["state_root"]) / "sessions" / DAY / "binding.json").read_bytes()
                 for config in exchange[0].values()}
    original = module._binding
    def changed(config):
        binding = original(config)
        if provenance == "adapter_sources":
            binding[provenance]["nightly_exchange.py"] = "0" * 64
        else:
            binding[provenance] = "0" * 64
        return binding
    monkeypatch.setattr(module, "_binding", changed)
    scout = wake(exchange, "scout")
    atlas = wake(exchange, "atlas")
    assert scout["status"] == atlas["status"] == "COMPLETE"
    assert scout["joint_packet_sha256"] == atlas["joint_packet_sha256"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert not exchange[3]


@pytest.mark.parametrize("field", ["workflow_config_sha256", "profile_sha256", "account_config_sha256"])
def test_private_binding_hash_change_still_refuses_export(exchange, monkeypatch, field):
    wake(exchange, "atlas")
    config = exchange[0]["atlas"]
    binding_path = Path(config["state_root"]) / "sessions" / DAY / "binding.json"
    binding_before = binding_path.read_bytes()
    shared_before = files(Path(config["exchange_root"]))
    original = module._binding
    def changed(config):
        binding = original(config)
        binding[field] = "0" * 64
        return binding
    monkeypatch.setattr(module, "_binding", changed)
    with pytest.raises(ValueError, match="immutable"):
        wake(exchange, "atlas")
    assert binding_path.read_bytes() == binding_before
    assert files(Path(config["exchange_root"])) == shared_before


def test_first_export_freezes_current_binding_without_reading_old_repair_metadata(exchange, monkeypatch):
    from ml import nightly_exchange_repair as repair
    config = exchange[0]["scout"]
    session = Path(config["state_root"]) / "sessions" / DAY
    metadata = session / "repair-transitions.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_bytes(b"obsolete unreadable transition metadata\n")
    before = metadata.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("Historical source repair metadata is not a nightly operating prerequisite")
    for name in ("original_binding", "verify_transition", "dispatch_guard", "resolve"):
        monkeypatch.setattr(repair, name, forbidden)
    monkeypatch.setattr(repair.registry, "read", forbidden)
    assert wake(exchange, "scout")["reason"] == "PREPARATION_ATLAS"
    assert json.loads((session / "binding.json").read_text()) == module._binding(config)
    assert metadata.read_bytes() == before




def test_acceptance_with_wrong_stats_is_not_completion(exchange):
    synthesized(exchange)
    assert wake(exchange, "atlas")["status"] == "COMPLETE"
    config = exchange[0]["scout"]
    joint = module._receive(config, DAY, "2026-09-08", "scout", "joint")
    accepted = module._receive(config, DAY, "2026-09-08", "atlas", "accepted")
    receipt_path = accepted["files"]["atlas-receipt.json"]
    receipt = json.loads(receipt_path.read_text())
    receipt["stats_packages"]["Atlas"] = "b" * 64
    write(receipt_path, receipt)
    with pytest.raises(ValueError, match="exact Scout synthesis"):
        module._verify_acceptance(config, DAY, "2026-09-08", joint, accepted)


@pytest.mark.parametrize("stamp,action,review", [
    ("2026-10-08T02:00:00Z", "2026-10-08", "2026-10-07"),
    ("2026-10-08T10:30:00Z", "2026-10-08", "2026-10-07"),
    ("2026-10-10T04:00:00Z", "2026-10-12", "2026-10-09"),
])
def test_target_uses_completed_session_successor(stamp, action, review):
    assert module._context(stamp) == (action, review)


def test_config_check_binds_exact_checkout_profile_release_and_disjoint_22_symbols(exchange, monkeypatch, tmp_path):
    config = deepcopy(exchange[0]["scout"])
    config["owners"] = {"scout": [f"S{i}" for i in range(11)], "atlas": [f"A{i}" for i in range(11)]}
    profile = Path(config["local_profile"])
    value = json.loads(profile.read_text())
    value["symbols"] = config["owners"]["scout"]
    write(profile, value)
    selected = write(tmp_path / "config.json", config)
    monkeypatch.setattr(module, "__file__", str(Path(value["checkout"]) / "tools/nightly_exchange.py"))
    assert module.load_config(selected) == config
    before = files(tmp_path)
    assert module.main(["--config", str(selected), "--check"]) == 0
    assert files(tmp_path) == before
    monkeypatch.setattr(module, "__file__", str(tmp_path / "different/tools/nightly_exchange.py"))
    with pytest.raises(ValueError, match="installed local profile"):
        module.load_config(selected)


@pytest.mark.parametrize("field,value", [("private_exchange_authorized", False), ("account_scope_sha256", "unset"),
    ("exchange_root", "relative/folder"), ("actor", "scout")])
def test_config_rejects_nonoperating_template_or_invalid_binding(exchange, tmp_path, field, value):
    config = deepcopy(exchange[0]["scout"])
    config[field] = value
    path = write(tmp_path / "bad-config.json", config)
    with pytest.raises(ValueError):
        module.load_config(path)


def test_actual_local_preparation_reader_accepts_frozen_evidence_after_combined_adoption(tmp_path, monkeypatch):
    from tests.test_nightly_joint_readiness import _prepare, DAY as action, REVIEW as review
    native, state, _, _ = _prepare(tmp_path, monkeypatch, "scout")
    native["state_root"] = str(tmp_path / "native")
    write(Path(native["state_root"]) / "runs" / action / "state.json", state)
    config = {"actor": "Scout", "state_root": str(tmp_path / "exchange")}
    assert module._local_state(config, native, action, review) == state
    source = Path(state["steps"]["verify_display"]["output"]["source_gameplan_run"]) / "receipt.json"
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(ValueError, match="output changed"):
        module._local_state(config, native, action, review)


def test_legacy_ownership_handshake_retains_original_observations(exchange, rendezvous):
    configs, _, inputs, _, clock, reads = rendezvous
    OWNERSHIP_OBSERVATIONS(configs["atlas"], DAY, "2026-09-08", inputs, clock)
    assert reads == ["Scout", "Atlas"] and not exchange[3]
    original = list(reads)
    values = OWNERSHIP_OBSERVATIONS(configs["atlas"], DAY, "2026-09-08", inputs, clock)
    assert reads == original and values[0]["actor"] == "Atlas"


def test_ownership_timeout_has_no_ledger_or_account_reads(exchange, rendezvous, monkeypatch):
    tick = rendezvous[3]
    monkeypatch.setattr(module, "sleep", lambda seconds: tick.__setitem__(0, tick[0] + seconds))
    config, inputs, clock = rendezvous[0]['atlas'], rendezvous[2], rendezvous[4]
    with pytest.raises(module.Pending, match="FRESH_SCOUT_OWNERSHIP_RESPONSE"):
        OWNERSHIP_OBSERVATIONS(config, DAY, "2026-09-08", inputs, clock)
    assert tick[0] == 50 and not rendezvous[5] and not exchange[3]
    config = exchange[0]["atlas"]
    original = module._receive(config, DAY, "2026-09-08", "atlas", "ownership_request")["digest"]
    tick[0] = 60
    with pytest.raises(module.Pending, match="FRESH_SCOUT_OWNERSHIP_RESPONSE"):
        OWNERSHIP_OBSERVATIONS(config, DAY, "2026-09-08", inputs, clock)
    assert module._receive(config, DAY, "2026-09-08", "atlas", "ownership_request")["digest"] != original
    assert not exchange[3]


def test_slow_response_read_past_deadline_never_captures_atlas(exchange, rendezvous, monkeypatch):
    original = module._receive
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[4] == "ownership":
            rendezvous[3][0] = 51
        return result
    monkeypatch.setattr(module, "_receive", slow)
    with pytest.raises(module.Pending, match="FRESH_SCOUT_OWNERSHIP_RESPONSE"):
        OWNERSHIP_OBSERVATIONS(rendezvous[0]['atlas'], DAY, "2026-09-08", rendezvous[2], rendezvous[4])
    assert rendezvous[5] == ["Scout"] and not exchange[3]


def test_ownership_response_is_not_restamped_for_same_challenge(rendezvous):
    configs, natives, inputs, tick, clock, reads = rendezvous
    OWNERSHIP_OBSERVATIONS(configs["atlas"], DAY, "2026-09-08", inputs, clock)
    before = files(module._folder(configs["scout"], DAY, "scout", "ownership"))
    tick[0] += 1
    assert module._respond_ownership(configs["scout"], natives["scout"], DAY, "2026-09-08", inputs, clock)
    assert reads == ["Scout", "Atlas"]
    assert files(module._folder(configs["scout"], DAY, "scout", "ownership")) == before


def test_unknown_ownership_selector_cannot_be_overwritten(rendezvous):
    configs, _, inputs, _, clock, _ = rendezvous
    config = configs["atlas"]
    write(module._folder(config, DAY, "atlas", "ownership_request") / "selection.json",
          {"content_sha256": "b" * 64})
    with pytest.raises(ValueError, match="Unknown ownership publication"):
        OWNERSHIP_OBSERVATIONS(config, DAY, "2026-09-08", inputs, clock)


def test_responder_startup_dedup_restart_keeps_deadline_and_live_lock(rendezvous, monkeypatch):
    configs, _, inputs, tick, clock, _ = rendezvous
    config = configs["scout"]
    launches = []
    monkeypatch.setattr(module, "_spawn_responder", lambda *args: launches.append(args))
    ENSURE_RESPONDER(config, DAY, "2026-09-08", inputs, clock())
    record_path = Path(config["state_root"]) / "sessions" / DAY / "ownership-responder.json"
    record = json.loads(record_path.read_text())
    tick[0] = 5
    ENSURE_RESPONDER(config, DAY, "2026-09-08", inputs, clock())
    assert len(launches) == 1
    tick[0] = 15
    ENSURE_RESPONDER(config, DAY, "2026-09-08", inputs, clock())
    assert len(launches) == 2
    assert json.loads(record_path.read_text())["deadline_at"] == record["deadline_at"]
    with module.FileLock(str(Path(config["state_root"]) / "ownership-responder.lock")):
        tick[0] = 400
        ENSURE_RESPONDER(config, DAY, "2026-09-08", inputs, clock())
    assert len(launches) == 2


def test_responder_bounded_loop_expires_without_request(rendezvous, monkeypatch):
    config = rendezvous[0]["scout"]
    monkeypatch.setattr(module, "_spawn_responder", lambda *args: 123)
    ENSURE_RESPONDER(config, DAY, "2026-09-08", rendezvous[2], rendezvous[4]())
    monkeypatch.setattr(module, "sleep", lambda seconds: rendezvous[3].__setitem__(0, rendezvous[3][0] + 60))
    result = module.serve_ownership(config, now=NOW)
    assert result["status"] == "OWNERSHIP_RESPONDER_FINISHED"
    assert rendezvous[3][0] == 360 and not rendezvous[5]


def test_responder_source_change_is_durable_failure_without_export(rendezvous, monkeypatch):
    config = rendezvous[0]["scout"]
    monkeypatch.setattr(module, "_spawn_responder", lambda *args: 123)
    ENSURE_RESPONDER(config, DAY, "2026-09-08", rendezvous[2], rendezvous[4]())
    original = module._binding
    monkeypatch.setattr(module, "_binding", lambda value: {**original(value), "fixture_changed": True})
    with pytest.raises(ValueError, match="source, bindings"):
        module.serve_ownership(config, now=NOW)
    result = json.loads((Path(config["state_root"]) / "sessions" / DAY / "ownership-responder-result.json").read_text())
    assert result["status"] == "FAILED" and not rendezvous[5]
    assert not module._folder(config, DAY, "scout", "ownership").exists()


def test_hidden_responder_launch_has_no_recursive_refresh(monkeypatch, tmp_path):
    seen = {}
    def popen(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return type("Child", (), {"pid": 123})()
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    assert module._spawn_responder(tmp_path / "config.json", tmp_path) == 123
    assert "--serve-ownership" in seen["command"] and "--allow-snapshot-refresh" not in seen["command"]
    if module.os.name == "nt":
        assert seen["kwargs"]["creationflags"] == module.subprocess.CREATE_NO_WINDOW
    assert seen["kwargs"]["stdin"] == module.subprocess.DEVNULL


def test_responder_cli_dispatches_only_child_mode(exchange, monkeypatch, tmp_path):
    config = exchange[0]["scout"]
    monkeypatch.setattr(module, "load_config", lambda _: config)
    monkeypatch.setattr(module, "serve_ownership", lambda _: {"status": "fixture-served"})
    monkeypatch.setattr(module, "run_once", lambda *a, **kw: pytest.fail("child must not launch an exchange wake"))
    assert module.main(["--config", str(tmp_path / "config.json"), "--serve-ownership"]) == 0
    assert module.main(["--config", str(tmp_path / "config.json"), "--serve-ownership", "--check"]) == 1


def test_responder_does_not_publish_after_its_own_deadline(rendezvous, monkeypatch):
    configs, natives, inputs, tick, clock, reads = rendezvous
    request = {"schema_version": "nightly-ownership-request-v1", "challenge": "a" * 64,
               "requested_at": clock().isoformat(), "expires_at": (clock() + pd.Timedelta(seconds=50)).isoformat()}
    module._publish(configs["atlas"], DAY, "2026-09-08", "ownership_request", {"request.json": module._bytes(request)}, inputs)
    from tools import nightly_ownership
    original = nightly_ownership.capture_ownership
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        tick[0] += 2
        return result
    monkeypatch.setattr(nightly_ownership, "capture_ownership", delayed)
    assert not module._respond_ownership(configs["scout"], natives["scout"], DAY, "2026-09-08", inputs, clock,
                                         responder_deadline=clock() + pd.Timedelta(seconds=1))
    assert reads == ["Scout"]
    assert not module._folder(configs["scout"], DAY, "scout", "ownership").exists()


def test_active_account_retains_native_union_snapshot_without_peer_ledger(exchange, monkeypatch):
    config = exchange[0]["atlas"]
    path = Path(config["account_config"])
    value = json.loads(path.read_text())
    value["activation"]["status"] = "ACTIVE"
    write(path, value)
    monkeypatch.setattr(module, "_ownership_observations", lambda *args: pytest.fail("ACTIVE uses its native union ledger"))
    wake(exchange, "scout")
    assert wake(exchange, "atlas", capture=True)["reason"] == "JOINT_SCOUT"
    assert len(exchange[3]) == 0


@pytest.mark.parametrize('activation', ['PREPARING', 'ACTIVE'])
def test_complete_joint_handoff_never_requests_scout_history(exchange, monkeypatch, activation):
    path = Path(exchange[0]['atlas']['account_config'])
    value = json.loads(path.read_text()); value['activation']['status'] = activation
    write(path, value)
    for name in ('_ownership_observations', '_ensure_ownership_responder'):
        monkeypatch.setattr(module, name, lambda *a: pytest.fail('Scout history is not a planning dependency'))
    wake(exchange, 'scout')
    assert wake(exchange, 'atlas', capture=True)['reason'] == 'JOINT_SCOUT'
    assert wake(exchange, 'scout')['status'] == 'COMPLETE'
    assert wake(exchange, 'atlas')['status'] == 'COMPLETE'
    assert wake(exchange, 'scout')['status'] == 'COMPLETE'
    assert len(exchange[3]) == 0




@pytest.mark.parametrize("old_transitions", [b"unreadable historical metadata\n", b'{"obsolete": true}\n'])
def test_source_change_and_old_repair_metadata_resume_same_partial_adoption(exchange, monkeypatch, old_transitions):
    from ml import nightly_exchange_repair as repair
    from ml import nightly_repair_registry as registry
    configs, natives, states, captures = exchange
    for actor in ("scout", "atlas"):
        native = natives[actor]
        repo = Path(native["repository"])
        (repo / "ml").mkdir(exist_ok=True)
        (repo / "ml/nightly_synthesis.py").write_text("# original fixture source\n")
        state = {"schema_version": module.workflow.VERSION, "actor": actor.title(), "action_date": DAY,
                 "source_session": "2026-09-08", "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING", "owner_pid": None,
                 "source_identity": module.workflow.source_identity(repo), "steps": {}}
        for step in module.workflow.workflow_steps(state):
            state["steps"][step] = {"status": "COMPLETE", "output": states[actor]["steps"]["local_handoff"]["output"]}
        states[actor] = state
        write(Path(native["state_root"]) / "runs" / DAY / "state.json", state)
    monkeypatch.setattr(module.workflow, "_verify_configuration_binding", lambda *args: None)
    monkeypatch.setattr(module.workflow, "_verify_symbol_binding", lambda *args: None)
    monkeypatch.setattr(module.workflow, "_verify_local_preparation", lambda *args: None)
    monkeypatch.setattr(module, "_local_state", LOCAL_STATE)
    prepared(exchange)
    original_adopt, attempts = nightly_synthesis._adopt_stats, []
    def defect(root, *args):
        if root == Path(natives["scout"]["datastore"]):
            attempts.append(True)
            raise ValueError("reproduced deterministic Stats-adoption defect")
        return original_adopt(root, *args)
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", defect)
    with pytest.raises(ValueError, match="deterministic"):
        wake(exchange, "scout")
    assert len(attempts) == 1
    config, native = configs["scout"], natives["scout"]
    session = Path(config["state_root"]) / "sessions" / DAY
    retained_paths = [session / "synthesis-selection.json", session / "failure.json"]
    for actor in ("scout", "atlas"):
        actor_session = Path(configs[actor]["state_root"]) / "sessions" / DAY
        transitions = actor_session / "repair-transitions.json"
        transitions.write_bytes(old_transitions)
        retained_paths.extend([transitions, actor_session / "binding.json",
                               Path(natives[actor]["state_root"]) / "runs" / DAY / "state.json"])
        retained_paths.extend(Path(path) for path in states[actor]["steps"]["local_handoff"]["output"]["files"])
        (Path(natives[actor]["repository"]) / "ml/nightly_synthesis.py").write_text("# installed source after preparation\n")
    claim = {"owner": "Scout source reconciliation", "repair_id": "historical-exchange-repair", "token": "a" * 64,
             "action_date": DAY, "domain": "exchange", "completion_record": None}
    registry.acquire(native["state_root"], claim)
    retained_paths.append(registry.path(native["state_root"]))
    originals = {path: path.read_bytes() for path in retained_paths}
    failures_before = files(session / "failures")
    def forbidden(*args, **kwargs):
        pytest.fail("Source provenance and historical repair claims cannot gate nightly delivery")
    monkeypatch.setattr(module.workflow, "source_identity", forbidden)
    for name in ("original_binding", "verify_transition", "dispatch_guard", "resolve"):
        monkeypatch.setattr(repair, name, forbidden)
    monkeypatch.setattr(registry, "read", forbidden)
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", original_adopt)
    scout = wake(exchange, "scout", now="2026-09-09T11:00:00Z")
    atlas = wake(exchange, "atlas", now="2026-09-09T11:00:00Z")
    assert scout["status"] == atlas["status"] == "COMPLETE"
    assert scout["delivered"] and scout["joint_packet_sha256"] == atlas["joint_packet_sha256"]
    assert wake(exchange, "scout", now="2026-09-09T11:00:00Z") == scout
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert files(session / "failures") == failures_before
    assert len(captures) == 0


def test_actual_operating_error_remains_visible_when_old_repair_metadata_is_malformed(exchange, monkeypatch):
    config = exchange[0]["scout"]
    session = Path(config["state_root"]) / "sessions" / DAY
    transitions = session / "repair-transitions.json"
    transitions.parent.mkdir(parents=True, exist_ok=True)
    transitions.write_bytes(b"malformed historical metadata\n")
    failure = write(session / "failure.json", {"original": "preserved"})
    originals = {path: path.read_bytes() for path in (transitions, failure)}
    actual_error = ValueError("actual current operating failure")
    def fail(*args):
        raise actual_error
    monkeypatch.setattr(module, "_run", fail)
    with pytest.raises(ValueError, match="actual current operating failure") as caught:
        wake(exchange, "scout")
    assert caught.value is actual_error
    status = json.loads((session / "status.json").read_text())
    assert status["status"] == "FAILED" and status["error_type"] == "ValueError"
    assert status["error"] == str(actual_error) and status["failure_recording_error"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())


@pytest.mark.parametrize("held,reason", [("workflow", "WORKFLOW_BUSY"), ("exchange", "EXCHANGE_BUSY")])
@pytest.mark.parametrize("status", ["FAILED", "COMPLETE"])
def test_healthy_shared_lock_is_quiet_pending_without_mutating_evidence(exchange, monkeypatch, held, reason, status):
    from filelock import FileLock
    from ml import nightly_repair_registry as registry
    config, native = exchange[0]["scout"], exchange[1]["scout"]
    root = Path(native["state_root"])
    session = Path(config["state_root"]) / "sessions" / DAY
    paths = [write(root / "runs" / DAY / "state.json", {"status": "COMPLETE", "original": True}),
             write(session / "status.json", {"status": status, "original": True}),
             write(session / "failure.json", {"attempts": 2, "original": True})]
    claim = {"owner": "Atlas source reconciliation", "repair_id": "fixture-repair", "token": "a" * 64,
             "action_date": DAY, "domain": "preparation", "completion_record": None}
    registry.acquire(root, claim)
    paths.append(registry.path(root))
    original = {path: path.read_bytes() for path in paths}
    for name in ("_run", "_completed_result"):
        monkeypatch.setattr(module, name, lambda *a, **kw: pytest.fail("Healthy lock blocks numerical work and receipt reads"))
    lock_root = root if held == "workflow" else Path(config["state_root"])
    with FileLock(str(lock_root / (held + ".lock")), timeout=0):
        result = wake(exchange, "scout")
    assert result["status"] == "PENDING" and result["reason"] == reason
    assert all(path.read_bytes() == raw for path, raw in original.items())
    assert not exchange[3]


def test_normal_exchange_holds_shared_workflow_guard_through_source_relevant_work(exchange, monkeypatch):
    from filelock import FileLock, Timeout
    native, config = exchange[1]["scout"], exchange[0]["scout"]
    def bounded_work(*args):
        for root, filename in ((native["state_root"], "workflow.lock"), (config["state_root"], "exchange.lock")):
            with pytest.raises(Timeout):
                with FileLock(str(Path(root) / filename), timeout=0):
                    pytest.fail("Source installation must remain excluded until the bounded exchange ends")
        return {"status": "PENDING", "reason": "FIXTURE_PEER_DELAY"}
    monkeypatch.setattr(module, "_run", bounded_work)
    assert wake(exchange, "scout")["reason"] == "FIXTURE_PEER_DELAY"


@pytest.fixture
def enrollment_exchange(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from ml.intraday import admission_enrollment as enrollment
    monkeypatch.setattr(enrollment, "_private_permissions", lambda *a, **k: None)
    owners = {"atlas": [f"A{i}" for i in range(11)], "scout": [f"S{i}" for i in range(11)]}
    configs = {actor: dict(actor=actor.title(), exchange_root=str(tmp_path / "CODEXSTORE/ducketz-nightly-exchange/v1"),
                           state_root=str(tmp_path / actor / "state"), workflow_config=str(tmp_path / actor / "workflow.json"),
                           owners=owners, account_scope_sha256="a" * 64, private_exchange_authorized=True)
               for actor in ("atlas", "scout")}
    calls = []
    def checked_load(path):
        calls.append(str(path))
        config = configs[path.name]
        if config["private_exchange_authorized"] is not True:
            raise ValueError("Direct local authorization required")
        return deepcopy(config)
    monkeypatch.setattr(module, "load_config", checked_load)
    monkeypatch.setattr(module.workflow, "load_config", lambda path: {"state_root": str(path.parent / "workflow")})
    now = datetime(2026, 10, 13, 6, tzinfo=timezone.utc)
    value = enrollment.create_offer(tmp_path / "atlas-key", enrollment_id="1" * 32, server_identity="atlas-test",
                                     endpoint="https://atlas.test:9443", account_fingerprint="a" * 64,
                                     symbols=owners["scout"], now=now)
    args = dict(enrollment_id="1" * 32, action_date="2026-10-13", review_session="2026-10-12")
    return configs, value, args, calls, now


def test_enrollment_reuses_private_packet_protocol_and_preserves_nightly(enrollment_exchange, tmp_path):
    from ml.intraday import admission_enrollment as enrollment
    configs, offer, args, calls, now = enrollment_exchange
    sentinel = Path(configs["atlas"]["exchange_root"]) / "sessions/2026-10-13/atlas/snapshot/selection.json"
    write(sentinel, {"historical": "unchanged"})
    before = sentinel.read_bytes()
    outgoing = module.publish_enrollment(Path("atlas"), **args, kind="admission_offer", payload=offer)
    incoming = module.receive_enrollment(Path("scout"), **args, kind="admission_offer")
    assert incoming == outgoing and incoming["trusted_payload_sha256"] == enrollment.payload_sha256(offer)
    request = enrollment.create_request(tmp_path / "scout-key", incoming["payload"],
        trusted_offer_sha256=incoming["trusted_payload_sha256"], expected_server_identity="atlas-test",
        account_fingerprint="a" * 64, symbols=configs["scout"]["owners"]["scout"], now=now)
    module.publish_enrollment(Path("scout"), **args, kind="admission_csr", payload=request)
    received_request = module.receive_enrollment(Path("atlas"), **args, kind="admission_csr")
    response = enrollment.sign_request(tmp_path / "atlas-key", received_request["payload"], now=now)
    module.publish_enrollment(Path("atlas"), **args, kind="admission_cert", payload=response)
    received_response = module.receive_enrollment(Path("scout"), **args, kind="admission_cert")
    enrollment.accept_response(tmp_path / "scout-key", received_response["payload"], now=now)
    assert sentinel.read_bytes() == before
    shared = Path(configs["atlas"]["exchange_root"])
    bytes_before = files(shared)
    assert module.publish_enrollment(Path("atlas"), **args, kind="admission_offer", payload=offer) == outgoing
    assert module.receive_enrollment(Path("scout"), **args, kind="admission_offer") == incoming
    assert files(shared) == bytes_before
    assert len(calls) == 16  # Initial configuration and under-lock revalidation on every operation.
    assert all(b"PRIVATE KEY" not in value for value in bytes_before.values())
    packets = list(shared.glob("admission-enrollment/*/sessions/*/*/*/packets/*.json"))
    assert len(packets) == 3 and all(json.loads(p.read_text())["schema_version"] == module.PACKET for p in packets)


@pytest.mark.parametrize("change", ["private_key", "scope", "symbols", "identity", "wrong_sender", "unauthorized"])
def test_enrollment_rejects_exports_outside_private_binding(enrollment_exchange, change):
    configs, value, args, _, _ = enrollment_exchange
    payload, actor = deepcopy(value), "atlas"
    if change == "private_key":
        payload["ca_certificate"] = "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----\n"
    elif change == "scope":
        payload["account_fingerprint"] = "b" * 64
    elif change == "symbols":
        payload["symbols"] = configs["atlas"]["owners"]["atlas"]
    elif change == "identity":
        payload["enrollment_id"] = "2" * 32
    elif change == "wrong_sender":
        actor = "scout"
    else:
        configs["atlas"]["private_exchange_authorized"] = False
    with pytest.raises(ValueError):
        module.publish_enrollment(Path(actor), **args, kind="admission_offer", payload=payload)
    assert not Path(configs["atlas"]["exchange_root"]).exists()


def test_enrollment_mutated_packet_and_reselection_fail_closed(enrollment_exchange):
    configs, value, args, _, _ = enrollment_exchange
    module.publish_enrollment(Path("atlas"), **args, kind="admission_offer", payload=value)
    shared = Path(configs["atlas"]["exchange_root"])
    packet = next(shared.glob("admission-enrollment/*/sessions/*/*/*/packets/*.json"))
    original = packet.read_bytes()
    packet.write_bytes(original[:-2] + b"x\n")
    with pytest.raises(ValueError):
        module.receive_enrollment(Path("scout"), **args, kind="admission_offer")
    packet.write_bytes(original)
    changed = dict(value, nonce="f" * 64)
    with pytest.raises(ValueError, match="immutable"):
        module.publish_enrollment(Path("atlas"), **args, kind="admission_offer", payload=changed)
    assert packet.read_bytes() == original


def test_enrollment_partial_sync_is_pending_and_other_identity_cannot_replay(enrollment_exchange):
    configs, value, args, _, _ = enrollment_exchange
    with pytest.raises(module.Pending):
        module.receive_enrollment(Path("scout"), **args, kind="admission_offer")
    module.publish_enrollment(Path("atlas"), **args, kind="admission_offer", payload=value)
    packet = next(Path(configs["atlas"]["exchange_root"]).glob("admission-enrollment/*/sessions/*/*/*/packets/*.json"))
    original = packet.read_bytes()
    packet.write_bytes(original[:30])
    with pytest.raises(module.Pending, match="PACKET_SYNC"):
        module.receive_enrollment(Path("scout"), **args, kind="admission_offer")
    packet.write_bytes(original)
    assert module.receive_enrollment(Path("scout"), **args, kind="admission_offer")["payload"] == value
    with pytest.raises(module.Pending):
        module.receive_enrollment(Path("scout"), **dict(args, enrollment_id="2" * 32), kind="admission_offer")


def test_enrollment_holds_existing_writer_and_install_guards(enrollment_exchange, monkeypatch):
    from filelock import FileLock, Timeout
    configs, value, args, _, _ = enrollment_exchange
    original = module._publish
    def guarded(config, *a, **k):
        for path in (Path(configs["atlas"]["workflow_config"]).parent / "workflow/workflow.lock",
                     Path(configs["atlas"]["state_root"]) / "exchange.lock"):
            with pytest.raises(Timeout):
                with FileLock(str(path), timeout=0):
                    pytest.fail("Enrollment must exclude overlapping source installation and exchange writers")
        return original(config, *a, **k)
    monkeypatch.setattr(module, "_publish", guarded)
    module.publish_enrollment(Path("atlas"), **args, kind="admission_offer", payload=value)


def test_enrollment_cli_records_private_receipt_without_printing_endpoint(enrollment_exchange, tmp_path, capsys):
    _, value, args, _, _ = enrollment_exchange
    payload = write(tmp_path / "public-offer.json", value)
    receipt = tmp_path / "private-receipt.json"
    common = ["--enrollment-kind", "admission_offer", "--enrollment-id", args["enrollment_id"],
              "--enrollment-action-date", args["action_date"], "--enrollment-review-session", args["review_session"]]
    assert module.main(["--config", "atlas", *common, "--enrollment-payload", str(payload),
                        "--enrollment-receipt", str(receipt)]) == 0
    output = capsys.readouterr().out
    assert "https://" not in output and "atlas.test" not in output
    saved = json.loads(receipt.read_text())
    assert saved["payload"] == value and json.loads(output)["packet_sha256"] == saved["packet_sha256"]
    assert module.main(["--config", "scout", *common, "--enrollment-receipt", str(tmp_path / "scout-receipt.json")]) == 0
    capsys.readouterr()
    assert module.main(["--config", "atlas", *common, "--check", "--enrollment-receipt", str(receipt)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"


def test_enrollment_cli_error_redacts_private_connection_details(enrollment_exchange, tmp_path, capsys, monkeypatch):
    _, _, args, _, _ = enrollment_exchange
    def fail(*a, **k):
        raise ValueError("private endpoint https://private.test:9443 and C:/private/private.key")
    monkeypatch.setattr(module, "receive_enrollment", fail)
    assert module.main(["--config", "scout", "--enrollment-kind", "admission_offer",
                        "--enrollment-id", args["enrollment_id"], "--enrollment-action-date", args["action_date"],
                        "--enrollment-review-session", args["review_session"],
                        "--enrollment-receipt", str(tmp_path / "private-receipt.json")]) == 1
    output = capsys.readouterr()
    assert output.err == "" and "private" not in output.out and "https://" not in output.out
    assert json.loads(output.out)["error"] == "enrollment_failed"
