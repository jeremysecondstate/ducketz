"""Public-only market lookup and fills must use fresh, executable book depth."""
import copy
from datetime import datetime, timezone
import math
import threading

import pytest
import requests

from ml.hyperliquid_paper_market import PublicPaperMarket, consume_fill, simulate_fill


NOW = 2_000_000_000.0


def utc(stamp=NOW):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return copy.deepcopy(self.payload)


class PublicFixture:
    def __init__(self):
        self.calls = []
        self.symbols = ("BTC", "ETH", "HYPE", "ZEC")
        self.perps = [
            {"universe": [{"name": coin, "szDecimals": 3, "maxLeverage": 10} for coin in self.symbols]},
            [{"markPx": "100", "oraclePx": "99.9", "funding": "0.00001"} for _ in self.symbols],
        ]
        self.spots = [
            {
                "tokens": [{"index": 0, "name": "USDC", "szDecimals": 8}]
                + [{"index": index + 40, "name": name, "szDecimals": 4}
                   for index, name in enumerate(("UBTC", "UETH", "HYPE", "UZEC"))],
                "universe": [{"index": index + 100, "tokens": [index + 40, 0]}
                             for index in range(4)],
            },
            [{"coin": f"@{index + 100}", "markPx": "101", "midPx": "101"} for index in range(4)],
        ]
        self.books = {
            coin: {"coin": coin, "time": int(NOW * 1000), "levels": [
                [{"px": "99", "sz": "2"}, {"px": "98", "sz": "3"}],
                [{"px": "101", "sz": "1"}, {"px": "102", "sz": "2"}],
            ]}
            for coin in (*self.symbols, *(f"@{index + 100}" for index in range(4)))
        }
        self.funding = []
        self.lock = threading.Lock()

    def post(self, url, *, json, headers, timeout):
        assert url.endswith("/info")
        assert headers == {"Content-Type": "application/json"}
        assert timeout > 0
        with self.lock:
            self.calls.append(dict(json))
        operation = json["type"]
        if operation == "metaAndAssetCtxs":
            return Response(self.perps)
        if operation == "spotMetaAndAssetCtxs":
            return Response(self.spots)
        if operation == "l2Book":
            return Response(self.books[json["coin"]])
        if operation == "fundingHistory":
            return Response(self.funding)
        raise AssertionError(f"Unexpected operation {operation}")

    def client(self, **kwargs):
        return PublicPaperMarket(clock=kwargs.pop("clock", lambda: NOW), post=self.post, **kwargs)


@pytest.fixture
def public():
    return PublicFixture()


def market(**changes):
    return {
        "coin": "BTC", "kind": "perp", "wire_coin": "BTC", "sz_decimals": 3,
        "mark": 100.0, "oracle": 99.9, "funding_rate": 0.00001,
        "bids": [[99.0, 2.0], [98.0, 3.0]], "asks": [[101.0, 1.0], [102.0, 2.0]],
        "book_time_utc": utc(), "received_at_utc": utc(), **changes,
    }


def test_public_snapshot_resolves_each_exact_spot_pair_independently_of_token_index(public):
    result = public.client().snapshot(list(public.symbols))
    assert result["errors"] == {}
    assert len(result["markets"]) == 8
    assert result["observed_at_utc"] == utc()
    for index, symbol in enumerate(public.symbols):
        perp = result["markets"][f"perp:{symbol}"]
        spot = result["markets"][f"spot:{symbol}"]
        assert perp["wire_coin"] == symbol
        assert perp["funding_rate"] == 0.00001
        assert perp["oracle"] == 99.9
        assert spot["wire_coin"] == f"@{100 + index}"
        assert spot["base_token_index"] == 40 + index
        assert spot["sz_decimals"] == 4
        assert spot["mark"] == 101.0
        assert spot["mark"] != perp["mark"]
        assert spot["funding_rate"] == 0.0
    assert {row["coin"] for row in public.calls if row["type"] == "l2Book"} == {
        "BTC", "ETH", "HYPE", "ZEC", "@100", "@101", "@102", "@103",
    }


