"""Native intraday ownership remains protected in read-only Gameplan evidence."""
from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from ml.gameplan_trade_snapshot import _ownership
from ml.stock_trader.horizon_ledger import HorizonLedger
from test_intraday_native_ledger import ACCOUNT, LOCAL, NOW, evidence, legacy_filled_lots, portfolio, reserve
from test_gameplan_cash_ledger import forecast, run, snapshot


def native(root):
    ledger = HorizonLedger(root / "state/independent-stock-trader/holdings.sqlite3", ACCOUNT)
    legacy_filled_lots(ledger)
    return ledger


def observe(root, held, at="2026-10-12T18:00:00+00:00"):
    return _ownership(root, LOCAL, ACCOUNT, {"AAPL": held, "AMZN": 0}, at)


def assert_native_matches(root, ledger, held, *, pending=False):
    expected = ledger.snapshot()
    before = {p.name: p.read_bytes() for p in ledger.path.parent.iterdir()}
    actual = observe(root, held)
    assert actual["safe_for_planning"] is not pending
    assert actual["reason_codes"] == (["PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION"] if pending else [])
    rows = {r["allocation_id_sha256"]: r for r in actual["active_allocations"]}
    for item in expected.allocations:
        if item.status != "ACTIVE":
            continue
        row = rows[hashlib.sha256(item.allocation_id.encode()).hexdigest()]
        assert (row["owned_shares"], row["reserved_buy_shares"], row["reserved_sell_shares"]) == (
            item.filled_shares, item.reserved_buy_shares, item.reserved_sell_shares)
        if item.horizon == "15m":
            assert row["stream"] == "atlas-15m"
    assert actual["owned_shares"]["AAPL"] == sum(a.filled_shares for a in expected.allocations)
    assert {p.name: p.read_bytes() for p in ledger.path.parent.iterdir()} == before
    assert ACCOUNT not in json.dumps(actual)
    assert all(r.reservation_id not in json.dumps(actual) for r in expected.reservations)
    return actual


def test_intraday_buy_and_multilot_sale_follow_native_accounting_without_writes(tmp_path):
    ledger = native(tmp_path)
    buy = reserve(ledger, "buy", quantity=2)
    assert_native_matches(tmp_path, ledger, 12, pending=True)
    assert ledger.reconcile(portfolio("bought", "2026-10-12T17:15:05+00:00", held=14),
        order_evidence=(evidence(buy, "bought", filled=2),)).ready
    assert_native_matches(tmp_path, ledger, 14)
    sell = reserve(ledger, "sale", side="SELL", quantity=14, snapshot_id="bought",
                   as_of="2026-10-12T17:15:06+00:00")
    assert_native_matches(tmp_path, ledger, 14, pending=True)
    assert ledger.reconcile(portfolio("sold", "2026-10-12T17:15:08+00:00", held=0),
        order_evidence=(evidence(sell, "sold", filled=14, at="2026-10-12T17:15:07+00:00"),)).ready
    assert_native_matches(tmp_path, ledger, 0)


@pytest.mark.parametrize("terminal", [False, True])
def test_partial_sale_and_cancelled_remainder_preserve_real_owner_lots(tmp_path, terminal):
    ledger = native(tmp_path)
    sell = reserve(ledger, side="SELL", quantity=12)
    status = "CANCELLED" if terminal else "PARTIAL"
    assert ledger.reconcile(portfolio("partial", "2026-10-12T17:15:05+00:00", held=9),
        order_evidence=(evidence(sell, "partial", filled=3, status=status),)).ready
    assert_native_matches(tmp_path, ledger, 9, pending=not terminal)


def test_existing_intraday_owner_and_assigned_free_shares_share_distinct_slices(tmp_path):
    ledger = native(tmp_path)
    buy = reserve(ledger, "buy", quantity=2)
    assert ledger.reconcile(portfolio("bought", "2026-10-12T17:15:05+00:00", held=14),
        order_evidence=(evidence(buy, "bought", filled=2),)).ready
    sell = reserve(ledger, "sale", side="SELL", quantity=14, snapshot_id="bought",
                   as_of="2026-10-12T17:15:06+00:00")
    slices = ledger.intraday_sell_attribution(sell.reservation_id)["slices"]
    assert sum(row["allocation"] == buy.allocation_id for row in slices) == 2
    assert_native_matches(tmp_path, ledger, 14, pending=True)
    assert ledger.reconcile(portfolio("sold", "2026-10-12T17:15:08+00:00", held=0),
        order_evidence=(evidence(sell, "sold", filled=14, at="2026-10-12T17:15:07+00:00"),)).ready
    assert_native_matches(tmp_path, ledger, 0)


