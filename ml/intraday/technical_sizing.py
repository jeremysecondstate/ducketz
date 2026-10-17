"""Pure, configurable research sizing from an original forecast's modern bars.

There are no production recipes or hourly-reference bindings here. This module
never acquires data, reads accounts, reserves capacity, or submits orders.
Configured coefficients are explicit research parameters, not historically
verified point-in-time parameters or a trained sizing model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
import json
import math
from numbers import Real
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from technicals.hyperliquid_features import SharedCalculations
from .data import validate_candles
from .prediction import ContractError, STEP, TARGET_VERSION, UnresolvedPolicy, digest, encoded, identity, instant, number
from .sizing import size


VERSION = "intraday-transparent-technical-sizing-research-v2"


def _finite(value, label):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ContractError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return result


def _period(value):
    if type(value) is not int or value < 1:
        raise ContractError("Indicator periods must be positive integers")
    return value


def _clean(value):
    """JSON copies preserve all row provenance; optional missing fields are null."""
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return instant(value.to_pydatetime() if isinstance(value, pd.Timestamp) else value).isoformat()
    if isinstance(value, np.generic):
        return _clean(value.item())
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class IndicatorSpec:
    name: str
    kind: str
    parameters: Mapping[str, object]
    derivatives: bool = False

    def validate(self):
        identity(self.name)
        if not self.name.replace("_", "").isalnum() or type(self.derivatives) is not bool:
            raise ContractError("Indicator names use letters, digits and underscores")
        required = {
            "ema": {"span", "adjust"}, "sma": {"window"},
            "rsi": {"window", "smoothing"},
            "bollinger": {"window", "deviations", "ddof"},
            "stochastic": {"window", "k_smoothing", "d_smoothing"},
            "momentum": {"lag"}, "vwap": {"window"},
            "atr": {"window", "smoothing"},
            "relative_volume": {"window", "baseline"},
        }
        if self.kind not in required or set(self.parameters) != required[self.kind]:
            raise ContractError("Unknown indicator or incomplete/unknown parameters")
        for key in {"window", "span", "lag", "k_smoothing", "d_smoothing"} & set(self.parameters):
            _period(self.parameters[key])
        if self.kind == "ema" and type(self.parameters["adjust"]) is not bool:
            raise ContractError("EMA adjust must be explicit boolean; seed is first observation")
        if self.kind in {"rsi", "atr"} and self.parameters["smoothing"] not in {"simple", "ewm_first"}:
            raise ContractError("Smoothing must be simple or first-observation EWM")
        if self.kind == "bollinger":
            if _finite(self.parameters["deviations"], "Band deviations") <= 0:
                raise ContractError("Band deviations must be positive")
            if type(self.parameters["ddof"]) is not int or self.parameters["ddof"] not in {0, 1}:
                raise ContractError("Bollinger ddof must be 0 or 1")
        if self.kind == "relative_volume" and self.parameters["baseline"] not in {"previous", "inclusive"}:
            raise ContractError("Relative-volume baseline must be previous or inclusive")
        return self


@dataclass(frozen=True)
class WindowPolicy:
    # Both are research choices; neither silently fills gaps or closures.
    basis: str  # contiguous_session or observed_history
    max_bars: int | None = None

    def validate(self):
        if self.basis not in {"contiguous_session", "observed_history"}:
            raise ContractError("An explicit indicator window basis is required")
        if self.max_bars is not None:
            _period(self.max_bars)
        return self


@dataclass(frozen=True)
class ScoreTerm:
    feature: str
    center: float
    scale: float
    clip_min: float
    clip_max: float
    weight: float
    scale_feature: str | None = None

    def validate(self):
        identity(self.feature)
        values = {name: _finite(getattr(self, name), name)
                  for name in ("center", "scale", "clip_min", "clip_max", "weight")}
        if values["scale"] <= 0 or values["clip_min"] > values["clip_max"]:
            raise ContractError("Normalization requires positive scale and ordered bounds")
        if self.scale_feature is not None:
            identity(self.scale_feature)
        return self


@dataclass(frozen=True)
class ScoreRecipe:
    version: str
    terms: tuple[ScoreTerm, ...]
    bias: float
    output_min: float
    output_max: float
    sum_divisor: float = 1.0

    def validate(self):
        identity(self.version)
        if not self.terms or len({term.feature for term in self.terms}) != len(self.terms):
            raise ContractError("Recipe requires unique explicit feature terms")
        for term in self.terms:
            term.validate()
        lo, hi = _finite(self.output_min, "Output minimum"), _finite(self.output_max, "Output maximum")
        _finite(self.bias, "Score bias")
        if _finite(self.sum_divisor, "Score sum divisor") <= 0:
            raise ContractError("Score sum divisor must be positive")
        if not 0 <= lo <= hi < 1:
            raise ContractError("Score output bounds must be nonnegative and below one")
        return self


@dataclass(frozen=True)
class SymbolSizingPolicy:
    symbol: str
    version: str
    indicators: tuple[IndicatorSpec, ...]
    window: WindowPolicy
    buy: ScoreRecipe | None = None
    sell: ScoreRecipe | None = None

    def validate(self):
        identity(self.symbol)
        identity(self.version)
        self.window.validate()
        if not self.indicators or len({spec.name for spec in self.indicators}) != len(self.indicators):
            raise ContractError("Symbol policy requires unique explicit indicators")
        for spec in self.indicators:
            spec.validate()
        for recipe in (self.buy, self.sell):
            if recipe is not None:
                recipe.validate()
        return self

    @classmethod
    def from_payload(cls, value):
        def recipe(item):
            return None if item is None else ScoreRecipe(
                **{**item, "terms": tuple(ScoreTerm(**term) for term in item["terms"])})
        return cls(**{**value,
            "indicators": tuple(IndicatorSpec(**item) for item in value["indicators"]),
            "window": WindowPolicy(**value["window"]),
            "buy": recipe(value.get("buy")), "sell": recipe(value.get("sell"))}).validate()


@dataclass(frozen=True)
class HourlyReference:
    symbol: str
    side: str
    quantity: int
    version: str
    available_at: str
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class SymbolReference:
    """Independent decimal share scale; no scheduled hourly order is required."""
    symbol: str
    side: str
    shares: str
    version: str
    available_at: str
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class AvailableInput:
    value: float
    available_at: str
    source: str
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class IndicatorSnapshot:
    snapshot_id: str
    payload_json: str

    def payload(self):
        value = json.loads(self.payload_json)
        if digest(value) != self.snapshot_id:
            raise ContractError("Indicator snapshot evidence changed")
        return value


@dataclass(frozen=True)
class SizingResult:
    result_id: str
    payload_json: str

    def payload(self):
        value = json.loads(self.payload_json)
        if digest(value) != self.result_id:
            raise ContractError("Sizing result evidence changed")
        return value

    @property
    def status(self):
        return self.payload()["status"]

    @property
    def desired_quantity(self):
        return self.payload()["desired_quantity"]

    @property
    def adjusted_quantity(self):
        return None


def _records(frame):
    rows = []
    for row in frame.to_dict(orient="records"):
        for name in ("opened_at", "available_at", "session_open", "session_close"):
            if name in row and row[name] is not None and not pd.isna(row[name]):
                row[name] = instant(row[name].to_pydatetime() if isinstance(row[name], pd.Timestamp) else row[name]).isoformat()
        for name in ("open", "high", "low", "close", "volume"):
            row[name] = float(row[name])
        rows.append(_clean(row))
    return rows


def _ratio(left, right):
    return left.div(right.where(right.ne(0))).replace([np.inf, -np.inf], np.nan)


def _indicators(frame, specs):
    c = SharedCalculations(frame)
    close = c.series("close")
    outputs = {}
    for spec in specs:
        p, kind, name = spec.parameters, spec.kind, spec.name
        window = p.get("window")
        values = {}
        if kind in {"ema", "sma", "vwap"}:
            if kind == "ema":
                level = c.ema("close", p["span"], adjust=p["adjust"])
            elif kind == "sma":
                level = c.rolling("close", window, "mean")
            else:
                typical = (c.series("high") + c.series("low") + close) / 3
                level = _ratio((typical * c.series("volume")).rolling(window, min_periods=window).sum(),
                               c.rolling("volume", window, "sum"))
            values = {"level": level, "distance": _ratio(close, level) - 1}
        elif kind == "rsi":
            gains, losses = close.diff().clip(lower=0), -close.diff().clip(upper=0)
            if p["smoothing"] == "simple":
                gain, loss = gains.rolling(window, min_periods=window).mean(), losses.rolling(window, min_periods=window).mean()
            else:
                gain = gains.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
                loss = losses.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
            values = {"value": (100 * _ratio(gain, gain + loss)).mask(gain.eq(0) & loss.eq(0), 50)}
        elif kind == "bollinger":
            mid = c.rolling("close", window, "mean")
            deviation = c.rolling("close", window, "std", ddof=p["ddof"]) * p["deviations"]
            lower, upper = mid - deviation, mid + deviation
            values = {"mid": mid, "lower": lower, "upper": upper,
                      "width": _ratio(upper - lower, mid), "percent_b": _ratio(close - lower, upper - lower)}
        elif kind == "stochastic":
            low = c.rolling("low", window, "min")
            raw = _ratio(close - low, c.rolling("high", window, "max") - low)
            k = raw.rolling(p["k_smoothing"], min_periods=p["k_smoothing"]).mean()
            values = {"raw_k": raw, "k": k, "d": k.rolling(p["d_smoothing"], min_periods=p["d_smoothing"]).mean()}
        elif kind == "momentum":
            values = {"value": c.pct_change("close", p["lag"])}
        elif kind == "atr":
            tr = c.true_range()
            atr = (tr.rolling(window, min_periods=window).mean() if p["smoothing"] == "simple"
                   else tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean())
            values = {"level": atr, "relative": _ratio(atr, close)}
        elif kind == "relative_volume":
            volume = c.series("volume")
            baseline = volume.shift(1) if p["baseline"] == "previous" else volume
            values = {"value": _ratio(volume, baseline.rolling(window, min_periods=window).mean())}
        for suffix, series in values.items():
            key = f"{name}.{suffix}"
            outputs[key] = series
            if spec.derivatives:
                outputs[key + ".rate"] = series.diff()
                outputs[key + ".acceleration"] = series.diff().diff()
    return pd.DataFrame(outputs, index=frame.index)


def _indicator_config(specs, window):
    return _clean({"indicators": [asdict(spec) for spec in specs], "window": asdict(window)})


def _frozen_synthetic_policy(snapshot):
    """Read the issued forecast's population choice, never current PC settings."""
    scope = snapshot.get("dataset_scope")
    manifest = snapshot.get("manifest")
    if scope is not None and not isinstance(scope, Mapping):
        raise ContractError("Frozen sizing dataset scope is invalid")
    declared = []
    for basis, values in (("dataset_scope", scope), ("manifest", manifest)):
        if isinstance(values, Mapping) and "synthetic_policy" in values:
            policy = values["synthetic_policy"]
            if policy is None and basis == "manifest":
                continue  # Legacy unrecorded population retains its original include behavior.
            if not isinstance(policy, str) or policy not in {"include", "exclude"}:
                raise ContractError("Frozen sizing synthetic policy is invalid")
            declared.append((policy, basis))
    if len({policy for policy, _ in declared}) > 1:
        raise ContractError("Frozen sizing population declarations disagree")
    return declared[0] if declared else ("include", "legacy_unrecorded")


