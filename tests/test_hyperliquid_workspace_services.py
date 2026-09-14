from __future__ import annotations

import pytest
from eth_account import Account

import app.hyperliquid_accounts as accounts
import app.services.hyperliquid as service
from app.config import HyperliquidAccountConfig, hyperliquid_accounts
from app.services.hyperliquid_markets import DEFAULT_MARKETS, market_watch, perpetual_contexts, spot_catalog
from app.services.hyperliquid_trading import HyperliquidOrderTicket, HyperliquidTradingConfig

OWNER = "0x" + "aa" * 20
AGENT = "0x" + "bb" * 20
PROFILE = accounts.HYPERLIQUID_ACCOUNT_PROFILES["clearpond"]
SPOT_META = [{"tokens": [{"index": 0, "name": "USDC"}, {"index": 150, "name": "HYPE"}, {"index": 200, "name": "UBTC"}], "universe": [{"index": 107, "tokens": [150, 0]}, {"index": 142, "tokens": [200, 0]}]}, [{"coin": "@107", "midPx": "79"}, {"coin": "@142", "midPx": "112000"}]]


@pytest.fixture(autouse=True)
def isolated_accounts(monkeypatch):
    for p in accounts.HYPERLIQUID_ACCOUNT_PROFILES.values():
        for key in (*p.wallet_address_env_keys, *p.api_address_env_keys, *p.api_secret_env_keys):
            monkeypatch.delenv(key, raising=False)
    accounts._wallet_cache.clear()


class RoleClient:
    info_url = "https://example.invalid/info"

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post_info(self, payload):
        self.calls.append(payload)
        return self.response


def test_agent_resolves_to_owner_and_cache_is_scoped_to_network(monkeypatch):
    monkeypatch.setenv("HYPE_API_CLEARPOND_WALLET", AGENT)
    client = RoleClient({"role": "agent", "data": {"user": OWNER}})
    assert accounts.resolve_portfolio_wallet(PROFILE, client) == OWNER
    assert accounts.resolve_portfolio_wallet(PROFILE, client) == OWNER
    assert client.calls == [{"type": "userRole", "user": AGENT}]
    client.info_url = "https://other.invalid/info"
    client.response = {"role": "missing"}
    with pytest.raises(ValueError, match="could not be verified"):
        accounts.resolve_portfolio_wallet(PROFILE, client)
    assert len(client.calls) == 2


def test_explicit_owner_wins_without_network_and_config_includes_all_three(monkeypatch):
    monkeypatch.setenv("HYPE_WALLET_ADDRESS_CLEARPOND", OWNER)
    client = RoleClient(None)
    assert accounts.resolve_portfolio_wallet(PROFILE, client) == OWNER
    assert not client.calls
    assert [a.label for a in hyperliquid_accounts()] == ["Jeremy", "Alex", "Clearpond"]


@pytest.mark.parametrize("response", [None, {}, {"role": "missing"}, {"role": "agent", "data": {}}, {"role": "agent", "data": {"user": "0xwrong"}}])
def test_unverified_api_owner_never_falls_back_to_agent_wallet(monkeypatch, response):
    monkeypatch.setenv("HYPE_API_CLEARPOND_WALLET", AGENT)
    with pytest.raises(ValueError, match="could not be verified"):
        accounts.resolve_portfolio_wallet(PROFILE, RoleClient(response))


def test_clearpond_signing_identity_and_existing_execution_gates(monkeypatch):
    # Synthetic test signer. Real .env values are removed by the fixture.
    secret = "11" * 32
    signer = Account.from_key(secret).address
    monkeypatch.setenv("HYPE_WALLET_ADDRESS_CLEARPOND", OWNER)
    monkeypatch.setenv("HYPE_API_CLEARPOND_WALLET", signer)
    monkeypatch.setenv("HYPE_API_CLEARPOND_PRIVATE", secret)
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "false")
    with pytest.raises(PermissionError, match="ENABLE_LIVE_ORDERS"):
        HyperliquidTradingConfig("clearpond").validate_for_live_action()
    monkeypatch.setenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "true")
    monkeypatch.setenv("HYPERLIQUID_MAX_LIVE_ORDER_DOLLARS", "500")
    config = HyperliquidTradingConfig("clearpond")
    config.validate_for_live_order(HyperliquidOrderTicket("BTC", True, .001, 100000, "Gtc"))
    assert config.wallet_address == OWNER
    with pytest.raises(PermissionError, match="exceeds"):
        config.validate_for_live_order(HyperliquidOrderTicket("BTC", True, 1, 100000, "Gtc"))
    monkeypatch.setenv("HYPE_API_CLEARPOND_WALLET", AGENT)
    with pytest.raises(ValueError, match="does not match"):
        HyperliquidTradingConfig("clearpond").validate_for_live_action()


