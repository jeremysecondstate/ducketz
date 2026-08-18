from __future__ import annotations

import hashlib
import json
import warnings
import zipfile
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


class _NoParquetStore:
    def to_parquet(self, _path: Path, **_kwargs: object) -> None:
        return None


class _ProbeDBNStore:
    def __init__(
        self,
        *,
        dataset: str = opra_history.DATASET,
        schema: str = "ohlcv-1d",
        start: str = "2025-01-01",
        end: str = "2025-01-02",
        symbols: tuple[str, ...] = ("AAPL.OPT",),
        stype_in: str = "parent",
        records: tuple[object, ...] = (),
        parser_warning: str | None = None,
    ) -> None:
        self.dataset = dataset
        self.schema = schema
        self.start = pd.Timestamp(start, tz="UTC")
        self.end = pd.Timestamp(end, tz="UTC")
        self.symbols = list(symbols)
        self.stype_in = stype_in
        self.stype_out = "instrument_id"
        self.limit = None
        self.metadata = SimpleNamespace(
            version=1,
            partial=[],
            not_found=[],
            ts_out=False,
        )
        self._records = records
        self._parser_warning = parser_warning

    def __iter__(self) -> object:
        if self._parser_warning is not None:
            warnings.warn(self._parser_warning, RuntimeWarning)
        return iter(self._records)


_OPRA_COLLISION_MAPPINGS = {
    855638726: "NVDA  270319P00240000",
    855638733: "NVDA  270319P00115000",
    855638790: "NVDA  260605C00255000",
}


class _BatchCollisionStore(_ProbeDBNStore):
    def __init__(self, *, null_symbols: bool = True) -> None:
        super().__init__(
            start="2025-01-01",
            end="2025-01-02",
            symbols=("NVDA.OPT",),
        )
        self.metadata.partial = list(_OPRA_COLLISION_MAPPINGS.values())
        self.metadata.mappings = {
            raw_symbol: [
                {
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-02",
                    # Same low 24 bits, but the wrong OPRA channel (47 vs. 51).
                    "symbol": str(instrument_id - (4 << 24)),
                }
            ]
            for instrument_id, raw_symbol in _OPRA_COLLISION_MAPPINGS.items()
        }
        self._null_symbols = null_symbols

    def to_parquet(self, path: Path, **_kwargs: object) -> None:
        ids = [*_OPRA_COLLISION_MAPPINGS, 123]
        pd.DataFrame(
            {
                "ts_event": [pd.Timestamp("2025-01-01T00:00:00Z")] * len(ids),
                "publisher_id": [20, 22, 35, 30],
                "instrument_id": ids,
                "symbol": [
                    *(
                        [None] * len(_OPRA_COLLISION_MAPPINGS)
                        if self._null_symbols
                        else list(_OPRA_COLLISION_MAPPINGS.values())
                    ),
                    "NVDA  250117C00100000",
                ],
                "open": [1.0, 2.0, 3.0, 4.0],
                "high": [1.5, 2.5, 3.5, 4.5],
                "low": [0.5, 1.5, 2.5, 3.5],
                "close": [1.25, 2.25, 3.25, 4.25],
                "volume": [10, 20, 30, 40],
            }
        ).to_parquet(path, index=False)


