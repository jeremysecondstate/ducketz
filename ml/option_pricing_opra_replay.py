from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.databento_opra_history import record_consumer_usage
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum, utc_timestamp
from ml.option_pricing.causal import evaluate_offline_predictions
from ml.option_pricing.opra_materialization import materialize_committed_opra_history
from ml.option_pricing.prediction import create_prediction_rows
from ml.option_pricing.rates import load_point_in_time_rate_observations


REPLAY_VERSION = "option-pricing-opra-causal-replay-v1"
REPLAY_RECEIPT_VERSION = "option-pricing-opra-causal-replay-receipt-v1"


def run_opra_pricing_replay(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    published_at: object | None = None,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    published = utc_timestamp(published_at)
    rate_observations, rate_files = load_point_in_time_rate_observations(root)
    samples, source_files, errors = materialize_committed_opra_history(
        root,
        symbols=symbols,
        rate_observations=rate_observations,
    )
    source_files = tuple(dict.fromkeys((*source_files, *rate_files)))
    if samples.empty:
        raise RuntimeError("Verified OPRA history produced no causal Pricing samples")
    prediction_frames: list[pd.DataFrame] = []
    for target, frame in samples.groupby("target_snapshot_for", sort=True):
        created = pd.Timestamp(target)
        available = created + pd.Timedelta(seconds=60)
        prediction_frames.append(
            create_prediction_rows(
                frame,
                prediction_created_at=created,
                prediction_available_at=available,
                include_baseline_uncertainty=True,
            )
        )
    predictions = pd.concat(prediction_frames, ignore_index=True, sort=False)
    evaluations = evaluate_offline_predictions(
        predictions,
        samples,
        evaluated_at=published,
    )
    if predictions.empty or evaluations.empty:
        raise RuntimeError("OPRA replay produced no Pricing predictions/evaluations")
    if not samples["source_provider"].astype("string").eq("databento-opra").all():
        raise RuntimeError("OPRA replay samples contain another provider")
    natural = ["symbol", "target_snapshot_for", "contract_symbol"]
    if samples.duplicated(natural).any() or predictions.duplicated(
        [*natural, "prediction_created_at"]
    ).any():
        raise RuntimeError("OPRA replay contains duplicate natural keys")
    source_clock = pd.to_datetime(samples["source_snapshot_for"], utc=True)
    target_clock = pd.to_datetime(samples["target_snapshot_for"], utc=True)
    prediction_clock = pd.to_datetime(
        predictions["prediction_available_at"], utc=True
    )
    observed_clock = pd.to_datetime(samples["observed_available_at"], utc=True)
    if not source_clock.lt(target_clock).all():
        raise RuntimeError("OPRA replay source evidence leaks a target clock")
    observed_lookup = samples.set_index(natural)["observed_available_at"]
    prediction_keys = pd.MultiIndex.from_frame(predictions[natural])
    aligned_observed = pd.to_datetime(
        observed_lookup.reindex(prediction_keys).to_numpy(), utc=True
    )
    if not (prediction_clock.to_numpy() < aligned_observed).all():
        raise RuntimeError("OPRA replay outcome is not strictly post-prediction")

    runs = root / "ml" / "option-pricing-opra-replay-runs"
    runs.mkdir(parents=True, exist_ok=True)
    destination = runs / published.strftime("%Y%m%dT%H%M%S.%fZ")
    if destination.exists():
        destination = runs / f"{destination.name}-{uuid.uuid4().hex[:8]}"
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=runs))
    try:
        frames = {
            "pricing-samples.parquet": samples,
            "pricing-predictions.parquet": predictions,
            "pricing-evaluations.parquet": evaluations,
        }
        outputs: dict[str, dict[str, object]] = {}
        for name, frame in frames.items():
            path = staging / name
            frame.to_parquet(path, index=False, compression="zstd")
            outputs[name] = {
                "row_count": len(frame),
                "size_bytes": path.stat().st_size,
                "checksum_sha256": file_checksum(path),
            }
        manifest = {
            "schema_version": REPLAY_VERSION,
            "provider": "databento-opra",
            "dataset": "OPRA.PILLAR",
            "mode": "OFFLINE_CAUSAL_REPLAY",
            "published_at": published.isoformat(),
            "symbols": sorted(set(samples["symbol"].astype(str))),
            "target_count": int(samples["target_snapshot_for"].nunique()),
            "earliest_target": pd.Timestamp(samples["target_snapshot_for"].min()).isoformat(),
            "latest_target": pd.Timestamp(samples["target_snapshot_for"].max()).isoformat(),
            "source_provider_rows": {
                str(key): int(value)
                for key, value in samples["source_provider"].value_counts().items()
            },
            "complete_evaluation_rows": int(
                evaluations["evaluation_status"].astype("string").eq("COMPLETE").sum()
            ),
            "duplicate_natural_key_rows": 0,
            "future_data_leakage_rows": 0,
            "materialization_errors": dict(errors),
            "input_files": [
                {
                    "path": Path(path).relative_to(root).as_posix(),
                    "checksum_sha256": file_checksum(path),
                }
                for path in source_files
                if Path(path).is_file() and root in Path(path).resolve().parents
            ],
            "outputs": outputs,
        }
        _write_json(staging / "manifest.json", manifest)
        receipt = {
            "schema_version": REPLAY_RECEIPT_VERSION,
            "provider": "databento-opra",
            "published_at": published.isoformat(),
            "run_path": destination.relative_to(root).as_posix(),
            "manifest_checksum_sha256": file_checksum(staging / "manifest.json"),
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except Exception:
        raise
    pointer = root / "ml" / "option-pricing-opra-replay-latest" / "run.json"
    _write_json_atomic(
        pointer,
        {
            "schema_version": "option-pricing-opra-causal-replay-pointer-v1",
            "current": {
                "run_path": destination.relative_to(root).as_posix(),
                "published_at": published.isoformat(),
                "receipt_checksum_sha256": file_checksum(destination / "receipt.json"),
            },
        },
    )
    record_consumer_usage(
        root,
        consumer="active-pricing-replay-publication",
        schemas=("definition", "cbbo-1m"),
        rows=len(samples),
        source_files=source_files,
    )
    return {
        "run_directory": destination,
        "samples": len(samples),
        "predictions": len(predictions),
        "evaluations": len(evaluations),
        "complete_evaluations": int(
            evaluations["evaluation_status"].astype("string").eq("COMPLETE").sum()
        ),
        "manifest": manifest,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a causal offline Active Pricing replay from verified OPRA history."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    parser.add_argument("--symbols", nargs="+", required=True)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    result = run_opra_pricing_replay(root, symbols=args.symbols)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    _write_json(temporary, payload)
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