class PortfolioClient:
    def __init__(self, mode):
        self.mode = mode

    def post_info(self, payload):
        assert payload["user"] == OWNER
        kind = payload["type"]
        if kind == "clearinghouseState":
            return {"withdrawable": "100", "marginSummary": {"accountValue": "130", "totalMarginUsed": "30"}, "assetPositions": [{"position": {"coin": "HYPE", "szi": "2", "positionValue": "180", "entryPx": "80", "unrealizedPnl": "20"}}]}
        if kind == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "token": 0, "total": "511.4", "hold": "50"}], "tokenToAvailableAfterMaintenance": [[0, "400"]]}
        if kind == "userAbstraction":
            return self.mode
        if kind == "frontendOpenOrders":
            return []
        if kind == "userFills":
            return [{"coin": "HYPE", "time": i} for i in reversed(range(60))]
        raise AssertionError(kind)


@pytest.mark.parametrize("mode, expected", [("unifiedAccount", 511.4), ("portfolioMargin", 511.4), ("disabled", 641.4)])
def test_balance_modes_do_not_add_notional_or_duplicate_pnl(mode, expected):
    snapshot = service._sync_hyperliquid_portfolio_with_market(HyperliquidAccountConfig("Clearpond", OWNER), PortfolioClient(mode), all_mids={"HYPE": "90"}, spot_meta_and_asset_ctxs=SPOT_META, hype_market={}, chain_status={})
    assert snapshot.total_value == expected
    assert snapshot.account_facts["unrealized_pnl"] == 20
    assert snapshot.account_facts["spot_available"]["USDC"] == 400
    assert snapshot.holdings[0].price == 90  # entry price is 80
    assert [row["time"] for row in snapshot.account_facts["activity"]] == list(reversed(range(10, 60)))
    if mode != "disabled":
        assert len(snapshot.cash) == 1
        assert snapshot.account_facts["available"] == 400
    else:
        assert snapshot.cash[1].amount == 110  # collateral, not negative notional residual


def test_spot_valuation_uses_pair_index_and_never_the_perpetual_quote():
    route = spot_catalog(SPOT_META)
    assert route["BTC"]["pair"] == "UBTC/USDC"
    assert route["BTC"]["coin"] == "@142"
    assert service._spot_price("HYPE", {"token": 150}, {"HYPE": "900", "@150": "700", "@107": "80"}, SPOT_META) == 80


def test_one_missing_account_does_not_hide_other_accounts_or_markets(monkeypatch):
    class Client(PortfolioClient):
        def __init__(self, **kwargs):
            super().__init__("unifiedAccount")

        def post_info(self, payload):
            if payload["type"] == "allMids":
                return {"HYPE": "90"}
            if payload["type"] == "spotMetaAndAssetCtxs":
                return SPOT_META
            return super().post_info(payload)
    monkeypatch.setattr(service, "HyperliquidInfoClient", Client)
    monkeypatch.setattr(service, "market_watch", lambda *args: {"market_catalog": list(DEFAULT_MARKETS)})
    monkeypatch.setattr(service.HyperEvmRpcClient, "chain_status", lambda *args: {"available": False})
    monkeypatch.setattr(service, "hyperliquid_accounts", lambda: [HyperliquidAccountConfig("Jeremy", OWNER), HyperliquidAccountConfig("Alex", OWNER), HyperliquidAccountConfig("Clearpond", "", "clearpond")])
    result = service.sync_hyperliquid_portfolios()
    assert len(result) == 3
    assert not result[0].account_facts["sync_error"]
    assert not result[1].account_facts["sync_error"]
    assert result[2].account_facts["sync_error"]
    assert result[2].account_facts["market_catalog"] == list(DEFAULT_MARKETS)


def test_four_market_prices_survive_one_chart_failure_and_validate_alignment():
    metadata = [{"universe": [{"name": c, "szDecimals": 3} for c in DEFAULT_MARKETS]}, [{"markPx": str(i + 100), "prevDayPx": "100"} for i in range(4)]]
    class Client:
        def post_info(self, payload):
            if payload["type"] == "metaAndAssetCtxs":
                return metadata
            if payload["req"]["coin"] == "ETH":
                raise RuntimeError("chart failed")
            return [{"c": "1"}, {"c": "nan"}, {"c": "2"}]
    result = market_watch(Client())["markets"]
    assert all(result[c]["status"] == "current" for c in DEFAULT_MARKETS)
    assert result["ETH"]["chart_status"] == "unavailable"
    assert result["BTC"]["closes_24h"] == [1, 2]
    assert result["ZEC"]["change_percent_24h"] == pytest.approx(3)
    with pytest.raises(ValueError, match="align"):
        perpetual_contexts([metadata[0], metadata[1][:-1]])
