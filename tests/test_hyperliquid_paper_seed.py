"""Read-only bootstrap using mocked public responses; no real accounts or IO."""
from copy import deepcopy

import pandas as pd
import pytest

from app.hyperliquid_accounts import HYPERLIQUID_ACCOUNT_PROFILES
from ml.hyperliquid_paper_ledger import PaperLedger
from ml.hyperliquid_paper_seed import AccountReader, READ_TYPES, mirror_accounts


NOW = pd.Timestamp("2026-09-25T12:00:00Z").timestamp()
OWNERS = {name: "0x" + str(index) * 40 for index, name in enumerate(("alex", "jeremy", "clearpond"), 1)}
AGENTS = {name: "0x" + str(index) * 40 for index, name in enumerate(("alex", "jeremy", "clearpond"), 4)}


def data():
    def account(mode, equity, cash, positions=(), spots=()):
        return {
            "userAbstraction": mode,
            "clearinghouseState": {"marginSummary": {"accountValue": str(equity)}, "assetPositions": list(positions)},
            "spotClearinghouseState": {"balances": [{"coin": "USDC", "total": str(cash)}, *spots]},
            "frontendOpenOrders": [{"oid": 1}],
        }
    return {
        "alex": account("unifiedAccount", 900, 900, [{"position": {
            "coin": "BTC", "szi": "-2", "entryPx": "50", "unrealizedPnl": "-100", "leverage": {"value": 10},
        }}], [{"coin": "HYPE", "total": "0.001"}]),
        "jeremy": account("default", 1200, 200, [{"position": {
            "coin": "BTC", "szi": "10", "entryPx": "80", "unrealizedPnl": "200",
        }}]),
        "clearpond": account("default", 0, 300, spots=[{"coin": "UBTC", "total": "2"}]),
    }


class Reader:
    def __init__(self, snapshots=None):
        self.snapshots = snapshots or data()
        self.calls = []
        self.roles = {AGENTS[name]: {"role": "agent", "data": {"user": OWNERS[name]}} for name in OWNERS}

    def post_info(self, payload):
        self.calls.append(deepcopy(payload))
        assert set(payload) == {"type", "user"}
        assert payload["type"] in READ_TYPES
        if payload["type"] == "userRole":
            return deepcopy(self.roles[payload["user"]])
        lookup = {**{owner: name for name, owner in OWNERS.items()}, **{agent: name for name, agent in AGENTS.items()}}
        return deepcopy(self.snapshots[lookup[payload["user"]]][payload["type"]])


class Markets:
    def __init__(self):
        self.calls = []
        self.omit = set()
        self.override = {}

    def snapshot(self, symbols):
        self.calls.append(list(symbols))
        markets = {f"{kind}:{coin}": {"mark": 20.0 if coin == "HYPE" else 100.0}
                   for coin in symbols for kind in ("spot", "perp") if f"{kind}:{coin}" not in self.omit}
        for key, value in self.override.items():
            markets[key] = {"mark": value}
        return {"markets": markets, "errors": {}, "observed_at_utc": pd.Timestamp(NOW, unit="s", tz="UTC").isoformat()}


def values(use_agents=False):
    return {(profile.api_address_env_keys[0] if use_agents else profile.wallet_address_env_keys[0]):
            AGENTS[name] if use_agents else OWNERS[name]
            for name, profile in HYPERLIQUID_ACCOUNT_PROFILES.items()}


def mirror(*, reader=None, markets=None, env=None, clock=lambda: NOW):
    return mirror_accounts(markets or Markets(), ["BTC"], reader=reader or Reader(),
                           values=values() if env is None else env, clock=clock)


def test_unified_cash_avoids_pnl_double_count_and_standard_keeps_both_wallets(tmp_path):
    result = mirror()
    assert result["initial_cash"] == {"alex": 1000, "jeremy": 1200, "clearpond": 300}
    result.pop("quotes")
    with PaperLedger(tmp_path / "ledger.sqlite", **result) as ledger:
        state = ledger.state(result["initial_marks"])
        assert state["accounts"]["alex"]["equity"] == pytest.approx(900.02)
        assert state["accounts"]["jeremy"]["equity"] == 1400
        assert state["accounts"]["clearpond"]["equity"] == 500
        assert state["pooled"]["equity"] == pytest.approx(2800.02)
        assert state["pooled"]["total_pnl"] == 0
        assert ledger.history("fills") == []


