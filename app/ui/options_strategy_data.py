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
    PRICING_SCENARIO_FALLBACK_SCORE_BASIS,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
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
    PRICING_SCENARIO_FALLBACK_SCORE_BASIS: "Pricing Scenario",
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
    predictive_score: float
    expected_net_profit: float | None
    expected_return: float | None
    portfolio_fit: PortfolioFit
    score_basis: str
    position: SchwabPositionContext
    order_draft: StrategyOrderDraft
    row: Mapping[str, object]


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
        "raw_profit_probability",
        "calibrated_profit_probability",
        "expected_net_profit",
        "expected_return_on_risk",
        "decision_score",
        "score_basis",
        "candidate_rank",
        "pricing_status",
        "pricing_source",
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
            if row.get("model_policy_version") != STRATEGY_MODEL_POLICY_VERSION:
                raise ValueError(
                    "Options strategy candidate model policy is incompatible"
                )
            candidate_key = str(row.get("candidate_key") or "").strip()
            if not candidate_key:
                raise ValueError("Options strategy candidate key is blank")
            rank = _required_rank(row.get("candidate_rank"))
            probability = _required_probability(
                row.get("decision_score"),
                label="Predictive score",
            )
            basis_code = str(row.get("score_basis") or "").strip().upper()
            basis_label = _SCORE_BASIS_LABELS.get(basis_code)
            if basis_label is None:
                raise ValueError(
                    f"Options strategy candidate score basis is invalid: {basis_code or 'missing'}"
                )
            model_status = str(row.get("model_status") or "").strip().upper()
            raw_probability = _required_probability(
                row.get("raw_profit_probability"),
                label="Raw profit probability",
            )
            backing_probability = (
                _required_probability(
                    row.get("calibrated_profit_probability"),
                    label="Calibrated model probability",
                )
                if basis_code
                in {
                    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
                    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
                }
                else raw_probability
            )
            expected_status = (
                "MODEL_FIT"
                if basis_code
                in {
                    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
                    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
                }
                else "PRICING_SCENARIO"
            )
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
            ):
                raise ValueError(
                    "Options strategy score basis does not match its pricing source"
                )
            if not math.isclose(probability, backing_probability, abs_tol=1e-12):
                raise ValueError(
                    "Options strategy predictive score does not match its score basis"
                )
            if (
                basis_code == PRICING_SCENARIO_FALLBACK_SCORE_BASIS
                and _number(row.get("calibrated_profit_probability")) is not None
            ):
                raise ValueError(
                    "Scenario-prior candidate cannot claim a calibrated probability"
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
                    "predictive_score": probability * 100.0,
                    "score_basis": basis_label,
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
                    predictive_score=float(item["predictive_score"]),
                    expected_net_profit=float(item["expected_net_profit"]),
                    expected_return=float(item["expected_return"]),
                    portfolio_fit=item["fit"],
                    score_basis=str(item["score_basis"]),
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
        if position.available_cash is None:
            labels.append("Funds Not Reported")
            details.append(
                f"The estimated requirement is ${required_funds:,.2f}; "
                "current available funds were not reported."
            )
        elif position.available_cash >= required_funds:
            labels.append("Funds Available")
            details.append(
                f"The account reports ${position.available_cash:,.2f} "
                f"available against an estimated ${required_funds:,.2f} "
                "requirement."
            )
        else:
            labels.append("Funds Below Estimate")
            details.append(
                f"The account reports ${position.available_cash:,.2f} "
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
