"""Causal, shared OHLCV calculations for the Hyperliquid data experiment.

The legacy BERA source is provenance, never imported or executed.  ``*_v2``
columns are explicitly new versions: full-history scalers and backward filling
are removed, and centered smoothers use trailing polynomial endpoint estimates.
All windows count input candles, independent of the candle interval.  A model
should fit its own scaler on its training partition only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import erf, sqrt
from time import perf_counter
from typing import Callable, Hashable

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


LEGACY_ROOT = "C:/DATASTORE/hyperliquid/BERA-EXAMPLE/calculations"
FEATURE_SCHEMA_VERSION = "btc_shared_causal_v1"


@dataclass(frozen=True)
class FeatureResult:
    frame: pd.DataFrame
    feature_specs: list[dict]
    diagnostics: dict


def _divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Undefined ratios remain missing, including zero denominators."""
    return numerator.div(denominator.where(denominator.ne(0))).replace(
        [np.inf, -np.inf], np.nan
    )


def _window_result(series: pd.Series, window: int, calculate: Callable) -> pd.Series:
    """Vectorized trailing windows, with bounded temporary memory."""
    output = np.full(len(series), np.nan, dtype=np.float64)
    if len(series) < window:
        return pd.Series(output, index=series.index)
    windows = sliding_window_view(series.to_numpy(dtype=np.float64), window)
    for start in range(0, len(windows), 8192):
        part = windows[start : start + 8192]
        good = np.isfinite(part).all(axis=1)
        chunk = np.full(len(part), np.nan)
        if good.any():
            chunk[good] = calculate(part[good])
        output[start + window - 1 : start + window - 1 + len(part)] = chunk
    return pd.Series(output, index=series.index)


