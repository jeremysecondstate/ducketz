from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from app.services.databento_cme_context import DatabentoCmeContextSpec
from datafetching import databento_fetch
from datafetching.cme_cross_asset_context import (
    CME_CONTEXT_CALCULATION_VERSION,
    CME_CONTEXT_COLUMNS,
    CME_CONTEXT_NAME,
    CmeCrossAssetNotReady,
    CmeCrossAssetQualityError,
    calculate_cme_cross_asset_context,
    cme_cross_asset_context_path,
    materialize_cme_cross_asset_context,
)
from datafetching.parquet_store import ParquetStore
from datafetching.cme_history import (
    cme_normalized_event_paths,
    persist_cme_event_history,
)

_WINDOW_START = pd.Timestamp("2026-07-29T17:00:00Z")
_CALCULATED_AT = pd.Timestamp("2026-07-29T18:02:00Z")
_ROOT_PRICES = {
    "NQ": (100.0, 110.0),
    "ES": (100.0, 105.0),
    "RTY": (100.0, 108.0),
    "GC": (100.0, 102.0),
    "CL": (-2.0, 1.0),
}


def test_cme_context_uses_exact_common_window_and_maximum_availability(
    tmp_path: Path,
) -> None:
    bars, bbo, mbp = _source_frames()

    calculated = calculate_cme_cross_asset_context(
        bars,
        bbo,
        mbp,
        calculated_at=_CALCULATED_AT,
    )

    assert calculated.columns.tolist() == list(CME_CONTEXT_COLUMNS)
    assert len(calculated) == 1
    row = calculated.iloc[0]
    assert row["window_start"] == _WINDOW_START
    assert row["window_end"] == _WINDOW_START + pd.Timedelta(hours=1)
    assert row["observed_at"] == pd.Timestamp("2026-07-29T18:00:00Z")
    assert row["fetched_at"] == pd.Timestamp("2026-07-29T18:01:00Z")
    assert row["available_at"] == _CALCULATED_AT
    assert row["nq_return"] == pytest.approx(math.log(1.10))
    assert row["es_return"] == pytest.approx(math.log(1.05))
    assert row["rty_minus_es_return"] == pytest.approx(
        math.log(1.08) - math.log(1.05)
    )
    assert row["nq_minus_es_return"] == pytest.approx(
        math.log(1.10) - math.log(1.05)
    )
    assert row["gold_return"] == pytest.approx(math.log(1.02))
    assert row["crude_return"] == pytest.approx(math.log(2.5))
    assert row["relative_spread"] == pytest.approx(0.02)
    assert row["book_imbalance"] == pytest.approx(0.20)
    assert bool(row["constituent_complete"])
    assert not bool(row["source_stale"])

    _write_sources(tmp_path, bars, bbo, mbp)
    output_path = materialize_cme_cross_asset_context(
        tmp_path,
        calculated_at=_CALCULATED_AT,
    )

    assert output_path == cme_cross_asset_context_path(tmp_path)
    assert output_path == (
        tmp_path
        / "pools"
        / "cme"
        / "features"
        / "cross-asset-context"
        / "databento"
        / "1h.parquet"
    )
    stored = pd.read_parquet(output_path)
    assert stored.columns[0] == "id"
    assert stored.columns.tolist().count("id") == 1
    assert stored["id"].item() == (
        f"{CME_CONTEXT_NAME}|2026-07-29T18:00:00Z|"
        f"{CME_CONTEXT_CALCULATION_VERSION}"
    )
    assert (
        materialize_cme_cross_asset_context(
            tmp_path,
            calculated_at=_CALCULATED_AT + pd.Timedelta(minutes=1),
        )
        is None
    )


