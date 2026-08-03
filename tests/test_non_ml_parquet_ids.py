from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

import datafetching.databento_fetch as databento_fetch
from app.models.market_data import MarketQuote
from app.services.databento_cme_context import (
    DatabentoCmeContextProvider,
    DatabentoCmeContextSpec,
)
from datafetching.bar_timing import annotate_bar_timing
from datafetching.parquet_store import ParquetStore, _infer_keys
from fundamentals.parquet_io import write_fundamental_parquet
from options.snapshot import _atomic_upsert
from signals.parquet_io import write_signal_parquet
from technicals.parquet_io import BarDataset, write_technical_parquet


def test_raw_provider_identifiers_survive_beside_ducketz_id(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    path = store.save_raw_frame(
        source="databento",
        category="trades",
        symbol="GOOG",
        endpoint="timesales",
        frame=pd.DataFrame(
            {
                "id": ["native-record-A", "native-record-B"],
                "instrument_id": [991, 992],
                "publisher_id": [12, 12],
                "ts_event": [
                    pd.Timestamp("2026-07-29T15:01:02.123456Z"),
                    pd.Timestamp("2026-07-29T15:01:02.123456Z"),
                ],
                "price": [198.25, 198.30],
            }
        ),
    )

    assert path is not None
    stored = pd.read_parquet(path)
    assert stored.columns[0] == "id"
    assert stored["id"].tolist() == ["native-record-A", "native-record-B"]
    assert stored["provider_native_identifier"].tolist() == [
        "native-record-A",
        "native-record-B",
    ]
    assert stored["instrument_id"].tolist() == [991, 992]
    assert stored["publisher_id"].tolist() == [12, 12]
    assert stored["ts_event"].nunique() == 1


def test_provider_literal_id_is_the_raw_natural_key_when_no_clock_exists(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    path = store.save_raw_frame(
        source="example",
        category="reference",
        symbol="GOOG",
        endpoint="instruments",
        frame=pd.DataFrame(
            {
                "id": ["native-A", "native-B"],
                "symbol": ["GOOG", "GOOG"],
                "name": ["Alphabet A", "Alphabet B"],
            }
        ),
    )

    assert path is not None
    updated_path = store.save_raw_frame(
        source="example",
        category="reference",
        symbol="GOOG",
        endpoint="instruments",
        frame=pd.DataFrame(
            {
                "id": ["native-A"],
                "symbol": ["GOOG"],
                "name": ["Alphabet A updated"],
            }
        ),
    )
    assert updated_path == path
    stored = pd.read_parquet(path)
    assert stored["id"].tolist() == ["native-A", "native-B"]
    assert stored["provider_native_identifier"].tolist() == [
        "native-A",
        "native-B",
    ]
    assert stored.loc[
        stored["provider_native_identifier"].eq("native-A"), "name"
    ].item() == (
        "Alphabet A updated"
    )
    assert len(stored) == 2


def test_fundamental_technical_signal_and_option_writers_add_readable_ids(
    tmp_path: Path,
) -> None:
    fundamental_path = write_fundamental_parquet(
        tmp_path,
        symbol="GOOG",
        period_type="quarterly",
        source="fmp",
        frame=pd.DataFrame(
            {
                "period_end_date": [pd.Timestamp("2026-03-31")],
                "fiscal_period": ["Q1"],
                "fundamental_score": [72.5],
                "observation_id": ["obsolete-observation"],
                "content_hash": ["obsolete-hash"],
                "content_identity": ["obsolete-identity"],
                "checksum_sha256": ["f" * 64],
                "sampleId": ["obsolete-sample"],
                "eventUUID": ["550e8400-e29b-41d4-a716-446655440000"],
                "contentHash": ["obsolete-camel-hash"],
                "coordination_generation": ["obsolete-generation"],
                "coordination_status": ["READY"],
                "loop_a_cycle_generation": ["obsolete-cycle"],
                "loop_a_cycle_status": ["WRITING"],
            }
        ),
        source_files=(),
    )
    fundamental = pd.read_parquet(fundamental_path)
    assert fundamental.columns[0] == "id"
    assert fundamental["id"].tolist() == ["2026-03-31T00:00:00Z"]
    assert "observation_id" not in fundamental
    assert "content_hash" not in fundamental
    assert "content_identity" not in fundamental
    assert "checksum_sha256" not in fundamental
    assert "sampleId" not in fundamental
    assert "eventUUID" not in fundamental
    assert "contentHash" not in fundamental
    assert "coordination_generation" not in fundamental
    assert "coordination_status" not in fundamental
    assert "loop_a_cycle_generation" not in fundamental
    assert "loop_a_cycle_status" not in fundamental

    bars = annotate_bar_timing(
        pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-07-28T00:00:00Z")],
                "open": [195.0],
                "high": [200.0],
                "low": [194.0],
                "close": [198.0],
                "volume": [1_000_000.0],
            }
        ),
        timeframe="1d",
        as_of=pd.Timestamp("2026-07-30T00:00:00Z"),
    )
    technical_path = write_technical_parquet(
        tmp_path / "technicals",
        calculation="market-regime",
        dataset=BarDataset(
            provider="schwab",
            timeframe="1d",
            symbol="GOOG",
            frame=bars,
            source_files=(),
            adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
            split_event_count=0,
            split_events_json="[]",
        ),
        frame=pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-07-28T00:00:00Z")],
                "trend_score": [63.0],
                "source_snapshot_id": ["obsolete-snapshot"],
            }
        ),
    )
    technical = pd.read_parquet(technical_path)
    assert technical.columns[0] == "id"
    assert technical["id"].tolist() == ["2026-07-28T00:00:00Z"]
    assert "source_snapshot_id" not in technical

    signal_path = write_signal_parquet(
        tmp_path / "signals",
        frame=pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-07-28T23:59:59Z")],
                "lifecycle_phase": ["CONFIRMED_EXPANSION"],
                "evaluation_policy_id": ["obsolete-policy"],
            }
        ),
    )
    signal = pd.read_parquet(signal_path)
    assert signal.columns[0] == "id"
    assert signal["id"].tolist() == ["2026-07-28T23:59:59Z"]
    assert "evaluation_policy_id" not in signal

    option_path = tmp_path / "options" / "contracts.parquet"
    _atomic_upsert(
        option_path,
        pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-07-29T15:30:00Z")],
                "contract_symbol": ["GOOG  260731C00200000"],
                "mark": [2.5],
                "sample_id": ["obsolete-sample"],
            }
        ),
        keys=("timestamp", "contract_symbol"),
    )
    options = pd.read_parquet(option_path)
    assert options.columns[0] == "id"
    assert options["id"].tolist() == [
        "2026-07-29T15:30:00Z|GOOG  260731C00200000"
    ]
    assert "sample_id" not in options