def test_portfolio_margin_uses_unified_balance_math():
    source = data()
    source["alex"]["userAbstraction"] = "portfolioMargin"
    assert mirror(reader=Reader(source))["initial_cash"]["alex"] == 1000


def test_preserves_passive_spot_dust_and_marks_spot_basis_as_opening_value():
    market = Markets()
    result = mirror(markets=market)
    assert market.calls == [["BTC", "HYPE"]]
    dust = next(p for p in result["initial_positions"] if p["account"] == "alex" and p["kind"] == "spot")
    assert dust["quantity"] == 0.001
    assert dust["passive_inherited"] is True
    assert dust["average_entry"] == 20
    assert dust["entry_source"] == "opening_mark_not_historical_cost"
    assert result["metadata"]["accounts"]["alex"]["open_orders_not_imported"] == 1
    assert result["metadata"]["live_updates_after_seed"] is False


def test_mirror_keeps_historical_basis_with_separate_opening_stop_reference():
    result = mirror()
    positions = {(p["account"], p["kind"]): p for p in result["initial_positions"]}
    short = positions["alex", "perp"]
    long = positions["jeremy", "perp"]
    assert (short["average_entry"], long["average_entry"]) == (50, 80)
    assert short["entry_source"] == long["entry_source"] == "historical_exchange_entry"
    assert short["risk_reference_price"] == long["risk_reference_price"] == 100
    assert all(p["risk_reference_source"] == "opening_mark" for p in positions.values())
    assert result["metadata"]["inherited_stop_reference"] == "opening_mark"
    # A new risk origin changes neither collateral nor the mirrored valuation.
    assert result["initial_cash"] == {"alex": 1000, "jeremy": 1200, "clearpond": 300}


def test_snapshot_retains_each_public_read_window_and_quote_provenance():
    import itertools
    import json
    import threading
    counter = itertools.count()
    lock = threading.Lock()
    def clock():
        with lock:
            return NOW + next(counter) / 1000
    reader = Reader()
    reader.snapshots["alex"]["clearinghouseState"]["time"] = int(NOW * 1000)
    result = mirror(reader=reader, env=values(use_agents=True), clock=clock)
    metadata = result["metadata"]
    start = pd.Timestamp(metadata["snapshot_started_at_utc"])
    finish = pd.Timestamp(metadata["snapshot_completed_at_utc"])
    quotes = metadata["quote_observation"]
    for account in metadata["accounts"].values():
        reads = account["read_observations"]
        assert set(reads) == READ_TYPES
        for read in reads.values():
            assert start <= pd.Timestamp(read["started_at_utc"]) < pd.Timestamp(read["completed_at_utc"]) < finish
            assert pd.Timestamp(read["completed_at_utc"]) < pd.Timestamp(quotes["started_at_utc"])
    assert metadata["accounts"]["alex"]["read_observations"]["clearinghouseState"]["exchange_time_ms"] == int(NOW * 1000)
    assert pd.Timestamp(quotes["started_at_utc"]) < pd.Timestamp(quotes["completed_at_utc"]) < finish
    assert quotes["markets"]["perp:BTC"]["mark"] == result["initial_marks"]["perp:BTC"]
    assert result["now"] == metadata["snapshot_completed_at_utc"]
    assert metadata["snapshot_not_atomic_across_accounts"] is True
    assert all(address not in json.dumps(metadata) for address in [*OWNERS.values(), *AGENTS.values()])


@pytest.mark.parametrize("token, coin", [("UBTC", "BTC"), ("UETH", "ETH"), ("UZEC", "ZEC")])
def test_spot_token_aliases_resolve_to_the_shared_market_symbol(token, coin):
    source = data()
    source["clearpond"]["spotClearinghouseState"]["balances"][1]["coin"] = token
    result = mirror(reader=Reader(source))
    position = next(p for p in result["initial_positions"] if p["account"] == "clearpond")
    assert position["coin"] == coin


def test_explicit_owners_are_used_directly_with_no_role_or_signer_requests():
    reader = Reader()
    class AddressOnly(dict):
        def get(self, key, default=None):
            assert "SECRET" not in key and "PRIVATE" not in key
            return super().get(key, default)
    result = mirror(reader=reader, env=AddressOnly(values()))
    assert len(reader.calls) == 12
    assert {call["user"] for call in reader.calls} == set(OWNERS.values())
    assert all(call["type"] != "userRole" for call in reader.calls)
    assert "private" not in str(result).lower()


