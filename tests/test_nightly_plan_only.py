"""Two complete Gameplans finish the nightly handoff without a third input."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pandas as pd
import pytest

from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum
from ml.gameplan_stats_handoff import export_stats_package
from ml.joint_capital_adoption import read_accepted_joint_plan
from ml.joint_capital_plan import build_owner_package, content_sha256, publish_owner_package, PLAN_ONLY_MODE
from tests.gameplan_stats_fixture import forecast, write_review
from tests.test_joint_capital_plan import DAY, NOW, SCOPE, records, prices, snapshot, package
from tests.test_nightly_exchange import exchange, files, wake
from tools import nightly_exchange as module


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("No network in Gameplan tests"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("No network in Gameplan tests"))


@pytest.fixture
def source_inputs(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    owners = {}
    universes = {"atlas": ["AAPL", "MSFT", "AMZN", "GOOG", "META", "NVDA", "TSLA", "AMD", "INTC", "NFLX", "IBM"],
                 "scout": ["DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN"]}
    basis = snapshot(cash=1000)
    basis["observed_at"] = "2026-09-08T18:00:00Z"
    basis["account_equity"] = 100000
    for name in ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure"):
        basis[name] = dict.fromkeys(sum(universes.values(), []), 0)
    basis["quotes"] = {symbol: {"ask": 11} for symbol in sum(universes.values(), [])}
    for actor, symbols in universes.items():
        rows = [{**row, "id": symbol + ":" + row["id"]} for symbol in symbols for row in records(symbol)]
        path = prices(symbols[0])
        path["points"] = {key: value for symbol in symbols for key, value in prices(symbol)["points"].items()}
        package = build_owner_package(owner_id=actor, run_id="run-" + actor, source_revision="b" * 40,
            action_date=DAY, frozen_symbols=symbols, created_at=NOW,
            source_hashes={name: content_sha256([actor, name]) for name in
                ("receipt_sha256", "manifest_sha256", "forecasts_sha256", "price_path_sha256")},
            forecasts=rows, price_path=path, account_basis=basis if actor == "atlas" else None)
        plan = publish_owner_package(sources, package)
        stats_root = tmp_path / ("stats-" + actor)
        write_review(stats_root, [forecast(symbol, session="2026-09-08") for symbol in symbols], session="2026-09-08")
        stats = export_stats_package(stats_root, producer=actor.title(), symbols=symbols,
                                     destination=sources / (actor + "-stats.json"))
        owners[actor] = {"symbols": symbols,
            "plan_package": {"root": str(sources), "path": str(plan), "file_sha256": file_checksum(plan),
                             "package_sha256": package["package_sha256"]},
            "stats_package": {"path": str(stats), "file_sha256": file_checksum(stats)}}
    return {"owners": owners}


def forbid_third_inputs(monkeypatch):
    from tools import nightly_account_snapshot
    def forbidden(*args, **kwargs):
        pytest.fail("Two prepared Gameplans must not require account capture or cross-trader verification")
    for name in ("_own_snapshot", "_verify_synthesis_freshness", "_ownership_observations", "_ensure_ownership_responder"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(nightly_account_snapshot, "capture_snapshot", forbidden)


def deliver(exchange):
    assert wake(exchange, "scout")["reason"] == "PREPARATION_ATLAS"
    assert wake(exchange, "atlas")["reason"] == "JOINT_SCOUT"
    return wake(exchange, "scout", now="2026-09-09T20:00:00Z")


def test_every_night_scout_delivery_finishes_before_any_atlas_acceptance(exchange, monkeypatch):
    forbid_third_inputs(monkeypatch)
    result = deliver(exchange)
    assert result["status"] == "COMPLETE" and result["delivered"]
    assert result["delivery_mode"] == "TWO_PLAN_GAMEPLAN" and not result["peer_verified"]
    assert "accepted_packet_sha256" not in result
    assert exchange[3] == []
    config = exchange[0]["scout"]
    session = Path(config["state_root"]) / "sessions" / DAY
    selection = json.loads((session / "synthesis-selection.json").read_text())
    assert set(selection["inputs"]) == {"atlas_preparation", "scout_preparation"}
    assert "snapshot" not in selection["spec"]
    joint = module._receive(config, DAY, "2026-09-08", "scout", "joint")
    assert set(joint["files"]) == {"joint-plan.json", "scout-receipt.json", "atlas-plan.json",
                                  "atlas-stats.json", "scout-plan.json", "scout-stats.json"}
    assert not module._folder(config, DAY, "atlas", "accepted").exists()
    assert not module._folder(config, DAY, "atlas", "snapshot").exists()
    shared = Path(config["exchange_root"])
    shared.rename(shared.with_name("offline"))
    before = files(Path(config["state_root"]))
    assert wake(exchange, "scout", now="2026-09-09T23:30:00Z") == result
    assert files(Path(config["state_root"])) == before


def test_atlas_reads_all_22_symbols_original_forecasts_prices_and_stats(exchange, monkeypatch):
    forbid_third_inputs(monkeypatch)
    delivered = deliver(exchange)
    received = wake(exchange, "atlas", now="2026-09-09T20:05:00Z")
    assert received["status"] == "COMPLETE"
    for actor in ("atlas", "scout"):
        root = Path(exchange[1][actor]["datastore"])
        plan, binding, run = read_accepted_joint_plan(root, DAY)
        assert plan["composition_mode"] == "MERGED_GAMEPLAN_WITH_SAVED_BALANCES"
        assert len(plan["forecasts"]) == 528 and len(load_gameplan(root).symbols) == 22
        assert len(load_gameplan_stats(root).symbols) == 22
        assert plan["account_snapshot_observed_at"] == "2026-09-08T18:00:00Z"
        assert plan["account_basis_age_seconds"] == 26 * 3600
        assert plan["ledger"]["summary"]["starting_cash"] == 1000
        assert plan["ledger"]["summary"]["ending_cash_low"] >= 0
        assert plan["ledger"]["events"]
        for owner in ("atlas", "scout"):
            packet = module._receive(exchange[0][actor], DAY, "2026-09-08", owner, "preparation")
            original = json.loads(packet["files"]["plan.json"].read_text())
            output = {row["original_forecast_id"]: row for row in plan["forecasts"] if row["owner_id"] == owner}
            assert plan["source_price_paths"][owner] == original["price_path"]
            assert binding["owner_packages"][owner] == original["package_sha256"]
            for row in original["forecasts"]:
                assert all(output[row["id"]][key] == value for key, value in row.items() if key != "id")
                assert "direction_based_trade_quantity" in output[row["id"]]
    assert wake(exchange, "scout", now="2026-09-09T20:10:00Z") == delivered
    assert wake(exchange, "atlas", now="2026-09-09T20:10:00Z") == received


def test_missing_balance_basis_keeps_intents_without_invented_quantities(tmp_path, monkeypatch):
    from ml.joint_capital_plan import compose_joint_plan
    from ml.joint_capital_adoption import accept_joint_plan
    packages = [package("atlas", "AAPL"), package("scout", "ABCL")]
    universes = {row["owner_id"]: row["frozen_symbols"] for row in packages}
    hashes = {row["owner_id"]: row["package_sha256"] for row in packages}
    plan = compose_joint_plan(packages, action_date=DAY, expected_universes=universes,
        expected_package_sha256=hashes, account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    root = tmp_path / "instructions-only"
    accept_joint_plan(root, plan, expected_sha256=plan["plan_sha256"], expected_package_sha256=hashes,
        expected_universes=universes, account_scope_sha256=SCOPE, local_actor="atlas", executor_owner="atlas",
        action_date=DAY, accepted_at=NOW)
    from ml.stock_trader import gameplan_execution, catchup
    from ml.stock_trader.independent_runtime import _loaded_fallback_policy
    monkeypatch.setattr(gameplan_execution, "_assert_execution_deployment", lambda *a, **kw: None)
    from ml.account_gameplan import config
    monkeypatch.setattr(config, "load_account_config", lambda *a: None)
    monkeypatch.setattr(catchup, "catchup_signals", lambda *a, **kw: pytest.fail("No invented projected ledger"))
    signals, sources = gameplan_execution.load_execution_signals(root, as_of="2026-09-09T11:05:00Z")
    buys = [signal for signal in signals.values() if signal.calibrated_probability > .5]
    assert len(buys) == 2
    assert all(signal.planned_quantity is None for signal in buys)
    assert {path.name for path in sources} == {"accepted-plan.json", "joint-plan.json"}
    assert _loaded_fallback_policy(root, sources[0].parent, DAY) == (None, None)


def test_missing_second_plan_remains_only_preparation_wait(exchange, monkeypatch):
    forbid_third_inputs(monkeypatch)
    result = wake(exchange, "atlas", capture=True)
    assert result["status"] == "PENDING" and result["reason"] == "PREPARATION_SCOUT"
    assert exchange[3] == []


def test_atlas_embeds_latest_native_balances_without_intraday_verification(exchange, monkeypatch):
    forbid_third_inputs(monkeypatch)
    output = exchange[2]["atlas"]["steps"]["local_handoff"]["output"]
    path = Path(output["package"])
    plan = json.loads(path.read_text())
    basis = plan.pop("account_basis")
    # Its saved broker arithmetic is usable even when an unrelated 15m ledger
    # advertises unavailable ownership. No such flag determines trade intent.
    basis["observed_at"] = "2026-09-09T09:30:00Z"
    basis["ownership"] = {"safe_for_planning": False, "blocked_symbols": ["AAPL"],
        "active_allocations": [{"symbol": "AAPL", "horizon": "15m", "owned_shares": -999,
                                "stream": "scout-15m"}]}
    plan["package_sha256"] = content_sha256({k: v for k, v in plan.items() if k != "package_sha256"})
    path.write_text(json.dumps(plan))
    output["files"][str(path)] = file_checksum(path)
    native = Path(exchange[1]["atlas"]["datastore"]) / "ml/account-gameplan-runs/prepared/account-snapshot.json"
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps(basis))
    result = deliver(exchange)
    assert result["status"] == "COMPLETE"
    exported = module._receive(exchange[0]["scout"], DAY, "2026-09-08", "atlas", "preparation")
    carried = json.loads(exported["files"]["plan.json"].read_text())["account_basis"]
    assert carried["observed_at"] == basis["observed_at"] and carried["available_cash"] == basis["available_cash"]
    assert carried["ownership"] == {"active_allocations": []}
    combined = read_accepted_joint_plan(Path(exchange[1]["scout"]["datastore"]), DAY)[0]
    assert combined["ledger"]["summary"]["starting_cash"] == 1000
    assert combined["ledger"]["events"]
    assert wake(exchange, "atlas", now="2026-09-09T20:05:00Z")["status"] == "COMPLETE"


def test_legacy_preparations_reuse_saved_snapshot_without_refresh(exchange, monkeypatch):
    forbid_third_inputs(monkeypatch)
    output = exchange[2]["atlas"]["steps"]["local_handoff"]["output"]
    path = Path(output["package"])
    plan = json.loads(path.read_text())
    basis = plan.pop("account_basis")
    plan["package_sha256"] = content_sha256({k: v for k, v in plan.items() if k != "package_sha256"})
    path.write_text(json.dumps(plan))
    output["files"][str(path)] = file_checksum(path)
    assert wake(exchange, "scout")["reason"] == "PREPARATION_ATLAS"
    assert wake(exchange, "atlas")["reason"] == "JOINT_SCOUT"
    module._publish(exchange[0]["atlas"], DAY, "2026-09-08", "snapshot", {"snapshot.json": module._bytes(basis)})
    completed = wake(exchange, "scout", now="2026-09-09T20:00:00Z")
    assert completed["status"] == "COMPLETE"
    combined = read_accepted_joint_plan(Path(exchange[1]["scout"]["datastore"]), DAY)[0]
    assert combined["account_snapshot_observed_at"] == basis["observed_at"]
    assert combined["ledger"]["summary"]["starting_cash"] == 1000
    assert combined["composition_mode"] == "MERGED_GAMEPLAN_WITH_SAVED_BALANCES"
