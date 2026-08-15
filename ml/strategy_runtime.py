from __future__ import annotations

import argparse
import json
import math
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
from ml.option_pricing.strategy_shadow import STRATEGY_PRICING_MODES
from ml.strategy_publication import (
    STRATEGY_PUBLICATION_VERSION,
    publish_strategy_run,
    read_current_strategy_publication,
)
from ml.strategy_selection import STRATEGY_SELECTION_OPRA_FIRST_SPREADS_V2
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    PRICING_SCENARIO_FALLBACK_SCORE_BASIS,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
)
from ml.strategy_selection.research_trace import strategy_research_trace
from ml.strategy_selection.runtime import run_strategy_selection
from options.publication import option_snapshot_pointer_path


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
    pricing_mode: str = "active",
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
        provider="databento-opra-first/schwab-fallback",
        schema="provider-neutral-options-and-stock-bbo",
        reporter=reporter,
        extra={"source_loop_b_run": str(source.run_directory)},
    ) as timing:
        samples = pd.read_parquet(samples_path)
        predictions = pd.read_parquet(predictions_path)
        configured_symbols = _configured_symbols(configuration)
        if configured_symbols:
            sample_symbols = samples["symbol"].astype("string").str.upper()
            prediction_symbols = predictions["symbol"].astype("string").str.upper()
            samples = samples.loc[sample_symbols.isin(configured_symbols)].copy()
            predictions = predictions.loc[
                prediction_symbols.isin(configured_symbols)
            ].copy()
        evidence_symbols = configured_symbols or tuple(
            sorted(samples["symbol"].astype("string").str.upper().unique())
        )
        option_snapshot_heads = _option_snapshot_heads(
            root,
            evidence_symbols,
            available_not_after=created,
        )
        selection = run_strategy_selection(
            root,
            samples=samples,
            predictions=predictions,
            forbidden_target_starts={},
            run_timestamp=created,
            input_available_at=created,
            sample_source_files=(
                samples_path,
                predictions_path,
                source.run_directory / "publication.json",
            ),
            history_available_not_after=created,
            pricing_mode=pricing_mode,
        )
        timing.annotate(
            row_count=len(selection.candidates),
            operation="compared",
            audit_rows=len(selection.audit),
        )

    _validate_strategy_candidate_rows(selection.candidates)

    candidates = _strategy_output_frame(
        selection.candidates,
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
        "policy": STRATEGY_SELECTION_OPRA_FIRST_SPREADS_V2,
        "models_trained": selection.models_trained,
        "models_reused": selection.models_reused,
        "model_reports": dict(selection.model_reports),
        "copied_model_artifacts": copied_models,
        "research_trace": strategy_research_trace(),
        "pricing_evidence": dict(selection.pricing_report),
        "strategy_candidate_contract": _strategy_candidate_contract(),
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
                )
            )
        ),
        output_files=output_names,
        configuration={
            "policy": STRATEGY_SELECTION_OPRA_FIRST_SPREADS_V2,
            "pricing_mode": str(pricing_mode).strip().lower(),
            "pricing_evidence_contract": dict(selection.pricing_report),
            "source_loop_b": source_record,
            "source_loop_b_run": source.run_directory.relative_to(root).as_posix(),
            "source_loop_b_input_cutoff": input_cutoff.isoformat(),
            "strategy_evidence_cutoff": created.isoformat(),
            "source_loop_b_symbols": list(configured_symbols),
            "option_snapshot_heads": option_snapshot_heads,
            "option_snapshot_receipts": option_receipts,
            "stock_bbo_source_files": stock_bbo_files,
            "strategy_candidate_contract": _strategy_candidate_contract(),
            "publication_contract": {
                "version": STRATEGY_PUBLICATION_VERSION,
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


def _configured_symbols(configuration: Mapping[str, object]) -> tuple[str, ...]:
    raw = configuration.get("symbols")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in raw
            if value is not None and str(value).strip()
        )
    )


