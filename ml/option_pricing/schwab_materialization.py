from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import tracemalloc
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.bar_readiness import BarReadinessError, read_bar_readiness
from datafetching.bar_timing import bar_end_timestamps
from datafetching.decision_time import is_eligible_option_target
from datafetching.ids import add_readable_id
from ml.artifacts import file_checksum, semantic_metadata_fingerprint, utc_timestamp
from ml.option_pricing.causal import build_causal_samples, reconcile_predictions
from ml.option_pricing.policies import (
    ContractSelectionPolicy,
    LEGACY_LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
    LOOP_NATIVE_CALL_PUTS,
    LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
    LOOP_NATIVE_SYMBOLS,
    OPTION_PRICING_CONTRACT_POLICY_VERSION,
    OPTION_PRICING_DIVIDEND_POLICY_VERSION,
    OPTION_PRICING_EXPIRATION_POLICY_VERSION,
    OPTION_PRICING_RATE_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
    OPTION_PRICING_VOLATILITY_POLICY_VERSION,
    SEMANTIC_FEATURE_COLUMNS,
)
from ml.option_pricing.publication import (
    authoritative_option_pricing_runs,
    receipt_proven_prediction_rows,
)
from ml.option_pricing.target_outcome import authoritative_target_outcomes
from options.publication import (
    CommittedOptionSnapshot,
    canonical_option_snapshots,
    committed_option_snapshots,
)


LEGACY_SCHWAB_MATERIALIZATION_SCHEMA_VERSION = "loop-native-schwab-materialization-v2"
SCHWAB_MATERIALIZATION_SCHEMA_VERSION = "loop-native-provider-materialization-v3"
SCHWAB_MATERIALIZATION_RECEIPT_VERSION = (
    "loop-native-provider-materialization-receipt-v3"
)
LEGACY_SCHWAB_MATERIALIZATION_RECEIPT_VERSION = (
    "loop-native-schwab-materialization-receipt-v2"
)
SCHWAB_MATERIALIZATION_POINTER_VERSION = (
    "loop-native-provider-materialization-pointer-v3"
)
LEGACY_SCHWAB_MATERIALIZATION_POINTER_VERSION = (
    "loop-native-schwab-materialization-pointer-v2"
)
SCHWAB_MATERIALIZATION_SAMPLE_NAME = "causal-residual-samples.parquet"
SCHWAB_MATERIALIZATION_REPORT_NAME = "materialization-report.json"
SCHWAB_MATERIALIZATION_MANIFEST_NAME = "manifest.json"
SCHWAB_MATERIALIZATION_RECEIPT_NAME = "receipt.json"
OFFLINE_SCHWAB_BOOTSTRAP = "OFFLINE_SCHWAB_BOOTSTRAP"
PROSPECTIVE_SCHWAB = "PROSPECTIVE_SCHWAB"
OFFLINE_OPRA_BACKFILL = "OFFLINE_OPRA_BACKFILL"
PROSPECTIVE_OPRA = "PROSPECTIVE_OPRA"

_SEMANTIC_COLUMNS = (
    "symbol",
    "contract_symbol",
    "expiration_date",
    "call_put",
    "strike",
    "multiplier",
    "mini",
    "non_standard",
)


class SchwabMaterializationError(RuntimeError):
    """Receipt-proven Schwab evidence failed the Loop-native contract."""


@dataclass(frozen=True)
class SnapshotCollapse:
    selected: Mapping[tuple[str, pd.Timestamp], CommittedOptionSnapshot]
    eligible: Mapping[str, tuple[CommittedOptionSnapshot, ...]]
    report: Mapping[str, object]


@dataclass(frozen=True)
class SchwabMaterialization:
    directory: Path | None
    samples: pd.DataFrame
    report: Mapping[str, object]
    manifest: Mapping[str, object]
    receipt: Mapping[str, object] | None
    source_files: tuple[Path, ...]
    dry_run: bool
    reused: bool = False


def materialize_loop_native_schwab_history(
    datastore_root: Path,
    *,
    symbols: Sequence[str] = LOOP_NATIVE_SYMBOLS,
    trainer_cutoff: object,
    rate_observations: pd.DataFrame | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    offline_emulation_delay_seconds: int = 60,
    dry_run: bool = False,
    published_at: object | None = None,
    opra_samples: pd.DataFrame | None = None,
    opra_source_files: Sequence[Path] = (),
) -> SchwabMaterialization:
    """Build OPRA-primary evidence with Schwab fallback, without provider calls."""

    root = Path(datastore_root).resolve()
    cutoff = utc_timestamp(trainer_cutoff)
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    if clean_symbols != LOOP_NATIVE_SYMBOLS:
        raise SchwabMaterializationError(
            f"Loop-native materialization requires the exact {len(LOOP_NATIVE_SYMBOLS)}-symbol production universe"
        )
    if offline_emulation_delay_seconds < 0:
        raise ValueError("offline_emulation_delay_seconds cannot be negative")
    policy = contract_policy or ContractSelectionPolicy()
    started = time.perf_counter()
    tracing_started_here = not tracemalloc.is_tracing()
    if tracing_started_here:
        tracemalloc.start()
    try:
        snapshots_by_symbol = {
            symbol: committed_option_snapshots(root, symbol=symbol)
            for symbol in clean_symbols
        }
        opra_snapshots_by_symbol: dict[
            str, tuple[CommittedOptionSnapshot, ...]
        ] = {}
        opra_snapshot_reports: dict[str, Mapping[str, object]] = {}
        for symbol in clean_symbols:
            snapshots, snapshot_report = canonical_option_snapshots(
                root,
                symbol=symbol,
                provider="databento-opra",
                available_not_after=cutoff,
            )
            opra_snapshots_by_symbol[symbol] = snapshots
            opra_snapshot_reports[symbol] = snapshot_report
        collapsed = collapse_schwab_publications(
            snapshots_by_symbol,
            datastore_root=root,
            cutoff=cutoff,
        )
        bootstrap, bootstrap_report, bootstrap_files = _bootstrap_samples(
            root,
            collapse=collapsed,
            trainer_cutoff=cutoff,
            rate_observations=rate_observations,
            contract_policy=policy,
            emulation_delay_seconds=offline_emulation_delay_seconds,
        )
        prospective_snapshots_by_symbol = {
            symbol: tuple(
                sorted(
                    (
                        *opra_snapshots_by_symbol.get(symbol, ()),
                        *collapsed.eligible.get(symbol, ()),
                    ),
                    key=lambda value: (
                        value.snapshot_for,
                        0 if value.provider == "databento-opra" else 1,
                        _receipt_time(value),
                    ),
                )
            )
            for symbol in clean_symbols
        }
        prospective, prospective_report, prospective_files = _prospective_samples(
            root,
            snapshots_by_symbol=prospective_snapshots_by_symbol,
            trainer_cutoff=cutoff,
            rate_observations=rate_observations,
            contract_policy=policy,
        )
        schwab_samples = _canonical_materialized_samples(bootstrap, prospective)
        _validate_available_sample_causality(
            schwab_samples, trainer_cutoff=cutoff, root=root
        )
        prepared_opra = _prepare_opra_samples(
            opra_samples,
            trainer_cutoff=cutoff,
        )
        _validate_opra_sample_causality(prepared_opra, trainer_cutoff=cutoff)
        disagreement = _provider_disagreement_report(prepared_opra, schwab_samples)
        samples = _canonical_provider_samples(prepared_opra, schwab_samples)
        route_report = _route_report(samples, symbols=clean_symbols)
        elapsed = time.perf_counter() - started
        peak_memory = tracemalloc.get_traced_memory()[1]
        report = {
            "schema_version": SCHWAB_MATERIALIZATION_SCHEMA_VERSION,
            "policy_version": LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
            "trainer_cutoff": cutoff.isoformat(),
            "scope": {
                "symbols": list(clean_symbols),
                "call_puts": list(LOOP_NATIVE_CALL_PUTS),
                "routes": [
                    {"symbol": symbol, "call_put": call_put}
                    for symbol in clean_symbols
                    for call_put in LOOP_NATIVE_CALL_PUTS
                ],
            },
            "snapshot_collapse": dict(collapsed.report),
            "opra_snapshot_canonicalization": opra_snapshot_reports,
            "offline_bootstrap": bootstrap_report,
            "prospective": prospective_report,
            "opra": {
                "evidence_lane": OFFLINE_OPRA_BACKFILL,
                "sample_rows": len(prepared_opra),
                "available_rows": int(
                    prepared_opra.get(
                        "sample_status", pd.Series(dtype="string")
                    ).astype("string").eq("AVAILABLE").sum()
                ),
                "prospective_count_increment": 0,
                "prospective_snapshot_count": sum(
                    len(value) for value in opra_snapshots_by_symbol.values()
                ),
            },
            "provider_precedence": ["databento-opra", "schwab"],
            "provider_disagreement": disagreement,
            "routes": route_report,
            "input_coverage": _input_coverage_report(samples),
            "sample_rows": len(samples),
            "available_sample_rows": int(
                samples.get("sample_status", pd.Series(dtype="string"))
                .astype("string")
                .eq("AVAILABLE")
                .sum()
            ),
            "runtime": {
                "elapsed_seconds": elapsed,
                "peak_memory_bytes": peak_memory,
            },
            "external_provider_requests": 0,
            "historical_opra_used": not prepared_opra.empty,
            "current_revised_rate_history_used_for_historical_targets": False,
            "automated_action_allowed": False,
        }
        source_files = tuple(
            dict.fromkeys(
                (*map(Path, opra_source_files), *bootstrap_files, *prospective_files)
            )
        )
        manifest_base = {
            "schema_version": SCHWAB_MATERIALIZATION_SCHEMA_VERSION,
            "policy_version": LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
            "trainer_cutoff": cutoff.isoformat(),
            "scope": report["scope"],
            "selected_input_receipts": _selected_receipt_inventory(
                collapsed.selected,
                root=root,
            ),
            "input_files": _input_file_inventory(source_files, root=root),
            "causality_validation": {
                "status": "PASS",
                "feature_cutoff": "offline-emulated-or-prospective-prediction-created",
                "label_rule": "target-quote-and-receipt-strictly-after-publication",
                "trainer_rule": "observed-receipt-strictly-before-trainer-cutoff",
                "underlying_rule": "immutable-loop-a-readiness-strictly-before-prediction",
                "target_snapshot_allowed_as_feature": False,
                "target_time_iv_allowed_as_feature": False,
                "provider_precedence": "databento-opra-then-schwab",
                "offline_opra_prospective_credit_allowed": False,
                "current_revised_rate_history_used": False,
            },
            "consulted_receipt_count": collapsed.report["consulted_receipt_count"],
            "duplicate_publication_count": collapsed.report[
                "duplicate_publication_count"
            ],
            "contract_policy": {
                "version": OPTION_PRICING_CONTRACT_POLICY_VERSION,
                **policy.__dict__,
                "maximum_target_receipt_to_quote_staleness_seconds": (
                    policy.maximum_source_staleness_seconds
                ),
            },
            "offline_emulation_delay_seconds": offline_emulation_delay_seconds,
            "sample_columns": list(samples.columns),
            "sample_rows": len(samples),
            "report_hash_sha256": semantic_metadata_fingerprint(report),
            "automated_action_allowed": False,
        }
        if dry_run:
            return SchwabMaterialization(
                directory=None,
                samples=samples,
                report=report,
                manifest=manifest_base,
                receipt=None,
                source_files=source_files,
                dry_run=True,
            )
        return _publish_materialization(
            root,
            samples=samples,
            report=report,
            manifest_base=manifest_base,
            source_files=source_files,
            published_at=published_at,
        )
    finally:
        if tracing_started_here:
            tracemalloc.stop()


