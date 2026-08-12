from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ml.artifacts import utc_timestamp
from ml.option_pricing.policies import (
    ContractSelectionPolicy,
    LOOP_NATIVE_SHADOW_SCHEMA_VERSION,
    LoopNativeModelPolicy,
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
)
from ml.option_pricing.prediction import create_bsgp_shadow_rows, create_prediction_rows
from ml.option_pricing.publication import (
    authoritative_option_pricing_runs,
    receipt_proven_prediction_rows,
)
from ml.option_pricing.schwab_materialization import (
    OFFLINE_SCHWAB_BOOTSTRAP,
    read_current_loop_native_schwab_materialization,
)
from ml.option_pricing.shadow_model import (
    LOOP_NATIVE_MODEL_FILE,
    LOOP_NATIVE_MODEL_MANIFEST,
    LOOP_NATIVE_MODEL_RECEIPT,
    LoopNativeModelGeneration,
    LoopNativeModelLoad,
    read_current_loop_native_model_generation,
    read_loop_native_model_generation,
)
from ml.option_pricing.target_outcome import authoritative_target_outcomes


STRATEGY_PRICING_EVIDENCE_VERSION = "strategy-option-pricing-evidence-v2"
# Kept as an import-compatible alias while old readers are migrated.
STRATEGY_PRICING_SHADOW_VERSION = STRATEGY_PRICING_EVIDENCE_VERSION
STRATEGY_PRICING_MODES = ("off", "shadow", "active")

_NORMAL = NormalDist()
_USABLE_PROJECTION_STATUSES = {
    "COMPLETE",
    "BASELINE_COPIED",
    "BASELINE_COPIED_SURFACE",
}
_BSGP_READY_STATUS = "BSGP_SHADOW_READY"
_PRICING_SOURCES = {"BSGP", "BLACK_SCHOLES"}


