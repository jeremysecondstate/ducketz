from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pandas as pd

from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_strategy_orders import (
    SchwabPositionContext,
    StrategyOrderDraft,
    build_strategy_order_draft,
    schwab_position_context,
)
from datafetching.parquet_store import resolve_datastore_dir
from ml.current_publication import resolve_current_output
from ml.strategy_publication import resolve_current_strategy_output
from ml.parquet_contracts import (
    STRATEGY_AUDIT_SCHEMA,
    STRATEGY_CANDIDATE_SCHEMA,
    verify_parquet_schema,
)
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    COMPATIBLE_STRATEGY_MODEL_POLICY_VERSIONS,
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_COVERAGE_SCORE_BASIS,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
)


HORIZON_LABELS = {
    "1h": "1 Hour",
    "4h": "4 Hour",
    "1d": "1 Day",
    "1w": "Five-Session Aggregate",
    "1w-d1": "Week Day 1",
    "1w-d2": "Week Day 2",
    "1w-d3": "Week Day 3",
    "1w-d4": "Week Day 4",
    "1w-d5": "Week Day 5",
}
HORIZON_ORDER = tuple(HORIZON_LABELS)
PORTFOLIO_FIT_POLICY_VERSION = "current-schwab-position-fit-v2"
_SCORE_BASIS_LABELS = {
    BSGP_CALIBRATED_MODEL_SCORE_BASIS: "BSGP + Strategy ML",
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS: "Black-Scholes + ML",
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS: "OPRA Execution + ML",
    SCENARIO_COVERAGE_SCORE_BASIS: "Scenario Coverage",
}


@dataclass(frozen=True)
class PortfolioFit:
    label: str
    detail: str
    policy_version: str = PORTFOLIO_FIT_POLICY_VERSION


@dataclass(frozen=True)
class StrategyCandidateView:
    candidate_id: str
    symbol: str
    horizon: str
    horizon_label: str
    rank: int
    strategy_name: str
    strategy_display_name: str
    exact_legs: str
    direction_probability_up: float
    predictive_score: float | None
    scenario_coverage: float
    expected_net_profit: float | None
    expected_return: float | None
    portfolio_fit: PortfolioFit
    score_basis: str
    pricing_summary: str
    quality_warning: str
    manual_order_actionable: bool
    manual_actionability: str
    position: SchwabPositionContext
    order_draft: StrategyOrderDraft
    row: Mapping[str, object]

    @property
    def model_summary(self) -> str:
        return (
            "Calibrated profit model active"
            if self.predictive_score is not None
            else "ML unavailable for this row; Scenario Coverage only"
        )


@dataclass(frozen=True)
class StrategyCandidatesView:
    source_path: Path
    audit_source_path: Path | None
    loaded_at: datetime
    candidates: tuple[StrategyCandidateView, ...]
    symbols: tuple[str, ...]
    horizons_by_symbol: Mapping[str, tuple[str, ...]]
    route_diagnoses: Mapping[tuple[str, str], str]
    empty_diagnosis: str | None


def default_strategy_candidates_path() -> Path:
    return (
        resolve_datastore_dir()
        / "ml"
        / "strategy-latest"
        / "strategy-candidates.parquet"
    )


def load_strategy_candidates(
    path: Path | None = None,
    *,
    snapshot: PortfolioSnapshot,
    loaded_at: datetime | None = None,
) -> StrategyCandidatesView:
    requested = Path(path or default_strategy_candidates_path())
    source = _resolve_authoritative_source(requested)
    if not source.is_file():
        raise FileNotFoundError(
            f"Options strategy candidates have not been published: {source}"
        )
    verify_parquet_schema(source, STRATEGY_CANDIDATE_SCHEMA)
    frame = pd.read_parquet(source)
    candidates = _candidate_views(frame, snapshot=snapshot)
    audit_source, audit = _load_matching_audit(source)
    route_diagnoses = _route_diagnoses(audit)
    routes = {(item.symbol, item.horizon) for item in candidates}
    routes.update(route_diagnoses)
    symbols = tuple(sorted({symbol for symbol, _horizon in routes}))
    horizons_by_symbol = {
        symbol: tuple(
            horizon
            for horizon in HORIZON_ORDER
            if (symbol, horizon) in routes
        )
        for symbol in symbols
    }
    return StrategyCandidatesView(
        source_path=source,
        audit_source_path=audit_source,
        loaded_at=loaded_at or datetime.now(timezone.utc),
        candidates=candidates,
        symbols=symbols,
        horizons_by_symbol=horizons_by_symbol,
        route_diagnoses=route_diagnoses,
        empty_diagnosis=_dominant_audit_reason(audit),
    )


