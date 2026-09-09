from copy import deepcopy
from dataclasses import replace

import pytest

from ml.stock_trader.horizon_broker import (
    ORDER_HISTORY_LIMIT, OrderHistoryError, capture_order_evidence, normalize_order_evidence,
)
from ml.stock_trader.horizon_ledger import HorizonLedger
from test_stock_horizon_ledger import ACCOUNT, entry, portfolio


OBSERVED = "2026-09-08T11:00:30+00:00"


@pytest.fixture
def ledger(tmp_path):
    result = HorizonLedger(tmp_path / "horizons.sqlite3", ACCOUNT)
    result.reconcile(portfolio())
    return result


def submitted(ledger):
    reservation = entry(ledger)
    return ledger.mark_submission(reservation.reservation_id, status="SUBMITTED", broker_order_id="tracked-1",
                                 evidence_id="submission-1", observed_at="2026-09-08T11:00:11+00:00")


def raw_order(*, status="FILLED", filled=10):
    return {
        "orderId": "tracked-1", "status": status, "quantity": 10.0, "filledQuantity": float(filled),
        "remainingQuantity": float(10 - filled), "orderType": "LIMIT", "price": 100.0,
        "orderLegCollection": [{"legId": 1, "instruction": "BUY", "quantity": 10.0,
            "instrument": {"assetType": "EQUITY", "symbol": "COST"}}],
        "orderActivityCollection": ([{"activityType": "EXECUTION", "activityId": "activity-1",
            "executionLegs": [{"legId": 1, "executionId": "execution-1", "quantity": filled,
                               "price": 99.99, "time": "2026-09-08T11:00:20Z"}]}] if filled else []),
    }


class FakeHistory:
    def __init__(self, orders, accounts=(ACCOUNT, ACCOUNT)):
        self.orders = orders
        self.calls = []
        self.accounts = iter(accounts)

    def stable_account_fingerprint(self):
        return next(self.accounts)

    def get_orders(self, **kwargs):
        self.calls.append(kwargs)
        return self.orders


def test_no_pending_orders_never_contacts_broker(ledger):
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"Unexpected broker access: {name}")
    assert capture_order_evidence(Forbidden(), ledger, account_fingerprint=ACCOUNT, as_of=OBSERVED) == ()


def test_complete_fill_evidence_reconciles_without_inventing_inventory(ledger):
    reservation = submitted(ledger)
    session = FakeHistory([raw_order()])
    evidence = capture_order_evidence(session, ledger, account_fingerprint=ACCOUNT, as_of=OBSERVED)
    assert len(session.calls) == 1
    assert session.calls[0]["max_results"] == ORDER_HISTORY_LIMIT
    assert (session.calls[0]["to_entered_time"] - session.calls[0]["from_entered_time"]).days == 14
    assert ledger.lookup_reservation(reservation.reservation_id).filled_quantity == 0
    assert evidence[0].cumulative_filled_quantity == 10
    assert evidence[0].fills[0].price == "99.99"
    assert ledger.reconcile(portfolio("filled", "2026-09-08T11:00:31Z", 30), order_evidence=evidence).ready
    assert ledger.snapshot().allocations[0].filled_shares == 10


@pytest.mark.parametrize("damage", ["symbol", "side", "asset", "quantity", "leg_quantity", "second_leg", "order_id"])
def test_wrong_contract_or_order_never_becomes_evidence(ledger, damage):
    reservation = submitted(ledger)
    raw = raw_order()
    leg = raw["orderLegCollection"][0]
    if damage == "symbol": leg["instrument"]["symbol"] = "AAPL"
    elif damage == "side": leg["instruction"] = "SELL"
    elif damage == "asset": leg["instrument"]["assetType"] = "OPTION"
    elif damage == "quantity": raw["quantity"] = 11
    elif damage == "leg_quantity": leg["quantity"] = 11
    elif damage == "second_leg": raw["orderLegCollection"].append(deepcopy(leg))
    elif damage == "order_id": raw["orderId"] = "manual-order"
    with pytest.raises(OrderHistoryError):
        normalize_order_evidence(raw, reservation, account_fingerprint=ACCOUNT, observed_at=OBSERVED)
    assert ledger.snapshot().allocations[0].filled_shares == 0


@pytest.mark.parametrize("damage", ["no_details", "missing_timestamp", "future_timestamp", "wrong_leg", "fractional", "partial_details"])
def test_incomplete_or_ambiguous_fill_evidence_fails_closed(ledger, damage):
    reservation = submitted(ledger)
    raw = raw_order()
    fill = raw["orderActivityCollection"][0]["executionLegs"][0]
    if damage == "no_details": raw["orderActivityCollection"] = []
    elif damage == "missing_timestamp": fill.pop("time")
    elif damage == "future_timestamp": fill["time"] = "2026-09-08T11:00:31Z"
    elif damage == "wrong_leg": fill["legId"] = 2
    elif damage == "fractional": fill["quantity"] = 9.5
    elif damage == "partial_details": fill["quantity"] = 9
    with pytest.raises(OrderHistoryError):
        normalize_order_evidence(raw, reservation, account_fingerprint=ACCOUNT, observed_at=OBSERVED)


