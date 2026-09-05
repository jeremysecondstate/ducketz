from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.databento_opra_history import (
    OPRA_STRATEGY_HISTORY_SCHEMAS,
    record_consumer_usage,
)
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, utc_timestamp
from ml.current_publication import read_current_publication
from ml.strategy_profit_training import (
    MODELED_EXECUTION_SOURCE,
    MODELED_HORIZONS,
    build_modeled_strategy_outcomes,
    calibrate_execution_haircuts,
)
from ml.strategy_selection.contracts import StrategySelectionPolicy
from ml.strategy_selection.model import (
    fit_or_reuse_strategy_model,
    partition_strategy_outcomes,
)
from ml.strategy_selection.slow_model import (
    publish_slow_strategy_authority,
    strategy_model_promotion_gate,
)


TRAINING_RUNTIME_VERSION = "multi-horizon-strategy-profit-training-runtime-v2"

# The intraday OPRA archive has many observations per session but fewer
# independent calendar sessions than the daily archive.  Keep all three
# chronological cohorts substantial while matching the already-established
# Loop B intraday training minimums.  Daily and weekly retain the conservative
# 252/63/63 contract.
_MINIMUM_TRAIN_DECISIONS = {
    "1h": 60,
    "4h": 30,
    "1d": 252,
    "1w": 252,
}
_CALIBRATION_DECISIONS = {
    "1h": 30,
    "4h": 15,
    "1d": 63,
    "1w": 63,
}
_ASSESSMENT_DECISIONS = {
    "1h": 30,
    "4h": 15,
    "1d": 63,
    "1w": 63,
}


@dataclass(frozen=True)
class StrategyProfitTrainingResult:
    run_directory: Path
    reports: Mapping[str, Mapping[str, object]]
    candidate_outcome_rows: int
    published_at: pd.Timestamp


