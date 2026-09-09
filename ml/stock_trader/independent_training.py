"""Source-bound enrichment fits for the independent long-stock target contract.

The probability/return/downside/allocation heads learn observed stock outcomes.
Execution quote offsets and urgency remain explicit policy defaults: historical
OHLC outcomes are not broker fills. Frozen directional probabilities are neither
invented nor scored in-sample as features of these independent baseline fits.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, write_manifest
from ml.independent_stock_targets import GROUPS, STOCK_TARGET_CONTRACT_VERSION, STOCK_TIMEZONE, stock_target_windows
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS, canonical_sha256, finite, utc
from ml.stock_trader.model import (
    ENRICHMENT_MODEL_POINTER_VERSION, INDEPENDENT_ENRICHMENT_FEATURE_NAMES as CALENDAR_FEATURES,
    INDEPENDENT_MARKET_ENRICHMENT_FEATURE_NAMES as FEATURES, INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION,
    INDEPENDENT_ENRICHMENT_SCHEMA_VERSION, IndependentEnrichmentModel, LinearEnrichmentModel,
    LinearHead, _HEAD_LINKS, independent_scope_key, independent_target_feature_values,
)
from ml.stock_trader.market_features import INDEPENDENT_MARKET_FEATURE_NAMES, INDEPENDENT_MARKET_FEATURE_CONTRACT
from ml.stock_trader.scope_qualification import POOLED_SCOPE_QUALIFICATION_VERSION, qualify_pooled_scope_coverage
from ml.stock_trader.training import _inverse_softplus, _logit, _ridge, _write_json_atomic


FIT_BASIS = "exact-independent-long-stock-return-v1"
MINIMUM_ASSESSMENT_CLUSTERS = 10
MINIMUM_TRAIN_SCOPE_CLUSTERS = 20
_PARTITIONS = ("train", "selection", "calibration", "assessment")
_VERIFIED_COHORT_EVIDENCE: set[tuple[str, str, str]] = set()


def _plain_records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", date_format="iso", double_precision=15))


def _admit_targets(frame: pd.DataFrame, *, group: str, trained_at: pd.Timestamp,
                   require_market_features: bool = True) -> pd.DataFrame:
    """Validate real boundary observations and causal availability independently."""
    if frame.empty:
        return frame.copy()
    required = {"symbol", "route", "model_group", "action_date", "target_role", "execution_eligible",
                "target_contract_version", "decision_timestamp", "information_available_at",
                "target_window_start", "target_window_end", "observed_open_timestamp", "observed_close_timestamp",
                "target_open", "target_close", "observed_return", "assumed_round_trip_cost",
                "target_boundary_aligned", "target_price_source_contract", "target_price_dataset"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError("Independent enrichment cohort missing: " + ", ".join(sorted(missing)))
    data = frame.loc[frame.target_role.eq("EXECUTION") & frame.execution_eligible.eq(True)].copy()
    if data.empty:
        return data
    if require_market_features:
        missing_market = set(INDEPENDENT_MARKET_FEATURE_NAMES).difference(data.columns)
        if missing_market:
            raise ValueError("Independent enrichment missing market features: " + ", ".join(sorted(missing_market)))
        market = data[list(INDEPENDENT_MARKET_FEATURE_NAMES)].apply(pd.to_numeric, errors="coerce")
        if not np.isfinite(market.to_numpy(dtype=float)).all():
            raise ValueError("Independent enrichment market features must be finite causal observations")
        data[list(INDEPENDENT_MARKET_FEATURE_NAMES)] = market
    if not data.model_group.eq(group).all() or not data.target_contract_version.eq(STOCK_TARGET_CONTRACT_VERSION).all():
        raise ValueError("Independent enrichment rejects a relabelled horizon/target contract")
    if not data.symbol.isin(STOCK_TRADER_SYMBOLS).all():
        raise ValueError("Independent enrichment contains an unconfigured symbol")
    for column in ("decision_timestamp", "information_available_at", "target_window_start", "target_window_end",
                   "observed_open_timestamp", "observed_close_timestamp"):
        data[column] = pd.to_datetime(data[column], utc=True, errors="coerce")
        if data[column].isna().any():
            raise ValueError("Independent enrichment contains an invalid timestamp")
    if data.duplicated(["symbol", "route", "target_window_start", "target_window_end"]).any():
        raise ValueError("Independent enrichment repeats a natural target")
    from ml.independent_stock_targets import _calendar
    dates = pd.to_datetime(data.action_date)
    calendar = _calendar(dates.min().date(), dates.max().date())
    windows = {}
    for row in data.to_dict("records"):
        day = pd.Timestamp(row["action_date"]).date()
        if day not in windows:
            windows[day] = {spec["route"]: spec for spec in stock_target_windows(day, calendar=calendar)}
        spec = windows[day].get(row["route"])
        if spec is None or not spec["execution_eligible"] or spec["model_group"] != group or any(
            utc(row[key]) != utc(spec[key]) for key in ("target_window_start", "target_window_end")
        ):
            raise ValueError("Independent enrichment target differs from the exact clock contract")
    start, end = data.target_window_start, data.target_window_end
    if not (data.decision_timestamp.lt(start) & data.information_available_at.le(data.decision_timestamp)
            & end.le(trained_at) & data.observed_close_timestamp.le(trained_at)).all():
        raise ValueError("Independent enrichment contains future features or immature outcomes")
    if not (data.target_boundary_aligned.eq(True)
            & data.observed_open_timestamp.ge(start) & data.observed_open_timestamp.le(start + pd.Timedelta(minutes=5))
            & data.observed_close_timestamp.le(end) & data.observed_close_timestamp.ge(end - pd.Timedelta(minutes=5))
            & data.observed_open_timestamp.lt(data.observed_close_timestamp)).all():
        raise ValueError("Independent enrichment outcome is not boundary aligned")
    prices = data[["target_open", "target_close"]].apply(pd.to_numeric, errors="coerce")
    raw = pd.to_numeric(data.observed_return, errors="coerce")
    costs = pd.to_numeric(data.assumed_round_trip_cost, errors="coerce")
    if not (np.isfinite(prices).all().all() and prices.gt(0).all().all()
            and np.isfinite(raw).all() and np.isfinite(costs).all() and costs.ge(0).all()
            and np.allclose(raw, prices.target_close / prices.target_open - 1, rtol=1e-9, atol=1e-12)):
        raise ValueError("Independent enrichment returns differ from observed target prices")
    for column in ("target_price_source_contract", "target_price_dataset"):
        if data[column].isna().any() or data[column].astype(str).str.strip().eq("").any() or data[column].nunique() != 1:
            raise ValueError("Independent enrichment cannot mix or omit target price sources")
    from ml.stock_target_prices import stock_price_dataset
    if stock_price_dataset(str(data.target_price_source_contract.iloc[0])) != str(data.target_price_dataset.iloc[0]):
        raise ValueError("Independent enrichment target price source and dataset differ")
    data["net_return"] = raw - costs
    data["target"] = data.net_return.gt(0).astype(int)
    data["scope"] = [independent_scope_key(row.symbol, group, row.target_window_start, row.target_window_end)
                     for row in data.itertuples()]
    features = pd.DataFrame([independent_target_feature_values(symbol=row.symbol, horizon=group,
                    start=row.target_window_start, end=row.target_window_end, cost=row.assumed_round_trip_cost)
                    for row in data.itertuples()], index=data.index)
    data[list(CALENDAR_FEATURES)] = features[list(CALENDAR_FEATURES)]
    return data.sort_values(["decision_timestamp", "symbol", "route"], kind="stable").reset_index(drop=True)


def _partitions(data: pd.DataFrame, *, group: str) -> dict[str, pd.DataFrame]:
    from ml.nightly_gameplan import _chronological_partitions
    parts = _chronological_partitions(data, group=group)
    # Native target overlap purging is strengthened to actual next feature time.
    for left, right in zip(_PARTITIONS, _PARTITIONS[1:]):
        boundary = parts[right].decision_timestamp.min()
        parts[left] = parts[left].loc[parts[left].target_window_end.lt(boundary)].copy()
    for name, part in parts.items():
        if part.empty or part.target.nunique() != 2:
            raise RuntimeError(f"{group} {name} lacks two mature purged outcome classes")
    return parts


def _summary(frame: pd.DataFrame) -> dict:
    return {"rows": len(frame), "decision_clusters": int(frame.decision_timestamp.nunique()),
            "first_decision": frame.decision_timestamp.min().isoformat(),
            "last_decision": frame.decision_timestamp.max().isoformat(),
            "last_target_end": frame.target_window_end.max().isoformat(),
            "positive_rate": float(frame.target.mean()),
            "rows_sha256": canonical_sha256(_plain_records(frame))}


def _fit_heads(frame: pd.DataFrame, *, penalty: float) -> dict:
    x = frame[list(FEATURES)].to_numpy(dtype=float)
    means, scales = x.mean(axis=0), x.std(axis=0)
    scales = np.where(scales > 1e-9, scales, 1)
    z = (x - means) / scales
    net, raw = frame.net_return.to_numpy(), frame.observed_return.to_numpy()
    magnitude = max(float(np.quantile(np.abs(net), .9)), .005)
    targets = {
        "allocation_fraction": _logit(np.clip(np.maximum(net, 0) / magnitude, .01, .99)),
        "expected_net_return": net,
    }
    heads = {}
    for name, target in targets.items():
        intercept, coefficients = _ridge(z, target, penalty=penalty)
        heads[name] = {"intercept": intercept, "coefficients": coefficients, "link": _HEAD_LINKS[name]}
    heads["trade_probability"], probability_fit = _fit_binomial_head(z, (net > 0).astype(float), penalty=penalty)
    heads["adverse_return"], downside_fit = _fit_arithmetic_downside_head(z, np.maximum(-raw, 0), penalty=penalty)
    # Historical price outcomes contain no order-execution labels. Explicit
    # conservative defaults cannot masquerade as trained broker-execution heads.
    for name, value in {"execution_urgency": .5, "limit_offset_bps": 0.,
                        "protective_distance_pct": .005, "expected_holding_minutes": 1.}.items():
        link = _HEAD_LINKS[name]
        intercept = float(_logit(np.array([value]))[0]) if link == "sigmoid" else float(_inverse_softplus(np.array([max(value, 1e-12)]))[0])
        heads[name] = {"intercept": intercept, "coefficients": [0.] * len(FEATURES), "link": link}
    return {"feature_names": list(FEATURES), "feature_means": means.tolist(), "feature_scales": scales.tolist(),
            "feature_contract_version": INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION,
            "market_feature_contract": INDEPENDENT_MARKET_FEATURE_CONTRACT,
            "heads": heads, "ridge_penalty": penalty, "magnitude_scale_training_only": magnitude,
            "head_training_objectives": {"trade_probability": probability_fit, "adverse_return": downside_fit},
            "base_probability": float((net > 0).mean()), "base_net_return": float(net.mean()),
            "base_adverse_return": float(np.maximum(-raw, 0).mean())}


def _fit_binomial_head(z: np.ndarray, target: np.ndarray, *, penalty: float) -> tuple[dict, dict]:
    """Fit Bernoulli likelihood, not squared error on transformed binary labels."""
    design = np.column_stack((np.ones(len(z)), z))
    labels = np.asarray(target, dtype=float)
    initial = np.zeros(design.shape[1])
    initial[0] = float(_logit(np.array([np.clip(labels.mean(), 1e-8, 1 - 1e-8)]))[0])
    def objective(parameters):
        score = design @ parameters
        loss = float(np.mean(np.logaddexp(0, score) - labels * score)
                     + .5 * penalty * np.dot(parameters[1:], parameters[1:]) / len(labels))
        gradient = design.T @ (expit(score) - labels) / len(labels)
        gradient[1:] += penalty * parameters[1:] / len(labels)
        return loss, gradient
    fitted = minimize(objective, initial, jac=True, method="L-BFGS-B",
                      options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-9})
    if not fitted.success or not np.isfinite(fitted.x).all():
        raise RuntimeError("Independent enrichment binomial objective did not converge")
    return ({"intercept": float(fitted.x[0]), "coefficients": fitted.x[1:].tolist(), "link": "sigmoid"},
            {"objective": "penalized_binomial_negative_log_likelihood", "converged": True,
             "iterations": int(fitted.nit), "training_objective": float(fitted.fun)})


def _fit_arithmetic_downside_head(z: np.ndarray, target: np.ndarray, *, penalty: float) -> tuple[dict, dict]:
    """Fit the nonnegative arithmetic conditional mean evaluated by downside MSE.

