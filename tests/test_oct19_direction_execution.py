"""October 19 direction/autonomy regression checks; synthetic offline inputs."""
import pandas as pd
import pytest

from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
from ml.stock_trader.contracts import ActivationIntent, PortfolioState, PredictionSignal, QuoteState, StockTraderPolicy
from ml.stock_trader.gameplan_direction_engine import build_gameplan_direction_trade_decisions
from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, LedgerError, OrderEvidence, PortfolioEvidence
from ml.stock_trader.independent_runtime import _direction_sell_capacities
from ml.universe import PRODUCTION_LOOPS_SYMBOLS

ALL_SYMBOLS = tuple(PRODUCTION_LOOPS_SYMBOLS) + ("DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN")


ACCOUNT = "a" * 64
START = "2026-10-19T11:00:00+00:00"
NOW = "2026-10-19T11:00:10+00:00"
END = "2026-10-19T15:00:00+00:00"
ACTIVE = ActivationIntent(True, "ACTIVE", "synthetic manual activation", "fixture", "fixture-activation")


def signal(symbol, horizon, probability=.4872):
    return PredictionSignal(symbol, horizon, f"signal-{symbol}-{horizon}", START,
        START, END, END, START, probability, 0., {horizon: probability},
        "fixture", "fixture", "b" * 64, "PRE", STOCK_TARGET_CONTRACT_VERSION, "xnas-itch-archive-v1")


def portfolio(symbol, held, *, pending=0, cash=0):
    return PortfolioState(NOW, 100000., cash, held * 10., 0., {symbol: held}, {symbol: held * 10.},
        {}, {symbol: pending}, 0, {symbol: QuoteState(symbol, 10., 10., 10., 10., 1000., NOW)},
        "fixture-portfolio", "fixture-broker")


def evidence(symbol, snapshot, at, held):
    return PortfolioEvidence(snapshot, ACCOUNT, at, {symbol: held}, {symbol: 10.}, {symbol: 10000.}, "c" * 64)


def fill(order, quantity, *, at, status="FILLED"):
    return OrderEvidence("evidence-" + order.reservation_id, order.reservation_id, ACCOUNT, at,
        "broker-" + order.reservation_id, status, order.quantity, quantity,
        0 if status in {"FILLED", "CANCELLED"} else order.quantity - quantity,
        (FillEvidence("fill-" + order.reservation_id, quantity, 10., at),) if quantity else ())


def bought_owners(tmp_path, symbol, horizons):
    ledger = HorizonLedger(tmp_path / (symbol + ".sqlite3"), ACCOUNT)
    held, snapshot, at = 0, "initial", START
    assert ledger.reconcile(evidence(symbol, snapshot, at, held)).ready
    for index, horizon in enumerate(horizons):
        order = ledger.reserve_entry(symbol=symbol, horizon=horizon, forecast_id="owner-" + horizon,
            target_start=START, target_end=END, quantity=10, limit_price=10., snapshot_id=snapshot,
            idempotency_key="buy-" + horizon, batch_id="buy-" + horizon, as_of=at)
        held += 10
        at = (pd.Timestamp(START) + pd.Timedelta(seconds=index + 1)).isoformat()
        snapshot = "filled-" + horizon
        assert ledger.reconcile(evidence(symbol, snapshot, at, held), order_evidence=(fill(order, 10, at=at),)).ready
    assert ledger.reconcile(evidence(symbol, "sale", NOW, held + 5)).ready
    return ledger, held + 5


