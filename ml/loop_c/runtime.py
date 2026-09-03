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
from ml.artifacts import (
    create_timestamp_directory,
    semantic_metadata_fingerprint,
    utc_timestamp,
    write_manifest,
)
from ml.loop_c.engine import evaluate_loop_c
from ml.loop_c.inputs import load_loop_c_inputs
from ml.loop_c.paper_ledger import (
    build_paper_trade_snapshot,
    paper_candidate_has_bounded_exit,
)
from ml.loop_c.policy import (
    LoopCMode,
    LoopCRiskLimits,
    LoopCSequenceModelBinding,
)
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
    model_binding: LoopCSequenceModelBinding,
    portfolio: Mapping[str, object] | None = None,
    broker: Mapping[str, object] | None = None,
    halt_requested: bool = False,
    input_contracts: Mapping[str, object] | None = None,
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
    sequence_publication = read_current_sequence_publication(root)
    binding_summary = _validate_sequence_model_binding(
        sequence_publication,
        model_binding,
    )
    sequence = load_sequence_distributions(
        root,
        routes=routes,
        consumer="LOOP_C_OBSERVE",
        as_of=now,
    )
    if sequence.status not in {"READY_SHADOW", "PARTIAL_SHADOW"}:
        raise ValueError(f"Loop C sequence distribution is unavailable: {sequence.status}")
    if not sequence.distributions.empty:
        schema_values = set(
            sequence.distributions["schema_version"].astype("string").dropna()
        )
        if schema_values != {model_binding.distribution_schema_version}:
            raise ValueError("Loop C sequence distribution schema differs from its binding")
    merged = _merge_candidates(candidates, sequence.distributions)
    merged["paper_outcome_eligible"] = [
        paper_candidate_has_bounded_exit(row)
        for row in merged.to_dict(orient="records")
    ]
    model_authority = str(sequence_publication.receipt.get("authority", "NONE"))
    model_published_at = sequence_publication.receipt.get("published_at")
    sequence_path = sequence_publication.run_directory / "distributions.parquet"
    sequence_inputs = (
        sequence_publication.run_directory / "manifest.json",
        sequence_publication.run_directory / "publication.json",
        sequence_path,
    )
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
    paper_trade: dict[str, object] | None = None
    if decision.action == "RESEARCH_PROPOSAL" and decision.candidate_id is not None:
        selected = merged.loc[
            merged["id"].astype("string").eq(decision.candidate_id)
        ]
        if len(selected) != 1:
            raise ValueError("Loop C selected candidate is not unique")
        paper_trade = build_paper_trade_snapshot(
            selected.iloc[0].to_dict(),
            decision=decision.as_record(),
            strategy_run_path=strategy.run_directory.relative_to(root).as_posix(),
            loop_c_run_path=run_directory.relative_to(root).as_posix(),
        )
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
        "paper_trade": paper_trade,
        "sequence_consumer": {
            "status": sequence.status,
            "matched_routes": sequence.matched_routes,
            "requested_routes": sequence.requested_routes,
            "model_binding": binding_summary,
        },
        "input_contracts": dict(input_contracts or {}),
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
            "sequence_model_binding": binding_summary,
            "paper_trade_id": (
                paper_trade.get("paper_trade_id")
                if isinstance(paper_trade, Mapping)
                else None
            ),
            "paper_trade_asset_class": "OPTIONS_STRATEGY",
            "paper_trade_eligible_horizons": ["1d", "1w"],
            "halt_requested": bool(halt_requested),
            "input_contracts": dict(input_contracts or {}),
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Loop C in observe-only mode.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--decision-timestamp", required=True)
    parser.add_argument("--portfolio-snapshot", type=Path, required=True)
    parser.add_argument("--broker-snapshot", type=Path, required=True)
    parser.add_argument("--halt-control", type=Path, required=True)
    parser.add_argument("--risk-limits", type=Path, required=True)
    parser.add_argument("--validate-inputs-only", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(root_dir=args.root_dir, target=args.datastore_target)
        inputs = load_loop_c_inputs(
            root,
            risk_limits_path=args.risk_limits,
            portfolio_snapshot_path=args.portfolio_snapshot,
            broker_snapshot_path=args.broker_snapshot,
            halt_control_path=args.halt_control,
            as_of=args.decision_timestamp,
        )
        if args.validate_inputs_only:
            if args.publish:
                raise ValueError("--publish cannot be used with --validate-inputs-only")
            result = {
                "status": "READY_INPUTS",
                "input_contracts": dict(inputs.public_summary),
                "halt_requested": inputs.halt_requested,
                "orders_enabled": False,
                "orders_placed": 0,
            }
        else:
            with exclusive_runtime_lock(
                root / ".ducketz-loop-c-observe.lock",
                process_name="Duckets Loop C observe runtime",
            ):
                result = run_loop_c_observe_once(
                    root,
                    decision_timestamp=args.decision_timestamp,
                    risk_limits=inputs.risk_limits,
                    model_binding=inputs.model_binding,
                    portfolio=inputs.portfolio,
                    broker=inputs.broker,
                    halt_requested=inputs.halt_requested,
                    input_contracts=inputs.public_summary,
                    source_files=inputs.source_files,
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


def _validate_sequence_model_binding(
    publication: object,
    binding: LoopCSequenceModelBinding,
) -> dict[str, object]:
    manifest = getattr(publication, "manifest", None)
    receipt = getattr(publication, "receipt", None)
    if not isinstance(manifest, Mapping) or not isinstance(receipt, Mapping):
        raise ValueError("Loop C sequence publication is malformed")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Loop C sequence manifest configuration is missing")
    runtime_configuration = configuration.get("configuration")
    if not isinstance(runtime_configuration, Mapping):
        raise ValueError("Loop C sequence runtime configuration is missing")
    fingerprint = semantic_metadata_fingerprint(runtime_configuration)
    consumers = configuration.get("consumers")
    if (
        manifest.get("model_name") != binding.model_name
        or configuration.get("policy_version") != binding.sequence_policy_version
        or fingerprint != binding.configuration_fingerprint
        or receipt.get("authority") != binding.required_authority
        or not isinstance(consumers, list)
        or binding.consumer not in consumers
    ):
        raise ValueError("Current sequence publication differs from the approved Loop C binding")
    return {
        "schema_version": binding.schema_version,
        "model_name": binding.model_name,
        "sequence_policy_version": binding.sequence_policy_version,
        "configuration_fingerprint": fingerprint,
        "distribution_schema_version": binding.distribution_schema_version,
        "authority": binding.required_authority,
        "consumer": binding.consumer,
        "horizons": list(binding.horizons),
    }


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["LOOP_C_DECISION_SCHEMA", "main", "run_loop_c_observe_once"]
