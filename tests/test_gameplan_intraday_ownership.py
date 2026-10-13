"""Native intraday ownership remains protected in read-only Gameplan evidence."""
from contextlib import closing, contextmanager
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


@contextmanager
def fixture_stream(ledger, stream, allocation_id=None):
    """Transpose only temporary fixture identity to PR88's native stream schema.

    The installed Atlas writer predates PR88's stream argument. PR88 writes the
    same stream in allocation/reservation markers and reservation request JSON;
    its table schema, share slices and fills are unchanged. Restore fixture bytes
    logically afterward so the older writer can construct the next lifecycle.
    """
    with closing(sqlite3.connect(ledger.path)) as db:
        markers = db.execute("SELECT kind,id,stream FROM intraday_streams").fetchall()
        requests = db.execute("SELECT id,allocation,request FROM reservations").fetchall()
        owners = {identity for kind, identity, _ in markers if kind == "allocation"
                  and (allocation_id is None or identity == allocation_id)}
        orders = {identity for identity, owner, _ in requests if owner in owners}
        for kind, identity, _ in markers:
            if identity in (owners if kind == "allocation" else orders):
                db.execute("UPDATE intraday_streams SET stream=? WHERE kind=? AND id=?", (stream, kind, identity))
        for identity, _, raw in requests:
            if identity in orders:
                value = json.loads(raw)
                value["stream"] = stream
                db.execute("UPDATE reservations SET request=? WHERE id=?", (json.dumps(value), identity))
        db.commit()
    try:
        yield
    finally:
        with closing(sqlite3.connect(ledger.path)) as db:
            for kind, identity, value in markers:
                db.execute("UPDATE intraday_streams SET stream=? WHERE kind=? AND id=?", (value, kind, identity))
            for identity, _, raw in requests:
                db.execute("UPDATE reservations SET request=? WHERE id=?", (raw, identity))
            db.commit()


def assert_native_matches(root, ledger, held, *, pending=False, stream="atlas-15m"):
    with fixture_stream(ledger, stream):
        return _assert_native_matches(root, ledger, held, pending=pending)


