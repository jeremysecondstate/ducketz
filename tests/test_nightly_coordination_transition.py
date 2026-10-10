"""Offline retained-document migration; no providers, workers or live state."""
from pathlib import Path
import subprocess
import types

import pandas as pd
import pytest
from filelock import FileLock, Timeout

from ml import nightly_exchange_repair as repair
from ml.artifacts import file_checksum
from tools import nightly_exchange as exchange
from tests.test_nightly_exchange_repair import env, write, DAY, NOW, claimed, prepared, applied


def release(root, commit, previous=None):
    contract = write(root / "coordination/contract.json", {"version": "cross-pc-v2"})
    manifest = write(root / "installation.json", {"commit": commit, "files": {
        "coordination/contract.json": file_checksum(contract)}})
    return {"commit": commit, "contract_version": "cross-pc-v2", "manifest_sha256": file_checksum(manifest),
            "release_root": str(root), "previous": previous}


@pytest.fixture
def administrative(env, tmp_path, monkeypatch):
    config = env["config"]
    before_profile = {"actor": "Scout", "contract_version": "cross-pc-v2", "machine": "pc-new",
                      "checkout": str(env["repo"]), "symbols": ["EXAMPLE"], "private_export": True}
    before_active = release(tmp_path / "old-release", "1" * 40)
    write(Path(config["local_profile"]), before_profile)
    write(Path(config["coordination_active"]), before_active)
    binding = lambda value: repair._installed_binding(value, env["native"])
    monkeypatch.setattr(repair, "_binding", binding)
    monkeypatch.setattr(exchange, "_binding", binding)
    # Production anchors use the exchange adapter's canonical compact bytes.
    env["original_binding"].write_bytes(exchange._bytes(binding(config)))
    before_paths = {"before_profile": write(tmp_path / "retained-profile.json", before_profile),
                    "before_active": write(tmp_path / "retained-active.json", before_active)}
    write(Path(config["local_profile"]), {**before_profile, "coordination_notification_policy": "github_only"})
    write(Path(config["coordination_active"]), release(tmp_path / "new-release", "2" * 40, before_active))
    protected = [env[key] for key in ("original_binding", "prep", "selection", "partial", "budget", "status")]
    original_bytes = {path: path.read_bytes() for path in protected}
    env.update(before_paths=before_paths, original_bytes=original_bytes)
    env.update(reviewed_target_source=repair.workflow.source_identity(env["repo"]),
               reviewed_target_files=repair._inventory(env["repo"]))
    return env


def transition(env, **kwargs):
    request = dict(action_date=DAY, owner="Scout existing repair owner", repair_id="scout-binding-transition-20261010",
        reason="Exact reviewed channel-only migration", completion_record="reviewed-local-transition-proof",
        reviewed=True, now=NOW, target_source=env["reviewed_target_source"], target_files=env["reviewed_target_files"],
        **env["before_paths"])
    request.update(kwargs)
    return repair.coordination_transition(env["config"], **request)


def test_exact_documents_append_compatible_transition_without_rebinding_or_source_install(administrative):
    e = administrative
    inventory = repair._inventory(e["repo"])
    result = transition(e)
    assert result["status"] == "COORDINATION_TRANSITION_APPLIED"
    assert result["before_source"] == result["after_source"] == e["state"]["source_identity"]
    assert repair._inventory(e["repo"]) == inventory
    assert all(path.read_bytes() == raw for path, raw in e["original_bytes"].items())
    assert repair.registry.read(e["work"]) is None
    assert not (e["session"] / "repair-claim.json").exists()
    assert transition(e)["status"] == "COORDINATION_TRANSITION_ALREADY_APPLIED"
    assert len(repair._transition_entries(e["session"])) == 1
    assert repair.verify_transition(e["config"], e["state"], repair._read(e["original_binding"])) == {"verified": True, "repairs": 1}
    assert exchange._check_ownership_binding(e["config"], DAY) == repair._binding(e["config"])
    # The unchanged ordinary repair route can now acquire the original failure.
    assert claimed(e).exists()


@pytest.mark.parametrize("field,value", [("actor", "Atlas"), ("machine", "pc-original"), ("symbols", ["OTHER"]),
    ("checkout", "elsewhere"), ("private_export", False), ("operating_roles", {"runtime_deployment": True}),
    ("peer_implementation_installation", {"authorized": True}), ("unknown_authority", None),
    ("coordination_notification_policy", "anything")])
def test_operating_or_authority_changes_cannot_piggyback(administrative, field, value):
    e = administrative
    p = Path(e["config"]["local_profile"])
    write(p, {**repair._read(p), field: value})
    with pytest.raises(ValueError, match="non-administrative"):
        transition(e)
    assert not (e["session"] / "source-repairs").exists()


