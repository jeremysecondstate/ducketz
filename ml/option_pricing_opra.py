from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import pandas as pd

from datafetching.decision_time import latest_completed_bar_clock
from datafetching.cme_runtime import load_repository_environment
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.option_pricing.opra import (
    DEFAULT_MARKET_TIMES,
    DEFAULT_SYMBOLS,
    configure_historical_client_timeouts,
    normalize_definition_records,
    read_opra_import,
    resolve_market_schedule,
    run_import_phase,
    schedule_contract_report,
)
from ml.option_pricing.eligibility import publish_eligibility_policy
from ml.option_pricing.causal import completed_bar_close
from ml.option_pricing.rates import (
    load_point_in_time_rate_observations,
    rate_coverage_report,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate or explicitly import narrow historical OPRA.PILLAR evidence. "
            "The default is cost estimation only; paid requests require --execute, "
            "--max-cost-usd, and an exact operator-approved authorization record."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument(
        "--start-date",
        default=None,
        help="First XNYS session date (default: six calendar months before end).",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Last XNYS session date (default: yesterday in America/New_York).",
    )
    parser.add_argument(
        "--market-times",
        nargs="+",
        default=list(DEFAULT_MARKET_TIMES),
        help="America/New_York HH:MM observations; early-close times are removed.",
    )
    parser.add_argument(
        "--definition-evidence",
        type=Path,
        default=None,
        help=(
            "Verified definition-phase evidence directory. When omitted, the "
            "latest matching verified definition import is used if available."
        ),
    )
    parser.add_argument(
        "--definitions-only",
        action="store_true",
        help="Estimate/import definitions even when prior definition evidence exists.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute this bounded phase after all request costs pass the ceiling.",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="Required explicit aggregate ceiling for --execute.",
    )
    parser.add_argument(
        "--authorization-record",
        type=Path,
        default=None,
        help=(
            "Approved immutable JSON record matching the exact request plan; required "
            "for --execute."
        ),
    )
    parser.add_argument(
        "--write-authorization-template",
        type=Path,
        default=None,
        help=(
            "In estimate-only mode, write the exact pending approval record here. "
            "Requires --max-cost-usd and never calls get_range."
        ),
    )
    args = parser.parse_args(argv)
    if args.execute and args.max_cost_usd is None:
        parser.error("--execute requires --max-cost-usd")
    if args.execute and args.authorization_record is None:
        parser.error("--execute requires --authorization-record")
    if args.execute and args.write_authorization_template is not None:
        parser.error("--write-authorization-template cannot be combined with --execute")
    if args.write_authorization_template is not None and args.max_cost_usd is None:
        parser.error("--write-authorization-template requires --max-cost-usd")
    if args.max_cost_usd is not None and args.max_cost_usd < 0:
        parser.error("--max-cost-usd must be non-negative")

    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    today_ny = pd.Timestamp.now(tz="America/New_York").date()
    end = pd.Timestamp(args.end_date).date() if args.end_date else today_ny - pd.Timedelta(days=1)
    normalized_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in args.symbols)
    )
    if len(normalized_symbols) != len(DEFAULT_SYMBOLS) or set(normalized_symbols) != set(
        DEFAULT_SYMBOLS
    ):
        parser.error("Eligibility OPRA scope requires exactly: NVDA GOOG MU")
    normalized_symbols = tuple(DEFAULT_SYMBOLS)
    if tuple(args.market_times) != tuple(DEFAULT_MARKET_TIMES):
        parser.error(
            "Eligibility OPRA timing requires exactly: 10:00 11:30 13:30 15:00"
        )
    if args.start_date:
        start = pd.Timestamp(args.start_date).date()
        schedule = resolve_market_schedule(
            symbols=normalized_symbols,
            start_date=start,
            end_date=end,
            market_times=args.market_times,
        )
    else:
        start = (pd.Timestamp(end) - pd.DateOffset(months=6)).date()
        for _ in range(62):
            schedule = resolve_market_schedule(
                symbols=normalized_symbols,
                start_date=start,
                end_date=end,
                market_times=args.market_times,
            )
            if schedule_contract_report(schedule).get("status") == "PASS":
                break
            start = (pd.Timestamp(start) - pd.Timedelta(days=1)).date()
        else:
            parser.error("Could not resolve the mandatory 504-cluster OPRA schedule")
    if not schedule:
        parser.error("The requested scope resolves to no eligible XNYS market times")
    schedule_contract = schedule_contract_report(schedule)
    if schedule_contract.get("status") != "PASS":
        parser.error(
            "The requested OPRA schedule does not satisfy the production eligibility "
            f"contract: {schedule_contract}"
        )

    load_repository_environment()
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        parser.error("DATABENTO_API_KEY is required for metadata cost estimation")
    import databento as db

    definitions = None
    definition_directory = None
    reference_underlyings: dict[tuple[str, str], float] = {}
    if not args.definitions_only:
        definition_directory = args.definition_evidence or _latest_definition_import(root)
        if definition_directory is not None:
            definitions = _load_definition_evidence(
                definition_directory,
                datastore_root=root,
                databento_module=db,
            )
            for point in schedule:
                try:
                    target = pd.Timestamp(point.target_snapshot_for)
                    decision = latest_completed_bar_clock(
                        root,
                        symbol=point.symbol,
                        as_of=target,
                    )
                    if pd.Timestamp(decision.decision_timestamp) == target:
                        reference_underlyings[(point.symbol, point.target_snapshot_for)] = (
                            completed_bar_close(decision)
                        )
                except Exception:
                    # The narrow CBBO planner skips observations without exact
                    # completed underlying evidence; it never widens the chain.
                    continue

    rate_observations, rate_paths = load_point_in_time_rate_observations(root)
    rate_coverage = rate_coverage_report(
        rate_observations,
        target_snapshot_fors=[point.target_snapshot_for for point in schedule],
    )
    if definitions is not None and len(reference_underlyings) != len(schedule):
        parser.error(
            "CBBO estimation requires an exact completed underlying bar for every "
            f"scheduled symbol/target ({len(reference_underlyings)}/{len(schedule)} available)"
        )
    if definitions is not None and rate_coverage.get("status") != "PASS":
        parser.error(
            "CBBO estimation requires causal rate coverage at every source boundary: "
            f"{rate_coverage}"
        )

    print("DUCKETS OPRA HISTORICAL EVIDENCE")
    print("=================================")
    print(f"DATASTORE: {root}")
    print("Dataset: OPRA.PILLAR")
    print(f"Symbols: {', '.join(dict.fromkeys(value.upper() for value in args.symbols))}")
    print(f"Sessions: {start} through {end}")
    print(f"America/New_York times: {', '.join(args.market_times)}")
    print(f"Resolved symbol-time observations: {len(schedule)}")
    print(
        "Eligibility schedule: "
        f"status={schedule_contract['status']} "
        f"clusters_per_symbol={schedule_contract['clusters_per_symbol']} "
        f"first_target={schedule_contract['first_target']} "
        f"last_target={schedule_contract['last_target']}"
    )
    print(f"Point-in-time rate input files available: {len(rate_paths)}")
    print(
        "Point-in-time rate coverage: "
        f"status={rate_coverage['status']} "
        f"targets={rate_coverage['covered_target_count']}/{rate_coverage['target_count']}"
    )
    print("Dividend input policy: lagged provider value, then causal put-call parity")
    print("Mode: PAID EXECUTION" if args.execute else "Mode: ESTIMATE ONLY (no get_range)")
    if definition_directory is not None:
        print(f"Definition evidence: {definition_directory}")
        print(
            "Completed underlying observations usable for moneyness filtering: "
            f"{len(reference_underlyings)}"
        )
    print()

    client = db.Historical(key)
    configure_historical_client_timeouts(client)
    if args.execute:
        with exclusive_runtime_lock(
            root / ".ducketz-option-pricing-opra-import.lock",
            process_name="Duckets OPRA evidence importer",
        ):
            policy_artifact = publish_eligibility_policy(root)
            result = run_import_phase(
                root,
                client=client,
                schedule=schedule,
                execute=True,
                max_cost_usd=args.max_cost_usd,
                normalized_definitions=definitions,
                reference_underlyings=reference_underlyings,
                eligibility_policy_artifact=policy_artifact,
                authorization_record=args.authorization_record,
            )
    else:
        result = run_import_phase(
            root,
            client=client,
            schedule=schedule,
            execute=False,
            max_cost_usd=args.max_cost_usd,
            normalized_definitions=definitions,
            reference_underlyings=reference_underlyings,
            authorization_template_path=args.write_authorization_template,
        )
    print()
    print(
        f"{result.status}: phase={result.phase}; requests={result.request_count}; "
        f"estimated_cost_usd={result.estimated_cost_usd:.6f}; "
        f"billable_size_bytes={result.estimated_billable_size_bytes}; "
        f"downloaded={result.downloaded_count}"
    )
    if result.evidence_directory is not None:
        print(f"Immutable evidence: {result.evidence_directory}")
    if result.phase == "definitions" and result.status in {"IMPORTED", "ALREADY_COMMITTED"}:
        print("Next step: rerun estimate-only to filter definitions and cost CBBO requests.")
    return 0


