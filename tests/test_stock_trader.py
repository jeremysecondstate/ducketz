from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import audit, inputs
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
from ml.stock_trader.model import ENRICHMENT_FEATURE_NAMES, model_from_payload
from ml.stock_trader.publication import publish_decision_run, read_decision_run
from ml.stock_trader.publication import (
    read_execution_event,
    record_execution_result,
    reserve_execution_intent,
)
from ml.stock_trader.reconciliation import reconcile_submitted_orders
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


def test_prediction_loader_uses_only_current_actionable_live_loop_b_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "ml" / "runs" / "loop-b-test"
    run.mkdir(parents=True)
    rows: list[dict[str, object]] = []
    for symbol in STOCK_TRADER_SYMBOLS:
        for horizon in ("1h", "4h", "1d", "1w"):
            rows.append(
                {
                    "id": f"{symbol}-{horizon}",
                    "symbol": symbol,
                    "provider": "databento",
                    "horizon": horizon,
                    "decision_timestamp": "2026-08-31T15:55:00+00:00",
                    "information_available_at": "2026-08-31T15:57:00+00:00",
                    "target_window_start": "2026-08-31T17:00:00+00:00",
                    "target_window_end": "2026-08-31T18:00:00+00:00",
                    "actionable_until": "2026-08-31T17:00:00+00:00",
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
    assert signals["AAPL"].prediction_id == "AAPL-1h"
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
    assert all(
        payload["orderLegCollection"][0]["instruction"] in {"BUY", "SELL"}
        for payload in schwab.submitted
    )
    assert all(
        payload["orderLegCollection"][0]["instrument"]["assetType"] == "EQUITY"
        for payload in schwab.submitted
    )


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
    assert (
        report["decision_outcome_pairs"][0]["order_style_reason_code"]
        == "NO_ORDER_WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE"
    )
    assert "NO_ORDER_WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE" in markdown


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
