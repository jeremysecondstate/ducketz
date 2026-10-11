"""Read the published trading instructions without rerunning research validation.

Model assessment and artifact checks belong to Gameplan production. Execution
needs the saved symbol, direction, allocation horizon and holding window.
"""
from __future__ import annotations

import json
from numbers import Real
from pathlib import Path

import pandas as pd

from ml.stock_trader.contracts import PredictionSignal, STOCK_TRADER_SYMBOLS, finite, utc
from ml.stock_trader.session import checkpoint_session_for_target


class GameplanDeploymentUnavailable(ValueError):
    """The selected source handoff does not authorize this loaded Gameplan."""


def _assert_execution_deployment(root, publication, *, action_date):
    from ml.gameplan_deployment import assert_execution_gameplan
    try:
        from ml.joint_capital_adoption import assert_accepted_execution
        if assert_accepted_execution(root, getattr(publication, "run_directory", publication), action_date=action_date):
            return
        assert_execution_gameplan(root, publication, action_date=action_date)
    except (OSError, ValueError, RuntimeError) as exc:
        raise GameplanDeploymentUnavailable(str(exc)) from exc


def execution_frame(root: Path, *, action_date: str):
    root = Path(root).resolve()
    from ml.joint_capital_adoption import read_accepted_joint_plan
    combined = read_accepted_joint_plan(root, action_date)
    if combined:
        plan, _, run = combined
        _assert_execution_deployment(root, run, action_date=action_date)
        frame = pd.DataFrame(plan["forecasts"])
        for name in ("target_window_start", "target_window_end"):
            frame[name] = pd.to_datetime(frame[name], utc=True, errors="raise")
        return frame.loc[frame.execution_eligible.eq(True)].copy(), run
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


def _saved_abstention(row):
    """Honor the publisher's explicit no-history direction without changing p."""
    if (str(row["direction"]).upper() != "NO_EDGE"
            or row.get("model_status") != "RESEARCH_NO_TARGET_HISTORY"):
        return False
    fields = ["symbol_fitted_target_rows"]
    if (row.get("target_contract_version") == "independent-stock-targets-v1"
            or "symbol_route_fitted_target_rows" in row):
        fields.append("symbol_route_fitted_target_rows")
    counts = []
    for name in fields:
        value = row.get(name)
        count = finite(value)
        if (isinstance(value, bool) or not isinstance(value, Real) or count is None
                or count < 0 or not count.is_integer()):
            raise ValueError("Gameplan abstention has invalid fitted history for " + str(row["id"]))
        counts.append(count)
    if 0 not in counts:
        raise ValueError("Gameplan abstention lacks zero fitted history for " + str(row["id"]))
    return True


def _validated_instructions(frame):
    """Validate every selected slot, including rows that emit no signal."""
    from ml.stock_direction_policy import stock_direction

    seen = set()
    instructions = []
    for row in frame.to_dict("records"):
        key = (str(row["symbol"]), str(row["model_group"]), row["target_window_start"])
        if key[1] not in {"1h", "4h", "1d", "1w"} or key in seen:
            raise ValueError("Gameplan has an unsupported or duplicated stock allocation")
        seen.add(key)
        probability = finite(row["calibrated_probability"])
        if probability is None or not 0 <= probability <= 1:
            raise ValueError("Gameplan probability is invalid for " + str(row["id"]))
        abstention = _saved_abstention(row)
        expected = stock_direction(probability)
        direction = str(row["direction"]).upper()
        if not abstention and direction != expected and not (direction == "NEUTRAL" and expected == "NO_EDGE"):
            raise ValueError("Gameplan direction disagrees with its saved probability for " + str(row["id"]))
        instructions.append((row, probability, abstention))
    return instructions


def load_execution_signals(root: Path, *, as_of):
    now = utc(as_of)
    local = now.tz_convert("America/Los_Angeles")
    from ml.joint_capital_adoption import read_accepted_joint_plan
    selected = read_accepted_joint_plan(root, local.date().isoformat())
    if selected:
        plan, _, run = selected
        _assert_execution_deployment(root, run, action_date=local.date().isoformat())
        from ml.stock_trader.catchup import catchup_signals
        return catchup_signals(plan, as_of=now, source_fingerprint=run.name), (run / "accepted-plan.json", run / "joint-plan.json")
    frame, run = execution_frame(root, action_date=local.date().isoformat())
    from ml.stock_trader.catchup import catchup_signals, native_catchup_plan
    native = native_catchup_plan(root, action_date=local.date().isoformat(), source_run=run)
    if native:
        plan, _, sources = native
        return catchup_signals(plan, as_of=now, source_fingerprint=run.name), sources
    start = local.floor("h").tz_convert("UTC")
    if not 4 <= local.hour < 17:
        return {}, ()
    due = frame.loc[frame.target_window_start.eq(start)]
    signals = {}
    for row, probability, abstention in _validated_instructions(due):
        if abstention:
            continue
        key = (str(row["symbol"]), str(row["model_group"]))
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
        instructions = _validated_instructions(frame)
        return {"status": "READY", "reason": "SAVED_GAMEPLAN_TRADING_INSTRUCTIONS",
                "execution_window_count": len(frame),
                "saved_abstention_count": sum(abstention for _, _, abstention in instructions),
                "run_path": str(run)}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return {"status": "NOT_READY", "reason": "GAMEPLAN_TRADING_INSTRUCTIONS_UNAVAILABLE", "error": str(exc)}
