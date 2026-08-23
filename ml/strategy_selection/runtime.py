from __future__ import annotations

import hashlib
import json
from collections import Counter, OrderedDict
from pathlib import Path
from threading import RLock
from typing import Mapping, Sequence

import pandas as pd

from ml.option_pricing.strategy_shadow import (
    STRATEGY_PRICING_EVIDENCE_VERSION,
    STRATEGY_PRICING_MODES,
    StrategyPricingEvidenceCatalog,
    attach_strategy_pricing_evidence,
    load_strategy_pricing_evidence,
)
from ml.option_pricing_opra_replay import ensure_opra_pricing_replay

from ml.strategy_selection.candidates import (
    construct_strategy_candidates,
    evaluate_candidate_outcome,
)
from ml.strategy_selection.chain import (
    OptionChainHistory,
    entry_chain_receipt,
    entry_stock_quote,
    exit_chain_receipt,
    exit_stock_quote,
    load_option_chain_history,
)
from ml.strategy_selection.contracts import (
    MARKET_STATE_POLICY_VERSION,
    STRATEGY_CANDIDATE_POLICY_VERSION,
    STRATEGY_OUTCOME_POLICY_VERSION,
    STRATEGY_PRIOR_POLICY_VERSION,
    STRATEGY_REGISTRY_VERSION,
    StrategySelectionPolicy,
    StrategySelectionRun,
)
from ml.strategy_selection.market_state import (
    infer_market_state,
    score_market_state_prior,
)
from ml.strategy_selection.model import (
    fit_or_reuse_strategy_model,
    partition_strategy_outcomes,
    score_strategy_candidates,
)
from ml.strategy_selection.opra_cache import (
    ensure_opra_strategy_cache,
    strategy_opra_prediction_clocks,
)
from ml.strategy_selection.outcome_store import (
    publish_strategy_outcome_artifact,
    read_strategy_outcome_artifact,
)
from ml.strategy_selection.registry import STRATEGY_REGISTRY


_EXIT_DELAYS = {
    "1h": pd.Timedelta(hours=2),
    "4h": pd.Timedelta(hours=6),
    "1d": pd.Timedelta(days=2),
    "1w": pd.Timedelta(days=4),
    "1w-d1": pd.Timedelta(days=2),
    "1w-d2": pd.Timedelta(days=2),
    "1w-d3": pd.Timedelta(days=2),
    "1w-d4": pd.Timedelta(days=2),
    "1w-d5": pd.Timedelta(days=2),
}

_HISTORICAL_OUTCOME_CACHE_LIMIT = 4_096
_HISTORICAL_OUTCOME_CACHE: OrderedDict[
    str, tuple[pd.DataFrame, int, Mapping[str, int], tuple[Path, ...]]
] = OrderedDict()
_HISTORICAL_OUTCOME_CACHE_LOCK = RLock()