def test_cme_context_rejects_incomplete_and_limit_saturated_book() -> None:
    bars, bbo, mbp = _source_frames()
    incomplete = bars.loc[
        ~(
            bars["provider_symbol"].eq("RTY.v.0")
            & bars["timestamp"].eq(pd.Timestamp("2026-07-29T17:27:00Z"))
        )
    ]
    with pytest.raises(CmeCrossAssetNotReady, match="60 exact common"):
        calculate_cme_cross_asset_context(
            incomplete,
            bbo,
            mbp,
            calculated_at=_CALCULATED_AT,
        )

    saturated = mbp.copy()
    saturated["request_limit_saturated"] = True
    with pytest.raises(CmeCrossAssetQualityError, match="limit-saturated"):
        calculate_cme_cross_asset_context(
            bars,
            bbo,
            saturated,
            calculated_at=_CALCULATED_AT,
        )


def test_cme_context_rejects_stale_book_and_future_ending_window() -> None:
    bars, bbo, mbp = _source_frames()
    with pytest.raises(CmeCrossAssetQualityError, match="stale"):
        calculate_cme_cross_asset_context(
            bars,
            bbo,
            mbp,
            calculated_at=pd.Timestamp("2026-07-29T18:16:00Z"),
        )

    with pytest.raises(CmeCrossAssetQualityError, match="future-ending"):
        calculate_cme_cross_asset_context(
            bars,
            bbo,
            mbp,
            calculated_at=pd.Timestamp("2026-07-29T17:59:59Z"),
        )


def test_cme_context_rejects_receive_or_receipt_after_calculation() -> None:
    bars, bbo, mbp = _source_frames()
    future_receipt = bbo.copy()
    future_receipt["fetched_at"] = pd.Timestamp("2026-07-29T18:03:00Z")

    with pytest.raises(
        CmeCrossAssetQualityError,
        match="after calculation completion",
    ):
        calculate_cme_cross_asset_context(
            bars,
            future_receipt,
            mbp,
            calculated_at=_CALCULATED_AT,
        )


def test_databento_fetch_materializes_only_after_source_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = pd.Timestamp("2026-07-29T17:00:00Z").to_pydatetime()
    spec = DatabentoCmeContextSpec(
        group_key="context",
        output_symbol="CME_CONTEXT",
        symbols=("NQ.v.0",),
        dataset="GLBX.MDP3",
        schema="ohlcv-1m",
        stype_in="continuous",
        start=start,
        end=(pd.Timestamp(start) + pd.Timedelta(minutes=1)).to_pydatetime(),
        limit=None,
    )

    class _Provider:
        def specs(self) -> tuple[DatabentoCmeContextSpec, ...]:
            return (spec,)

        def fetch_cme_context(
            self,
            _: DatabentoCmeContextSpec,
        ) -> tuple[list[dict[str, object]], None, DatabentoCmeContextSpec]:
            return (
                [
                    {
                        "symbol": "NQ.v.0",
                        "timestamp": "2026-07-29T17:00:00Z",
                        "fetched_at": "2026-07-29T17:01:00Z",
                    }
                ],
                None,
                spec,
            )

    class _Store:
        root_dir = tmp_path
        source_persisted = False

        def save_macro_rows(self, *_: object, **__: object) -> Path:
            self.source_persisted = True
            return tmp_path / "source.parquet"

        def save_raw_frame(self, *_: object, **__: object) -> None:
            return None

        def save_error(self, *_: object, **__: object) -> None:
            pytest.fail("post-persistence materialization should not fail")

    store = _Store()

    def _materialize(root: Path) -> Path:
        assert root == tmp_path
        assert store.source_persisted
        return tmp_path / "calculated.parquet"

    monkeypatch.setattr(
        databento_fetch,
        "DatabentoCmeContextProvider",
        _Provider,
    )
    monkeypatch.setattr(
        databento_fetch,
        "materialize_cme_cross_asset_context",
        _materialize,
    )

    assert databento_fetch._fetch_cme(store) == (3, 0, 0)


