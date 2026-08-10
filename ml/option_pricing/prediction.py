from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from ml.option_pricing.constraints import (
    ProjectionError,
    project_prediction_intervals,
    project_surface_values,
    shape_violations,
)
from ml.option_pricing.model import PricingRouteModel
from ml.option_pricing.policies import (
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
    ProjectionPolicy,
)


def create_prediction_rows(
    samples: pd.DataFrame,
    *,
    prediction_created_at: object,
    prediction_available_at: object,
    models: Mapping[tuple[str, str], PricingRouteModel] | None = None,
    projection_policy: ProjectionPolicy | None = None,
) -> pd.DataFrame:
    """Price every available causal row and shape-project each expiration surface."""

    created = _utc(prediction_created_at, "prediction_created_at")
    available = _utc(prediction_available_at, "prediction_available_at")
    if available < created:
        raise ValueError("Prediction availability cannot precede creation")
    required = {
        "symbol",
        "source_provider",
        "prediction_mode",
        "call_put",
        "contract_symbol",
        "expiration_date",
        "target_snapshot_for",
        "source_snapshot_for",
        "source_available_at",
        "source_quote_staleness_seconds",
        "underlying_price",
        "strike",
        "multiplier",
        "risk_free_rate",
        "lagged_implied_volatility",
        "target_years_to_expiration",
        "dividend_yield",
        "black_scholes_price",
        "sample_status",
    }
    if missing := sorted(required.difference(samples.columns)):
        raise ValueError("Causal pricing samples are missing: " + ", ".join(missing))
    eligible = samples.loc[samples["sample_status"].eq("AVAILABLE")].copy()
    if eligible.empty:
        return pd.DataFrame()
    eligible["target_snapshot_for"] = pd.to_datetime(
        eligible["target_snapshot_for"], utc=True, errors="coerce"
    )
    if eligible["target_snapshot_for"].isna().any():
        raise ValueError("Causal pricing samples contain invalid targets")
    live = eligible["prediction_mode"].astype("string").str.upper().eq("LIVE")
    if eligible.loc[live, "target_snapshot_for"].ge(created).any():
        raise ValueError("A live prediction requires a completed target bar")
    route_models = models or {}
    records: list[dict[str, object]] = []
    for (symbol, call_put), route in eligible.groupby(
        [
            eligible["symbol"].astype("string").str.strip().str.upper(),
            eligible["call_put"].astype("string").str.strip().str.upper(),
        ],
        sort=False,
    ):
        model = route_models.get((str(symbol), str(call_put)))
        if model is None:
            residual_mean = np.zeros(len(route), dtype=float)
            standard_deviation = width80 = width95 = np.full(
                len(route), np.nan, dtype=float
            )
        else:
            residual_mean, standard_deviation, width80, width95 = (
                model.predict_residual(route)
            )
        for position, row in enumerate(route.to_dict("records")):
            underlying = float(row["underlying_price"])
            black_scholes = float(row["black_scholes_price"])
            raw_fair = black_scholes + residual_mean[position] * underlying
            # The sample's Black-Scholes value was computed from these exact
            # causal inputs. Reuse it for the American lower bound instead of
            # repeating the expensive normal-CDF calculation for every row.
            strike = float(row["strike"])
            intrinsic = max(
                underlying - strike if str(call_put) == "CALL" else strike - underlying,
                0.0,
            )
            upper = underlying if str(call_put) == "CALL" else strike
            lower = max(intrinsic, black_scholes)
            if lower > upper + 1e-9:
                raise ValueError("Configured American option bounds are inconsistent")
            lower = min(lower, upper)
            has_uncertainty = model is not None
            records.append(
                {
                    "symbol": str(symbol),
                    "source_provider": row["source_provider"],
                    "prediction_mode": str(row["prediction_mode"]).upper(),
                    "call_put": str(call_put),
                    "contract_symbol": row["contract_symbol"],
                    "expiration_date": row["expiration_date"],
                    "target_snapshot_for": row["target_snapshot_for"],
                    "source_snapshot_for": row["source_snapshot_for"],
                    "source_available_at": row["source_available_at"],
                    "source_quote_staleness_seconds": row.get(
                        "source_quote_staleness_seconds"
                    ),
                    "prediction_created_at": created,
                    "prediction_available_at": available,
                    "model_name": "bsgp" if model is not None else "black_scholes",
                    "model_version": (
                        model.model_version if model is not None else OPTION_PRICING_POLICY_VERSION
                    ),
                    "model_status": "MODEL_FIT" if model is not None else "BASELINE_ONLY",
                    "underlying_price": underlying,
                    "strike": strike,
                    "multiplier": row["multiplier"],
                    "risk_free_rate": row["risk_free_rate"],
                    "lagged_implied_volatility": row["lagged_implied_volatility"],
                    "target_years_to_expiration": row["target_years_to_expiration"],
                    "dividend_yield": row["dividend_yield"],
                    "black_scholes_price": black_scholes,
                    "predicted_normalized_residual": float(residual_mean[position]),
                    "raw_fair_value": raw_fair,
                    "point_lower_bound": lower,
                    "point_upper_bound": upper,
                    "predictive_standard_deviation": (
                        float(standard_deviation[position] * underlying)
                        if has_uncertainty
                        else None
                    ),
                    "raw_interval_80_lower": (
                        float(raw_fair - width80[position] * underlying)
                        if has_uncertainty
                        else None
                    ),
                    "raw_interval_80_upper": (
                        float(raw_fair + width80[position] * underlying)
                        if has_uncertainty
                        else None
                    ),
                    "raw_interval_95_lower": (
                        float(raw_fair - width95[position] * underlying)
                        if has_uncertainty
                        else None
                    ),
                    "raw_interval_95_upper": (
                        float(raw_fair + width95[position] * underlying)
                        if has_uncertainty
                        else None
                    ),
                    "pricing_policy_version": OPTION_PRICING_POLICY_VERSION,
                    "timing_policy_version": OPTION_PRICING_TIMING_POLICY_VERSION,
                    "schema_version": OPTION_PRICING_SCHEMA_VERSION,
                    "automated_action_allowed": False,
                }
            )
    predictions = pd.DataFrame(records)
    predictions["_row_order"] = np.arange(len(predictions), dtype=int)
    projected: list[pd.DataFrame] = []
    group_columns = (
        "symbol",
        "target_snapshot_for",
        "call_put",
        "expiration_date",
    )
    for _key, surface in predictions.groupby(list(group_columns), sort=False):
        projected.append(
            _project_surface(
                surface,
                policy=projection_policy or ProjectionPolicy(),
            )
        )
    return (
        pd.concat(projected, ignore_index=True, sort=False)
        .sort_values("_row_order", kind="mergesort")
        .drop(columns="_row_order")
        .reset_index(drop=True)
    )


