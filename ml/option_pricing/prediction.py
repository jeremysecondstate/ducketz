from __future__ import annotations

from dataclasses import dataclass
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
    LOOP_NATIVE_SHADOW_SCHEMA_VERSION,
    LoopNativeModelPolicy,
    FINITE_BASIS_RESIDUAL_MODEL_NAME,
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
    ProjectionPolicy,
)
from ml.option_pricing.shadow_model import (
    LoopNativeModelGeneration,
    LoopNativeModelLoad,
    predict_loop_native_residuals,
)


def create_prediction_rows(
    samples: pd.DataFrame,
    *,
    prediction_created_at: object,
    prediction_available_at: object,
    models: Mapping[tuple[str, str], PricingRouteModel] | None = None,
    projection_policy: ProjectionPolicy | None = None,
    fallback_standard_deviation_normalized: float | None = None,
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
    fallback_standard_deviation = (
        LoopNativeModelPolicy().black_scholes_fallback_standard_deviation_normalized
        if fallback_standard_deviation_normalized is None
        else float(fallback_standard_deviation_normalized)
    )
    if not np.isfinite(fallback_standard_deviation) or fallback_standard_deviation <= 0.0:
        raise ValueError("Black-Scholes fallback uncertainty must be finite and positive")
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
            standard_deviation = np.full(len(route), np.nan, dtype=float)
            width80 = np.full(len(route), np.nan, dtype=float)
            width95 = np.full(len(route), np.nan, dtype=float)
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
                    "source_quote_timestamp": row.get("source_quote_timestamp"),
                    "source_evidence_available_at": row.get(
                        "source_evidence_available_at", row["source_available_at"]
                    ),
                    "provider_ingested_at": row.get("provider_ingested_at"),
                    "evidence_lane": row.get("evidence_lane"),
                    "fallback_used": row.get("fallback_used"),
                    "source_quote_staleness_seconds": row.get(
                        "source_quote_staleness_seconds"
                    ),
                    "prediction_created_at": created,
                    "prediction_available_at": available,
                    "model_name": (
                        FINITE_BASIS_RESIDUAL_MODEL_NAME
                        if model is not None
                        else "black_scholes"
                    ),
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
                    "predictive_standard_deviation": float(
                        standard_deviation[position] * underlying
                    ) if has_uncertainty else None,
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


@dataclass
class _LoopNativeRouteView:
    generation: LoopNativeModelGeneration
    policy: LoopNativeModelPolicy

    @property
    def model_version(self) -> str:
        return self.generation.directory.name

    def predict_residual(
        self,
        rows: pd.DataFrame,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        diagnostics = predict_loop_native_residuals(
            self.generation,
            rows,
            policy=self.policy,
        )
        return (
            diagnostics["normalized_residual"].to_numpy(dtype=float),
            diagnostics["predictive_standard_deviation_normalized"].to_numpy(
                dtype=float
            ),
            diagnostics["width_80_normalized"].to_numpy(dtype=float),
            diagnostics["width_95_normalized"].to_numpy(dtype=float),
        )


def create_bsgp_shadow_rows(
    samples: pd.DataFrame,
    baseline_predictions: pd.DataFrame,
    *,
    prediction_created_at: object,
    prediction_available_at: object,
    model_load: LoopNativeModelLoad,
    model_policy: LoopNativeModelPolicy | None = None,
    projection_policy: ProjectionPolicy | None = None,
) -> pd.DataFrame:
    """Build a versioned BSGP sidecar without mutating baseline predictions."""

    if baseline_predictions.empty:
        return pd.DataFrame()
    created = _utc(prediction_created_at, "prediction_created_at")
    available = _utc(prediction_available_at, "prediction_available_at")
    if available < created:
        raise ValueError("Shadow availability cannot precede creation")
    keys = [
        "symbol",
        "target_snapshot_for",
        "call_put",
        "contract_symbol",
        "prediction_created_at",
    ]
    baseline = baseline_predictions.drop(columns="id", errors="ignore").copy()
    if baseline.duplicated(keys).any():
        raise ValueError("Baseline predictions contain duplicate shadow natural keys")
    policy = model_policy or LoopNativeModelPolicy()
    eligible = samples.loc[
        samples["sample_status"].astype("string").eq("AVAILABLE")
    ].copy()
    eligible["prediction_created_at"] = created
    if eligible.duplicated(keys).any():
        raise ValueError("Causal samples contain duplicate shadow natural keys")
    generation = model_load.generation
    if generation is None:
        diagnostics = pd.DataFrame(index=eligible.index)
        diagnostics["normalized_residual"] = 0.0
        fallback_std = policy.black_scholes_fallback_standard_deviation_normalized
        diagnostics["predictive_standard_deviation_normalized"] = fallback_std
        diagnostics["width_80_normalized"] = fallback_std * 1.2815515655446004
        diagnostics["width_95_normalized"] = fallback_std * 1.959963984540054
        diagnostics["status"] = model_load.status
        diagnostics["reason"] = model_load.reason
        diagnostics["support_status"] = "MODEL_UNAVAILABLE"
        diagnostics["support_distance"] = np.nan
        diagnostics["shrinkage"] = 0.0
        diagnostics["route_support_sessions"] = 0
        shadow_priced = baseline.copy()
        shadow_priced["raw_fair_value"] = shadow_priced["black_scholes_price"]
    else:
        diagnostics = predict_loop_native_residuals(
            generation,
            eligible,
            policy=policy,
        )
        adapter = _LoopNativeRouteView(generation, policy)
        route_models = {
            (symbol, call_put): adapter
            for symbol in eligible["symbol"]
            .astype("string")
            .str.strip()
            .str.upper()
            .unique()
            for call_put in eligible["call_put"]
            .astype("string")
            .str.strip()
            .str.upper()
            .unique()
        }
        shadow_priced = create_prediction_rows(
            eligible.drop(columns="prediction_created_at"),
            prediction_created_at=created,
            prediction_available_at=available,
            models=route_models,  # type: ignore[arg-type]
            projection_policy=projection_policy,
        )
    diagnostic_frame = eligible.loc[:, keys].copy()
    diagnostic_frame = pd.concat(
        (diagnostic_frame.reset_index(drop=True), diagnostics.reset_index(drop=True)),
        axis=1,
    )
    merged = baseline.merge(
        shadow_priced[
            [
                *keys,
                "raw_fair_value",
                "constrained_fair_value",
                "predictive_standard_deviation",
                "raw_interval_80_lower",
                "raw_interval_80_upper",
                "raw_interval_95_lower",
                "raw_interval_95_upper",
                "constrained_interval_80_lower",
                "constrained_interval_80_upper",
                "constrained_interval_95_lower",
                "constrained_interval_95_upper",
                "projection_correction",
                "projection_status",
                "raw_bound_violation",
                "raw_monotonicity_violation",
                "raw_convexity_violation",
                "constrained_bound_violation",
                "constrained_monotonicity_violation",
                "constrained_convexity_violation",
            ]
        ],
        on=keys,
        how="left",
        validate="one_to_one",
        suffixes=("_baseline", "_shadow"),
    ).merge(
        diagnostic_frame,
        on=keys,
        how="left",
        validate="one_to_one",
    )
    if len(merged) != len(baseline) or merged["status"].isna().any():
        raise ValueError("Shadow inference did not align with every baseline row")
    for column in (
        "raw_fair_value",
        "constrained_fair_value",
        "predictive_standard_deviation",
        "raw_interval_80_lower",
        "raw_interval_80_upper",
        "raw_interval_95_lower",
        "raw_interval_95_upper",
        "constrained_interval_80_lower",
        "constrained_interval_80_upper",
        "constrained_interval_95_lower",
        "constrained_interval_95_upper",
        "projection_correction",
        "projection_status",
        "raw_bound_violation",
        "raw_monotonicity_violation",
        "raw_convexity_violation",
        "constrained_bound_violation",
        "constrained_monotonicity_violation",
        "constrained_convexity_violation",
    ):
        shadow_column = f"{column}_shadow"
        if shadow_column in merged:
            merged[column] = merged[shadow_column]
    if generation is None:
        # Baseline projection is already authoritative and is copied verbatim.
        merged["constrained_fair_value_shadow"] = merged[
            "constrained_fair_value_baseline"
        ]
        merged["projection_correction"] = 0.0
        merged["projection_status"] = "BASELINE_COPIED"
        for column in (
            "raw_bound_violation",
            "raw_monotonicity_violation",
            "raw_convexity_violation",
            "constrained_bound_violation",
            "constrained_monotonicity_violation",
            "constrained_convexity_violation",
        ):
            baseline_column = f"{column}_baseline"
            if baseline_column in merged:
                merged[column] = merged[baseline_column]
    generation_path = (
        str(generation.receipt.get("run_path", ""))
        if generation is not None
        else ""
    )
    output = pd.DataFrame(
        {
            "symbol": merged["symbol"],
            "source_provider": merged["source_provider"],
            "prediction_mode": merged["prediction_mode"],
            "call_put": merged["call_put"],
            "contract_symbol": merged["contract_symbol"],
            "expiration_date": merged["expiration_date"],
            "target_snapshot_for": merged["target_snapshot_for"],
            "source_snapshot_for": merged["source_snapshot_for"],
            "source_available_at": merged["source_available_at"],
            "prediction_created_at": merged["prediction_created_at"],
            "prediction_available_at": available,
            "underlying_price": merged["underlying_price"],
            "strike": merged["strike"],
            "multiplier": merged["multiplier"],
            "black_scholes_price": merged["black_scholes_price"],
            "baseline_constrained_fair_value": merged[
                "constrained_fair_value_baseline"
            ],
            "bsgp_shadow_fair_value_raw": merged["raw_fair_value"],
            "bsgp_shadow_fair_value_constrained": merged[
                "constrained_fair_value_shadow"
            ],
            "bsgp_shadow_normalized_residual": merged["normalized_residual"],
            "bsgp_shadow_predictive_standard_deviation": merged[
                "predictive_standard_deviation"
            ],
            "bsgp_shadow_raw_interval_80_lower": merged["raw_interval_80_lower"],
            "bsgp_shadow_raw_interval_80_upper": merged["raw_interval_80_upper"],
            "bsgp_shadow_raw_interval_95_lower": merged["raw_interval_95_lower"],
            "bsgp_shadow_raw_interval_95_upper": merged["raw_interval_95_upper"],
            "bsgp_shadow_constrained_interval_80_lower": merged[
                "constrained_interval_80_lower"
            ],
            "bsgp_shadow_constrained_interval_80_upper": merged[
                "constrained_interval_80_upper"
            ],
            "bsgp_shadow_constrained_interval_95_lower": merged[
                "constrained_interval_95_lower"
            ],
            "bsgp_shadow_constrained_interval_95_upper": merged[
                "constrained_interval_95_upper"
            ],
            "bsgp_shadow_status": merged["status"],
            "bsgp_shadow_reason": merged["reason"],
            "bsgp_shadow_model_generation_path": generation_path,
            "bsgp_shadow_model_generation_attestation": (
                generation.generation_hash if generation is not None else ""
            ),
            "bsgp_shadow_model_published_at": (
                generation.published_at if generation is not None else pd.NaT
            ),
            "bsgp_shadow_model_trained_through": (
                generation.trained_through if generation is not None else pd.NaT
            ),
            "bsgp_shadow_model_expires_at": (
                generation.expires_at if generation is not None else pd.NaT
            ),
            "bsgp_shadow_support_status": merged["support_status"],
            "bsgp_shadow_support_distance": merged["support_distance"],
            "bsgp_shadow_shrinkage": merged["shrinkage"],
            "bsgp_shadow_route_support_sessions": merged[
                "route_support_sessions"
            ],
            "bsgp_shadow_input_staleness_seconds": merged[
                "source_quote_staleness_seconds"
            ],
            "bsgp_shadow_projection_correction": merged["projection_correction"],
            "bsgp_shadow_projection_status": merged["projection_status"],
            "bsgp_shadow_raw_bound_violation": merged["raw_bound_violation"],
            "bsgp_shadow_raw_monotonicity_violation": merged[
                "raw_monotonicity_violation"
            ],
            "bsgp_shadow_raw_convexity_violation": merged[
                "raw_convexity_violation"
            ],
            "bsgp_shadow_constrained_bound_violation": merged[
                "constrained_bound_violation"
            ],
            "bsgp_shadow_constrained_monotonicity_violation": merged[
                "constrained_monotonicity_violation"
            ],
            "bsgp_shadow_constrained_convexity_violation": merged[
                "constrained_convexity_violation"
            ],
            "shadow_schema_version": LOOP_NATIVE_SHADOW_SCHEMA_VERSION,
            "automated_action_allowed": False,
        }
    )
    # A projected surface is a coupled object.  If any contract cannot use the
    # shadow model, copying only that row after projection could either move it
    # away from Black-Scholes or break shape constraints.  Fail the complete
    # expiration surface back to its already constrained baseline instead.
    fallback_priority = (
        "BASELINE_FALLBACK_INPUT_UNAVAILABLE",
        "BASELINE_FALLBACK_OUT_OF_SUPPORT",
        "BASELINE_FALLBACK_UNCALIBRATED",
        "BASELINE_FALLBACK_STALE_MODEL",
        "BASELINE_FALLBACK_NO_MODEL",
    )
    surface_columns = (
        "symbol",
        "target_snapshot_for",
        "call_put",
        "expiration_date",
    )
    interval_columns = {
        "bsgp_shadow_predictive_standard_deviation": "predictive_standard_deviation_baseline",
        "bsgp_shadow_raw_interval_80_lower": "raw_interval_80_lower_baseline",
        "bsgp_shadow_raw_interval_80_upper": "raw_interval_80_upper_baseline",
        "bsgp_shadow_raw_interval_95_lower": "raw_interval_95_lower_baseline",
        "bsgp_shadow_raw_interval_95_upper": "raw_interval_95_upper_baseline",
        "bsgp_shadow_constrained_interval_80_lower": "constrained_interval_80_lower_baseline",
        "bsgp_shadow_constrained_interval_80_upper": "constrained_interval_80_upper_baseline",
        "bsgp_shadow_constrained_interval_95_lower": "constrained_interval_95_lower_baseline",
        "bsgp_shadow_constrained_interval_95_upper": "constrained_interval_95_upper_baseline",
    }
    violation_columns = {
        "bsgp_shadow_raw_bound_violation": "raw_bound_violation_baseline",
        "bsgp_shadow_raw_monotonicity_violation": (
            "raw_monotonicity_violation_baseline"
        ),
        "bsgp_shadow_raw_convexity_violation": "raw_convexity_violation_baseline",
        "bsgp_shadow_constrained_bound_violation": (
            "constrained_bound_violation_baseline"
        ),
        "bsgp_shadow_constrained_monotonicity_violation": (
            "constrained_monotonicity_violation_baseline"
        ),
        "bsgp_shadow_constrained_convexity_violation": (
            "constrained_convexity_violation_baseline"
        ),
    }
    for _, surface_index in output.groupby(list(surface_columns), sort=False).groups.items():
        index = list(surface_index)
        observed_statuses = set(output.loc[index, "bsgp_shadow_status"].astype(str))
        if observed_statuses == {"BSGP_SHADOW_READY"}:
            continue
        fallback_status = next(
            (
                status
                for status in fallback_priority
                if status in observed_statuses
            ),
            "BASELINE_FALLBACK_NO_MODEL",
        )
        observed_reasons = sorted(
            {
                str(value).strip()
                for value in output.loc[index, "bsgp_shadow_reason"]
                if str(value).strip()
            }
        )
        output.loc[index, "bsgp_shadow_fair_value_raw"] = output.loc[
            index, "black_scholes_price"
        ].to_numpy()
        output.loc[index, "bsgp_shadow_fair_value_constrained"] = output.loc[
            index, "baseline_constrained_fair_value"
        ].to_numpy()
        output.loc[index, "bsgp_shadow_normalized_residual"] = 0.0
        for output_column, baseline_column in interval_columns.items():
            # Baseline uncertainty is intentionally unavailable. Preserve it
            # as numeric NaN so pandas cannot coerce a float prediction column
            # through an object array of Python ``None`` values.
            output.loc[index, output_column] = pd.to_numeric(
                merged.loc[index, baseline_column], errors="coerce"
            ).to_numpy(dtype=float)
        output.loc[index, "bsgp_shadow_status"] = fallback_status
        output.loc[index, "bsgp_shadow_reason"] = (
            "Complete expiration surface copied from baseline because a coupled "
            "row fell back"
            + (": " + "; ".join(observed_reasons) if observed_reasons else "")
        )
        output.loc[index, "bsgp_shadow_shrinkage"] = 0.0
        output.loc[index, "bsgp_shadow_projection_correction"] = (
            output.loc[index, "baseline_constrained_fair_value"].to_numpy()
            - output.loc[index, "black_scholes_price"].to_numpy()
        )
        output.loc[index, "bsgp_shadow_projection_status"] = (
            "BASELINE_COPIED_SURFACE"
        )
        for output_column, baseline_column in violation_columns.items():
            output.loc[index, output_column] = merged.loc[
                index, baseline_column
            ].to_numpy()
    if not output["black_scholes_price"].reset_index(drop=True).equals(
        baseline["black_scholes_price"].reset_index(drop=True)
    ):
        raise ValueError("Shadow construction changed Black-Scholes values")
    return output.reset_index(drop=True)


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


__all__ = ["create_bsgp_shadow_rows", "create_prediction_rows"]
