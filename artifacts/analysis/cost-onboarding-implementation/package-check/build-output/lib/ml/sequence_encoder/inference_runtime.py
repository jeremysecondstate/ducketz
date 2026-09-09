from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import exchange_calendars as xcals
import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, utc_timestamp, write_manifest
from ml.current_publication import read_current_publication
from ml.parquet_contracts import write_parquet_with_schema
from ml.sequence_encoder.contracts import DISTRIBUTION_SCHEMA, EMBEDDING_SCHEMA
from ml.sequence_encoder.inference import infer_loop_b_sequence_shadow
from ml.sequence_encoder.publication import (
    publish_sequence_run,
    read_current_sequence_publication,
)


@dataclass(frozen=True)
class SequenceInferenceRunResult:
    status: str
    run_directory: Path | None
    distribution_rows: int
    embedding_rows: int
    published: bool


def run_sequence_inference_once(
    datastore_root: Path,
    *,
    information_cutoff: object,
    run_timestamp: object | None = None,
    if_changed: bool = True,
    publish_shadow: bool = True,
    require_market_open: bool = False,
) -> SequenceInferenceRunResult:
    root = Path(datastore_root).resolve()
    created = utc_timestamp(run_timestamp)
    cutoff = utc_timestamp(information_cutoff)
    if require_market_open and not _xnys_open(created):
        return SequenceInferenceRunResult(
            status="MARKET_CLOSED_SKIPPED",
            run_directory=None,
            distribution_rows=0,
            embedding_rows=0,
            published=False,
        )
    source_model = read_current_sequence_publication(root)
    loop_b = read_current_publication(root)
    source_loop_b = _current_record(loop_b.pointer)
    if if_changed and _same_source_loop_b(source_model.manifest, source_loop_b):
        return SequenceInferenceRunResult(
            status="UNCHANGED_SKIPPED",
            run_directory=source_model.run_directory,
            distribution_rows=_output_rows(
                source_model.run_directory / "distributions.parquet"
            ),
            embedding_rows=_output_rows(
                source_model.run_directory / "embeddings.parquet"
            ),
            published=False,
        )
    samples_path = loop_b.run_directory / "samples.parquet"
    predictions_path = loop_b.run_directory / "predictions.parquet"
    import pandas as pd

    samples = pd.read_parquet(samples_path)
    predictions = pd.read_parquet(predictions_path)
    inference = infer_loop_b_sequence_shadow(
        root,
        samples=samples,
        predictions=predictions,
        information_cutoff=cutoff,
        prediction_created_at=created,
    )
    if inference.distributions.empty or inference.embeddings.empty:
        raise ValueError(
            f"Sequence inference did not produce current distributions: {inference.status}"
        )
    run_directory = create_timestamp_directory(
        root / "ml" / "sequence-encoder-runs",
        timestamp=created,
    )
    for name in ("model.pt", "calibration.joblib", "preprocessor.json"):
        _copy_atomic(source_model.run_directory / name, run_directory / name)
    write_parquet_with_schema(
        inference.distributions,
        run_directory / "distributions.parquet",
        DISTRIBUTION_SCHEMA,
    )
    write_parquet_with_schema(
        inference.embeddings,
        run_directory / "embeddings.parquet",
        EMBEDDING_SCHEMA,
    )
    source_configuration = source_model.manifest.get("configuration")
    if not isinstance(source_configuration, Mapping):
        raise ValueError("Source sequence manifest configuration is invalid")
    model_origin = source_configuration.get("model_origin") or {
        "run_path": source_model.run_directory.relative_to(root).as_posix(),
        "run_timestamp": source_model.manifest.get("run_timestamp"),
    }
    report = {
        "schema_version": "pooled-causal-sequence-inference-report-v1",
        "status": inference.status,
        "authority": "SHADOW_ONLY",
        "source_loop_b": source_loop_b,
        "source_model": model_origin,
        "counts": {
            "distributions": len(inference.distributions),
            "embeddings": len(inference.embeddings),
        },
        "details": dict(inference.details),
        "safety": {
            "orders_enabled": False,
            "orders_placed": 0,
            "automated_action_allowed": False,
        },
    }
    _write_json_atomic(run_directory / "report.json", report)
    output_names = (
        "model.pt",
        "calibration.joblib",
        "preprocessor.json",
        "distributions.parquet",
        "embeddings.parquet",
        "report.json",
    )
    write_manifest(
        run_directory,
        run_timestamp=created,
        input_files=tuple(
            dict.fromkeys(
                (
                    samples_path,
                    predictions_path,
                    loop_b.run_directory / "publication.json",
                    *inference.source_files,
                )
            )
        ),
        output_files=output_names,
        model_name="pooled-causal-sequence-encoder",
        feature_columns=tuple(source_model.manifest.get("feature_columns", ())),
        target_column="multi_horizon_direction_and_cost_adjusted_return",
        configuration={
            "policy_version": source_configuration.get("policy_version"),
            "authority": "SHADOW_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
            "source_loop_b": source_loop_b,
            "model_origin": model_origin,
            "model_contract": source_configuration.get("model_contract"),
            "configuration": source_configuration.get("configuration"),
            "consumers": ["LOOP_B", "OPTIONS_STRATEGY", "LOOP_C_OBSERVE"],
        },
        datastore_root=root,
    )
    if publish_shadow:
        publish_sequence_run(
            root,
            run_directory=run_directory,
            published_at=created,
            source_loop_b=source_loop_b,
        )
    return SequenceInferenceRunResult(
        status=inference.status,
        run_directory=run_directory,
        distribution_rows=len(inference.distributions),
        embedding_rows=len(inference.embeddings),
        published=publish_shadow,
    )