def run_strategy_profit_training_once(
    datastore_root: Path,
    *,
    run_timestamp: object | None = None,
    reporter: Callable[[str], None] | None = print,
    resume_run_directory: Path | None = None,
) -> StrategyProfitTrainingResult:
    root = Path(datastore_root)
    source = read_current_publication(root)
    samples_path = source.run_directory / "samples.parquet"
    predictions_path = source.run_directory / "predictions.parquet"
    samples = pd.read_parquet(samples_path)
    predictions = pd.read_parquet(predictions_path)
    configuration = source.manifest.get("configuration")
    configured = (
        configuration.get("symbols")
        if isinstance(configuration, Mapping)
        else None
    )
    symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in (
                configured
                if isinstance(configured, (list, tuple))
                else samples["symbol"].astype("string").unique()
            )
            if str(value).strip()
        )
    )
    created = utc_timestamp(run_timestamp)
    if resume_run_directory is None:
        run = create_timestamp_directory(
            root / "ml" / "strategy-profit-training-runs",
            timestamp=created,
        )
    else:
        run = Path(resume_run_directory).resolve()
        run_root = (root / "ml" / "strategy-profit-training-runs").resolve()
        if run_root not in run.parents or not run.is_dir():
            raise ValueError("Resume run must be an existing Strategy training run")
    if reporter is not None:
        reporter(
            "Strategy profit training: calibrating conservative OPRA execution "
            f"haircuts for {len(symbols)} symbols"
        )
    haircuts = calibrate_execution_haircuts(root, symbols=symbols)
    history_freshness = _validate_opra_history_freshness(
        samples,
        execution_report=haircuts.report,
        datastore_root=root,
        symbols=symbols,
    )
    execution_report = {
        **dict(haircuts.report),
        "history_freshness": history_freshness,
    }
    execution_report_path = run / "execution-haircut-report.json"
    _write_json(execution_report_path, execution_report)
    base_policy = StrategySelectionPolicy()
    models = {}
    reports: dict[str, Mapping[str, object]] = {}
    outputs: list[Path] = [execution_report_path]
    total_rows = 0
    for horizon in MODELED_HORIZONS:
        policy = replace(
            base_policy,
            minimum_train_decisions=_MINIMUM_TRAIN_DECISIONS[horizon],
            calibration_decisions=_CALIBRATION_DECISIONS[horizon],
            assessment_decisions=_ASSESSMENT_DECISIONS[horizon],
        )
        resumed = _resumable_horizon(
            run,
            horizon=horizon,
            execution_model_fingerprint=haircuts.fingerprint,
        )
        if resumed is not None:
            built_frame, report = resumed
            outcomes_path = run / f"{horizon}-modeled-outcomes.parquet"
            report_path = run / f"{horizon}-model-report.json"
            reports[horizon] = report
            total_rows += len(built_frame)
            outputs.extend((outcomes_path, report_path))
            recorded_gate = report.get("promotion_gate", {})
            if recorded_gate.get("status") != "PROMOTED":
                if reporter is not None:
                    reporter(
                        "Strategy profit training: resumed rejected "
                        f"{horizon} assessment; outcomes={len(built_frame)}"
                    )
                continue
            partitions = partition_strategy_outcomes(built_frame, policy=policy)
            model = fit_or_reuse_strategy_model(
                root,
                horizon=horizon,
                partitions=partitions,
                policy=policy,
                input_files=(
                    samples_path,
                    predictions_path,
                    outcomes_path,
                    execution_report_path,
                ),
                trained_at=created,
                publish_latest=False,
            )
            gate = strategy_model_promotion_gate(
                model.offline_evaluation,
                minimum_assessment_decisions=policy.assessment_decisions,
            )
            if gate.get("status") != "PROMOTED":
                raise RuntimeError(
                    f"Resumed {horizon} Strategy model no longer passes promotion"
            )
            models[horizon] = model
            if reporter is not None:
                reporter(
                    "Strategy profit training: resumed promoted "
                    f"{horizon} artifacts; outcomes={len(built_frame)}"
                )
            continue
        if reporter is not None:
            reporter(
                f"Strategy profit training: materializing {horizon} modeled outcomes"
            )
        built = build_modeled_strategy_outcomes(
            root,
            samples=samples,
            predictions=predictions,
            horizon=horizon,
            haircuts=haircuts,
            policy=policy,
            reporter=reporter,
        )
        if built.frame.empty:
            raise RuntimeError(f"No {horizon} modeled Strategy outcomes were built")
        outcomes_path = run / f"{horizon}-modeled-outcomes.parquet"
        built.frame.to_parquet(outcomes_path, index=False)
        total_rows += len(built.frame)
        partitions = partition_strategy_outcomes(built.frame, policy=policy)
        model = fit_or_reuse_strategy_model(
            root,
            horizon=horizon,
            partitions=partitions,
            policy=policy,
            input_files=(
                samples_path,
                predictions_path,
                outcomes_path,
                execution_report_path,
            ),
            trained_at=created,
            publish_latest=False,
        )
        gate = strategy_model_promotion_gate(
            model.offline_evaluation,
            minimum_assessment_decisions=policy.assessment_decisions,
        )
        report = {
            "schema_version": TRAINING_RUNTIME_VERSION,
            "status": (
                "MODEL_FIT"
                if gate.get("status") == "PROMOTED"
                else "MODEL_REJECTED"
            ),
            "calibration_status": "AVAILABLE",
            "horizon": horizon,
            "model_source": MODELED_EXECUTION_SOURCE,
            "execution_evidence": dict(built.report),
            "execution_haircut_validation": dict(execution_report),
            "complete_outcome_rows": len(built.frame),
            "pricing_eligible_outcome_rows": len(built.frame),
            "pricing_excluded_outcome_rows": 0,
            "pricing_exclusion_reason_counts": {},
            "usable_decision_clusters": int(
                built.frame["target_window_start"].nunique()
            ),
            "required_decision_clusters": (
                policy.minimum_train_decisions
                + policy.calibration_decisions
                + policy.assessment_decisions
            ),
            "training_decisions": partitions.train_decisions,
            "calibration_decisions": partitions.calibration_decisions,
            "assessment_decisions": partitions.assessment_decisions,
            "training_date_range": _date_range(partitions.train),
            "calibration_date_range": _date_range(partitions.calibration),
            "assessment_date_range": _date_range(partitions.assessment),
            "evidence_quality_counts": {
                str(key): int(value)
                for key, value in built.frame["execution_evidence_type"]
                .value_counts()
                .items()
            },
            "offline_evaluation": dict(model.offline_evaluation),
            "promotion_gate": gate,
            "artifact_directory": str(model.artifact_directory),
            "real_lockbox_used": False,
            "orders_placed": 0,
            "research_only": True,
        }
        report_path = run / f"{horizon}-model-report.json"
        _write_json(report_path, report)
        reports[horizon] = report
        outputs.extend((outcomes_path, report_path))
        if gate.get("status") == "PROMOTED":
            models[horizon] = model
        elif reporter is not None:
            reporter(
                f"Strategy profit training: {horizon} model retained as "
                "research-only after its assessment gate rejected promotion"
            )
    summary_path = run / "training-report.json"
    _write_json(
        summary_path,
        {
            "schema_version": TRAINING_RUNTIME_VERSION,
            "source_loop_b_run": source.run_directory.relative_to(root).as_posix(),
            "source_loop_b_run_timestamp": source.manifest.get("run_timestamp"),
            "symbols": list(symbols),
            "horizons": list(MODELED_HORIZONS),
            "promoted_horizons": [
                horizon for horizon in MODELED_HORIZONS if horizon in models
            ],
            "rejected_horizons": [
                horizon for horizon in MODELED_HORIZONS if horizon not in models
            ],
            "execution_model_fingerprint_sha256": haircuts.fingerprint,
            "model_reports": reports,
            "candidate_outcome_rows": total_rows,
            "orders_placed": 0,
            "research_only": True,
        },
    )
    outputs.append(summary_path)
    if not models:
        raise RuntimeError("No Strategy profit model passed its promotion gate")
    record_consumer_usage(
        root,
        consumer="strategy-profit-training-hgb-mlp",
        schemas=("ohlcv-1h", "cbbo-1m"),
        rows=total_rows,
        source_files=haircuts.source_files,
        refresh_health=True,
    )
    publish_slow_strategy_authority(
        root,
        run_directory=run,
        models=models,
        reports=reports,
        published_at=created,
        output_files=outputs,
    )
    if reporter is not None:
        reporter(
            "Strategy profit training published: "
            f"outcomes={total_rows}; run={run}"
        )
    return StrategyProfitTrainingResult(
        run_directory=run,
        reports=reports,
        candidate_outcome_rows=total_rows,
        published_at=created,
    )


