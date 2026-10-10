"""Netting examples and durable partial-order accounting, with synthetic inputs."""
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader.catchup import catchup_signals
from ml.stock_trader.contracts import QuoteState
from test_gameplan_direction_engine import inputs, build


NOW = "2026-09-08T12:15:00Z"


def plan(buy=1, sell=1):
    return {"action_date": "2026-09-08", "as_of": "2026-09-08T09:00:00Z", "plan_sha256": "a" * 64,
        "ledger": {"events": [
            {"forecast_id": "opening", "symbol": "AAPL", "horizon": "1h", "action": "BUY", "quantity": buy,
             "timestamp": "2026-09-08T11:00:00Z", "sequence": 1},
            {"forecast_id": "five", "symbol": "AAPL", "horizon": "1h", "action": "SELL", "quantity": sell,
             "timestamp": "2026-09-08T12:00:00Z", "sequence": 2}]}}


def reserved(signal, *, side="BUY", filled=0, remaining=0, status="WORKING", identity="r1"):
    return SimpleNamespace(forecast_id=signal.prediction_id, side=side, filled_quantity=filled,
                           reserved_quantity=remaining, status=status, reservation_id=identity)


def test_opposite_missed_instructions_cancel_and_starting_inventory_sale_nets():
    assert catchup_signals(plan(), as_of=NOW, source_fingerprint="run") == {}
    signals = catchup_signals(plan(sell=2), as_of=NOW, source_fingerprint="run")
    signal = signals["AAPL", "1h"]
    assert signal.planned_quantity == 1 and signal.calibrated_probability == 0
    _, portfolio = inputs()
    portfolio = replace(portfolio, observed_at=NOW, held_shares={"AAPL": 10},
        quotes={"AAPL": QuoteState("AAPL", 100, 100, 100, 100, 100, NOW)})
    decision, = build(signals, portfolio, bearish_sell_capacities={("AAPL", "1h"): 10}, decided_at=NOW)
    assert (decision.action, decision.quantity) == ("SELL", 1)


def test_partial_fill_working_quantity_and_cancelled_residual_are_exact():
    source = plan(buy=5, sell=1)
    initial = catchup_signals(source, as_of=NOW, source_fingerprint="run")["AAPL", "1h"]
    assert initial.planned_quantity == 4
    work = reserved(initial, filled=1, remaining=3)
    assert catchup_signals(source, as_of=NOW, source_fingerprint="run", reservations=[work]) == {}
    cancelled = reserved(initial, filled=1, remaining=0, status="CANCELLED")
    residual = catchup_signals(source, as_of=NOW, source_fingerprint="run", reservations=[cancelled])["AAPL", "1h"]
    assert residual.planned_quantity == 3 and residual.prediction_id != initial.prediction_id
    filled = reserved(residual, filled=3, status="FILLED", identity="r2")
    assert catchup_signals(source, as_of=NOW, source_fingerprint="run", reservations=[cancelled, filled]) == {}


def test_unrelated_manual_reservations_do_not_change_intended_delta():
    source = plan(buy=5, sell=1)
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="run")["AAPL", "1h"]
    external = reserved(replace(signal, prediction_id="manual-unrelated"), filled=100)
    actual = catchup_signals(source, as_of=NOW, source_fingerprint="run", reservations=[external])["AAPL", "1h"]
    assert actual == signal


def test_native_forecast_reservations_from_before_upgrade_are_not_replayed():
    source = plan(buy=5, sell=1)
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="run")["AAPL", "1h"]
    earlier = reserved(replace(signal, prediction_id="opening"), filled=5, status="FILLED")
    residual = catchup_signals(source, as_of=NOW, source_fingerprint="run", reservations=[earlier])["AAPL", "1h"]
    assert residual.planned_quantity == 1 and residual.calibrated_probability == 0


def test_future_intentions_wait_and_past_hours_remain_due_until_close():
    source = plan(buy=5, sell=1)
    assert catchup_signals(source, as_of="2026-09-08T11:15:00Z", source_fingerprint="run")["AAPL", "1h"].planned_quantity == 5
    assert catchup_signals(source, as_of="2026-09-08T23:50:00Z", source_fingerprint="run")["AAPL", "1h"].planned_quantity == 4
    assert catchup_signals(source, as_of="2026-09-09T00:00:00Z", source_fingerprint="run") == {}
    assert catchup_signals(source, as_of="2026-09-09T12:15:00Z", source_fingerprint="run") == {}


def test_current_cash_still_bounds_a_synthesized_quantity():
    signals = catchup_signals(plan(buy=5, sell=1), as_of=NOW, source_fingerprint="run")
    _, portfolio = inputs()
    portfolio = replace(portfolio, available_cash=200, observed_at=NOW,
        quotes={"AAPL": QuoteState("AAPL", 100, 100, 100, 100, 100, NOW)})
    decision, = build(signals, portfolio, decided_at=NOW)
    assert decision.action == "BUY" and decision.quantity <= 2
