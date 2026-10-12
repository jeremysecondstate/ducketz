from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ml.intraday.book import Capacity, Fill, OfflineBook, OrderEvidence, Request
from ml.intraday.prediction import ContractError, UnresolvedPolicy

NOW = "2026-10-08T17:16:00+00:00"


def book(tmp_path, cash="1000"):
    result = OfflineBook(tmp_path / "offline-intraday.sqlite3", account_id="offline-account",
                         atlas_symbols=("AAPL",), gameplan_symbols=("AAPL", "ABCL"))
    result.capacity(Capacity("initial", NOW, cash, cash, {"AAPL": cash, "ABCL": cash}))
    return result


def buy(rid="buy", qty=10, **kwargs):
    return Request(rid, "forecast-" + rid, "AAPL", "BUY", qty, "100", "atlas-15m", **kwargs)


def evidence(rid, *, qty=10, status="FILLED", fills=None, observed=NOW, eid=None):
    return OrderEvidence(eid or rid + "-evidence", rid, "broker-" + rid, observed, status, qty,
                         fills if fills is not None else (Fill("fill-" + rid, qty, "100", observed),))


def test_durable_idempotence_capacity_and_no_account_wide_unknown_veto(tmp_path):
    ledger = book(tmp_path)
    reserved = ledger.reserve(buy(qty=4), admission_id="fixture-admission")
    assert reserved["permitted"] == 4
    assert ledger.reserve(buy(qty=4), admission_id="retry") == reserved
    ledger.begin_submit("buy")
    ledger.unknown("buy")
    reopened = OfflineBook(ledger.path, account_id="offline-account", atlas_symbols=("AAPL",), gameplan_symbols=("AAPL", "ABCL"))
    assert reopened.order("buy")["status"] == "UNKNOWN"
    assert reopened.reserve(buy("other", qty=8), admission_id="other-admitted")["permitted"] == 6
    with pytest.raises(ContractError):
        reopened.begin_submit("buy")
    with pytest.raises(ContractError, match="changed intent"):
        reopened.reserve(buy(qty=5), admission_id="bad-retry")
    with pytest.raises(ContractError, match="Forecast"):
        reopened.reserve(replace(buy("another"), forecast_id="forecast-buy"), admission_id="bad-duplicate")


