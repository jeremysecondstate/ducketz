from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from ml.option_pricing.policies import LoopNativeModelPolicy
from ml.option_pricing.shadow_model import (
    _fit_finite_basis_gp,
    surface_weights,
)


SYMBOLS = ("NVDA", "GOOG", "MU", "AAPL", "MSFT", "AMZN", "META", "TSLA", "CAT", "SNDK")
COMPONENT_COUNTS = (128, 256)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare 128 and 256 Nystroem components with the production "
            "robust-scaling/Bayesian-Ridge architecture on a deterministic, "
            "structurally representative fixture."
        )
    )
    parser.add_argument("--accuracy-ceiling", type=float, default=0.01)
    parser.add_argument("--latency-ceiling-seconds", type=float, default=30.0)
    parser.add_argument("--inference-repeats", type=int, default=7)
    args = parser.parse_args()
    if args.accuracy_ceiling <= 0.0 or args.latency_ceiling_seconds <= 0.0:
        parser.error("acceptance ceilings must be positive")
    if args.inference_repeats < 2:
        parser.error("inference-repeats must be at least two")

    samples = _training_fixture()
    inference = _inference_fixture()
    results: dict[str, object] = {}
    passing: list[int] = []
    for component_count in COMPONENT_COUNTS:
        measured = _benchmark_configuration(
            samples,
            inference,
            component_count=component_count,
            inference_repeats=args.inference_repeats,
        )
        accepted = bool(
            measured["assessment_weighted_rmse_normalized"] <= args.accuracy_ceiling
            and measured["ten_symbol_inference_p95_seconds"]
            <= args.latency_ceiling_seconds
        )
        measured["accepted"] = accepted
        results[str(component_count)] = measured
        if accepted:
            passing.append(component_count)

    retained = min(passing) if passing else None
    configured = LoopNativeModelPolicy().component_count
    payload = {
        "fixture_kind": "DETERMINISTIC_STRUCTURAL_BENCHMARK",
        "real_market_claim": False,
        "symbols": list(SYMBOLS),
        "training_rows": int(len(samples)),
        "inference_rows": int(len(inference)),
        "acceptance": {
            "assessment_weighted_rmse_normalized_max": args.accuracy_ceiling,
            "ten_symbol_inference_p95_seconds_max": args.latency_ceiling_seconds,
        },
        "configurations": results,
        "retained_components": retained,
        "configured_components": configured,
        "policy_matches_benchmark": retained == configured,
        "limitation": (
            "This fixture verifies comparative numerical capacity and local latency; "
            "it does not establish real-market calibration or profitability."
        ),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if retained is not None and retained == configured else 2


def _benchmark_configuration(
    samples: pd.DataFrame,
    inference: pd.DataFrame,
    *,
    component_count: int,
    inference_repeats: int,
) -> dict[str, object]:
    policy = replace(LoopNativeModelPolicy(), component_count=component_count)
    train = samples.loc[samples["partition"].eq("train")]
    calibration = samples.loc[samples["partition"].eq("calibration")]
    assessment = samples.loc[samples["partition"].eq("assessment")]
    fitted: dict[str, object] = {}
    selected_gamma: dict[str, float] = {}
    gamma_scores: dict[str, dict[str, float]] = {}
    fit_started = time.perf_counter()
    for call_put in ("CALL", "PUT"):
        side_train = train.loc[train["call_put"].eq(call_put)]
        side_calibration = calibration.loc[calibration["call_put"].eq(call_put)]
        target = side_train["normalized_market_residual"].to_numpy(dtype=float)
        cal_target = side_calibration["normalized_market_residual"].to_numpy(dtype=float)
        scores: dict[str, float] = {}
        best_model = None
        best_gamma = math.nan
        best_score = math.inf
        for gamma in policy.gamma_grid:
            model = _fit_finite_basis_gp(
                side_train,
                target,
                gamma=gamma,
                policy=policy,
            )
            predicted, _ = model.predict(side_calibration)
            score = _weighted_rmse(
                predicted,
                cal_target,
                surface_weights(side_calibration),
            )
            scores[str(gamma)] = score
            if score < best_score:
                best_model = model
                best_gamma = float(gamma)
                best_score = score
        if best_model is None:
            raise RuntimeError(f"No model was fitted for {call_put}")
        fitted[call_put] = best_model
        selected_gamma[call_put] = best_gamma
        gamma_scores[call_put] = scores
    fit_seconds = time.perf_counter() - fit_started

    assessment_predictions: list[np.ndarray] = []
    assessment_targets: list[np.ndarray] = []
    assessment_weights: list[np.ndarray] = []
    for call_put in ("CALL", "PUT"):
        side = assessment.loc[assessment["call_put"].eq(call_put)]
        predicted, _ = fitted[call_put].predict(side)  # type: ignore[union-attr]
        assessment_predictions.append(predicted)
        assessment_targets.append(
            side["normalized_market_residual"].to_numpy(dtype=float)
        )
        assessment_weights.append(surface_weights(side))
    assessment_rmse = _weighted_rmse(
        np.concatenate(assessment_predictions),
        np.concatenate(assessment_targets),
        np.concatenate(assessment_weights),
    )

    def infer_once() -> None:
        for call_put in ("CALL", "PUT"):
            side = inference.loc[inference["call_put"].eq(call_put)]
            fitted[call_put].predict(side)  # type: ignore[union-attr]

    infer_once()
    inference_measurements: list[float] = []
    for _ in range(inference_repeats):
        started = time.perf_counter()
        infer_once()
        inference_measurements.append(time.perf_counter() - started)

    return {
        "selected_gamma": selected_gamma,
        "calibration_weighted_rmse_by_gamma": gamma_scores,
        "assessment_weighted_rmse_normalized": assessment_rmse,
        "fit_and_gamma_selection_seconds": fit_seconds,
        "ten_symbol_inference_median_seconds": float(
            np.median(inference_measurements)
        ),
        "ten_symbol_inference_p95_seconds": float(
            np.percentile(inference_measurements, 95)
        ),
        "inference_measurements_seconds": inference_measurements,
    }


def _training_fixture() -> pd.DataFrame:
    rng = np.random.default_rng(20_260_811)
    timestamps = pd.date_range(
        "2026-05-01T14:00:00Z",
        periods=30,
        freq="1D",
    )
    rows: list[dict[str, object]] = []
    for session_index, target in enumerate(timestamps):
        partition = (
            "train" if session_index < 20 else "calibration" if session_index < 25 else "assessment"
        )
        for symbol_index, symbol in enumerate(SYMBOLS):
            underlying = 45.0 + 22.0 * symbol_index + 0.35 * session_index
            rate = 0.041 + 0.00015 * math.sin(session_index / 4.0)
            dividend = 0.004 + 0.0007 * (symbol_index % 4)
            for call_put in ("CALL", "PUT"):
                side = 1.0 if call_put == "CALL" else -1.0
                for strike_index, log_moneyness in enumerate(
                    np.linspace(-0.18, 0.18, 8)
                ):
                    years = 14.0 / 365.0 + (strike_index % 4) * 21.0 / 365.0
                    volatility = (
                        0.19
                        + 0.035 * abs(log_moneyness)
                        + 0.003 * (symbol_index % 3)
                        + 0.004 * math.cos(session_index / 5.0)
                    )
                    residual = (
                        side * 0.006 * math.tanh(-4.0 * log_moneyness)
                        + 0.004 * (volatility - 0.20)
                        + 0.0015 * math.sin(8.0 * math.sqrt(years))
                        + 0.0008 * math.log(underlying / 100.0)
                        + rng.normal(0.0, 0.0007)
                    )
                    rows.append(
                        {
                            "symbol": symbol,
                            "target_snapshot_for": target,
                            "call_put": call_put,
                            "underlying_price": underlying,
                            "strike": underlying * math.exp(log_moneyness),
                            "risk_free_rate": rate,
                            "lagged_implied_volatility": volatility,
                            "target_years_to_expiration": years,
                            "dividend_yield": dividend,
                            "normalized_market_residual": residual,
                            "partition": partition,
                        }
                    )
    return pd.DataFrame(rows)


def _inference_fixture() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    target = pd.Timestamp("2026-08-10T19:00:00Z")
    for symbol_index, symbol in enumerate(SYMBOLS):
        underlying = 48.0 + 22.0 * symbol_index
        for call_put in ("CALL", "PUT"):
            for expiration_index in range(7):
                years = (14.0 + 14.0 * expiration_index) / 365.0
                for log_moneyness in np.linspace(-0.22, 0.22, 100):
                    rows.append(
                        {
                            "symbol": symbol,
                            "target_snapshot_for": target,
                            "call_put": call_put,
                            "underlying_price": underlying,
                            "strike": underlying * math.exp(float(log_moneyness)),
                            "risk_free_rate": 0.041,
                            "lagged_implied_volatility": 0.21 + 0.03 * abs(log_moneyness),
                            "target_years_to_expiration": years,
                            "dividend_yield": 0.005 + 0.0007 * (symbol_index % 4),
                        }
                    )
    return pd.DataFrame(rows)


def _weighted_rmse(
    prediction: np.ndarray,
    target: np.ndarray,
    weights: np.ndarray,
) -> float:
    return float(math.sqrt(np.average(np.square(prediction - target), weights=weights)))


if __name__ == "__main__":
    raise SystemExit(main())
