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
from ml.stock_trader.market_features import INDEPENDENT_MARKET_FEATURE_NAMES, frozen_market_feature_values


ENRICHMENT_MODEL_SCHEMA_VERSION = "stock-trader-enrichment-model-v1"
ENRICHMENT_MODEL_POINTER_VERSION = "stock-trader-enrichment-model-pointer-v1"
INDEPENDENT_ENRICHMENT_SCHEMA_VERSION = "stock-trader-independent-enrichment-model-v1"
INDEPENDENT_ENRICHMENT_FEATURE_NAMES: tuple[str, ...] = (
    "assumed_round_trip_cost", "log_target_duration_minutes",
    "target_clock_sin", "target_clock_cos", "target_weekday_sin", "target_weekday_cos",
    *(f"symbol_{symbol}" for symbol in STOCK_TRADER_SYMBOLS),
)
INDEPENDENT_MARKET_ENRICHMENT_FEATURE_NAMES = (*INDEPENDENT_ENRICHMENT_FEATURE_NAMES, *INDEPENDENT_MARKET_FEATURE_NAMES)
INDEPENDENT_ENRICHMENT_FEATURE_CONTRACT_VERSION = "independent-stock-enrichment-market-features-v1"
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
    *(f"symbol_{symbol}" for symbol in STOCK_TRADER_SYMBOLS),
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
    supported_horizons: tuple[str, ...] = ("1h",)
    qualified_target_contracts: tuple[str, ...] = ()

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
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
    if signal.target_definition_version == STOCK_TARGET_CONTRACT_VERSION:
        values.update(independent_target_feature_values(
            symbol=signal.symbol, horizon=signal.primary_horizon,
            start=signal.target_window_start, end=signal.target_window_end,
            cost=signal.assumed_round_trip_cost,
        ))
        market_values = getattr(signal, "enrichment_feature_values", {})
        if market_values:
            values.update(frozen_market_feature_values(market_values))
    return values


def load_current_enrichment_model(datastore_root: Path) -> EnrichmentModel:
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
    if payload.get("schema_version") == INDEPENDENT_ENRICHMENT_SCHEMA_VERSION:
        from ml.stock_trader.independent_training import verify_independent_model_sources
        verify_independent_model_sources(root, payload, manifest)
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


def model_from_payload(payload: Mapping[str, object]) -> EnrichmentModel:
    if payload.get("schema_version") == INDEPENDENT_ENRICHMENT_SCHEMA_VERSION:
        from ml.stock_trader.independent_training import independent_model_from_payload
        return independent_model_from_payload(payload)
    if payload.get("schema_version") != ENRICHMENT_MODEL_SCHEMA_VERSION:
        raise ValueError("Unsupported stock trader enrichment model schema")
    training = payload.get("training")
    if isinstance(training, Mapping):
        if tuple(training.get("supported_horizons", ("1h",))) != ("1h",):
            raise ValueError("Hourly enrichment v1 cannot claim unsupported horizon qualification")
        if training.get("qualified_target_contracts"):
            raise ValueError("Hourly enrichment v1 cannot claim independent target-contract qualification")
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


def enrichment_signal_readiness(
    model: EnrichmentModel,
    signal: PredictionSignal,
) -> dict[str, object]:
    """Report whether fitted enrichment evidence covers this exact signal scope.

The existing v1 trainer uses prospective hourly outcomes and constant 60-minute
holding targets. It supplies no duration-qualified independent-target evidence,
including for the independent hourly route. A new direction model alone cannot
extend that enrichment authority to another target contract or horizon.
"""

    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION

    if isinstance(model, IndependentEnrichmentModel):
        return model.signal_readiness(signal)

    horizon = str(signal.primary_horizon)
    target_contract = str(signal.target_definition_version)
    duration = (utc(signal.target_window_end) - utc(signal.target_window_start)).total_seconds() / 60.0
    if target_contract == STOCK_TARGET_CONTRACT_VERSION:
        reason = "ENRICHMENT_INDEPENDENT_TARGET_CONTRACT_NOT_QUALIFIED"
    elif horizon not in model.supported_horizons:
        reason = "ENRICHMENT_HORIZON_NOT_QUALIFIED"
    elif duration != 60.0:
        reason = "ENRICHMENT_TARGET_DURATION_NOT_QUALIFIED"
    else:
        reason = "ENRICHMENT_HOURLY_SCOPE_SUPPORTED"
    return {
        "status": "READY" if reason == "ENRICHMENT_HOURLY_SCOPE_SUPPORTED" else "NOT_READY",
        "reason": reason,
        "model_fingerprint": model.model_fingerprint,
        "requested_horizon": horizon,
        "requested_target_contract": target_contract,
        "requested_duration_minutes": duration,
        "supported_horizons": list(model.supported_horizons),
        "qualified_target_contracts": list(model.qualified_target_contracts),
        "holding_target_minutes": 60.0,
        "qualification_basis": "Existing v1 enrichment fits hourly outcomes with constant 60-minute holding targets and has no independent-target qualification.",
    }