def _option_snapshot_heads(
    root: Path,
    symbols: Sequence[str],
    *,
    available_not_after: object | None = None,
) -> dict[str, str]:
    cutoff = (
        utc_timestamp(available_not_after)
        if available_not_after is not None
        else None
    )
    heads: dict[str, str] = {}
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols)
    )
    for symbol in clean_symbols:
        pointer = option_snapshot_pointer_path(root, symbol=symbol)
        if not pointer.is_file():
            heads[symbol] = "MISSING"
            continue
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            available_at = utc_timestamp(payload["available_at"])
            heads[symbol] = (
                "FUTURE"
                if cutoff is not None and available_at > cutoff
                else file_checksum(pointer)
            )
        except Exception:
            heads[symbol] = "INVALID"
    return heads


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
        default="active",
        help=(
            "Use verified option-pricing evidence before Strategy scoring; active "
            "is the production path and shadow remains diagnostic-only."
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
    print(f"Pricing evidence: {args.pricing_mode}")
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


def _current_source_already_processed(root: Path, *, pricing_mode: str = "active") -> bool:
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
    loop_b_configuration = loop_b.manifest.get("configuration")
    loop_b_configuration = (
        loop_b_configuration
        if isinstance(loop_b_configuration, Mapping)
        else {}
    )
    symbols = _configured_symbols(loop_b_configuration)
    observed_heads = (
        configuration.get("option_snapshot_heads")
        if isinstance(configuration, Mapping)
        else None
    )
    current_heads = _option_snapshot_heads(root, symbols)
    return (
        isinstance(source, Mapping)
        and isinstance(current, Mapping)
        and dict(source) == dict(current)
        and observed_mode == str(pricing_mode).strip().lower()
        and isinstance(observed_heads, Mapping)
        and dict(observed_heads) == current_heads
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


def _strategy_candidate_contract() -> dict[str, object]:
    return {
        "schema_version": STRATEGY_CANDIDATE_SCHEMA_VERSION,
        "model_policy_version": STRATEGY_MODEL_POLICY_VERSION,
        "ranking_policy_version": STRATEGY_RANKING_POLICY_VERSION,
        "decision_score": "profitable_outcome_probability",
        "fitted_score_bases": [
            BSGP_CALIBRATED_MODEL_SCORE_BASIS,
            BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
        ],
        "fallback_score_basis": PRICING_SCENARIO_FALLBACK_SCORE_BASIS,
        "pricing_evidence_before_probability": True,
    }


def _validate_strategy_candidate_rows(frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    required = {
        "symbol",
        "horizon",
        "decision_timestamp",
        "candidate_key",
        "decision_score",
        "score_basis",
        "candidate_rank",
        "raw_profit_probability",
        "calibrated_profit_probability",
        "expected_net_profit",
        "expected_return_on_risk",
        "model_status",
        "model_policy_version",
        "ranking_policy_version",
        "schema_version",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(
            "Strategy candidates are missing publication fields: "
            + ", ".join(missing)
        )
    if not frame["schema_version"].eq(STRATEGY_CANDIDATE_SCHEMA_VERSION).all():
        raise ValueError("Strategy candidate schema version is incompatible")
    if not frame["model_policy_version"].eq(STRATEGY_MODEL_POLICY_VERSION).all():
        raise ValueError("Strategy candidate model policy version is incompatible")
    if not frame["ranking_policy_version"].eq(STRATEGY_RANKING_POLICY_VERSION).all():
        raise ValueError("Strategy candidate ranking policy version is incompatible")

    score = pd.to_numeric(frame["decision_score"], errors="coerce")
    raw = pd.to_numeric(frame["raw_profit_probability"], errors="coerce")
    calibrated = pd.to_numeric(
        frame["calibrated_profit_probability"], errors="coerce"
    )
    for values, label in (
        (score, "decision score"),
        (raw, "raw profit probability"),
    ):
        if not values.map(math.isfinite).all() or not values.between(0.0, 1.0).all():
            raise ValueError(f"Strategy candidate {label} must be finite in [0, 1]")
    for column in ("expected_net_profit", "expected_return_on_risk"):
        values = pd.to_numeric(frame[column], errors="coerce")
        if not values.map(math.isfinite).all():
            raise ValueError(f"Strategy candidate {column} must be finite")

    bsgp_fitted = frame["score_basis"].eq(
        BSGP_CALIBRATED_MODEL_SCORE_BASIS
    )
    black_scholes_fitted = frame["score_basis"].eq(
        BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS
    )
    fitted = bsgp_fitted | black_scholes_fitted
    prior = frame["score_basis"].eq(
        PRICING_SCENARIO_FALLBACK_SCORE_BASIS
    )
    if not (fitted | prior).all():
        raise ValueError("Strategy candidate score basis is invalid")
    if (
        not frame.loc[fitted, "model_status"].eq("MODEL_FIT").all()
        or not calibrated.loc[fitted].map(math.isfinite).all()
        or not calibrated.loc[fitted].between(0.0, 1.0).all()
        or not score.loc[fitted].sub(calibrated.loc[fitted]).abs().le(1e-12).all()
    ):
        raise ValueError("Fitted Strategy candidate score contract is invalid")
    pricing_source = frame.get(
        "pricing_source", pd.Series("", index=frame.index)
    ).astype("string").str.upper()
    pricing_status = frame.get(
        "pricing_status", pd.Series("", index=frame.index)
    ).astype("string")
    if not pricing_status.isin(
        ("Active", "Black-Scholes fallback", "Delayed", "Unavailable")
    ).all():
        raise ValueError("Strategy candidate pricing status is not user-facing")
    if (
        not pricing_source.loc[bsgp_fitted].eq("BSGP").all()
        or not pricing_source.loc[black_scholes_fitted]
        .eq("BLACK_SCHOLES")
        .all()
    ):
        raise ValueError(
            "Calibrated Strategy score basis does not match pricing source"
        )
    if (
        not frame.loc[prior, "model_status"].eq("PRICING_SCENARIO").all()
        or not calibrated.loc[prior].isna().all()
        or not score.loc[prior].sub(raw.loc[prior]).abs().le(1e-12).all()
    ):
        raise ValueError("Scenario-prior Strategy candidate score contract is invalid")

    ranks = pd.to_numeric(frame["candidate_rank"], errors="coerce")
    if ranks.isna().any() or not ranks.ge(1).all() or not ranks.mod(1).eq(0).all():
        raise ValueError("Strategy candidate ranks must be positive integers")
    keys = frame["candidate_key"].astype("string")
    if keys.isna().any() or keys.str.strip().eq("").any():
        raise ValueError("Strategy candidate keys must be nonblank")
    symbols = frame["symbol"].astype("string").str.strip().str.upper()
    horizons = frame["horizon"].astype("string").str.strip().str.lower()
    decisions = pd.to_datetime(
        frame["decision_timestamp"], utc=True, errors="coerce"
    )
    if (
        symbols.isna().any()
        or symbols.eq("").any()
        or horizons.isna().any()
        or horizons.eq("").any()
        or decisions.isna().any()
    ):
        raise ValueError("Strategy candidate route keys must be complete and valid")
    validation = frame.assign(
        __symbol=symbols,
        __horizon=horizons,
        __decision_timestamp=decisions,
        __rank=ranks.astype(int),
        __candidate_key=keys,
    )
    route_decisions = validation.groupby(
        ["__symbol", "__horizon"], dropna=False, sort=False
    )["__decision_timestamp"].nunique(dropna=False)
    if not route_decisions.eq(1).all():
        raise ValueError("Strategy publication requires one decision per route")
    group_columns = ["__symbol", "__horizon", "__decision_timestamp"]
    for _route, group in validation.groupby(group_columns, dropna=False, sort=False):
        observed = sorted(group["__rank"].tolist())
        if observed != list(range(1, len(group) + 1)):
            raise ValueError("Strategy candidate ranks must be complete from 1 through N")
        if group["__candidate_key"].duplicated().any():
            raise ValueError("Strategy candidate keys must be unique per decision")


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
