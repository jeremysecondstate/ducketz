from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import pandas as pd

from ml.option_pricing.publication import (
    OPTION_PRICING_REQUIRED_OUTPUTS,
    read_current_option_pricing_publication,
    receipt_proven_prediction_rows,
)
from ml.parquet_contracts import (
    frame_with_readable_id,
    verify_parquet_schema,
    write_parquet_with_schema,
)


_NATURAL_KEYS = {
    "pricing-samples.parquet": (
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
    ),
    "pricing-predictions.parquet": (
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "prediction_created_at",
    ),
    "pricing-evaluations.parquet": (
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "prediction_created_at",
    ),
    "pricing-surfaces.parquet": (
        "symbol",
        "target_snapshot_for",
        "call_put",
        "expiration_bucket",
        "moneyness_bucket",
    ),
    "pricing-monitoring.parquet": (
        "metric_name",
        "scope_type",
        "scope_value",
        "monitored_at",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read the current Pricing authority without changing it, then regenerate "
            "and verify its Parquet contracts in a temporary datastore."
        )
    )
    parser.add_argument("--source-datastore", type=Path, required=True)
    args = parser.parse_args()
    root = args.source_datastore.resolve()

    started = time.perf_counter()
    publication = read_current_option_pricing_publication(root)
    authority_verify_seconds = time.perf_counter() - started

    started = time.perf_counter()
    proven = receipt_proven_prediction_rows(root)
    receipt_proof_seconds = time.perf_counter() - started

    rows: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="ducketz-pricing-inventory-") as temporary:
        destination = Path(temporary)
        started = time.perf_counter()
        for name, schema in OPTION_PRICING_REQUIRED_OUTPUTS.items():
            frame = pd.read_parquet(publication.run_directory / name)
            rows[name] = len(frame)
            identified = frame_with_readable_id(
                frame.drop(columns="id", errors="ignore"),
                key_columns=_NATURAL_KEYS[name],
            )
            output = destination / name
            write_parquet_with_schema(identified, output, schema)
            verify_parquet_schema(output, schema)
        regenerate_and_verify_seconds = time.perf_counter() - started

    print(
        json.dumps(
            {
                "source_run": publication.run_directory.name,
                "rows": rows,
                "receipt_proven_prediction_rows": len(proven),
                "authority_verify_seconds": authority_verify_seconds,
                "receipt_proof_seconds": receipt_proof_seconds,
                "regenerate_and_verify_seconds": regenerate_and_verify_seconds,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
