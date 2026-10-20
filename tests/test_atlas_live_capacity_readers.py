"""Offline reader coverage for current instructions without planning prices."""
from dataclasses import replace
from copy import deepcopy
import json
import pandas as pd
import pytest

from app.ui.gameplan_data import (Gameplan, GameplanError, _direction_actions, _forecast,
    _projection_metadata, load_gameplan, plan_sessions)
from ml.gameplan_execution_policy import current_gameplan_policy_metadata, direction_only_rows
from ml.stock_direction_policy import stock_direction
from ml.stock_trader import gameplan_execution


DAY = "2026-10-20"
SYMBOLS = ("AAPL", "AMZN", "SNDK", "MU", "NVDA", "GOOG", "COST", "CROX", "PATH", "IONQ", "TWST",
           "DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN")


def instructions():
    start = pd.Timestamp(DAY + " 04:00", tz="America/Los_Angeles")
    rows = []
    for index, symbol in enumerate(SYMBOLS):
        probability = (.5001, .4999, .48, .5)[index % 4]
        rows.append(dict(id=f"{DAY}:{symbol}:1h@04:00", symbol=symbol, action_date=DAY,
            model_group="1h", route="1h@04:00", target_role="EXECUTION", execution_eligible=True,
            model_status="RESEARCH_NOT_PROMOTED", calibrated_probability=probability,
            direction=stock_direction(probability), target_window_start=start,
            target_window_end=start + pd.Timedelta(hours=1), target_definition_version="original-target"))
    return direction_only_rows(pd.DataFrame(rows), action_date=DAY)


def test_all_twenty_two_directions_display_without_prices_or_saved_quantities(tmp_path):
    frame, ledger = instructions()
    forecasts = tuple(_forecast(row) for row in frame.to_dict("records"))
    actions = _direction_actions(forecasts)
    assert len(forecasts) == 22 and {row.symbol for row in forecasts} == set(SYMBOLS)
    for row in forecasts:
        expected = "BUY" if row.probability >= .5001 else "SELL" if row.probability <= .4999 else "HOLD"
        assert row.action == expected
        assert row.price is None and row.quantity == (0 if expected == "HOLD" else None)
        assert row.direction_policy_version == current_gameplan_policy_metadata(DAY)["direction_policy_version"]
    assert actions and all(row.quantity is None and row.price is None for row in actions)
    plan = Gameplan(DAY, forecasts[0].start, forecasts[0].start, tmp_path, tmp_path / "Gameplan.md",
                    forecasts, actions, projection_status="DIRECTION_ONLY")
    assert plan.projection_available and len(plan.symbols) == 22
    assert replace(plan, projection_status="UNAVAILABLE_PRICE_REFERENCES").projection_available is False
    report = {**current_gameplan_policy_metadata(DAY), "direction_projection_status": "DIRECTION_ONLY",
              "direction_based_projection": ledger}
    assert _projection_metadata(ledger, report, frame)[0] == "DIRECTION_ONLY"


def test_sell_intent_cannot_be_downgraded_to_hold_by_a_saved_quantity():
    frame, _ = instructions()
    bearish = next(row for row in frame.to_dict("records") if row["calibrated_probability"] == .48)
    with pytest.raises(GameplanError, match="disagrees with its probability"):
        _forecast({**bearish, "direction_based_action": "HOLD", "direction_based_trade_quantity": 0})
    with pytest.raises(GameplanError, match="quantity disagree"):
        _forecast({**bearish, "direction_based_trade_quantity": -1})


def test_current_reader_never_validates_retired_planning_prices_or_cash_estimates():
    frame, _ = instructions()
    row = frame.iloc[0].to_dict()
    current = _forecast({**row, "trade_price_mid": "retired-not-a-number",
                         "projected_cash_after_low": -1, "projected_cash_after_base": "retired"})
    assert current.action == "BUY" and current.price is None
    assert current.cash_after_low is None and current.cash_after_base is None