def collapse_schwab_publications(
    snapshots_by_symbol: Mapping[str, Sequence[CommittedOptionSnapshot]],
    *,
    datastore_root: Path,
    cutoff: object,
) -> SnapshotCollapse:
    """Select the earliest valid receipt per ``(symbol, snapshot_for)``."""

    root = Path(datastore_root).resolve()
    trainer_cutoff = utc_timestamp(cutoff)
    selected: dict[tuple[str, pd.Timestamp], CommittedOptionSnapshot] = {}
    eligible_by_symbol: dict[str, tuple[CommittedOptionSnapshot, ...]] = {}
    consulted: list[dict[str, object]] = []
    duplicate_counts: Counter[str] = Counter()
    natural_target_counts: Counter[str] = Counter()
    session_counts: dict[str, int] = {}
    max_per_target = 0
    for symbol in LOOP_NATIVE_SYMBOLS:
        snapshots = tuple(snapshots_by_symbol.get(symbol, ()))
        eligible: list[CommittedOptionSnapshot] = []
        for snapshot in snapshots:
            _validate_snapshot_location(snapshot, root=root, expected_symbol=symbol)
            receipt_time = _receipt_time(snapshot)
            if receipt_time < trainer_cutoff:
                eligible.append(snapshot)
            else:
                semantics = _contract_semantics(snapshot.contracts_path)
                consulted.append(
                    {
                        "symbol": symbol,
                        "snapshot_for": snapshot.snapshot_for.isoformat(),
                        "receipt_published_at": receipt_time.isoformat(),
                        "run_path": snapshot.directory.relative_to(root).as_posix(),
                        "receipt_path": snapshot.receipt_path.relative_to(root).as_posix(),
                        "receipt_checksum_sha256": file_checksum(
                            snapshot.receipt_path
                        ),
                        "contracts_checksum_sha256": file_checksum(
                            snapshot.contracts_path
                        ),
                        "contract_rows": len(semantics),
                        "semantic_contract_hash_sha256": (
                            semantic_metadata_fingerprint(
                                {"rows": semantics.to_dict("records")}
                            )
                        ),
                        "selection": (
                            "EXCLUDED_RECEIPT_NOT_STRICTLY_BEFORE_TRAINER_CUTOFF"
                        ),
                    }
                )
        eligible.sort(
            key=lambda value: (
                value.snapshot_for,
                _receipt_time(value),
                value.directory.as_posix(),
            )
        )
        eligible_by_symbol[symbol] = tuple(eligible)
        groups: dict[pd.Timestamp, list[CommittedOptionSnapshot]] = {}
        for snapshot in eligible:
            groups.setdefault(snapshot.snapshot_for, []).append(snapshot)
        natural_target_counts[symbol] = len(groups)
        session_counts[symbol] = len(
            {
                target.tz_convert("America/New_York").date()
                for target in groups
            }
        )
        for target, group in sorted(groups.items()):
            group.sort(
                key=lambda value: (
                    _receipt_time(value),
                    value.directory.as_posix(),
                )
            )
            max_per_target = max(max_per_target, len(group))
            duplicate_counts[symbol] += max(0, len(group) - 1)
            semantic_by_contract: dict[str, tuple[object, ...]] = {}
            semantic_hashes: list[str] = []
            for index, snapshot in enumerate(group):
                semantics = _contract_semantics(snapshot.contracts_path)
                semantic_hash = semantic_metadata_fingerprint(
                    {"rows": semantics.to_dict("records")}
                )
                semantic_hashes.append(semantic_hash)
                for row in semantics.itertuples(index=False, name=None):
                    contract_symbol = str(row[1])
                    identity = tuple(row[position] for position in range(len(row)) if position != 1)
                    previous = semantic_by_contract.get(contract_symbol)
                    if previous is not None and previous != identity:
                        raise SchwabMaterializationError(
                            "Conflicting semantic contract data for duplicate Schwab "
                            f"target {symbol}/{target.isoformat()}: {contract_symbol}"
                        )
                    semantic_by_contract[contract_symbol] = identity
                consulted.append(
                    {
                        "symbol": symbol,
                        "snapshot_for": target.isoformat(),
                        "receipt_published_at": _receipt_time(snapshot).isoformat(),
                        "run_path": snapshot.directory.relative_to(root).as_posix(),
                        "receipt_path": snapshot.receipt_path.relative_to(root).as_posix(),
                        "receipt_checksum_sha256": file_checksum(snapshot.receipt_path),
                        "contracts_checksum_sha256": file_checksum(snapshot.contracts_path),
                        "contract_rows": len(semantics),
                        "semantic_contract_hash_sha256": semantic_hash,
                        "selection": (
                            "SELECTED_EARLIEST_VALID_RECEIPT"
                            if index == 0
                            else "DUPLICATE_LINEAGE_DIAGNOSTIC_ONLY"
                        ),
                    }
                )
            chosen = group[0]
            selected[(symbol, target)] = chosen
    symbol_order = {symbol: index for index, symbol in enumerate(LOOP_NATIVE_SYMBOLS)}
    consulted.sort(
        key=lambda row: (
            symbol_order[str(row["symbol"])],
            str(row["snapshot_for"]),
            str(row["receipt_published_at"]),
            str(row["run_path"]),
        )
    )
    return SnapshotCollapse(
        selected=selected,
        eligible=eligible_by_symbol,
        report={
            "natural_key": ["symbol", "snapshot_for"],
            "selection_policy": "earliest-valid-committed-receipt-before-cutoff-v1",
            "consulted_receipt_count": len(consulted),
            "selected_snapshot_count": len(selected),
            "duplicate_publication_count": int(sum(duplicate_counts.values())),
            "maximum_publications_per_natural_target": max_per_target,
            "selected_by_symbol": dict(natural_target_counts),
            "duplicates_by_symbol": dict(duplicate_counts),
            "distinct_sessions_by_symbol": session_counts,
            "consulted_receipts": consulted,
            "conflicting_semantic_contracts": 0,
        },
    )


