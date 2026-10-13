"""Production composition against temporary books and synthetic capture only."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import stat

import pytest

from ml.account_gameplan.config import AccountConfig
from ml.intraday.admission_host import AdmissionHost
from ml.intraday.admission_service import AdmissionError
from ml.intraday.scout_journal import JournalError
from ml.intraday import account_execution
from ml.stock_trader.contracts import PortfolioState, QuoteState
from tests.test_intraday_account_authority import ACCOUNT, ATLAS, SCOUT, Clock
from tests.test_intraday_admission_service import request, bound

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    state = SimpleNamespace(root=tmp_path, clock=Clock(), compatible=True, broker_created=0, captures=0, cash=100,
        working=[], account=ACCOUNT, binding={"server_identity": "reviewed-atlas-server", "account_fingerprint": ACCOUNT,
        "scout_symbols": list(SCOUT)}, observed=None)
    state.config = AccountConfig("pc-original", "pc-original", {"pc-original": ATLAS, "pc-new": SCOUT}, ACCOUNT,
                                 "e" * 64, {"status": "ACTIVE"})
    for name in ("server_certificate", "server_private_key", "client_ca"):
        path = tmp_path / (name + ".pem")
        path.write_bytes(("synthetic-" + name).encode())
        state.binding[name] = str(path)
    def proof(*args, **kwargs):
        return dict(ready=state.compatible, compatibility_state="absent" if state.compatible else "blocked",
                    reason_codes=[] if state.compatible else ["INCOMPATIBLE_WRITER"], observed_at=state.clock().isoformat())
    class Broker:
        def stable_account_fingerprint(self): return state.account
        def verify_read_snapshot(self, identity):
            assert identity == "b" * 64
    def broker():
        state.broker_created += 1
        return Broker()
    state.host = AdmissionHost(repository=REPOSITORY, datastore=tmp_path, binding_path=tmp_path / "host-binding.json",
        clock=state.clock, broker_factory=broker, binding_loader=lambda _: dict(state.binding),
        config_loader=lambda _: state.config, cutover_verifier=lambda *args: None,
        atlas_reader=proof, gameplan_reader=proof)
    return state


def synthetic_capture(env, monkeypatch):
    # The real _capture method remains in use; only provider observation
    # boundaries are synthetic. Their fingerprints intentionally differ here.
    monkeypatch.setattr(env.host, "_capture_code_current", lambda: None)
    def observe(broker, ledger, **kwargs):
        assert not ledger.pending_reservations()
        return ()
    def capture(broker, **kwargs):
        env.captures += 1
        env.clock.tick()
        now = env.clock().isoformat()
        assert kwargs["literal_cash_only"] is kwargs["include_order_identities"] is True
        symbols = (*ATLAS, *SCOUT)
        return PortfolioState(now, 10000, env.cash, 0, 0, dict.fromkeys(symbols, 20), dict.fromkeys(symbols, 0), {}, {},
            len(env.working), {s: QuoteState(s, 10, 10, 10, 10, 1000, now) for s in symbols}, "c" * 64,
            "b" * 64, tuple(env.working))
    monkeypatch.setattr(account_execution, "capture_order_evidence", observe)
    monkeypatch.setattr(account_execution, "capture_portfolio_state", capture)


def test_construction_capabilities_and_readiness_never_create_broker_lease_or_books(env):
    assert env.host.capabilities("pc-new")["read_only"] is True
    response = env.host.readiness("pc-new", {"request_nonce": "offline-host"})
    assert response["ready"] is True and env.broker_created == 0
    assert list(env.root.rglob("*.sqlite3")) == []


def test_redirected_root_is_rejected_before_resolution_or_binding_load(env, monkeypatch):
    original_lstat = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == env.root:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_file_attributes=0x400)
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("path resolved before alias check"))
    with pytest.raises(JournalError, match="reparse"):
        AdmissionHost(repository=REPOSITORY, datastore=env.root, binding_path=env.root / "host-binding.json",
            binding_loader=lambda _: pytest.fail("binding loaded before alias check"))


def test_incompatible_writers_prevent_mutating_context_and_capture(env):
    env.compatible = False
    with pytest.raises(AdmissionError, match="ALL_LOADED_WRITERS"):
        env.host.reserve("pc-new", request().payload())
    assert env.broker_created == 0 and list(env.root.rglob("*.sqlite3")) == []


def test_loaded_capture_code_is_checked_against_actual_imported_methods(env):
    env.host._capture_code_current()


def test_readiness_cannot_advertise_ready_when_host_capture_is_stale(env, monkeypatch):
    def stale_capture():
        raise AdmissionError("LOADED_CAPTURE_DIFFERS_FROM_REVIEWED_SOURCE")
    monkeypatch.setattr(env.host, "_capture_code_current", stale_capture)
    with pytest.raises(AdmissionError, match="LOADED_CAPTURE_DIFFERS"):
        env.host.readiness("pc-new", {"request_nonce": "stale-host"})
    assert env.broker_created == 0 and list(env.root.rglob("*.sqlite3")) == []


def test_existing_capture_and_canonical_one_pool_are_used_without_manual_trading_grant(env, monkeypatch):
    synthetic_capture(env, monkeypatch)
    response = env.host.reserve("pc-new", request().payload())
    assert response["grant"]["quantity"] == 10 and env.captures == 1 and env.broker_created == 1
    assert env.host.reserve("pc-new", request().payload()) == response and env.captures == 1
    # A second forecast shares that same fully committed cash, no stream budget.
    second = request(request_id="second", forecast_id="d" * 64, forecast_sha256="d" * 64)
    declined = env.host.reserve("pc-new", second.payload())
    assert declined["status"] == "DECLINED" and env.captures == 2
    with sqlite3.connect(env.root / "state/account-gameplan/authority.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert db.execute("SELECT expires FROM lease").fetchone()[0] == env.clock().isoformat()
    accounting = env.host.accounting("pc-new", dict(request_nonce="account-view", **bound(request())))
    assert accounting["projection"]["account_reservation"]["reservation_id"] == response["grant"]["reservation_id"]


def test_authorization_refreshes_existing_capture_and_keeps_once_only_claim(env, monkeypatch):
    synthetic_capture(env, monkeypatch)
    env.host.reserve("pc-new", request().payload())
    response = env.host.authorize_send("pc-new", bound(request(), claim_id="original-local-journal-claim"))
    assert response["status"] == "SEND_AUTHORIZED_ONCE" and env.captures == 2
    assert env.host.status("pc-new", bound(request()))["send_authorized"] is False
    # No broker order submission method exists on this synthetic read adapter.
    assert not hasattr(env.host._broker_instance, "submit_prepared_order")


def test_private_binding_or_actual_account_change_blocks_existing_host(env, monkeypatch):
    synthetic_capture(env, monkeypatch)
    env.binding["server_identity"] = "changed-server"
    with pytest.raises(AdmissionError, match="PRIVATE_HOST_BINDING_CHANGED"):
        env.host.reserve("pc-new", request().payload())
    env.binding["server_identity"] = "reviewed-atlas-server"
    env.account = "f" * 64
    with pytest.raises(AdmissionError, match="BROKER_ACCOUNT_DIFFERS"):
        env.host.reserve("pc-new", request().payload())
    with sqlite3.connect(env.root / "state/account-gameplan/authority.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["server_certificate", "server_private_key", "client_ca"])
def test_credential_file_rotation_requires_reload_without_exporting_private_hashes(env, name):
    Path(env.binding[name]).write_bytes(b"different-synthetic-credential")
    with pytest.raises(AdmissionError, match="CREDENTIALS_CHANGED_RELOAD_REQUIRED"):
        env.host.readiness("pc-new", {"request_nonce": "rotated-file"})
    with pytest.raises(AdmissionError, match="CREDENTIALS_CHANGED_RELOAD_REQUIRED"):
        env.host.capabilities("pc-new")
    with pytest.raises(AdmissionError, match="CREDENTIALS_CHANGED_RELOAD_REQUIRED"):
        env.host.reserve("pc-new", request().payload())
    assert env.broker_created == 0 and list(env.root.rglob("*.sqlite3")) == []


def test_exact_external_pending_sell_is_carried_into_native_sale_capture(env, monkeypatch):
    synthetic_capture(env, monkeypatch)
    env.working = [dict(status="CURRENT", asset_type="EQUITY", instruction="SELL", order_id="777", symbol="MSFT",
        remaining_quantity=5, filled_quantity=0, limit_price="10", reserved_cash="0")]
    response = env.host.reserve("pc-new", request(side="SELL", quantity=20).payload())
    assert response["grant"]["quantity"] == 15


def test_cli_without_serve_only_runs_readonly_probe(monkeypatch, capsys):
    from ml.intraday import admission_host
    calls = []
    class Host:
        def __init__(self, **kwargs): calls.append("constructed")
        def readiness(self, participant, body):
            calls.append("readiness")
            return {"ready": False, "read_only": True}
    monkeypatch.setattr(admission_host, "AdmissionHost", Host)
    assert admission_host.main(["--repository", str(REPOSITORY), "--datastore", "C:/unused", "--binding", "C:/unused/host.json"]) == 1
    assert calls == ["constructed", "readiness"] and '"read_only": true' in capsys.readouterr().out
