from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import audit, daily_adaptation, handoff, inputs
from ml.stock_trader import runtime
from ml.stock_trader.audit import _pair_decision_with_reality
from ml.stock_trader.contracts import (
    ActivationIntent,
    EnrichmentOutput,
    PortfolioState,
    PredictionSignal,
    QuoteState,
    STOCK_TRADER_SYMBOLS,
    StockTraderPolicy,
)
from ml.stock_trader.control import read_activation_intent, write_activation_intent
from ml.stock_trader.engine import build_trade_decisions
from ml.stock_trader.execution_lifecycle import (
    build_stock_trader_execution_lifecycle,
)
from ml.stock_trader.model import ENRICHMENT_FEATURE_NAMES, model_from_payload
from ml.stock_trader.publication import publish_decision_run, read_decision_run
from ml.stock_trader.publication import (
    read_execution_event,
    record_execution_result,
    reserve_execution_intent,
)
from ml.stock_trader.reconciliation import reconcile_submitted_orders
from ml.stock_trader.session import (
    checkpoint_session_for_target,
    decision_targets_open,
    next_stock_target_start,
    stock_execution_window,
    time_in_force_for_checkpoint,
)
from ml.stock_trader.state import capture_portfolio_state
from ml.stock_trader.training import fit_enrichment_model_payload


NOW = "2026-08-31T16:00:00+00:00"


@dataclass
class ConstantModel:
    expected_net_return: float = 0.01
    allocation_fraction: float = 0.50
    urgency: float = 0.10
    model_name: str = "test-enrichment"
    model_version: str = "v1"
    model_fingerprint: str = "test-model-fingerprint"

    def predict(self, feature_values: dict[str, float]) -> EnrichmentOutput:
        return EnrichmentOutput(
            model_name=self.model_name,
            model_version=self.model_version,
            model_fingerprint=self.model_fingerprint,
            trade_probability=0.90,
            allocation_fraction=self.allocation_fraction,
            expected_net_return=self.expected_net_return,
            adverse_return=0.02,
            execution_urgency=self.urgency,
            limit_offset_bps=2.0,
            protective_distance_pct=0.03,
            expected_holding_minutes=60.0,
            feature_values=dict(feature_values),
        )


class FakeSchwab:
    def __init__(self) -> None:
        self.account_calls = 0
        self.order_calls = 0
        self.quote_calls = 0
        self.submitted: list[dict[str, object]] = []

    def get_account(self) -> dict[str, object]:
        self.account_calls += 1
        return {
            "securitiesAccount": {
                "currentBalances": {
                    "liquidationValue": 100_000.0,
                    "cashAvailableForTrading": 25_000.0,
                    "availableFundsNonMarginableTrade": 24_000.0,
                    "buyingPowerNonMarginableTrade": 26_000.0,
                    "settledCash": 23_000.0,
                    "cashBalance": 24_500.0,
                },
                "positions": [
                    {
                        "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                        "longQuantity": 10.0,
                        "shortQuantity": 0.0,
                        "quantity": 10.0,
                        "settledLongQuantity": 10.0,
                        "settledShortQuantity": 0.0,
                        "settledQuantity": 10.0,
                        "marketPrice": 100.0,
                        "marketValue": 1_000.0,
                        "costBasis": 900.0,
                        "averagePrice": 90.0,
                        "longOpenProfitLoss": 100.0,
                        "currentDayProfitLoss": 10.0,
                    }
                ],
            }
        }

    def get_open_orders(self) -> list[object]:
        self.order_calls += 1
        return []

    def get_equity_quotes(self, symbols: tuple[str, ...]) -> dict[str, object]:
        self.quote_calls += 1
        assert symbols == STOCK_TRADER_SYMBOLS
        return {
            symbol: {
                "bidPrice": 99.90,
                "askPrice": 100.10,
                "lastPrice": 100.0,
                "mark": 100.0,
                "totalVolume": 1_000_000,
            }
            for symbol in symbols
        }

    def submit_order(self, order_payload: dict[str, object]) -> str:
        self.submitted.append(order_payload)
        return f"https://api.schwabapi.com/trader/v1/accounts/hidden/orders/{len(self.submitted)}"


class NeverCalledSchwab(FakeSchwab):
    def get_account(self) -> dict[str, object]:  # pragma: no cover - assertion path
        raise AssertionError("inactive trader must not contact Schwab")


class FilledOrderHistory:
    def get_recent_orders(self) -> list[dict[str, object]]:
        return [
            {
                "orderId": "12345",
                "status": "FILLED",
                "orderType": "LIMIT",
                "session": "NORMAL",
                "duration": "DAY",
                "quantity": 10.0,
                "filledQuantity": 10.0,
                "remainingQuantity": 0.0,
                "price": 100.2,
                "orderActivityCollection": [
                    {
                        "executionLegs": [
                            {"quantity": 4.0, "price": 100.15},
                            {"quantity": 6.0, "price": 100.25},
                        ]
                    }
                ],
            }
        ]


def test_operator_intent_is_one_persistent_true_false_switch(tmp_path: Path) -> None:
    missing = read_activation_intent(tmp_path)
    assert missing.active is False
    assert missing.reason == "OPERATOR_INTENT_MISSING"

    path = write_activation_intent(tmp_path, active=True)
    assert path.read_text(encoding="utf-8") == "CONFIRM_ACTIVE_TRADING=TRUE\n"
    active = read_activation_intent(tmp_path)
    assert active.active is True
    assert active.reason == "OPERATOR_INTENT_TRUE"

    write_activation_intent(tmp_path, active=False)
    inactive = read_activation_intent(tmp_path)
    assert inactive.active is False
    assert inactive.reason == "OPERATOR_INTENT_FALSE"

    path.write_text("CONFIRM_ACTIVE_TRADING=YES\n", encoding="utf-8")
    malformed = read_activation_intent(tmp_path)
    assert malformed.active is False
    assert malformed.reason == "OPERATOR_INTENT_MALFORMED"