@dataclass(frozen=True)
class StrategyPricingEvidenceCatalog:
    predictions: pd.DataFrame
    source_files: tuple[Path, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyPricingShadowResult:
    candidates: pd.DataFrame
    source_files: tuple[Path, ...]
    report: Mapping[str, object]


def load_strategy_pricing_evidence(
    datastore_root: Path,
    *,
    available_not_after: object,
    include_offline_replay: bool = True,
) -> StrategyPricingEvidenceCatalog:
    """Load only receipt-verified live evidence and explicitly offline replay rows."""

    root = Path(datastore_root)
    cutoff = utc_timestamp(available_not_after)
    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    errors: list[str] = []

    try:
        live, live_files = _loop_native_target_predictions(
            root,
            available_not_after=cutoff,
        )
        if not live.empty:
            frames.append(live)
            files.extend(live_files)
    except Exception as exc:
        errors.append(f"loop_native:{type(exc).__name__}:{exc}")

    if not frames:
        try:
            legacy, legacy_files = _legacy_verified_predictions(
                root,
                available_not_after=cutoff,
            )
            if not legacy.empty:
                frames.append(legacy)
                files.extend(legacy_files)
        except Exception as exc:
            errors.append(f"legacy:{type(exc).__name__}:{exc}")

    if include_offline_replay:
        try:
            replay, replay_files = _offline_replay_predictions(
                root,
                available_not_after=cutoff,
            )
            if not replay.empty:
                frames.append(replay)
                files.extend(replay_files)
        except Exception as exc:
            errors.append(f"offline_replay:{type(exc).__name__}:{exc}")

    if not frames:
        return StrategyPricingEvidenceCatalog(
            pd.DataFrame(), tuple(dict.fromkeys(files)), tuple(errors)
        )
    predictions = pd.concat(frames, ignore_index=True, sort=False)
    for column in (
        "target_snapshot_for",
        "expiration_date",
        "source_snapshot_for",
        "source_available_at",
        "prediction_created_at",
        "prediction_available_at",
        "model_published_at",
    ):
        if column not in predictions:
            predictions[column] = pd.NaT
        predictions[column] = pd.to_datetime(
            predictions[column], utc=True, errors="coerce"
        )
    required_clocks = predictions[
        ["target_snapshot_for", "prediction_created_at", "prediction_available_at"]
    ]
    predictions = predictions.loc[~required_clocks.isna().any(axis=1)].copy()
    predictions["__lane_priority"] = np.where(
        predictions["evidence_lane"].astype("string").str.upper().eq("LIVE"),
        0,
        1,
    )
    predictions = predictions.sort_values(
        ["__lane_priority", "prediction_available_at", "prediction_created_at"],
        kind="stable",
    ).drop_duplicates(
        [
            "symbol",
            "target_snapshot_for",
            "call_put",
            "contract_symbol",
            "evidence_lane",
        ],
        keep="first",
    ).drop(columns="__lane_priority")
    return StrategyPricingEvidenceCatalog(
        predictions.reset_index(drop=True),
        tuple(dict.fromkeys(files)),
        tuple(errors),
    )


def attach_strategy_pricing_evidence(
    candidates: pd.DataFrame,
    *,
    catalog: StrategyPricingEvidenceCatalog,
    pricing_mode: str,
    per_contract_fee: float,
    allow_offline_replay: bool,
) -> StrategyPricingShadowResult:
    mode = _pricing_mode(pricing_mode)
    output = candidates.copy()
    if output.empty:
        return StrategyPricingShadowResult(
            output,
            catalog.source_files,
            _evidence_report(output, mode=mode, errors=catalog.errors),
        )
    if mode == "off":
        output = _unavailable_columns(
            output,
            mode="OFF",
            status="Unavailable",
            reason="PRICING_MODE_OFF",
        )
        return StrategyPricingShadowResult(
            output,
            (),
            _evidence_report(output, mode=mode, errors=catalog.errors),
        )
    if catalog.predictions.empty:
        output = _unavailable_columns(
            output,
            mode=mode.upper(),
            status="Delayed",
            reason="VERIFIED_PRICING_EVIDENCE_NOT_YET_AVAILABLE",
        )
        return StrategyPricingShadowResult(
            output,
            catalog.source_files,
            _evidence_report(output, mode=mode, errors=catalog.errors),
        )

    diagnostics = [
        _candidate_diagnostic(
            candidate,
            predictions=catalog.predictions,
            per_contract_fee=per_contract_fee,
            mode=mode,
            allow_offline_replay=allow_offline_replay,
        )
        for candidate in output.to_dict("records")
    ]
    diagnostic_frame = pd.DataFrame(diagnostics, index=output.index)
    for column in diagnostic_frame:
        output[column] = diagnostic_frame[column]
    return StrategyPricingShadowResult(
        output,
        catalog.source_files,
        _evidence_report(output, mode=mode, errors=catalog.errors),
    )


def attach_strategy_pricing_shadow(
    candidates: pd.DataFrame,
    *,
    datastore_root: Path,
    pricing_mode: str = "off",
    available_not_after: object,
    per_contract_fee: float,
) -> StrategyPricingShadowResult:
    """Compatibility entry point; active callers attach evidence before scoring."""

    catalog = load_strategy_pricing_evidence(
        datastore_root,
        available_not_after=available_not_after,
        include_offline_replay=True,
    )
    return attach_strategy_pricing_evidence(
        candidates,
        catalog=catalog,
        pricing_mode=pricing_mode,
        per_contract_fee=per_contract_fee,
        allow_offline_replay=False,
    )


def _loop_native_target_predictions(
    datastore_root: Path,
    *,
    available_not_after: pd.Timestamp,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    for outcome in authoritative_target_outcomes(datastore_root):
        if outcome.published_at > available_not_after:
            continue
        shadow = outcome.shadow_predictions()
        if shadow.empty:
            continue
        available = pd.to_datetime(
            shadow["prediction_available_at"], utc=True, errors="coerce"
        )
        shadow = shadow.loc[
            available.le(available_not_after)
            & ~shadow["automated_action_allowed"].fillna(True).astype(bool)
            & shadow["bsgp_shadow_fair_value_constrained"].notna()
            & shadow["bsgp_shadow_projection_status"].isin(
                _USABLE_PROJECTION_STATUSES
            )
            & shadow["shadow_schema_version"].eq(
                LOOP_NATIVE_SHADOW_SCHEMA_VERSION
            )
        ].copy()
        if shadow.empty:
            continue
        status = shadow["bsgp_shadow_status"].astype("string")
        canonical = shadow.copy()
        canonical["fair_value"] = shadow["bsgp_shadow_fair_value_constrained"]
        canonical["fair_value_95_lower"] = shadow[
            "bsgp_shadow_constrained_interval_95_lower"
        ]
        canonical["fair_value_95_upper"] = shadow[
            "bsgp_shadow_constrained_interval_95_upper"
        ]
        canonical["predictive_standard_deviation"] = shadow[
            "bsgp_shadow_predictive_standard_deviation"
        ]
        canonical["residual_shrinkage"] = shadow["bsgp_shadow_shrinkage"]
        canonical["pricing_source"] = np.where(
            status.eq(_BSGP_READY_STATUS), "BSGP", "BLACK_SCHOLES"
        )
        canonical["pricing_evidence_status"] = status
        canonical["model_published_at"] = shadow[
            "bsgp_shadow_model_published_at"
        ]
        canonical["input_staleness_seconds"] = _effective_input_staleness(
            shadow,
            reported_column="bsgp_shadow_input_staleness_seconds",
        )
        canonical["evidence_lane"] = "LIVE"
        frames.append(canonical)
        files.extend(
            (
                outcome.shadow_predictions_path,
                outcome.manifest_path,
                outcome.receipt_path,
            )
        )
    if not frames:
        return pd.DataFrame(), ()
    return pd.concat(frames, ignore_index=True, sort=False), tuple(
        dict.fromkeys(path for path in files if path is not None)
    )


def _legacy_verified_predictions(
    datastore_root: Path,
    *,
    available_not_after: pd.Timestamp,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    run = _latest_reachable_run(
        datastore_root, available_not_after=available_not_after
    )
    path = run / "pricing-predictions.parquet"
    predictions = receipt_proven_prediction_rows(datastore_root)
    available = pd.to_datetime(
        predictions.get("prediction_available_at"), utc=True, errors="coerce"
    )
    compatible = (
        predictions["schema_version"].eq(OPTION_PRICING_SCHEMA_VERSION)
        & predictions["pricing_policy_version"].eq(OPTION_PRICING_POLICY_VERSION)
        & predictions["timing_policy_version"].eq(
            OPTION_PRICING_TIMING_POLICY_VERSION
        )
    )
    predictions = predictions.loc[
        available.le(available_not_after)
        & compatible
        & predictions["prediction_status"].isin(("AVAILABLE", "CREATED"))
        & predictions["projection_status"].eq("COMPLETE")
        & ~predictions["automated_action_allowed"].fillna(True).astype(bool)
    ].copy()
    if predictions.empty:
        return pd.DataFrame(), ()
    predictions["fair_value"] = predictions["constrained_fair_value"]
    predictions["fair_value_95_lower"] = predictions[
        "constrained_interval_95_lower"
    ]
    predictions["fair_value_95_upper"] = predictions[
        "constrained_interval_95_upper"
    ]
    predictions["residual_shrinkage"] = np.where(
        predictions["model_name"].astype("string").str.lower().eq("bsgp"),
        1.0,
        0.0,
    )
    predictions["pricing_source"] = np.where(
        predictions["model_name"].astype("string").str.lower().eq("bsgp"),
        "BSGP",
        "BLACK_SCHOLES",
    )
    predictions["pricing_evidence_status"] = predictions["model_status"]
    predictions["model_published_at"] = predictions["prediction_available_at"]
    predictions["input_staleness_seconds"] = _effective_input_staleness(
        predictions,
        reported_column="source_quote_staleness_seconds",
    )
    predictions["evidence_lane"] = "LIVE"
    return predictions, (path, run / "manifest.json", run / "publication.json")


def _offline_replay_predictions(
    datastore_root: Path,
    *,
    available_not_after: pd.Timestamp,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    materialization = read_current_loop_native_schwab_materialization(
        datastore_root,
        load_samples=False,
    )
    if materialization.receipt is None or materialization.directory is None:
        return pd.DataFrame(), ()
    if utc_timestamp(materialization.receipt.get("published_at")) > available_not_after:
        return pd.DataFrame(), ()
    sample_path = materialization.directory / "causal-residual-samples.parquet"
    sample_stat = sample_path.stat()
    model_directory = ""
    model_size = 0
    model_modified_ns = 0
    try:
        generation = read_current_loop_native_model_generation(datastore_root)
        model_materialization = generation.manifest.get("materialization")
        expected_run = materialization.directory.relative_to(
            Path(datastore_root).resolve()
        ).as_posix()
        if (
            isinstance(model_materialization, Mapping)
            and model_materialization.get("run_path") == expected_run
        ):
            model_path = generation.directory / LOOP_NATIVE_MODEL_FILE
            model_stat = model_path.stat()
            model_directory = str(generation.directory.resolve())
            model_size = int(model_stat.st_size)
            model_modified_ns = int(model_stat.st_mtime_ns)
    except Exception:
        # Black-Scholes replay remains independently usable when no compatible
        # cross-fit model generation exists.
        pass
    samples = _cached_offline_replay_predictions(
        str(sample_path.resolve()),
        int(sample_stat.st_size),
        int(sample_stat.st_mtime_ns),
        str(Path(datastore_root).resolve()),
        model_directory,
        model_size,
        model_modified_ns,
    ).copy()
    if samples.empty:
        return pd.DataFrame(), ()
    projected = samples
    run = materialization.directory
    source_files = tuple(
        dict.fromkeys(
            (
                sample_path,
                run / "manifest.json",
                run / "receipt.json",
                *materialization.source_files,
                *(
                    (
                        Path(model_directory) / LOOP_NATIVE_MODEL_FILE,
                        Path(model_directory) / LOOP_NATIVE_MODEL_MANIFEST,
                        Path(model_directory) / LOOP_NATIVE_MODEL_RECEIPT,
                    )
                    if model_directory
                    else ()
                ),
            )
        )
    )
    return projected, source_files


@lru_cache(maxsize=8)
def _cached_offline_replay_predictions(
    sample_path: str,
    size: int,
    modified_ns: int,
    datastore_root: str,
    model_directory: str,
    model_size: int,
    model_modified_ns: int,
) -> pd.DataFrame:
    path = Path(sample_path)
    stat = path.stat()
    if stat.st_size != size or stat.st_mtime_ns != modified_ns:
        raise RuntimeError("Immutable pricing replay changed during cached read")
    samples = pd.read_parquet(path).drop(columns="id", errors="ignore")
    if samples.empty:
        return pd.DataFrame()
    lane = samples.get(
        "evidence_lane", pd.Series("", index=samples.index, dtype="string")
    ).astype("string")
    replay = samples.loc[
        samples["sample_status"].astype("string").eq("AVAILABLE")
        & lane.eq(OFFLINE_SCHWAB_BOOTSTRAP)
    ].copy()
    if replay.empty:
        return pd.DataFrame()
    replay["offline_emulated_prediction_at"] = pd.to_datetime(
        replay["offline_emulated_prediction_at"], utc=True, errors="coerce"
    )
    replay["observed_quote_timestamp"] = pd.to_datetime(
        replay["observed_quote_timestamp"], utc=True, errors="coerce"
    )
    replay["observed_available_at"] = pd.to_datetime(
        replay["observed_available_at"], utc=True, errors="coerce"
    )
    replay = replay.loc[
        replay["offline_emulated_prediction_at"].notna()
        & replay["observed_quote_timestamp"].gt(
            replay["offline_emulated_prediction_at"]
        )
        & replay["observed_available_at"].gt(
            replay["offline_emulated_prediction_at"]
        )
    ].copy()
    generation: LoopNativeModelGeneration | None = None
    model_policy: LoopNativeModelPolicy | None = None
    assessment_sessions: frozenset[str] = frozenset()
    crossfit_known_at: pd.Timestamp | None = None
    if model_directory:
        model_path = Path(model_directory) / LOOP_NATIVE_MODEL_FILE
        model_stat = model_path.stat()
        if (
            model_stat.st_size != model_size
            or model_stat.st_mtime_ns != model_modified_ns
        ):
            raise RuntimeError("Immutable BSGP cross-fit model changed during cached read")
        generation = read_loop_native_model_generation(
            Path(model_directory),
            datastore_root=Path(datastore_root),
        )
        model_policy = _loop_native_policy_from_manifest(generation.manifest)
        assessment_sessions, crossfit_known_at = _crossfit_boundary(
            replay,
            generation=generation,
        )
    projected: list[pd.DataFrame] = []
    for emulated_at, group in replay.groupby(
        "offline_emulated_prediction_at", sort=True
    ):
        priced = create_prediction_rows(
            group,
            prediction_created_at=emulated_at,
            prediction_available_at=emulated_at,
        )
        if priced.empty:
            continue
        priced["fair_value"] = priced["constrained_fair_value"]
        priced["fair_value_95_lower"] = priced[
            "constrained_interval_95_lower"
        ]
        priced["fair_value_95_upper"] = priced[
            "constrained_interval_95_upper"
        ]
        priced["residual_shrinkage"] = 0.0
        priced["pricing_source"] = "BLACK_SCHOLES"
        priced["pricing_evidence_status"] = "OFFLINE_REPLAY_BASELINE"
        priced["model_published_at"] = pd.NaT
        session = (
            pd.Timestamp(group["target_snapshot_for"].iloc[0])
            .tz_convert("America/New_York")
            .strftime("%Y-%m-%d")
        )
        if (
            generation is not None
            and model_policy is not None
            and crossfit_known_at is not None
            and session in assessment_sessions
            and pd.Timestamp(emulated_at) > crossfit_known_at
        ):
            shadow = create_bsgp_shadow_rows(
                group,
                priced,
                prediction_created_at=emulated_at,
                prediction_available_at=emulated_at,
                model_load=LoopNativeModelLoad(
                    generation,
                    "OFFLINE_CAUSAL_CROSSFIT",
                    "Chronological assessment replay using only earlier fit/calibration labels.",
                ),
                model_policy=model_policy,
            )
            shadow_keys = [
                "symbol",
                "target_snapshot_for",
                "call_put",
                "contract_symbol",
                "prediction_created_at",
            ]
            shadow_values = shadow.loc[
                :,
                [
                    *shadow_keys,
                    "bsgp_shadow_fair_value_raw",
                    "bsgp_shadow_fair_value_constrained",
                    "bsgp_shadow_constrained_interval_95_lower",
                    "bsgp_shadow_constrained_interval_95_upper",
                    "bsgp_shadow_predictive_standard_deviation",
                    "bsgp_shadow_normalized_residual",
                    "bsgp_shadow_shrinkage",
                    "bsgp_shadow_status",
                ],
            ]
            priced = priced.merge(
                shadow_values,
                on=shadow_keys,
                how="left",
                validate="one_to_one",
            )
            ready = priced["bsgp_shadow_status"].astype("string").eq(
                _BSGP_READY_STATUS
            )
            priced["fair_value"] = priced[
                "bsgp_shadow_fair_value_constrained"
            ]
            priced["fair_value_95_lower"] = priced[
                "bsgp_shadow_constrained_interval_95_lower"
            ]
            priced["fair_value_95_upper"] = priced[
                "bsgp_shadow_constrained_interval_95_upper"
            ]
            priced["predictive_standard_deviation"] = priced[
                "bsgp_shadow_predictive_standard_deviation"
            ]
            priced["residual_shrinkage"] = priced["bsgp_shadow_shrinkage"]
            priced["pricing_source"] = np.where(
                ready,
                "BSGP",
                "BLACK_SCHOLES",
            )
            priced["pricing_evidence_status"] = np.where(
                ready,
                "OFFLINE_CAUSAL_CROSSFIT_BSGP",
                priced["bsgp_shadow_status"],
            )
        priced["input_staleness_seconds"] = _effective_input_staleness(
            priced,
            reported_column="source_quote_staleness_seconds",
        )
        priced["evidence_lane"] = OFFLINE_SCHWAB_BOOTSTRAP
        projected.append(priced)
    if not projected:
        return pd.DataFrame()
    return pd.concat(projected, ignore_index=True, sort=False)


def _loop_native_policy_from_manifest(
    manifest: Mapping[str, object],
) -> LoopNativeModelPolicy:
    payload = manifest.get("finite_basis_policy")
    if not isinstance(payload, Mapping):
        raise RuntimeError("BSGP cross-fit model policy is missing")
    values = dict(payload)
    values["gamma_grid"] = tuple(values.get("gamma_grid", ()))
    return LoopNativeModelPolicy(**values)


def _crossfit_boundary(
    replay: pd.DataFrame,
    *,
    generation: LoopNativeModelGeneration,
) -> tuple[frozenset[str], pd.Timestamp | None]:
    partitions = generation.manifest.get("chronological_session_partitions")
    if not isinstance(partitions, Mapping):
        return frozenset(), None
    training = frozenset(str(value) for value in partitions.get("training", ()))
    calibration = frozenset(
        str(value) for value in partitions.get("calibration", ())
    )
    assessment = frozenset(str(value) for value in partitions.get("assessment", ()))
    if not training or not calibration or not assessment:
        return frozenset(), None
    sessions = (
        pd.to_datetime(replay["target_snapshot_for"], utc=True, errors="coerce")
        .dt.tz_convert("America/New_York")
        .dt.strftime("%Y-%m-%d")
    )
    observed = pd.to_datetime(
        replay["observed_available_at"], utc=True, errors="coerce"
    )
    known = observed.loc[sessions.isin(training | calibration)].dropna()
    if known.empty:
        return frozenset(), None
    known_at = pd.Timestamp(known.max())
    emulated = pd.to_datetime(
        replay.loc[sessions.isin(assessment), "offline_emulated_prediction_at"],
        utc=True,
        errors="coerce",
    ).dropna()
    if emulated.empty or not emulated.gt(known_at).all():
        return frozenset(), None
    return assessment, known_at


def _candidate_diagnostic(
    candidate: Mapping[str, object],
    *,
    predictions: pd.DataFrame,
    per_contract_fee: float,
    mode: str,
    allow_offline_replay: bool,
) -> dict[str, object]:
    try:
        legs = json.loads(str(candidate.get("legs_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        legs = []
    option_legs = [
        leg
        for leg in legs
        if isinstance(leg, Mapping)
        and str(leg.get("asset") or "").upper() == "OPTION"
    ]
    if not option_legs:
        return _diagnostic(
            mode=mode,
            status="Unavailable",
            coverage=1.0,
            reason="CANDIDATE_HAS_NO_OPTION_LEGS",
            source="NOT_APPLICABLE",
            edge=0.0,
            conservative_edge=0.0,
            edge_to_friction=0.0,
            uncertainty=0.0,
            probability_favorable=0.5,
            relative_edge=0.0,
            model_age_seconds=0.0,
            residual_shrinkage=0.0,
        )
    matched: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    missing: list[str] = []
    for leg in option_legs:
        prediction, reason = _matching_prediction(
            predictions,
            symbol=str(candidate.get("symbol") or ""),
            leg=leg,
            allow_offline_replay=allow_offline_replay,
        )
        if prediction is None:
            missing.append(f"{leg.get('contract_symbol')}:{reason}")
        else:
            matched.append((leg, prediction))
    coverage = len(matched) / len(option_legs)
    if missing:
        delayed = all(
            reason.rsplit(":", 1)[-1]
            in {"PREDICTION_NOT_COMMITTED_BEFORE_QUOTE", "PREDICTION_MISSING"}
            for reason in missing
        )
        return _diagnostic(
            mode=mode,
            status="Delayed" if delayed else "Unavailable",
            coverage=coverage,
            reason=";".join(missing),
        )

    edge = 0.0
    conservative_edge = 0.0
    uncertainty = 0.0
    exposure = 0.0
    ages: list[float] = []
    shrinkage_numerator = 0.0
    sources: list[str] = []
    for leg, prediction in matched:
        fair = _required_finite(prediction.get("fair_value"), "fair value")
        fair_lower = _required_finite(
            prediction.get("fair_value_95_lower"), "fair value 95 lower"
        )
        fair_upper = _required_finite(
            prediction.get("fair_value_95_upper"), "fair value 95 upper"
        )
        if not fair_lower <= fair <= fair_upper:
            raise ValueError("Pricing prediction intervals are not nested around fair value")
        bid = _required_finite(leg.get("bid"), "leg bid")
        ask = _required_finite(leg.get("ask"), "leg ask")
        quantity = _required_finite(leg.get("quantity"), "leg quantity")
        multiplier = _required_finite(leg.get("multiplier"), "leg multiplier")
        scale = quantity * multiplier
        if str(leg.get("side") or "").upper() == "LONG":
            edge += scale * (fair - ask)
            conservative_edge += scale * (fair_lower - ask)
        elif str(leg.get("side") or "").upper() == "SHORT":
            edge += scale * (bid - fair)
            conservative_edge += scale * (bid - fair_upper)
        else:
            raise ValueError("Option pricing evidence requires LONG or SHORT legs")
        standard_deviation = _finite(
            prediction.get("predictive_standard_deviation")
        )
        if standard_deviation is None or standard_deviation <= 0.0:
            standard_deviation = max(
                fair - fair_lower, fair_upper - fair
            ) / 1.959963984540054
        # This L1 interval aggregation is deliberately conservative. It does not
        # invent independence or cross-model CALL/PUT correlation.
        uncertainty += scale * standard_deviation
        underlying = _required_finite(
            prediction.get("underlying_price"), "prediction underlying"
        )
        leg_exposure = scale * underlying
        exposure += leg_exposure
        shrinkage_numerator += leg_exposure * (
            _finite(prediction.get("residual_shrinkage")) or 0.0
        )
        sources.append(str(prediction.get("pricing_source") or ""))
        ages.append(_prediction_age_seconds(prediction, leg=leg))

    friction = _round_trip_friction(legs, per_contract_fee=per_contract_fee)
    source = "BSGP" if set(sources) == {"BSGP"} else "BLACK_SCHOLES"
    status = "Active" if source == "BSGP" else "Black-Scholes fallback"
    probability = (
        float(_NORMAL.cdf(edge / uncertainty))
        if uncertainty > 0.0
        else (1.0 if edge > 0.0 else 0.0 if edge < 0.0 else 0.5)
    )
    return _diagnostic(
        mode=mode,
        status=status,
        coverage=coverage,
        reason="",
        source=source,
        edge=edge,
        conservative_edge=conservative_edge,
        edge_to_friction=edge / friction if friction > 0.0 else 0.0,
        uncertainty=uncertainty,
        probability_favorable=float(np.clip(probability, 0.0, 1.0)),
        relative_edge=edge / exposure if exposure > 0.0 else 0.0,
        model_age_seconds=max(ages) if ages else 0.0,
        residual_shrinkage=(
            shrinkage_numerator / exposure if exposure > 0.0 else 0.0
        ),
    )


def _matching_prediction(
    predictions: pd.DataFrame,
    *,
    symbol: str,
    leg: Mapping[str, object],
    allow_offline_replay: bool,
) -> tuple[Mapping[str, object] | None, str]:
    target = _timestamp(leg.get("target_snapshot_for"))
    quote = _timestamp(leg.get("quote_timestamp"))
    expiration = _timestamp(leg.get("expiration_date"))
    strike = _finite(leg.get("strike"))
    multiplier = _finite(leg.get("multiplier"))
    contract_symbol = str(leg.get("contract_symbol") or "").strip()
    call_put = str(leg.get("option_type") or "").strip().upper()
    if target is None:
        return None, "TARGET_SNAPSHOT_MISSING"
    if quote is None:
        return None, "LEG_QUOTE_TIMESTAMP_MISSING"
    if (
        expiration is None
        or strike is None
        or multiplier is None
        or not contract_symbol
        or call_put not in {"CALL", "PUT"}
    ):
        return None, "SEMANTIC_CONTRACT_INCOMPLETE"
    maximum_age = float(ContractSelectionPolicy().maximum_source_staleness_seconds)
    target_age = (quote - target).total_seconds()
    if target_age < 0.0 or target_age > maximum_age:
        return None, "TARGET_EVENT_STALE"
    expiration_values = pd.to_datetime(
        predictions["expiration_date"], utc=True, errors="coerce"
    )
    strikes = pd.to_numeric(predictions["strike"], errors="coerce")
    multipliers = pd.to_numeric(predictions["multiplier"], errors="coerce")
    matches = predictions.loc[
        predictions["symbol"]
        .astype("string")
        .str.upper()
        .eq(symbol.strip().upper())
        & predictions["target_snapshot_for"].eq(target)
        & predictions["contract_symbol"].astype("string").eq(contract_symbol)
        & predictions["call_put"].astype("string").str.upper().eq(call_put)
        & expiration_values.dt.normalize().eq(expiration.normalize())
        & strikes.sub(strike).abs().le(1e-9)
        & multipliers.sub(multiplier).abs().le(1e-9)
    ].copy()
    if not allow_offline_replay and not matches.empty:
        matches = matches.loc[
            matches["evidence_lane"].astype("string").str.upper().eq("LIVE")
        ]
    if matches.empty:
        return None, "PREDICTION_MISSING"
    matches = matches.loc[
        matches["prediction_created_at"].lt(quote)
        & matches["prediction_available_at"].lt(quote)
    ].copy()
    if matches.empty:
        return None, "PREDICTION_NOT_COMMITTED_BEFORE_QUOTE"
    input_age = pd.to_numeric(
        matches.get("input_staleness_seconds"), errors="coerce"
    )
    matches = matches.loc[
        input_age.between(0.0, maximum_age)
    ]
    if matches.empty:
        return None, "PRICING_INPUT_STALE"
    matches = matches.loc[
        matches["pricing_source"].astype("string").isin(_PRICING_SOURCES)
        & pd.to_numeric(matches["fair_value"], errors="coerce").notna()
        & pd.to_numeric(
            matches["fair_value_95_lower"], errors="coerce"
        ).notna()
        & pd.to_numeric(
            matches["fair_value_95_upper"], errors="coerce"
        ).notna()
    ]
    if matches.empty:
        return None, "PRICING_VALUE_INVALID"
    matches["__lane_priority"] = np.where(
        matches["evidence_lane"].astype("string").str.upper().eq("LIVE"),
        0,
        1,
    )
    matches = matches.sort_values(
        ["__lane_priority", "prediction_available_at", "prediction_created_at"],
        kind="stable",
    )
    return matches.drop(columns="__lane_priority").iloc[0].to_dict(), ""


def _prediction_age_seconds(
    prediction: Mapping[str, object],
    *,
    leg: Mapping[str, object],
) -> float:
    quote = _timestamp(leg.get("quote_timestamp"))
    published = _timestamp(prediction.get("model_published_at"))
    if published is None:
        published = _timestamp(prediction.get("prediction_available_at"))
    if quote is None or published is None:
        return 0.0
    return max((quote - published).total_seconds(), 0.0)


def _effective_input_staleness(
    frame: pd.DataFrame,
    *,
    reported_column: str,
) -> pd.Series:
    reported = pd.to_numeric(
        frame.get(
            reported_column,
            pd.Series(np.nan, index=frame.index, dtype=float),
        ),
        errors="coerce",
    )
    target = pd.to_datetime(
        frame.get(
            "target_snapshot_for",
            pd.Series(pd.NaT, index=frame.index),
        ),
        utc=True,
        errors="coerce",
    )
    source = pd.to_datetime(
        frame.get(
            "source_snapshot_for",
            pd.Series(pd.NaT, index=frame.index),
        ),
        utc=True,
        errors="coerce",
    )
    source_age = (target - source).dt.total_seconds()
    return pd.concat((reported, source_age), axis=1).max(
        axis=1, skipna=False
    )


def _round_trip_friction(
    legs: Sequence[object],
    *,
    per_contract_fee: float,
) -> float:
    friction = 0.0
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        quantity = _required_finite(leg.get("quantity"), "leg quantity")
        multiplier = _required_finite(leg.get("multiplier"), "leg multiplier")
        bid = _required_finite(leg.get("bid"), "leg bid")
        ask = _required_finite(leg.get("ask"), "leg ask")
        friction += quantity * multiplier * max(ask - bid, 0.0)
        if str(leg.get("asset") or "").upper() == "OPTION":
            friction += 2.0 * quantity * per_contract_fee
    return friction


def _latest_reachable_run(
    datastore_root: Path,
    *,
    available_not_after: object,
) -> Path:
    cutoff = utc_timestamp(available_not_after)
    reachable = authoritative_option_pricing_runs(datastore_root)
    eligible = [
        (run, published)
        for run, published in reachable.items()
        if published <= cutoff
    ]
    if not eligible:
        raise FileNotFoundError(
            "No reachable Pricing publication existed by Strategy cutoff"
        )
    return max(eligible, key=lambda item: item[1])[0]


def _unavailable_columns(
    frame: pd.DataFrame,
    *,
    mode: str,
    status: str,
    reason: str,
) -> pd.DataFrame:
    output = frame.copy()
    diagnostics = _diagnostic(
        mode=mode.lower(),
        status=status,
        coverage=0.0,
        reason=reason,
    )
    for column, value in diagnostics.items():
        output[column] = value
    return output


def _diagnostic(
    *,
    mode: str,
    status: str,
    coverage: float,
    reason: str,
    source: str = "UNAVAILABLE",
    edge: float | None = None,
    conservative_edge: float | None = None,
    edge_to_friction: float | None = None,
    uncertainty: float | None = None,
    probability_favorable: float | None = None,
    relative_edge: float | None = None,
    model_age_seconds: float | None = None,
    residual_shrinkage: float | None = None,
) -> dict[str, object]:
    return {
        "pricing_mode": str(mode).upper(),
        "pricing_status": status,
        "pricing_leg_coverage": float(coverage),
        "pricing_missing_reason": reason,
        "pricing_candidate_edge": edge,
        "pricing_conservative_edge": conservative_edge,
        "pricing_edge_to_friction": edge_to_friction,
        "pricing_uncertainty": uncertainty,
        "pricing_probability_favorable": probability_favorable,
        "pricing_relative_edge": relative_edge,
        "pricing_model_age_seconds": model_age_seconds,
        "pricing_residual_shrinkage": residual_shrinkage,
        "pricing_source": source,
    }


def _evidence_report(
    frame: pd.DataFrame,
    *,
    mode: str,
    errors: Sequence[str],
) -> dict[str, object]:
    status = frame.get(
        "pricing_status", pd.Series("", index=frame.index, dtype="string")
    ).astype("string")
    covered = status.isin(("Active", "Black-Scholes fallback"))
    return {
        "schema_version": STRATEGY_PRICING_EVIDENCE_VERSION,
        "mode": mode,
        "candidate_rows": len(frame),
        "covered_rows": int(covered.sum()),
        "complete_coverage_fraction": float(covered.mean()) if len(frame) else 0.0,
        "status_counts": {
            str(key): int(value) for key, value in status.value_counts().items()
        },
        "load_errors": list(errors),
        "joint_uncertainty_policy": (
            "sum-of-leg-posterior-standard-deviations; conservative L1 interval "
            "bound; no invented CALL/PUT cross-model correlation"
        ),
        "rankings_changed_during_attachment": False,
        "order_construction_changed": False,
    }


def _pricing_mode(value: str) -> str:
    mode = str(value).strip().lower()
    if mode not in STRATEGY_PRICING_MODES:
        raise ValueError("pricing_mode must be off, shadow, or active")
    return mode


def _required_finite(value: object, label: str) -> float:
    parsed = _finite(value)
    if parsed is None:
        raise ValueError(f"Pricing evidence requires finite {label}")
    return parsed


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _timestamp(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


__all__ = [
    "STRATEGY_PRICING_EVIDENCE_VERSION",
    "STRATEGY_PRICING_MODES",
    "STRATEGY_PRICING_SHADOW_VERSION",
    "StrategyPricingEvidenceCatalog",
    "StrategyPricingShadowResult",
    "attach_strategy_pricing_evidence",
    "attach_strategy_pricing_shadow",
    "load_strategy_pricing_evidence",
]