def _assert_native_matches(root, ledger, held, *, pending=False):
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
            assert row["stream"] == ledger.allocation_stream(item.allocation_id)
    assert actual["owned_shares"]["AAPL"] == sum(a.filled_shares for a in expected.allocations)
    assert {p.name: p.read_bytes() for p in ledger.path.parent.iterdir()} == before
    assert ACCOUNT not in json.dumps(actual)
    assert all(r.reservation_id not in json.dumps(actual) for r in expected.reservations)
    return actual


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_intraday_buy_and_multilot_sale_follow_native_accounting_without_writes(tmp_path, stream):
    ledger = native(tmp_path)
    buy = reserve(ledger, "buy", quantity=2)
    assert_native_matches(tmp_path, ledger, 12, pending=True, stream=stream)
    assert ledger.reconcile(portfolio("bought", "2026-10-12T17:15:05+00:00", held=14),
        order_evidence=(evidence(buy, "bought", filled=2),)).ready
    assert_native_matches(tmp_path, ledger, 14, stream=stream)
    sell = reserve(ledger, "sale", side="SELL", quantity=14, snapshot_id="bought",
                   as_of="2026-10-12T17:15:06+00:00")
    assert_native_matches(tmp_path, ledger, 14, pending=True, stream=stream)
    assert ledger.reconcile(portfolio("sold", "2026-10-12T17:15:08+00:00", held=0),
        order_evidence=(evidence(sell, "sold", filled=14, at="2026-10-12T17:15:07+00:00"),)).ready
    assert_native_matches(tmp_path, ledger, 0, stream=stream)


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_partial_sale_and_cancelled_remainder_preserve_real_owner_lots(tmp_path, stream, terminal):
    ledger = native(tmp_path)
    sell = reserve(ledger, side="SELL", quantity=12)
    status = "CANCELLED" if terminal else "PARTIAL"
    assert ledger.reconcile(portfolio("partial", "2026-10-12T17:15:05+00:00", held=9),
        order_evidence=(evidence(sell, "partial", filled=3, status=status),)).ready
    assert_native_matches(tmp_path, ledger, 9, pending=not terminal, stream=stream)


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_existing_intraday_owner_and_assigned_free_shares_share_distinct_slices(tmp_path, stream):
    ledger = native(tmp_path)
    buy = reserve(ledger, "buy", quantity=2)
    assert ledger.reconcile(portfolio("bought", "2026-10-12T17:15:05+00:00", held=14),
        order_evidence=(evidence(buy, "bought", filled=2),)).ready
    sell = reserve(ledger, "sale", side="SELL", quantity=14, snapshot_id="bought",
                   as_of="2026-10-12T17:15:06+00:00")
    slices = ledger.intraday_sell_attribution(sell.reservation_id)["slices"]
    assert sum(row["allocation"] == buy.allocation_id for row in slices) == 2
    assert_native_matches(tmp_path, ledger, 14, pending=True, stream=stream)
    assert ledger.reconcile(portfolio("sold", "2026-10-12T17:15:08+00:00", held=0),
        order_evidence=(evidence(sell, "sold", filled=14, at="2026-10-12T17:15:07+00:00"),)).ready
    assert_native_matches(tmp_path, ledger, 0, stream=stream)


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


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_intraday_lot_is_neither_unallocated_nor_expired_gameplan_stock(stream):
    state = snapshot(held=3)
    state["ownership"]["active_allocations"] = [dict(symbol="AAPL", horizon="15m",
        stream=stream, owned_shares=2, target_end="2026-09-09T10:00:00Z")]
    rows, report = run([forecast(probability=.4), forecast(hour=5, probability=.4)], state)
    assert rows.direction_based_trade_quantity.tolist() == [-1, 0]
    assert report["ending_positions"] == {"AAPL": 2}
    assert [e["reason"] for e in report["events"]] == ["BEARISH_SELL"]
    assert report["ending_allocations"][0]["horizon"] == "15m"
    assert report["ending_allocations"][0]["stream"] == stream


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_intraday_owned_stock_never_funds_cross_horizon_fallback(stream):
    from test_gameplan_fallback_planning import state, run as fallback_run
    value = state({"15m": 100})
    value["ownership"]["active_allocations"][0]["stream"] = stream
    rows, report = fallback_run(snapshot=value)
    assert rows.direction_based_trade_quantity.dropna().eq(0).all()
    assert report["events"] == []
    assert report["ending_positions"] == {"AAPL": 100}
    assert report["cross_horizon_fallback"]["donors"] == {}
    assert report["cross_horizon_fallback"]["symbol_daily_caps"] == {"AAPL": 0}


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_nightly_export_preserves_only_verified_intraday_stream(stream):
    import pandas as pd
    from test_nightly_account_snapshot import raw_snapshot, NOW as EXPORT_NOW, SCOPE, SYMBOLS
    from tools.nightly_account_snapshot import _project
    raw = raw_snapshot()
    row = raw["ownership"]["active_allocations"][0]
    row.update(horizon="15m", stream=stream)
    result = _project(raw, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(EXPORT_NOW))
    assert result["ownership"]["active_allocations"][0]["stream"] == stream
    assert "SECRET" not in json.dumps(result)
    row["stream"] = "unknown"
    with pytest.raises(ValueError, match="allocation identity or universe"):
        _project(raw, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(EXPORT_NOW))


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_legacy_planning_override_preserves_intraday_pending_cash(stream):
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
    row.update(horizon="15m", stream=stream, reserved_buy_shares=2)
    ownership["reserved_buy_cash_by_symbol"]["AAPL"] = 240
    planned, override = _planning_snapshot(raw)
    assert override is None
    assert planned["available_cash"] == raw["available_cash"] - 240
    assert planned["reserved_cash"] == 240
    assert planned["pending_buy_shares"]["AAPL"] == 2
    assert planned["ownership"]["active_allocations"][0]["reserved_buy_shares"] == 2
    assert ownership["safe_for_planning"] is False


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_joint_synthesis_protects_intraday_ownership_without_expanding_forecasts(stream):
    from test_joint_capital_plan import compose, package, snapshot as joint_state
    state = joint_state(cash=0)
    state["held_shares"]["AAPL"] = 3
    state["symbol_exposure"]["AAPL"] = state["stock_market_value_by_symbol"]["AAPL"] = 30
    state["gross_exposure"] = 30
    state["ownership"]["active_allocations"] = [dict(symbol="AAPL", horizon="15m",
        stream=stream, owned_shares=2, target_end="2026-09-09T10:00:00Z")]
    result = compose([package(probability=.5), package("atlas", "AAPL", .3)], state)
    assert result["ledger"]["ending_positions"]["AAPL"] == 2
    assert result["ledger"]["ending_allocations"][0]["stream"] == stream
    assert [(row["symbol"], row["action"], row["quantity"]) for row in result["ledger"]["events"]] == [("AAPL", "SELL", 1)]
    assert {row["model_group"] for row in result["forecasts"]} == {"1h", "4h", "1d", "1w"}
    assert result["live_orders_authorized"] is False


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
@pytest.mark.parametrize("damage", ["allocation_marker", "reservation_marker", "request_stream", "missing_request_stream", "unknown_stream", "malformed_request", "non_object_request"])
def test_reservation_stream_must_match_request_and_parent(tmp_path, stream, damage):
    ledger = native(tmp_path)
    order = reserve(ledger, side="SELL", quantity=12)
    with fixture_stream(ledger, stream):
        other = "scout-15m" if stream == "atlas-15m" else "atlas-15m"
        with closing(sqlite3.connect(ledger.path)) as db:
            if damage in {"allocation_marker", "reservation_marker", "unknown_stream"}:
                kind = "allocation" if damage == "allocation_marker" else "reservation"
                db.execute("UPDATE intraday_streams SET stream=? WHERE kind=?",
                           ("unknown" if damage == "unknown_stream" else other, kind))
            else:
                value = json.loads(db.execute("SELECT request FROM reservations WHERE id=?", (order.reservation_id,)).fetchone()[0])
                if damage == "missing_request_stream": value.pop("stream")
                else: value["stream"] = other
                raw = "{" if damage == "malformed_request" else "[]" if damage == "non_object_request" else json.dumps(value)
                db.execute("UPDATE reservations SET request=? WHERE id=?", (raw, order.reservation_id))
            db.commit()
        actual = observe(tmp_path, 12)
        assert actual["status"] == "UNAVAILABLE"
        assert actual["safe_for_planning"] is False
        assert actual["reason_codes"] == ["OWNERSHIP_READ_OR_CONSISTENCY_CHECK_FAILED"]