class SharedCalculations:
    """Per-build memoization keyed by source and every calculation parameter.

    The cache has no lifetime beyond one feature build.  Disabling it is a
    benchmark control with identical formulas, never a different feature set.
    Input candles and cached Series are never mutated by feature formulas.
    """

    def __init__(self, bars: pd.DataFrame, *, use_cache: bool = True):
        required = {"open", "high", "low", "close", "volume"}
        missing = required.difference(bars.columns)
        if missing:
            raise ValueError(f"Missing OHLCV columns: {sorted(missing)}")
        self.inputs = {
            name: bars[name].astype(np.float64).copy() for name in sorted(required)
        }
        self.factories: dict[str, Callable[[], pd.Series]] = {}
        self.cache: dict[Hashable, pd.Series] = {}
        self.use_cache = use_cache
        self.cache_hits = 0
        self.cache_misses = 0
        self.call_counts: Counter = Counter()
        self.seconds: Counter = Counter()

    def _compute(self, key: Hashable, calculate: Callable[[], pd.Series]) -> pd.Series:
        if self.use_cache and key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        self.cache_misses += 1
        started = perf_counter()
        result = calculate()
        self.seconds[key] += perf_counter() - started
        self.call_counts[key] += 1
        if self.use_cache:
            self.cache[key] = result
        return result

    def register(self, name: str, calculate: Callable[[], pd.Series]) -> str:
        if name in self.inputs or name in self.factories:
            raise ValueError(f"Duplicate calculation source: {name}")
        self.factories[name] = calculate
        return name

    def series(self, source: str) -> pd.Series:
        if source in self.inputs:
            return self.inputs[source]
        return self._compute(("derived", source), self.factories[source])

    def log_return(self, lag: int = 1) -> pd.Series:
        def calculate():
            close = self.series("close")
            return np.log(close.where(close.gt(0)) / close.shift(lag).where(close.shift(lag).gt(0)))

        return self._compute(("log_return", "close", lag), calculate)

    def pct_change(self, source: str, lag: int = 1) -> pd.Series:
        return self._compute(
            ("pct_change", source, lag),
            lambda: _divide(self.series(source), self.series(source).shift(lag)) - 1,
        )

    def diff(self, source: str, lag: int = 1) -> pd.Series:
        return self._compute(("diff", source, lag), lambda: self.series(source).diff(lag))

    def rolling(self, source: str, window: int, statistic: str, *, ddof: int = 1) -> pd.Series:
        def calculate():
            rolling = self.series(source).rolling(window, min_periods=window)
            if statistic == "std":
                return rolling.std(ddof=ddof)
            if statistic not in {"mean", "sum", "min", "max"}:
                raise ValueError(f"Unsupported rolling statistic: {statistic}")
            return getattr(rolling, statistic)()

        return self._compute(("rolling", source, window, statistic, ddof), calculate)

    def ema(self, source: str, span: int, *, adjust: bool = False) -> pd.Series:
        def calculate():
            value = self.series(source)
            # Do not turn an undefined current observation into a stale feature.
            return value.ewm(span=span, adjust=adjust, min_periods=span).mean().where(value.notna())

        return self._compute(("ema", source, span, adjust, span), calculate)

    def true_range(self, lag: int = 1) -> pd.Series:
        def calculate():
            previous = self.series("close").shift(lag)
            candidates = np.column_stack(
                [
                    (self.series("high") - self.series("low")).to_numpy(),
                    (self.series("high") - previous).abs().to_numpy(),
                    (self.series("low") - previous).abs().to_numpy(),
                ]
            )
            # Unknown previous closes are not implicitly replaced by high-low.
            return pd.Series(np.max(candidates, axis=1), index=previous.index)

        return self._compute(("true_range", lag), calculate)

    def regression(self, source: str, window: int, statistic: str) -> pd.Series:
        def calculate(part):
            x = np.arange(window, dtype=float) - (window - 1) / 2
            y = part - part.mean(axis=1, keepdims=True)
            covariance = np.einsum("ij,j->i", y, x)
            xx = np.dot(x, x)
            if statistic == "slope":
                return covariance / xx
            if statistic != "r2":
                raise ValueError(f"Unsupported regression statistic: {statistic}")
            yy = np.einsum("ij,ij->i", y, y)
            return np.divide(covariance**2, xx * yy, out=np.zeros(len(part)), where=yy > 0).clip(0, 1)

        return self._compute(
            ("regression", source, window, statistic),
            lambda: _window_result(self.series(source), window, calculate),
        )

    def smooth(self, source: str, window: int, order: int = 2) -> pd.Series:
        """Least-squares polynomial evaluated at the latest point of each window."""
        def calculate():
            x = np.arange(window, dtype=float) - (window - 1)
            design = np.vander(x, N=order + 1, increasing=True)
            weights = np.linalg.pinv(design)[0]
            return _window_result(self.series(source), window, lambda part: np.einsum("ij,j->i", part, weights))

        return self._compute(("trailing_polynomial", source, window, order), calculate)

    def entropy(self, source: str, window: int, bins: int = 10, *, base: float = np.e) -> pd.Series:
        def calculate(part):
            low = part.min(axis=1, keepdims=True)
            spread = part.max(axis=1, keepdims=True) - low
            scaled = np.divide(part - low, spread, out=np.zeros_like(part), where=spread > 0)
            bucket = np.minimum((scaled * bins).astype(np.int64), bins - 1)
            offsets = np.arange(len(part))[:, None] * bins
            counts = np.bincount((bucket + offsets).ravel(), minlength=len(part) * bins).reshape(-1, bins)
            probabilities = counts / window
            logs = np.zeros_like(probabilities)
            np.log(probabilities, out=logs, where=probabilities > 0)
            return -(probabilities * logs).sum(axis=1) / np.log(base)

        return self._compute(
            ("histogram_entropy", source, window, bins, base),
            lambda: _window_result(self.series(source), window, calculate),
        )

    def correlation(self, left: str, right: str, window: int) -> pd.Series:
        def calculate():
            # Center prices to reduce cancellation in rolling.corr on BTC levels.
            a = self.series(left) - self.series(left).iloc[0] if len(self.series(left)) else self.series(left)
            b = self.series(right) - self.series(right).iloc[0] if len(self.series(right)) else self.series(right)
            return a.rolling(window, min_periods=window).corr(b).replace([np.inf, -np.inf], np.nan).clip(-1, 1)

        return self._compute(("correlation", left, right, window), calculate)

    def zscore(self, source: str, window: int) -> pd.Series:
        return _divide(self.series(source) - self.rolling(source, window, "mean"), self.rolling(source, window, "std", ddof=0))

    def diagnostics(self) -> dict:
        return {
            "cache_enabled": self.use_cache,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "unique_computations": len(self.call_counts),
            "computation_timings": [
                {"key": repr(key), "calls": self.call_counts[key], "seconds": seconds}
                for key, seconds in sorted(self.seconds.items(), key=lambda pair: pair[1], reverse=True)
            ],
            "timing_note": "Per-computation times include dependencies; do not sum them as wall time.",
        }