def _load_matching_audit(
    candidates_source: Path,
) -> tuple[Path | None, pd.DataFrame]:
    source = candidates_source.with_name("strategy-audit.parquet")
    if not source.is_file():
        return None, pd.DataFrame()
    verify_parquet_schema(source, STRATEGY_AUDIT_SCHEMA)
    return source, pd.read_parquet(source)


def _route_diagnoses(frame: pd.DataFrame) -> dict[tuple[str, str], str]:
    if frame.empty:
        return {}
    diagnoses: dict[tuple[str, str], str] = {}
    for (symbol, horizon), route in frame.groupby(
        ["symbol", "horizon"], sort=False, dropna=False
    ):
        if pd.isna(symbol) or pd.isna(horizon):
            continue
        clean_symbol = str(symbol).strip().upper()
        clean_horizon = str(horizon).strip().lower()
        if not clean_symbol or clean_horizon not in HORIZON_LABELS:
            continue
        reason = _dominant_audit_reason(route)
        if reason:
            diagnoses[(clean_symbol, clean_horizon)] = reason
    return diagnoses


def _dominant_audit_reason(frame: pd.DataFrame) -> str | None:
    if frame.empty:
        return None
    counts = pd.to_numeric(frame["candidate_count"], errors="coerce")
    failures = frame.loc[counts.fillna(0).le(0)]
    reasons = [
        str(value).strip()
        for value in failures["reason"]
        if pd.notna(value) and str(value).strip()
    ]
    if reasons:
        frequencies = Counter(reasons)
        return min(frequencies, key=lambda reason: (-frequencies[reason], reason))
    statuses = [
        str(value).strip().replace("_", " ").title()
        for value in failures["construction_status"]
        if pd.notna(value) and str(value).strip()
    ]
    if not statuses:
        return None
    frequencies = Counter(statuses)
    return min(frequencies, key=lambda status: (-frequencies[status], status))


