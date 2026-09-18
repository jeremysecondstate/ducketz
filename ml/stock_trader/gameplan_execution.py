"""Read the published trading instructions without rerunning research validation.

Model assessment and artifact checks belong to Gameplan production. Execution
needs the saved symbol, direction, allocation horizon and holding window.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ml.stock_trader.contracts import PredictionSignal, STOCK_TRADER_SYMBOLS, finite, utc
from ml.stock_trader.session import checkpoint_session_for_target


class GameplanDeploymentUnavailable(ValueError):
    """The selected source handoff does not authorize this loaded Gameplan."""


def _assert_execution_deployment(root, publication, *, action_date):
    from ml.gameplan_deployment import assert_execution_gameplan
    try:
        assert_execution_gameplan(root, publication, action_date=action_date)
    except (OSError, ValueError, RuntimeError) as exc:
        raise GameplanDeploymentUnavailable(str(exc)) from exc


def execution_frame(root: Path, *, action_date: str):
    root = Path(root).resolve()
    pointer = json.loads((root / "ml/nightly-gameplan-latest/run.json").read_text(encoding="utf-8"))
    run = (root / pointer["current"]["run_path"]).resolve()
    if run.parent != (root / "ml/nightly-gameplan-runs").resolve():
        raise ValueError("Gameplan run is outside its saved directory")
    _assert_execution_deployment(root, run, action_date=action_date)
    frame = pd.read_parquet(run / "forecasts.parquet")
    required = {"id", "symbol", "model_group", "direction", "calibrated_probability",
                "target_window_start", "target_window_end", "execution_eligible", "action_date"}
    if required.difference(frame):
        raise ValueError("Gameplan is missing trading instructions: " + ", ".join(sorted(required.difference(frame))))
    frame = frame.loc[frame.action_date.astype(str).eq(action_date)
                      & frame.symbol.isin(STOCK_TRADER_SYMBOLS) & frame.execution_eligible.eq(True)].copy()
    if frame.empty:
        raise ValueError("No saved stock trading instructions for " + action_date)
    for name in ("target_window_start", "target_window_end"):
        frame[name] = pd.to_datetime(frame[name], utc=True, errors="raise")
    if frame.id.isna().any() or frame.id.duplicated().any() or (frame.target_window_end <= frame.target_window_start).any():
        raise ValueError("Gameplan trading IDs or holding windows are invalid")
    return frame, run


def load_execution_signals(root: Path, *, as_of):
    now = utc(as_of)
    local = now.tz_convert("America/Los_Angeles")
    frame, run = execution_frame(root, action_date=local.date().isoformat())
    start = local.floor("h").tz_convert("UTC")
    if not 4 <= local.hour < 17:
        return {}, ()
    due = frame.loc[frame.target_window_start.eq(start)]
    signals = {}
    for row in due.to_dict("records"):
        key = (str(row["symbol"]), str(row["model_group"]))
        if key[1] not in {"1h", "4h", "1d", "1w"} or key in signals:
            raise ValueError("Gameplan has an unsupported or duplicated stock allocation")
        probability = finite(row["calibrated_probability"])
        if probability is None or not 0 <= probability <= 1:
            raise ValueError("Gameplan probability is invalid for " + str(row["id"]))
        from ml.stock_direction_policy import stock_direction
        if str(row["direction"]).upper() not in {stock_direction(probability), "NEUTRAL" if stock_direction(probability) == "NO_EDGE" else ""}:
            raise ValueError("Gameplan direction disagrees with its saved probability")
        end = utc(row["target_window_end"])
        signals[key] = PredictionSignal(
            symbol=key[0], primary_horizon=key[1], prediction_id=str(row["id"]),
            decision_timestamp=utc(row.get("decision_timestamp", start)).isoformat(),
            target_window_start=start.isoformat(), target_window_end=end.isoformat(),
            actionable_until=min(start + pd.Timedelta(hours=1), end).isoformat(),
            prediction_created_at=utc(row.get("frozen_at", start)).isoformat(),
            calibrated_probability=probability, assumed_round_trip_cost=0.,
            horizon_probabilities={key[1]: probability}, model_name=str(row.get("model_family", "Gameplan")),
            model_version=str(row.get("model_artifact", "")), source_fingerprint=run.name,
            checkpoint_session=checkpoint_session_for_target(start),
            target_definition_version=str(row.get("target_contract_version", "")),
            target_price_source_contract=str(row.get("target_price_source_contract", "")),
        )
    # Execution records retain the saved run ID. No training files, reports or
    # planning estimates need to be hashed before asking the broker for a quote.
    return signals, ()


def execution_preflight(root: Path, *, action_date):
    try:
        frame, run = execution_frame(root, action_date=action_date.isoformat())
        return {"status": "READY", "reason": "SAVED_GAMEPLAN_TRADING_INSTRUCTIONS",
                "execution_window_count": len(frame), "run_path": str(run)}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return {"status": "NOT_READY", "reason": "GAMEPLAN_TRADING_INSTRUCTIONS_UNAVAILABLE", "error": str(exc)}