def test_wrong_quote_or_familiar_display_symbol_is_not_a_spot_alias_fallback(public):
    public.spots[0]["tokens"][1]["name"] = "BTC"
    result = public.client().snapshot(["BTC", "ETH"])
    assert "spot:BTC" in result["errors"]
    assert "spot:BTC" not in result["markets"]
    assert "perp:BTC" in result["markets"]
    assert "spot:ETH" in result["markets"]
    public.spots[0]["tokens"][1]["name"] = "UBTC"
    public.spots[0]["tokens"][0]["name"] = "USDT"
    assert "spot:BTC" in public.client().snapshot(["BTC"])["errors"]


def test_ambiguous_spot_pairs_and_perpetual_names_are_reported_not_guessed(public):
    public.spots[0]["universe"].append({"index": 900, "tokens": [40, 0]})
    public.perps[0]["universe"].append(dict(public.perps[0]["universe"][0]))
    public.perps[1].append(dict(public.perps[1][0]))
    result = public.client().snapshot(["BTC", "ETH"])
    assert set(result["errors"]) == {"perp:BTC", "spot:BTC"}
    assert set(result["markets"]) == {"perp:ETH", "spot:ETH"}


def test_book_identity_mismatch_never_prices_another_market(public):
    public.books["BTC"]["coin"] = "ETH"
    result = public.client().snapshot(["BTC"])
    assert "identity" in result["errors"]["perp:BTC"]
    assert "perp:BTC" not in result["markets"]
    assert "spot:BTC" in result["markets"]


@pytest.mark.parametrize(("offset", "accepted"), [(-45, True), (-45.001, False), (5, True), (5.001, False)])
def test_timestamp_age_and_future_tolerance_are_enforced(public, offset, accepted):
    public.books["BTC"]["time"] = round((NOW + offset) * 1000)
    result = public.client().snapshot(["BTC"])
    assert ("perp:BTC" in result["markets"]) is accepted
    assert ("perp:BTC" in result["errors"]) is not accepted


def test_quotes_are_rechecked_at_snapshot_completion(public):
    times = iter([NOW, NOW, NOW + 46])
    result = public.client(clock=lambda: next(times), max_workers=1).snapshot(["BTC"])
    assert result["markets"] == {}
    assert result["errors"] == {"perp:BTC": "stale_book", "spot:BTC": "stale_book"}


def test_book_sorting_merges_duplicate_prices_without_inventing_liquidity(public):
    public.books["BTC"]["levels"] = [
        [{"px": "98", "sz": "2"}, {"px": "99", "sz": "1"}, {"px": "99", "sz": "3"}],
        [{"px": "103", "sz": "5"}, {"px": "101", "sz": "2"}],
    ]
    book = public.client().snapshot(["BTC"])["markets"]["perp:BTC"]
    assert book["bids"] == [[99.0, 4.0], [98.0, 2.0]]
    assert book["asks"] == [[101.0, 2.0], [103.0, 5.0]]


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "0", True])
def test_invalid_book_prices_and_sizes_are_rejected(public, value):
    public.books["BTC"]["levels"][0][0]["px"] = value
    assert "perp:BTC" in public.client().snapshot(["BTC"])["errors"]
    public.books["BTC"]["levels"][0][0]["px"] = "99"
    public.books["BTC"]["levels"][0][0]["sz"] = value
    assert "perp:BTC" in public.client().snapshot(["BTC"])["errors"]


def test_crossed_or_locked_books_have_no_executable_quote(public):
    public.books["BTC"]["levels"][0][0]["px"] = "101"
    assert "crossed or locked" in public.client().snapshot(["BTC"])["errors"]["perp:BTC"]


@pytest.mark.parametrize("payload", [
    {"type": "clearinghouseState", "user": "anything"},
    {"type": "userFunding", "user": "anything"},
    {"type": "exchange"}, {"type": "withdraw3"},
    {"type": "metaAndAssetCtxs", "user": "anything"},
    {"type": "l2Book", "coin": "BTC", "action": {}},
    {"type": "l2Book", "coin": "../BTC"},
])
def test_private_unknown_or_extra_request_fields_never_reach_transport(public, payload):
    with pytest.raises(ValueError):
        public.client().post_info(payload)
    assert public.calls == []


