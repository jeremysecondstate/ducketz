"""Read due independent stock signals from a verified immutable Gameplan.

This reader has no order authority. Each (symbol, horizon) is a separate signal;
portfolio sizing, ownership, exactly-once execution, and exits belong to the
stock executor. Neither a context forecast nor a legacy target can authorize an
independent horizon entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum
from ml.stock_direction_policy import stock_direction
from ml.nightly_gameplan import ASSUMED_ROUND_TRIP_COST, EXECUTION_AUTHORITY, read_current_gameplan
from ml.stock_trader.contracts import PredictionSignal, STOCK_TRADER_SYMBOLS, canonical_sha256, utc
from ml.stock_trader.gameplan import GAMEPLAN_TIMEZONE, _entry_deadline, _gameplan_source_files
from ml.stock_trader.session import checkpoint_session_for_target
from ml.stock_trader.market_features import read_frozen_market_feature_values


INDEPENDENT_STOCK_HORIZONS = ("1h", "4h", "1d", "1w")
_EXPECTED_GROUP_COUNTS = {"1h": 14, "4h": 4, "1d": 5, "1w": 1}
_REQUIRED_COLUMNS = {
    "id", "symbol", "model_group", "route", "decision_timestamp",
    "information_available_at", "target_window_start", "target_window_end",
    "calibrated_probability", "model_family", "model_status", "direction",
    "frozen_at", "action_date", "target_contract_version", "execution_authority",
    "broker_orders_enabled", "target_role", "execution_eligible", "action_anchor_local",
}
_TIMESTAMP_COLUMNS = (
    "decision_timestamp", "information_available_at", "target_window_start",
    "target_window_end", "frozen_at",
)


def load_current_independent_gameplan_signals(
    datastore_root: Path,
    *,
    as_of: object,
    require_promoted_model_reports: bool = False,
    late_opening_date: str | None = None,
    execution_ready_plan: bool = False,
) -> tuple[dict[tuple[str, str], PredictionSignal], tuple[Path, ...]]:
    """Return only due, promoted, sufficiently directional independent signals.