Softplus is only the prediction link. Applying least squares to inverse-softplus
labels would estimate a transformed/geometric center and understate mean losses,
particularly when most observed targets are zero. Normalize the direct residual
using training outcomes only; the regularizer excludes the intercept.
"""
    design = np.column_stack((np.ones(len(z)), z))
    downside = np.asarray(target, dtype=float)
    outcome_scale = max(float(np.sqrt(np.mean(downside ** 2))), 1e-6)
    initial = np.zeros(design.shape[1])
    initial[0] = float(_inverse_softplus(np.array([max(float(downside.mean()), 1e-12)]))[0])
    def objective(parameters):
        score = design @ parameters
        predicted = np.logaddexp(0, score)
        residual = predicted - downside
        loss = float(.5 * np.mean((residual / outcome_scale) ** 2)
                     + .5 * penalty * np.dot(parameters[1:], parameters[1:]) / len(downside))
        gradient = design.T @ (residual * expit(score)) / (len(downside) * outcome_scale ** 2)
        gradient[1:] += penalty * parameters[1:] / len(downside)
        return loss, gradient
    fitted = minimize(objective, initial, jac=True, method="L-BFGS-B",
                      options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-9})
    if not fitted.success or not np.isfinite(fitted.x).all():
        raise RuntimeError("Independent enrichment arithmetic downside objective did not converge")
    predicted = np.logaddexp(0, design @ fitted.x)
    return ({"intercept": float(fitted.x[0]), "coefficients": fitted.x[1:].tolist(), "link": "softplus"},
            {"objective": "penalized_direct_arithmetic_downside_squared_error", "converged": True,
             "iterations": int(fitted.nit), "training_objective": float(fitted.fun),
             "outcome_scale_training_only": outcome_scale,
             "training_downside_mse": float(np.mean((predicted - downside) ** 2))})


def _linear_model(record: Mapping, *, group: str, fingerprint: str) -> LinearEnrichmentModel:
    feature_names = tuple(record.get("feature_names", ()))
    if feature_names not in (FEATURES, CALENDAR_FEATURES):
        raise ValueError("Independent enrichment feature contract differs")
    if feature_names == FEATURES and (
        record.get("feature_contract_version") != INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION
        or record.get("market_feature_contract") != INDEPENDENT_MARKET_FEATURE_CONTRACT
    ):
        raise ValueError("Independent enrichment market feature contract is invalid")
    means, scales = tuple(record.get("feature_means", ())), tuple(record.get("feature_scales", ()))
    if len(means) != len(feature_names) or len(scales) != len(feature_names) or not np.isfinite([*means, *scales]).all() or min(scales) <= 0:
        raise ValueError("Independent enrichment normalization is invalid")
    raw_heads = record.get("heads", {})
    if set(raw_heads) != set(_HEAD_LINKS):
        raise ValueError("Independent enrichment head contract differs")
    heads = {}
    for name, link in _HEAD_LINKS.items():
        raw = raw_heads[name]
        coefficients = tuple(raw.get("coefficients", ()))
        intercept = finite(raw.get("intercept"))
        if raw.get("link") != link or intercept is None or len(coefficients) != len(feature_names) or not np.isfinite(coefficients).all():
            raise ValueError("Independent enrichment fitted coefficients are invalid")
        heads[name] = LinearHead(intercept, coefficients, link)
    return LinearEnrichmentModel("independent-stock-" + group, "v1", fingerprint, feature_names, means, scales, heads)


def _predictions(model: LinearEnrichmentModel, frame: pd.DataFrame, calibration=(1., 0.)) -> pd.DataFrame:
    rows = []
    for row in frame.to_dict("records"):
        predicted = model.predict(row)
        probability = np.clip(predicted.trade_probability, 1e-12, 1 - 1e-12)
        logit = math.log(probability / (1 - probability))
        calibrated = 1 / (1 + math.exp(-float(np.clip(calibration[0] * logit + calibration[1], -40, 40))))
        rows.append({"probability": calibrated, "expected_net_return": predicted.expected_net_return,
                     "adverse_return": predicted.adverse_return})
    return pd.DataFrame(rows, index=frame.index)


def _scores(frame: pd.DataFrame, predictions: pd.DataFrame, record: Mapping) -> dict:
    from ml.nightly_gameplan import _proper_scores
    target = frame.target.to_numpy(dtype=float)
    probability = predictions.probability.to_numpy(dtype=float)
    scores = _proper_scores(target, probability)
    base = _proper_scores(target, np.full(len(frame), record["base_probability"]))
    return {"brier": scores["brier_score"], "log_loss": scores["log_loss"], "ece": scores["expected_calibration_error_10_bin"],
            "base_brier": base["brier_score"], "base_log_loss": base["log_loss"],
            "return_mse": float(np.mean((frame.net_return.to_numpy() - predictions.expected_net_return.to_numpy()) ** 2)),
            "base_return_mse": float(np.mean((frame.net_return.to_numpy() - record["base_net_return"]) ** 2)),
            "adverse_mse": float(np.mean((np.maximum(-frame.observed_return.to_numpy(), 0) - predictions.adverse_return.to_numpy()) ** 2)),
            "base_adverse_mse": float(np.mean((np.maximum(-frame.observed_return.to_numpy(), 0) - record["base_adverse_return"]) ** 2)),
            "probability_range": [float(probability.min()), float(probability.max())]}


def _quality_passes(scores: Mapping) -> bool:
    return bool(scores["brier"] < scores["base_brier"] and scores["log_loss"] < scores["base_log_loss"]
                and scores["ece"] <= .15 and scores["return_mse"] < scores["base_return_mse"]
                and scores["adverse_mse"] <= scores["base_adverse_mse"]
                and scores["probability_range"][1] - scores["probability_range"][0] > 1e-8)


def _scope_evidence(record: Mapping, model: LinearEnrichmentModel) -> dict:
    assessment = pd.DataFrame(record.get("assessment_rows", ()))
    if assessment.empty:
        raise ValueError("Fitted independent enrichment has no untouched assessment rows")
    for column in ("decision_timestamp", "target_window_start", "target_window_end"):
        assessment[column] = pd.to_datetime(assessment[column], utc=True, errors="raise")
    if assessment.duplicated(["scope", "target_window_start", "target_window_end"]).any():
        raise ValueError("Independent enrichment assessment repeats a natural target")
    summary = record["partition_evidence"]["assessment"]
    if (len(assessment) != summary["rows"] or assessment.decision_timestamp.nunique() != summary["decision_clusters"]
            or assessment.decision_timestamp.min() != utc(summary["first_decision"])
            or assessment.decision_timestamp.max() != utc(summary["last_decision"])
            or assessment.target_window_end.max() != utc(summary["last_target_end"])):
        raise ValueError("Independent enrichment assessment differs from its chronological evidence")
    if not (assessment.target_window_end.gt(assessment.target_window_start)
            & assessment.decision_timestamp.lt(assessment.target_window_start)).all():
        raise ValueError("Independent enrichment assessment target causality differs")
    for row in assessment.to_dict("records"):
        group = str(row["route"]).split("@", 1)[0]
        if independent_scope_key(row["symbol"], group, row["target_window_start"], row["target_window_end"]) != row["scope"]:
            raise ValueError("Independent enrichment assessment scope differs from its target")
        expected = independent_target_feature_values(symbol=row["symbol"], horizon=group,
            start=row["target_window_start"], end=row["target_window_end"], cost=row["assumed_round_trip_cost"])
        if not np.allclose([row[name] for name in CALENDAR_FEATURES], [expected[name] for name in CALENDAR_FEATURES], rtol=1e-10, atol=1e-12):
            raise ValueError("Independent enrichment assessment features are not causal target features")
        if not all(finite(row.get(name)) is not None for name in model.feature_names):
            raise ValueError("Independent enrichment assessment features are missing or nonfinite")
        if row["target"] != int(row["net_return"] > 0) or not math.isclose(
            row["net_return"], row["observed_return"] - row["assumed_round_trip_cost"], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("Independent enrichment assessment labels differ from returns")
    predictions = _predictions(model, assessment, tuple(record["probability_calibration"]))
    overall = _scores(assessment, predictions, record)
    global_ready = (assessment.decision_timestamp.nunique() >= MINIMUM_ASSESSMENT_CLUSTERS and _quality_passes(overall))
    scope_policy = record.get("scope_qualification_policy")
    if scope_policy not in (None, POOLED_SCOPE_QUALIFICATION_VERSION):
        raise ValueError("Independent enrichment scope qualification policy is unsupported")
    support = {}
    for scope, train_count in record["fit_scope_decision_clusters"].items():
        selected = assessment.loc[assessment.scope.eq(scope)]
        scores = _scores(selected, predictions.loc[selected.index], record) if len(selected) else None
        clusters = int(selected.decision_timestamp.nunique())
        ready = (global_ready and train_count >= MINIMUM_TRAIN_SCOPE_CLUSTERS
                 and clusters >= MINIMUM_ASSESSMENT_CLUSTERS and scores is not None and _quality_passes(scores))
        reason = "HELD_OUT_EXACT_SCOPE_QUALIFIED" if ready else (
            "INSUFFICIENT_EXACT_SCOPE_EVIDENCE" if train_count < MINIMUM_TRAIN_SCOPE_CLUSTERS or clusters < MINIMUM_ASSESSMENT_CLUSTERS
            else "HELD_OUT_QUALITY_NOT_PROMOTED")
        support[scope] = {"status": "READY" if ready else "RESEARCH", "reason": reason,
                          "target_price_source_contract": record["target_price_source_contract"],
                          "fit_decision_clusters": train_count, "assessment_decision_clusters": clusters,
                          "assessment_scores": scores, "horizon_assessment_scores": overall}
    if scope_policy == POOLED_SCOPE_QUALIFICATION_VERSION:
        for scope in set(assessment.scope).difference(support):
            selected = assessment.loc[assessment.scope.eq(scope)]
            support[scope] = {"assessment_decision_clusters": int(selected.decision_timestamp.nunique()),
                              "assessment_scores": _scores(selected, predictions.loc[selected.index], record),
                              "horizon_assessment_scores": overall}
        return qualify_pooled_scope_coverage(global_ready=bool(global_ready),
            fit_scope_decision_clusters=record["fit_scope_decision_clusters"], scope_diagnostics=support,
            target_price_source_contract=record["target_price_source_contract"])
    return support


def _fit_logit_platt(probability: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    values = np.clip(np.asarray(probability, dtype=float), 1e-12, 1 - 1e-12)
    logits = np.log(values / (1 - values))
    def objective(parameters):
        score = parameters[0] * logits + parameters[1]
        return float(np.mean(np.logaddexp(0, score) - labels * score))
    result = minimize(objective, np.array([1., 0.]), method="L-BFGS-B", bounds=((0., 20.), (-20., 20.)))
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError("Probability calibration did not converge")
    return float(result.x[0]), float(result.x[1])


def _select_development_calibration(calibration: pd.DataFrame, probability: np.ndarray) -> tuple[tuple[float, float], dict]:
    """Identity vs existing logit-Platt chosen before any final assessment read."""
    frame = calibration.copy().reset_index(drop=True)
    values = np.asarray(probability, dtype=float)
    if (frame.empty or values.ndim != 1 or len(values) != len(frame) or not np.isfinite(values).all()
            or ((values < 0) | (values > 1)).any() or not frame.target.isin([0, 1]).all()):
        raise ValueError("Enrichment development calibration observations are invalid")
    for name in ("decision_timestamp", "target_window_start", "target_window_end"):
        frame[name] = pd.to_datetime(frame[name], utc=True, errors="raise")
        if frame[name].isna().any():
            raise ValueError("Enrichment development calibration timestamps are invalid")
    frame["raw_probability"] = values
    clusters = pd.Index(frame.decision_timestamp.unique()).sort_values()
    midpoint = len(clusters) // 2
    fit = frame.loc[frame.decision_timestamp.isin(clusters[:midpoint])].copy()
    validation = frame.loc[frame.decision_timestamp.isin(clusters[midpoint:])].copy()
    before_purge = len(fit)
    if len(validation):
        fit = fit.loc[fit.target_window_end.lt(validation.decision_timestamp.min())].copy()
    report = {"policy": "independent-enrichment-logit-development-selection-v1",
        "selection_basis": "minimum_later_calibration_development_log_loss_identity_wins_ties",
        "assessment_used_for_selection": False, "split": "first_half_vs_second_half_decision_clusters",
        "fit_rows": len(fit), "validation_rows": len(validation), "purged_rows": before_purge - len(fit),
        "fit_decision_clusters": int(fit.decision_timestamp.nunique()),
        "validation_decision_clusters": int(validation.decision_timestamp.nunique()),
        "fit_last_target_end": fit.target_window_end.max().isoformat() if len(fit) else None,
        "validation_first_decision": validation.decision_timestamp.min().isoformat() if len(validation) else None,
        "candidate_metrics": {}, "selected_family": "identity", "full_calibration_refit_rows": 0}
    if fit.decision_timestamp.nunique() < 2 or validation.decision_timestamp.nunique() < 2 or fit.target.nunique() != 2:
        report["selection_status"] = "IDENTITY_INSUFFICIENT_PURGED_DEVELOPMENT_SUPPORT"
        return (1., 0.), report
    try:
        fitted_platt = _fit_logit_platt(fit.raw_probability.to_numpy(), fit.target.to_numpy())
    except RuntimeError:
        report["selection_status"] = "IDENTITY_PLATT_DEVELOPMENT_FIT_FAILED"
        return (1., 0.), report
    candidates = {"identity": (1., 0.), "platt": fitted_platt}
    raw = np.clip(validation.raw_probability.to_numpy(), 1e-12, 1 - 1e-12)
    logits = np.log(raw / (1 - raw))
    labels = validation.target.to_numpy()
    for family, (slope, intercept) in candidates.items():
        predicted = np.clip(expit(slope * logits + intercept), 1e-6, 1 - 1e-6)
        report["candidate_metrics"][family] = {
            "log_loss": float(-np.mean(labels * np.log(predicted) + (1 - labels) * np.log(1 - predicted))),
            "brier_score": float(np.mean((labels - predicted) ** 2)), "rows": len(validation)}
    selected = min(candidates, key=lambda family: report["candidate_metrics"][family]["log_loss"])
    report.update(selected_family=selected, selection_status="SELECTED_ON_PURGED_DEVELOPMENT")
    if selected == "identity":
        return (1., 0.), report
    report["full_calibration_refit_rows"] = len(frame)
    return _fit_logit_platt(values, frame.target.to_numpy()), report


def _fit_development_selected_heads(parts: Mapping[str, pd.DataFrame], *, group: str, fingerprint: str) -> dict:
    """Choose each fixed head's regularization on its own development objective."""
    penalties = (1., 5., 20.)
    head_objectives = {"trade_probability": "log_loss", "expected_net_return": "return_mse",
                       "adverse_return": "adverse_mse", "allocation_fraction": "allocation_transformed_target_mse"}
    metrics = {}
    for penalty in penalties:
        candidate = _fit_heads(parts["train"], penalty=penalty)
        model = _linear_model(candidate, group=group, fingerprint=fingerprint)
        selected = _scores(parts["selection"], _predictions(model, parts["selection"]), candidate)
        selected_x = parts["selection"][list(FEATURES)].to_numpy(dtype=float)
        normalized = (selected_x - np.asarray(candidate["feature_means"])) / np.asarray(candidate["feature_scales"])
        allocation_head = candidate["heads"]["allocation_fraction"]
        predicted_score = allocation_head["intercept"] + normalized @ np.asarray(allocation_head["coefficients"])
        allocation_target = _logit(np.clip(np.maximum(parts["selection"].net_return.to_numpy(), 0)
                                          / candidate["magnitude_scale_training_only"], .01, .99))
        selected["allocation_transformed_target_mse"] = float(np.mean((predicted_score - allocation_target) ** 2))
        metrics[penalty] = {name: selected[name] for name in head_objectives.values()}
    choices = {head: min(penalties, key=lambda penalty: metrics[penalty][objective])
               for head, objective in head_objectives.items()}
    fit = pd.concat([parts["train"], parts["selection"]], ignore_index=True)
    refits = {penalty: _fit_heads(fit, penalty=penalty) for penalty in sorted(set(choices.values()))}
    record = dict(refits[choices["expected_net_return"]])
    record["heads"] = dict(record["heads"])
    for head, penalty in choices.items():
        record["heads"][head] = refits[penalty]["heads"][head]
    record["head_training_objectives"] = {
        head: refits[choices[head]]["head_training_objectives"][head]
        for head in ("trade_probability", "adverse_return")}
    record.pop("ridge_penalty")
    record["head_ridge_penalties"] = choices
    record["head_development_selection"] = {
        "policy": "independent-enrichment-head-objective-selection-v1", "candidate_penalties": list(penalties),
        "assessment_used_for_selection": False, "tie_policy": "first_penalty_in_fixed_order",
        "head_objectives": head_objectives, "selected_penalties": choices,
        "candidate_metrics": {str(penalty): value for penalty, value in metrics.items()}}
    return record


