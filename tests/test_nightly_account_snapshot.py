"""Private union snapshots use temporary bindings and mocked account readers."""
import builtins
from copy import deepcopy
import json
from pathlib import Path
import runpy
import sqlite3

import pandas as pd
import pytest

from ml.account_gameplan.config import CONFIG, VERSION as ACCOUNT_VERSION, digest
from ml.artifacts import file_checksum
from ml import nightly_workflow
from tools import nightly_account_snapshot as module


NOW, SCOPE = "2026-10-08T10:00:00Z", "a" * 64
ATLAS = ["AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ"]
SCOUT = ["DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN"]
SYMBOLS = sorted(ATLAS + SCOUT)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def contents(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def account_config(path, *, scope=SCOPE):
    value = {"schema_version": ACCOUNT_VERSION, "machine_id": "pc-original", "coordinator_id": "pc-original",
        "participants": {"pc-original": ATLAS, "pc-new": SCOUT}, "account_fingerprint": scope}
    value["activation"] = {"status": "PREPARING", "binding_sha256": digest(value)}
    write(path, value)
    return value


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    repository, root = tmp_path / "checkout", tmp_path / "data"
    watchlist = repository / "datafetching/watchlist.local.txt"
    watchlist.parent.mkdir(parents=True)
    watchlist.write_text("\n".join(ATLAS) + "\n")
    profile = repository / "scratch/cross-pc/local-profile.json"
    write(profile, {"actor": "Atlas", "machine": "pc-original", "contract_version": "cross-pc-v2",
                    "checkout": str(repository), "symbol_profile_path": str(watchlist), "symbols": ATLAS})
    release = repository / "scratch/cross-pc/releases/fixture"
    manifest = release / "installation.json"
    write(release / "coordination/contract.json", {"contract_version": "cross-pc-v2"})
    write(manifest, {"commit": "c" * 40, "files": {
        "coordination/contract.json": file_checksum(release / "coordination/contract.json")}})
    active = repository / "scratch/cross-pc/active.json"
    write(active, {"release_root": str(release), "commit": "c" * 40, "manifest_sha256": file_checksum(manifest)})
    workflow_path = repository / "scratch/nightly-workflow/config.json"
    write(workflow_path, {"schema_version": nightly_workflow.VERSION, "actor": "Atlas",
        "repository": str(repository), "datastore": str(root), "state_root": str(repository / "scratch/nightly-workflow"),
        "local_profile": str(profile), "coordination_active": str(active), "peer_communication_enabled": False,
        "reviewer": {"model": "fixture", "reasoning_effort": "high"}})
    account_path = root / CONFIG
    account_config(account_path)
    monkeypatch.setattr(module, "__file__", str(repository / "tools/nightly_account_snapshot.py"))
    config = {"actor": "Atlas", "private_exchange_authorized": True,
        "workflow_config": str(workflow_path), "local_profile": str(profile), "coordination_active": str(active),
        "account_config": str(account_path), "account_scope_sha256": SCOPE,
        "owners": {"atlas": list(ATLAS), "scout": list(SCOUT)},
        "exchange_root": str(tmp_path / "private-exchange"), "state_root": str(tmp_path / "exchange-state")}
    return config, root


def raw_snapshot():
    maps = {key: dict.fromkeys(SYMBOLS, 0) for key in module._MAPS}
    maps["held_shares"]["AAPL"] = 2
    maps["symbol_exposure"]["AAPL"] = maps["stock_market_value_by_symbol"]["AAPL"] = 200
    maps["pending_sell_shares"]["AAPL"] = 1
    maps["pending_buy_shares"]["AMZN"] = 1
    return {"observed_at": NOW, "status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED",
        "account_fingerprint": SCOPE, "account_equity": 10000, "available_cash": 750,
        "reserved_cash": 250, "gross_exposure": 700, **maps,
        "quotes": {symbol: {"ask": 100, "raw_identifier": "SECRET_QUOTE_ID"} for symbol in SYMBOLS},
        "ownership": {"status": "OBSERVED_CONSISTENT", "safe_for_planning": True,
            "account_matches": True, "blocked_symbols": [], "reason_codes": [],
            "owned_shares": {symbol: int(symbol == "AAPL") for symbol in SYMBOLS},
            "last_saved_reconciliation_at": "2026-10-08T09:00:00Z", "last_saved_reconciliation_ready": True,
            "current_broker_reconciliation_performed": False, "local_db": "SECRET_DATABASE_PATH",
            "active_allocations": [{"allocation_id_sha256": "b" * 64, "symbol": "AAPL", "horizon": "1d",
                "status": "ACTIVE", "owned_shares": 1, "reserved_buy_shares": 0, "reserved_sell_shares": 0,
                "target_start": "2026-10-07T11:00:00Z", "target_end": "2026-10-09T00:00:00Z",
                "prediction_id": "SECRET_PREDICTION_ID", "allocation_id": "SECRET_ALLOCATION_ID"}]},
        "orders_placed": 0, "orders_enabled": False, "raw_account": {"accountNumber": "SECRET_ACCOUNT_NUMBER"},
        "raw_orders": [{"orderId": "SECRET_ORDER_ID"}], "balances": {"private": "SECRET_BALANCE_METADATA"},
        "source_path": "SECRET_LOCAL_PATH", "credential": "SECRET_CREDENTIAL"}


@pytest.mark.parametrize('damage', [None, 'private', 'cash', 'orders'])
def test_retained_reservation_export_is_bounded_and_allowlisted(damage):
    from ml.planning_reservations import retain_local_reservations, PENDING
    raw = raw_snapshot()
    raw.update(working_order_count=0, reserved_cash=0)
    raw['pending_buy_shares'] = dict.fromkeys(SYMBOLS, 0)
    raw['pending_sell_shares'] = dict.fromkeys(SYMBOLS, 0)
    own = raw['ownership']
    own.update(safe_for_planning=False, reason_codes=[PENDING],
               reserved_buy_cash_by_symbol=dict.fromkeys(SYMBOLS, 0))
    own['active_allocations'][0]['reserved_buy_shares'] = 1
    own['reserved_buy_cash_by_symbol']['AAPL'] = 120
    value = retain_local_reservations(raw)
    if damage == 'private': value['planning_reservation_evidence']['accountNumber'] = 'PRIVATE'
    if damage == 'cash': value['planning_reservation_evidence']['local_cash_withheld'] = 0
    if damage == 'orders': value['planning_reservation_evidence']['broker_pending_buy_shares']['AAPL'] = 1
    if damage:
        with pytest.raises(ValueError): module._project(value, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(NOW))
    else:
        result = module._project(value, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(NOW))
        assert result['available_cash'] == 630 and result['reserved_cash'] == 120
        assert result['ownership']['last_saved_reconciliation_at'] == '2026-10-08T09:00:00+00:00'
        assert 'SECRET' not in json.dumps(result)


@pytest.fixture
def captured(configured, monkeypatch):
    config, root = configured
    value, calls = raw_snapshot(), []
    def capture(datastore, account, *, observed_at):
        assert datastore == root and account.symbols == tuple(SYMBOLS)
        assert account.role == "coordinator" and account.activation["status"] == "PREPARING"
        assert account.account_fingerprint == SCOPE and pd.Timestamp(observed_at) == pd.Timestamp(NOW)
        calls.append(True)
        return deepcopy(value)
    monkeypatch.setattr("ml.account_gameplan.preparation._native_snapshot", capture)
    return config, value, calls


def test_capture_minimizes_private_export_without_changing_bindings_or_reserving(captured, tmp_path):
    config, raw, calls = captured
    before, original = contents(tmp_path), deepcopy(raw)
    result = module.capture_snapshot(config, now=NOW)
    assert calls == [True] and raw == original and contents(tmp_path) == before
    assert result["account_scope_sha256"] == SCOPE and result["orders_placed"] == 0
    assert result["orders_enabled"] is False and result["authority"] == "INFORMATIONAL_READ_ONLY"
    assert result["gross_exposure"] == 700 and result["reserved_cash"] == 250
    assert sum(result["symbol_exposure"].values()) == 200  # Outside positions are not dropped from gross.
    assert result["available_cash"] == 750  # Never sum two producers' cash or subtract reservations twice.
    assert result["pending_buy_shares"]["AMZN"] == result["pending_sell_shares"]["AAPL"] == 1
    assert all(set(result[key]) == set(SYMBOLS) for key in module._MAPS)
    assert set(result["quotes"]["AAPL"]) == {"ask"}
    assert result["ownership"]["active_allocations"][0]["allocation_id_sha256"] == "b" * 64
    assert "SECRET_" not in json.dumps(result)
    assert not (Path(config["exchange_root"])).exists()


@pytest.mark.parametrize("damage", ["scout", "permission", "scope", "overlap", "incomplete_partition", "profile",
                                  "account_registry", "other_account_path", "relative_path", "release"])
def test_binding_failures_happen_before_any_account_read(configured, monkeypatch, damage):
    config, _ = configured
    if damage == "scout": config["actor"] = "Scout"
    elif damage == "permission": config["private_exchange_authorized"] = False
    elif damage == "scope": config["account_scope_sha256"] = "f" * 64
    elif damage == "overlap": config["owners"]["scout"][0] = "AAPL"
    elif damage == "incomplete_partition": config["owners"]["scout"].pop()
    elif damage == "profile":
        path = Path(config["local_profile"])
        value = json.loads(path.read_text()); value["machine"] = "pc-new"; write(path, value)
    elif damage == "account_registry":
        path = Path(config["account_config"])
        value = json.loads(path.read_text()); value["participants"]["pc-new"][0] = "IBM"
        value["activation"]["binding_sha256"] = digest({k: v for k, v in value.items() if k != "activation"})
        write(path, value)
    elif damage == "other_account_path":
        path = Path(config["account_config"])
        other = path.with_name("other.json"); other.write_bytes(path.read_bytes()); config["account_config"] = str(other)
    elif damage == "relative_path": config["account_config"] = "state/account-gameplan/config.json"
    else:
        active = json.loads(Path(config["coordination_active"]).read_text())
        write(Path(active["release_root"]) / "coordination/contract.json", {"changed": True})
    monkeypatch.setattr("ml.account_gameplan.preparation._native_snapshot", lambda *a, **k: pytest.fail("Unbound read"))
    with pytest.raises(ValueError):
        module.capture_snapshot(config, now=NOW)


@pytest.mark.parametrize("damage", ["stale", "future", "identity", "scope_conflict", "missing_symbol", "extra_symbol",
    "unknown_reserve", "unsafe_ownership", "missing_owned", "duplicate_allocation", "overdrawn", "exposure",
    "missing_external_gross", "missing_pending_quote", "raw_allocation_id", "invalid_timestamp", "orders_enabled"])
def test_incomplete_or_inconsistent_account_evidence_never_exports(captured, tmp_path, damage):
    config, value, calls = captured
    if damage == "stale": value["observed_at"] = "2026-10-08T09:58:00Z"
    elif damage == "future": value["observed_at"] = "2026-10-08T10:01:00Z"
    elif damage == "identity": value["account_fingerprint"] = "f" * 64
    elif damage == "scope_conflict": value["account_scope_sha256"] = "f" * 64
    elif damage == "missing_symbol": value["held_shares"].pop("DOCU")
    elif damage == "extra_symbol": value["pending_buy_shares"]["IBM"] = 1
    elif damage == "unknown_reserve": value["reserved_cash"] = None
    elif damage == "unsafe_ownership": value["ownership"]["safe_for_planning"] = False
    elif damage == "missing_owned": value["ownership"]["owned_shares"].pop("DOCU")
    elif damage == "duplicate_allocation": value["ownership"]["active_allocations"].append(deepcopy(value["ownership"]["active_allocations"][0]))
    elif damage == "overdrawn": value["ownership"]["active_allocations"][0]["owned_shares"] = 3
    elif damage == "exposure": value["other_symbol_exposure"]["AAPL"] = 200
    elif damage == "missing_external_gross": value["gross_exposure"] = 199
    elif damage == "missing_pending_quote": value["quotes"]["AMZN"]["ask"] = None
    elif damage == "raw_allocation_id": value["ownership"]["active_allocations"][0]["allocation_id_sha256"] = "SECRET_ALLOCATION_ID"
    elif damage == "invalid_timestamp": value["ownership"]["active_allocations"][0]["target_end"] = "2026-10-09"
    else: value["orders_enabled"] = True
    before = contents(tmp_path)
    with pytest.raises(ValueError):
        module.capture_snapshot(config, now=NOW)
    assert calls == [True] and contents(tmp_path) == before


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), -1, "SECRET_ACCOUNT_VALUE"])
def test_only_finite_numeric_cash_is_exportable(captured, invalid):
    config, value, _ = captured
    value["available_cash"] = invalid
    with pytest.raises(ValueError, match="account quantity"):
        module.capture_snapshot(config, now=NOW)