def read_loop_native_schwab_materialization(
    directory: Path,
    *,
    datastore_root: Path,
    load_samples: bool = True,
) -> SchwabMaterialization:
    root = Path(datastore_root).resolve()
    run = Path(directory).resolve()
    allowed = (root / "ml" / "option-pricing-loop-native-materializations").resolve()
    if run.parent != allowed:
        raise SchwabMaterializationError("Materialization path escapes its immutable root")
    try:
        manifest = json.loads(
            (run / SCHWAB_MATERIALIZATION_MANIFEST_NAME).read_text(encoding="utf-8")
        )
        receipt = json.loads(
            (run / SCHWAB_MATERIALIZATION_RECEIPT_NAME).read_text(encoding="utf-8")
        )
        report = json.loads(
            (run / SCHWAB_MATERIALIZATION_REPORT_NAME).read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SchwabMaterializationError("Materialization metadata is unreadable") from exc
    if not all(isinstance(value, Mapping) for value in (manifest, receipt, report)):
        raise SchwabMaterializationError("Materialization metadata is malformed")
    expected_scope = {
        "symbols": list(LOOP_NATIVE_SYMBOLS),
        "call_puts": list(LOOP_NATIVE_CALL_PUTS),
        "routes": [
            {"symbol": symbol, "call_put": call_put}
            for symbol in LOOP_NATIVE_SYMBOLS
            for call_put in LOOP_NATIVE_CALL_PUTS
        ],
    }
    legacy_symbols = (
        "NVDA",
        "GOOG",
        "MU",
        "AAPL",
        "MSFT",
        "AMZN",
        "META",
        "TSLA",
        "CAT",
        "SNDK",
    )
    legacy_scope = {
        "symbols": list(legacy_symbols),
        "call_puts": list(LOOP_NATIVE_CALL_PUTS),
        "routes": [
            {"symbol": symbol, "call_put": call_put}
            for symbol in legacy_symbols
            for call_put in LOOP_NATIVE_CALL_PUTS
        ],
    }
    published = utc_timestamp(receipt.get("published_at"))
    trainer_cutoff = utc_timestamp(manifest.get("trainer_cutoff"))
    schema_version = manifest.get("schema_version")
    legacy = schema_version == LEGACY_SCHWAB_MATERIALIZATION_SCHEMA_VERSION
    expected_policy = (
        LEGACY_LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION
        if legacy
        else LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION
    )
    expected_receipt = (
        LEGACY_SCHWAB_MATERIALIZATION_RECEIPT_VERSION
        if legacy
        else SCHWAB_MATERIALIZATION_RECEIPT_VERSION
    )
    if (
        schema_version
        not in {
            LEGACY_SCHWAB_MATERIALIZATION_SCHEMA_VERSION,
            SCHWAB_MATERIALIZATION_SCHEMA_VERSION,
        }
        or manifest.get("policy_version") != expected_policy
        or manifest.get("scope") != (legacy_scope if legacy else expected_scope)
        or report.get("schema_version") != schema_version
        or report.get("policy_version") != expected_policy
        or receipt.get("schema_version") != expected_receipt
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or manifest.get("published_at") != published.isoformat()
        or receipt.get("trainer_cutoff") != trainer_cutoff.isoformat()
        or not trainer_cutoff < published
        or receipt.get("manifest_checksum_sha256")
        != file_checksum(run / SCHWAB_MATERIALIZATION_MANIFEST_NAME)
        or receipt.get("automated_action_allowed") is not False
        or manifest.get("automated_action_allowed") is not False
        or report.get("automated_action_allowed") is not False
        or not isinstance(manifest.get("causality_validation"), Mapping)
        or manifest["causality_validation"].get("status") != "PASS"
        or manifest["causality_validation"].get(
            "target_time_iv_allowed_as_feature"
        )
        is not False
        or manifest["causality_validation"].get(
            "target_snapshot_allowed_as_feature"
        )
        is not False
        or manifest["causality_validation"].get("underlying_rule")
        != "immutable-loop-a-readiness-strictly-before-prediction"
        or manifest["causality_validation"].get(
            "current_revised_rate_history_used"
        )
        is not False
        or manifest.get("report_hash_sha256")
        != semantic_metadata_fingerprint(report)
        or int(receipt.get("sample_rows", -1))
        != int(manifest.get("sample_rows", -2))
    ):
        raise SchwabMaterializationError("Materialization receipt verification failed")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != {
        SCHWAB_MATERIALIZATION_SAMPLE_NAME,
        SCHWAB_MATERIALIZATION_REPORT_NAME,
    }:
        raise SchwabMaterializationError("Materialization output inventory is invalid")
    for name, raw in outputs.items():
        relative = Path(str(name))
        path = (run / relative).resolve()
        metadata = raw if isinstance(raw, Mapping) else {}
        if (
            relative.is_absolute()
            or path.parent != run
            or not path.is_file()
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise SchwabMaterializationError(
                f"Materialization output checksum mismatch: {path}"
            )
    sample_path = run / SCHWAB_MATERIALIZATION_SAMPLE_NAME
    if receipt.get("sample_checksum_sha256") != file_checksum(sample_path):
        raise SchwabMaterializationError(
            "Materialization receipt sample checksum mismatch"
        )
    if load_samples:
        samples = pd.read_parquet(sample_path)
        if (
            list(samples.columns).count("id") != 1
            or samples.columns.tolist()[0] != "id"
            or len(samples) != int(manifest.get("sample_rows", -1))
        ):
            raise SchwabMaterializationError("Materialization sample identity is invalid")
    else:
        # Validate the row count from Parquet metadata so the lineage-only
        # path remains fail-closed without loading sample rows.
        import pyarrow.parquet as pq

        metadata = pq.read_metadata(sample_path)
        if metadata.num_rows != int(manifest.get("sample_rows", -1)):
            raise SchwabMaterializationError("Materialization sample row count changed")
        samples = pd.DataFrame()
    _verify_selected_receipts(manifest.get("selected_input_receipts"), root=root)
    _verify_input_file_inventory(manifest.get("input_files"), root=root)
    return SchwabMaterialization(
        directory=run,
        samples=samples.drop(columns="id") if load_samples else samples,
        report=report,
        manifest=manifest,
        receipt=receipt,
        source_files=(
            run / SCHWAB_MATERIALIZATION_MANIFEST_NAME,
            run / SCHWAB_MATERIALIZATION_RECEIPT_NAME,
            run / SCHWAB_MATERIALIZATION_SAMPLE_NAME,
            run / SCHWAB_MATERIALIZATION_REPORT_NAME,
        ),
        dry_run=False,
    )


def read_current_loop_native_schwab_materialization(
    datastore_root: Path,
    *,
    load_samples: bool = True,
) -> SchwabMaterialization:
    root = Path(datastore_root).resolve()
    pointer_path = (
        root
        / "ml"
        / "option-pricing-loop-native-materialization-latest"
        / "run.json"
    )
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SchwabMaterializationError(
            "Loop-native materialization pointer is unreadable"
        ) from exc
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version")
        not in {
            LEGACY_SCHWAB_MATERIALIZATION_POINTER_VERSION,
            SCHWAB_MATERIALIZATION_POINTER_VERSION,
        }
        or not isinstance(pointer.get("current"), Mapping)
    ):
        raise SchwabMaterializationError("Materialization pointer is malformed")
    record = pointer["current"]
    expected = {
        "run_path",
        "published_at",
        "trainer_cutoff",
        "manifest_checksum_sha256",
        "receipt_checksum_sha256",
    }
    if set(record) != expected:
        raise SchwabMaterializationError("Materialization pointer fields changed")
    relative = Path(str(record.get("run_path", "")))
    run = (root / relative).resolve()
    allowed = (root / "ml" / "option-pricing-loop-native-materializations").resolve()
    if relative.is_absolute() or run.parent != allowed:
        raise SchwabMaterializationError("Materialization pointer escapes its root")
    materialization = read_loop_native_schwab_materialization(
        run,
        datastore_root=root,
        load_samples=load_samples,
    )
    if (
        record.get("manifest_checksum_sha256")
        != file_checksum(run / SCHWAB_MATERIALIZATION_MANIFEST_NAME)
        or record.get("receipt_checksum_sha256")
        != file_checksum(run / SCHWAB_MATERIALIZATION_RECEIPT_NAME)
        or utc_timestamp(record.get("published_at"))
        != utc_timestamp(materialization.receipt.get("published_at"))
        or utc_timestamp(record.get("trainer_cutoff"))
        != utc_timestamp(materialization.manifest.get("trainer_cutoff"))
    ):
        raise SchwabMaterializationError(
            "Materialization pointer disagrees with immutable receipt"
        )
    return materialization


def _bootstrap_samples(
    root: Path,
    *,
    collapse: SnapshotCollapse,
    trainer_cutoff: pd.Timestamp,
    rate_observations: pd.DataFrame | None,
    contract_policy: ContractSelectionPolicy,
    emulation_delay_seconds: int,
) -> tuple[pd.DataFrame, dict[str, object], tuple[Path, ...]]:
    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    pair_reports: list[dict[str, object]] = []
    rejection_counts: Counter[str] = Counter()
    selected_values = collapse.selected
    for symbol in LOOP_NATIVE_SYMBOLS:
        targets = sorted(
            target for (route_symbol, target) in selected_values if route_symbol == symbol
        )
        for target in targets:
            if not _is_regular_session_target(target.isoformat()):
                rejection_counts["NON_REGULAR_SESSION_TARGET"] += 1
                pair_reports.append(
                    _pair_report(
                        symbol,
                        target,
                        None,
                        "EXCLUDED",
                        "NON_REGULAR_SESSION_TARGET",
                    )
                )
                continue
            target_snapshot = selected_values[(symbol, target)]
            emulated = target + pd.Timedelta(seconds=emulation_delay_seconds)
            request_started = _timestamp_or_none(
                target_snapshot.receipt.get("request_started_at")
            )
            if request_started is None or not emulated < request_started:
                rejection_counts["TARGET_REQUEST_NOT_AFTER_EMULATED_PREDICTION"] += 1
                pair_reports.append(
                    _pair_report(
                        symbol,
                        target,
                        None,
                        "EXCLUDED",
                        "TARGET_REQUEST_NOT_AFTER_EMULATED_PREDICTION",
                    )
                )
                continue
            candidates = [
                selected_values[(symbol, source_target)]
                for source_target in targets
                if source_target < target
                and _is_regular_session_target(source_target.isoformat())
                and _receipt_time(selected_values[(symbol, source_target)]) < emulated
            ]
            if not candidates:
                rejection_counts["SOURCE_RECEIPT_UNAVAILABLE"] += 1
                pair_reports.append(
                    _pair_report(
                        symbol,
                        target,
                        None,
                        "EXCLUDED",
                        "SOURCE_RECEIPT_UNAVAILABLE",
                    )
                )
                continue
            source_snapshot = max(
                candidates,
                key=lambda value: (value.snapshot_for, _receipt_time(value)),
            )
            try:
                (
                    underlying,
                    bar_file,
                    bar_timestamp,
                    readiness_ready_at,
                    readiness_path,
                    readiness_receipt_path,
                ) = _bootstrap_underlying(
                    root,
                    symbol=symbol,
                    target_snapshot_for=target,
                    emulated_prediction_at=emulated,
                )
                source_contracts = pd.read_parquet(source_snapshot.contracts_path)
                target_contracts = _semantic_target_alignment(
                    source_contracts,
                    pd.read_parquet(target_snapshot.contracts_path),
                )
                samples = build_causal_samples(
                    source_contracts,
                    target_contracts=target_contracts,
                    target_underlying_price=underlying,
                    source_snapshot_for=source_snapshot.snapshot_for,
                    source_available_at=_receipt_time(source_snapshot),
                    target_snapshot_for=target,
                    source_provider="schwab",
                    prediction_mode="OFFLINE",
                    observed_available_at=_receipt_time(target_snapshot),
                    prediction_created_at=target,
                    prediction_available_at=emulated,
                    provider_ingested_at=_receipt_time(target_snapshot),
                    evidence_lane=OFFLINE_SCHWAB_BOOTSTRAP,
                    fallback_used=True,
                    contract_policy=contract_policy,
                    rate_observations=rate_observations,
                    datastore_root=root,
                    allow_source_chain_carry_fallback=False,
                )
            except SchwabMaterializationError as exc:
                if any(
                    marker in str(exc).lower()
                    for marker in (
                        "path escapes",
                        "identities are not unique",
                        "snapshot clock changed",
                    )
                ):
                    raise
                reason = f"PAIR_MATERIALIZATION_FAILED:{type(exc).__name__}"
                rejection_counts[reason] += 1
                pair_reports.append(
                    _pair_report(symbol, target, source_snapshot, "EXCLUDED", reason)
                )
                continue
            except Exception as exc:
                reason = f"PAIR_MATERIALIZATION_FAILED:{type(exc).__name__}"
                rejection_counts[reason] += 1
                pair_reports.append(
                    _pair_report(symbol, target, source_snapshot, "EXCLUDED", reason)
                )
                continue
            quote = pd.to_datetime(
                samples.get("observed_quote_timestamp"), utc=True, errors="coerce"
            )
            target_available = pd.to_datetime(
                samples.get("observed_available_at"), utc=True, errors="coerce"
            )
            late = quote.gt(emulated) & target_available.gt(emulated)
            formerly_available = samples["sample_status"].eq("AVAILABLE")
            invalid = formerly_available & ~late
            samples.loc[invalid, "sample_status"] = "TARGET_TIMING_INVALID"
            samples.loc[invalid, "exclusion_reason"] = (
                "Target quote/receipt did not strictly follow the offline emulated "
                "prediction time."
            )
            for column in (
                "observed_bid",
                "observed_ask",
                "observed_mid",
                "bid_ask_spread",
                "normalized_residual",
                "dollar_residual",
            ):
                samples.loc[invalid, column] = np.nan
            observed_quote = pd.to_datetime(
                samples.get("observed_quote_timestamp"),
                utc=True,
                errors="coerce",
            )
            observed_receipt = pd.to_datetime(
                samples.get("observed_available_at"),
                utc=True,
                errors="coerce",
            )
            target_staleness = (
                observed_receipt - observed_quote
            ).dt.total_seconds()
            samples["observed_quote_staleness_seconds"] = target_staleness
            stale_target = (
                samples["sample_status"].eq("AVAILABLE")
                & (
                    target_staleness.isna()
                    | target_staleness.lt(0.0)
                    | target_staleness.gt(
                        float(contract_policy.maximum_source_staleness_seconds)
                    )
                )
            )
            samples.loc[stale_target, "sample_status"] = "TARGET_QUOTE_STALE"
            samples.loc[stale_target, "exclusion_reason"] = (
                "Target BBO exceeds the configured receipt-to-quote staleness window."
            )
            for column in (
                "observed_bid",
                "observed_ask",
                "observed_mid",
                "bid_ask_spread",
                "normalized_residual",
                "dollar_residual",
            ):
                samples.loc[stale_target, column] = np.nan
            samples["dollar_residual"] = (
                pd.to_numeric(samples.get("observed_mid"), errors="coerce")
                - pd.to_numeric(
                    samples.get("black_scholes_price"), errors="coerce"
                )
            )
            samples["evidence_lane"] = OFFLINE_SCHWAB_BOOTSTRAP
            samples["offline_emulated_prediction_at"] = emulated
            samples["prediction_created_at"] = pd.NaT
            samples["prediction_available_at"] = pd.NaT
            samples["prospective_eligible"] = False
            samples["source_receipt_path"] = source_snapshot.receipt_path.relative_to(
                root
            ).as_posix()
            samples["source_receipt_checksum_sha256"] = file_checksum(
                source_snapshot.receipt_path
            )
            samples["prediction_receipt_path"] = ""
            samples["prediction_receipt_checksum_sha256"] = ""
            samples["target_receipt_path"] = target_snapshot.receipt_path.relative_to(
                root
            ).as_posix()
            samples["target_receipt_checksum_sha256"] = file_checksum(
                target_snapshot.receipt_path
            )
            samples["underlying_bar_timestamp"] = bar_timestamp
            samples["underlying_bar_path"] = bar_file.relative_to(root).as_posix()
            samples["underlying_readiness_ready_at"] = readiness_ready_at
            samples["underlying_readiness_path"] = readiness_path.relative_to(
                root
            ).as_posix()
            samples["underlying_readiness_receipt_path"] = (
                readiness_receipt_path.relative_to(root).as_posix()
            )
            samples["rate_input_kind"] = _rate_input_kind(
                samples,
                source_contracts=source_contracts,
                source_available_at=_receipt_time(source_snapshot),
            )
            samples["carry_input_kind"] = _carry_input_kind(
                samples,
                source_contracts=source_contracts,
                source_available_at=_receipt_time(source_snapshot),
            )
            frames.append(samples)
            files.extend(
                (
                    source_snapshot.receipt_path,
                    source_snapshot.contracts_path,
                    target_snapshot.receipt_path,
                    target_snapshot.contracts_path,
                    target_snapshot.raw_path,
                    readiness_path,
                    readiness_receipt_path,
                )
            )
            counts = Counter(samples["sample_status"].astype(str))
            rejection_counts.update(
                {status: count for status, count in counts.items() if status != "AVAILABLE"}
            )
            pair_reports.append(
                {
                    **_pair_report(symbol, target, source_snapshot, "MATERIALIZED", ""),
                    "emulated_prediction_at": emulated.isoformat(),
                    "target_request_started_at": request_started.isoformat(),
                    "contract_rows": len(samples),
                    "available_rows": int(samples["sample_status"].eq("AVAILABLE").sum()),
                }
            )
    combined = (
        pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    )
    sessions = _distinct_sessions(combined)
    return (
        combined,
        {
            "evidence_lane": OFFLINE_SCHWAB_BOOTSTRAP,
            "pair_count": len(pair_reports),
            "materialized_pair_count": sum(
                value["status"] == "MATERIALIZED" for value in pair_reports
            ),
            "sample_rows": len(combined),
            "available_rows": int(
                combined.get("sample_status", pd.Series(dtype="string"))
                .astype("string")
                .eq("AVAILABLE")
                .sum()
            ),
            "distinct_sessions": sessions,
            "prospective_count_increment": 0,
            "rejection_reasons": dict(sorted(rejection_counts.items())),
            "pairs": pair_reports,
        },
        tuple(dict.fromkeys(files)),
    )


def _prospective_samples(
    root: Path,
    *,
    snapshots_by_symbol: Mapping[str, Sequence[CommittedOptionSnapshot]],
    trainer_cutoff: pd.Timestamp,
    rate_observations: pd.DataFrame | None,
    contract_policy: ContractSelectionPolicy | None = None,
) -> tuple[pd.DataFrame, dict[str, object], tuple[Path, ...]]:
    policy = contract_policy or ContractSelectionPolicy()
    try:
        predictions = receipt_proven_prediction_rows(root)
    except Exception as exc:
        has_pricing_authority = (
            root / "ml" / "option-pricing-target-latest" / "run.json"
        ).exists() or (root / "ml" / "option-pricing-latest" / "run.json").exists()
        if has_pricing_authority:
            raise SchwabMaterializationError(
                "Prospective Pricing receipt chain failed verification"
            ) from exc
        predictions = pd.DataFrame()
    if predictions.empty:
        return (
            pd.DataFrame(),
            {
                "evidence_lanes": [PROSPECTIVE_OPRA, PROSPECTIVE_SCHWAB],
                "sample_rows": 0,
                "distinct_sessions": 0,
                "receipt_cutoff": trainer_cutoff.isoformat(),
            },
            (),
        )
    (
        source_samples,
        source_sample_files,
        source_lineage_rejections,
    ) = _receipt_proven_live_source_samples(
        root,
        trainer_cutoff=trainer_cutoff,
    )
    created = pd.to_datetime(
        predictions["prediction_created_at"], utc=True, errors="coerce"
    )
    available = pd.to_datetime(
        predictions["prediction_available_at"], utc=True, errors="coerce"
    )
    predictions = predictions.loc[
        created.lt(trainer_cutoff)
        & available.lt(trainer_cutoff)
        & predictions["prediction_mode"].astype("string").str.upper().eq("LIVE")
        & predictions["source_provider"]
        .astype("string")
        .str.lower()
        .isin(("databento-opra", "schwab"))
    ].copy()
    cutoff_snapshots = {
        symbol: tuple(
            snapshot
            for snapshot in snapshots_by_symbol.get(symbol, ())
            if _receipt_time(snapshot) < trainer_cutoff
        )
        for symbol in LOOP_NATIVE_SYMBOLS
    }
    evaluations = reconcile_predictions(
        predictions,
        snapshots_by_symbol=cutoff_snapshots,
        evaluated_at=trainer_cutoff,
    )
    evaluation_status = evaluations.get(
        "evaluation_status",
        pd.Series("UNKNOWN", index=evaluations.index, dtype="string"),
    ).astype("string")
    evaluation_status_counts = Counter(evaluation_status.astype(str))
    prospective_eligible = evaluations.get(
        "prospective_eligible",
        pd.Series(False, index=evaluations.index, dtype="boolean"),
    )
    eligible_mask = prospective_eligible.fillna(False).astype(bool)
    observed_available = pd.to_datetime(
        evaluations.get("observed_available_at"), utc=True, errors="coerce"
    )
    rejection_counts: Counter[str] = Counter(source_lineage_rejections)
    for status, count in Counter(evaluation_status.loc[~eligible_mask].astype(str)).items():
        reason = (
            "TARGET_AUTHORITY_NOT_PROVEN"
            if status == "COMPLETE"
            else status
        )
        rejection_counts[reason] += count
    late_receipt = eligible_mask & ~observed_available.lt(trainer_cutoff)
    rejection_counts["TARGET_RECEIPT_NOT_BEFORE_TRAINER_CUTOFF"] += int(
        late_receipt.sum()
    )
    if rejection_counts["TARGET_RECEIPT_NOT_BEFORE_TRAINER_CUTOFF"] == 0:
        del rejection_counts["TARGET_RECEIPT_NOT_BEFORE_TRAINER_CUTOFF"]
    evaluations = evaluations.loc[
        eligible_mask & observed_available.lt(trainer_cutoff)
    ].copy()
    if evaluations.empty:
        return (
            pd.DataFrame(),
            {
                "evidence_lanes": [PROSPECTIVE_OPRA, PROSPECTIVE_SCHWAB],
                "sample_rows": 0,
                "distinct_sessions": 0,
                "receipt_cutoff": trainer_cutoff.isoformat(),
                "source_lineage_rejections": dict(source_lineage_rejections),
                "evaluation_status_counts": dict(
                    sorted(evaluation_status_counts.items())
                ),
                "rejection_reasons": dict(sorted(rejection_counts.items())),
            },
            tuple(source_sample_files),
        )
    keys = [
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "prediction_created_at",
    ]
    prediction_lookup = {
        tuple(str(row[key]) for key in keys): row
        for row in predictions.to_dict("records")
    }
    rows: list[dict[str, object]] = []
    files: list[Path] = list(source_sample_files)
    source_contract_cache: dict[Path, pd.DataFrame] = {}
    for evaluation in evaluations.to_dict("records"):
        observed_staleness = pd.to_numeric(
            pd.Series([evaluation.get("observed_quote_staleness_seconds")]),
            errors="coerce",
        ).iloc[0]
        if (
            not np.isfinite(observed_staleness)
            or observed_staleness < 0.0
            or observed_staleness > policy.maximum_source_staleness_seconds
        ):
            rejection_counts["TARGET_QUOTE_STALE"] += 1
            continue
        prediction = prediction_lookup.get(tuple(str(evaluation[key]) for key in keys))
        if prediction is None:
            rejection_counts["PREDICTION_LOOKUP_MISSING"] += 1
            continue
        source_key = (
            str(prediction["source_provider"]).strip().lower(),
            str(prediction["symbol"]).strip().upper(),
            utc_timestamp(prediction["target_snapshot_for"]),
            str(prediction["contract_symbol"]),
        )
        source_sample = source_samples.get(source_key)
        if source_sample is None:
            rejection_counts["SOURCE_SAMPLE_RECEIPT_MISSING"] += 1
            continue
        source_target = utc_timestamp(source_sample.get("source_snapshot_for"))
        source_available = utc_timestamp(source_sample.get("source_available_at"))
        source_snapshot_matches = sorted(
            (
                snapshot
                for snapshot in snapshots_by_symbol.get(source_key[1], ())
                if snapshot.provider == source_key[0]
                and snapshot.snapshot_for == source_target
                and _receipt_time(snapshot) == source_available
            ),
            key=lambda value: value.directory.as_posix(),
        )
        if not source_snapshot_matches:
            rejection_counts["SOURCE_PROVIDER_RECEIPT_MISSING"] += 1
            continue
        source_snapshot = source_snapshot_matches[0]
        source_contracts = source_contract_cache.get(source_snapshot.contracts_path)
        if source_contracts is None:
            source_contracts = pd.read_parquet(source_snapshot.contracts_path)
            for optional in ("interest_rate", "dividend_yield"):
                if optional not in source_contracts:
                    source_contracts[optional] = np.nan
            source_contract_cache[source_snapshot.contracts_path] = source_contracts
        source_contract = source_contracts.loc[
            source_contracts["contract_symbol"].astype(str).eq(source_key[3])
        ]
        if len(source_contract) != 1:
            raise SchwabMaterializationError(
                "Prospective source contract identity is not unique"
            )
        source_contract_row = source_contract.iloc[0]
        if utc_timestamp(source_contract_row.get("quote_timestamp")) != utc_timestamp(
            source_sample.get("source_quote_timestamp")
        ):
            raise SchwabMaterializationError(
                "Prospective source quote clock disagrees with provider contract receipt"
            )
        if (
            str(source_sample.get("call_put", "")).strip().upper()
            != str(prediction.get("call_put", "")).strip().upper()
            or utc_timestamp(source_sample.get("expiration_date")).normalize()
            != utc_timestamp(prediction.get("expiration_date")).normalize()
        ):
            raise SchwabMaterializationError(
                "Prospective prediction disagrees with causal contract identity"
            )
        for column in (
            "underlying_price",
            "strike",
            "multiplier",
            "risk_free_rate",
            "lagged_implied_volatility",
            "target_years_to_expiration",
            "dividend_yield",
            "black_scholes_price",
        ):
            left = float(prediction[column])
            right = float(source_sample[column])
            if not np.isfinite(left) or not np.isfinite(right) or not np.isclose(
                left, right, rtol=0.0, atol=1e-12
            ):
                raise SchwabMaterializationError(
                    "Prospective prediction disagrees with its immutable causal sample: "
                    f"{source_key}/{column}"
                )
        observed_mid = float(evaluation["observed_mid"])
        underlying = float(prediction["underlying_price"])
        target = utc_timestamp(prediction["target_snapshot_for"])
        matching = [
            snapshot
            for snapshot in cutoff_snapshots.get(str(prediction["symbol"]), ())
            if snapshot.provider
            == str(evaluation.get("outcome_provider", "")).strip().lower()
            and snapshot.snapshot_for == target
            and _receipt_time(snapshot)
            == utc_timestamp(evaluation["observed_available_at"])
        ]
        target_snapshot = matching[0] if matching else None
        if target_snapshot is None:
            raise SchwabMaterializationError(
                "Prospective evaluation lost its exact verified target receipt"
            )
        source_run_relative = Path(
            str(prediction.get("_pricing_outcome_run_path", "") or "").rstrip("/")
        )
        source_run = (root / source_run_relative).resolve()
        allowed_source_parents = {
            (root / "ml" / "option-pricing-target-outcomes").resolve(),
            (root / "ml" / "option-pricing-runs").resolve(),
        }
        expected_source_checksum = prediction.get(
            "_pricing_outcome_receipt_checksum_sha256"
        )
        matching_source_receipts = [
            candidate
            for candidate in (
                source_run / "receipt.json",
                source_run / "publication.json",
            )
            if candidate.is_file()
            and file_checksum(candidate) == expected_source_checksum
        ]
        if (
            source_run_relative.is_absolute()
            or source_run.parent not in allowed_source_parents
            or len(matching_source_receipts) != 1
        ):
            raise SchwabMaterializationError(
                "Prospective Pricing prediction receipt failed verification"
            )
        source_receipt_path = matching_source_receipts[0]
        source_receipt_relative = source_receipt_path.relative_to(root)
        files.extend(
            (
                source_receipt_path,
                source_snapshot.receipt_path,
                source_snapshot.contracts_path,
                target_snapshot.receipt_path,
                target_snapshot.contracts_path,
            )
        )
        rows.append(
            {
                "symbol": prediction["symbol"],
                "source_provider": prediction["source_provider"],
                "outcome_provider": evaluation["outcome_provider"],
                "prediction_mode": "LIVE",
                "call_put": prediction["call_put"],
                "contract_symbol": prediction["contract_symbol"],
                "expiration_date": prediction["expiration_date"],
                "target_snapshot_for": prediction["target_snapshot_for"],
                "source_snapshot_for": prediction["source_snapshot_for"],
                "source_available_at": prediction["source_available_at"],
                "source_quote_timestamp": source_sample.get(
                    "source_quote_timestamp"
                ),
                "source_quote_staleness_seconds": prediction.get(
                    "source_quote_staleness_seconds"
                ),
                "observed_quote_timestamp": evaluation["observed_quote_timestamp"],
                "observed_available_at": evaluation["observed_available_at"],
                "underlying_price": underlying,
                "strike": prediction["strike"],
                "multiplier": prediction["multiplier"],
                "risk_free_rate": prediction["risk_free_rate"],
                "rate_source_at": source_sample.get("rate_source_at"),
                "lagged_implied_volatility": prediction[
                    "lagged_implied_volatility"
                ],
                "volatility_source_at": source_sample.get(
                    "volatility_source_at"
                ),
                "target_years_to_expiration": prediction[
                    "target_years_to_expiration"
                ],
                "dividend_yield": prediction["dividend_yield"],
                "dividend_source_at": source_sample.get("dividend_source_at"),
                "source_mid": np.nan,
                "observed_bid": evaluation["observed_bid"],
                "observed_ask": evaluation["observed_ask"],
                "observed_mid": observed_mid,
                "bid_ask_spread": evaluation["bid_ask_spread"],
                "observed_quote_staleness_seconds": observed_staleness,
                "black_scholes_price": prediction["black_scholes_price"],
                "normalized_residual": (
                    observed_mid - float(prediction["black_scholes_price"])
                )
                / underlying,
                "dollar_residual": (
                    observed_mid - float(prediction["black_scholes_price"])
                ),
                "sample_status": "AVAILABLE",
                "exclusion_reason": "",
                "expiration_policy_version": OPTION_PRICING_EXPIRATION_POLICY_VERSION,
                "timing_policy_version": OPTION_PRICING_TIMING_POLICY_VERSION,
                "rate_policy_version": OPTION_PRICING_RATE_POLICY_VERSION,
                "dividend_policy_version": OPTION_PRICING_DIVIDEND_POLICY_VERSION,
                "volatility_policy_version": OPTION_PRICING_VOLATILITY_POLICY_VERSION,
                "contract_policy_version": OPTION_PRICING_CONTRACT_POLICY_VERSION,
                "schema_version": OPTION_PRICING_SCHEMA_VERSION,
                "evidence_lane": evaluation["evidence_lane"],
                "fallback_used": str(prediction["source_provider"])
                .strip()
                .lower()
                == "schwab",
                "offline_emulated_prediction_at": pd.NaT,
                "prospective_eligible": True,
                "source_receipt_path": source_snapshot.receipt_path.relative_to(
                    root
                ).as_posix(),
                "source_receipt_checksum_sha256": file_checksum(
                    source_snapshot.receipt_path
                ),
                "prediction_receipt_path": source_receipt_relative.as_posix(),
                "prediction_receipt_checksum_sha256": prediction.get(
                    "_pricing_outcome_receipt_checksum_sha256", ""
                ),
                "target_receipt_path": (
                    target_snapshot.receipt_path.relative_to(root).as_posix()
                    if target_snapshot is not None
                    else ""
                ),
                "target_receipt_checksum_sha256": (
                    file_checksum(target_snapshot.receipt_path)
                    if target_snapshot is not None
                    else ""
                ),
                "underlying_bar_timestamp": source_sample.get(
                    "_underlying_bar_timestamp"
                ),
                "underlying_bar_path": source_sample.get(
                    "_underlying_bar_path", ""
                ),
                "underlying_readiness_ready_at": source_sample.get(
                    "_underlying_readiness_ready_at"
                ),
                "underlying_readiness_path": source_sample.get(
                    "_underlying_readiness_path", ""
                ),
                "underlying_readiness_receipt_path": source_sample.get(
                    "_underlying_readiness_receipt_path", ""
                ),
                "rate_input_kind": _prospective_rate_input_kind(
                    prediction,
                    source_sample=source_sample,
                    source_contract=source_contract_row,
                    rate_observations=rate_observations,
                ),
                "carry_input_kind": _prospective_carry_input_kind(
                    prediction,
                    source_sample=source_sample,
                    source_contract=source_contract_row,
                ),
                "prediction_created_at": prediction["prediction_created_at"],
                "prediction_available_at": prediction["prediction_available_at"],
            }
        )
    frame = pd.DataFrame(rows)
    return (
        frame,
        {
            "evidence_lanes": {
                lane: int(
                    frame.get("evidence_lane", pd.Series(dtype="string"))
                    .astype("string")
                    .eq(lane)
                    .sum()
                )
                for lane in (PROSPECTIVE_OPRA, PROSPECTIVE_SCHWAB)
            },
            "sample_rows": len(frame),
            "distinct_sessions": _distinct_sessions(frame),
            "receipt_cutoff": trainer_cutoff.isoformat(),
            "source_lineage_rejections": dict(source_lineage_rejections),
            "evaluation_status_counts": dict(
                sorted(evaluation_status_counts.items())
            ),
            "rejection_reasons": dict(sorted(rejection_counts.items())),
        },
        tuple(dict.fromkeys(files)),
    )


def _receipt_proven_live_source_samples(
    root: Path,
    *,
    trainer_cutoff: pd.Timestamp,
) -> tuple[
    dict[tuple[str, str, pd.Timestamp, str], Mapping[str, object]],
    tuple[Path, ...],
    Mapping[str, int],
]:
    selected: dict[tuple[str, str, pd.Timestamp, str], Mapping[str, object]] = {}
    files: list[Path] = []
    rejection_counts: Counter[str] = Counter()

    def add_frame(frame: pd.DataFrame, *, label: str) -> None:
        if frame.empty:
            return
        available = frame.loc[
            frame.get(
                "sample_status",
                pd.Series("", index=frame.index, dtype="string"),
            )
            .astype("string")
            .eq("AVAILABLE")
            & frame.get(
                "prediction_mode",
                pd.Series("", index=frame.index, dtype="string"),
            )
            .astype("string")
            .str.upper()
            .eq("LIVE")
            & frame.get(
                "source_provider",
                pd.Series("", index=frame.index, dtype="string"),
            )
            .astype("string")
            .str.lower()
            .isin(("databento-opra", "schwab"))
        ]
        for row in available.to_dict("records"):
            key = (
                str(row.get("source_provider", "")).strip().lower(),
                str(row.get("symbol", "")).strip().upper(),
                utc_timestamp(row.get("target_snapshot_for")),
                str(row.get("contract_symbol", "")),
            )
            previous = selected.get(key)
            if previous is not None:
                columns = (
                    *SEMANTIC_FEATURE_COLUMNS,
                    "source_snapshot_for",
                    "source_available_at",
                    "source_quote_timestamp",
                    "rate_source_at",
                    "volatility_source_at",
                    "dividend_source_at",
                    "_underlying_bar_path",
                    "_underlying_bar_timestamp",
                    "_underlying_readiness_ready_at",
                    "_underlying_readiness_path",
                    "_underlying_readiness_receipt_path",
                    "black_scholes_price",
                )
                if semantic_metadata_fingerprint(
                    {name: previous.get(name) for name in columns}
                ) != semantic_metadata_fingerprint(
                    {name: row.get(name) for name in columns}
                ):
                    raise SchwabMaterializationError(
                        "Duplicate Pricing generations conflict on causal source samples: "
                        + label
                    )
                continue
            selected[key] = row

    for publication in authoritative_target_outcomes(root):
        if publication.published_at >= trainer_cutoff:
            continue
        frame = _live_available_source_rows(publication.samples())
        if frame.empty:
            continue
        # The original target-outcome contract did not preserve the complete
        # input inventory.  It remains valid legacy evidence, but cannot be
        # upgraded into a v3 causal source merely because it is reachable.
        if publication.shadow_predictions_path is None:
            rejection_counts[
                "SOURCE_TARGET_OUTCOME_GENERATION_INPUT_INVENTORY_UNAVAILABLE"
            ] += 1
            continue
        publication_inputs = _verified_manifest_input_paths(
            json.loads(publication.manifest_path.read_text(encoding="utf-8")),
            root=root,
        )
        frame = _attach_underlying_bar_proofs(
            frame,
            input_paths=publication_inputs,
            root=root,
        )
        add_frame(frame, label=publication.directory.as_posix())
        files.extend(
            (
                publication.samples_path,
                publication.predictions_path,
                publication.outcome_path,
                publication.manifest_path,
                publication.receipt_path,
                *publication_inputs,
            )
        )
    for run, published_at in authoritative_option_pricing_runs(root).items():
        if published_at >= trainer_cutoff:
            continue
        manifest_path = run / "manifest.json"
        samples_path = run / "pricing-samples.parquet"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SchwabMaterializationError(
                "Legacy Pricing sample manifest is unreadable"
            ) from exc
        outputs = manifest.get("output_files")
        metadata = outputs.get(samples_path.name) if isinstance(outputs, Mapping) else None
        if (
            not isinstance(metadata, Mapping)
            or not samples_path.is_file()
            or int(metadata.get("size", -1)) != samples_path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(samples_path)
        ):
            raise SchwabMaterializationError(
                "Legacy Pricing causal sample output failed verification"
            )
        if not isinstance(manifest.get("input_files"), list):
            rejection_counts[
                "SOURCE_PRICING_GENERATION_INPUT_INVENTORY_UNAVAILABLE"
            ] += 1
            continue
        try:
            manifest_inputs = _verified_manifest_input_paths(manifest, root=root)
        except SchwabMaterializationError as exc:
            if str(exc).startswith(
                "Pricing source manifest input failed verification:"
            ):
                rejection_counts[
                    "SOURCE_PRICING_GENERATION_INPUT_FILE_UNVERIFIED"
                ] += 1
                continue
            raise
        try:
            source_frame = _attach_underlying_bar_proofs(
                _live_available_source_rows(
                    pd.read_parquet(samples_path).drop(columns="id", errors="ignore")
                ),
                input_paths=manifest_inputs,
                root=root,
            )
        except SchwabMaterializationError as exc:
            if _is_unverifiable_legacy_underlying_bar(exc):
                rejection_counts[
                    "SOURCE_PRICING_GENERATION_UNDERLYING_BAR_UNVERIFIED"
                ] += 1
                continue
            raise
        add_frame(source_frame, label=run.as_posix())
        files.extend(
            (
                samples_path,
                run / "pricing-predictions.parquet",
                manifest_path,
                run / "publication.json",
                *manifest_inputs,
            )
        )
    return (
        selected,
        tuple(dict.fromkeys(files)),
        dict(sorted(rejection_counts.items())),
    )


def _canonical_materialized_samples(*frames: pd.DataFrame) -> pd.DataFrame:
    available = [frame for frame in frames if not frame.empty]
    if not available:
        return pd.DataFrame()
    output = pd.concat(available, ignore_index=True, sort=False)
    output["target_snapshot_for"] = pd.to_datetime(
        output["target_snapshot_for"], utc=True, errors="coerce"
    )
    output["source_available_at"] = pd.to_datetime(
        output["source_available_at"], utc=True, errors="coerce"
    )
    order = {
        PROSPECTIVE_OPRA: 0,
        PROSPECTIVE_SCHWAB: 1,
        OFFLINE_SCHWAB_BOOTSTRAP: 2,
    }
    output["_lane_order"] = output["evidence_lane"].map(order).fillna(99)
    return (
        output.sort_values(
            [
                "_lane_order",
                "symbol",
                "target_snapshot_for",
                "call_put",
                "contract_symbol",
                "source_available_at",
            ],
            kind="stable",
        )
        .drop_duplicates(
            ["evidence_lane", "symbol", "target_snapshot_for", "contract_symbol"],
            keep="first",
        )
        .drop(columns="_lane_order")
        .reset_index(drop=True)
    )


def _prepare_opra_samples(
    samples: pd.DataFrame | None,
    *,
    trainer_cutoff: pd.Timestamp,
) -> pd.DataFrame:
    if samples is None or samples.empty:
        return pd.DataFrame()
    output = samples.drop(columns="id", errors="ignore").copy()
    output["source_provider"] = "databento-opra"
    output["prediction_mode"] = "OFFLINE"
    output["evidence_lane"] = OFFLINE_OPRA_BACKFILL
    output["fallback_used"] = False
    output["prospective_eligible"] = False
    output["offline_emulated_prediction_at"] = pd.to_datetime(
        output.get("prediction_available_at"), utc=True, errors="coerce"
    )
    output["dollar_residual"] = (
        pd.to_numeric(output.get("normalized_residual"), errors="coerce")
        * pd.to_numeric(output.get("underlying_price"), errors="coerce")
    )
    outcome_quote = pd.to_datetime(
        output.get("observed_quote_timestamp"), utc=True, errors="coerce"
    )
    outcome_available = pd.to_datetime(
        output.get("observed_available_at"), utc=True, errors="coerce"
    )
    output["observed_quote_staleness_seconds"] = (
        outcome_available - outcome_quote
    ).dt.total_seconds()
    source_available = pd.to_datetime(
        output.get("source_available_at"), utc=True, errors="coerce"
    )
    output["rate_source_at"] = pd.to_datetime(
        output.get("rate_source_at"), utc=True, errors="coerce"
    ).fillna(source_available)
    output["volatility_source_at"] = pd.to_datetime(
        output.get("volatility_source_at"), utc=True, errors="coerce"
    ).fillna(source_available)
    output["dividend_source_at"] = pd.to_datetime(
        output.get("dividend_source_at"), utc=True, errors="coerce"
    ).fillna(source_available)
    output["underlying_readiness_ready_at"] = source_available
    output["underlying_readiness_path"] = "OPRA_OFFLINE_LOOP_A_BAR_PROOF"
    output["underlying_readiness_receipt_path"] = "OPRA_IMPORT_MANIFEST_PROOF"
    output["underlying_bar_timestamp"] = pd.to_datetime(
        output.get("target_snapshot_for"), utc=True, errors="coerce"
    )
    output["underlying_bar_path"] = "OPRA_OFFLINE_LOOP_A_BAR_PROOF"
    for column in (
        "source_receipt_path",
        "source_receipt_checksum_sha256",
        "prediction_receipt_path",
        "prediction_receipt_checksum_sha256",
        "target_receipt_path",
        "target_receipt_checksum_sha256",
    ):
        if column not in output:
            output[column] = "VERIFIED_IN_MATERIALIZATION_MANIFEST"
    output["rate_input_kind"] = output.get(
        "rate_source", pd.Series("FMP_OR_ALFRED", index=output.index)
    )
    output["carry_input_kind"] = output.get(
        "dividend_confidence", pd.Series("EXPLICIT_FALLBACK", index=output.index)
    )
    ingested = pd.to_datetime(
        output.get("provider_ingested_at"), utc=True, errors="coerce"
    )
    late_import = ingested.isna() | ingested.ge(trainer_cutoff)
    formerly_available = output["sample_status"].astype("string").eq("AVAILABLE")
    output.loc[formerly_available & late_import, "sample_status"] = (
        "IMPORT_NOT_AVAILABLE_BY_TRAINER"
    )
    output.loc[formerly_available & late_import, "exclusion_reason"] = (
        "Present-day historical import receipt did not predate the trainer cutoff."
    )
    return output.reset_index(drop=True)


def _validate_opra_sample_causality(
    samples: pd.DataFrame,
    *,
    trainer_cutoff: pd.Timestamp,
) -> None:
    if samples.empty:
        return
    available = samples.loc[
        samples["sample_status"].astype("string").eq("AVAILABLE")
    ].copy()
    if available.empty:
        return
    required = {
        "source_snapshot_for",
        "source_quote_timestamp",
        "source_available_at",
        "prediction_created_at",
        "prediction_available_at",
        "observed_quote_timestamp",
        "observed_available_at",
        "provider_ingested_at",
        "evidence_lane",
        "prospective_eligible",
    }
    if missing := sorted(required.difference(available.columns)):
        raise SchwabMaterializationError(
            "OPRA materialization lacks causal proof columns: " + ", ".join(missing)
        )
    for column in required.difference({"evidence_lane", "prospective_eligible"}):
        available[column] = pd.to_datetime(
            available[column], utc=True, errors="coerce"
        )
    if available[list(required.difference({"evidence_lane", "prospective_eligible"}))].isna().any(axis=None):
        raise SchwabMaterializationError("OPRA materialization has invalid causal clocks")
    valid = (
        available["source_quote_timestamp"].lt(available["prediction_created_at"])
        & available["source_available_at"].lt(available["prediction_created_at"])
        & available["prediction_created_at"].le(available["prediction_available_at"])
        & available["observed_quote_timestamp"].gt(available["prediction_available_at"])
        & available["observed_available_at"].gt(available["prediction_available_at"])
        & available["provider_ingested_at"].lt(trainer_cutoff)
        & available["observed_available_at"].lt(trainer_cutoff)
    )
    if not valid.all():
        raise SchwabMaterializationError("OPRA materialization violates causal clocks")
    if (
        not available["evidence_lane"].astype("string").eq(OFFLINE_OPRA_BACKFILL).all()
        or available["prospective_eligible"].fillna(True).astype(bool).any()
    ):
        raise SchwabMaterializationError(
            "Offline OPRA evidence was incorrectly marked prospective"
        )


def _canonical_provider_samples(
    opra: pd.DataFrame,
    schwab: pd.DataFrame,
) -> pd.DataFrame:
    frames = [frame for frame in (opra, schwab) if not frame.empty]
    if not frames:
        return pd.DataFrame()
    output = pd.concat(frames, ignore_index=True, sort=False)
    output["_provider_order"] = np.where(
        output["source_provider"].astype("string").str.lower().eq("databento-opra"),
        0,
        1,
    )
    output["_available_order"] = np.where(
        output["sample_status"].astype("string").eq("AVAILABLE"), 0, 1
    )
    natural = ["symbol", "target_snapshot_for", "contract_symbol"]
    output = output.sort_values(
        [*natural, "_available_order", "_provider_order", "source_available_at"],
        kind="stable",
    ).drop_duplicates(natural, keep="first")
    output["fallback_used"] = output["source_provider"].astype("string").str.lower().eq(
        "schwab"
    )
    return output.drop(columns=["_provider_order", "_available_order"]).reset_index(
        drop=True
    )


def _provider_disagreement_report(
    opra: pd.DataFrame,
    schwab: pd.DataFrame,
) -> Mapping[str, object]:
    if opra.empty or schwab.empty:
        return {
            "overlap_rows": 0,
            "median_absolute_midpoint_difference": None,
            "median_difference_in_opra_half_spreads": None,
        }
    keys = ["symbol", "target_snapshot_for", "contract_symbol"]
    left = opra.loc[opra["sample_status"].astype("string").eq("AVAILABLE"), [*keys, "observed_mid", "bid_ask_spread"]]
    right = schwab.loc[schwab["sample_status"].astype("string").eq("AVAILABLE"), [*keys, "observed_mid"]]
    overlap = left.merge(right, on=keys, suffixes=("_opra", "_schwab"))
    if overlap.empty:
        return {
            "overlap_rows": 0,
            "median_absolute_midpoint_difference": None,
            "median_difference_in_opra_half_spreads": None,
        }
    difference = (
        pd.to_numeric(overlap["observed_mid_opra"], errors="coerce")
        - pd.to_numeric(overlap["observed_mid_schwab"], errors="coerce")
    ).abs()
    half_spread = pd.to_numeric(
        overlap["bid_ask_spread"], errors="coerce"
    ) / 2.0
    normalized = difference / half_spread.where(half_spread.gt(0.0))
    return {
        "overlap_rows": len(overlap),
        "median_absolute_midpoint_difference": float(difference.median()),
        "median_difference_in_opra_half_spreads": (
            float(normalized.median()) if normalized.notna().any() else None
        ),
    }


def _validate_available_sample_causality(
    samples: pd.DataFrame,
    *,
    trainer_cutoff: pd.Timestamp,
    root: Path,
) -> None:
    if samples.empty:
        return
    available = samples.loc[
        samples.get("sample_status", pd.Series("", index=samples.index))
        .astype("string")
        .eq("AVAILABLE")
    ].copy()
    if available.empty:
        return
    required = {
        "evidence_lane",
        "prospective_eligible",
        "source_snapshot_for",
        "target_snapshot_for",
        "source_available_at",
        "source_quote_timestamp",
        "rate_source_at",
        "volatility_source_at",
        "dividend_source_at",
        "observed_quote_timestamp",
        "observed_available_at",
        "offline_emulated_prediction_at",
        "prediction_created_at",
        "prediction_available_at",
        "underlying_bar_timestamp",
        "underlying_bar_path",
        "underlying_readiness_ready_at",
        "underlying_readiness_path",
        "underlying_readiness_receipt_path",
        "source_receipt_path",
        "source_receipt_checksum_sha256",
        "prediction_receipt_path",
        "prediction_receipt_checksum_sha256",
        "target_receipt_path",
        "target_receipt_checksum_sha256",
        "rate_input_kind",
        "carry_input_kind",
    }
    if missing := sorted(required.difference(available.columns)):
        raise SchwabMaterializationError(
            "Available materialized samples lack causal proof columns: "
            + ", ".join(missing)
        )
    clock_columns = (
        "source_snapshot_for",
        "target_snapshot_for",
        "source_available_at",
        "source_quote_timestamp",
        "rate_source_at",
        "volatility_source_at",
        "dividend_source_at",
        "observed_quote_timestamp",
        "observed_available_at",
        "offline_emulated_prediction_at",
        "prediction_created_at",
        "prediction_available_at",
        "underlying_bar_timestamp",
        "underlying_readiness_ready_at",
    )
    for column in clock_columns:
        available[column] = pd.to_datetime(
            available[column], utc=True, errors="coerce"
        )
    offline = available["evidence_lane"].astype("string").eq(
        OFFLINE_SCHWAB_BOOTSTRAP
    )
    prospective = available["evidence_lane"].astype("string").isin(
        (PROSPECTIVE_OPRA, PROSPECTIVE_SCHWAB)
    )
    if not (offline | prospective).all():
        raise SchwabMaterializationError("Available samples contain an unknown evidence lane")
    if (
        available.loc[offline, "offline_emulated_prediction_at"].isna().any()
        or available.loc[prospective, "prediction_created_at"].isna().any()
        or available.loc[prospective, "prediction_available_at"].isna().any()
    ):
        raise SchwabMaterializationError("Available samples contain unverifiable prediction clocks")
    prediction_cutoff = available["prediction_created_at"].where(
        prospective,
        available["offline_emulated_prediction_at"],
    )
    publication_cutoff = available["prediction_available_at"].where(
        prospective,
        available["offline_emulated_prediction_at"],
    )
    causal_clocks = (
        available["source_snapshot_for"].lt(available["target_snapshot_for"])
        & available["source_quote_timestamp"].le(available["source_available_at"])
        & available["source_available_at"].lt(prediction_cutoff)
        & available["source_quote_timestamp"].lt(prediction_cutoff)
        & available["rate_source_at"].lt(prediction_cutoff)
        & available["volatility_source_at"].lt(prediction_cutoff)
        & available["dividend_source_at"].lt(prediction_cutoff)
        & available["underlying_readiness_ready_at"].lt(prediction_cutoff)
        & publication_cutoff.ge(prediction_cutoff)
        & available["observed_quote_timestamp"].gt(publication_cutoff)
        & available["observed_available_at"].gt(publication_cutoff)
        & available["observed_quote_timestamp"].le(
            available["observed_available_at"]
        )
        & available["observed_available_at"].lt(trainer_cutoff)
    )
    if causal_clocks.isna().any() or not causal_clocks.all():
        raise SchwabMaterializationError(
            "Available materialized samples violate causal input/label clocks"
        )
    if (
        available.loc[offline, "prospective_eligible"].fillna(False).astype(bool).any()
        or not available.loc[prospective, "prospective_eligible"]
        .fillna(False)
        .astype(bool)
        .all()
    ):
        raise SchwabMaterializationError(
            "Offline/prospective sample counting labels are inconsistent"
        )
    allowed_rate_kinds = {
        "SOURCE_SCHWAB_CHAIN_FIELD",
        "SOURCE_PROVIDER_COMPARISON_FALLBACK",
        "CAUSAL_FMP_TREASURY_CURVE",
        "POINT_IN_TIME_VERIFIED_RATE_RECEIPT",
    }
    allowed_carry_kinds = {
        "SOURCE_SCHWAB_CHAIN_FIELD",
        "DECLARED_FMP",
        "CAUSAL_RECURRING_ESTIMATE",
        "PUT_CALL_PARITY_FALLBACK",
        "ZERO_NO_KNOWN_DIVIDEND",
    }
    if (
        not set(available["rate_input_kind"].astype(str)).issubset(
            allowed_rate_kinds
        )
        or not set(available["carry_input_kind"].astype(str)).issubset(
            allowed_carry_kinds
        )
    ):
        raise SchwabMaterializationError(
            "Available samples contain unverified rate or carry provenance"
        )
    bar_proofs = available[
        [
            "symbol",
            "underlying_price",
            "underlying_bar_path",
            "underlying_bar_timestamp",
            "underlying_readiness_ready_at",
            "underlying_readiness_path",
            "underlying_readiness_receipt_path",
            "target_snapshot_for",
        ]
    ].drop_duplicates()
    for proof in bar_proofs.to_dict("records"):
        symbol = str(proof["symbol"]).strip().upper()
        target = utc_timestamp(proof["target_snapshot_for"])
        try:
            readiness = read_bar_readiness(
                root,
                target_snapshot_for=target,
                required_symbols=(symbol,),
            )
        except BarReadinessError as exc:
            raise SchwabMaterializationError(
                "Available sample Loop A readiness failed verification"
            ) from exc
        clock = readiness.decision_clock(symbol)
        bar_path = clock.source_file.resolve()
        readiness_relative = Path(str(proof["underlying_readiness_path"]))
        receipt_relative = Path(
            str(proof["underlying_readiness_receipt_path"])
        )
        ends = bar_end_timestamps(
            pd.Series([clock.bar_timestamp]),
            clock.timeframe,
        )
        if (
            readiness_relative.is_absolute()
            or receipt_relative.is_absolute()
            or (root / readiness_relative).resolve()
            != readiness.readiness_path.resolve()
            or (root / receipt_relative).resolve()
            != readiness.receipt_path.resolve()
            or root not in bar_path.parents
            or Path(str(proof["underlying_bar_path"])).as_posix()
            != bar_path.relative_to(root).as_posix()
            or utc_timestamp(proof["underlying_bar_timestamp"])
            != pd.Timestamp(clock.bar_timestamp)
            or len(ends) != 1
            or pd.isna(ends.iloc[0])
            or pd.Timestamp(ends.iloc[0]) != target
            or utc_timestamp(proof["underlying_readiness_ready_at"])
            != readiness.ready_at
            or not np.isclose(
                float(proof["underlying_price"]),
                readiness.close(symbol),
                rtol=0.0,
                atol=1e-12,
            )
        ):
            raise SchwabMaterializationError(
                "Available sample underlying readiness proof failed verification"
            )
    _verify_sample_receipt_columns(
        available,
        root=root,
        path_column="source_receipt_path",
        checksum_column="source_receipt_checksum_sha256",
    )
    _verify_sample_receipt_columns(
        available,
        root=root,
        path_column="target_receipt_path",
        checksum_column="target_receipt_checksum_sha256",
    )
    _verify_sample_receipt_columns(
        available.loc[prospective],
        root=root,
        path_column="prediction_receipt_path",
        checksum_column="prediction_receipt_checksum_sha256",
    )


def _verify_sample_receipt_columns(
    frame: pd.DataFrame,
    *,
    root: Path,
    path_column: str,
    checksum_column: str,
) -> None:
    for raw in frame[[path_column, checksum_column]].drop_duplicates().to_dict("records"):
        relative = Path(str(raw[path_column]))
        path = (root / relative).resolve()
        if (
            relative.is_absolute()
            or root not in path.parents
            or not path.is_file()
            or raw[checksum_column] != file_checksum(path)
        ):
            raise SchwabMaterializationError(
                f"Materialized sample receipt failed verification: {path}"
            )


def _publish_materialization(
    root: Path,
    *,
    samples: pd.DataFrame,
    report: Mapping[str, object],
    manifest_base: Mapping[str, object],
    source_files: Sequence[Path],
    published_at: object | None,
) -> SchwabMaterialization:
    timestamp = utc_timestamp(published_at)
    parent = root / "ml" / "option-pricing-loop-native-materializations"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = 2
    while destination.exists():
        destination = parent / f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        output = (
            add_readable_id(
                samples,
                key_columns=(
                    "evidence_lane",
                    "symbol",
                    "target_snapshot_for",
                    "contract_symbol",
                ),
            )
            if not samples.empty
            else pd.DataFrame({"id": pd.Series(dtype="string")})
        )
        output.to_parquet(staging / SCHWAB_MATERIALIZATION_SAMPLE_NAME, index=False)
        _write_json(staging / SCHWAB_MATERIALIZATION_REPORT_NAME, report)
        outputs = {
            name: {
                "size": (staging / name).stat().st_size,
                "checksum_sha256": file_checksum(staging / name),
            }
            for name in (
                SCHWAB_MATERIALIZATION_SAMPLE_NAME,
                SCHWAB_MATERIALIZATION_REPORT_NAME,
            )
        }
        manifest = {
            **dict(manifest_base),
            "published_at": timestamp.isoformat(),
            "outputs": outputs,
        }
        _write_json(staging / SCHWAB_MATERIALIZATION_MANIFEST_NAME, manifest)
        receipt = {
            "schema_version": SCHWAB_MATERIALIZATION_RECEIPT_VERSION,
            "run_path": destination.relative_to(root).as_posix(),
            "published_at": timestamp.isoformat(),
            "trainer_cutoff": manifest["trainer_cutoff"],
            "manifest_checksum_sha256": file_checksum(
                staging / SCHWAB_MATERIALIZATION_MANIFEST_NAME
            ),
            "sample_checksum_sha256": outputs[
                SCHWAB_MATERIALIZATION_SAMPLE_NAME
            ]["checksum_sha256"],
            "sample_rows": len(samples),
            "automated_action_allowed": False,
        }
        _write_json(staging / SCHWAB_MATERIALIZATION_RECEIPT_NAME, receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    verified = read_loop_native_schwab_materialization(
        destination,
        datastore_root=root,
    )
    pointer = {
        "schema_version": SCHWAB_MATERIALIZATION_POINTER_VERSION,
        "current": {
            "run_path": destination.relative_to(root).as_posix(),
            "published_at": timestamp.isoformat(),
            "trainer_cutoff": manifest["trainer_cutoff"],
            "manifest_checksum_sha256": file_checksum(
                destination / SCHWAB_MATERIALIZATION_MANIFEST_NAME
            ),
            "receipt_checksum_sha256": file_checksum(
                destination / SCHWAB_MATERIALIZATION_RECEIPT_NAME
            ),
        },
    }
    _write_json_atomic(
        root
        / "ml"
        / "option-pricing-loop-native-materialization-latest"
        / "run.json",
        pointer,
    )
    return SchwabMaterialization(
        directory=verified.directory,
        samples=verified.samples,
        report=verified.report,
        manifest=verified.manifest,
        receipt=verified.receipt,
        source_files=tuple(
            dict.fromkeys((*verified.source_files, *map(Path, source_files)))
        ),
        dry_run=False,
    )


def _contract_semantics(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_parquet(path, columns=list(_SEMANTIC_COLUMNS))
    except Exception as exc:
        raise SchwabMaterializationError(
            f"Schwab semantic contract data is unreadable: {path}"
        ) from exc
    if frame.empty:
        raise SchwabMaterializationError(f"Schwab contract snapshot is empty: {path}")
    output = frame.loc[:, list(_SEMANTIC_COLUMNS)].copy()
    output["symbol"] = output["symbol"].astype("string").str.strip().str.upper()
    output["contract_symbol"] = output["contract_symbol"].astype("string").str.strip()
    output["expiration_date"] = pd.to_datetime(
        output["expiration_date"], utc=True, errors="coerce"
    ).dt.normalize()
    output["call_put"] = output["call_put"].astype("string").str.strip().str.upper()
    output["strike"] = pd.to_numeric(output["strike"], errors="coerce").round(10)
    output["multiplier"] = pd.to_numeric(
        output["multiplier"], errors="coerce"
    ).round(10)
    output["mini"] = output["mini"].astype("boolean")
    output["non_standard"] = output["non_standard"].astype("boolean")
    if output.isna().any(axis=None) or output[["symbol", "contract_symbol"]].eq("").any(
        axis=None
    ):
        raise SchwabMaterializationError(
            f"Schwab semantic contract data is incomplete: {path}"
        )
    duplicate = output.loc[output["contract_symbol"].duplicated(keep=False)]
    if not duplicate.empty and duplicate.drop_duplicates().groupby(
        "contract_symbol"
    ).size().gt(1).any():
        raise SchwabMaterializationError(
            f"Schwab contract symbols have conflicting semantics: {path}"
        )
    return output.drop_duplicates().sort_values(
        ["expiration_date", "call_put", "strike", "contract_symbol"],
        kind="stable",
    ).reset_index(drop=True)


def _semantic_target_alignment(
    source: pd.DataFrame,
    target: pd.DataFrame,
) -> pd.DataFrame:
    semantic = (
        "symbol",
        "expiration_date",
        "call_put",
        "strike",
        "multiplier",
        "mini",
        "non_standard",
    )

    def keys(frame: pd.DataFrame) -> pd.Series:
        values = frame.copy()
        values["symbol"] = values["symbol"].astype("string").str.upper()
        values["expiration_date"] = pd.to_datetime(
            values["expiration_date"], utc=True, errors="coerce"
        ).dt.normalize()
        values["call_put"] = values["call_put"].astype("string").str.upper()
        values["strike"] = pd.to_numeric(values["strike"], errors="coerce").round(10)
        values["multiplier"] = pd.to_numeric(
            values["multiplier"], errors="coerce"
        ).round(10)
        return values.loc[:, list(semantic)].astype(str).agg("|".join, axis=1)

    source_keys = keys(source)
    target_keys = keys(target)
    if source_keys.duplicated().any() or target_keys.duplicated().any():
        raise SchwabMaterializationError(
            "Source/target semantic contract identities are not unique"
        )
    source_symbols = dict(zip(source_keys, source["contract_symbol"], strict=True))
    aligned = target.copy()
    aligned["_semantic_key"] = target_keys
    aligned = aligned.loc[aligned["_semantic_key"].isin(source_symbols)].copy()
    aligned["contract_symbol"] = aligned["_semantic_key"].map(source_symbols)
    return aligned.drop(columns="_semantic_key").reset_index(drop=True)


def _bootstrap_underlying(
    root: Path,
    *,
    symbol: str,
    target_snapshot_for: pd.Timestamp,
    emulated_prediction_at: pd.Timestamp,
) -> tuple[float, Path, pd.Timestamp, pd.Timestamp, Path, Path]:
    target = utc_timestamp(target_snapshot_for)
    try:
        readiness = read_bar_readiness(
            root,
            target_snapshot_for=target,
            required_symbols=(symbol,),
        )
    except BarReadinessError as exc:
        raise SchwabMaterializationError(
            "Bootstrap lacks an immutable Loop A readiness receipt"
        ) from exc
    if not readiness.ready_at < emulated_prediction_at:
        raise SchwabMaterializationError(
            "Bootstrap Loop A readiness was not available before emulated prediction"
        )
    clock = readiness.decision_clock(symbol)
    bar_file = clock.source_file.resolve()
    if root not in bar_file.parents:
        raise SchwabMaterializationError(
            "Bootstrap underlying bar path escapes datastore"
        )
    ends = bar_end_timestamps(pd.Series([clock.bar_timestamp]), clock.timeframe)
    if (
        len(ends) != 1
        or pd.isna(ends.iloc[0])
        or pd.Timestamp(ends.iloc[0]) != target
    ):
        raise SchwabMaterializationError(
            "Bootstrap underlying does not prove the exact completed target bar"
        )
    value = readiness.close(symbol)
    if not np.isfinite(value) or value <= 0.0:
        raise SchwabMaterializationError("Bootstrap underlying close is invalid")
    return (
        value,
        bar_file,
        pd.Timestamp(clock.bar_timestamp),
        readiness.ready_at,
        readiness.readiness_path.resolve(),
        readiness.receipt_path.resolve(),
    )


def _selected_receipt_inventory(
    selected: Mapping[tuple[str, pd.Timestamp], CommittedOptionSnapshot],
    *,
    root: Path,
) -> list[dict[str, object]]:
    return [
        {
            "symbol": symbol,
            "snapshot_for": target.isoformat(),
            "receipt_path": snapshot.receipt_path.relative_to(root).as_posix(),
            "receipt_checksum_sha256": file_checksum(snapshot.receipt_path),
            "manifest_path": (snapshot.directory / "manifest.json")
            .relative_to(root)
            .as_posix(),
            "manifest_checksum_sha256": file_checksum(
                snapshot.directory / "manifest.json"
            ),
            "contracts_path": snapshot.contracts_path.relative_to(root).as_posix(),
            "contracts_checksum_sha256": file_checksum(snapshot.contracts_path),
        }
        for (symbol, target), snapshot in sorted(selected.items())
    ]


def _verify_selected_receipts(raw: object, *, root: Path) -> None:
    if not isinstance(raw, list):
        raise SchwabMaterializationError("Selected receipt inventory is malformed")
    for item in raw:
        if not isinstance(item, Mapping):
            raise SchwabMaterializationError("Selected receipt inventory is malformed")
        expected = {
            "symbol",
            "snapshot_for",
            "receipt_path",
            "receipt_checksum_sha256",
            "manifest_path",
            "manifest_checksum_sha256",
            "contracts_path",
            "contracts_checksum_sha256",
        }
        if set(item) != expected:
            raise SchwabMaterializationError("Selected receipt inventory fields changed")
        relative = Path(str(item.get("receipt_path", "")))
        path = (root / relative).resolve()
        manifest_relative = Path(str(item.get("manifest_path", "")))
        manifest_path = (root / manifest_relative).resolve()
        contracts_relative = Path(str(item.get("contracts_path", "")))
        contracts_path = (root / contracts_relative).resolve()
        if (
            relative.is_absolute()
            or manifest_relative.is_absolute()
            or contracts_relative.is_absolute()
            or root not in path.parents
            or root not in manifest_path.parents
            or root not in contracts_path.parents
            or not path.is_file()
            or not manifest_path.is_file()
            or not contracts_path.is_file()
            or item.get("receipt_checksum_sha256") != file_checksum(path)
            or item.get("manifest_checksum_sha256") != file_checksum(manifest_path)
            or item.get("contracts_checksum_sha256") != file_checksum(contracts_path)
        ):
            raise SchwabMaterializationError(
                f"Selected Schwab receipt failed verification: {path}"
            )


def _input_file_inventory(
    files: Sequence[Path],
    *,
    root: Path,
) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    seen: set[Path] = set()
    for raw in files:
        path = Path(raw).resolve()
        if path in seen:
            continue
        seen.add(path)
        if root not in path.parents or not path.is_file():
            raise SchwabMaterializationError(
                f"Materialization input file escapes or is missing: {path}"
            )
        inventory.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "checksum_sha256": file_checksum(path),
            }
        )
    return sorted(inventory, key=lambda value: str(value["path"]))


def _verify_input_file_inventory(raw: object, *, root: Path) -> None:
    if not isinstance(raw, list):
        raise SchwabMaterializationError("Materialization input inventory is malformed")
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "size",
            "checksum_sha256",
        }:
            raise SchwabMaterializationError(
                "Materialization input inventory fields changed"
            )
        relative = Path(str(item.get("path", "")))
        path = (root / relative).resolve()
        normalized = relative.as_posix()
        if (
            normalized in seen
            or relative.is_absolute()
            or root not in path.parents
            or not path.is_file()
            or int(item.get("size", -1)) != path.stat().st_size
            or item.get("checksum_sha256") != file_checksum(path)
        ):
            raise SchwabMaterializationError(
                f"Materialization input failed verification: {path}"
            )
        seen.add(normalized)


