from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.databento_archive import archive_lineage_sources
from datafetching.databento_opra_history import canonical_root, record_consumer_usage
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum, utc_timestamp
from ml.option_pricing.causal import evaluate_offline_predictions
from ml.option_pricing.opra_materialization import (
    _normalize_prediction_clocks,
    materialize_committed_opra_history,
)
from ml.option_pricing.prediction import create_prediction_rows
from ml.option_pricing.rates import load_point_in_time_rate_observations


REPLAY_VERSION = "option-pricing-opra-causal-replay-v1"
REPLAY_RECEIPT_VERSION = "option-pricing-opra-causal-replay-receipt-v1"


def run_opra_pricing_replay(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    published_at: object | None = None,
    target_clocks: Mapping[object, Sequence[object]] | None = None,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    published = utc_timestamp(published_at)
    clean_symbols = tuple(
        sorted(
            {
                str(value).strip().upper()
                for value in symbols
                if str(value).strip()
            }
        )
    )
    clocks = _normalize_prediction_clocks(target_clocks)
    source_fingerprint = opra_replay_source_fingerprint(
        root, symbols=clean_symbols
    )
    base = _verified_current_replay(
        root,
        symbols=clean_symbols,
        source_fingerprint=source_fingerprint,
    )
    rate_observations, rate_files = load_point_in_time_rate_observations(root)
    consumed: list[Path] = list(rate_files)
    errors: dict[str, str] = {}
    requested_clock_records: dict[
        pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]
    ] = {}

    if base is not None and clocks:
        samples, source_files, materialization_errors = (
            materialize_committed_opra_history(
                root,
                symbols=clean_symbols,
                rate_observations=rate_observations,
                target_snapshot_fors=tuple(clocks),
                prediction_clocks=clocks,
            )
        )
        predictions = _prediction_rows(samples)
        evaluations = (
            evaluate_offline_predictions(
                predictions,
                samples,
                evaluated_at=published,
            )
            if not predictions.empty
            else pd.DataFrame()
        )
        base_samples, base_predictions, base_evaluations = _base_frames(base)
        base_samples = _repair_emulated_creation_clock(base_samples)
        base_predictions = _repair_emulated_creation_clock(base_predictions)
        base_evaluations = _repair_emulated_creation_clock(base_evaluations)
        replaced_targets = frozenset(clocks)
        samples = _replace_targets(base_samples, samples, replaced_targets)
        predictions = _replace_targets(
            base_predictions, predictions, replaced_targets
        )
        evaluations = _replace_targets(
            base_evaluations, evaluations, replaced_targets
        )
        consumed.extend(_manifest_input_paths(root, base["manifest"]))
        consumed.extend(source_files)
        errors.update(_manifest_errors(base["manifest"]))
        errors.update(materialization_errors)
        requested_clock_records.update(
            _manifest_requested_clocks(base["manifest"])
        )
        requested_clock_records.update(clocks)
    else:
        samples, source_files, materialization_errors = (
            materialize_committed_opra_history(
                root,
                symbols=clean_symbols,
                rate_observations=rate_observations,
            )
        )
        consumed.extend(source_files)
        errors.update(materialization_errors)
        if clocks:
            targeted, targeted_files, targeted_errors = (
                materialize_committed_opra_history(
                    root,
                    symbols=clean_symbols,
                    rate_observations=rate_observations,
                    target_snapshot_fors=tuple(clocks),
                    prediction_clocks=clocks,
                )
            )
            samples = _replace_targets(
                samples,
                targeted,
                frozenset(clocks),
            )
            consumed.extend(targeted_files)
            errors.update(targeted_errors)
            requested_clock_records.update(clocks)
        predictions = _prediction_rows(samples)
        evaluations = (
            evaluate_offline_predictions(
                predictions,
                samples,
                evaluated_at=published,
            )
            if not predictions.empty
            else pd.DataFrame()
        )

    consumed_source_files = tuple(dict.fromkeys((*consumed, *rate_files)))
    source_files = _compact_replay_lineage(root, consumed_source_files)
    if samples.empty:
        raise RuntimeError("Verified OPRA history produced no causal Pricing samples")
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
    prediction_created = pd.to_datetime(
        predictions["prediction_created_at"], utc=True
    )
    prediction_target = pd.to_datetime(
        predictions["target_snapshot_for"], utc=True
    )
    observed_clock = pd.to_datetime(samples["observed_available_at"], utc=True)
    if not source_clock.lt(target_clock).all():
        raise RuntimeError("OPRA replay source evidence leaks a target clock")
    if not (
        prediction_target.lt(prediction_created)
        & prediction_created.le(prediction_clock)
    ).all():
        raise RuntimeError("OPRA replay prediction clocks are not strictly causal")
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
            "requested_prediction_clocks": [
                {
                    "target_snapshot_for": target.isoformat(),
                    "prediction_created_at": values[0].isoformat(),
                    "prediction_available_at": values[1].isoformat(),
                }
                for target, values in sorted(requested_clock_records.items())
            ],
            "opra_source_fingerprint": source_fingerprint,
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
        source_files=consumed_source_files,
        refresh_health=False,
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