def test_transport_retries_transient_failure_but_not_auth_failure(monkeypatch):
    monkeypatch.setattr("ml.hyperliquid_paper_market.time.sleep", lambda _: None)
    attempts = []

    def transient(url, **kwargs):
        attempts.append(1)
        return Response({"ok": True}, 503 if len(attempts) < 3 else 200)

    client = PublicPaperMarket(post=transient)
    assert client.post_info({"type": "metaAndAssetCtxs"}) == {"ok": True}
    assert len(attempts) == 3
    attempts.clear()

    def unauthorized(url, **kwargs):
        attempts.append(1)
        return Response({}, 401)

    with pytest.raises(requests.HTTPError):
        PublicPaperMarket(post=unauthorized).post_info({"type": "metaAndAssetCtxs"})
    assert len(attempts) == 1


def test_buy_and_sell_fill_walk_correct_depth_with_adverse_slippage():
    buy = simulate_fill(market(), 2, now=NOW)
    assert buy["quantity"] == 2
    assert buy["raw_book_vwap"] == 101.5
    assert buy["price"] == pytest.approx(101.5 * 1.0002)
    assert buy["notional"] == pytest.approx(2 * buy["price"])
    assert buy["levels_consumed"] == 2
    sell = simulate_fill(market(), -3, now=NOW)
    assert sell["quantity"] == -3
    assert sell["raw_book_vwap"] == pytest.approx((99 * 2 + 98) / 3)
    assert sell["price"] < sell["raw_book_vwap"]
    assert sell["unfilled_quantity"] == 0


def test_partial_fill_is_capped_by_visible_depth_and_remainder_preserves_sign():
    buy = simulate_fill(market(), 5, now=NOW)
    sell = simulate_fill(market(), -9, now=NOW)
    assert buy["quantity"] == 3
    assert buy["unfilled_quantity"] == 2
    assert sell["quantity"] == -5
    assert sell["unfilled_quantity"] == -4
    assert buy["status"] == sell["status"] == "partial"


def test_consuming_fills_shares_visible_depth_between_accounts_without_mutating_input():
    quote = market()
    original = copy.deepcopy(quote)
    first = simulate_fill(quote, 2, now=NOW)
    assert first["consumed_levels"] == [{"price": 101.0, "quantity": 1.0}, {"price": 102.0, "quantity": 1.0}]
    remaining = consume_fill(quote, first)
    assert quote == original
    assert remaining["bids"] == original["bids"]
    assert remaining["asks"] == [[102.0, 1.0]]
    second = simulate_fill(remaining, 2, now=NOW)
    assert second["quantity"] == 1.0
    assert second["unfilled_quantity"] == 1.0
    assert second["price"] == pytest.approx(102 * 1.0002)
    empty = consume_fill(remaining, second)
    assert empty["asks"] == []
    assert simulate_fill(empty, 1, now=NOW)["quantity"] == 0


def test_sell_consumption_only_reduces_bids_and_zero_fill_is_an_independent_copy():
    quote = market()
    fill = simulate_fill(quote, -3, now=NOW)
    remaining = consume_fill(quote, fill)
    assert remaining["bids"] == [[98.0, 2.0]]
    assert remaining["asks"] == quote["asks"]
    unfilled = simulate_fill(quote, 0.0001, now=NOW)
    assert unfilled["consumed_levels"] == []
    clone = consume_fill(quote, unfilled)
    clone["asks"][0][1] = 999
    assert quote["asks"][0][1] == 1


@pytest.mark.parametrize("change", [
    {"wire_coin": "ETH"}, {"book_time_utc": utc(NOW - 1)},
    {"consumed_levels": [{"price": 101, "quantity": 3}]},
    {"consumed_levels": [{"price": 777, "quantity": 2}]},
    {"consumed_levels": [{"price": 101, "quantity": 2}]},
])
def test_liquidity_consumption_rejects_another_book_or_invalid_fill_breakdown(change):
    quote = market()
    fill = {**simulate_fill(quote, 2, now=NOW), **change}
    with pytest.raises(ValueError):
        consume_fill(quote, fill)
    assert quote["asks"] == [[101.0, 1.0], [102.0, 2.0]]


