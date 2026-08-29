from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum, utc_timestamp
from ml.runtime_pipeline import _canonical_live_evaluations
from technicals.parquet_io import discover_bar_datasets


ABLATION_SCHEMA_VERSION = "loops-nightly-target-source-ablation-v1"
RECEIPT_SCHEMA_VERSION = "loops-nightly-target-source-transform-proof-receipt-v1"
PREREGISTRATION_ID = "20260827-daily-regular-session-target-source-v2"
PREREGISTRATION_SHA256 = (
    "4c2a77cfb8c6abd32c8a3c73c874d0354a470275b8f84bf814307964161d8a86"
)
ELIGIBLE_SESSION = "2026-08-27"
SOURCE_RUN = "20260828T080625.382115Z"
CAUSAL_INPUT_CUTOFF = pd.Timestamp("2026-08-28T08:06:25.286678Z")
SESSION_CLOSE = pd.Timestamp("2026-08-27T20:00:00Z")
EXPECTED_PUBLICATION_SHA256 = (
    "085f4e4b4dba6a42fad2d32ce980dfd42845807c8f7f8784cf66850e6f4c69e4"
)
EXPECTED_MANIFEST_SHA256 = (
    "a38ca85faaf2480a0988d916f0deefdba5dd598c71f1c842e43a55f04270e32a"
)
EXPECTED_SAMPLES_SHA256 = (
    "4cfd023fc2de0c0a9e6a9a144f4a5ba0102170f5724fe95680f75d06b72152b1"
)
EXPECTED_EVALUATIONS_SHA256 = (
    "273483e84175e3437654f161097279905bfc843b1c6ba0c7b1daedd21279d393"
)
EXPECTED_TARGET_INPUT_SET_SHA256 = (
    "c1246e7764b8e94ea382d124227655006efb44aa6a408050ece2c1f3a54cda9e"
)
EXPECTED_GATE_SOURCE_SET_SHA256 = (
    "67ef1b8e4eeae87c0b7a43c6fe2fbeabbceaf7b2c3f2595058c3a3e0945bed88"
)
EXPECTED_COHORT_SHA256 = (
    "a5da6ed44aec0b56366ecee3a268baaf0f533d3551c8fd9116856fb1e4b77c6a"
)
EXPECTED_HANDOFF_SHA256 = (
    "282004615ca97847edf2ce7a1d6e73f1f59db9bc01d53d995d787dd999886680"
)
EXPECTED_HANDOFF_RELATIVE_PATH = Path(
    "logs/ducketz/system-guardian/scheduler-handoff/runs/"
    "00000061-20260828T090103059150Z.json"
)
EXPECTED_HORIZON_COUNTS = {
    "1d": 42,
    "1w": 18,
    "1w-d1": 42,
    "1w-d2": 30,
    "1w-d3": 18,
    "1w-d4": 6,
}
EXPECTED_LABEL_FLIPS = {
    "1d": 5,
    "1w": 2,
    "1w-d1": 5,
    "1w-d2": 4,
    "1w-d3": 2,
    "1w-d4": 1,
}
EXPECTED_SYMBOLS = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK")
EXPECTED_ROW_COUNT = 156
EXPECTED_ASSUMED_ROUND_TRIP_COST = 0.001

GATE_SOURCE_FILES = (
    "datafetching/derived_bars.py",
    "ml/rolling_samples.py",
    "ml/runtime_pipeline.py",
    "technicals/parquet_io.py",
    "tests/test_databento_derived_bars.py",
    "tests/test_ml_rolling_horizons.py",
    "tests/test_ml_runtime_pipeline.py",
)

_KEY_COLUMNS = (
    "symbol",
    "horizon",
    "decision_timestamp",
    "target_window_start",
    "target_window_end",
)
_ALLOWED_TRANSFORM_COLUMNS = {
    "target_open",
    "target_close",
    "observed_forward_raw_return",
    "observed_forward_cost_adjusted_return",
    "observed_target",
}


@dataclass(frozen=True)
class TargetSourceAblationResult:
    status: str
    decision: str
    directory: Path
    report_path: Path
    proof_path: Path
    manifest_path: Path
    receipt_path: Path
    report: Mapping[str, object]


