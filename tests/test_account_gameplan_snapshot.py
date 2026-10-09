"""Explicit account-union reads use synthetic sessions and temporary data only."""
from copy import deepcopy
import hashlib
import json

import pytest
import pandas as pd

from test_gameplan_trade_snapshot import ReadSession, STAMP, IDENTITY, buy_order
from ml.account_gameplan.snapshot import capture_account_planning_snapshot, ownership_evidence_fingerprint
from ml.gameplan_trade_snapshot import capture_trade_planning_snapshot
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS
from ml.stock_trader.state import capture_portfolio_state, _stock_working_order_effects


SYMBOLS = ("AAPL", "MSFT")
_DEFAULT_SESSION = object()


class UnionSession(ReadSession):
    def __init__(self):
        super().__init__()
        original = deepcopy(self.account["securitiesAccount"]["positions"][0])
        original.update(longQuantity=3, marketValue=300)
        original["instrument"]["symbol"] = "MSFT"
        outside = deepcopy(original)
        outside.update(longQuantity=10, marketValue=1000)
        outside["instrument"]["symbol"] = "IBM"
        self.account["securitiesAccount"]["positions"].extend([original, outside])
        self.quotes["MSFT"] = deepcopy(self.quotes["AAPL"])
        self.requested = []

    def get_equity_quotes(self, symbols):
        self.calls.append("quotes")
        self.requested.append(tuple(symbols))
        return deepcopy(self.quotes)


def evidence():
    result = []
    for producer, symbol, held, owned in (("Atlas", "AAPL", 2, 1), ("Scout", "MSFT", 3, 2)):
        item = {"producer": producer, "account_fingerprint": IDENTITY,
            "observed_at": "2026-09-09T04:39:30+00:00", "symbols": [symbol], "held_shares": {symbol: held},
            "ownership": {"status": "OBSERVED_CONSISTENT", "safe_for_planning": True,
                "account_matches": True, "blocked_symbols": [], "reason_codes": [],
                "last_saved_reconciliation_at": "2026-09-09T04:00:00Z", "last_saved_reconciliation_ready": True,
                "current_broker_reconciliation_performed": False, "owned_shares": {symbol: owned},
                "active_allocations": [{"allocation_id_sha256": hashlib.sha256(producer.encode()).hexdigest(),
                    "symbol": symbol, "horizon": "1d", "status": "ACTIVE", "owned_shares": owned,
                    "reserved_buy_shares": 0, "reserved_sell_shares": 0,
                    "target_start": "2026-09-08T11:00:00Z", "target_end": "2026-09-10T00:00:00Z"}]}}
        result.append(item)
    return seal(result)


def seal(records):
    for record in records:
        record["source_fingerprint"] = ownership_evidence_fingerprint(record)
    return records


def capture(session=_DEFAULT_SESSION, records=None, **kwargs):
    records = evidence() if records is None else records
    parameters = dict(symbols=SYMBOLS, expected_account_fingerprint=IDENTITY,
        expected_sources={r["producer"]: r["source_fingerprint"] for r in records},
        ownership_evidence=records, session=UnionSession() if session is _DEFAULT_SESSION else session,
        observed_at=STAMP, clock=lambda: pd.Timestamp(STAMP) + pd.Timedelta(seconds=1))
    parameters.update(kwargs)
    return capture_account_planning_snapshot(**parameters)


def off_universe_order():
    order = buy_order()
    order["orderLegCollection"][0]["instrument"]["symbol"] = "IBM"
    return order


@pytest.mark.parametrize("parallel", [False, True])
def test_explicit_portfolio_universe_preserves_all_account_reserves_and_exposure(parallel):
    session = UnionSession()
    session.orders = [off_universe_order()]
    result = capture_portfolio_state(session, observed_at=STAMP, parallel=parallel,
        literal_cash_only=True, symbols=SYMBOLS)
    assert session.requested == [SYMBOLS]
    assert result.held_shares == {"AAPL": 2, "MSFT": 3}
    assert result.symbol_exposure == {"AAPL": 200, "MSFT": 300}
    assert result.gross_exposure == 1500
    assert result.available_cash == 1300  # Literal $1500 less the account-wide $200 once.
    assert result.pending_buy_shares == {"AAPL": 0, "MSFT": 0}
    assert result.working_order_count == 1


def test_default_portfolio_universe_and_fingerprint_are_unchanged():
    implicit = capture_portfolio_state(UnionSession(), observed_at=STAMP, parallel=False)
    explicit = capture_portfolio_state(UnionSession(), observed_at=STAMP, parallel=False, symbols=STOCK_TRADER_SYMBOLS)
    assert implicit == explicit
    assert tuple(implicit.held_shares) == STOCK_TRADER_SYMBOLS


