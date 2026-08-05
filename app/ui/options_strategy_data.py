from __future__ import annotations

import json
import math
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
    STRATEGY_CANDIDATE_SCHEMA,
    verify_parquet_schema,
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
PORTFOLIO_FIT_POLICY_VERSION = "current-schwab-position-fit-v1"


@dataclass(frozen=True)
class PortfolioFit:
    label: str
    detail: str
    score_adjustment: float
    policy_version: str = PORTFOLIO_FIT_POLICY_VERSION


@dataclass(frozen=True)
class StrategyCandidateView:
    candidate_id: str
    symbol: str
    horizon: str
    horizon_label: str
    rank: int
    market_rank: int | None
    strategy_name: str
    strategy_display_name: str
    exact_legs: str
    raw_probability: float | None
    market_probability: float | None
    expected_net_profit: float | None
    expected_return: float | None
    market_score: float | None
    portfolio_fit: PortfolioFit
    overall_score: float | None
    position: SchwabPositionContext
    order_draft: StrategyOrderDraft
    row: Mapping[str, object]


@dataclass(frozen=True)
class StrategyCandidatesView:
    source_path: Path
    loaded_at: datetime
    candidates: tuple[StrategyCandidateView, ...]
    symbols: tuple[str, ...]
    horizons_by_symbol: Mapping[str, tuple[str, ...]]


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
    symbols = tuple(sorted({item.symbol for item in candidates}))
    horizons_by_symbol = {
        symbol: tuple(
            horizon
            for horizon in HORIZON_ORDER
            if any(
                item.symbol == symbol and item.horizon == horizon
                for item in candidates
            )
        )
        for symbol in symbols
    }
    return StrategyCandidatesView(
        source_path=source,
        loaded_at=loaded_at or datetime.now(timezone.utc),
        candidates=candidates,
        symbols=symbols,
        horizons_by_symbol=horizons_by_symbol,
    )


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
        "strategy_name",
        "strategy_display_name",
        "legs_json",
        "decision_score",
        "candidate_rank",
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
        clean_symbol = str(symbol).strip().upper()
        clean_horizon = str(horizon).strip().lower()
        if not clean_symbol or clean_horizon not in HORIZON_LABELS:
            continue
        position = schwab_position_context(
            snapshot.account_facts,
            symbol=clean_symbol,
            observed_at=snapshot.synced_at,
        )
        route_rows: list[dict[str, object]] = []
        for row in route.to_dict("records"):
            fit = portfolio_fit(row, position=position)
            market_score = _number(row.get("decision_score"))
            raw_probability = _number(row.get("raw_profit_probability"))
            calibrated_probability = _number(
                row.get("calibrated_profit_probability")
            )
            route_rows.append(
                {
                    "row": row,
                    "fit": fit,
                    "market_score": market_score,
                    "raw_probability": raw_probability,
                    "market_probability": (
                        calibrated_probability
                        if calibrated_probability is not None
                        else raw_probability
                    ),
                    "overall_score": (
                        market_score + fit.score_adjustment
                        if market_score is not None
                        else None
                    ),
                }
            )
        route_rows.sort(
            key=lambda item: (
                _descending(item["overall_score"]),
                _descending(item["market_probability"]),
                str(item["row"].get("candidate_key") or ""),
            )
        )
        for rank, item in enumerate(route_rows, start=1):
            row = item["row"]
            draft = build_strategy_order_draft(row, position=position)
            output.append(
                StrategyCandidateView(
                    candidate_id=str(row["id"]),
                    symbol=clean_symbol,
                    horizon=clean_horizon,
                    horizon_label=HORIZON_LABELS[clean_horizon],
                    rank=rank,
                    market_rank=_integer(row.get("candidate_rank")),
                    strategy_name=str(row["strategy_name"]),
                    strategy_display_name=str(row["strategy_display_name"]),
                    exact_legs=_exact_legs(row, position=position),
                    raw_probability=item["raw_probability"],
                    market_probability=item["market_probability"],
                    expected_net_profit=_number(row.get("expected_net_profit")),
                    expected_return=_number(
                        row.get("expected_return_on_risk")
                    ),
                    market_score=item["market_score"],
                    portfolio_fit=item["fit"],
                    overall_score=item["overall_score"],
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
    adjustment = 0.0

    if requirement == "EXISTING_100_SHARES":
        if position.shares >= shares_needed > 0.0:
            labels.append("Uses Held Shares")
            details.append(
                f"Uses {shares_needed:g} of the {position.shares:g} "
                f"{position.symbol} shares in the account."
            )
            adjustment += 0.05
        else:
            coverage = (
                max(min(position.shares / shares_needed, 1.0), 0.0)
                if shares_needed > 0.0
                else 0.0
            )
            labels.append("Share Coverage")
            details.append(
                f"The strategy uses {shares_needed:g} shares; the account "
                f"currently reports {position.shares:g}."
            )
            adjustment -= 0.05 * (1.0 - coverage)
    elif (
        requirement == "EXISTING_OR_ATOMIC_100_SHARES"
        and position.shares >= shares_needed > 0.0
    ):
        labels.append("Protects Held Shares")
        details.append(
            f"Applies protection to {shares_needed:g} of the "
            f"{position.shares:g} shares held."
        )
        adjustment += 0.05

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
            adjustment += 0.03
        else:
            labels.append("Funds Below Estimate")
            details.append(
                f"The account reports ${position.available_cash:,.2f} "
                f"available against an estimated ${required_funds:,.2f} "
                "requirement."
            )
            adjustment -= 0.03

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
            score_adjustment=adjustment,
        )

    net_delta = _number(candidate.get("net_delta")) or 0.0
    if position.shares > 0.0 and net_delta < 0.0:
        hedge_fraction = min(abs(net_delta) / position.shares, 1.0)
        return PortfolioFit(
            label="Downside Hedge",
            detail=(
                f"Adds negative delta alongside the {position.shares:g} "
                f"{position.symbol} shares held."
            ),
            score_adjustment=0.03 * hedge_fraction,
        )
    if position.shares > 0.0 and net_delta > 0.0:
        return PortfolioFit(
            label="Adds Exposure",
            detail=(
                f"Adds positive delta alongside the {position.shares:g} "
                f"{position.symbol} shares held."
            ),
            score_adjustment=0.0,
        )
    if position.shares > 0.0:
        return PortfolioFit(
            label="Balances Exposure",
            detail=(
                f"Has limited directional effect alongside the "
                f"{position.shares:g} {position.symbol} shares held."
            ),
            score_adjustment=0.01,
        )
    return PortfolioFit(
        label="Independent of Shares",
        detail="This strategy does not depend on an existing stock position.",
        score_adjustment=0.0,
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


def _descending(value: object) -> float:
    number = _number(value)
    return -number if number is not None else math.inf


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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