def _verified_manifest_input_paths(
    manifest: Mapping[str, object],
    *,
    root: Path,
) -> tuple[Path, ...]:
    raw = manifest.get("input_files")
    if not isinstance(raw, list):
        raise SchwabMaterializationError(
            "Pricing source manifest input inventory is malformed"
        )
    verified: list[Path] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise SchwabMaterializationError(
                "Pricing source manifest input inventory is malformed"
            )
        status = item.get("status")
        if status is not None and str(status).upper() != "PRESENT":
            continue
        relative = Path(str(item.get("path", "")))
        path = (root / relative).resolve()
        if relative.is_absolute() or root not in path.parents:
            raise SchwabMaterializationError(
                f"Pricing source manifest input escapes the datastore: {path}"
            )
        if (
            not path.is_file()
            or int(item.get("size", -1)) != path.stat().st_size
            or item.get("checksum_sha256") != file_checksum(path)
        ):
            raise SchwabMaterializationError(
                f"Pricing source manifest input failed verification: {path}"
            )
        verified.append(path)
    return tuple(dict.fromkeys(verified))


def _is_unverifiable_legacy_underlying_bar(
    exc: SchwabMaterializationError,
) -> bool:
    message = str(exc)
    return message.startswith(
        "Pricing source lacks immutable Loop A readiness for "
    ) or message.startswith(
        "Pricing source does not inventory its exact Loop A readiness for "
    ) or message.startswith(
        "Loop A readiness does not prove the exact completed underlying bar"
    )