def test_working_order_identities_are_opt_in_complete_account_wide_and_bound():
    session = UnionSession()
    session.orders = [off_universe_order()]
    plain = capture_portfolio_state(session, observed_at=STAMP, parallel=False, symbols=SYMBOLS)
    assert plain.broker_working_orders is None
    session.calls.clear()
    bound = capture_portfolio_state(session, observed_at=STAMP, parallel=False, symbols=SYMBOLS,
                                    include_order_identities=True)
    assert bound.available_cash == plain.available_cash and bound.gross_exposure == plain.gross_exposure
    assert len(bound.broker_working_orders) == 1
    order = bound.broker_working_orders[0]
    assert order == {"order_id": "SECRET_ORDER_NUMBER", "instruction": "BUY", "asset_type": "EQUITY",
        "symbol": "IBM", "underlying_symbol": None, "remaining_quantity": 2.0,
        "filled_quantity": 1.0, "limit_price": 100.0, "reserved_cash": 200.0, "status": "CURRENT"}
    assert set(session.calls) == {"prepare", "verify", "account", "orders", "quotes"}
    assert all(session.calls.count(name) == 1 for name in session.calls)
    assert bound.source_fingerprint != plain.source_fingerprint
    session.orders[0]["orderId"] = "CHANGED_ID"
    changed = capture_portfolio_state(session, observed_at=STAMP, parallel=False, symbols=SYMBOLS,
                                      include_order_identities=True)
    assert changed.source_fingerprint != bound.source_fingerprint


def test_explicit_empty_working_orders_differ_from_uncaptured_identities():
    plain = capture_portfolio_state(UnionSession(), observed_at=STAMP, parallel=False, symbols=SYMBOLS)
    bound = capture_portfolio_state(UnionSession(), observed_at=STAMP, parallel=False, symbols=SYMBOLS,
                                    include_order_identities=True)
    assert plain.broker_working_orders is None and bound.broker_working_orders == ()
    assert bound.source_fingerprint != plain.source_fingerprint


@pytest.mark.parametrize("failure", ["missing", "blank", "duplicate", "unknown", "incomplete_option"])
def test_account_working_order_identity_capture_fails_closed(failure):
    session = UnionSession()
    session.orders = [off_universe_order()]
    if failure == "missing": session.orders[0].pop("orderId")
    elif failure == "blank": session.orders[0]["orderId"] = " "
    elif failure == "duplicate": session.orders.append(deepcopy(session.orders[0]))
    elif failure == "unknown": session.orders[0]["status"] = "UNKNOWN"
    elif failure == "incomplete_option": session.orders.append(buy_order("OPTION"))
    with pytest.raises(ValueError):
        capture_portfolio_state(session, observed_at=STAMP, parallel=False, symbols=SYMBOLS,
                                include_order_identities=True)


@pytest.mark.parametrize("symbols", [[], (), "AAPL", {"AAPL"}, ["aapl"], [" AAPL"], ["AAPL", "AAPL"],
                                     ["AAPL", ""], ["AAPL", None], ["AAPL", "../../STATE"], ["AAPL", "MSFT "]])
def test_invalid_explicit_symbols_fail_before_account_reads(symbols):
    session = UnionSession()
    with pytest.raises(ValueError):
        capture_portfolio_state(session, observed_at=STAMP, symbols=symbols)
    assert session.calls == []


def test_working_effects_keep_off_universe_reserve_when_option_metadata_is_incomplete():
    working = {"status": "INCOMPLETE", "items": [
        {"asset_type": "EQUITY", "status": "CURRENT", "symbol": "IBM", "pending_stock_share_effect": 2,
         "reserved_cash": 200, "unavailable_reasons": []},
        {"asset_type": "EQUITY", "status": "CURRENT", "symbol": "MSFT", "pending_stock_share_effect": -1,
         "reserved_cash": 0, "unavailable_reasons": []}, {"asset_type": "OPTION"}]}
    reserve, buys, sells, status = _stock_working_order_effects(working, symbols=SYMBOLS)
    assert reserve == 200 and buys == {"AAPL": 0, "MSFT": 0} and sells == {"AAPL": 0, "MSFT": 1}
    assert status == "CURRENT_STOCK_SCOPE_OPTION_METADATA_INCOMPLETE"


def test_combined_snapshot_reads_account_once_and_does_not_sum_producer_cash(tmp_path):
    session = UnionSession()
    session.orders = [off_universe_order()]
    result = capture(session)
    assert result["status"] == "OBSERVED" and result["available_cash"] == 1300
    assert result["reserved_cash"] == 200 and result["gross_exposure"] == 1500
    assert result["held_shares"] == {"AAPL": 2, "MSFT": 3}
    assert result["ownership"]["owned_shares"] == {"AAPL": 1, "MSFT": 2}
    assert result["ownership"]["safe_for_planning"] is True
    assert all(session.calls.count(name) == 1 for name in ("account", "orders", "quotes", "prepare", "verify"))
    assert result["orders_placed"] == 0 and result["orders_enabled"] is False
    assert "SECRET_" not in json.dumps(result) and list(tmp_path.iterdir()) == []


