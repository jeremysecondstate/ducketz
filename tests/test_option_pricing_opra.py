from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import datafetching.databento_opra_history as opra_history
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
    TIMEOUT = 0

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
    assert (
        result["estimates"]["ohlcv-1d"]["estimated_download_size_bytes"]
        == 1_024
    )


def test_opra_scope_outside_plan_window_fails_instead_of_clamping(
    tmp_path: Path,
) -> None:
    with pytest.raises(opra_history.OpraSyncError, match="outside the configured included"):
        storage_preflight(
            SimpleNamespace(metadata=_Metadata()),
            datastore_root=tmp_path,
            entitlement=_entitlement(),
            scope=SyncScope(
                schemas=("ohlcv-1d",),
                start="2024-12-31",
                end="2025-01-03",
                symbols=("AAPL.OPT",),
            ),
        )


def test_standard_entitlement_uses_plan_windows_without_cost_probe(
    tmp_path: Path,
) -> None:
    class Metadata:
        TIMEOUT = 0

        def list_schemas(self, **_kwargs: object) -> tuple[str, ...]:
            return STANDARD_SCHEMAS

        def get_dataset_range(self, **_kwargs: object) -> dict[str, object]:
            return {
                "schema": {
                    schema: {"start": "2000-01-01", "end": "2026-08-15"}
                    for schema in STANDARD_SCHEMAS
                }
            }

    client = SimpleNamespace(metadata=Metadata(), timeseries=SimpleNamespace(TIMEOUT=0))
    result = opra_history.discover_standard_entitlement(
        client,
        datastore_root=tmp_path,
        observed_at="2026-08-15T00:00:00Z",
    )

    assert result["entitlements"]["definition"]["entitled_start"] == "2013-08-15"
    assert result["entitlements"]["cbbo-1s"]["entitled_start"] == "2025-08-15"
    assert result["entitlement_authority"].endswith(
        "databento_standard_plan_data_access.md"
    )


def test_symbol_bucket_is_stable_and_full_universe_is_explicit() -> None:
    assert symbol_bucket(()) == "full-universe"
    assert symbol_bucket(("NVDA.OPT", "AAPL.OPT")) == symbol_bucket(
        ("AAPL.OPT", "NVDA.OPT")
    )


def test_dense_cmbp_days_are_split_into_deterministic_intraday_partitions() -> None:
    calls: list[dict[str, object]] = []

    class Metadata:
        def get_record_count(self, **kwargs: object) -> int:
            calls.append(kwargs)
            duration = pd.Timestamp(str(kwargs["end"])) - pd.Timestamp(
                str(kwargs["start"])
            )
            return (
                opra_history.TARGET_HIGH_VOLUME_PARTITION_ROWS + 1
                if duration > pd.Timedelta(hours=12)
                else 100
            )

    segments = opra_history._partition_time_segments(
        SimpleNamespace(metadata=Metadata()),
        schema="cmbp-1",
        day="2026-08-14",
        symbols=("AAPL.OPT",),
    )

    assert segments == [
        (
            "2026-08-14T00:00:00+00:00",
            "2026-08-14T12:00:00+00:00",
            "000000-120000",
        ),
        (
            "2026-08-14T12:00:00+00:00",
            "2026-08-15T00:00:00+00:00",
            "120000-000000",
        ),
    ]
    assert len(calls) == 3
    assert all(call["symbols"] == ["AAPL.OPT"] for call in calls)


def test_low_volume_schema_keeps_one_daily_partition_without_metadata_probe() -> None:
    segments = opra_history._partition_time_segments(
        SimpleNamespace(),
        schema="definition",
        day="2026-08-14",
        symbols=("AAPL.OPT",),
    )
    assert segments == [
        (
            "2026-08-14T00:00:00+00:00",
            "2026-08-15T00:00:00+00:00",
            None,
        )
    ]


def test_parent_symbol_no_data_day_is_a_nonfatal_partition_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeSeries:
        def get_range(self, **_kwargs: object) -> object:
            raise RuntimeError(
                "422 symbology_invalid_request Could not resolve smart symbols: AAPL.OPT"
            )

    monkeypatch.setattr(opra_history.time, "sleep", lambda _seconds: None)
    entitlement = _entitlement()
    with pytest.raises(opra_history.OpraNoDataError):
        opra_history._download_partition(
            SimpleNamespace(timeseries=TimeSeries()),
            datastore_root=tmp_path,
            entitlement=entitlement,
            schema="definition",
            day="2025-01-01",
            symbols=("AAPL.OPT",),
        )


def test_cmbp_normalization_removes_only_exact_provider_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cmbp.parquet"
    rows = pd.DataFrame(
        {
            "ts_recv": [pd.Timestamp("2026-08-14T13:30:00Z")] * 3,
            "publisher_id": [30] * 3,
            "instrument_id": [123] * 3,
            "symbol": ["GOOG  260814C00200000"] * 3,
            "ts_event": [pd.Timestamp("2026-08-14T13:29:59.999Z")] * 3,
            "rtype": [177] * 3,
            "action": ["A"] * 3,
            "side": ["A"] * 3,
            "price": [7.35] * 3,
            "size": [2, 2, 3],
            "flags": [192] * 3,
            "ts_in_delta": [0] * 3,
            "bid_px_00": [6.5] * 3,
            "ask_px_00": [7.35] * 3,
            "bid_sz_00": [17] * 3,
            "ask_sz_00": [2, 2, 3],
            "bid_pb_00": [21] * 3,
            "ask_pb_00": [37] * 3,
        }
    ).set_index("ts_recv")
    rows.to_parquet(path)

    removed = opra_history._deduplicate_normalized_parquet(path, schema="cmbp-1")
    validation = opra_history.validate_parquet(path, schema="cmbp-1")

    assert removed == 1
    assert validation["row_count"] == 2
    assert validation["duplicate_natural_key_rows"] == 0
    assert validation["event_timestamp_column"] == "ts_event"
    assert validation["partition_timestamp_column"] == "ts_recv"
    assert validation["earliest_event_timestamp"] == "2026-08-14T13:29:59.999000+00:00"
    assert validation["earliest_partition_timestamp"] == "2026-08-14T13:30:00+00:00"


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
