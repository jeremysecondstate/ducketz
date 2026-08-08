from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.decision_time import DecisionClock, latest_completed_bar_clock
from ml.option_pricing.black_scholes import (
    black_scholes_price,
    implied_volatility,
    target_years_to_expiration,
)
from ml.option_pricing.policies import (
    ContractSelectionPolicy,
    OPTION_PRICING_CONTRACT_POLICY_VERSION,
    OPTION_PRICING_DIVIDEND_POLICY_VERSION,
    OPTION_PRICING_EXPIRATION_POLICY_VERSION,
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_RATE_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
    OPTION_PRICING_VOLATILITY_POLICY_VERSION,
    SEMANTIC_FEATURE_COLUMNS,
)
from options.publication import CommittedOptionSnapshot, committed_option_snapshots


@dataclass(frozen=True)
class CausalSampleBatch:
    samples: pd.DataFrame
    source_files: tuple[Path, ...]
    status: str
    reason: str
    target_snapshot_for: pd.Timestamp | None


def select_strictly_earlier_snapshot(
    snapshots: Sequence[CommittedOptionSnapshot],
    *,
    target_snapshot_for: object,
    prediction_created_at: object,
) -> CommittedOptionSnapshot | None:
    """Choose the newest receipt strictly earlier on both causal clocks."""

    target = _utc(target_snapshot_for, "target_snapshot_for")
    created = _utc(prediction_created_at, "prediction_created_at")
    eligible = [
        snapshot
        for snapshot in snapshots
        if snapshot.snapshot_for < target and snapshot.available_at < created
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda snapshot: (snapshot.snapshot_for, snapshot.available_at),
    )


def build_live_prediction_inputs(
    datastore_root: Path,
    *,
    symbol: str,
    prediction_created_at: object,
    contract_policy: ContractSelectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
) -> CausalSampleBatch:
    """Resolve a pre-quote target bar and materialize strictly lagged inputs."""

    root = Path(datastore_root)
    clean_symbol = str(symbol).strip().upper()
    created = _utc(prediction_created_at, "prediction_created_at")
    clock = latest_completed_bar_clock(root, symbol=clean_symbol, as_of=created)
    target = pd.Timestamp(clock.decision_timestamp)
    snapshots = committed_option_snapshots(root, symbol=clean_symbol)
    if any(snapshot.snapshot_for == target for snapshot in snapshots):
        return CausalSampleBatch(
            pd.DataFrame(),
            (),
            "TARGET_ALREADY_OBSERVED",
            "A verified Options receipt for the target was visible before prediction.",
            target,
        )
    source = select_strictly_earlier_snapshot(
        snapshots,
        target_snapshot_for=target,
        prediction_created_at=created,
    )
    if source is None:
        return CausalSampleBatch(
            pd.DataFrame(),
            (),
            "SOURCE_SURFACE_UNAVAILABLE",
            "No strictly earlier committed Schwab surface was available.",
            target,
        )
    underlying = completed_bar_close(clock)
    source_contracts = pd.read_parquet(source.contracts_path)
    samples = build_causal_samples(
        source_contracts,
        target_contracts=None,
        target_underlying_price=underlying,
        source_snapshot_for=source.snapshot_for,
        source_available_at=source.available_at,
        target_snapshot_for=target,
        source_provider="schwab",
        prediction_mode="LIVE",
        contract_policy=contract_policy,
        rate_observations=rate_observations,
    )
    return CausalSampleBatch(
        samples,
        (clock.source_file, source.contracts_path, source.receipt_path),
        "READY" if samples["sample_status"].eq("AVAILABLE").any() else "NO_ELIGIBLE_CONTRACTS",
        "" if samples["sample_status"].eq("AVAILABLE").any() else "No contracts passed the causal feature contract.",
        target,
    )


def completed_bar_close(clock: DecisionClock) -> float:
    frame = pd.read_parquet(clock.source_file)
    if "timestamp" not in frame.columns or "close" not in frame.columns:
        raise ValueError("Canonical target bar lacks timestamp or close")
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    selected = pd.to_numeric(
        frame.loc[timestamps.eq(pd.Timestamp(clock.bar_timestamp)), "close"],
        errors="coerce",
    ).dropna()
    if len(selected) != 1:
        raise ValueError("Canonical target boundary did not resolve exactly one close")
    value = float(selected.iloc[0])
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("Canonical target close must be finite and positive")
    return value