def ensure_opra_pricing_replay(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    published_at: object | None = None,
    target_clocks: Mapping[object, Sequence[object]] | None = None,
) -> Mapping[str, object] | None:
    """Reuse a current replay or rebuild it when verified OPRA inputs change."""

    root = Path(datastore_root).resolve()
    clean_symbols = tuple(
        sorted(
            {
                str(value).strip().upper()
                for value in symbols
                if str(value).strip()
            }
        )
    )
    fingerprint = opra_replay_source_fingerprint(root, symbols=clean_symbols)
    if not fingerprint:
        return None
    clocks = _normalize_prediction_clocks(target_clocks)
    current = _verified_current_replay(
        root,
        symbols=clean_symbols,
        source_fingerprint=fingerprint,
    )
    if current is not None and (
        not clocks
        or set(clocks).issubset(
            _manifest_requested_clocks(current["manifest"])
        )
    ):
        return {
            "run_directory": current["run_directory"],
            "reused": True,
            "manifest": current["manifest"],
        }
    result = dict(
        run_opra_pricing_replay(
            root,
            symbols=clean_symbols,
            published_at=published_at,
            target_clocks=clocks,
        )
    )
    result["reused"] = False
    return result


def _prediction_rows(samples: pd.DataFrame) -> pd.DataFrame:
    if samples.empty:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for _target, frame in samples.groupby("target_snapshot_for", sort=True):
        created_values = pd.to_datetime(
            frame["prediction_created_at"], utc=True, errors="coerce"
        ).dropna().unique()
        available_values = pd.to_datetime(
            frame["prediction_available_at"], utc=True, errors="coerce"
        ).dropna().unique()
        if len(created_values) != 1 or len(available_values) != 1:
            raise RuntimeError(
                "One OPRA replay target must have one emulated prediction clock"
            )
        frames.append(
            create_prediction_rows(
                frame,
                prediction_created_at=pd.Timestamp(created_values[0]),
                prediction_available_at=pd.Timestamp(available_values[0]),
                include_baseline_uncertainty=True,
            )
        )
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _replace_targets(
    base: pd.DataFrame,
    replacement: pd.DataFrame,
    targets: frozenset[pd.Timestamp],
) -> pd.DataFrame:
    if base.empty:
        return replacement.reset_index(drop=True)
    target_values = pd.to_datetime(
        base["target_snapshot_for"], utc=True, errors="coerce"
    )
    retained = base.loc[~target_values.isin(targets)]
    if replacement.empty:
        return retained.reset_index(drop=True)
    return pd.concat(
        [retained, replacement], ignore_index=True, sort=False
    ).reset_index(drop=True)


def _repair_emulated_creation_clock(frame: pd.DataFrame) -> pd.DataFrame:
    """Migrate legacy replays whose emulated creation equaled the target."""

    if frame.empty or "prediction_created_at" not in frame:
        return frame
    output = frame.copy()
    targets = pd.to_datetime(
        output["target_snapshot_for"], utc=True, errors="coerce"
    )
    created = pd.to_datetime(
        output["prediction_created_at"], utc=True, errors="coerce"
    )
    invalid = created.notna() & targets.notna() & created.le(targets)
    output.loc[invalid, "prediction_created_at"] = (
        targets.loc[invalid] + pd.Timedelta(seconds=1)
    )
    return output


