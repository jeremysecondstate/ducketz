"""Atlas acceptance and late-start bridge; only temporary files and fake brokers."""
from dataclasses import replace
import hashlib
import json

import pandas as pd
import pytest

from ml.account_gameplan import config, execution
from ml.stock_trader import gameplan_execution, independent_runtime as runtime
from ml.stock_trader.contracts import QuoteState
from ml.stock_trader.horizon_ledger import OrderEvidence, FillEvidence
from test_account_gameplan_execution import account_env, run, state, native
from test_independent_stock_runtime import environment
from test_joint_capital_adoption import adopt
from test_joint_capital_plan import package, compose, records, prices, snapshot, DAY, NOW, SCOPE

_REAL_ACCOUNT_PLAN = gameplan_execution.account_execution_plan


def accepted(root, *, twenty_two=False):
    from ml.joint_capital_plan import build_owner_package
    universes = ({"atlas": ["XA" + chr(65+i) for i in range(11)],
                  "scout": ["XB" + chr(65+i) for i in range(11)]}
                 if twenty_two else {"atlas": ["AAPL"], "scout": ["ABCL"]})
    packages = []
    for owner, symbols in universes.items():
        sample = package(owner, symbols[0])
        raw = json.dumps({"fixture": owner}).encode()
        source = root / "ml/nightly-gameplan-runs" / sample["run_id"]
        source.mkdir(parents=True)
        (source / "receipt.json").write_bytes(raw)
        hashes = {**sample["source_hashes"], "receipt_sha256": hashlib.sha256(raw).hexdigest()}
        path = prices(symbols[0])
        path["points"] = {key: value for symbol in symbols for key,value in prices(symbol)["points"].items()}
        packages.append(build_owner_package(owner_id=owner, run_id=sample["run_id"], source_revision="b"*40,
            action_date=DAY, frozen_symbols=symbols, created_at=NOW, source_hashes=hashes,
            forecasts=[{**row,"id":symbol+":"+row["id"]} for symbol in symbols for row in records(symbol,.7)],
            price_path=path))
    money = snapshot(cash=1000 if twenty_two else 100)
    if twenty_two:
        money["account_equity"] = 2000
    symbols = [symbol for values in universes.values() for symbol in values]
    for key in ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure"):
        money[key] = dict.fromkeys(symbols,0)
    money["quotes"] = {symbol:{"ask":11} for symbol in symbols}
    joint = compose(packages, money, expected_universes=universes)
    target = adopt(root,joint)
    return joint,target,universes


def bind(root, universes, *, status="ACTIVE", account=SCOPE):
    value={"schema_version":config.VERSION,"machine_id":"pc-original","coordinator_id":"pc-original",
        "participants":{"pc-original":universes["atlas"],"pc-new":universes["scout"]},
        "account_fingerprint":account}
    fingerprint=config.digest(value)
    receipt={"schema_version":config.VERSION,"status":"VERIFIED","binding_sha256":fingerprint,
        "machine_id":"pc-original","coordinator_id":"pc-original","peer_execution_fenced":True,
        "peer_fence_receipt_sha256":"f"*64,"migration_manifest_sha256":"b"*64,
        "fresh_union_reconciliation_sha256":"c"*64,"installed_source_commit":"d"*40,"orders_placed":0}
    destination=root/"state/account-gameplan/cutovers/fixture.json"
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(receipt))
    value["activation"]={"status":status,"binding_sha256":fingerprint,
        "receipt_path":destination.relative_to(root).as_posix(),
        "receipt_sha256":hashlib.sha256(destination.read_bytes()).hexdigest()}
    (root/config.CONFIG).write_text(json.dumps(value))
    return config.load_account_config(root)


def test_accepted_union_retains_real_private_cutover_and_local_source_checks(tmp_path,monkeypatch):
    joint,target,universes=accepted(tmp_path,twenty_two=True)
    bind(tmp_path,universes)
    guards=[]
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan",lambda *a,**k:guards.append(a[1]))
    plan,private=gameplan_execution.account_execution_plan(tmp_path,action_date=DAY)
    assert plan.path==target and len(private.symbols)==22 and len(plan.rows)==22*24
    assert set(plan.rows.producer_id)=={"pc-original","pc-new"}
    assert plan.report["joint_composition"]==joint
    assert guards==[tmp_path/"ml/nightly-gameplan-runs/run-atlas"]
    signals,_=gameplan_execution.load_execution_signals(tmp_path,as_of=DAY+"T12:15:00Z")
    assert signals and all(s.prediction_id.startswith("catchup:") for s in signals.values())
    (tmp_path/"ml/nightly-gameplan-runs/run-atlas/receipt.json").write_text("changed")
    with pytest.raises(ValueError,match="LOCAL_FROZEN_SOURCE_CHANGED"):
        gameplan_execution.account_execution_plan(tmp_path,action_date=DAY)