def fit_independent_enrichment_model_payload(training_groups: Mapping[str, pd.DataFrame], *, trained_at: object,
                                            source_fingerprint: str) -> tuple[dict, dict]:
    timestamp = utc_timestamp(trained_at)
    if set(training_groups) != set(GROUPS) or len(source_fingerprint) != 64:
        raise ValueError("Independent enrichment requires all four source-bound cohort groups")
    horizons, reports = {}, {}
    source_contracts = set()
    for group in GROUPS:
        data = _admit_targets(training_groups[group], group=group, trained_at=timestamp)
        source_contracts.update(zip(data.get("target_price_source_contract", ()), data.get("target_price_dataset", ())))
        if len(source_contracts) > 1:
            raise ValueError("Independent enrichment refuses cross-group mixed price sources")
        report = {"status": "INSUFFICIENT_EVIDENCE", "admitted_rows": len(data),
                  "excluded_context_rows": len(training_groups[group]) - len(data),
                  "symbols_without_targets": sorted(set(STOCK_TRADER_SYMBOLS) - set(data.get("symbol", ())))}
        try:
            if data.empty:
                raise RuntimeError("No exact execution outcomes")
            parts = _partitions(data, group=group)
        except RuntimeError as exc:
            report["reason"] = str(exc)
            horizons[group] = {"fitted": False, "reason": str(exc)}
            reports[group] = report
            continue
        fit = pd.concat([parts["train"], parts["selection"]], ignore_index=True)
        record = _fit_development_selected_heads(parts, group=group, fingerprint=source_fingerprint)
        model = _linear_model(record, group=group, fingerprint=source_fingerprint)
        raw = _predictions(model, parts["calibration"]).probability.to_numpy()
        try:
            calibrated, calibration_selection = _select_development_calibration(parts["calibration"], raw)
        except RuntimeError:
            horizons[group] = {"fitted": False, "reason": "Probability calibration did not converge"}
            reports[group] = {**report, "reason": horizons[group]["reason"]}
            continue
        evidence_columns = list(dict.fromkeys([*FEATURES, "symbol", "route", "scope", "decision_timestamp",
                           "target_window_start", "target_window_end", "target", "net_return", "observed_return"]))
        record.update({"fitted": True, "fit_basis": FIT_BASIS, "probability_calibration": list(calibrated),
                       "scope_qualification_policy": POOLED_SCOPE_QUALIFICATION_VERSION,
                       "calibration_selection": calibration_selection,
                       "partition_evidence": {name: _summary(frame) for name, frame in parts.items()},
                       "fit_scope_decision_clusters": {str(scope): int(count) for scope, count in fit.groupby("scope").decision_timestamp.nunique().items()},
                       "assessment_rows": _plain_records(parts["assessment"][evidence_columns]),
                       "cohort_rows_sha256": canonical_sha256(_plain_records(data)),
                       "target_price_source_contract": str(data.target_price_source_contract.iloc[0]),
                       "target_price_dataset": str(data.target_price_dataset.iloc[0])})
        support = _scope_evidence(record, model)
        report.update(status="FITTED", partition_evidence=record["partition_evidence"],
                      head_training_objectives=record["head_training_objectives"],
                      head_development_selection=record["head_development_selection"],
                      calibration_selection=calibration_selection,
                      qualified_scope_count=sum(value["status"] == "READY" for value in support.values()),
                      fitted_scope_count=len(record["fit_scope_decision_clusters"]),
                      diagnostic_scope_count=len(support), scope_readiness=support)
        horizons[group], reports[group] = record, report
    payload = {"schema_version": INDEPENDENT_ENRICHMENT_SCHEMA_VERSION,
               "scope_qualification_policy": POOLED_SCOPE_QUALIFICATION_VERSION,
               "feature_contract_version": INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION,
               "market_feature_contract": INDEPENDENT_MARKET_FEATURE_CONTRACT,
               "model_name": "independent-stock-horizon-enrichment", "model_version": timestamp.strftime("%Y%m%dT%H%M%S.%fZ"),
               "trained_at": timestamp.isoformat(), "target_contract_version": STOCK_TARGET_CONTRACT_VERSION,
               "source_fingerprint": source_fingerprint, "fit_basis": FIT_BASIS, "horizons": horizons,
               "execution_head_basis": "fixed_conservative_policy_defaults_no_historical_fill_labels",
               "holding_head_basis": "exact_declared_target_expiry_not_a_fitted_duration_proxy"}
    payload["model_fingerprint"] = canonical_sha256(payload)
    model = independent_model_from_payload(payload)
    report = {"status": "MODELS_FIT" if model.horizon_models else "INSUFFICIENT_EVIDENCE",
              "trained_at": timestamp.isoformat(), "model_fingerprint": model.model_fingerprint,
              "supported_horizons": list(model.supported_horizons), "qualified_target_contracts": list(model.qualified_target_contracts),
              "horizons": reports, "broker_orders_enabled": False, "orders_placed": 0}
    return payload, report