def _resumable_horizon(
    run: Path,
    *,
    horizon: str,
    execution_model_fingerprint: str,
) -> tuple[pd.DataFrame, Mapping[str, object]] | None:
    outcomes_path = run / f"{horizon}-modeled-outcomes.parquet"
    report_path = run / f"{horizon}-model-report.json"
    if not outcomes_path.is_file() or not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        evidence = report["execution_evidence"]
        gate_status = report.get("promotion_gate", {}).get("status")
        if (
            report.get("status") not in {"MODEL_FIT", "MODEL_REJECTED"}
            or report.get("horizon") != horizon
            or gate_status not in {"PROMOTED", "REJECTED"}
            or evidence.get("execution_model_fingerprint_sha256")
            != execution_model_fingerprint
        ):
            return None
        frame = pd.read_parquet(outcomes_path)
        if frame.empty or len(frame) != int(report["complete_outcome_rows"]):
            return None
        return frame, report
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
        return None


def _validate_opra_history_freshness(
    samples: pd.DataFrame,
    *,
    execution_report: Mapping[str, object],
    datastore_root: Path | None = None,
    symbols: Sequence[str] = (),
) -> Mapping[str, object]:
    daily = samples.loc[
        samples["horizon"].astype("string").eq("1d")
        & samples["label_status"].astype("string").eq("COMPLETE")
    ]
    if daily.empty:
        raise RuntimeError("No complete 1d samples exist for OPRA freshness validation")
    required = pd.Timestamp(daily["target_window_start"].max()).date()
    sessions = tuple(
        str(value)
        for key in ("fit_sessions", "assessment_sessions")
        for value in execution_report.get(key, ())
    )
    if not sessions:
        raise RuntimeError("OPRA execution calibration published no source sessions")
    latest = max(pd.Timestamp(value).date() for value in sessions)
    if latest < required:
        raise RuntimeError(
            "OPRA strategy history is stale: "
            f"latest_common_session={latest.isoformat()} "
            f"required_complete_1d_session={required.isoformat()}"
        )
    required_completion = required + timedelta(days=1)
    cursor_completion: dict[str, str] = {}
    if datastore_root is not None:
        cursor_root = (
            Path(datastore_root)
            / "market-data"
            / "databento"
            / "opra"
            / "OPRA.PILLAR"
            / "state"
            / "symbol-history"
        )
        for symbol in symbols:
            clean_symbol = str(symbol).strip().upper()
            for schema in OPRA_STRATEGY_HISTORY_SCHEMAS:
                identity = f"{clean_symbol}/{schema}"
                cursor_path = cursor_root / clean_symbol / f"{schema}.json"
                if not cursor_path.is_file():
                    raise RuntimeError(
                        f"OPRA strategy history cursor is missing: {identity}"
                    )
                cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
                if (
                    cursor.get("dataset") != "OPRA.PILLAR"
                    or str(cursor.get("symbol", "")).strip().upper()
                    != clean_symbol
                    or cursor.get("schema") != schema
                ):
                    raise RuntimeError(
                        f"OPRA strategy history cursor identity is invalid: {identity}"
                    )
                completed = pd.Timestamp(
                    str(cursor.get("completed_through"))
                ).date()
                if completed < required_completion:
                    raise RuntimeError(
                        "OPRA strategy history cursor is stale: "
                        f"scope={identity} completed_through={completed.isoformat()} "
                        f"required_completed_through={required_completion.isoformat()}"
                    )
                cursor_completion[identity] = completed.isoformat()
    return {
        "status": "CURRENT_FOR_LATEST_COMPLETE_1D_SAMPLE",
        "latest_common_ohlcv_1h_cbbo_1m_session": latest.isoformat(),
        "required_complete_1d_session": required.isoformat(),
        "required_cursor_completed_through": required_completion.isoformat(),
        "verified_strategy_history_cursors": cursor_completion,
    }


