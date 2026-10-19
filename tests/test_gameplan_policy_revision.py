"""Policy corrections preserve saved inputs and never obtain broker state."""
import hashlib
import json

import pytest

from ml.gameplan_policy_revision import revise_accepted_gameplan
from ml.joint_capital_plan import _json_bytes
from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION


def _fixture(tmp_path, monkeypatch, *, near_neutral=False, full_universe=False):
    import test_joint_capital_plan as fixture
    from ml import gameplan_cash_ledger as ledger

    monkeypatch.setattr(fixture, "DAY", "2026-10-19")
    monkeypatch.setattr(fixture, "NOW", "2026-10-17T07:54:09Z")
    universes = {"atlas": ["AAPL"], "scout": ["ABCL"]}
    if full_universe:
        universes = {"atlas": ["AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ"],
            "scout": ["DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN"]}
    packages = []
    for owner, symbols in universes.items():
        probability = .50005 if near_neutral and owner == "atlas" else .7 if owner == "atlas" else .4872
        sample = fixture.package(owner, symbols[0], probability)
        price_path = fixture.prices(symbols[0])
        price_path["points"] = {key: value for symbol in symbols for key, value in fixture.prices(symbol)["points"].items()}
        packages.append(fixture.build_owner_package(owner_id=owner, run_id=sample["run_id"], source_revision="b" * 40,
            action_date=fixture.DAY, frozen_symbols=symbols, created_at=fixture.NOW, source_hashes=sample["source_hashes"],
            forecasts=[{**row, "id": symbol + ":" + row["id"]} for symbol in symbols for row in fixture.records(symbol, probability)],
            price_path=price_path))
    state = fixture.snapshot(cash=0)
    all_symbols = [symbol for symbols in universes.values() for symbol in symbols]
    for key in ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure"):
        state[key] = dict.fromkeys(all_symbols, 0)
    state["quotes"] = {symbol: {"ask": 11} for symbol in all_symbols}
    state["held_shares"]["ABCL"] = 527
    state["gross_exposure"] = 5533.5
    state["symbol_exposure"]["ABCL"] = state["stock_market_value_by_symbol"]["ABCL"] = 5533.5
    state["ownership"]["active_allocations"] = [{"symbol": "ABCL", "horizon": "1w", "owned_shares": 527,
        "reserved_sell_shares": 0, "reserved_buy_shares": 0, "target_end": "2026-10-26T20:00:00Z"}]
    with monkeypatch.context() as legacy:
        legacy.setattr(ledger, "autonomous_horizon_sales", lambda *args: False)
        plan = fixture.compose(packages, state, expected_universes=universes)
    root = tmp_path / "datastore"
    run = root / "ml/joint-gameplan-runs" / plan["plan_sha256"]
    run.mkdir(parents=True)
    binding = {"schema_version": "accepted-joint-gameplan-v1", "action_date": fixture.DAY,
        "plan_sha256": plan["plan_sha256"], "run_path": run.relative_to(root).as_posix(),
        "accepted_at": fixture.NOW, "owner_packages": {p["owner_id"]: p["package_sha256"] for p in packages},
        "owner_universes": {p["owner_id"]: p["frozen_symbols"] for p in packages},
        "execution_symbols": all_symbols, "account_scope_sha256": fixture.SCOPE,
        "local_actor": "atlas", "executor_owner": "atlas", "orders_placed": 0, "activation_changed": False}
    pointer = root / f"ml/joint-gameplan-by-date/{fixture.DAY}/run.json"
    pointer.parent.mkdir(parents=True)
    for path, value in ((pointer, binding), (run / "accepted-plan.json", binding), (run / "joint-plan.json", plan)):
        path.write_bytes(_json_bytes(value))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_bytes(_json_bytes(state))
    paths, hashes = {}, {}
    for package in packages:
        path = tmp_path / (package["owner_id"] + ".json")
        path.write_bytes(_json_bytes(package))
        paths[package["owner_id"]] = path
        hashes[package["owner_id"]] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = {"action_date": fixture.DAY, "expected_plan_sha256": plan["plan_sha256"],
        "snapshot_path": snapshot_path, "snapshot_file_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "owner_package_paths": paths, "owner_package_file_sha256": hashes,
        "revised_at": "2026-10-19T22:15:00Z", "policy": fixture.POLICY}
    return root, run, pointer, plan, spec


