from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import requests

from ml.gameplan_trade_snapshot import capture_trade_planning_snapshot


STAMP = "2026-09-09T04:40:00+00:00"
IDENTITY = "a" * 64


class ReadSession:
    def __init__(self):
        self.calls = []
        self.account = {"securitiesAccount": {"accountNumber": "SECRET_ACCOUNT_NUMBER",
            "currentBalances": {"liquidationValue": 10000, "cashBalance": 2000,
                "settledCash": 1500, "cashAvailableForTrading": 8000,
                "availableFundsNonMarginableTrade": 9000, "buyingPowerNonMarginableTrade": 10000},
            "positions": [{"instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                "longQuantity": 2, "shortQuantity": 0, "marketValue": 200,
                "marketPrice": 100, "currentDayProfitLoss": 0}]}}
        self.orders = []
        self.quotes = {"AAPL": {"bidPrice": 99, "askPrice": 101, "lastPrice": 100.5,
            "mark": 100, "quoteTime": 1788927600000, "tradeTime": 1788927500000}}

    def prepare_read_snapshot(self):
        self.calls.append("prepare")
        return "SECRET_CREDENTIAL_FINGERPRINT"

    def verify_read_snapshot(self, fingerprint):
        self.calls.append("verify")
        assert fingerprint == "SECRET_CREDENTIAL_FINGERPRINT"

    def stable_account_fingerprint(self):
        self.calls.append("stable")
        return IDENTITY

    def get_account(self):
        self.calls.append("account")
        return self.account

    def get_open_orders(self):
        self.calls.append("orders")
        return self.orders

    def get_equity_quotes(self, symbols):
        self.calls.append("quotes")
        assert tuple(symbols) == ("AAPL",)
        return self.quotes

    def submit_order(self, *_args, **_kwargs):
        raise AssertionError("Never submit")

    cancel_order = submit_order
    replace_order = submit_order


def capture(root, session=None):
    return capture_trade_planning_snapshot(root, symbols=["AAPL"],
        session=session or ReadSession(), observed_at=STAMP)


def buy_order(asset_type="EQUITY"):
    return {"orderId": "SECRET_ORDER_NUMBER", "status": "WORKING", "orderType": "LIMIT",
        "orderStrategyType": "SINGLE", "complexOrderStrategyType": "NONE",
        "quantity": 3, "filledQuantity": 1, "remainingQuantity": 2, "price": 100,
        "orderLegCollection": [{"instruction": "BUY" if asset_type == "EQUITY" else "BUY_TO_OPEN",
            "quantity": 3, "instrument": {"assetType": asset_type,
                "symbol": "AAPL" if asset_type == "EQUITY" else "AAPL  260918C00100000"}}]}


