from __future__ import annotations

import argparse
import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.observability import timed_stage
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    utc_timestamp,
    write_manifest,
)
from ml.current_publication import read_current_publication
from ml.parquet_contracts import (
    STRATEGY_AUDIT_SCHEMA,
    STRATEGY_CANDIDATE_SCHEMA,
    empty_frame,
    frame_with_readable_id,
    write_parquet_with_schema,
)
from ml.option_pricing.strategy_shadow import (
    STRATEGY_PRICING_MODES,
    attach_strategy_pricing_shadow,
)
from ml.strategy_publication import (
    publish_strategy_run,
    read_current_strategy_publication,
)
from ml.strategy_selection import STRATEGY_SELECTION_SCHWAB_SPREADS_V1
from ml.strategy_selection.contracts import StrategySelectionPolicy
from ml.strategy_selection.research_trace import strategy_research_trace
from ml.strategy_selection.runtime import run_strategy_selection


@dataclass(frozen=True)
class StrategyRuntimeResult:
    run_directory: Path
    source_loop_b_directory: Path
    candidate_rows: int
    audit_rows: int
    models_trained: int
    models_reused: int
    published_at: pd.Timestamp


def run_strategy_once(
    datastore_root: Path,
    *,
    run_timestamp: object | None = None,
    runtime_clock: Callable[[], object] | None = None,
    reporter: Callable[[str], None] | None = print,
    pricing_mode: str = "off",
) -> StrategyRuntimeResult:
    """Consume one already-published Loop B run and publish a separate run."""

    root = Path(datastore_root)
    source = read_current_publication(root)
    source_record = dict(source.pointer.get("current", {}))
    if not source_record:
        source_record = {
            "run_path": source.run_directory.relative_to(root).as_posix(),
            "run_timestamp": source.manifest.get("run_timestamp"),
        }
    samples_path = source.run_directory / "samples.parquet"
    predictions_path = source.run_directory / "predictions.parquet"
    if not samples_path.is_file() or not predictions_path.is_file():
        raise RuntimeError(
            "Published Loop B run lacks samples or predictions required by Strategy"
        )
    created = utc_timestamp(run_timestamp)
    clock = runtime_clock or (lambda: utc_timestamp())
    configuration = source.manifest.get("configuration")
    configuration = configuration if isinstance(configuration, Mapping) else {}
    input_cutoff = utc_timestamp(
        configuration.get("causal_input_cutoff")
        or source.manifest.get("run_timestamp")
    )
    run_directory = create_timestamp_directory(
        root / "ml" / "strategy-runs",
        timestamp=created,
    )

    with timed_stage(
        "strategy.select",
        provider="schwab",
        schema="normalized-options-and-stock-bbo",
        reporter=reporter,
        extra={"source_loop_b_run": str(source.run_directory)},
    ) as timing:
        samples = pd.read_parquet(samples_path)
        predictions = pd.read_parquet(predictions_path)
        selection = run_strategy_selection(
            root,
            samples=samples,
            predictions=predictions,
            forbidden_target_starts={},
            run_timestamp=created,
            input_available_at=input_cutoff,
            sample_source_files=(
                samples_path,
                predictions_path,
                source.run_directory / "publication.json",
            ),
            history_available_not_after=created,
        )
        timing.annotate(
            row_count=len(selection.candidates),
            operation="compared",
            audit_rows=len(selection.audit),
        )

    pricing_shadow = attach_strategy_pricing_shadow(
        selection.candidates,
        datastore_root=root,
        pricing_mode=pricing_mode,
        available_not_after=created,
        per_contract_fee=StrategySelectionPolicy().per_contract_fee,
    )

    candidates = _strategy_output_frame(
        pricing_shadow.candidates,
        schema=STRATEGY_CANDIDATE_SCHEMA,
        key_columns=("symbol", "horizon", "decision_timestamp", "candidate_key"),
    )
    audit = _strategy_output_frame(
        selection.audit,
        schema=STRATEGY_AUDIT_SCHEMA,
        key_columns=(
            "symbol",
            "horizon",
            "decision_timestamp",
            "strategy_name",
        ),
    )
    candidates_name = "strategy-candidates.parquet"
    audit_name = "strategy-audit.parquet"
    write_parquet_with_schema(
        candidates,
        run_directory / candidates_name,
        STRATEGY_CANDIDATE_SCHEMA,
    )
    write_parquet_with_schema(
        audit,
        run_directory / audit_name,
        STRATEGY_AUDIT_SCHEMA,
    )

    copied_models = _copy_model_artifacts(
        root,
        run_directory,
        selection.model_reports,
    )
    reports_name = "strategy-model-reports.json"
    reports_payload = {
        "policy": STRATEGY_SELECTION_SCHWAB_SPREADS_V1,
        "models_trained": selection.models_trained,
        "models_reused": selection.models_reused,
        "model_reports": dict(selection.model_reports),
        "copied_model_artifacts": copied_models,
        "research_trace": strategy_research_trace(),
        "pricing_shadow": dict(pricing_shadow.report),
    }
    (run_directory / reports_name).write_text(
        json.dumps(reports_payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    model_files = tuple(
        path.relative_to(run_directory).as_posix()
        for path in sorted((run_directory / "model-artifacts").rglob("*"))
        if path.is_file()
    ) if (run_directory / "model-artifacts").is_dir() else ()
    output_names = (candidates_name, audit_name, reports_name, *model_files)
    option_receipts = _receipt_lineage(
        root,
        selection.source_files,
        marker=("options", "snapshots", "schwab"),
        receipt_name="receipt.json",
    )
    stock_bbo_files = _file_lineage(
        root,
        (
            path
            for path in selection.source_files
            if "quote-liquidity" in path.parts
        ),
    )
    write_manifest(
        run_directory,
        run_timestamp=created,
        input_files=tuple(
            dict.fromkeys(
                (
                    samples_path,
                    predictions_path,
                    source.run_directory / "publication.json",
                    *selection.source_files,
                    *pricing_shadow.source_files,
                )
            )
        ),
        output_files=output_names,
        configuration={
            "policy": STRATEGY_SELECTION_SCHWAB_SPREADS_V1,
            "pricing_mode": str(pricing_mode).strip().lower(),
            "pricing_shadow_contract": dict(pricing_shadow.report),
            "source_loop_b": source_record,
            "source_loop_b_run": source.run_directory.relative_to(root).as_posix(),
            "source_loop_b_input_cutoff": input_cutoff.isoformat(),
            "option_snapshot_receipts": option_receipts,
            "stock_bbo_source_files": stock_bbo_files,
            "publication_contract": {
                "version": "strategy-publication-v1",
                "authority": "ml/strategy-latest/run.json",
                "immutable_source_loop_b": True,
            },
        },
        datastore_root=root,
    )
    published_at = utc_timestamp(clock())
    publication = publish_strategy_run(
        root,
        run_directory=run_directory,
        source_loop_b=source_record,
        published_at=published_at,
    )
    return StrategyRuntimeResult(
        run_directory=publication.run_directory,
        source_loop_b_directory=source.run_directory,
        candidate_rows=len(candidates),
        audit_rows=len(audit),
        models_trained=selection.models_trained,
        models_reused=selection.models_reused,
        published_at=published_at,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run independent strategy selection from a published Loop B run."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--interval-minutes", type=int, default=60)
    parser.add_argument("--phase-offset-minutes", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--pricing-mode",
        choices=STRATEGY_PRICING_MODES,
        default="off",
        help=(
            "off preserves current behavior; shadow persists verified pre-quote "
            "Pricing diagnostics without changing ranks or order construction."
        ),
    )
    args = parser.parse_args(argv)
    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if not 0 <= args.phase_offset_minutes < args.interval_minutes:
        parser.error(
            "--phase-offset-minutes must satisfy 0 <= phase < interval-minutes"
        )
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    print("DUCKETS STRATEGY RUNTIME")
    print("========================")
    print(f"DATASTORE: {root}")
    print("Input: already-published authoritative Loop B run")
    print("Authority: ml/strategy-latest/run.json")
    print(f"Pricing diagnostics: {args.pricing_mode} (never changes candidate ranking)")
    print("Stop: Ctrl+C")
    print()
    lock = root / ".ducketz-strategy-runtime.lock"
    with exclusive_runtime_lock(lock, process_name="Duckets Strategy runtime"):
        try:
            while True:
                if not args.once:
                    boundary = next_boundary(
                        datetime.now(timezone.utc),
                        interval_minutes=args.interval_minutes,
                        phase_offset_minutes=args.phase_offset_minutes,
                    )
                    print(f"Next Strategy cycle: {boundary.isoformat()}")
                    time.sleep(
                        max(
                            0.0,
                            (boundary - datetime.now(timezone.utc)).total_seconds(),
                        )
                    )
                try:
                    if _current_source_already_processed(root, pricing_mode=args.pricing_mode):
                        print("Strategy skipped: current Loop B run is already published.")
                        if args.once:
                            return 0
                        continue
                    result = run_strategy_once(root, pricing_mode=args.pricing_mode)
                    print(
                        "Strategy published: "
                        f"candidates={result.candidate_rows}; audit={result.audit_rows}; "
                        f"run={result.run_directory}"
                    )
                    exit_code = 0
                except Exception as exc:
                    print(f"Strategy failed: {type(exc).__name__}: {exc}")
                    exit_code = 1
                if args.once:
                    return exit_code
        except KeyboardInterrupt:
            print("Strategy runtime stopped.")
            return 0


def next_boundary(
    now: datetime,
    *,
    interval_minutes: int,
    phase_offset_minutes: int,
) -> datetime:
    current = now.astimezone(timezone.utc)
    anchor = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=phase_offset_minutes
    )
    if current < anchor:
        return anchor
    count = int((current - anchor).total_seconds() // (interval_minutes * 60))
    return anchor + timedelta(minutes=(count + 1) * interval_minutes)


def _current_source_already_processed(root: Path, *, pricing_mode: str = "off") -> bool:
    try:
        loop_b = read_current_publication(root)
        strategy = read_current_strategy_publication(root)
    except Exception:
        return False
    source = strategy.receipt.get("source_loop_b")
    current = loop_b.pointer.get("current")
    configuration = strategy.manifest.get("configuration")
    observed_mode = (
        configuration.get("pricing_mode")
        if isinstance(configuration, Mapping)
        else None
    )
    return (
        isinstance(source, Mapping)
        and isinstance(current, Mapping)
        and dict(source) == dict(current)
        and observed_mode == str(pricing_mode).strip().lower()
    )


def _strategy_output_frame(
    frame: pd.DataFrame,
    *,
    schema: object,
    key_columns: Sequence[str],
) -> pd.DataFrame:
    if frame.empty:
        return empty_frame(schema)  # type: ignore[arg-type]
    identified = frame_with_readable_id(frame, key_columns=key_columns)
    return identified.loc[:, list(getattr(schema, "names"))].copy()


def _copy_model_artifacts(
    datastore_root: Path,
    run_directory: Path,
    reports: Mapping[str, Mapping[str, object]],
) -> dict[str, str]:
    root = Path(datastore_root).resolve()
    allowed = (root / "ml" / "strategy-models").resolve()
    copied: dict[str, str] = {}
    for horizon, report in reports.items():
        raw = report.get("artifact_directory")
        if not raw:
            continue
        source = Path(str(raw)).resolve()
        if allowed not in source.parents or not source.is_dir():
            raise RuntimeError(f"Strategy model artifact escapes model registry: {source}")
        destination = run_directory / "model-artifacts" / str(horizon) / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        copied[str(horizon)] = destination.relative_to(run_directory).as_posix()
    return copied


def _receipt_lineage(
    root: Path,
    paths: Sequence[Path],
    *,
    marker: tuple[str, ...],
    receipt_name: str,
) -> list[dict[str, object]]:
    selected = [
        path
        for path in paths
        if path.name == receipt_name
        and all(part in path.parts for part in marker)
    ]
    return _file_lineage(root, selected)


def _file_lineage(root: Path, paths: Sequence[Path]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in dict.fromkeys(Path(value) for value in paths):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            rendered = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            rendered = str(resolved)
        records.append(
            {
                "path": rendered,
                "size": resolved.stat().st_size,
                "checksum_sha256": file_checksum(resolved),
            }
        )
    return records


if __name__ == "__main__":
    raise SystemExit(main())