class _PointInTimeDefinitionStore(_ProbeDBNStore):
    def __init__(
        self,
        *,
        interval_start: str = "2025-01-01",
        interval_end: str = "2025-01-02",
        omitted_ids: tuple[int, ...] = (),
    ) -> None:
        ids = tuple(str(value) for value in sorted(_OPRA_COLLISION_MAPPINGS))
        super().__init__(
            schema="definition",
            start="2025-01-01",
            end="2025-01-02",
            symbols=ids,
            stype_in="instrument_id",
        )
        self.metadata.mappings = {
            value: [
                {
                    "start_date": interval_start,
                    "end_date": interval_end,
                    "symbol": value,
                }
            ]
            for value in ids
        }
        self._omitted_ids = set(omitted_ids)

    def to_df(self, *, map_symbols: bool) -> pd.DataFrame:
        assert map_symbols is False
        rows = [
            {
                "ts_recv": pd.Timestamp("2025-01-01T10:30:01.6Z"),
                "ts_event": pd.Timestamp("2025-01-01T10:30:01.5Z"),
                "instrument_id": instrument_id,
                "raw_symbol": raw_symbol,
                "security_update_action": "A",
                "instrument_class": raw_symbol[12],
                "raw_instrument_id": instrument_id,
                "channel_id": 51,
                "asset": "NVDA",
            }
            for instrument_id, raw_symbol in _OPRA_COLLISION_MAPPINGS.items()
            if instrument_id not in self._omitted_ids
        ]
        return pd.DataFrame(rows).set_index("ts_recv")