def require_enrichment_signal_support(model: EnrichmentModel, signal: PredictionSignal) -> None:
    """Refuse applying hourly fitted sizing evidence to unqualified targets."""

    readiness = enrichment_signal_readiness(model, signal)
    if readiness["status"] != "READY":
        raise ValueError(
            f"{readiness['reason']}: {signal.symbol}/{signal.primary_horizon} "
            f"requires {signal.target_definition_version or 'unspecified target contract'} "
            f"and {readiness['requested_duration_minutes']:g} minutes; current enrichment "
            f"does not supply qualified fitted evidence for that exact scope ({readiness['qualification_basis']})"
        )


def independent_target_feature_values(*, symbol: str, horizon: str, start: object,
                                      end: object, cost: float) -> dict[str, float]:
    """Features available before entry; no invented historical quote/portfolio inputs."""
    from ml.independent_stock_targets import GROUPS, STOCK_TIMEZONE
    left, right = utc(start), utc(end)
    minutes = (right - left).total_seconds() / 60.0
    if horizon not in GROUPS or minutes <= 0 or symbol not in STOCK_TRADER_SYMBOLS:
        raise ValueError("Invalid independent enrichment target features")
    local = left.tz_convert(STOCK_TIMEZONE)
    angle = 2 * math.pi * (local.hour * 60 + local.minute) / 1440
    weekday = 2 * math.pi * local.weekday() / 7
    return {
        "assumed_round_trip_cost": max(0.0, float(cost)),
        "log_target_duration_minutes": math.log(minutes),
        "target_clock_sin": math.sin(angle), "target_clock_cos": math.cos(angle),
        "target_weekday_sin": math.sin(weekday), "target_weekday_cos": math.cos(weekday),
        **{f"symbol_{item}": float(item == symbol) for item in STOCK_TRADER_SYMBOLS},
        **{f"independent_horizon_{item}": float(item == horizon) for item in GROUPS},
        "target_duration_minutes": minutes, "target_anchor_hour": float(local.hour),
    }


def independent_scope_key(symbol: str, horizon: str, start: object, end: object) -> str:
    from ml.independent_stock_targets import STOCK_TIMEZONE
    left, right = utc(start), utc(end)
    local = left.tz_convert(STOCK_TIMEZONE)
    route = f"{horizon}@{local.hour:02d}:00" if horizon in {"1h", "4h"} else f"{horizon}@D+{1 if horizon == '1d' else 5}"
    duration = (right - left).total_seconds() / 60
    return f"{symbol}|{route}|{duration:g}m"


