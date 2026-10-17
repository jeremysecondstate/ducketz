from copy import deepcopy
import json
from pathlib import Path

import pytest

from ml.artifacts import file_checksum
from ml.joint_capital_plan import publish_owner_package
from ml.nightly_synthesis import run_synthesis
from tests.test_nightly_synthesis import specification
from tests.test_joint_capital_plan import package, NOW
from tools import nightly_exchange as exchange
from tools import nightly_exchange_revision as revision


@pytest.fixture
def setup_revision(tmp_path, specification, monkeypatch):
    original = run_synthesis(specification, now=NOW)
    config = {"actor": "Scout", "private_exchange_authorized": True,
        "workflow_config": str(tmp_path / "workflow.json"), "local_profile": str(tmp_path / "profile.json"),
        "state_root": str(tmp_path / "exchange-local"), "exchange_root": str(tmp_path / "exchange-shared"),
        "account_scope_sha256": specification["account_scope_sha256"],
        "owners": {actor: value["symbols"] for actor, value in specification["owners"].items()}}
    action, review = specification["action_date"], specification["review_session"]
    prepared = {}
    for actor in ("atlas", "scout"):
        owner = specification["owners"][actor]
        prepared[actor] = exchange._publish({**config, "actor": actor.title()}, action, review, "preparation",
            {f"{kind}.json": Path(owner[f"{kind}_package"]["path"]) for kind in ("plan", "stats")})
    files = {"joint-plan.json": Path(original["ui_evidence"]["plan_run"]) / "joint-plan.json",
             "scout-receipt.json": Path(original["receipt_path"])}
    for actor, owner in specification["owners"].items():
        files.update({f"{actor}-{kind}.json": Path(owner[f"{kind}_package"]["path"]) for kind in ("plan", "stats")})
    joint = exchange._publish(config, action, review, "joint", files,
        {f"{actor}_preparation": selected["digest"] for actor, selected in prepared.items()})
    status = {"status": "COMPLETE", "actor": "Scout", "action_date": action, "review_session": review,
              "delivery_mode": "TWO_PLAN_GAMEPLAN", "delivered": True, "joint_packet_sha256": joint["digest"]}
    exchange._write(Path(config["state_root"]) / "sessions" / action / "status.json", exchange._bytes(status))
    new_package = package("scout", "ABCL", .65)
    (tmp_path / "new-source").mkdir()
    new_path = publish_owner_package(tmp_path / "new-source", new_package)
    spec = {"schema_version": revision.VERSION, "revision_id": "explicit-revision-001", "action_date": action,
        "review_session": review, "as_of": NOW,
        "predecessor": {"joint_packet_sha256": joint["digest"], "joint_plan_sha256": original["ui_evidence"]["plan_sha256"],
            **{f"{actor}_package_sha256": owner["plan_package"]["package_sha256"] for actor, owner in specification["owners"].items()}},
        "scout": {"plan_package": {"root": str(new_path.parent), "path": str(new_path),
            "file_sha256": file_checksum(new_path), "package_sha256": new_package["package_sha256"]},
            "stats_package": specification["owners"]["scout"]["stats_package"]}}
    monkeypatch.setattr(exchange, "load_config", lambda path: config)
    monkeypatch.setattr(exchange.workflow, "load_config", lambda path: {"datastore": specification["datastore_root"]})
    monkeypatch.setattr(revision, "validate_local_profile", lambda *args: None)
    return config, spec, specification


def test_delivers_idempotent_plan_only_revision_and_keeps_all_previous_bytes(setup_revision, tmp_path):
    config, spec, original = setup_revision
    watched = [p for folder in (Path(config["state_root"]), Path(config["exchange_root"]), Path(original["datastore_root"]))
               for p in folder.rglob("*") if p.is_file()]
    before = {str(p): p.read_bytes() for p in watched}
    result = revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)
    assert result["status"] == "DELIVERED_RESEARCH_REVISION" and result["delivered"]
    assert result["cash_basis"] == "SIZED_AT_EXECUTION"
    assert result["account_snapshot_sha256"] is None
    assert result["predecessor_account_snapshot_sha256"] is not None
    assert not result["accepted_execution_selection_changed"] and not result["execution_authorized"]
    assert result["adoption_required"] and result["orders_placed"] == 0
    assert all(Path(p).read_bytes() == value for p, value in before.items())
    assert file_checksum(Path(result["delivery_path"])) == result["packet_sha256"]
    assert revision.deliver_revision(tmp_path / "config.json", spec, now=NOW) == result