def independent_model_from_payload(payload: Mapping) -> IndependentEnrichmentModel:
    observed = str(payload.get("model_fingerprint", ""))
    if observed != canonical_sha256({key: value for key, value in payload.items() if key != "model_fingerprint"}):
        raise ValueError("Independent enrichment fingerprint does not match its payload")
    if (payload.get("schema_version") != INDEPENDENT_ENRICHMENT_SCHEMA_VERSION
            or payload.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
            or payload.get("fit_basis") != FIT_BASIS or len(str(payload.get("source_fingerprint", ""))) != 64
            or set(payload.get("horizons", {})) != set(GROUPS)):
        raise ValueError("Independent enrichment training contract is invalid")
    models, calibrators, support = {}, {}, {}
    for group, record in payload["horizons"].items():
        if not record.get("fitted"):
            continue
        if record.get("fit_basis") != FIT_BASIS:
            raise ValueError("Independent enrichment has no exact fitted target basis")
        from ml.stock_target_prices import stock_price_dataset
        if (stock_price_dataset(str(record.get("target_price_source_contract", ""))) != record.get("target_price_dataset")
                or len(str(record.get("cohort_rows_sha256", ""))) != 64):
            raise ValueError("Independent enrichment fitted target provenance is invalid")
        summaries = record.get("partition_evidence", {})
        if set(summaries) != set(_PARTITIONS):
            raise ValueError("Independent enrichment lacks chronological evidence")
        for left, right in zip(_PARTITIONS, _PARTITIONS[1:]):
            if utc(summaries[left]["last_target_end"]) >= utc(summaries[right]["first_decision"]):
                raise ValueError("Independent enrichment has training/holdout label leakage")
        if utc(summaries["assessment"]["last_target_end"]) > utc(payload["trained_at"]):
            raise ValueError("Independent enrichment assessment includes immature returns")
        calibration = tuple(record.get("probability_calibration", ()))
        if len(calibration) != 2 or not np.isfinite(calibration).all() or not 0 <= calibration[0] <= 20:
            raise ValueError("Independent enrichment probability calibration is invalid")
        model = _linear_model(record, group=group, fingerprint=observed)
        if tuple(record["feature_names"]) == FEATURES and (
            payload.get("feature_contract_version") != INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION
            or payload.get("market_feature_contract") != INDEPENDENT_MARKET_FEATURE_CONTRACT
        ):
            raise ValueError("Independent enrichment payload and fitted feature contracts differ")
        if payload.get("feature_contract_version") == INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION and tuple(record["feature_names"]) != FEATURES:
            raise ValueError("Independent enrichment cannot relabel a calendar-only fit as market-aware")
        if record.get("scope_qualification_policy") == POOLED_SCOPE_QUALIFICATION_VERSION and (
            tuple(record["feature_names"]) != FEATURES or payload.get("scope_qualification_policy") != POOLED_SCOPE_QUALIFICATION_VERSION
        ):
            raise ValueError("Pooled scope qualification requires a newly fitted market-feature contract")
        models[group], calibrators[group] = model, calibration
        support.update(_scope_evidence(record, model))
    return IndependentEnrichmentModel(str(payload["model_name"]), str(payload["model_version"]), observed,
                                      models, calibrators, support)


