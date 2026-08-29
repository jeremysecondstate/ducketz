from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

import exchange_calendars as xcals
import pandas as pd

from datafetching.ids import add_readable_id
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, utc_timestamp, write_manifest
from ml.loop_c.engine import evaluate_loop_c
from ml.loop_c.policy import LoopCMode, LoopCRiskLimits
from ml.loop_c.publication import publish_loop_c_observe_run
from ml.parquet_contracts import LOOP_C_DECISION_SCHEMA, write_parquet_with_schema
from ml.sequence_encoder.consumer import load_sequence_distributions
from ml.sequence_encoder.publication import read_current_sequence_publication
from ml.strategy_publication import read_current_strategy_publication


def run_loop_c_observe_once(
    datastore_root: Path,
    *,
    decision_timestamp: object,
    risk_limits: LoopCRiskLimits,
    portfolio: Mapping[str, object] | None = None,
    broker: Mapping[str, object] | None = None,
    halt_requested: bool = False,
    source_files: Sequence[Path] = (),
    publish: bool = False,
) -> dict[str, object]:
    root = Path(datastore_root).resolve()
    now = utc_timestamp(decision_timestamp)
    strategy = read_current_strategy_publication(root)
    candidates_path = strategy.run_directory / "strategy-candidates.parquet"
    candidates = pd.read_parquet(candidates_path)
    routes = candidates.loc[
        :, ["symbol", "horizon", "decision_timestamp"]
    ].drop_duplicates()
    sequence = load_sequence_distributions(
        root,
        routes=routes,
        consumer="LOOP_C_OBSERVE",
        as_of=now,
    )
    merged = _merge_candidates(candidates, sequence.distributions)
    try:
        sequence_publication = read_current_sequence_publication(root)
        model_authority = str(sequence_publication.receipt.get("authority", "NONE"))
        model_published_at = sequence_publication.receipt.get("published_at")
        sequence_path = sequence_publication.run_directory / "distributions.parquet"
        sequence_inputs = (
            sequence_publication.run_directory / "manifest.json",
            sequence_publication.run_directory / "publication.json",
            sequence_path,
        )
    except Exception:
        model_authority = "NONE"
        model_published_at = None
        sequence_path = None
        sequence_inputs = ()
    decision = evaluate_loop_c(
        merged,
        decision_timestamp=now,
        mode=LoopCMode.OBSERVE,
        market_session_open=_market_open(now),
        portfolio=portfolio,
        broker=broker,
        risk_limits=risk_limits,
        model_authority=model_authority,
        model_published_at=model_published_at,
        halt_requested=halt_requested,
    )
    run_directory = create_timestamp_directory(root / "ml" / "loop-c-runs", timestamp=now)
    record = decision.as_record()
    record["reason_codes_json"] = json.dumps(record.pop("reason_codes"))
    record["candidate_key"] = record.pop("candidate_id")
    frame = add_readable_id(
        pd.DataFrame([record]),
        key_columns=("decision_timestamp", "mode", "action"),
    )
    write_parquet_with_schema(frame, run_directory / "decisions.parquet", LOOP_C_DECISION_SCHEMA)
    report = {
        "schema_version": "loop-c-observe-report-v1",
        "status": decision.status,
        "decision": decision.as_record(),
        "sequence_consumer": {
            "status": sequence.status,
            "matched_routes": sequence.matched_routes,
            "requested_routes": sequence.requested_routes,
        },
        "safety": {
            "authority": "OBSERVE_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
            "broker_submission_path_present": False,
            "halt_requested": bool(halt_requested),
        },
    }
    (run_directory / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    inputs = tuple(
        dict.fromkeys(
            (
                candidates_path,
                strategy.run_directory / "manifest.json",
                strategy.run_directory / "publication.json",
                *sequence_inputs,
                *(Path(path) for path in source_files),
            )
        )
    )
    write_manifest(
        run_directory,
        run_timestamp=now,
        input_files=inputs,
        output_files=("decisions.parquet", "report.json"),
        configuration={
            "authority": "OBSERVE_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
            "risk_limits": asdict(risk_limits),
            "strategy_source": dict(strategy.pointer.get("current", {})),
            "sequence_status": sequence.status,
            "halt_requested": bool(halt_requested),
        },
        datastore_root=root,
    )
    if publish:
        publish_loop_c_observe_run(root, run_directory=run_directory, published_at=now)
    return {
        "status": decision.status,
        "action": decision.action,
        "run_directory": str(run_directory),
        "published": publish,
        "orders_enabled": False,
        "orders_placed": 0,
    }


def _merge_candidates(candidates: pd.DataFrame, distributions: pd.DataFrame) -> pd.DataFrame:
    if distributions.empty:
        return pd.DataFrame(
            columns=(
                "id",
                "symbol",
                "horizon",
                "calibrated_probability",
                "sequence_directional_probability",
                "sequence_expected_return",
                "sequence_adverse_return",
                "expected_return_on_risk",
                "total_uncertainty",
                "max_loss",
                "capital_required",
            )
        )
    keys = ["symbol", "horizon", "decision_timestamp"]
    sequence = distributions.loc[
        :,
        [
            *keys,
            "calibrated_probability_up",
            "expected_return",
            "return_quantile_10",
            "return_quantile_90",
            "total_uncertainty",
        ],
    ].rename(
        columns={
            "calibrated_probability_up": "sequence_probability_up",
            "expected_return": "sequence_expected_return",
            "return_quantile_10": "sequence_quantile_10",
            "return_quantile_90": "sequence_quantile_90",
        }
    )
    output = candidates.merge(sequence, on=keys, how="inner", validate="many_to_one")
    output["calibrated_probability"] = pd.to_numeric(
        output["calibrated_profit_probability"], errors="coerce"
    )
    delta = pd.to_numeric(output["net_delta"], errors="coerce")
    probability_up = pd.to_numeric(
        output["sequence_probability_up"], errors="coerce"
    )
    # Direction-agnostic structures retain their Strategy profit probability;
    # directional structures must also agree with the sequence distribution.
    output["sequence_directional_probability"] = 1.0
    output.loc[
        delta.gt(0.05), "sequence_directional_probability"
    ] = probability_up
    output.loc[
        delta.lt(-0.05), "sequence_directional_probability"
    ] = 1.0 - probability_up
    output.loc[delta.isna(), "sequence_directional_probability"] = float("nan")
    q10 = pd.to_numeric(output["sequence_quantile_10"], errors="coerce")
    q90 = pd.to_numeric(output["sequence_quantile_90"], errors="coerce")
    neutral_adverse = -pd.concat([q10.abs(), q90.abs()], axis=1).max(axis=1)
    output["sequence_adverse_return"] = q10.where(
        delta.gt(0.05),
        (-q90).where(delta.lt(-0.05), neutral_adverse),
    )
    return output


def _market_open(now: pd.Timestamp) -> bool:
    calendar = xcals.get_calendar(
        "XNYS",
        start=now.tz_convert("America/New_York").date() - pd.Timedelta(days=7),
        end=now.tz_convert("America/New_York").date() + pd.Timedelta(days=7),
    )
    try:
        return bool(calendar.is_open_on_minute(now.floor("min"), ignore_breaks=False))
    except Exception:
        return False


def _load_mapping(path: Path | None) -> Mapping[str, object] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"Snapshot must be an object: {path}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Loop C in observe-only mode.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--decision-timestamp", required=True)
    parser.add_argument("--portfolio-snapshot", type=Path)
    parser.add_argument("--broker-snapshot", type=Path)
    parser.add_argument("--halt-control", type=Path)
    parser.add_argument("--risk-limits", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(root_dir=args.root_dir, target=args.datastore_target)
        limits_value = _load_mapping(args.risk_limits)
        if limits_value is None:
            raise ValueError("Risk limits are required")
        halt_control = _load_mapping(args.halt_control)
        with exclusive_runtime_lock(
            root / ".ducketz-loop-c-observe.lock",
            process_name="Duckets Loop C observe runtime",
        ):
            result = run_loop_c_observe_once(
                root,
                decision_timestamp=args.decision_timestamp,
                risk_limits=LoopCRiskLimits(**dict(limits_value)),
                portfolio=_load_mapping(args.portfolio_snapshot),
                broker=_load_mapping(args.broker_snapshot),
                halt_requested=(
                    bool(halt_control.get("halt_requested"))
                    if isinstance(halt_control, Mapping)
                    else False
                ),
                source_files=tuple(
                    path
                    for path in (
                        args.risk_limits,
                        args.portfolio_snapshot,
                        args.broker_snapshot,
                        args.halt_control,
                    )
                    if path is not None
                ),
                publish=args.publish,
            )
        exit_code = 0
    except Exception as exc:
        result = {
            "status": "ERROR",
            "error": str(exc),
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(json.dumps(result, separators=(",", ":") if args.compact else None))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["LOOP_C_DECISION_SCHEMA", "main", "run_loop_c_observe_once"]