The frozen target end is retained verbatim as the required position expiry.
Calls outside an action boundary's existing entry grace return no entries;
neither late predictions nor missing action slots are replayed.
"""

    if execution_ready_plan:
        from ml.stock_trader.gameplan_execution import load_execution_signals
        return load_execution_signals(datastore_root, as_of=as_of)

    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION

    root = Path(datastore_root).resolve()
    timestamp = utc(as_of)
    local = timestamp.tz_convert(GAMEPLAN_TIMEZONE)
    if late_opening_date is not None:
        from ml.stock_trader.gameplan import validate_late_opening_date
        validate_late_opening_date(late_opening_date, timestamp)
    publication = read_current_gameplan(root)
    configuration = publication.manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Independent stock Gameplan has no configuration")
    if configuration.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION:
        raise ValueError("Independent stock execution requires its versioned target contract; legacy windows are not eligible")
    action_date = str(publication.receipt.get("action_date") or "")
    if action_date != local.date().isoformat() or configuration.get("action_date") != action_date:
        raise ValueError("Current independent stock Gameplan is not for this action date")
    symbols = tuple(configuration.get("symbols") or ())
    if symbols != tuple(STOCK_TRADER_SYMBOLS):
        raise ValueError("Independent stock Gameplan universe differs from the configured trader")
    outputs = publication.manifest.get("output_files")
    if not isinstance(outputs, Mapping) or not {
        "gameplan.json", "forecasts.parquet", "option-strategy-intents.parquet"
    }.issubset(outputs):
        raise ValueError("Independent stock Gameplan manifest omits required forecast outputs")
    source_files = _gameplan_source_files(root, publication.run_directory)
    plan = json.loads((publication.run_directory / "gameplan.json").read_text(encoding="utf-8"))
    if (
        plan.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
        or plan.get("action_date") != action_date
        or tuple(plan.get("symbols") or ()) != symbols
        or plan.get("execution_authority") != EXECUTION_AUTHORITY
        or plan.get("broker_orders_enabled") is not False
        or plan.get("orders_placed") != 0
    ):
        raise ValueError("Independent stock Gameplan payload differs from its publication")
    normalized = _validated_independent_forecasts(
        pd.read_parquet(publication.run_directory / "forecasts.parquet"),
        action_date=action_date,
        symbols=symbols,
    )
    from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE
    if not normalized["target_price_source_contract"].eq(
        configuration.get("target_price_source_contract", CANONICAL_STOCK_PRICE_SOURCE)
    ).all():
        raise ValueError("Independent stock forecast rows differ from their declared target price source")
    _validate_intent_keys(
        pd.read_parquet(publication.run_directory / "option-strategy-intents.parquet"),
        normalized,
    )
    promoted_groups = verified_promoted_model_groups(publication) if require_promoted_model_reports else None
    source_fingerprint = canonical_sha256({
        "run": publication.run_directory.relative_to(root).as_posix(),
        "files": {source.relative_to(root).as_posix(): file_checksum(source) for source in source_files},
    })
    if not 4 <= local.hour < 17:
        return {}, source_files
    action_start = local.floor("h").tz_convert("UTC")
    deadline = _entry_deadline(action_start, late_opening_date=late_opening_date)
    if not action_start <= timestamp < deadline:
        return {}, source_files
    due = normalized.loc[
        normalized["target_role"].eq("EXECUTION")
        & normalized["target_window_start"].eq(action_start)
    ]
    signals: dict[tuple[str, str], PredictionSignal] = {}
    for row in due.to_dict("records"):
        symbol, horizon = str(row["symbol"]), str(row["model_group"])
        probability = float(row["calibrated_probability"])
        direction = str(row["direction"]).upper()
        if str(row["model_status"]).upper() != "PROMOTED" or direction in {"NEUTRAL", "NO_EDGE"}:
            continue
        if promoted_groups is not None and horizon not in promoted_groups:
            raise ValueError(f"{symbol}/{horizon} forecast promotion differs from its verified model report")
        if direction not in {"BULLISH", "BEARISH"}:
            raise ValueError(f"{symbol}/{horizon} has an invalid direction")
        if direction != stock_direction(probability):
            raise ValueError(f"{symbol}/{horizon} direction and probability disagree")
        signals[(symbol, horizon)] = PredictionSignal(
            symbol=symbol,
            primary_horizon=horizon,
            prediction_id=str(row["id"]),
            decision_timestamp=utc(row["decision_timestamp"]).isoformat(),
            target_window_start=utc(row["target_window_start"]).isoformat(),
            target_window_end=utc(row["target_window_end"]).isoformat(),
            actionable_until=min(deadline, utc(row["target_window_end"])).isoformat(),
            prediction_created_at=utc(row["frozen_at"]).isoformat(),
            calibrated_probability=probability,
            assumed_round_trip_cost=ASSUMED_ROUND_TRIP_COST,
            horizon_probabilities={horizon: probability},
            model_name=str(row["model_family"]),
            model_version=str(row.get("model_artifact") or ""),
            source_fingerprint=source_fingerprint,
            checkpoint_session=checkpoint_session_for_target(action_start),
            target_definition_version=STOCK_TARGET_CONTRACT_VERSION,
            target_price_source_contract=str(row["target_price_source_contract"]),
            enrichment_feature_values=read_frozen_market_feature_values(row),
        )
    return signals, source_files


def verified_promoted_model_groups(publication) -> frozenset[str]:
    """Require actual manifest-bound model gates for deterministic stock sizing."""
    import joblib
    from ml.nightly_gameplan import GAMEPLAN_VERSION
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
    from ml.stock_target_prices import stock_price_dataset
    from ml.stock_trader.contracts import finite
    outputs = publication.manifest.get("output_files", {})
    if not {"model-reports.json", "forecasts.parquet"}.issubset(outputs):
        raise ValueError("Stock execution requires manifest-bound forecast model reports")
    config = publication.manifest.get("configuration", {})
    source = config.get("target_price_source_contract")
    if (config.get("schema_version") != GAMEPLAN_VERSION
            or config.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
            or config.get("target_price_dataset") != stock_price_dataset(source)):
        raise ValueError("Stock model publication has an incompatible schema or price source")
    forecasts = pd.read_parquet(publication.run_directory / "forecasts.parquet")
    reports = json.loads((publication.run_directory / "model-reports.json").read_text(encoding="utf-8"))
    if not isinstance(reports, dict) or not set(INDEPENDENT_STOCK_HORIZONS).issubset(reports):
        raise ValueError("Stock execution requires all four forecast model reports")
    from ml.gameplan_promotion import validate_promoted_report
    promoted = set()
    for horizon in INDEPENDENT_STOCK_HORIZONS:
        report = reports[horizon]
        if not isinstance(report, Mapping):
            raise ValueError("Stock forecast model report is invalid")
        gate = report.get("promotion_gate", {})
        if not isinstance(gate, Mapping):
            raise ValueError("Stock forecast promotion gate is invalid")
        if gate.get("status") != "PROMOTED":
            continue
        validate_promoted_report(report)
        if (report.get("schema_version") != GAMEPLAN_VERSION or report.get("group") != horizon
                or report.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
                or report.get("target_price_source_contract") != source
                or report.get("target_price_dataset") != stock_price_dataset(source)):
            raise ValueError("Promoted model report differs from its horizon/schema/target/source contract")
        try:
            model_file = report["model_file"]
            model_name = str(model_file["path"])
            cohort_name = report.get("deployment", {}).get("retained_cohort_output", f"training-cohort-{horizon}.parquet")
            for name in (model_name, cohort_name):
                if name not in outputs or not (publication.run_directory / name).resolve().is_relative_to(publication.run_directory.resolve()):
                    raise ValueError("Promoted model or training evidence is not manifest-bound")
            model_path = publication.run_directory / model_name
            if model_path.stat().st_size != model_file["size"] or file_checksum(model_path) != model_file["checksum_sha256"]:
                raise ValueError("Promoted model artifact differs from its bound report")
            try:
                payload = joblib.load(model_path)
            except Exception as exc:
                raise ValueError("Promoted model artifact cannot be loaded") from exc
            if (not isinstance(payload, Mapping) or payload.get("schema_version") != GAMEPLAN_VERSION
                    or payload.get("group") != horizon
                    or tuple(payload.get("feature_columns", ())) != tuple(report["features"]["admitted"])
                    or not payload.get("feature_columns")
                    or tuple(payload.get("categorical_columns", ())) != ("symbol", "route")
                    or payload.get("selected_family") != report["selected_family"]
                    or getattr(payload.get("calibrator"), "method", None) != report["calibration_method"]
                    or not callable(getattr(payload.get("estimator"), "predict_proba", None))
                    or pd.Timestamp(payload["trained_at"]) > pd.Timestamp(publication.receipt["published_at"])):
                raise ValueError("Promoted model payload differs from its verified report")
            rows = forecasts.loc[forecasts.model_group.eq(horizon)]
            if (rows.empty or not rows.model_artifact.eq(model_name).all()
                    or not rows.model_family.eq(report["selected_family"]).all()):
                raise ValueError("Forecast rows do not reference their qualified model artifact")
            support = report["target_support_by_symbol"]
            for row in rows.loc[rows.model_status.eq("PROMOTED")].to_dict("records"):
                counts = support.get(str(row["symbol"]), {})
                if (counts.get("fitted_rows", 0) <= 0
                        or counts.get("fitted_rows_by_route", {}).get(str(row["route"]), 0) <= 0):
                    raise ValueError("Promoted forecast lacks fitted symbol and exact-route target support")
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Promoted model lacks valid fitted artifact and assessment evidence") from exc
        promoted.add(horizon)
    return frozenset(promoted)


def _validated_independent_forecasts(
    frame: pd.DataFrame,
    *,
    action_date: str,
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION

    missing = _REQUIRED_COLUMNS.difference(frame.columns)
    if missing:
        raise ValueError("Independent stock forecasts are missing columns: " + ", ".join(sorted(missing)))
    if frame.empty or frame["id"].isna().any() or frame["id"].astype(str).eq("").any() or frame["id"].duplicated().any():
        raise ValueError("Independent stock forecast IDs are empty or duplicated")
    data = frame.copy()
    from ml.stock_target_prices import independent_price_identity
    source_identities = [independent_price_identity(row) for row in data.to_dict("records")]
    if len(set(source_identities)) != 1:
        raise ValueError("Independent stock forecasts mix target price sources")
    data["target_price_source_contract"] = [value[0] for value in source_identities]
    data["target_price_dataset"] = [value[1] for value in source_identities]
    if set(data["symbol"]) != set(symbols) or data.duplicated(["symbol", "route"]).any():
        raise ValueError("Independent stock forecast universe or route identity is invalid")
    if not data["action_date"].astype(str).eq(action_date).all():
        raise ValueError("Independent stock forecast action dates differ")
    if not data["target_contract_version"].eq(STOCK_TARGET_CONTRACT_VERSION).all():
        raise ValueError("Independent stock execution rejects legacy target rows")
    if not data["execution_authority"].eq(EXECUTION_AUTHORITY).all() or not data["broker_orders_enabled"].map(lambda value: value is False).all():
        raise ValueError("Independent stock forecasts claim unexpected execution authority")
    for column in _TIMESTAMP_COLUMNS:
        data[column] = pd.to_datetime(data[column], utc=True, errors="coerce")
    if data[list(_TIMESTAMP_COLUMNS)].isna().any().any():
        raise ValueError("Independent stock forecast timestamp is invalid")
    probabilities = pd.to_numeric(data["calibrated_probability"], errors="coerce")
    if probabilities.isna().any() or not probabilities.between(0.0, 1.0).all():
        raise ValueError("Independent stock forecast probability is invalid")
    data["calibrated_probability"] = probabilities
    if not data["target_window_end"].gt(data["target_window_start"]).all():
        raise ValueError("Independent stock target expiry must follow its start")
    for symbol, rows in data.groupby("symbol", sort=False):
        if rows["model_group"].value_counts().to_dict() != _EXPECTED_GROUP_COUNTS:
            raise ValueError(f"{symbol} independent forecast grid must have 14/4/5/1 rows")
    _validate_target_windows(data, action_date=action_date)
    execution = data.loc[data["target_role"].eq("EXECUTION")]
    if any(not execution[column].le(execution["target_window_start"]).all() for column in (
        "decision_timestamp", "information_available_at", "frozen_at"
    )):
        raise ValueError("Independent stock execution forecast contains future information")
    if not data["information_available_at"].le(data["frozen_at"]).all():
        raise ValueError("Independent stock forecast information postdates publication")
    if execution.duplicated(["symbol", "model_group", "target_window_start"]).any():
        raise ValueError("Independent stock execution slots are duplicated")
    return data


def _validate_target_windows(data: pd.DataFrame, *, action_date: str) -> None:
    """Compare frozen windows with the independently versioned target builder."""

    from ml.independent_stock_targets import stock_target_windows

    expected = {str(spec["route"]): spec for spec in stock_target_windows(pd.Timestamp(action_date).date())}
    for row in data.to_dict("records"):
        spec = expected.get(str(row["route"]))
        if spec is None:
            raise ValueError("Independent stock forecast route is invalid")
        for key in ("model_group", "target_role", "execution_eligible"):
            if row[key] != spec[key]:
                raise ValueError(f"Independent stock target {row['route']} differs in {key}")
        expected_action = utc(spec["target_window_start"]).tz_convert(GAMEPLAN_TIMEZONE).strftime("%H:%M") if spec["execution_eligible"] else None
        action = None if pd.isna(row["action_anchor_local"]) else row["action_anchor_local"]
        if action != expected_action:
            raise ValueError(f"Independent stock target {row['route']} differs in action_anchor_local")
        for key in ("target_window_start", "target_window_end"):
            if utc(row[key]) != utc(spec[key]):
                raise ValueError(f"Independent stock target {row['route']} differs in {key}")


def _validate_intent_keys(intents: pd.DataFrame, forecasts: pd.DataFrame) -> None:
    if not {"symbol", "route"}.issubset(intents.columns) or intents.duplicated(["symbol", "route"]).any():
        raise ValueError("Independent stock Gameplan option table has invalid identities")
    if set(map(tuple, intents[["symbol", "route"]].to_numpy())) != set(map(tuple, forecasts[["symbol", "route"]].to_numpy())):
        raise ValueError("Independent stock Gameplan option table does not match forecast routes")


__all__ = ["INDEPENDENT_STOCK_HORIZONS", "load_current_independent_gameplan_signals"]