def test_exact_saved_account_basis_is_preserved_without_capture(setup_revision, tmp_path):
    config, spec, original = setup_revision
    spec["saved_account_basis"] = original["snapshot"]
    result = revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)
    assert result["account_snapshot_sha256"] == result["predecessor_account_snapshot_sha256"]
    assert not result["account_capture_performed"]


@pytest.mark.parametrize("field", sorted(revision.PREDECESSOR_FIELDS))
def test_wrong_predecessor_is_rejected_before_packet_write(setup_revision, tmp_path, field):
    config, spec, _ = setup_revision
    spec["predecessor"][field] = "f" * 64
    with pytest.raises(ValueError, match="predecessor"):
        revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)
    assert not list((Path(config["exchange_root"]) / "sessions" / spec["action_date"] / "scout" / "revisions").glob("*/packet.json"))


def test_changed_shared_selector_fails_stale_guard(setup_revision, tmp_path):
    config, spec, _ = setup_revision
    path = exchange._folder(config, spec["action_date"], "atlas", "preparation") / "selection.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="Stale predecessor"):
        revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)


def test_conflicting_revision_for_same_predecessor_is_rejected(setup_revision, tmp_path):
    config, spec, _ = setup_revision
    result = revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)
    other = deepcopy(spec)
    other["revision_id"] = "conflicting-revision-002"
    with pytest.raises(ValueError, match="Conflicting revision"):
        revision.deliver_revision(tmp_path / "config.json", other, now=NOW)
    assert file_checksum(Path(result["delivery_path"])) == result["packet_sha256"]


def test_changed_source_after_delivery_cannot_reuse_receipt(setup_revision, tmp_path):
    _, spec, _ = setup_revision
    revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)
    path = Path(spec["scout"]["plan_package"]["path"])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="reviewed SHA"):
        revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)


def test_partial_packet_delivery_resumes_same_claim(setup_revision, tmp_path, monkeypatch):
    config, spec, _ = setup_revision
    original = exchange._write
    destination = exchange._folder(config, spec["action_date"], "scout", "revisions") / spec["revision_id"] / "packet.json"
    def fail(path, data, **kwargs):
        if Path(path) == destination:
            raise OSError("fixture delivery interruption")
        return original(path, data, **kwargs)
    monkeypatch.setattr(exchange, "_write", fail)
    with pytest.raises(OSError, match="delivery interruption"):
        revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)
    monkeypatch.setattr(exchange, "_write", original)
    assert revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)["delivered"]


@pytest.mark.parametrize("identity", ["../bad-id", "/absolute", "x", "a/../../escape"])
def test_revision_identity_cannot_escape_namespace(setup_revision, tmp_path, identity):
    _, spec, _ = setup_revision
    spec["revision_id"] = identity
    with pytest.raises(ValueError, match="safe"):
        revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)


def test_scoped_paths_reject_parent_escape(tmp_path):
    root = tmp_path / "selected-root"
    root.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        revision._within(root, root / ".." / "elsewhere" / "packet.json")


def test_delivery_readback_failure_never_records_completion(setup_revision, tmp_path, monkeypatch):
    config, spec, _ = setup_revision
    original = exchange._write
    destination = exchange._folder(config, spec["action_date"], "scout", "revisions") / spec["revision_id"] / "packet.json"
    def corrupt(path, data, **kwargs):
        result = original(path, data, **kwargs)
        if Path(path) == destination:
            Path(path).write_bytes(data + b" ")
        return result
    monkeypatch.setattr(exchange, "_write", corrupt)
    with pytest.raises(ValueError, match="readback differs"):
        revision.deliver_revision(tmp_path / "config.json", spec, now=NOW)
    assert not (destination.parent / "delivery.json").exists()