def run_target_source_ablation(
    datastore_root: Path,
    *,
    repo_root: Path,
    created_at: object | None = None,
) -> TargetSourceAblationResult:
    """Run the one preregistered, receipt-bound, label-source transform.

    The transform is deliberately disconnected from every current/latest pointer,
    model authority, runtime owner, UI publication, portfolio path, and order path.
    """

    root = Path(datastore_root).resolve()
    repository = Path(repo_root).resolve()
    existing = _find_existing_result(root)
    if existing is not None:
        return existing

    created = utc_timestamp(created_at)
    preregistration = _validate_preregistration(root)
    source = _validate_source_run(root)
    gate_inventory = _validate_gate_sources(repository)
    target_input_inventory = _validate_target_inputs(
        root,
        manifest=source["manifest"],
    )

    cohort, samples = _load_frozen_cohort(root)
    proof, transform_summary = _build_transform_proof(
        root,
        cohort=cohort,
        samples=samples,
    )
    diagnostics = _diagnostics(proof)

    report: dict[str, object] = {
        "schema_version": ABLATION_SCHEMA_VERSION,
        "status": "COMPLETE_SHADOW_ONLY",
        "decision": "PROPOSAL_ONLY",
        "created_at": created.isoformat(),
        "eligible_session": ELIGIBLE_SESSION,
        "scope": "ISOLATED_READ_ONLY_OFFLINE_TARGET_TRANSFORM",
        "preregistration": preregistration,
        "source": {
            "run": SOURCE_RUN,
            "run_path": f"ml/runs/{SOURCE_RUN}",
            "causal_input_cutoff": CAUSAL_INPUT_CUTOFF.isoformat(),
            "publication_checksum_sha256": EXPECTED_PUBLICATION_SHA256,
            "manifest_checksum_sha256": EXPECTED_MANIFEST_SHA256,
            "samples_checksum_sha256": EXPECTED_SAMPLES_SHA256,
            "evaluations_checksum_sha256": EXPECTED_EVALUATIONS_SHA256,
        },
        "immutable_inputs": {
            "target_input_set_sha256": EXPECTED_TARGET_INPUT_SET_SHA256,
            "target_files": target_input_inventory,
            "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
            "gate_files": gate_inventory,
        },
        "cohort": _cohort_summary(cohort),
        "transform": transform_summary,
        "diagnostics": diagnostics,
        "safety": {
            "chronological_source_frozen": True,
            "causal_cutoff_verified": True,
            "cohort_frozen": True,
            "forecast_fields_unchanged": True,
            "fit_performed": False,
            "calibration_performed": False,
            "threshold_selection_performed": False,
            "lockbox_opened": False,
            "account_or_portfolio_read_performed": False,
            "production_mutation": False,
            "production_candidate_mutation": False,
            "production_model_authority_mutation": False,
            "production_authority_mutation": False,
            "runtime_mutation": False,
            "ui_or_ranking_mutation": False,
            "promotion_performed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
        "limitations": [
            "This is an integrity-only label-source transform, not a fitted challenger.",
            "Diagnostics are descriptive because nested daily/weekly rows are not independent.",
            "The current models were trained on the native daily label definition.",
            "Corporate-action and adjustment semantics require a separately authorized production repair review.",
        ],
        "next_gate": (
            "Stage 15 may compare only this immutable proof; any production "
            "source-selection repair requires a separately approved ML Improvement Proposal."
        ),
    }
    return _publish_result(
        root,
        created=created,
        report=report,
        proof=proof,
    )


def _validate_preregistration(root: Path) -> dict[str, object]:
    handoff_path = root / EXPECTED_HANDOFF_RELATIVE_PATH
    _require_file_checksum(
        handoff_path,
        EXPECTED_HANDOFF_SHA256,
        label="scheduler handoff receipt",
    )
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    if int(payload.get("sequence", -1)) != 61:
        raise RuntimeError("Preregistration handoff sequence is not 61")
    schedule = payload.get("schedule")
    if not isinstance(schedule, Mapping):
        raise RuntimeError("Preregistration handoff schedule is missing")
    if str(schedule.get("eligible_session")) != ELIGIBLE_SESSION:
        raise RuntimeError("Preregistration eligible session changed")
    if str(schedule.get("stage_id")) != "select-nightly-bottleneck":
        raise RuntimeError("Preregistration did not originate at stage 13")
    continuity = payload.get("continuity")
    actions = continuity.get("actions") if isinstance(continuity, Mapping) else None
    if not isinstance(actions, list):
        raise RuntimeError("Preregistration canonical fragments are missing")
    fragments: dict[int, str] = {}
    declared_fingerprint: str | None = None
    for raw_action in actions:
        action = str(raw_action)
        if action.startswith("PREREG_CANONICAL_") and "=" in action:
            marker, value = action.split("=", 1)
            number = int(marker.split("_OF_", 1)[0].rsplit("_", 1)[1])
            fragments[number] = value
        elif action.startswith("PREREG_SHA256="):
            declared_fingerprint = action.split("=", 1)[1]
    if sorted(fragments) != [1, 2, 3, 4, 5, 6]:
        raise RuntimeError("Preregistration must contain exactly six ordered fragments")
    canonical = "".join(fragments[index] for index in range(1, 7))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if fingerprint != PREREGISTRATION_SHA256 or declared_fingerprint != fingerprint:
        raise RuntimeError("Preregistration fingerprint does not verify")
    required_fragments = (
        f"schema=loops-nightly-prereg-v1|id={PREREGISTRATION_ID}",
        f"eligible_session={ELIGIBLE_SESSION}",
        f"source_run={SOURCE_RUN}",
        "causal_input_cutoff=2026-08-28T08:06:25.286678+00:00",
        f"publication_sha256={EXPECTED_PUBLICATION_SHA256}",
        f"source_manifest_sha256={EXPECTED_MANIFEST_SHA256}",
        f"samples_sha256={EXPECTED_SAMPLES_SHA256}",
        f"evaluations_sha256={EXPECTED_EVALUATIONS_SHA256}",
        f"target_input_set_sha256={EXPECTED_TARGET_INPUT_SET_SHA256}",
        f"gate_source_set_sha256={EXPECTED_GATE_SOURCE_SET_SHA256}",
        f"cohort_sha256={EXPECTED_COHORT_SHA256}",
        "harness=one small read-only offline transform composing those gates",
        "ml.strategy_value_challenger is NOT_APPLICABLE",
        "lockbox=CLOSED_UNTOUCHED",
        "orders_placed=0",
    )
    missing = [value for value in required_fragments if value not in canonical]
    if missing:
        raise RuntimeError(
            "Preregistration is missing required canonical content: " + repr(missing)
        )
    return {
        "id": PREREGISTRATION_ID,
        "sha256": fingerprint,
        "schema_version": "loops-nightly-prereg-v1",
        "fragment_count": 6,
        "canonical_utf8_bytes": len(canonical.encode("utf-8")),
        "handoff_sequence": 61,
        "handoff_receipt_path": EXPECTED_HANDOFF_RELATIVE_PATH.as_posix(),
        "handoff_receipt_checksum_sha256": EXPECTED_HANDOFF_SHA256,
    }


def _validate_source_run(root: Path) -> dict[str, object]:
    run = root / "ml" / "runs" / SOURCE_RUN
    publication_path = run / "publication.json"
    manifest_path = run / "manifest.json"
    samples_path = run / "samples.parquet"
    evaluations_path = run / "evaluations.parquet"
    _require_file_checksum(
        publication_path,
        EXPECTED_PUBLICATION_SHA256,
        label="source publication",
    )
    _require_file_checksum(
        manifest_path,
        EXPECTED_MANIFEST_SHA256,
        label="source manifest",
    )
    _require_file_checksum(
        samples_path,
        EXPECTED_SAMPLES_SHA256,
        label="source samples",
    )
    _require_file_checksum(
        evaluations_path,
        EXPECTED_EVALUATIONS_SHA256,
        label="source evaluations",
    )
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    if str(publication.get("run_path")) != f"ml/runs/{SOURCE_RUN}":
        raise RuntimeError("Source publication run path changed")
    if str(publication.get("manifest_checksum_sha256")) != EXPECTED_MANIFEST_SHA256:
        raise RuntimeError("Source publication no longer binds the frozen manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise RuntimeError("Source manifest configuration is missing")
    manifest_cutoff = pd.Timestamp(str(configuration.get("causal_input_cutoff")))
    if manifest_cutoff.tzinfo is None:
        manifest_cutoff = manifest_cutoff.tz_localize("UTC")
    else:
        manifest_cutoff = manifest_cutoff.tz_convert("UTC")
    if manifest_cutoff != CAUSAL_INPUT_CUTOFF:
        raise RuntimeError(
            "Source manifest causal cutoff differs from the preregistration"
        )
    output_files = manifest.get("output_files")
    if not isinstance(output_files, Mapping):
        raise RuntimeError("Source manifest output inventory is missing")
    expected_outputs = {
        "samples.parquet": EXPECTED_SAMPLES_SHA256,
        "evaluations.parquet": EXPECTED_EVALUATIONS_SHA256,
    }
    for name, checksum in expected_outputs.items():
        metadata = output_files.get(name)
        if not isinstance(metadata, Mapping):
            raise RuntimeError(f"Source manifest output is missing: {name}")
        if str(metadata.get("checksum_sha256")) != checksum:
            raise RuntimeError(f"Source manifest output checksum changed: {name}")
    return {"manifest": manifest, "publication": publication}


def _validate_gate_sources(repo_root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    hash_records: list[str] = []
    for relative in GATE_SOURCE_FILES:
        path = repo_root / Path(relative)
        if not path.is_file():
            raise RuntimeError(f"Preregistered gate source is missing: {path}")
        checksum = file_checksum(path)
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "checksum_sha256": checksum,
            }
        )
        hash_records.append(f"{relative}|{checksum}")
    aggregate = _aggregate_records(hash_records)
    if aggregate != EXPECTED_GATE_SOURCE_SET_SHA256:
        raise RuntimeError("Preregistered checked-in gate source set changed")
    return sorted(records, key=lambda item: str(item["path"]))


def _validate_target_inputs(
    root: Path,
    *,
    manifest: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_inputs = manifest.get("input_files")
    if not isinstance(raw_inputs, list):
        raise RuntimeError("Source manifest input inventory is missing")
    manifest_inputs = {
        str(item.get("path")): item
        for item in raw_inputs
        if isinstance(item, Mapping)
    }
    expected_paths = []
    for symbol in EXPECTED_SYMBOLS:
        prefix = f"stocks\\{symbol}\\bars\\1d\\databento\\normalized\\"
        expected_paths.extend(
            (
                prefix + f"{symbol}_derived_1m_1d.parquet",
                prefix + f"{symbol}_source_2555d_1d_ohlcv-1d_1d.parquet",
            )
        )
    records: list[dict[str, object]] = []
    hash_records: list[str] = []
    cutoff_ns = int(CAUSAL_INPUT_CUTOFF.value)
    for relative in sorted(expected_paths):
        metadata = manifest_inputs.get(relative)
        if not isinstance(metadata, Mapping):
            raise RuntimeError(f"Frozen target input is absent from manifest: {relative}")
        forward = relative.replace("\\", "/")
        path = root.joinpath(*forward.split("/"))
        if not path.is_file():
            raise RuntimeError(f"Frozen target input is missing: {path}")
        checksum = file_checksum(path)
        if checksum != str(metadata.get("checksum_sha256")):
            raise RuntimeError(f"Frozen target input checksum changed: {relative}")
        manifest_mtime_ns = int(metadata.get("modified_time_ns", -1))
        if path.stat().st_mtime_ns != manifest_mtime_ns:
            raise RuntimeError(f"Frozen target input mtime changed: {relative}")
        if manifest_mtime_ns > cutoff_ns:
            raise RuntimeError(f"Frozen target input is after causal cutoff: {relative}")
        records.append(
            {
                "path": forward,
                "size": path.stat().st_size,
                "modified_time_ns": manifest_mtime_ns,
                "checksum_sha256": checksum,
            }
        )
        hash_records.append(f"{forward}|{checksum}")
    if len(records) != 12:
        raise RuntimeError("Preregistration requires exactly 12 daily target inputs")
    aggregate = _aggregate_records(hash_records)
    if aggregate != EXPECTED_TARGET_INPUT_SET_SHA256:
        raise RuntimeError("Preregistered daily target input set changed")
    return records


def _load_frozen_cohort(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    run = root / "ml" / "runs" / SOURCE_RUN
    evaluations = pd.read_parquet(run / "evaluations.parquet")
    canonical = _canonical_live_evaluations(evaluations)
    cohort = canonical.loc[
        canonical["horizon"].isin(EXPECTED_HORIZON_COUNTS)
        & pd.to_datetime(canonical["target_window_end"], utc=True).le(SESSION_CLOSE)
    ].copy()
    cohort = cohort.sort_values("id", kind="mergesort").reset_index(drop=True)
    ids = sorted(cohort["id"].astype(str))
    if len(ids) != EXPECTED_ROW_COUNT:
        raise RuntimeError("Frozen cohort row count changed")
    if _aggregate_records(ids) != EXPECTED_COHORT_SHA256:
        raise RuntimeError("Frozen cohort identity changed")
    counts = {
        str(key): int(value)
        for key, value in cohort["horizon"].value_counts().sort_index().items()
    }
    if counts != EXPECTED_HORIZON_COUNTS:
        raise RuntimeError("Frozen cohort horizon counts changed")
    if tuple(sorted(cohort["symbol"].astype(str).unique())) != EXPECTED_SYMBOLS:
        raise RuntimeError("Frozen cohort symbols changed")
    costs = pd.to_numeric(cohort["assumed_round_trip_cost"], errors="coerce")
    if not costs.eq(EXPECTED_ASSUMED_ROUND_TRIP_COST).all():
        raise RuntimeError("Frozen cohort round-trip cost changed")

    samples = pd.read_parquet(run / "samples.parquet")
    for column in _KEY_COLUMNS:
        if column in {"decision_timestamp", "target_window_start", "target_window_end"}:
            samples[column] = pd.to_datetime(samples[column], utc=True)
            cohort[column] = pd.to_datetime(cohort[column], utc=True)
    keys = cohort.loc[:, list(_KEY_COLUMNS)].drop_duplicates()
    if len(keys) != len(cohort):
        raise RuntimeError("Frozen evaluation cohort has duplicate natural keys")
    matched = samples.merge(keys, on=list(_KEY_COLUMNS), how="inner", validate="one_to_one")
    if len(matched) != len(cohort):
        raise RuntimeError("Frozen samples do not map one-to-one to evaluations")
    max_label_available = pd.to_datetime(
        matched["label_available_at"], utc=True, errors="coerce"
    ).max()
    if max_label_available != pd.Timestamp("2026-08-27T20:05:00Z"):
        raise RuntimeError("Frozen cohort label-availability bound changed")
    if max_label_available > CAUSAL_INPUT_CUTOFF:
        raise RuntimeError("Frozen cohort labels were not causally mature at cutoff")
    return cohort, matched


def _build_transform_proof(
    root: Path,
    *,
    cohort: pd.DataFrame,
    samples: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, object]]:
    sample_columns = [
        *_KEY_COLUMNS,
        "label_available_at",
        "target_open",
        "target_close",
        "forward_raw_return",
        "forward_cost_adjusted_return",
        "target_cost_adjusted_positive",
        "assumed_round_trip_cost",
    ]
    baseline = cohort.merge(
        samples.loc[:, sample_columns],
        on=list(_KEY_COLUMNS),
        how="inner",
        validate="one_to_one",
        suffixes=("", "_sample"),
    )
    if len(baseline) != EXPECTED_ROW_COUNT:
        raise RuntimeError("Frozen sample/evaluation join changed")
    _require_numeric_match(
        baseline["observed_forward_raw_return"],
        baseline["forward_raw_return"],
        label="published raw-return sample/evaluation parity",
    )
    _require_numeric_match(
        baseline["observed_forward_cost_adjusted_return"],
        baseline["forward_cost_adjusted_return"],
        label="published cost-adjusted sample/evaluation parity",
    )
    if not pd.to_numeric(baseline["observed_target"], errors="coerce").eq(
        pd.to_numeric(
            baseline["target_cost_adjusted_positive"], errors="coerce"
        )
    ).all():
        raise RuntimeError("Published target sample/evaluation parity changed")

    native_lookup: dict[tuple[str, pd.Timestamp], tuple[float, float]] = {}
    derived_lookup: dict[tuple[str, pd.Timestamp], tuple[float, float]] = {}
    constituent_session_failures: list[str] = []
    for symbol in EXPECTED_SYMBOLS:
        folder = root / "stocks" / symbol / "bars" / "1d" / "databento" / "normalized"
        native = pd.read_parquet(
            folder / f"{symbol}_source_2555d_1d_ohlcv-1d_1d.parquet"
        )
        derived = pd.read_parquet(folder / f"{symbol}_derived_1m_1d.parquet")
        for frame, lookup, label in (
            (native, native_lookup, "native"),
            (derived, derived_lookup, "derived"),
        ):
            frame = frame.copy()
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            if frame["timestamp"].duplicated().any():
                raise RuntimeError(f"{label} daily input has duplicate sessions: {symbol}")
            for row in frame.itertuples(index=False):
                lookup[(symbol, pd.Timestamp(row.timestamp).normalize())] = (
                    float(row.open),
                    float(row.close),
                )

        discovered = discover_bar_datasets(
            root,
            symbol=symbol,
            providers=("databento",),
            timeframes={"1d"},
        )
        if len(discovered) != 1:
            raise RuntimeError(f"Canonical 1d discovery changed for {symbol}")
        discovered_frame = discovered[0].frame.copy()
        discovered_frame["timestamp"] = pd.to_datetime(
            discovered_frame["timestamp"], utc=True
        )
        discovered_rows = {
            pd.Timestamp(row.timestamp).normalize(): (float(row.open), float(row.close))
            for row in discovered_frame.itertuples(index=False)
        }
        native_rows = {
            session: values
            for (row_symbol, session), values in native_lookup.items()
            if row_symbol == symbol
        }
        for session in sorted(
            set(pd.to_datetime(baseline.loc[baseline["symbol"].eq(symbol), "target_window_start"], utc=True).dt.normalize())
            | set(pd.to_datetime(baseline.loc[baseline["symbol"].eq(symbol), "target_window_end"], utc=True).dt.normalize())
        ):
            if discovered_rows.get(session) != native_rows.get(session):
                raise RuntimeError(
                    f"Canonical daily discovery no longer selects native input: {symbol} {session}"
                )

    candidate_rows: list[dict[str, object]] = []
    for row in baseline.itertuples(index=False):
        symbol = str(row.symbol)
        start_session = pd.Timestamp(row.target_window_start).normalize()
        end_session = pd.Timestamp(row.target_window_end).normalize()
        native_start = native_lookup.get((symbol, start_session))
        native_end = native_lookup.get((symbol, end_session))
        derived_start = derived_lookup.get((symbol, start_session))
        derived_end = derived_lookup.get((symbol, end_session))
        if None in (native_start, native_end, derived_start, derived_end):
            raise RuntimeError(
                f"Target endpoint session is missing: {symbol} {start_session} {end_session}"
            )
        assert native_start is not None
        assert native_end is not None
        assert derived_start is not None
        assert derived_end is not None
        published_open = float(row.target_open)
        published_close = float(row.target_close)
        native_open = native_start[0]
        native_close = native_end[1]
        candidate_open = derived_start[0]
        candidate_close = derived_end[1]
        if (published_open, published_close) != (native_open, native_close):
            raise RuntimeError("Published endpoints no longer match native daily inputs")
        if (published_open, published_close) == (candidate_open, candidate_close):
            raise RuntimeError("Published endpoints unexpectedly match derived inputs")
        if not all(
            math.isfinite(value) and value > 0.0
            for value in (candidate_open, candidate_close)
        ):
            raise RuntimeError("Candidate endpoints must be finite and positive")
        candidate_raw = candidate_close / candidate_open - 1.0
        cost = float(row.assumed_round_trip_cost)
        candidate_cost_adjusted = candidate_raw - cost
        candidate_label = int(candidate_cost_adjusted > 0.0)
        candidate_rows.append(
            {
                "id": str(row.id),
                "symbol": symbol,
                "horizon": str(row.horizon),
                "decision_timestamp": pd.Timestamp(row.decision_timestamp),
                "target_window_start": pd.Timestamp(row.target_window_start),
                "target_window_end": pd.Timestamp(row.target_window_end),
                "label_available_at": pd.Timestamp(row.label_available_at),
                "prediction_created_at": pd.Timestamp(row.prediction_created_at),
                "model_name": str(row.model_name),
                "model_version": str(row.model_version),
                "raw_probability": float(row.raw_probability),
                "calibrated_probability": float(row.calibrated_probability),
                "assumed_round_trip_cost": cost,
                "native_target_open": native_open,
                "native_target_close": native_close,
                "published_target_open": published_open,
                "published_target_close": published_close,
                "published_forward_raw_return": float(row.forward_raw_return),
                "published_forward_cost_adjusted_return": float(
                    row.forward_cost_adjusted_return
                ),
                "published_target": int(row.target_cost_adjusted_positive),
                "candidate_target_open": candidate_open,
                "candidate_target_close": candidate_close,
                "candidate_forward_raw_return": candidate_raw,
                "candidate_forward_cost_adjusted_return": candidate_cost_adjusted,
                "candidate_target": candidate_label,
                "label_changed": bool(
                    candidate_label != int(row.target_cost_adjusted_positive)
                ),
            }
        )

        sessions = _xnys_sessions(start_session, end_session)
        for session in sessions:
            if (symbol, session) not in derived_lookup:
                constituent_session_failures.append(
                    f"{symbol}|{start_session.date()}|{end_session.date()}|{session.date()}"
                )
    if constituent_session_failures:
        raise RuntimeError(
            "Candidate target windows lack derived constituent sessions: "
            + ", ".join(constituent_session_failures[:10])
        )

    proof = pd.DataFrame(candidate_rows).sort_values("id", kind="mergesort")
    if len(proof) != EXPECTED_ROW_COUNT:
        raise RuntimeError("Transform proof row count changed")
    _require_numeric_match(
        proof["published_forward_raw_return"],
        proof["published_target_close"] / proof["published_target_open"] - 1.0,
        label="published raw-return recomputation",
    )
    _require_numeric_match(
        proof["published_forward_cost_adjusted_return"],
        proof["published_forward_raw_return"] - proof["assumed_round_trip_cost"],
        label="published cost-adjusted return recomputation",
    )
    _require_numeric_match(
        proof["candidate_forward_raw_return"],
        proof["candidate_target_close"] / proof["candidate_target_open"] - 1.0,
        label="candidate raw-return recomputation",
    )
    _require_numeric_match(
        proof["candidate_forward_cost_adjusted_return"],
        proof["candidate_forward_raw_return"] - proof["assumed_round_trip_cost"],
        label="candidate cost-adjusted return recomputation",
    )
    expected_candidate_label = proof[
        "candidate_forward_cost_adjusted_return"
    ].gt(0.0).astype(int)
    if not proof["candidate_target"].eq(expected_candidate_label).all():
        raise RuntimeError("Candidate label recomputation changed")
    flips = {
        str(key): int(value)
        for key, value in proof.groupby("horizon")["label_changed"].sum().sort_index().items()
    }
    if flips != EXPECTED_LABEL_FLIPS:
        raise RuntimeError("Candidate label-flip baseline changed")

    unchanged_columns = [
        column
        for column in cohort.columns
        if column not in _ALLOWED_TRANSFORM_COLUMNS
    ]
    candidate_evaluations = cohort.copy(deep=True)
    candidate_evaluations["observed_forward_raw_return"] = proof[
        "candidate_forward_raw_return"
    ].to_numpy()
    candidate_evaluations["observed_forward_cost_adjusted_return"] = proof[
        "candidate_forward_cost_adjusted_return"
    ].to_numpy()
    candidate_evaluations["observed_target"] = proof["candidate_target"].to_numpy()
    if not candidate_evaluations.loc[:, unchanged_columns].equals(
        cohort.loc[:, unchanged_columns]
    ):
        raise RuntimeError("A nonlabel forecast field changed in the transform")

    transition_matrix = _transition_matrix(
        proof["published_target"], proof["candidate_target"]
    )
    summary = {
        "candidate_definition": (
            "derived_1m_1d open at target start exchange session and close at "
            "target end exchange session"
        ),
        "baseline_definition": (
            "canonical discovery merge selecting source_2555d_1d_ohlcv-1d_1d"
        ),
        "rows": len(proof),
        "symbols": int(proof["symbol"].nunique()),
        "horizon_counts": {
            str(key): int(value)
            for key, value in proof["horizon"].value_counts().sort_index().items()
        },
        "published_endpoint_matches_native": len(proof),
        "published_endpoint_matches_derived": 0,
        "candidate_endpoint_matches_derived": len(proof),
        "candidate_endpoint_mismatches": 0,
        "candidate_return_recomputation_mismatches": 0,
        "candidate_label_recomputation_mismatches": 0,
        "nonlabel_forecast_field_mismatches": 0,
        "constituent_session_failures": 0,
        "label_changes": int(proof["label_changed"].sum()),
        "label_changes_by_horizon": flips,
        "native_to_candidate_label_transition_matrix": transition_matrix,
        "round_trip_cost": EXPECTED_ASSUMED_ROUND_TRIP_COST,
        "production_wiring": False,
    }
    return proof.reset_index(drop=True), summary


def _cohort_summary(cohort: pd.DataFrame) -> dict[str, object]:
    return {
        "sha256": EXPECTED_COHORT_SHA256,
        "rows": len(cohort),
        "symbols": sorted(cohort["symbol"].astype(str).unique()),
        "horizon_counts": {
            str(key): int(value)
            for key, value in cohort["horizon"].value_counts().sort_index().items()
        },
        "decision_timestamp_min": pd.Timestamp(
            cohort["decision_timestamp"].min()
        ).isoformat(),
        "decision_timestamp_max": pd.Timestamp(
            cohort["decision_timestamp"].max()
        ).isoformat(),
        "target_window_start_min": pd.Timestamp(
            cohort["target_window_start"].min()
        ).isoformat(),
        "target_window_start_max": pd.Timestamp(
            cohort["target_window_start"].max()
        ).isoformat(),
        "target_window_end_max": pd.Timestamp(
            cohort["target_window_end"].max()
        ).isoformat(),
        "prediction_vintages_preserved": True,
        "model_generations_preserved": True,
    }


def _diagnostics(proof: pd.DataFrame) -> dict[str, object]:
    by_horizon: dict[str, object] = {}
    by_cluster: list[dict[str, object]] = []
    for horizon, frame in proof.groupby("horizon", sort=True):
        cluster_sizes = frame.groupby("target_window_start")["id"].transform("size")
        equal_cluster_weights = 1.0 / pd.to_numeric(cluster_sizes, errors="raise").to_numpy(dtype=float)
        by_horizon[str(horizon)] = {
            "rows": len(frame),
            "target_start_clusters": int(frame["target_window_start"].nunique()),
            "row_weighted": _comparison_metrics(frame),
            "equal_target_start_cluster_weighted": _comparison_metrics(
                frame,
                sample_weight=equal_cluster_weights,
            ),
        }
        for target_start, cluster in frame.groupby("target_window_start", sort=True):
            by_cluster.append(
                {
                    "horizon": str(horizon),
                    "target_window_start": pd.Timestamp(target_start).isoformat(),
                    "rows": len(cluster),
                    "metrics": _comparison_metrics(cluster),
                }
            )
    return {
        "interpretation": "DESCRIPTIVE_ONLY_NO_POST_HOC_ACCEPTANCE",
        "by_horizon": by_horizon,
        "by_horizon_target_start_cluster": by_cluster,
    }


def _comparison_metrics(
    frame: pd.DataFrame,
    *,
    sample_weight: np.ndarray | None = None,
) -> dict[str, object]:
    return {
        "published_labels": {
            "raw": _probability_metrics(
                frame["published_target"],
                frame["raw_probability"],
                sample_weight=sample_weight,
            ),
            "calibrated": _probability_metrics(
                frame["published_target"],
                frame["calibrated_probability"],
                sample_weight=sample_weight,
            ),
        },
        "candidate_labels": {
            "raw": _probability_metrics(
                frame["candidate_target"],
                frame["raw_probability"],
                sample_weight=sample_weight,
            ),
            "calibrated": _probability_metrics(
                frame["candidate_target"],
                frame["calibrated_probability"],
                sample_weight=sample_weight,
            ),
        },
    }


def _probability_metrics(
    target_values: Sequence[object] | pd.Series,
    probability_values: Sequence[object] | pd.Series,
    *,
    sample_weight: np.ndarray | None = None,
) -> dict[str, object]:
    target = pd.to_numeric(pd.Series(target_values), errors="raise").to_numpy(dtype=int)
    probability = pd.to_numeric(
        pd.Series(probability_values), errors="raise"
    ).to_numpy(dtype=float)
    if target.ndim != 1 or probability.ndim != 1 or len(target) != len(probability):
        raise RuntimeError("Probability diagnostics require aligned vectors")
    if not np.isfinite(probability).all() or np.any(probability < 0.0) or np.any(probability > 1.0):
        raise RuntimeError("Probability diagnostics require finite values in [0, 1]")
    if not set(np.unique(target)).issubset({0, 1}):
        raise RuntimeError("Probability diagnostics require binary targets")
    weights = (
        np.ones(len(target), dtype=float)
        if sample_weight is None
        else np.asarray(sample_weight, dtype=float)
    )
    if weights.shape != target.shape or not np.isfinite(weights).all() or np.any(weights <= 0.0):
        raise RuntimeError("Probability diagnostic weights are invalid")
    clipped = np.clip(probability, 1e-12, 1.0 - 1e-12)
    log_loss = -np.average(
        target * np.log(clipped) + (1 - target) * np.log(1 - clipped),
        weights=weights,
    )
    brier = np.average((probability - target) ** 2, weights=weights)
    auc = (
        float(roc_auc_score(target, probability, sample_weight=weights))
        if np.unique(target).size == 2
        else None
    )
    boundaries = np.linspace(0.0, 1.0, 11)
    total_weight = float(weights.sum())
    ece = 0.0
    reliability_bins: list[dict[str, object]] = []
    for index in range(10):
        lower = float(boundaries[index])
        upper = float(boundaries[index + 1])
        selected = (probability >= lower) & (
            probability <= upper if index == 9 else probability < upper
        )
        count = int(selected.sum())
        selected_weight = float(weights[selected].sum())
        if count:
            mean_probability = float(
                np.average(probability[selected], weights=weights[selected])
            )
            observed_rate = float(
                np.average(target[selected], weights=weights[selected])
            )
            ece += selected_weight / total_weight * abs(mean_probability - observed_rate)
        else:
            mean_probability = None
            observed_rate = None
        reliability_bins.append(
            {
                "lower": lower,
                "upper": upper,
                "rows": count,
                "weight": selected_weight,
                "mean_probability": mean_probability,
                "observed_rate": observed_rate,
            }
        )
    return {
        "rows": len(target),
        "target_base_rate": float(np.average(target, weights=weights)),
        "brier_score": float(brier),
        "log_loss": float(log_loss),
        "roc_auc": auc,
        "expected_calibration_error_10_bin": float(ece),
        "reliability_bins": reliability_bins,
    }


def _transition_matrix(
    baseline: Sequence[object] | pd.Series,
    candidate: Sequence[object] | pd.Series,
) -> dict[str, int]:
    left = pd.to_numeric(pd.Series(baseline), errors="raise").astype(int)
    right = pd.to_numeric(pd.Series(candidate), errors="raise").astype(int)
    if len(left) != len(right):
        raise RuntimeError("Transition matrix inputs are not aligned")
    return {
        f"{source}_to_{target}": int(((left == source) & (right == target)).sum())
        for source in (0, 1)
        for target in (0, 1)
    }


def _xnys_sessions(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Timestamp, ...]:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(start.tz_localize(None), end.tz_localize(None))
    normalized: list[pd.Timestamp] = []
    for value in sessions:
        timestamp = pd.Timestamp(value)
        normalized.append(
            timestamp.tz_localize("UTC")
            if timestamp.tzinfo is None
            else timestamp.tz_convert("UTC")
        )
    return tuple(normalized)


def _publish_result(
    root: Path,
    *,
    created: pd.Timestamp,
    report: Mapping[str, object],
    proof: pd.DataFrame,
) -> TargetSourceAblationResult:
    parent = root / "ml" / "nightly-target-source-ablation-runs"
    parent.mkdir(parents=True, exist_ok=True)
    base = created.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = parent / base
    suffix = 2
    while destination.exists():
        destination = parent / f"{base}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        proof_path = staging / "transform-proof.parquet"
        report_path = staging / "report.json"
        manifest_path = staging / "manifest.json"
        proof.to_parquet(proof_path, index=False)
        _write_json(report_path, report)
        manifest = {
            "schema_version": "loops-nightly-target-source-ablation-manifest-v1",
            "created_at": created.isoformat(),
            "preregistration_id": PREREGISTRATION_ID,
            "source_fingerprint_sha256": PREREGISTRATION_SHA256,
            "inputs": {
                "handoff_receipt_checksum_sha256": EXPECTED_HANDOFF_SHA256,
                "publication_checksum_sha256": EXPECTED_PUBLICATION_SHA256,
                "manifest_checksum_sha256": EXPECTED_MANIFEST_SHA256,
                "samples_checksum_sha256": EXPECTED_SAMPLES_SHA256,
                "evaluations_checksum_sha256": EXPECTED_EVALUATIONS_SHA256,
                "target_input_set_sha256": EXPECTED_TARGET_INPUT_SET_SHA256,
                "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
                "cohort_sha256": EXPECTED_COHORT_SHA256,
            },
            "outputs": {
                "report.json": {
                    "size": report_path.stat().st_size,
                    "checksum_sha256": file_checksum(report_path),
                },
                "transform-proof.parquet": {
                    "size": proof_path.stat().st_size,
                    "checksum_sha256": file_checksum(proof_path),
                    "rows": len(proof),
                },
            },
        }
        _write_json(manifest_path, manifest)
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "COMPLETE_SHADOW_ONLY",
            "decision": "PROPOSAL_ONLY",
            "created_at": created.isoformat(),
            "eligible_session": ELIGIBLE_SESSION,
            "run_path": destination.relative_to(root).as_posix(),
            "preregistration_id": PREREGISTRATION_ID,
            "source_fingerprint_sha256": PREREGISTRATION_SHA256,
            "source_run": SOURCE_RUN,
            "cohort_sha256": EXPECTED_COHORT_SHA256,
            "row_count": len(proof),
            "report_checksum_sha256": file_checksum(report_path),
            "proof_checksum_sha256": file_checksum(proof_path),
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "promotion_performed": False,
            "production_mutation": False,
            "production_candidate_mutation": False,
            "production_model_authority_mutation": False,
            "production_authority_mutation": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return _load_result(destination, status="COMPLETE_SHADOW_ONLY")


def _find_existing_result(root: Path) -> TargetSourceAblationResult | None:
    parent = root / "ml" / "nightly-target-source-ablation-runs"
    if not parent.is_dir():
        return None
    matches: list[Path] = []
    for receipt_path in sorted(parent.glob("*/receipt.json")):
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("source_fingerprint_sha256")) == PREREGISTRATION_SHA256:
            matches.append(receipt_path.parent)
    if len(matches) > 1:
        raise RuntimeError("More than one receipt exists for the preregistered fingerprint")
    if not matches:
        return None
    return _load_result(matches[0], status="UNCHANGED_SKIPPED")


def _load_result(directory: Path, *, status: str) -> TargetSourceAblationResult:
    receipt_path = directory / "receipt.json"
    report_path = directory / "report.json"
    proof_path = directory / "transform-proof.parquet"
    manifest_path = directory / "manifest.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if str(receipt.get("schema_version")) != RECEIPT_SCHEMA_VERSION:
        raise RuntimeError("Transform-proof receipt schema is invalid")
    if str(receipt.get("status")) != "COMPLETE_SHADOW_ONLY":
        raise RuntimeError("Transform-proof receipt is not complete")
    if str(receipt.get("decision")) != "PROPOSAL_ONLY":
        raise RuntimeError("Transform-proof receipt decision changed")
    if int(receipt.get("row_count", -1)) != EXPECTED_ROW_COUNT:
        raise RuntimeError("Transform-proof receipt row count changed")
    expected_files = {
        report_path: str(receipt.get("report_checksum_sha256")),
        proof_path: str(receipt.get("proof_checksum_sha256")),
        manifest_path: str(receipt.get("manifest_checksum_sha256")),
    }
    for path, checksum in expected_files.items():
        _require_file_checksum(path, checksum, label="transform-proof artifact")
    forbidden_true = (
        "promotion_performed",
        "production_mutation",
        "production_candidate_mutation",
        "production_model_authority_mutation",
        "production_authority_mutation",
        "orders_enabled",
    )
    if any(bool(receipt.get(name)) for name in forbidden_true):
        raise RuntimeError("Transform-proof receipt violates isolation safety")
    if int(receipt.get("orders_placed", -1)) != 0:
        raise RuntimeError("Transform-proof receipt violates zero-order safety")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return TargetSourceAblationResult(
        status=status,
        decision=str(receipt["decision"]),
        directory=directory,
        report_path=report_path,
        proof_path=proof_path,
        manifest_path=manifest_path,
        receipt_path=receipt_path,
        report=report,
    )


def _require_file_checksum(path: Path, expected: str, *, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"{label} is missing: {path}")
    actual = file_checksum(path)
    if actual != expected:
        raise RuntimeError(f"{label} checksum mismatch: {path}")


def _require_numeric_match(
    left: Sequence[object] | pd.Series,
    right: Sequence[object] | pd.Series,
    *,
    label: str,
) -> None:
    left_values = pd.to_numeric(pd.Series(left), errors="raise").to_numpy(dtype=float)
    right_values = pd.to_numeric(pd.Series(right), errors="raise").to_numpy(dtype=float)
    if left_values.shape != right_values.shape or not np.allclose(
        left_values,
        right_values,
        rtol=0.0,
        atol=1e-15,
        equal_nan=False,
    ):
        raise RuntimeError(f"{label} changed")


def _aggregate_records(records: Sequence[str]) -> str:
    payload = "\n".join(sorted(str(record) for record in records))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _compact_result(result: TargetSourceAblationResult) -> dict[str, object]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": result.status,
        "decision": result.decision,
        "run_path": str(result.directory),
        "receipt_path": str(result.receipt_path),
        "receipt_checksum_sha256": file_checksum(result.receipt_path),
        "report_path": str(result.report_path),
        "proof_path": str(result.proof_path),
        "source_fingerprint_sha256": PREREGISTRATION_SHA256,
        "promotion_performed": False,
        "production_mutation": False,
        "production_candidate_mutation": False,
        "production_model_authority_mutation": False,
        "production_authority_mutation": False,
        "orders_enabled": False,
        "orders_placed": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the preregistered daily/weekly target-source shadow transform."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    try:
        result = run_target_source_ablation(root, repo_root=args.repo_root)
    except Exception as exc:
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "BLOCKED",
            "decision": "BLOCKED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "source_fingerprint_sha256": PREREGISTRATION_SHA256,
            "promotion_performed": False,
            "production_mutation": False,
            "production_candidate_mutation": False,
            "production_model_authority_mutation": False,
            "production_authority_mutation": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        print(
            json.dumps(payload, sort_keys=True, default=str)
            if args.compact
            else json.dumps(payload, indent=2, sort_keys=True, default=str)
        )
        return 2
    payload = _compact_result(result)
    print(
        json.dumps(payload, sort_keys=True, default=str)
        if args.compact
        else json.dumps(payload, indent=2, sort_keys=True, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ABLATION_SCHEMA_VERSION",
    "PREREGISTRATION_ID",
    "PREREGISTRATION_SHA256",
    "RECEIPT_SCHEMA_VERSION",
    "TargetSourceAblationResult",
    "run_target_source_ablation",
]