def test_normalized_and_error_store_outputs_have_one_readable_ducketz_id(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    normalized_path = store.save_corporate_rows(
        "fmp",
        "GOOG",
        "income_statement_quarterly",
        [
            {
                "date": "2026-03-31",
                "period": "Q1",
                "calendarYear": "2026",
                "revenue": 90_000_000_000,
                "source_publication_id": "obsolete-publication",
            }
        ],
    )
    error_path = store.save_error(
        source="fmp",
        category="corporate",
        symbol="GOOG",
        request_key="income_statement_quarterly",
        error_type="Timeout",
        error_message="provider did not respond",
    )
    quote_path = store.save_quote(
        MarketQuote(
            symbol="GOOG",
            source="schwab",
            fetched_at=pd.Timestamp("2026-07-29T15:45:00Z").to_pydatetime(),
            bid=198.0,
            ask=198.1,
        )
    )

    assert normalized_path is not None
    assert error_path is not None
    assert quote_path is not None
    normalized = pd.read_parquet(normalized_path)
    errors = pd.read_parquet(error_path)
    quotes = pd.read_parquet(quote_path)
    assert normalized.columns.tolist().count("id") == 1
    assert normalized["id"].tolist() == ["2026-03-31"]
    assert "source_publication_id" not in normalized
    assert errors.columns.tolist().count("id") == 1
    assert errors["id"].is_unique
    assert errors["id"].str.endswith("Z").all()
    assert quotes["id"].tolist() == ["2026-07-29T15:45:00Z"]


def test_versioned_statement_write_backfills_legacy_availability(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    path = store.save_corporate_rows(
        "fmp",
        "MU",
        "income_statement_annual",
        [
            {
                "date": "2024-08-29",
                "period": "FY",
                "fetched_at": "2026-07-01T12:00:00Z",
                "revenue": 90.0,
            },
            {
                "date": "2025-08-28",
                "period": "FY",
                "fetched_at": "2026-07-01T12:00:00Z",
                "revenue": 100.0,
            }
        ],
        keys=("date", "period"),
    )
    assert path is not None

    updated_path = store.save_corporate_rows(
        "fmp",
        "MU",
        "income_statement_annual",
        [
            {
                "date": "2024-08-29",
                "period": "FY",
                "fetched_at": "2026-07-30T05:57:54Z",
                "available_at": "2026-07-30T05:57:54Z",
                "revenue": 90.0,
            },
            {
                "date": "2025-08-28",
                "period": "FY",
                "fetched_at": "2026-07-30T05:57:54Z",
                "available_at": "2026-07-30T05:57:54Z",
                "revenue": 110.0,
            }
        ],
        keys=("date", "period", "available_at"),
        mode="append_if_revised",
    )

    assert updated_path == path
    stored = pd.read_parquet(path).sort_values("available_at").reset_index(
        drop=True
    )
    assert stored["available_at"].tolist() == [
        pd.Timestamp("2026-07-01T12:00:00Z"),
        pd.Timestamp("2026-07-01T12:00:00Z"),
        pd.Timestamp("2026-07-30T05:57:54Z"),
    ]
    assert stored["revenue"].tolist() == [90.0, 100.0, 110.0]
    assert set(stored["id"]) == {
        "2024-08-29|FY|2026-07-01T12:00:00Z",
        "2025-08-28|FY|2026-07-01T12:00:00Z",
        "2025-08-28|FY|2026-07-30T05:57:54Z",
    }
    assert stored["id"].is_unique


def test_unchanged_statement_refetch_retains_first_receipt(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    keys = ("date", "period", "available_at")
    first_rows = [
        {
            "date": "2024-08-29",
            "period": "FY",
            "fetched_at": "2026-07-01T12:00:00Z",
            "available_at": "2026-07-01T12:00:00Z",
            "revenue": 90.0,
        },
        {
            "date": "2025-08-28",
            "period": "FY",
            "fetched_at": "2026-07-01T12:00:00Z",
            "available_at": "2026-07-01T12:00:00Z",
            "revenue": 100.0,
        },
    ]
    path = store.save_corporate_rows(
        "fmp",
        "MU",
        "income_statement_annual",
        first_rows,
        keys=keys,
        mode="append_if_revised",
    )
    assert path is not None

    unchanged_rows = [
        {
            **row,
            "fetched_at": "2026-07-30T05:57:54Z",
            "available_at": "2026-07-30T05:57:54Z",
        }
        for row in first_rows
    ]
    assert (
        store.save_corporate_rows(
            "fmp",
            "MU",
            "income_statement_annual",
            unchanged_rows,
            keys=keys,
            mode="append_if_revised",
        )
        is None
    )

    stored = pd.read_parquet(path)
    assert len(stored) == 2
    assert stored["available_at"].eq(
        pd.Timestamp("2026-07-01T12:00:00Z")
    ).all()
    assert stored["revenue"].tolist() == [90.0, 100.0]
    assert stored["id"].is_unique


def test_raw_rows_use_minimum_readable_keys_without_collapsing_symbols(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    first = pd.DataFrame(
        {
            "symbol": ["GOOG", "MSFT"],
            "ts_event": ["2026-07-29T15:01:02Z"] * 2,
            "price": [198.25, 512.50],
        }
    )
    path = store.save_raw_frame(
        source="example",
        category="trades",
        symbol="MULTI",
        endpoint="timesales",
        frame=first,
    )
    assert path is not None

    store.save_raw_frame(
        source="example",
        category="trades",
        symbol="MULTI",
        endpoint="timesales",
        frame=pd.DataFrame(
            {
                "symbol": ["GOOG"],
                "ts_event": ["2026-07-29T15:01:02Z"],
                "price": [199.00],
            }
        ),
    )
    stored = pd.read_parquet(path)
    assert stored["id"].tolist() == [
        "GOOG|2026-07-29T15:01:02Z",
        "MSFT|2026-07-29T15:01:02Z",
    ]
    assert len(stored) == 2
    assert stored.loc[stored["symbol"].eq("GOOG"), "price"].item() == 199.00


def test_databento_mbp_rows_use_book_event_values_for_readable_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_timestamp = pd.Timestamp("2026-07-29T17:01:22.486829957Z")
    receive_timestamp = pd.Timestamp("2026-07-29T17:01:22.487159889Z")
    records = pd.DataFrame(
        {
            "ts_recv": [receive_timestamp] * 4,
            "rtype": [10] * 4,
            "publisher_id": [1] * 4,
            "instrument_id": [12_345] * 4,
            "action": ["T", "C", "T", "T"],
            "side": ["A", "B", "A", "A"],
            "depth": [0, 0, 0, 0],
            "price": [27_620.50, 27_620.50, 27_620.25, 27_620.25],
            "size": [1, 1, 2, 2],
            "flags": [0, 128, 0, 0],
            "sequence": [359_962_893] * 4,
            "symbol": ["NQ.v.0"] * 4,
        },
        index=pd.DatetimeIndex([event_timestamp] * 4, name="ts_event"),
    )

    class _FakeStore:
        def to_df(self) -> pd.DataFrame:
            return records.copy()

    class _FakeTimeseries:
        def get_range(self, **_: object) -> _FakeStore:
            return _FakeStore()

    class _FakeClient:
        timeseries = _FakeTimeseries()

    provider = DatabentoCmeContextProvider(
        api_key="test-key",
        dataset="GLBX.MDP3",
        schemas=("mbp-10",),
        context_symbols=("NQ.v.0",),
        contract_symbols=(),
        chunk_days=None,
    )
    monkeypatch.setattr(provider, "_client", lambda: _FakeClient())
    start = datetime(2026, 7, 29, 17, 1, 20, tzinfo=timezone.utc)
    spec = DatabentoCmeContextSpec(
        group_key="context",
        output_symbol="CME_CONTEXT",
        symbols=("NQ.v.0",),
        dataset="GLBX.MDP3",
        schema="mbp-10",
        stype_in="continuous",
        start=start,
        end=start + timedelta(seconds=5),
        limit=None,
    )

    rows, raw_frame, effective_spec = provider.fetch_cme_context(spec)

    assert len(raw_frame) == 3
    assert len(rows) == 3
    assert raw_frame["symbol"].tolist() == [row["symbol"] for row in rows]

    store = ParquetStore(tmp_path)
    normalized_path = store.save_macro_rows(
        "databento",
        effective_spec.symbol,
        effective_spec.key,
        rows,
        pool="cme",
    )
    raw_path = store.save_raw_frame(
        source="databento",
        category="macro",
        symbol=effective_spec.symbol,
        endpoint=f"{effective_spec.key}_raw",
        dataset_key=effective_spec.key,
        frame=raw_frame,
        pool="cme",
    )

    assert normalized_path is not None
    assert raw_path is not None
    normalized = pd.read_parquet(normalized_path)
    raw = pd.read_parquet(raw_path)
    expected_ids = [
        "NQ.v.0|2026-07-29T17:01:22.486829957Z|359962893|T|A|0|27620.5",
        "NQ.v.0|2026-07-29T17:01:22.486829957Z|359962893|C|B|0|27620.5",
        "NQ.v.0|2026-07-29T17:01:22.486829957Z|359962893|T|A|0|27620.25",
    ]
    assert set(normalized["id"]) == set(expected_ids)
    assert set(raw["id"]) == set(expected_ids)
    assert normalized.columns.tolist().count("id") == 1
    assert raw.columns.tolist().count("id") == 1
    assert "instrument_id" not in normalized.columns
    assert "publisher_id" not in normalized.columns
    assert {"instrument_id", "publisher_id"}.issubset(raw.columns)

    updated_rows = [dict(row) for row in rows]
    updated_rows[1]["size"] = 3
    updated_rows[1]["fetched_at"] = "2026-07-29T17:02:00+00:00"
    updated_raw = raw_frame.copy()
    updated_raw.loc[
        updated_raw["action"].eq("C"),
        "size",
    ] = 3
    assert store.save_macro_rows(
        "databento",
        effective_spec.symbol,
        effective_spec.key,
        updated_rows,
        pool="cme",
    ) == normalized_path
    assert store.save_raw_frame(
        source="databento",
        category="macro",
        symbol=effective_spec.symbol,
        endpoint=f"{effective_spec.key}_raw",
        dataset_key=effective_spec.key,
        frame=updated_raw,
        pool="cme",
    ) == raw_path

    normalized = pd.read_parquet(normalized_path)
    raw = pd.read_parquet(raw_path)
    assert len(normalized) == 3
    assert len(raw) == 3
    assert set(normalized["id"]) == set(expected_ids)
    assert set(raw["id"]) == set(expected_ids)
    assert normalized.loc[normalized["action"].eq("C"), "size"].item() == 3
    assert raw.loc[raw["action"].eq("C"), "size"].item() == 3
    for column in ("timestamp", "ts_event", "ts_recv", "fetched_at"):
        assert str(normalized[column].dtype) == "datetime64[ns, UTC]"
    for column in ("ts_event", "ts_recv"):
        assert str(raw[column].dtype) == "datetime64[ns, UTC]"
    assert store.save_macro_rows(
        "databento",
        effective_spec.symbol,
        effective_spec.key,
        updated_rows,
        pool="cme",
    ) is None
    assert store.save_raw_frame(
        source="databento",
        category="macro",
        symbol=effective_spec.symbol,
        endpoint=f"{effective_spec.key}_raw",
        dataset_key=effective_spec.key,
        frame=updated_raw,
        pool="cme",
    ) is None


def test_databento_bbo_rows_without_events_publish_status_and_remain_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receive_timestamp = pd.Timestamp("2026-08-02T12:01:00Z")
    records = pd.DataFrame(
        {
            "ts_event": [pd.NaT],
            "rtype": [33],
            "publisher_id": [1],
            "instrument_id": [42_004_177],
            "side": ["N"],
            "price": [float("nan")],
            "size": [0],
            "flags": [128],
            "sequence": [2_577],
            "bid_px_00": [28_275.00],
            "ask_px_00": [28_314.75],
            "symbol": ["NQ.v.0"],
        },
        index=pd.DatetimeIndex([receive_timestamp], name="ts_recv"),
    )
    provider = DatabentoCmeContextProvider(
        api_key="test-key",
        dataset="GLBX.MDP3",
        schemas=("bbo-1m",),
        context_symbols=("NQ.v.0",),
        contract_symbols=(),
        limit=5_000,
    )
    start = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    spec = DatabentoCmeContextSpec(
        group_key="context",
        output_symbol="CME_CONTEXT",
        symbols=("NQ.v.0",),
        dataset="GLBX.MDP3",
        schema="bbo-1m",
        stype_in="continuous",
        start=start,
        end=start + timedelta(minutes=2),
        limit=5_000,
    )
    backtracked = replace(
        spec,
        latest_event_timestamp="",
        availability_status="BACKTRACKED",
    )
    monkeypatch.setattr(provider, "_client", lambda: object())
    monkeypatch.setattr(provider, "_request_specs", lambda _: (spec,))
    monkeypatch.setattr(
        provider,
        "_fetch_latest_frame_for_spec",
        lambda *_: (backtracked, records.copy()),
    )

    rows, raw_frame, effective_spec = provider.fetch_cme_context(spec)

    assert len(raw_frame) == 1
    assert pd.isna(raw_frame.loc[0, "ts_event"])
    assert effective_spec.latest_event_timestamp == ""
    assert len(rows) == 1
    assert rows[0]["cme_row_kind"] == "schema_status"

    store = ParquetStore(tmp_path)
    status_path = store.save_macro_rows(
        "databento",
        effective_spec.symbol,
        f"{effective_spec.key}_status",
        rows,
        pool="cme",
        mode="snapshot",
    )
    raw_path = store.save_raw_frame(
        source="databento",
        category="macro",
        symbol=effective_spec.symbol,
        endpoint=f"{effective_spec.key}_raw",
        dataset_key=effective_spec.key,
        frame=raw_frame,
        pool="cme",
    )
    assert status_path is not None
    assert raw_path is not None
    stored_status = pd.read_parquet(status_path)
    assert stored_status["cme_row_kind"].tolist() == ["schema_status"]
    stored_raw = pd.read_parquet(raw_path)
    assert stored_raw["ts_event"].isna().all()


def test_cme_bbo_upsert_normalizes_non_key_temporal_columns_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    path = store.target_path(
        scope="normalized",
        source="databento",
        category="macro",
        symbol="CME_CONTRACTS",
        suffix="cme_contracts_bbo-1m",
        dataset_key="cme_contracts_bbo-1m",
        pool="cme",
    )
    path.parent.mkdir(parents=True)
    existing_event = pd.Timestamp("2026-07-29T17:01:20.123456789Z")
    pd.DataFrame(
        [
            _bbo_row(
                timestamp=existing_event,
                ts_event=existing_event,
                sequence=100,
                price=27_620.00,
            )
        ]
    ).to_parquet(path, index=False)
    assert pq.read_schema(path).field("timestamp").type == pq.read_schema(
        path
    ).field("ts_event").type

    shared_event = pd.Timestamp("2026-07-29T17:01:22Z")
    epoch_seconds = int(shared_event.timestamp())
    incoming = [
        _bbo_row(
            timestamp=epoch_seconds,
            ts_event=shared_event.isoformat(),
            sequence=101,
            price=27_620.25,
        ),
        _bbo_row(
            timestamp=shared_event.isoformat(),
            ts_event=shared_event.isoformat(),
            sequence=102,
            price=27_620.50,
        ),
    ]
    inferred = _infer_keys(
        pd.DataFrame(
            [
                {**row, "request_key": "cme_contracts_bbo-1m"}
                for row in incoming
            ]
        ),
        "macro",
    )
    assert inferred == ("symbol", "ts_event", "sequence", "side", "price")
    assert "timestamp" not in inferred

    assert store.save_macro_rows(
        "databento",
        "CME_CONTRACTS",
        "cme_contracts_bbo-1m",
        incoming,
        pool="cme",
    ) == path
    assert store.save_macro_rows(
        "databento",
        "CME_CONTRACTS",
        "cme_contracts_bbo-1m",
        incoming,
        pool="cme",
    ) is None

    stored = pd.read_parquet(path)
    schema = pq.read_schema(path)
    assert len(stored) == 3
    assert stored["id"].is_unique
    assert stored["timestamp"].tolist() == [
        existing_event,
        shared_event,
        shared_event,
    ]
    for column in ("timestamp", "ts_event", "ts_recv", "fetched_at"):
        assert str(stored[column].dtype) == "datetime64[ns, UTC]"
        assert str(schema.field(column).type) == "timestamp[ns, tz=UTC]"


def test_invalid_non_key_temporal_value_aborts_upsert_without_data_loss(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    path = store.save_macro_rows(
        "databento",
        "CME_CONTRACTS",
        "cme_contracts_bbo-1m",
        [
            _bbo_row(
                timestamp="2026-07-29T17:01:20Z",
                ts_event="2026-07-29T17:01:20Z",
                sequence=100,
                price=27_620.00,
            )
        ],
        pool="cme",
    )
    assert path is not None
    before = path.read_bytes()

    with pytest.raises(
        ValueError,
        match="incoming temporal column 'timestamp'.*invalid non-null",
    ):
        store.save_macro_rows(
            "databento",
            "CME_CONTRACTS",
            "cme_contracts_bbo-1m",
            [
                _bbo_row(
                    timestamp="not-a-timestamp",
                    ts_event="2026-07-29T17:01:21Z",
                    sequence=101,
                    price=27_620.25,
                )
            ],
            pool="cme",
        )

    assert path.read_bytes() == before
    stored = pd.read_parquet(path)
    assert len(stored) == 1
    assert stored["timestamp"].notna().all()
    assert not path.with_suffix(".tmp.parquet").exists()


def _bbo_row(
    *,
    timestamp: object,
    ts_event: object,
    sequence: int,
    price: float,
) -> dict[str, object]:
    return {
        "symbol": "NQU6",
        "source": "databento",
        "timestamp": timestamp,
        "ts_event": ts_event,
        "ts_recv": "2026-07-29T17:01:23Z",
        "fetched_at": "2026-07-29T17:01:24Z",
        "sequence": sequence,
        "side": "A",
        "price": price,
        "size": 1,
    }


def test_cme_no_rows_status_is_a_separate_latest_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 7, 29, 17, 1, 20, tzinfo=timezone.utc)
    spec = DatabentoCmeContextSpec(
        group_key="context",
        output_symbol="CME_CONTEXT",
        symbols=("NQ.v.0",),
        dataset="GLBX.MDP3",
        schema="mbp-10",
        stype_in="continuous",
        start=start,
        end=start + timedelta(seconds=5),
        limit=5_000,
    )
    status_spec = replace(
        spec,
        availability_status="NO CURRENT ROWS",
    )
    responses = iter(
        (
            (
                [
                    {
                        "symbol": "NQ.v.0",
                        "timestamp": "2026-07-29T17:01:21Z",
                        "fetched_at": "2026-07-29T17:01:30Z",
                        "price": 27_620.25,
                    },
                    {
                        "symbol": "NQ.v.0",
                        "timestamp": "2026-07-29T17:01:22Z",
                        "fetched_at": "2026-07-29T17:01:30Z",
                        "price": 27_620.50,
                    },
                ],
                pd.DataFrame(),
                spec,
            ),
            (
                [
                    {
                        "symbol": "CME_CONTEXT",
                        "fetched_at": "2026-07-29T17:02:00Z",
                        "timestamp": "2026-07-29T17:02:00Z",
                        "cme_row_kind": "schema_status",
                    }
                ],
                pd.DataFrame(),
                status_spec,
            ),
            (
                [
                    {
                        "symbol": "CME_CONTEXT",
                        "fetched_at": "2026-07-29T17:03:00Z",
                        "timestamp": "2026-07-29T17:03:00Z",
                        "cme_row_kind": "schema_status",
                    }
                ],
                pd.DataFrame(),
                status_spec,
            ),
        )
    )

    class _FakeProvider:
        def specs(self) -> tuple[DatabentoCmeContextSpec, ...]:
            return (spec,)

        def fetch_cme_context(
            self,
            _: DatabentoCmeContextSpec,
        ) -> tuple[list[dict[str, object]], pd.DataFrame, DatabentoCmeContextSpec]:
            return next(responses)

    monkeypatch.setattr(
        databento_fetch,
        "DatabentoCmeContextProvider",
        _FakeProvider,
    )
    store = ParquetStore(tmp_path)

    databento_fetch._fetch_cme(store)
    databento_fetch._fetch_cme(store)
    databento_fetch._fetch_cme(store)

    event_path = next(
        tmp_path.rglob("CME_CONTEXT_cme_context_mbp-10.parquet")
    )
    status_path = next(
        tmp_path.rglob("CME_CONTEXT_cme_context_mbp-10_status.parquet")
    )
    events = pd.read_parquet(event_path)
    status = pd.read_parquet(status_path)
    assert len(events) == 2
    assert events["id"].tolist() == [
        "2026-07-29T17:01:21Z",
        "2026-07-29T17:01:22Z",
    ]
    assert len(status) == 1
    assert status["id"].tolist() == ["2026-07-29T17:03:00Z"]


def test_opaque_provider_id_remains_raw_data_but_is_not_the_ducketz_id(
    tmp_path: Path,
) -> None:
    opaque = "a" * 64
    store = ParquetStore(tmp_path)
    path = store.save_raw_frame(
        source="example",
        category="trades",
        symbol="GOOG",
        endpoint="timesales",
        frame=pd.DataFrame(
            {
                "id": [opaque],
                "ts_event": ["2026-07-29T15:01:02Z"],
                "price": [198.25],
            }
        ),
    )

    assert path is not None
    stored = pd.read_parquet(path)
    assert stored["provider_native_identifier"].tolist() == [opaque]
    assert stored["id"].tolist() == ["2026-07-29T15:01:02Z"]


def test_normalized_rows_without_a_natural_key_fail_instead_of_using_row_numbers(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    with pytest.raises(
        ValueError,
        match="requires at least one natural column",
    ):
        store.save_corporate_rows(
            "example",
            "GOOG",
            "unkeyed",
            [{"value": 1.0}, {"value": 2.0}],
        )
    with pytest.raises(
        ValueError,
        match="requires at least one natural column",
    ):
        store.save_raw_frame(
            source="example",
            category="other",
            symbol="GOOG",
            endpoint="unkeyed",
            frame=pd.DataFrame({"value": [1.0, 2.0]}),
        )


def test_unregistered_raw_id_columns_are_rejected(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    with pytest.raises(
        ValueError,
        match="not registered for example: sample_id",
    ):
        store.save_raw_frame(
            source="example",
            category="trades",
            symbol="GOOG",
            endpoint="timesales",
            frame=pd.DataFrame(
                {
                    "sample_id": ["internal-looking-value"],
                    "timestamp": ["2026-07-29T15:01:02Z"],
                }
            ),
        )


def test_provider_id_allowlist_is_source_specific(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    with pytest.raises(
        ValueError,
        match="not registered for fred: instrument_id",
    ):
        store.save_raw_frame(
            source="fred",
            category="macro",
            symbol="GDP",
            endpoint="GDP",
            frame=pd.DataFrame(
                {
                    "timestamp": ["2026-07-29T15:01:02Z"],
                    "instrument_id": [11667],
                    "value": [1.0],
                }
            ),
        )


def test_unregistered_raw_hash_identity_columns_are_rejected(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    with pytest.raises(
        ValueError,
        match="not registered for example",
    ):
        store.save_raw_frame(
            source="example",
            category="trades",
            symbol="GOOG",
            endpoint="timesales",
            frame=pd.DataFrame(
                {
                    "timestamp": ["2026-07-29T15:01:02Z"],
                    "content_hash": ["opaque"],
                    "lineage_hash": ["opaque"],
                    "checksum_sha256": ["opaque"],
                    "sampleId": ["opaque"],
                    "eventUUID": ["opaque"],
                    "contentHash": ["opaque"],
                }
            ),
        )