def load_current_training_cohorts(datastore_root: Path, *, gameplan_run: Path | None = None) -> tuple[dict[str, pd.DataFrame], tuple[Path, ...], dict]:
    from ml.nightly_gameplan import read_current_gameplan, read_gameplan_run
    root = Path(datastore_root).resolve()
    publication = (read_gameplan_run(root, Path(gameplan_run)) if gameplan_run is not None
                   else read_current_gameplan(root))
    configuration = publication.manifest.get("configuration", {})
    if configuration.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION:
        raise ValueError("Independent enrichment requires the current independent Gameplan")
    outputs = publication.manifest["output_files"]
    names = [f"training-cohort-{group}.parquet" for group in GROUPS]
    if any(name not in outputs for name in names):
        raise ValueError("Current Gameplan has no source-bound independent training cohorts")
    paths = tuple(publication.run_directory / name for name in names)
    sources = (publication.run_directory / "manifest.json", publication.run_directory / "receipt.json", *paths)
    inventory = {path.relative_to(root).as_posix(): file_checksum(path) for path in sources}
    metadata = {"source_gameplan_run": publication.run_directory.relative_to(root).as_posix(),
                "source_files": inventory, "source_fingerprint": canonical_sha256(inventory)}
    return {group: pd.read_parquet(path) for group, path in zip(GROUPS, paths)}, sources, metadata