def test_cme_persistence_failure_records_group_schema_request_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = _WINDOW_START.to_pydatetime()
    spec = DatabentoCmeContextSpec(
        group_key="contracts",
        output_symbol="CME_CONTRACTS",
        symbols=("NQU6",),
        dataset="GLBX.MDP3",
        schema="bbo-1m",
        stype_in="raw_symbol",
        start=start,
        end=(_WINDOW_START + pd.Timedelta(minutes=5)).to_pydatetime(),
        limit=5_000,
        latest_event_timestamp="2026-07-29T17:01:22Z",
    )
    rows = [
        {
            "symbol": "NQU6",
            "timestamp": "2026-07-29T17:01:22Z",
            "ts_event": "2026-07-29T17:01:22Z",
            "sequence": 101,
            "side": "A",
            "price": 27_620.25,
            "fetched_at": "2026-07-29T17:01:24Z",
        }
    ]

    class _Provider:
        def specs(self) -> tuple[DatabentoCmeContextSpec, ...]:
            return (spec,)

        def fetch_cme_context(
            self,
            _: DatabentoCmeContextSpec,
        ) -> tuple[list[dict[str, object]], None, DatabentoCmeContextSpec]:
            return rows, None, spec

    class _Store:
        root_dir = tmp_path

        def __init__(self) -> None:
            self.errors: list[dict[str, object]] = []

        def save_macro_rows(self, *_: object, **__: object) -> None:
            raise RuntimeError("simulated persistence failure")

        def save_error(self, **kwargs: object) -> Path:
            self.errors.append(kwargs)
            return tmp_path / "recorded-cme-error.parquet"

    def _not_ready(_: Path) -> None:
        raise CmeCrossAssetNotReady("no complete window")

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", _Provider)
    monkeypatch.setattr(
        databento_fetch,
        "materialize_cme_cross_asset_context",
        _not_ready,
    )
    store = _Store()

    assert databento_fetch._fetch_cme(store) == (1, 1, 0)
    assert len(store.errors) == 1
    error = store.errors[0]
    target = (
        tmp_path
        / "pools"
        / "cme"
        / "CME_CONTRACTS"
        / "cme_contracts_bbo-1m"
        / "databento"
        / "normalized"
        / "CME_CONTRACTS_cme_contracts_bbo-1m.parquet"
    )
    assert error["symbol"] == "CME_CONTRACTS"
    assert error["category"] == "macro"
    assert error["request_key"] == "cme_contracts_bbo-1m"
    assert error["pool"] == "cme"
    message = str(error["error_message"])
    assert "group=contracts" in message
    assert "schema=bbo-1m" in message
    assert "request_key=cme_contracts_bbo-1m" in message
    assert f"target={target}" in message
    metadata = error["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["cme_context_group"] == "contracts"
    assert metadata["provider_schema"] == "bbo-1m"
    assert metadata["persistence_stage"] == "normalized"
    assert metadata["persistence_target_file"] == str(target)
    assert metadata["target_schema"] == "<missing>"
    assert metadata["incoming_row_count"] == 1
    incoming_schema = json.loads(str(metadata["incoming_schema"]))
    assert incoming_schema["timestamp"] in {"object", "str", "string"}
    assert "ts_event" in incoming_schema


def test_cme_local_quality_skip_is_advisory_not_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Provider:
        def specs(self) -> tuple[()]:
            return ()

    def _quality_skip(_: Path) -> None:
        raise CmeCrossAssetQualityError("optional context is limit-saturated")

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", _Provider)
    monkeypatch.setattr(
        databento_fetch,
        "materialize_cme_cross_asset_context",
        _quality_skip,
    )

    outcome = databento_fetch._fetch_cme(ParquetStore(tmp_path))

    assert outcome == (0, 0, 1)
    advisories = list(tmp_path.rglob("diagnostics/*.parquet"))
    assert len(advisories) == 1
    stored = pd.read_parquet(advisories[0])
    assert stored["severity"].tolist() == ["advisory"]
    assert stored["advisory_type"].tolist() == [
        "CmeCrossAssetQualityError"
    ]


def _source_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    fetched_at = pd.Timestamp("2026-07-29T18:01:00Z")
    timestamps = pd.date_range(
        _WINDOW_START,
        periods=60,
        freq="1min",
        tz="UTC",
    )
    bar_rows: list[dict[str, object]] = []
    for root, (start_price, end_price) in _ROOT_PRICES.items():
        for index, timestamp in enumerate(timestamps):
            close = end_price if index == len(timestamps) - 1 else start_price
            bar_rows.append(
                {
                    "provider_symbol": f"{root}.v.0",
                    "provider_stype_in": "continuous",
                    "timestamp": timestamp,
                    "timeframe": "1m",
                    "open": start_price,
                    "high": max(start_price, close) + 1.0,
                    "low": min(start_price, close) - 1.0,
                    "close": close,
                    "volume": 100.0,
                    "fetched_at": fetched_at,
                }
            )

    bbo_rows = [
        {
            "provider_symbol": f"{root}.v.0",
            "provider_stype_in": "continuous",
            "timestamp": pd.Timestamp("2026-07-29T17:59:00Z"),
            "ts_recv": pd.Timestamp("2026-07-29T17:59:00.100000Z"),
            "bid_px_00": 99.0,
            "ask_px_00": 101.0,
            "fetched_at": fetched_at,
        }
        for root in _ROOT_PRICES
    ]
    mbp_rows: list[dict[str, object]] = []
    for root in _ROOT_PRICES:
        for side, size in (("B", 60.0), ("A", 40.0)):
            mbp_rows.append(
                {
                    "provider_symbol": f"{root}.v.0",
                    "provider_stype_in": "continuous",
                    "timestamp": pd.Timestamp("2026-07-29T17:59:50Z"),
                    "ts_recv": pd.Timestamp(
                        "2026-07-29T17:59:50.100000Z"
                    ),
                    "side": side,
                    "depth": 0,
                    "size": size,
                    "request_limit_saturated": False,
                    "fetched_at": fetched_at,
                }
            )
    return (
        pd.DataFrame(bar_rows),
        pd.DataFrame(bbo_rows),
        pd.DataFrame(mbp_rows),
    )


@pytest.mark.parametrize("saturated", [False, True])
def test_inline_cme_updates_partitioned_context_with_provider_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saturated: bool,
) -> None:
    schemas = ("ohlcv-1m", "bbo-1m", "mbp-10")
    sources = dict(zip(schemas, _source_frames()))
    specs = {}
    for schema, frame in sources.items():
        frame["symbol"] = frame["provider_symbol"]
        spec = DatabentoCmeContextSpec(
            group_key="context", output_symbol="CME_CONTEXT",
            symbols=tuple(frame["symbol"].unique()), dataset="GLBX.MDP3",
            schema=schema, stype_in="continuous", limit=5_000,
            start=_WINDOW_START.to_pydatetime(),
            end=(_WINDOW_START + pd.Timedelta(hours=1)).to_pydatetime(),
            limit_saturated=saturated and schema == "mbp-10",
        )
        specs[schema] = spec
        stale = frame.copy()
        for column in ("timestamp", "ts_recv", "fetched_at"):
            if column in stale:
                stale[column] -= pd.Timedelta(days=1)
        persist_cme_event_history(
            tmp_path, spec=spec, normalized_rows=stale.to_dict("records"),
            raw_frame=None,
        )
    sources["mbp-10"]["request_limit_saturated"] = saturated

    class _Provider:
        def specs(self):
            return tuple(specs.values())

        def fetch_cme_context(self, spec):
            return sources[spec.schema].to_dict("records"), None, spec

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", _Provider)
    monkeypatch.setattr(
        databento_fetch, "materialize_cme_cross_asset_context",
        lambda root: materialize_cme_cross_asset_context(
            root, calculated_at=_CALCULATED_AT,
        ),
    )
    outcome = databento_fetch._fetch_cme(ParquetStore(tmp_path))
    assert outcome[1:] == (0, int(saturated))
    for schema, expected in sources.items():
        paths = cme_normalized_event_paths(tmp_path, group_key="context", schema=schema)
        stored = pd.concat([pd.read_parquet(path) for path in paths])
        fresh = stored.loc[stored.timestamp.ge(_WINDOW_START)]
        if saturated and schema == "mbp-10":
            assert fresh.empty
            continue
        assert len(fresh) == len(expected)
        assert set(fresh.provider_symbol) == set(expected.provider_symbol)
        assert "CME_CONTEXT" not in set(fresh.provider_symbol)
        for column in ("timestamp", "ts_recv", "fetched_at"):
            if column in expected:
                assert sorted(fresh[column]) == sorted(expected[column])
        if schema == "mbp-10":
            assert fresh.request_limit_saturated.eq(saturated).all()
        # Identical recapture uses native deduplication and preserves receipts.
        before = {path: path.read_bytes() for path in paths}
        replay = expected.copy()
        replay["fetched_at"] += pd.Timedelta(minutes=1)
        persisted = persist_cme_event_history(
            tmp_path, spec=specs[schema],
            normalized_rows=replay.to_dict("records"), raw_frame=None,
        )
        assert persisted.written == 0
        assert all(path.read_bytes() == content for path, content in before.items())
    output = cme_cross_asset_context_path(tmp_path)
    if saturated:
        assert not output.exists()
        diagnostic = next(tmp_path.rglob("diagnostics/*.parquet"))
        assert pd.read_parquet(diagnostic).advisory_message.str.contains("limit-saturated").all()
    else:
        stored = pd.read_parquet(output)
        assert len(stored) == 1
        assert stored.window_end.item() == _WINDOW_START + pd.Timedelta(hours=1)
        assert stored.available_at.item() == _CALCULATED_AT
        assert stored.nq_return.item() == pytest.approx(math.log(1.1))