def _attach_underlying_bar_proofs(
    frame: pd.DataFrame,
    *,
    input_paths: Sequence[Path],
    root: Path,
) -> pd.DataFrame:
    output = frame.copy()
    if output.empty:
        return output
    verified_inputs = {Path(raw).resolve() for raw in input_paths}
    for path in verified_inputs:
        if root not in path.parents:
            raise SchwabMaterializationError(
                f"Underlying readiness proof escapes the datastore: {path}"
            )
    proof_paths: list[str] = []
    proof_timestamps: list[pd.Timestamp] = []
    readiness_times: list[pd.Timestamp] = []
    readiness_paths: list[str] = []
    readiness_receipt_paths: list[str] = []
    cache: dict[
        tuple[str, pd.Timestamp],
        tuple[Path, pd.Timestamp, pd.Timestamp, Path, Path, float],
    ] = {}
    for row in output.to_dict("records"):
        symbol = str(row.get("symbol", "")).strip().upper()
        target = utc_timestamp(row.get("target_snapshot_for"))
        key = (symbol, target)
        proof = cache.get(key)
        if proof is None:
            try:
                readiness = read_bar_readiness(
                    root,
                    target_snapshot_for=target,
                    required_symbols=(symbol,),
                )
            except BarReadinessError as exc:
                raise SchwabMaterializationError(
                    "Pricing source lacks immutable Loop A readiness for "
                    f"{symbol}/{target.isoformat()}"
                ) from exc
            evidence = {
                readiness.readiness_path.resolve(),
                readiness.receipt_path.resolve(),
            }
            if not evidence.issubset(verified_inputs):
                raise SchwabMaterializationError(
                    "Pricing source does not inventory its exact Loop A readiness for "
                    f"{symbol}/{target.isoformat()}"
                )
            clock = readiness.decision_clock(symbol)
            bar_path = clock.source_file.resolve()
            if root not in bar_path.parents:
                raise SchwabMaterializationError(
                    f"Underlying bar path in Loop A readiness escapes the datastore: {bar_path}"
                )
            ends = bar_end_timestamps(
                pd.Series([clock.bar_timestamp]),
                clock.timeframe,
            )
            if (
                len(ends) != 1
                or pd.isna(ends.iloc[0])
                or pd.Timestamp(ends.iloc[0]) != target
            ):
                raise SchwabMaterializationError(
                    "Loop A readiness does not prove the exact completed underlying bar"
                )
            proof = (
                bar_path,
                pd.Timestamp(clock.bar_timestamp),
                readiness.ready_at,
                readiness.readiness_path.resolve(),
                readiness.receipt_path.resolve(),
                readiness.close(symbol),
            )
            cache[key] = proof
        underlying = float(row.get("underlying_price", float("nan")))
        if not np.isfinite(underlying) or not np.isclose(
            underlying,
            proof[5],
            rtol=0.0,
            atol=1e-12,
        ):
            raise SchwabMaterializationError(
                "Pricing source underlying disagrees with immutable Loop A readiness"
            )
        proof_paths.append(proof[0].relative_to(root).as_posix())
        proof_timestamps.append(proof[1])
        readiness_times.append(proof[2])
        readiness_paths.append(proof[3].relative_to(root).as_posix())
        readiness_receipt_paths.append(proof[4].relative_to(root).as_posix())
    output["_underlying_bar_path"] = proof_paths
    output["_underlying_bar_timestamp"] = proof_timestamps
    output["_underlying_readiness_ready_at"] = readiness_times
    output["_underlying_readiness_path"] = readiness_paths
    output["_underlying_readiness_receipt_path"] = readiness_receipt_paths
    return output


