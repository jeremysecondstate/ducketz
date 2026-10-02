"""Published no-history abstentions stay non-actionable without weakening checks."""
from __future__ import annotations

from datetime import date
import hashlib
import json

import pandas as pd
import pytest

from ml.stock_trader.contracts import ActivationIntent, STOCK_TRADER_SYMBOLS
from ml.stock_trader.gameplan_execution import execution_preflight, load_execution_signals


DAY = date(2026, 9, 8)
START = pd.Timestamp("2026-09-08T11:00:00Z")
NOW = START + pd.Timedelta(minutes=1)
INDEPENDENT = "independent-stock-targets-v1"
SYMBOL = STOCK_TRADER_SYMBOLS[0]


def instruction(**changes):
    row = dict(
        id=f"{DAY}:{SYMBOL}:1h@04:00", symbol=SYMBOL, model_group="1h",
        direction="NO_EDGE", calibrated_probability=.8,
        model_status="RESEARCH_NO_TARGET_HISTORY", symbol_fitted_target_rows=10,
        symbol_route_fitted_target_rows=0, target_contract_version=INDEPENDENT,
        target_window_start=START, target_window_end=START + pd.Timedelta(hours=1),
        execution_eligible=True, action_date=DAY.isoformat(),
        frozen_at="2026-09-08T09:00:00Z",
    )
    return {**row, **changes}


def save(root, rows):
    run = root / "ml/nightly-gameplan-runs/synthetic-publication"
    run.mkdir(parents=True)
    path = run / "forecasts.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    pointer = root / "ml/nightly-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"current": {"run_path": run.relative_to(root).as_posix()}}))
    return path


def assert_rejected(root):
    assert execution_preflight(root, action_date=DAY)["status"] == "NOT_READY"
    with pytest.raises(ValueError):
        load_execution_signals(root, as_of=NOW)


