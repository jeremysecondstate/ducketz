"""Actual local receipts and UI adoption; no providers, fitting or order calls."""
from copy import deepcopy
import json
from pathlib import Path
import re

import pandas as pd
import pytest

from ml import nightly_handoff, nightly_readiness, nightly_synthesis
from ml.nightly_joint_readiness import verify_joint_readiness
from ml.account_gameplan.config import CONFIG, VERSION as ACCOUNT_VERSION, digest
from ml.artifacts import file_checksum
from ml.gameplan_stats_handoff import export_stats_package
from ml.joint_capital_plan import build_owner_package, content_sha256, publish_owner_package
from tests.gameplan_stats_fixture import forecast, write_review
from tests import test_joint_capital_plan as joint_fixture
from tests.test_nightly_workflow import _display_fixture


DAY, REVIEW, NOW = "2026-09-14", "2026-09-11", "2026-09-14T10:00:00Z"
LOCAL_SYMBOLS, PEER_SYMBOLS = ["AAPL", "GOOG", "NVDA"], ["ABCL", "DBX", "DOCU"]


def _bind(path):
    return {"path": str(path), "file_sha256": file_checksum(path)}


def _json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _prepare(tmp_path, monkeypatch, actor):
    """Bind synthetic packages to real original worker/feedback artifacts."""
    monkeypatch.setattr(joint_fixture, "DAY", DAY)
    monkeypatch.setattr(joint_fixture, "NOW", NOW)
    root = tmp_path / "local"
    state, trade, stats, source = _display_fixture(root)
    config = {"actor": actor.title(), "datastore": str(root), "repository": str(tmp_path / "checkout")}
    source_identity = {"commit": "reviewed-fixture"}
    state.update(schema_version=nightly_readiness.workflow.VERSION, actor=actor.title(),
                 source_identity=source_identity, status="LOCAL_COMPLETE_PEER_SETUP_PENDING",
                 run_id="local-preparation", completed_at=NOW)
    for step in nightly_readiness.workflow.STEPS:
        entry = state["steps"].setdefault(step, {"output": {"files": {}}})
        entry["status"] = "COMPLETE"
    state["steps"]["verify_display"]["output"] = nightly_readiness.workflow._display(config, state)
    exports = tmp_path / "exports"
    exports.mkdir()
    owners = {}
    for owner in ("atlas", "scout"):
        symbols = LOCAL_SYMBOLS if owner == actor else PEER_SYMBOLS
        rows = [dict(row, id=f"{symbol}-{row['id']}") for symbol in symbols
                for row in joint_fixture.records(symbol)]
        prices = joint_fixture.prices(symbols[0])
        prices["points"] = {key: value for symbol in symbols for key, value in joint_fixture.prices(symbol)["points"].items()}
        if owner == actor:
            _json(source / "manifest.json", {"fixture": "frozen native source"})
            pd.DataFrame(rows).to_parquet(source / "forecasts.parquet", index=False)
            _json(trade / "planning-price-path.json", prices)
            hashes = {field: file_checksum(path) for field, path in (
                ("receipt_sha256", source / "receipt.json"), ("manifest_sha256", source / "manifest.json"),
                ("forecasts_sha256", source / "forecasts.parquet"), ("price_path_sha256", trade / "planning-price-path.json"))}
            owner_stats, stats_root, run_id = stats, root, source.name
        else:
            hashes = {field: content_sha256([owner, field]) for field in
                      ("receipt_sha256", "manifest_sha256", "forecasts_sha256", "price_path_sha256")}
            stats_root = tmp_path / "peer-stats"
            owner_stats = write_review(stats_root, [forecast(symbol, session=REVIEW) for symbol in symbols], session=REVIEW)
            run_id = f"fixture-{owner}"
        payload = build_owner_package(owner_id=owner, run_id=run_id, source_revision="b" * 40,
            action_date=DAY, frozen_symbols=symbols, created_at=NOW, source_hashes=hashes,
            forecasts=rows, price_path=prices)
        plan_path = publish_owner_package(exports, payload)
        stats_path = export_stats_package(stats_root, producer=owner.title(), symbols=symbols,
            destination=exports / f"{owner}-stats.json", action_date=REVIEW, review_run=owner_stats)
        owners[owner] = {"symbols": symbols, "plan_package": {**_bind(plan_path), "root": str(exports),
            "package_sha256": payload["package_sha256"]}, "stats_package": _bind(stats_path)}
    local_plan = Path(owners[actor]["plan_package"]["path"])
    local_stats = Path(owners[actor]["stats_package"]["path"])
    state["steps"]["local_handoff"]["output"] = {"package": str(local_plan), "stats_package": str(local_stats),
        "delivery": "HELD_FOR_SEPARATE_PC_SETUP", "files": {str(path): file_checksum(path) for path in (local_plan, local_stats)}}
    snapshot = joint_fixture.snapshot(cash=200)
    for field in ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure"):
        snapshot[field] = dict.fromkeys(LOCAL_SYMBOLS + PEER_SYMBOLS, 0)
    snapshot["quotes"] = {symbol: {"ask": 11} for symbol in LOCAL_SYMBOLS + PEER_SYMBOLS}
    snapshot_path = exports / "snapshot.json"
    _json(snapshot_path, snapshot)
    spec = {"schema_version": nightly_synthesis.VERSION, "completion_id": "fixture-scout-completion",
        "datastore_root": str(root if actor == "scout" else tmp_path / "scout-adoption"),
        "state_root": str(tmp_path / "scout-state"), "local_actor": "scout", "executor_owner": "atlas",
        "action_date": DAY, "review_session": REVIEW, "account_scope_sha256": joint_fixture.SCOPE,
        "as_of": NOW, "accepted_at": NOW, "snapshot": _bind(snapshot_path), "owners": owners}
    monkeypatch.setattr(nightly_readiness.workflow, "verify_installation", lambda *_: None)
    monkeypatch.setattr(nightly_readiness.workflow, "_verify_configuration_binding", lambda *_: None)
    monkeypatch.setattr(nightly_readiness.workflow, "_verify_symbol_binding", lambda *_: None)
    monkeypatch.setattr(nightly_readiness.workflow, "source_identity", lambda *_: source_identity)
    monkeypatch.setattr(nightly_readiness.workflow, "status", lambda *_: deepcopy(state))
    # The regression requires frozen verification, never today's changed default pointers.
    monkeypatch.setattr(nightly_readiness.workflow, "_display", lambda *_: pytest.fail("Readiness must not invoke _display"))
    scout_receipt = nightly_synthesis.run_synthesis(spec, now=NOW)
    if actor == "scout":
        return config, state, scout_receipt, spec
    checkout = Path(config["repository"])
    profile = checkout / "scratch/cross-pc/local-profile.json"
    profile.parent.mkdir(parents=True)
    _json(profile, {"contract_version": "cross-pc-v2", "actor": "Atlas", "machine": "pc-original",
                    "checkout": str(checkout), "symbols": LOCAL_SYMBOLS})
    account = {"schema_version": ACCOUNT_VERSION, "machine_id": "pc-original", "coordinator_id": "pc-original",
               "participants": {"pc-original": LOCAL_SYMBOLS, "pc-new": PEER_SYMBOLS},
               "account_fingerprint": joint_fixture.SCOPE}
    account["activation"] = {"status": "PREPARING", "binding_sha256": digest(account)}
    account_path = root / CONFIG
    account_path.parent.mkdir(parents=True)
    _json(account_path, account)
    plan_path = Path(scout_receipt["ui_evidence"]["plan_run"]) / "joint-plan.json"
    handoff_spec = {"schema_version": nightly_handoff.VERSION, "completion_id": "fixture-atlas-completion",
        "datastore_root": str(root), "state_root": str(tmp_path / "atlas-state"), "local_actor": "atlas",
        "executor_owner": "atlas", "action_date": DAY, "review_session": REVIEW,
        "account_scope_sha256": joint_fixture.SCOPE, "accepted_at": NOW, "local_profile": _bind(profile),
        "account_config": _bind(account_path), "scout_receipt": _bind(Path(scout_receipt["receipt_path"])),
        "joint_plan": {**_bind(plan_path), "root": str(plan_path.parent),
                       "plan_sha256": scout_receipt["ui_evidence"]["plan_sha256"]}, "owners": deepcopy(owners)}
    monkeypatch.setattr(nightly_handoff, "__file__", str(checkout / "ml/nightly_handoff.py"))
    return config, state, nightly_handoff.run_handoff(handoff_spec, now=NOW), handoff_spec