def test_current_display_history_menu_does_not_require_retired_account_projection(tmp_path):
    from gameplan_fixture import write_plan
    from ml.artifacts import file_checksum
    from ml.account_gameplan.planner import VERSION as ACCOUNT_VERSION
    frame, ledger = instructions()
    metadata = current_gameplan_policy_metadata(DAY)
    run = write_plan(tmp_path, session=DAY, rows=frame.to_dict("records"), ledger=ledger,
        report_updates={**metadata, "direction_projection_status": "DIRECTION_ONLY", "direction_based_projection": ledger})
    receipt_path = run / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(metadata)
    receipt_path.write_text(json.dumps(receipt))
    latest = tmp_path / "ml/gameplan-trade-plan-latest/run.json"
    pointer = json.loads(latest.read_text())
    pointer["current"]["receipt_sha256"] = file_checksum(receipt_path)
    latest.write_text(json.dumps(pointer))
    retired = tmp_path / "ml/account-gameplan-latest/run.json"
    retired.parent.mkdir(parents=True)
    retired.write_text('{"retired_price_projection":')
    old_session = "2026-10-01"
    history = tmp_path / "ml/account-gameplan-runs/original-publication"
    history.mkdir(parents=True)
    (history / "receipt.json").write_text(json.dumps({"schema_version": ACCOUNT_VERSION,
        "status": "COMPLETE", "action_date": old_session, "completed_at": old_session + "T10:00:00Z",
        "manifest_sha256": "a" * 64}))
    assert plan_sessions(tmp_path) == (DAY, old_session)
    assert load_gameplan(tmp_path).projection_status == "DIRECTION_ONLY"
    with pytest.raises(GameplanError, match="account Gameplan pointer"):
        load_gameplan(tmp_path, old_session)


