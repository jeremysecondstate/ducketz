from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import utc_timestamp
from ml.option_pricing.policies import (
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
)
from ml.option_pricing.publication import (
    authoritative_option_pricing_runs,
    receipt_proven_prediction_rows,
)


STRATEGY_PRICING_SHADOW_VERSION = "strategy-option-pricing-shadow-v1"
STRATEGY_PRICING_MODES = ("off", "shadow")


@dataclass(frozen=True)
class StrategyPricingShadowResult:
    candidates: pd.DataFrame
    source_files: tuple[Path, ...]
    report: Mapping[str, object]


def attach_strategy_pricing_shadow(
    candidates: pd.DataFrame,
    *,
    datastore_root: Path,
    pricing_mode: str = "off",
    available_not_after: object,
    per_contract_fee: float,
) -> StrategyPricingShadowResult:
    mode = str(pricing_mode).strip().lower()
    if mode not in STRATEGY_PRICING_MODES:
        raise ValueError("pricing_mode must be off or shadow")
    output = candidates.copy()
    if output.empty:
        return StrategyPricingShadowResult(
            output,
            (),
            {
                "schema_version": STRATEGY_PRICING_SHADOW_VERSION,
                "mode": mode,
                "candidate_rows": 0,
                "covered_rows": 0,
                "rankings_changed": False,
                "automated_action_allowed": False,
            },
        )
    if mode == "off":
        output = _unavailable_columns(output, mode="OFF", status="OFF", reason="PRICING_MODE_OFF")
        return StrategyPricingShadowResult(
            output,
            (),
            _shadow_report(output, mode=mode, publication_error=None),
        )

    try:
        run = _latest_reachable_run(
            datastore_root,
            available_not_after=available_not_after,
        )
        path = run / "pricing-predictions.parquet"
        predictions = receipt_proven_prediction_rows(datastore_root)
        cutoff = utc_timestamp(available_not_after)
        predictions = predictions.loc[
            pd.to_datetime(
                predictions.get("prediction_available_at"),
                utc=True,
                errors="coerce",
            ).le(cutoff)
        ].copy()
        if predictions.empty:
            raise FileNotFoundError("Verified Pricing publication has no predictions")
        compatible = (
            predictions["schema_version"].eq(OPTION_PRICING_SCHEMA_VERSION)
            & predictions["pricing_policy_version"].eq(
                OPTION_PRICING_POLICY_VERSION
            )
            & predictions["timing_policy_version"].eq(
                OPTION_PRICING_TIMING_POLICY_VERSION
            )
        )
        if not compatible.all():
            raise ValueError("Verified Pricing prediction policy/schema is incompatible")
        predictions = predictions.loc[
            predictions["prediction_status"].isin(("AVAILABLE", "CREATED"))
            & predictions["projection_status"].eq("COMPLETE")
            & ~predictions["automated_action_allowed"].fillna(True).astype(bool)
        ].copy()
        if predictions.empty:
            raise FileNotFoundError("Verified Pricing publication has no usable shadow predictions")
        for column in (
            "target_snapshot_for",
            "expiration_date",
            "prediction_created_at",
            "prediction_available_at",
        ):
            predictions[column] = pd.to_datetime(predictions[column], utc=True, errors="coerce")
        source_files = (path, run / "manifest.json", run / "publication.json")
    except Exception as exc:
        reason = f"PRICING_EVIDENCE_UNAVAILABLE:{type(exc).__name__}:{exc}"
        output = _unavailable_columns(
            output,
            mode="SHADOW",
            status="EVIDENCE_UNAVAILABLE",
            reason=reason,
        )
        return StrategyPricingShadowResult(
            output,
            (),
            _shadow_report(output, mode=mode, publication_error=reason),
        )

    diagnostics = [
        _candidate_diagnostic(
            candidate,
            predictions=predictions,
            per_contract_fee=per_contract_fee,
        )
        for candidate in output.to_dict("records")
    ]
    diagnostic_frame = pd.DataFrame(diagnostics, index=output.index)
    for column in diagnostic_frame:
        output[column] = diagnostic_frame[column]
    return StrategyPricingShadowResult(
        output,
        source_files,
        _shadow_report(output, mode=mode, publication_error=None),
    )


