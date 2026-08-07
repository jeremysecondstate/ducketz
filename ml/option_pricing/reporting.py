from __future__ import annotations

import json
import math
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ml.artifacts import utc_timestamp
from ml.option_pricing.policies import OPTION_PRICING_POLICY_VERSION


GATE_VERSION = "option-pricing-ten-part-gate-v1"
SURFACE_VERSION = "option-pricing-compact-surface-v1"
EDGE_BUCKETS = (-math.inf, -2.0, -1.0, 0.0, 1.0, 2.0, math.inf)
INTERVAL_80_TOLERANCE = (0.72, 0.88)
INTERVAL_95_TOLERANCE = (0.90, 0.99)
MAX_MEDIAN_NORMALIZED_PROJECTION = 0.0025


def build_pricing_surfaces(
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    *,
    available_at: object,
) -> pd.DataFrame:
    if predictions.empty:
        return pd.DataFrame()
    created = utc_timestamp(available_at)
    rows = predictions.copy()
    rows["expiration_bucket"] = rows["target_years_to_expiration"].map(
        expiration_bucket
    )
    rows["moneyness_bucket"] = [
        moneyness_bucket(str(cp), strike, spot)
        for cp, strike, spot in zip(
            rows["call_put"], rows["strike"], rows["underlying_price"]
        )
    ]
    if not evaluations.empty:
        evaluation_columns = [
            "symbol",
            "target_snapshot_for",
            "contract_symbol",
            "prediction_created_at",
            "observed_normalized_residual",
            "model_edge_in_half_spreads",
            "interval_80_covered",
            "interval_95_covered",
            "bid_ask_spread",
            "observed_quote_staleness_seconds",
            "evaluation_status",
        ]
        available = [name for name in evaluation_columns if name in evaluations]
        rows = rows.merge(
            evaluations.loc[:, available],
            how="left",
            on=[
                "symbol",
                "target_snapshot_for",
                "contract_symbol",
                "prediction_created_at",
            ],
            suffixes=("", "_evaluation"),
            validate="one_to_one",
        )
    group_columns = [
        "symbol",
        "target_snapshot_for",
        "call_put",
        "expiration_bucket",
        "moneyness_bucket",
    ]
    output: list[dict[str, object]] = []
    for key, group in rows.groupby(group_columns, dropna=False, sort=True):
        valid = group["prediction_status"].isin(("AVAILABLE", "CREATED"))
        raw_violations = _any_violation(
            group,
            ("raw_bound_violation", "raw_monotonicity_violation", "raw_convexity_violation"),
        )
        constrained_violations = _any_violation(
            group,
            (
                "constrained_bound_violation",
                "constrained_monotonicity_violation",
                "constrained_convexity_violation",
            ),
        )
        edge = _numeric(group, "model_edge_in_half_spreads")
        spread = _numeric(group, "bid_ask_spread")
        relative_spread = spread / _numeric(group, "underlying_price").replace(0, np.nan)
        matured = group.get(
            "evaluation_status", pd.Series(index=group.index, dtype="string")
        ).isin(("EVALUATED", "COMPLETE"))
        causal_coverage = float(valid.mean()) if len(group) else 0.0
        constrained_rate = _mean_bool(constrained_violations)
        quality = bool(
            causal_coverage >= 0.8
            and (math.isnan(constrained_rate) or constrained_rate == 0.0)
        )
        output.append(
            {
                "symbol": key[0],
                "target_snapshot_for": key[1],
                "available_at": created,
                "call_put": key[2],
                "expiration_bucket": key[3],
                "moneyness_bucket": key[4],
                "source_provider": _one_or_mixed(group, "source_provider"),
                "prediction_mode": _one_or_mixed(group, "prediction_mode"),
                "contract_count": len(group),
                "causal_coverage": causal_coverage,
                "median_normalized_residual": _median(
                    _numeric(group, "observed_normalized_residual")
                ),
                "median_predictive_standard_deviation": _median(
                    _numeric(group, "predictive_standard_deviation")
                ),
                "median_model_edge_in_half_spreads": _median(edge),
                "positive_edge_fraction": _fraction(edge.gt(0), edge.notna()),
                "negative_edge_fraction": _fraction(edge.lt(0), edge.notna()),
                "raw_arbitrage_violation_rate": _mean_bool(raw_violations),
                "constrained_arbitrage_violation_rate": constrained_rate,
                "interval_80_coverage": _mean_bool(
                    group.loc[matured, "interval_80_covered"]
                    if "interval_80_covered" in group
                    else pd.Series(dtype="boolean")
                ),
                "interval_95_coverage": _mean_bool(
                    group.loc[matured, "interval_95_covered"]
                    if "interval_95_covered" in group
                    else pd.Series(dtype="boolean")
                ),
                "median_bid_ask_spread": _median(spread),
                "median_relative_bid_ask_spread": _median(relative_spread),
                "median_quote_staleness_seconds": _median(
                    _numeric(group, "observed_quote_staleness_seconds")
                ),
                "surface_quality_pass": quality,
                "surface_status": "AVAILABLE" if quality else "LIMITED_EVIDENCE",
                "pricing_policy_version": OPTION_PRICING_POLICY_VERSION,
                "schema_version": SURFACE_VERSION,
                "automated_action_allowed": False,
            }
        )
    return pd.DataFrame(output)


