"""Fresh orders use original probabilities and current broker capacity."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import gameplan_execution
from test_gameplan_direction_engine import inputs, build


@pytest.mark.parametrize("probability", [.48, .5001])
@pytest.mark.parametrize("estimated_quantity", [0, 1, 999999])
def test_saved_estimated_quantity_cannot_reduce_or_increase_current_live_order(probability, estimated_quantity):
    signals, portfolio = inputs(probability=probability)
    key = "AAPL", "1h"
    portfolio = replace(portfolio, held_shares={"AAPL": 17}, pending_sell_shares={"AAPL": 2})
    options = dict(bearish_sell_capacities={key: 15}, verified_promoted_signals=frozenset())
    original, = build({key: signals[key]}, portfolio, **options)
    estimated = replace(signals[key], planned_quantity=estimated_quantity)
    current, = build({key: estimated}, portfolio, **options)
    assert original.quantity > 0
    assert (current.action, current.quantity, current.limit_price) == (
        original.action, original.quantity, original.limit_price)
    if probability == .48:
        assert current.action == "SELL" and current.quantity == 15


def test_native_historical_projected_delta_is_not_replayed_as_a_fresh_signal(tmp_path, monkeypatch):
    from ml.stock_trader import catchup
    run = tmp_path / "ml/nightly-gameplan-runs/original"
    run.mkdir(parents=True)
    receipt = run / "receipt.json"
    receipt.write_text("{}")
    derived = tmp_path / "ml/gameplan-trade-plan-runs/current/receipt.json"
    frame = pd.DataFrame([dict(id="original-neutral", symbol="AAPL", model_group="1h",
        target_window_start=pd.Timestamp("2026-10-19T11:00:00Z"),
        target_window_end=pd.Timestamp("2026-10-19T12:00:00Z"),
        calibrated_probability=.49995, direction="NO_EDGE",
        direction_policy_version="stock-direction-5001-4999-v3")])
    frame.attrs["direction_source_files"] = (receipt, derived)
    monkeypatch.setattr(gameplan_execution, "execution_frame", lambda *args, **kwargs: (frame, run))
    monkeypatch.setattr(catchup, "native_catchup_plan", lambda *args, **kwargs:
        pytest.fail("An estimated historical ledger cannot supply fresh order intent"))
    monkeypatch.setattr(catchup, "catchup_signals", lambda *args, **kwargs:
        pytest.fail("The original probability cannot be replaced with synthetic 0/1"))
    signals, sources = gameplan_execution.load_execution_signals(tmp_path, as_of="2026-10-19T11:10:00Z")
    signal = signals["AAPL", "1h"]
    assert signal.prediction_id == "original-neutral" and signal.calibrated_probability == .49995
    assert signal.planned_quantity is None and signal.catchup_components == ()
    assert sources == (receipt, derived)


@pytest.mark.parametrize("legacy_marker", ["contract", "components"])
def test_legacy_synthetic_projection_cannot_create_a_fresh_order_even_with_live_capacity(legacy_marker):
    signals, portfolio = inputs(probability=1.)
    key = "AAPL", "1h"
    legacy = replace(signals[key], planned_quantity=1,
        **({"target_definition_version": "accepted-gameplan-catchup-v1"} if legacy_marker == "contract"
           else {"catchup_components": ({"forecast_id": "old-estimate", "quantity": 1},)}))
    decision, = build({key: legacy}, portfolio)
    assert decision.quantity == 0 and decision.order_payload is None
    assert decision.decision_reason_code == "LEGACY_PROJECTED_INTENTION"


def test_current_native_recipe_uses_derived_calls_without_rewriting_scientific_forecasts(tmp_path, monkeypatch):
    from ml.gameplan_trade_planning import _publish_direction_plan
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    from test_joint_capital_plan import records, DAY, NOW

    source = tmp_path / "ml/nightly-gameplan-runs/scientific"
    source.mkdir(parents=True)
    rows = records("AAPL", .48)
    for row in rows:
        row.update(probability_target_contract=RAW_DIRECTION_TARGET, gameplan_variant="YG")
        if row["calibrated_probability"] == .48:
            row.update(direction="NO_EDGE", model_status="RESEARCH_NOT_PROMOTED")
    scientific = source / "forecasts.parquet"
    pd.DataFrame(rows).to_parquet(scientific)
    (source / "receipt.json").write_text(json.dumps({"action_date": DAY}))
    (source / "manifest.json").write_text("{}")
    original = scientific.read_bytes()
    publication = SimpleNamespace(run_directory=source,
        manifest={"configuration": {"symbols": ["AAPL"], "probability_target_contract": RAW_DIRECTION_TARGET}},
        receipt={"action_date": DAY})
    derived = _publish_direction_plan(tmp_path, publication, pd.DataFrame(rows),
        observed=pd.Timestamp(NOW), deadline_at=pd.Timestamp(f"{DAY}T23:59:59Z"), clock=lambda: NOW)
    latest = tmp_path / "ml/nightly-gameplan-latest/run.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"current": {"run_path": source.relative_to(tmp_path).as_posix()}}))
    monkeypatch.setattr(gameplan_execution, "STOCK_TRADER_SYMBOLS", ("AAPL",))
    monkeypatch.setattr(gameplan_execution, "_assert_execution_deployment", lambda *args, **kwargs: None)

    signals, sources = gameplan_execution.load_execution_signals(tmp_path, as_of=f"{DAY}T11:15:00Z")
    signal = signals["AAPL", "1h"]
    assert signal.calibrated_probability == .48 and signal.planned_quantity is None
    assert signal.prediction_id == next(row["id"] for row in rows if row["calibrated_probability"] == .48)
    assert (source / "receipt.json") in sources and (derived / "receipt.json") in sources
    assert scientific.read_bytes() == original
    assert not (derived / "planning-price-path.json").exists()
    (source / "receipt.json").write_text('{"changed":true}')
    with pytest.raises(ValueError, match="frozen forecast source"):
        gameplan_execution.load_execution_signals(tmp_path, as_of=f"{DAY}T11:15:00Z")