@pytest.mark.parametrize("probability", [.2, .8])
@pytest.mark.parametrize("symbol_count,route_count", [(0, 0), (10, 0)])
def test_publisher_finalized_abstentions_preserve_probability_and_other_signals(
    tmp_path, probability, symbol_count, route_count,
):
    from ml.independent_stock_targets import stock_target_windows
    from ml.nightly_gameplan import _finalize_forecasts

    symbols = tuple(STOCK_TRADER_SYMBOLS[:2])
    windows = stock_target_windows(DAY)
    assert len(windows) == 24
    rows = []
    for symbol in symbols:
        abstain = symbol == symbols[0]
        rows.extend({**window, "symbol": symbol,
                     "calibrated_probability": probability if abstain else .65,
                     "model_status": "RESEARCH_NO_TARGET_HISTORY" if abstain else "PROMOTED",
                     "symbol_fitted_target_rows": symbol_count if abstain else 10,
                     "symbol_route_fitted_target_rows": route_count if abstain else 5}
                    for window in windows)
    frozen = _finalize_forecasts(
        pd.DataFrame(rows), symbols=symbols, action_date=DAY,
        frozen_at=pd.Timestamp("2026-09-08T09:00:00Z"), action_start=START,
        action_end=START + pd.Timedelta(hours=13), opra_freshness=None,
        target_contract_version=INDEPENDENT,
    )
    abstentions = frozen.loc[frozen.symbol.eq(symbols[0])]
    assert len(abstentions) == 24
    assert abstentions.direction.eq("NO_EDGE").all()
    assert abstentions.calibrated_probability.eq(probability).all()
    path = save(tmp_path, frozen.to_dict("records"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert execution_preflight(tmp_path, action_date=DAY)["status"] == "READY"
    signals, sources = load_execution_signals(tmp_path, as_of=NOW)
    assert set(signals) == {(symbols[1], horizon) for horizon in ("1h", "4h", "1d", "1w")}
    assert {signal.calibrated_probability for signal in signals.values()} == {.65}
    assert sources == ()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert pd.read_parquet(path).loc[lambda f: f.symbol.eq(symbols[0]), "calibrated_probability"].eq(probability).all()


def test_all_abstentions_finish_without_broker_capture_or_ledger_creation(tmp_path, monkeypatch):
    from ml.stock_trader import independent_runtime as runtime
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY

    path = save(tmp_path, [instruction(symbol=symbol, id=f"{symbol}-abstention")
                           for symbol in STOCK_TRADER_SYMBOLS])
    before = path.read_bytes()
    monkeypatch.setattr(runtime, "read_gameplan_stock_activation_intent", lambda root: ActivationIntent(
        True, "ACTIVE", "synthetic", "synthetic", "synthetic-control"))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("An all-abstention batch must not construct a broker or capture account state")

    monkeypatch.setattr(runtime, "SchwabSession", forbidden)
    monkeypatch.setattr(runtime, "capture_portfolio_state", forbidden)
    monkeypatch.setattr(runtime, "load_current_enrichment_model", forbidden)
    result = runtime.run_independent_stock_trader_once(
        tmp_path, execute=True, session_managed=True,
        sizing_policy=GAMEPLAN_SIZING_POLICY, runtime_clock=lambda: NOW,
    )
    assert result.status == "NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL"
    assert result.error is None
    assert result.submitted_orders == 0
    assert result.broker_state_capture is None
    assert calls == []
    assert not (tmp_path / runtime.LEDGER_RELATIVE_PATH).exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["symbol_fitted_target_rows", "symbol_route_fitted_target_rows"])
@pytest.mark.parametrize("value", [None, True, False, "0", -1, .5, float("nan"), float("inf")])
def test_abstention_support_requires_valid_integral_numeric_counts(tmp_path, field, value):
    row = instruction(symbol_fitted_target_rows=0, symbol_route_fitted_target_rows=0)
    row[field] = value
    save(tmp_path, [row])
    assert_rejected(tmp_path)


@pytest.mark.parametrize("field", ["symbol_fitted_target_rows", "symbol_route_fitted_target_rows"])
def test_independent_abstention_requires_both_support_fields(tmp_path, field):
    row = instruction()
    del row[field]
    save(tmp_path, [row])
    assert_rejected(tmp_path)


def test_legacy_abstention_requires_symbol_support_but_not_independent_route_metadata(tmp_path):
    row = instruction(target_contract_version="legacy-target", symbol_fitted_target_rows=0.0)
    del row["symbol_route_fitted_target_rows"]
    save(tmp_path, [row])
    assert execution_preflight(tmp_path, action_date=DAY)["status"] == "READY"
    assert load_execution_signals(tmp_path, as_of=NOW) == ({}, ())


@pytest.mark.parametrize("changes", [
    {"symbol_fitted_target_rows": 10, "symbol_route_fitted_target_rows": 5},
    {"model_status": "PROMOTED"},
    {"model_status": "RESEARCH"},
    {"direction": "NEUTRAL"},
    {"direction": "BULLISH", "calibrated_probability": .2},
    {"direction": "BEARISH", "calibrated_probability": .8},
])
def test_other_direction_probability_contradictions_are_not_abstentions(tmp_path, changes):
    save(tmp_path, [instruction(**changes)])
    assert_rejected(tmp_path)


@pytest.mark.parametrize("probability", [None, float("nan"), float("inf"), -.1, 1.1])
def test_abstention_never_excuses_invalid_probability(tmp_path, probability):
    save(tmp_path, [instruction(calibrated_probability=probability)])
    assert_rejected(tmp_path)


def test_preflight_rejects_later_contradiction_before_its_slot_is_due(tmp_path):
    save(tmp_path, [
        instruction(id="current", direction="BULLISH", model_status="RESEARCH", calibrated_probability=.8),
        instruction(id="later", direction="BEARISH", calibrated_probability=.8,
                    target_window_start=START + pd.Timedelta(hours=1),
                    target_window_end=START + pd.Timedelta(hours=2)),
    ])
    assert execution_preflight(tmp_path, action_date=DAY)["status"] == "NOT_READY"
    assert set(load_execution_signals(tmp_path, as_of=NOW)[0]) == {(SYMBOL, "1h")}
    with pytest.raises(ValueError):
        load_execution_signals(tmp_path, as_of=NOW + pd.Timedelta(hours=1))


@pytest.mark.parametrize("second_abstains", [False, True])
def test_skipped_abstention_does_not_hide_duplicate_allocation(tmp_path, second_abstains):
    second = instruction(id="duplicate")
    if not second_abstains:
        second.update(direction="BULLISH", model_status="RESEARCH", calibrated_probability=.8)
    save(tmp_path, [instruction(), second])
    assert_rejected(tmp_path)


def test_same_allocation_in_different_hourly_windows_is_not_a_duplicate(tmp_path):
    save(tmp_path, [instruction(), instruction(id="next-hour", target_window_start=START + pd.Timedelta(hours=1),
                                             target_window_end=START + pd.Timedelta(hours=2))])
    assert execution_preflight(tmp_path, action_date=DAY)["status"] == "READY"
    assert load_execution_signals(tmp_path, as_of=NOW) == ({}, ())


@pytest.mark.parametrize("probability,direction", [(.5, "NO_EDGE"), (.5, "NEUTRAL"), (.2, "BEARISH"), (.8, "BULLISH")])
@pytest.mark.parametrize("model_status", ["PROMOTED", "RESEARCH"])
def test_matching_instructions_preserve_neutral_alias_and_have_no_promotion_veto(
    tmp_path, probability, direction, model_status,
):
    row = instruction(direction=direction, calibrated_probability=probability, model_status=model_status)
    del row["symbol_fitted_target_rows"]
    del row["symbol_route_fitted_target_rows"]
    save(tmp_path, [row])
    assert execution_preflight(tmp_path, action_date=DAY)["status"] == "READY"
    signals, _ = load_execution_signals(tmp_path, as_of=NOW)
    assert signals[SYMBOL, "1h"].calibrated_probability == probability