def _base_frames(
    current: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    run = Path(current["run_directory"])
    return (
        pd.read_parquet(run / "pricing-samples.parquet"),
        pd.read_parquet(run / "pricing-predictions.parquet"),
        pd.read_parquet(run / "pricing-evaluations.parquet"),
    )


def _verified_current_replay(
    root: Path,
    *,
    symbols: Sequence[str],
    source_fingerprint: str,
) -> Mapping[str, object] | None:
    pointer_path = root / "ml" / "option-pricing-opra-replay-latest" / "run.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        current = pointer["current"]
        run = (root / str(current["run_path"])).resolve()
        manifest_path = run / "manifest.json"
        receipt_path = run / "receipt.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        outputs = manifest.get("outputs")
        if (
            pointer.get("schema_version")
            != "option-pricing-opra-causal-replay-pointer-v1"
            or root not in run.parents
            or current.get("receipt_checksum_sha256")
            != file_checksum(receipt_path)
            or receipt.get("schema_version") != REPLAY_RECEIPT_VERSION
            or receipt.get("manifest_checksum_sha256")
            != file_checksum(manifest_path)
            or manifest.get("schema_version") != REPLAY_VERSION
            or manifest.get("opra_source_fingerprint") != source_fingerprint
            or not set(manifest.get("symbols", ())).issuperset(symbols)
            or not isinstance(outputs, Mapping)
        ):
            return None
        for name in (
            "pricing-samples.parquet",
            "pricing-predictions.parquet",
            "pricing-evaluations.parquet",
        ):
            path = run / name
            item = outputs.get(name)
            if (
                not isinstance(item, Mapping)
                or not path.is_file()
                or path.stat().st_size != int(item.get("size_bytes", -1))
                or file_checksum(path) != item.get("checksum_sha256")
            ):
                return None
        return {
            "run_directory": run,
            "manifest": manifest,
            "manifest_path": manifest_path,
            "receipt_path": receipt_path,
            "pointer_path": pointer_path,
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _manifest_input_paths(
    root: Path,
    manifest: Mapping[str, object],
) -> tuple[Path, ...]:
    paths: list[Path] = []
    values = manifest.get("input_files")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        path = (root / str(item.get("path") or "")).resolve()
        if root in path.parents and path.is_file():
            paths.append(path)
    return tuple(dict.fromkeys(paths))


def _manifest_errors(manifest: Mapping[str, object]) -> dict[str, str]:
    values = manifest.get("materialization_errors")
    return (
        {str(key): str(value) for key, value in values.items()}
        if isinstance(values, Mapping)
        else {}
    )


def _manifest_requested_clocks(
    manifest: Mapping[str, object],
) -> dict[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]]:
    values = manifest.get("requested_prediction_clocks")
    raw: dict[object, Sequence[object]] = {}
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for item in values:
            if not isinstance(item, Mapping):
                continue
            raw[item.get("target_snapshot_for")] = (
                item.get("prediction_created_at"),
                item.get("prediction_available_at"),
            )
    try:
        return _normalize_prediction_clocks(raw)
    except (TypeError, ValueError):
        return {}


def opra_replay_source_fingerprint(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
) -> str:
    root = Path(datastore_root).resolve()
    selected = {
        str(value).strip().upper() for value in symbols if str(value).strip()
    }
    import hashlib

    digest = hashlib.sha256()
    count = 0
    for schema in ("definition", "cbbo-1m"):
        for manifest_path in sorted(
            canonical_root(root).glob(
                f"{schema}/*/dates/*/segments/*/manifest.json"
            )
        ):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                request = manifest.get("request")
                request_symbols = (
                    request.get("symbols") if isinstance(request, Mapping) else ()
                )
                parents = {
                    str(value).strip().upper().removesuffix(".OPT")
                    for value in request_symbols or ()
                }
                if selected and parents.isdisjoint(selected):
                    continue
                normalized = manifest["normalized"]
                receipt_path = manifest_path.with_name("receipt.json")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt.get("manifest_checksum_sha256") != file_checksum(
                    manifest_path
                ):
                    continue
            except (KeyError, OSError, json.JSONDecodeError):
                continue
            digest.update(manifest_path.as_posix().encode("utf-8"))
            digest.update(
                str(normalized.get("checksum_sha256") or "").encode("utf-8")
            )
            digest.update(file_checksum(receipt_path).encode("utf-8"))
            count += 1
    return digest.hexdigest() if count else ""


def _compact_replay_lineage(
    root: Path,
    source_files: Sequence[Path],
) -> tuple[Path, ...]:
    datastore = Path(root).resolve()
    opra_root = canonical_root(datastore).resolve()
    compact: list[Path] = []
    for raw_path in source_files:
        path = Path(raw_path).resolve()
        if path.name == "normalized.parquet" and opra_root in path.parents:
            compact.extend(
                (path.with_name("manifest.json"), path.with_name("receipt.json"))
            )
            continue
        compact.append(path)
        if path.suffix == ".parquet" and "stocks" in path.parts:
            compact.extend(archive_lineage_sources(datastore, path))
    return tuple(dict.fromkeys(path for path in compact if path.is_file()))


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
