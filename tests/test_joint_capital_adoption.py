"""Frozen combined-plan adoption is local and never grants Scout order authority."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from test_joint_capital_plan import compose, DAY, NOW, SCOPE
from ml.joint_capital_adoption import accept_joint_plan, read_accepted_joint_plan, assert_accepted_execution
from ml.stock_trader.contracts import execution_symbols, STOCK_RESEARCH_SYMBOLS
from ml.stock_trader.gameplan_execution import load_execution_signals, execution_preflight
from app.ui.gameplan_data import load_gameplan, plan_sessions
from test_independent_stock_runtime import environment


def adopt(root, plan=None, actor="atlas"):
    plan = plan or compose()
    return accept_joint_plan(root, plan, expected_sha256=plan["plan_sha256"],
        expected_package_sha256={owner: value["package_sha256"] for owner, value in plan["input_bindings"].items()},
        expected_universes={owner: value["frozen_symbols"] for owner, value in plan["input_bindings"].items()},
        account_scope_sha256=SCOPE, local_actor=actor, executor_owner="atlas", action_date=DAY, accepted_at=NOW)


def test_adoption_cli_requires_local_profile():
    from ml.joint_capital_adoption import main
    with pytest.raises(SystemExit) as error:
        main(["--spec", "unused.json"])
    assert error.value.code == 2


@pytest.mark.parametrize("mismatch", ["actor", "symbols", "checkout", "duplicate_symbols"])
def test_adoption_cli_rejects_profile_mismatch_before_reading_plan(tmp_path, monkeypatch, mismatch):
    from ml import joint_capital_adoption as module
    spec = {"local_actor": "scout", "expected_universes": {"scout": ["ABCL"], "atlas": ["AAPL"]}}
    profile = {"actor": "Scout", "symbols": ["ABCL"], "checkout": str(Path(module.__file__).resolve().parents[1])}
    if mismatch == "actor":
        spec["local_actor"] = "atlas"
    elif mismatch == "symbols":
        spec["expected_universes"]["scout"] = ["DOCU"]
    elif mismatch == "checkout":
        profile["checkout"] = str(tmp_path / "other-checkout")
    else:
        profile["symbols"] = ["ABCL", "ABCL"]
    specification, local_profile = tmp_path / "spec.json", tmp_path / "profile.json"
    specification.write_text(json.dumps(spec))
    local_profile.write_text(json.dumps(profile))
    monkeypatch.setattr(module, "load_joint_plan", lambda *args, **kwargs: pytest.fail("Plan read before PC binding"))
    monkeypatch.setattr(module, "accept_joint_plan", lambda *args, **kwargs: pytest.fail("Adopted a mismatched PC binding"))
    with pytest.raises(ValueError, match="local profile|different checkout"):
        module.main(["--spec", str(specification), "--local-profile", str(local_profile)])


def test_adoption_cli_preserves_profile_bound_scout_role(tmp_path, monkeypatch):
    from ml import joint_capital_adoption as module
    spec = {"local_actor": "scout", "executor_owner": "atlas", "expected_universes": {"scout": ["ABCL"], "atlas": ["AAPL"]},
            "plan_root": str(tmp_path / "source"), "plan_path": str(tmp_path / "source/plan.json"),
            "datastore_root": str(tmp_path / "datastore"), "expected_sha256": "a" * 64}
    profile = {"actor": "Scout", "symbols": ["ABCL"], "checkout": str(Path(module.__file__).resolve().parents[1])}
    specification, local_profile = tmp_path / "spec.json", tmp_path / "profile.json"
    specification.write_text(json.dumps(spec))
    local_profile.write_text(json.dumps(profile))
    plan, observed = {"synthetic": True}, {}
    monkeypatch.setattr(module, "load_joint_plan", lambda *args, **kwargs: plan)
    def capture(root, selected, **kwargs):
        observed.update(root=root, plan=selected, **kwargs)
        return tmp_path / "synthetic-run"
    monkeypatch.setattr(module, "accept_joint_plan", capture)
    assert module.main(["--spec", str(specification), "--local-profile", str(local_profile)]) == 0
    assert observed["local_actor"] == "scout" and observed["executor_owner"] == "atlas"
    assert observed["expected_universes"]["scout"] == profile["symbols"]
    assert not (tmp_path / "datastore").exists()


def test_scout_can_display_both_universes_without_executing(tmp_path):
    research = tuple(STOCK_RESEARCH_SYMBOLS)
    run = adopt(tmp_path, actor="scout")
    ui = load_gameplan(tmp_path)
    assert ui.run_directory == run
    assert set(ui.symbols) == {"AAPL", "ABCL"}
    assert ui.actions[0].quantity == 1
    assert plan_sessions(tmp_path) == (DAY,)
    assert set(execution_symbols(tmp_path, DAY)) == {"AAPL", "ABCL"}
    assert STOCK_RESEARCH_SYMBOLS == research
    assert execution_preflight(tmp_path, action_date=pd.Timestamp(DAY).date())["status"] == "NOT_READY"
    with pytest.raises(ValueError, match="not its execution owner"):
        load_execution_signals(tmp_path, as_of=f"{DAY}T12:15:00Z")
    assert not (tmp_path / "controls").exists()


def test_full_twenty_two_symbol_handoff_keeps_research_membership_local(tmp_path):
    from test_joint_capital_plan import package, records, prices, snapshot
    from ml.joint_capital_plan import build_owner_package, compose_joint_plan

    universes = {"atlas": ["AAPL", "AMZN", "SNDK", "MU", "NVDA", "GOOG", "COST", "CROX", "PATH", "IONQ", "TWST"],
        "scout": ["DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN"]}
    packages = []
    for owner, symbols in universes.items():
        sample = package(owner, symbols[0])
        price_path = prices(symbols[0])
        price_path["points"] = {key: value for symbol in symbols for key, value in prices(symbol)["points"].items()}
        packages.append(build_owner_package(owner_id=owner, run_id=sample["run_id"], source_revision="b" * 40,
            action_date=DAY, frozen_symbols=symbols, created_at=NOW, source_hashes=sample["source_hashes"],
            forecasts=[{**row, "id": symbol + ":" + row["id"]} for symbol in symbols for row in records(symbol)],
            price_path=price_path))
    state = snapshot(cash=1000)
    symbols = [symbol for group in universes.values() for symbol in group]
    for key in ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure"):
        state[key] = dict.fromkeys(symbols, 0)
    state["quotes"] = {symbol: {"ask": 11} for symbol in symbols}
    plan = compose_joint_plan(packages, state, action_date=DAY, expected_universes=universes,
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in packages},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    adopt(tmp_path, plan, actor="scout")
    assert len(load_gameplan(tmp_path).symbols) == len(execution_symbols(tmp_path, DAY)) == 22
    assert len(STOCK_RESEARCH_SYMBOLS) == 11


def test_atlas_reads_accepted_quantities_and_overdue_same_session(tmp_path):
    run = adopt(tmp_path)
    signals, sources = load_execution_signals(tmp_path, as_of=f"{DAY}T12:15:00Z")
    assert set(signals) == {("AAPL", "1h")}
    signal = signals["AAPL", "1h"]
    assert signal.planned_quantity == 1
    assert pd.Timestamp(signal.actionable_until) > pd.Timestamp(f"{DAY}T12:15:00Z")
    assert run / "accepted-plan.json" in sources
    assert assert_accepted_execution(tmp_path, run, action_date=DAY, account_scope_sha256=SCOPE)
    with pytest.raises(ValueError, match="account fingerprints"):
        assert_accepted_execution(tmp_path, run, action_date=DAY, account_scope_sha256="c" * 64)


def test_session_binding_digest_and_immutable_selection(tmp_path):
    plan = compose()
    run = adopt(tmp_path, plan)
    assert adopt(tmp_path, plan) == run
    assert read_accepted_joint_plan(tmp_path, "2026-09-10") is None
    changed = deepcopy(plan)
    changed["action_date"] = "2026-09-10"
    with pytest.raises(ValueError, match="digest"):
        adopt(tmp_path / "changed", changed)
    pointer = tmp_path / f"ml/joint-gameplan-by-date/{DAY}/run.json"
    value = json.loads(pointer.read_text())
    value["local_actor"] = "scout"
    pointer.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="receipt disagree"):
        read_accepted_joint_plan(tmp_path, DAY)


def test_native_completed_direction_ledger_is_used_for_late_catchup(tmp_path, monkeypatch):
    from gameplan_fixture import write_plan
    from ml.artifacts import file_checksum, write_manifest
    from ml.stock_trader.catchup import native_catchup_plan
    from ml.stock_trader import gameplan_execution

    plan = compose()
    native = tmp_path / "ml/nightly-gameplan-runs/native"
    native.mkdir(parents=True)
    (native / "receipt.json").write_text(json.dumps({"action_date": DAY}))
    pd.DataFrame(plan["forecasts"]).to_parquet(native / "forecasts.parquet")
    latest = tmp_path / "ml/nightly-gameplan-latest/run.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"current": {"run_path": native.relative_to(tmp_path).as_posix()}}))
    source_hash = file_checksum(native / "receipt.json")
    run = write_plan(tmp_path, session=DAY, rows=plan["forecasts"], ledger=plan["ledger"],
                     report_updates={"source_gameplan_run": native.relative_to(tmp_path).as_posix()})
    for name in ("report.json", "receipt.json"):
        target = run / name
        value = json.loads(target.read_text())
        value["source_receipt_sha256"] = source_hash
        target.write_text(json.dumps(value))
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["configuration"]["source_receipt_sha256"] = source_hash
    write_manifest(run, run_timestamp=manifest["run_timestamp"], input_files=[],
        output_files=["trade-plan.parquet", "direction-ledger.json", "report.json", "Gameplan.md"],
        configuration=manifest["configuration"])
    receipt = json.loads((run / "receipt.json").read_text())
    receipt["manifest_sha256"] = file_checksum(run / "manifest.json")
    (run / "receipt.json").write_text(json.dumps(receipt))
    pointer = tmp_path / "ml/gameplan-trade-plan-latest/run.json"
    value = json.loads(pointer.read_text())
    value["current"].update(source_receipt_sha256=source_hash, receipt_sha256=file_checksum(run / "receipt.json"))
    pointer.write_text(json.dumps(value))
    monkeypatch.setattr(gameplan_execution, "STOCK_TRADER_SYMBOLS", ("AAPL", "ABCL"))
    signals, sources = load_execution_signals(tmp_path, as_of=f"{DAY}T12:15:00Z")
    assert signals["AAPL", "1h"].planned_quantity == 1
    assert run / "receipt.json" in sources
    assert native_catchup_plan(tmp_path, action_date=DAY, source_run=native)[1] == native
    (native / "receipt.json").write_text('{"changed":true}')
    with pytest.raises(ValueError, match="forecast source"):
        load_execution_signals(tmp_path, as_of=f"{DAY}T12:15:00Z")


def test_owner_package_retry_accepts_only_identical_bytes(tmp_path):
    from test_joint_capital_plan import package
    from ml.joint_capital_plan import publish_owner_package
    payload = package()
    path = publish_owner_package(tmp_path, payload)
    assert publish_owner_package(tmp_path, payload) == path
    path.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="immutable bytes"):
        publish_owner_package(tmp_path, payload)


@pytest.mark.parametrize("later_opposite", [False, True])
def test_native_runtime_executes_overdue_combined_quantity_once_with_mock_broker(environment, monkeypatch, later_opposite):
    from ml.stock_trader import independent_runtime as runtime
    from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals
    from ml.stock_trader.contracts import PortfolioState, QuoteState
    from ml.stock_trader.horizon_ledger import OrderEvidence, FillEvidence
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    from test_independent_stock_runtime import ACCOUNT, BROKER_ID

    env = environment
    combined = None
    if later_opposite:
        from test_joint_capital_plan import records, package
        rows = records("AAPL", .7)
        later = next(row for row in rows if row["route"] == "1h@05:00")
        later.update(calibrated_probability=.4, direction="BEARISH")
        combined = compose([package(), package("atlas", "AAPL", rows=rows)])
    adopt(env.root, combined)
    env.now = pd.Timestamp(f"{DAY}T{'11' if later_opposite else '12'}:15:00Z")
    env.held = {"AAPL": 0, "ABCL": 0}
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", load_current_independent_gameplan_signals)
    observed_universes = []

    def capture(*args, **kwargs):
        observed_universes.append(kwargs["symbols"])
        return PortfolioState(env.now.isoformat(), 100_000, 100_000, sum(env.held.values()) * 100,
            0, dict(env.held), {s: q * 100 for s, q in env.held.items()}, {}, {}, 0,
            {s: QuoteState(s, 100, 100, 100, 100, 1000, env.now.isoformat()) for s in env.held},
            "snapshot-" + env.now.isoformat(), BROKER_ID)

    def fill(broker, ledger, **kwargs):
        evidence = []
        for reservation in ledger.snapshot().reservations:
            if reservation.status == "SUBMITTED":
                env.held[reservation.symbol] += reservation.quantity * (1 if reservation.side == "BUY" else -1)
                evidence.append(OrderEvidence("e-" + reservation.reservation_id, reservation.reservation_id,
                    ACCOUNT, env.now.isoformat(), reservation.broker_order_id, "FILLED", reservation.quantity,
                    reservation.quantity, 0, (FillEvidence("f-" + reservation.reservation_id,
                        reservation.quantity, 100, env.now.isoformat()),)))
        return tuple(evidence)

    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence", fill)
    options = dict(execute=True, session=env.broker, runtime_clock=lambda: env.now,
                   session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY)
    result = runtime.run_independent_stock_trader_once(env.root, **options)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == 1
    leg = env.broker.submissions[0]["orderLegCollection"][0]
    assert leg["quantity"] == 1 and leg["instrument"]["symbol"] == "AAPL"
    env.now = env.now + (pd.Timedelta(hours=1) if later_opposite else pd.Timedelta(seconds=30))
    repeated = runtime.run_independent_stock_trader_once(env.root, **options)
    assert not repeated.error, repeated.error
    assert repeated.submitted_orders == int(later_opposite) and len(env.broker.submissions) == 1 + int(later_opposite)
    if later_opposite:
        assert env.broker.submissions[-1]["orderLegCollection"][0]["instruction"] == "SELL"
        env.now += pd.Timedelta(seconds=30)
        final = runtime.run_independent_stock_trader_once(env.root, **options)
        assert final.submitted_orders == 0 and len(env.broker.submissions) == 2
    assert all(set(symbols) == {"AAPL", "ABCL"} for symbols in observed_universes)
