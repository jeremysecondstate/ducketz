from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from datafetching.databento_opra_history import (
    STANDARD_SCHEMAS,
    SyncScope,
    storage_preflight,
    symbol_bucket,
)
from ml.option_pricing.opra import (
    OPRA_PRICE_SCALE,
    normalize_cbbo_records,
    normalize_definition_records,
    point_in_time_definition_asof,
    select_historical_source_target,
)
from ml.artifacts import file_checksum
from ml.option_pricing.strategy_shadow import load_strategy_pricing_evidence


class _Metadata:
    def get_cost(self, **_kwargs: object) -> float:
        return 0.0

    def get_billable_size(self, **_kwargs: object) -> int:
        return 1_024

    def get_record_count(self, **_kwargs: object) -> int:
        return 8


def _entitlement() -> dict[str, object]:
    return {
        "entitlements": {
            schema: {
                "level": "L0" if schema.startswith("ohlcv") else "L1",
                "dataset_start": "2025-01-01",
                "entitled_start": "2025-01-01",
                "entitled_end": "2025-01-03",
            }
            for schema in STANDARD_SCHEMAS
        }
    }


def test_storage_preflight_preserves_requested_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "datafetching.databento_opra_history.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=10 * 1024**3),
    )
    result = storage_preflight(
        SimpleNamespace(metadata=_Metadata()),
        datastore_root=tmp_path,
        entitlement=_entitlement(),
        scope=SyncScope(schemas=("ohlcv-1d",), symbols=("AAPL.OPT",)),
    )
    assert result["capacity_pass"] is True
    assert result["estimates"]["ohlcv-1d"]["symbols"] == ["AAPL.OPT"]
    assert result["estimates"]["ohlcv-1d"]["cost_usd"] == 0.0


def test_symbol_bucket_is_stable_and_full_universe_is_explicit() -> None:
    assert symbol_bucket(()) == "full-universe"
    assert symbol_bucket(("NVDA.OPT", "AAPL.OPT")) == symbol_bucket(
        ("AAPL.OPT", "NVDA.OPT")
    )


def test_definition_asof_never_uses_future_definition() -> None:
    raw = pd.DataFrame(
        {
            "symbol": ["AAPL  250117C00100000", "AAPL  250117C00100000"],
            "ts_recv": [
                pd.Timestamp("2025-01-02T14:00:00Z"),
                pd.Timestamp("2025-01-02T16:00:00Z"),
            ],
            "expiration": [pd.Timestamp("2025-01-17T00:00:00Z")] * 2,
            "instrument_class": ["C", "C"],
            "strike_price": [100 * OPRA_PRICE_SCALE, 101 * OPRA_PRICE_SCALE],
            "contract_multiplier": [100, 100],
        }
    )
    normalized = normalize_definition_records(raw)
    selected = point_in_time_definition_asof(
        normalized, pd.Timestamp("2025-01-02T15:00:00Z")
    )
    assert selected.iloc[0]["strike"] == 100.0


def test_quote_outcome_is_strictly_after_prediction_availability() -> None:
    raw = pd.DataFrame(
        {
            "symbol": ["AAPL  250117C00100000"] * 4,
            "ts_recv": pd.to_datetime(
                [
                    "2025-01-02T14:59:00Z",
                    "2025-01-02T15:00:00Z",
                    "2025-01-02T15:01:00Z",
                    "2025-01-02T15:02:00Z",
                ],
                utc=True,
            ),
            "bid_px_00": [1.0, 1.1, 1.2, 1.3],
            "ask_px_00": [1.2, 1.3, 1.4, 1.5],
        }
    )
    cbbo = normalize_cbbo_records(raw)
    source, outcome = select_historical_source_target(
        cbbo,
        target_snapshot_for="2025-01-02T15:00:00Z",
        prediction_available_at="2025-01-02T15:01:00Z",
    )
    assert source["quote_timestamp"].max() < pd.Timestamp("2025-01-02T15:00:00Z")
    assert outcome["quote_timestamp"].min() > pd.Timestamp("2025-01-02T15:01:00Z")


def test_strategy_catalog_reads_receipt_verified_canonical_opra_replay(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    run = root / "ml" / "option-pricing-opra-replay-runs" / "run-1"
    run.mkdir(parents=True)
    input_path = root / "market-data" / "source.json"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("{}\n", encoding="utf-8")
    prediction_path = run / "pricing-predictions.parquet"
    pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "source_provider": ["databento-opra"],
            "call_put": ["CALL"],
            "contract_symbol": ["AAPL  250117C00100000"],
            "expiration_date": [pd.Timestamp("2025-01-17T00:00:00Z")],
            "target_snapshot_for": [pd.Timestamp("2025-01-02T15:00:00Z")],
            "source_snapshot_for": [pd.Timestamp("2025-01-02T14:59:00Z")],
            "source_available_at": [pd.Timestamp("2025-01-02T14:59:00Z")],
            "prediction_created_at": [pd.Timestamp("2025-01-02T15:00:00Z")],
            "prediction_available_at": [pd.Timestamp("2025-01-02T15:01:00Z")],
            "evidence_lane": ["OFFLINE_OPRA_STANDARD_HISTORY"],
            "model_status": ["BASELINE_ONLY"],
            "source_quote_staleness_seconds": [60.0],
            "underlying_price": [100.0],
            "strike": [100.0],
            "multiplier": [100.0],
            "predictive_standard_deviation": [1.0],
            "constrained_fair_value": [2.0],
            "constrained_interval_95_lower": [1.0],
            "constrained_interval_95_upper": [3.0],
        }
    ).to_parquet(prediction_path, index=False)
    published = "2025-01-03T00:00:00Z"
    manifest = {
        "schema_version": "option-pricing-opra-causal-replay-v1",
        "provider": "databento-opra",
        "published_at": published,
        "input_files": [
            {
                "path": input_path.relative_to(root).as_posix(),
                "checksum_sha256": file_checksum(input_path),
            }
        ],
        "outputs": {
            prediction_path.name: {
                "row_count": 1,
                "checksum_sha256": file_checksum(prediction_path),
            }
        },
    }
    manifest_path = run / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt = {
        "schema_version": "option-pricing-opra-causal-replay-receipt-v1",
        "provider": "databento-opra",
        "published_at": published,
        "run_path": run.relative_to(root).as_posix(),
        "manifest_checksum_sha256": file_checksum(manifest_path),
    }
    receipt_path = run / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    pointer_path = root / "ml" / "option-pricing-opra-replay-latest" / "run.json"
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text(
        json.dumps(
            {
                "schema_version": "option-pricing-opra-causal-replay-pointer-v1",
                "current": {
                    "run_path": run.relative_to(root).as_posix(),
                    "receipt_checksum_sha256": file_checksum(receipt_path),
                    "published_at": published,
                },
            }
        ),
        encoding="utf-8",
    )

    catalog = load_strategy_pricing_evidence(
        root,
        available_not_after="2025-01-04T00:00:00Z",
    )

    assert len(catalog.predictions) == 1
    assert catalog.predictions.iloc[0]["source_provider"] == "databento-opra"
    assert catalog.predictions.iloc[0]["pricing_source"] == "BLACK_SCHOLES"
    assert prediction_path in catalog.source_files