def test_mixed_native_streams_keep_distinct_owners_and_pending_quantities(tmp_path):
    ledger = native(tmp_path)
    atlas = reserve(ledger, "atlas", quantity=2)
    scout = reserve(ledger, "scout", symbol="AMZN", quantity=3)
    with fixture_stream(ledger, "scout-15m", scout.allocation_id):
        before = {p.name:p.read_bytes() for p in ledger.path.parent.iterdir()}
        result = observe(tmp_path, 12)
        assert result["reason_codes"] == ["PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION"]
        assert result["safe_for_planning"] is False
        rows = {row["stream"]:row for row in result["active_allocations"] if row["horizon"] == "15m"}
        assert {(key,row["symbol"],row["reserved_buy_shares"]) for key,row in rows.items()} == {
            ("atlas-15m","AAPL",2), ("scout-15m","AMZN",3)}
        assert {p.name:p.read_bytes() for p in ledger.path.parent.iterdir()} == before


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_closed_intraday_history_is_validated_without_relabeling(tmp_path, stream):
    ledger = native(tmp_path)
    order = reserve(ledger, "closed", quantity=2)
    assert ledger.reconcile(portfolio("cancelled", "2026-10-12T17:15:05+00:00", held=12),
        order_evidence=(evidence(order, "cancelled", filled=0, status="CANCELLED"),)).ready
    with fixture_stream(ledger, stream):
        # Native reconciliation closes a zero-quantity terminal allocation.
        allocation = next(row for row in ledger.snapshot().allocations if row.allocation_id == order.allocation_id)
        assert allocation.status == "CLOSED"
        result = observe(tmp_path, 12)
        assert result["safe_for_planning"] is True
        assert all(row["horizon"] != "15m" for row in result["active_allocations"])