def test_live_reader_keeps_original_probabilities_and_never_uses_projected_caps(tmp_path, monkeypatch):
    frame, _ = instructions()
    for column in ("target_window_start", "target_window_end"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    # An obsolete estimate must have no effect on fresh executor quantities.
    frame["direction_based_trade_quantity"] = 1
    frame["trade_price_mid"] = .01
    run = tmp_path / "saved-original-forecasts"
    monkeypatch.setattr(gameplan_execution, "execution_frame", lambda *args, **kwargs: (frame, run))
    signals, sources = gameplan_execution.load_execution_signals(tmp_path, as_of=DAY + "T11:15:00Z")
    assert len(signals) == 22 and sources == ()
    for row in frame.to_dict("records"):
        signal = signals[row["symbol"], "1h"]
        assert signal.calibrated_probability == row["calibrated_probability"]
        assert signal.planned_quantity is None and signal.catchup_components == ()
        assert signal.prediction_id == row["id"]
        assert signal.target_window_end == row["target_window_end"].isoformat()


@pytest.mark.parametrize("version", ["stock-direction-50-01-v3", "stock-direction-5001-4999-v3"])
def test_both_current_saved_policy_identities_validate_without_model_promotion(version):
    frame, _ = instructions()
    frame["direction_policy_version"] = version
    validated = gameplan_execution._validated_instructions(frame)
    assert len(validated) == 22 and all(not abstention for _, _, abstention in validated)
    assert {row["model_status"] for row, _, _ in validated} == {"RESEARCH_NOT_PROMOTED"}


@pytest.mark.parametrize("probability,action", [(.7, "BUY"), (.48, "SELL")])
def test_current_no_history_flag_cannot_suppress_a_valid_probability(tmp_path, monkeypatch, probability, action):
    frame, _ = instructions()
    saved = frame.iloc[[0]].copy()
    saved["calibrated_probability"] = probability
    saved["direction"] = "NO_EDGE"
    saved["model_status"] = "RESEARCH_NO_TARGET_HISTORY"
    saved["symbol_fitted_target_rows"] = 0
    # Preserve the scientific source's no-history label, while current order
    # intent follows its original probability with no model-assessment veto.
    saved = saved.drop(columns=["source_saved_direction", "source_saved_direction_policy_version"])
    current, _ = direction_only_rows(saved, action_date=DAY)
    row = current.iloc[0].to_dict()
    assert row["source_saved_direction"] == "NO_EDGE"
    assert _forecast(row).action == action
    validated = gameplan_execution._validated_instructions(current)
    assert validated[0][1:] == (probability, False)
    for column in ("target_window_start", "target_window_end"):
        current[column] = pd.to_datetime(current[column], utc=True)
    monkeypatch.setattr(gameplan_execution, "execution_frame", lambda *args, **kwargs: (current, tmp_path))
    signals, _ = gameplan_execution.load_execution_signals(tmp_path, as_of=DAY + "T11:15:00Z")
    assert signals["AAPL", "1h"].calibrated_probability == probability
    assert signals["AAPL", "1h"].planned_quantity is None
    corrupted = {**row, "direction": "NO_EDGE", "direction_based_action": "HOLD", "direction_based_trade_quantity": 0}
    with pytest.raises(ValueError, match="direction disagrees"):
        gameplan_execution._validated_instructions(pd.DataFrame([corrupted]))
    with pytest.raises(GameplanError, match="disagrees with its probability"):
        _forecast(corrupted)


@pytest.mark.parametrize("probability,action", [(.7, "BUY"), (.48, "SELL"), (.5001, "BUY"), (.4999, "SELL")])
def test_fresh_old_scientific_no_history_label_uses_original_probability_without_a_gate(tmp_path, probability, action):
    frame, _ = instructions()
    science = frame.iloc[[0]].copy()
    science = science.drop(columns=[name for name in science if name.startswith(("direction_based_", "source_saved_"))
        or name in {"gameplan_recipe_version", "execution_quantity_basis", "direction_policy_version",
                    "direction_up_threshold", "direction_down_threshold"}])
    science["calibrated_probability"] = probability
    science["direction"] = "NO_EDGE"
    science["model_status"] = "RESEARCH_NO_TARGET_HISTORY"
    # No fitted-history field is needed to act on a real saved probability.
    original = science.copy(deep=True)
    for column in ("target_window_start", "target_window_end"):
        science[column] = pd.to_datetime(science[column], utc=True)
        original[column] = pd.to_datetime(original[column], utc=True)
    from ml.artifacts import file_checksum
    native = tmp_path / "ml/nightly-gameplan-runs/original-scientific-publication"
    native.mkdir(parents=True)
    path = native / "forecasts.parquet"
    science.to_parquet(path, index=False)
    before = file_checksum(path)
    latest = tmp_path / "ml/nightly-gameplan-latest/run.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"current": {"run_path": native.relative_to(tmp_path).as_posix()}}))
    assert gameplan_execution.execution_preflight(tmp_path, action_date=pd.Timestamp(DAY).date())["status"] == "READY"
    signals, _ = gameplan_execution.load_execution_signals(tmp_path, as_of=DAY + "T11:15:00Z")
    signal = signals["AAPL", "1h"]
    from ml.stock_direction_policy import stock_action
    assert stock_action(signal.calibrated_probability) == action
    assert signal.calibrated_probability == probability and signal.planned_quantity is None
    assert signal.prediction_id == science.iloc[0].id
    pd.testing.assert_frame_equal(science, original)
    assert file_checksum(path) == before


def test_concurrent_public_direction_reader_keeps_latest_hold_and_original_longer_target():
    frame, _ = instructions()
    base = frame.iloc[0].to_dict()
    source = []
    for horizon, start, end, probability in (("1h", "11:00", "15:00", .7),
            ("1h", "12:00", "13:00", .5), ("1h", "13:00", "14:00", .4),
            ("4h", "11:00", "15:00", .4999)):
        source.append({**base, "id": horizon + "-" + start, "model_group": horizon,
            "route": horizon + "-" + start, "calibrated_probability": probability,
            "target_window_start": DAY + "T" + start + ":00Z",
            "target_window_end": DAY + "T" + end + ":00Z", "frozen_at": DAY + "T06:00:00Z"})
    current, ledger = direction_only_rows(pd.DataFrame(source), action_date=DAY)
    plan = {**current_gameplan_policy_metadata(DAY), "action_date": DAY, "as_of": DAY + "T06:00:00Z",
            "forecasts": current.astype(object).where(current.notna(), None).to_dict("records"), "ledger": ledger}
    original = deepcopy(plan)
    signals = gameplan_execution.accepted_direction_only_signals(plan,
        as_of=DAY + "T12:15:00Z", source_fingerprint="original-current-plan")
    assert signals["AAPL", "1h"].calibrated_probability == .5
    longer = signals["AAPL", "4h"]
    assert longer.calibrated_probability == .4999 and longer.planned_quantity is None
    assert longer.target_window_start == DAY + "T11:00:00+00:00"
    assert longer.target_window_end == longer.actionable_until == DAY + "T15:00:00+00:00"
    assert ("AAPL", "1h") not in gameplan_execution.accepted_direction_only_signals(plan,
        as_of=DAY + "T14:30:00Z", source_fingerprint="original-current-plan")
    assert plan == original
