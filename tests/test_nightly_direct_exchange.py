"""The normal nightly path combines once on Atlas; Scout only receives it."""
import json
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