def test_state_capture_reads_each_shared_input_once_and_sizes_cash(tmp_path: Path) -> None:
    schwab = FakeSchwab()
    state = capture_portfolio_state(schwab, observed_at=NOW, parallel=False)

    assert (schwab.account_calls, schwab.order_calls, schwab.quote_calls) == (1, 1, 1)
    assert state.account_equity == 100_000.0
    assert state.available_cash == 24_000.0
    assert state.held_shares["AAPL"] == 10.0
    assert state.symbol_exposure["AAPL"] == 1_000.0
    assert set(state.quotes) == set(STOCK_TRADER_SYMBOLS)


def test_option_only_working_metadata_gap_does_not_block_stock_state() -> None:
    schwab = FakeSchwab()
    schwab.get_open_orders = lambda: [
        {
            "orderId": "option-order",
            "status": "WORKING",
            "orderType": "NET_DEBIT",
            "orderStrategyType": "SINGLE",
            "complexOrderStrategyType": "VERTICAL",
            "quantity": 1.0,
            "filledQuantity": 0.0,
            "remainingQuantity": 1.0,
            "price": 1.25,
            "childOrderStrategies": [],
            "orderLegCollection": [
                {
                    "instruction": "BUY_TO_OPEN",
                    "quantity": 1.0,
                    "instrument": {
                        "assetType": "OPTION",
                        "symbol": "MU260918C00100000",
                    },
                },
                {
                    "instruction": "SELL_TO_OPEN",
                    "quantity": 1.0,
                    "instrument": {
                        "assetType": "OPTION",
                        "symbol": "MU260918C00110000",
                    },
                },
            ],
        }
    ]

    state = capture_portfolio_state(schwab, observed_at=NOW, parallel=False)

    assert state.available_cash == 24_000.0
    assert state.working_order_count == 1
    assert state.pending_buy_shares == {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}
    assert state.pending_sell_shares == {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}


def test_prediction_loader_uses_only_current_actionable_live_loop_b_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "ml" / "runs" / "loop-b-test"
    run.mkdir(parents=True)
    rows: list[dict[str, object]] = []
    for symbol in STOCK_TRADER_SYMBOLS:
        for horizon in ("1h", "4h", "1d", "1w"):
            target_start = (
                "2026-08-31T18:00:00+00:00"
                if horizon == "1h"
                else "2026-08-31T17:00:00+00:00"
            )
            rows.append(
                {
                    "id": f"{symbol}-{horizon}",
                    "symbol": symbol,
                    "provider": "databento",
                    "horizon": horizon,
                    "decision_timestamp": "2026-08-31T15:55:00+00:00",
                    "information_available_at": "2026-08-31T15:57:00+00:00",
                    "target_window_start": target_start,
                    "target_window_end": (
                        pd.Timestamp(target_start) + pd.Timedelta(hours=1)
                    ).isoformat(),
                    "actionable_until": target_start,
                    "target_definition_version": "test-v1",
                    "target_specification": "test",
                    "prediction_created_at": "2026-08-31T15:58:00+00:00",
                    "model_name": "loop-b-test",
                    "model_version": "v1",
                    "calibration_method": "isotonic",
                    "prediction_mode": "LIVE",
                    "prediction_status": "CREATED",
                    "assumed_round_trip_cost": 0.001,
                    "raw_probability": 0.65,
                    "calibrated_probability": 0.70,
                }
            )
    rows.append({**rows[0], "id": "ignored-backtest", "prediction_mode": "BACKTEST"})
    pd.DataFrame(rows).to_parquet(run / "predictions.parquet", index=False)
    (run / "manifest.json").write_text("{}\n", encoding="utf-8")
    (run / "publication.json").write_text("{}\n", encoding="utf-8")
    pointer = tmp_path / "ml" / "latest" / "run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        inputs,
        "read_current_publication",
        lambda _root: SimpleNamespace(
            run_directory=run,
            manifest={},
            receipt={},
            pointer={},
        ),
    )

    signals, sources = inputs.load_current_prediction_signals(tmp_path, as_of=NOW)

    assert set(signals) == set(STOCK_TRADER_SYMBOLS)
    assert signals["AAPL"].prediction_id == "AAPL-4h"
    assert signals["AAPL"].primary_horizon == "4h"
    assert signals["AAPL"].checkpoint_session == "REGULAR"
    assert signals["AAPL"].target_definition_version == "test-v1"
    assert signals["AAPL"].horizon_probabilities == {
        "1h": 0.70,
        "4h": 0.70,
        "1d": 0.70,
        "1w": 0.70,
    }
    assert run / "predictions.parquet" in sources


def test_ml_sizing_produces_buy_sell_and_auditable_order_style() -> None:
    portfolio = _portfolio(held={"AAPL": 10.0})
    signals = _signals(probability_by_symbol={"AAPL": 0.30})
    decisions = build_trade_decisions(
        signals,
        portfolio,
        ConstantModel(allocation_fraction=0.50, urgency=0.10),
        _activation(True),
        decided_at=NOW,
    )
    by_symbol = {decision.symbol: decision for decision in decisions}

    sell = by_symbol["AAPL"]
    assert sell.action == "SELL"
    assert sell.quantity == 5
    assert sell.order_type == "LIMIT"
    assert sell.limit_price == 100.1
    assert sell.order_style_reason_code == "LOW_URGENCY_STABLE_PREDICTION_PASSIVE_LIMIT"
    assert sell.order_payload is not None
    assert sell.order_payload["orderLegCollection"][0]["instruction"] == "SELL"
    assert sell.order_payload["orderLegCollection"][0]["positionEffect"] == "CLOSING"

    buy = by_symbol["AMZN"]
    assert buy.action == "BUY"
    assert buy.quantity > 0
    assert buy.order_payload is not None
    assert buy.order_payload["orderLegCollection"][0]["instruction"] == "BUY"
    assert buy.protective_price == 97.0