def build_causal_samples(
    source_contracts: pd.DataFrame,
    *,
    target_contracts: pd.DataFrame | None,
    target_underlying_price: float,
    source_snapshot_for: object,
    source_available_at: object,
    target_snapshot_for: object,
    source_provider: str,
    prediction_mode: str,
    observed_available_at: object | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the six-feature causal contract without target quote leakage."""

    policy = contract_policy or ContractSelectionPolicy()
    source_time = _utc(source_snapshot_for, "source_snapshot_for")
    source_available = _utc(source_available_at, "source_available_at")
    target_time = _utc(target_snapshot_for, "target_snapshot_for")
    mode = str(prediction_mode).strip().upper()
    if mode not in {"LIVE", "OFFLINE"}:
        raise ValueError("prediction_mode must be LIVE or OFFLINE")
    if not source_time < target_time:
        raise ValueError("Source option surface must be strictly earlier than target")
    if not math.isfinite(float(target_underlying_price)) or target_underlying_price <= 0.0:
        raise ValueError("Target underlying price must be finite and positive")
    required = {
        "symbol",
        "contract_symbol",
        "call_put",
        "expiration_date",
        "strike",
        "underlying_price",
        "bid",
        "ask",
        "multiplier",
        "mini",
        "non_standard",
    }
    missing = sorted(required.difference(source_contracts.columns))
    if missing:
        raise ValueError("Source option contracts are missing: " + ", ".join(missing))
    source = source_contracts.copy()
    source["expiration_date"] = pd.to_datetime(
        source["expiration_date"], utc=True, errors="coerce"
    )
    for column in (
        "strike",
        "underlying_price",
        "bid",
        "ask",
        "multiplier",
        "interest_rate",
        "dividend_yield",
        "implied_volatility",
        "quote_staleness_seconds",
    ):
        if column not in source:
            source[column] = np.nan
        source[column] = pd.to_numeric(source[column], errors="coerce")
    if "quote_timestamp" not in source:
        source["quote_timestamp"] = pd.NaT
    source["quote_timestamp"] = pd.to_datetime(
        source["quote_timestamp"], utc=True, errors="coerce"
    )
    source = source.sort_values(
        ["expiration_date", "strike", "call_put", "contract_symbol"],
        kind="mergesort",
    ).drop_duplicates("contract_symbol", keep="last")

    resolved_rate, rate_source_at = _surface_rate(
        source,
        source_available_at=source_available,
        rate_observations=rate_observations,
    )
    resolved_dividend, dividend_source_at = _surface_dividend(
        source,
        risk_free_rate=resolved_rate,
        source_snapshot_for=source_time,
        source_available_at=source_available,
    )
    source["_resolved_rate"] = resolved_rate
    source["_resolved_dividend"] = resolved_dividend
    source["_resolved_iv"] = _source_implied_volatilities(
        source,
        source_snapshot_for=source_time,
        risk_free_rate=resolved_rate,
        dividend_yield=resolved_dividend,
    )
    target_definitions = source.loc[
        :,
        ["contract_symbol", "strike", "expiration_date", "call_put"],
    ].copy()
    target_definitions["underlying_price"] = float(target_underlying_price)
    target_definitions["lagged_implied_volatility"] = interpolate_lagged_iv_surface(
        source,
        target_definitions,
        target_snapshot_for=target_time,
    )

    target_by_contract: dict[str, Mapping[str, object]] = {}
    observed_at = (
        _utc(observed_available_at, "observed_available_at")
        if observed_available_at is not None
        else None
    )
    if target_contracts is not None:
        target = target_contracts.copy()
        if "contract_symbol" not in target:
            raise ValueError("Target option contracts lack contract_symbol")
        if target["contract_symbol"].astype("string").duplicated().any():
            raise ValueError("Target option contracts contain duplicate symbols")
        target_by_contract = {
            str(row["contract_symbol"]): row
            for row in target.to_dict("records")
        }

    rows: list[dict[str, object]] = []
    for definition in target_definitions.to_dict("records"):
        contract_symbol = str(definition["contract_symbol"])
        source_row = source.loc[source["contract_symbol"].astype(str).eq(contract_symbol)].iloc[0]
        status, reason = _source_contract_status(
            source_row,
            target_underlying_price=float(target_underlying_price),
            target_snapshot_for=target_time,
            policy=policy,
        )
        lagged_iv = _finite_or_none(definition["lagged_implied_volatility"])
        if status == "AVAILABLE" and resolved_rate is None:
            status, reason = "RATE_UNAVAILABLE", "No causal lagged rate observation was available."
        if status == "AVAILABLE" and resolved_dividend is None:
            status, reason = "DIVIDEND_UNAVAILABLE", "No causal lagged dividend policy resolved."
        if status == "AVAILABLE" and lagged_iv is None:
            status, reason = "VOLATILITY_UNAVAILABLE", "Earlier IV surface cannot interpolate without extrapolation."
        years = target_years_to_expiration(target_time, definition["expiration_date"])
        if status == "AVAILABLE" and years <= 0.0:
            status, reason = "EXPIRED", "Contract expires no later than the target boundary."

        observed_bid = observed_ask = observed_mid = observed_quote = None
        if target_contracts is not None:
            observed = target_by_contract.get(contract_symbol)
            if observed is None:
                status, reason = "TARGET_CONTRACT_MISSING", "Target receipt omitted the exact semantic contract."
            elif not _same_semantic_contract(definition, observed):
                status, reason = "TARGET_CONTRACT_MISMATCH", "Target contract semantics changed."
            else:
                observed_bid = _finite_or_none(observed.get("bid"))
                observed_ask = _finite_or_none(observed.get("ask"))
                observed_quote = _timestamp_or_none(observed.get("quote_timestamp"))
                if (
                    observed_bid is None
                    or observed_ask is None
                    or observed_ask < observed_bid
                    or (observed_bid + observed_ask) / 2.0 <= 0.0
                ):
                    status, reason = "TARGET_QUOTE_INVALID", "Target NBBO is missing, crossed, or nonpositive."
                elif observed_at is None or observed_at <= target_time:
                    status, reason = (
                        "TARGET_AVAILABILITY_INVALID",
                        "Target evidence was not available strictly after the emulated prediction boundary.",
                    )
                elif observed_quote is None or observed_quote <= target_time:
                    status, reason = (
                        "TARGET_TIMING_INVALID",
                        "Target quote is not strictly later than the emulated prediction boundary.",
                    )
                else:
                    observed_mid = (observed_bid + observed_ask) / 2.0

        black_scholes = normalized_residual = None
        if status == "AVAILABLE":
            black_scholes = black_scholes_price(
                float(target_underlying_price),
                float(definition["strike"]),
                float(resolved_rate),
                float(lagged_iv),
                years,
                float(resolved_dividend),
                str(definition["call_put"]),
            )
            if observed_mid is not None:
                normalized_residual = (
                    observed_mid - black_scholes
                ) / float(target_underlying_price)
        source_mid = (
            (float(source_row["bid"]) + float(source_row["ask"])) / 2.0
            if pd.notna(source_row["bid"]) and pd.notna(source_row["ask"])
            else None
        )
        rows.append(
            {
                "symbol": str(source_row["symbol"]).strip().upper(),
                "source_provider": str(source_provider).strip().lower(),
                "prediction_mode": mode,
                "call_put": str(definition["call_put"]).strip().upper(),
                "contract_symbol": contract_symbol,
                "expiration_date": definition["expiration_date"],
                "target_snapshot_for": target_time,
                "source_snapshot_for": source_time,
                "source_available_at": source_available,
                "source_quote_timestamp": _timestamp_or_none(source_row.get("quote_timestamp")),
                "source_quote_staleness_seconds": _finite_or_none(
                    source_row.get("quote_staleness_seconds")
                ),
                "observed_quote_timestamp": observed_quote,
                "observed_available_at": observed_at,
                "underlying_price": float(target_underlying_price),
                "strike": float(definition["strike"]),
                "multiplier": _finite_or_none(source_row.get("multiplier")),
                "risk_free_rate": resolved_rate,
                "rate_source_at": rate_source_at,
                "lagged_implied_volatility": lagged_iv,
                "volatility_source_at": _timestamp_or_none(source_row.get("quote_timestamp")) or source_available,
                "target_years_to_expiration": years,
                "dividend_yield": resolved_dividend,
                "dividend_source_at": dividend_source_at,
                "source_mid": source_mid,
                "observed_bid": observed_bid,
                "observed_ask": observed_ask,
                "observed_mid": observed_mid,
                "bid_ask_spread": (
                    observed_ask - observed_bid
                    if observed_bid is not None and observed_ask is not None
                    else None
                ),
                "black_scholes_price": black_scholes,
                "normalized_residual": normalized_residual,
                "sample_status": status,
                "exclusion_reason": reason,
                "expiration_policy_version": OPTION_PRICING_EXPIRATION_POLICY_VERSION,
                "timing_policy_version": OPTION_PRICING_TIMING_POLICY_VERSION,
                "rate_policy_version": OPTION_PRICING_RATE_POLICY_VERSION,
                "dividend_policy_version": OPTION_PRICING_DIVIDEND_POLICY_VERSION,
                "volatility_policy_version": OPTION_PRICING_VOLATILITY_POLICY_VERSION,
                "contract_policy_version": OPTION_PRICING_CONTRACT_POLICY_VERSION,
                "schema_version": OPTION_PRICING_SCHEMA_VERSION,
            }
        )
    return pd.DataFrame(rows)


def interpolate_lagged_iv_surface(
    source_contracts: pd.DataFrame,
    target_contracts: pd.DataFrame,
    *,
    target_snapshot_for: object,
) -> pd.Series:
    """Interpolate an earlier IV surface without strike or tenor extrapolation."""

    target_time = _utc(target_snapshot_for, "target_snapshot_for")
    required_source = {"strike", "underlying_price", "expiration_date", "_resolved_iv"}
    required_target = {"strike", "underlying_price", "expiration_date"}
    if missing := sorted(required_source.difference(source_contracts.columns)):
        raise ValueError("Earlier IV surface is missing: " + ", ".join(missing))
    if missing := sorted(required_target.difference(target_contracts.columns)):
        raise ValueError("IV interpolation targets are missing: " + ", ".join(missing))
    points = source_contracts.copy()
    points["_x"] = np.log(
        pd.to_numeric(points["strike"], errors="coerce")
        / pd.to_numeric(points["underlying_price"], errors="coerce")
    )
    points["_t"] = points["expiration_date"].map(
        lambda value: target_years_to_expiration(target_time, value)
    )
    points["_iv"] = pd.to_numeric(points["_resolved_iv"], errors="coerce")
    points = points.loc[
        np.isfinite(points["_x"])
        & np.isfinite(points["_t"])
        & np.isfinite(points["_iv"])
        & points["_iv"].gt(0.0)
        & points["_t"].gt(0.0)
    ]
    surfaces: list[tuple[float, np.ndarray, np.ndarray]] = []
    for tenor, group in points.groupby("_t", sort=True):
        collapsed = (
            group.groupby("_x", as_index=False, sort=True)["_iv"]
            .median()
            .sort_values("_x")
        )
        surfaces.append(
            (
                float(tenor),
                collapsed["_x"].to_numpy(dtype=float),
                collapsed["_iv"].to_numpy(dtype=float),
            )
        )
    results: list[float] = []
    for row in target_contracts.to_dict("records"):
        strike = _finite_or_none(row.get("strike"))
        underlying = _finite_or_none(row.get("underlying_price"))
        if strike is None or underlying is None or strike <= 0.0 or underlying <= 0.0:
            results.append(np.nan)
            continue
        x_target = math.log(strike / underlying)
        t_target = target_years_to_expiration(target_time, row.get("expiration_date"))
        tenor_values: list[tuple[float, float]] = []
        for tenor, xs, ivs in surfaces:
            if x_target < xs[0] - 1e-12 or x_target > xs[-1] + 1e-12:
                continue
            if len(xs) == 1 and not math.isclose(x_target, xs[0], abs_tol=1e-12):
                continue
            value = float(ivs[0]) if len(xs) == 1 else float(np.interp(x_target, xs, ivs))
            tenor_values.append((tenor, value))
        if not tenor_values:
            results.append(np.nan)
            continue
        tenor_values.sort()
        tenors = np.array([value[0] for value in tenor_values], dtype=float)
        ivs = np.array([value[1] for value in tenor_values], dtype=float)
        if t_target < tenors[0] - 1e-12 or t_target > tenors[-1] + 1e-12:
            results.append(np.nan)
        elif len(tenors) == 1 and not math.isclose(t_target, tenors[0], abs_tol=1e-12):
            results.append(np.nan)
        else:
            results.append(float(ivs[0]) if len(tenors) == 1 else float(np.interp(t_target, tenors, ivs)))
    return pd.Series(results, index=target_contracts.index, dtype="float64")


def model_feature_frame(samples: pd.DataFrame) -> pd.DataFrame:
    """Return only the declared six causal semantic inputs."""

    missing = sorted(set(SEMANTIC_FEATURE_COLUMNS).difference(samples.columns))
    if missing:
        raise ValueError("Pricing samples lack semantic inputs: " + ", ".join(missing))
    matrix = samples.loc[:, list(SEMANTIC_FEATURE_COLUMNS)].apply(
        pd.to_numeric, errors="coerce"
    )
    if not np.isfinite(matrix.to_numpy(dtype=float)).all():
        raise ValueError("Pricing semantic inputs must be finite")
    return matrix


def canonicalize_predictions(predictions: pd.DataFrame) -> pd.DataFrame:
    """Keep the earliest committed prediction per natural contract target."""

    if predictions.empty:
        return predictions.copy()
    required = {
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "prediction_created_at",
        "prediction_available_at",
    }
    if missing := sorted(required.difference(predictions.columns)):
        raise ValueError("Pricing predictions are missing: " + ", ".join(missing))
    output = predictions.copy()
    for column in (
        "target_snapshot_for",
        "prediction_created_at",
        "prediction_available_at",
    ):
        output[column] = pd.to_datetime(output[column], utc=True, errors="coerce")
    valid = (
        output["prediction_created_at"].notna()
        & output["prediction_available_at"].notna()
        & output["prediction_available_at"].ge(output["prediction_created_at"])
    )
    output = output.loc[valid].sort_values(
        ["prediction_available_at", "prediction_created_at"], kind="mergesort"
    )
    return output.drop_duplicates(
        ["symbol", "target_snapshot_for", "contract_symbol"], keep="first"
    ).reset_index(drop=True)


def reconcile_predictions(
    predictions: pd.DataFrame,
    *,
    snapshots_by_symbol: Mapping[str, Sequence[CommittedOptionSnapshot]],
    evaluated_at: object,
) -> pd.DataFrame:
    """Reconcile canonical predictions only to exact later option receipts."""

    evaluated = _utc(evaluated_at, "evaluated_at")
    canonical = canonicalize_predictions(predictions)
    contract_cache: dict[Path, pd.DataFrame] = {}
    rows: list[dict[str, object]] = []
    for prediction in canonical.to_dict("records"):
        symbol = str(prediction["symbol"]).strip().upper()
        target = _utc(prediction["target_snapshot_for"], "target_snapshot_for")
        created = _utc(prediction["prediction_created_at"], "prediction_created_at")
        available = _utc(prediction["prediction_available_at"], "prediction_available_at")
        matching = sorted(
            (
                snapshot
                for snapshot in snapshots_by_symbol.get(symbol, ())
                if snapshot.snapshot_for == target
                and snapshot.available_at > created
                and snapshot.available_at > available
            ),
            key=lambda snapshot: snapshot.available_at,
        )
        status = "PENDING_TARGET_RECEIPT"
        observed: Mapping[str, object] | None = None
        receipt_available: pd.Timestamp | None = None
        if matching:
            snapshot = matching[0]
            receipt_available = snapshot.available_at
            contracts = contract_cache.setdefault(
                snapshot.contracts_path,
                pd.read_parquet(snapshot.contracts_path),
            )
            exact = contracts.loc[
                contracts["contract_symbol"].astype(str).eq(str(prediction["contract_symbol"]))
            ]
            if exact.empty:
                status = "MISSING_TARGET_CONTRACT"
            elif len(exact) != 1 or not _same_semantic_contract(prediction, exact.iloc[0]):
                status = "TARGET_CONTRACT_MISMATCH"
            else:
                observed = exact.iloc[0].to_dict()
                quote_time = _timestamp_or_none(observed.get("quote_timestamp"))
                bid = _finite_or_none(observed.get("bid"))
                ask = _finite_or_none(observed.get("ask"))
                if quote_time is None:
                    status = "TARGET_QUOTE_TIMESTAMP_MISSING"
                elif quote_time <= created or quote_time <= available:
                    status = "STALE_PRE_PREDICTION_QUOTE"
                elif bid is None or ask is None or ask < bid or (bid + ask) / 2.0 <= 0.0:
                    status = "TARGET_QUOTE_INVALID"
                else:
                    status = "COMPLETE"
        rows.append(
            _evaluation_row(
                prediction,
                observed=observed,
                observed_available_at=receipt_available,
                evaluated_at=evaluated,
                status=status,
            )
        )
    return pd.DataFrame(rows)


def evaluate_offline_predictions(
    predictions: pd.DataFrame,
    samples: pd.DataFrame,
    *,
    evaluated_at: object,
) -> pd.DataFrame:
    """Evaluate OFFLINE predictions against their verified emulated targets."""

    evaluated = _utc(evaluated_at, "evaluated_at")
    offline = canonicalize_predictions(predictions)
    offline = offline.loc[
        offline["prediction_mode"].astype("string").str.upper().eq("OFFLINE")
    ]
    if offline.empty:
        return pd.DataFrame()
    required = {
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "observed_bid",
        "observed_ask",
        "observed_quote_timestamp",
        "observed_available_at",
    }
    if missing := sorted(required.difference(samples.columns)):
        raise ValueError("Offline pricing samples are missing: " + ", ".join(missing))
    targets = samples.copy()
    targets["target_snapshot_for"] = pd.to_datetime(
        targets["target_snapshot_for"], utc=True, errors="coerce"
    )
    if targets.duplicated(
        ["symbol", "target_snapshot_for", "contract_symbol"]
    ).any():
        raise ValueError("Offline pricing samples contain duplicate natural targets")
    lookup = {
        (
            str(row["symbol"]).strip().upper(),
            _utc(row["target_snapshot_for"], "target_snapshot_for"),
            str(row["contract_symbol"]),
        ): row
        for row in targets.to_dict("records")
    }
    rows: list[dict[str, object]] = []
    for prediction in offline.to_dict("records"):
        key = (
            str(prediction["symbol"]).strip().upper(),
            _utc(prediction["target_snapshot_for"], "target_snapshot_for"),
            str(prediction["contract_symbol"]),
        )
        sample = lookup.get(key)
        status = "MISSING_OFFLINE_TARGET"
        observed: Mapping[str, object] | None = None
        observed_available: pd.Timestamp | None = None
        if sample is not None:
            if not _same_semantic_contract(prediction, sample):
                status = "TARGET_CONTRACT_MISMATCH"
            else:
                quote = _timestamp_or_none(sample.get("observed_quote_timestamp"))
                created = _utc(prediction["prediction_created_at"], "prediction_created_at")
                available = _utc(prediction["prediction_available_at"], "prediction_available_at")
                bid = _finite_or_none(sample.get("observed_bid"))
                ask = _finite_or_none(sample.get("observed_ask"))
                observed_available = _timestamp_or_none(sample.get("observed_available_at"))
                observed = {
                    **sample,
                    "bid": bid,
                    "ask": ask,
                    "quote_timestamp": quote,
                }
                if quote is None:
                    status = "TARGET_QUOTE_TIMESTAMP_MISSING"
                elif quote <= created or quote <= available:
                    status = "STALE_PRE_PREDICTION_QUOTE"
                elif observed_available is None or observed_available <= available:
                    status = "TARGET_AVAILABILITY_INVALID"
                elif bid is None or ask is None or ask < bid or (bid + ask) / 2.0 <= 0:
                    status = "TARGET_QUOTE_INVALID"
                else:
                    status = "COMPLETE"
        rows.append(
            _evaluation_row(
                prediction,
                observed=observed,
                observed_available_at=observed_available,
                evaluated_at=evaluated,
                status=status,
            )
        )
    return pd.DataFrame(rows)


def _evaluation_row(
    prediction: Mapping[str, object],
    *,
    observed: Mapping[str, object] | None,
    observed_available_at: pd.Timestamp | None,
    evaluated_at: pd.Timestamp,
    status: str,
) -> dict[str, object]:
    bid = _finite_or_none(observed.get("bid")) if observed is not None else None
    ask = _finite_or_none(observed.get("ask")) if observed is not None else None
    midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    spread = ask - bid if bid is not None and ask is not None else None
    underlying = _finite_or_none(prediction.get("underlying_price"))
    fair = _finite_or_none(prediction.get("constrained_fair_value"))
    raw = _finite_or_none(prediction.get("raw_fair_value"))
    error = fair - midpoint if fair is not None and midpoint is not None else None
    observed_residual = (
        (midpoint - float(prediction["black_scholes_price"])) / underlying
        if midpoint is not None and underlying not in {None, 0.0}
        else None
    )
    half_spread = spread / 2.0 if spread is not None and spread > 0.0 else None
    complete = status == "COMPLETE"
    prospective = bool(
        complete
        and str(prediction.get("prediction_mode", "")).upper() == "LIVE"
        and str(prediction.get("source_provider", "")).strip().lower() == "schwab"
    )

    def covered(lower_name: str, upper_name: str) -> bool | None:
        lower = _finite_or_none(prediction.get(lower_name))
        upper = _finite_or_none(prediction.get(upper_name))
        return (
            bool(lower <= midpoint <= upper)
            if lower is not None and upper is not None and midpoint is not None
            else None
        )

    return {
        "symbol": str(prediction.get("symbol", "")).strip().upper(),
        "source_provider": prediction.get("source_provider"),
        "prediction_mode": prediction.get("prediction_mode"),
        "call_put": prediction.get("call_put"),
        "contract_symbol": prediction.get("contract_symbol"),
        "expiration_date": prediction.get("expiration_date"),
        "target_snapshot_for": prediction.get("target_snapshot_for"),
        "prediction_created_at": prediction.get("prediction_created_at"),
        "prediction_available_at": prediction.get("prediction_available_at"),
        "observed_quote_timestamp": (
            _timestamp_or_none(observed.get("quote_timestamp")) if observed is not None else None
        ),
        "observed_available_at": observed_available_at,
        "evaluated_at": evaluated_at,
        "model_name": prediction.get("model_name"),
        "model_version": prediction.get("model_version"),
        "underlying_price": underlying,
        "strike": prediction.get("strike"),
        "multiplier": prediction.get("multiplier"),
        "lagged_implied_volatility": prediction.get("lagged_implied_volatility"),
        "target_years_to_expiration": prediction.get("target_years_to_expiration"),
        "observed_bid": bid,
        "observed_ask": ask,
        "observed_mid": midpoint,
        "bid_ask_spread": spread,
        "observed_quote_staleness_seconds": (
            (observed_available_at - _timestamp_or_none(observed.get("quote_timestamp"))).total_seconds()
            if observed is not None
            and observed_available_at is not None
            and _timestamp_or_none(observed.get("quote_timestamp")) is not None
            else None
        ),
        "black_scholes_price": prediction.get("black_scholes_price"),
        "predicted_normalized_residual": prediction.get("predicted_normalized_residual"),
        "observed_normalized_residual": observed_residual,
        "raw_fair_value": raw,
        "constrained_fair_value": fair,
        "predictive_standard_deviation": prediction.get("predictive_standard_deviation"),
        "constrained_interval_80_lower": prediction.get("constrained_interval_80_lower"),
        "constrained_interval_80_upper": prediction.get("constrained_interval_80_upper"),
        "constrained_interval_95_lower": prediction.get("constrained_interval_95_lower"),
        "constrained_interval_95_upper": prediction.get("constrained_interval_95_upper"),
        "dollar_error": error if complete else None,
        "normalized_absolute_error": (
            abs(error) / underlying if complete and error is not None and underlying else None
        ),
        "normalized_squared_error": (
            (error / underlying) ** 2 if complete and error is not None and underlying else None
        ),
        "error_in_half_spreads": (
            abs(error) / half_spread if complete and error is not None and half_spread else None
        ),
        "model_edge_in_half_spreads": (
            (fair - midpoint) / half_spread if complete and fair is not None and midpoint is not None and half_spread else None
        ),
        "interval_80_covered": covered("constrained_interval_80_lower", "constrained_interval_80_upper") if complete else None,
        "interval_95_covered": covered("constrained_interval_95_lower", "constrained_interval_95_upper") if complete else None,
        "prospective_eligible": prospective,
        "evaluation_status": status,
        "pricing_policy_version": OPTION_PRICING_POLICY_VERSION,
        "timing_policy_version": OPTION_PRICING_TIMING_POLICY_VERSION,
        "schema_version": OPTION_PRICING_SCHEMA_VERSION,
    }


def _surface_rate(
    source: pd.DataFrame,
    *,
    source_available_at: pd.Timestamp,
    rate_observations: pd.DataFrame | None,
) -> tuple[float | None, pd.Timestamp | None]:
    provider = pd.to_numeric(source.get("interest_rate"), errors="coerce")
    provider = provider.loc[np.isfinite(provider) & provider.between(-0.20, 1.0)]
    if not provider.empty:
        return float(provider.median()), source_available_at
    if rate_observations is None or rate_observations.empty:
        return None, None
    required = {"available_at", "risk_free_rate"}
    if not required.issubset(rate_observations.columns):
        raise ValueError("Rate observations require available_at and risk_free_rate")
    observations = rate_observations.copy()
    observations["available_at"] = pd.to_datetime(
        observations["available_at"], utc=True, errors="coerce"
    )
    observations["risk_free_rate"] = pd.to_numeric(
        observations["risk_free_rate"], errors="coerce"
    )
    observations = observations.loc[
        observations["available_at"].lt(source_available_at)
        & observations["risk_free_rate"].between(-0.20, 1.0)
    ].sort_values("available_at")
    if observations.empty:
        return None, None
    row = observations.iloc[-1]
    return float(row["risk_free_rate"]), pd.Timestamp(row["available_at"])


def _surface_dividend(
    source: pd.DataFrame,
    *,
    risk_free_rate: float | None,
    source_snapshot_for: pd.Timestamp,
    source_available_at: pd.Timestamp,
) -> tuple[float | None, pd.Timestamp | None]:
    provider = pd.to_numeric(source.get("dividend_yield"), errors="coerce")
    provider = provider.loc[np.isfinite(provider) & provider.between(-0.20, 0.50)]
    if not provider.empty:
        return float(provider.median()), source_available_at
    if risk_free_rate is None:
        return None, None
    candidates: list[float] = []
    paired = source.copy()
    paired["_mid"] = (paired["bid"] + paired["ask"]) / 2.0
    for (_expiration, strike), group in paired.groupby(
        ["expiration_date", "strike"], dropna=True
    ):
        calls = group.loc[group["call_put"].astype(str).str.upper().eq("CALL")]
        puts = group.loc[group["call_put"].astype(str).str.upper().eq("PUT")]
        if calls.empty or puts.empty:
            continue
        call = calls.iloc[-1]
        put = puts.iloc[-1]
        years = target_years_to_expiration(source_snapshot_for, call["expiration_date"])
        spot = _finite_or_none(call.get("underlying_price"))
        call_mid = _finite_or_none(call.get("_mid"))
        put_mid = _finite_or_none(put.get("_mid"))
        if years <= 0.0 or spot in {None, 0.0} or call_mid is None or put_mid is None:
            continue
        discounted_spot = call_mid - put_mid + float(strike) * math.exp(-risk_free_rate * years)
        if discounted_spot <= 0.0:
            continue
        value = -math.log(discounted_spot / spot) / years
        if math.isfinite(value) and -0.20 <= value <= 0.50:
            candidates.append(value)
    return (
        (float(np.median(candidates)), source_available_at)
        if candidates
        else (None, None)
    )


def _source_implied_volatilities(
    source: pd.DataFrame,
    *,
    source_snapshot_for: pd.Timestamp,
    risk_free_rate: float | None,
    dividend_yield: float | None,
) -> pd.Series:
    output: list[float] = []
    for row in source.to_dict("records"):
        supplied = _finite_or_none(row.get("implied_volatility"))
        if supplied is not None and 0.0 < supplied <= 5.0:
            output.append(supplied)
            continue
        if risk_free_rate is None or dividend_yield is None:
            output.append(np.nan)
            continue
        bid = _finite_or_none(row.get("bid"))
        ask = _finite_or_none(row.get("ask"))
        spot = _finite_or_none(row.get("underlying_price"))
        strike = _finite_or_none(row.get("strike"))
        if bid is None or ask is None or ask < bid or spot is None or strike is None:
            output.append(np.nan)
            continue
        years = target_years_to_expiration(source_snapshot_for, row.get("expiration_date"))
        try:
            value = implied_volatility(
                (bid + ask) / 2.0,
                spot,
                strike,
                risk_free_rate,
                years,
                dividend_yield,
                str(row.get("call_put")),
            )
        except ValueError:
            value = np.nan
        output.append(value)
    return pd.Series(output, index=source.index, dtype="float64")


def _source_contract_status(
    row: pd.Series,
    *,
    target_underlying_price: float,
    target_snapshot_for: pd.Timestamp,
    policy: ContractSelectionPolicy,
) -> tuple[str, str]:
    multiplier = _finite_or_none(row.get("multiplier"))
    mini = _explicit_bool(row.get("mini"))
    nonstandard = _explicit_bool(row.get("non_standard"))
    if mini is not False or nonstandard is not False or multiplier is None or not math.isclose(multiplier, policy.required_multiplier):
        return "NONSTANDARD_CONTRACT", "Contract is mini, adjusted, nonstandard, or not a 100-share contract."
    strike = _finite_or_none(row.get("strike"))
    if strike is None or strike <= 0.0:
        return "INVALID_STRIKE", "Strike must be finite and positive."
    years = target_years_to_expiration(target_snapshot_for, row.get("expiration_date"))
    days = years * 365.0
    if not policy.minimum_days_to_expiration <= days <= policy.maximum_days_to_expiration:
        return "DTE_OUT_OF_RANGE", "Target expiration is outside the pilot DTE range."
    if abs(math.log(strike / target_underlying_price)) > policy.maximum_absolute_log_moneyness:
        return "MONEYNESS_OUT_OF_RANGE", "Contract is outside the pilot log-moneyness range."
    bid = _finite_or_none(row.get("bid"))
    ask = _finite_or_none(row.get("ask"))
    if bid is None or ask is None or ask <= bid or (bid + ask) / 2.0 <= 0.0:
        return "SOURCE_QUOTE_INVALID", "Earlier BBO is missing, locked, crossed, or nonpositive."
    staleness = _finite_or_none(row.get("quote_staleness_seconds"))
    if staleness is None or staleness < 0.0 or staleness > policy.maximum_source_staleness_seconds:
        return "SOURCE_QUOTE_STALE", "Earlier BBO exceeds the configured staleness window."
    quote_time = _timestamp_or_none(row.get("quote_timestamp"))
    if quote_time is None or quote_time >= target_snapshot_for:
        return "SOURCE_TIMING_INVALID", "Earlier option quote is not strictly before target."
    return "AVAILABLE", ""


def _same_semantic_contract(
    expected: Mapping[str, object],
    observed: Mapping[str, object] | pd.Series,
) -> bool:
    try:
        expected_expiration = pd.Timestamp(expected["expiration_date"]).date()
        observed_expiration = pd.Timestamp(observed["expiration_date"]).date()
        multiplier_matches = True
        if "multiplier" in expected:
            multiplier_matches = math.isclose(
                float(expected["multiplier"]),
                float(observed["multiplier"]),
                abs_tol=1e-9,
            )
        return bool(
            str(expected["contract_symbol"]) == str(observed["contract_symbol"])
            and str(expected["call_put"]).strip().upper()
            == str(observed["call_put"]).strip().upper()
            and math.isclose(float(expected["strike"]), float(observed["strike"]), abs_tol=1e-9)
            and expected_expiration == observed_expiration
            and multiplier_matches
        )
    except (KeyError, TypeError, ValueError):
        return False


def _explicit_bool(value: object) -> bool | None:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_or_none(value: object) -> pd.Timestamp | None:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(timestamp) else pd.Timestamp(timestamp)


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "CausalSampleBatch",
    "build_causal_samples",
    "build_live_prediction_inputs",
    "canonicalize_predictions",
    "completed_bar_close",
    "evaluate_offline_predictions",
    "interpolate_lagged_iv_surface",
    "model_feature_frame",
    "reconcile_predictions",
    "select_strictly_earlier_snapshot",
]