def run_strategy_selection(
    datastore_root: Path,
    *,
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    forbidden_target_starts: Mapping[str, Sequence[object]],
    run_timestamp: object,
    input_available_at: object,
    policy: StrategySelectionPolicy | None = None,
    sample_source_files: Sequence[Path] = (),
    history_available_not_after: object | None = None,
    pricing_mode: str = "off",
    pricing_catalog: StrategyPricingEvidenceCatalog | None = None,
) -> StrategySelectionRun:
    effective_policy = policy or StrategySelectionPolicy()
    _validate_inputs(samples, predictions)
    _assert_lockbox_excluded(
        samples,
        forbidden_target_starts=forbidden_target_starts,
    )
    created = _utc(run_timestamp)
    input_cutoff = _utc(input_available_at)
    mode = str(pricing_mode).strip().lower()
    if mode not in STRATEGY_PRICING_MODES:
        raise ValueError("pricing_mode must be off, shadow, or active")
    symbols = tuple(
        sorted(set(samples["symbol"].astype("string").str.upper()))
    )
    replay_bootstrap_error: str | None = None
    if mode != "off":
        try:
            opra_target_clocks = strategy_opra_prediction_clocks(
                datastore_root,
                samples=samples,
                symbols=symbols,
            )
            ensure_opra_pricing_replay(
                datastore_root,
                symbols=symbols,
                published_at=created,
                target_clocks=opra_target_clocks,
            )
        except Exception as exc:
            replay_bootstrap_error = f"{type(exc).__name__}: {exc}"
    catalog = pricing_catalog
    if catalog is None:
        catalog = (
            load_strategy_pricing_evidence(
                datastore_root,
                available_not_after=created,
                include_offline_replay=True,
            )
            if mode != "off"
            else StrategyPricingEvidenceCatalog(pd.DataFrame(), ())
        )
    if replay_bootstrap_error is not None:
        catalog = StrategyPricingEvidenceCatalog(
            catalog.predictions,
            catalog.source_files,
            (*catalog.errors, f"opra_replay_bootstrap:{replay_bootstrap_error}"),
            catalog.authority_states,
        )
    prediction_probabilities = _prediction_probabilities(predictions)
    histories: dict[str, OptionChainHistory] = {}
    source_files: list[Path] = []
    source_files.extend(catalog.source_files)
    history_errors: dict[str, str] = {}
    try:
        opra_cache = ensure_opra_strategy_cache(
            datastore_root,
            samples=samples,
            symbols=symbols,
            published_at=created,
        )
        if opra_cache is not None:
            source_files.extend(opra_cache.source_files)
    except Exception as exc:
        history_errors["__opra_observed_outcome_cache__"] = (
            f"{type(exc).__name__}: {exc}"
        )
    for symbol in symbols:
        try:
            history = load_option_chain_history(
                datastore_root,
                symbol=str(symbol),
                available_not_after=history_available_not_after,
                allow_historical_opra_replay=False,
            )
        except Exception as exc:
            history_errors[str(symbol)] = f"{type(exc).__name__}: {exc}"
            continue
        histories[str(symbol)] = history
        source_files.extend(history.source_files)

    model_reports: dict[str, Mapping[str, object]] = {}
    models: dict[str, object] = {}
    models_trained = 0
    models_reused = 0
    completed = samples.loc[samples["label_status"].eq("COMPLETE")].copy()
    required_decisions = (
        effective_policy.minimum_train_decisions
        + effective_policy.calibration_decisions
        + effective_policy.assessment_decisions
    )
    lockbox_boundaries: dict[str, pd.Timestamp] = {}
    for horizon, raw_values in forbidden_target_starts.items():
        values = tuple(raw_values)
        if values:
            lockbox_boundaries[horizon] = min(_utc(value) for value in values)
    for horizon in tuple(dict.fromkeys(samples["horizon"].astype(str))):
        horizon_samples = completed.loc[completed["horizon"].eq(horizon)].copy()
        outcomes, outcome_report, outcome_files = _historical_outcomes(
            horizon_samples,
            datastore_root=Path(datastore_root),
            horizon=horizon,
            histories=histories,
            prediction_probabilities=prediction_probabilities,
            policy=effective_policy,
            strictly_before=lockbox_boundaries.get(horizon),
            pricing_mode=mode,
            pricing_catalog=catalog,
        )
        source_files.extend(outcome_files)
        if outcomes.empty:
            model_outcomes = outcomes
            eligibility_report = _empty_pricing_eligibility_report()
        else:
            pricing_eligible = _pricing_model_eligible(outcomes)
            model_outcomes = outcomes.loc[pricing_eligible].reset_index(drop=True)
            eligibility_report = _pricing_model_eligibility_report(
                outcomes,
                eligible=pricing_eligible,
            )
        outcome_report = {**outcome_report, **eligibility_report}
        if model_outcomes.empty:
            complete_outcome_rows = int(
                outcome_report.get("complete_outcome_rows", 0)
            )
            pricing_excluded_rows = int(
                outcome_report.get("pricing_excluded_outcome_rows", 0)
            )
            pricing_gate_excluded_all = (
                complete_outcome_rows > 0
                and pricing_excluded_rows == complete_outcome_rows
            )
            model_reports[horizon] = {
                "status": "MODEL_NOT_FIT",
                "reason": (
                    "Complete observed-BBO outcomes exist, but none passed the "
                    "exact causal Pricing evidence gate."
                    if pricing_gate_excluded_all
                    else "No complete observed-BBO candidate outcomes were materialized."
                ),
                "calibration_status": (
                    "NOT_ATTEMPTED_NO_PRICING_ELIGIBLE_OUTCOMES"
                    if pricing_gate_excluded_all
                    else "NOT_ATTEMPTED_NO_OUTCOMES"
                ),
                **outcome_report,
                "required_decision_clusters": required_decisions,
                "usable_decision_clusters": 0,
                "real_lockbox_used": False,
            }
            continue
        try:
            partitions = partition_strategy_outcomes(
                model_outcomes,
                policy=effective_policy,
            )
            model = fit_or_reuse_strategy_model(
                datastore_root,
                horizon=horizon,
                partitions=partitions,
                policy=effective_policy,
                input_files=tuple(
                    dict.fromkeys((*sample_source_files, *source_files))
                ),
                trained_at=created,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            model_reports[horizon] = {
                "status": "MODEL_NOT_FIT",
                "reason": reason,
                "calibration_status": (
                    "UNAVAILABLE"
                    if "calibration unavailable" in reason.lower()
                    else "NOT_AVAILABLE_MODEL_NOT_FIT"
                ),
                **outcome_report,
                "complete_outcome_rows": len(outcomes),
                "usable_decision_clusters": int(
                    model_outcomes["target_window_start"].nunique()
                ),
                "required_decision_clusters": required_decisions,
                "real_lockbox_used": False,
            }
            continue
        models[horizon] = model
        models_trained += int(not model.reused)
        models_reused += int(model.reused)
        model_reports[horizon] = {
            "status": "MODEL_FIT",
            "calibration_status": "AVAILABLE",
            **outcome_report,
            "complete_outcome_rows": len(outcomes),
            "usable_decision_clusters": int(
                model_outcomes["target_window_start"].nunique()
            ),
            "required_decision_clusters": required_decisions,
            "artifact_directory": str(model.artifact_directory),
            "offline_evaluation": dict(model.offline_evaluation),
            "real_lockbox_used": False,
        }

    live_predictions = _canonical_live_predictions(predictions)
    candidate_frames: list[pd.DataFrame] = []
    audit_frames: list[pd.DataFrame] = []
    for prediction in live_predictions.to_dict("records"):
        symbol = str(prediction["symbol"]).strip().upper()
        horizon = str(prediction["horizon"]).strip().lower()
        sample = _matching_sample(samples, prediction)
        history = histories.get(symbol)
        if history is None:
            reason = history_errors.get(
                symbol, "OPRA-first provider-neutral chain history unavailable"
            )
            audit_frames.append(
                _failed_route_audit(
                    sample,
                    reason=reason,
                    construction_status="CHAIN_HISTORY_UNAVAILABLE",
                )
            )
            continue
        entry = entry_chain_receipt(
            history,
            minimum_snapshot_for=sample["bar_end_timestamp"],
            information_available_at=sample["information_available_at"],
            target_window_start=sample["target_window_start"],
            known_at=input_cutoff,
        )
        if entry is None:
            reason = (
                "No causally eligible point-in-time option-chain receipt was available "
                "by the Strategy run cutoff and before the target window."
            )
            audit_frames.append(
                _failed_route_audit(
                    sample,
                    reason=reason,
                    construction_status="ENTRY_RECEIPT_UNAVAILABLE",
                )
            )
            continue
        stock_quote = entry_stock_quote(
            history,
            information_available_at=sample["information_available_at"],
            target_window_start=sample["target_window_start"],
            known_at=input_cutoff,
        )
        entry_contracts = _attach_causal_underlying_quote(
            entry.contracts,
            stock_quote=stock_quote,
        )
        try:
            candidates, audit = construct_strategy_candidates(
                sample,
                entry_contracts,
                surface=entry.surface,
                stock_quote=stock_quote,
                policy=effective_policy,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            audit_frames.append(
                _failed_route_audit(
                    sample,
                    reason=reason,
                    construction_status="CANDIDATE_CONSTRUCTION_FAILED",
                )
            )
            continue
        audit_frames.append(audit)
        if candidates.empty:
            continue
        candidates = _attach_context(candidates, sample)
        pricing = attach_strategy_pricing_evidence(
            candidates,
            catalog=catalog,
            pricing_mode=mode,
            per_contract_fee=effective_policy.per_contract_fee,
            allow_offline_replay=False,
        )
        candidates = pricing.candidates
        state = infer_market_state(
            sample,
            surface=entry.surface,
            probability_up=float(prediction["calibrated_probability"]),
        )
        candidates = score_market_state_prior(
            candidates,
            state=state,
            policy=effective_policy,
        )
        model = models.get(horizon)
        if model is None:
            candidate_frames.append(candidates)
            continue
        eligible = _pricing_model_eligible(candidates)
        scored_frames = [candidates.loc[~eligible].copy()]
        if eligible.any():
            scored_frames.append(
                score_strategy_candidates(
                    model,  # type: ignore[arg-type]
                    candidates.loc[eligible].copy(),
                )
            )
        scored = _rerank_candidates(
            pd.concat(scored_frames, ignore_index=True, sort=False)
        )
        candidate_frames.append(scored)

    candidates = (
        pd.concat(candidate_frames, ignore_index=True, sort=False)
        if candidate_frames
        else pd.DataFrame()
    )
    audit = (
        pd.concat(audit_frames, ignore_index=True, sort=False)
        if audit_frames
        else pd.DataFrame()
    )
    return StrategySelectionRun(
        candidates=candidates,
        audit=audit,
        source_files=tuple(dict.fromkeys(source_files)),
        model_reports=model_reports,
        models_trained=models_trained,
        models_reused=models_reused,
        pricing_report=_pricing_report(candidates, mode=mode, catalog=catalog),
    )


def _historical_outcomes(
    samples: pd.DataFrame,
    *,
    datastore_root: Path,
    horizon: str,
    histories: Mapping[str, OptionChainHistory],
    prediction_probabilities: Mapping[
        tuple[str, str, pd.Timestamp, pd.Timestamp, pd.Timestamp], float
    ],
    policy: StrategySelectionPolicy,
    strictly_before: pd.Timestamp | None,
    pricing_mode: str,
    pricing_catalog: StrategyPricingEvidenceCatalog,
) -> tuple[pd.DataFrame, dict[str, object], tuple[Path, ...]]:
    outcome_frames: list[pd.DataFrame] = []
    failures: Counter[str] = Counter()
    candidate_rows = 0
    cache_hits = 0
    cache_misses = 0
    persistent_hits = 0
    persistent_misses = 0
    persistent_published = 0
    outcome_files: list[Path] = []
    possible_samples, coverage_failures = _samples_with_possible_receipts(
        samples,
        histories=histories,
        strictly_before=strictly_before,
    )
    failures.update(coverage_failures)
    for sample in possible_samples.sort_values(
        ["target_window_start", "symbol", "decision_timestamp"],
        kind="mergesort",
    ).to_dict("records"):
        symbol = str(sample["symbol"]).strip().upper()
        history = histories.get(symbol)
        if history is None:
            failures["chain_history_unavailable"] += 1
            continue
        entry = entry_chain_receipt(
            history,
            minimum_snapshot_for=sample["bar_end_timestamp"],
            information_available_at=sample["information_available_at"],
            target_window_start=sample["target_window_start"],
            known_at=pd.Timestamp(sample["target_window_start"])
            - pd.Timedelta(nanoseconds=1),
            receipt_choice="earliest",
        )
        if entry is None:
            failures["entry_receipt_unavailable"] += 1
            continue
        maximum_delay = _EXIT_DELAYS[str(sample["horizon"])]
        exit_receipt = exit_chain_receipt(
            history,
            target_window_end=sample["target_window_end"],
            maximum_delay=maximum_delay,
            strictly_before=strictly_before,
        )
        if exit_receipt is None:
            failures["exit_receipt_unavailable"] += 1
            continue
        stock_entry = entry_stock_quote(
            history,
            information_available_at=sample["information_available_at"],
            target_window_start=sample["target_window_start"],
            known_at=pd.Timestamp(sample["target_window_start"])
            - pd.Timedelta(nanoseconds=1),
            receipt_choice="earliest",
        )
        stock_exit = exit_stock_quote(
            history,
            target_window_end=sample["target_window_end"],
            maximum_delay=maximum_delay,
            strictly_before=strictly_before,
        )
        probability_up = prediction_probabilities.get(_prediction_key(sample))
        cache_key = _historical_outcome_cache_key(
            sample,
            entry_contracts=entry.contracts,
            entry_surface=entry.surface,
            exit_contracts=exit_receipt.contracts,
            exit_surface=exit_receipt.surface,
            stock_entry=stock_entry,
            stock_exit=stock_exit,
            probability_up=probability_up,
            pricing_mode=pricing_mode,
            pricing_catalog=pricing_catalog,
            policy=policy,
        )
        cached = _historical_outcome_cache_get(cache_key)
        if cached is not None:
            (
                cached_frame,
                cached_candidate_rows,
                cached_failures,
                cached_files,
            ) = cached
            outcome_frames.append(cached_frame)
            candidate_rows += cached_candidate_rows
            failures.update(cached_failures)
            outcome_files.extend(cached_files)
            cache_hits += 1
            continue
        stored = read_strategy_outcome_artifact(
            datastore_root,
            horizon=horizon,
            cache_key=cache_key,
        )
        if stored is not None:
            outcome_frames.append(stored.frame)
            candidate_rows += stored.candidate_rows
            failures.update(stored.failures)
            outcome_files.extend(stored.evidence_files)
            persistent_hits += 1
            cache_hits += 1
            _historical_outcome_cache_put(
                cache_key,
                stored.frame,
                candidate_rows=stored.candidate_rows,
                failures=stored.failures,
                evidence_files=stored.evidence_files,
            )
            continue
        persistent_misses += 1
        cache_misses += 1
        observation_failures: Counter[str] = Counter()
        try:
            candidates, _audit = construct_strategy_candidates(
                sample,
                entry.contracts,
                surface=entry.surface,
                stock_quote=stock_entry,
                policy=policy,
            )
        except Exception:
            failures["candidate_construction_failed"] += 1
            continue
        if candidates.empty:
            failures["no_constructible_candidate"] += 1
            continue
        candidates = _attach_context(candidates, sample)
        pricing = attach_strategy_pricing_evidence(
            candidates,
            catalog=pricing_catalog,
            pricing_mode=pricing_mode,
            per_contract_fee=policy.per_contract_fee,
            allow_offline_replay=True,
        )
        candidates = pricing.candidates
        state = infer_market_state(
            sample,
            surface=entry.surface,
            probability_up=probability_up,
        )
        candidates = score_market_state_prior(
            candidates,
            state=state,
            policy=policy,
        )
        candidate_rows += len(candidates)
        evaluated: list[dict[str, object]] = []
        for candidate in candidates.to_dict("records"):
            result = evaluate_candidate_outcome(
                candidate,
                exit_receipt.contracts,
                exit_surface=exit_receipt.surface,
                exit_stock_quote=stock_exit,
                policy=policy,
            )
            evaluated.append({**candidate, **result})
            if result["outcome_status"] != "COMPLETE":
                observation_failures[str(result["outcome_status"]).lower()] += 1
        evaluated_frame = pd.DataFrame(evaluated)
        failures.update(observation_failures)
        outcome_frames.append(evaluated_frame)
        stored = publish_strategy_outcome_artifact(
            datastore_root,
            horizon=horizon,
            cache_key=cache_key,
            frame=evaluated_frame,
            candidate_rows=len(candidates),
            failures=observation_failures,
        )
        outcome_files.extend(stored.evidence_files)
        persistent_published += 1
        _historical_outcome_cache_put(
            cache_key,
            evaluated_frame,
            candidate_rows=len(candidates),
            failures=observation_failures,
            evidence_files=stored.evidence_files,
        )
    outcomes = (
        pd.concat(outcome_frames, ignore_index=True, sort=False)
        if outcome_frames
        else pd.DataFrame()
    )
    complete_rows = (
        int(outcomes["outcome_status"].eq("COMPLETE").sum())
        if not outcomes.empty
        else 0
    )
    complete = outcomes.loc[
        outcomes["outcome_status"].eq("COMPLETE")
    ].reset_index(drop=True) if not outcomes.empty else outcomes
    report = {
        "sample_rows_considered": len(samples),
        "candidate_rows_constructed": candidate_rows,
        "complete_outcome_rows": complete_rows,
        "failures": dict(sorted(failures.items())),
        "incremental_outcome_cache_hits": cache_hits,
        "incremental_outcome_cache_misses": cache_misses,
        "process_memory_outcome_cache_hits": cache_hits - persistent_hits,
        "persistent_outcome_cache_hits": persistent_hits,
        "persistent_outcome_cache_misses": persistent_misses,
        "persistent_outcome_artifacts_published": persistent_published,
    }
    return complete, report, tuple(dict.fromkeys(outcome_files))


def _attach_causal_underlying_quote(
    contracts: pd.DataFrame,
    *,
    stock_quote: pd.Series | None,
) -> pd.DataFrame:
    """Fill an absent OPRA underlying only from the already-cutoff stock BBO."""

    observed = pd.to_numeric(contracts["underlying_price"], errors="coerce")
    if observed.notna().any() or stock_quote is None:
        return contracts
    midpoint = pd.to_numeric(
        pd.Series([stock_quote.get("mid")]), errors="coerce"
    ).iloc[0]
    if pd.isna(midpoint):
        bid = pd.to_numeric(
            pd.Series([stock_quote.get("bid")]), errors="coerce"
        ).iloc[0]
        ask = pd.to_numeric(
            pd.Series([stock_quote.get("ask")]), errors="coerce"
        ).iloc[0]
        midpoint = (bid + ask) / 2.0
    if pd.isna(midpoint) or float(midpoint) <= 0.0:
        return contracts
    output = contracts.copy()
    output["underlying_price"] = float(midpoint)
    return output


def _historical_outcome_cache_key(
    sample: Mapping[str, object],
    *,
    entry_contracts: pd.DataFrame,
    entry_surface: pd.Series,
    exit_contracts: pd.DataFrame,
    exit_surface: pd.Series,
    stock_entry: pd.Series | None,
    stock_exit: pd.Series | None,
    probability_up: float | None,
    pricing_mode: str,
    pricing_catalog: StrategyPricingEvidenceCatalog,
    policy: StrategySelectionPolicy,
) -> str:
    """Fingerprint only immutable evidence used by one historical observation."""

    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "candidate_policy": STRATEGY_CANDIDATE_POLICY_VERSION,
                "outcome_policy": STRATEGY_OUTCOME_POLICY_VERSION,
                "market_state_policy": MARKET_STATE_POLICY_VERSION,
                "prior_policy": STRATEGY_PRIOR_POLICY_VERSION,
                "registry": STRATEGY_REGISTRY_VERSION,
                "pricing_evidence": STRATEGY_PRICING_EVIDENCE_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(repr(policy).encode("utf-8"))
    digest.update(str(pricing_mode).encode("utf-8"))
    digest.update(repr(probability_up).encode("utf-8"))
    _update_frame_digest(digest, "sample", pd.DataFrame([sample]))
    _update_frame_digest(digest, "entry-contracts", entry_contracts)
    _update_frame_digest(digest, "entry-surface", entry_surface.to_frame().T)
    _update_frame_digest(digest, "exit-contracts", exit_contracts)
    _update_frame_digest(digest, "exit-surface", exit_surface.to_frame().T)
    _update_frame_digest(
        digest,
        "stock-entry",
        stock_entry.to_frame().T if stock_entry is not None else pd.DataFrame(),
    )
    _update_frame_digest(
        digest,
        "stock-exit",
        stock_exit.to_frame().T if stock_exit is not None else pd.DataFrame(),
    )
    target = pd.Timestamp(entry_surface["snapshot_for"])
    symbol = str(sample["symbol"]).strip().upper()
    pricing = pricing_catalog.predictions
    if not pricing.empty:
        pricing_target = pd.to_datetime(
            pricing["target_snapshot_for"], utc=True, errors="coerce"
        )
        pricing = pricing.loc[
            pricing["symbol"].astype("string").str.upper().eq(symbol)
            & pricing_target.eq(target)
        ]
    _update_frame_digest(digest, "pricing", pricing)
    return digest.hexdigest()


def _update_frame_digest(
    digest: "hashlib._Hash",
    label: str,
    frame: pd.DataFrame,
) -> None:
    digest.update(label.encode("utf-8"))
    if frame.empty:
        digest.update(b"<empty>")
        return
    normalized = frame.reindex(sorted(frame.columns), axis=1).copy()
    for column in normalized.columns:
        normalized[column] = normalized[column].map(_stable_cache_value)
    row_hashes = pd.util.hash_pandas_object(
        normalized,
        index=False,
        categorize=True,
    ).to_numpy(dtype="uint64", copy=True)
    row_hashes.sort()
    digest.update("\x1f".join(normalized.columns).encode("utf-8"))
    digest.update(row_hashes.tobytes())


def _stable_cache_value(value: object) -> str:
    if value is None or value is pd.NA or value is pd.NaT:
        return "<null>"
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    try:
        if pd.isna(value):
            return "<null>"
    except (TypeError, ValueError):
        pass
    return str(value)


def _historical_outcome_cache_get(
    key: str,
) -> tuple[pd.DataFrame, int, Mapping[str, int], tuple[Path, ...]] | None:
    with _HISTORICAL_OUTCOME_CACHE_LOCK:
        cached = _HISTORICAL_OUTCOME_CACHE.get(key)
        if cached is None:
            return None
        _HISTORICAL_OUTCOME_CACHE.move_to_end(key)
        frame, candidate_rows, failures, evidence_files = cached
        return (
            frame.copy(deep=True),
            candidate_rows,
            dict(failures),
            tuple(evidence_files),
        )


def _historical_outcome_cache_put(
    key: str,
    frame: pd.DataFrame,
    *,
    candidate_rows: int,
    failures: Mapping[str, int],
    evidence_files: Sequence[Path],
) -> None:
    with _HISTORICAL_OUTCOME_CACHE_LOCK:
        _HISTORICAL_OUTCOME_CACHE[key] = (
            frame.copy(deep=True),
            int(candidate_rows),
            dict(failures),
            tuple(Path(path) for path in evidence_files),
        )
        _HISTORICAL_OUTCOME_CACHE.move_to_end(key)
        while len(_HISTORICAL_OUTCOME_CACHE) > _HISTORICAL_OUTCOME_CACHE_LIMIT:
            _HISTORICAL_OUTCOME_CACHE.popitem(last=False)


def _pricing_model_eligible(frame: pd.DataFrame) -> pd.Series:
    gates = _pricing_model_gate_masks(frame)
    eligible = pd.Series(True, index=frame.index, dtype=bool)
    for gate in gates.values():
        eligible &= gate
    return eligible


def _pricing_model_gate_masks(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "PRICING_MODE_NOT_ACTIVE": frame["pricing_mode"]
        .astype("string")
        .str.upper()
        .eq("ACTIVE"),
        "PRICING_SOURCE_NOT_BASELINE": frame["pricing_source"]
        .astype("string")
        .str.upper()
        .isin(("BSGP", "BLACK_SCHOLES")),
        "INCOMPLETE_LEG_COVERAGE": pd.to_numeric(
            frame["pricing_leg_coverage"], errors="coerce"
        ).ge(1.0 - 1e-12),
        "SURFACE_QUALITY_FAILED": frame["surface_quality_pass"]
        .fillna(False)
        .astype(bool),
        "LIQUIDITY_POLICY_FAILED": frame["liquidity_policy_pass"]
        .fillna(False)
        .astype(bool),
        "OPTION_QUOTES_INVALID": frame["all_option_quotes_valid"]
        .fillna(False)
        .astype(bool),
    }


def _pricing_model_eligibility_report(
    frame: pd.DataFrame,
    *,
    eligible: pd.Series | None = None,
) -> dict[str, object]:
    """Publish auditable Pricing-gate evidence without double-counting exclusions."""

    gates = _pricing_model_gate_masks(frame)
    accepted = _pricing_model_eligible(frame) if eligible is None else eligible
    accepted = accepted.reindex(frame.index, fill_value=False).astype(bool)
    remaining = ~accepted
    primary_reasons: dict[str, int] = {}
    gate_failures: dict[str, int] = {}
    for reason, gate in gates.items():
        passed = gate.reindex(frame.index, fill_value=False).astype(bool)
        gate_failures[reason] = int((~passed).sum())
        assigned = remaining & ~passed
        count = int(assigned.sum())
        if count:
            primary_reasons[reason] = count
        remaining &= ~assigned
    if remaining.any():
        raise RuntimeError("Pricing eligibility left excluded rows unclassified")
    excluded = int((~accepted).sum())
    if sum(primary_reasons.values()) != excluded:
        raise RuntimeError("Pricing exclusion reasons do not reconcile")
    return {
        "pricing_eligible_outcome_rows": int(accepted.sum()),
        "pricing_excluded_outcome_rows": excluded,
        "pricing_exclusion_reason_counts": primary_reasons,
        "pricing_gate_failure_counts": gate_failures,
        "pricing_status_counts": _text_value_counts(frame, "pricing_status"),
        "pricing_source_counts": _text_value_counts(frame, "pricing_source"),
        "pricing_mode_counts": _text_value_counts(frame, "pricing_mode"),
        "pricing_exclusion_reasons_are_mutually_exclusive": True,
    }


def _empty_pricing_eligibility_report() -> dict[str, object]:
    reasons = tuple(_pricing_model_gate_masks(pd.DataFrame({
        "pricing_mode": pd.Series(dtype="string"),
        "pricing_source": pd.Series(dtype="string"),
        "pricing_leg_coverage": pd.Series(dtype=float),
        "surface_quality_pass": pd.Series(dtype=bool),
        "liquidity_policy_pass": pd.Series(dtype=bool),
        "all_option_quotes_valid": pd.Series(dtype=bool),
    })))
    return {
        "pricing_eligible_outcome_rows": 0,
        "pricing_excluded_outcome_rows": 0,
        "pricing_exclusion_reason_counts": {},
        "pricing_gate_failure_counts": {reason: 0 for reason in reasons},
        "pricing_status_counts": {},
        "pricing_source_counts": {},
        "pricing_mode_counts": {},
        "pricing_exclusion_reasons_are_mutually_exclusive": True,
    }


def _text_value_counts(frame: pd.DataFrame, column: str) -> dict[str, int]:
    values = frame.get(column, pd.Series(pd.NA, index=frame.index, dtype="string"))
    normalized = values.astype("string").fillna("<MISSING>")
    return {
        str(key): int(value)
        for key, value in normalized.value_counts(dropna=False).items()
    }


def _rerank_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    output = frame.copy()
    decision = pd.to_numeric(output["decision_score"], errors="coerce")
    scenario = pd.to_numeric(
        output["scenario_coverage_score"], errors="coerce"
    )
    output["__calibrated_rank"] = decision.notna().astype(int)
    output["__rank_value"] = decision.where(decision.notna(), scenario)
    output = output.sort_values(
        [
            "__calibrated_rank",
            "__rank_value",
            "expected_return_on_risk",
            "candidate_key",
        ],
        ascending=[False, False, False, True],
        kind="mergesort",
    ).drop(columns=["__calibrated_rank", "__rank_value"]).reset_index(drop=True)
    output["candidate_rank"] = range(1, len(output) + 1)
    return output


def _pricing_report(
    frame: pd.DataFrame,
    *,
    mode: str,
    catalog: StrategyPricingEvidenceCatalog,
) -> dict[str, object]:
    status = frame.get(
        "pricing_status", pd.Series("", index=frame.index, dtype="string")
    ).astype("string")
    fitted = frame.get(
        "calibrated_profit_probability",
        pd.Series(float("nan"), index=frame.index),
    ).notna()
    surface_failed = ~frame.get(
        "surface_quality_pass", pd.Series(False, index=frame.index)
    ).fillna(False).astype(bool)
    liquidity_failed = ~frame.get(
        "liquidity_policy_pass", pd.Series(False, index=frame.index)
    ).fillna(False).astype(bool)
    return {
        "mode": mode,
        "candidate_rows": len(frame),
        "status_counts": {
            str(key): int(value) for key, value in status.value_counts().items()
        },
        "prediction_rows_loaded": len(catalog.predictions),
        "load_errors": list(catalog.errors),
        "authority_states": dict(catalog.authority_states),
        "calibration_state": "AVAILABLE" if fitted.any() else "UNAVAILABLE",
        "calibrated_candidate_rows": int(fitted.sum()),
        "scenario_coverage_candidate_rows": int((~fitted).sum()),
        "surface_quality_failure_rows": int(surface_failed.sum()),
        "liquidity_policy_failure_rows": int(liquidity_failed.sum()),
        "attached_before_training_and_scoring": True,
    }


def _samples_with_possible_receipts(
    samples: pd.DataFrame,
    *,
    histories: Mapping[str, OptionChainHistory],
    strictly_before: pd.Timestamp | None,
) -> tuple[pd.DataFrame, Counter[str]]:
    """Remove only samples proven impossible by actual receipt coverage."""

    if samples.empty:
        return samples.copy(), Counter()
    retained: list[pd.DataFrame] = []
    failures: Counter[str] = Counter()
    symbols = samples["symbol"].astype("string").str.upper()
    for symbol, group in samples.groupby(symbols, sort=False):
        history = histories.get(str(symbol))
        if history is None or history.surfaces.empty:
            retained.append(group)
            continue

        surfaces = history.surfaces
        snapshot_min = pd.Timestamp(surfaces["snapshot_for"].min())
        snapshot_max = pd.Timestamp(surfaces["snapshot_for"].max())
        available_min = pd.Timestamp(surfaces["available_at"].min())
        available_max = pd.Timestamp(surfaces["available_at"].max())
        bar_end = pd.to_datetime(
            group["bar_end_timestamp"], utc=True, errors="coerce"
        )
        information = pd.to_datetime(
            group["information_available_at"], utc=True, errors="coerce"
        )
        target_start = pd.to_datetime(
            group["target_window_start"], utc=True, errors="coerce"
        )
        target_end = pd.to_datetime(
            group["target_window_end"], utc=True, errors="coerce"
        )
        entry_upper = target_start - pd.Timedelta(nanoseconds=1)
        entry_impossible = (
            snapshot_max < bar_end
        ) | (snapshot_min > entry_upper) | (available_max < information) | (
            available_min > entry_upper
        )

        delays = group["horizon"].astype(str).map(_EXIT_DELAYS)
        exit_upper = target_end + delays
        if strictly_before is not None:
            strict_upper = strictly_before - pd.Timedelta(nanoseconds=1)
            exit_upper = exit_upper.where(exit_upper.le(strict_upper), strict_upper)
        exit_impossible = (
            snapshot_max < target_end
        ) | (snapshot_min > exit_upper) | (available_max < target_end) | (
            available_min > exit_upper
        )
        entry_mask = entry_impossible.fillna(False)
        exit_mask = exit_impossible.fillna(False)
        failures["entry_receipt_unavailable"] += int(entry_mask.sum())
        failures["exit_receipt_unavailable"] += int(
            ((~entry_mask) & exit_mask).sum()
        )
        retained.append(group.loc[~entry_mask & ~exit_mask])
    return (
        pd.concat(retained, ignore_index=True, sort=False)
        if retained
        else samples.iloc[0:0].copy(),
        failures,
    )


def _canonical_live_predictions(predictions: pd.DataFrame) -> pd.DataFrame:
    live = predictions.loc[
        predictions["prediction_mode"].eq("LIVE")
        & predictions["prediction_status"].isin({"CREATED", "PREDICTED"})
    ].copy()
    if live.empty:
        return live
    live["prediction_created_at"] = pd.to_datetime(
        live["prediction_created_at"], utc=True, errors="coerce"
    )
    return (
        live.sort_values("prediction_created_at", kind="mergesort")
        .drop_duplicates(["symbol", "horizon", "decision_timestamp"], keep="first")
        .reset_index(drop=True)
    )


def _matching_sample(
    samples: pd.DataFrame,
    prediction: Mapping[str, object],
) -> dict[str, object]:
    mask = (
        samples["symbol"].eq(prediction["symbol"])
        & samples["horizon"].eq(prediction["horizon"])
        & pd.to_datetime(samples["decision_timestamp"], utc=True).eq(
            _utc(prediction["decision_timestamp"])
        )
        & pd.to_datetime(samples["target_window_start"], utc=True).eq(
            _utc(prediction["target_window_start"])
        )
        & pd.to_datetime(samples["target_window_end"], utc=True).eq(
            _utc(prediction["target_window_end"])
        )
    )
    matched = samples.loc[mask]
    if len(matched) != 1:
        raise ValueError("Live strategy prediction did not match exactly one redacted sample")
    return matched.iloc[0].to_dict()


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
    context_frame = pd.DataFrame(
        {column: [value] * len(candidates) for column, value in context.items()},
        index=candidates.index,
    )
    return pd.concat((candidates.copy(), context_frame), axis=1)


def _prediction_probabilities(
    predictions: pd.DataFrame,
) -> dict[tuple[str, str, pd.Timestamp, pd.Timestamp, pd.Timestamp], float]:
    eligible = predictions.loc[
        predictions["prediction_status"].isin({"CREATED", "PREDICTED"})
    ]
    output: dict[
        tuple[str, str, pd.Timestamp, pd.Timestamp, pd.Timestamp], float
    ] = {}
    for row in eligible.to_dict("records"):
        key = _prediction_key(row)
        if key in output:
            raise ValueError("Strategy market state received duplicate predictions")
        probability = pd.to_numeric(row.get("calibrated_probability"), errors="coerce")
        if pd.notna(probability):
            output[key] = float(probability)
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


def _failed_route_audit(
    sample: Mapping[str, object],
    *,
    reason: str,
    construction_status: str,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": sample["symbol"],
                "horizon": sample["horizon"],
                "decision_timestamp": sample["decision_timestamp"],
                "strategy_name": definition.name,
                "strategy_display_name": definition.display_name,
                "strategy_family": definition.family,
                "account_approval": "SPREADS",
                "authorization_status": "AUTHORIZED_SPREADS",
                "construction_status": construction_status,
                "candidate_count": 0,
                "reason": reason,
                "registry_version": STRATEGY_REGISTRY_VERSION,
                "candidate_policy_version": STRATEGY_CANDIDATE_POLICY_VERSION,
            }
            for definition in STRATEGY_REGISTRY.values()
        ]
    )