def test_inline_cme_history_failure_retains_hot_evidence_and_skips_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, _, _ = _source_frames()
    bars["symbol"] = bars["provider_symbol"]
    spec = DatabentoCmeContextSpec(
        group_key="context", output_symbol="CME_CONTEXT",
        symbols=tuple(bars.symbol.unique()), dataset="GLBX.MDP3",
        schema="ohlcv-1m", stype_in="continuous", limit=5_000,
        start=_WINDOW_START.to_pydatetime(),
        end=(_WINDOW_START + pd.Timedelta(hours=1)).to_pydatetime(),
    )

    class _Provider:
        def specs(self):
            return (spec,)

        def fetch_cme_context(self, requested):
            return bars.to_dict("records"), bars.copy(), requested

    def _failure(*args, **kwargs):
        raise OSError("simulated event-history write failure")

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", _Provider)
    monkeypatch.setattr(databento_fetch, "persist_cme_event_history", _failure)
    monkeypatch.setattr(
        databento_fetch, "materialize_cme_cross_asset_context",
        lambda root: pytest.fail("partial durable history must not materialize"),
    )
    assert databento_fetch._fetch_cme(ParquetStore(tmp_path)) == (2, 1, 0)
    assert len(list(tmp_path.rglob("normalized/*.parquet"))) == 1
    assert len(list(tmp_path.rglob("raw/*.parquet"))) == 1
    error_path = next(tmp_path.rglob("errors/**/*.parquet"))
    error = pd.read_parquet(error_path).iloc[0]
    assert error.persistence_stage == "event_history"
    assert "simulated event-history write failure" in error.error_message
    assert not cme_cross_asset_context_path(tmp_path).exists()