@pytest.mark.parametrize("sql", [
    "DELETE FROM intraday_streams WHERE kind='allocation'",
    "UPDATE metadata SET value='unknown' WHERE key='intraday_accounting_version'",
    "DROP TABLE intraday_fill_attribution",
    "DELETE FROM intraday_sell_slices",
    "UPDATE intraday_sell_slices SET quantity=quantity+1 WHERE ordinal=0",
    "UPDATE intraday_sell_slices SET allocation='missing' WHERE ordinal=0",
    "UPDATE allocations SET symbol='AMZN' WHERE id=(SELECT allocation FROM intraday_sell_slices WHERE ordinal=0)",
    "UPDATE intraday_fill_attribution SET allocation='missing'",
    "UPDATE intraday_fill_attribution SET quantity=quantity+1",
    "UPDATE intraday_fill_attribution SET fill='missing'",
    "UPDATE intraday_fill_attribution SET slice_ordinal=99",
    "DELETE FROM intraday_fill_attribution",
    "UPDATE fills SET quantity=quantity+1 WHERE id='fill-partial'",
])
def test_incomplete_or_inconsistent_intraday_proof_stays_unavailable(tmp_path, sql):
    ledger = native(tmp_path)
    sell = reserve(ledger, side="SELL", quantity=12)
    assert ledger.reconcile(portfolio("partial", "2026-10-12T17:15:05+00:00", held=9),
        order_evidence=(evidence(sell, "partial", filled=3, status="PARTIAL"),)).ready
    with closing(sqlite3.connect(ledger.path)) as db:
        db.execute(sql)
        db.commit()
    result = observe(tmp_path, 9)
    assert result["safe_for_planning"] is False
    assert result["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("horizon,stream", [("15m", None), ("15m", "unknown"), ("5m", "atlas-15m")])
def test_unverified_intraday_lot_cannot_become_plannable(horizon, stream):
    state = snapshot(held=3)
    state["ownership"]["active_allocations"] = [dict(symbol="AAPL", horizon=horizon,
        stream=stream, owned_shares=3, target_end="2026-09-09T10:00:00Z")]
    with pytest.raises(ValueError, match="Invalid active horizon allocation"):
        run([forecast(probability=.4)], state)


def test_intraday_lot_is_neither_unallocated_nor_expired_gameplan_stock():
    state = snapshot(held=3)
    state["ownership"]["active_allocations"] = [dict(symbol="AAPL", horizon="15m",
        stream="atlas-15m", owned_shares=2, target_end="2026-09-09T10:00:00Z")]
    rows, report = run([forecast(probability=.4), forecast(hour=5, probability=.4)], state)
    assert rows.direction_based_trade_quantity.tolist() == [-1, 0]
    assert report["ending_positions"] == {"AAPL": 2}
    assert [e["reason"] for e in report["events"]] == ["BEARISH_SELL"]
    assert report["ending_allocations"][0]["horizon"] == "15m"


def test_intraday_owned_stock_never_funds_cross_horizon_fallback():
    from test_gameplan_fallback_planning import state, run as fallback_run
    value = state({"15m": 100})
    value["ownership"]["active_allocations"][0]["stream"] = "atlas-15m"
    rows, report = fallback_run(snapshot=value)
    assert rows.direction_based_trade_quantity.dropna().eq(0).all()
    assert report["events"] == []
    assert report["ending_positions"] == {"AAPL": 100}
    assert report["cross_horizon_fallback"]["donors"] == {}
    assert report["cross_horizon_fallback"]["symbol_daily_caps"] == {"AAPL": 0}


def test_nightly_export_preserves_only_verified_intraday_stream():
    import pandas as pd
    from test_nightly_account_snapshot import raw_snapshot, NOW as EXPORT_NOW, SCOPE, SYMBOLS
    from tools.nightly_account_snapshot import _project
    raw = raw_snapshot()
    row = raw["ownership"]["active_allocations"][0]
    row.update(horizon="15m", stream="atlas-15m")
    result = _project(raw, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(EXPORT_NOW))
    assert result["ownership"]["active_allocations"][0]["stream"] == "atlas-15m"
    assert "SECRET" not in json.dumps(result)
    row["stream"] = "unknown"
    with pytest.raises(ValueError, match="allocation identity or universe"):
        _project(raw, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(EXPORT_NOW))


def test_legacy_planning_override_preserves_intraday_pending_cash():
    from ml.gameplan_trade_planning import _planning_snapshot
    from ml.planning_reservations import PENDING
    from test_nightly_account_snapshot import raw_snapshot, SYMBOLS
    raw = raw_snapshot()
    raw.update(working_order_count=0, reserved_cash=0)
    raw["pending_buy_shares"] = dict.fromkeys(SYMBOLS, 0)
    raw["pending_sell_shares"] = dict.fromkeys(SYMBOLS, 0)
    ownership = raw["ownership"]
    ownership.update(safe_for_planning=False, reason_codes=[PENDING],
                     reserved_buy_cash_by_symbol=dict.fromkeys(SYMBOLS, 0))
    row = ownership["active_allocations"][0]
    row.update(horizon="15m", stream="atlas-15m", reserved_buy_shares=2)
    ownership["reserved_buy_cash_by_symbol"]["AAPL"] = 240
    planned, override = _planning_snapshot(raw)
    assert override is None
    assert planned["available_cash"] == raw["available_cash"] - 240
    assert planned["reserved_cash"] == 240
    assert planned["pending_buy_shares"]["AAPL"] == 2
    assert planned["ownership"]["active_allocations"][0]["reserved_buy_shares"] == 2
    assert ownership["safe_for_planning"] is False


def test_joint_synthesis_protects_intraday_ownership_without_expanding_forecasts():
    from test_joint_capital_plan import compose, package, snapshot as joint_state
    state = joint_state(cash=0)
    state["held_shares"]["AAPL"] = 3
    state["symbol_exposure"]["AAPL"] = state["stock_market_value_by_symbol"]["AAPL"] = 30
    state["gross_exposure"] = 30
    state["ownership"]["active_allocations"] = [dict(symbol="AAPL", horizon="15m",
        stream="atlas-15m", owned_shares=2, target_end="2026-09-09T10:00:00Z")]
    result = compose([package(probability=.5), package("atlas", "AAPL", .3)], state)
    assert result["ledger"]["ending_positions"]["AAPL"] == 2
    assert [(row["symbol"], row["action"], row["quantity"]) for row in result["ledger"]["events"]] == [("AAPL", "SELL", 1)]
    assert {row["model_group"] for row in result["forecasts"]} == {"1h", "4h", "1d", "1w"}
    assert result["live_orders_authorized"] is False
