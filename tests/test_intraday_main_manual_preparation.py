"""Canonical manual preparation exercises public diagnostics with fixture state."""
import json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]

def binding(tmp_path, actor="Atlas"):
    from ml.intraday.local_execution import CONFIG_PATH, VERSION
    machine={"Atlas":"pc-original","Scout":"pc-new"}[actor]
    symbols=["AAPL","AMZN","GOOG","MU","NVDA","SNDK","COST","CROX","PATH","TWST","IONQ"]
    profile=tmp_path/"profile.json";watchlist=tmp_path/"watchlist.txt";data=tmp_path/"data"
    watchlist.write_text("\n".join(symbols)+"\n")
    profile.write_text(json.dumps(dict(actor=actor,machine=machine,symbols=symbols,symbol_profile_path=str(watchlist))))
    own=data/CONFIG_PATH;own.parent.mkdir(parents=True)
    own.write_text(json.dumps(dict(schema_version=VERSION,actor=actor,machine=machine,symbols=symbols,account_fingerprint="a"*64,manual_start_only=True,legacy_history_policy="PRESERVE_ORIGINALS_NEW_BROKER_CAPACITY_EPOCH")))
    return dict(datastore=data,profile=profile,watchlist=watchlist)

def inventory(path):
    return {p.relative_to(path).as_posix():p.read_bytes() for p in path.rglob("*") if p.is_file()}

@pytest.mark.parametrize("actor",["Atlas","Scout"])
def test_real_nightly_cli_uses_only_existing_own_binding(tmp_path,capsys,actor):
    from tools.prepare_independent_intraday import main
    args=binding(tmp_path,actor)
    legacy=args["datastore"]/"state/account-gameplan/authority.sqlite3"
    legacy.parent.mkdir(parents=True);legacy.write_bytes(b"unreadable foreign legacy")
    before=inventory(tmp_path)
    command=["--nightly"]
    for key,value in args.items():command.extend(["--"+key,str(value)])
    assert main(command)==0
    value=json.loads(capsys.readouterr().out)
    assert value["status"]=="PREPARED_MANUAL_START_ONLY" and len(value["symbols"])==11
    assert value["workers_started"]==value["broker_calls"]==value["orders_placed"]==0
    assert not value["peer_dependency"] and not value["gameplan_dependency"]
    assert inventory(tmp_path)==before

def test_real_default_atlas_diagnostic_never_constructs_runtime(tmp_path,monkeypatch,capsys):
    from ml.intraday import live_session
    args=binding(tmp_path)
    def forbidden(*a,**k):raise AssertionError("Diagnostic must never start runtime")
    monkeypatch.setattr(live_session,"run_manual",forbidden)
    before=inventory(tmp_path)
    command=["--repository",str(ROOT)]
    for key,value in args.items():command.extend(["--"+key,str(value)])
    assert live_session.main(command)==0
    value=json.loads(capsys.readouterr().out)
    assert value["manual_live_start_ready"] and value["reason_codes"]==[]
    assert value["saved_forecasting"]["status"]=="missing"
    assert value["orders_placed"]==0 and not value["activation_changed"] and not value["execution_authorized"]
    assert inventory(tmp_path)==before

def test_canonical_readiness_requires_real_own_adapter_wiring(tmp_path):
    from ml.intraday import readiness
    import shutil
    for name in readiness.PUBLIC_SOURCES:
        destination=tmp_path/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/name,destination)
    before=readiness._source_capabilities(tmp_path)
    assert before["live_execution_implemented"]
    source=tmp_path/"ml/intraday/native_ledger.py"
    source.write_text("# IntradayLedger reserve_intraday _apply_order reconcile\nclass IntradayLedger: pass\n")
    after=readiness._source_capabilities(tmp_path)
    assert not after["live_execution_implemented"]
    assert "SOURCE_CAPABILITY_UNVERIFIED:native_fill_accounting" in after["missing_integration"]

def test_missing_binding_is_reported_without_activation(tmp_path):
    from ml.intraday import readiness
    args=binding(tmp_path)
    from ml.intraday.local_execution import CONFIG_PATH
    (args["datastore"]/CONFIG_PATH).unlink()
    result=readiness.inspect_readiness(repository=ROOT,**args,as_of="2026-10-13T01:00:00Z")
    assert not result["manual_live_start_ready"]
    assert result["reason_codes"]==["INDEPENDENT_LOCAL_15M_BINDING_MISSING_OR_INVALID"]
    assert not result["execution_authorized"] and result["orders_placed"]==0

def test_missing_nightly_binding_is_direct_operating_error(tmp_path):
    from tools.prepare_independent_intraday import prepare_existing
    args=binding(tmp_path)
    from ml.intraday.local_execution import CONFIG_PATH
    (args["datastore"]/CONFIG_PATH).unlink()
    with pytest.raises(FileNotFoundError):prepare_existing(**args)


@pytest.mark.parametrize("relative,expected,replacement", [
    ("ml/intraday/account_execution.py", "IntradayLedger as HorizonLedger, ", ""),
    ("ml/intraday/local_execution.py", "from .native_ledger import IntradayLedger", "from ml.stock_trader.horizon_ledger import HorizonLedger as IntradayLedger"),
    ("ml/intraday/native_ledger.py", "class IntradayLedger(HorizonLedger):", "class IntradayLedger:")])
def test_readiness_rejects_disconnected_native_ledger_adapter(tmp_path,relative,expected,replacement):
    from ml.intraday import readiness
    import shutil
    for name in readiness.PUBLIC_SOURCES:
        destination=tmp_path/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/name,destination)
    source=tmp_path/relative
    text=source.read_text()
    assert expected in text
    source.write_text(text.replace(expected,replacement))
    result=readiness._source_capabilities(tmp_path)
    assert "SOURCE_CAPABILITY_UNVERIFIED:native_fill_accounting" in result["missing_integration"]


@pytest.mark.parametrize("ready,checks,exit_code", [(True,True,0),(False,False,2),(True,False,2),(False,True,2)])
def test_scout_default_diagnostic_honors_own_existing_report(tmp_path,monkeypatch,capsys,ready,checks,exit_code):
    from ml.intraday import live_session, scout_prestart
    args=binding(tmp_path,"Scout")
    calls=[]
    def own_report(repository,datastore):
        calls.append((repository,datastore))
        return dict(ready=ready,local_checks_passed=checks,runtime_started=False,orders_or_commitments_created=False)
    def forbidden(*a,**k):raise AssertionError("Diagnostic must never start runtime")
    monkeypatch.setattr(scout_prestart,"run_local_checks",own_report)
    monkeypatch.setattr(live_session,"run_manual",forbidden)
    before=inventory(tmp_path)
    command=["--repository",str(ROOT)]
    for key,value in args.items():command.extend(["--"+key,str(value)])
    assert live_session.main(command)==exit_code
    value=json.loads(capsys.readouterr().out)
    assert value==dict(ready=ready,local_checks_passed=checks,runtime_started=False,orders_or_commitments_created=False)
    assert calls==[(ROOT,args["datastore"])]
    assert inventory(tmp_path)==before