def test_inline_saturated_recapture_preserves_complete_history_and_blocks_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, book = _source_frames()
    book["symbol"] = book["provider_symbol"]
    spec = DatabentoCmeContextSpec(
        group_key="context", output_symbol="CME_CONTEXT",
        symbols=tuple(book.symbol.unique()), dataset="GLBX.MDP3",
        schema="mbp-10", stype_in="continuous", limit=5_000,
        start=_WINDOW_START.to_pydatetime(),
        end=(_WINDOW_START + pd.Timedelta(hours=1)).to_pydatetime(),
        limit_saturated=True,
    )
    persisted = persist_cme_event_history(
        tmp_path, spec=replace(spec, limit_saturated=False),
        normalized_rows=book.to_dict("records"), raw_frame=None,
    )
    before = {path: path.read_bytes() for path in persisted.paths}
    book["request_limit_saturated"] = True

    class _Provider:
        def specs(self):
            return (spec,)

        def fetch_cme_context(self, requested):
            return book.to_dict("records"), book.copy(), requested

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", _Provider)
    monkeypatch.setattr(
        databento_fetch, "persist_cme_event_history",
        lambda *args, **kwargs: pytest.fail("capped capture must not enter shared history"),
    )
    monkeypatch.setattr(
        databento_fetch, "materialize_cme_cross_asset_context",
        lambda root: pytest.fail("current capped capture must block materialization"),
    )
    assert databento_fetch._fetch_cme(ParquetStore(tmp_path)) == (2, 0, 1)
    assert all(path.read_bytes() == content for path, content in before.items())
    hot = next(tmp_path.rglob("CME_CONTEXT/*/databento/normalized/*.parquet"))
    assert pd.read_parquet(hot).request_limit_saturated.all()