@pytest.mark.parametrize("target", ["before_profile", "before_active", "manifest", "release_file", "workflow", "adapter", "source", "unbound_tool", "no_op"])
def test_missing_tampered_or_unrelated_evidence_refuses_before_audit_writes(administrative, target):
    e = administrative
    if target.startswith("before"):
        e["before_paths"][target].write_text("{}")
    elif target in {"manifest", "release_file"}:
        active = repair._read(e["before_paths"]["before_active"])
        name = "installation.json" if target == "manifest" else "coordination/contract.json"
        (Path(active["release_root"]) / name).write_text("{}")
    elif target == "workflow":
        Path(e["config"]["workflow_config"]).write_text("{}")
    elif target == "adapter":
        (e["repo"] / "tools/nightly_account_snapshot.py").write_text("# unauthorized")
    elif target == "source":
        (e["repo"] / "ml/immutable_policy.py").write_text("# unauthorized")
    elif target == "unbound_tool":
        (e["repo"] / "tools/new_unreviewed_tool.py").write_text("# another writer")
    else:
        Path(e["config"]["local_profile"]).write_bytes(e["before_paths"]["before_profile"].read_bytes())
        Path(e["config"]["coordination_active"]).write_bytes(e["before_paths"]["before_active"].read_bytes())
    with pytest.raises(ValueError):
        transition(e)
    assert not (e["session"] / "source-repairs").exists()


@pytest.mark.parametrize("name", ["exchange.lock", "ownership-launch.lock", "ownership-responder.lock"])
def test_concurrent_exchange_or_responder_prevents_any_transition(administrative, name):
    e = administrative
    with FileLock(str(e["exchange"] / name), timeout=0):
        with pytest.raises(Timeout):
            transition(e)
    assert not (e["session"] / "source-repairs").exists()


def test_other_repair_owner_and_unexpired_lease_are_preserved(administrative):
    e = administrative
    guard = {"owner": "another owner", "repair_id": "existing-other-repair", "completion_record": None,
             "token": "a" * 64, "action_date": "2026-10-08", "domain": "preparation"}
    repair.registry.acquire(e["work"], guard)
    with pytest.raises(ValueError, match="existing repair owner"):
        transition(e)
    assert repair.registry.read(e["work"]) == guard
    repair.registry.release(e["work"], guard, verified=True)
    write(e["session"] / "ownership-responder.json", {"started_at": NOW, "deadline_at": "2026-10-09T21:05:00Z",
          "action_date": DAY, "review_session": "2026-10-08"})
    with pytest.raises(ValueError, match="unexpired"):
        transition(e)
    assert not (e["session"] / "source-repairs").exists()


@pytest.mark.parametrize("change", [{"deadline_at": "NaT"}, {"started_at": "NaT"}, {"deadline_at": "2026-10-09T20:55:00"},
                                  {"action_date": "2026-10-08"}, {"deadline_at": "2026-10-09T20:59:00Z"}])
def test_unverifiable_responder_lease_never_counts_as_expired(administrative, change):
    e = administrative
    write(e["session"] / "ownership-responder.json", {"started_at": "2026-10-09T20:50:00Z",
          "deadline_at": "2026-10-09T20:55:00Z", "action_date": DAY, "review_session": "2026-10-08", **change})
    with pytest.raises(ValueError, match="unverifiable"):
        transition(e)
    assert not (e["session"] / "source-repairs").exists()


@pytest.mark.parametrize("phase", ["spec.json", "applied.json", "repair-transitions.json"])
def test_interrupted_append_reuses_same_exact_proof(administrative, monkeypatch, phase):
    e = administrative
    atomic = repair._atomic
    def interrupted(path, raw, **kwargs):
        atomic(path, raw, **kwargs)
        if Path(path).name == phase:
            raise OSError("injected interruption")
    monkeypatch.setattr(repair, "_atomic", interrupted)
    with pytest.raises(OSError, match="injected"):
        transition(e)
    saved = (e["session"] / "source-repairs/scout-binding-transition-20261010/spec.json").read_bytes()
    monkeypatch.setattr(repair, "_atomic", atomic)
    transition(e)
    assert len(repair._transition_entries(e["session"])) == 1
    assert (e["session"] / "source-repairs/scout-binding-transition-20261010/spec.json").read_bytes() == saved


def test_drift_during_transition_never_appends_permission(administrative, monkeypatch):
    e = administrative
    atomic = repair._atomic
    def racing(path, raw, **kwargs):
        atomic(path, raw, **kwargs)
        if Path(path).name == "spec.json":
            p = Path(e["config"]["local_profile"])
            write(p, {**repair._read(p), "symbols": ["OTHER"]})
    monkeypatch.setattr(repair, "_atomic", racing)
    with pytest.raises(ValueError, match="changed during"):
        transition(e)
    assert not (e["session"] / "repair-transitions.json").exists()


@pytest.mark.parametrize("replacement", [{"owner": "different owner"}, {"reason": "different rationale"},
                                      {"completion_record": "different completion identity"}])
def test_retry_cannot_change_reviewed_identity(administrative, replacement):
    e = administrative
    transition(e)
    original = (e["session"] / "repair-transitions.json").read_bytes()
    with pytest.raises(ValueError, match="retry identity"):
        transition(e, **replacement)
    assert (e["session"] / "repair-transitions.json").read_bytes() == original


