from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.option_pricing.candidate import read_current_candidate
from ml.option_pricing.eligibility import read_eligibility_policy
from ml.option_pricing.lockbox import score_closed_lockbox_once
from ml.option_pricing.operations import EXIT_EVIDENCE, EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-time closed Option Pricing lockbox evaluator. This command never "
            "fits, calibrates, selects, tunes, activates, or submits an order."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    parser.add_argument("--authorization-record", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    current = read_current_candidate(root)
    if current is None or current.get("candidate_id") != args.candidate_id:
        parser.error("--candidate-id must name the current verified frozen candidate")
    policy = read_eligibility_policy(
        root / str(current["eligibility_policy_path"]),
        datastore_root=root,
    )
    result = score_closed_lockbox_once(
        root,
        candidate_id=args.candidate_id,
        policy_artifact=policy,
        authorization_path=args.authorization_record,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return EXIT_OK if result.get("status") == "PASS" else EXIT_EVIDENCE


if __name__ == "__main__":
    raise SystemExit(main())