def test_extended_hours_force_limit_orders_and_explicit_schwab_session() -> None:
    decisions = build_trade_decisions(
        _signals(),
        _portfolio(),
        ConstantModel(allocation_fraction=0.20, urgency=0.99),
        _activation(True),
        policy=StockTraderPolicy(allow_market_orders=True),
        decided_at="2026-08-31T12:00:00+00:00",
        time_in_force="AM",
    )

    selected = [decision for decision in decisions if decision.quantity > 0]
    assert selected
    assert all(decision.order_type == "LIMIT" for decision in selected)
    assert all(decision.order_payload["session"] == "AM" for decision in selected)
    assert all(decision.order_payload["duration"] == "DAY" for decision in selected)
    assert all(
        decision.order_style_reason_code
        == "VERY_HIGH_URGENCY_EXTENDED_MARKETABLE_LIMIT"
        for decision in selected
    )


def test_extended_hours_wide_spread_fails_closed() -> None:
    portfolio = _portfolio()
    wide_quotes = {
        symbol: replace(quote, bid=99.0, ask=101.0)
        for symbol, quote in portfolio.quotes.items()
    }
    decisions = build_trade_decisions(
        _signals(),
        replace(portfolio, quotes=wide_quotes),
        ConstantModel(allocation_fraction=0.20),
        _activation(True),
        decided_at="2026-08-31T21:00:00+00:00",
        time_in_force="PM",
    )

    assert all(decision.action == "NO_TRADE" for decision in decisions)
    assert all(
        decision.decision_reason_code == "EXTENDED_SPREAD_TOO_WIDE"
        for decision in decisions
    )


def test_weak_expected_value_logs_no_order_and_keeps_hypothetical_size() -> None:
    decisions = build_trade_decisions(
        _signals(),
        _portfolio(),
        ConstantModel(expected_net_return=-0.001, allocation_fraction=0.50),
        _activation(True),
        decided_at=NOW,
    )
    decision = decisions[0]

    assert decision.action == "NO_TRADE"
    assert decision.hypothetical_quantity > 0
    assert decision.quantity == 0
    assert decision.decision_reason_code == "WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE"
    assert (
        decision.order_style_reason_code
        == "NO_ORDER_WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE"
    )
    assert decision.order_payload is None


def test_same_prediction_has_stable_retry_id_and_missing_model_keeps_features() -> None:
    first = build_trade_decisions(
        _signals(),
        _portfolio(),
        None,
        _activation(True),
        decided_at=NOW,
    )[0]
    retry = build_trade_decisions(
        _signals(),
        _portfolio(),
        None,
        _activation(True),
        decided_at="2026-08-31T16:07:00+00:00",
    )[0]

    assert first.decision_id == retry.decision_id
    assert first.decision_reason_code == "ENRICHMENT_MODEL_UNAVAILABLE"
    assert set(first.enrichment["feature_values"]) == set(ENRICHMENT_FEATURE_NAMES)


def test_sell_never_exceeds_owned_uncommitted_shares() -> None:
    portfolio = _portfolio(
        held={"AAPL": 10.0}, pending_sells={"AAPL": 7.0}
    )
    decisions = build_trade_decisions(
        _signals(probability_by_symbol={"AAPL": 0.20}),
        portfolio,
        ConstantModel(allocation_fraction=1.0),
        _activation(True),
        decided_at=NOW,
    )
    aapl = decisions[0]
    assert aapl.action == "SELL"
    assert aapl.quantity == 3


def test_runtime_false_toggle_never_contacts_broker(tmp_path: Path) -> None:
    write_activation_intent(tmp_path, active=False)
    result = runtime.run_stock_trader_once(
        tmp_path,
        decided_at=NOW,
        execute=True,
        session=NeverCalledSchwab(),
    )

    assert result.status == "TRADER_INACTIVE"
    assert result.submitted_orders == 0
    payload, receipt = read_decision_run(tmp_path, result.run_directory)
    assert payload["status"] == "TRADER_INACTIVE"
    assert payload["decisions"] == []
    assert receipt["orders_selected"] == 0


def test_runtime_logs_prediction_input_failure_without_contacting_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_activation_intent(tmp_path, active=True)
    monkeypatch.setattr(
        runtime,
        "load_current_prediction_signals",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad Loop B pointer")),
    )

    result = runtime.run_stock_trader_once(
        tmp_path,
        decided_at=NOW,
        execute=True,
        session=NeverCalledSchwab(),
    )

    assert result.status == "PREDICTION_INPUTS_UNAVAILABLE"
    assert result.error == "ValueError: bad Loop B pointer"
    payload, receipt = read_decision_run(tmp_path, result.run_directory)
    assert payload["status"] == "PREDICTION_INPUTS_UNAVAILABLE"
    assert receipt["orders_selected"] == 0


def test_runtime_prediction_deadline_expiry_never_contacts_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_activation_intent(tmp_path, active=True)
    waited = _handoff_result(
        {},
        status="PREDICTION_DEADLINE_EXPIRED",
        completed_at="2026-08-31T16:58:30+00:00",
    )
    monkeypatch.setattr(runtime, "wait_for_actionable_prediction", lambda *_a, **_k: waited)

    result = runtime.run_stock_trader_once(
        tmp_path,
        decided_at="2026-08-31T16:47:00+00:00",
        execute=True,
        session=NeverCalledSchwab(),
        wait_for_prediction=True,
    )

    assert result.status == "PREDICTION_DEADLINE_EXPIRED"
    assert result.prediction_handoff_status == "PREDICTION_DEADLINE_EXPIRED"
    payload, _receipt = read_decision_run(tmp_path, result.run_directory)
    assert payload["prediction_handoff"]["poll_count"] == waited.poll_count
    assert payload["decisions"] == []


