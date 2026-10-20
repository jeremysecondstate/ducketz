"""Current nightly context uses real account facts without planning ownership."""
from copy import deepcopy
import json
import socket

import pandas as pd
import pytest

from tools import nightly_account_snapshot as module
from tests.test_nightly_account_snapshot import (
    NOW, SCOPE, SYMBOLS, configured, contents, raw_snapshot,
)
from tests.test_gameplan_trade_snapshot import ReadSession, buy_order


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("No network verification"))
    monkeypatch.setattr("ml.gameplan_trade_snapshot.DEFAULT_BROKER_STATE_RETRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr("ml.gameplan_trade_snapshot.DEFAULT_BROKER_STATE_RETRY_MAX_SECONDS", 0)


def project(value):
    return module._project(value, symbols=SYMBOLS, scope=SCOPE, observed=pd.Timestamp(NOW))


def test_ownership_review_diagnostics_do_not_block_actual_account_context():
    value = raw_snapshot()
    value["ownership"].update(status="REVIEW_REQUIRED", safe_for_planning=False,
        reason_codes=["CLOSED_ALLOCATION_HAS_INVENTORY", "OWNERSHIP_QUANTITY_INCONSISTENT", "SECRET_DIAGNOSTIC"])
    original = deepcopy(value)
    result = project(value)
    assert value == original
    assert result["available_cash"] == 750 and result["held_shares"]["AAPL"] == 2
    assert result["ownership_diagnostics"] == {"status": "REVIEW_REQUIRED",
        "reason_codes": ["CLOSED_ALLOCATION_HAS_INVENTORY", "OWNERSHIP_QUANTITY_INCONSISTENT"],
        "required_for_directional_context": False}
    assert "ownership" not in result and "safe_for_planning" not in json.dumps(result)
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("ownership", [None, [], {"status": {}, "reason_codes": None},
    {"status": "REVIEW_REQUIRED", "reason_codes": "SECRET"}])
def test_unavailable_or_malformed_ownership_is_not_an_account_precondition(ownership):
    value = raw_snapshot(); value["ownership"] = ownership
    assert project(value)["available_cash"] == 750


@pytest.mark.parametrize("quotes", [None, {}, {"AAPL": {"ask": -1, "mark": float("nan")}}, "SECRET"])
def test_optional_quotes_do_not_limit_current_direction_context(quotes):
    value = raw_snapshot(); value["quotes"] = quotes
    result = project(value)
    assert result["pending_buy_shares"]["AMZN"] == 1
    assert result["available_cash"] == 750
    assert "SECRET" not in json.dumps(result)


def test_genuine_zero_account_has_no_budget_or_quote_requirement():
    value = raw_snapshot()
    value.update({key: 0 for key in module._MONEY})
    value.update({key: dict.fromkeys(SYMBOLS, 0) for key in module._MAPS})
    value.update(working_order_count=0, quotes=None, ownership=None)
    result = project(value)
    assert result["account_equity"] == result["available_cash"] == 0
    assert result["quotes"] == {} and "ownership" not in result


@pytest.mark.parametrize("key,invalid", [
    ("available_cash", -1), ("available_cash", float("nan")), ("available_cash", True),
    ("account_equity", -1), ("reserved_cash", None),
    ("account_fingerprint", "b" * 64), ("account_scope_sha256", "b" * 64),
    ("orders_enabled", True), ("orders_placed", 1), ("orders_placed", False),
    ("working_order_count", None), ("working_order_count", -1), ("working_order_count", 0),
    ("observed_at", "2026-10-08T09:58:00Z"), ("observed_at", "2026-10-08T10:01:00Z"),
])
def test_invalid_real_account_evidence_still_rejects(key, invalid):
    value = raw_snapshot(); value[key] = invalid
    with pytest.raises(ValueError): project(value)


@pytest.mark.parametrize("invalid", [-1, float("nan"), True])
def test_invalid_real_position_quantity_rejects(invalid):
    value = raw_snapshot(); value["held_shares"]["AAPL"] = invalid
    with pytest.raises(ValueError): project(value)


class UnionSession(ReadSession):
    def get_equity_quotes(self, symbols):
        assert set(symbols) == set(SYMBOLS)
        self.calls.append("quotes")
        return {symbol: deepcopy(self.quotes["AAPL"]) for symbol in symbols}


def install_session(monkeypatch, session):
    monkeypatch.setattr("ml.gameplan_trade_snapshot.SchwabSession", lambda: session)
    monkeypatch.setattr("ml.gameplan_trade_snapshot._ownership", lambda *a, **k: pytest.fail("No trader ledger reads"))
    monkeypatch.setattr("ml.planning_reservations.retain_local_reservations", lambda *a, **k: pytest.fail("No planning reservations"))
    monkeypatch.setattr("ml.account_gameplan.preparation._native_snapshot", lambda *a, **k: pytest.fail("No legacy planning capture"))


def test_default_capture_reads_actual_account_once_without_any_trader_history(configured, monkeypatch, tmp_path):
    session = UnionSession(); session.orders = [buy_order()]
    install_session(monkeypatch, session)
    before = contents(tmp_path)
    result = module.capture_snapshot(configured[0], now=NOW,
        ownership_observations={"status": "REVIEW_REQUIRED", "private": "SECRET"})
    assert contents(tmp_path) == before
    assert result["held_shares"]["AAPL"] == 2 and result["available_cash"] == 1300
    assert result["reserved_cash"] == 200 and result["working_order_count"] == 1
    assert result["ownership_diagnostics"]["status"] == "NOT_REQUESTED"
    assert "ownership" not in result and "SECRET" not in json.dumps(result)
    assert all(session.calls.count(name) == 1 for name in ("prepare", "verify", "account", "orders", "quotes"))


@pytest.mark.parametrize("kind", ["none", "error", "missing"])
def test_actual_capture_does_not_require_available_quotes(configured, monkeypatch, kind):
    session = UnionSession()
    def quotes(symbols):
        session.calls.append("quotes")
        if kind == "error": raise RuntimeError("SECRET_PROVIDER_MESSAGE")
        return None if kind == "none" else {}
    monkeypatch.setattr(session, "get_equity_quotes", quotes)
    install_session(monkeypatch, session)
    result = module.capture_snapshot(configured[0], now=NOW)
    assert result["available_cash"] == 1500 and result["held_shares"]["AAPL"] == 2
    assert not any(quote for quote in result["quotes"].values())
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("damage", ["account", "negative_position", "orders"])
def test_raw_broker_invalid_identity_cash_positions_or_orders_reject(configured, monkeypatch, damage):
    session = UnionSession()
    if damage == "account": monkeypatch.setattr(session, "stable_account_fingerprint", lambda: "b" * 64)
    elif damage == "negative_position": session.account["securitiesAccount"]["positions"][0]["longQuantity"] = -1
    else: session.orders = None
    install_session(monkeypatch, session)
    with pytest.raises(ValueError, match="read-only snapshot capture failed"):
        module.capture_snapshot(configured[0], now=NOW)