def indicator_snapshot(candles, *, symbol, reference_at, data_cutoff, indicator_specs,
                       window_policy, forecast_snapshot, forecast_snapshot_hash):
    """Calculate solely from a verified frozen runtime snapshot's modern bars.

    ``candles`` may be that snapshot's records or a frame containing additional
    future rows; admitted rows must match the frozen original byte semantics.
    No later price revision can replace the information used by the forecast.
    The frozen synthetic policy is applied before indicators and their warmup,
    so an observed-only forecast cannot acquire synthetic sizing inputs.
    """
    identity(symbol)
    reference, cutoff = instant(reference_at), instant(data_cutoff)
    if digest(forecast_snapshot) != forecast_snapshot_hash:
        raise ContractError("Original forecast snapshot hash changed")
    if instant(forecast_snapshot["cutoff"]) != cutoff or instant(forecast_snapshot["inference"]["reference_at"]) != reference:
        raise ContractError("Sizing must use the original forecast cutoff and reference")
    if forecast_snapshot["inference"]["symbol"] != symbol:
        raise ContractError("Original forecast snapshot belongs to another symbol")
    specs = tuple(spec.validate() for spec in indicator_specs)
    window_policy.validate()
    if not specs or len({spec.name for spec in specs}) != len(specs):
        raise ContractError("Unique explicit indicator specifications required")
    config = _indicator_config(specs, window_policy)
    synthetic_policy, population_basis = _frozen_synthetic_policy(forecast_snapshot)
    value = {"version": VERSION, "symbol": symbol, "reference_at": reference.isoformat(),
             "data_cutoff": cutoff.isoformat(), "forecast_snapshot_hash": forecast_snapshot_hash,
             "indicator_config": config, "indicator_config_hash": digest(config),
             "input_population": {"synthetic_policy": synthetic_policy, "basis": population_basis},
             "status": "unavailable", "features": {}, "candles": [], "reason": None}
    try:
        def admitted(rows):
            frame = pd.DataFrame(rows)
            opened = pd.Series([pd.Timestamp(instant(v.to_pydatetime() if isinstance(v, pd.Timestamp) else v))
                                for v in frame["opened_at"]], index=frame.index)
            available = pd.Series([pd.Timestamp(instant(v.to_pydatetime() if isinstance(v, pd.Timestamp) else v))
                                   for v in frame["available_at"]], index=frame.index)
            return validate_candles(frame.loc[available.le(pd.Timestamp(cutoff)) & opened.le(pd.Timestamp(reference))])
        frame = admitted(candles)
        frozen = admitted(forecast_snapshot["candles"])
    except (ContractError, ValueError, KeyError) as exc:
        value["reason"] = "invalid_or_unavailable_modern_candles: " + str(exc)
        return IndicatorSnapshot(digest(value), encoded(value))
    if _records(frame) != _records(frozen):
        raise ContractError("Sizing candles differ from the original forecast snapshot")
    if frame.empty or frame.symbol.nunique() != 1 or frame.symbol.iloc[0] != symbol or frame.source_id.nunique() != 1:
        value["reason"] = "one_symbol_and_source_required"
    elif frame.opened_at.iloc[-1] != pd.Timestamp(reference):
        value["reason"] = "original_completed_reference_unavailable"
    else:
        admitted = len(frame)
        excluded_synthetic = int(frame.synthetic.sum()) if synthetic_policy == "exclude" else 0
        if synthetic_policy == "exclude":
            frame = frame.loc[~frame.synthetic].copy()
            if frame.empty or frame.opened_at.iloc[-1] != pd.Timestamp(reference):
                value["reason"] = "original_observed_reference_unavailable"
                return IndicatorSnapshot(digest(value), encoded(value))
        population_rows = len(frame)
        if window_policy.basis == "contiguous_session":
            frame = frame.loc[frame.session_id.eq(frame.session_id.iloc[-1])].copy()
            breaks = frame.opened_at.diff().ne(pd.Timedelta(STEP))
            frame = frame.loc[breaks.cumsum().eq(breaks.cumsum().iloc[-1])].copy()
        if window_policy.max_bars is not None:
            frame = frame.tail(window_policy.max_bars).copy()
        values = _indicators(frame, specs)
        # Runtime also freezes its unrounded reference-price observation in
        # inference. Preserve that value even if its ancillary candle JSON was
        # serialized at lower numeric precision; never change the issued anchor.
        inference = forecast_snapshot["inference"]
        reference_price = inference.get("reference_open", frame.open.iloc[-1])
        reference_price = str(number(reference_price, positive=True))
        if (inference.get("source", str(frame.source_id.iloc[0])) != str(frame.source_id.iloc[0])
                or inference.get("source_dataset", str(frame.source_dataset.iloc[0])) != str(frame.source_dataset.iloc[0])):
            raise ContractError("Frozen inference and candle sources disagree")
        value.update(status="ready", features=_clean(values.iloc[-1].to_dict()), candles=_records(frame),
                     reference_open=reference_price,
                     reference_price_basis="frozen_inference" if "reference_open" in inference else "frozen_bar",
                     source_id=str(frame.source_id.iloc[0]), source_dataset=str(frame.source_dataset.iloc[0]),
                     information_available_at=frame.available_at.max().isoformat(),
                     provenance={"used_rows": len(frame), "input_candle_rows": admitted,
                        "excluded_synthetic_rows": excluded_synthetic,
                        "excluded_window_rows": population_rows - len(frame),
                        "synthetic_rows": int(frame.synthetic.sum()),
                        "availability_kinds": sorted(frame.availability_kind.unique().tolist()),
                        "session_ids": frame.session_id.drop_duplicates().tolist(),
                        "elapsed_minutes_previous": float(frame.opened_at.diff().dt.total_seconds().iloc[-1] / 60) if len(frame) > 1 else None,
                        "elapsed_minutes_total": float((frame.opened_at.iloc[-1] - frame.opened_at.iloc[0]).total_seconds() / 60),
                        "gaps_or_closures": int(frame.opened_at.diff().dropna().ne(pd.Timedelta(STEP)).sum()),
                        "missing_policy": "no_filling", "synthetic_policy": (
                            "exclude_before_indicators" if synthetic_policy == "exclude" else "include_validated_with_original_evidence"),
                        "synthetic_policy_basis": population_basis,
                        "derivative_basis": "first_and_second_differences_per_observed_step"})
        value["candles_hash"] = digest(value["candles"])
    return IndicatorSnapshot(digest(value), encoded(value))


