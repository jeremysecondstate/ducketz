from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ml.artifacts import file_checksum, file_inventory, utc_timestamp, verify_manifest
from ml.current_publication import authoritative_receipt_runs
from ml.option_pricing.eligibility import EligibilityPolicy, paired_session_inference
from ml.strategy_publication import STRATEGY_PUBLICATION_VERSION
from ml.strategy_selection.candidates import evaluate_candidate_outcome
from ml.strategy_selection.chain import (
    exit_chain_receipt,
    exit_stock_quote,
    load_schwab_chain_history,
)
from ml.strategy_selection.contracts import StrategySelectionPolicy


STRATEGY_OUTCOME_EVIDENCE_VERSION = "option-pricing-strategy-outcome-evidence-v1"
STRATEGY_OUTCOME_RECEIPT_VERSION = "option-pricing-strategy-outcome-receipt-v1"
STRATEGY_OUTCOME_POINTER_VERSION = "option-pricing-strategy-outcome-pointer-v1"

_EXIT_DELAYS = {
    "1h": pd.Timedelta(hours=2),
    "4h": pd.Timedelta(hours=6),
    "1d": pd.Timedelta(days=2),
    "1w": pd.Timedelta(days=4),
    "1w-d1": pd.Timedelta(days=2),
    "1w-d2": pd.Timedelta(days=2),
    "1w-d3": pd.Timedelta(days=2),
    "1w-d4": pd.Timedelta(days=2),
    "1w-d5": pd.Timedelta(days=2),
}

_OBSERVATION_COLUMNS = (
    "strategy_run_path",
    "strategy_published_at",
    "symbol",
    "call_put_routes",
    "horizon",
    "decision_timestamp",
    "target_window_end",
    "candidate_key",
    "candidate_rank",
    "decision_score",
    "legs_checksum_sha256",
    "option_contract_quantity",
    "round_trip_contract_fees_usd",
    "entry_bid_ask_spread_usd",
    "exit_bid_ask_spread_usd",
    "total_bid_ask_spread_usd",
    "scenario_expected_net_profit_usd",
    "bsgp_expected_net_profit_usd",
    "bsgp_uncertainty_usd",
    "realized_net_profit_usd",
    "scenario_absolute_error_usd",
    "bsgp_absolute_error_usd",
    "paired_improvement_usd",
    "uncertainty_contains_outcome",
    "exit_available_at",
    "outcome_policy_version",
    "exact_candidate_cohort",
)


class StrategyOutcomeError(RuntimeError):
    """Strategy outcome evidence failed receipt or causal verification."""