def _project_surface(
    surface: pd.DataFrame,
    *,
    policy: ProjectionPolicy,
) -> pd.DataFrame:
    output = surface.sort_values(
        ["strike", "contract_symbol"], kind="mergesort"
    ).copy()
    strikes = pd.to_numeric(output["strike"], errors="coerce").to_numpy(dtype=float)
    raw = pd.to_numeric(output["raw_fair_value"], errors="coerce").to_numpy(dtype=float)
    lower = pd.to_numeric(output["point_lower_bound"], errors="coerce").to_numpy(dtype=float)
    upper = pd.to_numeric(output["point_upper_bound"], errors="coerce").to_numpy(dtype=float)
    call_put = str(output["call_put"].iloc[0])
    try:
        if output["predictive_standard_deviation"].notna().all():
            standard_deviation = pd.to_numeric(
                output["predictive_standard_deviation"], errors="coerce"
            ).to_numpy(dtype=float)
            weights = 1.0 / np.maximum(np.square(standard_deviation), 1e-12)
            projections = project_prediction_intervals(
                strikes,
                raw,
                pd.to_numeric(output["raw_interval_80_lower"], errors="coerce").to_numpy(dtype=float),
                pd.to_numeric(output["raw_interval_80_upper"], errors="coerce").to_numpy(dtype=float),
                pd.to_numeric(output["raw_interval_95_lower"], errors="coerce").to_numpy(dtype=float),
                pd.to_numeric(output["raw_interval_95_upper"], errors="coerce").to_numpy(dtype=float),
                lower,
                upper,
                call_put,
                weights=weights,
                policy=policy,
            )
            mean = projections["mean"]
            output["constrained_interval_80_lower"] = projections[
                "interval_80_lower"
            ].constrained
            output["constrained_interval_80_upper"] = projections[
                "interval_80_upper"
            ].constrained
            output["constrained_interval_95_lower"] = projections[
                "interval_95_lower"
            ].constrained
            output["constrained_interval_95_upper"] = projections[
                "interval_95_upper"
            ].constrained
        else:
            mean = project_surface_values(
                strikes,
                raw,
                lower,
                upper,
                call_put,
                policy=policy,
            )
            for column in (
                "constrained_interval_80_lower",
                "constrained_interval_80_upper",
                "constrained_interval_95_lower",
                "constrained_interval_95_upper",
            ):
                output[column] = np.nan
        output["constrained_fair_value"] = mean.constrained
        output["raw_bound_violation"] = mean.raw_violations.bound
        output["raw_monotonicity_violation"] = mean.raw_violations.monotonicity
        output["raw_convexity_violation"] = mean.raw_violations.convexity
        output["constrained_bound_violation"] = mean.constrained_violations.bound
        output["constrained_monotonicity_violation"] = mean.constrained_violations.monotonicity
        output["constrained_convexity_violation"] = mean.constrained_violations.convexity
        output["projection_correction"] = mean.correction
        output["projection_status"] = "COMPLETE"
        output["prediction_status"] = "CREATED"
    except (ProjectionError, ValueError) as exc:
        try:
            diagnostics = shape_violations(
                strikes,
                raw,
                lower,
                upper,
                call_put,
                tolerance=policy.tolerance,
            )
            output["raw_bound_violation"] = diagnostics.bound
            output["raw_monotonicity_violation"] = diagnostics.monotonicity
            output["raw_convexity_violation"] = diagnostics.convexity
        except ValueError:
            output["raw_bound_violation"] = True
            output["raw_monotonicity_violation"] = True
            output["raw_convexity_violation"] = True
        for column in (
            "constrained_fair_value",
            "constrained_interval_80_lower",
            "constrained_interval_80_upper",
            "constrained_interval_95_lower",
            "constrained_interval_95_upper",
            "projection_correction",
        ):
            output[column] = np.nan
        output["constrained_bound_violation"] = pd.NA
        output["constrained_monotonicity_violation"] = pd.NA
        output["constrained_convexity_violation"] = pd.NA
        output["projection_status"] = f"FAILED: {type(exc).__name__}: {exc}"
        output["prediction_status"] = "SURFACE_UNAVAILABLE"
    return output


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


__all__ = ["create_prediction_rows"]