def _same_source_loop_b(
    manifest: Mapping[str, object],
    source_loop_b: Mapping[str, object],
) -> bool:
    configuration = manifest.get("configuration")
    prior = configuration.get("source_loop_b") if isinstance(configuration, Mapping) else None
    return isinstance(prior, Mapping) and dict(prior) == dict(source_loop_b)


def _current_record(pointer: Mapping[str, object]) -> dict[str, object]:
    current = pointer.get("current")
    if isinstance(current, Mapping):
        return dict(current)
    return {
        "run_path": pointer.get("path"),
        "run_timestamp": pointer.get("run_timestamp"),
        "legacy": True,
    }


def _output_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    import pyarrow.parquet as pq

    return int(pq.ParquetFile(path).metadata.num_rows)


def _xnys_open(value: object) -> bool:
    now = utc_timestamp(value)
    local_date = now.tz_convert("America/New_York").date()
    calendar = xcals.get_calendar(
        "XNYS",
        start=local_date - pd.Timedelta(days=7),
        end=local_date + pd.Timedelta(days=7),
    )
    try:
        return bool(
            calendar.is_open_on_minute(now.floor("min"), ignore_breaks=False)
        )
    except Exception:
        return False


def _copy_atomic(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Sequence model artifact is missing: {source}")
    temporary = destination.with_suffix(destination.suffix + f".tmp-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(value), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish current shared sequence distributions in shadow mode."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--information-cutoff", required=True)
    parser.add_argument("--run-timestamp")
    parser.add_argument("--no-if-changed", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--require-market-open", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(root_dir=args.root_dir, target=args.datastore_target)
        with exclusive_runtime_lock(
            root / ".ducketz-sequence-encoder-runtime.lock",
            process_name="Duckets pooled sequence encoder",
        ):
            result = run_sequence_inference_once(
                root,
                information_cutoff=args.information_cutoff,
                run_timestamp=args.run_timestamp,
                if_changed=not args.no_if_changed,
                publish_shadow=not args.no_publish,
                require_market_open=args.require_market_open,
            )
        payload = {
            **asdict(result),
            "run_directory": str(result.run_directory) if result.run_directory else None,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 0
    except Exception as exc:
        payload = {
            "status": "ERROR",
            "error": str(exc),
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(
        json.dumps(
            payload,
            separators=(",", ":") if args.compact else None,
            default=str,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SequenceInferenceRunResult", "main", "run_sequence_inference_once"]