def test_reprojects_cross_horizon_sale_preserving_original_artifacts(tmp_path, monkeypatch):
    root, original_run, pointer, original, spec = _fixture(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in [*original_run.iterdir(), spec["snapshot_path"], *spec["owner_package_paths"].values()]}
    revised_run = revise_accepted_gameplan(root, **spec)
    revised = json.loads((revised_run / "joint-plan.json").read_bytes())
    assert revised_run != original_run
    assert revised["as_of"] == original["as_of"]
    assert revised["account_snapshot_observed_at"] == original["account_snapshot_observed_at"]
    assert revised["policy_revision"]["revised_at"] == "2026-10-19T22:15:00+00:00"
    assert revised["policy_revision"]["ancestor_plan_sha256"] == [original["plan_sha256"]]
    assert "NOT_CURRENT_BROKER_STATE" in revised["policy_revision"]["account_basis"]
    assert revised["account_snapshot_sha256"] == original["account_snapshot_sha256"]
    assert revised["input_bindings"] == original["input_bindings"]
    old = {r["id"]: r for r in original["forecasts"]}
    for row in revised["forecasts"]:
        assert row["calibrated_probability"] == old[row["id"]]["calibrated_probability"]
        assert row["original_forecast_id"] == old[row["id"]]["original_forecast_id"]
        assert row["source_direction"] == old[row["id"]]["direction"]
        assert row["direction_policy_version"] == STOCK_DIRECTION_POLICY_VERSION
        assert row["direction_up_threshold"] == .5001
        assert row["direction_down_threshold"] == .4999
    selected = next(row for row in revised["forecasts"] if row["symbol"] == "ABCL" and row["calibrated_probability"] == .4872)
    previous = old[selected["id"]]
    assert previous["direction_based_action"] == "HOLD"
    assert selected["direction_based_action"] == "SELL" and selected["direction_based_trade_quantity"] < 0
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert json.loads(pointer.read_bytes())["plan_sha256"] == revised["plan_sha256"]
    assert revise_accepted_gameplan(root, **spec) == revised_run
    assert not (root / "controls").exists()
    assert not revised["live_orders_authorized"] and not revised["live_capital_reserved"]


def test_revision_relabels_old_narrow_edge_without_changing_probability(tmp_path, monkeypatch):
    root, _, _, _, spec = _fixture(tmp_path, monkeypatch, near_neutral=True)
    revised = json.loads((revise_accepted_gameplan(root, **spec) / "joint-plan.json").read_bytes())
    row = next(row for row in revised["forecasts"] if row["calibrated_probability"] == .50005)
    assert row["source_direction"] == "BULLISH"
    assert row["direction"] == "NO_EDGE"
    assert row["direction_based_action"] == "HOLD"


def test_revision_preserves_both_eleven_symbol_sources(tmp_path, monkeypatch):
    root, _, _, original, spec = _fixture(tmp_path, monkeypatch, full_universe=True)
    revised = json.loads((revise_accepted_gameplan(root, **spec) / "joint-plan.json").read_bytes())
    assert len(revised["forecasts"]) == 528
    assert len({row["symbol"] for row in revised["forecasts"]}) == 22
    assert revised["input_bindings"] == original["input_bindings"]
    assert {owner: len(source["frozen_symbols"]) for owner, source in revised["input_bindings"].items()} == {"atlas": 11, "scout": 11}


def test_repeated_policy_revision_carries_every_prior_plan_identity(tmp_path, monkeypatch):
    root, _, _, original, spec = _fixture(tmp_path, monkeypatch)
    first_run = revise_accepted_gameplan(root, **spec)
    first = json.loads((first_run / "joint-plan.json").read_bytes())
    second_spec = {**spec, "expected_plan_sha256": first["plan_sha256"], "revised_at": "2026-10-19T22:16:00Z"}
    second_run = revise_accepted_gameplan(root, **second_spec)
    second = json.loads((second_run / "joint-plan.json").read_bytes())
    assert second["policy_revision"]["ancestor_plan_sha256"] == [original["plan_sha256"], first["plan_sha256"]]
    assert revise_accepted_gameplan(root, **second_spec) == second_run


def test_revision_rejects_changed_snapshot_and_preserves_selection(tmp_path, monkeypatch):
    root, _, pointer, _, spec = _fixture(tmp_path, monkeypatch)
    before = pointer.read_bytes()
    value = json.loads(spec["snapshot_path"].read_bytes())
    value["available_cash"] = 999
    spec["snapshot_path"].write_bytes(_json_bytes(value))
    with pytest.raises(ValueError, match="file hash"):
        revise_accepted_gameplan(root, **spec)
    assert pointer.read_bytes() == before


def test_revision_compares_selection_again_under_adoption_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from ml import gameplan_policy_revision as module

    root, _, pointer, _, spec = _fixture(tmp_path, monkeypatch)
    changed = pointer.read_bytes() + b"\n"

    @contextmanager
    def concurrent_change(*args, **kwargs):
        pointer.write_bytes(changed)
        yield

    monkeypatch.setattr(module, "exclusive_runtime_lock", concurrent_change)
    with pytest.raises(ValueError, match="changed during"):
        revise_accepted_gameplan(root, **spec)
    assert pointer.read_bytes() == changed


def test_revision_rejects_wrong_original_account_basis(tmp_path, monkeypatch):
    root, _, pointer, _, spec = _fixture(tmp_path, monkeypatch)
    before = pointer.read_bytes()
    value = json.loads(spec["snapshot_path"].read_bytes())
    value["available_cash"] = 999
    spec["snapshot_path"].write_bytes(_json_bytes(value))
    spec["snapshot_file_sha256"] = hashlib.sha256(spec["snapshot_path"].read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="original saved observation"):
        revise_accepted_gameplan(root, **spec)
    assert pointer.read_bytes() == before
