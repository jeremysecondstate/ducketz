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
        from ml.account_gameplan.config import load_account_config
        if load_account_config(root) is not None:
            plan, _ = account_execution_plan(root, action_date=action_date)
            if publication is None or Path(publication).resolve() != plan.path:
                raise ValueError("UNSELECTED_COMBINED_ACCOUNT_GAMEPLAN")
            return
        assert_execution_gameplan(root, publication, action_date=action_date)
    except (OSError, ValueError, RuntimeError) as exc:
        raise GameplanDeploymentUnavailable(str(exc)) from exc


def account_execution_plan(root, *, action_date):
    """Read this session's immutable selection and the reviewed sole-host binding."""
    from datetime import date
    from ml.account_gameplan.config import (VERSION, assert_coordinator, load_account_config,
                                           local_path, verify_cutover)
    from ml.account_gameplan.planner import read_account_plan
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    config = load_account_config(root)
    if config is None:
        raise ValueError("COMBINED_ACCOUNT_NOT_CONFIGURED")
    assert_coordinator(config, GAMEPLAN_SIZING_POLICY)
    verify_cutover(root, config)
    day = date.fromisoformat(str(action_date)).isoformat()
    saved = json.loads((Path(root) / "ml/account-gameplan-by-date" / day / "run.json").read_text())
    if (saved.get("schema_version") != VERSION or saved.get("status") != "SELECTED" or saved.get("action_date") != day
            or saved.get("config_sha256") != config.fingerprint):
        raise ValueError("COMBINED_ACCOUNT_SELECTION_BINDING_MISMATCH")
    ref = saved["current"]
    run = local_path(root, ref["run_path"], "ml/account-gameplan-runs")
    plan = read_account_plan(run, expected_manifest_sha256=ref["manifest_sha256"])
    membership = {key: tuple(value) for key, value in config.participants.items()}
    found = {source["producer_id"]: tuple(source["symbols"]) for source in plan.report["sources"]}
    if (found != membership or plan.report["action_date"] != day
            or plan.report.get("account_fingerprint") != config.account_fingerprint):
        raise ValueError("COMBINED_ACCOUNT_UNIVERSE_SESSION_OR_ACCOUNT_MISMATCH")
    # The local producer retains its native selected-source guard. The peer's
    # immutable export carries its own source-selection proof.
    from ml.artifacts import file_checksum
    from ml.gameplan_deployment import assert_execution_gameplan
    source = next(row for row in plan.report["sources"] if row["producer_id"] == config.machine_id)
    native = local_path(root, source["source_gameplan_run"], "ml/nightly-gameplan-runs")
    if file_checksum(native / "receipt.json") != source["source_receipt_sha256"]:
        raise ValueError("LOCAL_FROZEN_SOURCE_CHANGED")
    assert_execution_gameplan(root, native, action_date=day)
    return plan, config


def execution_frame(root: Path, *, action_date: str):
    root = Path(root).resolve()
    from ml.account_gameplan.config import load_account_config
    config = load_account_config(root)
    if config is not None:
        plan, config = account_execution_plan(root, action_date=action_date)
        run, frame, symbols = plan.path, plan.rows.copy(), config.symbols
    else:
        pointer = json.loads((root / "ml/nightly-gameplan-latest/run.json").read_text(encoding="utf-8"))
        run = (root / pointer["current"]["run_path"]).resolve()
        if run.parent != (root / "ml/nightly-gameplan-runs").resolve():
            raise ValueError("Gameplan run is outside its saved directory")
        _assert_execution_deployment(root, run, action_date=action_date)
        frame = pd.read_parquet(run / "forecasts.parquet")
        symbols = STOCK_TRADER_SYMBOLS
    required = {"id", "symbol", "model_group", "direction", "calibrated_probability",
                "target_window_start", "target_window_end", "execution_eligible", "action_date"}
    if required.difference(frame):
        raise ValueError("Gameplan is missing trading instructions: " + ", ".join(sorted(required.difference(frame))))
    frame = frame.loc[frame.action_date.astype(str).eq(action_date)
                      & frame.symbol.isin(symbols) & frame.execution_eligible.eq(True)].copy()
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
    frame, run = execution_frame(root, action_date=local.date().isoformat())
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
