import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from ml import directional_forecasts as directional
from ml import nightly_gameplan as nightly
from ml.directional_forecast_evaluation import evaluate_saved_directional_forecasts, POINTER
from app.ui.rolling_forecast_data import _with_pending_onboarding, adapt_gameplan_forecasts
from app.ui.rolling_forecasts import prediction_pulse_entries
from test_ui_rolling_forecasts import _nightly_gameplan_rows
from test_ui_symbol_onboarding import _registered_plan, _view


def _publish_fixture(root, monkeypatch, *, plan_id="test-plan"):
    now = pd.Timestamp("2026-09-04T08:00:00Z")
    monkeypatch.setattr(directional, "utc_timestamp", lambda *args: now)
    run = root / directional.RUN_ROOT / "20260904T080000.000000Z"
    run.mkdir(parents=True)
    frame = _nightly_gameplan_rows()
    frame = frame.loc[frame.symbol.eq("AAPL")].copy()
    frame["symbol"] = "COST"
    frame["id"] = f"directional:{run.name}:" + frame["id"]
    frame["execution_authority"] = directional.AUTHORITY
    frame["option_feature_count"] = 0
    frame.to_parquet(run / "forecasts.parquet", index=False)
    (run / "model-reports.json").write_text("{}")
    for group in nightly.MODEL_GROUPS:
        path = run / "models" / group / "model.joblib"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixture-model")
    config = {"schema_version": directional.VERSION, "symbol": "COST", "symbols": ["COST"],
              "action_date": "2026-09-04", "onboarding_plan_id": plan_id,
              "execution_authority": directional.AUTHORITY, "broker_orders_enabled": False,
              "orders_placed": 0, "feature_scope": "NON_OPTIONS"}
    return directional._publish(root, run, config=config, inputs=(), features=("mr__example",), created=now)


def test_forecast_only_publication_does_not_replace_gameplan_or_activate(tmp_path, monkeypatch):
    production = tmp_path / "ml/nightly-gameplan-latest/run.json"
    production.parent.mkdir(parents=True)
    production.write_text("original frozen Gameplan")
    publication = _publish_fixture(tmp_path, monkeypatch)
    actual = directional.read_current_directional_forecast(tmp_path, "COST")
    assert actual.run_directory == publication.run_directory
    assert production.read_text() == "original frozen Gameplan"
    assert not list(tmp_path.rglob("activation.json"))
    assert not (publication.run_directory / "option-strategy-intents.parquet").exists()
    with pytest.raises(RuntimeError):
        nightly.read_gameplan_run(tmp_path, publication.run_directory)


@pytest.mark.parametrize("damage", ["forecast", "model", "pointer", "receipt"])
def test_corrupt_directional_publications_fail_closed(tmp_path, monkeypatch, damage):
    publication = _publish_fixture(tmp_path, monkeypatch)
    paths = {"forecast": publication.run_directory / "forecasts.parquet",
             "model": publication.run_directory / "models/1h/model.joblib",
             "pointer": tmp_path / directional.POINTER_ROOT / "COST.json",
             "receipt": publication.run_directory / "receipt.json"}
    paths[damage].write_text("{}")
    with pytest.raises((RuntimeError, ValueError)):
        directional.read_current_directional_forecast(tmp_path, "COST")


def test_verified_onboarding_forecasts_fill_pulse_without_gameplan_authority(tmp_path, monkeypatch):
    directory = _registered_plan(tmp_path)
    plan_id = json.loads((directory / "plan.json").read_text())["plan_id"]
    publication = _publish_fixture(tmp_path, monkeypatch, plan_id=plan_id)
    original = _view()
    view = _with_pending_onboarding(original, tmp_path)
    pending = view.pending_symbols[0]
    assert pending.forecast.symbol == "COST"
    assert view.symbols == original.symbols
    assert view.published_route_count == original.published_route_count
    assert all(value is not None for _, value in prediction_pulse_entries(view)[-1][1])
    assert all(not route.is_actionable and not route.automated_action_allowed
               for route in pending.forecast.all_routes)
    assert all(route.option_plan_status == "OPTIONS_RESEARCH_NOT_PREPARED" for route in pending.forecast.all_routes)
    # The ordinary Gameplan adapter cannot silently treat this as a Gameplan.
    with pytest.raises(RuntimeError):
        adapt_gameplan_forecasts(pd.read_parquet(publication.run_directory / "forecasts.parquet"),
            source_path=Path("run.json"), action_date="2026-09-04")
    (publication.run_directory / "forecasts.parquet").write_bytes(b"damaged")
    fallback = _with_pending_onboarding(original, tmp_path)
    assert fallback.pending_symbols[0].forecast is None
    assert fallback.symbols == original.symbols


