"""Read-only, verified snapshots for the Gameplan Stats tab.

Use the saved actuals review, never current quotes or a newly fitted model.
The summary follows the horizon filter; the explicitly labelled 1h grid does not.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from datafetching.parquet_store import resolve_datastore_dir
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_probability_target import (
    LEGACY_COST_TARGET, RAW_DIRECTION_TARGET, observed_probability_target,
    probability_target_contract, probability_target_metadata,
)

VERSION = "gameplan-actuals-review-v1"
PACIFIC = ZoneInfo("America/Los_Angeles")
HORIZONS = ("1h", "4h", "1d", "1w")
STATUSES = {"EVALUATED", "PENDING_MATURITY", "MATURE_AWAITING_DATA"}


class GameplanStatsError(ValueError):
    """The requested saved review is unavailable or cannot be verified."""


@dataclass(frozen=True)
class PredictionOutcome:
    forecast_id: str
    symbol: str
    horizon: str
    route: str
    role: str
    direction: str
    status: str
    probability: float
    correct: bool | None
    brier: float | None
    actual_return: float | None
    target_cost: float
    start: datetime
    end: datetime
    observed_start: datetime | None
    observed_end: datetime | None
    probability_target_contract: str = LEGACY_COST_TARGET

    @property
    def probability_target_threshold(self) -> float:
        return 0.0 if self.probability_target_contract == RAW_DIRECTION_TARGET else self.target_cost

    @property
    def state(self) -> str:
        if self.status == "PENDING_MATURITY":
            return "pending"
        if self.status == "MATURE_AWAITING_DATA":
            return "awaiting_data"
        if self.correct is None:
            return "neutral"
        return "correct" if self.correct else "incorrect"


@dataclass(frozen=True)
class PredictionMetrics:
    total: int
    evaluated: int
    pending: int
    awaiting_data: int
    neutral: int
    correct: int
    scored: int
    bullish_correct: int
    bullish_scored: int
    bearish_correct: int
    bearish_scored: int
    brier: float | None

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.scored if self.scored else None

    @property
    def bullish_accuracy(self) -> float | None:
        return self.bullish_correct / self.bullish_scored if self.bullish_scored else None

    @property
    def bearish_accuracy(self) -> float | None:
        return self.bearish_correct / self.bearish_scored if self.bearish_scored else None


def prediction_metrics(rows: tuple[PredictionOutcome, ...]) -> PredictionMetrics:
    evaluated = tuple(row for row in rows if row.status == "EVALUATED")
    scored = tuple(row for row in evaluated if row.correct is not None)
    bullish = tuple(row for row in scored if row.direction == "BULLISH")
    bearish = tuple(row for row in scored if row.direction == "BEARISH")
    errors = [row.brier for row in evaluated if row.brier is not None]
    return PredictionMetrics(
        total=len(rows), evaluated=len(evaluated),
        pending=sum(row.status == "PENDING_MATURITY" for row in rows),
        awaiting_data=sum(row.status == "MATURE_AWAITING_DATA" for row in rows),
        neutral=len(evaluated) - len(scored),
        correct=sum(row.correct is True for row in scored), scored=len(scored),
        bullish_correct=sum(row.correct is True for row in bullish), bullish_scored=len(bullish),
        bearish_correct=sum(row.correct is True for row in bearish), bearish_scored=len(bearish),
        brier=sum(errors) / len(errors) if errors else None,
    )


@dataclass(frozen=True)
class GameplanStatsReview:
    session: str
    reviewed_at: datetime
    outcomes_through: datetime
    run_directory: Path
    report_path: Path
    outcomes: tuple[PredictionOutcome, ...]
    excluded_forecasts: int = 0
    probability_target_contract: str = LEGACY_COST_TARGET

    @property
    def display_name(self) -> str:
        return "Yung Gameplan (YG)" if self.probability_target_contract == RAW_DIRECTION_TARGET else "OG Gameplan"

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({row.symbol for row in self.outcomes}))

    def rows(self, horizon: str = "all", symbol: str | None = None) -> tuple[PredictionOutcome, ...]:
        if horizon not in ("all", *HORIZONS):
            raise ValueError("Unknown forecast horizon")
        return tuple(row for row in self.outcomes
                     if (horizon == "all" or row.horizon == horizon)
                     and (symbol is None or row.symbol == symbol))

    def metrics(self, horizon: str = "all", symbol: str | None = None) -> PredictionMetrics:
        return prediction_metrics(self.rows(horizon, symbol))

    def hourly(self, symbol: str) -> tuple[PredictionOutcome | None, ...]:
        by_route = {row.route: row for row in self.outcomes
                    if row.symbol == symbol and row.horizon == "1h" and row.role == "EXECUTION"}
        return tuple(by_route.get(f"1h@{hour:02d}:00") for hour in range(4, 17))


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GameplanStatsError(f"Invalid review metadata: {path.name}")
    return value


def _session(value: object) -> str:
    text = str(value)
    if date.fromisoformat(text).isoformat() != text:
        raise GameplanStatsError("Invalid review session date")
    return text


def _pointer(path: Path, root: Path) -> tuple[str, Path, dict]:
    pointer = _json(path)
    current = pointer.get("current", {})
    if pointer.get("schema_version") != VERSION or not isinstance(current, dict):
        raise GameplanStatsError("Unsupported actuals-review pointer")
    session = _session(current.get("action_date"))
    run = (root / str(current.get("run_path", ""))).resolve()
    if run.parent != (root / "ml/gameplan-actuals-review-runs").resolve():
        raise GameplanStatsError("Actuals-review pointer is outside its saved run directory")
    return session, run, current


def review_sessions(datastore_root: Path | None = None) -> tuple[str, ...]:
    """List dated publication pointers, including a freshly published latest date.

    Do not scan training runs or read price history just to populate a selector.
    The chosen pointer and its outputs are verified by load_gameplan_stats.
    """
    root = resolve_datastore_dir(root_dir=datastore_root).resolve()
    dates = set()
    by_date = root / "ml/gameplan-actuals-review-by-date"
    if by_date.is_dir():
        for path in by_date.glob("*/run.json"):
            try:
                dates.add(_session(path.parent.name))
            except (ValueError, TypeError):
                continue
    latest = root / "ml/gameplan-actuals-review-latest/run.json"
    if latest.is_file():
        dates.add(_pointer(latest, root)[0])
    return tuple(sorted(dates, reverse=True))


def _timestamp(value: object, label: str) -> datetime:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise GameplanStatsError(f"Missing or unzoned {label}")
    return stamp.tz_convert(PACIFIC).to_pydatetime()


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _outcome(row: dict) -> PredictionOutcome:
    contract = probability_target_contract(row)
    probability = _number(row["calibrated_probability"])
    status, direction = str(row["actuals_status"]), str(row["direction"])
    if probability is None or not 0 <= probability <= 1 or status not in STATUSES:
        raise GameplanStatsError("A saved forecast has an invalid probability or outcome status")
    if direction not in {"BULLISH", "BEARISH", "NO_EDGE"} or row["model_group"] not in HORIZONS:
        raise GameplanStatsError("A saved forecast has an unsupported direction or horizon")
    start, end = _timestamp(row["target_window_start"], "target start"), _timestamp(row["target_window_end"], "target end")
    if start >= end:
        raise GameplanStatsError("A saved forecast has an invalid target window")
    observed = []
    for field in ("actual_start_observed_at", "actual_end_observed_at"):
        value = row.get(field)
        observed.append(None if value is None or pd.isna(value) else _timestamp(value, field))
    cost = _number(row.get("assumed_round_trip_cost"))
    cost = 0.001 if cost is None else cost
    actual_return = _number(row.get("actual_return"))
    correct = brier = None
    if status == "EVALUATED":
        target, saved_brier = _number(row["model_observed_target"]), _number(row["model_brier_score"])
        if actual_return is None or target not in (0, 1) or target != observed_probability_target(actual_return, cost, contract):
            raise GameplanStatsError("A saved evaluated forecast has invalid target evidence")
        brier = (probability - target) ** 2
        if saved_brier is None or not math.isclose(brier, saved_brier, rel_tol=1e-9, abs_tol=1e-12):
            raise GameplanStatsError("Saved probability error disagrees with its forecast outcome")
        if direction != "NO_EDGE":
            correct = actual_return > 0 if direction == "BULLISH" else actual_return < 0
            if pd.isna(row["direction_correct"]) or bool(row["direction_correct"]) != correct:
                raise GameplanStatsError("Saved direction result disagrees with the actual return")
        elif not pd.isna(row["direction_correct"]):
            raise GameplanStatsError("A neutral forecast cannot have a directional score")
    # Do not score pending/missing rows, even if they contain partial observations.
    return PredictionOutcome(
        str(row["id"]), str(row["symbol"]), str(row["model_group"]), str(row["route"]),
        str(row["target_role"]), direction, status, probability, correct, brier,
        actual_return, cost, start, end, *observed, contract,
    )


def load_gameplan_stats(datastore_root: Path | None = None, session: str | None = None) -> GameplanStatsReview:
    root = resolve_datastore_dir(root_dir=datastore_root).resolve()
    try:
        requested = _session(session) if session is not None else None
        pointer_path = root / "ml/gameplan-actuals-review-latest/run.json"
        if requested:
            dated = root / f"ml/gameplan-actuals-review-by-date/{requested}/run.json"
            if dated.is_file():
                pointer_path = dated
        selected, run, pointer = _pointer(pointer_path, root)
        if requested and selected != requested:
            raise GameplanStatsError(f"No verified results have been published for {requested}")
        receipt_path = run / "receipt.json"
        if file_checksum(receipt_path) != pointer.get("receipt_sha256"):
            raise GameplanStatsError("Review receipt does not match its publication pointer")
        receipt = _json(receipt_path)
        if (receipt.get("schema_version") != VERSION or receipt.get("status") != "COMPLETE"
                or receipt.get("action_date") != selected
                or (root / str(receipt.get("run_path", ""))).resolve() != run
                or receipt.get("manifest_sha256") != file_checksum(run / "manifest.json")):
            raise GameplanStatsError("Review receipt and saved publication disagree")
        manifest = verify_manifest(run)
        required_outputs = {"report.json", "forecast-results.parquet", "Gameplan-results.md"}
        configuration = manifest.get("configuration", {})
        if (not required_outputs.issubset(manifest["output_files"])
                or configuration.get("schema_version") != VERSION
                or configuration.get("action_date") != selected):
            raise GameplanStatsError("Saved review manifest has an invalid result contract")
        report = _json(run / "report.json")
        if (report.get("status") != "COMPLETE" or report.get("schema_version") != VERSION
                or report.get("action_date") != selected or report.get("preview")):
            raise GameplanStatsError("Only completed, verified session reviews can be displayed")
        frame = pd.read_parquet(run / "forecast-results.parquet")
        contract = probability_target_contract(frame) if not frame.empty else probability_target_contract(report)
        for metadata in (report, configuration):
            if "probability_target_contract" in metadata and probability_target_contract(metadata) != contract:
                raise GameplanStatsError("Saved review probability targets disagree")
        if "gameplan_variant" in frame and not frame.gameplan_variant.eq(probability_target_metadata(contract)["gameplan_variant"]).all():
            raise GameplanStatsError("Saved review variant disagrees with its probability target")
        outcomes: tuple[PredictionOutcome, ...] = ()
        excluded = 0
        if not frame.empty:
            required = {"id", "symbol", "model_status", "model_group", "route", "target_role", "action_date",
                        "direction", "actuals_status", "direction_correct", "calibrated_probability",
                        "model_observed_target", "model_brier_score", "actual_return", "target_window_start", "target_window_end"}
            if (not required.issubset(frame) or frame.id.isna().any() or frame.id.duplicated().any()
                    or not frame.action_date.astype(str).eq(selected).all()
                    or frame.symbol.isna().any() or frame.duplicated(["symbol", "route"]).any()):
                raise GameplanStatsError("Saved forecast identities or session fields are invalid")
            approved = frame.loc[frame.model_status.eq("PROMOTED")]
            excluded = len(frame) - len(approved)
            outcomes = tuple(_outcome(row) for row in approved.to_dict("records"))
        return GameplanStatsReview(
            selected, _timestamp(report["reviewed_at"], "review time"),
            _timestamp(report["outcomes_through"], "outcome cutoff"), run,
            run / "Gameplan-results.md", outcomes, excluded, contract,
        )
    except GameplanStatsError:
        raise
    except FileNotFoundError as exc:
        raise GameplanStatsError("Saved results are not available yet. Refresh after the nightly results review finishes.") from exc
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise GameplanStatsError(f"Could not verify saved Gameplan results: {exc}") from exc