@pytest.fixture(params=["scout", "atlas"])
def adopted(tmp_path, monkeypatch, request):
    return _prepare(tmp_path, monkeypatch, request.param)


def _selection(config):
    return Path(config["datastore"]) / "ml/nightly-joint-readiness-by-date" / DAY / "run.json"


def _repin_receipt(config, receipt):
    path = Path(receipt["receipt_path"])
    _json(path, receipt)
    selection = _selection(config)
    pin = json.loads(selection.read_text())
    pin["receipt_sha256"] = file_checksum(path)
    _json(selection, pin)


def _assert_joint_failure(config, message):
    # The standalone verifier remains strict. The scheduled readiness report
    # preserves separately verified original local work when this phase fails.
    with pytest.raises(ValueError, match=message):
        verify_joint_readiness(config, nightly_readiness.workflow.status(config))
    result = nightly_readiness.readiness(config, now=NOW)
    assert result["status"] == "JOINT_VERIFICATION_FAILED" and result["local_ready"]
    assert not result["joint_ready"] and not result["ui_ready"]
    assert not result["execution_authorized"] and result["orders_placed"] == 0
    assert re.search(message, result["error"])


def test_real_completed_adoption_verifies_local_and_combined_readiness_without_mutation(adopted, tmp_path):
    config, _, receipt, _ = adopted
    before = _bytes(tmp_path)
    result = nightly_readiness.readiness(config, now=NOW)
    assert result["status"] == receipt["status"]
    assert result["local_ready"] and result["joint_ready"] and result["ui_ready"]
    assert result["peer_verified"] is False and result["execution_authorized"] is False
    assert result["peer_communication_enabled"] is False and result["orders_placed"] == 0
    assert result["receipt_sha256"] == file_checksum(Path(receipt["receipt_path"]))
    assert _bytes(tmp_path) == before