def test_repeated_identical_fills_without_ids_keep_stable_distinct_identities(ledger):
    reservation = submitted(ledger)
    raw = raw_order()
    base = raw["orderActivityCollection"][0]["executionLegs"][0]
    base.pop("executionId")
    base["quantity"] = 5
    raw["orderActivityCollection"][0]["executionLegs"].append(deepcopy(base))
    one = normalize_order_evidence(raw, reservation, account_fingerprint=ACCOUNT, observed_at=OBSERVED)
    raw["orderActivityCollection"][0]["executionLegs"].reverse()
    two = normalize_order_evidence(raw, reservation, account_fingerprint=ACCOUNT, observed_at=OBSERVED)
    assert one == two
    assert len({fill.fill_id for fill in one.fills}) == 2
    assert sum(fill.quantity for fill in one.fills) == 10


def test_fill_ids_do_not_change_when_new_cumulative_fills_are_added_or_reordered(ledger):
    reservation = submitted(ledger)
    raw = raw_order(status="WORKING", filled=4)
    one = normalize_order_evidence(raw, reservation, account_fingerprint=ACCOUNT, observed_at=OBSERVED)
    assert one.status == "PARTIAL"
    previous_fill = raw["orderActivityCollection"][0]["executionLegs"][0]
    raw["status"], raw["filledQuantity"], raw["remainingQuantity"] = "FILLED", 10, 0
    new_fill = {**previous_fill, "executionId": "execution-2", "quantity": 6, "time": "2026-09-08T11:00:25Z"}
    raw["orderActivityCollection"][0]["executionLegs"] = [new_fill, previous_fill]
    two = normalize_order_evidence(raw, reservation, account_fingerprint=ACCOUNT, observed_at=OBSERVED)
    assert one.fills[0] in two.fills


def test_expired_partial_order_retains_fills_but_releases_unfilled_reservation(ledger):
    reservation = submitted(ledger)
    evidence = normalize_order_evidence(raw_order(status="EXPIRED", filled=4), reservation,
                                       account_fingerprint=ACCOUNT, observed_at=OBSERVED)
    assert (evidence.status, evidence.broker_status, evidence.remaining_quantity) == ("CANCELLED", "EXPIRED", 0)
    assert ledger.reconcile(portfolio("cancel", "2026-09-08T11:00:31Z", 24), order_evidence=(evidence,)).ready
    assert ledger.snapshot().allocations[0].filled_shares == 4
    assert ledger.snapshot().allocations[0].reserved_buy_shares == 0


@pytest.mark.parametrize("response,reason", [([], "MISSING"), ({"orders": []}, "COMPLETE_LIST"),
    ([raw_order(), raw_order()], "DUPLICATE_ORDER_IDS"), ([raw_order()] * ORDER_HISTORY_LIMIT, "TRUNCATED")])
def test_history_completeness_failures_are_not_silently_ignored(ledger, response, reason):
    submitted(ledger)
    with pytest.raises(OrderHistoryError, match=reason):
        capture_order_evidence(FakeHistory(response), ledger, account_fingerprint=ACCOUNT, as_of=OBSERVED)


def test_identity_rotation_and_unknown_submissions_fail_closed(ledger):
    reserved = entry(ledger)
    session = FakeHistory([raw_order()])
    with pytest.raises(OrderHistoryError, match="WITHOUT_EXACT"):
        capture_order_evidence(session, ledger, account_fingerprint=ACCOUNT, as_of=OBSERVED)
    assert not session.calls
    ledger.mark_submission(reserved.reservation_id, status="SUBMITTED", broker_order_id="tracked-1",
                           evidence_id="accepted", observed_at="2026-09-08T11:00:11Z")
    session = FakeHistory([raw_order()], accounts=(ACCOUNT, "c" * 64))
    with pytest.raises(OrderHistoryError, match="CHANGED_DURING"):
        capture_order_evidence(session, ledger, account_fingerprint=ACCOUNT, as_of=OBSERVED)
    assert ledger.snapshot().allocations[0].filled_shares == 0


def test_observation_clock_captures_fills_during_the_history_request(ledger):
    submitted(ledger)
    raw = raw_order()
    raw["orderActivityCollection"][0]["executionLegs"][0]["time"] = "2026-09-08T11:00:31Z"
    session = FakeHistory([raw])
    evidence = capture_order_evidence(session, ledger, account_fingerprint=ACCOUNT, as_of=OBSERVED,
                                     observation_clock=lambda: "2026-09-08T11:00:32Z")
    assert evidence[0].observed_at == "2026-09-08T11:00:32+00:00"
    assert session.calls[0]["to_entered_time"].isoformat() == OBSERVED
    assert ledger.reconcile(portfolio("after-history", "2026-09-08T11:00:33Z", 30), order_evidence=evidence).ready


def test_order_capture_observation_clock_cannot_go_backwards(ledger):
    submitted(ledger)
    with pytest.raises(OrderHistoryError, match="CLOCK_MOVED_BACKWARDS"):
        capture_order_evidence(FakeHistory([raw_order()]), ledger, account_fingerprint=ACCOUNT, as_of=OBSERVED,
                               observation_clock=lambda: "2026-09-08T11:00:29Z")
