from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.databento_opra_history import canonical_root
from ml.artifacts import file_checksum
from ml.option_pricing.opra import normalize_cbbo_records
from ml.strategy_selection.candidates import (
    construct_strategy_candidates,
    evaluate_candidate_outcome,
)
from ml.strategy_selection.contracts import StrategySelectionPolicy
from ml.strategy_selection.market_state import (
    infer_market_state,
    score_market_state_prior,
)


MODELED_EXECUTION_VERSION = "opra-ohlcv-1h-conservative-execution-v1"
MODELED_EXECUTION_RECEIPT_VERSION = (
    "opra-ohlcv-1h-conservative-execution-receipt-v1"
)
MODELED_EXECUTION_SOURCE = "OPRA_OHLCV_MODELED_EXECUTION"
MODELED_CHAIN_PROVIDER = "databento-opra-ohlcv-modeled"
MODELED_HORIZONS = ("1d", "1w")
_OCC = re.compile(r"^([A-Z.]{1,6})\s*(\d{6})([CP])(\d{8})$")
_ABSOLUTE_PRICE_CUSHION = 0.01
_MINIMUM_MODELED_PREMIUM = 0.10
_MINIMUM_CONTRACT_HOUR_VOLUME = 10.0
_HAIRCUT_QUANTILE = 0.975
_MINIMUM_CALIBRATION_MATCHES = 2_000
_MINIMUM_HOLDOUT_COVERAGE = 0.80


@dataclass(frozen=True)
class ExecutionHaircutModel:
    bucket_haircuts: Mapping[str, float]
    global_haircut: float
    fingerprint: str
    report: Mapping[str, object]
    source_files: tuple[Path, ...]

    def haircut(self, price: float) -> float:
        return float(
            self.bucket_haircuts.get(_premium_bucket(price), self.global_haircut)
        )


@dataclass(frozen=True)
class ModeledOutcomeResult:
    frame: pd.DataFrame
    report: Mapping[str, object]
    checkpoint_files: tuple[Path, ...]


