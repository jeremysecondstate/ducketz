from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Mapping

import numpy as np
import pandas as pd

from ml.strategy_selection.contracts import (
    STRATEGY_CANDIDATE_POLICY_VERSION,
    STRATEGY_OUTCOME_POLICY_VERSION,
    STRATEGY_REGISTRY_VERSION,
    StrategyDefinition,
    StrategySelectionPolicy,
)
from ml.strategy_selection.registry import STRATEGY_REGISTRY


_HORIZON_MAX_RELATIVE_SPREAD = {
    "1h": 0.10,
    "4h": 0.12,
    "1d": 0.20,
    "1w": 0.25,
    "1w-d1": 0.20,
    "1w-d2": 0.20,
    "1w-d3": 0.20,
    "1w-d4": 0.20,
    "1w-d5": 0.20,
}
_HORIZON_MIN_OPEN_INTEREST = {
    "1h": 25.0,
    "4h": 25.0,
    "1d": 10.0,
    "1w": 10.0,
    "1w-d1": 10.0,
    "1w-d2": 10.0,
    "1w-d3": 10.0,
    "1w-d4": 10.0,
    "1w-d5": 10.0,
}


def construct_strategy_candidates(
    sample: Mapping[str, object] | pd.Series,
    contracts: pd.DataFrame,
    *,
    surface: pd.Series,
    stock_quote: pd.Series | None,
    policy: StrategySelectionPolicy,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    symbol = str(sample["symbol"]).strip().upper()
    horizon = str(sample["horizon"]).strip().lower()
    decision = _utc(sample["decision_timestamp"])
    target_start = _utc(sample["target_window_start"])
    target_end = _utc(sample["target_window_end"])
    available_at = _utc(surface["available_at"])
    if available_at >= target_start:
        raise ValueError("Entry option receipt is not available before target entry")

    chain = _prepare_contracts(contracts, horizon=horizon, policy=policy)
    underlying = _single_finite(chain["underlying_price"], "underlying_price")
    expirations = _eligible_expirations(chain, target_end=target_end)
    audit_rows: list[dict[str, object]] = []
    candidate_rows: list[dict[str, object]] = []

    for definition in STRATEGY_REGISTRY.values():
        failures: list[str] = []
        created = 0
        front_choices = expirations[: policy.maximum_expiration_choices]
        if definition.expiration_structure == "MULTI":
            front_choices = tuple(
                expiration
                for expiration in front_choices
                if _next_expiration(expirations, expiration) is not None
            )
        for front in front_choices:
            back = (
                _next_expiration(expirations, front)
                if definition.expiration_structure == "MULTI"
                else None
            )
            for width in policy.candidate_width_steps:
                try:
                    row = _candidate_row(
                        definition,
                        sample=sample,
                        chain=chain,
                        surface=surface,
                        stock_quote=stock_quote,
                        front_expiration=front,
                        back_expiration=back,
                        width_steps=int(width),
                        underlying=underlying,
                        policy=policy,
                    )
                except ValueError as exc:
                    failures.append(str(exc))
                    continue
                candidate_rows.append(row)
                created += 1
        status = (
            "LIFECYCLE_TRACKED"
            if created and definition.lifecycle
            else "CONSTRUCTED"
            if created
            else "NOT_CONSTRUCTIBLE"
        )
        audit_rows.append(
            {
                "symbol": symbol,
                "horizon": horizon,
                "decision_timestamp": decision,
                "strategy_name": definition.name,
                "strategy_display_name": definition.display_name,
                "strategy_family": definition.family,
                "account_approval": policy.account_approval,
                "authorization_status": "AUTHORIZED_SPREADS",
                "construction_status": status,
                "candidate_count": created,
                "reason": _summarize_failures(failures) if not created else definition.notes,
                "registry_version": STRATEGY_REGISTRY_VERSION,
                "candidate_policy_version": STRATEGY_CANDIDATE_POLICY_VERSION,
            }
        )

    candidates = pd.DataFrame(candidate_rows)
    if not candidates.empty:
        candidates = candidates.sort_values(
            ["strategy_name", "front_expiration", "width_steps", "candidate_key"],
            kind="mergesort",
        ).reset_index(drop=True)
    audit = pd.DataFrame(audit_rows).sort_values(
        "strategy_name", kind="mergesort"
    ).reset_index(drop=True)
    return candidates, audit


def evaluate_candidate_outcome(
    candidate: Mapping[str, object] | pd.Series,
    exit_contracts: pd.DataFrame,
    *,
    exit_surface: pd.Series,
    exit_stock_quote: pd.Series | None,
    policy: StrategySelectionPolicy,
) -> dict[str, object]:
    if bool(candidate.get("lifecycle", False)):
        return {
            "outcome_status": "LIFECYCLE_PATH_REQUIRED",
            "outcome_reason": "Stateful strategy requires lifecycle-specific causal labels.",
        }
    legs = json.loads(str(candidate["legs_json"]))
    if not isinstance(legs, list) or not legs:
        raise ValueError("Candidate legs are invalid")
    by_symbol = {
        str(row["contract_symbol"]): row
        for row in exit_contracts.to_dict("records")
    }
    exit_cash = 0.0
    exit_fees = 0.0
    exit_option_quote_flags: list[bool] = []
    exit_stock_quote_flags: list[bool] = []
    for leg in legs:
        side = str(leg["side"])
        quantity = int(leg["quantity"])
        if leg["asset"] == "STOCK":
            if exit_stock_quote is None:
                return {
                    "outcome_status": "MISSING_EXIT_STOCK_QUOTE",
                    "outcome_reason": "Exact causal Schwab stock BBO is required.",
                }
            exit_stock_quote_flags.append(
                _scalar_true(exit_stock_quote.get("quote_quality_pass", False))
            )
            price = float(
                exit_stock_quote["bid"] if side == "LONG" else exit_stock_quote["ask"]
            )
            if not math.isfinite(price) or price < 0.0:
                return {
                    "outcome_status": "INVALID_EXIT_STOCK_BBO",
                    "outcome_reason": "Exit stock BBO is not numerically usable.",
                }
            exit_cash += (1.0 if side == "LONG" else -1.0) * quantity * price
            continue
        contract = by_symbol.get(str(leg["contract_symbol"]))
        if contract is None:
            return {
                "outcome_status": "MISSING_EXIT_CONTRACT",
                "outcome_reason": f"Exit chain omitted {leg['contract_symbol']}.",
            }
        price = float(contract["bid"] if side == "LONG" else contract["ask"])
        if not math.isfinite(price) or price < 0.0:
            return {
                "outcome_status": "INVALID_EXIT_BBO",
                "outcome_reason": (
                    f"Exit BBO is not numerically usable for {leg['contract_symbol']}."
                ),
            }
        exit_option_quote_flags.append(
            _scalar_true(contract.get("quote_valid", False))
        )
        multiplier = float(leg["multiplier"])
        exit_cash += (1.0 if side == "LONG" else -1.0) * quantity * multiplier * price
        exit_fees += quantity * policy.per_contract_fee

    net_profit = float(candidate["entry_cash_flow"]) + exit_cash - exit_fees
    capital = float(candidate["capital_required"])
    return_on_risk = net_profit / capital if capital > 0.0 else np.nan
    return {
        "outcome_status": "COMPLETE",
        "outcome_reason": "",
        "exit_available_at": _utc(exit_surface["available_at"]),
        "exit_cash_flow": exit_cash - exit_fees,
        "net_profit": net_profit,
        "return_on_risk": return_on_risk,
        "profitable": int(net_profit > 0.0),
        "exit_surface_quality_pass": _scalar_true(
            exit_surface.get("surface_quality_pass", False)
        ),
        "exit_all_option_quotes_valid": all(exit_option_quote_flags),
        "exit_stock_quote_quality_pass": (
            all(exit_stock_quote_flags) if exit_stock_quote_flags else None
        ),
        "outcome_policy_version": STRATEGY_OUTCOME_POLICY_VERSION,
    }


def _candidate_row(
    definition: StrategyDefinition,
    *,
    sample: Mapping[str, object] | pd.Series,
    chain: pd.DataFrame,
    surface: pd.Series,
    stock_quote: pd.Series | None,
    front_expiration: pd.Timestamp,
    back_expiration: pd.Timestamp | None,
    width_steps: int,
    underlying: float,
    policy: StrategySelectionPolicy,
) -> dict[str, object]:
    selected_legs: list[dict[str, object]] = []
    option_symbols: set[str] = set()
    for rule in definition.legs:
        if rule.asset == "STOCK":
            if stock_quote is None:
                raise ValueError("exact causal stock BBO unavailable")
            stock_bid = float(stock_quote["bid"])
            stock_ask = float(stock_quote["ask"])
            if (
                not math.isfinite(stock_bid)
                or not math.isfinite(stock_ask)
                or stock_bid < 0.0
                or stock_ask <= 0.0
                or stock_ask < stock_bid
            ):
                raise ValueError("stock BBO is not numerically usable")
            selected_legs.append(
                {
                    "asset": "STOCK",
                    "contract_symbol": str(sample["symbol"]).strip().upper(),
                    "side": rule.side,
                    "quantity": rule.quantity,
                    "bid": stock_bid,
                    "ask": stock_ask,
                    "multiplier": 1.0,
                    "quote_quality_pass": _scalar_true(
                        stock_quote.get("quote_quality_pass", False)
                    ),
                    "available_at": _utc(stock_quote["available_at"]).isoformat(),
                }
            )
            continue
        expiration = (
            front_expiration if rule.expiration_role == "FRONT" else back_expiration
        )
        if expiration is None:
            raise ValueError("back expiration unavailable")
        contract = _select_contract(
            chain,
            option_type=str(rule.option_type),
            expiration=expiration,
            strike_offset=rule.strike_offset,
            width_steps=width_steps,
            underlying=underlying,
        )
        contract_symbol = str(contract["contract_symbol"])
        if contract_symbol in option_symbols:
            raise ValueError("strike grid collapsed two option legs onto one contract")
        option_symbols.add(contract_symbol)
        selected_legs.append(
            {
                "asset": "OPTION",
                "contract_symbol": contract_symbol,
                "side": rule.side,
                "quantity": rule.quantity,
                "option_type": rule.option_type,
                "expiration_role": rule.expiration_role,
                "expiration_date": _utc(contract["expiration_date"]).isoformat(),
                "strike": float(contract["strike"]),
                "bid": float(contract["bid"]),
                "ask": float(contract["ask"]),
                "multiplier": float(contract["multiplier"]),
                "delta": _finite_or_none(contract.get("delta")),
                "gamma": _finite_or_none(contract.get("gamma")),
                "theta": _finite_or_none(contract.get("theta")),
                "vega": _finite_or_none(contract.get("vega")),
                "relative_bid_ask_spread": float(contract["relative_bid_ask_spread"]),
                "open_interest": float(contract["open_interest"]),
                "volume": _finite_or_none(contract.get("volume")),
                "quote_valid": _scalar_true(contract.get("quote_valid", False)),
                "liquidity_policy_pass": _scalar_true(
                    contract.get("__liquidity_policy_pass", False)
                ),
                "quote_staleness_seconds": float(
                    contract["quote_staleness_seconds"]
                ),
                "available_at": _utc(surface["available_at"]).isoformat(),
            }
        )

    entry_cash, entry_fees = _entry_cash_flow(selected_legs, policy=policy)
    risk = _risk_summary(
        selected_legs,
        entry_cash_flow=entry_cash,
        underlying=underlying,
        multi_expiration=definition.expiration_structure == "MULTI",
    )
    option_legs = [leg for leg in selected_legs if leg["asset"] == "OPTION"]
    relative_spreads = [float(leg["relative_bid_ask_spread"]) for leg in option_legs]
    open_interest = [float(leg["open_interest"]) for leg in option_legs]
    volumes = [float(leg["volume"] or 0.0) for leg in option_legs]
    stock_legs = [leg for leg in selected_legs if leg["asset"] == "STOCK"]
    surface_quality_pass = _scalar_true(
        surface.get("surface_quality_pass", False)
    )
    all_option_quotes_valid = all(
        bool(leg["quote_valid"]) for leg in option_legs
    )
    liquidity_policy_pass = all(
        bool(leg["liquidity_policy_pass"]) for leg in option_legs
    )
    stock_quote_quality_pass: bool | None = (
        all(bool(leg["quote_quality_pass"]) for leg in stock_legs)
        if stock_legs
        else None
    )
    quality_observations = {
        "surface_quality_pass": surface_quality_pass,
        "all_option_quotes_valid": all_option_quotes_valid,
        "liquidity_policy_pass": liquidity_policy_pass,
        "stock_quote_quality_pass": stock_quote_quality_pass,
    }
    greeks = {
        name: sum(
            (1.0 if leg["side"] == "LONG" else -1.0)
            * int(leg["quantity"])
            * float(leg["multiplier"])
            * float(leg.get(name) or 0.0)
            for leg in option_legs
        )
        for name in ("delta", "gamma", "theta", "vega")
    }
    stock_delta = sum(
        (1.0 if leg["side"] == "LONG" else -1.0) * int(leg["quantity"])
        for leg in selected_legs
        if leg["asset"] == "STOCK"
    )
    net_delta = greeks["delta"] + stock_delta
    front_text = front_expiration.date().isoformat()
    back_text = back_expiration.date().isoformat() if back_expiration is not None else "none"
    candidate_key = (
        f"{definition.name}|w{width_steps}|front={front_text}|back={back_text}"
    )
    capital = float(risk["capital_required"])
    return {
        "symbol": str(sample["symbol"]).strip().upper(),
        "horizon": str(sample["horizon"]).strip().lower(),
        "decision_timestamp": _utc(sample["decision_timestamp"]),
        "information_available_at": _utc(sample["information_available_at"]),
        "target_window_start": _utc(sample["target_window_start"]),
        "target_window_end": _utc(sample["target_window_end"]),
        "entry_available_at": max(
            _utc(leg["available_at"]) for leg in selected_legs
        ),
        "strategy_name": definition.name,
        "strategy_display_name": definition.display_name,
        "strategy_family": definition.family,
        "candidate_key": candidate_key,
        "account_approval": policy.account_approval,
        "authorization_status": "AUTHORIZED_SPREADS",
        "construction_status": "LIFECYCLE_TRACKED" if definition.lifecycle else "CONSTRUCTED",
        "risk_form": definition.risk_form,
        "expiration_structure": definition.expiration_structure,
        "stock_requirement": definition.stock_requirement,
        "cash_requirement": definition.cash_requirement,
        "lifecycle": definition.lifecycle,
        "front_expiration": front_expiration,
        "back_expiration": back_expiration,
        "front_days_to_expiration": (
            front_expiration.normalize()
            + pd.Timedelta(days=1)
            - _utc(surface["available_at"])
        ).total_seconds()
        / 86_400.0,
        "back_days_to_expiration": (
            (
                back_expiration.normalize()
                + pd.Timedelta(days=1)
                - _utc(surface["available_at"])
            ).total_seconds()
            / 86_400.0
            if back_expiration is not None
            else np.nan
        ),
        "target_elapsed_hours": (
            _utc(sample["target_window_end"])
            - _utc(sample["target_window_start"])
        ).total_seconds()
        / 3_600.0,
        "width_steps": width_steps,
        "leg_count": len(selected_legs),
        "legs_json": json.dumps(selected_legs, sort_keys=True, separators=(",", ":")),
        "underlying_price": underlying,
        "entry_cash_flow": entry_cash,
        "entry_fees": entry_fees,
        "entry_net_credit": max(entry_cash, 0.0),
        "entry_net_debit": max(-entry_cash, 0.0),
        "max_profit": risk["max_profit"],
        "max_loss": risk["max_loss"],
        "capital_required": capital,
        "risk_calculation_status": risk["status"],
        "net_delta": net_delta,
        "net_gamma": greeks["gamma"],
        "net_theta": greeks["theta"],
        "net_vega": greeks["vega"],
        "mean_relative_spread": float(np.mean(relative_spreads)),
        "max_relative_spread": float(np.max(relative_spreads)),
        "minimum_open_interest": float(np.min(open_interest)),
        "total_volume": float(np.sum(volumes)),
        "entry_debit_to_underlying": max(-entry_cash, 0.0) / (underlying * 100.0),
        "max_loss_to_underlying": capital / (underlying * 100.0),
        "net_delta_per_share": net_delta / 100.0,
        "surface_quality_pass": surface_quality_pass,
        "all_option_quotes_valid": all_option_quotes_valid,
        "liquidity_policy_pass": liquidity_policy_pass,
        "stock_quote_quality_pass": stock_quote_quality_pass,
        "maximum_quote_staleness_seconds": float(
            max(float(leg["quote_staleness_seconds"]) for leg in option_legs)
        ),
        "quality_observations_json": json.dumps(
            quality_observations, sort_keys=True, separators=(",", ":")
        ),
        "registry_version": STRATEGY_REGISTRY_VERSION,
        "candidate_policy_version": STRATEGY_CANDIDATE_POLICY_VERSION,
        "outcome_status": "PENDING",
        "outcome_reason": "",
    }


def _prepare_contracts(
    contracts: pd.DataFrame,
    *,
    horizon: str,
    policy: StrategySelectionPolicy,
) -> pd.DataFrame:
    frame = contracts.copy()
    for column in (
        "expiration_date",
        "strike",
        "bid",
        "ask",
        "multiplier",
        "relative_bid_ask_spread",
        "open_interest",
        "quote_staleness_seconds",
    ):
        if column == "expiration_date":
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
        else:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    maximum_spread = min(
        policy.maximum_relative_bid_ask_spread,
        _HORIZON_MAX_RELATIVE_SPREAD[horizon],
    )
    minimum_open_interest = max(
        policy.minimum_open_interest,
        _HORIZON_MIN_OPEN_INTEREST[horizon],
    )
    structural = (
        ~frame["mini"].fillna(True).astype(bool)
        & ~frame["non_standard"].fillna(True).astype(bool)
        & frame["multiplier"].eq(100.0)
        & frame["expiration_date"].notna()
        & frame["strike"].gt(0.0)
        & frame["bid"].ge(0.0)
        & frame["ask"].gt(0.0)
        & frame["ask"].ge(frame["bid"])
        & frame["relative_bid_ask_spread"].ge(0.0)
        & frame["open_interest"].ge(0.0)
        & frame["quote_staleness_seconds"].ge(0.0)
    )
    result = frame.loc[structural].copy()
    if result.empty:
        raise ValueError("No standard contracts contain a numerically usable BBO")
    result["__liquidity_policy_pass"] = (
        result["quote_valid"].map(_scalar_true)
        & result["relative_bid_ask_spread"].le(maximum_spread)
        & result["open_interest"].ge(minimum_open_interest)
        & result["quote_staleness_seconds"].le(
            policy.maximum_quote_staleness_seconds
        )
    )
    return result


def _eligible_expirations(
    chain: pd.DataFrame,
    *,
    target_end: pd.Timestamp,
) -> tuple[pd.Timestamp, ...]:
    expirations = tuple(
        pd.Timestamp(value)
        for value in sorted(chain["expiration_date"].dropna().unique())
        if pd.Timestamp(value).normalize() + pd.Timedelta(days=1) > target_end
    )
    if not expirations:
        raise ValueError("No option expiration survives the target window")
    return expirations


def _next_expiration(
    expirations: tuple[pd.Timestamp, ...],
    front: pd.Timestamp,
) -> pd.Timestamp | None:
    later = [value for value in expirations if value > front]
    return later[0] if later else None


def _select_contract(
    chain: pd.DataFrame,
    *,
    option_type: str,
    expiration: pd.Timestamp,
    strike_offset: int,
    width_steps: int,
    underlying: float,
) -> pd.Series:
    expiry = chain.loc[
        chain["expiration_date"].eq(expiration)
        & chain["call_put"].astype("string").str.upper().eq(option_type)
    ].sort_values("strike", kind="mergesort")
    if expiry.empty:
        raise ValueError(f"missing {option_type} contracts for {expiration.date()}")
    strikes = np.asarray(sorted(expiry["strike"].dropna().unique()), dtype=float)
    atm = int(np.argmin(np.abs(strikes - underlying)))
    location = atm + int(strike_offset) * int(width_steps)
    if location < 0 or location >= len(strikes):
        raise ValueError("insufficient strike depth for strategy geometry")
    strike = float(strikes[location])
    rows = expiry.loc[expiry["strike"].eq(strike)]
    if len(rows) != 1:
        raise ValueError("strategy geometry did not resolve one exact contract")
    return rows.iloc[0]


def _entry_cash_flow(
    legs: list[dict[str, object]],
    *,
    policy: StrategySelectionPolicy,
) -> tuple[float, float]:
    cash = 0.0
    fees = 0.0
    for leg in legs:
        side = str(leg["side"])
        quantity = int(leg["quantity"])
        price = float(leg["ask"] if side == "LONG" else leg["bid"])
        multiplier = float(leg["multiplier"])
        cash += (-1.0 if side == "LONG" else 1.0) * quantity * multiplier * price
        if leg["asset"] == "OPTION":
            fees += quantity * policy.per_contract_fee
    return cash - fees, fees


def _risk_summary(
    legs: list[dict[str, object]],
    *,
    entry_cash_flow: float,
    underlying: float,
    multi_expiration: bool,
) -> dict[str, object]:
    if multi_expiration:
        short_assignment = sum(
            int(leg["quantity"])
            * float(leg["multiplier"])
            * float(leg.get("strike") or underlying)
            for leg in legs
            if leg["asset"] == "OPTION" and leg["side"] == "SHORT"
        )
        debit = max(-entry_cash_flow, 0.0)
        capital = max(debit + short_assignment, 0.01)
        return {
            "max_profit": np.nan,
            "max_loss": capital,
            "capital_required": capital,
            "status": "PATH_DEPENDENT_CONSERVATIVE_ASSIGNMENT_BOUND",
        }

    strikes = sorted(
        float(leg["strike"])
        for leg in legs
        if leg["asset"] == "OPTION"
    )
    upper = max([underlying * 4.0, *(strike * 4.0 for strike in strikes)])
    points = sorted({0.0, underlying, upper, *strikes})
    payoffs = [_terminal_profit(legs, entry_cash_flow, price) for price in points]
    high_slope = sum(
        (1.0 if leg["side"] == "LONG" else -1.0)
        * int(leg["quantity"])
        * (
            float(leg["multiplier"])
            if leg["asset"] == "OPTION" and leg.get("option_type") == "CALL"
            else 1.0
            if leg["asset"] == "STOCK"
            else 0.0
        )
        for leg in legs
    )
    if high_slope < -1e-9:
        raise ValueError("candidate would retain uncovered unlimited call-side risk")
    max_loss = max(0.0, -float(min(payoffs)))
    max_profit = np.nan if high_slope > 1e-9 else max(0.0, float(max(payoffs)))
    capital = max(max_loss, max(-entry_cash_flow, 0.0), 0.01)
    return {
        "max_profit": max_profit,
        "max_loss": max_loss,
        "capital_required": capital,
        "status": "EXPIRATION_PAYOFF_EXACT",
    }


def _terminal_profit(
    legs: list[dict[str, object]],
    entry_cash_flow: float,
    terminal_price: float,
) -> float:
    value = entry_cash_flow
    for leg in legs:
        sign = 1.0 if leg["side"] == "LONG" else -1.0
        quantity = int(leg["quantity"])
        multiplier = float(leg["multiplier"])
        if leg["asset"] == "STOCK":
            value += sign * quantity * terminal_price
        elif leg["option_type"] == "CALL":
            value += sign * quantity * multiplier * max(
                terminal_price - float(leg["strike"]), 0.0
            )
        else:
            value += sign * quantity * multiplier * max(
                float(leg["strike"]) - terminal_price, 0.0
            )
    return value


def _single_finite(values: pd.Series, label: str) -> float:
    finite = pd.to_numeric(values, errors="coerce").dropna().unique()
    if len(finite) != 1 or not math.isfinite(float(finite[0])):
        raise ValueError(f"Candidate receipt requires one finite {label}")
    return float(finite[0])


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scalar_true(value: object) -> bool:
    return value is True or (isinstance(value, np.bool_) and bool(value))


def _summarize_failures(failures: list[str]) -> str:
    if not failures:
        return "No eligible expiration or exact leg geometry was available."
    counts: defaultdict[str, int] = defaultdict(int)
    for failure in failures:
        counts[failure] += 1
    return "; ".join(
        f"{message} ({count})" if count > 1 else message
        for message, count in sorted(counts.items())[:4]
    )


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Strategy candidate timestamp is invalid")
    return pd.Timestamp(timestamp)


__all__ = ["construct_strategy_candidates", "evaluate_candidate_outcome"]