@pytest.mark.parametrize("failure_stage", ["fetch", "normalized", "raw"])
def test_inline_cme_capture_error_never_materializes_partial_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    bars, _, _ = _source_frames()
    bars["symbol"] = bars["provider_symbol"]
    spec = DatabentoCmeContextSpec(
        group_key="context", output_symbol="CME_CONTEXT",
        symbols=tuple(bars.symbol.unique()), dataset="GLBX.MDP3",
        schema="ohlcv-1m", stype_in="continuous", limit=5_000,
        start=_WINDOW_START.to_pydatetime(),
        end=(_WINDOW_START + pd.Timedelta(hours=1)).to_pydatetime(),
    )

    class _Provider:
        def specs(self):
            return (spec,)

        def fetch_cme_context(self, requested):
            if failure_stage == "fetch":
                raise ValueError("simulated fetch failure")
            return bars.to_dict("records"), bars.copy(), requested

    class _Store(ParquetStore):
        def save_macro_rows(self, *args, **kwargs):
            if failure_stage == "normalized":
                raise OSError("simulated normalized failure")
            return super().save_macro_rows(*args, **kwargs)

        def save_raw_frame(self, *args, **kwargs):
            if failure_stage == "raw":
                raise OSError("simulated raw failure")
            return super().save_raw_frame(*args, **kwargs)

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", _Provider)
    monkeypatch.setattr(
        databento_fetch, "call_with_persistent_databento_retry",
        lambda call, **kwargs: call(),
    )
    monkeypatch.setattr(
        databento_fetch, "materialize_cme_cross_asset_context",
        lambda root: pytest.fail("partial source capture must not materialize"),
    )
    result = databento_fetch._fetch_cme(_Store(tmp_path))
    assert result == (0 if failure_stage == "fetch" else 3, 1, 0)
    errors = list(tmp_path.rglob("errors/**/*.parquet"))
    assert len(errors) == 1
    assert pd.read_parquet(errors[0]).error_message.str.contains(
        f"simulated {failure_stage} failure"
    ).all()
    assert not cme_cross_asset_context_path(tmp_path).exists()