def ledger(root, *, account=IDENTITY, shares=1, ready=1, block=False):
    path = root / "state/independent-stock-trader/holdings.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE allocations (id TEXT,account TEXT,symbol TEXT,horizon TEXT,start TEXT,end TEXT,status TEXT);
            CREATE TABLE reservations (allocation TEXT,side TEXT,quantity INTEGER,price TEXT,filled INTEGER,status TEXT);
            CREATE TABLE blocks (symbol TEXT,reason TEXT);
            CREATE TABLE snapshots (observed_at TEXT,ready INTEGER,payload TEXT,owned TEXT);
        """)
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("account", account),
            ("version", "independent-stock-horizon-ledger-v1")])
        db.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?)", ("private-allocation", account,
            "AAPL", "1d", "2026-09-08T11:00:00Z", "2026-09-10T00:00:00Z", "ACTIVE"))
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?)",
            ("private-allocation", "BUY", shares, "100", shares, "FILLED"))
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?)", ("2026-09-09T04:00:00Z", ready,
            json.dumps({"held_shares": {"AAPL": 2}}), json.dumps({"AAPL": shares})))
        if block:
            db.execute("INSERT INTO blocks VALUES (?,?)", ("AAPL", "SECRET_BLOCK_TEXT"))
    return path


def test_single_read_sanitized_cash_only_and_no_state_created(tmp_path):
    session = ReadSession()
    session.orders = [buy_order()]
    result = capture(tmp_path, session)
    assert result["status"] == "OBSERVED"
    assert result["available_cash"] == 1300
    assert result["broker_available_cash"] == 7800
    assert result["reserved_cash"] == 200
    assert result["held_shares"] == {"AAPL": 2}
    assert result["stock_market_value_by_symbol"] == {"AAPL": 200}
    assert result["other_symbol_exposure"] == {"AAPL": 0}
    assert result["pending_buy_shares"] == {"AAPL": 2}
    assert result["working_order_count"] == 1
    assert all(session.calls.count(name) == 1 for name in ("account", "orders", "quotes", "prepare", "verify"))
    assert result["ownership"]["safe_for_planning"] is False
    assert result["ownership"]["reason_codes"] == ["OWNERSHIP_DATABASE_MISSING"]
    assert list(tmp_path.iterdir()) == []
    serialized = json.dumps(result)
    assert "SECRET_" not in serialized
    assert IDENTITY not in serialized
    assert result["orders_placed"] == 0


def test_actual_trade_time_is_distinct_from_capture(tmp_path):
    result = capture(tmp_path)
    quote = result["quotes"]["AAPL"]
    assert quote["price_reference"] == 100.5
    assert quote["price_reference_source"] == "SCHWAB_LAST_TRADE"
    assert quote["price_reference_time"] == quote["trade_time"]
    assert quote["price_reference_time"] != quote["observed_at"]
    assert quote["live_executable"] is False


@pytest.mark.parametrize("bad", [None, "invalid", float("nan")])
def test_invalid_supplied_cash_does_not_become_zero_fact(tmp_path, bad):
    session = ReadSession()
    session.account["securitiesAccount"]["currentBalances"]["cashBalance"] = bad
    result = capture(tmp_path, session)
    assert result["available_cash"] is None
    assert result["cash_reason_codes"] == ["REPORTED_CASH_BALANCE_INVALID"]
    assert result["broker_available_cash"] == 8000


def test_buying_power_alone_does_not_substitute_for_cash(tmp_path):
    session = ReadSession()
    balances = session.account["securitiesAccount"]["currentBalances"]
    del balances["cashBalance"], balances["settledCash"]
    result = capture(tmp_path, session)
    assert result["available_cash"] is None
    assert "LITERAL_CASH_BALANCE_UNAVAILABLE" in result["cash_reason_codes"]


def test_negative_actual_cash_is_known_zero_capacity(tmp_path):
    session = ReadSession()
    session.account["securitiesAccount"]["currentBalances"]["cashBalance"] = -100
    assert capture(tmp_path, session)["available_cash"] == 0


def test_account_wide_option_reserve_uncertainty_blocks_cash(tmp_path):
    session = ReadSession()
    session.orders = [buy_order("OPTION")]
    result = capture(tmp_path, session)
    assert result["available_cash"] is None
    assert result["reserved_cash"] is None
    assert "ACCOUNT_WIDE_PENDING_RESERVES_UNAVAILABLE" in result["cash_reason_codes"]
    assert "SECRET_" not in json.dumps(result)


def test_missing_trade_time_uses_timed_midpoint_then_no_fabrication(tmp_path):
    session = ReadSession()
    del session.quotes["AAPL"]["tradeTime"]
    quote = capture(tmp_path, session)["quotes"]["AAPL"]
    assert quote["price_reference"] == 100
    assert quote["price_reference_source"] == "SCHWAB_BID_ASK_MIDPOINT"
    del session.quotes["AAPL"]["quoteTime"]
    quote = capture(tmp_path, session)["quotes"]["AAPL"]
    assert quote["price_reference"] is None
    assert quote["price_reference_time"] is None


def test_ledger_read_is_query_only_and_identifier_free(tmp_path):
    path = ledger(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = capture(tmp_path)
    owner = result["ownership"]
    assert owner["status"] == "OBSERVED_CONSISTENT"
    assert owner["safe_for_planning"] is True
    assert owner["active_allocations"][0]["owned_shares"] == 1
    assert owner["active_allocations"][0]["target_start"] == "2026-09-08T11:00:00+00:00"
    assert owner["active_allocations"][0]["target_end"] == "2026-09-10T00:00:00+00:00"
    assert owner["current_broker_reconciliation_performed"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert list(path.parent.iterdir()) == [path]
    assert IDENTITY not in json.dumps(result)
    assert "private-allocation" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("end", None), ("end", "not-a-time"), ("end", "NaT"),
                                        ("end", "2026-09-10T00:00:00"), ("start", None),
                                        ("end", "2026-09-08T11:00:00Z"),
                                        ("end", "2026-09-08T10:00:00Z")])
def test_invalid_allocation_expiry_cannot_become_current_time_or_spendable_cash(tmp_path, field, value):
    path = ledger(tmp_path)
    # Column names come only from the fixed parameter cases above.
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE allocations SET {field}=?", (value,))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    owner = capture(tmp_path)["ownership"]
    assert owner["safe_for_planning"] is False
    assert owner["reason_codes"] == ["OWNERSHIP_ALLOCATION_TIME_INVALID"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("options,reason", [
    ({"account": "b" * 64}, "OWNERSHIP_ACCOUNT_MISMATCH"),
    ({"shares": 3}, "BROKER_SHARES_BELOW_HORIZON_INVENTORY"),
    ({"ready": 0}, "LAST_SAVED_RECONCILIATION_NOT_READY"),
    ({"block": True}, "PERSISTENT_OWNERSHIP_BLOCKS"),
])
def test_ledger_identity_inventory_or_blocks_prevent_positive_planning(tmp_path, options, reason):
    ledger(tmp_path, **options)
    owner = capture(tmp_path)["ownership"]
    assert owner["safe_for_planning"] is False
    assert reason in owner["reason_codes"]
    assert "SECRET_" not in json.dumps(owner)


def test_broker_error_never_serializes_account_url_or_credentials(tmp_path):
    session = ReadSession()
    def fail():
        raise RuntimeError("SECRET_TOKEN https://broker/accounts/SECRET_ACCOUNT")
    session.get_account = fail
    result = capture(tmp_path, session)
    assert result["status"] == "UNAVAILABLE"
    assert result["available_cash"] is None
    assert "SECRET" not in json.dumps(result)


def test_identity_change_invalidates_entire_capture(tmp_path):
    session = ReadSession()
    identities = iter((IDENTITY, "b" * 64))
    session.stable_account_fingerprint = lambda: next(identities)
    result = capture(tmp_path, session)
    assert result["status"] == "UNAVAILABLE"
    assert result["available_cash"] is None


def test_ambiguous_position_does_not_invent_zero_held_shares(tmp_path):
    session = ReadSession()
    del session.account["securitiesAccount"]["positions"][0]["longQuantity"]
    result = capture(tmp_path, session)
    assert result["status"] == "UNAVAILABLE"
    assert result["held_shares"] == {}
    assert result["available_cash"] is None


@pytest.mark.parametrize("price", [None, 2.19])
def test_direct_option_dollar_exposure_does_not_require_unit_price_or_multiplier(tmp_path, price):
    session = ReadSession()
    option = {"instrument": {"assetType": "OPTION", "symbol": "GOOG  260918C00340000"},
              "longQuantity": 1, "shortQuantity": 0, "marketValue": 219}
    if price is not None:
        option["marketPrice"] = price
    session.account["securitiesAccount"]["positions"].append(option)
    result = capture(tmp_path, session)
    assert result["status"] == "OBSERVED"
    assert result["gross_exposure"] == 419
    assert result["held_shares"] == {"AAPL": 2}
    assert result["available_cash"] == 1500


def test_stock_and_option_exposure_are_separate_for_the_same_underlying(tmp_path):
    session = ReadSession()
    positions = session.account["securitiesAccount"]["positions"]
    positions[0].update(instrument={"symbol": "GOOG", "assetType": "EQUITY"},
        longQuantity=1, marketValue=336.45, marketPrice=336.45)
    positions.append({"instrument": {"symbol": "GOOG  260918C00340000", "assetType": "OPTION"},
                      "longQuantity": 1, "shortQuantity": 0, "marketValue": 219})
    session.get_equity_quotes = lambda _symbols: {"GOOG": {"bidPrice": 335, "askPrice": 336,
        "lastPrice": 335.5, "tradeTime": 1788927500000, "quoteTime": 1788927600000}}
    result = capture_trade_planning_snapshot(tmp_path, symbols=["GOOG"], session=session, observed_at=STAMP)
    assert result["status"] == "OBSERVED"
    assert result["held_shares"] == {"GOOG": 1}
    assert result["stock_market_value_by_symbol"] == {"GOOG": 336.45}
    assert result["other_symbol_exposure"] == {"GOOG": 219}
    assert result["symbol_exposure"] == {"GOOG": 555.45}
    assert result["gross_exposure"] == 555.45


def test_short_option_exposure_uses_native_absolute_gross_attribution(tmp_path):
    session = ReadSession()
    session.account["securitiesAccount"]["positions"].append({
        "instrument": {"symbol": "AAPL  260918C00100000", "assetType": "OPTION"},
        "longQuantity": 0, "shortQuantity": 1, "marketValue": -125})
    result = capture(tmp_path, session)
    assert result["status"] == "OBSERVED"
    assert result["stock_market_value_by_symbol"] == {"AAPL": 200}
    assert result["other_symbol_exposure"] == {"AAPL": 125}
    assert result["symbol_exposure"] == {"AAPL": 325}
    assert result["gross_exposure"] == 325


def test_full_reported_fourteen_position_shape_includes_etfs_and_option(tmp_path):
    session = ReadSession()
    economics = [("AMZN", "EQUITY", 1, 257.94), ("VXUS", "COLLECTIVE_INVESTMENT", 30, 2657.4),
        ("NBIS", "EQUITY", 25, 6138.75), ("GOOG", "EQUITY", 1, 336.45),
        ("EWY", "COLLECTIVE_INVESTMENT", 10, 1919.4), ("NVDA", "EQUITY", 1, 226.04),
        ("TENB", "EQUITY", 10, 337), ("ZETA", "EQUITY", 10, 308.8),
        ("SNDK", "EQUITY", 1, 1755.12), ("SLS", "EQUITY", 100, 1397),
        ("AAPL", "EQUITY", 1, 317.61), ("MRNA", "EQUITY", 5, 703.75),
        ("MU", "EQUITY", 1, 1007.64)]
    positions = [{"instrument": {"symbol": symbol, "assetType": asset},
        "longQuantity": quantity, "shortQuantity": 0, "marketValue": value}
        for symbol, asset, quantity, value in economics]
    positions.append({"instrument": {"assetType": "OPTION", "symbol": "GOOG  260918C00340000"},
        "longQuantity": 1, "shortQuantity": 0, "marketValue": 219})
    session.account["securitiesAccount"]["positions"] = positions
    result = capture(tmp_path, session)
    assert result["status"] == "OBSERVED"
    assert result["gross_exposure"] == pytest.approx(sum(row[3] for row in economics) + 219)
    assert result["held_shares"] == {"AAPL": 1}
    assert result["symbol_exposure"] == {"AAPL": 317.61}
    assert result["stock_market_value_by_symbol"] == {"AAPL": 317.61}
    assert result["other_symbol_exposure"] == {"AAPL": 0}
    assert result["available_cash"] == 1500


@pytest.mark.parametrize("change,code", [
    ({"netQuantity": 3}, "POSITION_QUANTITY_OR_VALUATION_UNAVAILABLE_OR_CONFLICTING"),
    ({"marketPrice": 2}, "POSITION_QUANTITY_OR_VALUATION_UNAVAILABLE_OR_CONFLICTING"),
    ({"instrument": {"assetType": "UNKNOWN", "symbol": "VXUS"}}, "POSITION_ASSET_TYPE_UNKNOWN"),
])
def test_etf_scope_exception_does_not_hide_genuine_position_failures(tmp_path, change, code):
    session = ReadSession()
    session.account["securitiesAccount"]["positions"].append({
        "instrument": {"assetType": "COLLECTIVE_INVESTMENT", "symbol": "VXUS"},
        "longQuantity": 30, "shortQuantity": 0, "marketValue": 2657.4, **change})
    result = capture(tmp_path, session)
    assert result["status"] == "UNAVAILABLE"
    assert result["position_validation_failures"] == [{"position_index": 1, "reason_code": code}]


@pytest.mark.parametrize("change", [
    {"netQuantity": 2},
    {"marketValue": None},
    {"marketValue": -219},
    {"symbol": "AAPL  260918C00340000"},
    {"underlyingSymbol": "AAPL"},
    {"instrument": {"assetType": "UNKNOWN", "symbol": "GOOG  260918C00340000"}},
    {"instrument": {"assetType": "OPTION", "symbol": "GOOG  260918C00340000", "multiplier": 0}},
    {"instrument": {"assetType": "OPTION", "symbol": "GOOG  260918C00340000", "multiplier": 100},
     "marketPrice": 4},
])
def test_option_price_exception_preserves_identity_quantity_and_valuation_gates(tmp_path, change):
    session = ReadSession()
    option = {"instrument": {"assetType": "OPTION", "symbol": "GOOG  260918C00340000"},
              "longQuantity": 1, "shortQuantity": 0, "marketValue": 219, **change}
    session.account["securitiesAccount"]["positions"].append(option)
    result = capture(tmp_path, session)
    assert result["status"] == "UNAVAILABLE"
    assert result["failed_component"] == "POSITION_ECONOMICS"
    assert result["available_cash"] is None


def test_manual_reduction_still_above_owned_inventory_requires_review(tmp_path):
    ledger(tmp_path)
    session = ReadSession()
    position = session.account["securitiesAccount"]["positions"][0]
    position.update(longQuantity=1, marketValue=100)
    owner = capture(tmp_path, session)["ownership"]
    assert owner["safe_for_planning"] is False
    assert "UNEXPLAINED_SHARE_REDUCTION_SINCE_SAVED_RECONCILIATION" in owner["reason_codes"]


def test_unknown_requested_symbol_is_rejected_before_broker_read(tmp_path):
    session = ReadSession()
    with pytest.raises(ValueError, match="configured production symbols"):
        capture_trade_planning_snapshot(tmp_path, symbols=["INVALID"], session=session)
    assert session.calls == []


def test_classified_timeout_retries_complete_coherent_read_only_snapshot(tmp_path, monkeypatch):
    session = ReadSession()
    original = session.get_open_orders
    attempts = 0
    def sometimes_timeout():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            session.calls.append("orders")
            raise requests.ReadTimeout("SECRET_ACCOUNT_URL")
        return original()
    session.get_open_orders = sometimes_timeout
    delays = []
    monkeypatch.setattr("ml.gameplan_trade_snapshot.time.sleep", delays.append)
    result = capture(tmp_path, session)
    assert result["status"] == "OBSERVED"
    assert result["available_cash"] == 1500
    assert session.calls.count("account") == 2
    assert session.calls.count("prepare") == 2
    assert session.calls.count("orders") == 2
    assert session.calls.count("quotes") == 1
    assert delays == [3]
    assert result["broker_state_capture"]["status"] == "CURRENT_AFTER_RETRY"
    assert result["broker_state_capture"]["attempts"] == 2
    assert result["broker_state_capture"]["last_error_type"] == "ReadTimeout"
    assert result["broker_state_capture"]["last_error_operation"] == "WORKING_ORDERS"
    assert "SECRET_" not in json.dumps(result)


def test_http_authorization_failure_is_not_retried(tmp_path, monkeypatch):
    session = ReadSession()
    def forbidden():
        session.calls.append("orders")
        response = requests.Response()
        response.status_code = 403
        raise requests.HTTPError("SECRET_ACCOUNT_URL", response=response)
    session.get_open_orders = forbidden
    delays = []
    monkeypatch.setattr("ml.gameplan_trade_snapshot.time.sleep", delays.append)
    result = capture(tmp_path, session)
    assert result["status"] == "UNAVAILABLE"
    assert session.calls.count("orders") == 1
    assert delays == []
    assert result["broker_state_capture"]["transient_failures"] == 0
    assert result["broker_state_capture"]["last_error_operation"] == "WORKING_ORDERS"
    assert "SECRET_" not in json.dumps(result)


def test_retry_budget_exhaustion_returns_sanitized_failure(tmp_path, monkeypatch):
    session = ReadSession()
    def timeout():
        session.calls.append("orders")
        raise requests.ReadTimeout("SECRET_ACCOUNT_URL")
    session.get_open_orders = timeout
    times = iter([0, 0, 121])
    monkeypatch.setattr("ml.gameplan_trade_snapshot.time.monotonic", lambda: next(times))
    delays = []
    monkeypatch.setattr("ml.gameplan_trade_snapshot.time.sleep", delays.append)
    result = capture(tmp_path, session)
    assert result["status"] == "UNAVAILABLE"
    assert result["available_cash"] is None
    assert result["stock_market_value_by_symbol"] == {}
    assert result["other_symbol_exposure"] == {}
    assert result["broker_state_capture"]["elapsed_seconds"] == 121
    assert result["broker_state_capture"]["maximum_retry_seconds"] == 120
    assert session.calls.count("orders") == 1
    assert delays == []