@dataclass(frozen=True)
class IndependentEnrichmentModel:
    """Distinct horizon fits whose execution support is derived from held-out evidence."""

    model_name: str
    model_version: str
    model_fingerprint: str
    horizon_models: Mapping[str, LinearEnrichmentModel]
    calibration: Mapping[str, tuple[float, float]]
    scope_readiness: Mapping[str, Mapping[str, object]]

    @property
    def supported_horizons(self) -> tuple[str, ...]:
        from ml.independent_stock_targets import GROUPS
        return tuple(group for group in GROUPS if any(
            value.get("status") == "READY" and key.split("|", 2)[1].startswith(group + "@")
            for key, value in self.scope_readiness.items()))

    @property
    def qualified_target_contracts(self) -> tuple[str, ...]:
        from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
        return (STOCK_TARGET_CONTRACT_VERSION,) if self.supported_horizons else ()

    def signal_readiness(self, signal: PredictionSignal) -> dict[str, object]:
        from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, STOCK_TIMEZONE, stock_target_windows
        duration = (utc(signal.target_window_end) - utc(signal.target_window_start)).total_seconds() / 60
        reason = "ENRICHMENT_EXACT_SCOPE_NOT_QUALIFIED"
        scope = independent_scope_key(signal.symbol, signal.primary_horizon, signal.target_window_start, signal.target_window_end)
        support = self.scope_readiness.get(scope, {})
        fitted = self.horizon_models.get(signal.primary_horizon)
        requires_market = fitted is not None and any(name in fitted.feature_names for name in INDEPENDENT_MARKET_FEATURE_NAMES)
        market_ready = True
        if requires_market:
            try:
                frozen_market_feature_values(getattr(signal, "enrichment_feature_values", {}))
            except (ValueError, TypeError, AttributeError):
                market_ready = False
        valid_window = False
        try:
            valid_window = any(
                spec["execution_eligible"] and spec["model_group"] == signal.primary_horizon
                and utc(spec["target_window_start"]) == utc(signal.target_window_start)
                and utc(spec["target_window_end"]) == utc(signal.target_window_end)
                for spec in stock_target_windows(utc(signal.target_window_start).tz_convert(STOCK_TIMEZONE).date()))
        except (ValueError, TypeError):
            pass
        if signal.target_definition_version != STOCK_TARGET_CONTRACT_VERSION:
            reason = "ENRICHMENT_TARGET_CONTRACT_MISMATCH"
        elif not valid_window:
            reason = "ENRICHMENT_EXACT_TARGET_WINDOW_INVALID"
        elif support and getattr(signal, "target_price_source_contract", "") != support.get("target_price_source_contract"):
            reason = "ENRICHMENT_TARGET_PRICE_SOURCE_MISMATCH"
        elif not market_ready:
            reason = "ENRICHMENT_MARKET_FEATURES_MISSING_OR_NONFINITE"
        elif support.get("status") == "READY":
            reason = "ENRICHMENT_INDEPENDENT_EXACT_SCOPE_SUPPORTED"
        return {
            "status": "READY" if reason == "ENRICHMENT_INDEPENDENT_EXACT_SCOPE_SUPPORTED" else "NOT_READY",
            "reason": reason, "scope": scope, "evidence": dict(support),
            "model_fingerprint": self.model_fingerprint, "requested_horizon": signal.primary_horizon,
            "requested_target_contract": signal.target_definition_version,
            "requested_duration_minutes": duration, "supported_horizons": list(self.supported_horizons),
            "requested_target_price_source": getattr(signal, "target_price_source_contract", ""),
            "required_market_feature_names": list(INDEPENDENT_MARKET_FEATURE_NAMES) if requires_market else [],
            "qualified_target_contracts": list(self.qualified_target_contracts),
            "qualification_basis": "Separate exact-target long-stock fits and untouched chronological assessment, by symbol/route/elapsed duration.",
        }

    def predict(self, feature_values: Mapping[str, float]) -> EnrichmentOutput:
        from dataclasses import replace
        from ml.independent_stock_targets import GROUPS
        groups = [name for name in GROUPS if feature_values.get(f"independent_horizon_{name}") == 1.0]
        if len(groups) != 1 or groups[0] not in self.horizon_models:
            raise ValueError("Independent enrichment requires a fitted horizon and exact target features")
        group = groups[0]
        result = self.horizon_models[group].predict(feature_values)
        probability = min(1 - 1e-12, max(1e-12, result.trade_probability))
        slope, intercept = self.calibration[group]
        calibrated = _apply_link(slope * math.log(probability / (1 - probability)) + intercept, "sigmoid")
        # Expiry is a contractual quantity, never an estimated 60-minute proxy.
        return replace(result, model_name=self.model_name, model_version=self.model_version,
                       model_fingerprint=self.model_fingerprint, trade_probability=calibrated,
                       expected_holding_minutes=float(feature_values["target_duration_minutes"]))


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
    "enrichment_signal_readiness",
    "load_current_enrichment_model",
    "model_from_payload",
    "require_enrichment_signal_support",
]
