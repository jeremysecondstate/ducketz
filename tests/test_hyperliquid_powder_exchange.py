"""Fake-only contracts from the official API pages cited by the broker module."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ml.hyperliquid_powder_exchange import PowderExchange, PreSubmitRejected, MAINNET, ACCOUNTS
import ml.hyperliquid_powder_lock as locks

NOW = 1800000000.0
OWNERS = {a: "0x" + str(i) * 40 for i, a in enumerate(ACCOUNTS, 1)}
AGENTS = {a: "0x" + str(i) * 40 for i, a in enumerate(ACCOUNTS, 4)}


class Client:
    info_url = MAINNET + "/info"

    def __init__(self):
        self.mode = "unifiedAccount"
        self.orders = []
        self.inventory = {a: [] for a in ACCOUNTS}
        self.perp_equity = {a: "1250" for a in ACCOUNTS}
        self.balances = {a: [{"coin": "USDC", "token": 0, "total": "1000", "hold": "10"}] for a in ACCOUNTS}
        self.status = {"status": "unknownOid"}
        self.fills = []
        self.maintenance = [[0, "800"]]
        self.roles = {**{agent: {"role": "agent", "data": {"user": OWNERS[a]}} for a, agent in AGENTS.items()},
                      **{owner: {"role": "user"} for owner in OWNERS.values()}}
        self.calls = []

    def post_info(self, payload):
        self.calls.append(deepcopy(payload))
        kind = payload["type"]
        if kind == "userRole":
            return deepcopy(self.roles[payload["user"]])
        if kind == "orderStatus":
            return deepcopy(self.status)
        if kind == "userFillsByTime":
            assert payload["aggregateByTime"] is False
            return deepcopy(self.fills)
        account = next(a for a, owner in OWNERS.items() if owner == payload["user"])
        if kind == "clearinghouseState":
            return {"assetPositions": deepcopy(self.inventory[account]), "marginSummary": {"accountValue": self.perp_equity[account]}, "withdrawable": "600"}
        if kind == "spotClearinghouseState":
            return {"balances": deepcopy(self.balances[account]), "tokenToAvailableAfterMaintenance": deepcopy(self.maintenance)}
        if kind == "userAbstraction":
            return self.mode
        if kind == "frontendOpenOrders":
            return deepcopy(self.orders)
        raise AssertionError(kind)


class Market:
    info_url = MAINNET + "/info"

    def __init__(self):
        self.markets = {f"{kind}:BTC": {"coin": "BTC", "kind": kind, "wire_coin": "BTC" if kind == "perp" else "@1",
                       "mark": 100.12345, "sz_decimals": 3,
                       "book_time_utc": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
                       "bids": [[100.12, 100]], "asks": [[100.13, 100]]} for kind in ("perp", "spot")}

    def snapshot(self, symbols):
        return {"markets": deepcopy(self.markets), "errors": {}}


@pytest.fixture
def broker(monkeypatch, tmp_path):
    # Tests must never inherit session gates or touch the configured lock root.
    monkeypatch.delenv("HYPERLIQUID_INFO_URL", raising=False)
    monkeypatch.delenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("HYPERLIQUID_ENABLE_POWDER", raising=False)
    monkeypatch.setattr(locks, "LOCK_ROOT", tmp_path / "locks")
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("Real network request"))
    client, market = Client(), Market()
    configs = {a: SimpleNamespace(api_address=AGENTS[a], api_secret="synthetic:" + a,
                                  wallet_address=OWNERS[a], max_live_notional=500) for a in ACCOUNTS}
    exchange = SimpleNamespace(base_url=MAINNET, vault_address=None, orders=[])

    def order(*args, **kwargs):
        exchange.orders.append((args, kwargs))
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 9}}]}}}

    exchange.order = order
    view = PowderExchange(["BTC"], client=client, market=market, clock=lambda: NOW,
                          config_factory=configs.__getitem__, signer=lambda secret: AGENTS[secret.split(":")[1]],
                          exchange_factory=lambda *_: exchange)
    view.pre_submit_check = lambda request, actual: None
    view.test_client, view.test_market, view.test_configs, view.test_exchange = client, market, configs, exchange
    return view


def intent(broker, account="jeremy", delta=100):
    obs = broker.observe()
    request = broker.make_order(account, "BTC", delta, False, obs)
    request["forecast_valid_until"] = NOW + 30
    return {"request": request, "created_at": NOW, "cloid": "0x" + "a" * 32}


def status_for(request, cloid, state="filled", remaining=0):
    return {"status": "order", "order": {"status": state, "order": {
        "oid": 9, "coin": request["coin"], "side": "B" if request["is_buy"] else "A",
        "origSz": str(request["size"]), "sz": str(remaining), "cloid": cloid}}}


def fill_for(request, quantity=None, tid=1):
    return {"oid": 9, "tid": tid, "coin": request["coin"], "side": "B" if request["is_buy"] else "A",
            "sz": str(quantity or request["size"]), "px": str(request["limit_price"]),
            "time": int(NOW * 1000), "fee": "0.015", "feeToken": "USDC", "hash": "fixture"}


def test_constructor_has_no_io(broker):
    assert broker.test_client.calls == []
    assert broker.network == MAINNET


def test_preflight_checks_explicit_owner_authority_and_uniqueness(broker):
    assert broker.preflight() == OWNERS
    assert len(broker.test_client.calls) == 6
    broker.test_configs["alex"].wallet_address = OWNERS["jeremy"]
    with pytest.raises(PermissionError, match="differs"):
        broker.preflight()


@pytest.mark.parametrize("role", ["subAccount", "vault", "missing"])
def test_subaccounts_and_vaults_are_blocked(broker, role):
    broker.test_client.roles[OWNERS["alex"]] = {"role": role}
    with pytest.raises(PermissionError, match="unsupported"):
        broker.preflight()


def test_identity_cannot_change_after_binding(broker):
    broker.preflight()
    new = "0x" + "9" * 40
    broker.test_configs["alex"].wallet_address = new
    broker.test_client.roles[AGENTS["alex"]]["data"]["user"] = new
    broker.test_client.roles[new] = {"role": "user"}
    with pytest.raises(PermissionError, match="changed"):
        broker.preflight()


def test_same_owner_and_different_network_block(broker, monkeypatch):
    broker.test_configs["alex"].wallet_address = OWNERS["jeremy"]
    broker.test_client.roles[AGENTS["alex"]]["data"]["user"] = OWNERS["jeremy"]
    with pytest.raises(PermissionError, match="distinct"):
        broker.preflight()
    monkeypatch.setenv("HYPERLIQUID_INFO_URL", "https://api.hyperliquid-testnet.xyz/info")
    with pytest.raises(PermissionError, match="mainnet"):
        broker.preflight()


def test_unified_and_separate_equity_and_passive_spot(broker):
    broker.test_client.balances["alex"].append({"coin": "UBTC", "total": ".01", "hold": "0", "token": 1})
    obs = broker.observe()
    assert obs["accounts"]["alex"]["equity"] == pytest.approx(1001.0012345)
    assert obs["accounts"]["alex"]["positions"]["BTC"]["quantity"] == 0
    assert obs["accounts"]["alex"]["passive_positions"]["BTC"]["quantity"] == .01
    assert obs["accounts"]["alex"]["gross"] == pytest.approx(1.0012345)
    assert obs["accounts"]["jeremy"]["available_cash"] == 800
    broker.test_client.mode = "default"
    assert broker.observe()["accounts"]["jeremy"]["equity"] == 2250


def test_complete_empty_spot_balance_is_zero_for_perp_only_legacy_account(broker):
    broker.test_client.mode = "default"
    broker.test_client.balances = {a: [] for a in ACCOUNTS}
    broker.test_client.perp_equity["clearpond"] = "0"
    obs = broker.observe()
    assert obs["accounts"]["jeremy"]["equity"] == 1250
    assert obs["accounts"]["jeremy"]["available_cash"] == 600
    assert obs["accounts"]["clearpond"]["equity"] == 0
    assert obs["accounts"]["clearpond"]["available_cash"] == 0
    broker.test_client.mode = "unifiedAccount"
    assert broker.observe()["accounts"]["jeremy"]["available_cash"] == 0
    broker.test_client.balances["clearpond"] = None
    with pytest.raises(ValueError, match="spot balances"):
        broker.observe()


@pytest.mark.parametrize("damage", ["role", "asset", "orders", "marks", "balance", "abstraction", "stale"])
def test_observation_fails_closed(broker, damage):
    if damage in {"role", "asset"}:
        broker.test_client.inventory["alex"] = [{"position": {"coin": "BTC" if damage == "role" else "DOGE", "szi": "1" if damage == "role" else "-1"}}]
    elif damage == "orders":
        broker.test_client.orders = [{"oid": 1}]
    elif damage == "marks":
        del broker.test_market.markets["spot:BTC"]
    elif damage == "balance":
        broker.test_client.balances["alex"][0]["total"] = "nan"
    elif damage == "abstraction":
        broker.test_client.mode = "portfolioMargin"
    else:
        broker.test_market.markets["perp:BTC"]["book_time_utc"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises((ValueError, PermissionError)):
        broker.observe()


@pytest.mark.parametrize("cap", [float("nan"), float("inf"), 0, -1, True])
def test_order_caps_are_finite_and_positive(cap):
    with pytest.raises(ValueError):
        PowderExchange(["BTC"], max_order_notional=cap)


def test_sizing_caps_all_orders_and_obeys_precision(broker):
    obs = broker.observe()
    request = broker.make_order("jeremy", "BTC", 2000, False, obs)
    assert request["size"] * request["limit_price"] <= 500
    assert Decimal(str(request["size"])) % Decimal(".001") == 0
    assert Decimal(str(request["limit_price"])) % Decimal(".01") == 0
    obs["accounts"]["jeremy"]["positions"]["BTC"]["quantity"] = 20
    reduced = broker.make_order("jeremy", "BTC", -2000, True, obs)
    assert reduced["size"] * reduced["limit_price"] <= 500
    assert reduced["reduce_only"] is True
    with pytest.raises(ValueError, match="minimum"):
        broker.make_order("jeremy", "BTC", .01, False, obs)


@pytest.mark.parametrize("cap", [float("nan"), float("inf"), 0, -1, True])
def test_environment_cap_is_also_strict(broker, cap):
    broker.test_configs["jeremy"].max_live_notional = cap
    with pytest.raises(ValueError, match="environment order cap"):
        broker.preflight()


def test_effective_cap_covers_reductions_and_increase_cash_budget(broker):
    broker.test_configs["jeremy"].max_live_notional = 50
    obs = broker.observe()
    request = broker.make_order("jeremy", "BTC", 2000, False, obs)
    assert request["size"] * request["limit_price"] <= 50
    obs["accounts"]["jeremy"]["positions"]["BTC"]["quantity"] = 20
    request = broker.make_order("jeremy", "BTC", -2000, True, obs)
    assert request["size"] * request["limit_price"] <= 50
    broker.test_configs["jeremy"].max_live_notional = 500
    broker._markets["perp:BTC"]["asks"] = [[120, 100]]
    request = broker.make_order("jeremy", "BTC", 100, False, obs)
    assert request["size"] * request["limit_price"] <= 100


@pytest.mark.parametrize("change", ["quantity", "passive", "orders", "quote", "cap", "delay", "gate", "forecast"])
def test_submit_revalidates_after_sdk_metadata_without_signing(broker, monkeypatch, change):
    proposed = intent(broker)
    monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")

    def construct(*args):
        if change == "quantity":
            broker.test_client.inventory["jeremy"] = [{"position": {"coin": "BTC", "szi": "1", "entryPx": "100", "unrealizedPnl": "0"}}]
        elif change == "passive":
            broker.test_client.balances["alex"].append({"coin": "UBTC", "total": ".01", "hold": "0", "token": 1})
        elif change == "orders":
            broker.test_client.orders = [{"oid": 12}]
        elif change == "quote":
            broker.test_market.markets["perp:BTC"]["asks"] = [[80, 100]]
        elif change == "cap":
            broker.test_configs["jeremy"].max_live_notional = 20
        elif change == "delay":
            broker.clock = lambda: NOW + 46
        elif change == "gate":
            monkeypatch.delenv("HYPERLIQUID_ENABLE_LIVE_ORDERS")
        else:
            broker.clock = lambda: NOW + 30
        return broker.test_exchange

    broker.exchange_factory = construct
    with locks.account_ownership() as token:
        broker.ownership_token = token
        with pytest.raises(PreSubmitRejected):
            broker.submit(proposed)
    assert broker.test_exchange.orders == []
    assert proposed["cloid"] not in broker._submitted


def test_adverse_marks_and_cash_decline_allow_policy_approved_perp_reduction(broker, monkeypatch):
    broker.test_client.inventory["jeremy"] = [{"position": {"coin": "BTC", "szi": "1", "entryPx": "100", "unrealizedPnl": "0"}}]
    observed = broker.observe()
    request = broker.make_order("jeremy", "BTC", -50, True, observed)
    proposed = {"request": request, "created_at": NOW, "cloid": "0x" + "a" * 32}
    approved = []

    def construct(*args):
        broker.test_client.balances["jeremy"][0]["total"] = "500"
        broker.test_client.inventory["jeremy"][0]["position"]["unrealizedPnl"] = "-10"
        broker.test_market.markets["perp:BTC"].update(mark=90, bids=[[90, 100]], asks=[[90.01, 100]])
        return broker.test_exchange

    def policy_check(order, actual):
        assert order["reduce_only"] is True
        assert actual["accounts"]["jeremy"]["available_cash"] == 490
        assert actual["accounts"]["jeremy"]["positions"]["BTC"]["mark"] == 90
        approved.append(True)

    broker.exchange_factory, broker.pre_submit_check = construct, policy_check
    monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
    with locks.account_ownership() as token:
        broker.ownership_token = token
        assert broker.submit(proposed)["status"] == "ok"
    assert approved == [True]
    assert len(broker.test_exchange.orders) == 1


def test_fresh_policy_rejects_increase_when_cash_becomes_insufficient(broker, monkeypatch):
    proposed = intent(broker)
    checked = []

    def construct(*args):
        broker.test_client.balances["jeremy"][0]["total"] = "50"
        return broker.test_exchange

    def policy_check(order, actual):
        checked.append(True)
        if actual["accounts"]["jeremy"]["available_cash"] < order["size"] * order["limit_price"]:
            raise PermissionError("Fresh cash is insufficient")

    broker.exchange_factory, broker.pre_submit_check = construct, policy_check
    monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
    with locks.account_ownership() as token:
        broker.ownership_token = token
        with pytest.raises(PreSubmitRejected, match="Fresh cash"):
            broker.submit(proposed)
    assert checked == [True]
    assert broker.test_exchange.orders == []


@pytest.mark.parametrize("fresh_available", [.1, .7])
def test_spot_reduction_uses_current_available_size_not_prior_collateral(broker, monkeypatch, fresh_available):
    broker.test_client.balances["clearpond"].append({"coin": "UBTC", "total": "2", "hold": "0", "token": 1})
    broker.test_client.maintenance.append([1, "1"])
    request = broker.make_order("clearpond", "BTC", -50, False, broker.observe())
    proposed = {"request": request, "created_at": NOW, "cloid": "0x" + "a" * 32}

    def construct(*args):
        broker.test_client.maintenance[1] = [1, str(fresh_available)]
        return broker.test_exchange

    broker.exchange_factory = construct
    monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
    with locks.account_ownership() as token:
        broker.ownership_token = token
        if fresh_available < request["size"]:
            with pytest.raises(PreSubmitRejected, match="actual available quantity"):
                broker.submit(proposed)
            assert broker.test_exchange.orders == []
        else:
            assert broker.submit(proposed)["status"] == "ok"
            assert len(broker.test_exchange.orders) == 1


def test_observation_age_starts_before_account_reads(broker):
    ticks = iter([NOW, NOW + 10])
    broker.clock = lambda: next(ticks)
    obs = broker.observe()
    assert obs["observed_at"] == obs["started_at"] == NOW
    assert obs["completed_at"] == NOW + 10


def test_managed_spot_sell_is_bounded_by_unified_base_maintenance(broker):
    broker.test_client.balances["clearpond"].append({"coin": "UBTC", "total": "2", "hold": ".1", "token": 1})
    with pytest.raises(ValueError, match="managed token maintenance"):
        broker.observe()
    broker.test_client.maintenance.append([1, ".2"])
    obs = broker.observe()
    assert obs["accounts"]["clearpond"]["positions"]["BTC"]["available_quantity"] == .2
    assert broker.make_order("clearpond", "BTC", -200, False, obs)["size"] == .2


def test_increase_requires_forecast_deadline(broker, monkeypatch):
    proposed = intent(broker)
    del proposed["request"]["forecast_valid_until"]
    monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
    with locks.account_ownership() as token:
        broker.ownership_token = token
        with pytest.raises(PreSubmitRejected, match="forecast deadline"):
            broker.submit(proposed)
    assert broker.test_exchange.orders == []


@pytest.mark.parametrize("callback", ["missing", "reject", "expired"])
def test_runtime_policy_callback_is_mandatory_and_followed_by_age_check(broker, monkeypatch, callback):
    proposed = intent(broker)
    monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
    if callback == "missing":
        broker.pre_submit_check = None
    elif callback == "reject":
        broker.pre_submit_check = lambda *args: (_ for _ in ()).throw(ValueError("fresh policy disallows increase"))
    else:
        broker.pre_submit_check = lambda *args: setattr(broker, "clock", lambda: NOW + 46)
    with locks.account_ownership() as token:
        broker.ownership_token = token
        with pytest.raises(PreSubmitRejected):
            broker.submit(proposed)
    assert broker.test_exchange.orders == []


def test_submission_requires_both_gates_ownership_and_no_repeat(broker, monkeypatch):
    proposed = intent(broker)
    with pytest.raises(PermissionError, match="ownership"):
        broker.submit(proposed)
    with locks.account_ownership() as token:
        broker.ownership_token = token
        with pytest.raises(PermissionError, match="gates"):
            broker.submit(proposed)
        monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
        monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
        ack = broker.submit(proposed)
        assert ack["status"] == "ok"
        assert len(broker.test_exchange.orders) == 1
        with pytest.raises(PermissionError, match="already attempted"):
            broker.submit(proposed)
        with pytest.raises(PermissionError, match="Manual trading"):
            with locks.manual_action_guard("jeremy"):
                pytest.fail("Manual mutation entered")
    with pytest.raises(PermissionError):
        locks.assert_ownership(token, "jeremy")


def test_transport_timeout_is_never_retried(broker, monkeypatch):
    proposed = intent(broker)
    monkeypatch.setenv("HYPERLIQUID_ENABLE_POWDER", "true")
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
    broker.test_exchange.order = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("fake transport"))
    with locks.account_ownership() as token:
        broker.ownership_token = token
        with pytest.raises(TimeoutError):
            broker.submit(proposed)
        with pytest.raises(PermissionError, match="already attempted"):
            broker.submit(proposed)


def test_full_fill_requires_real_fill_evidence(broker):
    proposed = intent(broker)
    broker.test_client.status = status_for(proposed["request"], proposed["cloid"])
    assert broker.reconcile(proposed)["state"] == "UNKNOWN"
    broker.test_client.fills = [fill_for(proposed["request"])]
    result = broker.reconcile(proposed)
    assert result["state"] == "FILLED"
    assert result["fills"][0]["quantity"] == proposed["request"]["size"]
    assert result["fills"][0]["fee"] == .015


def test_reconciliation_clock_skew_and_lowered_current_cap(broker):
    proposed = intent(broker)
    broker.test_client.status = status_for(proposed["request"], proposed["cloid"])
    fill = fill_for(proposed["request"])
    fill["time"] -= 4000
    broker.test_client.fills = [fill]
    broker.test_configs["jeremy"].max_live_notional = 20
    assert broker.reconcile(proposed)["state"] == "FILLED"
    query = next(call for call in broker.test_client.calls if call["type"] == "userFillsByTime")
    assert query["startTime"] == int((NOW - 5) * 1000)


def test_partial_ioc_canceled_remainder_and_duplicate_fills(broker):
    proposed = intent(broker, "alex", -100)
    request = proposed["request"]
    amount = .2
    broker.test_client.status = status_for(request, proposed["cloid"], "canceled", request["size"] - amount)
    fill = fill_for(request, amount)
    broker.test_client.fills = [fill, deepcopy(fill)]
    result = broker.reconcile(proposed)
    assert result["state"] == "CANCELED"
    assert len(result["fills"]) == 1
    assert result["fills"][0]["quantity"] == -.2
    broker.test_client.fills = []
    assert broker.reconcile(proposed)["state"] == "UNKNOWN"


@pytest.mark.parametrize("damage", ["unknown", "truncated", "identity", "price", "time", "fee", "duplicate", "size"])
def test_unproven_results_stay_unknown(broker, damage):
    proposed = intent(broker)
    broker.test_client.status = status_for(proposed["request"], proposed["cloid"])
    fill = fill_for(proposed["request"])
    broker.test_client.fills = [fill]
    if damage == "unknown":
        broker.test_client.status = {"status": "unknownOid"}
    elif damage == "truncated":
        broker.test_client.fills = [fill] * 500
    elif damage == "identity":
        broker.test_client.status["order"]["order"]["coin"] = "ETH"
    elif damage == "price":
        fill["px"] = "999999"
    elif damage == "time":
        fill["time"] -= 6000
    elif damage == "fee":
        del fill["fee"]
    elif damage == "duplicate":
        broker.test_client.fills.append({**fill, "fee": "100"})
    else:
        fill["sz"] = "9999"
    assert broker.reconcile(proposed)["state"] == "UNKNOWN"


@pytest.mark.parametrize("method", ["submit", "modify_order", "cancel"])
def test_manual_signed_methods_hold_shared_guard(broker, monkeypatch, method):
    from app.services.hyperliquid_trading import HyperliquidExecutionAdapter, HyperliquidOrderTicket
    adapter = HyperliquidExecutionAdapter("alex")
    config = SimpleNamespace(validate_for_live_order=lambda _: None, validate_for_live_action=lambda: None)
    monkeypatch.setattr(adapter, "config", lambda: config)

    def signed(*args):
        with pytest.raises(PermissionError):
            with locks.account_ownership():
                pytest.fail("Powder entered during manual action")
        return "fake signed result"

    monkeypatch.setattr(adapter, "_local_signed_submit", signed)
    monkeypatch.setattr(adapter, "_local_signed_modify", signed)
    monkeypatch.setattr(adapter, "_local_signed_cancel", signed)
    ticket = HyperliquidOrderTicket("BTC", False, 1, 100, "Ioc")
    arguments = (ticket,) if method == "submit" else (9, ticket) if method == "modify_order" else ("BTC", 9)
    assert getattr(adapter, method)(*arguments) == "fake signed result"