@pytest.mark.parametrize("failure_stage", ["fetch", "normalized", "raw", "history", "saturated"])
def test_inline_contracts_failure_does_not_block_valid_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    frames = dict(zip(("ohlcv-1m", "bbo-1m", "mbp-10"), _source_frames()))
    specs = []
    for schema, frame in frames.items():
        frame["symbol"] = frame.provider_symbol
        specs.append(DatabentoCmeContextSpec(
            group_key="context", output_symbol="CME_CONTEXT",
            symbols=tuple(frame.symbol.unique()), dataset="GLBX.MDP3",
            schema=schema, stype_in="continuous", limit=5_000,
            start=_WINDOW_START.to_pydatetime(),
            end=(_WINDOW_START + pd.Timedelta(hours=1)).to_pydatetime(),
        ))
    contract_spec = replace(
        specs[1], group_key="contracts", output_symbol="CME_CONTRACTS",
        symbols=("NQU6",), stype_in="raw_symbol",
        limit_saturated=failure_stage == "saturated",
    )
    contracts = frames["bbo-1m"].iloc[:1].copy()
    contracts["symbol"] = contracts["provider_symbol"] = "NQU6"
    contracts["provider_stype_in"] = "raw_symbol"
    contracts["request_limit_saturated"] = contract_spec.limit_saturated

    class _Provider:
        def specs(self):
            return (contract_spec, *specs)

        def fetch_cme_context(self, spec):
            if spec.group_key == "contracts":
                if failure_stage == "fetch":
                    raise ValueError("simulated contracts fetch failure")
                return contracts.to_dict("records"), contracts.copy(), spec
            return frames[spec.schema].to_dict("records"), None, spec

    class _Store(ParquetStore):
        def save_macro_rows(self, *args, **kwargs):
            if args[1] == "CME_CONTRACTS" and failure_stage == "normalized":
                raise OSError("simulated contracts normalized failure")
            return super().save_macro_rows(*args, **kwargs)

        def save_raw_frame(self, *args, **kwargs):
            if kwargs["symbol"] == "CME_CONTRACTS" and failure_stage == "raw":
                raise OSError("simulated contracts raw failure")
            return super().save_raw_frame(*args, **kwargs)

    def _persist(root, *, spec, **kwargs):
        if spec.group_key == "contracts" and failure_stage == "history":
            raise OSError("simulated contracts history failure")
        return persist_cme_event_history(root, spec=spec, **kwargs)

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", _Provider)
    monkeypatch.setattr(databento_fetch, "persist_cme_event_history", _persist)
    monkeypatch.setattr(databento_fetch, "call_with_persistent_databento_retry", lambda call, **kwargs: call())
    monkeypatch.setattr(
        databento_fetch, "materialize_cme_cross_asset_context",
        lambda root: materialize_cme_cross_asset_context(root, calculated_at=_CALCULATED_AT),
    )
    result = databento_fetch._fetch_cme(_Store(tmp_path))
    assert result[1:] == ((0, 1) if failure_stage == "saturated" else (1, 0))
    published = pd.read_parquet(cme_cross_asset_context_path(tmp_path))
    assert len(published) == 1
    assert published.window_end.item() == _WINDOW_START + pd.Timedelta(hours=1)
    assert published.nq_return.item() == pytest.approx(math.log(1.1))
    if failure_stage == "saturated":
        assert not cme_normalized_event_paths(tmp_path, group_key="contracts", schema="bbo-1m")


def _write_sources(
    root: Path,
    bars: pd.DataFrame,
    bbo: pd.DataFrame,
    mbp: pd.DataFrame,
) -> None:
    datasets = {
        "cme_context_ohlcv-1m": bars,
        "cme_context_bbo-1m": bbo,
        "cme_context_mbp-10": mbp,
    }
    for dataset, frame in datasets.items():
        path = (
            root
            / "pools"
            / "cme"
            / "CME_CONTEXT"
            / dataset
            / "databento"
            / "normalized"
            / f"CME_CONTEXT_{dataset}.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
