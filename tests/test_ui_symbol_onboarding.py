from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from app.ui.rolling_forecast_data import _with_pending_onboarding, adapt_gameplan_forecasts
from app.ui.rolling_forecasts import prediction_pulse_entries
from test_ui_rolling_forecasts import _nightly_gameplan_rows


def _registered_plan(root: Path) -> Path:
    directory = root / "onboarding-evidence"
    directory.mkdir()
    payload = {"schema_version": "symbol-onboarding-v1", "datastore_root": str(root),
               "symbol": "COST", "candidate_symbols": ["AAPL", "COST"], "previous_symbols": ["AAPL"]}
    plan_id = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (directory / "plan.json").write_text(json.dumps({**payload, "plan_id": plan_id}))
    (directory / "progress.json").write_text(json.dumps({"plan_id": plan_id, "status": "HISTORY_FETCHED"}))
    registry = root / "state/symbol-onboarding/COST.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"schema_version": "symbol-onboarding-v1", "symbol": "COST",
        "plan_id": plan_id, "plan_path": str(directory / "plan.json"),
        "progress_path": str(directory / "progress.json"), "activation_path": str(directory / "activation.json")}))
    return directory


def _view():
    return adapt_gameplan_forecasts(_nightly_gameplan_rows(), source_path=Path("run.json"),
        action_date="2026-09-04", loaded_at=datetime(2026, 9, 4, 12, 30, tzinfo=timezone.utc))


def test_registered_onboarding_is_visible_without_inventing_forecasts(tmp_path):
    _registered_plan(tmp_path)
    original = _view()
    view = _with_pending_onboarding(original, tmp_path)

    assert [item.symbol for item in view.pending_symbols] == ["COST"]
    assert "History imported" in view.pending_symbols[0].detail
    assert view.symbols == original.symbols
    assert view.source_row_count == original.source_row_count
    assert view.published_route_count == original.published_route_count
    assert view.frozen_weekly_snapshot_count == original.frozen_weekly_snapshot_count
    assert prediction_pulse_entries(view)[-1] == ("COST", (("1h", None), ("4h", None), ("1d", None)))
    assert not view.automated_action_allowed


def test_pending_symbol_is_replaced_by_its_published_gameplan(tmp_path):
    _registered_plan(tmp_path)
    frame = _nightly_gameplan_rows()
    cost = frame.copy()
    cost["symbol"] = "COST"
    published = adapt_gameplan_forecasts(cost, source_path=Path("run.json"), action_date="2026-09-04",
        loaded_at=datetime(2026, 9, 4, 12, 30, tzinfo=timezone.utc))

    view = _with_pending_onboarding(published, tmp_path)
    assert not view.pending_symbols
    assert len([entry for entry in prediction_pulse_entries(view) if entry[0] == "COST"]) == 1
    assert all(value is not None for _, value in prediction_pulse_entries(view)[0][1])


@pytest.mark.parametrize("damage", ["plan", "progress", "registry", "root"])
def test_unbound_status_cannot_add_a_symbol_or_hide_valid_forecasts(tmp_path, damage):
    directory = _registered_plan(tmp_path)
    if damage == "plan":
        plan = json.loads((directory / "plan.json").read_text())
        plan["symbol"] = "NVDA"
        (directory / "plan.json").write_text(json.dumps(plan))
    elif damage == "progress":
        (directory / "progress.json").write_text('{"plan_id":"different","status":"HISTORY_FETCHED"}')
    elif damage == "registry":
        (tmp_path / "state/symbol-onboarding/COST.json").write_text('null')
    else:
        registry = tmp_path / "state/symbol-onboarding/COST.json"
        data = json.loads(registry.read_text())
        data["progress_path"] = str(tmp_path / "unrelated.json")
        registry.write_text(json.dumps(data))
    original = _view()
    view = _with_pending_onboarding(original, tmp_path)
    assert view.symbols == original.symbols
    assert not view.pending_symbols
    assert len(view.warnings) == len(original.warnings) + 1


def test_unregistered_or_completed_onboarding_does_not_reappear(tmp_path):
    original = _view()
    assert not _with_pending_onboarding(original, tmp_path).pending_symbols
    directory = _registered_plan(tmp_path)
    progress = json.loads((directory / "progress.json").read_text())
    (directory / "activation.json").write_text(json.dumps({"plan_id": progress["plan_id"], "status": "ACTIVE"}))
    assert not _with_pending_onboarding(original, tmp_path).pending_symbols