def build_strategy_outcome_evidence(
    datastore_root: Path,
    *,
    evaluated_at: object | None = None,
    policy: EligibilityPolicy | None = None,
) -> tuple[pd.DataFrame, Mapping[str, object], tuple[Path, ...]]:
    """Join verified Strategy candidates to future adverse-BBO outcomes."""

    root = Path(datastore_root).resolve()
    cutoff = utc_timestamp(evaluated_at)
    effective = policy or EligibilityPolicy()
    selection_policy = StrategySelectionPolicy(
        per_contract_fee=effective.per_contract_fee_usd
    )
    observations: list[dict[str, object]] = []
    exclusions: Counter[str] = Counter()
    source_files: list[Path] = []
    histories: dict[str, object] = {}
    seen: set[tuple[str, str, pd.Timestamp, str, str]] = set()
    verified_runs = _verified_strategy_runs(root, available_not_after=cutoff)
    for run, manifest, receipt in verified_runs:
        candidates_path = run / "strategy-candidates.parquet"
        candidates = pd.read_parquet(candidates_path)
        source_files.extend((candidates_path, run / "manifest.json", run / "publication.json"))
        if candidates.empty:
            continue
        for candidate in candidates.to_dict("records"):
            if str(candidate.get("symbol", "")).strip().upper() not in effective.required_symbols:
                exclusions["symbol_not_configured"] += 1
                continue
            if (
                str(candidate.get("pricing_mode", "")).upper() != "SHADOW"
                or str(candidate.get("pricing_status", "")).upper() != "COVERED"
                or _finite(candidate.get("pricing_leg_coverage")) != 1.0
            ):
                exclusions["pricing_diagnostic_not_exactly_covered"] += 1
                continue
            decision = pd.to_datetime(
                candidate.get("decision_timestamp"), utc=True, errors="coerce"
            )
            target_end = pd.to_datetime(
                candidate.get("target_window_end"), utc=True, errors="coerce"
            )
            if pd.isna(decision) or pd.isna(target_end) or target_end <= decision:
                exclusions["invalid_candidate_clocks"] += 1
                continue
            horizon = str(candidate.get("horizon", "")).strip().lower()
            delay = _EXIT_DELAYS.get(horizon)
            if delay is None:
                exclusions["unsupported_horizon"] += 1
                continue
            legs_text = str(candidate.get("legs_json") or "")
            try:
                legs = json.loads(legs_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                exclusions["invalid_legs"] += 1
                continue
            if not isinstance(legs, list) or not legs:
                exclusions["invalid_legs"] += 1
                continue
            legs_hash = hashlib.sha256(legs_text.encode("utf-8")).hexdigest()
            natural_key = (
                str(candidate.get("symbol", "")).strip().upper(),
                horizon,
                pd.Timestamp(decision),
                str(candidate.get("candidate_key", "")),
                legs_hash,
            )
            if natural_key in seen:
                exclusions["duplicate_candidate_receipt"] += 1
                continue
            seen.add(natural_key)
            symbol = natural_key[0]
            try:
                history = histories.get(symbol)
                if history is None:
                    history = load_schwab_chain_history(
                        root,
                        symbol=symbol,
                        available_not_after=cutoff,
                    )
                    histories[symbol] = history
                    source_files.extend(history.source_files)
                exit_receipt = exit_chain_receipt(
                    history,
                    target_window_end=target_end,
                    maximum_delay=delay,
                    strictly_before=cutoff + pd.Timedelta(nanoseconds=1),
                )
                if exit_receipt is None:
                    exclusions["future_option_receipt_unavailable"] += 1
                    continue
                stock_exit = exit_stock_quote(
                    history,
                    target_window_end=target_end,
                    maximum_delay=delay,
                    strictly_before=cutoff + pd.Timedelta(nanoseconds=1),
                )
                outcome = evaluate_candidate_outcome(
                    candidate,
                    exit_receipt.contracts,
                    exit_surface=exit_receipt.surface,
                    exit_stock_quote=stock_exit,
                    policy=selection_policy,
                )
                exit_spread = _exit_bid_ask_spread_cost(
                    legs,
                    exit_contracts=exit_receipt.contracts,
                    exit_stock_quote=stock_exit,
                )
            except Exception:
                exclusions["outcome_materialization_error"] += 1
                continue
            if outcome.get("outcome_status") != "COMPLETE":
                exclusions[str(outcome.get("outcome_status", "incomplete")).lower()] += 1
                continue
            exit_available = pd.to_datetime(
                outcome.get("exit_available_at"), utc=True, errors="coerce"
            )
            if pd.isna(exit_available) or exit_available <= target_end or exit_available <= decision:
                exclusions["non_future_exit_receipt"] += 1
                continue
            scenario = _finite(candidate.get("expected_net_profit"))
            pricing_edge = _finite(candidate.get("pricing_candidate_edge"))
            uncertainty = _finite(candidate.get("pricing_uncertainty"))
            realized = _finite(outcome.get("net_profit"))
            if None in (scenario, pricing_edge, uncertainty, realized):
                exclusions["nonfinite_comparison_value"] += 1
                continue
            try:
                pair = strategy_pair_values(
                    candidate,
                    realized_net_profit_usd=float(realized),
                    per_contract_fee_usd=effective.per_contract_fee_usd,
                )
            except StrategyOutcomeError:
                exclusions["invalid_strategy_pair"] += 1
                continue
            observations.append(
                {
                    "strategy_run_path": run.relative_to(root).as_posix(),
                    "strategy_published_at": receipt["published_at"],
                    "symbol": symbol,
                    "call_put_routes": ",".join(pair["call_put_routes"]),
                    "horizon": horizon,
                    "decision_timestamp": pd.Timestamp(decision),
                    "target_window_end": pd.Timestamp(target_end),
                    "candidate_key": natural_key[3],
                    "candidate_rank": candidate.get("candidate_rank"),
                    "decision_score": candidate.get("decision_score"),
                    "legs_checksum_sha256": legs_hash,
                    "option_contract_quantity": pair["option_contract_quantity"],
                    "round_trip_contract_fees_usd": pair[
                        "round_trip_contract_fees_usd"
                    ],
                    "entry_bid_ask_spread_usd": pair[
                        "entry_bid_ask_spread_usd"
                    ],
                    "exit_bid_ask_spread_usd": exit_spread,
                    "total_bid_ask_spread_usd": (
                        pair["entry_bid_ask_spread_usd"] + exit_spread
                    ),
                    "scenario_expected_net_profit_usd": scenario,
                    "bsgp_expected_net_profit_usd": pair[
                        "bsgp_expected_net_profit_usd"
                    ],
                    "bsgp_uncertainty_usd": uncertainty,
                    "realized_net_profit_usd": realized,
                    "scenario_absolute_error_usd": pair[
                        "scenario_absolute_error_usd"
                    ],
                    "bsgp_absolute_error_usd": pair[
                        "bsgp_absolute_error_usd"
                    ],
                    "paired_improvement_usd": pair["paired_improvement_usd"],
                    "uncertainty_contains_outcome": pair[
                        "uncertainty_contains_outcome"
                    ],
                    "exit_available_at": pd.Timestamp(exit_available),
                    "outcome_policy_version": outcome.get("outcome_policy_version"),
                    "exact_candidate_cohort": True,
                }
            )
    frame = pd.DataFrame(observations, columns=_OBSERVATION_COLUMNS)
    report = compare_strategy_outcomes(
        frame,
        policy=effective,
        exclusions=exclusions,
        verified_strategy_run_count=len(verified_runs),
        evaluated_at=cutoff,
        evidence_kind="REAL_RECEIPT_PROVEN",
    )
    return frame, report, tuple(dict.fromkeys(source_files))


def strategy_pair_values(
    candidate: Mapping[str, object],
    *,
    realized_net_profit_usd: float,
    per_contract_fee_usd: float,
) -> Mapping[str, object]:
    """Create the exact fee-aware paired Strategy comparison values."""

    try:
        legs = json.loads(str(candidate.get("legs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyOutcomeError("Strategy pair has unreadable exact legs") from exc
    if not isinstance(legs, list) or not legs:
        raise StrategyOutcomeError("Strategy pair has no exact legs")
    option_legs = [
        leg
        for leg in legs
        if isinstance(leg, Mapping)
        and str(leg.get("asset", "")).upper() == "OPTION"
    ]
    try:
        option_quantities = [int(leg.get("quantity", 0)) for leg in option_legs]
    except (TypeError, ValueError) as exc:
        raise StrategyOutcomeError("Strategy pair has invalid option quantity") from exc
    option_quantity = sum(option_quantities)
    if (
        option_quantity < 1
        or any(quantity < 1 for quantity in option_quantities)
        or per_contract_fee_usd < 0.0
    ):
        raise StrategyOutcomeError("Strategy pair has invalid fee denominator")
    call_put_routes = tuple(
        sorted(
            {
                str(leg.get("option_type", "")).strip().upper()
                for leg in legs
                if isinstance(leg, Mapping)
                and str(leg.get("asset", "")).upper() == "OPTION"
                and str(leg.get("option_type", "")).strip().upper()
                in {"CALL", "PUT"}
            }
        )
    )
    if not call_put_routes:
        raise StrategyOutcomeError("Strategy pair has no exact call/put route")
    scenario = _finite(candidate.get("expected_net_profit"))
    pricing_edge = _finite(candidate.get("pricing_candidate_edge"))
    uncertainty = _finite(candidate.get("pricing_uncertainty"))
    realized = _finite(realized_net_profit_usd)
    if None in (scenario, pricing_edge, uncertainty, realized):
        raise StrategyOutcomeError("Strategy pair has nonfinite values")
    entry_fees = _finite(candidate.get("entry_fees"))
    expected_entry_fees = option_quantity * per_contract_fee_usd
    if entry_fees is None or not np.isclose(
        entry_fees, expected_entry_fees, rtol=0.0, atol=1e-9
    ):
        raise StrategyOutcomeError("Strategy pair was not built with the configured fee")
    entry_spread = _entry_bid_ask_spread_cost(legs)
    expected_friction = entry_spread + expected_entry_fees
    edge_to_friction = _finite(candidate.get("pricing_edge_to_friction"))
    if (
        edge_to_friction is None
        or expected_friction <= 0.0
        or not np.isclose(
            float(pricing_edge) / expected_friction,
            edge_to_friction,
            rtol=1e-9,
            atol=1e-9,
        )
    ):
        raise StrategyOutcomeError("Strategy Pricing friction does not match its exact legs")
    fees = entry_fees + expected_entry_fees
    bsgp_expected = float(pricing_edge) - fees
    scenario_loss = abs(float(realized) - float(scenario))
    bsgp_loss = abs(float(realized) - bsgp_expected)
    return {
        "option_contract_quantity": option_quantity,
        "call_put_routes": call_put_routes,
        "round_trip_contract_fees_usd": fees,
        "entry_bid_ask_spread_usd": entry_spread,
        "scenario_expected_net_profit_usd": scenario,
        "bsgp_expected_net_profit_usd": bsgp_expected,
        "bsgp_uncertainty_usd": uncertainty,
        "realized_net_profit_usd": realized,
        "scenario_absolute_error_usd": scenario_loss,
        "bsgp_absolute_error_usd": bsgp_loss,
        "paired_improvement_usd": scenario_loss - bsgp_loss,
        "uncertainty_contains_outcome": abs(float(realized) - bsgp_expected)
        <= float(uncertainty),
    }


def compare_strategy_outcomes(
    observations: pd.DataFrame,
    *,
    policy: EligibilityPolicy | None = None,
    exclusions: Mapping[str, int] | None = None,
    verified_strategy_run_count: int = 0,
    evaluated_at: object | None = None,
    evidence_kind: str = "FIXTURE_TEST_ONLY",
) -> Mapping[str, object]:
    """Evaluate paired prediction error on one exact candidate cohort."""

    effective = policy or EligibilityPolicy()
    required = {
        "symbol",
        "call_put_routes",
        "decision_timestamp",
        "paired_improvement_usd",
        "uncertainty_contains_outcome",
        "round_trip_contract_fees_usd",
        "entry_bid_ask_spread_usd",
        "exit_bid_ask_spread_usd",
        "total_bid_ask_spread_usd",
        "exact_candidate_cohort",
    }
    if missing := sorted(required.difference(observations.columns)):
        raise StrategyOutcomeError(
            "Strategy comparison observations are missing: " + ", ".join(missing)
        )
    cohort = observations.loc[
        observations["exact_candidate_cohort"].fillna(False).astype(bool)
    ].copy()
    friction_columns = (
        "round_trip_contract_fees_usd",
        "entry_bid_ask_spread_usd",
        "exit_bid_ask_spread_usd",
        "total_bid_ask_spread_usd",
    )
    friction = cohort.loc[:, friction_columns].apply(pd.to_numeric, errors="coerce")
    complete_friction = bool(
        not cohort.empty
        and np.isfinite(friction.to_numpy(dtype=float)).all()
        and friction.ge(0.0).all().all()
        and np.allclose(
            friction["total_bid_ask_spread_usd"],
            friction["entry_bid_ask_spread_usd"]
            + friction["exit_bid_ask_spread_usd"],
            rtol=0.0,
            atol=1e-9,
        )
    )
    inference = paired_session_inference(
        cohort,
        difference_column="paired_improvement_usd",
        timestamp_column="decision_timestamp",
        confidence_level=effective.confidence_level,
    )
    timestamps = pd.to_datetime(
        cohort["decision_timestamp"], utc=True, errors="coerce"
    )
    sessions = int(
        timestamps.dt.tz_convert("America/New_York").dt.date.nunique()
    ) if timestamps.notna().any() else 0
    coverage = (
        float(cohort["uncertainty_contains_outcome"].astype(bool).mean())
        if len(cohort)
        else None
    )
    lower = _finite(inference.get("lower_confidence_bound"))
    route_reports: dict[str, object] = {}
    for symbol, call_put in effective.required_routes:
        route_name = f"{symbol}/{call_put.lower()}"
        symbol_mask = (
            cohort["symbol"].astype("string").str.upper().eq(symbol).fillna(False)
        ).astype(bool)
        route_membership = (
            cohort["call_put_routes"]
            .astype("string")
            .fillna("")
            .str.upper()
            .str.split(",")
            .map(lambda values: call_put in values)
            .fillna(False)
            .astype(bool)
        )
        route_mask = symbol_mask & route_membership
        route = cohort.loc[route_mask].copy()
        route_friction = route.loc[:, friction_columns].apply(
            pd.to_numeric, errors="coerce"
        )
        route_complete_friction = bool(
            not route.empty
            and np.isfinite(route_friction.to_numpy(dtype=float)).all()
            and route_friction.ge(0.0).all().all()
            and np.allclose(
                route_friction["total_bid_ask_spread_usd"],
                route_friction["entry_bid_ask_spread_usd"]
                + route_friction["exit_bid_ask_spread_usd"],
                rtol=0.0,
                atol=1e-9,
            )
        )
        route_inference = paired_session_inference(
            route,
            difference_column="paired_improvement_usd",
            timestamp_column="decision_timestamp",
            confidence_level=effective.confidence_level,
        )
        route_timestamps = pd.to_datetime(
            route.get("decision_timestamp"), utc=True, errors="coerce"
        )
        route_sessions = int(
            route_timestamps.dt.tz_convert("America/New_York").dt.date.nunique()
        ) if route_timestamps.notna().any() else 0
        route_coverage = (
            float(route["uncertainty_contains_outcome"].astype(bool).mean())
            if len(route)
            else None
        )
        route_lower = _finite(route_inference.get("lower_confidence_bound"))
        route_pass = bool(
            len(route) >= effective.minimum_strategy_pairs
            and route_sessions >= effective.minimum_strategy_sessions
            and route_lower is not None
            and route_lower > effective.minimum_strategy_improvement_usd
            and route_coverage is not None
            and route_complete_friction
            and effective.strategy_uncertainty_coverage_minimum
            <= route_coverage
            <= effective.strategy_uncertainty_coverage_maximum
        )
        route_reports[route_name] = {
            "status": "PASS" if route_pass else "NOT_PROVEN",
            "paired_candidate_count": len(route),
            "distinct_sessions": route_sessions,
            "effect_size_usd": route_inference.get("mean_difference"),
            "lower_confidence_bound_usd": route_lower,
            "uncertainty_coverage": route_coverage,
            "session_values": route_inference.get("session_values", {}),
            "fees_and_spreads_complete": route_complete_friction,
            "total_round_trip_contract_fees_usd": float(
                route_friction["round_trip_contract_fees_usd"].sum()
            ),
            "total_entry_bid_ask_spread_usd": float(
                route_friction["entry_bid_ask_spread_usd"].sum()
            ),
            "total_exit_bid_ask_spread_usd": float(
                route_friction["exit_bid_ask_spread_usd"].sum()
            ),
            "total_bid_ask_spread_usd": float(
                route_friction["total_bid_ask_spread_usd"].sum()
            ),
        }
    passed = bool(
        len(cohort) >= effective.minimum_strategy_pairs
        and sessions >= effective.minimum_strategy_sessions
        and lower is not None
        and lower > effective.minimum_strategy_improvement_usd
        and coverage is not None
        and complete_friction
        and effective.strategy_uncertainty_coverage_minimum
        <= coverage
        <= effective.strategy_uncertainty_coverage_maximum
        and all(
            isinstance(value, Mapping) and value.get("status") == "PASS"
            for value in route_reports.values()
        )
    )
    return {
        "schema_version": STRATEGY_OUTCOME_EVIDENCE_VERSION,
        "status": "PASS" if passed else "NOT_PROVEN",
        "evidence_kind": str(evidence_kind).strip().upper(),
        "evaluated_at": utc_timestamp(evaluated_at).isoformat(),
        "verified_strategy_run_count": verified_strategy_run_count,
        "paired_candidate_count": len(cohort),
        "distinct_sessions": sessions,
        "same_candidate_cohort": True,
        "paired_metric": "scenario_absolute_error_minus_bsgp_absolute_error_usd",
        "effect_size_usd": inference.get("mean_difference"),
        "lower_confidence_bound_usd": lower,
        "confidence_level": effective.confidence_level,
        "statistical_method": inference.get("method"),
        "session_values": inference.get("session_values", {}),
        "routes": route_reports,
        "uncertainty_coverage": coverage,
        "required_uncertainty_coverage": [
            effective.strategy_uncertainty_coverage_minimum,
            effective.strategy_uncertainty_coverage_maximum,
        ],
        "fees_included": True,
        "bid_ask_spread_included": complete_friction,
        "total_round_trip_contract_fees_usd": float(
            pd.to_numeric(
                cohort["round_trip_contract_fees_usd"], errors="coerce"
            ).sum()
        ),
        "total_entry_bid_ask_spread_usd": float(
            pd.to_numeric(cohort["entry_bid_ask_spread_usd"], errors="coerce").sum()
        ),
        "total_exit_bid_ask_spread_usd": float(
            pd.to_numeric(cohort["exit_bid_ask_spread_usd"], errors="coerce").sum()
        ),
        "total_bid_ask_spread_usd": float(
            pd.to_numeric(cohort["total_bid_ask_spread_usd"], errors="coerce").sum()
        ),
        "exclusions": dict(sorted((exclusions or {}).items())),
        "rankings_changed": False,
        "order_construction_changed": False,
        "order_payloads_created": False,
        "automated_action_allowed": False,
    }


def _entry_bid_ask_spread_cost(legs: Sequence[Mapping[str, object]]) -> float:
    total = 0.0
    for leg in legs:
        bid = _finite(leg.get("bid"))
        ask = _finite(leg.get("ask"))
        multiplier = _finite(leg.get("multiplier"))
        try:
            quantity = int(leg.get("quantity", 0))
        except (TypeError, ValueError) as exc:
            raise StrategyOutcomeError("Strategy entry leg quantity is invalid") from exc
        if (
            bid is None
            or ask is None
            or multiplier is None
            or ask < bid
            or multiplier <= 0.0
            or quantity < 1
        ):
            raise StrategyOutcomeError("Strategy entry leg has an invalid executable BBO")
        total += (ask - bid) * quantity * multiplier
    return total


def _exit_bid_ask_spread_cost(
    legs: Sequence[Mapping[str, object]],
    *,
    exit_contracts: pd.DataFrame,
    exit_stock_quote: pd.Series | None,
) -> float:
    total = 0.0
    for leg in legs:
        asset = str(leg.get("asset", "")).upper()
        if asset == "OPTION":
            exact = exit_contracts.loc[
                exit_contracts["contract_symbol"]
                .astype("string")
                .eq(str(leg.get("contract_symbol", "")))
            ]
            if len(exact) != 1:
                raise StrategyOutcomeError("Strategy exit leg is not an exact contract")
            quote: Mapping[str, object] = exact.iloc[0]
        elif asset == "STOCK" and exit_stock_quote is not None:
            quote = exit_stock_quote
        elif asset == "STOCK":
            raise StrategyOutcomeError("Strategy exit stock BBO is missing")
        else:
            raise StrategyOutcomeError("Strategy exit leg asset is invalid")
        bid = _finite(quote.get("bid"))
        ask = _finite(quote.get("ask"))
        multiplier = _finite(leg.get("multiplier"))
        try:
            quantity = int(leg.get("quantity", 0))
        except (TypeError, ValueError) as exc:
            raise StrategyOutcomeError("Strategy exit leg quantity is invalid") from exc
        if (
            bid is None
            or ask is None
            or multiplier is None
            or ask < bid
            or multiplier <= 0.0
            or quantity < 1
        ):
            raise StrategyOutcomeError("Strategy exit leg has an invalid executable BBO")
        total += (ask - bid) * quantity * multiplier
    return total


def publish_strategy_outcome_evidence(
    datastore_root: Path,
    *,
    observations: pd.DataFrame,
    report: Mapping[str, object],
    source_files: Sequence[Path],
    published_at: object | None = None,
    policy: EligibilityPolicy | None = None,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    if report.get("status") == "PASS" and (
        report.get("evidence_kind") != "REAL_RECEIPT_PROVEN"
        or observations.empty
        or int(report.get("verified_strategy_run_count", 0)) < 1
        or not source_files
    ):
        raise StrategyOutcomeError(
            "A passing Strategy report requires real receipt-proven source evidence"
        )
    if report.get("status") == "PASS":
        try:
            rebuilt_observations, rebuilt_report, rebuilt_sources = (
                build_strategy_outcome_evidence(
                    root,
                    evaluated_at=report.get("evaluated_at"),
                    policy=policy,
                )
            )
        except Exception as exc:
            raise StrategyOutcomeError(
                "Passing Strategy report was not reproduced from receipt evidence"
            ) from exc
        try:
            pd.testing.assert_frame_equal(
                observations.reset_index(drop=True),
                rebuilt_observations.reset_index(drop=True),
                check_dtype=True,
                check_like=False,
            )
        except AssertionError as exc:
            raise StrategyOutcomeError(
                "Passing Strategy observations were not reproduced from receipts"
            ) from exc
        if (
            dict(report) != dict(rebuilt_report)
            or {Path(value).resolve() for value in source_files}
            != {Path(value).resolve() for value in rebuilt_sources}
        ):
            raise StrategyOutcomeError(
                "Passing Strategy report was not reproduced from receipt evidence"
            )
    timestamp = utc_timestamp(published_at)
    parent = root / "ml" / "option-pricing-strategy-evidence"
    parent.mkdir(parents=True, exist_ok=True)
    name = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = parent / name
    suffix = 2
    while destination.exists():
        destination = parent / f"{name}-{suffix}"
        suffix += 1
    staging = parent / f".{destination.name}.tmp-{os.getpid()}"
    staging.mkdir()
    try:
        observations_path = staging / "observations.parquet"
        observations.reindex(columns=_OBSERVATION_COLUMNS).to_parquet(
            observations_path, index=False
        )
        report_path = staging / "report.json"
        _write_json(report_path, report)
        receipt = {
            "schema_version": STRATEGY_OUTCOME_RECEIPT_VERSION,
            "run_path": destination.relative_to(root).as_posix(),
            "published_at": timestamp.isoformat(),
            "outputs": file_inventory(
                staging, ("observations.parquet", "report.json")
            ),
            "source_files": _source_inventory(root, source_files),
            "rankings_changed": False,
            "order_construction_changed": False,
            "automated_action_allowed": False,
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        raise
    pointer = {
        "schema_version": STRATEGY_OUTCOME_POINTER_VERSION,
        "current": {
            "run_path": destination.relative_to(root).as_posix(),
            "published_at": timestamp.isoformat(),
            "report_checksum_sha256": file_checksum(destination / "report.json"),
            "receipt_checksum_sha256": file_checksum(destination / "receipt.json"),
        },
    }
    _write_json_atomic(
        root / "ml" / "option-pricing-strategy-latest" / "report.json",
        pointer,
    )
    return read_current_strategy_outcome_evidence(root)


def read_current_strategy_outcome_evidence(
    datastore_root: Path,
) -> Mapping[str, object] | None:
    root = Path(datastore_root).resolve()
    pointer_path = root / "ml" / "option-pricing-strategy-latest" / "report.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyOutcomeError("Strategy outcome pointer is unreadable") from exc
    if not isinstance(pointer, Mapping):
        raise StrategyOutcomeError("Strategy outcome pointer is malformed")
    current = pointer.get("current")
    if (
        pointer.get("schema_version") != STRATEGY_OUTCOME_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise StrategyOutcomeError("Strategy outcome pointer is malformed")
    directory = (root / str(current.get("run_path", ""))).resolve()
    allowed = (root / "ml" / "option-pricing-strategy-evidence").resolve()
    if directory.parent != allowed:
        raise StrategyOutcomeError("Strategy outcome pointer escapes evidence root")
    try:
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyOutcomeError("Strategy outcome artifact is unreadable") from exc
    expected_current = {
        "run_path": directory.relative_to(root).as_posix(),
        "published_at": receipt.get("published_at"),
        "report_checksum_sha256": file_checksum(directory / "report.json"),
        "receipt_checksum_sha256": file_checksum(directory / "receipt.json"),
    }
    if (
        not isinstance(report, Mapping)
        or not isinstance(receipt, Mapping)
        or dict(current) != expected_current
        or receipt.get("schema_version") != STRATEGY_OUTCOME_RECEIPT_VERSION
        or report.get("schema_version") != STRATEGY_OUTCOME_EVIDENCE_VERSION
        or report.get("rankings_changed") is not False
        or report.get("order_construction_changed") is not False
        or report.get("order_payloads_created") is not False
        or report.get("automated_action_allowed") is not False
        or receipt.get("rankings_changed") is not False
        or receipt.get("order_construction_changed") is not False
        or receipt.get("automated_action_allowed") is not False
    ):
        raise StrategyOutcomeError("Strategy outcome receipt verification failed")
    for name, raw in receipt.get("outputs", {}).items():
        metadata = raw if isinstance(raw, Mapping) else {}
        path = directory / str(name)
        if (
            not path.is_file()
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise StrategyOutcomeError(f"Strategy outcome output changed: {path}")
    for raw in receipt.get("source_files", ()):
        if not isinstance(raw, Mapping):
            raise StrategyOutcomeError("Strategy source inventory is malformed")
        path = (root / str(raw.get("path", ""))).resolve()
        if (
            not path.is_file()
            or int(raw.get("size", -1)) != path.stat().st_size
            or raw.get("checksum_sha256") != file_checksum(path)
        ):
            raise StrategyOutcomeError(f"Strategy source evidence changed: {path}")
    return dict(report)


def _verified_strategy_runs(
    root: Path, *, available_not_after: pd.Timestamp
) -> list[tuple[Path, Mapping[str, object], Mapping[str, object]]]:
    loop_b = authoritative_receipt_runs(root)
    verified: list[tuple[Path, Mapping[str, object], Mapping[str, object]]] = []
    for receipt_path in sorted((root / "ml" / "strategy-runs").glob("*/publication.json")):
        run = receipt_path.parent.resolve()
        try:
            manifest = verify_manifest(run)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            configuration = manifest.get("configuration")
            configuration = configuration if isinstance(configuration, Mapping) else {}
            source = configuration.get("source_loop_b")
            source = source if isinstance(source, Mapping) else {}
            source_run = (root / str(source.get("run_path", ""))).resolve()
            published = pd.to_datetime(
                receipt.get("published_at"), utc=True, errors="coerce"
            )
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("schema_version") != STRATEGY_PUBLICATION_VERSION
                or receipt.get("run_path") != run.relative_to(root).as_posix()
                or receipt.get("manifest_checksum_sha256")
                != file_checksum(run / "manifest.json")
                or receipt.get("source_loop_b") != source
                or source_run not in loop_b
                or pd.isna(published)
                or published > available_not_after
                or configuration.get("pricing_mode") != "shadow"
                or not _manifest_inputs_verified(root, manifest)
            ):
                continue
            verified.append((run, manifest, receipt))
        except Exception:
            continue
    return verified


def _manifest_inputs_verified(root: Path, manifest: Mapping[str, object]) -> bool:
    inventory = manifest.get("input_files")
    if not isinstance(inventory, list) or not inventory:
        return False
    for raw in inventory:
        if not isinstance(raw, Mapping):
            return False
        path = (root / str(raw.get("path", ""))).resolve()
        if (
            raw.get("status") != "present"
            or not path.is_file()
            or int(raw.get("size", -1)) != path.stat().st_size
            or raw.get("checksum_sha256") != file_checksum(path)
        ):
            return False
    return True


def _source_inventory(root: Path, paths: Sequence[Path]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in dict.fromkeys(Path(value).resolve() for value in paths):
        if not path.is_file():
            raise StrategyOutcomeError(f"Strategy source disappeared: {path}")
        try:
            rendered = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise StrategyOutcomeError(
                f"Strategy source escapes datastore: {path}"
            ) from exc
        records.append(
            {
                "path": rendered,
                "size": path.stat().st_size,
                "checksum_sha256": file_checksum(path),
            }
        )
    return records


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    _write_json(temporary, payload)
    temporary.replace(path)


__all__ = [
    "STRATEGY_OUTCOME_EVIDENCE_VERSION",
    "StrategyOutcomeError",
    "build_strategy_outcome_evidence",
    "compare_strategy_outcomes",
    "publish_strategy_outcome_evidence",
    "read_current_strategy_outcome_evidence",
    "strategy_pair_values",
]