def test_a_forecast_for_another_plan_cannot_fill_pending_symbol(tmp_path, monkeypatch):
    _registered_plan(tmp_path)
    _publish_fixture(tmp_path, monkeypatch, plan_id="another-plan")
    view = _with_pending_onboarding(_view(), tmp_path)
    assert view.pending_symbols[0].forecast is None
    assert any("another onboarding plan" in warning for warning in view.warnings)


def test_supplemental_forecast_widgets_render_and_collapse(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    from app.ui.rolling_forecasts import RollingForecastTab

    directory = _registered_plan(tmp_path)
    plan_id = json.loads((directory / "plan.json").read_text())["plan_id"]
    _publish_fixture(tmp_path, monkeypatch, plan_id=plan_id)
    view = _with_pending_onboarding(_view(), tmp_path)
    pending = view.pending_symbols[0]
    routes = list(pending.forecast.routes)
    routes[1] = replace(routes[1], probability_warning="Raw scores are uncalibrated.",
                        raw_probability_up=0.512, probability_up=0.518)
    view = replace(view, pending_symbols=(replace(pending,
                   forecast=replace(pending.forecast, routes=tuple(routes))),))
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        monkeypatch.setattr(RollingForecastTab, "refresh", lambda self: None)
        parent = ttk.Frame(root)
        parent.pack(fill=tk.BOTH, expand=True)
        tab = RollingForecastTab(root=root, parent=parent)
        tab._render_view(view)
        root.update_idletasks()
        assert "COST" in {section.symbol for section in tab._symbol_sections}
        assert tab._prediction_pulse_forecast_only_symbols == {"COST"}
        assert "COST" not in tab._prediction_pulse_pending_symbols
        assert ("COST", "4h") in tab._prediction_pulse_raw_scores
        def texts(widget):
            own = [str(widget.cget("text"))] if "text" in widget.keys() else []
            return own + [text for child in widget.winfo_children() for text in texts(child)]
        labels = texts(parent)
        assert "Raw Up Score" in labels
        assert "51.2% *" in labels
        assert "Raw scores are uncalibrated." in labels
        tab._set_all_symbols_expanded(False)
        root.update_idletasks()
    finally:
        root.destroy()


def test_directional_evaluation_preserves_pending_then_scores_exact_mature_outcome(tmp_path, monkeypatch):
    publication = _publish_fixture(tmp_path, monkeypatch)
    frame = pd.read_parquet(publication.run_directory / "forecasts.parquet")
    first = frame.iloc[0]
    observed = pd.DataFrame([{**first.to_dict(), "target": 1, "observed_return": 0.02}])
    early = evaluate_saved_directional_forecasts(tmp_path, observed_groups={"1h": observed},
                                               evaluated_at="2026-09-03T00:00:00Z")
    assert pd.read_parquet(early / "evaluations.parquet")["evaluation_status"].eq("PENDING_MATURITY").all()
    mature = evaluate_saved_directional_forecasts(tmp_path, observed_groups={"1h": observed},
                                                evaluated_at=pd.Timestamp(first.target_window_end) + pd.Timedelta(seconds=1))
    result = pd.read_parquet(mature / "evaluations.parquet")
    assert result.evaluation_status.eq("EVALUATED").sum() == 1
    assert len(result) == 24
    assert (tmp_path / POINTER).is_file()
    assert not (tmp_path / "ml/gameplan-evaluation-latest/run.json").exists()
