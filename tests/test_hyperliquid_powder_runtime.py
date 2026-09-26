"""Fake broker only: no credentials, signing, live orders or network calls."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from ml.hyperliquid_powder_ledger import PowderLedger
from ml.hyperliquid_powder_runtime import PowderRuntime, SafetyHalt, load_config, main, read_status

BASE = 1_790_400_000.0


class Clock:
    def __init__(self):
        self.now = BASE

    def __call__(self):
        return self.now


def configuration(tmp_path, symbols=("BTC",), **policy_overrides):
    root = tmp_path / "data"
    markets = tmp_path / "markets.json"
    models = tmp_path / "models.json"
    paper = tmp_path / "paper.json"
    powder = tmp_path / "powder.json"
    markets.write_text(json.dumps({"version": 1, "symbols": list(symbols), "interval": "15m", "output_root": str(root)}))
    models.write_text(json.dumps({"version": 1, "markets_config": str(markets), "horizons_bars": [4]}))
    paper.write_text(json.dumps({"version": 1, "data_root": str(root), "model_config": str(models), **policy_overrides}))
    powder.write_text(json.dumps({"version": 1, "mode": "powder", "paper_config": str(paper), "max_order_notional": 500, "poll_seconds": 30}))
    return powder, root


class FakeBroker:
    network = "https://fake.exchange.invalid"

    def __init__(self, clock, symbols=("BTC",)):
        self.clock, self.symbols = clock, symbols
        self.identities = {account: "0x" + str(i) * 40 for i, account in enumerate(("alex", "jeremy", "clearpond"), 1)}
        self.accounts = {account: {"equity": 10000.0, "available_cash": 10000.0, "gross": 0,
            "positions": {coin: {"quantity": 0.0, "mark": 100.0, "entry_price": None,
                                  "kind": "spot" if account == "clearpond" else "perp"} for coin in symbols}}
            for account in self.identities}
        self.submitted, self.reconciled = [], []
        self.results = {}
        self.outcome = "FILLED"
        self.fraction = 1.0
        self.timeout = False
        self.fee_token = "USDC"
        self.fee = 0.1
        self.ledger = None
        self.open_orders = []
        self.observation_age = 0
        self.before_submit = None

    def preflight(self):
        return dict(self.identities)

    def observe(self):
        for values in self.accounts.values():
            values["gross"] = sum(abs(p["quantity"]) * p["mark"] for p in values["positions"].values()) + sum(p["quantity"] * p["mark"] for p in values.get("passive_positions", {}).values())
        return {"observed_at": self.clock() - self.observation_age, "identities": dict(self.identities),
                "accounts": deepcopy(self.accounts), "open_orders": deepcopy(self.open_orders)}

    def make_order(self, account, symbol, delta_notional, reduce_only, observation):
        position = observation["accounts"][account]["positions"][symbol]
        notional = min(abs(delta_notional), 500)
        if notional < 10:
            raise ValueError("Below minimum executable notional")
        return {"account": account, "symbol": symbol, "coin": symbol, "is_buy": delta_notional > 0,
                "size": notional / position["mark"], "limit_price": position["mark"],
                "reduce_only": bool(reduce_only and account != "clearpond"), "tif": "Ioc"}

    def submit(self, intent):
        assert self.ledger.get_intent(intent["id"])["state"] == "SUBMITTING"
        from ml.hyperliquid_powder_exchange import PreSubmitRejected
        if self.before_submit:
            self.before_submit()
        try:
            self.pre_submit_check(intent["request"], self.observe())
        except Exception as exc:
            raise PreSubmitRejected(str(exc)) from None
        self.submitted.append(deepcopy(intent))
        if self.timeout:
            raise TimeoutError("Unknown transport outcome")
        return {"status": "ok", "filled": True}  # This ACK must never create a fill.

    def reconcile(self, intent):
        self.reconciled.append(intent["id"])
        if intent["id"] in self.results:
            return deepcopy(self.results[intent["id"]])
        if self.outcome in {"UNKNOWN", "OPEN", "REJECTED"}:
            return {"state": self.outcome, "fills": [], "detail": "fake exchange evidence"}
        request = intent["request"]
        quantity = request["size"] * self.fraction * (1 if request["is_buy"] else -1)
        fill = {"account": request["account"], "symbol": request["symbol"], "coin": request["coin"],
                "tid": str(intent["id"]), "oid": intent["id"], "time": int(self.clock() * 1000),
                "quantity": quantity, "price": request["limit_price"], "fee": self.fee,
                "fee_token": self.fee_token}
        pos = self.accounts[request["account"]]["positions"][request["symbol"]]
        pos["quantity"] += quantity
        if request["account"] == "clearpond" and self.fee_token in {request["symbol"], "U" + request["symbol"]}:
            pos["quantity"] -= self.fee
        if request["account"] != "clearpond":
            pos["entry_price"] = request["limit_price"] if pos["quantity"] else None
        result = {"state": "FILLED" if self.fraction == 1 else "CANCELED", "fills": [fill], "oid": intent["id"]}
        self.results[intent["id"]] = result
        return deepcopy(result)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Powder runtime tests cannot reach a network or signer")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


@pytest.fixture
def runtime(tmp_path):
    path, root = configuration(tmp_path)
    clock = Clock()
    broker = FakeBroker(clock)
    ledger = PowderLedger(root / "_powder" / "ledger.sqlite3", clock=clock)
    broker.ledger = ledger
    forecasts = {"BTC": ({"p_not_down": .65, "qualified": True, "prediction_id": "same-forecast", "_valid_until_epoch": BASE + 900}, .02)}

    def reader(coin, now, policy, interval, horizon):
        if coin not in forecasts:
            raise ValueError("Forecast stale")
        return deepcopy(forecasts[coin])

    instance = PowderRuntime(path, broker=broker, ledger=ledger, clock=clock, forecast_reader=reader)
    yield instance, broker, ledger, clock, forecasts, path
    try:
        ledger.close()
    except Exception:
        pass


@pytest.mark.parametrize("overrides", [{"version": 2}, {"version": True}, {"mode": "paper"},
                                     {"poll_seconds": False}, {"max_order_notional": 501}, {"extra": 1}])
def test_config_rejects_invalid_settings(tmp_path, overrides):
    path, _ = configuration(tmp_path)
    values = json.loads(path.read_text())
    path.write_text(json.dumps({**values, **overrides}))
    with pytest.raises(ValueError):
        load_config(path)


def test_check_and_status_create_no_powder_files_and_do_not_arm(tmp_path):
    path, root = configuration(tmp_path)
    broker = FakeBroker(Clock())
    instance = PowderRuntime(path, broker=broker, clock=broker.clock, forecast_reader=lambda *a: ({"p_not_down": .6, "qualified": True}, .02))
    result = instance.check()
    assert result["execution_enabled"] is False
    assert not broker.submitted
    assert not (root / "_powder").exists()
    assert read_status(path)["state"] == "NOT_ACTIVATED"
    assert not (root / "_powder").exists()


@pytest.mark.parametrize("arguments", [[], ["--activate"], ["--execute"], ["--check", "--execute"], ["--status", "--activate"]])
def test_cli_requires_fresh_explicit_activation_pair(arguments):
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2


def test_activation_adopts_actual_inventory_without_orders(runtime):
    engine, broker, ledger, *_ = runtime
    broker.accounts["alex"]["positions"]["BTC"].update(quantity=-3, entry_price=100)
    engine.activate(execute=True)
    assert not broker.submitted
    assert ledger.metadata()["baseline"]["accounts"]["alex"]["positions"]["BTC"]["quantity"] == -3


def test_one_capped_ioc_per_tick_replans_same_forecast(runtime):
    engine, broker, ledger, clock, *_ = runtime
    engine.activate(execute=True)
    engine.tick()
    assert len(broker.submitted) == 1
    request = broker.submitted[0]["request"]
    assert request["size"] * request["limit_price"] <= 500
    assert len(ledger.fills()) == 1
    clock.now += 30
    engine.tick()
    assert len(broker.submitted) == 2
    assert all(i["decision"]["forecast"]["prediction_id"] == "same-forecast" for i in broker.submitted)


@pytest.mark.parametrize("state", ["UNKNOWN", "OPEN"])
def test_ack_and_unknown_or_open_intent_never_allow_duplicate_submission(runtime, state):
    engine, broker, ledger, clock, *_ = runtime
    broker.outcome = state
    broker.timeout = state == "UNKNOWN"
    engine.activate(execute=True)
    assert engine.tick()["state"] == "BLOCKED"
    assert not ledger.fills()
    clock.now += 30
    assert engine.tick()["state"] == "BLOCKED"
    assert len(broker.submitted) == 1


def test_partial_fill_is_reconciled_then_next_tick_replans_from_actual_inventory(runtime):
    engine, broker, ledger, clock, *_ = runtime
    broker.fraction = .4
    engine.activate(execute=True)
    engine.tick()
    assert ledger.list_intents()[0]["state"] == "CANCELED"
    assert ledger.fills()[0]["quantity"] == pytest.approx(2)
    clock.now += 30
    engine.tick()
    assert len(broker.submitted) == 2


def test_external_inventory_change_halts_without_new_submission(runtime):
    engine, broker, _, _, *_ = runtime
    engine.activate(execute=True)
    broker.accounts["jeremy"]["positions"]["BTC"].update(quantity=1, entry_price=100)
    with pytest.raises(SafetyHalt, match="external position"):
        engine.tick()
    assert not broker.submitted


def test_base_asset_spot_fee_is_not_misclassified_as_external_inventory(runtime):
    engine, broker, ledger, clock, *_ = runtime
    broker.fee_token, broker.fee = "UBTC", .001
    engine.activate(execute=True)
    engine.tick()
    assert broker.submitted[0]["request"]["account"] == "clearpond"
    clock.now += 30
    engine.tick()
    assert len(broker.submitted) == 2
    assert ledger.latest_observation()["accounts"]["clearpond"]["positions"]["BTC"]["quantity"] == pytest.approx(4.999)


def test_stop_reductions_precede_signal_increases_and_persist_cooldown(runtime):
    engine, broker, ledger, _, *_ = runtime
    broker.accounts["jeremy"]["positions"]["BTC"].update(quantity=10, entry_price=105)
    engine.activate(execute=True)
    engine.tick()
    intent = broker.submitted[0]
    assert intent["request"]["account"] == "jeremy"
    assert intent["request"]["reduce_only"] is True
    assert intent["reason"] == "stop_loss"
    assert ledger.get_cooldown("jeremy", "BTC") > engine.clock()


def test_missing_forecast_only_reduces_existing_positions(runtime):
    engine, broker, _, _, forecasts, _ = runtime
    broker.accounts["jeremy"]["positions"]["BTC"].update(quantity=2, entry_price=100)
    engine.activate(execute=True)
    forecasts.clear()
    engine.tick()
    assert len(broker.submitted) == 1
    assert broker.submitted[0]["request"]["reduce_only"]


@pytest.mark.parametrize("change", ["identity", "network", "configuration", "open_order", "stale"])
def test_binding_and_observation_changes_fail_closed(runtime, change):
    engine, broker, _, _, _, path = runtime
    engine.activate(execute=True)
    if change == "identity":
        broker.identities["alex"] = "changed"
    elif change == "network":
        broker.network = "another-network"
    elif change == "configuration":
        values = json.loads(path.read_text()); values["poll_seconds"] = 31; path.write_text(json.dumps(values))
    elif change == "open_order":
        broker.open_orders = [{"oid": 123}]
    else:
        broker.observation_age = 100
    with pytest.raises(SafetyHalt):
        engine.tick()
    assert not broker.submitted


def test_stop_request_prevents_new_orders_without_flattening(runtime):
    engine, broker, _, _, *_ = runtime
    broker.accounts["alex"]["positions"]["BTC"].update(quantity=-2, entry_price=100)
    engine.activate(execute=True)
    engine.stopped.set()
    assert engine.tick()["state"] == "STOPPED"
    assert broker.accounts["alex"]["positions"]["BTC"]["quantity"] == -2
    assert not broker.submitted


def test_prepared_intent_is_abandoned_on_restart_without_submission(runtime):
    engine, broker, ledger, _, *_ = runtime
    engine.activate(execute=True)
    request = broker.make_order("jeremy", "BTC", 100, False, broker.observe())
    intent = ledger.prepare(request, "signal_rebalance", {})
    engine.activate(execute=True)
    assert ledger.get_intent(intent["id"])["state"] == "REJECTED"
    assert not broker.submitted


def test_cash_reserve_prevents_new_risk(runtime):
    engine, broker, _, _, *_ = runtime
    for account in broker.accounts.values():
        account["available_cash"] = 500
    engine.activate(execute=True)
    engine.tick()
    assert not broker.submitted


def test_missing_initial_forecast_blocks_adoption_and_orders(runtime):
    engine, broker, ledger, _, forecasts, _ = runtime
    broker.accounts["jeremy"]["positions"]["BTC"].update(quantity=2, entry_price=100)
    forecasts.clear()
    result = engine.activate(execute=True)
    assert result["state"] == "NOT_READY"
    assert result["execution_enabled"] is False
    assert "BTC" in result["forecast_errors"]
    assert ledger.metadata()["baseline"] is None
    assert not broker.submitted


def test_check_reports_not_ready_when_forecast_is_missing(runtime):
    engine, broker, _, _, forecasts, _ = runtime
    forecasts.clear()
    assert engine.check()["state"] == "NOT_READY"
    assert not broker.submitted


def test_passive_inventory_is_preserved_and_external_changes_halt(runtime):
    engine, broker, _, _, *_ = runtime
    broker.accounts["alex"]["passive_positions"] = {"BTC": {"quantity": 1, "mark": 100}}
    engine.activate(execute=True)
    broker.accounts["alex"]["passive_positions"]["BTC"]["quantity"] = 2
    with pytest.raises(SafetyHalt, match="external passive"):
        engine.tick()
    assert not broker.submitted


def test_inherited_passive_symbol_breach_blocks_all_increases(runtime):
    engine, broker, _, _, *_ = runtime
    broker.accounts["alex"]["passive_positions"] = {"BTC": {"quantity": 46, "mark": 100}}
    engine.activate(execute=True)
    result = engine.tick()
    assert "symbol:BTC" in result["risk_limit_violations"]
    assert not broker.submitted


def test_passive_exposure_consumes_symbol_budget(runtime):
    engine, broker, _, _, *_ = runtime
    broker.accounts["alex"]["passive_positions"] = {"BTC": {"quantity": 44, "mark": 100}}
    engine.activate(execute=True)
    engine.tick()
    request = broker.submitted[0]["request"]
    assert request["size"] * request["limit_price"] == pytest.approx(60)


@pytest.mark.parametrize("change", ["stop", "forecast", "expiry", "cash", "mark", "config", "external"])
def test_pre_submit_replans_after_final_reads_without_signing(runtime, change):
    engine, broker, ledger, clock, forecasts, path = runtime
    # A small held long becomes a mandatory stop only if marks move during the final read.
    broker.accounts["jeremy"]["positions"]["BTC"].update(quantity=1, entry_price=100)
    engine.activate(execute=True)

    def mutate():
        if change == "stop":
            engine.stopped.set()
        elif change == "forecast":
            forecasts["BTC"][0]["p_not_down"] = .35
        elif change == "expiry":
            clock.now += 901
        elif change == "cash":
            broker.accounts["clearpond"]["available_cash"] = 1000
        elif change == "mark":
            broker.accounts["jeremy"]["positions"]["BTC"]["mark"] = 96
        elif change == "config":
            values = json.loads(path.read_text()); values["poll_seconds"] = 31; path.write_text(json.dumps(values))
        elif change == "external":
            broker.accounts["alex"]["positions"]["BTC"]["quantity"] = -1

    broker.before_submit = mutate
    result = engine.tick()
    assert not broker.submitted
    assert ledger.list_intents()[0]["state"] == "REJECTED"
    assert not ledger.pending()
    if change in {"config", "external"}:
        assert result["state"] == "HALTED"
    elif change == "stop":
        assert result["state"] == "STOPPED"


def test_reconciled_fill_before_crash_is_accepted_on_restart(runtime):
    engine, broker, ledger, clock, _, path = runtime
    engine.activate(execute=True)
    engine.tick()  # Fill committed, but last observation still predates it.
    clock.now += 30
    restarted = PowderRuntime(path, broker=broker, ledger=ledger, clock=clock, forecast_reader=engine.forecast_reader)
    assert restarted.activate(execute=True)["state"] == "RUNNING"
    assert ledger.latest_observation()["accounts"]["clearpond"]["positions"]["BTC"]["quantity"] == 5
    assert len(broker.submitted) == 1


def test_external_inventory_is_not_readopted_after_restart(runtime):
    engine, broker, ledger, clock, _, path = runtime
    engine.activate(execute=True)
    broker.accounts["jeremy"]["positions"]["BTC"].update(quantity=1, entry_price=100)
    restarted = PowderRuntime(path, broker=broker, ledger=ledger, clock=clock, forecast_reader=engine.forecast_reader)
    with pytest.raises(SafetyHalt, match="external position"):
        restarted.activate(execute=True)
    assert ledger.metadata()["baseline"]["accounts"]["jeremy"]["positions"]["BTC"]["quantity"] == 0


def test_explicit_exchange_rejection_is_terminal_without_fabricated_fill(runtime, monkeypatch):
    engine, broker, ledger, _, *_ = runtime
    engine.activate(execute=True)
    monkeypatch.setattr(broker, "submit", lambda intent: {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Rejected by exchange"}]}}})
    engine.tick()
    assert ledger.list_intents()[0]["state"] == "REJECTED"
    assert not ledger.fills()
    assert not ledger.pending()


def test_cli_readiness_failures_return_nonzero_without_traceback_or_secret(tmp_path, monkeypatch, capsys):
    from ml import hyperliquid_powder_runtime as module
    monkeypatch.setattr(module, "_dispatch", lambda args: (_ for _ in ()).throw(ValueError("SECRET_TEST_VALUE")))
    assert main(["--check"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["state"] == "NOT_READY"
    assert "SECRET_TEST_VALUE" not in output


def test_execution_environment_restores_process_gates(monkeypatch):
    from ml.hyperliquid_powder_runtime import _execution_environment
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "false")
    monkeypatch.delenv("HYPERLIQUID_ENABLE_POWDER", raising=False)
    with _execution_environment():
        assert os.environ["HYPERLIQUID_ENABLE_LIVE_ORDERS"] == "true"
        assert os.environ["HYPERLIQUID_ENABLE_POWDER"] == "true"
    assert os.environ["HYPERLIQUID_ENABLE_LIVE_ORDERS"] == "false"
    assert "HYPERLIQUID_ENABLE_POWDER" not in os.environ


def test_dust_stop_does_not_starve_an_executable_reduction(runtime):
    engine, broker, ledger, _, *_ = runtime
    broker.accounts["alex"]["positions"]["BTC"].update(quantity=-.05, entry_price=90)
    broker.accounts["jeremy"]["positions"]["BTC"].update(quantity=20, entry_price=100)
    engine.activate(execute=True)
    engine.tick()
    assert len(broker.submitted) == 1
    assert broker.submitted[0]["request"]["account"] == "jeremy"
    assert broker.submitted[0]["request"]["reduce_only"]
    assert ledger.get_cooldown("alex", "BTC") > engine.clock()


def test_unexecutable_increase_does_not_starve_another_account(runtime, monkeypatch):
    engine, broker, _, _, *_ = runtime
    make = broker.make_order

    def skip_clearpond(account, *args):
        if account == "clearpond":
            raise ValueError("Rounded quantity unavailable")
        return make(account, *args)

    monkeypatch.setattr(broker, "make_order", skip_clearpond)
    engine.activate(execute=True)
    engine.tick()
    assert len(broker.submitted) == 1
    assert broker.submitted[0]["request"]["account"] == "jeremy"


@pytest.mark.parametrize("change", ["stop", "configuration"])
def test_final_forecast_read_cannot_hide_stop_or_configuration_change(runtime, change):
    engine, broker, ledger, _, _, path = runtime
    original_reader = engine.forecast_reader
    calls = 0

    def reader(*args):
        nonlocal calls
        calls += 1
        value = original_reader(*args)
        if calls == 4:  # activation, tick, preparation, then final broker callback
            if change == "stop":
                engine.stopped.set()
            else:
                values = json.loads(path.read_text()); values["poll_seconds"] = 31; path.write_text(json.dumps(values))
        return value

    engine.forecast_reader = reader
    engine.activate(execute=True)
    result = engine.tick()
    assert calls == 4
    assert not broker.submitted
    assert ledger.list_intents()[0]["state"] == "REJECTED"
    assert result["state"] == ("STOPPED" if change == "stop" else "HALTED")