def _latest_definition_import(datastore_root: Path) -> Path | None:
    evidence = Path(datastore_root) / "ml" / "option-pricing-evidence" / "opra"
    for receipt in sorted(evidence.glob("*/receipt.json"), reverse=True):
        try:
            payload = read_opra_import(receipt.parent, datastore_root=datastore_root)
        except Exception:
            continue
        manifest = payload["manifest"]
        if manifest.get("phase") == "definitions":
            return receipt.parent
    return None


def _load_definition_evidence(
    directory: Path,
    *,
    datastore_root: Path,
    databento_module: object,
) -> pd.DataFrame:
    payload = read_opra_import(directory, datastore_root=datastore_root)
    manifest = payload["manifest"]
    if manifest.get("phase") != "definitions":
        raise RuntimeError("--definition-evidence is not a definition-phase import")
    outputs = manifest.get("outputs", {})
    frames: list[pd.DataFrame] = []
    for name in sorted(outputs):
        path = Path(directory) / str(name)
        store = databento_module.DBNStore.from_file(path)
        frames.append(
            store.to_df(
                price_type="fixed",
                pretty_ts=False,
                map_symbols=True,
            ).reset_index()
        )
    raw = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    return normalize_definition_records(raw)


if __name__ == "__main__":
    raise SystemExit(main())
