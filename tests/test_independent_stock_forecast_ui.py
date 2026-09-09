from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.ui.rolling_forecast_data import ForecastDataError, adapt_gameplan_forecasts
from app.ui.rolling_forecasts import prediction_pulse_entries
from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, stock_target_windows
from test_ui_rolling_forecasts import _nightly_gameplan_rows


SYMBOLS = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST")


def _independent_rows():
    rows = []
    for symbol in SYMBOLS:
        for index, spec in enumerate(stock_target_windows(date(2026, 9, 8))):
            rows.append({
                **spec,
                "id": f"{symbol}:{spec['route']}",
                "symbol": symbol,
                "decision_timestamp": pd.Timestamp("2026-09-05T00:05:00Z"),
                "information_available_at": pd.Timestamp("2026-09-05T00:05:00Z"),
                "frozen_at": pd.Timestamp("2026-09-08T06:00:00Z"),
                "action_date": "2026-09-08",
                "execution_authority": "ADVISORY_PAPER_ONLY",
                "broker_orders_enabled": False,
                "calibrated_probability": 0.60 + index / 1000,
                "raw_probability": 0.60 + index / 1000,
                "model_family": "fixture",
                "model_status": "RESEARCH_NOT_PROMOTED" if spec["model_group"] == "1w" else "PROMOTED",
                "action_anchor_local": spec["target_window_start"].tz_convert("America/Los_Angeles").strftime("%H:%M") if spec["execution_eligible"] else None,
            })
    return pd.DataFrame(rows)


def _view(frame, observed):
    return adapt_gameplan_forecasts(
        frame, source_path=Path("fixture/run.json"), action_date="2026-09-08",
        loaded_at=pd.Timestamp(observed).to_pydatetime(),
        gameplan={"target_contract_version": STOCK_TARGET_CONTRACT_VERSION},
    )


@pytest.mark.parametrize(("observed", "hourly_route", "four_hour_route"), [
    ("2026-09-08T10:00:00Z", "1h@04:00", "4h@04:00"),
    ("2026-09-08T11:01:00Z", "1h@04:00", "4h@04:00"),
    ("2026-09-08T12:30:00Z", "1h@05:00", "4h@04:00"),
    ("2026-09-08T15:30:00Z", "1h@08:00", "4h@08:00"),
])
def test_seven_symbol_independent_display_uses_forward_entry_routes(observed, hourly_route, four_hour_route):
    view = _view(_independent_rows(), observed)
    assert view.published_route_count == 168
    assert len(prediction_pulse_entries(view)) == 7
    assert len(view.symbols) == 7
    assert view.pending_symbols == ()
    assert view.automated_action_allowed is False
    for symbol in view.symbols:
        hourly, four_hour, daily, weekly = symbol.routes
        assert hourly.id == f"{symbol.symbol}:{hourly_route}"
        assert four_hour.id == f"{symbol.symbol}:{four_hour_route}"
        assert daily.target_window_start == pd.Timestamp("2026-09-08T11:00:00Z")
        assert daily.target_window_end == pd.Timestamp("2026-09-09T00:00:00Z")
        assert weekly.target_window_start == pd.Timestamp("2026-09-08T11:00:00Z")
        assert weekly.target_window_end == pd.Timestamp("2026-09-15T00:00:00Z")
        assert weekly.model_evidence_status == "RESEARCH_NOT_PROMOTED"
        assert "Research" in weekly.actionability_label
        assert len(symbol.research_context) == 1
        gap = symbol.research_context[0]
        assert gap.id == f"{symbol.symbol}:1h@gap"
        assert gap.target_role == "OPENING_GAP_RESEARCH"
        assert gap.execution_eligible is False
        assert gap.actionability_status == "OPENING_GAP_RESEARCH"
        assert gap.actionable_until is None
        assert "No Entry" in gap.actionability_label
        assert gap.probability_up is not None


def test_sixteen_four_hour_card_displays_real_overnight_window_and_outlook_roles():
    view = _view(_independent_rows(), "2026-09-08T23:01:00Z")
    for symbol in view.symbols:
        four_hour = symbol.routes[1]
        assert four_hour.id.endswith("4h@16:00")
        assert four_hour.target_window_start == pd.Timestamp("2026-09-08T23:00:00Z")
        assert four_hour.target_window_end == pd.Timestamp("2026-09-09T14:00:00Z")
        assert "overnight exposure" in four_hour.window_detail
        assert "04:00–07:00" in four_hour.window_detail
        for outlook in symbol.weekly_outlook.sessions[1:]:
            assert outlook.target_role == "OUTLOOK"
            assert outlook.execution_eligible is False
            assert outlook.actionable_until is None
            assert "No Entry" in outlook.actionability_label


def test_independent_display_refuses_relabelled_legacy_window():
    frame = _independent_rows()
    index = frame.index[frame.symbol.eq("COST") & frame.route.eq("1d@D+1")][0]
    frame.loc[index, "target_window_start"] += pd.Timedelta(hours=2, minutes=30)
    with pytest.raises(ForecastDataError, match="windows differ"):
        _view(frame, "2026-09-08T11:01:00Z")


def test_seven_symbol_baseline_gameplan_still_uses_original_window_contract():
    original = _nightly_gameplan_rows()
    rows = []
    for symbol in SYMBOLS:
        frame = original.copy()
        frame["symbol"] = symbol
        frame["id"] = frame["id"].str.replace(":AAPL:", f":{symbol}:", regex=False)
        rows.append(frame)
    view = adapt_gameplan_forecasts(
        pd.concat(rows, ignore_index=True), source_path=Path("fixture/run.json"),
        action_date="2026-09-04", loaded_at=pd.Timestamp("2026-09-04T11:30:00Z").to_pydatetime(),
    )
    assert view.published_route_count == 168
    assert len(prediction_pulse_entries(view)) == 7
    assert view.pending_symbols == ()
    for symbol in view.symbols:
        assert symbol.routes[0].id.endswith("1h@05:00")
        assert symbol.routes[1].id.endswith("4h@08:00")
        assert symbol.routes[2].target_window_start == pd.Timestamp("2026-09-04T13:30:00Z")
        assert symbol.research_context == ()
        assert symbol.weekly_outlook.aggregate.actionability_label == "Research Forecast — Not Promoted"
