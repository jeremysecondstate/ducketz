from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

import numpy as np

from ml.artifacts import file_checksum, verify_manifest
from ml.stock_trader.contracts import (
    EnrichmentOutput,
    PortfolioState,
    PredictionSignal,
    QuoteState,
    STOCK_TRADER_SYMBOLS,
    canonical_sha256,
    finite,
    utc,
)


ENRICHMENT_MODEL_SCHEMA_VERSION = "stock-trader-enrichment-model-v1"
ENRICHMENT_MODEL_POINTER_VERSION = "stock-trader-enrichment-model-pointer-v1"
ENRICHMENT_FEATURE_NAMES: tuple[str, ...] = (
    "calibrated_probability",
    "signed_signal",
    "signal_strength",
    "probability_4h",
    "probability_1d",
    "probability_1w",
    "horizon_agreement",
    "assumed_round_trip_cost",
    "relative_spread",
    "log_volume",
    "available_cash_fraction",
    "symbol_exposure_fraction",
    "gross_exposure_fraction",
    "held_value_fraction",
    "pending_buy_value_fraction",
    "pending_sell_value_fraction",
    "daily_pnl_fraction",
    "prediction_age_minutes",
    "time_of_day_sin",
    "time_of_day_cos",
    "symbol_AAPL",
    "symbol_AMZN",
    "symbol_GOOG",
    "symbol_MU",
    "symbol_NVDA",
    "symbol_SNDK",
)
_HEAD_LINKS: Mapping[str, str] = {
    "trade_probability": "sigmoid",
    "allocation_fraction": "sigmoid",
    "expected_net_return": "identity",
    "adverse_return": "softplus",
    "execution_urgency": "sigmoid",
    "limit_offset_bps": "softplus",
    "protective_distance_pct": "softplus",
    "expected_holding_minutes": "softplus",
}


class EnrichmentModel(Protocol):
    model_name: str
    model_version: str
    model_fingerprint: str

    def predict(self, feature_values: Mapping[str, float]) -> EnrichmentOutput: ...


@dataclass(frozen=True)
class LinearHead:
    intercept: float
    coefficients: tuple[float, ...]
    link: str


