from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, utc_timestamp
from ml.current_publication import read_current_publication
from ml.strategy_profit_training import (
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


TRAINING_RUNTIME_VERSION = "daily-weekly-strategy-profit-training-runtime-v1"


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
    run = create_timestamp_directory(
        root / "ml" / "strategy-profit-training-runs",
        timestamp=created,
    )
    if reporter is not None:
        reporter(
            "Strategy profit training: calibrating conservative OPRA execution "
            f"haircuts for {len(symbols)} symbols"
        )
    haircuts = calibrate_execution_haircuts(root, symbols=symbols)
    execution_report_path = run / "execution-haircut-report.json"
    _write_json(execution_report_path, haircuts.report)
    policy = StrategySelectionPolicy()
    models = {}
    reports: dict[str, Mapping[str, object]] = {}
    outputs: list[Path] = [execution_report_path]
    total_rows = 0
    for horizon in MODELED_HORIZONS:
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
        gate = strategy_model_promotion_gate(model.offline_evaluation)
        report = {
            "schema_version": TRAINING_RUNTIME_VERSION,
            "status": (
                "MODEL_FIT"
                if gate.get("status") == "PROMOTED"
                else "MODEL_REJECTED"
            ),
            "calibration_status": "AVAILABLE",
            "horizon": horizon,
            "model_source": "OPRA_OHLCV_MODELED_EXECUTION",
            "execution_evidence": dict(built.report),
            "execution_haircut_validation": dict(haircuts.report),
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
        models[horizon] = model
        reports[horizon] = report
        outputs.extend((outcomes_path, report_path))
        if gate.get("status") != "PROMOTED":
            raise RuntimeError(
                f"{horizon} Strategy model failed promotion: "
                + json.dumps(gate, sort_keys=True)
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
            "execution_model_fingerprint_sha256": haircuts.fingerprint,
            "model_reports": reports,
            "candidate_outcome_rows": total_rows,
            "orders_placed": 0,
            "research_only": True,
        },
    )
    outputs.append(summary_path)
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
            "Train receipt-verified daily/weekly Strategy profit models from "
            "conservatively modeled OPRA hourly execution evidence."
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
    args = parser.parse_args(argv)
    if not 0 <= args.utc_hour <= 23:
        parser.error("--utc-hour must be in [0, 23]")
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    print("DUCKETS DAILY/WEEKLY STRATEGY PROFIT TRAINING")
    print("================================================")
    print(f"DATASTORE: {root}")
    print("Horizons: 1d and 1w (weekly day components reuse 1d)")
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
                run_strategy_profit_training_once(root)
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