def next_daily_boundary(now: datetime, *, utc_hour: int) -> datetime:
    current = now.astimezone(timezone.utc)
    boundary = current.replace(
        hour=utc_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if boundary <= current:
        boundary += timedelta(days=1)
    return boundary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train receipt-verified 1h/4h/1d/1w Strategy profit models from "
            "exact-first OPRA execution evidence."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--utc-hour", type=int, default=22)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--resume-run",
        type=Path,
        default=None,
        help="Resume already-promoted horizons from an incomplete run directory",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.utc_hour <= 23:
        parser.error("--utc-hour must be in [0, 23]")
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    print("DUCKETS MULTI-HORIZON STRATEGY PROFIT TRAINING")
    print("=================================================")
    print(f"DATASTORE: {root}")
    print("Horizons: 1h, 4h, 1d, and 1w (weekly day components reuse 1d)")
    print("Orders: disabled; research artifacts only")
    lock = root / ".ducketz-strategy-profit-training-runtime.lock"
    with exclusive_runtime_lock(
        lock,
        process_name="Duckets Strategy profit training runtime",
    ):
        while True:
            if not args.once:
                boundary = next_daily_boundary(
                    datetime.now(timezone.utc), utc_hour=args.utc_hour
                )
                print(f"Next Strategy profit training: {boundary.isoformat()}")
                time.sleep(
                    max(
                        0.0,
                        (boundary - datetime.now(timezone.utc)).total_seconds(),
                    )
                )
            try:
                run_strategy_profit_training_once(
                    root,
                    resume_run_directory=args.resume_run,
                )
                code = 0
            except Exception as exc:
                print(
                    "Strategy profit training failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                code = 1
            if args.once:
                return code


def _date_range(frame: pd.DataFrame) -> dict[str, str | None]:
    if frame.empty:
        return {"start": None, "end": None}
    values = pd.to_datetime(
        frame["target_window_start"], utc=True, errors="coerce"
    ).dropna()
    return {
        "start": pd.Timestamp(values.min()).isoformat() if len(values) else None,
        "end": pd.Timestamp(values.max()).isoformat() if len(values) else None,
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "StrategyProfitTrainingResult",
    "next_daily_boundary",
    "run_strategy_profit_training_once",
]