@pytest.mark.parametrize("failure",["preparing","cutover","account","symbols","dual_selection"])
def test_accepted_joint_cannot_bypass_private_account_gate(tmp_path,monkeypatch,failure):
    _,target,universes=accepted(tmp_path)
    if failure=="symbols":universes={**universes,"scout":["MU"]}
    bind(tmp_path,universes,status="PREPARING" if failure=="preparing" else "ACTIVE",
         account="c"*64 if failure=="account" else SCOPE)
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan",lambda *a,**k:None)
    if failure=="cutover":(tmp_path/"state/account-gameplan/cutovers/fixture.json").write_text("{}")
    if failure=="dual_selection":
        legacy=tmp_path/"ml/account-gameplan-by-date"/DAY/"run.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("{}")
    with pytest.raises((ValueError,OSError)):
        gameplan_execution.load_execution_signals(tmp_path,as_of=DAY+"T12:15:00Z")
    assert not (tmp_path/execution.AUTHORITY_PATH).exists()


@pytest.fixture
def accepted_env(account_env,monkeypatch):
    env=account_env
    joint,target,universes=accepted(env.root,twenty_two=True)
    env.config=replace(env.config,participants={"pc-original":tuple(sorted(universes["atlas"])),
        "pc-new":tuple(sorted(universes["scout"]))})
    env.now=pd.Timestamp(DAY+"T12:15:00Z")
    env.held.update(dict.fromkeys(env.config.symbols,0.))
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan",lambda *a,**k:None)
    monkeypatch.setattr(gameplan_execution,"account_execution_plan",_REAL_ACCOUNT_PLAN)
    env.plan,_=_REAL_ACCOUNT_PLAN(env.root,action_date=DAY)
    monkeypatch.setattr(runtime,"load_current_independent_gameplan_signals",
        lambda root,**kwargs:gameplan_execution.load_execution_signals(root,as_of=kwargs["as_of"]))
    env.portfolio_transform=lambda p:replace(p,held_shares={s:env.held[s] for s in env.config.symbols},
        symbol_exposure={s:env.held[s]*100 for s in env.config.symbols},
        quotes={s:QuoteState(s,100.,100.,100.,100.,1000.,env.now.isoformat()) for s in env.config.symbols})
    return env


def test_accepted_catchup_uses_existing_account_authority_for_twenty_two_symbols(accepted_env):
    env=accepted_env
    result=run(env)
    assert result.status=="ORDERS_SUBMITTED",result.error
    assert result.submitted_orders>0
    reservations=state(env)["reservations"]
    assert {r["request"]["participant_id"] for r in reservations}=={"pc-original","pc-new"}
    assert all(r["request"]["forecast_id"].startswith("catchup:") for r in reservations)
    assert sum(r["request"]["quantity"] for r in reservations)==sum(
        event["quantity"] for event in env.plan.ledger["events"])


def test_partial_cancel_catchup_residual_reconciles_native_and_account_ledgers(accepted_env):
    env=accepted_env
    assert run(env).submitted_orders>0
    prior=native(env).snapshot().reservations
    env.now+=pd.Timedelta(seconds=1)
    env.order_evidence=tuple(OrderEvidence("cancel-"+r.reservation_id,r.reservation_id,SCOPE,
        env.now.isoformat(),r.broker_order_id,"CANCELLED",r.quantity,1,0,
        (FillEvidence("fill-"+r.reservation_id,1,r.limit_price,env.now.isoformat()),)) for r in prior)
    for reservation in prior:
        env.held[reservation.symbol] += 1
    result=run(env)
    assert result.submitted_orders==len(prior),result.error
    saved=state(env)["reservations"]
    assert sum(r["status"]=="CANCELLED" for r in saved)==len(prior)
    assert sum(r["status"]=="SUBMITTED" for r in saved)==len(prior)
    assert all(r["filled_quantity"]==1 for r in saved if r["status"]=="CANCELLED")
    assert sum(r["request"]["quantity"] for r in saved if r["status"]=="SUBMITTED")==sum(r.quantity-1 for r in prior)


def test_changed_reconciled_intention_cannot_gain_account_cash(accepted_env,monkeypatch):
    env=accepted_env
    original=execution.AccountExecutionContext._catchup_participant
    def changed(self,decision,native):
        prediction={**decision.prediction,"planned_quantity":decision.prediction["planned_quantity"]+1}
        return original(self,replace(decision,prediction=prediction),native)
    monkeypatch.setattr(execution.AccountExecutionContext,"_catchup_participant",changed)
    result=run(env)
    assert result.status=="SUBMISSION_STOPPED_SAFETY_CHECK",result.error
    assert "RECONCILED_ACCOUNT_INTENTION" in result.error
    assert env.broker.submissions==[]