def test_combined_display_without_completion_receipt_preserves_local_preparation(adopted, tmp_path):
    config, _, _, _ = adopted
    _selection(config).unlink()
    before = _bytes(tmp_path)
    result = nightly_readiness.readiness(config, now=NOW)
    assert result["status"] == "JOINT_VERIFICATION_PENDING" and result["local_ready"]
    assert not result["joint_ready"] and not result["execution_authorized"]
    assert _bytes(tmp_path) == before


@pytest.mark.parametrize("damage", ["actor", "action_date", "review_session", "account", "plan", "owner_package", "stats", "missing_evidence"])
def test_rehashed_wrong_receipt_bindings_fail_without_mutation(adopted, tmp_path, damage):
    config, _, receipt, _ = adopted
    bad = deepcopy(receipt)
    if damage == "actor":
        bad["local_actor"] = "atlas" if config["actor"] == "Scout" else "scout"
    elif damage == "action_date":
        bad["action_date"] = "2026-09-15"
    elif damage == "review_session":
        bad["review_session"] = "2026-09-10"
    elif damage == "account":
        bad["account_scope_sha256"] = "f" * 64
    elif damage == "plan":
        bad["ui_evidence"]["plan_sha256"] = "f" * 64
    elif damage == "owner_package":
        bad["owner_packages"][config["actor"].lower()] = "f" * 64
    elif damage == "missing_evidence":
        del bad["ui_evidence"]
    else:
        bad["stats_packages"][config["actor"]] = "f" * 64
    _repin_receipt(config, bad)
    before = _bytes(tmp_path)
    _assert_joint_failure(config, "differs|differ")
    assert _bytes(tmp_path) == before


