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
    assert wake(exchange, "atlas")["reason"] == "ACCOUNT_SNAPSHOT_REFRESH_REQUIRED"
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
        states[actor] = {"steps": {"local_handoff": {"output": output}}}
        configs[actor], natives[actor] = config, native
    monkeypatch.setattr(module.workflow, "load_config", lambda path: json.loads(path.read_text()))
    monkeypatch.setattr(module.workflow, "verify_installation", lambda _: None)
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
    assert wake(exchange, "scout")["reason"] == "ACCEPTED_ATLAS"


def test_two_machine_real_synthesis_handoff_and_repeat_without_changes(exchange):
    configs, natives, _, captures = exchange
    synthesized(exchange)
    accepted = wake(exchange, "atlas")
    assert accepted["status"] == "COMPLETE" and accepted["joint_ready"] and accepted["peer_verified"]
    confirmed = wake(exchange, "scout")
    assert confirmed["status"] == "COMPLETE" and confirmed["accepted_packet_sha256"] == accepted["accepted_packet_sha256"]
    for actor, native in natives.items():
        assert set(load_gameplan(Path(native["datastore"])).symbols) == {"AAPL", "ABCL"}
        assert set(load_gameplan_stats(Path(native["datastore"])).symbols) == {"AAPL", "ABCL"}
    private_before = files(Path(configs["scout"]["exchange_root"]))
    source_before = {actor: files(Path(native["datastore"])) for actor, native in natives.items()}
    assert wake(exchange, "atlas", now="2026-09-09T12:00:00Z", capture=True) == accepted
    assert wake(exchange, "scout", now="2026-09-09T12:00:00Z") == confirmed
    assert files(Path(configs["scout"]["exchange_root"])) == private_before
    assert {actor: files(Path(native["datastore"])) for actor, native in natives.items()} == source_before
    assert len(captures) == 1
    assert not accepted["execution_authorized"] and accepted["orders_placed"] == 0
    assert json.loads(Path(configs["atlas"]["account_config"]).read_text())["activation"]["status"] == "PREPARING"


def test_no_snapshot_read_until_both_preparations_and_explicit_flag(exchange):
    assert wake(exchange, "atlas", capture=True)["reason"] == "PREPARATION_SCOUT"
    assert exchange[3] == []
    assert wake(exchange, "scout")["reason"] == "SNAPSHOT_ATLAS"
    assert wake(exchange, "atlas")["reason"] == "ACCOUNT_SNAPSHOT_REFRESH_REQUIRED"
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
    payload.write_bytes(payload.read_bytes()[:100])
    assert wake(exchange, "atlas", now="2026-09-09T10:20:00Z", capture=True)["reason"] == "PACKET_SYNC"
    assert len(exchange[3]) == 1


def test_snapshot_refresh_before_synthesis_and_frozen_retry_after_adoption(exchange):
    prepared(exchange)
    assert wake(exchange, "scout", now="2026-09-09T10:16:00Z")["reason"] == "FRESH_ACCOUNT_SNAPSHOT"
    assert wake(exchange, "atlas", now="2026-09-09T10:16:00Z", capture=True)["reason"] == "JOINT_SCOUT"
    assert wake(exchange, "scout", now="2026-09-09T10:16:00Z")["reason"] == "ACCEPTED_ATLAS"
    assert len(exchange[3]) == 2
    assert wake(exchange, "atlas", now="2026-09-09T11:00:00Z", capture=True)["status"] == "COMPLETE"
    assert wake(exchange, "scout", now="2026-09-09T11:00:00Z")["status"] == "COMPLETE"
    assert len(exchange[3]) == 2