def test_false_toggle_during_prediction_wait_stops_before_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_activation_intent(tmp_path, active=True)
    target = pd.Timestamp("2026-08-31T17:00:00+00:00")

    def wait_then_deactivate(*_args, **_kwargs):
        write_activation_intent(tmp_path, active=False)
        return _handoff_result(
            _signals_for_target(target, fingerprint="fresh"),
            status="FRESH_ACTIONABLE_RECEIPT",
            completed_at="2026-08-31T16:50:00+00:00",
        )

    monkeypatch.setattr(runtime, "wait_for_actionable_prediction", wait_then_deactivate)

    result = runtime.run_stock_trader_once(
        tmp_path,
        decided_at="2026-08-31T16:47:00+00:00",
        execute=True,
        session=NeverCalledSchwab(),
        wait_for_prediction=True,
    )

    assert result.status == "TRADER_INACTIVE_AFTER_PREDICTION_WAIT"
    assert result.activation_active is False


def test_runtime_requires_execute_and_true_toggle_for_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_activation_intent(tmp_path, active=True)
    monkeypatch.setattr(
        runtime,
        "load_current_prediction_signals",
        lambda *_args, **_kwargs: (_signals(), ()),
    )
    monkeypatch.setattr(
        runtime,
        "load_current_enrichment_model",
        lambda *_args, **_kwargs: ConstantModel(allocation_fraction=0.20),
    )
    monkeypatch.setattr(runtime, "_model_source_files", lambda *_args: ())
    schwab = FakeSchwab()

    dry = runtime.run_stock_trader_once(
        tmp_path,
        decided_at=NOW,
        execute=False,
        session=schwab,
        parallel_state=False,
    )
    assert dry.selected_orders > 0
    assert dry.submitted_orders == 0
    assert schwab.submitted == []

    live = runtime.run_stock_trader_once(
        tmp_path,
        decided_at="2026-08-31T17:00:00+00:00",
        execute=True,
        session=schwab,
        parallel_state=False,
    )
    assert live.status == "ORDERS_SUBMITTED"
    assert live.submitted_orders == live.selected_orders
    payload, _receipt = read_decision_run(tmp_path, live.run_directory)
    assert payload["live_decision_count"] == 6
    assert payload["shadow_decision_count"] == 6
    assert {row["decision_lane"] for row in payload["decisions"]} == {
        "LIVE",
        "SHADOW",
    }
    assert len({row["decision_id"] for row in payload["decisions"]}) == 12
    assert all(
        payload["orderLegCollection"][0]["instruction"] in {"BUY", "SELL"}
        for payload in schwab.submitted
    )
    assert all(
        payload["orderLegCollection"][0]["instrument"]["assetType"] == "EQUITY"
        for payload in schwab.submitted
    )


def test_execution_window_labels_open_queue_pre_core_post_and_gaps() -> None:
    premarket_queue = stock_execution_window(
        "2026-08-31T10:47:00+00:00",
        allow_premarket_queue=True,
    )
    queued = stock_execution_window(
        "2026-08-31T13:20:00+00:00", allow_open_queue=True
    )
    premarket = stock_execution_window("2026-08-31T12:00:00+00:00")
    morning_gap = stock_execution_window("2026-08-31T13:27:00+00:00")
    core = stock_execution_window("2026-08-31T16:00:00+00:00")
    afternoon_gap = stock_execution_window("2026-08-31T20:02:00+00:00")
    postmarket = stock_execution_window("2026-08-31T21:00:00+00:00")
    closed = stock_execution_window("2026-09-01T00:00:00+00:00")

    assert (
        premarket_queue.executable,
        premarket_queue.mode,
        premarket_queue.time_in_force,
    ) == (True, "PREMARKET_QUEUE", "AM")
    assert premarket_queue.queue_target_start == pd.Timestamp(
        "2026-08-31T11:00:00Z"
    )
    assert decision_targets_open(
        "2026-08-31T11:00:00Z", premarket_queue
    ) is True
    assert decision_targets_open(
        "2026-08-31T13:30:00Z", premarket_queue
    ) is False
    assert (queued.executable, queued.mode, queued.time_in_force) == (
        True,
        "OPEN_QUEUE",
        "DAY",
    )
    assert (premarket.executable, premarket.mode, premarket.time_in_force) == (
        True,
        "PREMARKET",
        "AM",
    )
    assert (morning_gap.executable, morning_gap.mode) == (False, "CLOSED")
    assert (core.executable, core.mode, core.time_in_force) == (
        True,
        "CORE",
        "DAY",
    )
    assert (afternoon_gap.executable, afternoon_gap.mode) == (False, "CLOSED")
    assert (postmarket.executable, postmarket.mode, postmarket.time_in_force) == (
        True,
        "AFTER_HOURS",
        "PM",
    )
    assert (closed.executable, closed.mode) == (False, "CLOSED")


def test_early_close_day_does_not_invent_extended_stock_sessions() -> None:
    before_core = stock_execution_window(
        "2026-11-27T13:00:00+00:00",
        allow_premarket_queue=True,
    )
    after_early_close = stock_execution_window("2026-11-27T19:00:00+00:00")

    assert (before_core.executable, before_core.mode) == (False, "CLOSED")
    assert (after_early_close.executable, after_early_close.mode) == (
        False,
        "CLOSED",
    )