def test_size_rounding_is_toward_zero_and_never_exceeds_visible_lots():
    positive = simulate_fill(market(sz_decimals=2), 0.129, min_notional=0, now=NOW)
    negative = simulate_fill(market(sz_decimals=2), -0.129, min_notional=0, now=NOW)
    assert positive["quantity"] == 0.12
    assert negative["quantity"] == -0.12
    assert positive["unfilled_quantity"] == pytest.approx(0.009)
    assert negative["unfilled_quantity"] == pytest.approx(-0.009)
    sparse = simulate_fill(market(sz_decimals=2, asks=[[101, 0.015], [102, 0.024]]), 1,
                           min_notional=0, now=NOW)
    assert sparse["quantity"] == 0.03


def test_minimum_notional_applies_to_actual_partial_fill_not_requested_size():
    fill = simulate_fill(market(asks=[[101, 0.001]]), 10, now=NOW)
    assert fill["quantity"] == 0
    assert fill["price"] is None
    assert fill["reason"] == "below_min_notional"
    assert fill["unfilled_quantity"] == 10


def test_stale_missing_or_crossed_quotes_never_fall_back_to_mark_or_candle():
    for quote, now in ((market(), NOW + 46), (market(asks=[]), NOW),
                       (market(bids=[[102, 2]]), NOW), (market(book_time_utc=None), NOW)):
        quote["mark"] = 999999
        quote["decision_price"] = 123
        result = simulate_fill(quote, 1, now=now)
        assert result["quantity"] == 0
        assert result["notional"] == 0
        assert result["price"] is None


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), -float("inf"), True, None])
def test_invalid_fill_quantities_cannot_create_money(quantity):
    with pytest.raises(ValueError):
        simulate_fill(market(), quantity, now=NOW)


@pytest.mark.parametrize(("name", "value"), [
    ("extra_slippage_bps", -1), ("extra_slippage_bps", 10000),
    ("extra_slippage_bps", float("nan")), ("min_notional", -1),
    ("min_notional", float("inf")), ("now", float("nan")),
])
def test_invalid_fill_assumptions_are_rejected(name, value):
    settings = {"now": NOW, name: value}
    with pytest.raises(ValueError):
        simulate_fill(market(), 1, **settings)


def test_funding_history_is_sorted_validated_and_deduplicated(public):
    public.funding = [
        {"coin": "BTC", "time": 2000, "fundingRate": "0.001", "premium": "0.002"},
        {"coin": "BTC", "time": 1000, "fundingRate": "-0.001", "premium": "-0.002"},
        {"coin": "BTC", "time": 1000, "fundingRate": "-0.001", "premium": "-0.002"},
    ]
    records = public.client().funding_history("BTC", 1000, 2000)
    assert [record["time"] for record in records] == [1000, 2000]
    assert records[0]["funding_rate"] == -0.001
    assert all(math.isfinite(record["funding_rate"]) for record in records)


@pytest.mark.parametrize("change", [{"coin": "ETH"}, {"time": 999}, {"time": True}, {"fundingRate": "nan"}])
def test_invalid_funding_records_are_rejected(public, change):
    public.funding = [{"coin": "BTC", "time": 1000, "fundingRate": "0.001", **change}]
    with pytest.raises(ValueError):
        public.client().funding_history("BTC", 1000, 2000)


def test_conflicting_funding_values_are_not_silently_rewritten(public):
    public.funding = [
        {"coin": "BTC", "time": 1000, "fundingRate": "0.001"},
        {"coin": "BTC", "time": 1000, "fundingRate": "0.002"},
    ]
    with pytest.raises(ValueError, match="Conflicting"):
        public.client().funding_history("BTC", 1000, 2000)


@pytest.mark.parametrize("url", ["https://api.hyperliquid.xyz/exchange", "ftp://example/info",
                                  "https://user:secret@example/info", "https://example/info?secret=x"])
def test_client_cannot_be_pointed_at_execution_endpoint_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        PublicPaperMarket(url)


@pytest.mark.parametrize("symbols", [[], "BTC", ["btc"], ["BTC", "BTC"], [None]])
def test_invalid_snapshot_symbols_are_rejected_before_network(public, symbols):
    with pytest.raises(ValueError):
        public.client().snapshot(symbols)
    assert public.calls == []