def _legacy_exclusions() -> list[dict]:
    groups = {
        "Wavelet/fractal transform requires a separate causal port and dependency decision.":
            "afdi afmd cpdi dydir wific hwvi wcfdi_rolling qcma",
        "Full-history Hilbert/spectral transform requires a trailing-window redesign.":
            "fhtaf htdir vmt amfmvca cry2al asepsa",
        "Whole-history fitted statistical model/decomposition requires a separate causal training policy.":
            "pets stif pwetf vcari fmevs frto catrave caled hyperdimentrend mndea",
        "Whole-history statistical features require separate trailing-window definitions.":
            "dirin capi",
        "External live order-book dependency is not reconstructible from historical OHLCV.":
            "meoa vwci",
        "Feature internally trains a model; defer until its refit policy and cost are evaluated.":
            "tpwa mdpi",
    }
    return [
        {"legacy_file": f"{LEGACY_ROOT}/{name}.py", "legacy_name": name, "reason": reason}
        for reason, names in groups.items()
        for name in names.split()
    ]


def build_features(bars: pd.DataFrame, *, use_cache: bool = True) -> FeatureResult:
    """Build 38 numeric features without modifying bars or observing future rows.

    Rows before a calculation has enough history stay NaN.  Undefined ratios
    remain NaN; the caller decides which rows are suitable for model training.
    Raw input validation, completed-candle selection, gaps and storage belong to
    the ingestion layer.  No future targets or model fitting occur here.
    """
    started = perf_counter()
    c = SharedCalculations(bars, use_cache=use_cache)
    columns: dict[str, pd.Series] = {}
    specs: list[dict] = []

    def add(name: str, value: pd.Series, formula: str, legacy: str | None = None, notes: str = ""):
        columns[name] = value.replace([np.inf, -np.inf], np.nan).astype(np.float64)
        specs.append({
            "name": name,
            "description": formula,
            "formula": formula,
            "legacy_source": f"{LEGACY_ROOT}/{legacy}.py" if legacy else None,
            "adaptation_notes": notes,
            "availability": "At completion of the current candle; trailing observations only.",
        })

    for lag in (1, 2, 3, 4, 6):
        c.register(f"r{lag}", lambda lag=lag: c.log_return(lag))
    c.register("pct1", lambda: c.pct_change("close", 1))
    c.register("range", lambda: c.series("high") - c.series("low"))
    c.register("body", lambda: c.series("close") - c.series("open"))
    c.register("typical", lambda: (c.series("high") + c.series("low") + c.series("close")) / 3)
    c.register("typical_delta", lambda: c.diff("typical"))
    c.register("velocity", lambda: c.diff("close"))
    c.register("acceleration", lambda: c.diff("velocity"))
    c.register("tr1", lambda: c.true_range(1))
    c.register("tr2", lambda: c.true_range(2))
    c.register("tr3", lambda: c.true_range(3))
    no_scale = "Removed full-history scaling and all backward/zero filling; retain missing warmup."
    trailing = no_scale + " Centered Savitzky-Golay replaced by trailing polynomial endpoint smoothing."

    for lag in (1, 3, 6):
        add(f"log_return_{lag}", c.series(f"r{lag}"), f"log(close[t] / close[t-{lag}])")
    for span in (6, 10, 20, 50):
        add(f"ema_close_{span}", c.ema("close", span), f"EWM(close, span={span}, adjust=False, min_periods={span})")
    for window in (6, 10, 20):
        add(f"volatility_log_return_{window}", c.rolling("r1", window, "std"), f"Sample std of one-candle log returns over {window} candles")

    gains = c.diff("close").clip(lower=0)
    losses = -c.diff("close").clip(upper=0)
    gain_average = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss_average = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + _divide(gain_average, loss_average))
    rsi = rsi.mask(loss_average.eq(0) & gain_average.gt(0), 100)
    rsi = rsi.mask(loss_average.eq(0) & gain_average.eq(0), 50)
    add("rsi_14", rsi, "RSI from alpha=1/14 EWMs of gains/losses, seeded at first change; 14 valid changes required", notes="Flat market=50; gain-only=100; loss-only=0. This uses EWM initialization, not an initial 14-value SMA seed.")
    add("atr_14", c.rolling("tr1", 14, "mean"), "Simple trailing mean of conventional true range over 14 candles")
    bb_mid = c.rolling("close", 20, "mean")
    bb_std = c.rolling("close", 20, "std", ddof=0)
    add("bb_mid_20", bb_mid, "20-candle simple moving average of close")
    add("bb_width_20", _divide(4 * bb_std, bb_mid), "Bollinger bandwidth: 4 * population_std20(close) / mean20(close)")
    add("bb_position_20", _divide(c.series("close") - (bb_mid - 2 * bb_std), 4 * bb_std), "Bollinger %B at two population standard deviations; flat band is undefined")
    add("volume_ratio_20", _divide(c.series("volume"), c.rolling("volume", 20, "mean")), "volume / mean20(volume)")
    low14 = c.rolling("low", 14, "min")
    add("stochastic_14", _divide(c.series("close") - low14, c.rolling("high", 14, "max") - low14), "(close - lowest_low14) / (highest_high14 - lowest_low14), unscaled 0..1")
    add("momentum_6", c.pct_change("close", 6), "close[t] / close[t-6] - 1")

    c.register("signed_body_volume", lambda: pd.Series(np.where(c.series("close") > c.series("open"), c.series("volume"), -c.series("volume")), index=bars.index))
    c.register("volume_mean6", lambda: c.rolling("volume", 6, "mean"))
    lwmf = (_divide(c.series("r1"), c.rolling("r1", 9, "std")) * c.rolling("signed_body_volume", 6, "mean")
            * c.pct_change("volume_mean6").abs() * _divide(c.series("r1").abs(), c.series("volume")))
    add("lwmf_v2", lwmf, "(log_return1/std9(log_return1)) * mean6(signed_body_volume) * abs(pct_change(mean6(volume))) * abs(log_return1)/volume", "lwmf", no_scale)

    add("tdindi_v2", c.regression("close", 10, "slope") * c.regression("close", 10, "r2") * _divide(c.series("volume"), c.rolling("volume", 10, "mean")), "OLS slope10(close) * R_squared10(close) * volume/mean10(volume)", "tdindi", no_scale + " Vectorized closed-form OLS replaces repeated polyfit; flat-window R2=0.")

    c.register("vaa_raw", lambda: _divide(_divide(c.series("acceleration"), c.rolling("volume", 10, "mean")), c.rolling("close", 10, "std")))
    add("vaa_v2", c.ema("vaa_raw", 5), "EWM5(acceleration / mean10(volume) / std10(close))", "vaa", "Preserved formula; full warmup required for smoothing; zero denominators remain missing.")
    add("adi_v2", np.tanh((c.series("close") - c.ema("close", 10)) * (c.series("volume") / (c.rolling("volume", 10, "mean") + 1e-6)) / (c.rolling("range", 10, "mean") + 1e-6)), "tanh((close - EMA10(close)) * volume/(mean10(volume)+1e-6) / (mean10(high-low)+1e-6))", "adi", "Preserved epsilon and formula; EMA requires 10 observations.")
    add("dirpresh_v2", _divide(c.rolling("typical_delta", 5, "mean") - c.rolling("typical_delta", 15, "mean"), c.rolling("typical_delta", 15, "std")) * _divide(c.series("volume"), c.rolling("volume", 15, "mean")), "(mean5(delta_typical) - mean15(delta_typical))/std15(delta_typical) * volume/mean15(volume)", "dirpresh", no_scale + " Full windows replace min_periods=1.")
    add("vwtmi_v2", _divide(c.pct_change("close", 6) * _divide(c.series("volume"), c.rolling("volume", 6, "mean")), c.rolling("pct1", 6, "std")), "pct_change6(close) * volume/mean6(volume) / std6(pct_change1(close))", "vwtmi", no_scale)

    c.register("angular_momentum", lambda: (c.diff("close", 3) - c.diff("low", 3)) * c.diff("high", 2))
    c.register("angular_mean6", lambda: c.rolling("angular_momentum", 6, "mean"))
    c.register("torque", lambda: c.diff("angular_mean6"))
    add("pvam_v2", c.smooth("angular_mean6", 11) + c.smooth("torque", 11), "trailing_poly11(mean6((diff3(close)-diff3(low))*diff2(high))) + trailing_poly11(diff(mean6(angular_momentum)))", "pvam", trailing + " Removed the explicit one-row future shift.")

    c.register("body_volume", lambda: c.series("body") * c.series("volume"))
    c.register("apdi_raw", lambda: _divide(c.rolling("body_volume", 5, "sum"), c.rolling("r1", 5, "std")))
    add("apdi_v2", c.ema("apdi_raw", 5), "EWM5(sum5((close-open)*volume) / std5(log_return1))", "apdi", no_scale)

    c.register("open_close_log", lambda: np.log(_divide(c.series("close"), c.series("open"))))
    c.register("ivts_raw", lambda: _divide(c.series("open_close_log") * np.log1p(_divide(c.series("volume"), c.rolling("volume", 20, "mean"))), c.rolling("open_close_log", 20, "std")) * (1 - c.entropy("open_close_log", 5, base=2) / np.log2(10)))
    add("ivts_v2", c.smooth("ivts_raw", 7), "trailing_poly7(open_close_log * log1p(volume/mean20(volume)) / std20(open_close_log) * (1-Shannon_entropy5(open_close_log)/log2(10)))", "ivts", trailing + " Histogram bin counts normalized to probabilities, correcting density-as-probability entropy.")
    add("directins_v2", _divide(c.regression("close", 15, "slope") * _divide(c.series("volume"), c.rolling("volume", 15, "mean")).clip(0.5, 1.5), c.rolling("r1", 15, "std")), "OLS slope15(close) * clip(volume/mean15(volume),0.5,1.5) / std15(log_return1)", "directional_insight", no_scale + " Vectorized closed-form OLS replaces repeated polyfit.")
    add("paindex_v2", _divide(c.ema("acceleration", 3) * _divide(c.series("volume"), c.rolling("volume", 6, "mean")), c.rolling("acceleration", 3, "std")), "EWM3(acceleration) * volume/mean6(volume) / std3(acceleration)", "paindex", "Removed filling; require full rolling and EWM windows.")
    add("dvwa_v2", _divide(c.ema("acceleration", 14), c.rolling("tr2", 14, "mean")) * _divide(c.series("volume"), c.rolling("volume", 14, "mean")), "EWM14(acceleration) / mean14(true_range_against_close_lag2) * volume/mean14(volume)", "dvwa", "Preserved unusual legacy two-candle true-range reference; removed fills and require complete windows.")

    c.register("momentum_r6_5", lambda: c.rolling("r6", 5, "mean"))
    c.register("relative_range", lambda: _divide(c.series("range"), c.series("low")))
    add("pdpf_v2", c.series("momentum_r6_5") * c.rolling("r6", 15, "std") * (1 + c.entropy("momentum_r6_5", 15)) * c.smooth("relative_range", 7), "mean5(log_return6) * std15(log_return6) * (1+entropy15(mean5(log_return6))) * trailing_poly7((high-low)/low)", "pdpf", trailing)
    c.register("signed_change_volume", lambda: np.sign(c.diff("close")) * c.series("volume"))
    c.register("dypim_raw", lambda: _divide(c.rolling("r4", 6, "mean"), c.rolling("r4", 6, "std")) * _divide(c.rolling("signed_change_volume", 6, "sum"), c.rolling("volume", 6, "sum")) * 0.7)
    add("dypim_v2", c.smooth("dypim_raw", 11), "trailing_poly11(mean6(log_return4)/std6(log_return4) * sum6(sign(delta_close)*volume)/sum6(volume) * 0.7)", "dypim", trailing)
    c.register("volume_pct1", lambda: c.pct_change("volume"))
    c.register("pmvf_raw", lambda: c.rolling("r3", 14, "mean") * (1 + c.rolling("r3", 14, "std")) * (1 + c.rolling("volume_pct1", 10, "mean")))
    add("pmvf_v2", c.smooth("pmvf_raw", 7), "trailing_poly7(mean14(log_return3) * (1+std14(log_return3)) * (1+mean10(pct_change1(volume))))", "pmvf", trailing)

    c.register("epdii_volatility", lambda: c.rolling("tr3", 14, "std"))
    c.register("momentum_price6", lambda: c.diff("close", 6))
    add("epdii_v2", c.zscore("epdii_volatility", 50) * c.zscore("momentum_price6", 50) * np.sign(c.series("body")) * _divide(c.series("volume"), c.rolling("volume", 10, "mean")), "trailing_zscore50(std14(true_range_lag3)) * trailing_zscore50(diff6(close)) * sign(close-open) * volume/mean10(volume)", "epdii", "Global z-scores replaced with trailing 50-candle population z-scores. Removed fills; preserved lag3 true range.")

    c.register("delta_close3", lambda: c.diff("close", 3))
    cvei_volatility = c.rolling("r4", 10, "std")
    cvei_weight = 0.4 / (1 + cvei_volatility) + 0.6 / (1 + c.entropy("close", 5, base=2))
    add("cvei_v2", c.rolling("delta_close3", 5, "mean") * cvei_weight + c.correlation("close", "volume", 12) * cvei_volatility, "mean5(diff3(close)) * (0.4/(1+std10(log_return4)) + 0.6/(1+entropy5(close))) + corr12(close,volume)*std10(log_return4)", "cvei", no_scale + " Use probability-normalized Shannon entropy in bits, correcting legacy density calculation.")

    def spectral_weight():
        def calculate(part):
            window = part.shape[1]
            x = np.arange(window, dtype=float) - (window - 1) / 2
            centered = part - part.mean(axis=1, keepdims=True)
            slope = np.einsum("ij,j->i", centered, x) / np.dot(x, x)
            residual = centered - slope[:, None] * x
            amplitude = np.abs(np.fft.rfft(residual, axis=1))
            total = amplitude.sum(axis=1)
            freq = np.fft.rfftfreq(window)
            centroid = np.divide(amplitude @ freq, total, out=np.zeros(len(part)), where=total > 1e-12) / 0.5
            power = amplitude**2
            total_power = power.sum(axis=1, keepdims=True)
            probability = np.divide(power, total_power, out=np.zeros_like(power), where=total_power > 1e-24)
            logs = np.zeros_like(probability)
            np.log(probability, out=logs, where=probability > 0)
            entropy = -(probability * logs).sum(axis=1) / np.log(power.shape[1])
            weight = (1 - centroid) * (1 - entropy)
            return np.where(total > 1e-12, weight, 0)

        return _window_result(c.series("close"), 20, calculate)

    c.register("spectral_weight20", spectral_weight)
    c.register("smdi_raw", lambda: c.ema("r1", 10) * c.series("spectral_weight20"))
    add("smdi_v2", c.ema("smdi_raw", 10), "EWM10(EWM10(log_return1) * (1-spectral_centroid20/0.5) * (1-normalized_spectral_entropy20))", "smdi", no_scale + " Batched trailing FFT and OLS detrending; zero spectral power has weight0; zero entropy probabilities safely contribute0.")

    c.register("roc5", lambda: c.pct_change("close", 5))
    c.register("roc5_vol6", lambda: c.rolling("roc5", 6, "std"))
    c.register("smooth_close11", lambda: c.smooth("close", 11))
    trend = c.pct_change("smooth_close11", 6)
    normal_input = trend / (c.series("roc5_vol6") + 1e-9)
    # erf implements the normal CDF direction adjustment, not a learned probability.
    adjustment = pd.Series(np.fromiter((erf(value / sqrt(2)) for value in normal_input), dtype=float, count=len(normal_input)), index=bars.index)
    add("etsa_v2", trend * (1 + c.entropy("roc5_vol6", 6) * adjustment), "trend6(trailing_poly11(close)) * (1+entropy6(std6(ROC5))*erf(trend/(std6(ROC5)+1e-9)/sqrt(2)))", "etsa", trailing + " Scalar row-wise pandas apply replaced by numeric normal-CDF equivalent; this transform is not a calibrated probability.")
    add("roc_v2", c.pct_change("close", 2), "close[t] / close[t-2] - 1", "rochange", "Preserved actual lag2 formula; legacy output was misleadingly named ROC5. Removed unused lag9 ratio calculation.")

    frame = pd.DataFrame(columns, index=bars.index)
    diagnostics = c.diagnostics()
    diagnostics.update({
        "schema_version": FEATURE_SCHEMA_VERSION,
        "feature_count": len(frame.columns),
        "legacy_adaptations": sum(spec["legacy_source"] is not None for spec in specs),
        "legacy_exclusions": _legacy_exclusions(),
        "rows": len(frame),
        "complete_feature_rows": int(frame.notna().all(axis=1).sum()),
        "null_counts": {name: int(count) for name, count in frame.isna().sum().items()},
        "elapsed_seconds": perf_counter() - started,
        "warmup_policy": "No backfill or zero fill; full windows required; undefined denominators remain NaN.",
        "scaling_policy": "No whole-history feature scaling; any model scaler must be fitted on its training partition.",
    })
    return FeatureResult(frame=frame, feature_specs=specs, diagnostics=diagnostics)