def test_local_binding_changes_during_capture_do_not_export(configured, monkeypatch):
    config, _ = configured
    def capture(*args, **kwargs):
        path = Path(config["account_config"])
        path.write_bytes(path.read_bytes() + b" ")
        return raw_snapshot()
    monkeypatch.setattr("ml.account_gameplan.preparation._native_snapshot", capture)
    with pytest.raises(ValueError, match="bindings changed"):
        module.capture_snapshot(config, now=NOW)


def test_provider_exception_never_leaks_into_publication_error(configured, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_ACCOUNT_NUMBER SECRET_CREDENTIAL")
    monkeypatch.setattr("ml.account_gameplan.preparation._native_snapshot", fail)
    with pytest.raises(ValueError, match="read-only snapshot capture failed") as caught:
        module.capture_snapshot(configured[0], now=NOW)
    assert "SECRET" not in str(caught.value) and caught.value.__suppress_context__


def test_scout_can_import_adapter_without_atlas_account_subsystem(monkeypatch):
    original = builtins.__import__
    def no_account_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("ml.account_gameplan") or (name == "ml.stock_trader.state" and "validated_symbols" in fromlist):
            pytest.fail("Scout cannot import the Atlas account subsystem")
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", no_account_import)
    loaded = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/nightly_account_snapshot.py"))
    with pytest.raises(ValueError, match="authorized Atlas"):
        loaded["capture_snapshot"]({"actor": "Scout", "private_exchange_authorized": True}, now=NOW)


def test_real_native_snapshot_reads_mock_broker_and_existing_union_ledger_without_writes(configured, monkeypatch, tmp_path):
    from tests.test_gameplan_trade_snapshot import ReadSession, buy_order, ledger
    config, root = configured
    path = ledger(root)
    held = dict.fromkeys(SYMBOLS, 0); held["AAPL"] = 2
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE snapshots SET payload=?", (json.dumps({"held_shares": held}),))
    class UnionSession(ReadSession):
        def get_equity_quotes(self, symbols):
            assert set(symbols) == set(SYMBOLS)
            self.calls.append("quotes")
            return {symbol: deepcopy(self.quotes["AAPL"]) for symbol in symbols}
    session = UnionSession()
    outside = deepcopy(session.account["securitiesAccount"]["positions"][0])
    outside.update(longQuantity=5, marketValue=500); outside["instrument"]["symbol"] = "IBM"
    session.account["securitiesAccount"]["positions"].append(outside)
    order = buy_order(); order["orderLegCollection"][0]["instrument"]["symbol"] = "IBM"
    session.orders = [order]
    monkeypatch.setattr("ml.gameplan_trade_snapshot.SchwabSession", lambda: session)
    before = contents(tmp_path)
    result = module.capture_snapshot(config, now=NOW)
    assert result["reserved_cash"] == 200 and result["available_cash"] == 1300
    assert result["gross_exposure"] == 700 and result["held_shares"] == held
    assert result["ownership"]["owned_shares"]["AAPL"] == 1
    assert all(session.calls.count(name) == 1 for name in ("prepare", "verify", "account", "orders", "quotes"))
    assert "SECRET_" not in json.dumps(result) and contents(tmp_path) == before


def test_missing_native_ownership_ledger_never_becomes_empty_safe_inventory(configured, monkeypatch, tmp_path):
    from tests.test_gameplan_trade_snapshot import ReadSession
    class UnionSession(ReadSession):
        def get_equity_quotes(self, symbols):
            return {symbol: deepcopy(self.quotes["AAPL"]) for symbol in symbols}
    monkeypatch.setattr("ml.gameplan_trade_snapshot.SchwabSession", UnionSession)
    before = contents(tmp_path)
    with pytest.raises(ValueError, match="read-only snapshot capture failed"):
        module.capture_snapshot(configured[0], now=NOW)
    assert contents(tmp_path) == before


@pytest.fixture
def partitioned(configured, monkeypatch, tmp_path):
    from contextlib import closing
    import gc
    from tools import nightly_ownership
    from tests.test_nightly_ownership import setup_owner
    from tests.test_gameplan_trade_snapshot import ledger, ReadSession, buy_order
    config, root = configured
    monkeypatch.setattr(nightly_workflow, "source_identity", lambda _: {"commit": "c" * 40, "source_sha256": "d" * 64})
    scout, scout_root, scout_probe = setup_owner(tmp_path / "peer", "Scout")
    atlas_probe = Path(json.loads(Path(config["workflow_config"]).read_text())["repository"]) / "tools/nightly_ownership.py"
    atlas_probe.parent.mkdir(parents=True); atlas_probe.write_text("# fixture identity\n")
    path = ledger(root); gc.collect()
    held = dict.fromkeys(ATLAS, 0); held["AAPL"] = 2
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE snapshots SET payload=?", (json.dumps({"held_shares": held}),))
    observations = []
    for producer, probe in ((config, atlas_probe), (scout, scout_probe)):
        monkeypatch.setattr(nightly_ownership, "__file__", str(probe))
        observations.append(nightly_ownership.capture_ownership(producer, now=pd.Timestamp(NOW) - pd.Timedelta(seconds=2)))
    class UnionSession(ReadSession):
        def get_equity_quotes(self, symbols):
            assert set(symbols) == set(SYMBOLS)
            self.calls.append("quotes")
            return {symbol: deepcopy(self.quotes["AAPL"]) for symbol in symbols}
    session = UnionSession()
    scout_position = deepcopy(session.account["securitiesAccount"]["positions"][0])
    scout_position["instrument"]["symbol"] = "DOCU"
    outside = deepcopy(scout_position)
    outside.update(longQuantity=5, marketValue=500); outside["instrument"]["symbol"] = "IBM"
    session.account["securitiesAccount"]["positions"].extend([scout_position, outside])
    order = buy_order(); order["orderLegCollection"][0]["instrument"]["symbol"] = "IBM"
    session.orders = [order]
    monkeypatch.setattr("ml.gameplan_trade_snapshot.SchwabSession", lambda: session)
    monkeypatch.setattr("ml.account_gameplan.preparation._native_snapshot", lambda *a, **k: pytest.fail("Union native ledger fallback"))
    return config, observations, session


def test_real_eleven_plus_eleven_ownership_uses_one_account_read_and_preserves_all_native_files(partitioned, tmp_path):
    config, observations, session = partitioned
    before = contents(tmp_path)
    result = module.capture_snapshot(config, now=NOW, ownership_observations=observations)
    assert contents(tmp_path) == before
    assert result["ownership"]["owned_shares"]["AAPL"] == result["ownership"]["owned_shares"]["DOCU"] == 1
    assert result["held_shares"]["AAPL"] == result["held_shares"]["DOCU"] == 2
    assert result["reserved_cash"] == 200 and result["available_cash"] == 1300
    assert result["gross_exposure"] == 900
    assert all(session.calls.count(name) == 1 for name in ("prepare", "verify", "account", "orders", "quotes"))
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("damage", ["stale", "duplicate", "account", "partition", "private_field"])
def test_bad_producer_observations_fail_before_broker_capture(partitioned, damage):
    from tools import nightly_ownership
    config, observations, session = partitioned
    value = observations[1]
    if damage == "stale": stamp = pd.Timestamp(NOW) + pd.Timedelta(seconds=61)
    else: stamp = NOW
    if damage == "duplicate": observations[1] = deepcopy(observations[0])
    elif damage == "account": value["envelope"]["account_fingerprint"] = "b" * 64
    elif damage == "partition": value["envelope"]["symbols"].pop()
    elif damage == "private_field": value["envelope"]["raw_account"] = "SECRET"
    if damage != "duplicate": value["envelope"]["source_fingerprint"] = nightly_ownership._fingerprint(value["envelope"])
    with pytest.raises(ValueError): module.capture_snapshot(config, now=stamp, ownership_observations=observations)
    assert session.calls == []


def test_current_broker_holdings_must_equal_both_saved_expectations(partitioned, tmp_path):
    config, observations, session = partitioned
    session.account["securitiesAccount"]["positions"][1].update(longQuantity=3, marketValue=300)
    before = contents(tmp_path)
    with pytest.raises(ValueError): module.capture_snapshot(config, now=NOW, ownership_observations=observations)
    assert contents(tmp_path) == before and session.calls.count("account") == 1


def test_ownership_expiring_during_broker_capture_cannot_be_exported(partitioned, monkeypatch):
    from tools import nightly_ownership
    config, observations, session = partitioned
    old = (pd.Timestamp(NOW) - pd.Timedelta(seconds=50)).isoformat()
    for value in observations:
        value["ledger_observed_at"] = value["envelope"]["observed_at"] = old
        value["envelope"]["source_fingerprint"] = nightly_ownership._fingerprint(value["envelope"])
    elapsed = [0]
    original = session.get_account
    def account():
        elapsed[0] = 20
        return original()
    monkeypatch.setattr(session, "get_account", account)
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])
    with pytest.raises(ValueError, match="stale"):
        module.capture_snapshot(config, now=NOW, ownership_observations=observations)