def test_private_export_preserves_mixed_streams_and_original_route_uniqueness():
    from copy import deepcopy
    import pandas as pd
    from test_nightly_account_snapshot import raw_snapshot, NOW as EXPORT_NOW, SCOPE, SYMBOLS
    from tools.nightly_account_snapshot import _project
    raw = raw_snapshot()
    atlas = raw["ownership"]["active_allocations"][0]
    atlas.update(horizon="15m", stream="atlas-15m")
    scout = dict(atlas, allocation_id_sha256="e"*64, symbol="AMZN", stream="scout-15m")
    raw["ownership"]["active_allocations"].append(scout)
    for name in ("held_shares", "pending_sell_shares", "symbol_exposure", "stock_market_value_by_symbol"):
        raw[name]["AMZN"] = raw[name]["AAPL"]
    raw["ownership"]["owned_shares"]["AMZN"] = raw["ownership"]["owned_shares"]["AAPL"]
    raw["gross_exposure"] += raw["stock_market_value_by_symbol"]["AMZN"]
    result = _project(raw, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(EXPORT_NOW))
    assert {row["stream"] for row in result["ownership"]["active_allocations"]} == {"atlas-15m", "scout-15m"}
    duplicate = deepcopy(raw)
    duplicate["ownership"]["active_allocations"][1]["symbol"] = "AAPL"
    with pytest.raises(ValueError, match="allocation identity or universe"):
        _project(duplicate, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(EXPORT_NOW))


def test_mixed_stream_planning_preserves_stock_cash_and_identities():
    from test_gameplan_cash_ledger import path
    state = snapshot(symbols=("AAPL", "AMZN"), cash=0, held=3)
    state["ownership"]["active_allocations"] = [dict(symbol=symbol, horizon="15m",
        stream=stream, owned_shares=2, reserved_sell_shares=1, reserved_buy_shares=1,
        target_end="2026-09-09T10:00:00Z") for symbol, stream in
        (("AAPL", "atlas-15m"), ("AMZN", "scout-15m"))]
    state["pending_sell_shares"] = {"AAPL": 1, "AMZN": 1}
    state["pending_buy_shares"] = {"AAPL": 1, "AMZN": 1}
    state["reserved_cash"] = 22
    rows, report = run([forecast(symbol, probability=.4) for symbol in ("AAPL", "AMZN")],
                       state, path(("AAPL", "AMZN")))
    assert rows.direction_based_trade_quantity.tolist() == [-1, -1]
    assert report["ending_positions"] == {"AAPL": 2, "AMZN": 2}
    assert {(row["symbol"], row["stream"], row["reserved"]) for row in report["ending_allocations"]} == {
        ("AAPL", "atlas-15m", 1), ("AMZN", "scout-15m", 1)}


def test_snapshot_keeps_one_active_intraday_route_per_symbol(tmp_path):
    ledger = native(tmp_path)
    reserve(ledger, "atlas", quantity=2)
    scout = reserve(ledger, "scout", symbol="AMZN", quantity=3)
    with fixture_stream(ledger, "scout-15m", scout.allocation_id):
        with closing(sqlite3.connect(ledger.path)) as db:
            # Corrupt only the fixture index to exercise the reader's guard.
            db.execute("DROP INDEX one_active_horizon")
            db.execute("UPDATE allocations SET symbol='AAPL' WHERE id=?", (scout.allocation_id,))
            db.commit()
        result = observe(tmp_path, 12)
        assert result["safe_for_planning"] is False
        assert "DUPLICATE_ACTIVE_HORIZON" in result["reason_codes"]


@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_closed_history_still_requires_consistent_streams(tmp_path, stream):
    ledger = native(tmp_path)
    order = reserve(ledger, "closed", quantity=2)
    assert ledger.reconcile(portfolio("cancelled", "2026-10-12T17:15:05+00:00", held=12),
        order_evidence=(evidence(order, "cancelled", filled=0, status="CANCELLED"),)).ready
    with fixture_stream(ledger, stream):
        with closing(sqlite3.connect(ledger.path)) as db:
            db.execute("DELETE FROM intraday_streams WHERE kind='reservation'")
            db.commit()
        result = observe(tmp_path, 12)
        assert result["status"] == "UNAVAILABLE"
        assert result["safe_for_planning"] is False