def assessment_metrics(evaluations: pd.DataFrame) -> dict[str, object]:
    evaluated = evaluations.loc[
        evaluations.get(
            "evaluation_status", pd.Series(index=evaluations.index)
        ).isin(("EVALUATED", "COMPLETE"))
    ].copy()
    if evaluated.empty:
        return {
            "status": "NOT_PROVEN",
            "snapshot_count": 0,
            "contract_row_count": 0,
            "primary_snapshot_weighted": {},
            "secondary_contract_weighted": {},
            "buckets": {},
            "edge_bucket_monotonic": None,
        }
    evaluated["absolute_error"] = _numeric(evaluated, "dollar_error").abs()
    evaluated["normalized_absolute_error"] = _numeric(
        evaluated, "normalized_absolute_error"
    )
    evaluated["normalized_squared_error"] = _numeric(
        evaluated, "normalized_squared_error"
    )
    snapshot = (
        evaluated.groupby("target_snapshot_for", sort=True)
        .agg(
            normalized_mae=("normalized_absolute_error", "mean"),
            normalized_mse=("normalized_squared_error", "mean"),
            dollar_mae=("absolute_error", "mean"),
            error_in_half_spreads=("error_in_half_spreads", lambda x: x.abs().mean()),
            interval_80_coverage=("interval_80_covered", "mean"),
            interval_95_coverage=("interval_95_covered", "mean"),
            interval_80_width=(
                "constrained_interval_80_upper",
                lambda x: float("nan"),
            ),
        )
        .reset_index()
    )
    # Widths need both columns; calculate before snapshot aggregation.
    evaluated["interval_80_width"] = (
        _numeric(evaluated, "constrained_interval_80_upper")
        - _numeric(evaluated, "constrained_interval_80_lower")
    )
    evaluated["interval_95_width"] = (
        _numeric(evaluated, "constrained_interval_95_upper")
        - _numeric(evaluated, "constrained_interval_95_lower")
    )
    width_snapshot = evaluated.groupby("target_snapshot_for").agg(
        interval_80_width=("interval_80_width", "mean"),
        interval_95_width=("interval_95_width", "mean"),
    )
    snapshot = snapshot.drop(columns="interval_80_width").merge(
        width_snapshot,
        left_on="target_snapshot_for",
        right_index=True,
        validate="one_to_one",
    )
    primary = {
        "normalized_mae": _mean(snapshot["normalized_mae"]),
        "normalized_rmse": math.sqrt(max(0.0, _mean(snapshot["normalized_mse"]))),
        "dollar_mae": _mean(snapshot["dollar_mae"]),
        "absolute_error_in_half_spreads": _mean(snapshot["error_in_half_spreads"]),
        "interval_80_coverage": _mean(snapshot["interval_80_coverage"]),
        "interval_95_coverage": _mean(snapshot["interval_95_coverage"]),
        "average_interval_80_width": _mean(snapshot["interval_80_width"]),
        "average_interval_95_width": _mean(snapshot["interval_95_width"]),
    }
    secondary = {
        "normalized_mae": _mean(evaluated["normalized_absolute_error"]),
        "normalized_rmse": math.sqrt(
            max(0.0, _mean(evaluated["normalized_squared_error"]))
        ),
        "dollar_mae": _mean(evaluated["absolute_error"]),
    }
    buckets: dict[str, object] = {}
    evaluated["moneyness_bucket"] = [
        moneyness_bucket(str(cp), strike, spot)
        for cp, strike, spot in zip(
            evaluated["call_put"], evaluated["strike"], evaluated["underlying_price"]
        )
    ]
    evaluated["expiration_bucket"] = evaluated["target_years_to_expiration"].map(
        expiration_bucket
    )
    evaluated["liquidity_bucket"] = pd.cut(
        _numeric(evaluated, "bid_ask_spread")
        / _numeric(evaluated, "observed_mid").replace(0, np.nan),
        bins=(-math.inf, 0.05, 0.15, math.inf),
        labels=("tight", "medium", "wide"),
    ).astype("string")
    evaluated["volatility_regime"] = pd.cut(
        _numeric(evaluated, "lagged_implied_volatility"),
        bins=(-math.inf, 0.25, 0.50, math.inf),
        labels=("low", "medium", "high"),
    ).astype("string")
    for dimension in (
        "symbol",
        "call_put",
        "moneyness_bucket",
        "expiration_bucket",
        "liquidity_bucket",
        "volatility_regime",
        "source_provider",
    ):
        buckets[dimension] = _bucket_metrics(evaluated, dimension)
    edge_report, monotonic = _edge_bucket_report(evaluated)
    buckets["predicted_edge"] = edge_report
    return {
        "status": "ASSESSED",
        "snapshot_count": int(evaluated["target_snapshot_for"].nunique()),
        "contract_row_count": len(evaluated),
        "primary_snapshot_weighted": primary,
        "secondary_contract_weighted": secondary,
        "buckets": buckets,
        "edge_bucket_monotonic": monotonic,
        "opra_to_schwab_drift": _source_drift(evaluated),
    }