def verify_independent_model_sources(root: Path, payload: Mapping, manifest: Mapping) -> None:
    """A standalone claim of qualification cannot replace a native bound cohort."""
    source = payload.get("source_publication", {})
    inventory = source.get("source_files", {})
    if (not inventory or source.get("source_fingerprint") != canonical_sha256(inventory)
            or payload.get("source_fingerprint") != source.get("source_fingerprint")):
        raise ValueError("Independent enrichment has no verified source publication")
    run = (root / str(source.get("source_gameplan_run", ""))).resolve()
    if not run.is_relative_to(root / "ml" / "nightly-gameplan-runs"):
        raise ValueError("Independent enrichment source escapes Gameplan runs")
    expected = {run / "manifest.json", run / "receipt.json", *(run / f"training-cohort-{group}.parquet" for group in GROUPS)}
    input_checksums = {str(item.get("path", "")).replace("\\", "/"): item.get("checksum_sha256")
                       for item in manifest.get("input_files", ())}
    if {(root / name).resolve() for name in inventory} != expected:
        raise ValueError("Independent enrichment source cohort inventory differs")
    for name, checksum in inventory.items():
        path = (root / name).resolve()
        if input_checksums.get(name) != checksum or not path.is_file() or file_checksum(path) != checksum:
            raise ValueError("Independent enrichment source cohort checksum differs")
    # Hashes are necessary but a model must also prove that its declared source
    # really is a native publication and that assessment/counts came from it.
    # Cache only after checking every source checksum above on every load.
    cache_key = (str(root), str(payload.get("model_fingerprint", "")), str(payload.get("source_fingerprint", "")))
    if cache_key in _VERIFIED_COHORT_EVIDENCE:
        return
    from ml.nightly_gameplan import read_gameplan_run
    try:
        publication = read_gameplan_run(root, run)
    except (RuntimeError, ValueError, OSError) as exc:
        raise ValueError("Independent enrichment source is not a valid native Gameplan publication") from exc
    if publication.manifest.get("configuration", {}).get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION:
        raise ValueError("Independent enrichment source publication target contract differs")
    trained_at = utc(payload["trained_at"])
    for group in GROUPS:
        record = payload["horizons"][group]
        cohort_name = f"training-cohort-{group}.parquet"
        if cohort_name not in publication.manifest["output_files"]:
            raise ValueError("Independent enrichment source publication omits a cohort")
        require_market = payload.get("feature_contract_version") == INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION
        data = _admit_targets(pd.read_parquet(run / cohort_name), group=group, trained_at=trained_at,
                             require_market_features=require_market)
        if not record.get("fitted"):
            continue
        if canonical_sha256(_plain_records(data)) != record.get("cohort_rows_sha256"):
            raise ValueError("Independent enrichment fitted cohort differs from its immutable source")
        try:
            parts = _partitions(data, group=group)
        except RuntimeError as exc:
            raise ValueError("Independent enrichment source cannot support its fitted partitions") from exc
        if {name: _summary(part) for name, part in parts.items()} != record.get("partition_evidence"):
            raise ValueError("Independent enrichment partition evidence differs from its source cohort")
        fit = pd.concat([parts["train"], parts["selection"]], ignore_index=True)
        counts = {str(scope): int(count) for scope, count in fit.groupby("scope").decision_timestamp.nunique().items()}
        if counts != record.get("fit_scope_decision_clusters"):
            raise ValueError("Independent enrichment fit scope counts differ from its source cohort")
        columns = list(dict.fromkeys([*record["feature_names"], "symbol", "route", "scope", "decision_timestamp",
                       "target_window_start", "target_window_end", "target", "net_return", "observed_return"]))
        if canonical_sha256(_plain_records(parts["assessment"][columns])) != canonical_sha256(record.get("assessment_rows", ())):
            raise ValueError("Independent enrichment assessment evidence differs from its source cohort")
    _VERIFIED_COHORT_EVIDENCE.add(cache_key)