def test_agent_addresses_resolve_public_owner_before_balance_reads():
    reader = Reader()
    mirror(reader=reader, env=values(use_agents=True))
    assert {call["user"] for call in reader.calls if call["type"] == "userRole"} == set(AGENTS.values())
    assert {call["user"] for call in reader.calls if call["type"] != "userRole"} == set(OWNERS.values())


@pytest.mark.parametrize("role", ["user", "subAccount"])
def test_owner_addresses_in_api_address_fields_are_accepted(role):
    reader = Reader()
    reader.roles = {agent: {"role": role} for agent in AGENTS.values()}
    mirror(reader=reader, env=values(use_agents=True))
    assert {call["user"] for call in reader.calls if call["type"] != "userRole"} == set(AGENTS.values())


@pytest.mark.parametrize("role", [None, {"role": "missing"}, {"role": "agent", "data": None},
                                    {"role": "agent", "data": {"user": True}}, {"role": "agent", "data": {"user": "bad"}}])
def test_unknown_or_malformed_agent_owner_fails_without_guessing(role):
    reader = Reader()
    reader.roles[AGENTS["alex"]] = role
    with pytest.raises(ValueError):
        mirror(reader=reader, env=values(use_agents=True))


def test_placeholder_owner_key_does_not_shadow_valid_fallback():
    env = values()
    profile = HYPERLIQUID_ACCOUNT_PROFILES["alex"]
    env[profile.wallet_address_env_keys[0]] = "  KEY IN HERE  "
    env[profile.wallet_address_env_keys[1]] = OWNERS["alex"]
    assert mirror(env=env)["initial_cash"]["alex"] == 1000


@pytest.mark.parametrize("market", ["perp:BTC", "spot:BTC", "spot:HYPE"])
def test_missing_quote_for_inherited_inventory_prevents_bootstrap(market):
    markets = Markets()
    markets.omit.add(market)
    with pytest.raises(ValueError, match="Missing public"):
        mirror(markets=markets)


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", "-Infinity", "-1", "0"])
def test_invalid_valuation_marks_are_rejected_before_ledger_creation(bad):
    markets = Markets()
    markets.override["spot:HYPE"] = bad
    with pytest.raises(ValueError):
        mirror(markets=markets)


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", "-1"])
def test_invalid_spot_balance_is_never_silently_imported(bad):
    source = data()
    source["clearpond"]["spotClearinghouseState"]["balances"][1]["total"] = bad
    with pytest.raises(ValueError):
        mirror(reader=Reader(source))


@pytest.mark.parametrize("account, quantity", [("alex", "1"), ("jeremy", "-1"), ("clearpond", "1")])
def test_actual_perp_direction_must_match_the_requested_role(account, quantity):
    source = data()
    source[account]["clearinghouseState"]["assetPositions"] = [{"position": {
        "coin": "BTC", "szi": quantity, "entryPx": "100", "unrealizedPnl": "0",
    }}]
    with pytest.raises(ValueError, match="account role"):
        mirror(reader=Reader(source))


@pytest.mark.parametrize("field", ["clearinghouseState", "spotClearinghouseState", "frontendOpenOrders"])
def test_incomplete_account_payload_does_not_become_an_empty_portfolio(field):
    source = data()
    source["alex"][field] = {"error": "unavailable"}
    with pytest.raises(ValueError):
        mirror(reader=Reader(source))


def test_missing_account_equity_is_not_replaced_by_zero():
    source = data()
    source["alex"]["clearinghouseState"]["marginSummary"] = {}
    with pytest.raises(ValueError, match="valuation"):
        mirror(reader=Reader(source))


def test_overflowing_valuation_is_rejected_before_creating_the_ledger():
    source = data()
    source["clearpond"]["spotClearinghouseState"]["balances"][1]["total"] = "1e308"
    with pytest.raises(ValueError, match="finite"):
        mirror(reader=Reader(source))


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), True, -1])
def test_opening_timestamp_requires_finite_real_time(clock_value):
    with pytest.raises(ValueError):
        mirror(clock=lambda: clock_value)


@pytest.mark.parametrize("payload", [
    {"type": "order", "user": OWNERS["alex"]},
    {"type": "clearinghouseState", "user": OWNERS["alex"], "signature": "forbidden"},
    {"type": "clearinghouseState", "user": "not-an-address"},
])
def test_reader_rejects_nonpublic_operations_before_http(monkeypatch, payload):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid request reached HTTP")
    monkeypatch.setattr("ml.hyperliquid_paper_seed.requests.post", unexpected)
    with pytest.raises(ValueError):
        AccountReader().post_info(payload)