@pytest.mark.parametrize("field,filename", [("source_gameplan_run", "forecasts.parquet"), ("plan_run", "planning-price-path.json")])
def test_joint_completion_cannot_replace_original_native_source(adopted, tmp_path, field, filename):
    config, state, _, _ = adopted
    path = Path(state["steps"]["verify_display"]["output"][field]) / filename
    path.write_bytes(path.read_bytes() + b" ")
    before = _bytes(tmp_path)
    _assert_joint_failure(config, "frozen native source")
    assert _bytes(tmp_path) == before


def test_changed_completion_receipt_is_not_accepted_by_saved_pin(adopted, tmp_path):
    config, _, receipt, _ = adopted
    path = Path(receipt["receipt_path"])
    path.write_bytes(path.read_bytes() + b" ")
    before = _bytes(tmp_path)
    _assert_joint_failure(config, "receipt changed")
    assert _bytes(tmp_path) == before


def test_completed_retry_repairs_only_missing_selection(adopted, tmp_path):
    config, _, receipt, spec = adopted
    selection = _selection(config)
    selection_bytes = selection.read_bytes()
    receipt_bytes = Path(receipt["receipt_path"]).read_bytes()
    selection.unlink()
    before = _bytes(tmp_path)
    run = nightly_synthesis.run_synthesis if config["actor"] == "Scout" else nightly_handoff.run_handoff
    assert run(spec, now=NOW) == receipt
    assert selection.read_bytes() == selection_bytes
    assert Path(receipt["receipt_path"]).read_bytes() == receipt_bytes
    after = _bytes(tmp_path)
    del after[str(selection.relative_to(tmp_path))]
    assert after == before


def test_completed_retry_cannot_overwrite_different_selection(adopted, tmp_path):
    config, _, _, spec = adopted
    selection = _selection(config)
    pin = json.loads(selection.read_text())
    pin["completion_id"] = "another-reviewed-completion"
    _json(selection, pin)
    before = _bytes(tmp_path)
    run = nightly_synthesis.run_synthesis if config["actor"] == "Scout" else nightly_handoff.run_handoff
    with pytest.raises(ValueError, match="different joint readiness receipt"):
        run(spec, now=NOW)
    assert _bytes(tmp_path) == before


@pytest.mark.parametrize("damage", ["actor", "action_date", "review_session", "relative_path", "completion_id"])
def test_selection_is_bound_to_local_actor_session_and_explicit_receipt(adopted, tmp_path, damage):
    config, _, _, _ = adopted
    path = _selection(config)
    pin = json.loads(path.read_text())
    if damage == "actor":
        pin["local_actor"] = "atlas" if config["actor"] == "Scout" else "scout"
    elif damage == "action_date":
        pin["action_date"] = "2026-09-15"
    elif damage == "review_session":
        pin["review_session"] = "2026-09-10"
    elif damage == "relative_path":
        pin["receipt_path"] = "receipt.json"
    else:
        pin["completion_id"] = "another-completion"
    _json(path, pin)
    before = _bytes(tmp_path)
    _assert_joint_failure(config, "differs|absolute paths")
    assert _bytes(tmp_path) == before


@pytest.mark.parametrize("pointer_kind", ["gameplan-actuals-review-latest", f"gameplan-actuals-review-by-date/{REVIEW}"])
def test_default_and_dated_stats_must_both_display_the_completed_adoption(adopted, tmp_path, pointer_kind):
    config, state, _, _ = adopted
    root = Path(config["datastore"])
    original = Path(state["steps"]["verify_display"]["output"]["stats_run"])
    path = root / "ml" / pointer_kind / "run.json"
    pointer = json.loads(path.read_text())
    pointer["current"].update(run_path=original.relative_to(root).as_posix(),
                              receipt_sha256=file_checksum(original / "receipt.json"))
    _json(path, pointer)
    before = _bytes(tmp_path)
    _assert_joint_failure(config, "Default combined UI differs")
    assert _bytes(tmp_path) == before