def evaluate_size(forecast, indicators, *, symbol_policy=None, hourly_reference=None,
                  symbol_reference=None, account_inputs=None, calculated_at,
                  half_share_ties=None, rounded_equality=None, unavailable_reason=None):
    """Return immutable desired sizing evidence; capacity remains uncomputed.

    Missing policy/reference/feature/rounding decisions return null quantities.
    A genuine computed zero remains zero. This result is separate sizing
    evidence, not a directly compatible ShadowStore intent or order request.
    """
    saved = dict(forecast)
    forecast_id = saved.pop("forecast_id", None)
    if forecast_id != digest(saved):
        raise ContractError("Sizing requires an original immutable forecast")
    probability = number(saved["probability_not_down"])
    if probability > 1 or saved["target_version"] != TARGET_VERSION:
        raise ContractError("Forecast probability or target contract is invalid")
    at, reference = instant(calculated_at), instant(saved["reference_at"])
    if (instant(saved["issued_at"]) > at or instant(saved["issued_at"]) < reference + STEP
            or instant(saved["training_cutoff"]) > instant(saved["issued_at"])
            or instant(saved["data_through"]) != reference + STEP
            or instant(saved["target_open"]) != reference + 2 * STEP
            or instant(saved["target_close"]) != reference + 3 * STEP):
        raise ContractError("Sizing cannot move an exact target or precede its forecast")
    side = "BUY" if probability >= number("0.50") else "SELL"
    snapshot = indicators.payload()
    if (snapshot["forecast_snapshot_hash"] != saved["snapshot_hash"] or snapshot["symbol"] != saved["symbol"]
            or instant(snapshot["reference_at"]) != reference or instant(snapshot["data_cutoff"]) != instant(saved["training_cutoff"])):
        raise ContractError("Sizing indicator evidence differs from the original forecast")
    if snapshot["status"] == "ready":
        if (number(saved["reference_open"]) != number(snapshot["reference_open"])
                or saved.get("source_id") != snapshot["source_id"] or saved.get("source_dataset") != snapshot["source_dataset"]):
            raise ContractError("Sizing reference price or source differs from its forecast")
    value = {"version": VERSION, "forecast_id": forecast_id, "symbol": saved["symbol"], "side": side,
             "reference_at": saved["reference_at"], "target_open": saved["target_open"], "target_close": saved["target_close"],
             "calculated_at": at.isoformat(), "indicator_snapshot_id": indicators.snapshot_id,
             "forecast_snapshot_hash": saved["snapshot_hash"], "status": "unresolved", "reason": None,
             "desired_quantity": None, "adjusted_quantity": None, "execution_disposition": "sizing_unresolved",
             "orders_enabled": False, "score": None, "term_contributions": [],
             "calculated_after_target": at >= instant(saved["target_close"]),
             "parameter_availability": "supplied_research_configuration_not_verified_historically"}
    def finish(status, reason):
        value.update(status=status, reason=reason)
        return SizingResult(digest(value), encoded(value))
    if symbol_policy is None:
        return finish("unresolved", "symbol_sizing_policy_missing")
    symbol_policy.validate()
    value.update(policy=_clean(asdict(symbol_policy)), policy_hash=digest(_clean(asdict(symbol_policy))))
    if symbol_policy.symbol != saved["symbol"]:
        return finish("unresolved", "symbol_policy_mismatch")
    if digest(_indicator_config(symbol_policy.indicators, symbol_policy.window)) != snapshot["indicator_config_hash"]:
        return finish("unresolved", "indicator_configuration_mismatch")
    recipe = symbol_policy.buy if side == "BUY" else symbol_policy.sell
    if recipe is None:
        return finish("unresolved", "side_score_recipe_missing")
    value.update(recipe_version=recipe.version, recipe_hash=digest(_clean(asdict(recipe))))
    if snapshot["status"] != "ready":
        return finish("unavailable", snapshot["reason"])
    if unavailable_reason is not None:
        identity(unavailable_reason)
        return finish("unavailable", unavailable_reason)
    if hourly_reference is not None and symbol_reference is not None:
        raise ContractError("Select exactly one hourly or independent symbol reference")
    selected_reference = symbol_reference if symbol_reference is not None else hourly_reference
    if selected_reference is None:
        return finish("unresolved", "hourly_reference_missing")
    baseline = _clean(asdict(selected_reference))
    kind = "symbol_reference" if symbol_reference is not None else "hourly_reference"
    value.update({kind: baseline, kind + "_hash": digest(baseline), "reference_kind": kind})
    valid_quantity = (type(hourly_reference.quantity) is int and hourly_reference.quantity > 0
                      if symbol_reference is None else bool(number(symbol_reference.shares, positive=True)))
    if (selected_reference.symbol != saved["symbol"] or selected_reference.side != side
            or not valid_quantity or not selected_reference.version or not selected_reference.evidence):
        return finish("unresolved", "hourly_reference_binding_invalid")
    if instant(selected_reference.available_at) > at:
        return finish("unavailable", "hourly_reference_unavailable_at_sizing")
    features = dict(snapshot["features"])
    features["probability_not_down"] = float(probability)
    value["account_inputs"] = {}
    required_facts = {term.feature for term in recipe.terms} | {term.scale_feature for term in recipe.terms}
    for name, item in (account_inputs or {}).items():
        if name in features:
            raise ContractError("Account facts cannot replace indicator or forecast inputs")
        value["account_inputs"][name] = _clean(asdict(item))
        # Context such as cash and inventory is logged, but it cannot silently
        # become a prerequisite or capacity gate for an unrelated TA recipe.
        if name not in required_facts:
            continue
        identity(item.source)
        if instant(item.available_at) > at:
            return finish("unavailable", "account_input_unavailable_at_sizing: " + name)
        if not item.evidence:
            return finish("unavailable", "account_input_evidence_missing: " + name)
        features[name] = _finite(item.value, name)
    # Decimal coefficients avoid turning an exact .7 - .45 half-share example
    # into .24999999999999994 and silently bypassing the unresolved tie rule.
    # Indicator measurements remain the exact decimal representation of their
    # recorded float values, not claims of analytic infinite precision.
    with localcontext() as arithmetic:
        arithmetic.prec = 34
        arithmetic.rounding = ROUND_HALF_EVEN
        dec = lambda x: Decimal(str(x))
        numerator = Decimal(0)
        divisor = dec(recipe.sum_divisor)
        for term in recipe.terms:
            raw = features.get(term.feature)
            if raw is None:
                return finish("unavailable", "sizing_feature_unavailable: " + term.feature)
            raw = _finite(raw, term.feature)
            feature_scale = features.get(term.scale_feature) if term.scale_feature is not None else 1.0
            if feature_scale is None or _finite(feature_scale, "Normalization feature") <= 0:
                return finish("unavailable", "normalization_feature_unavailable: " + str(term.scale_feature))
            scale = dec(term.scale) * dec(feature_scale)
            normalized = (dec(raw) - dec(term.center)) / scale
            clipped = min(dec(term.clip_max), max(dec(term.clip_min), normalized))
            weighted = dec(term.weight) * clipped
            contribution = weighted / divisor
            if not math.isfinite(float(normalized)) or not math.isfinite(float(contribution)):
                return finish("unavailable", "nonfinite_feature_calculation: " + term.feature)
            numerator += weighted
            value["term_contributions"].append({"feature": term.feature, "value": raw,
                "normalized": float(normalized), "clipped": float(clipped), "weight": term.weight,
                "scale_feature": term.scale_feature, "scale_feature_value": feature_scale,
                "effective_scale_decimal": str(scale), "weighted_numerator_decimal": str(weighted),
                "contribution": float(contribution), "contribution_decimal": str(contribution)})
        unbounded = dec(recipe.bias) + numerator / divisor
        if not math.isfinite(float(unbounded)):
            return finish("unavailable", "nonfinite_score_calculation")
        score = min(dec(recipe.output_max), max(dec(recipe.output_min), unbounded))
    value.update(unbounded_score=str(unbounded), score_numerator=str(numerator), sum_divisor=str(divisor),
                 score=str(score), score_clipped=score != unbounded,
                 score_arithmetic="decimal34_of_recorded_values")
    try:
        explanation_inputs = {"indicator_snapshot_id": indicators.snapshot_id, "policy_hash": value["policy_hash"],
                              kind + "_hash": value[kind + "_hash"], "term_contributions": value["term_contributions"]}
        if symbol_reference is None:
            explanation = size(symbol=saved["symbol"], side=side, hourly_reference=hourly_reference.quantity,
                reference_version=hourly_reference.version, score=str(score), score_version=recipe.version,
                inputs=explanation_inputs, half_share_ties=half_share_ties, rounded_equality=rounded_equality).payload()
        else:
            shares = number(symbol_reference.shares, positive=True)
            with localcontext() as arithmetic:
                arithmetic.prec = 34
                arithmetic.rounding = ROUND_HALF_EVEN
                raw_quantity = shares * score
                if half_share_ties not in {None, "half_up", "half_even"}:
                    raise ContractError("Unsupported half-share rule")
                if raw_quantity % 1 == Decimal(".5") and half_share_ties is None:
                    raise UnresolvedPolicy("Half-share ties require an explicit sizing rule")
                desired = int(raw_quantity.to_integral_value(rounding=ROUND_HALF_UP if half_share_ties == "half_up" else ROUND_HALF_EVEN))
            if rounded_equality not in {None, "allow"}:
                raise ContractError("No flooring or minimum-trade veto is selected")
            if desired == shares and rounded_equality is None:
                raise UnresolvedPolicy("Nearest rounding equals reference size; explicit handling required")
            explanation = dict(symbol=saved["symbol"], side=side, reference_version=symbol_reference.version,
                score_version=recipe.version, reference_shares=str(shares), score=str(score),
                raw_quantity=str(raw_quantity), desired_quantity=desired,
                half_share_ties=half_share_ties, rounded_equality=rounded_equality,
                rounded_at_or_above_reference=Decimal(desired) >= shares, inputs=explanation_inputs)
    except UnresolvedPolicy as exc:
        return finish("unresolved", str(exc))
    value.update(desired_quantity=explanation["desired_quantity"], explanation=explanation,
                 execution_disposition="pending_capacity", capacity_status="not_evaluated")
    return finish("sized", None)


__all__ = ["VERSION", "IndicatorSpec", "WindowPolicy", "ScoreTerm", "ScoreRecipe", "SymbolSizingPolicy",
           "HourlyReference", "SymbolReference", "AvailableInput", "IndicatorSnapshot", "SizingResult", "indicator_snapshot", "evaluate_size"]
