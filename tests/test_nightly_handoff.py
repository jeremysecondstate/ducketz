from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.account_gameplan.config import CONFIG, VERSION as ACCOUNT_VERSION, digest, load_account_config
from ml.artifacts import file_checksum
from ml import nightly_handoff as module
from ml.nightly_synthesis import run_synthesis
from tests.test_nightly_synthesis import specification as synthesis_inputs
from tests.test_joint_capital_plan import NOW, SCOPE


def bind(path):
    return {"path": str(path), "file_sha256": file_checksum(path)}


@pytest.fixture
def handoff_inputs(synthesis_inputs, tmp_path, monkeypatch):
    scout = run_synthesis(synthesis_inputs, now=NOW)
    checkout, root = tmp_path / "atlas-checkout", tmp_path / "atlas-data"
    profile = checkout / "scratch/cross-pc/local-profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"contract_version": "cross-pc-v2", "actor": "Atlas", "machine": "pc-original",
                                   "checkout": str(checkout), "symbols": ["AAPL"]}))
    account_path = root / CONFIG
    account_path.parent.mkdir(parents=True)
    account = {"schema_version": ACCOUNT_VERSION, "machine_id": "pc-original", "coordinator_id": "pc-original",
               "participants": {"pc-original": ["AAPL"], "pc-new": ["ABCL"]}, "account_fingerprint": SCOPE}
    account["activation"] = {"status": "PREPARING", "binding_sha256": digest(account)}
    account_path.write_text(json.dumps(account))
    plan_path = Path(scout["ui_evidence"]["plan_run"]) / "joint-plan.json"
    monkeypatch.setattr(module, "__file__", str(checkout / "ml/nightly_handoff.py"))
    return {"schema_version": module.VERSION, "completion_id": "20260909-atlas-handoff-fixture",
        "datastore_root": str(root), "state_root": str(tmp_path / "atlas-state"),
        "local_actor": "atlas", "executor_owner": "atlas", "action_date": synthesis_inputs["action_date"],
        "review_session": synthesis_inputs["review_session"], "account_scope_sha256": SCOPE, "accepted_at": NOW,
        "local_profile": bind(profile), "account_config": bind(account_path), "scout_receipt": bind(Path(scout["receipt_path"])),
        "joint_plan": {**bind(plan_path), "root": str(plan_path.parent), "plan_sha256": scout["ui_evidence"]["plan_sha256"]},
        "owners": deepcopy(synthesis_inputs["owners"])}


def journal(spec):
    return Path(spec["state_root"]) / spec["completion_id"] / "state.json"


def existing_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in Path(root).rglob("*") if path.is_file()}


def test_preflight_has_no_writes_and_never_recomposes(handoff_inputs, monkeypatch):
    import ml.joint_capital_plan
    import ml.nightly_synthesis
    fail = lambda *a, **kw: pytest.fail("Atlas cannot reallocate Scout's plan")
    monkeypatch.setattr(ml.joint_capital_plan, "compose_joint_plan", fail)
    monkeypatch.setattr(ml.nightly_synthesis, "compose_joint_plan", fail)
    spec = handoff_inputs
    before = existing_bytes(spec["datastore_root"])
    result = module.preflight_handoff(spec, now=NOW)
    assert result["plan"]["plan_sha256"] == spec["joint_plan"]["plan_sha256"]
    assert existing_bytes(spec["datastore_root"]) == before
    assert not Path(spec["state_root"]).exists()


def test_received_plan_and_stats_adopt_idempotently_without_activation(handoff_inputs, monkeypatch):
    import ml.joint_capital_plan
    monkeypatch.setattr(ml.joint_capital_plan, "compose_joint_plan", lambda *a, **kw: pytest.fail("No recomposition"))
    spec = handoff_inputs
    account_path = Path(spec["account_config"]["path"])
    profile_path = Path(spec["local_profile"]["path"])
    before = account_path.read_bytes(), profile_path.read_bytes()
    result = module.run_handoff(spec, now=NOW)
    assert result["status"] == "HANDOFF_VERIFIED_LOCAL"
    assert result["ui_ready"] and result["joint_ready"]
    assert result["execution_authorized"] is False and result["activation_changed"] is False
    assert result["orders_placed"] == 0
    root = Path(spec["datastore_root"])
    assert set(load_gameplan(root).symbols) == {"AAPL", "ABCL"}
    assert set(load_gameplan_stats(root).symbols) == {"AAPL", "ABCL"}
    assert (Path(result["ui_evidence"]["plan_run"]) / "joint-plan.json").read_bytes() == Path(spec["joint_plan"]["path"]).read_bytes()
    assert load_account_config(root).activation["status"] == "PREPARING"
    assert (account_path.read_bytes(), profile_path.read_bytes()) == before
    receipt_bytes = Path(result["receipt_path"]).read_bytes()
    runs = tuple((root / "ml/gameplan-actuals-review-runs").iterdir())
    assert module.run_handoff(spec, now=NOW) == result
    assert Path(result["receipt_path"]).read_bytes() == receipt_bytes
    assert tuple((root / "ml/gameplan-actuals-review-runs").iterdir()) == runs


@pytest.mark.parametrize("key", ["joint_plan", "scout_receipt", "local_profile", "account_config"])
def test_selected_file_mutations_fail_before_writes(handoff_inputs, key):
    spec = handoff_inputs
    path = Path(spec[key]["path"])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="reviewed SHA"):
        module.run_handoff(spec, now=NOW)
    assert not Path(spec["state_root"]).exists()
    assert not (Path(spec["datastore_root"]) / "ml").exists()