@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
@pytest.mark.parametrize("horizon,owners", [("1h", ("4h", "1d", "1w")), ("4h", ("1h", "1d", "1w"))])
def test_all_symbols_hourly_bearish_sells_any_horizon_without_fallback_quota(tmp_path, symbol, horizon, owners):
    ledger, held = bought_owners(tmp_path, symbol, owners)
    signals = {(symbol, horizon): signal(symbol, horizon)}
    state, current = ledger.snapshot(), portfolio(symbol, held)
    capacities = _direction_sell_capacities(signals, state, current, ())
    assert capacities[symbol, horizon] == held
    decision, = build_gameplan_direction_trade_decisions(signals, current, ACTIVE,
        verified_promoted_signals=frozenset(signals), active_allocations=frozenset((symbol, h) for h in owners),
        ledger_ready=True, bearish_sell_capacities=capacities, decided_at=NOW,
        policy=StockTraderPolicy(minimum_trade_probability=.99))
    assert decision.action == "SELL" and decision.quantity == held
    assert decision.prediction["position_purpose"] == "DIRECTION_EXIT"
    order = ledger.reserve_direction_exit(symbol=symbol, horizon=horizon, forecast_id=decision.prediction["prediction_id"],
        target_start=START, target_end=END, quantity=held, limit_price=10., snapshot_id="sale",
        idempotency_key="sale-" + horizon, batch_id="sale", as_of=NOW)
    assert ledger.reservation_stream(order.reservation_id) == "gameplan"
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == held
    # Cancel after selling all bought lots but none of the observed free stock.
    at = "2026-10-19T11:00:11+00:00"
    assert ledger.reconcile(evidence(symbol, "sold", at, 5),
        order_evidence=(fill(order, held - 5, at=at, status="CANCELLED"),)).ready
    assert all(a.filled_shares == 0 and a.reserved_sell_shares == 0 for a in ledger.snapshot().allocations)
    assert sum(row["quantity"] for row in ledger.intraday_sell_attribution(order.reservation_id)["fills"]) == held - 5


def test_competing_hourly_sales_are_bounded_by_real_shares_and_pending_sells(tmp_path):
    ledger, held = bought_owners(tmp_path, "AAPL", ("1w",))
    signals = {("AAPL", h): signal("AAPL", h) for h in ("1h", "4h")}
    current = portfolio("AAPL", held, pending=2)
    capacities = _direction_sell_capacities(signals, ledger.snapshot(), current, ())
    decisions = build_gameplan_direction_trade_decisions(signals, current, ACTIVE,
        verified_promoted_signals=frozenset(signals), active_allocations=frozenset(), ledger_ready=True,
        bearish_sell_capacities=capacities, decided_at=NOW)
    assert sum(d.quantity for d in decisions) == held - 2
    order = ledger.reserve_direction_exit(symbol="AAPL", horizon="1h", forecast_id="sale-1h",
        target_start=START, target_end=END, quantity=held - 2, limit_price=10., snapshot_id="sale",
        idempotency_key="sale-1h", batch_id="same-batch", as_of=NOW, pending_sell_shares=2)
    assert order.quantity == held - 2
    with pytest.raises(LedgerError, match="EXCEEDS_AVAILABLE_HELD_STOCK"):
        ledger.reserve_direction_exit(symbol="AAPL", horizon="4h", forecast_id="sale-4h",
            target_start=START, target_end=END, quantity=3, limit_price=10., snapshot_id="sale",
            idempotency_key="sale-4h", batch_id="same-batch", as_of=NOW, pending_sell_shares=2)


@pytest.mark.parametrize("horizon", ["1d", "1w"])
def test_daily_weekly_keep_own_inventory_rules(tmp_path, horizon):
    other = "1w" if horizon == "1d" else "1d"
    ledger, held = bought_owners(tmp_path, "AAPL", (other,))
    signals = {("AAPL", horizon): signal("AAPL", horizon)}
    capacities = _direction_sell_capacities(signals, ledger.snapshot(), portfolio("AAPL", held), ())
    assert capacities["AAPL", horizon] == 5
    with pytest.raises(LedgerError, match="EXCEEDS_AVAILABLE_HELD_STOCK"):
        ledger.reserve_direction_exit(symbol="AAPL", horizon=horizon, forecast_id="long-sale",
            target_start=START, target_end=END, quantity=held, limit_price=10., snapshot_id="sale",
            idempotency_key="long-sale", batch_id="sale", as_of=NOW)


@pytest.mark.parametrize("probability,direction", [(.49995, "BEARISH"), (.50005, "BULLISH")])
@pytest.mark.parametrize("version", [None, float("nan"), "stock-direction-50-v2"])
def test_historical_saved_direction_remains_readable_when_current_band_is_neutral(probability, direction, version):
    from ml.stock_direction_policy import stock_direction
    from ml.stock_trader.gameplan_execution import _validated_instructions
    row = {"id": "historical", "symbol": "ABCL", "model_group": "1h", "target_window_start": START,
           "calibrated_probability": probability, "direction": direction, "direction_policy_version": version}
    assert len(_validated_instructions(pd.DataFrame([row]))) == 1
    assert stock_direction(probability) == "NO_EDGE"
    row["direction"] = "BULLISH" if direction == "BEARISH" else "BEARISH"
    with pytest.raises(ValueError, match="disagrees"):
        _validated_instructions(pd.DataFrame([row]))