def test_callback_receives_exact_current_account_and_holdings_without_cash():
    seen = []
    def read(**kwargs):
        seen.append(kwargs)
        return evidence()
    assert capture(ownership_evidence=read)["ownership"]["safe_for_planning"] is True
    assert len(seen) == 1
    assert seen[0]["account_fingerprint"] == IDENTITY and seen[0]["held_shares"] == {"AAPL": 2, "MSFT": 3}
    assert 0 <= (pd.Timestamp(seen[0]["observed_at"]) - pd.Timestamp(STAMP)).total_seconds() < 1


@pytest.mark.parametrize("failure", ["account", "held", "owned", "allocation_quantity", "duplicate_allocation", "duplicate_route",
    "partition_overlap", "missing_partition", "stale", "future", "naive", "unready", "blocked", "unknown",
    "pending_owned_buy", "pending_owned_sell", "missing_allocation", "missing_fingerprint", "missing_observed",
    "source_content_changed", "reconciliation_future", "hidden_unknown"])
def test_ownership_cannot_become_safe_by_asserting_a_flag(failure):
    records = evidence()
    owner = records[0]["ownership"]
    allocation = owner["active_allocations"][0]
    if failure == "account": records[0]["account_fingerprint"] = "b" * 64
    elif failure == "held": records[0]["held_shares"]["AAPL"] = 3
    elif failure == "owned": owner["owned_shares"]["AAPL"] = 0
    elif failure == "allocation_quantity": allocation["owned_shares"] = 4
    elif failure == "duplicate_allocation": records[1]["ownership"]["active_allocations"][0]["allocation_id_sha256"] = allocation["allocation_id_sha256"]
    elif failure == "duplicate_route":
        duplicate = deepcopy(allocation)
        duplicate["allocation_id_sha256"] = "f" * 64
        owner["active_allocations"].append(duplicate)
    elif failure == "partition_overlap": records[1]["symbols"] = ["AAPL"]
    elif failure == "missing_partition": records.pop()
    elif failure == "stale": records[0]["observed_at"] = "2026-09-09T04:00:00Z"
    elif failure == "future": records[0]["observed_at"] = "2026-09-09T04:41:00Z"
    elif failure == "naive": records[0]["observed_at"] = "2026-09-09T04:39:30"
    elif failure == "unready": owner["last_saved_reconciliation_ready"] = False
    elif failure == "blocked": owner["blocked_symbols"] = ["AAPL"]
    elif failure == "unknown": owner["reason_codes"] = ["UNKNOWN_SUBMISSION"]
    elif failure == "pending_owned_buy": allocation["reserved_buy_shares"] = 1
    elif failure == "pending_owned_sell": allocation["reserved_sell_shares"] = 1
    elif failure == "missing_allocation": owner["active_allocations"] = []
    elif failure == "missing_fingerprint": allocation.pop("allocation_id_sha256")
    elif failure == "missing_observed": records[0]["observed_at"] = None
    elif failure == "source_content_changed": records[0]["held_shares"]["AAPL"] = 0
    elif failure == "reconciliation_future": owner["last_saved_reconciliation_at"] = "2026-09-10T00:00:00Z"
    elif failure == "hidden_unknown": owner["unknown_reservations"] = ["ambiguous"]
    if failure != "source_content_changed": seal(records)
    result = capture(records=records)
    assert result["status"] == "UNAVAILABLE" and result["available_cash"] is None
    assert result["ownership"]["safe_for_planning"] is False
    assert result["failed_component"] == "OWNERSHIP_EVIDENCE"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_union_pending_order_is_conservatively_unavailable(side):
    session = UnionSession()
    order = buy_order()
    order["orderLegCollection"][0]["instruction"] = side
    session.orders = [order]
    assert capture(session)["status"] == "UNAVAILABLE"


def test_unknown_order_or_wrong_live_account_never_supplies_cash():
    session = UnionSession()
    session.orders = [buy_order()]
    session.orders[0]["status"] = "UNKNOWN"
    assert capture(session)["available_cash"] is None
    session = UnionSession()
    session.stable_account_fingerprint = lambda: "b" * 64
    assert capture(session)["available_cash"] is None


@pytest.mark.parametrize("kwargs", [{"expected_sources": {}}, {"expected_sources": {"Atlas": "invalid"}},
    {"expected_account_fingerprint": None}, {"maximum_age_seconds": 61}, {"maximum_age_seconds": True},
    {"maximum_age_seconds": "60"}, {"maximum_age_seconds": None},
    {"observed_at": "2026-09-08T00:00:00Z"}, {"session": None}])
def test_missing_authority_or_stale_capture_fails_before_account_reads(kwargs):
    with pytest.raises(ValueError): capture(**kwargs)


def test_source_cash_field_is_not_accepted_even_in_a_hashed_envelope():
    records = evidence()
    records[0]["available_cash"] = 1000000
    with pytest.raises(ValueError): ownership_evidence_fingerprint(records[0])
    assert capture(records=records)["available_cash"] is None


def test_legacy_planning_still_rejects_off_watchlist_universe(tmp_path):
    with pytest.raises(ValueError, match="configured production symbols"):
        capture_trade_planning_snapshot(tmp_path, symbols=["SYNTHETICPEER"], session=UnionSession(), observed_at=STAMP)
