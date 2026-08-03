from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.strategy_selection.candidates import (
    construct_strategy_candidates,
    evaluate_candidate_outcome,
)
from ml.strategy_selection.chain import (
    SchwabChainHistory,
    entry_chain_receipt,
    entry_stock_quote,
    exit_chain_receipt,
    exit_stock_quote,
    load_schwab_chain_history,
)
from ml.strategy_selection.contracts import (
    STRATEGY_CANDIDATE_POLICY_VERSION,
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
) -> StrategySelectionRun:
    effective_policy = policy or StrategySelectionPolicy()
    _validate_inputs(samples, predictions)
    _assert_lockbox_excluded(
        samples,
        forbidden_target_starts=forbidden_target_starts,
    )
    created = _utc(run_timestamp)
    input_cutoff = _utc(input_available_at)
    prediction_probabilities = _prediction_probabilities(predictions)
    histories: dict[str, SchwabChainHistory] = {}
    source_files: list[Path] = []
    history_errors: dict[str, str] = {}
    for symbol in sorted(set(samples["symbol"].astype("string").str.upper())):
        try:
            history = load_schwab_chain_history(
                datastore_root,
                symbol=str(symbol),
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
        outcomes, outcome_report = _historical_outcomes(
            horizon_samples,
            histories=histories,
            prediction_probabilities=prediction_probabilities,
            policy=effective_policy,
            strictly_before=lockbox_boundaries.get(horizon),
        )
        if outcomes.empty:
            model_reports[horizon] = {
                "status": "MODEL_NOT_FIT",
                "reason": "No complete observed-BBO candidate outcomes were materialized.",
                **outcome_report,
                "required_decision_clusters": required_decisions,
                "usable_decision_clusters": 0,
                "real_lockbox_used": False,
            }
            continue
        try:
            partitions = partition_strategy_outcomes(
                outcomes,
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
            model_reports[horizon] = {
                "status": "MODEL_NOT_FIT",
                "reason": f"{type(exc).__name__}: {exc}",
                **outcome_report,
                "complete_outcome_rows": len(outcomes),
                "usable_decision_clusters": int(
                    outcomes["target_window_start"].nunique()
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
            **outcome_report,
            "complete_outcome_rows": len(outcomes),
            "usable_decision_clusters": int(
                outcomes["target_window_start"].nunique()
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
            reason = history_errors.get(symbol, "Schwab chain history unavailable")
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
                "No causally eligible exact Schwab chain receipt was available "
                "by the completed Loop A cycle cutoff and before the target window."
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
        try:
            candidates, audit = construct_strategy_candidates(
                sample,
                entry.contracts,
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
        scored = score_strategy_candidates(
            model,  # type: ignore[arg-type]
            candidates,
        )
        scored["model_status"] = "MODEL_FIT"
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
    )


def _historical_outcomes(
    samples: pd.DataFrame,
    *,
    histories: Mapping[str, SchwabChainHistory],
    prediction_probabilities: Mapping[
        tuple[str, str, pd.Timestamp, pd.Timestamp, pd.Timestamp], float
    ],
    policy: StrategySelectionPolicy,
    strictly_before: pd.Timestamp | None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    outcome_frames: list[pd.DataFrame] = []
    failures: Counter[str] = Counter()
    candidate_rows = 0
    for sample in samples.sort_values(
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
        state = infer_market_state(
            sample,
            surface=entry.surface,
            probability_up=prediction_probabilities.get(_prediction_key(sample)),
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
                failures[str(result["outcome_status"]).lower()] += 1
        outcome_frames.append(pd.DataFrame(evaluated))
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
    return outcomes.loc[
        outcomes["outcome_status"].eq("COMPLETE")
    ].reset_index(drop=True) if not outcomes.empty else outcomes, {
        "sample_rows_considered": len(samples),
        "candidate_rows_constructed": candidate_rows,
        "complete_outcome_rows": complete_rows,
        "failures": dict(sorted(failures.items())),
    }


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