def test_daily_adaptation_waits_for_the_full_actionable_stock_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_close = daily_adaptation.adapt_after_latest_completed_session(
        tmp_path,
        as_of="2026-08-31T23:30:00+00:00",
    )
    assert before_close["status"] == "NO_COMPLETED_XNYS_SESSION_TODAY"

    observed: dict[str, object] = {}

    def fake_audit(root: Path, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            run_directory=root / "ml" / "stock-trader-weekly-audits" / "test",
            pair_count=12,
            mature_pair_count=8,
        )

    monkeypatch.setattr(
        daily_adaptation,
        "build_stock_trader_weekly_audit",
        fake_audit,
    )
    monkeypatch.setattr(
        daily_adaptation,
        "train_and_publish_enrichment_model",
        lambda root, **_kwargs: root / "ml" / "stock-trader-model-runs" / "test",
    )

    after_close = daily_adaptation.adapt_after_latest_completed_session(
        tmp_path,
        as_of="2026-09-01T00:01:00+00:00",
    )

    assert after_close["status"] == "DAILY_ADAPTATION_PUBLISHED"
    assert after_close["session"] == "2026-08-31"
    assert after_close["equity_actionable_open"] == (
        "2026-08-31T11:00:00+00:00"
    )
    assert after_close["equity_actionable_close"] == (
        "2026-09-01T00:00:00+00:00"
    )
    assert observed["window_start"] == pd.Timestamp("2026-08-31T11:00:00Z")
    assert observed["window_end"] > pd.Timestamp("2026-09-01T00:00:00Z")


def test_next_stock_target_start_includes_hourly_and_four_hour_checkpoints() -> None:
    assert next_stock_target_start("2026-08-31T13:17:00+00:00") == pd.Timestamp(
        "2026-08-31T13:30:00+00:00"
    )
    assert next_stock_target_start("2026-08-31T14:47:00+00:00") == pd.Timestamp(
        "2026-08-31T15:00:00+00:00"
    )
    assert next_stock_target_start("2026-08-31T15:05:00+00:00") == pd.Timestamp(
        "2026-08-31T15:30:00+00:00"
    )
    assert next_stock_target_start("2026-08-31T23:17:00+00:00") == pd.Timestamp(
        "2026-08-31T23:30:00+00:00"
    )
    assert checkpoint_session_for_target("2026-08-31T11:30:00+00:00") == "PRE"
    assert checkpoint_session_for_target("2026-08-31T15:30:00+00:00") == (
        "REGULAR"
    )
    assert checkpoint_session_for_target("2026-08-31T23:30:00+00:00") == "POST"
    assert time_in_force_for_checkpoint("PRE") == "AM"
    assert time_in_force_for_checkpoint("REGULAR") == "DAY"
    assert time_in_force_for_checkpoint("POST") == "PM"
    assert time_in_force_for_checkpoint(
        "POST", current_checkpoint_session="REGULAR"
    ) == "EXT"


def test_prediction_handoff_waits_for_fresh_receipt_after_keeping_fallback(
    tmp_path: Path,
) -> None:
    clock = _FakeClock("2026-08-31T16:47:00+00:00")
    target = pd.Timestamp("2026-08-31T17:00:00+00:00")
    fallback_sources = _prediction_receipt_sources(
        tmp_path,
        "fallback",
        run_timestamp="2026-08-31T16:06:00+00:00",
        promoted_at="2026-08-31T16:20:00+00:00",
    )
    fresh_sources = _prediction_receipt_sources(
        tmp_path,
        "fresh",
        run_timestamp="2026-08-31T16:36:00+00:00",
        promoted_at="2026-08-31T16:50:00+00:00",
    )
    calls = 0

    def loader(_root: Path, *, as_of: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _signals_for_target(target, fingerprint="fallback"), fallback_sources
        return _signals_for_target(target, fingerprint="fresh"), fresh_sources

    result = handoff.wait_for_actionable_prediction(
        tmp_path,
        started_at=clock(),
        expected_target_window_start=target,
        poll_seconds=15.0,
        clock=clock,
        sleeper=clock.sleep,
        consumed_prediction_ids=set(),
        signal_loader=loader,
    )

    assert result.status == "FRESH_ACTIONABLE_RECEIPT"
    assert result.fallback_used is False
    assert result.fallback_candidate_observed is True
    assert result.source_run_path == "ml/runs/fresh"
    assert result.poll_count == 2
    assert set(result.signals) == set(STOCK_TRADER_SYMBOLS)


def test_prediction_handoff_uses_age_aware_fallback_at_deadline(
    tmp_path: Path,
) -> None:
    clock = _FakeClock("2026-08-31T16:47:00+00:00")
    target = pd.Timestamp("2026-08-31T17:00:00+00:00")
    sources = _prediction_receipt_sources(
        tmp_path,
        "fallback-only",
        run_timestamp="2026-08-31T16:06:00+00:00",
        promoted_at="2026-08-31T16:20:00+00:00",
    )

    result = handoff.wait_for_actionable_prediction(
        tmp_path,
        started_at=clock(),
        expected_target_window_start=target,
        poll_seconds=900.0,
        clock=clock,
        sleeper=clock.sleep,
        consumed_prediction_ids=set(),
        signal_loader=lambda _root, *, as_of: (
            _signals_for_target(target, fingerprint="fallback-only"),
            sources,
        ),
    )

    assert result.status == "FALLBACK_ACTIONABLE_RECEIPT"
    assert result.fallback_used is True
    assert result.completed_at == pd.Timestamp("2026-08-31T16:58:30+00:00")
    assert result.to_dict()["poll_policy"]["fallback_age_feature"] == (
        "prediction_age_minutes"
    )


def test_prediction_handoff_does_not_reconsume_live_prediction_generation(
    tmp_path: Path,
) -> None:
    clock = _FakeClock("2026-08-31T16:47:00+00:00")
    target = pd.Timestamp("2026-08-31T17:00:00+00:00")
    signals = _signals_for_target(target, fingerprint="already-used")
    sources = _prediction_receipt_sources(
        tmp_path,
        "already-used",
        run_timestamp="2026-08-31T16:36:00+00:00",
        promoted_at="2026-08-31T16:50:00+00:00",
    )

    result = handoff.wait_for_actionable_prediction(
        tmp_path,
        started_at=clock(),
        expected_target_window_start=target,
        poll_seconds=900.0,
        clock=clock,
        sleeper=clock.sleep,
        consumed_prediction_ids={signal.prediction_id for signal in signals.values()},
        signal_loader=lambda _root, *, as_of: (signals, sources),
    )

    assert result.status == "PREDICTION_GENERATION_ALREADY_CONSUMED"
    assert result.signals == {}
    assert set(result.consumed_prediction_ids) == {
        signal.prediction_id for signal in signals.values()
    }


def test_decision_publication_is_checksum_verified(tmp_path: Path) -> None:
    policy = StockTraderPolicy()
    decisions = build_trade_decisions(
        _signals(),
        _portfolio(),
        ConstantModel(),
        _activation(True),
        policy=policy,
        decided_at=NOW,
    )
    publication = publish_decision_run(
        tmp_path,
        decisions,
        decided_at=NOW,
        activation=_activation(True),
        policy=policy,
        execution_requested=False,
    )
    payload, receipt = read_decision_run(tmp_path, publication.run_directory)
    assert len(payload["decisions"]) == 6
    assert receipt["decision_ids"] == [decision.decision_id for decision in decisions]

    publication.decisions_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="size mismatch|checksum mismatch"):
        read_decision_run(tmp_path, publication.run_directory)