@dataclass(frozen=True)
class LinearEnrichmentModel:
    """Small, deterministic multi-head model used on the hourly critical path.

    The artifact is plain JSON and inference is a few dot products.  Training can
    be replaced later without changing the decision contract.
    """

    model_name: str
    model_version: str
    model_fingerprint: str
    feature_names: tuple[str, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    heads: Mapping[str, LinearHead]

    def predict(self, feature_values: Mapping[str, float]) -> EnrichmentOutput:
        missing = [name for name in self.feature_names if name not in feature_values]
        if missing:
            raise ValueError("Enrichment features are missing: " + ", ".join(missing))
        raw = np.asarray([float(feature_values[name]) for name in self.feature_names])
        if not np.isfinite(raw).all():
            raise ValueError("Enrichment features must be finite")
        scales = np.asarray(self.feature_scales, dtype=float)
        normalized = (raw - np.asarray(self.feature_means, dtype=float)) / scales
        values: dict[str, float] = {}
        for name, required_link in _HEAD_LINKS.items():
            head = self.heads[name]
            if head.link != required_link:
                raise ValueError(f"Enrichment head {name} must use {required_link}")
            score = head.intercept + float(np.dot(normalized, head.coefficients))
            values[name] = _apply_link(score, head.link)
        return EnrichmentOutput(
            model_name=self.model_name,
            model_version=self.model_version,
            model_fingerprint=self.model_fingerprint,
            trade_probability=_clip(values["trade_probability"], 0.0, 1.0),
            allocation_fraction=_clip(values["allocation_fraction"], 0.0, 1.0),
            expected_net_return=values["expected_net_return"],
            adverse_return=max(0.0, values["adverse_return"]),
            execution_urgency=_clip(values["execution_urgency"], 0.0, 1.0),
            limit_offset_bps=max(0.0, values["limit_offset_bps"]),
            protective_distance_pct=max(0.0, values["protective_distance_pct"]),
            expected_holding_minutes=max(1.0, values["expected_holding_minutes"]),
            feature_values={name: float(feature_values[name]) for name in self.feature_names},
        )


def build_feature_values(
    signal: PredictionSignal,
    portfolio: PortfolioState,
    quote: QuoteState,
    *,
    as_of: object,
) -> dict[str, float]:
    timestamp = utc(as_of)
    equity = max(portfolio.account_equity, 1.0)
    reference_price = quote.midpoint
    probabilities = {
        str(name): float(value)
        for name, value in signal.horizon_probabilities.items()
        if finite(value) is not None
    }
    primary_sign = 1.0 if signal.calibrated_probability >= 0.5 else -1.0
    available_signs = [
        1.0 if probability >= 0.5 else -1.0
        for probability in probabilities.values()
    ]
    agreement = (
        sum(sign == primary_sign for sign in available_signs) / len(available_signs)
        if available_signs
        else 1.0
    )
    created = utc(signal.prediction_created_at)
    minute_of_day = timestamp.hour * 60 + timestamp.minute + timestamp.second / 60.0
    angle = 2.0 * math.pi * minute_of_day / (24.0 * 60.0)
    held = max(0.0, float(portfolio.held_shares.get(signal.symbol, 0.0)))
    pending_buy = max(0.0, float(portfolio.pending_buy_shares.get(signal.symbol, 0.0)))
    pending_sell = max(0.0, float(portfolio.pending_sell_shares.get(signal.symbol, 0.0)))
    values = {
        "calibrated_probability": signal.calibrated_probability,
        "signed_signal": 2.0 * signal.calibrated_probability - 1.0,
        "signal_strength": abs(2.0 * signal.calibrated_probability - 1.0),
        "probability_4h": probabilities.get("4h", 0.5),
        "probability_1d": probabilities.get("1d", 0.5),
        "probability_1w": probabilities.get("1w", 0.5),
        "horizon_agreement": agreement,
        "assumed_round_trip_cost": max(0.0, signal.assumed_round_trip_cost),
        "relative_spread": max(0.0, quote.relative_spread),
        "log_volume": math.log1p(max(0.0, quote.volume or 0.0)),
        "available_cash_fraction": max(0.0, portfolio.available_cash) / equity,
        "symbol_exposure_fraction": max(
            0.0, float(portfolio.symbol_exposure.get(signal.symbol, 0.0))
        ) / equity,
        "gross_exposure_fraction": max(0.0, portfolio.gross_exposure) / equity,
        "held_value_fraction": held * reference_price / equity,
        "pending_buy_value_fraction": pending_buy * reference_price / equity,
        "pending_sell_value_fraction": pending_sell * reference_price / equity,
        "daily_pnl_fraction": portfolio.daily_pnl / equity,
        "prediction_age_minutes": max(0.0, (timestamp - created).total_seconds() / 60.0),
        "time_of_day_sin": math.sin(angle),
        "time_of_day_cos": math.cos(angle),
    }
    values.update(
        {
            f"symbol_{symbol}": 1.0 if signal.symbol == symbol else 0.0
            for symbol in STOCK_TRADER_SYMBOLS
        }
    )
    return values


def load_current_enrichment_model(datastore_root: Path) -> LinearEnrichmentModel:
    root = Path(datastore_root).resolve()
    pointer_path = root / "ml" / "stock-trader-model-latest" / "run.json"
    pointer = _read_object(pointer_path, "stock trader model pointer")
    if pointer.get("schema_version") != ENRICHMENT_MODEL_POINTER_VERSION:
        raise ValueError(f"Unsupported stock trader model pointer: {pointer_path}")
    raw_run_path = pointer.get("run_path")
    if not isinstance(raw_run_path, str) or not raw_run_path:
        raise ValueError("Stock trader model pointer has no run_path")
    run = (root / raw_run_path).resolve()
    if not run.is_relative_to(root / "ml" / "stock-trader-model-runs"):
        raise ValueError("Stock trader model pointer escapes the model-runs directory")
    manifest = verify_manifest(run)
    manifest_path = run / "manifest.json"
    if pointer.get("manifest_sha256") != file_checksum(manifest_path):
        raise ValueError("Stock trader model pointer manifest checksum does not match")
    model_path = run / "model.json"
    outputs = manifest.get("output_files")
    if not isinstance(outputs, Mapping) or "model.json" not in outputs:
        raise ValueError("Stock trader model manifest does not publish model.json")
    if pointer.get("model_sha256") != file_checksum(model_path):
        raise ValueError("Stock trader model pointer model checksum does not match")
    payload = _read_object(model_path, "stock trader enrichment model")
    model = model_from_payload(payload)
    if pointer.get("model_fingerprint") != model.model_fingerprint:
        raise ValueError("Stock trader model pointer fingerprint does not match")
    receipt_path = run / "receipt.json"
    if pointer.get("receipt_sha256") != file_checksum(receipt_path):
        raise ValueError("Stock trader model pointer receipt checksum does not match")
    receipt = _read_object(receipt_path, "stock trader model receipt")
    if (
        receipt.get("manifest_sha256") != pointer.get("manifest_sha256")
        or receipt.get("model_sha256") != pointer.get("model_sha256")
        or receipt.get("model_fingerprint") != model.model_fingerprint
    ):
        raise ValueError("Stock trader model receipt differs from its pointer")
    return model


def model_from_payload(payload: Mapping[str, object]) -> LinearEnrichmentModel:
    if payload.get("schema_version") != ENRICHMENT_MODEL_SCHEMA_VERSION:
        raise ValueError("Unsupported stock trader enrichment model schema")
    feature_names = tuple(str(value) for value in _sequence(payload.get("feature_names")))
    if feature_names != ENRICHMENT_FEATURE_NAMES:
        raise ValueError("Stock trader enrichment model feature contract differs")
    means = _float_tuple(payload.get("feature_means"), "feature_means")
    scales = _float_tuple(payload.get("feature_scales"), "feature_scales")
    if len(means) != len(feature_names) or len(scales) != len(feature_names):
        raise ValueError("Stock trader model normalization dimensions differ")
    if any(value <= 0.0 for value in scales):
        raise ValueError("Stock trader model feature scales must be positive")
    raw_heads = payload.get("heads")
    if not isinstance(raw_heads, Mapping) or set(raw_heads) != set(_HEAD_LINKS):
        raise ValueError("Stock trader model must publish every required head")
    heads: dict[str, LinearHead] = {}
    for name, required_link in _HEAD_LINKS.items():
        raw_head = raw_heads.get(name)
        if not isinstance(raw_head, Mapping):
            raise ValueError(f"Stock trader model head {name} is malformed")
        coefficients = _float_tuple(raw_head.get("coefficients"), f"{name}.coefficients")
        if len(coefficients) != len(feature_names):
            raise ValueError(f"Stock trader model head {name} dimensions differ")
        link = str(raw_head.get("link") or "")
        if link != required_link:
            raise ValueError(f"Stock trader model head {name} must use {required_link}")
        intercept = finite(raw_head.get("intercept"))
        if intercept is None:
            raise ValueError(f"Stock trader model head {name} intercept is invalid")
        heads[name] = LinearHead(intercept, coefficients, link)
    fingerprint_payload = dict(payload)
    observed_fingerprint = str(fingerprint_payload.pop("model_fingerprint", ""))
    expected_fingerprint = canonical_sha256(fingerprint_payload)
    if observed_fingerprint != expected_fingerprint:
        raise ValueError("Stock trader model fingerprint does not match its payload")
    model_name = str(payload.get("model_name") or "").strip()
    model_version = str(payload.get("model_version") or "").strip()
    if not model_name or not model_version:
        raise ValueError("Stock trader model name and version are required")
    return LinearEnrichmentModel(
        model_name=model_name,
        model_version=model_version,
        model_fingerprint=observed_fingerprint,
        feature_names=feature_names,
        feature_means=means,
        feature_scales=scales,
        heads=heads,
    )


def _apply_link(value: float, link: str) -> float:
    if link == "identity":
        return value
    if link == "sigmoid":
        if value >= 0:
            return 1.0 / (1.0 + math.exp(-value))
        exp_value = math.exp(value)
        return exp_value / (1.0 + exp_value)
    if link == "softplus":
        if value > 30.0:
            return value
        if value < -30.0:
            return math.exp(value)
        return math.log1p(math.exp(value))
    raise ValueError(f"Unsupported enrichment link: {link}")


def _clip(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("Expected a JSON array")
    return value


def _float_tuple(value: object, label: str) -> tuple[float, ...]:
    output: list[float] = []
    for raw in _sequence(value):
        number = finite(raw)
        if number is None:
            raise ValueError(f"Stock trader model {label} contains a non-finite value")
        output.append(number)
    return tuple(output)


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is not an object: {path}")
    return payload


__all__ = [
    "ENRICHMENT_FEATURE_NAMES",
    "ENRICHMENT_MODEL_POINTER_VERSION",
    "ENRICHMENT_MODEL_SCHEMA_VERSION",
    "EnrichmentModel",
    "LinearEnrichmentModel",
    "build_feature_values",
    "load_current_enrichment_model",
    "model_from_payload",
]