def _live_available_source_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame.loc[
        frame.get(
            "sample_status",
            pd.Series("", index=frame.index, dtype="string"),
        )
        .astype("string")
        .eq("AVAILABLE")
        & frame.get(
            "prediction_mode",
            pd.Series("", index=frame.index, dtype="string"),
        )
        .astype("string")
        .str.upper()
        .eq("LIVE")
        & frame.get(
            "source_provider",
            pd.Series("", index=frame.index, dtype="string"),
        )
        .astype("string")
        .str.lower()
        .isin(("databento-opra", "schwab"))
    ].copy()


def _validate_snapshot_location(
    snapshot: CommittedOptionSnapshot,
    *,
    root: Path,
    expected_symbol: str,
) -> None:
    allowed = (
        root / "stocks" / expected_symbol / "options" / "snapshots" / "schwab"
    ).resolve()
    directory = snapshot.directory.resolve()
    if (
        snapshot.symbol != expected_symbol
        or directory.parent != allowed
        or snapshot.receipt_path.resolve().parent != directory
        or snapshot.contracts_path.resolve().parent != directory
        or str(snapshot.receipt.get("run_path", ""))
        != directory.relative_to(root).as_posix()
    ):
        raise SchwabMaterializationError(
            "Committed Schwab snapshot path or symbol is outside its authority"
        )