def test_async_reconciliation_attaches_fill_quantity_price_and_status(
    tmp_path: Path,
) -> None:
    policy = StockTraderPolicy()
    decision = build_trade_decisions(
        _signals(),
        _portfolio(),
        ConstantModel(allocation_fraction=0.10),
        _activation(True),
        policy=policy,
        decided_at=NOW,
    )[0]
    publication = publish_decision_run(
        tmp_path,
        (decision,),
        decided_at=NOW,
        activation=_activation(True),
        policy=policy,
        execution_requested=True,
    )
    event = reserve_execution_intent(
        tmp_path,
        decision,
        submitted_at=NOW,
        decision_publication=publication,
    )
    assert event is not None
    record_execution_result(
        event,
        status="SUBMITTED",
        completed_at=NOW,
        broker_location="https://api.schwabapi.com/trader/v1/accounts/hidden/orders/12345",
    )

    result = reconcile_submitted_orders(
        tmp_path,
        session=FilledOrderHistory(),
        observed_at="2026-08-31T16:10:00+00:00",
    )
    reconciled = read_execution_event(tmp_path, decision.decision_id)

    assert result.status == "RECONCILED"
    assert result.matched_order_count == 1
    assert reconciled is not None
    snapshot = reconciled["reconciliation"]
    assert snapshot["broker_status"] == "FILLED"
    assert snapshot["filled_quantity"] == 10.0
    assert snapshot["average_fill_price"] == pytest.approx(100.21)
    assert snapshot["fills"] == [
        {"quantity": 4.0, "price": 100.15, "executed_at": None},
        {"quantity": 6.0, "price": 100.25, "executed_at": None},
    ]


def test_live_fill_lifecycle_pairs_stock_trader_round_trips_fifo(
    tmp_path: Path,
) -> None:
    policy = StockTraderPolicy()
    buy = build_trade_decisions(
        _signals(),
        _portfolio(),
        ConstantModel(allocation_fraction=0.10),
        _activation(True),
        policy=policy,
        decided_at=NOW,
    )[0]
    buy = replace(
        buy,
        quantity=10,
        order_payload={
            **dict(buy.order_payload or {}),
            "orderLegCollection": [
                {
                    **dict((buy.order_payload or {})["orderLegCollection"][0]),
                    "quantity": 10,
                }
            ],
        },
    )
    buy_publication = publish_decision_run(
        tmp_path,
        (buy,),
        decided_at=NOW,
        activation=_activation(True),
        policy=policy,
        execution_requested=True,
    )
    buy_event = reserve_execution_intent(
        tmp_path,
        buy,
        submitted_at=NOW,
        decision_publication=buy_publication,
    )
    assert buy_event is not None
    record_execution_result(
        buy_event,
        status="SUBMITTED",
        completed_at=NOW,
        broker_location="https://example.test/orders/buy-1",
    )

    sell_time = "2026-08-31T17:00:00Z"
    sell_signals = _signals(probability_by_symbol={"AAPL": 0.30})
    sell = build_trade_decisions(
        sell_signals,
        _portfolio(held={"AAPL": 10.0}),
        ConstantModel(allocation_fraction=0.10),
        _activation(True),
        policy=policy,
        decided_at=sell_time,
    )[0]
    sell = replace(
        sell,
        quantity=10,
        order_payload={
            **dict(sell.order_payload or {}),
            "orderLegCollection": [
                {
                    **dict((sell.order_payload or {})["orderLegCollection"][0]),
                    "quantity": 10,
                }
            ],
        },
    )
    sell_publication = publish_decision_run(
        tmp_path,
        (sell,),
        decided_at=sell_time,
        activation=_activation(True),
        policy=policy,
        execution_requested=True,
    )
    sell_event = reserve_execution_intent(
        tmp_path,
        sell,
        submitted_at=sell_time,
        decision_publication=sell_publication,
    )
    assert sell_event is not None
    record_execution_result(
        sell_event,
        status="SUBMITTED",
        completed_at=sell_time,
        broker_location="https://example.test/orders/sell-1",
    )

    class _RoundTripHistory:
        def get_recent_orders(self) -> list[dict[str, object]]:
            return [
                {
                    "orderId": "buy-1",
                    "status": "FILLED",
                    "quantity": 10.0,
                    "filledQuantity": 10.0,
                    "remainingQuantity": 0.0,
                    "closeTime": "2026-08-31T16:01:00Z",
                    "orderActivityCollection": [
                        {
                            "executionTime": "2026-08-31T16:01:00Z",
                            "executionLegs": [{"quantity": 10.0, "price": 100.0}],
                        }
                    ],
                },
                {
                    "orderId": "sell-1",
                    "status": "FILLED",
                    "quantity": 10.0,
                    "filledQuantity": 10.0,
                    "remainingQuantity": 0.0,
                    "closeTime": "2026-08-31T17:01:00Z",
                    "orderActivityCollection": [
                        {
                            "executionTime": "2026-08-31T17:01:00Z",
                            "executionLegs": [{"quantity": 10.0, "price": 105.0}],
                        }
                    ],
                },
            ]

    reconcile_submitted_orders(
        tmp_path,
        session=_RoundTripHistory(),
        observed_at="2026-08-31T17:10:00Z",
    )
    decisions, _sources = audit.load_verified_decisions(
        tmp_path,
        window_start="1970-01-01T00:00:00Z",
        window_end="2026-08-31T18:00:00Z",
    )
    lifecycle, sources = build_stock_trader_execution_lifecycle(
        tmp_path,
        decisions,
        window_start="2026-08-31T15:00:00Z",
        window_end="2026-08-31T18:00:00Z",
    )

    assert lifecycle["status"] == "RECEIPT_MATCHED_ROUND_TRIPS"
    assert lifecycle["summary"]["matched_round_trip_quantity"] == 10.0
    assert (
        lifecycle["summary"]["gross_realized_pnl_before_unavailable_fees"]
        == 50.0
    )
    assert lifecycle["summary"]["unmatched_sell_quantity"] == 0
    assert lifecycle["summary"]["open_tracked_quantity_at_window_end"] == 0
    assert lifecycle["matched_fifo_segments"][0]["entry_prediction_id"]
    assert lifecycle["matched_fifo_segments"][0]["exit_prediction_id"]
    assert any(path.name == "snapshot.json" for path in sources)


