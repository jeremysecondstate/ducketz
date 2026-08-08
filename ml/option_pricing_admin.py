from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.option_pricing.candidate import freeze_candidate, read_current_candidate
from ml.option_pricing.eligibility import (
    read_current_eligibility_report,
    read_eligibility_policy,
)
from ml.option_pricing.lockbox import read_lockbox_result
from ml.option_pricing.operations import (
    EXIT_EVIDENCE,
    EXIT_OK,
    operational_preflight_report,
    publish_operational_readiness,
    read_current_operational_readiness,
    rollback_option_pricing_pointer,
)
from ml.option_pricing.publication import read_current_option_pricing_publication
from ml.option_pricing.strategy_outcomes import (
    build_strategy_outcome_evidence,
    publish_strategy_outcome_evidence,
    read_current_strategy_outcome_evidence,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit and publish shadow-only Option Pricing evidence artifacts."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Verify and print current evidence status")
    commands.add_parser(
        "strategy-evaluate",
        help="Publish a causal Strategy shadow outcome comparison",
    )
    commands.add_parser(
        "operational-preflight",
        help="Publish installation, CLI, capacity, chain, and benchmark checks",
    )
    commands.add_parser(
        "freeze-candidate",
        help="Freeze the current run only after all offline gates pass",
    )
    rollback = commands.add_parser(
        "rollback-pointer",
        help="Restore the immediately prior verified Pricing pointer",
    )
    rollback.add_argument("--authorization-record", type=Path, required=True)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    if args.command == "status":
        return _status(root)
    if args.command == "strategy-evaluate":
        observations, report, sources = build_strategy_outcome_evidence(root)
        published = publish_strategy_outcome_evidence(
            root,
            observations=observations,
            report=report,
            source_files=sources,
        )
        _print_json(published)
        return EXIT_OK if published.get("status") == "PASS" else EXIT_EVIDENCE
    if args.command == "operational-preflight":
        report = operational_preflight_report(root)
        published = publish_operational_readiness(root, report=report)
        _print_json(published)
        return EXIT_OK if published.get("status") == "PASS" else EXIT_EVIDENCE
    if args.command == "freeze-candidate":
        publication = read_current_option_pricing_publication(root)
        eligibility = read_current_eligibility_report(root)
        policy_reference = eligibility.report.get("eligibility_policy")
        policy_reference = (
            policy_reference if isinstance(policy_reference, Mapping) else {}
        )
        policy = read_eligibility_policy(
            Path(str(policy_reference.get("path", ""))),
            datastore_root=root,
        )
        candidate = freeze_candidate(
            root,
            pricing_run=publication.run_directory,
            policy_artifact=policy,
            eligibility_report=eligibility.report,
        )
        _print_json(candidate)
        return EXIT_OK
    if args.command == "rollback-pointer":
        receipt = rollback_option_pricing_pointer(
            root,
            authorization_path=args.authorization_record,
        )
        _print_json(receipt)
        return EXIT_OK
    parser.error("unsupported command")
    return 2


def _status(root: Path) -> int:
    status: dict[str, object] = {
        "datastore": str(root),
        "automated_action_allowed": False,
    }
    failures = 0
    for name, reader in (
        ("pricing", lambda: _pricing_status(root)),
        ("eligibility", lambda: read_current_eligibility_report(root).report),
        ("candidate", lambda: read_current_candidate(root)),
        ("strategy", lambda: read_current_strategy_outcome_evidence(root)),
        ("operational", lambda: read_current_operational_readiness(root)),
    ):
        try:
            value = reader()
            if value is None:
                status[name] = {
                    "status": "NOT_PROVEN",
                    "reason": f"No current {name} artifact",
                }
                failures += 1
            else:
                status[name] = value
        except Exception as exc:
            status[name] = {"status": "INVALID", "reason": f"{type(exc).__name__}: {exc}"}
            failures += 1
    candidate = status.get("candidate")
    candidate_id = (
        str(candidate.get("candidate_id", "")).strip()
        if isinstance(candidate, Mapping)
        else ""
    )
    if candidate_id:
        try:
            status["lockbox"] = read_lockbox_result(
                root, candidate_id=candidate_id
            )
        except Exception as exc:
            status["lockbox"] = {
                "status": "NOT_PROVEN",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            failures += 1
    else:
        status["lockbox"] = {
            "status": "NOT_PROVEN",
            "reason": "No verified frozen candidate exists",
        }
        failures += 1
    _print_json(status)
    return EXIT_EVIDENCE if failures else EXIT_OK


def _pricing_status(root: Path) -> Mapping[str, object]:
    publication = read_current_option_pricing_publication(root)
    return {
        "status": "VERIFIED",
        "run_directory": str(publication.run_directory),
        "run_timestamp": publication.manifest.get("run_timestamp"),
        "published_at": publication.receipt.get("published_at"),
        "publication_id": publication.receipt.get("publication_id"),
        "automated_action_allowed": False,
    }


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