def _candidate_views(
    frame: pd.DataFrame,
    *,
    snapshot: PortfolioSnapshot,
) -> tuple[StrategyCandidateView, ...]:
    if frame.empty:
        return ()
    required = {
        "id",
        "symbol",
        "horizon",
        "decision_timestamp",
        "strategy_name",
        "strategy_display_name",
        "legs_json",
        "scenario_coverage_score",
        "raw_profit_probability",
        "calibrated_profit_probability",
        "expected_net_profit",
        "expected_return_on_risk",
        "decision_score",
        "score_basis",
        "candidate_rank",
        "pricing_status",
        "pricing_source",
        "pricing_leg_coverage",
        "pricing_missing_reason",
        "surface_quality_pass",
        "liquidity_policy_pass",
        "all_option_quotes_valid",
        "model_status",
        "model_policy_version",
        "ranking_policy_version",
        "schema_version",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(
            "Options strategy candidates are missing required fields: "
            + ", ".join(missing)
        )
    output: list[StrategyCandidateView] = []
    for (symbol, horizon), route in frame.groupby(
        ["symbol", "horizon"], sort=False, dropna=False
    ):
        if pd.isna(symbol) or pd.isna(horizon):
            raise ValueError("Options strategy candidates contain a null route")
        clean_symbol = str(symbol).strip().upper()
        clean_horizon = str(horizon).strip().lower()
        if not clean_symbol:
            raise ValueError("Options strategy candidates contain a blank symbol")
        if clean_horizon not in HORIZON_LABELS:
            raise ValueError(
                f"Options strategy candidates contain unsupported horizon: {horizon}"
            )
        decision_times = pd.to_datetime(
            route["decision_timestamp"], utc=True, errors="coerce"
        )
        if decision_times.isna().any() or decision_times.nunique() != 1:
            raise ValueError(
                "Options strategy candidates must contain one valid decision per route"
            )
        position = schwab_position_context(
            snapshot.account_facts,
            symbol=clean_symbol,
            observed_at=snapshot.synced_at,
        )
        route_rows: list[dict[str, object]] = []
        for row in route.to_dict("records"):
            if row.get("schema_version") != STRATEGY_CANDIDATE_SCHEMA_VERSION:
                raise ValueError(
                    "Options strategy candidate schema version is incompatible"
                )
            if row.get("ranking_policy_version") != STRATEGY_RANKING_POLICY_VERSION:
                raise ValueError(
                    "Options strategy candidate ranking policy is incompatible"
                )
            if row.get("model_policy_version") not in (
                COMPATIBLE_STRATEGY_MODEL_POLICY_VERSIONS
            ):
                raise ValueError(
                    "Options strategy candidate model policy is incompatible"
                )
            candidate_key = str(row.get("candidate_key") or "").strip()
            if not candidate_key:
                raise ValueError("Options strategy candidate key is blank")
            rank = _required_rank(row.get("candidate_rank"))
            basis_code = str(row.get("score_basis") or "").strip().upper()
            basis_label = _SCORE_BASIS_LABELS.get(basis_code)
            if basis_label is None:
                raise ValueError(
                    f"Options strategy candidate score basis is invalid: {basis_code or 'missing'}"
                )
            model_status = str(row.get("model_status") or "").strip().upper()
            scenario_coverage = _required_probability(
                row.get("scenario_coverage_score"),
                label="Scenario coverage",
            )
            direction_probability_up = _required_probability(
                row.get("direction_probability_up"),
                label="Loop B direction probability",
            )
            fitted_basis = basis_code in {
                BSGP_CALIBRATED_MODEL_SCORE_BASIS,
                BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
                OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
            }
            probability = (
                _required_probability(
                    row.get("decision_score"),
                    label="Calibrated predictive probability",
                )
                if fitted_basis
                else None
            )
            raw_probability = (
                _required_probability(
                    row.get("raw_profit_probability"),
                    label="Raw model probability",
                )
                if fitted_basis
                else None
            )
            backing_probability = (
                _required_probability(
                    row.get("calibrated_profit_probability"),
                    label="Calibrated model probability",
                )
                if fitted_basis
                else None
            )
            expected_status = "MODEL_FIT" if fitted_basis else "HEURISTIC_ONLY"
            if model_status != expected_status:
                raise ValueError(
                    "Options strategy candidate model status does not match score basis"
                )
            pricing_source = str(row.get("pricing_source") or "").strip().upper()
            pricing_status = str(row.get("pricing_status") or "").strip()
            if pricing_status not in {
                "Active",
                "Black-Scholes fallback",
                "Delayed",
                "Unavailable",
            }:
                raise ValueError(
                    "Options strategy candidate pricing status is not user-facing"
                )
            if (
                basis_code == BSGP_CALIBRATED_MODEL_SCORE_BASIS
                and pricing_source != "BSGP"
            ) or (
                basis_code == BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS
                and pricing_source != "BLACK_SCHOLES"
            ) or (
                basis_code == OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS
                and pricing_source in {"BSGP", "BLACK_SCHOLES"}
            ):
                raise ValueError(
                    "Options strategy score basis does not match its pricing source"
                )
            if fitted_basis and not math.isclose(
                float(probability), float(backing_probability), abs_tol=1e-12
            ):
                raise ValueError(
                    "Options strategy predictive score does not match its score basis"
                )
            if (
                basis_code == SCENARIO_COVERAGE_SCORE_BASIS
                and any(
                    _number(row.get(column)) is not None
                    for column in (
                        "decision_score",
                        "raw_profit_probability",
                        "calibrated_profit_probability",
                    )
                )
            ):
                raise ValueError(
                    "Scenario-coverage candidate cannot claim any model probability"
                )
            pricing_coverage = _required_probability(
                row.get("pricing_leg_coverage"),
                label="Pricing leg coverage",
            )
            surface_pass = _required_bool(
                row.get("surface_quality_pass"),
                label="Surface quality policy",
            )
            liquidity_pass = _required_bool(
                row.get("liquidity_policy_pass"),
                label="Liquidity policy",
            )
            quotes_pass = _required_bool(
                row.get("all_option_quotes_valid"),
                label="Option quote validity",
            )
            missing_reason = str(row.get("pricing_missing_reason") or "").strip()
            quality_failures = []
            if not surface_pass:
                quality_failures.append("surface policy failed")
            if not liquidity_pass:
                quality_failures.append("liquidity policy failed")
            if not quotes_pass:
                quality_failures.append("quote validity failed")
            quality_warning = (
                "Quality warning: " + ", ".join(quality_failures)
                if quality_failures
                else "Quality policies passed"
            )
            probability_exclusion = _profit_probability_exclusion_reason(
                row,
                horizon=clean_horizon,
                basis_code=basis_code,
            )
            if probability_exclusion:
                quality_warning = (
                    f"{probability_exclusion} · {quality_warning}"
                )
            source_label = {
                "BSGP": "BSGP",
                "BLACK_SCHOLES": "Black-Scholes",
                "UNAVAILABLE": "Unavailable",
            }.get(pricing_source, pricing_source.title() or "Unavailable")
            pricing_summary = f"{pricing_status} pricing · {source_label}"
            if missing_reason:
                pricing_summary += " · " + _human_reason(missing_reason)
            manual_order_actionable = bool(
                fitted_basis
                and pricing_coverage >= 1.0 - 1e-12
                and pricing_status in {"Active", "Black-Scholes fallback"}
                and not quality_failures
            )
            manual_actionability = (
                "Manual review eligible; submission still requires user confirmation."
                if manual_order_actionable
                else (
                    "Research only; calibrated, fully covered, quality-passing pricing "
                    "evidence is unavailable. Manual broker review is required."
                )
            )
            expected_net_profit = _required_finite(
                row.get("expected_net_profit"),
                label="Expected net profit",
            )
            expected_return = _required_finite(
                row.get("expected_return_on_risk"),
                label="Expected return on risk",
            )
            fit = portfolio_fit(row, position=position)
            route_rows.append(
                {
                    "row": row,
                    "fit": fit,
                    "rank": rank,
                    "candidate_key": candidate_key,
                    "predictive_score": (
                        float(probability) * 100.0
                        if probability is not None
                        else None
                    ),
                    "scenario_coverage": scenario_coverage * 100.0,
                    "direction_probability_up": (
                        direction_probability_up * 100.0
                    ),
                    "score_basis": basis_label,
                    "pricing_summary": pricing_summary,
                    "quality_warning": quality_warning,
                    "manual_order_actionable": manual_order_actionable,
                    "manual_actionability": manual_actionability,
                    "expected_net_profit": expected_net_profit,
                    "expected_return": expected_return,
                }
            )
        ranks = sorted(int(item["rank"]) for item in route_rows)
        if ranks != list(range(1, len(route_rows) + 1)):
            raise ValueError(
                "Options strategy candidate ranks must be complete from 1 through N"
            )
        candidate_keys = [str(item["candidate_key"]) for item in route_rows]
        if len(set(candidate_keys)) != len(candidate_keys):
            raise ValueError("Options strategy candidate keys must be unique per route")
        route_rows.sort(
            key=lambda item: (
                int(item["rank"]),
                str(item["candidate_key"]),
            )
        )
        for item in route_rows:
            row = item["row"]
            draft = build_strategy_order_draft(row, position=position)
            output.append(
                StrategyCandidateView(
                    candidate_id=str(row["id"]),
                    symbol=clean_symbol,
                    horizon=clean_horizon,
                    horizon_label=HORIZON_LABELS[clean_horizon],
                    rank=int(item["rank"]),
                    strategy_name=str(row["strategy_name"]),
                    strategy_display_name=str(row["strategy_display_name"]),
                    exact_legs=_exact_legs(row, position=position),
                    direction_probability_up=float(
                        item["direction_probability_up"]
                    ),
                    predictive_score=(
                        float(item["predictive_score"])
                        if item["predictive_score"] is not None
                        else None
                    ),
                    scenario_coverage=float(item["scenario_coverage"]),
                    expected_net_profit=float(item["expected_net_profit"]),
                    expected_return=float(item["expected_return"]),
                    portfolio_fit=item["fit"],
                    score_basis=str(item["score_basis"]),
                    pricing_summary=str(item["pricing_summary"]),
                    quality_warning=str(item["quality_warning"]),
                    manual_order_actionable=bool(
                        item["manual_order_actionable"]
                    ),
                    manual_actionability=str(item["manual_actionability"]),
                    position=position,
                    order_draft=draft,
                    row=row,
                )
            )
    return tuple(output)


def portfolio_fit(
    candidate: Mapping[str, object],
    *,
    position: SchwabPositionContext,
) -> PortfolioFit:
    requirement = str(candidate.get("stock_requirement") or "NONE").upper()
    cash_requirement = str(candidate.get("cash_requirement") or "").upper()
    shares_needed = _candidate_stock_quantity(candidate)
    labels: list[str] = []
    details: list[str] = []

    if requirement == "EXISTING_100_SHARES":
        if position.shares >= shares_needed > 0.0:
            labels.append("Uses Held Shares")
            details.append(
                f"Uses {shares_needed:g} of the {position.shares:g} "
                f"{position.symbol} shares in the account."
            )
        else:
            labels.append("Share Coverage")
            details.append(
                f"The strategy uses {shares_needed:g} shares; the account "
                f"currently reports {position.shares:g}."
            )
    elif (
        requirement == "EXISTING_OR_ATOMIC_100_SHARES"
        and position.shares >= shares_needed > 0.0
    ):
        labels.append("Protects Held Shares")
        details.append(
            f"Applies protection to {shares_needed:g} of the "
            f"{position.shares:g} shares held."
        )

    needs_atomic_shares = requirement == "BUY_100_SHARES_ATOMICALLY" or (
        requirement == "EXISTING_OR_ATOMIC_100_SHARES"
        and position.shares < shares_needed
    )
    required_funds = _required_funds(
        candidate,
        needs_atomic_shares=needs_atomic_shares,
        cash_requirement=cash_requirement,
    )
    if required_funds is not None:
        uses_non_marginable_funds = bool(
            "STRIKE_TIMES_MULTIPLIER" in cash_requirement
            and position.non_marginable_funds is not None
        )
        applicable_funds = (
            position.non_marginable_funds
            if uses_non_marginable_funds
            else position.available_cash
        )
        balance_name = (
            "non-marginable funds"
            if uses_non_marginable_funds
            else position.available_cash_source.lower()
        )
        if applicable_funds is None:
            labels.append("Funds Not Reported")
            details.append(
                f"The estimated requirement is ${required_funds:,.2f}; "
                "current applicable funds were not reported."
            )
        elif applicable_funds >= required_funds:
            labels.append("Funds Available")
            details.append(
                f"Schwab reports ${applicable_funds:,.2f} in {balance_name} "
                f"available against an estimated ${required_funds:,.2f} "
                "requirement."
            )
        else:
            labels.append("Funds Below Estimate")
            details.append(
                f"Schwab reports ${applicable_funds:,.2f} in {balance_name} "
                f"available against an estimated ${required_funds:,.2f} "
                "requirement."
            )

    if labels:
        if len(labels) == 2 and labels == [
            "Uses Held Shares",
            "Funds Available",
        ]:
            label = "Uses Shares and Funds"
        else:
            label = " · ".join(labels)
        return PortfolioFit(
            label=label,
            detail=" ".join(details),
        )

    net_delta = _number(candidate.get("net_delta")) or 0.0
    if position.shares > 0.0 and net_delta < 0.0:
        return PortfolioFit(
            label="Downside Hedge",
            detail=(
                f"Adds negative delta alongside the {position.shares:g} "
                f"{position.symbol} shares held."
            ),
        )
    if position.shares > 0.0 and net_delta > 0.0:
        return PortfolioFit(
            label="Adds Exposure",
            detail=(
                f"Adds positive delta alongside the {position.shares:g} "
                f"{position.symbol} shares held."
            ),
        )
    if position.shares > 0.0:
        return PortfolioFit(
            label="Balances Exposure",
            detail=(
                f"Has limited directional effect alongside the "
                f"{position.shares:g} {position.symbol} shares held."
            ),
        )
    return PortfolioFit(
        label="Independent of Shares",
        detail="This strategy does not depend on an existing stock position.",
    )


def _exact_legs(
    candidate: Mapping[str, object],
    *,
    position: SchwabPositionContext,
) -> str:
    try:
        legs = json.loads(str(candidate.get("legs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "Exact Legs Unavailable"
    if not isinstance(legs, list):
        return "Exact Legs Unavailable"
    requirement = str(candidate.get("stock_requirement") or "NONE").upper()
    parts: list[str] = []
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        asset = str(leg.get("asset") or "").upper()
        side = str(leg.get("side") or "").upper()
        quantity = _integer(leg.get("quantity")) or 1
        if asset == "STOCK":
            verb = (
                "Use"
                if requirement == "EXISTING_100_SHARES"
                or (
                    requirement == "EXISTING_OR_ATOMIC_100_SHARES"
                    and position.shares >= quantity
                )
                else "Buy"
                if side == "LONG"
                else "Sell"
            )
            parts.append(f"{verb} {quantity:g} shares")
            continue
        strike = _number(leg.get("strike"))
        option_type = str(leg.get("option_type") or "Option").title()
        verb = "Buy" if side == "LONG" else "Sell"
        strike_text = f"${strike:g}" if strike is not None else "option"
        parts.append(f"{verb} {quantity:g} {strike_text} {option_type}")
    return " · ".join(parts) if parts else "Exact Legs Unavailable"


def _candidate_stock_quantity(candidate: Mapping[str, object]) -> float:
    try:
        legs = json.loads(str(candidate.get("legs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.0
    if not isinstance(legs, list):
        return 0.0
    return sum(
        _number(leg.get("quantity")) or 0.0
        for leg in legs
        if isinstance(leg, Mapping)
        and str(leg.get("asset") or "").upper() == "STOCK"
        and str(leg.get("side") or "").upper() == "LONG"
    )


def _required_funds(
    candidate: Mapping[str, object],
    *,
    needs_atomic_shares: bool,
    cash_requirement: str,
) -> float | None:
    estimates: list[float] = []
    if needs_atomic_shares:
        capital = _number(candidate.get("capital_required"))
        if capital is not None and capital > 0.0:
            estimates.append(capital)
    if "STRIKE_TIMES_MULTIPLIER" in cash_requirement:
        try:
            legs = json.loads(str(candidate.get("legs_json") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            legs = []
        put_requirements: list[float] = []
        for leg in (legs if isinstance(legs, list) else []):
            if (
                not isinstance(leg, Mapping)
                or str(leg.get("asset") or "").upper() != "OPTION"
                or str(leg.get("side") or "").upper() != "SHORT"
                or str(leg.get("option_type") or "").upper() != "PUT"
            ):
                continue
            strike = _number(leg.get("strike"))
            multiplier = _number(leg.get("multiplier"))
            quantity = _number(leg.get("quantity"))
            if (
                strike is not None
                and multiplier is not None
                and quantity is not None
            ):
                put_requirements.append(strike * multiplier * quantity)
        estimates.extend(value for value in put_requirements if value > 0.0)
        if not put_requirements:
            capital = _number(candidate.get("capital_required"))
            if capital is not None and capital > 0.0:
                estimates.append(capital)
    return max(estimates) if estimates else None


def _resolve_authoritative_source(path: Path) -> Path:
    candidate = Path(path)
    if (
        candidate.name == "strategy-candidates.parquet"
        and candidate.parent.name in {"latest", "strategy-latest"}
        and candidate.parent.parent.name == "ml"
    ):
        datastore_root = candidate.parents[2]
        if (datastore_root / "ml" / "strategy-latest" / "run.json").is_file():
            return resolve_current_strategy_output(
                datastore_root,
                "strategy-candidates.parquet",
            )
        if (datastore_root / "ml" / "latest" / "run.json").is_file():
            return resolve_current_output(
                datastore_root,
                "strategy-candidates.parquet",
            )
    return candidate


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _profit_probability_exclusion_reason(
    row: Mapping[str, object],
    *,
    horizon: str,
    basis_code: str,
) -> str | None:
    """Explain why a daily/weekly row retained Scenario Coverage only."""

    if (
        horizon in {"1h", "4h"}
        or basis_code != SCENARIO_COVERAGE_SCORE_BASIS
    ):
        return None
    if str(row.get("pricing_mode") or "").strip().upper() != "ACTIVE":
        return "ML probability unavailable: active evidence mode is not enabled"
    pricing_source = str(row.get("pricing_source") or "").strip().upper()
    if pricing_source in {"BSGP", "BLACK_SCHOLES"}:
        return (
            "ML probability unavailable: theoretical Pricing evidence did not "
            "pass its full-coverage quality gate"
        )
    if not _required_bool(
        row.get("all_option_quotes_valid"),
        label="Option quote validity",
    ):
        return "ML probability unavailable: one or more option quotes are invalid"
    spread = _number(row.get("max_relative_spread"))
    if spread is None:
        return "ML probability unavailable: maximum quoted spread is missing"
    if spread > 0.35 + 1e-12:
        return (
            "ML probability unavailable: maximum quoted spread "
            f"{spread * 100.0:.2f}% exceeds the 35.00% OPRA execution gate"
        )
    evidence_lag = _number(row.get("maximum_quote_staleness_seconds"))
    if evidence_lag is None:
        return "ML probability unavailable: option evidence lag is missing"
    if evidence_lag < 0.0 or evidence_lag > 7_200.0 + 1e-12:
        return (
            "ML probability unavailable: option evidence lag "
            f"{max(evidence_lag, 0.0) / 60.0:.1f} minutes exceeds the "
            "120-minute daily/weekly gate"
        )
    open_interest = _number(row.get("minimum_open_interest"))
    volume = _number(row.get("total_volume"))
    if (open_interest is None or open_interest < 1.0) and (
        volume is None or volume < 10.0
    ):
        return (
            "ML probability unavailable: neither minimum open interest nor "
            "total volume passed the OPRA execution gate"
        )
    return "ML probability unavailable: no verified compatible model was applied"


def _human_reason(value: str) -> str:
    text = str(value).strip()
    if ":" in text:
        subject, reason = text.split(":", 1)
        human_reason = " ".join(reason.replace("_", " ").split()).title()
        return f"{subject.strip()}: {human_reason}"
    return " ".join(text.replace("_", " ").split()).title()


def _required_bool(value: object, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    number = _number(value)
    if number in (0.0, 1.0):
        return bool(number)
    raise ValueError(f"{label} must be explicitly true or false")


def _required_probability(value: object, *, label: str) -> float:
    number = _number(value)
    if number is None or not 0.0 <= number <= 1.0:
        raise ValueError(f"{label} must be finite and between 0 and 1")
    return number


def _required_finite(value: object, *, label: str) -> float:
    number = _number(value)
    if number is None:
        raise ValueError(f"{label} must be finite")
    return number


def _required_rank(value: object) -> int:
    number = _number(value)
    if number is None or number < 1.0 or not number.is_integer():
        raise ValueError("Options strategy candidate rank must be a positive integer")
    return int(number)


def _integer(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


__all__ = [
    "HORIZON_LABELS",
    "HORIZON_ORDER",
    "PORTFOLIO_FIT_POLICY_VERSION",
    "PortfolioFit",
    "StrategyCandidateView",
    "StrategyCandidatesView",
    "default_strategy_candidates_path",
    "load_strategy_candidates",
    "portfolio_fit",
]