def test_no_trade_reason_is_paired_with_actual_market_reality(tmp_path: Path) -> None:
    decision = build_trade_decisions(
        _signals(),
        _portfolio(),
        ConstantModel(expected_net_return=-0.001),
        _activation(True),
        decided_at=NOW,
    )[0]
    evaluation = {
        "evaluation_status": "EVALUATED",
        "evaluated_at": "2026-08-31T18:15:00+00:00",
        "target_window_end": "2026-08-31T18:00:00+00:00",
        "observed_forward_raw_return": 0.02,
        "observed_forward_cost_adjusted_return": 0.019,
        "assumed_round_trip_cost": 0.001,
    }

    pair = _pair_decision_with_reality(
        tmp_path,
        decision.to_dict(),
        evaluation=evaluation,
        evaluated_at=runtime.utc("2026-08-31T19:00:00+00:00"),
    )

    assert pair["decision_id"] == decision.decision_id
    assert pair["decision_action"] == "NO_TRADE"
    assert (
        pair["decision_reason_code"]
        == "WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE"
    )
    assert pair["market_reality"]["status"] == "EVALUATED"
    assert pair["market_reality"]["direction_aligned_net_return"] == pytest.approx(
        0.019
    )
    assert pair["market_reality"]["hypothetical_quantity_result_dollars"] > 0


def test_weekly_audit_publishes_row_by_row_decision_reality_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = StockTraderPolicy()
    decisions = build_trade_decisions(
        _signals(),
        _portfolio(),
        ConstantModel(expected_net_return=-0.001),
        _activation(True),
        policy=policy,
        decided_at=NOW,
    )
    publish_decision_run(
        tmp_path,
        decisions,
        decided_at=NOW,
        activation=_activation(True),
        policy=policy,
        execution_requested=False,
        prediction_handoff={
            "schema_version": "stock-trader-prediction-handoff-v2",
            "status": "FALLBACK_ACTIONABLE_RECEIPT",
            "wait_seconds": 690.0,
            "fallback_used": True,
        },
    )
    evaluations = {
        f"prediction-{symbol}": {
            "id": f"prediction-{symbol}",
            "evaluation_status": "EVALUATED",
            "evaluated_at": "2026-08-31T18:15:00+00:00",
            "target_window_end": "2026-08-31T18:00:00+00:00",
            "observed_forward_raw_return": 0.02,
            "observed_forward_cost_adjusted_return": 0.019,
            "assumed_round_trip_cost": 0.001,
        }
        for symbol in STOCK_TRADER_SYMBOLS
    }
    monkeypatch.setattr(
        audit,
        "_load_loop_b_evaluations",
        lambda _root, _ids: (evaluations, ()),
    )

    result = audit.build_stock_trader_weekly_audit(
        tmp_path,
        window_start="2026-08-31T15:00:00+00:00",
        window_end="2026-08-31T17:00:00+00:00",
        evaluated_at="2026-08-31T19:00:00+00:00",
    )
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    markdown = result.markdown_path.read_text(encoding="utf-8")

    assert result.status == "WEEKLY_AUDIT_COMPLETE"
    assert result.pair_count == 6
    assert result.mature_pair_count == 6
    assert len(report["decision_outcome_pairs"]) == 6
    assert report["summary"]["fallback_decision_count"] == 6
    assert report["summary"]["prediction_handoff_runs"]["fallback_run_count"] == 1
    assert (
        report["decision_outcome_pairs"][0]["prediction_handoff"]["status"]
        == "FALLBACK_ACTIONABLE_RECEIPT"
    )
    assert (
        report["decision_outcome_pairs"][0]["order_style_reason_code"]
        == "NO_ORDER_WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE"
    )
    assert "NO_ORDER_WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE" in markdown
    assert "FALLBACK_ACTIONABLE_RECEIPT (fallback)" in markdown


def test_multihead_training_uses_mature_decision_outcome_pairs() -> None:
    pairs: list[dict[str, object]] = []
    for index in range(48):
        features = {
            name: 0.01 * (index + feature_index + 1)
            for feature_index, name in enumerate(ENRICHMENT_FEATURE_NAMES)
        }
        aligned = 0.01 if index % 3 else -0.005
        pairs.append(
            {
                "decision_id": f"decision-{index}",
                "model": {"feature_values": features},
                "market_reality": {
                    "status": "EVALUATED",
                    "direction_aligned_net_return": aligned,
                    "direction_aligned_raw_return": aligned + 0.001,
                },
            }
        )

    payload, report = fit_enrichment_model_payload(
        pairs,
        trained_at=NOW,
        minimum_rows=40,
    )
    model = model_from_payload(payload)
    result = model.predict(pairs[-1]["model"]["feature_values"])

    assert report["row_count"] == 48
    assert 0.0 <= result.trade_probability <= 1.0
    assert 0.0 <= result.allocation_fraction <= 1.0
    assert 0.0 <= result.execution_urgency <= 1.0
    assert result.protective_distance_pct > 0.0