def test_expired_authenticated_responder_renews_with_history_then_verifies(administrative, monkeypatch):
    e = administrative
    record = {"binding": repair._read(e["original_binding"]), "inputs": {}, "deadline_at": "2026-10-09T20:55:00Z",
              "started_at": "2026-10-09T20:50:00Z", "action_date": DAY, "review_session": "2026-10-08"}
    path = write(e["session"] / "ownership-responder.json", record)
    original = path.read_bytes()
    transition(e)
    monkeypatch.setattr(exchange, "_spawn_responder", lambda *args: None)
    exchange._ensure_ownership_responder(e["config"], DAY, "2026-10-08", {}, pd.Timestamp(NOW))
    assert repair._read(path)["binding"] == repair._binding(e["config"])
    assert list((e["session"] / "ownership-responder-history").glob("*.json"))[0].read_bytes() == original
    repair.verify_transition(e["config"], e["state"], repair._read(e["original_binding"]))
    bad = repair._read(path)
    bad["binding"] = {"forged": "historical"}
    bad["started_at"] = "2026-10-09T20:50:00Z"
    bad["deadline_at"] = "2026-10-09T20:55:00Z"
    write(path, bad)
    with pytest.raises(ValueError, match="authenticated historical"):
        exchange._ensure_ownership_responder(e["config"], DAY, "2026-10-08", {}, pd.Timestamp(NOW))


def test_selected_stale_snapshot_stays_frozen_after_transition(administrative):
    e = administrative
    write(e["budget"], {"observed_at": "2026-10-09T11:59:00Z", "budget": 100})
    before = e["budget"].read_bytes()
    transition(e)
    with pytest.raises(exchange.Pending, match="FROZEN_SNAPSHOT_STALE_BEFORE_ADOPTION_REVIEW_REQUIRED"):
        exchange._verify_synthesis_freshness({"datastore_root": str(e["data"]), "action_date": DAY,
            "snapshot": {"path": str(e["budget"])}}, pd.Timestamp(NOW))
    assert e["budget"].read_bytes() == before


@pytest.mark.parametrize("changed_source", ["ml/nightly_synthesis.py", "tools/nightly_exchange.py"])
def test_source_repair_after_admin_proof_preserves_mutable_protocol_pointers(administrative, changed_source):
    e = administrative
    transition(e)
    pointer = write(e["session"] / "ownership-request.json", {"challenge": "before"})
    spec = prepared(e, path=changed_source)
    applied(e, spec)
    write(pointer, {"challenge": "next protocol request"})
    repair.verify_transition(e["config"], e["state"], repair._read(e["original_binding"]))
    assert exchange._check_ownership_binding(e["config"], DAY) == repair._binding(e["config"])


def legacy(relative, name):
    raw = subprocess.check_output(["git", "show", "37bed6b7b0e0d4c1790b98aa5ea38a23e485795c:" + relative])
    module = types.ModuleType(name)
    module.__file__ = str(Path.cwd() / relative)
    exec(compile(raw, relative, "exec"), module.__dict__)
    return module


def test_unmodified_installed_verifier_and_adapter_accept_audited_admin_transition(administrative, monkeypatch):
    e = administrative
    output = e["state"]["steps"]["local_handoff"]["output"]
    output.update(package=str(e["output"]), stats_package=str(e["output"]))
    write(e["prep"], e["state"])
    write(e["budget"], {"observed_at": "2026-10-09T11:59:00Z", "budget": 100})
    frozen = {path: path.read_bytes() for path in (e["original_binding"], e["prep"], e["selection"], e["budget"])}
    transition(e)
    old_repair = legacy("ml/nightly_exchange_repair.py", "exact_base_repair")
    old_exchange = legacy("tools/nightly_exchange.py", "exact_base_exchange")
    monkeypatch.setattr(old_repair, "_native", lambda config: e["native"])
    monkeypatch.setattr(old_repair, "_binding", lambda config: repair._installed_binding(config, e["native"]))
    assert old_repair.verify_transition(e["config"], e["state"], repair._read(e["original_binding"])) == {"verified": True, "repairs": 1}
    monkeypatch.setattr(repair, "verify_transition", old_repair.verify_transition)
    monkeypatch.setattr(old_exchange, "_binding", old_repair._binding)
    def observed(*args):
        raise old_exchange.Pending("original adapter passed immutable binding and preparation checks")
    monkeypatch.setattr(old_exchange, "_owners", observed)
    with pytest.raises(old_exchange.Pending, match="original adapter passed"):
        old_exchange._run(e["config"], e["native"], DAY, "2026-10-08", lambda: pd.Timestamp(NOW), False)
    with pytest.raises(old_exchange.Pending, match="FROZEN_SNAPSHOT_STALE_BEFORE_ADOPTION_REVIEW_REQUIRED"):
        old_exchange._verify_synthesis_freshness({"datastore_root": str(e["data"]), "action_date": DAY,
            "snapshot": {"path": str(e["budget"])}}, pd.Timestamp(NOW))
    assert all(path.read_bytes() == raw for path, raw in frozen.items())
