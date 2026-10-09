from copy import deepcopy
import pytest
from ml.planning_reservations import retain_local_reservations, PENDING


def pending_snapshot():
    return {"status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED", "working_order_count": 0,
        "available_cash": 1000, "reserved_cash": 0, "held_shares": {"AAPL": 5},
        "pending_buy_shares": {"AAPL": 0}, "pending_sell_shares": {"AAPL": 0},
        "quotes": {"AAPL": {"ask": 101}}, "ownership": {"status": "REVIEW_REQUIRED",
        "safe_for_planning": False, "reason_codes": [PENDING], "account_matches": True,
        "last_saved_reconciliation_ready": True, "last_saved_reconciliation_at": "2026-10-08T23:00:00Z",
        "current_broker_reconciliation_performed": False, "blocked_symbols": [],
        "reserved_buy_cash_by_symbol": {"AAPL": 220},
        "active_allocations": [{"symbol": "AAPL", "horizon": "4h", "allocation_id_sha256": "a" * 64,
            "owned_shares": 3, "reserved_buy_shares": 2, "reserved_sell_shares": 1}]}}


def test_local_reservations_remain_unspendable_without_mutating_history():
    raw = pending_snapshot(); original = deepcopy(raw)
    result = retain_local_reservations(raw)
    assert raw == original
    assert result["available_cash"] == 780 and result["reserved_cash"] == 220
    assert result["pending_buy_shares"] == {"AAPL": 2}
    assert result["pending_sell_shares"] == {"AAPL": 1}
    assert result["ownership"]["active_allocations"] == raw["ownership"]["active_allocations"]
    assert result["ownership"]["last_saved_reconciliation_at"] == raw["ownership"]["last_saved_reconciliation_at"]
    assert result["ownership"]["current_broker_reconciliation_performed"] is False
    assert result["planning_reservation_evidence"]["broker_pending_buy_shares"] == {"AAPL": 0}
    assert retain_local_reservations(result) == result  # No repeat cash deduction.


def test_higher_quote_and_insufficient_cash_remain_conservative():
    raw = pending_snapshot(); raw["quotes"]["AAPL"]["ask"] = 150; raw["available_cash"] = 50
    result = retain_local_reservations(raw)
    assert result["reserved_cash"] == 300 and result["available_cash"] == 0


@pytest.mark.parametrize("problem", ["working", "pending", "wrong_account", "other_reason", "not_ready", "blocked", "missing_cost"])
def test_other_uncertainty_still_blocks(problem):
    raw = pending_snapshot()
    if problem == "working": raw["working_order_count"] = 1
    if problem == "pending": raw["pending_buy_shares"]["AAPL"] = 1
    if problem == "wrong_account": raw["ownership"]["account_matches"] = False
    if problem == "other_reason": raw["ownership"]["reason_codes"].append("BROKER_SHARES_BELOW_HORIZON_INVENTORY")
    if problem == "not_ready": raw["ownership"]["last_saved_reconciliation_ready"] = False
    if problem == "blocked": raw["ownership"]["blocked_symbols"] = ["AAPL"]
    if problem == "missing_cost": del raw["ownership"]["reserved_buy_cash_by_symbol"]
    assert retain_local_reservations(raw) == raw