def _activation(active: bool) -> ActivationIntent:
    return ActivationIntent(
        active=active,
        status="ACTIVE" if active else "INACTIVE",
        reason="OPERATOR_INTENT_TRUE" if active else "OPERATOR_INTENT_FALSE",
        path="C:/DATASTORE/controls/stock-trader/operator-intent.txt",
        checksum_sha256="activation-checksum",
    )


def _signals(
    *, probability_by_symbol: dict[str, float] | None = None
) -> dict[str, PredictionSignal]:
    probabilities = probability_by_symbol or {}
    return {
        symbol: PredictionSignal(
            symbol=symbol,
            primary_horizon="1h",
            prediction_id=f"prediction-{symbol}",
            decision_timestamp=NOW,
            target_window_start="2026-08-31T17:00:00+00:00",
            target_window_end="2026-08-31T18:00:00+00:00",
            actionable_until="2026-08-31T17:00:00+00:00",
            prediction_created_at="2026-08-31T15:58:00+00:00",
            calibrated_probability=probabilities.get(symbol, 0.70),
            assumed_round_trip_cost=0.001,
            horizon_probabilities={"1h": 0.70, "4h": 0.65, "1d": 0.60, "1w": 0.55},
            model_name="loop-b-test",
            model_version="loop-b-v1",
            source_fingerprint="loop-b-source",
        )
        for symbol in STOCK_TRADER_SYMBOLS
    }


class _FakeClock:
    def __init__(self, value: object) -> None:
        self.value = pd.Timestamp(value)

    def __call__(self) -> pd.Timestamp:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += pd.Timedelta(seconds=seconds)


def _signals_for_target(
    target: pd.Timestamp, *, fingerprint: str
) -> dict[str, PredictionSignal]:
    target_timestamp = pd.Timestamp(target)
    return {
        symbol: replace(
            signal,
            prediction_id=f"{fingerprint}-{symbol}",
            target_window_start=target_timestamp.isoformat(),
            target_window_end=(target_timestamp + pd.Timedelta(hours=1)).isoformat(),
            actionable_until=target_timestamp.isoformat(),
            source_fingerprint=fingerprint,
        )
        for symbol, signal in _signals().items()
    }


def _prediction_receipt_sources(
    root: Path,
    name: str,
    *,
    run_timestamp: str,
    promoted_at: str,
) -> tuple[Path, ...]:
    run = root / "ml" / "runs" / name
    run.mkdir(parents=True)
    publication = run / "publication.json"
    publication.write_text(
        json.dumps(
            {
                "run_path": run.relative_to(root).as_posix(),
                "run_timestamp": run_timestamp,
                "promoted_at": promoted_at,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return (publication,)


def _handoff_result(
    signals: dict[str, PredictionSignal],
    *,
    status: str,
    completed_at: object,
) -> handoff.PredictionHandoffResult:
    target = pd.Timestamp("2026-08-31T17:00:00+00:00")
    completed = pd.Timestamp(completed_at)
    return handoff.PredictionHandoffResult(
        status=status,
        signals=signals,
        source_files=(),
        started_at=pd.Timestamp("2026-08-31T16:47:00+00:00"),
        completed_at=completed,
        expected_target_window_start=target,
        deadline=target - pd.Timedelta(seconds=90),
        fresh_generation_not_before=target - pd.Timedelta(minutes=25),
        poll_count=2,
        fallback_used=status == "FALLBACK_ACTIONABLE_RECEIPT",
        fallback_candidate_observed=False,
        source_run_path=("ml/runs/fresh" if signals else None),
        source_run_timestamp=(
            pd.Timestamp("2026-08-31T16:36:00+00:00") if signals else None
        ),
        source_promoted_at=(completed if signals else None),
        source_fingerprint=("fresh" if signals else None),
        selected_prediction_ids=tuple(
            signal.prediction_id for signal in signals.values()
        ),
        consumed_prediction_ids=(),
        missing_symbols=tuple(
            symbol for symbol in STOCK_TRADER_SYMBOLS if symbol not in signals
        ),
        publication_error_count=0,
        last_error=None,
    )


def _portfolio(
    *,
    held: dict[str, float] | None = None,
    pending_sells: dict[str, float] | None = None,
) -> PortfolioState:
    held_map = {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}
    held_map.update(held or {})
    pending_sell_map = {symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS}
    pending_sell_map.update(pending_sells or {})
    return PortfolioState(
        observed_at=NOW,
        account_equity=100_000.0,
        available_cash=50_000.0,
        gross_exposure=sum(quantity * 100.0 for quantity in held_map.values()),
        daily_pnl=0.0,
        held_shares=held_map,
        symbol_exposure={
            symbol: held_map[symbol] * 100.0 for symbol in STOCK_TRADER_SYMBOLS
        },
        pending_buy_shares={symbol: 0.0 for symbol in STOCK_TRADER_SYMBOLS},
        pending_sell_shares=pending_sell_map,
        working_order_count=sum(value > 0 for value in pending_sell_map.values()),
        quotes={
            symbol: QuoteState(
                symbol=symbol,
                bid=99.90,
                ask=100.10,
                last=100.0,
                mark=100.0,
                volume=1_000_000.0,
                observed_at=NOW,
            )
            for symbol in STOCK_TRADER_SYMBOLS
        },
        source_fingerprint="portfolio-source",
    )