def _candidate_diagnostic(
    candidate: Mapping[str, object],
    *,
    predictions: pd.DataFrame,
    per_contract_fee: float,
) -> dict[str, object]:
    try:
        legs = json.loads(str(candidate.get("legs_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        legs = []
    option_legs = [leg for leg in legs if isinstance(leg, Mapping) and leg.get("asset") == "OPTION"]
    if not option_legs:
        return _diagnostic(
            status="NO_OPTION_LEGS",
            coverage=0.0,
            reason="CANDIDATE_HAS_NO_OPTION_LEGS",
        )
    matched: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    missing: list[str] = []
    for leg in option_legs:
        prediction, reason = _matching_prediction(
            predictions,
            symbol=str(candidate.get("symbol") or ""),
            leg=leg,
        )
        if prediction is None:
            missing.append(f"{leg.get('contract_symbol')}:{reason}")
        else:
            matched.append((leg, prediction))
    coverage = len(matched) / len(option_legs)
    if missing:
        return _diagnostic(
            status="PARTIAL" if matched else "UNAVAILABLE",
            coverage=coverage,
            reason=";".join(missing),
        )

    edge = 0.0
    friction = 0.0
    uncertainty = 0.0
    uncertainty_available = True
    for leg, prediction in matched:
        fair = float(prediction["constrained_fair_value"])
        bid = float(leg["bid"])
        ask = float(leg["ask"])
        quantity = int(leg["quantity"])
        multiplier = float(leg["multiplier"])
        if str(leg["side"]).upper() == "LONG":
            leg_edge = fair - ask
        else:
            leg_edge = bid - fair
        edge += leg_edge * quantity * multiplier
        friction += (ask - bid) * quantity * multiplier
        friction += per_contract_fee * quantity
        fair_lower = _finite(prediction.get("constrained_interval_95_lower"))
        fair_upper = _finite(prediction.get("constrained_interval_95_upper"))
        if fair_lower is None or fair_upper is None:
            uncertainty_available = False
        else:
            uncertainty += max(abs(fair - fair_lower), abs(fair_upper - fair)) * quantity * multiplier
    scenario = _finite(candidate.get("expected_net_profit"))
    status = "COVERED" if uncertainty_available else "COVERED_BASELINE_NO_INTERVAL"
    return {
        "pricing_mode": "SHADOW",
        "pricing_status": status,
        "pricing_leg_coverage": coverage,
        "pricing_missing_reason": "" if uncertainty_available else "PREDICTIVE_INTERVAL_UNAVAILABLE",
        "pricing_candidate_edge": edge,
        "pricing_edge_to_friction": edge / friction if friction > 0 else None,
        "pricing_uncertainty": uncertainty if uncertainty_available else None,
        "pricing_edge_minus_scenario_expected_profit": (
            edge - scenario if scenario is not None else None
        ),
    }


def _matching_prediction(
    predictions: pd.DataFrame,
    *,
    symbol: str,
    leg: Mapping[str, object],
) -> tuple[Mapping[str, object] | None, str]:
    target = _timestamp(leg.get("target_snapshot_for"))
    quote = _timestamp(leg.get("quote_timestamp"))
    expiration = _timestamp(leg.get("expiration_date"))
    strike = _finite(leg.get("strike"))
    multiplier = _finite(leg.get("multiplier"))
    call_put = str(leg.get("option_type") or "").strip().upper()
    if target is None:
        return None, "TARGET_SNAPSHOT_MISSING"
    if quote is None:
        return None, "LEG_QUOTE_TIMESTAMP_MISSING"
    if expiration is None or strike is None or multiplier is None or call_put not in {"CALL", "PUT"}:
        return None, "SEMANTIC_CONTRACT_INCOMPLETE"
    expiration_values = pd.to_datetime(predictions["expiration_date"], utc=True, errors="coerce")
    strikes = pd.to_numeric(predictions["strike"], errors="coerce")
    multipliers = pd.to_numeric(predictions["multiplier"], errors="coerce")
    matches = predictions.loc[
        predictions["symbol"].astype("string").str.upper().eq(symbol.strip().upper())
        & predictions["target_snapshot_for"].eq(target)
        & predictions["call_put"].astype("string").str.upper().eq(call_put)
        & expiration_values.dt.normalize().eq(expiration.normalize())
        & strikes.sub(strike).abs().le(1e-9)
        & multipliers.sub(multiplier).abs().le(1e-9)
    ].copy()
    if matches.empty:
        return None, "EXACT_SEMANTIC_PREDICTION_MISSING"
    matches = matches.loc[
        matches["prediction_created_at"].lt(quote)
        & matches["prediction_available_at"].lt(quote)
    ].sort_values(
        ["prediction_available_at", "prediction_created_at"], kind="stable"
    )
    if matches.empty:
        return None, "PREDICTION_NOT_COMMITTED_BEFORE_QUOTE"
    return matches.iloc[0].to_dict(), ""


def _latest_reachable_run(
    datastore_root: Path,
    *,
    available_not_after: object,
) -> Path:
    cutoff = utc_timestamp(available_not_after)
    reachable = authoritative_option_pricing_runs(datastore_root)
    eligible = [(run, published) for run, published in reachable.items() if published <= cutoff]
    if not eligible:
        raise FileNotFoundError("No reachable Pricing publication existed by Strategy cutoff")
    return max(eligible, key=lambda item: item[1])[0]


def _unavailable_columns(
    frame: pd.DataFrame,
    *,
    mode: str,
    status: str,
    reason: str,
) -> pd.DataFrame:
    output = frame.copy()
    output["pricing_mode"] = mode
    output["pricing_status"] = status
    output["pricing_leg_coverage"] = 0.0
    output["pricing_missing_reason"] = reason
    for column in (
        "pricing_candidate_edge",
        "pricing_edge_to_friction",
        "pricing_uncertainty",
        "pricing_edge_minus_scenario_expected_profit",
    ):
        output[column] = None
    return output


def _diagnostic(*, status: str, coverage: float, reason: str) -> dict[str, object]:
    return {
        "pricing_mode": "SHADOW",
        "pricing_status": status,
        "pricing_leg_coverage": coverage,
        "pricing_missing_reason": reason,
        "pricing_candidate_edge": None,
        "pricing_edge_to_friction": None,
        "pricing_uncertainty": None,
        "pricing_edge_minus_scenario_expected_profit": None,
    }


def _shadow_report(
    frame: pd.DataFrame,
    *,
    mode: str,
    publication_error: str | None,
) -> dict[str, object]:
    covered = frame["pricing_status"].isin(("COVERED", "COVERED_BASELINE_NO_INTERVAL"))
    return {
        "schema_version": STRATEGY_PRICING_SHADOW_VERSION,
        "mode": mode,
        "candidate_rows": len(frame),
        "covered_rows": int(covered.sum()),
        "complete_coverage_fraction": float(covered.mean()) if len(frame) else 0.0,
        "publication_error": publication_error,
        "improves_existing_prior": None,
        "future_observed_outcome_comparison_status": "NOT_PROVEN",
        "rankings_changed": False,
        "order_construction_changed": False,
        "automated_action_allowed": False,
    }


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _timestamp(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


__all__ = [
    "STRATEGY_PRICING_MODES",
    "STRATEGY_PRICING_SHADOW_VERSION",
    "StrategyPricingShadowResult",
    "attach_strategy_pricing_shadow",
]