def test_pre_adoption_crash_cannot_backdate_first_synthesis(exchange, monkeypatch):
    prepared(exchange)
    original = nightly_synthesis.run_synthesis
    monkeypatch.setattr(nightly_synthesis, "run_synthesis", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fixture interruption")))
    with pytest.raises(RuntimeError):
        wake(exchange, "scout")
    frozen = Path(exchange[0]["scout"]["state_root"]) / "sessions" / DAY / "synthesis-selection.json"
    before = frozen.read_bytes()
    monkeypatch.setattr(nightly_synthesis, "run_synthesis", original)
    assert wake(exchange, "scout", now="2026-09-09T10:16:00Z")["reason"] == "FROZEN_SNAPSHOT_STALE_BEFORE_ADOPTION_REVIEW_REQUIRED"
    assert frozen.read_bytes() == before
    assert not (Path(exchange[1]["scout"]["datastore"]) / "ml/joint-gameplan-by-date").exists()


def test_partial_ui_adoption_resumes_exact_spec_and_does_not_recapture(exchange, monkeypatch):
    prepared(exchange)
    original = nightly_synthesis._adopt_stats
    def interrupted(root, *args):
        if root == Path(exchange[1]["scout"]["datastore"]):
            raise RuntimeError("fixture stop after local plan adoption")
        return original(root, *args)
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", interrupted)
    with pytest.raises(RuntimeError):
        wake(exchange, "scout")
    frozen = Path(exchange[0]["scout"]["state_root"]) / "sessions" / DAY / "synthesis-selection.json"
    before = frozen.read_bytes()
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", original)
    assert wake(exchange, "scout", now="2026-09-09T11:00:00Z")["reason"] == "ACCEPTED_ATLAS"
    assert frozen.read_bytes() == before and len(exchange[3]) == 1


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


def test_atlas_checks_exact_original_snapshot_before_handoff(exchange):
    synthesized(exchange)
    config = exchange[0]["atlas"]
    selected = module._receive(config, DAY, "2026-09-08", "atlas", "snapshot")
    own_snapshot = Path(config["state_root"]) / "sessions" / DAY / "own-snapshots" / selected["digest"] / "snapshot.json"
    value = json.loads(own_snapshot.read_text())
    value["available_cash"] += 1
    write(own_snapshot, value)
    with pytest.raises(ValueError, match="exact captured snapshot"):
        wake(exchange, "atlas")
    assert not (Path(exchange[1]["atlas"]["datastore"]) / "ml/joint-gameplan-by-date").exists()


def test_atlas_rejects_joint_with_unpublished_snapshot_identity(exchange):
    synthesized(exchange)
    config = exchange[0]["atlas"]
    folder = module._folder(config, DAY, "scout", "joint")
    selected = json.loads((folder / "selection.json").read_text())
    packet = json.loads((folder / "packets" / (selected["content_sha256"] + ".json")).read_text())
    packet["inputs"]["snapshot"] = "b" * 64
    packet.pop("content_sha256")
    packet["content_sha256"] = module.content_sha256(packet)
    data = module._bytes(packet)
    (folder / "packets" / (packet["content_sha256"] + ".json")).write_bytes(data)
    write(folder / "selection.json", {"schema_version": module.SELECTION,
        "content_sha256": packet["content_sha256"], "file_sha256": module._sha(data), "bytes": len(data)})
    with pytest.raises(ValueError, match="not locally published by this Atlas attempt"):
        wake(exchange, "atlas")
    assert not (Path(exchange[1]["atlas"]["datastore"]) / "ml/joint-gameplan-by-date").exists()


def test_received_atlas_packet_and_cache_cannot_replace_own_capture_proof(exchange):
    synthesized(exchange)
    config = exchange[0]["atlas"]
    selected = module._receive(config, DAY, "2026-09-08", "atlas", "snapshot")
    proof = Path(config["state_root"]) / "sessions" / DAY / "own-snapshots" / selected["digest"] / "receipt.json"
    proof.unlink()
    # Valid peer-visible bytes can repopulate receive cache/history. They still
    # do not establish that Atlas captured this account observation itself.
    recovered = module._receive(config, DAY, "2026-09-08", "atlas", "snapshot")
    assert recovered["files"]["snapshot.json"].exists() and not proof.exists()
    with pytest.raises(ValueError, match="not locally published"):
        wake(exchange, "atlas")
    assert not (Path(exchange[1]["atlas"]["datastore"]) / "ml/joint-gameplan-by-date").exists()


def test_own_snapshot_origin_is_durable_before_shared_publication(exchange, monkeypatch):
    wake(exchange, "scout")
    original = module._write
    seen = []
    def observe(path, data, **kwargs):
        path = Path(path)
        if path.parent.name == "packets" and path.parent.parent.name == "snapshot":
            packet = json.loads(data)
            origin = Path(exchange[0]["atlas"]["state_root"]) / "sessions" / DAY / "own-snapshots" / packet["content_sha256"]
            proof = json.loads((origin / "receipt.json").read_text())
            assert proof["packet_file_sha256"] == module._sha(data)
            assert proof["snapshot_file_sha256"] == file_checksum(origin / "snapshot.json")
            seen.append(True)
        return original(path, data, **kwargs)
    monkeypatch.setattr(module, "_write", observe)
    assert wake(exchange, "atlas", capture=True)["reason"] == "JOINT_SCOUT"
    assert seen == [True]


def test_interrupted_snapshot_publication_replays_exact_bytes_without_capture(exchange, monkeypatch):
    wake(exchange, "scout")
    original = module._write
    def interrupted(path, data, **kwargs):
        path = Path(path)
        if path.parent.name == "packets" and path.parent.parent.name == "snapshot":
            raise OSError("fixture Drive write interrupted")
        return original(path, data, **kwargs)
    monkeypatch.setattr(module, "_write", interrupted)
    with pytest.raises(OSError, match="Drive write interrupted"):
        wake(exchange, "atlas", capture=True)
    assert len(exchange[3]) == 1
    selected = Path(exchange[0]["atlas"]["state_root"]) / "sessions" / DAY / "snapshot-publication.json"
    before = selected.read_bytes()
    monkeypatch.setattr(module, "_write", original)
    # Finishing an already authorized saved publication needs no new read flag.
    assert wake(exchange, "atlas")["reason"] == "JOINT_SCOUT"
    assert len(exchange[3]) == 1 and selected.read_bytes() == before
    received = module._receive(exchange[0]["scout"], DAY, "2026-09-08", "atlas", "snapshot")
    assert received["digest"] == json.loads(before)["content_sha256"]


def test_unknown_shared_snapshot_generation_is_not_overwritten_as_recovery(exchange):
    prepared(exchange)
    config = exchange[0]["atlas"]
    selected = module._folder(config, DAY, "atlas", "snapshot") / "selection.json"
    value = json.loads(selected.read_text())
    value["content_sha256"] = "b" * 64
    write(selected, value)
    before = selected.read_bytes()
    with pytest.raises(ValueError, match="not locally published"):
        wake(exchange, "atlas", capture=True)
    assert selected.read_bytes() == before and len(exchange[3]) == 1


def test_snapshot_capture_uses_post_capture_clock(exchange, monkeypatch):
    wake(exchange, "scout")
    from tools import nightly_account_snapshot
    ticks = [10]
    def delayed(config, now=None, ownership_observations=None):
        value = snapshot()
        value["observed_at"] = (pd.Timestamp(now) + pd.Timedelta(seconds=1)).isoformat()
        ticks[0] += 2
        return value
    monkeypatch.setattr(module, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(nightly_account_snapshot, "capture_snapshot", delayed)
    assert wake(exchange, "atlas", capture=True)["reason"] == "JOINT_SCOUT"


def test_io_elapsed_time_counts_toward_first_synthesis_freshness(exchange, monkeypatch):
    prepared(exchange)
    original = module._owners
    ticks = [100]
    def delayed(*args):
        value = original(*args)
        ticks[0] += 10
        return value
    monkeypatch.setattr(module, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(module, "_owners", delayed)
    assert wake(exchange, "scout", now="2026-09-09T10:14:55Z")["reason"] == "FRESH_ACCOUNT_SNAPSHOT"
    assert not (Path(exchange[0]["scout"]["state_root"]) / "sessions" / DAY / "synthesis-selection.json").exists()


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


def test_partial_synthesis_retains_original_snapshot_after_atlas_refresh(exchange, monkeypatch):
    prepared(exchange)
    original = nightly_synthesis._adopt_stats
    def interrupted(root, *args):
        if root == Path(exchange[1]["scout"]["datastore"]):
            raise RuntimeError("fixture stop after plan")
        return original(root, *args)
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", interrupted)
    with pytest.raises(RuntimeError):
        wake(exchange, "scout")
    frozen_path = Path(exchange[0]["scout"]["state_root"]) / "sessions" / DAY / "synthesis-selection.json"
    frozen = json.loads(frozen_path.read_text())
    assert wake(exchange, "atlas", now="2026-09-09T10:20:00Z", capture=True)["reason"] == "JOINT_SCOUT"
    latest = module._receive(exchange[0]["atlas"], DAY, "2026-09-08", "atlas", "snapshot")
    assert latest["digest"] != frozen["inputs"]["snapshot"]
    monkeypatch.setattr(nightly_synthesis, "_adopt_stats", original)
    assert wake(exchange, "scout", now="2026-09-09T10:20:00Z")["reason"] == "ACCEPTED_ATLAS"
    assert wake(exchange, "atlas", now="2026-09-09T10:20:00Z")["status"] == "COMPLETE"
    assert json.loads(frozen_path.read_text()) == frozen
    assert len(exchange[3]) == 2


def test_adapter_source_change_after_export_requires_review(exchange, monkeypatch):
    wake(exchange, "scout")
    original = module._binding
    def changed(config):
        binding = original(config)
        binding["adapter_sources"]["nightly_exchange.py"] = "0" * 64
        return binding
    monkeypatch.setattr(module, "_binding", changed)
    with pytest.raises(ValueError, match="immutable"):
        wake(exchange, "scout")


def test_joint_arriving_during_capture_preserves_snapshot_selection(exchange, monkeypatch):
    prepared(exchange)
    config = exchange[0]["atlas"]
    selected = module._folder(config, DAY, "atlas", "snapshot") / "selection.json"
    before = selected.read_bytes()
    from tools import nightly_account_snapshot
    def concurrent_joint(config, now=None, ownership_observations=None):
        joint = module._folder(config, DAY, "scout", "joint") / "selection.json"
        write(joint, {"fixture": "sync arrival in progress"})
        value = snapshot()
        value["observed_at"] = pd.Timestamp(now).isoformat()
        return value
    monkeypatch.setattr(nightly_account_snapshot, "capture_snapshot", concurrent_joint)
    assert wake(exchange, "atlas", now="2026-09-09T10:20:00Z", capture=True)["reason"] == "JOINT_ARRIVED_DURING_CAPTURE"
    assert selected.read_bytes() == before


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
    assert module._local_state({"actor": "Scout"}, native, action, review) == state
    source = Path(state["steps"]["verify_display"]["output"]["source_gameplan_run"]) / "receipt.json"
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(ValueError, match="output changed"):
        module._local_state({"actor": "Scout"}, native, action, review)


def test_fresh_ownership_handshake_precedes_single_account_capture(exchange, rendezvous, monkeypatch):
    monkeypatch.setattr(module, "_ownership_observations", OWNERSHIP_OBSERVATIONS)
    assert wake(exchange, "atlas", capture=True)["reason"] == "JOINT_SCOUT"
    assert rendezvous[5] == ["Scout", "Atlas"] and len(exchange[3]) == 1
    configs, _, inputs, _, clock, reads = rendezvous
    original = list(reads)
    values = OWNERSHIP_OBSERVATIONS(configs["atlas"], DAY, "2026-09-08", inputs, clock)
    assert reads == original and values[0]["actor"] == "Atlas"


def test_ownership_timeout_has_no_ledger_or_account_reads(exchange, rendezvous, monkeypatch):
    tick = rendezvous[3]
    monkeypatch.setattr(module, "sleep", lambda seconds: tick.__setitem__(0, tick[0] + seconds))
    monkeypatch.setattr(module, "_ownership_observations", OWNERSHIP_OBSERVATIONS)
    result = wake(exchange, "atlas", capture=True)
    assert result["reason"] == "FRESH_SCOUT_OWNERSHIP_RESPONSE"
    assert tick[0] == 50 and not rendezvous[5] and not exchange[3]
    config = exchange[0]["atlas"]
    original = module._receive(config, DAY, "2026-09-08", "atlas", "ownership_request")["digest"]
    result = wake(exchange, "atlas", now=pd.Timestamp(NOW) + pd.Timedelta(seconds=60), capture=True)
    assert result["reason"] == "FRESH_SCOUT_OWNERSHIP_RESPONSE"
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
    monkeypatch.setattr(module, "_ownership_observations", OWNERSHIP_OBSERVATIONS)
    assert wake(exchange, "atlas", capture=True)["reason"] == "FRESH_SCOUT_OWNERSHIP_RESPONSE"
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
    assert len(exchange[3]) == 1


def test_cutover_during_ownership_wait_is_refused_before_account_capture(exchange, monkeypatch):
    path = Path(exchange[0]["atlas"]["account_config"])
    def changed(*args):
        value = json.loads(path.read_text())
        value["activation"]["status"] = "ACTIVE"
        write(path, value)
        return []
    monkeypatch.setattr(module, "_ownership_observations", changed)
    wake(exchange, "scout")
    with pytest.raises(ValueError, match="operating bindings changed"):
        wake(exchange, "atlas", capture=True)
    assert not exchange[3]