def test_atomic_commitment_prevents_two_streams_spending_same_pool(tmp_path):
    ledger = book(tmp_path, cash="500")
    def admit(rid):
        request = buy(rid, qty=4)
        if rid == "gameplan":
            request = replace(request, stream="gameplan", horizon="1h")
        return ledger.reserve(request, admission_id="explicit-fixture-" + rid)["permitted"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        permitted = list(pool.map(admit, ("intraday", "gameplan")))
    assert sorted(permitted) == [1, 4]
    assert sum(permitted) == 5


def test_purchases_stay_charged_until_identified_balance_evidence(tmp_path):
    ledger = book(tmp_path)
    ledger.reserve(buy(), admission_id="purchase")
    ledger.begin_submit("buy")
    ledger.apply(evidence("buy"))
    with pytest.raises(ContractError, match="capacity"):
        ledger.reserve(buy("next", qty=1), admission_id="no-new-cash")
    ledger.capacity(Capacity("new-balance", "2026-10-08T17:17:00+00:00", "700", "700", {"AAPL": "700"},
                             accounted_fill_ids=("fill-buy",)))
    assert ledger.reserve(buy("next", qty=8), admission_id="new-evidence")["permitted"] == 7
    with pytest.raises(ContractError, match="actual known"):
        ledger.capacity(Capacity("bad", "2026-10-08T17:18:00+00:00", "900", "900", {"AAPL": "900"},
                                 accounted_fill_ids=("fictional-fill",)))


def test_matching_working_order_is_counted_once(tmp_path):
    ledger = book(tmp_path)
    ledger.reserve(buy(qty=4), admission_id="purchase")
    ledger.begin_submit("buy")
    ledger.apply(evidence("buy", qty=4, status="WORKING", fills=()))
    ledger.capacity(Capacity("working-snapshot", "2026-10-08T17:17:00+00:00", "600", "600", {"AAPL": "600"},
                             included_working={"buy": 4}))
    assert ledger.reserve(buy("next", qty=8), admission_id="next")["permitted"] == 6


def test_fill_after_matched_working_snapshot_does_not_double_charge(tmp_path):
    ledger = book(tmp_path)
    ledger.reserve(buy(qty=4), admission_id="purchase")
    ledger.begin_submit("buy")
    ledger.apply(evidence("buy", qty=4, status="WORKING", fills=()))
    ledger.capacity(Capacity("working-snapshot", "2026-10-08T17:17:00+00:00", "600", "600", {"AAPL": "600"},
                             included_working={"buy": 4}))
    ledger.apply(evidence("buy", qty=4, status="PARTIAL", fills=(Fill("later-fill", 1, "100", "2026-10-08T17:18:00+00:00"),),
                          observed="2026-10-08T17:18:00+00:00", eid="partial-after-snapshot"))
    assert ledger.reserve(buy("next", qty=8), admission_id="next")["permitted"] == 6


def sale(rid="sale", qty=4, **kwargs):
    return Request(rid, "forecast-" + (kwargs.get("parent_request") or rid), "AAPL", "SELL", qty, "100", "atlas-15m",
                   allocation_caps=(("hourly", qty),), sale_policy_version="offline-donor-fixture-not-production", **kwargs)


def test_sale_adjustment_partial_fills_cancel_race_and_remaining_replacement(tmp_path):
    ledger = book(tmp_path)
    ledger.import_allocation(allocation_id="hourly", symbol="AAPL", horizon="1h", held=3,
                             completed_purchase_id="completed-hourly-purchase")
    assert ledger.reserve(sale(), admission_id="fixture-sale")["permitted"] == 3
    with pytest.raises(ContractError, match="capacity"):
        ledger.reserve(sale("other", qty=1), admission_id="other-sale")
    ledger.begin_submit("sale")
    f1 = Fill("sell-fill-1", 1, "101", NOW)
    ledger.apply(evidence("sale", qty=3, status="PARTIAL", fills=(f1,)))
    ledger.cancel_requested("sale")
    assert ledger.order("sale")["status"] == "CANCEL_PENDING"
    with pytest.raises(ContractError, match="cancellation"):
        ledger.reserve(sale("replacement", qty=1, parent_request="sale"), admission_id="premature")
    f2 = Fill("sell-fill-2", 1, "102", "2026-10-08T17:16:02+00:00")
    cancelled = ledger.apply(evidence("sale", qty=3, status="CANCELLED", fills=(f1, f2),
                                     observed="2026-10-08T17:16:03+00:00", eid="cancel-confirmed"))
    assert cancelled["filled"] == 2 and cancelled["remaining"] == 1
    assert cancelled["average_fill_price"] == "101.5"
    assert ledger.snapshot()["allocations"]["hourly"]["held"] == 1
    with pytest.raises(ContractError, match="remaining"):
        ledger.reserve(sale("replacement", qty=2, parent_request="sale"), admission_id="too-many")
    assert ledger.reserve(sale("replacement", qty=1, parent_request="sale"), admission_id="remainder")["permitted"] == 1
    with pytest.raises(ContractError, match="replacement"):
        ledger.reserve(sale("second", qty=1, parent_request="sale"), admission_id="duplicate-remainder")
    ledger.begin_submit("replacement")
    ledger.apply(evidence("replacement", qty=1))
    allocation = ledger.snapshot()["allocations"]["hourly"]
    assert allocation["held"] == 0
    assert allocation["completed_purchase_id"] == "completed-hourly-purchase"
    assert len(ledger.history()) > 5


def test_completed_gameplan_purchase_is_not_replenished_after_intraday_sale(tmp_path):
    ledger = book(tmp_path)
    original = replace(buy(), stream="gameplan", horizon="1h")
    ledger.reserve(original, admission_id="gameplan")
    ledger.begin_submit("buy")
    ledger.apply(evidence("buy"))
    req = replace(sale(qty=3), allocation_caps=(("purchase:buy", 3),))
    ledger.reserve(req, admission_id="intraday")
    ledger.begin_submit("sale")
    ledger.apply(evidence("sale", qty=3))
    assert ledger.snapshot()["allocations"]["purchase:buy"]["held"] == 7
    assert ledger.reserve(original, admission_id="gameplan-retry")["status"] == "FILLED"
    assert ledger.order("buy")["filled"] == 10
    # Sale proceeds do not become spendable without new account evidence.
    with pytest.raises(ContractError, match="capacity"):
        ledger.reserve(buy("later", qty=1), admission_id="later")


def test_sale_policy_hierarchy_and_execution_binding_are_not_inferred(tmp_path):
    ledger = book(tmp_path)
    ledger.import_allocation(allocation_id="hourly", symbol="AAPL", horizon="1h", held=3, completed_purchase_id="hourly-buy")
    with pytest.raises(UnresolvedPolicy, match="ceilings"):
        ledger.reserve(replace(sale(), sale_policy_version=None), admission_id="no-policy")
    ledger.import_allocation(allocation_id="short", symbol="AAPL", horizon="15m", held=3, completed_purchase_id="short-buy")
    with pytest.raises(ContractError, match="shorter"):
        ledger.reserve(replace(sale(), stream="gameplan", horizon="1h", allocation_caps=(("short", 2),)), admission_id="wrong-way")
    with pytest.raises(ContractError, match="Scout"):
        ledger.reserve(replace(buy(), stream="scout-15m"), admission_id="scout")
    with pytest.raises(ContractError, match="scope"):
        ledger.reserve(replace(buy(), symbol="ABCL"), admission_id="peer-symbol")
    before = ledger.snapshot()
    assert ledger.reconcile_holdings({"AAPL": 5})["AAPL"] == {"tracked": 6, "observed": 5}
    assert ledger.snapshot() == before


def test_evidence_cannot_rewrite_fill_or_release_unknown_from_timeout(tmp_path):
    ledger = book(tmp_path)
    ledger.reserve(buy(qty=3), admission_id="purchase")
    ledger.begin_submit("buy")
    partial = evidence("buy", qty=3, status="PARTIAL", fills=(Fill("f1", 2, "99", NOW),))
    ledger.apply(partial)
    assert ledger.apply(partial)["filled"] == 2
    with pytest.raises(ContractError, match="omit"):
        ledger.apply(replace(partial, evidence_id="missing-fills", fills=()))
    with pytest.raises(ContractError, match="protection"):
        ledger.apply(replace(partial, evidence_id="bad-price", fills=(Fill("f1", 2, "101", NOW),)))
    with pytest.raises(ContractError, match="Complete"):
        ledger.apply(replace(partial, evidence_id="timeout", complete=False, status="CANCELLED"))
    assert ledger.order("buy")["filled"] == 2


def test_increased_replacement_price_reserves_additional_cost(tmp_path):
    ledger = book(tmp_path, cash="300")
    original = buy(qty=3)
    ledger.reserve(original, admission_id="original")
    ledger.begin_submit("buy")
    ledger.apply(evidence("buy", qty=3, status="CANCELLED", fills=(Fill("f1", 2, "100", NOW),)))
    remaining = replace(buy("replace", qty=1), forecast_id=original.forecast_id, limit="101", parent_request="buy")
    with pytest.raises(ContractError, match="capacity"):
        ledger.reserve(remaining, admission_id="extra-cost-unavailable")
    assert "replace" not in ledger.snapshot()["orders"]


def test_explicit_multi_allocation_fixture_preserves_hierarchy_and_fill_attribution(tmp_path):
    ledger = book(tmp_path)
    ledger.import_allocation(allocation_id="z-short", symbol="AAPL", horizon="15m", held=1, completed_purchase_id="old-short")
    ledger.import_allocation(allocation_id="a-long", symbol="AAPL", horizon="1h", held=3, completed_purchase_id="old-hourly")
    req = replace(sale(qty=3), allocation_caps=(("z-short", 1), ("a-long", 2)))
    ledger.reserve(req, admission_id="explicit-spanning-fixture")
    ledger.begin_submit("sale")
    ledger.apply(evidence("sale", qty=3, status="PARTIAL", fills=(Fill("f1", 2, "100", NOW),)))
    state = ledger.snapshot()
    assert state["fills"]["f1"]["allocation_effects"] == {"z-short": -1, "a-long": -1}
    assert state["allocations"]["z-short"]["held"] == 0
    assert state["allocations"]["a-long"]["held"] == 2


def test_completed_fill_evidence_survives_same_record_retry_and_stale_updates_fail(tmp_path):
    ledger = book(tmp_path)
    ledger.reserve(buy(qty=1), admission_id="buy")
    ledger.begin_submit("buy")
    complete = evidence("buy", qty=1)
    assert ledger.apply(complete)["filled"] == 1
    assert ledger.apply(complete)["filled"] == 1
    with pytest.raises(ContractError, match="Stale"):
        ledger.apply(replace(complete, evidence_id="stale", observed_at="2026-10-08T17:15:59+00:00"))
    assert ledger.snapshot()["allocations"]["purchase:buy"]["held"] == 1
