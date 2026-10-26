"""Export or adopt immutable later-outcome revisions for existing Gameplan Stats."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from ml.artifacts import file_checksum
from ml.gameplan_stats_outcome_revision import adopt_revision, export_revision

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("export", "adopt"))
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--base-run", type=Path, required=True)
    parser.add_argument("--base-receipt-sha256", required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--package-sha256", help="Required exact reviewed hash for adoption")
    parser.add_argument("--reviewed-at", help="Actual timezone-aware review time; omit for current UTC")
    args=parser.parse_args(argv)
    if args.operation=="export":
        path=export_revision(args.datastore,base_run=args.base_run,
            expected_base_receipt_sha256=args.base_receipt_sha256,destination=args.package,reviewed_at=args.reviewed_at)
        print(json.dumps(dict(status="EXPORTED",path=str(path),sha256=file_checksum(path))))
    else:
        if not args.package_sha256:parser.error("adopt requires --package-sha256")
        path=adopt_revision(args.datastore,base_run=args.base_run,
            expected_base_receipt_sha256=args.base_receipt_sha256,package=args.package,
            expected_sha256=args.package_sha256,reviewed_at=args.reviewed_at)
        print(json.dumps(dict(status="ADOPTED",run=str(path),receipt_sha256=file_checksum(path/"receipt.json"))))
    return 0

if __name__=="__main__":raise SystemExit(main())