@pytest.mark.parametrize("damage,message", [
    ("actor", "belongs to Atlas"), ("executor", "belongs to Atlas"),
    ("account", "Local account identity"), ("universe", "Local account identity"),
    ("profile", "Atlas profile"), ("scout_receipt_actor", "Scout's completed synthesis"),
    ("scout_receipt_stats", "source package hashes"), ("session", "session or account"),
    ("plan_hash", "selected digest"), ("receipt_plan_file", "frozen plan bytes"),
])
def test_receipt_identity_and_account_guards(handoff_inputs, damage, message):
    spec = handoff_inputs
    if damage == "actor":
        spec["local_actor"] = "scout"
    elif damage == "executor":
        spec["executor_owner"] = "scout"
    elif damage in {"account", "universe"}:
        path = Path(spec["account_config"]["path"])
        value = json.loads(path.read_text())
        if damage == "account":
            value["account_fingerprint"] = "f" * 64
        else:
            value["participants"]["pc-new"] = ["MU"]
        value["activation"]["binding_sha256"] = digest({k: v for k, v in value.items() if k != "activation"})
        path.write_text(json.dumps(value))
        spec["account_config"] = bind(path)
    elif damage == "profile":
        path = Path(spec["local_profile"]["path"])
        value = json.loads(path.read_text()); value["actor"] = "Scout"
        path.write_text(json.dumps(value)); spec["local_profile"] = bind(path)
    elif damage.startswith("scout_receipt") or damage == "receipt_plan_file":
        path = Path(spec["scout_receipt"]["path"])
        value = json.loads(path.read_text())
        if damage == "scout_receipt_actor":
            value["local_actor"] = "atlas"
        elif damage == "scout_receipt_stats":
            value["stats_packages"]["Scout"] = "f" * 64
        else:
            value["ui_evidence"]["files"] = {}
        path.write_text(json.dumps(value)); spec["scout_receipt"] = bind(path)
    elif damage == "session":
        spec["review_session"] = "2026-09-04"
    else:
        spec["joint_plan"]["plan_sha256"] = "f" * 64
    with pytest.raises(ValueError, match=message):
        module.preflight_handoff(spec, now=NOW)
    assert not Path(spec["state_root"]).exists()
    assert not (Path(spec["datastore_root"]) / "ml").exists()


@pytest.mark.parametrize("stage", ["plan", "stats"])
def test_crash_after_pointer_recovers_exact_adoption(handoff_inputs, monkeypatch, stage):
    spec = handoff_inputs
    name = "_adopt_" + stage
    original = getattr(module, name)
    def fail_after_pointer(root, *args):
        result = original(root, *args)
        if root == Path(spec["datastore_root"]):
            raise RuntimeError("fixture interrupted pointer checkpoint")
        return result
    monkeypatch.setattr(module, name, fail_after_pointer)
    with pytest.raises(RuntimeError, match="fixture interrupted"):
        module.run_handoff(spec, now=NOW)
    assert json.loads(journal(spec).read_text())["status"] == "PARTIAL_ADOPTION"
    monkeypatch.setattr(module, name, original)
    assert module.run_handoff(spec, now=NOW)["status"] == "HANDOFF_VERIFIED_LOCAL"


def test_retry_rejects_changed_specification_without_receipt_rewrite(handoff_inputs):
    spec = handoff_inputs
    module.run_handoff(spec, now=NOW)
    before = journal(spec).read_bytes()
    changed = deepcopy(spec)
    changed["accepted_at"] = "2026-09-09T10:01:00Z"
    with pytest.raises(ValueError, match="exact local specification"):
        module.run_handoff(changed, now="2026-09-09T10:01:00Z")
    assert journal(spec).read_bytes() == before


def test_completed_receipt_does_not_hide_changed_selected_package(handoff_inputs):
    spec = handoff_inputs
    module.run_handoff(spec, now=NOW)
    path = Path(spec["owners"]["scout"]["stats_package"]["path"])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="reviewed SHA"):
        module.run_handoff(spec, now=NOW)


def test_existing_non_atlas_acceptance_is_not_claimed_as_our_partial_adoption(handoff_inputs):
    spec = handoff_inputs
    prepared = module.preflight_handoff(spec, now=NOW)
    other = {**spec, "local_actor": "scout"}
    root = Path(spec["datastore_root"])
    module._adopt_plan(root, other, prepared)
    before = existing_bytes(root)
    with pytest.raises(ValueError, match="local authority"):
        module.run_handoff(spec, now=NOW)
    assert json.loads(journal(spec).read_text())["status"] == "FAILED"
    assert existing_bytes(root) == before


def test_cli_validate_only_checks_profile_and_does_not_adopt(handoff_inputs, tmp_path, capsys):
    spec = handoff_inputs
    path = tmp_path / "handoff-spec.json"; path.write_text(json.dumps(spec))
    assert module.main(["--spec", str(path), "--local-profile", spec["local_profile"]["path"], "--validate-only"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "INPUTS_VERIFIED"
    assert not Path(spec["state_root"]).exists()
    with pytest.raises(ValueError, match="installed local profile"):
        module.main(["--spec", str(path), "--local-profile", str(tmp_path / "other.json"), "--validate-only"])
