from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, RBF, WhiteKernel
from sklearn.preprocessing import RobustScaler

from ml.option_pricing.model import derived_feature_matrix
from ml.universe import RESEARCH_OPTION_BENCHMARK_SYMBOLS


SPY_EXACT_GP_BENCHMARK_VERSION = "spy-exact-gp-methodology-benchmark-v1"


@dataclass(frozen=True)
class ExactGpBenchmarkResult:
    report: Mapping[str, object]
    predictions: pd.DataFrame


def run_spy_exact_gp_benchmark(
    samples: pd.DataFrame,
    *,
    maximum_rows: int = 2_000,
    maximum_runtime_seconds: float = 120.0,
    minimum_volume: float = 1.0,
) -> ExactGpBenchmarkResult:
    """Run a bounded, research-only exact-GP methodology benchmark for SPY.

    This reproduces the paper's method shape, not its May--June 2019 result,
    unless the caller actually supplies that period's data.
    """

    if maximum_rows < 40:
        raise ValueError("maximum_rows must allow a meaningful chronological split")
    if maximum_runtime_seconds <= 0.0:
        raise ValueError("maximum_runtime_seconds must be positive")
    required = {
        "symbol",
        "call_put",
        "target_snapshot_for",
        "observed_mid",
        "black_scholes_price",
        "normalized_residual",
        "underlying_price",
        "sample_status",
    }
    if missing := sorted(required.difference(samples.columns)):
        raise ValueError("SPY exact-GP benchmark is missing: " + ", ".join(missing))
    frame = samples.loc[
        samples["symbol"].astype("string").str.upper().eq(
            RESEARCH_OPTION_BENCHMARK_SYMBOLS[0]
        )
        & samples["sample_status"].astype("string").eq("AVAILABLE")
        & samples["call_put"].astype("string").str.upper().isin({"CALL", "PUT"})
    ].copy()
    volume_column = next(
        (name for name in ("daily_volume", "volume", "trade_volume") if name in frame),
        None,
    )
    if volume_column is None:
        raise ValueError("SPY exact-GP benchmark requires a causal volume field")
    frame[volume_column] = pd.to_numeric(frame[volume_column], errors="coerce")
    frame = frame.loc[frame[volume_column].ge(float(minimum_volume))]
    frame["target_snapshot_for"] = pd.to_datetime(
        frame["target_snapshot_for"], utc=True, errors="coerce"
    )
    frame = frame.dropna(subset=["target_snapshot_for"]).sort_values(
        ["target_snapshot_for", "call_put", "contract_symbol"], kind="stable"
    )
    if len(frame) > maximum_rows:
        # Deterministically retain chronological coverage rather than sample randomly.
        positions = np.linspace(0, len(frame) - 1, maximum_rows, dtype=int)
        frame = frame.iloc[positions].copy()
    started = time.perf_counter()
    prediction_frames: list[pd.DataFrame] = []
    route_reports: dict[str, object] = {}
    for call_put in ("CALL", "PUT"):
        route = frame.loc[frame["call_put"].astype("string").str.upper().eq(call_put)]
        clusters = route["target_snapshot_for"].drop_duplicates().sort_values()
        if len(route) < 20 or len(clusters) < 2:
            route_reports[call_put] = {
                "status": "INSUFFICIENT_RESEARCH_ROWS",
                "rows": len(route),
                "clusters": len(clusters),
            }
            continue
        split_cluster = clusters.iloc[max(1, int(math.floor(len(clusters) * 0.8)))]
        train = route.loc[route["target_snapshot_for"].lt(split_cluster)]
        test = route.loc[route["target_snapshot_for"].ge(split_cluster)]
        if train.empty or test.empty:
            route_reports[call_put] = {"status": "EMPTY_CHRONOLOGICAL_PARTITION"}
            continue
        scaler = RobustScaler().fit(derived_feature_matrix(train))
        train_x = scaler.transform(derived_feature_matrix(train))
        test_x = scaler.transform(derived_feature_matrix(test))
        residual_y = pd.to_numeric(
            train["normalized_residual"], errors="coerce"
        ).to_numpy(dtype=float)
        normalized_price_y = (
            pd.to_numeric(train["observed_mid"], errors="coerce")
            / pd.to_numeric(train["underlying_price"], errors="coerce")
        ).to_numpy(dtype=float)
        kernel = ConstantKernel(1.0, (1e-3, 1e3)) * RBF(
            length_scale=np.ones(train_x.shape[1]),
            length_scale_bounds=(1e-2, 1e2),
        ) + WhiteKernel(noise_level=1e-4, noise_level_bounds=(1e-8, 1.0))
        residual_gp = GaussianProcessRegressor(
            kernel=kernel,
            normalize_y=True,
            random_state=17,
            n_restarts_optimizer=0,
        ).fit(train_x, residual_y)
        residual_mean, residual_std = residual_gp.predict(test_x, return_std=True)
        standard_gp = GaussianProcessRegressor(
            kernel=kernel,
            normalize_y=True,
            random_state=19,
            n_restarts_optimizer=0,
        ).fit(train_x, normalized_price_y)
        standard_mean = standard_gp.predict(test_x)
        underlying = pd.to_numeric(test["underlying_price"], errors="coerce").to_numpy(dtype=float)
        baseline = pd.to_numeric(test["black_scholes_price"], errors="coerce").to_numpy(dtype=float)
        observed = pd.to_numeric(test["observed_mid"], errors="coerce").to_numpy(dtype=float)
        constant = float(np.mean(residual_y))
        predictions = test.loc[
            :, ["symbol", "call_put", "contract_symbol", "target_snapshot_for"]
        ].copy()
        predictions["observed_mid"] = observed
        predictions["black_scholes"] = baseline
        predictions["constant_residual"] = baseline + constant * underlying
        predictions["exact_residual_gp"] = baseline + residual_mean * underlying
        predictions["exact_standard_gp"] = standard_mean * underlying
        predictions["exact_residual_gp_standard_deviation"] = residual_std * underlying
        prediction_frames.append(predictions)
        route_reports[call_put] = {
            "status": "COMPLETE",
            "training_rows": len(train),
            "test_rows": len(test),
            "training_start": train["target_snapshot_for"].min().isoformat(),
            "training_end": train["target_snapshot_for"].max().isoformat(),
            "test_start": test["target_snapshot_for"].min().isoformat(),
            "test_end": test["target_snapshot_for"].max().isoformat(),
            "normalized_rmse": {
                name: float(
                    np.sqrt(np.mean(np.square((predictions[name].to_numpy(dtype=float) - observed) / underlying)))
                )
                for name in (
                    "black_scholes",
                    "constant_residual",
                    "exact_residual_gp",
                    "exact_standard_gp",
                )
            },
            "interval_95_coverage": float(
                np.mean(
                    np.abs(predictions["exact_residual_gp"].to_numpy(dtype=float) - observed)
                    <= 1.959963984540054
                    * predictions["exact_residual_gp_standard_deviation"].to_numpy(dtype=float)
                )
            ),
        }
        if time.perf_counter() - started > maximum_runtime_seconds:
            raise TimeoutError("SPY exact-GP benchmark exceeded its declared runtime bound")
    predictions = (
        pd.concat(prediction_frames, ignore_index=True, sort=False)
        if prediction_frames
        else pd.DataFrame()
    )
    return ExactGpBenchmarkResult(
        report={
            "schema_version": SPY_EXACT_GP_BENCHMARK_VERSION,
            "symbol": "SPY",
            "research_only": True,
            "production_eligible": False,
            "automated_action_allowed": False,
            "paper_methodology_shape_implemented": True,
            "paper_result_reproduced": False,
            "paper_may_june_2019_experiment_claimed": False,
            "volume_filter": float(minimum_volume),
            "maximum_rows": int(maximum_rows),
            "maximum_runtime_seconds": float(maximum_runtime_seconds),
            "elapsed_seconds": time.perf_counter() - started,
            "routes": route_reports,
        },
        predictions=predictions,
    )


__all__ = [
    "ExactGpBenchmarkResult",
    "SPY_EXACT_GP_BENCHMARK_VERSION",
    "run_spy_exact_gp_benchmark",
]