def _receipt_time(snapshot: CommittedOptionSnapshot) -> pd.Timestamp:
    value = snapshot.receipt_published_at or snapshot.available_at
    return utc_timestamp(value)


def _pair_report(
    symbol: str,
    target: pd.Timestamp,
    source: CommittedOptionSnapshot | None,
    status: str,
    reason: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "target_snapshot_for": target.isoformat(),
        "source_snapshot_for": source.snapshot_for.isoformat() if source else None,
        "source_receipt_published_at": (
            _receipt_time(source).isoformat() if source else None
        ),
        "status": status,
        "reason": reason,
    }


def _rate_input_kind(
    samples: pd.DataFrame,
    *,
    source_contracts: pd.DataFrame,
    source_available_at: pd.Timestamp,
) -> str:
    provider = pd.to_numeric(
        source_contracts.get("interest_rate"), errors="coerce"
    ).dropna()
    source_times = pd.to_datetime(
        samples.get("rate_source_at"), utc=True, errors="coerce"
    ).dropna()
    resolved = pd.to_numeric(samples.get("risk_free_rate"), errors="coerce")
    if resolved.notna().sum() == 0:
        return "CAUSAL_RATE_UNAVAILABLE"
    if not provider.empty and not source_times.empty and source_times.eq(
        source_available_at
    ).all():
        return "SOURCE_SCHWAB_CHAIN_FIELD"
    return (
        "POINT_IN_TIME_VERIFIED_RATE_RECEIPT"
        if not source_times.empty
        else "CAUSAL_RATE_UNAVAILABLE"
    )