def train_and_publish_independent_enrichment_model(datastore_root: Path, *, trained_at: object | None = None,
                                                 publish_current: bool = True,
                                                 gameplan_run: Path | None = None) -> Path:
    root, timestamp = Path(datastore_root).resolve(), utc_timestamp(trained_at)
    groups, sources, source_metadata = load_current_training_cohorts(root, gameplan_run=gameplan_run)
    payload, report = fit_independent_enrichment_model_payload(groups, trained_at=timestamp,
                                source_fingerprint=source_metadata["source_fingerprint"])
    payload["source_publication"] = source_metadata
    payload["model_fingerprint"] = canonical_sha256({key: value for key, value in payload.items() if key != "model_fingerprint"})
    report.update(source_metadata, model_fingerprint=payload["model_fingerprint"])
    independent_model_from_payload(payload)
    run = create_timestamp_directory(root / "ml" / "stock-trader-model-runs", timestamp=timestamp)
    model_path, report_path = run / "model.json", run / "training-report.json"
    _write_json_atomic(model_path, payload)
    _write_json_atomic(report_path, report)
    manifest_path = write_manifest(run, run_timestamp=timestamp, input_files=sources,
        output_files=(model_path.name, report_path.name), model_name=str(payload["model_name"]),
        feature_columns=FEATURES, target_column="exact_long_stock_net_return",
        configuration={"target_contract_version": STOCK_TARGET_CONTRACT_VERSION, "source_fingerprint": source_metadata["source_fingerprint"],
                       "feature_contract_version": INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION,
                       "market_feature_contract": INDEPENDENT_MARKET_FEATURE_CONTRACT,
                       "supported_horizons": report["supported_horizons"], "automatic_activation_allowed": publish_current}, datastore_root=root)
    receipt_path = run / "receipt.json"
    receipt = {"schema_version": "stock-trader-enrichment-training-receipt-v1",
               "run_path": run.relative_to(root).as_posix(), "trained_at": timestamp.isoformat(),
               "model_fingerprint": payload["model_fingerprint"], "manifest_sha256": file_checksum(manifest_path),
               "model_sha256": file_checksum(model_path), "training_report_sha256": file_checksum(report_path),
               "target_contract_version": STOCK_TARGET_CONTRACT_VERSION, "source_fingerprint": source_metadata["source_fingerprint"]}
    _write_json_atomic(receipt_path, receipt)
    if publish_current:
        _write_json_atomic(root / "ml" / "stock-trader-model-latest" / "run.json",
            {"schema_version": ENRICHMENT_MODEL_POINTER_VERSION, "run_path": receipt["run_path"],
             "trained_at": timestamp.isoformat(), "model_fingerprint": payload["model_fingerprint"],
             "manifest_sha256": receipt["manifest_sha256"], "model_sha256": receipt["model_sha256"],
             "receipt_sha256": file_checksum(receipt_path)})
    return run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Train separate exact-target stock horizon enrichment models")
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS))
    location.add_argument("--root-dir", "--datastore", dest="root_dir", type=Path)
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--trained-at")
    parser.add_argument("--gameplan-run", type=Path,
                        help="Read this verified immutable Gameplan instead of following the current pointer.")
    args = parser.parse_args(argv)
    try:
        root = resolve_datastore_dir(root_dir=args.root_dir, target=args.datastore_target)
        with exclusive_runtime_lock(root / "locks" / "stock-trader-training.lock", process_name="stock-trader-training"):
            run = train_and_publish_independent_enrichment_model(root, trained_at=args.trained_at,
                publish_current=not args.stage_only, gameplan_run=args.gameplan_run)
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"status": "STAGED" if args.stage_only else "PUBLISHED", "run_path": str(run),
                      "report_path": str(run / "training-report.json"), "orders_placed": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