def _publish_collision_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    definition_store: _PointInTimeDefinitionStore,
) -> tuple[dict[str, object], object]:
    payload = b"controlled-collision-provider-dbn"
    source = tmp_path / "source.dbn.zst"
    source.write_bytes(payload)
    market_store = _BatchCollisionStore()
    monkeypatch.setattr(opra_history, "_load_dbn_store", lambda _path: market_store)

    class TimeSeries:
        calls: list[dict[str, object]] = []

        def get_range(self, **kwargs: object) -> object:
            self.calls.append(dict(kwargs))
            Path(str(kwargs["path"])).write_bytes(b"controlled-definition-dbn")
            return definition_store

    timeseries = TimeSeries()
    filename = "opra-pillar-20250101.ohlcv-1d.dbn.zst"
    manifest = opra_history._download_partition(
        SimpleNamespace(timeseries=timeseries),
        datastore_root=tmp_path,
        entitlement=_entitlement(),
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=("NVDA.OPT",),
        provider_file=source,
        provider_delivery={
            "mode": "batch",
            "job_id": "OPRA-TEST-COLLISION",
            "filename": filename,
            "provider_hash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        },
    )
    return dict(manifest), timeseries


def _client_with_missing_parquet() -> object:
    class TimeSeries:
        def get_range(self, **kwargs: object) -> object:
            Path(str(kwargs["path"])).write_bytes(b"controlled-provider-dbn")
            return _NoParquetStore()

    return SimpleNamespace(timeseries=TimeSeries())


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
    published = opra_history.publish_storage_preflight(tmp_path, result)
    assert published["path"] == (
        tmp_path
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "metadata"
        / "preflights"
        / "ohlcv-1d"
        / "AAPL.OPT"
        / "2025-01-01_to_2025-01-03"
        / "preflight.json"
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
    assert result["path"] == (
        tmp_path
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "metadata"
        / "entitlement.json"
    )


def test_symbol_bucket_is_stable_and_full_universe_is_explicit() -> None:
    assert symbol_bucket(()) == "all-symbols"
    assert symbol_bucket(("NVDA.OPT", "AAPL.OPT")) == symbol_bucket(
        ("AAPL.OPT", "NVDA.OPT")
    )
    assert symbol_bucket(("NVDA.OPT", "AAPL.OPT")) == "AAPL.OPT_and_NVDA.OPT"


def test_opra_baseline_and_catchup_partitions_use_readable_stable_layout(
    tmp_path: Path,
) -> None:
    daily = opra_history.partition_directory(
        tmp_path,
        schema="cbbo-1s",
        day="2026-08-14",
        symbols=("AAPL.OPT",),
    )
    split = opra_history.partition_directory(
        tmp_path,
        schema="cmbp-1",
        day="2026-08-14",
        symbols=("AAPL.OPT",),
        segment="000000-120000",
    )
    root = (
        tmp_path
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
    )
    assert daily == (
        root
        / "cbbo-1s"
        / "AAPL.OPT"
        / "dates"
        / "2026-08-14"
        / "segments"
        / "full-day"
    )
    assert split == (
        root
        / "cmbp-1"
        / "AAPL.OPT"
        / "dates"
        / "2026-08-14"
        / "segments"
        / "000000-120000"
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


def test_readable_zero_record_dbn_without_parquet_is_no_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reopened_paths: list[Path] = []

    def load(path: Path) -> object:
        reopened_paths.append(path)
        assert path.read_bytes() == b"controlled-provider-dbn"
        return _ProbeDBNStore()

    monkeypatch.setattr(opra_history, "_load_dbn_store", load)

    with pytest.raises(
        opra_history.OpraNoDataError,
        match="provider returned a readable zero-record DBN",
    ):
        opra_history._download_partition(
            _client_with_missing_parquet(),
            datastore_root=tmp_path,
            entitlement=_entitlement(),
            schema="ohlcv-1d",
            day="2025-01-01",
            symbols=("AAPL.OPT",),
        )

    assert len(reopened_paths) == 1
    assert reopened_paths[0].is_file()
    assert not opra_history.partition_directory(
        tmp_path,
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=("AAPL.OPT",),
    ).exists()
    assert list(opra_history.canonical_root(tmp_path).glob(".staging/**/provider.dbn.zst"))


def test_nonempty_dbn_without_parquet_remains_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        opra_history,
        "_load_dbn_store",
        lambda _path: _ProbeDBNStore(records=(object(),)),
    )

    with pytest.raises(opra_history.OpraSyncError) as raised:
        opra_history._download_partition(
            _client_with_missing_parquet(),
            datastore_root=tmp_path,
            entitlement=_entitlement(),
            schema="ohlcv-1d",
            day="2025-01-01",
            symbols=("AAPL.OPT",),
        )

    assert type(raised.value) is opra_history.OpraSyncError
    assert "contains records" in str(raised.value)


@pytest.mark.parametrize("failure", ("malformed", "truncated"))
def test_invalid_dbn_without_parquet_remains_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "malformed":
        def load(_path: Path) -> object:
            raise ValueError("invalid DBN header")
    else:
        def load(_path: Path) -> object:
            return _ProbeDBNStore(parser_warning="DBN file is truncated")

    monkeypatch.setattr(opra_history, "_load_dbn_store", load)

    with pytest.raises(opra_history.OpraSyncError) as raised:
        opra_history._download_partition(
            _client_with_missing_parquet(),
            datastore_root=tmp_path,
            entitlement=_entitlement(),
            schema="ohlcv-1d",
            day="2025-01-01",
            symbols=("AAPL.OPT",),
        )

    assert type(raised.value) is opra_history.OpraSyncError
    assert "no Parquet" in str(raised.value)


def test_mismatched_dbn_without_parquet_remains_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        opra_history,
        "_load_dbn_store",
        lambda _path: _ProbeDBNStore(schema="trades"),
    )

    with pytest.raises(opra_history.OpraSyncError) as raised:
        opra_history._download_partition(
            _client_with_missing_parquet(),
            datastore_root=tmp_path,
            entitlement=_entitlement(),
            schema="ohlcv-1d",
            day="2025-01-01",
            symbols=("AAPL.OPT",),
        )

    assert type(raised.value) is opra_history.OpraSyncError
    assert "metadata does not match" in str(raised.value)


def test_batch_metadata_allows_partial_but_not_unresolved_symbology() -> None:
    request = {
        "dataset": opra_history.DATASET,
        "schema": "ohlcv-1d",
        "start": "2025-01-01",
        "end": "2025-01-02",
        "symbols": ["AAPL.OPT"],
        "stype_in": "parent",
    }
    store = _ProbeDBNStore()
    store.metadata.partial = ["AAPL  250117C00100000"]

    with pytest.raises(opra_history.OpraSyncError, match="metadata does not match"):
        opra_history._validate_dbn_request_metadata(store, request=request)
    assert (
        opra_history._validate_dbn_request_metadata(
            store,
            request=request,
            allow_partially_resolved_symbols=True,
        )
        == 1
    )

    store.metadata.not_found = ["AAPL  250117P00999999"]
    with pytest.raises(opra_history.OpraSyncError, match="metadata does not match"):
        opra_history._validate_dbn_request_metadata(
            store,
            request=request,
            allow_partially_resolved_symbols=True,
        )


def test_batch_daily_symbol_collision_uses_exact_day_definition_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, timeseries = _publish_collision_batch(
        tmp_path,
        monkeypatch,
        definition_store=_PointInTimeDefinitionStore(),
    )
    destination = opra_history.partition_directory(
        tmp_path,
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=("NVDA.OPT",),
    )
    normalized = pd.read_parquet(destination / "normalized.parquet")
    resolved = normalized.set_index("instrument_id")["symbol"].to_dict()

    assert normalized["symbol"].isna().sum() == 0
    assert {key: resolved[key] for key in _OPRA_COLLISION_MAPPINGS} == (
        _OPRA_COLLISION_MAPPINGS
    )
    assert len(timeseries.calls) == 1
    lookup = timeseries.calls[0]
    assert lookup["schema"] == "definition"
    assert lookup["stype_in"] == "instrument_id"
    assert lookup["stype_out"] == "instrument_id"
    assert lookup["start"] == "2025-01-01"
    assert lookup["end"] == "2025-01-02"
    delivery = manifest["provider_delivery"]
    assert delivery["partially_resolved_symbol_count"] == 3
    assert delivery["partially_resolved_symbols_checksum_sha256"]
    assert delivery["normalized_symbol_mapping"] == "complete"
    fallback = delivery["symbol_mapping_fallback"]
    assert fallback["method"] == "same-day-instrument-definition-v1"
    assert fallback["original_null_symbol_count"] == 3
    assert fallback["post_repair_null_symbol_count"] == 0
    assert fallback["affected_instrument_ids"] == sorted(_OPRA_COLLISION_MAPPINGS)
    assert (
        destination / fallback["source"]["path"]
    ).read_bytes() == b"controlled-definition-dbn"
    assert opra_history.verify_partition(
        destination,
        datastore_root=tmp_path,
    )["manifest"] == manifest


def test_batch_daily_definition_fallback_rejects_noncovering_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(opra_history.OpraSyncError) as raised:
        _publish_collision_batch(
            tmp_path,
            monkeypatch,
            definition_store=_PointInTimeDefinitionStore(
                interval_start="2025-01-02",
                interval_end="2025-01-03",
            ),
        )

    message = str(raised.value)
    assert "opra-pillar-20250101.ohlcv-1d.dbn.zst" in message
    assert "null_count=3" in message
    assert f"instrument_ids={sorted(_OPRA_COLLISION_MAPPINGS)}" in message
    assert "does not uniquely cover every normalized record timestamp" in message
    assert not opra_history.partition_directory(
        tmp_path,
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=("NVDA.OPT",),
    ).exists()


def test_batch_daily_unresolved_symbols_fail_without_placeholder_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    omitted = max(_OPRA_COLLISION_MAPPINGS)
    with pytest.raises(opra_history.OpraSyncError) as raised:
        _publish_collision_batch(
            tmp_path,
            monkeypatch,
            definition_store=_PointInTimeDefinitionStore(omitted_ids=(omitted,)),
        )

    message = str(raised.value)
    assert "opra-pillar-20250101.ohlcv-1d.dbn.zst" in message
    assert "null_count=3" in message
    assert f"instrument_ids={sorted(_OPRA_COLLISION_MAPPINGS)}" in message
    assert f"No same-day definition record exists for instrument_id={omitted}" in message
    retained = list(
        opra_history.canonical_root(tmp_path).glob(
            ".staging/ohlcv-1d/NVDA.OPT/2025-01-01/full-day/attempt-*/normalized.parquet"
        )
    )
    assert len(retained) == 1
    failed = pd.read_parquet(retained[0])
    assert failed["symbol"].isna().sum() == 3
    assert not failed["symbol"].fillna("").str.contains("UNKNOWN|UNMAPPED").any()


def test_streaming_symbol_nulls_remain_strict_and_never_use_batch_fallback(
    tmp_path: Path,
) -> None:
    market_store = _BatchCollisionStore()

    class TimeSeries:
        calls = 0

        def get_range(self, **kwargs: object) -> object:
            self.calls += 1
            Path(str(kwargs["path"])).write_bytes(b"controlled-stream-dbn")
            return market_store

    timeseries = TimeSeries()
    with pytest.raises(
        opra_history.OpraSyncError,
        match="normalized symbol mapping remains unresolved",
    ) as raised:
        opra_history._download_partition(
            SimpleNamespace(timeseries=timeseries),
            datastore_root=tmp_path,
            entitlement=_entitlement(),
            schema="ohlcv-1d",
            day="2025-01-01",
            symbols=("NVDA.OPT",),
        )

    assert timeseries.calls == 1
    assert "null_count=3" in str(raised.value)
    assert not opra_history.partition_directory(
        tmp_path,
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=("NVDA.OPT",),
    ).exists()


def test_synchronize_fail_fast_skips_no_data_and_continues_after_existing_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("AAPL.OPT",)
    existing = opra_history.partition_directory(
        tmp_path,
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=symbols,
    )
    existing.mkdir(parents=True)
    downloaded_days: list[str] = []
    reports: list[str] = []

    monkeypatch.setattr(
        opra_history,
        "_validate_storage_preflight_receipt",
        lambda *_args, **_kwargs: {"capacity_pass": True},
    )
    monkeypatch.setattr(
        opra_history,
        "_partition_plan",
        lambda *_args, **_kwargs: [
            ("ohlcv-1d", "2025-01-01"),
            ("ohlcv-1d", "2025-01-02"),
            ("ohlcv-1d", "2025-01-03"),
        ],
    )
    monkeypatch.setattr(
        opra_history,
        "verify_partition",
        lambda *_args, **_kwargs: {
            "manifest": {"normalized": {"row_count": 5, "size_bytes": 50}}
        },
    )

    def download(_client: object, **kwargs: object) -> object:
        day = str(kwargs["day"])
        downloaded_days.append(day)
        if day == "2025-01-02":
            raise opra_history.OpraNoDataError(
                "provider returned a readable zero-record DBN"
            )
        return {"normalized": {"row_count": 7, "size_bytes": 70}}

    monkeypatch.setattr(opra_history, "_download_partition", download)
    monkeypatch.setattr(opra_history, "_publish_cursor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        opra_history,
        "publish_health",
        lambda _root: tmp_path / "health.json",
    )
    client = SimpleNamespace(
        metadata=SimpleNamespace(TIMEOUT=0),
        timeseries=SimpleNamespace(TIMEOUT=0),
    )

    result = opra_history.synchronize(
        client,
        datastore_root=tmp_path,
        entitlement=_entitlement(),
        scope=SyncScope(
            schemas=("ohlcv-1d",),
            start="2025-01-01",
            end="2025-01-04",
            symbols=symbols,
        ),
        reporter=reports.append,
        storage_preflight_receipt={"controlled": True},
        fail_fast=True,
    )

    assert downloaded_days == ["2025-01-02", "2025-01-03"]
    assert result.status == "COMPLETE"
    assert result.completed_partitions == 1
    assert result.skipped_partitions == 2
    assert result.completed_rows == 12
    assert result.completed_bytes == 120
    assert result.errors == {}
    assert reports == [
        "VERIFIED_EXISTING ohlcv-1d/2025-01-01",
        "NO_DATA ohlcv-1d/2025-01-02 provider returned a readable zero-record DBN",
        "PUBLISHED ohlcv-1d/2025-01-03 rows=7 bytes=70",
    ]


def test_daily_batch_publishes_files_and_persists_no_data_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = tuple(f"2025-01-{value:02d}" for value in range(1, 31))
    data_days = (days[0], days[-1])
    payloads = {day: day.encode("ascii") for day in data_days}

    class BatchStore:
        def __init__(self, day: str) -> None:
            self.dataset = opra_history.DATASET
            self.schema = "ohlcv-1d"
            self.start = pd.Timestamp(day, tz="UTC")
            self.end = self.start + pd.Timedelta(days=1)
            self.symbols = ["AAPL.OPT"]
            self.stype_in = "parent"
            self.stype_out = "instrument_id"
            self.limit = None
            self.metadata = SimpleNamespace(
                version=1,
                partial=["AAPL  250117C00100000"],
                not_found=[],
                ts_out=False,
            )

        def to_parquet(self, path: Path, **_kwargs: object) -> None:
            pd.DataFrame(
                {
                    "ts_event": [self.start + pd.Timedelta(hours=21)],
                    "publisher_id": [1],
                    "instrument_id": [2],
                    "symbol": ["AAPL  250117C00100000"],
                    "open": [1.0],
                    "high": [1.5],
                    "low": [0.5],
                    "close": [1.25],
                    "volume": [10],
                }
            ).to_parquet(path, index=False)

    class Batch:
        submit_calls = 0
        download_calls = 0

        def submit_job(self, **kwargs: object) -> dict[str, object]:
            self.submit_calls += 1
            assert kwargs["split_duration"] == "day"
            assert kwargs["start"] == days[0]
            assert kwargs["end"] == "2025-01-31"
            return {"id": "OPRA-TEST-BATCH"}

        def get_job_details(self, job_id: str) -> dict[str, object]:
            return {"id": job_id, "state": "done", "record_count": 2, "progress": 100}

        def list_files(self, _job_id: str) -> list[dict[str, object]]:
            return [
                {
                    "filename": f"opra-pillar-{day.replace('-', '')}.ohlcv-1d.dbn.zst",
                    "size": len(payload),
                    "hash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                }
                for day, payload in payloads.items()
            ]

        def download(
            self,
            *,
            job_id: str,
            output_dir: Path,
            keep_zip: bool,
        ) -> list[Path]:
            self.download_calls += 1
            assert keep_zip is True
            archive = Path(output_dir) / job_id / f"{job_id}.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive, "w") as output:
                for day, payload in payloads.items():
                    output.writestr(
                        f"opra-pillar-{day.replace('-', '')}.ohlcv-1d.dbn.zst",
                        payload,
                    )
            return [archive]

    batch = Batch()
    client = SimpleNamespace(
        metadata=SimpleNamespace(TIMEOUT=0),
        timeseries=SimpleNamespace(TIMEOUT=0),
        batch=batch,
    )
    monkeypatch.setattr(
        opra_history,
        "_validate_storage_preflight_receipt",
        lambda *_args, **_kwargs: {"capacity_pass": True},
    )
    monkeypatch.setattr(
        opra_history,
        "_partition_plan",
        lambda *_args, **_kwargs: [("ohlcv-1d", day) for day in days],
    )
    monkeypatch.setattr(
        opra_history,
        "_load_dbn_store",
        lambda path: BatchStore(Path(path).read_text(encoding="ascii")),
    )

    first = opra_history.synchronize(
        client,
        datastore_root=tmp_path,
        entitlement=_entitlement(),
        scope=SyncScope(
            schemas=("ohlcv-1d",),
            start=days[0],
            end="2025-01-31",
            symbols=("AAPL.OPT",),
        ),
        reporter=None,
        storage_preflight_receipt={"controlled": True},
        fail_fast=True,
        batch_download=True,
        refresh_health=False,
    )

    assert first.completed_partitions == 2
    assert first.skipped_partitions == 28
    assert first.completed_rows == 2
    assert batch.submit_calls == 1
    assert batch.download_calls == 1
    states = list(
        opra_history.canonical_root(tmp_path).glob(
            "state/batch-jobs/ohlcv-1d/AAPL.OPT/*/job.json"
        )
    )
    assert len(states) == 1
    state = json.loads(states[0].read_text(encoding="utf-8"))
    assert state["status"] == "COMPLETE"
    assert state["published_dates"] == list(data_days)
    assert len(state["no_data_dates"]) == 28
    for day in data_days:
        destination = opra_history.partition_directory(
            tmp_path,
            schema="ohlcv-1d",
            day=day,
            symbols=("AAPL.OPT",),
        )
        verified = opra_history.verify_partition(destination, datastore_root=tmp_path)
        delivery = verified["manifest"]["provider_delivery"]
        assert delivery["mode"] == "batch"
        assert delivery["partially_resolved_symbol_count"] == 1
        assert delivery["normalized_symbol_mapping"] == "complete"

    second = opra_history.synchronize(
        client,
        datastore_root=tmp_path,
        entitlement=_entitlement(),
        scope=SyncScope(
            schemas=("ohlcv-1d",),
            start=days[0],
            end="2025-01-31",
            symbols=("AAPL.OPT",),
        ),
        reporter=None,
        storage_preflight_receipt={"controlled": True},
        fail_fast=True,
        batch_download=True,
        refresh_health=False,
    )
    assert second.completed_partitions == 0
    assert second.skipped_partitions == 30
    assert second.completed_rows == 2
    assert batch.submit_calls == 1
    assert batch.download_calls == 1


def test_daily_batch_streams_a_short_remaining_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = tuple(f"2025-01-{value:02d}" for value in range(1, 31))
    for day in days[:-1]:
        opra_history.partition_directory(
            tmp_path,
            schema="ohlcv-1d",
            day=day,
            symbols=("AAPL.OPT",),
        ).mkdir(parents=True)

    monkeypatch.setattr(
        opra_history,
        "verify_partition",
        lambda *_args, **_kwargs: {
            "manifest": {"normalized": {"row_count": 1, "size_bytes": 1}}
        },
    )
    streamed: list[tuple[str, str]] = []

    def execute_stream_plan(*_args: object, **kwargs: object) -> object:
        streamed.extend(kwargs["plan"])
        return opra_history._SyncProgress(
            completed_partitions=1,
            completed_rows=1,
            completed_bytes=1,
        )

    monkeypatch.setattr(opra_history, "_execute_stream_plan", execute_stream_plan)
    client = SimpleNamespace(
        batch=SimpleNamespace(
            submit_job=lambda **_kwargs: pytest.fail(
                "a one-day remaining gap must not enter the batch queue"
            )
        )
    )

    result = opra_history._synchronize_daily_batch(
        client,
        datastore_root=tmp_path,
        entitlement=_entitlement(),
        schema="ohlcv-1d",
        days=days,
        symbols=("AAPL.OPT",),
        reporter=None,
    )

    assert streamed == [("ohlcv-1d", days[-1])]
    assert result.completed_partitions == 1
    assert result.skipped_partitions == 29
    assert result.completed_rows == 30


def test_batch_inventory_ignores_unplanned_dates_inside_the_requested_range() -> None:
    payload = b"provider-data"
    provider_hash = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    accepted, ignored = opra_history._batch_data_file_inventory(
        [
            {
                "filename": "opra-pillar-20240603.ohlcv-1d.dbn.zst",
                "size": len(payload),
                "hash": provider_hash,
            },
            {
                "filename": "opra-pillar-20240604.ohlcv-1d.dbn.zst",
                "size": len(payload),
                "hash": provider_hash,
            },
        ],
        planned_dates=("2024-06-04",),
        request_start="2024-06-03",
        request_end="2024-06-05",
    )

    assert [item["day"] for item in accepted] == ["2024-06-04"]
    assert [item["day"] for item in ignored] == ["2024-06-03"]


def test_batch_inventory_rejects_files_outside_the_requested_range() -> None:
    payload = b"provider-data"
    with pytest.raises(opra_history.OpraSyncError, match="outside its request range"):
        opra_history._batch_data_file_inventory(
            [
                {
                    "filename": "opra-pillar-20240602.ohlcv-1d.dbn.zst",
                    "size": len(payload),
                    "hash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                }
            ],
            planned_dates=("2024-06-03",),
            request_start="2024-06-03",
            request_end="2024-06-05",
        )


def test_batch_interrupt_resumes_the_same_job_without_resubmission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = tuple(f"2025-01-{value:02d}" for value in range(1, 31))

    class Batch:
        ready = False
        submit_calls = 0

        def submit_job(self, **_kwargs: object) -> dict[str, object]:
            self.submit_calls += 1
            return {"id": "OPRA-INTERRUPT-BATCH"}

        def get_job_details(self, job_id: str) -> dict[str, object]:
            return {
                "id": job_id,
                "state": "done" if self.ready else "processing",
                "record_count": 0 if self.ready else None,
                "progress": 100 if self.ready else 25,
            }

        def list_files(self, _job_id: str) -> list[dict[str, object]]:
            return [{"filename": "metadata.json", "size": 2, "hash": "sha256:00"}]

        def download(self, **_kwargs: object) -> list[Path]:
            pytest.fail("zero-record batch must not download an archive")

    batch = Batch()
    client = SimpleNamespace(
        metadata=SimpleNamespace(TIMEOUT=0),
        timeseries=SimpleNamespace(TIMEOUT=0),
        batch=batch,
    )
    monkeypatch.setattr(
        opra_history,
        "_validate_storage_preflight_receipt",
        lambda *_args, **_kwargs: {"capacity_pass": True},
    )
    monkeypatch.setattr(
        opra_history,
        "_partition_plan",
        lambda *_args, **_kwargs: [("ohlcv-1d", day) for day in days],
    )
    monkeypatch.setattr(
        opra_history.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    kwargs = {
        "datastore_root": tmp_path,
        "entitlement": _entitlement(),
        "scope": SyncScope(
            schemas=("ohlcv-1d",),
            start=days[0],
            end="2025-01-31",
            symbols=("AAPL.OPT",),
        ),
        "reporter": None,
        "storage_preflight_receipt": {"controlled": True},
        "fail_fast": True,
        "batch_download": True,
        "refresh_health": False,
    }
    with pytest.raises(KeyboardInterrupt):
        opra_history.synchronize(client, **kwargs)
    assert batch.submit_calls == 1

    batch.ready = True
    result = opra_history.synchronize(client, **kwargs)
    assert result.completed_partitions == 0
    assert result.skipped_partitions == 30
    assert batch.submit_calls == 1


def test_nonempty_dbn_publication_path_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        def to_parquet(self, path: Path, **_kwargs: object) -> None:
            pd.DataFrame(
                {
                    "ts_event": [pd.Timestamp("2025-01-01T21:00:00Z")],
                    "publisher_id": [1],
                    "instrument_id": [2],
                    "symbol": ["AAPL  250117C00100000"],
                    "open": [1.0],
                    "high": [1.5],
                    "low": [0.5],
                    "close": [1.25],
                    "volume": [10],
                }
            ).to_parquet(path, index=False)

    class TimeSeries:
        def get_range(self, **kwargs: object) -> object:
            Path(str(kwargs["path"])).write_bytes(b"controlled-nonempty-dbn")
            return Store()

    monkeypatch.setattr(
        opra_history,
        "_load_dbn_store",
        lambda _path: pytest.fail("nonempty publication must not use the empty probe"),
    )

    manifest = opra_history._download_partition(
        SimpleNamespace(timeseries=TimeSeries()),
        datastore_root=tmp_path,
        entitlement=_entitlement(),
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=("AAPL.OPT",),
    )
    destination = opra_history.partition_directory(
        tmp_path,
        schema="ohlcv-1d",
        day="2025-01-01",
        symbols=("AAPL.OPT",),
    )

    assert manifest["normalized"]["row_count"] == 1
    assert manifest["normalized"]["duplicate_natural_key_rows"] == 0
    assert (destination / "provider.dbn.zst").read_bytes() == b"controlled-nonempty-dbn"
    assert (destination / "normalized.parquet").is_file()
    assert opra_history.verify_partition(
        destination,
        datastore_root=tmp_path,
    )["manifest"] == manifest


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