def _validate_inputs(samples: pd.DataFrame, predictions: pd.DataFrame) -> None:
    sample_required = {
        "symbol",
        "horizon",
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "label_status",
    }
    prediction_required = {
        "symbol",
        "horizon",
        "decision_timestamp",
        "target_window_start",
        "target_window_end",
        "prediction_created_at",
        "prediction_mode",
        "prediction_status",
        "calibrated_probability",
    }
    for frame, required, label in (
        (samples, sample_required, "redacted Loop B samples"),
        (predictions, prediction_required, "Loop B predictions"),
    ):
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError(f"{label} are missing columns: " + ", ".join(missing))


def _assert_lockbox_excluded(
    samples: pd.DataFrame,
    *,
    forbidden_target_starts: Mapping[str, Sequence[object]],
) -> None:
    for horizon, values in forbidden_target_starts.items():
        forbidden = pd.to_datetime(pd.Index(tuple(values)), utc=True, errors="coerce")
        if forbidden.isna().any():
            raise ValueError("Real lockbox target starts contain invalid timestamps")
        observed = pd.to_datetime(
            samples.loc[samples["horizon"].eq(horizon), "target_window_start"],
            utc=True,
            errors="coerce",
        )
        if observed.isin(forbidden).any():
            raise RuntimeError(
                f"Real {horizon} lockbox rows reached strategy selection"
            )


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Strategy-selection runtime timestamp is invalid")
    return pd.Timestamp(timestamp)


__all__ = ["run_strategy_selection"]
