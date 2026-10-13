"""Gameplan planning reads only its own trading epoch and broker capacity."""
import ast
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from ml.gameplan_trade_snapshot import _LEDGER_PATH, _ownership
from ml.stock_trader.horizon_ledger import HorizonLedger
from tests.test_gameplan_trade_snapshot import IDENTITY, STAMP, capture, ledger
from tests.test_gameplan_cash_ledger import forecast, run, snapshot


@pytest.mark.parametrize("legacy_bytes", [b"not a database", b"other trader degraded"])
def test_legacy_trader_state_cannot_block_empty_gameplan_epoch(tmp_path, legacy_bytes):
    legacy = tmp_path / "state/independent-stock-trader/holdings.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(legacy_bytes)
    result = capture(tmp_path)
    assert result["ownership"]["safe_for_planning"] is True
    assert result["ownership"]["fresh_gameplan_epoch"] is True
    assert result["ownership"]["owned_shares"] == {"AAPL": 0}
    assert result["held_shares"] == {"AAPL": 2}
    assert legacy.read_bytes() == legacy_bytes
    assert not (tmp_path / _LEDGER_PATH).exists()


def test_initialized_empty_ledger_needs_no_saved_reconciliation(tmp_path):
    HorizonLedger(tmp_path / _LEDGER_PATH, IDENTITY)
    observed = _ownership(tmp_path, ("AAPL",), IDENTITY, {"AAPL": 2}, STAMP)
    assert observed["safe_for_planning"] and observed["fresh_gameplan_epoch"]
    assert observed["owned_shares"] == {"AAPL": 0}
    assert observed["last_saved_reconciliation_at"] is None


def test_own_historical_fills_are_immutable_when_actual_capacity_is_smaller(tmp_path):
    own_path = ledger(tmp_path, shares=3)
    before = own_path.read_bytes()
    result = capture(tmp_path)
    own = result["ownership"]
    assert own["safe_for_planning"] is True
    assert own["recorded_owned_shares"] == {"AAPL": 3}
    assert own["owned_shares"] == {"AAPL": 2}
    assert own["active_allocations"][0]["recorded_owned_shares"] == 3
    assert own["active_allocations"][0]["owned_shares"] == 2
    assert own_path.read_bytes() == before
    with sqlite3.connect(own_path) as db:
        assert db.execute("SELECT filled FROM reservations").fetchone()[0] == 3


def test_fresh_epoch_exports_actual_cash_and_positions_without_owned_fills():
    from tests.test_nightly_account_snapshot import raw_snapshot, NOW, SCOPE, SYMBOLS
    from ml.gameplan_trade_snapshot import _empty_ownership
    from tools.nightly_account_snapshot import _project
    raw = raw_snapshot()
    raw["ownership"] = _empty_ownership(SYMBOLS)
    projected = _project(raw, scope=SCOPE, symbols=SYMBOLS, observed=pd.Timestamp(NOW))
    assert projected["ownership"]["fresh_gameplan_epoch"] is True
    assert projected["ownership"]["active_allocations"] == []
    assert projected["held_shares"] == raw["held_shares"]
    assert projected["available_cash"] == raw["available_cash"]
    assert "SECRET" not in json.dumps(projected)


def test_fresh_gameplan_can_plan_existing_account_stock():
    state = snapshot(held=2)
    from ml.gameplan_trade_snapshot import _empty_ownership
    state["ownership"] = _empty_ownership(("AAPL",))
    rows, report = run([forecast(probability=.49)], state)
    assert rows.iloc[0].direction_based_trade_quantity < 0
    assert rows.iloc[0].projected_shares_after >= 0


def test_gameplan_runtime_does_not_import_or_emit_other_trader_checks():
    from ml.stock_trader import independent_runtime as runtime
    source = Path(runtime.__file__).read_text()
    imports = [node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    assert not any(name and name.startswith("ml.intraday") for name in imports)
    assert "intraday_runtime_compatibility" not in source
    assert runtime.LEDGER_RELATIVE_PATH == _LEDGER_PATH
    from ml.account_gameplan.execution import AUTHORITY_PATH
    assert AUTHORITY_PATH.parent == _LEDGER_PATH.parent


def test_pending_account_sells_reduce_planning_capacity_without_reading_their_owner(tmp_path):
    from tests.test_gameplan_trade_snapshot import ReadSession, buy_order
    own_path = ledger(tmp_path, shares=3)
    original = own_path.read_bytes()
    session = ReadSession()
    order = buy_order()
    order.update(quantity=1, filledQuantity=0, remainingQuantity=1)
    order["orderLegCollection"][0].update(instruction="SELL", quantity=1)
    session.orders = [order]
    result = capture(tmp_path, session)
    assert result["ownership"]["safe_for_planning"]
    assert result["ownership"]["recorded_owned_shares"]["AAPL"] == 3
    assert result["ownership"]["owned_shares"]["AAPL"] == 1
    assert result["pending_sell_shares"]["AAPL"] == 1
    assert own_path.read_bytes() == original