def _prospective_rate_input_kind(
    prediction: Mapping[str, object],
    *,
    source_sample: Mapping[str, object],
    source_contract: pd.Series,
    rate_observations: pd.DataFrame | None,
) -> str:
    created = utc_timestamp(prediction.get("prediction_created_at"))
    source_at = _timestamp_or_none(source_sample.get("rate_source_at"))
    if source_at is None or not source_at < created:
        raise SchwabMaterializationError(
            "Prospective risk-free input lacks a causal source clock"
        )
    expected = float(prediction.get("risk_free_rate"))
    declared_source = str(source_sample.get("rate_source", "")).strip().upper()
    if declared_source == "FMP_TREASURY_CURVE":
        return "CAUSAL_FMP_TREASURY_CURVE"
    if declared_source in {"ALFRED_FEDFUNDS_FALLBACK", "FRED_FEDFUNDS_FALLBACK"}:
        return "POINT_IN_TIME_VERIFIED_RATE_RECEIPT"
    provider = pd.to_numeric(
        pd.Series([source_contract.get("interest_rate")]), errors="coerce"
    ).iloc[0]
    if pd.notna(provider) and np.isclose(
        float(provider), expected, rtol=0.0, atol=1e-12
    ):
        return (
            "SOURCE_SCHWAB_CHAIN_FIELD"
            if str(prediction.get("source_provider", "")).strip().lower() == "schwab"
            else "SOURCE_PROVIDER_COMPARISON_FALLBACK"
        )
    observations = (
        rate_observations.copy()
        if rate_observations is not None
        else pd.DataFrame()
    )
    if not observations.empty:
        available = pd.to_datetime(
            observations.get("available_at"), utc=True, errors="coerce"
        )
        values = pd.to_numeric(
            observations.get("risk_free_rate"), errors="coerce"
        )
        matched = available.eq(source_at) & np.isclose(
            values.to_numpy(dtype=float), expected, rtol=0.0, atol=1e-12
        )
        if bool(np.asarray(matched).any()):
            return "POINT_IN_TIME_VERIFIED_RATE_RECEIPT"
    raise SchwabMaterializationError(
        "Prospective risk-free input is neither source-chain nor receipt-proven"
    )


def _prospective_carry_input_kind(
    prediction: Mapping[str, object],
    *,
    source_sample: Mapping[str, object],
    source_contract: pd.Series,
) -> str:
    created = utc_timestamp(prediction.get("prediction_created_at"))
    source_at = _timestamp_or_none(source_sample.get("dividend_source_at"))
    if source_at is None or not source_at < created:
        raise SchwabMaterializationError(
            "Prospective carry input lacks a causal source clock"
        )
    expected = float(prediction.get("dividend_yield"))
    confidence = str(source_sample.get("dividend_confidence", "")).strip().upper()
    if confidence in {
        "DECLARED_FMP",
        "CAUSAL_RECURRING_ESTIMATE",
        "PUT_CALL_PARITY_FALLBACK",
        "ZERO_NO_KNOWN_DIVIDEND",
    }:
        return confidence
    provider = pd.to_numeric(
        pd.Series([source_contract.get("dividend_yield")]), errors="coerce"
    ).iloc[0]
    if pd.notna(provider) and np.isclose(
        float(provider), expected, rtol=0.0, atol=1e-12
    ):
        return "SOURCE_SCHWAB_CHAIN_FIELD"
    raise SchwabMaterializationError(
        "Prospective carry input would rely on an unverified parity fallback"
    )


def _carry_input_kind(
    samples: pd.DataFrame,
    *,
    source_contracts: pd.DataFrame,
    source_available_at: pd.Timestamp,
) -> str:
    provider = pd.to_numeric(
        source_contracts.get("dividend_yield"), errors="coerce"
    ).dropna()
    source_times = pd.to_datetime(
        samples.get("dividend_source_at"), utc=True, errors="coerce"
    ).dropna()
    if not provider.empty and not source_times.empty and source_times.eq(
        source_available_at
    ).all():
        return "SOURCE_SCHWAB_CHAIN_FIELD"
    resolved = pd.to_numeric(samples.get("dividend_yield"), errors="coerce")
    if resolved.notna().any():
        return "SOURCE_CHAIN_AMERICAN_PARITY_APPROXIMATION_NOT_FRED"
    return "CAUSAL_CARRY_UNAVAILABLE"


def _route_report(
    samples: pd.DataFrame,
    *,
    symbols: Sequence[str],
) -> dict[str, object]:
    routes: dict[str, object] = {}
    for symbol in symbols:
        for call_put in LOOP_NATIVE_CALL_PUTS:
            name = f"{symbol}/{call_put.lower()}"
            if samples.empty:
                route = samples
            else:
                route = samples.loc[
                    samples["symbol"].astype("string").str.upper().eq(symbol)
                    & samples["call_put"].astype("string").str.upper().eq(call_put)
                ]
            statuses = route.get(
                "sample_status",
                pd.Series("", index=route.index, dtype="string"),
            )
            available = route.loc[statuses.astype("string").eq("AVAILABLE")]
            routes[name] = {
                "status": "PRESENT" if not available.empty else "MISSING",
                "row_count": len(route),
                "available_row_count": len(available),
                "surface_count": int(
                    available[["symbol", "target_snapshot_for", "call_put"]]
                    .drop_duplicates()
                    .shape[0]
                )
                if not available.empty
                else 0,
                "distinct_sessions": _distinct_sessions(available),
                "offline_rows": int(
                    available.get("evidence_lane", pd.Series(dtype="string"))
                    .astype("string")
                    .eq(OFFLINE_SCHWAB_BOOTSTRAP)
                    .sum()
                ),
                "prospective_rows": int(
                    available.get("evidence_lane", pd.Series(dtype="string"))
                    .astype("string")
                    .isin((PROSPECTIVE_OPRA, PROSPECTIVE_SCHWAB))
                    .sum()
                ),
                "prospective_opra_rows": int(
                    available.get("evidence_lane", pd.Series(dtype="string"))
                    .astype("string")
                    .eq(PROSPECTIVE_OPRA)
                    .sum()
                ),
                "prospective_schwab_rows": int(
                    available.get("evidence_lane", pd.Series(dtype="string"))
                    .astype("string")
                    .eq(PROSPECTIVE_SCHWAB)
                    .sum()
                ),
                "offline_sessions": _distinct_sessions(
                    available.loc[
                        available.get(
                            "evidence_lane",
                            pd.Series("", index=available.index, dtype="string"),
                        )
                        .astype("string")
                        .eq(OFFLINE_SCHWAB_BOOTSTRAP)
                    ]
                ),
                "prospective_sessions": _distinct_sessions(
                    available.loc[
                        available.get(
                            "evidence_lane",
                            pd.Series("", index=available.index, dtype="string"),
                        )
                        .astype("string")
                        .isin((PROSPECTIVE_OPRA, PROSPECTIVE_SCHWAB))
                    ]
                ),
            }
    return routes


def _input_coverage_report(samples: pd.DataFrame) -> dict[str, object]:
    if samples.empty:
        return {
            "source_quote_clock_rows": 0,
            "target_quote_clock_rows": 0,
            "source_receipt_clock_rows": 0,
            "target_receipt_clock_rows": 0,
            "underlying_readiness_clock_rows": 0,
            "rate_input_kinds": {},
            "carry_input_kinds": {},
            "current_revised_rate_history_used_for_historical_targets": False,
        }
    return {
        "source_quote_clock_rows": int(
            pd.to_datetime(
                samples.get("source_quote_timestamp"), utc=True, errors="coerce"
            )
            .notna()
            .sum()
        ),
        "target_quote_clock_rows": int(
            pd.to_datetime(
                samples.get("observed_quote_timestamp"), utc=True, errors="coerce"
            )
            .notna()
            .sum()
        ),
        "source_receipt_clock_rows": int(
            pd.to_datetime(
                samples.get("source_available_at"), utc=True, errors="coerce"
            )
            .notna()
            .sum()
        ),
        "target_receipt_clock_rows": int(
            pd.to_datetime(
                samples.get("observed_available_at"), utc=True, errors="coerce"
            )
            .notna()
            .sum()
        ),
        "underlying_readiness_clock_rows": int(
            pd.to_datetime(
                samples.get("underlying_readiness_ready_at"),
                utc=True,
                errors="coerce",
            )
            .notna()
            .sum()
        ),
        "rate_input_kinds": dict(
            sorted(
                Counter(
                    samples.get(
                        "rate_input_kind",
                        pd.Series("UNAVAILABLE", index=samples.index),
                    ).astype(str)
                ).items()
            )
        ),
        "carry_input_kinds": dict(
            sorted(
                Counter(
                    samples.get(
                        "carry_input_kind",
                        pd.Series("UNAVAILABLE", index=samples.index),
                    ).astype(str)
                ).items()
            )
        ),
        "current_revised_rate_history_used_for_historical_targets": False,
    }


def _distinct_sessions(frame: pd.DataFrame) -> int:
    if frame.empty or "target_snapshot_for" not in frame:
        return 0
    targets = pd.to_datetime(
        frame["target_snapshot_for"], utc=True, errors="coerce"
    ).dropna()
    return int(targets.dt.tz_convert("America/New_York").dt.date.nunique())


@lru_cache(maxsize=512)
def _is_regular_session_target(value: str) -> bool:
    return is_eligible_option_target(value)


def _timestamp_or_none(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def _write_json(path: Path, payload: object) -> None:
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "OFFLINE_OPRA_BACKFILL",
    "OFFLINE_SCHWAB_BOOTSTRAP",
    "PROSPECTIVE_OPRA",
    "PROSPECTIVE_SCHWAB",
    "SCHWAB_MATERIALIZATION_SCHEMA_VERSION",
    "SchwabMaterialization",
    "SchwabMaterializationError",
    "SnapshotCollapse",
    "collapse_schwab_publications",
    "materialize_loop_native_schwab_history",
    "read_current_loop_native_schwab_materialization",
    "read_loop_native_schwab_materialization",
]
