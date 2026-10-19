"""The normal nightly path combines once on Atlas; Scout only receives it."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
import pandas as pd

from ml import nightly_direct_exchange as direct, nightly_synthesis
from ml.joint_capital_adoption import read_accepted_joint_plan
from tools import nightly_exchange as module
from tests.test_nightly_exchange import exchange, source_inputs, write
from tests.test_joint_capital_plan import NOW, DAY


@pytest.fixture
def setup(exchange, monkeypatch):
    configs, natives, states, _ = exchange
    for actor, native in natives.items():
        write(Path(native["state_root"]) / "runs" / DAY / "state.json", states[actor])
    def forbidden(*a, **kw):
        pytest.fail("The direct nightly path must not consult legacy repair, source, or peer ownership gates")
    for name in ("_local_state", "_binding", "_ownership_observations", "_ensure_ownership_responder"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.workflow, "source_identity", forbidden)
    from ml import nightly_exchange_repair
    monkeypatch.setattr(nightly_exchange_repair, "dispatch_guard", forbidden)
    monkeypatch.setattr(nightly_exchange_repair, "resolve", forbidden)
    monkeypatch.setattr(nightly_synthesis, "__file__", str(Path(natives["atlas"]["repository"]) / "ml/nightly_synthesis.py"))
    return exchange


def wake(setup, actor, now=NOW):
    return module.run_once(setup[0][actor], now=now)


def test_normal_nightly_path_synthesizes_once_and_returns_same_plan_without_ack(setup, monkeypatch):
    calls = []
    compose = nightly_synthesis.compose_joint_plan
    def record(*a, **kw):
        calls.append(kw["action_date"])
        return compose(*a, **kw)
    monkeypatch.setattr(nightly_synthesis, "compose_joint_plan", record)
    assert wake(setup, "scout")["status"] == "PREPARATION_PUBLISHED"
    atlas = wake(setup, "atlas")
    assert atlas["status"] == "COMPLETE" and atlas["joint_ready"] and atlas["ui_ready"]
    assert not atlas["peer_verified"] and not atlas["execution_authorized"]
    assert atlas["peer_output_delivery"] == "DELIVERED"
    assert not (Path(setup[0]["scout"]["state_root"]) / "sessions" / DAY / "direct-completion.json").exists()
    scout = wake(setup, "scout")
    assert scout["status"] == "COMPLETE" and scout["joint_ready"]
    selections = {actor: read_accepted_joint_plan(Path(native["datastore"]), DAY)
                  for actor, native in setup[1].items()}
    assert selections["atlas"][0] == selections["scout"][0]
    assert selections["atlas"][1]["local_actor"] == "atlas"
    assert selections["scout"][1]["local_actor"] == "scout"
    assert calls == [DAY] and len(setup[3]) == 1
    assert wake(setup, "atlas", "2026-09-09T12:00:00Z") == atlas
    assert wake(setup, "scout", "2026-09-09T12:00:00Z") == scout
    assert calls == [DAY] and len(setup[3]) == 1


def test_legacy_claims_and_source_drift_never_block_completed_package_inputs(setup):
    original = {}
    for actor, native in setup[1].items():
        old = Path(native["state_root"]) / "repair-owner.json"
        write(old, {"owner": "legacy-intraday-adoption", "repair_id": "old-claim", "domain": "exchange"})
        state = Path(native["state_root"]) / "runs" / DAY / "state.json"
        value = json.loads(state.read_bytes())
        value.update(status="OLD_HELPER_PENDING", source_identity={"commit": "old-source"})
        write(state, value)
        original[old] = old.read_bytes()
    wake(setup, "scout")
    assert wake(setup, "atlas")["status"] == "COMPLETE"
    assert {path: path.read_bytes() for path in original} == original


def test_peer_output_delivery_failure_does_not_gate_or_repeat_atlas_synthesis(setup, monkeypatch):
    wake(setup, "scout")
    publish = module._publish
    def missing_delivery(config, action, review, kind, *a, **kw):
        if kind == "combined":
            raise OSError("fixture peer drive disconnected")
        return publish(config, action, review, kind, *a, **kw)
    monkeypatch.setattr(module, "_publish", missing_delivery)
    first = wake(setup, "atlas")
    assert first["status"] == "COMPLETE" and first["peer_output_delivery"] == "PENDING_DELIVERY"
    monkeypatch.setattr(nightly_synthesis, "run_synthesis", lambda *a, **kw: pytest.fail("Do not synthesize again"))
    monkeypatch.setattr(module, "_publish", publish)
    second = wake(setup, "atlas", "2026-09-09T12:00:00Z")
    assert second["status"] == "COMPLETE" and second["peer_output_delivery"] == "DELIVERED"
    assert first["receipt_path"] == second["receipt_path"] and len(setup[3]) == 1


def test_atlas_waits_only_for_missing_research_package_before_capturing(setup):
    pending = wake(setup, "atlas")
    assert pending["status"] == "PENDING" and pending["reason"] == "PREPARATION_SCOUT"
    assert not setup[3]
    assert wake(setup, "scout")["status"] == "PREPARATION_PUBLISHED"
    assert wake(setup, "atlas")["status"] == "COMPLETE"


def test_changed_package_data_is_rejected_without_capture(setup):
    output = setup[2]["scout"]["steps"]["local_handoff"]["output"]
    path = Path(output["package"])
    value = json.loads(path.read_bytes())
    value["action_date"] = "2026-09-08"
    write(path, value)
    with pytest.raises(ValueError):
        wake(setup, "scout")
    assert not setup[3]


def test_synthesis_uses_actual_post_capture_time_and_keeps_snapshot_observation(setup, monkeypatch):
    from tools import nightly_account_snapshot
    wake(setup, "scout")
    ticks = [10.0]
    monkeypatch.setattr(module, "monotonic", lambda: ticks[0])
    capture = nightly_account_snapshot.capture_snapshot
    def slow_capture(config, now=None):
        value = capture(config, now=now)
        value["observed_at"] = (pd.Timestamp(now) + pd.Timedelta(seconds=60)).isoformat()
        ticks[0] += 120
        return value
    monkeypatch.setattr(nightly_account_snapshot, "capture_snapshot", slow_capture)
    assert wake(setup, "atlas")["status"] == "COMPLETE"
    session = Path(setup[0]["atlas"]["state_root"]) / "sessions" / DAY
    frozen = json.loads((session / "direct-synthesis-selection.json").read_bytes())
    snapshot = json.loads((session / "direct-account-snapshot.json").read_bytes())
    assert pd.Timestamp(frozen["spec"]["as_of"]) == pd.Timestamp(NOW) + pd.Timedelta(seconds=120)
    assert pd.Timestamp(snapshot["observed_at"]) == pd.Timestamp(NOW) + pd.Timedelta(seconds=60)


def test_missing_optional_delivery_intent_cannot_halt_completed_atlas_result(setup):
    wake(setup, "scout")
    first = wake(setup, "atlas")
    session = Path(setup[0]["atlas"]["state_root"]) / "sessions" / DAY
    (session / "direct-synthesis-selection.json").unlink()
    retry = wake(setup, "atlas")
    assert retry["status"] == "COMPLETE" and retry["peer_output_delivery"] == "PENDING_DELIVERY"
    assert retry["receipt_path"] == first["receipt_path"] and len(setup[3]) == 1


def _select_policy_revision(setup, *, mutate=None, revision_changes=None):
    """Install an immutable local descendant, preserving the original synthesis."""
    from ml.joint_capital_plan import content_sha256
    native = setup[1]["atlas"]
    root = Path(native["datastore"])
    parent, binding, _ = read_accepted_joint_plan(root, DAY)
    plan = deepcopy(parent)
    revised_at = (pd.Timestamp(binding["accepted_at"]) + pd.Timedelta(minutes=1)).isoformat()
    revision = {"schema_version": "gameplan-policy-revision-v1",
        "parent_plan_sha256": parent["plan_sha256"],
        "ancestor_plan_sha256": [*parent.get("policy_revision", {}).get("ancestor_plan_sha256", []), parent["plan_sha256"]],
        "revised_at": revised_at, "direction_policy_version": "stock-direction-50-01-v3",
        "direction_up_threshold": .5001, "direction_down_threshold": .4999,
        "account_basis": "ORIGINAL_SAVED_OBSERVATION_NOT_CURRENT_BROKER_STATE"}
    revision.update(revision_changes or {})
    revision["revision_id"] = content_sha256(revision)
    plan["policy_revision"] = revision
    if mutate is not None:
        mutate(plan)
    plan.pop("plan_sha256")
    plan.pop("plan_id")
    views = plan.pop("owner_views")
    plan["plan_id"] = "joint-capital:" + content_sha256(plan)
    plan["owner_views"] = {owner: {**view, "plan_id": plan["plan_id"]} for owner, view in views.items()}
    plan["plan_sha256"] = content_sha256(plan)
    run = root / "ml/joint-gameplan-runs" / plan["plan_sha256"]
    revised_binding = {**binding, "plan_sha256": plan["plan_sha256"],
        "run_path": run.relative_to(root).as_posix(), "policy_revision": revision,
        "accepted_at": revised_at}
    write(run / "joint-plan.json", plan)
    write(run / "accepted-plan.json", revised_binding)
    write(root / "ml/joint-gameplan-by-date" / DAY / "run.json", revised_binding)
    return plan, run


def _completed_atlas(setup):
    wake(setup, "scout")
    result = wake(setup, "atlas")
    session = Path(setup[0]["atlas"]["state_root"]) / "sessions" / DAY
    protected = [session / "direct-completion.json", Path(result["receipt_path"]),
                 session / "direct-account-snapshot.json"]
    from tests.test_nightly_exchange import files
    original_exchange = files(Path(setup[0]["atlas"]["exchange_root"]))
    return result, {path: path.read_bytes() for path in protected}, original_exchange


def test_completed_synthesis_reuses_policy_descendant_and_separate_delivery(setup, monkeypatch):
    import base64
    from tests.test_nightly_exchange import files
    original, protected, original_exchange = _completed_atlas(setup)
    revised, run = _select_policy_revision(setup)
    from tools import nightly_account_snapshot
    monkeypatch.setattr(nightly_account_snapshot, "capture_snapshot", lambda *a, **kw: pytest.fail("No new account capture"))
    monkeypatch.setattr(nightly_synthesis, "run_synthesis", lambda *a, **kw: pytest.fail("No repeated synthesis"))
    result = wake(setup, "atlas")
    assert result["status"] == "COMPLETE" and result["peer_output_delivery"] == "DELIVERED"
    assert result["receipt_path"] == original["receipt_path"]
    assert result["plan_sha256"] == revised["plan_sha256"]
    assert result["synthesis_plan_sha256"] == revised["policy_revision"]["parent_plan_sha256"]
    assert result["policy_revision"] == revised["policy_revision"]
    assert result["orders_placed"] == 0 and not result["activation_changed"]
    root = Path(setup[0]["atlas"]["exchange_root"])
    current_exchange = files(root)
    assert all(current_exchange[name] == raw for name, raw in original_exchange.items())
    folder = root / "policy-revisions" / revised["policy_revision"]["revision_id"] / "sessions" / DAY / "atlas/combined"
    selected = json.loads((folder / "selection.json").read_bytes())
    packet = json.loads((folder / "packets" / (selected["content_sha256"] + ".json")).read_bytes())
    assert base64.b64decode(packet["files"]["joint-plan.json"]["base64"]) == (run / "joint-plan.json").read_bytes()
    published_receipt = json.loads(base64.b64decode(packet["files"]["atlas-synthesis-receipt.json"]["base64"]))
    assert published_receipt["schema_version"] == "gameplan-policy-revision-publication-v1"
    assert published_receipt["ui_evidence"]["plan_sha256"] == revised["plan_sha256"]
    assert wake(setup, "atlas") == result
    assert files(root) == current_exchange
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    assert len(setup[3]) == 1


def test_repeated_revision_follows_real_immutable_parent_chain(setup):
    _, protected, _ = _completed_atlas(setup)
    first, _ = _select_policy_revision(setup)
    second, _ = _select_policy_revision(setup)
    result = wake(setup, "atlas")
    assert result["status"] == "COMPLETE" and result["plan_sha256"] == second["plan_sha256"]
    assert result["synthesis_plan_sha256"] == first["policy_revision"]["parent_plan_sha256"]
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    assert len(setup[3]) == 1


@pytest.mark.parametrize("change", ["probability", "account", "ancestry", "parent", "time", "schema"])
def test_policy_revision_cannot_substitute_synthesis_inputs_or_ancestry(setup, change):
    _, protected, _ = _completed_atlas(setup)
    mutate, metadata = None, {}
    if change == "probability":
        mutate = lambda plan: plan["forecasts"][0].update(calibrated_probability=.8)
    elif change == "account":
        mutate = lambda plan: plan.update(account_snapshot_sha256="f" * 64)
    elif change == "ancestry":
        metadata = {"ancestor_plan_sha256": []}
    elif change == "parent":
        metadata = {"parent_plan_sha256": "../unexpected"}
    elif change == "time":
        metadata = {"revised_at": None}
    elif change == "schema":
        metadata = {"schema_version": "unrelated-replacement-v1"}
    _select_policy_revision(setup, mutate=mutate, revision_changes=metadata)
    with pytest.raises(ValueError):
        wake(setup, "atlas")
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    assert len(setup[3]) == 1


def test_revision_delivery_failure_never_gates_completed_local_result(setup, monkeypatch):
    _completed_atlas(setup)
    revised, _ = _select_policy_revision(setup)
    def disconnected(*args, **kwargs):
        raise OSError("private revision transport disconnected")
    monkeypatch.setattr(module, "_publish", disconnected)
    result = wake(setup, "atlas")
    assert result["status"] == "COMPLETE" and result["joint_ready"]
    assert result["plan_sha256"] == revised["plan_sha256"]
    assert result["peer_output_delivery"] == "PENDING_DELIVERY"
    assert len(setup[3]) == 1


def test_policy_revision_rejects_changed_original_receipt_and_missing_parent(setup):
    _, protected, _ = _completed_atlas(setup)
    revised, _ = _select_policy_revision(setup)
    receipt = next(path for path in protected if path.name == "receipt.json")
    receipt.write_bytes(protected[receipt] + b"\n")
    with pytest.raises(ValueError, match="Saved synthesis receipt changed"):
        wake(setup, "atlas")
    receipt.write_bytes(protected[receipt])
    root = Path(setup[1]["atlas"]["datastore"]) / "ml/joint-gameplan-runs"
    parent_path = root / revised["policy_revision"]["parent_plan_sha256"] / "joint-plan.json"
    parent_path.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        wake(setup, "atlas")
    assert len(setup[3]) == 1
