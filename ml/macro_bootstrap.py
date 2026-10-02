"""Sealed decision clocks for the first ALFRED backfill; never a model publication."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.fred_alfred_readiness import (
    FRED_ALFRED_MINIMUM_COVERAGE, FRED_ALFRED_MODEL_HORIZONS,
    FredAlfredReadinessError, _coverage_report, _relative_inventory, _verify_inventory,
)
from datafetching.loop_a_cycle import datastore_cycle_lock, require_complete_loop_a_cycle
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum, utc_timestamp
from ml.horizons import OPTION_PRICING_ACTIVE_FEATURE_PROFILE, horizon_specifications_for_profile

VERSION = "alfred-decision-bootstrap-v1"
PURPOSE = "ALFRED_REQUEST_AND_COVERAGE_ONLY"
COLUMNS = ("symbol", "horizon", "decision_timestamp")
PARENT = Path("ml/macro-decision-bootstrap")


@dataclass(frozen=True)
class BootstrapDecisions:
    receipt_path: Path
    decisions_path: Path
    decisions: pd.DataFrame
    source_files: tuple[Path, ...]
    receipt: Mapping[str, object]


def _safe_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise FredAlfredReadinessError("Bootstrap path is not a relative POSIX path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FredAlfredReadinessError("Bootstrap path traversal is forbidden")
    path = root.joinpath(*parts)
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.stat(), "st_file_attributes", 0) & 0x400):
            raise FredAlfredReadinessError("Bootstrap paths cannot use links or reparse points")
    resolved = path.resolve()
    if root not in resolved.parents:
        raise FredAlfredReadinessError("Bootstrap path escapes datastore")
    return resolved


def _specifications():
    return horizon_specifications_for_profile(OPTION_PRICING_ACTIVE_FEATURE_PROFILE, horizons=("1d", "1w"))


def _validate_decisions(frame, *, symbols, cutoff):
    if tuple(frame.columns) != COLUMNS or frame.empty:
        raise FredAlfredReadinessError("Bootstrap decisions have invalid columns or are empty")
    clean = frame.copy()
    clean["decision_timestamp"] = pd.to_datetime(clean["decision_timestamp"], utc=True, errors="raise")
    if clean.isna().any().any() or clean.duplicated(list(COLUMNS)).any():
        raise FredAlfredReadinessError("Bootstrap decision identities are missing or duplicated")
    if clean["decision_timestamp"].gt(cutoff).any():
        raise FredAlfredReadinessError("Bootstrap decisions exceed the sealed input cutoff")
    expected = {(symbol, horizon) for symbol in symbols for horizon in FRED_ALFRED_MODEL_HORIZONS}
    if set(zip(clean.symbol, clean.horizon)) != expected:
        raise FredAlfredReadinessError("Bootstrap decisions do not cover the exact universe and macro routes")
    return clean.sort_values(list(COLUMNS), kind="stable").reset_index(drop=True)


def create_bootstrap_decisions(root: Path, *, symbols: Sequence[str]) -> BootstrapDecisions:
    """Use production materialization clocks without enrichment, fitting or promotion."""
    root = Path(root).resolve()
    clean = tuple(str(symbol).strip().upper() for symbol in symbols)
    if not clean or len(set(clean)) != len(clean) or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) for s in clean):
        raise ValueError("An explicit unique stock universe is required")
    from ml.rolling_materialization import _materialize_rolling_samples
    with datastore_cycle_lock(root, reporter=None):
        cycle = require_complete_loop_a_cycle(root)
        if set(cycle.symbols) != set(clean) or "databento" not in cycle.providers:
            raise FredAlfredReadinessError("Bootstrap universe/provider differs from completed Loop A")
        cutoff = utc_timestamp(cycle.finished_at)
        specs = _specifications()
        result = _materialize_rolling_samples(
            root, symbols=clean, provider="databento", specifications=specs,
            materialized_at=cutoff, input_available_at=cutoff, reporter=None,
            _macro_decisions_only=True,
        )
        if any(route.status != "READY" or route.error for route in result.routes):
            raise FredAlfredReadinessError("Bootstrap requires every stock/macro route to materialize: " + "; ".join(f"{r.symbol}/{r.horizon}: {r.error or r.status}" for r in result.routes if r.status != "READY" or r.error))
        decisions = _validate_decisions(result.samples.loc[:, list(COLUMNS)], symbols=clean, cutoff=cutoff)
        sources = tuple(dict.fromkeys(result.source_files))
        if not sources:
            raise FredAlfredReadinessError("Bootstrap has no verified price/technical input lineage")
        inventory = _relative_inventory(root, sources)
        parent = root / PARENT
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=".building-", dir=parent))
        decisions_path = directory / "decisions.parquet"
        decisions.to_parquet(decisions_path, index=False)
        receipt = {
            "schema_version": VERSION, "purpose": PURPOSE,
            "automated_action_allowed": False, "model_publication": False,
            "symbols": list(clean), "provider": "databento",
            "feature_profile": OPTION_PRICING_ACTIVE_FEATURE_PROFILE,
            "specifications": {name: spec.as_dict() for name, spec in specs.items()},
            "input_cutoff": cutoff.isoformat(), "loop_a_generation": cycle.generation,
            "row_count": len(decisions), "decision_file": "decisions.parquet",
            "decision_size": decisions_path.stat().st_size,
            "decision_sha256": file_checksum(decisions_path), "source_files": inventory,
        }
        _verify_inventory(root, inventory)
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        destination = parent / (utc_timestamp().strftime("%Y%m%dT%H%M%S.%fZ") + "-" + str(os.getpid()))
        directory.replace(destination)
    return read_bootstrap_decisions(root, destination / "receipt.json")


def read_bootstrap_decisions(root: Path, receipt_path: Path, *, expected_checksum: str | None = None) -> BootstrapDecisions:
    root = Path(root).resolve()
    supplied = Path(receipt_path)
    supplied = supplied if supplied.is_absolute() else root / supplied
    try:
        relative = supplied.relative_to(root).as_posix()
    except ValueError:
        raise FredAlfredReadinessError("Bootstrap receipt escapes datastore") from None
    path = _safe_path(root, relative)
    if path.name != "receipt.json" or path.parent.parent != root / PARENT or path.parent.name.startswith("."):
        raise FredAlfredReadinessError("Bootstrap receipt is outside its sealed authority")
    try:
        receipt_bytes = path.read_bytes()
        if expected_checksum is not None and hashlib.sha256(receipt_bytes).hexdigest() != expected_checksum:
            raise FredAlfredReadinessError("Bootstrap receipt hash changed")
        receipt = json.loads(receipt_bytes)
        if (receipt["schema_version"] != VERSION or receipt["purpose"] != PURPOSE
            or receipt["automated_action_allowed"] is not False or receipt["model_publication"] is not False
            or receipt["provider"] != "databento" or receipt["feature_profile"] != OPTION_PRICING_ACTIVE_FEATURE_PROFILE
            or receipt["specifications"] != {name: spec.as_dict() for name, spec in _specifications().items()}
            or receipt["decision_file"] != "decisions.parquet"):
            raise FredAlfredReadinessError("Bootstrap receipt contract mismatch")
        symbols = receipt["symbols"]
        if not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols) or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) for s in symbols):
            raise FredAlfredReadinessError("Bootstrap universe is invalid")
        raw_cutoff = receipt["input_cutoff"]
        if not isinstance(raw_cutoff, str) or pd.Timestamp(raw_cutoff).tzinfo is None:
            raise FredAlfredReadinessError("Bootstrap cutoff must explicitly include UTC/offset")
        cutoff = utc_timestamp(raw_cutoff)
        if any(type(receipt[key]) is not int or receipt[key] <= 0 for key in ("row_count", "decision_size")):
            raise FredAlfredReadinessError("Bootstrap sizes/counts must be positive integers")
        if not receipt["loop_a_generation"] or pd.isna(cutoff):
            raise FredAlfredReadinessError("Bootstrap input generation or cutoff is missing")
        inventory = receipt["source_files"]
        if not isinstance(inventory, list) or not inventory:
            raise FredAlfredReadinessError("Bootstrap input inventory is empty")
        paths = [_safe_path(root, item["path"]) for item in inventory]
        if len(set(paths)) != len(paths):
            raise FredAlfredReadinessError("Bootstrap source inventory has duplicate paths")
        sources = _verify_inventory(root, inventory)
        decision_path = _safe_path(root, (path.parent / "decisions.parquet").relative_to(root).as_posix())
        raw = decision_path.read_bytes()
        if len(raw) != receipt["decision_size"] or hashlib.sha256(raw).hexdigest() != receipt["decision_sha256"]:
            raise FredAlfredReadinessError("Bootstrap decision bytes do not match the receipt")
        from io import BytesIO
        decisions = _validate_decisions(pd.read_parquet(BytesIO(raw)), symbols=symbols, cutoff=cutoff)
        if len(decisions) != receipt["row_count"]:
            raise FredAlfredReadinessError("Bootstrap row count differs from receipt")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise FredAlfredReadinessError("Bootstrap receipt or decision data is invalid") from exc
    return BootstrapDecisions(path, decision_path, decisions, sources, receipt)


def read_bootstrap_from_readiness(root: Path, report: Mapping) -> BootstrapDecisions:
    record = report.get("bootstrap_decisions")
    if not isinstance(record, Mapping):
        raise FredAlfredReadinessError("Readiness bootstrap identity is malformed")
    path = _safe_path(root, record.get("receipt_path"))
    digest = record.get("receipt_checksum_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FredAlfredReadinessError("Readiness bootstrap receipt hash is invalid")
    return read_bootstrap_decisions(root, path, expected_checksum=digest)


def validate_bootstrap_consumption(root: Path, *, evidence, samples, specifications, provider):
    """Fail closed if actual full-profile macro decisions exceed their sealed authority."""
    authority = evidence.bootstrap_authority
    if authority is None:
        return
    report_bytes = evidence.readiness.report_path.read_bytes()
    if hashlib.sha256(report_bytes).hexdigest() != authority.get("readiness_report_sha256"):
        raise FredAlfredReadinessError("Verified bootstrap readiness report changed")
    report = json.loads(report_bytes)
    if report.get("bootstrap_decisions") != {key: authority[key] for key in ("receipt_path", "receipt_checksum_sha256")}:
        raise FredAlfredReadinessError("Verified bootstrap authority changed")
    bootstrap = read_bootstrap_from_readiness(Path(root).resolve(), report)
    if provider != bootstrap.receipt["provider"]:
        raise FredAlfredReadinessError("Actual macro provider differs from bootstrap")
    selected = {h: s.as_dict() for h, s in specifications.items() if h in FRED_ALFRED_MODEL_HORIZONS}
    if selected != bootstrap.receipt["specifications"]:
        raise FredAlfredReadinessError("Actual macro horizon/profile contract differs from bootstrap")
    actual = samples.loc[samples.horizon.isin(FRED_ALFRED_MODEL_HORIZONS), list(COLUMNS)]
    actual = _validate_decisions(actual, symbols=bootstrap.receipt["symbols"], cutoff=utc_timestamp(bootstrap.receipt["input_cutoff"]))
    if not actual.equals(bootstrap.decisions):
        raise FredAlfredReadinessError("Actual macro decision scope differs from sealed bootstrap; rebuild and reverify")
    coverage = _coverage_report(actual, release_context=evidence.release_context,
                                vintages=evidence.vintages, minimum_coverage=FRED_ALFRED_MINIMUM_COVERAGE)
    if coverage["status"] != "PASS":
        raise FredAlfredReadinessError("Actual macro decisions fail ALFRED coverage or causal checks")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path)
    datastore.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc")
    parser.add_argument("--symbols", nargs="+", required=True)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=None if args.datastore else args.datastore_target)
    result = create_bootstrap_decisions(root, symbols=args.symbols)
    print(json.dumps({"status": "SEALED_DECISIONS_ONLY", "receipt": str(result.receipt_path),
                      "row_count": len(result.decisions), "automated_action_allowed": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