def build_gate_report(
    *,
    evaluations: pd.DataFrame,
    predictions: pd.DataFrame,
    model_reports: Mapping[str, object],
    lineage_verified: bool,
    strategy_shadow_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    metrics = assessment_metrics(evaluations)
    primary = metrics.get("primary_snapshot_weighted", {})
    primary = primary if isinstance(primary, Mapping) else {}
    comparison = _comparison_evidence(model_reports)
    partitions = _partition_evidence(model_reports)
    projection = _projection_evidence(predictions)
    prospective = _prospective_evidence(evaluations)
    strategy = strategy_shadow_report or {}

    coverage80 = _as_float(primary.get("interval_80_coverage"))
    coverage95 = _as_float(primary.get("interval_95_coverage"))
    interval_pass = (
        coverage80 is not None
        and coverage95 is not None
        and INTERVAL_80_TOLERANCE[0] <= coverage80 <= INTERVAL_80_TOLERANCE[1]
        and INTERVAL_95_TOLERANCE[0] <= coverage95 <= INTERVAL_95_TOLERANCE[1]
    )
    gates = [
        _gate(1, "lineage_timing_schema_publication", lineage_verified, "Verified immutable source and publication chain."),
        _gate(2, "chronological_partitions_and_closed_lockbox", partitions.get("pass"), partitions.get("detail")),
        _gate(3, "bsgp_beats_black_scholes", comparison.get("beats_black_scholes"), comparison.get("detail")),
        _gate(4, "bsgp_beats_constant_residual", comparison.get("beats_constant_residual"), comparison.get("detail")),
        _gate(5, "bsgp_beats_standard_gp", comparison.get("beats_standard_gp"), comparison.get("detail")),
        _gate(6, "interval_coverage", interval_pass if coverage80 is not None else None, {"observed_80": coverage80, "observed_95": coverage95, "tolerance_80": INTERVAL_80_TOLERANCE, "tolerance_95": INTERVAL_95_TOLERANCE}),
        _gate(7, "constraints_and_projection", projection.get("pass"), projection),
        _gate(8, "edge_after_spread_and_monotonicity", metrics.get("edge_bucket_monotonic"), {"edge_bucket_monotonic": metrics.get("edge_bucket_monotonic")}),
        _gate(9, "strategy_shadow_improves_prior", strategy.get("improves_existing_prior"), strategy or "No receipt-proven Strategy shadow comparison."),
        _gate(10, "prospective_predictions", prospective.get("pass"), prospective),
    ]
    all_pass = all(gate["status"] == "PASS" for gate in gates)
    return {
        "schema_version": GATE_VERSION,
        "gate_status": "EVIDENCE_COMPLETE_SHADOW_ONLY" if all_pass else "NOT_PRODUCTION_ELIGIBLE",
        "automated_action_allowed": False,
        "closed_lockbox_scored": False,
        "assessment": metrics,
        "gates": gates,
        "limitations": [
            "Fixture performance never counts as real evidence.",
            "The closed 126-cluster lockbox remains unscored.",
            "Passing evidence would still require a separate authorization before automation.",
        ],
    }


def build_monitoring_rows(
    *,
    report: Mapping[str, object],
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    monitored_at: object,
) -> pd.DataFrame:
    timestamp = utc_timestamp(monitored_at)
    gate_rows = report.get("gates", [])
    output: list[dict[str, object]] = []
    for gate in gate_rows if isinstance(gate_rows, Sequence) else []:
        if not isinstance(gate, Mapping):
            continue
        output.append(
            _monitoring_row(
                timestamp,
                category="readiness_gate",
                metric_name=f"gate_{gate.get('number')}_{gate.get('name')}",
                scope_type="global",
                scope_value="all-routes",
                status=str(gate.get("status")),
                observed_value=1.0 if gate.get("status") == "PASS" else 0.0,
                reference_value=1.0,
                unit="boolean",
                evidence_row_count=len(evaluations),
                details=gate.get("evidence"),
                frame=evaluations,
            )
        )
    for symbol, group in predictions.groupby("symbol", sort=True) if not predictions.empty else []:
        available = group["prediction_status"].isin(("AVAILABLE", "CREATED"))
        output.append(
            _monitoring_row(
                timestamp,
                category="causal_coverage",
                metric_name="available_prediction_fraction",
                scope_type="symbol",
                scope_value=str(symbol),
                status="OBSERVED",
                observed_value=float(available.mean()),
                reference_value=1.0,
                unit="fraction",
                evidence_row_count=len(group),
                details={"automated_action_allowed": False},
                frame=group,
            )
        )
    return pd.DataFrame(output)


def expiration_bucket(years: object) -> str:
    value = _as_float(years)
    if value is None:
        return "unknown"
    days = value * 365.0
    if days <= 30:
        return "7-30d"
    if days <= 60:
        return "31-60d"
    if days <= 120:
        return "61-120d"
    return "outside-policy"


def moneyness_bucket(call_put: str, strike: object, spot: object) -> str:
    k, s = _as_float(strike), _as_float(spot)
    if k is None or s is None or k <= 0 or s <= 0:
        return "unknown"
    signed = math.log(k / s) * (
        1.0 if str(call_put).strip().lower() == "call" else -1.0
    )
    if signed < -0.05:
        return "in-the-money"
    if signed > 0.05:
        return "out-of-the-money"
    return "near-the-money"


def _monitoring_row(
    timestamp: pd.Timestamp,
    *,
    category: str,
    metric_name: str,
    scope_type: str,
    scope_value: str,
    status: str,
    observed_value: float,
    reference_value: float,
    unit: str,
    evidence_row_count: int,
    details: object,
    frame: pd.DataFrame,
) -> dict[str, object]:
    times = pd.to_datetime(
        frame.get("target_snapshot_for", pd.Series(dtype="datetime64[ns, UTC]")),
        utc=True,
        errors="coerce",
    )
    return {
        "monitored_at": timestamp,
        "category": category,
        "metric_name": metric_name,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "status": status,
        "observed_value": observed_value,
        "reference_value": reference_value,
        "unit": unit,
        "evidence_row_count": evidence_row_count,
        "window_start": times.min() if times.notna().any() else pd.NaT,
        "window_end": times.max() if times.notna().any() else pd.NaT,
        "details": json.dumps(details, sort_keys=True, default=str),
    }


def _gate(number: int, name: str, passed: object, evidence: object) -> dict[str, object]:
    status = "PASS" if passed is True else "FAIL" if passed is False else "NOT_PROVEN"
    return {"number": number, "name": name, "status": status, "evidence": evidence}


def _comparison_evidence(reports: Mapping[str, object]) -> dict[str, object]:
    values = []
    for report in _route_reports(reports):
        assessment = report.get("assessment_metrics")
        metrics = assessment.get("models") if isinstance(assessment, Mapping) else None
        if not isinstance(metrics, Mapping) or "bsgp" not in metrics:
            continue
        values.append(metrics)
    if not values:
        return {"detail": "No untouched assessment comparator metrics."}
    def beats(name: str) -> bool:
        return all(
            _metric_value(value, "bsgp") < _metric_value(value, name)
            for value in values
        )
    return {
        "beats_black_scholes": beats("black_scholes"),
        "beats_constant_residual": beats("constant_residual"),
        "beats_standard_gp": beats("standard_gp"),
        "detail": f"Comparator evidence across {len(values)} fitted routes.",
    }


def _metric_value(metrics: Mapping[str, object], name: str) -> float:
    value = metrics.get(name)
    if isinstance(value, Mapping):
        raw = value.get("normalized_rmse", value.get("rmse"))
    else:
        raw = value
    parsed = _as_float(raw)
    return parsed if parsed is not None else math.inf


def _partition_evidence(reports: Mapping[str, object]) -> dict[str, object]:
    fitted = []
    for report in _route_reports(reports):
        partition = report.get("partition_contract") or report.get("partitions")
        if isinstance(partition, Mapping):
            fitted.append(partition)
    if not fitted:
        return {"pass": None, "detail": "No fitted route with closed-lockbox metadata."}
    required = {"train": 252, "calibration": 63, "assessment": 63, "lockbox": 126}
    valid = True
    for partition in fitted:
        counts = partition.get("cluster_counts", partition)
        if not isinstance(counts, Mapping):
            valid = False
            continue
        for name, minimum in required.items():
            observed = _as_float(counts.get(name) or counts.get(f"{name}_clusters"))
            valid &= observed is not None and observed >= minimum
        valid &= partition.get("lockbox_status", "CLOSED_UNTOUCHED_UNSCORED") == "CLOSED_UNTOUCHED_UNSCORED"
        span = _as_float(partition.get("calendar_span_months"))
        valid &= span is not None and span >= 6
    return {"pass": bool(valid), "detail": f"Partition metadata for {len(fitted)} routes; lockbox target values were not read."}


def _projection_evidence(predictions: pd.DataFrame) -> dict[str, object]:
    available = predictions.loc[
        predictions.get(
            "prediction_status", pd.Series(index=predictions.index)
        ).isin(("AVAILABLE", "CREATED"))
    ]
    if available.empty:
        return {"pass": None, "detail": "No available constrained predictions."}
    violations = _any_violation(
        available,
        ("constrained_bound_violation", "constrained_monotonicity_violation", "constrained_convexity_violation"),
    )
    correction = _numeric(available, "projection_correction").abs()
    normalized = correction / _numeric(available, "underlying_price").replace(0, np.nan)
    median = _median(normalized)
    passed = _mean_bool(violations) == 0.0 and not math.isnan(median) and median <= MAX_MEDIAN_NORMALIZED_PROJECTION
    return {
        "pass": passed,
        "constrained_violation_rate": _mean_bool(violations),
        "median_normalized_projection": median,
        "tolerance": MAX_MEDIAN_NORMALIZED_PROJECTION,
    }


def _prospective_evidence(evaluations: pd.DataFrame) -> dict[str, object]:
    valid = evaluations.loc[evaluations.get("prospective_eligible", pd.Series(index=evaluations.index)).fillna(False).astype(bool)].copy()
    if valid.empty:
        return {"pass": None, "detail": "No receipt-proven prospective completed predictions."}
    valid = valid.sort_values("prediction_created_at", kind="stable").drop_duplicates(
        ["symbol", "target_snapshot_for", "contract_symbol"], keep="first"
    )
    valid["session"] = pd.to_datetime(valid["target_snapshot_for"], utc=True).dt.tz_convert("America/New_York").dt.date
    rows: dict[str, object] = {}
    passed = True
    for symbol, group in valid.groupby("symbol"):
        evidence = {
            "completed_predictions": len(group),
            "distinct_sessions": int(group["session"].nunique()),
        }
        evidence["pass"] = evidence["completed_predictions"] >= 60 and evidence["distinct_sessions"] >= 20
        passed &= bool(evidence["pass"])
        rows[str(symbol)] = evidence
    return {"pass": passed, "symbols": rows}


def _route_reports(reports: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = reports.get("model_reports", reports)
    if not isinstance(raw, Mapping):
        return []
    return [value for value in raw.values() if isinstance(value, Mapping)]


def _bucket_metrics(frame: pd.DataFrame, column: str) -> dict[str, object]:
    output: dict[str, object] = {}
    for value, group in frame.groupby(column, dropna=False, sort=True):
        output[str(value)] = {
            "rows": len(group),
            "normalized_mae": _mean(_numeric(group, "normalized_absolute_error")),
            "normalized_rmse": math.sqrt(max(0.0, _mean(_numeric(group, "normalized_squared_error")))),
            "dollar_mae": _mean(_numeric(group, "dollar_error").abs()),
        }
    return output


def _edge_bucket_report(frame: pd.DataFrame) -> tuple[dict[str, object], bool | None]:
    edge = _numeric(frame, "model_edge_in_half_spreads")
    if edge.notna().sum() < 2:
        return {}, None
    bins = pd.cut(edge, bins=EDGE_BUCKETS, include_lowest=True)
    rows: dict[str, object] = {}
    ordered_errors: list[float] = []
    for value, group in frame.groupby(bins, observed=True, sort=True):
        realized = -_numeric(group, "dollar_error")
        mean = _mean(realized)
        rows[str(value)] = {"rows": len(group), "mean_realized_model_minus_market": mean}
        ordered_errors.append(mean)
    monotonic = len(ordered_errors) >= 2 and all(
        right >= left for left, right in zip(ordered_errors, ordered_errors[1:])
    )
    return rows, monotonic


def _source_drift(frame: pd.DataFrame) -> Mapping[str, object]:
    providers = frame.groupby("source_provider")["observed_normalized_residual"].median()
    if "databento-opra" not in providers.index or "schwab" not in providers.index:
        return {"status": "NOT_PROVEN", "reason": "Both OPRA and Schwab evidence are required."}
    return {
        "status": "OBSERVED",
        "median_normalized_residual_difference": float(
            providers["databento-opra"] - providers["schwab"]
        ),
    }


def _any_violation(frame: pd.DataFrame, columns: Sequence[str]) -> pd.Series:
    output = pd.Series(False, index=frame.index, dtype="boolean")
    found = False
    for column in columns:
        if column in frame:
            output |= frame[column].fillna(False).astype("boolean")
            found = True
    return output if found else pd.Series(pd.NA, index=frame.index, dtype="boolean")


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _median(values: pd.Series) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.median()) if not clean.empty else math.nan


def _mean(values: pd.Series) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.mean()) if not clean.empty else math.nan


def _mean_bool(values: pd.Series) -> float:
    clean = values.dropna()
    return float(clean.astype(bool).mean()) if not clean.empty else math.nan


def _fraction(condition: pd.Series, eligible: pd.Series) -> float:
    clean = condition.loc[eligible]
    return float(clean.mean()) if not clean.empty else math.nan


def _one_or_mixed(frame: pd.DataFrame, column: str) -> str:
    if column not in frame:
        return "unknown"
    values = tuple(frame[column].astype("string").dropna().unique())
    return str(values[0]) if len(values) == 1 else "mixed"


def _as_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


__all__ = [
    "GATE_VERSION",
    "SURFACE_VERSION",
    "assessment_metrics",
    "build_gate_report",
    "build_monitoring_rows",
    "build_pricing_surfaces",
    "expiration_bucket",
    "moneyness_bucket",
]