def calibrate_execution_haircuts(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    maximum_sessions: int = 15,
    holdout_sessions: int = 3,
) -> ExecutionHaircutModel:
    root = Path(datastore_root)
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols)
    )
    overlap = _overlap_dates(root, clean_symbols)
    if len(overlap) < holdout_sessions + 2:
        raise ValueError(
            "OPRA execution calibration needs at least five overlapping "
            "ohlcv-1h/CBBO sessions"
        )
    selected_dates = overlap[-maximum_sessions:]
    assessment_dates = set(selected_dates[-holdout_sessions:])
    rows: list[pd.DataFrame] = []
    source_files: list[Path] = []
    failures: Counter[str] = Counter()
    for symbol in clean_symbols:
        for session in selected_dates:
            hour_path = _opra_partition_path(
                root, "ohlcv-1h", symbol, session
            )
            cbbo_path = _opra_partition_path(
                root, "cbbo-1m", symbol, session
            )
            if not hour_path.is_file() or not cbbo_path.is_file():
                failures["partition_unavailable"] += 1
                continue
            try:
                raw_hour = _read_option_hour_day(str(hour_path))
            except Exception:
                failures["ohlcv_unreadable"] += 1
                continue
            hour_values = pd.DatetimeIndex(
                pd.to_datetime(raw_hour["ts_event"], utc=True, errors="coerce")
                .dropna()
                .unique()
            ).sort_values()
            if hour_values.empty:
                failures["ohlcv_empty"] += 1
                continue
            for hour in tuple(dict.fromkeys((hour_values[0], hour_values[-1]))):
                references = _hour_references(raw_hour, symbol=symbol, hour=hour)
                if references.empty:
                    failures["reference_empty"] += 1
                    continue
                try:
                    quotes = _cbbo_near_hour_end(
                        cbbo_path,
                        hour_end=pd.Timestamp(hour) + pd.Timedelta(hours=1),
                    )
                except Exception:
                    failures["cbbo_unreadable"] += 1
                    continue
                matched = references.merge(
                    quotes,
                    on="contract_symbol",
                    how="inner",
                    validate="one_to_one",
                )
                reference = pd.to_numeric(
                    matched["reference_price"], errors="coerce"
                )
                bid = pd.to_numeric(matched["actual_bid"], errors="coerce")
                ask = pd.to_numeric(matched["actual_ask"], errors="coerce")
                valid = (
                    np.isfinite(reference)
                    & reference.ge(_MINIMUM_MODELED_PREMIUM)
                    & np.isfinite(bid)
                    & bid.ge(0.0)
                    & np.isfinite(ask)
                    & ask.ge(bid)
                    & ask.gt(0.0)
                )
                matched = matched.loc[valid].copy()
                if matched.empty:
                    failures["no_valid_matches"] += 1
                    continue
                reference = pd.to_numeric(
                    matched["reference_price"], errors="coerce"
                )
                required = np.maximum.reduce(
                    (
                        (
                            pd.to_numeric(matched["actual_ask"], errors="coerce")
                            - reference
                            - _ABSOLUTE_PRICE_CUSHION
                        )
                        / reference,
                        (
                            reference
                            - pd.to_numeric(matched["actual_bid"], errors="coerce")
                            - _ABSOLUTE_PRICE_CUSHION
                        )
                        / reference,
                        np.zeros(len(matched), dtype=float),
                    )
                )
                matched["required_haircut"] = required
                matched["premium_bucket"] = reference.map(_premium_bucket)
                matched["symbol"] = symbol
                matched["session"] = session
                matched["hour"] = pd.Timestamp(hour)
                matched["partition"] = (
                    "assessment" if session in assessment_dates else "fit"
                )
                rows.append(matched)
                source_files.extend(
                    (
                        hour_path,
                        cbbo_path,
                        hour_path.with_name("manifest.json"),
                        cbbo_path.with_name("manifest.json"),
                        hour_path.with_name("receipt.json"),
                        cbbo_path.with_name("receipt.json"),
                    )
                )
    evidence = (
        pd.concat(rows, ignore_index=True, sort=False)
        if rows
        else pd.DataFrame()
    )
    fit = evidence.loc[evidence.get("partition", pd.Series(dtype="string")).eq("fit")]
    assessment = evidence.loc[
        evidence.get("partition", pd.Series(dtype="string")).eq("assessment")
    ]
    if len(fit) < _MINIMUM_CALIBRATION_MATCHES:
        raise ValueError(
            "OPRA execution calibration has insufficient matched CBBO/hourly "
            f"contracts: required {_MINIMUM_CALIBRATION_MATCHES}, observed {len(fit)}"
        )
    global_haircut = float(
        np.quantile(
            pd.to_numeric(fit["required_haircut"], errors="coerce").dropna(),
            _HAIRCUT_QUANTILE,
        )
    )
    global_haircut = float(np.clip(global_haircut, 0.01, 1.0))
    bucket_haircuts: dict[str, float] = {}
    bucket_counts: dict[str, int] = {}
    for bucket, group in fit.groupby("premium_bucket", sort=True):
        values = pd.to_numeric(group["required_haircut"], errors="coerce").dropna()
        bucket_counts[str(bucket)] = len(values)
        value = (
            float(np.quantile(values, _HAIRCUT_QUANTILE))
            if len(values) >= 100
            else global_haircut
        )
        bucket_haircuts[str(bucket)] = float(np.clip(value, 0.01, 1.0))
    assessment = assessment.copy()
    if assessment.empty:
        raise ValueError("OPRA execution calibration has no untouched holdout rows")
    predicted = pd.to_numeric(
        assessment["reference_price"], errors="coerce"
    )
    assessment["haircut"] = [
        bucket_haircuts.get(_premium_bucket(float(value)), global_haircut)
        for value in predicted
    ]
    modeled_bid = np.maximum(
        0.0,
        predicted * (1.0 - assessment["haircut"]) - _ABSOLUTE_PRICE_CUSHION,
    )
    modeled_ask = (
        predicted * (1.0 + assessment["haircut"])
        + _ABSOLUTE_PRICE_CUSHION
    )
    buy_coverage = modeled_ask.ge(
        pd.to_numeric(assessment["actual_ask"], errors="coerce")
    )
    sell_coverage = modeled_bid.le(
        pd.to_numeric(assessment["actual_bid"], errors="coerce")
    )
    joint_coverage = buy_coverage & sell_coverage
    holdout_coverage = float(joint_coverage.mean())
    if holdout_coverage < _MINIMUM_HOLDOUT_COVERAGE:
        raise ValueError(
            "OPRA execution haircut failed untouched CBBO coverage: "
            f"required {_MINIMUM_HOLDOUT_COVERAGE:.2%}, observed {holdout_coverage:.2%}"
        )
    model_payload = {
        "version": MODELED_EXECUTION_VERSION,
        "quantile": _HAIRCUT_QUANTILE,
        "absolute_price_cushion": _ABSOLUTE_PRICE_CUSHION,
        "global_haircut": global_haircut,
        "bucket_haircuts": bucket_haircuts,
        "fit_sessions": sorted(
            str(value) for value in set(selected_dates).difference(assessment_dates)
        ),
        "assessment_sessions": sorted(str(value) for value in assessment_dates),
    }
    fingerprint = hashlib.sha256(
        json.dumps(model_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    report = {
        **model_payload,
        "fingerprint_sha256": fingerprint,
        "fit_matches": len(fit),
        "assessment_matches": len(assessment),
        "bucket_fit_counts": bucket_counts,
        "assessment_buy_side_conservative_coverage": float(buy_coverage.mean()),
        "assessment_sell_side_conservative_coverage": float(sell_coverage.mean()),
        "assessment_joint_conservative_coverage": holdout_coverage,
        "minimum_required_joint_coverage": _MINIMUM_HOLDOUT_COVERAGE,
        "failures": dict(sorted(failures.items())),
        "exact_cbbo_used_for_haircut_calibration_only": True,
        "holdout_used_for_haircut_fitting": False,
    }
    return ExecutionHaircutModel(
        bucket_haircuts=bucket_haircuts,
        global_haircut=global_haircut,
        fingerprint=fingerprint,
        report=report,
        source_files=tuple(dict.fromkeys(source_files)),
    )


def build_modeled_strategy_outcomes(
    datastore_root: Path,
    *,
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    horizon: str,
    haircuts: ExecutionHaircutModel,
    policy: StrategySelectionPolicy | None = None,
    reporter: object = print,
) -> ModeledOutcomeResult:
    clean_horizon = str(horizon).strip().lower()
    if clean_horizon not in MODELED_HORIZONS:
        raise ValueError("Modeled Strategy outcomes are limited to 1d and 1w")
    effective_policy = policy or StrategySelectionPolicy()
    selected = samples.loc[
        samples["horizon"].astype("string").eq(clean_horizon)
        & samples["label_status"].astype("string").eq("COMPLETE")
    ].copy()
    selected = selected.sort_values(
        ["target_window_start", "symbol", "decision_timestamp"],
        kind="mergesort",
    )
    raw_sample_rows = len(selected)
    selected = _balanced_cluster_samples(
        Path(datastore_root),
        selected,
        horizon=clean_horizon,
    )
    probabilities = _prediction_probabilities(predictions)
    frames: list[pd.DataFrame] = []
    checkpoints: list[Path] = []
    failures: Counter[str] = Counter()
    cache_hits = 0
    generated = 0
    for index, sample in enumerate(selected.to_dict("records"), start=1):
        key = _sample_cache_key(sample, haircuts=haircuts)
        cached = _read_checkpoint(
            Path(datastore_root), horizon=clean_horizon, cache_key=key
        )
        if cached is not None:
            frame, files = cached
            frames.append(frame)
            checkpoints.extend(files)
            cache_hits += 1
            continue
        try:
            frame = _modeled_sample_outcomes(
                Path(datastore_root),
                sample=sample,
                probability_up=probabilities.get(_prediction_key(sample)),
                haircuts=haircuts,
                policy=effective_policy,
            )
        except Exception as exc:
            failures[f"{type(exc).__name__}:{str(exc)[:120]}"] += 1
            continue
        if frame.empty:
            failures["no_complete_candidate_outcomes"] += 1
            continue
        files = _publish_checkpoint(
            Path(datastore_root),
            horizon=clean_horizon,
            cache_key=key,
            frame=frame,
            sample=sample,
            execution_model_fingerprint=haircuts.fingerprint,
        )
        frames.append(frame)
        checkpoints.extend(files)
        generated += 1
        if callable(reporter) and (generated <= 3 or index % 25 == 0):
            reporter(
                "Strategy profit modeled outcomes: "
                f"horizon={clean_horizon}; considered={index}/{len(selected)}; "
                f"published={generated}; reused={cache_hits}"
            )
    output = (
        pd.concat(frames, ignore_index=True, sort=False)
        if frames
        else pd.DataFrame()
    )
    complete = (
        output.loc[output["outcome_status"].eq("COMPLETE")].reset_index(drop=True)
        if not output.empty
        else output
    )
    report = {
        "schema_version": MODELED_EXECUTION_VERSION,
        "horizon": clean_horizon,
        "raw_symbol_sample_rows_available": raw_sample_rows,
        "sample_rows_considered": len(selected),
        "sample_checkpoints_reused": cache_hits,
        "sample_checkpoints_published": generated,
        "candidate_outcome_rows": len(output),
        "complete_outcome_rows": len(complete),
        "usable_decision_clusters": (
            int(complete["target_window_start"].nunique())
            if not complete.empty
            else 0
        ),
        "symbol_counts": (
            {
                str(key): int(value)
                for key, value in complete["symbol"].value_counts().items()
            }
            if not complete.empty
            else {}
        ),
        "profitable_class_counts": (
            {
                str(key): int(value)
                for key, value in complete["profitable"].value_counts().items()
            }
            if not complete.empty
            else {}
        ),
        "target_start_min": (
            pd.Timestamp(complete["target_window_start"].min()).isoformat()
            if not complete.empty
            else None
        ),
        "target_start_max": (
            pd.Timestamp(complete["target_window_start"].max()).isoformat()
            if not complete.empty
            else None
        ),
        "execution_evidence_type_counts": (
            {
                str(key): int(value)
                for key, value in complete["execution_evidence_type"]
                .value_counts()
                .items()
            }
            if not complete.empty
            else {}
        ),
        "execution_model_fingerprint_sha256": haircuts.fingerprint,
        "failures": dict(sorted(failures.items())),
        "raw_ohlcv_rows_are_not_labeled_as_bbo": True,
        "cluster_symbol_sampling": (
            "one_deterministic_rotating_available_symbol_per_target_start; "
            "prevents correlated symbols from inflating independent cohort count"
        ),
        "entry_reference": "first_regular_session_hour_close_known_at_hour_end",
        "exit_reference": "final_regular_session_hour_close_known_at_close",
    }
    return ModeledOutcomeResult(
        frame=complete,
        report=report,
        checkpoint_files=tuple(dict.fromkeys(checkpoints)),
    )


def _balanced_cluster_samples(
    root: Path,
    samples: pd.DataFrame,
    *,
    horizon: str,
) -> pd.DataFrame:
    """Choose one rotating available symbol per independent decision cohort."""

    selected_rows: list[pd.DataFrame] = []
    grouped = samples.groupby("target_window_start", sort=True, dropna=False)
    for cluster_index, (_target, group) in enumerate(grouped):
        ordered = group.sort_values("symbol", kind="stable").reset_index(drop=True)
        if ordered.empty:
            continue
        offset = cluster_index % len(ordered)
        positions = tuple(range(offset, len(ordered))) + tuple(range(0, offset))
        chosen = None
        for position in positions:
            row = ordered.iloc[position]
            if _sample_source_partitions_available(root, row, horizon=horizon):
                chosen = ordered.iloc[[position]]
                break
        if chosen is not None:
            selected_rows.append(chosen)
    return (
        pd.concat(selected_rows, ignore_index=True, sort=False)
        if selected_rows
        else samples.iloc[0:0].copy()
    )


def _sample_source_partitions_available(
    root: Path,
    sample: Mapping[str, object] | pd.Series,
    *,
    horizon: str,
) -> bool:
    symbol = str(sample["symbol"]).strip().upper()
    start = _utc(sample["target_window_start"])
    end = _utc(sample["target_window_end"])
    entry = _opra_partition_path(
        root, "ohlcv-1h", symbol, start.date().isoformat()
    )
    exit_path = _opra_partition_path(
        root, "ohlcv-1h", symbol, end.date().isoformat()
    )
    stock = root / "stocks" / symbol / "bars" / "1h" / "databento" / "normalized"
    return entry.is_file() and exit_path.is_file() and stock.is_dir()


def _modeled_sample_outcomes(
    root: Path,
    *,
    sample: Mapping[str, object],
    probability_up: float | None,
    haircuts: ExecutionHaircutModel,
    policy: StrategySelectionPolicy,
) -> pd.DataFrame:
    symbol = str(sample["symbol"]).strip().upper()
    target_start = _utc(sample["target_window_start"])
    target_end = _utc(sample["target_window_end"])
    entry_hour = target_start.floor("h")
    exit_hour = (target_end - pd.Timedelta(nanoseconds=1)).floor("h")
    entry_reference_at = entry_hour + pd.Timedelta(hours=1)
    exit_reference_at = exit_hour + pd.Timedelta(hours=1)
    entry_path = _opra_partition_path(
        root, "ohlcv-1h", symbol, target_start.date().isoformat()
    )
    exit_path = _opra_partition_path(
        root, "ohlcv-1h", symbol, target_end.date().isoformat()
    )
    if not entry_path.is_file() or not exit_path.is_file():
        raise FileNotFoundError("OPRA hourly entry or exit partition unavailable")
    entry_stock = _stock_quote(
        root,
        symbol=symbol,
        hour=entry_hour,
        available_at=entry_reference_at,
    )
    exit_stock = _stock_quote(
        root,
        symbol=symbol,
        hour=exit_hour,
        available_at=exit_reference_at,
    )
    entry_chain = _modeled_chain(
        _read_option_hour_day(str(entry_path)),
        symbol=symbol,
        hour=entry_hour,
        available_at=entry_reference_at,
        underlying=float(entry_stock["mid"]),
        haircuts=haircuts,
    )
    exit_chain = _modeled_chain(
        _read_option_hour_day(str(exit_path)),
        symbol=symbol,
        hour=exit_hour,
        available_at=exit_reference_at,
        underlying=float(exit_stock["mid"]),
        haircuts=haircuts,
    )
    strategy_sample = dict(sample)
    strategy_sample["target_window_start"] = entry_reference_at + pd.Timedelta(
        nanoseconds=1
    )
    surface = pd.Series(
        {
            "symbol": symbol,
            "snapshot_for": target_start,
            "available_at": entry_reference_at,
            "surface_quality_pass": bool(
                entry_chain["call_put"].nunique() == 2
                and len(entry_chain) >= 20
            ),
            "source_provider": MODELED_CHAIN_PROVIDER,
        }
    )
    exit_surface = pd.Series(
        {
            "symbol": symbol,
            "snapshot_for": target_end,
            "available_at": exit_reference_at,
            "surface_quality_pass": bool(
                exit_chain["call_put"].nunique() == 2
                and len(exit_chain) >= 20
            ),
            "source_provider": MODELED_CHAIN_PROVIDER,
        }
    )
    candidates, _audit = construct_strategy_candidates(
        strategy_sample,
        entry_chain,
        surface=surface,
        stock_quote=entry_stock,
        policy=policy,
    )
    if candidates.empty:
        return pd.DataFrame()
    candidates = _attach_context(candidates, sample)
    candidates = _attach_modeled_pricing(candidates, policy=policy)
    state = infer_market_state(
        strategy_sample,
        surface=surface,
        probability_up=probability_up,
    )
    candidates = score_market_state_prior(
        candidates,
        state=state,
        policy=policy,
    )
    evaluated = []
    for candidate in candidates.to_dict("records"):
        outcome = evaluate_candidate_outcome(
            candidate,
            exit_chain,
            exit_surface=exit_surface,
            exit_stock_quote=exit_stock,
            policy=policy,
        )
        evaluated.append(
            {
                **candidate,
                **outcome,
                "execution_evidence_type": "MODELED_OPRA_OHLCV_1H",
                "execution_model_version": MODELED_EXECUTION_VERSION,
                "execution_model_fingerprint_sha256": haircuts.fingerprint,
                "entry_reference_at": entry_reference_at,
                "exit_reference_at": exit_reference_at,
                "source_target_window_start": target_start,
                "source_target_window_end": target_end,
                "execution_quality_pass": bool(
                    candidate["surface_quality_pass"]
                    and candidate["liquidity_policy_pass"]
                    and candidate["all_option_quotes_valid"]
                    and outcome.get("outcome_status") == "COMPLETE"
                    and outcome.get("exit_surface_quality_pass") is True
                    and outcome.get("exit_all_option_quotes_valid") is True
                ),
            }
        )
    frame = pd.DataFrame(evaluated)
    if frame.empty:
        return frame
    return frame.loc[
        frame["outcome_status"].eq("COMPLETE")
        & frame["execution_quality_pass"].fillna(False).astype(bool)
    ].reset_index(drop=True)


def _modeled_chain(
    raw_day: pd.DataFrame,
    *,
    symbol: str,
    hour: pd.Timestamp,
    available_at: pd.Timestamp,
    underlying: float,
    haircuts: ExecutionHaircutModel,
) -> pd.DataFrame:
    references = _hour_references(raw_day, symbol=symbol, hour=hour)
    if references.empty:
        raise ValueError("No OPRA hourly contract references at decision hour")
    parsed = references["contract_symbol"].astype("string").str.extract(_OCC)
    parsed.columns = ["occ_root", "expiration", "call_put_code", "strike_code"]
    root_matches = parsed["occ_root"].astype("string").str.strip().eq(symbol)
    frame = references.loc[root_matches].copy()
    parsed = parsed.loc[root_matches]
    frame["expiration_date"] = pd.to_datetime(
        parsed["expiration"], format="%y%m%d", utc=True, errors="coerce"
    )
    frame["call_put"] = parsed["call_put_code"].map(
        {"C": "CALL", "P": "PUT"}
    )
    frame["strike"] = pd.to_numeric(
        parsed["strike_code"], errors="coerce"
    ) / 1_000.0
    price = pd.to_numeric(frame["reference_price"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
    valid = (
        price.ge(_MINIMUM_MODELED_PREMIUM)
        & volume.ge(_MINIMUM_CONTRACT_HOUR_VOLUME)
        & frame["expiration_date"].notna()
        & frame["call_put"].notna()
        & pd.to_numeric(frame["strike"], errors="coerce").gt(0.0)
    )
    frame = frame.loc[valid].copy()
    if frame.empty:
        raise ValueError("No liquid standard OPRA hourly contracts survived")
    price = pd.to_numeric(frame["reference_price"], errors="coerce")
    applied = np.asarray([haircuts.haircut(float(value)) for value in price])
    frame["bid"] = np.maximum(
        0.0,
        price * (1.0 - applied) - _ABSOLUTE_PRICE_CUSHION,
    )
    frame["ask"] = (
        price * (1.0 + applied) + _ABSOLUTE_PRICE_CUSHION
    )
    midpoint = (frame["bid"] + frame["ask"]) / 2.0
    frame["relative_bid_ask_spread"] = (
        (frame["ask"] - frame["bid"]) / midpoint.where(midpoint.gt(0.0))
    )
    frame["symbol"] = symbol
    frame["underlying_price"] = float(underlying)
    frame["multiplier"] = 100.0
    frame["mini"] = False
    frame["non_standard"] = False
    frame["quote_valid"] = True
    frame["open_interest"] = np.nan
    frame["quote_staleness_seconds"] = 0.0
    frame["quote_timestamp"] = available_at
    frame["available_at"] = available_at
    frame["source_provider"] = MODELED_CHAIN_PROVIDER
    for greek in ("delta", "gamma", "theta", "vega"):
        frame[greek] = np.nan
    return frame.reset_index(drop=True)


def _attach_modeled_pricing(
    candidates: pd.DataFrame,
    *,
    policy: StrategySelectionPolicy,
) -> pd.DataFrame:
    output = candidates.copy()
    option_legs = pd.to_numeric(output["leg_count"], errors="coerce").clip(
        lower=1.0
    )
    uncertainty = (
        pd.to_numeric(output["mean_relative_spread"], errors="coerce")
        .fillna(0.5)
        .clip(lower=0.0)
        * pd.to_numeric(output["underlying_price"], errors="coerce")
        * option_legs
    )
    output["pricing_mode"] = "ACTIVE"
    output["pricing_status"] = "Modeled execution"
    output["pricing_leg_coverage"] = 1.0
    output["pricing_missing_reason"] = ""
    output["pricing_candidate_edge"] = 0.0
    output["pricing_conservative_edge"] = -uncertainty
    output["pricing_edge_to_friction"] = 0.0
    output["pricing_uncertainty"] = uncertainty
    output["pricing_probability_favorable"] = 0.5
    output["pricing_relative_edge"] = 0.0
    output["pricing_model_age_seconds"] = 0.0
    output["pricing_residual_shrinkage"] = 0.0
    output["pricing_source"] = MODELED_EXECUTION_SOURCE
    return output


def _hour_references(
    raw_day: pd.DataFrame,
    *,
    symbol: str,
    hour: object,
) -> pd.DataFrame:
    timestamp = _utc(hour)
    selected = raw_day.loc[
        pd.to_datetime(raw_day["ts_event"], utc=True, errors="coerce").eq(
            timestamp
        )
    ].copy()
    if selected.empty:
        return pd.DataFrame(columns=["contract_symbol", "reference_price", "volume"])
    selected["contract_symbol"] = selected["symbol"].astype("string").str.strip()
    selected["reference_price"] = pd.to_numeric(
        selected["close"], errors="coerce"
    )
    selected["volume"] = pd.to_numeric(
        selected["volume"], errors="coerce"
    ).fillna(0.0)
    valid = (
        selected["contract_symbol"].str.startswith(symbol)
        & np.isfinite(selected["reference_price"])
        & selected["reference_price"].gt(0.0)
        & selected["volume"].gt(0.0)
    )
    selected = selected.loc[valid].copy()
    selected["weight"] = selected["volume"].clip(lower=1.0)
    selected["weighted_price"] = (
        selected["reference_price"] * selected["weight"]
    )
    grouped = selected.groupby("contract_symbol", sort=False).agg(
        weighted_price=("weighted_price", "sum"),
        weight=("weight", "sum"),
        volume=("volume", "sum"),
    )
    grouped["reference_price"] = grouped["weighted_price"] / grouped["weight"]
    return grouped.reset_index().loc[
        :, ["contract_symbol", "reference_price", "volume"]
    ]


def _cbbo_near_hour_end(path: Path, *, hour_end: pd.Timestamp) -> pd.DataFrame:
    lower = hour_end - pd.Timedelta(minutes=5)
    upper = hour_end - pd.Timedelta(nanoseconds=1)
    columns = ["symbol", "bid_px_00", "ask_px_00"]
    try:
        raw = pd.read_parquet(
            path,
            columns=columns,
            filters=[("ts_recv", ">=", lower), ("ts_recv", "<=", upper)],
        ).reset_index()
    except Exception:
        raw = pd.read_parquet(path).reset_index()
        timestamps = pd.to_datetime(
            raw.get("ts_recv"), utc=True, errors="coerce"
        )
        raw = raw.loc[timestamps.ge(lower) & timestamps.le(upper)]
    normalized = normalize_cbbo_records(raw)
    if normalized.empty:
        return pd.DataFrame(
            columns=["contract_symbol", "actual_bid", "actual_ask"]
        )
    normalized = normalized.sort_values("quote_timestamp", kind="stable")
    latest = normalized.groupby("contract_symbol", sort=False).tail(1)
    latest = latest.rename(
        columns={"bid": "actual_bid", "ask": "actual_ask"}
    )
    return latest.loc[
        :, ["contract_symbol", "actual_bid", "actual_ask"]
    ].drop_duplicates("contract_symbol", keep="last")


@lru_cache(maxsize=32)
def _read_option_hour_day(path: str) -> pd.DataFrame:
    frame = pd.read_parquet(
        Path(path),
        columns=["symbol", "close", "volume"],
    ).reset_index()
    if "ts_event" not in frame:
        raise ValueError("OPRA hourly partition has no ts_event")
    return frame


@lru_cache(maxsize=12)
def _read_stock_hours(path: str) -> pd.DataFrame:
    frame = pd.read_parquet(Path(path))
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"], utc=True, errors="coerce"
    )
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _stock_quote(
    root: Path,
    *,
    symbol: str,
    hour: pd.Timestamp,
    available_at: pd.Timestamp,
) -> pd.Series:
    folder = root / "stocks" / symbol / "bars" / "1h" / "databento" / "normalized"
    paths = tuple(sorted(folder.glob("*_source_*_ohlcv-1h_1h.parquet")))
    if not paths:
        paths = tuple(sorted(folder.glob("*.parquet")))
    if not paths:
        raise FileNotFoundError(f"No hourly stock bars for {symbol}")
    bars = _read_stock_hours(str(paths[-1]))
    selected = bars.loc[bars["timestamp"].eq(hour)]
    if selected.empty:
        selected = bars.loc[
            bars["timestamp"].le(hour)
            & bars["timestamp"].dt.date.eq(hour.date())
        ].tail(1)
    if selected.empty:
        raise ValueError(f"No causal hourly stock reference for {symbol} {hour}")
    midpoint = float(pd.to_numeric(selected.iloc[-1]["close"], errors="coerce"))
    if not math.isfinite(midpoint) or midpoint <= 0.0:
        raise ValueError("Hourly stock reference is invalid")
    half_spread = max(0.01, midpoint * 0.0001)
    return pd.Series(
        {
            "symbol": symbol,
            "bid": midpoint - half_spread,
            "ask": midpoint + half_spread,
            "mid": midpoint,
            "available_at": available_at,
            "quote_quality_pass": True,
            "source_provider": "databento-equities-ohlcv-modeled",
        }
    )


def _overlap_dates(root: Path, symbols: Sequence[str]) -> list[str]:
    dates: set[str] | None = None
    for symbol in symbols:
        cbbo = {
            path.parent.parent.parent.name
            for path in canonical_root(root).glob(
                f"cbbo-1m/{symbol}.OPT/dates/*/segments/*/normalized.parquet"
            )
        }
        hourly = {
            path.parent.parent.parent.name
            for path in canonical_root(root).glob(
                f"ohlcv-1h/{symbol}.OPT/dates/*/segments/*/normalized.parquet"
            )
        }
        available = cbbo.intersection(hourly)
        dates = available if dates is None else dates.intersection(available)
    return sorted(dates or ())


def _opra_partition_path(
    root: Path,
    schema: str,
    symbol: str,
    session: str,
) -> Path:
    return (
        canonical_root(root)
        / schema
        / f"{symbol}.OPT"
        / "dates"
        / str(session)
        / "segments"
        / "full-day"
        / "normalized.parquet"
    )


def _sample_cache_key(
    sample: Mapping[str, object],
    *,
    haircuts: ExecutionHaircutModel,
) -> str:
    payload = {
        "version": MODELED_EXECUTION_VERSION,
        "execution_model": haircuts.fingerprint,
        "symbol": str(sample["symbol"]).strip().upper(),
        "horizon": str(sample["horizon"]).strip().lower(),
        "decision_timestamp": _utc(sample["decision_timestamp"]).isoformat(),
        "target_window_start": _utc(sample["target_window_start"]).isoformat(),
        "target_window_end": _utc(sample["target_window_end"]).isoformat(),
        "sample": {
            str(key): _stable(value)
            for key, value in sorted(sample.items(), key=lambda item: str(item[0]))
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _checkpoint_directory(root: Path, *, horizon: str, cache_key: str) -> Path:
    return (
        root
        / "ml"
        / "strategy-profit-modeled-outcomes"
        / MODELED_EXECUTION_VERSION
        / horizon
        / cache_key[:2]
        / cache_key
    )


def _read_checkpoint(
    root: Path,
    *,
    horizon: str,
    cache_key: str,
) -> tuple[pd.DataFrame, tuple[Path, ...]] | None:
    directory = _checkpoint_directory(root, horizon=horizon, cache_key=cache_key)
    data = directory / "outcomes.parquet"
    manifest = directory / "manifest.json"
    receipt = directory / "receipt.json"
    if not data.is_file() or not manifest.is_file() or not receipt.is_file():
        return None
    try:
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        info = manifest_payload["outcomes"]
        if (
            manifest_payload.get("schema_version") != MODELED_EXECUTION_VERSION
            or manifest_payload.get("cache_key") != cache_key
            or receipt_payload.get("schema_version")
            != MODELED_EXECUTION_RECEIPT_VERSION
            or receipt_payload.get("manifest_checksum_sha256")
            != file_checksum(manifest)
            or data.stat().st_size != int(info["size"])
            or file_checksum(data) != info["checksum_sha256"]
        ):
            return None
        return pd.read_parquet(data), (data, manifest, receipt)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _publish_checkpoint(
    root: Path,
    *,
    horizon: str,
    cache_key: str,
    frame: pd.DataFrame,
    sample: Mapping[str, object],
    execution_model_fingerprint: str,
) -> tuple[Path, ...]:
    directory = _checkpoint_directory(root, horizon=horizon, cache_key=cache_key)
    directory.mkdir(parents=True, exist_ok=True)
    data = directory / "outcomes.parquet"
    temporary = data.with_suffix(".parquet.tmp")
    frame.to_parquet(temporary, index=False)
    temporary.replace(data)
    manifest = directory / "manifest.json"
    _write_json_atomic(
        manifest,
        {
            "schema_version": MODELED_EXECUTION_VERSION,
            "cache_key": cache_key,
            "horizon": horizon,
            "symbol": str(sample["symbol"]).strip().upper(),
            "decision_timestamp": _utc(sample["decision_timestamp"]).isoformat(),
            "target_window_start": _utc(sample["target_window_start"]).isoformat(),
            "target_window_end": _utc(sample["target_window_end"]).isoformat(),
            "execution_model_fingerprint_sha256": execution_model_fingerprint,
            "outcomes": {
                "path": data.name,
                "rows": len(frame),
                "size": data.stat().st_size,
                "checksum_sha256": file_checksum(data),
            },
        },
    )
    receipt = directory / "receipt.json"
    _write_json_atomic(
        receipt,
        {
            "schema_version": MODELED_EXECUTION_RECEIPT_VERSION,
            "cache_key": cache_key,
            "manifest_checksum_sha256": file_checksum(manifest),
        },
    )
    return data, manifest, receipt


def _prediction_probabilities(
    predictions: pd.DataFrame,
) -> dict[tuple[str, str, pd.Timestamp, pd.Timestamp, pd.Timestamp], float]:
    output: dict[
        tuple[str, str, pd.Timestamp, pd.Timestamp, pd.Timestamp], float
    ] = {}
    selected = predictions.loc[
        predictions["prediction_status"].isin(("CREATED", "PREDICTED"))
    ]
    for row in selected.to_dict("records"):
        value = pd.to_numeric(row.get("calibrated_probability"), errors="coerce")
        if pd.notna(value):
            output[_prediction_key(row)] = float(value)
    return output


def _prediction_key(
    row: Mapping[str, object],
) -> tuple[str, str, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    return (
        str(row["symbol"]).strip().upper(),
        str(row["horizon"]).strip().lower(),
        _utc(row["decision_timestamp"]),
        _utc(row["target_window_start"]),
        _utc(row["target_window_end"]),
    )


def _attach_context(
    candidates: pd.DataFrame,
    sample: Mapping[str, object],
) -> pd.DataFrame:
    context = {
        str(column): value
        for column, value in sample.items()
        if column == "previous_period_direction" or "__" in str(column)
    }
    if not context:
        return candidates.copy()
    return pd.concat(
        (
            candidates.copy(),
            pd.DataFrame(
                {
                    column: [value] * len(candidates)
                    for column, value in context.items()
                },
                index=candidates.index,
            ),
        ),
        axis=1,
    )


def _premium_bucket(value: float) -> str:
    price = float(value)
    if price < 0.25:
        return "lt_0_25"
    if price < 1.0:
        return "0_25_to_1"
    if price < 5.0:
        return "1_to_5"
    return "ge_5"


def _stable(value: object) -> str:
    if value is None or value is pd.NA or value is pd.NaT:
        return "<null>"
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return "<null>"
    except (TypeError, ValueError):
        pass
    return str(value)


def _utc(value: object) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError("Strategy profit timestamp is invalid")
    return pd.Timestamp(parsed)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "ExecutionHaircutModel",
    "MODELED_EXECUTION_SOURCE",
    "MODELED_EXECUTION_VERSION",
    "MODELED_HORIZONS",
    "ModeledOutcomeResult",
    "build_modeled_strategy_outcomes",
    "calibrate_execution_haircuts",
]
