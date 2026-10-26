"""Independent local bindings and one-time preparation use fixture files only."""
import json
import sqlite3

import pytest

from ml.intraday.local_execution import CONFIG_PATH, VERSION, load_local_binding, validate_binding
from ml.intraday.prediction import ContractError
from tools.prepare_independent_intraday import prepare, prepare_existing


def fixture(tmp_path, actor="Atlas"):
    machine = {"Atlas":"pc-original", "Scout":"pc-new"}[actor]
    watch = tmp_path / "watchlist.txt"
    watch.write_text("AAPL\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"actor":actor,"machine":machine,"symbols":["AAPL"],"symbol_profile_path":str(watch)}))
    account = tmp_path / "legacy-account.json"
    account.write_text(json.dumps({"machine_id":machine,"participants":{machine:["AAPL"]},"account_fingerprint":"a"*64}))
    return {"datastore":tmp_path,"profile":profile,"watchlist":watch,"account_config":account}


@pytest.mark.parametrize("actor", ["Atlas", "Scout"])
def test_proposal_preserves_originals_creates_no_ledger_or_activation(tmp_path, actor):
    args = fixture(tmp_path, actor)
    before = {p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    proposal, evidence = prepare(**args)
    assert validate_binding(proposal).stream == actor.lower()+"-15m"
    assert proposal["manual_start_only"] is True
    assert evidence["orders_placed"] == evidence["broker_calls"] == 0
    assert {p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_pending_own_legacy_order_blocks_new_epoch_but_foreign_does_not(tmp_path):
    args=fixture(tmp_path)
    path=tmp_path/"state/independent-stock-trader/holdings.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE reservations(id TEXT,status TEXT); CREATE TABLE intraday_streams(kind TEXT,id TEXT,stream TEXT);")
        db.execute("INSERT INTO reservations VALUES('foreign','UNKNOWN')")
        db.execute("INSERT INTO intraday_streams VALUES('reservation','foreign','gameplan')")
    before=path.read_bytes()
    assert prepare(**args)[1]["legacy_local_terminal_orders"]==0
    assert path.read_bytes()==before
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO reservations VALUES('own','UNKNOWN')")
        db.execute("INSERT INTO intraday_streams VALUES('reservation','own','atlas-15m')")
    before=path.read_bytes()
    with pytest.raises(ValueError,match="unresolved"):
        prepare(**args)
    assert path.read_bytes()==before


def test_runtime_binding_reads_only_own_path_and_rejects_wrong_actor(tmp_path):
    proposal,_=prepare(**fixture(tmp_path))
    path=tmp_path/CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(proposal))
    assert load_local_binding(tmp_path).actor=="Atlas"
    proposal["actor"]="Scout"
    path.write_text(json.dumps(proposal))
    with pytest.raises(ContractError):
        load_local_binding(tmp_path)


def test_nightly_prepare_is_idempotent_and_never_scans_legacy_or_other_traders(tmp_path):
    args=fixture(tmp_path)
    proposal,_=prepare(**args)
    path=tmp_path/CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(proposal))
    args["account_config"].write_text("invalid prior account")
    old=tmp_path/"state/independent-stock-trader/holdings.sqlite3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"unreadable legacy database")
    before={p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    kwargs={k:v for k,v in args.items() if k!="account_config"}
    first=prepare_existing(**kwargs)
    assert first==prepare_existing(**kwargs)
    assert first["status"]=="PREPARED_MANUAL_START_ONLY"
    assert first["gameplan_dependency"] is first["peer_dependency"] is False
    assert first["workers_started"]==first["broker_calls"]==first["orders_placed"]==0
    assert {p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}==before
